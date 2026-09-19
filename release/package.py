"""Version-dir packaging and lockfile rollback for axiom-mcp.

The update contract fixes the shape of a Python install: a package lives in a
versioned virtual environment, and a running process is never pip-upgraded in
place. This module is the piece that builds those versioned environments. It
stages a built artifact under an install root, creates one isolated virtual
environment per version, installs the artifact into it with no network and no
dependency resolution, and records exactly what the environment contained in a
per-version lockfile.

Rollback is the other half, and it is deliberately narrow. Rolling back does not
download anything and does not pick a version by guessing: it reads an earlier
version's lockfile, proves the staged artifact still hashes to the digest that
lockfile recorded, brings the environment back to the locked frozen set if it has
drifted, and only then repoints the active version. If the artifact hash no
longer matches, the rollback is refused instead of installing bytes nobody has
seen before.

Every mutation happens inside the install root this process was told to use. The
running interpreter and the running package's own source tree are refused up
front through the same guard the update planner uses, so a packaging run cannot
become the in-place upgrade the contract forbids.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from axiom_mcp import version
from axiom_mcp.update import InPlaceUpgradeRefused, guard_install_root

__all__ = [
    "ARTIFACT_DIRNAME",
    "CURRENT_FILENAME",
    "HISTORY_FILENAME",
    "LAYOUT_VERSION",
    "LOCKFILE_FILENAME",
    "VERSIONS_DIRNAME",
    "VENV_DIRNAME",
    "CommandResult",
    "PackageError",
    "PackageRefused",
    "VersionEnvironment",
    "activate",
    "append_history",
    "build_wheel",
    "create_environment",
    "create_venv",
    "freeze",
    "history",
    "install",
    "install_artifact",
    "normalize_frozen",
    "read_current",
    "read_lockfile",
    "resolve_install_root",
    "rollback",
    "sha256_file",
    "status",
    "validate_version",
    "venv_python",
    "version_directory",
]

LAYOUT_VERSION = 1

VERSIONS_DIRNAME = "versions"
VENV_DIRNAME = "venv"
ARTIFACT_DIRNAME = "artifact"
LOCKFILE_FILENAME = "lockfile.json"
CURRENT_FILENAME = "current.json"
HISTORY_FILENAME = "history.jsonl"

DEFAULT_TIMEOUT_S = 600.0
DETAIL_LIMIT = 300

# A version name becomes a directory name, so it is validated before it can
# escape the versions directory through a separator or a parent reference.
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")

_VENV_PYTHON_WINDOWS = ("Scripts", "python.exe")
_VENV_PYTHON_POSIX = ("bin", "python")


class PackageError(RuntimeError):
    """A packaging or rollback failure with a stable code and a safe detail."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


class PackageRefused(PackageError):
    """The request was refused before, or instead of, a mutation."""


@dataclass(frozen=True)
class CommandResult:
    """One subprocess outcome, captured instead of streamed into a log."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., CommandResult]


def _first_error_line(result: CommandResult) -> str:
    """The last non-empty output line, which is pip's own failure summary."""
    for stream in (result.stderr, result.stdout):
        lines = [line.strip() for line in (stream or "").splitlines() if line.strip()]
        if lines:
            return lines[-1][:DETAIL_LIMIT]
    return f"exit{result.returncode}"


