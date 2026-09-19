"""Cross-language reader guard adapter: the surface a Rust daemon and this reader share.

The protocol is frozen in ``axiom-specs`` (``contracts/native-reader-writer-guards.md``) and is
consumed, not restated, by :mod:`axiom_mcp.guard.protocol`. This module adds the two things the
*other* language needs in order to interoperate with the Python side without re-deriving them:

* :func:`abi_descriptor` - one machine-readable description of the frozen surface (lock files,
  roles, primitives, byte range, open mode, share mode, acquisition and release order, bounded
  wait, crash release) in the field names the contract itself uses, so a foreign implementation
  or a conformance harness reads it instead of guessing; :func:`abi_json` and
  :func:`abi_sha256` pin exactly what this build declares.
* the holder *process contract* - the argv, the one-JSON-line stdout events and the exit codes
  that make exclusion observable between two independent processes.
  :func:`resolve_holder_argv` returns the implementation of that contract for this run: the
  in-repo :mod:`axiom_mcp.guard.interop` probe by default, or an external ABI-conformant holder
  (a Rust daemon) when ``AXIOM_GUARD_HOLDER_ARGV`` names one. Nothing here asks which language a
  holder is written in, because the contract requires the exclusion to be observed on the same
  files in two real processes rather than argued from a library name.

:class:`ReaderGuardAdapter` is the reader side of section 5 of the contract: acquire admission
shared, acquire data shared while admission is still held, release admission, copy the bounded
pointer/manifest/shard bytes under ``data.lock``, release data, and only then let the caller
parse or respond - so a publisher is never held behind parsing or network-response time.

Like :mod:`axiom_mcp.guard.interop`, this module is process-facing conformance support and not
the MCP tool surface: it adds no gateway command and no new public tool.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, TypeVar

from axiom_mcp.guard import protocol
from axiom_mcp.guard.engine import (
    PlatformBackend,
    SolutionGuard,
    load_backend,
    platform_backend_name,
)
from axiom_mcp.guard.errors import GuardError

__all__ = [
    "ABI_FORMAT_VERSION",
    "DEFAULT_HOLDER_ARGV",
    "HOLDER_ARGV_ENV",
    "HOLDER_PROCESS_CONTRACT",
    "LOCK_ROLES",
    "POSIX_DECLARATION",
    "WINDOWS_DECLARATION",
    "ReaderGuardAdapter",
    "abi_descriptor",
    "abi_json",
    "abi_sha256",
    "build_parser",
    "holder_env",
    "holder_is_foreign",
    "main",
    "read_event",
    "resolve_holder_argv",
    "spawn_holder",
    "wait_for_event",
]

T = TypeVar("T")

# Version of *this* description. It changes when a field is added or reworded here; the
# contract version it describes is ``protocol.CONTRACT_VERSION``.
ABI_FORMAT_VERSION = 1

LOCK_ROLES = {"admission.lock": "admission", "data.lock": "data"}

# The two platform rows of the normative lock table, under the contract field names. They live
# here rather than in ``protocol`` because they describe the *foreign* platform as well as this
# one, and ``tests/test_guard_protocol.py`` compares every one of them against the canonical
# document - including the digest over the contract frozen fields - so a drifted copy fails the
# build instead of being locked against by only one language.
POSIX_DECLARATION = {
    "primitive": "flock",
    "scope": "whole-file",
    "shared": "LOCK_SH",
    "exclusive": "LOCK_EX",
}

WINDOWS_DECLARATION = {
    "primitive": "LockFileEx",
    "byte_range": "offset 0, length 1",
    "open_mode": protocol.WINDOWS_OPEN_MODE,
    "share_mode": "FILE_SHARE_READ | FILE_SHARE_WRITE",
    "share_excludes": protocol.WINDOWS_SHARE_MODE_EXCLUDES,
    "exclusive_flag": "LOCKFILE_EXCLUSIVE_LOCK",
    "cancel_flag": "LOCKFILE_FAIL_IMMEDIATELY",
    "release": "UnlockFileEx matching range, then close every handle",
}

# The bounded wait and crash-release statements, in the contract's own field names.
BOUNDED_WAIT = {
    "policy": "bounded-retry",
    "unbounded_blocking_allowed": False,
    "cancel_supported": True,
    "default_timeout_ms": protocol.DEFAULT_TIMEOUT_MS,
    "max_timeout_ms": protocol.MAX_TIMEOUT_MS,
    "retry_initial_ms": protocol.RETRY_INITIAL_MS,
    "retry_max_ms": protocol.RETRY_MAX_MS,
    "on_timeout": (
        "release every acquired guard in reverse acquisition order, report a bounded "
        "lock-timeout and retry from the start of the acquisition order"
    ),
}

CRASH_RELEASE = {
    "mechanism": "process termination releases OS-owned locks",
    "pid_file_is_ownership": False,
    "lock_file_content_is_ownership": False,
    "stale_pid_is_live_holder": False,
    "stable_empty_files_may_persist": True,
    "removal_requires_all_processes_stopped": True,
}

INTEROP_REQUIREMENT = {
    "languages": ["rust", "python"],
    "distinct_processes": True,
    "same_primitive_per_platform": True,
    "library_name_alone_insufficient": True,
    "windows_and_posix_required": True,
    "required_scenarios": [
        "independent publisher",
        "independent reader",
        "gc process",
        "process kill",
        "handle closure",
        "timeout or cancellation",
        "sharing violation",
        "reader flood",
        "catalog vectors",
        "missing shards",
        "old pointer preservation",
    ],
}

# The environment variable that points the process-level tests at a foreign holder. Its value is
# a JSON array: ``["/path/to/rust-holder", "--flag"]``. An ABI-conformant holder implements the
# argv, events and exit codes below; nothing else about it is assumed.
HOLDER_ARGV_ENV = "AXIOM_GUARD_HOLDER_ARGV"

# The in-repo implementation of that contract. It is the default so the two-process evidence runs
# on any host, and it is the shape a foreign holder has to match.
DEFAULT_HOLDER_ARGV = (sys.executable, "-m", "axiom_mcp.guard.interop")

HOLDER_PROCESS_CONTRACT = {
    "argv": "<holder> hold --dir DIR --locks NAME[,NAME] --mode shared|exclusive --hold-ms MS",
    "probe_argv": "<holder> try --dir DIR --lock NAME --mode shared|exclusive --timeout-ms MS",
    "acquired_event": '{"event": "acquired", "pid": N, "platform": "...", "locks": [...], '
    '"mode": "...", "backend": "..."}',
    "released_event": '{"event": "released", ...}',
    "failed_event": '{"event": "failed", "reason": "...", ...}',
    "exit_ok": 0,
    "exit_timeout": 3,
    "exit_cancelled": 4,
    "exit_error": 5,
    "exit_usage": 6,
}


def abi_descriptor(*, backend: str | None = None) -> dict[str, Any]:
    """Describe the frozen guard surface in the contract's own field names.

    ``backend`` records which primitive this interpreter resolved for itself; the descriptor
    always declares both platforms, because a Rust daemon on the other platform must be able to
    read its own row from the same document.
    """
    lock_files = [
        {
            "name": name,
            "role": LOCK_ROLES[name],
            "posix_primitive": POSIX_DECLARATION["primitive"],
            "posix_scope": POSIX_DECLARATION["scope"],
            "posix_shared": POSIX_DECLARATION["shared"],
            "posix_exclusive": POSIX_DECLARATION["exclusive"],
            "windows_primitive": WINDOWS_DECLARATION["primitive"],
            "windows_byte_range": WINDOWS_DECLARATION["byte_range"],
            "windows_open_mode": WINDOWS_DECLARATION["open_mode"],
            "windows_share_mode": WINDOWS_DECLARATION["share_mode"],
            "windows_share_excludes": WINDOWS_DECLARATION["share_excludes"],
            "windows_exclusive_flag": WINDOWS_DECLARATION["exclusive_flag"],
            "windows_cancel_flag": WINDOWS_DECLARATION["cancel_flag"],
            "release": WINDOWS_DECLARATION["release"],
        }
        for name in protocol.LOCK_FILE_NAMES
    ]
    return {
        "abi_format_version": ABI_FORMAT_VERSION,
        "contract_id": protocol.CONTRACT_ID,
        "contract_version": protocol.CONTRACT_VERSION,
        "spec_version": protocol.SPEC_VERSION,
        "owner": "axiom-specs",
        "protocol_digest": protocol.PROTOCOL_DIGEST,
        "guard_directory_template": protocol.GUARD_DIRECTORY_TEMPLATE,
        "instances_directory_name": protocol.INSTANCES_DIRECTORY_NAME,
        "guard_directory_name": protocol.GUARD_DIRECTORY_NAME,
        "lock_files": lock_files,
        "acquisition_order": list(protocol.ACQUISITION_ORDER),
        "release_order": list(protocol.RELEASE_ORDER),
        "no_upgrade": True,
        "no_recursive_acquire": True,
        "single_guard_default": True,
        "bounded_wait": dict(BOUNDED_WAIT),
        "crash_release": dict(CRASH_RELEASE),
        "interop_requirement": dict(INTEROP_REQUIREMENT),
        "local_backend": backend if backend is not None else platform_backend_name(),
        "holder_process_contract": dict(HOLDER_PROCESS_CONTRACT),
    }


def abi_canonical_bytes(descriptor: Mapping[str, Any] | None = None) -> bytes:
    """Canonical JSON bytes of a descriptor, using the contract's digest algorithm."""
    payload = abi_descriptor() if descriptor is None else dict(descriptor)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def abi_json(descriptor: Mapping[str, Any] | None = None) -> str:
    """Return the canonical JSON text of the descriptor, so a harness can hash it."""
    return abi_canonical_bytes(descriptor).decode("utf-8")


def abi_sha256(descriptor: Mapping[str, Any] | None = None) -> str:
    """Return the sha256 of :func:`abi_json`, the value a foreign build pins."""
    return hashlib.sha256(abi_canonical_bytes(descriptor)).hexdigest()


def holder_is_foreign() -> bool:
    """Whether this run was pointed at a holder other than the in-repo probe."""
    return bool(os.environ.get(HOLDER_ARGV_ENV, "").strip())


def resolve_holder_argv() -> tuple[str, ...]:
    """Return the argv prefix of the holder that implements the process contract.

    The default is the in-repo probe. ``AXIOM_GUARD_HOLDER_ARGV`` replaces it with any other
    ABI-conformant holder - the Rust one, for instance - and is refused with a deterministic
    reason instead of being used half-resolved: a holder that cannot be started must be reported
    as unavailable by the evidence, never silently replaced by the Python probe.
    """
    raw = os.environ.get(HOLDER_ARGV_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_HOLDER_ARGV
    try:
        argv = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GuardError("holder_argv_invalid", f"{HOLDER_ARGV_ENV} is not JSON: {exc}") from exc
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) and item for item in argv)
    ):
        raise GuardError(
            "holder_argv_invalid",
            f"{HOLDER_ARGV_ENV} must be a JSON array of non-empty strings",
        )
    executable = shutil.which(argv[0]) if not os.path.isabs(argv[0]) else argv[0]
    if executable is None or not os.path.isfile(executable):
        raise GuardError(
            "holder_unavailable",
            f"the holder executable {argv[0]!r} named by {HOLDER_ARGV_ENV} was not found",
        )
    return (executable, *argv[1:])