def _run(
    argv: Sequence[str],
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    completed = subprocess.run(
        [str(item) for item in argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        cwd=None if cwd is None else str(cwd),
        env=None if env is None else dict(env),
        check=False,
    )
    return CommandResult(
        argv=tuple(str(item) for item in argv),
        returncode=int(completed.returncode),
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def sha256_file(path: str | os.PathLike[str]) -> str:
    """The SHA256 of one file, read in bounded chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def validate_version(version: str) -> str:
    """Return a safe version name, or refuse it before it becomes a path."""
    text = str(version or "").strip()
    if not _VERSION_PATTERN.fullmatch(text):
        raise PackageRefused("version_invalid", text or "empty")
    return text


def resolve_install_root(
    install_root: str | os.PathLike[str],
    *,
    protected: Sequence[pathlib.Path] | None = None,
) -> pathlib.Path:
    """Refuse an install root that would rewrite the running installation."""
    text = str(install_root or "").strip()
    if text == "":
        raise PackageRefused("install_root_invalid", "install_root_empty")
    try:
        guard_install_root(text, protected)
    except InPlaceUpgradeRefused as exc:
        raise PackageRefused("install_root_invalid", "; ".join(exc.reasons)) from exc
    return pathlib.Path(text).resolve()


def version_directory(install_root: str | os.PathLike[str], version: str) -> pathlib.Path:
    """The per-version directory under an install root, version validated first."""
    root = pathlib.Path(install_root)
    return root / VERSIONS_DIRNAME / validate_version(version)


def venv_python(venv_dir: str | os.PathLike[str]) -> pathlib.Path:
    """The interpreter inside one virtual environment, per platform."""
    directory = pathlib.Path(venv_dir)
    relative = _VENV_PYTHON_WINDOWS if os.name == "nt" else _VENV_PYTHON_POSIX
    return directory.joinpath(*relative)


def _write_json_atomic(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    """Replace a JSON document in one step, with LF endings and no BOM."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def read_lockfile(version_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Load one version's lockfile, refusing a missing or unreadable document."""
    path = pathlib.Path(version_dir) / LOCKFILE_FILENAME
    if not path.exists():
        raise PackageRefused("lockfile_missing", str(path))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PackageRefused("lockfile_corrupt", f"{path}") from exc
    if not isinstance(payload, dict):
        raise PackageRefused("lockfile_corrupt", f"{path}")
    return dict(payload)


def read_current(install_root: str | os.PathLike[str]) -> dict[str, Any] | None:
    """The active-version pointer, or None when nothing has been activated yet."""
    path = pathlib.Path(install_root) / CURRENT_FILENAME
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PackageRefused("current_corrupt", str(path)) from exc
    return dict(payload) if isinstance(payload, dict) else None


def activate(
    install_root: str | os.PathLike[str],
    version: str,
    *,
    clock: Callable[[], float] | None = None,
) -> pathlib.Path:
    """Point the install root at one version, atomically."""
    root = pathlib.Path(install_root)
    name = validate_version(version)
    path = root / CURRENT_FILENAME
    _write_json_atomic(
        path,
        {
            "layout": LAYOUT_VERSION,
            "active": name,
            "updated_at": _now(clock),
        },
    )
    return path


def history(install_root: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Every recorded install and rollback, oldest first."""
    path = pathlib.Path(install_root) / HISTORY_FILENAME
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except ValueError:
                continue
            if isinstance(payload, dict):
                records.append(dict(payload))
    return records


def append_history(install_root: str | os.PathLike[str], record: Mapping[str, Any]) -> pathlib.Path:
    """Append one history record as a single LF-terminated JSON line."""
    root = pathlib.Path(install_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / HISTORY_FILENAME
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(dict(record), sort_keys=True) + "\n")
    return path


def _now(clock: Callable[[], float] | None = None) -> float:
    return float((clock or time.time)())


def create_venv(
    venv_dir: str | os.PathLike[str],
    *,
    python: str | os.PathLike[str] | None = None,
    runner: Runner | None = None,
) -> pathlib.Path:
    """Create one virtual environment and return its interpreter."""
    run = runner or _run
    directory = pathlib.Path(venv_dir)
    directory.parent.mkdir(parents=True, exist_ok=True)
    interpreter = python or sys.executable
    result = run([str(interpreter), "-m", "venv", str(directory)])
    if result.returncode != 0:
        raise PackageError("venv_creation_failed", _first_error_line(result))
    path = venv_python(directory)
    if not path.exists():
        raise PackageError("venv_creation_failed", f"no interpreter at {path}")
    return path


def install_artifact(
    interpreter: str | os.PathLike[str],
    artifact: str | os.PathLike[str],
    *,
    runner: Runner | None = None,
    force: bool = False,
) -> None:
    """Install one artifact with no network and no dependency resolution.

    The no-index flag is what makes the install reproducible: pip may use the
    file it was given and nothing else, so a packaging run cannot silently pull a
    different version of a dependency than the artifact was built with.
    """
    run = runner or _run
    argv = [str(interpreter), "-m", "pip", "install", "--no-index", "--no-deps"]
    if force:
        argv.append("--force-reinstall")
    argv.append(str(artifact))
    result = run(argv)
    if result.returncode != 0:
        raise PackageError("install_failed", _first_error_line(result))


_DIRECT_URL_MARKER = " @ file://"


def normalize_frozen(lines: Sequence[str]) -> tuple[str, ...]:
    """Reduce a frozen set to what identifies it, not where it lives.

    pip records a direct-URL install as a distribution followed by an at-sign and
    a file URL with an absolute path. That path is a property of the install root,
    not of the environment, so two frozen sets are compared with the directory
    stripped from those lines and the artifact name plus any fragment kept.
    Without this, moving an install root would make every rollback look like a
    drift and would rewrite the lockfile.
    """
    normalized: list[str] = []
    for line in lines:
        text = str(line).strip()
        if _DIRECT_URL_MARKER not in text:
            normalized.append(text)
            continue
        head, _, tail = text.partition(_DIRECT_URL_MARKER)
        name = tail.rsplit("/", 1)[-1] if "/" in tail else tail
        normalized.append(f"{head}{_DIRECT_URL_MARKER}{name}")
    return tuple(normalized)


def freeze(
    interpreter: str | os.PathLike[str],
    *,
    runner: Runner | None = None,
) -> tuple[str, ...]:
    """The locked frozen set of one environment, sorted for a stable comparison."""
    run = runner or _run
    result = run([str(interpreter), "-m", "pip", "freeze", "--all"])
    if result.returncode != 0:
        raise PackageError("freeze_failed", _first_error_line(result))
    return tuple(sorted(line.strip() for line in result.stdout.splitlines() if line.strip()))


def stage_artifact(
    install_root: str | os.PathLike[str],
    version: str,
    artifact: str | os.PathLike[str],
) -> tuple[pathlib.Path, str]:
    """Copy an artifact next to its version and return (path, sha256).

    The copy is what rollback verifies later, so an artifact that has been
    replaced in its original location cannot change what a rollback installs.
    """
    source = pathlib.Path(artifact)
    if not source.exists():
        raise PackageRefused("artifact_missing", str(source))
    if not source.is_file():
        raise PackageRefused("artifact_not_a_file", str(source))
    target_dir = version_directory(install_root, version) / ARTIFACT_DIRNAME
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)
    return target, sha256_file(target)


def build_wheel(
    source_dir: str | os.PathLike[str],
    out_dir: str | os.PathLike[str],
    *,
    python: str | os.PathLike[str] | None = None,
    runner: Runner | None = None,
) -> pathlib.Path:
    """Build one wheel from a source tree, without build isolation.

    No isolation means the build stays offline and depends on the build backend
    already present in this interpreter instead of downloading one.
    """
    run = runner or _run
    interpreter = python or sys.executable
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    result = run(
        [
            str(interpreter),
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(out),
            str(source_dir),
        ]
    )
    if result.returncode != 0:
        raise PackageError("build_failed", _first_error_line(result))
    wheels = sorted(out.glob("*.whl"), key=lambda item: item.stat().st_mtime)
    if not wheels:
        raise PackageError("build_failed", f"no wheel produced in {out}")
    return wheels[-1]


@dataclass(frozen=True)
class VersionEnvironment:
    """One installed version: its directory, its interpreter and its locked set."""

    version: str
    root: pathlib.Path
    venv_dir: pathlib.Path
    python: pathlib.Path
    artifact: pathlib.Path
    artifact_sha256: str
    frozen: tuple[str, ...]
    lockfile: pathlib.Path

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "root": str(self.root),
            "venv": str(self.venv_dir),
            "python": str(self.python),
            "artifact": str(self.artifact),
            "artifact_sha256": self.artifact_sha256,
            "frozen": list(self.frozen),
            "lockfile": str(self.lockfile),
        }


def create_environment(
    install_root: str | os.PathLike[str],
    version: str,
    *,
    artifact: str | os.PathLike[str],
    python: str | os.PathLike[str] | None = None,
    runner: Runner | None = None,
) -> VersionEnvironment:
    """Build one isolated version environment from one artifact."""
    name = validate_version(version)
    root = version_directory(install_root, name)
    venv_dir = root / VENV_DIRNAME
    if venv_dir.exists():
        shutil.rmtree(venv_dir)
    root.mkdir(parents=True, exist_ok=True)
    staged, digest = stage_artifact(install_root, name, artifact)
    interpreter = create_venv(venv_dir, python=python, runner=runner)
    install_artifact(interpreter, staged, runner=runner)
    installed = freeze(interpreter, runner=runner)
    return VersionEnvironment(
        version=name,
        root=root,
        venv_dir=venv_dir,
        python=interpreter,
        artifact=staged,
        artifact_sha256=digest,
        frozen=installed,
        lockfile=root / LOCKFILE_FILENAME,
    )


def serialize_lockfile(
    environment: VersionEnvironment,
    *,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """The lockfile document: what was installed, from which bytes, and when."""
    return {
        "layout": LAYOUT_VERSION,
        "version": environment.version,
        "artifact": {
            "name": environment.artifact.name,
            "sha256": environment.artifact_sha256,
            "size": int(environment.artifact.stat().st_size),
        },
        "python": {
            "executable": str(environment.python),
            "version": sys.version.split()[0],
            "marker": version.VERSION,
        },
        "venv": VENV_DIRNAME,
        "frozen": list(environment.frozen),
        "created_at": _now(clock),
    }


def install(
    install_root: str | os.PathLike[str],
    version: str,
    *,
    artifact: str | os.PathLike[str],
    python: str | os.PathLike[str] | None = None,
    runner: Runner | None = None,
    clock: Callable[[], float] | None = None,
    protected: Sequence[pathlib.Path] | None = None,
) -> dict[str, Any]:
    """Install one artifact into an isolated version directory and activate it."""
    root = resolve_install_root(install_root, protected=protected)
    name = validate_version(version)
    version_dir = version_directory(root, name)
    existed = version_dir.exists()
    try:
        environment = create_environment(
            root, name, artifact=artifact, python=python, runner=runner
        )
    except PackageError:
        # A failed install must not leave a half-built version behind, and it must
        # not remove a version that was already there before this call.
        if not existed and version_dir.exists():
            shutil.rmtree(version_dir, ignore_errors=True)
        raise
    _write_json_atomic(environment.lockfile, serialize_lockfile(environment, clock=clock))
    activate(root, name, clock=clock)
    record = {
        "action": "install",
        "version": name,
        "artifact": environment.artifact.name,
        "artifact_sha256": environment.artifact_sha256,
        "frozen": list(environment.frozen),
        "at": _now(clock),
    }
    append_history(root, record)
    outcome = environment.as_dict()
    outcome["action"] = "install"
    outcome["active"] = name
    return outcome


def _installed_versions(records: Sequence[Mapping[str, Any]]) -> list[str]:
    ordered: list[str] = []
    for record in records:
        if str(record.get("action", "")) != "install":
            continue
        name = str(record.get("version", ""))
        if name and name not in ordered:
            ordered.append(name)
    return ordered


def resolve_rollback_target(
    root: pathlib.Path,
    to_version: str | None,
    records: Sequence[Mapping[str, Any]],
) -> str:
    """Choose the rollback target: an explicit version, or the previous install."""
    if to_version is not None and str(to_version).strip() != "":
        return validate_version(str(to_version))
    current = read_current(root)
    active = "" if current is None else str(current.get("active", ""))
    for name in reversed(_installed_versions(records)):
        if name != active:
            return name
    raise PackageRefused("no_previous_version", "no earlier install is recorded in history")


def rollback(
    install_root: str | os.PathLike[str],
    *,
    to_version: str | None = None,
    runner: Runner | None = None,
    clock: Callable[[], float] | None = None,
    protected: Sequence[pathlib.Path] | None = None,
) -> dict[str, Any]:
    """Restore an earlier version's lockfile environment and make it active.

    The restore is verified, not assumed: the staged artifact must still hash to
    the digest the lockfile recorded, and the environment's frozen set must equal
    the locked set after any repair before the pointer moves. Frozen sets are
    compared by identity rather than by absolute path, so an install root that was
    moved is still recognised as the same environment.
    """
    root = resolve_install_root(install_root, protected=protected)
    records = history(root)
    target = resolve_rollback_target(root, to_version, records)
    version_dir = version_directory(root, target)
    if not version_dir.exists():
        raise PackageRefused("rollback_target_missing", str(version_dir))
    lockfile = read_lockfile(version_dir)
    expected = tuple(str(item) for item in lockfile.get("frozen", ()) or ())
    artifact_info = lockfile.get("artifact") or {}
    artifact_name = str(artifact_info.get("name", ""))
    if artifact_name == "":
        raise PackageRefused("lockfile_corrupt", str(version_dir / LOCKFILE_FILENAME))
    staged = version_dir / ARTIFACT_DIRNAME / artifact_name
    if not staged.exists():
        raise PackageRefused("artifact_missing", str(staged))
    digest = sha256_file(staged)
    if digest != str(artifact_info.get("sha256", "")):
        raise PackageRefused("artifact_digest_mismatch", str(staged))

    repairs: list[str] = []
    venv_dir = version_dir / VENV_DIRNAME
    interpreter = venv_python(venv_dir)
    if not interpreter.exists():
        interpreter = create_venv(venv_dir, runner=runner)
        repairs.append("venv_recreated")
    current = freeze(interpreter, runner=runner)
    if normalize_frozen(current) != normalize_frozen(expected):
        install_artifact(interpreter, staged, runner=runner, force=True)
        current = freeze(interpreter, runner=runner)
        repairs.append("environment_reinstalled")
    if normalize_frozen(current) != normalize_frozen(expected):
        raise PackageError("environment_not_restored", target)

    activate(root, target, clock=clock)
    record = {
        "action": "rollback",
        "version": target,
        "restored": True,
        "repairs": list(repairs),
        "frozen": list(current),
        "at": _now(clock),
    }
    append_history(root, record)
    return {
        "action": "rollback",
        "version": target,
        "active": target,
        "restored": True,
        "repairs": list(repairs),
        "frozen": list(current),
        "artifact_sha256": digest,
        "lockfile": str(version_dir / LOCKFILE_FILENAME),
    }


def status(
    install_root: str | os.PathLike[str],
    *,
    protected: Sequence[pathlib.Path] | None = None,
) -> dict[str, Any]:
    """Report the layout, the active version and every version on disk."""
    root = resolve_install_root(install_root, protected=protected)
    current = read_current(root)
    records = history(root)
    versions_dir = root / VERSIONS_DIRNAME
    present: list[dict[str, Any]] = []
    if versions_dir.exists():
        for entry in sorted(versions_dir.iterdir()):
            if not entry.is_dir():
                continue
            lockfile = entry / LOCKFILE_FILENAME
            present.append(
                {
                    "version": entry.name,
                    "locked": lockfile.exists(),
                    "venv": (entry / VENV_DIRNAME).exists(),
                }
            )
    active = None if current is None else current.get("active")
    return {
        "layout": LAYOUT_VERSION,
        "install_root": str(root),
        "active": active,
        "versions": present,
        "history": records,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Install, roll back or report one install root. Prints JSON only."""
    parser = argparse.ArgumentParser(prog="package", description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)

    install_parser = subparsers.add_parser("install", help="install one artifact into a version")
    install_parser.add_argument("--install-root", required=True)
    install_parser.add_argument("--version", required=True)
    install_parser.add_argument("--artifact", required=True)

    rollback_parser = subparsers.add_parser("rollback", help="restore an earlier lockfile env")
    rollback_parser.add_argument("--install-root", required=True)
    rollback_parser.add_argument("--to", default=None)

    status_parser = subparsers.add_parser("status", help="report the install root")
    status_parser.add_argument("--install-root", required=True)

    arguments = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if arguments.command == "install":
            payload = install(
                arguments.install_root,
                arguments.version,
                artifact=arguments.artifact,
            )
        elif arguments.command == "rollback":
            payload = rollback(arguments.install_root, to_version=arguments.to)
        else:
            payload = status(arguments.install_root)
    except PackageError as exc:
        print(json.dumps({"error": exc.code, "detail": exc.detail}, sort_keys=True))
        return 1
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