def _package_root() -> Path:
    """The directory that holds the ``axiom_mcp`` package, so a child can import it."""
    return Path(__file__).resolve().parents[2]


def holder_env() -> dict[str, str]:
    """Environment for a holder child process.

    The in-repo probe is started with this checkout's package root on ``PYTHONPATH`` so the child
    imports the same code the parent does; a foreign holder is given the ambient environment and
    nothing else, because inventing an interpreter path for it would hide a broken holder.
    """
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if resolve_holder_argv() == DEFAULT_HOLDER_ARGV:
        root = str(_package_root())
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = root + (os.pathsep + existing if existing else "")
    return env


def spawn_holder(
    *args: str,
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.Popen[str]:
    """Start a holder process that speaks :data:`HOLDER_PROCESS_CONTRACT`."""
    command = [*(argv if argv is not None else resolve_holder_argv()), *args]
    return subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=dict(env if env is not None else holder_env()),
    )


def read_event(line: str) -> dict[str, Any]:
    """Parse one event line, refusing anything that is not a contract event object."""
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise GuardError(
            "holder_protocol", f"holder wrote a non-JSON line: {line.strip()!r} ({exc})"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("event"), str):
        raise GuardError(
            "holder_protocol", f"holder event is not an object with an event: {line!r}"
        )
    return payload


def wait_for_event(
    proc: subprocess.Popen[str], wanted: str, timeout: float = 15.0
) -> dict[str, Any]:
    """Read holder stdout until ``wanted`` arrives, or fail with what was seen.

    Reading the announcement instead of sleeping a fixed amount is what makes the exclusion
    observed rather than assumed: a test may only probe the guard once the holder has said it
    holds it.
    """
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    while time.monotonic() < deadline:
        line = proc.stdout.readline() if proc.stdout else ""
        if not line:
            raise GuardError(
                "holder_protocol",
                f"holder exited before {wanted!r}: saw {seen!r}, exit code {proc.poll()}",
            )
        seen.append(line.strip())
        payload = read_event(line)
        if payload["event"] == wanted:
            return payload
    raise GuardError("holder_protocol", f"no {wanted!r} event from the holder; saw {seen!r}")


class ReaderGuardAdapter:
    """The reader guard a Python reader and a Rust publisher share.

    The adapter adds no policy to :class:`~axiom_mcp.guard.engine.SolutionGuard`; it exposes the
    contract's reader shape and the ABI this build declares, so the reader path is the same one a
    foreign publisher must interleave with.
    """

    def __init__(
        self,
        guard_dir: str | os.PathLike[str],
        *,
        timeout_ms: int | None = None,
        cancel: Callable[[], bool] | None = None,
        backend: PlatformBackend | None = None,
        attempts: int = 1,
    ) -> None:
        self._guard = SolutionGuard(
            guard_dir, timeout_ms=timeout_ms, cancel=cancel, backend=backend
        )
        self._attempts = attempts

    def __repr__(self) -> str:
        return (
            f"ReaderGuardAdapter(guard_dir={str(self._guard.guard_dir)!r}, "
            f"backend={self._guard.backend_name!r}, timeout_ms={self._guard.timeout_ms})"
        )

    @property
    def guard(self) -> SolutionGuard:
        return self._guard

    @property
    def guard_dir(self) -> Path:
        return self._guard.guard_dir

    @property
    def backend_name(self) -> str:
        return self._guard.backend_name

    def abi(self) -> dict[str, Any]:
        """This build's ABI descriptor, with the backend it resolved locally."""
        return abi_descriptor(backend=self._guard.backend_name)

    def verify_surface(self) -> dict[str, Any]:
        """Create and verify the two declared lock files before either language locks them.

        The contract fixes the *file identity* every language locks, so a guard directory that is
        a reparse point or a lock file that is a symlink is refused: two languages following the
        same path through different targets would each believe they hold the same guard. Refusing
        is the only safe answer, because the files are opened with ``OPEN_ALWAYS``/``O_CREAT`` and
        a replacement can otherwise happen between the two opens.
        """
        directory = self._guard.guard_dir
        if os.path.islink(directory):
            raise GuardError(
                "guard_dir_untrusted",
                f"{directory} is a link; the guard directory must be a private real directory "
                "so every language resolves the same file identity",
            )
        if directory.exists() and not directory.is_dir():
            raise GuardError("guard_dir_untrusted", f"{directory} exists and is not a directory")
        self._guard.ensure_directory()
        files: dict[str, Any] = {}
        for name, path in protocol.lock_paths(directory).items():
            if os.path.islink(path):
                raise GuardError(
                    "guard_file_untrusted",
                    f"{path} is a link; the declared guard file must be the file both languages "
                    "lock",
                    lock=name,
                )
            handle = load_backend(self._guard.backend_name).open(path)
            try:
                identity = handle.identity()
            finally:
                handle.close()
            if identity != protocol.file_identity(path):
                raise GuardError(
                    "identity_mismatch",
                    f"{path} changed identity while it was opened; refusing to publish a guard "
                    "surface another language could lock elsewhere",
                    lock=name,
                )
            files[name] = {"path": str(path), "identity": identity}
        return {
            "guard_dir": str(directory),
            "backend": self._guard.backend_name,
            "lock_files": files,
        }

    def reader(self) -> AbstractContextManager[SolutionGuard]:
        """Hold admission then data shared, releasing admission early, per the contract."""
        return self._guard.reader(attempts=self._attempts)

    def read_pinned(self, pin: Callable[[], T]) -> T:
        """Run ``pin`` exactly once under ``data.lock`` and return what it copied.

        The pin happens while the data guard is held and the value is returned only after the
        guard has been released, so the caller parses and responds outside the wait a publisher
        can observe. The pin must therefore copy the bounded bytes and do the expensive work
        afterwards; a pin that parses inside the guard has already broken the rule this method
        exists to keep.
        """
        if not callable(pin):
            raise GuardError("invalid_pin", f"pin must be callable, got {pin!r}")
        with self._guard.reader(attempts=self._attempts):
            value = pin()
        return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axiom_mcp.guard.adapter",
        description="Print the cross-language reader guard ABI as canonical JSON.",
    )
    parser.add_argument(
        "command",
        choices=["abi"],
        help="abi: print this build's ABI descriptor and exit",
    )
    parser.add_argument("--digest", action="store_true", help="print only the descriptor sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.command == "abi":
        sys.stdout.write((abi_sha256() if args.digest else abi_json()) + "\n")
        sys.stdout.flush()
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
