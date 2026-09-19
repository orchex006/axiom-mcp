"""Locked native Python entrypoints for axiom-mcp.

A host does not start the gateway by activating a shell; it launches one
absolute interpreter with an argv array. This module owns that launch surface.

The distribution pins a Python line and an SDK revision, and the install layout
keeps one virtual environment per version. An entrypoint lock therefore has to
answer three questions with evidence instead of assumption:

* **which interpreter** - the one inside the locked version environment, never an
  ambient interpreter that merely happens to be reachable through ``PATH``;
* **how it is launched** - an executable, an argument vector, a working directory
  and a scoped environment; never a shell string and never an activation script
  (cross-platform CP-02 and CP-09);
* **what it may expose** - stdio by default, HTTP only as an explicit opt-in that
  binds loopback and carries a non-wildcard Host/Origin allowlist that admits its
  own bind address (CP-09).

Everything here is a *plan*. This module starts no server and opens no socket: it
resolves, verifies and refuses. The one subprocess it runs is the interpreter
probe, because "this environment is the locked one" is exactly the claim a plan
must not make on trust.

The launched environment is built from scratch rather than inherited: handing a
child process the caller's variables is how a locked environment stops being
locked. ``PATH`` is therefore deterministic (the environment's own scripts
directory, plus the Windows system directory) instead of the login shell's, which
is also what makes a plan digest stable enough to write into a lock file.

The version-directory layout (``current.json``, ``versions/<version>/venv`` and
``lockfile.json``) is owned by the packaging tool in ``release/package.py``.
Those names are repeated here rather than imported: ``release/`` is operator
tooling and is not part of the installed distribution, so the installed package
cannot import it. ``tests/test_entrypoints.py`` asserts that the two definitions
agree, so the copy cannot drift silently.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from axiom_mcp import http as http_transport
from axiom_mcp import paths, security, version

__all__ = [
    "CURRENT_FILENAME",
    "ENTRYPOINT_LOCK_FILENAME",
    "ENVIRONMENT_MARKER",
    "LOCKFILE_FILENAME",
    "LOCK_LAYOUT_VERSION",
    "MODE_HTTP",
    "MODE_STDIO",
    "MODES",
    "VERSIONS_DIRNAME",
    "VENV_DIRNAME",
    "CommandResult",
    "EntrypointError",
    "EntrypointRefused",
    "LaunchPlan",
    "RuntimeLock",
    "entrypoint_lock_reasons",
    "host_launch_document",
    "http_arguments",
    "launch_plan",
    "main",
    "plan_digest",
    "read_current",
    "read_lock",
    "read_lockfile",
    "read_venv_metadata",
    "resolve_runtime",
    "scoped_environment",
    "sha256_file",
    "shared_base_packages",
    "validate_version",
    "venv_python",
    "version_directory",
    "write_lock",
]

ENTRYPOINT_LOCK_FILENAME = "entrypoints.json"
LOCK_LAYOUT_VERSION = 1

MODE_STDIO = "stdio"
MODE_HTTP = "http"
MODES: tuple[str, ...] = (MODE_STDIO, MODE_HTTP)

# Mirrored from release/package.py (layout 1). See the module docstring.
VERSIONS_DIRNAME = "versions"
VENV_DIRNAME = "venv"
LOCKFILE_FILENAME = "lockfile.json"
CURRENT_FILENAME = "current.json"

_VENV_PYTHON_WINDOWS = ("Scripts", "python.exe")
_VENV_PYTHON_POSIX = ("bin", "python")
_VENV_SCRIPTS_WINDOWS = "Scripts"
_VENV_SCRIPTS_POSIX = "bin"

ENVIRONMENT_MARKER = "AXIOM_MCP_ENTRYPOINT"

DEFAULT_TIMEOUT_S = 120.0
DETAIL_LIMIT = 300

EXIT_SUCCESS = 0
EXIT_VALIDATION = 2

_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")

# Reads back what the locked interpreter actually is. The SDK package name is
# passed as an argument so this stays a probe instead of a second policy.
_PROBE_SOURCE = (
    "import importlib.metadata as metadata, json, sys\n"
    "package = sys.argv[1]\n"
    "report = {\n"
    "    'version': sys.version.split()[0],\n"
    "    'prefix': sys.prefix,\n"
    "    'base_prefix': sys.base_prefix,\n"
    "    'executable': sys.executable,\n"
    "}\n"
    "try:\n"
    "    report['sdk'] = metadata.version(package)\n"
    "except Exception:\n"
    "    report['sdk'] = None\n"
    "try:\n"
    "    module = __import__(package)\n"
    "    report['sdk_path'] = getattr(module, '__file__', '') or ''\n"
    "except Exception:\n"
    "    report['sdk_path'] = ''\n"
    "print(json.dumps(report))\n"
)


class EntrypointError(RuntimeError):
    """An entrypoint failure with a stable code and a safe detail."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


class EntrypointRefused(EntrypointError):
    """The request was refused before, or instead of, producing a plan."""


@dataclass(frozen=True)
class CommandResult:
    """One subprocess outcome, captured instead of streamed."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., CommandResult]


def _first_error_line(result: CommandResult) -> str:
    """The last non-empty output line, which is the interpreter's own summary."""

    for stream in (result.stderr, result.stdout):
        lines = [line.strip() for line in (stream or "").splitlines() if line.strip()]
        if lines:
            return lines[-1][:DETAIL_LIMIT]
    return f"exit{result.returncode}"


def _run(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> CommandResult:
    """Run one command with an argv array. There is no shell in this path."""

    completed = subprocess.run(
        [str(item) for item in argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=None if env is None else {str(key): str(value) for key, value in env.items()},
        timeout=timeout_s,
        check=False,
        shell=False,
    )
    return CommandResult(
        argv=tuple(str(item) for item in argv),
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_version(value: object) -> str:
    """A version name becomes a directory name, so it is validated first."""

    if not isinstance(value, str) or not _VERSION_PATTERN.fullmatch(value):
        raise EntrypointRefused("version_invalid", str(value)[:DETAIL_LIMIT])
    return value


def version_directory(install_root: str | os.PathLike[str], version_name: str) -> pathlib.Path:
    return pathlib.Path(install_root) / VERSIONS_DIRNAME / validate_version(version_name)


def venv_python(venv_dir: str | os.PathLike[str]) -> pathlib.Path:
    """The interpreter inside one virtual environment, per platform."""

    relative = _VENV_PYTHON_WINDOWS if os.name == "nt" else _VENV_PYTHON_POSIX
    return pathlib.Path(venv_dir).joinpath(*relative)


def read_current(install_root: str | os.PathLike[str]) -> dict[str, Any] | None:
    """The active-version pointer, or None when nothing has been activated yet."""

    path = pathlib.Path(install_root) / CURRENT_FILENAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EntrypointRefused("current_corrupt", str(path)) from exc
    return dict(payload) if isinstance(payload, dict) else None


def read_lockfile(version_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Load one version's lockfile, refusing a missing or unreadable document."""

    path = pathlib.Path(version_dir) / LOCKFILE_FILENAME
    if not path.is_file():
        raise EntrypointRefused("lockfile_missing", str(path))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EntrypointRefused("lockfile_corrupt", str(path)) from exc
    if not isinstance(payload, dict):
        raise EntrypointRefused("lockfile_corrupt", str(path))
    return dict(payload)


def read_venv_metadata(venv_dir: str | os.PathLike[str]) -> dict[str, str]:
    """Read ``pyvenv.cfg`` leniently, because its spelling is not always UTF-8.

    On Windows the file records the creation command, so a virtual environment
    created at a path with non-ASCII characters can hold bytes in the host code
    page rather than UTF-8. Decoding is lossy on purpose: this reads one boolean,
    and a replacement character in a recorded command must not make a launchable
    environment look unreadable.
    """

    path = pathlib.Path(venv_dir) / "pyvenv.cfg"
    if not path.is_file():
        raise EntrypointRefused("venv_metadata_missing", str(path))
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError as exc:
        raise EntrypointRefused("venv_metadata_unreadable", str(path)) from exc
    entries: dict[str, str] = {}
    for line in text.splitlines():
        name, separator, value = line.partition("=")
        if separator:
            entries[name.strip().lower()] = value.strip()
    if not entries:
        raise EntrypointRefused("venv_metadata_corrupt", str(path))
    return entries


def shared_base_packages(metadata: Mapping[str, str]) -> bool:
    """Whether the environment declares that it borrows base-interpreter packages."""

    return metadata.get("include-system-site-packages", "false").strip().lower() == "true"


def _normcase(text: str) -> str:
    return os.path.normcase(text)


def _same_path(first: pathlib.Path | str, second: pathlib.Path | str) -> bool:
    return _normcase(str(first)) == _normcase(str(second))


def _under(child: pathlib.Path, parent: pathlib.Path) -> bool:
    """Containment on normalized paths, never on a string prefix."""

    return (
        child == parent
        or parent in child.parents
        or _normcase(str(child)).startswith(_normcase(str(parent)) + os.sep)
    )


# ``site`` locates the host user site through these variables: ``APPDATA`` first on
# Windows, with ``USERPROFILE`` or ``HOMEDRIVE``+``HOMEPATH`` as the fallbacks, and
# ``HOME`` elsewhere. They name the account the launcher runs as, not a shell's own
# settings.
_USER_SITE_ANCHORS = {
    "nt": ("APPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"),
    "posix": ("HOME",),
}


def _host_value(parent: Mapping[str, str] | None, name: str) -> str:
    """A fact about the host that a plan must not lose.

    A caller that supplies the name is believed. Otherwise the launcher's own
    environment answers, because these name the account and the platform the
    launcher runs as rather than the settings of the shell that asked for a plan.
    An empty or absent value stays absent rather than becoming an empty variable.
    """

    value = str((parent or {}).get(name) or "").strip()
    if not value:
        value = str(os.environ.get(name) or "").strip()
    return value


def _user_site_anchors(parent: Mapping[str, str] | None) -> dict[str, str]:
    """The anchors an interpreter needs to locate the user site of its own account.

    The caller's environment is not copied wholesale: only these names are carried,
    and only with a non-blank value.
    """

    names = _USER_SITE_ANCHORS.get("nt" if os.name == "nt" else "posix", ())
    anchors: dict[str, str] = {}
    for name in names:
        value = _host_value(parent, name)
        if value:
            anchors[name] = value
    return anchors


def scoped_environment(
    *,
    venv: str | os.PathLike[str],
    shared: bool,
    mode: str,
    parent: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The deterministic environment an entrypoint is launched with.

    The caller's environment is *not* copied. Only four things are set, and each
    one is justified:

    * ``VIRTUAL_ENV`` names the locked environment, so a diagnostic can tell which
      environment a process believed it was in;
    * the entrypoint marker records which transport was launched;
    * ``PYTHONNOUSERSITE`` is set only when the environment declares that it does
      not share base packages - when it declares sharing, suppressing the base
      user site would contradict the environment's own ``pyvenv.cfg`` rather than
      protect it;
    * the host's own user-site anchors are carried whenever the environment does
      not suppress its user site, because ``site`` derives the user site's location
      from them; a launch environment that dropped them would silently remove the
      packages the environment declared it could reach;
    * ``PATH`` is the environment's own scripts directory plus, on Windows, the
      system directory the platform loader needs; it is never the login shell's.

    The three host facts above - the user site's anchors, ``SystemRoot`` and the
    loader directory - survive a caller that supplies a partial environment,
    because an interpreter launched without them cannot initialize the platform
    socket layer at all.

    Because nothing else is inherited, two launches of the same locked plan in
    different shells produce the same environment - which is what lets a plan
    digest be written down and verified later.
    """

    environment: dict[str, str] = {
        "VIRTUAL_ENV": str(venv),
        ENVIRONMENT_MARKER: mode,
    }
    if not shared:
        environment["PYTHONNOUSERSITE"] = "1"
    else:
        environment.update(_user_site_anchors(parent))
    scripts = pathlib.Path(venv) / (
        _VENV_SCRIPTS_WINDOWS if os.name == "nt" else _VENV_SCRIPTS_POSIX
    )
    entries = [str(scripts)]
    if os.name == "nt":
        system_root = _host_value(parent, "SYSTEMROOT") or _host_value(parent, "WINDIR")
        if system_root:
            entries.append(str(pathlib.Path(system_root) / "system32"))
            environment["SYSTEMROOT"] = system_root
            environment["WINDIR"] = system_root
    environment["PATH"] = os.pathsep.join(entries)
    return environment


@dataclass(frozen=True)
class RuntimeLock:
    """The environment an entrypoint is locked to, as verified rather than claimed."""

    install_root: str
    version: str
    venv: str
    interpreter: str
    python_version: str
    python_requires: str
    component_version: str
    interpreter_prefix: str
    base_prefix: str
    sdk_package: str
    sdk_pin: str
    sdk_version: str
    sdk_path: str
    sdk_inside_environment: bool
    shared_base_packages: bool
    frozen: tuple[str, ...]
    lockfile: str
    lockfile_sha256: str

    @property
    def isolated(self) -> bool:
        return not _same_path(self.interpreter_prefix, self.base_prefix)

    def as_dict(self) -> dict[str, Any]:
        return {
            "install_root": self.install_root,
            "version": self.version,
            "venv": self.venv,
            "interpreter": self.interpreter,
            "python_version": self.python_version,
            "python_requires": self.python_requires,
            "component_version": self.component_version,
            "interpreter_prefix": self.interpreter_prefix,
            "base_prefix": self.base_prefix,
            "isolated": self.isolated,
            "sdk_package": self.sdk_package,
            "sdk_pin": self.sdk_pin,
            "sdk_version": self.sdk_version,
            "sdk_path": self.sdk_path,
            "sdk_inside_environment": self.sdk_inside_environment,
            "shared_base_packages": self.shared_base_packages,
            "frozen": list(self.frozen),
            "lockfile": self.lockfile,
            "lockfile_sha256": self.lockfile_sha256,
        }


def _probe_report(result: CommandResult) -> dict[str, Any]:
    lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    if not lines:
        raise EntrypointRefused("interpreter_probe_corrupt", _first_error_line(result))
    try:
        payload = json.loads(lines[-1])
    except ValueError as exc:
        raise EntrypointRefused("interpreter_probe_corrupt", _first_error_line(result)) from exc
    if not isinstance(payload, dict):
        raise EntrypointRefused("interpreter_probe_corrupt", _first_error_line(result))
    return payload


def resolve_runtime(
    install_root: str | os.PathLike[str],
    *,
    mode: str = MODE_STDIO,
    parent: Mapping[str, str] | None = None,
    probe: Runner | None = None,
) -> RuntimeLock:
    """Verify which environment this host would actually launch, and refuse a guess.

    A lockfile naming an interpreter is not proof that the interpreter is in the
    locked environment: the recorded path is therefore required to resolve inside
    this version's own ``venv`` directory, and the interpreter is then asked what
    it is. A bare interpreter, an interpreter outside the environment, an
    unsupported Python line or an SDK that is not the pin are all refusals rather
    than warnings, because every one of them means the running code is not the
    code the lock names.
    """

    if mode not in MODES:
        raise EntrypointRefused("mode_unknown", str(mode))
    run = probe or _run

    root = pathlib.Path(paths.bind_native_root(install_root))
    if not root.is_dir():
        raise EntrypointRefused("install_root_missing", str(root))
    current = read_current(root)
    if current is None:
        raise EntrypointRefused("current_missing", str(root / CURRENT_FILENAME))
    installed = validate_version(current.get("active"))

    directory = version_directory(root, installed)
    lockfile_path = directory / LOCKFILE_FILENAME
    document = read_lockfile(directory)
    recorded_version = str(document.get("version", ""))
    if recorded_version != installed:
        raise EntrypointRefused(
            "lockfile_version_mismatch", f"{recorded_version or 'absent'}!={installed}"
        )

    venv_dir = directory / VENV_DIRNAME
    python_section = document.get("python")
    recorded_python = ""
    component_version = ""
    if isinstance(python_section, Mapping):
        recorded_python = str(python_section.get("executable", "") or "")
        component_version = str(python_section.get("marker", "") or "")

    interpreter = pathlib.Path(recorded_python) if recorded_python else venv_python(venv_dir)
    if not interpreter.is_file():
        fallback = venv_python(venv_dir)
        if not fallback.is_file():
            raise EntrypointRefused("interpreter_missing", str(interpreter))
        interpreter = fallback
    try:
        resolved_interpreter = interpreter.resolve()
        resolved_venv = venv_dir.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise EntrypointRefused("interpreter_unresolvable", str(interpreter)) from exc
    if not _under(resolved_interpreter, resolved_venv):
        raise EntrypointRefused("interpreter_outside_environment", str(resolved_interpreter))

    metadata = read_venv_metadata(venv_dir)
    shared = shared_base_packages(metadata)
    environment = scoped_environment(venv=venv_dir, shared=shared, mode=mode, parent=parent)

    result = run(
        [str(resolved_interpreter), "-c", _PROBE_SOURCE, version.SDK_PACKAGE], env=environment
    )
    if result.returncode != 0:
        raise EntrypointRefused("interpreter_probe_failed", _first_error_line(result))
    report = _probe_report(result)

    python_version = str(report.get("version", ""))
    if not version.python_supported(python_version):
        raise EntrypointRefused("interpreter_version_unsupported", python_version)
    prefix = str(report.get("prefix", ""))
    base_prefix = str(report.get("base_prefix", ""))
    if not prefix or not base_prefix:
        raise EntrypointRefused("interpreter_probe_corrupt", _first_error_line(result))
    if _same_path(prefix, base_prefix):
        raise EntrypointRefused("interpreter_not_isolated", f"{prefix}")
    if not _same_path(prefix, resolved_venv):
        raise EntrypointRefused("interpreter_prefix_mismatch", f"{prefix}!={resolved_venv}")

    sdk_version = report.get("sdk")
    if not sdk_version:
        raise EntrypointRefused("sdk_not_installed", version.SDK_PACKAGE)
    if str(sdk_version) != version.SDK_PIN:
        raise EntrypointRefused("sdk_pin_mismatch", f"{sdk_version}!={version.SDK_PIN}")
    sdk_path = str(report.get("sdk_path", "") or "")

    frozen_raw = document.get("frozen", [])
    if not isinstance(frozen_raw, Sequence) or isinstance(frozen_raw, (str, bytes)):
        raise EntrypointRefused("lockfile_corrupt", str(lockfile_path))
    frozen = tuple(str(item) for item in frozen_raw)

    return RuntimeLock(
        install_root=str(root),
        version=installed,
        venv=str(resolved_venv),
        interpreter=str(resolved_interpreter),
        python_version=python_version,
        python_requires=version.PYTHON_REQUIRES,
        component_version=component_version,
        interpreter_prefix=prefix,
        base_prefix=base_prefix,
        sdk_package=version.SDK_PACKAGE,
        sdk_pin=version.SDK_PIN,
        sdk_version=str(sdk_version),
        sdk_path=sdk_path,
        sdk_inside_environment=bool(sdk_path) and _under(pathlib.Path(sdk_path), resolved_venv),
        shared_base_packages=shared,
        frozen=frozen,
        lockfile=str(lockfile_path),
        lockfile_sha256=sha256_file(lockfile_path),
    )


@dataclass(frozen=True)
class LaunchPlan:
    """One transport's launch surface: an executable, an argv array, and a scope."""

    mode: str
    executable: str
    arguments: tuple[str, ...]
    cwd: str
    environment: tuple[tuple[str, str], ...]
    runtime: RuntimeLock

    @property
    def argv(self) -> tuple[str, ...]:
        """The exact command line as an array. A shell never sees this plan."""

        return (self.executable, *self.arguments)

    @property
    def environment_map(self) -> dict[str, str]:
        return dict(self.environment)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "executable": self.executable,
            "arguments": list(self.arguments),
            "cwd": self.cwd,
            "environment": self.environment_map,
            "digest": plan_digest(self),
            "runtime": self.runtime.as_dict(),
        }


def plan_digest(plan: LaunchPlan) -> str:
    """A stable digest over everything that decides what a launch runs.

    The environment is part of the digest because it is built from scratch and is
    therefore reproducible; a plan digest that changed between shells would be
    useless to write down.
    """

    payload = {
        "mode": plan.mode,
        "executable": plan.executable,
        "arguments": list(plan.arguments),
        "cwd": plan.cwd,
        "environment": dict(plan.environment),
        "runtime": {
            "interpreter": plan.runtime.interpreter,
            "python_version": plan.runtime.python_version,
            "sdk_pin": plan.runtime.sdk_pin,
            "sdk_version": plan.runtime.sdk_version,
            "lockfile_sha256": plan.runtime.lockfile_sha256,
        },
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def host_launch_document(plan: LaunchPlan) -> dict[str, Any]:
    """The document a host configuration consumes for one entrypoint.

    ``command`` is an absolute interpreter and ``args`` is a list, so a path that
    contains a space, an apostrophe or a non-ASCII character arrives as one
    argument instead of being split by a shell. No string form of this plan is
    produced anywhere: a shell string is the failure this document prevents.
    """

    return {
        "entrypoint": plan.mode,
        "component": version.COMPONENT,
        "command": plan.executable,
        "args": list(plan.arguments),
        "cwd": plan.cwd,
        "env": plan.environment_map,
        "digest": plan_digest(plan),
    }


def _bind_host_header(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def http_arguments(
    *,
    name: str,
    host: Any = None,
    port: Any = None,
    allow_hosts: Sequence[str] = (),
    allow_origins: Sequence[str] = (),
    allow_non_loopback_bind: bool = False,
) -> tuple[str, ...]:
    """Validate the HTTP opt-in and render it as argv.

    The bind rules are not restated here: the existing transport settings own the
    address, and the existing security matchers own what a Host or Origin entry
    means. This function adds the check neither of them can make alone - a
    transport security allowlist must admit the address the gateway is about to
    bind, because a gateway whose own allowlist refuses it answers nothing.
    """

    host_value = http_transport.DEFAULT_HOST if host is None else str(host)
    port_value = http_transport.DEFAULT_PORT if port is None else port
    try:
        settings = http_transport.HttpTransportSettings(
            host=host_value,
            port=port_value,
            allow_non_loopback_bind=bool(allow_non_loopback_bind),
        )
    except http_transport.HttpTransportError as exc:
        raise EntrypointRefused(exc.code, exc.detail) from exc

    hosts = tuple(entry for entry in (str(item).strip() for item in allow_hosts) if entry)
    origins = tuple(entry for entry in (str(item).strip() for item in allow_origins) if entry)
    if not hosts or not origins:
        raise EntrypointRefused("transport_allowlist_required", f"{settings.host}:{settings.port}")
    for entry in (*hosts, *origins):
        if "*" in entry:
            raise EntrypointRefused("wildcard_allowlist_refused", entry[:DETAIL_LIMIT])
    bind_host = _bind_host_header(settings.host, settings.port)
    bind_origin = f"http://{bind_host}"
    if not security.host_allowed(bind_host, hosts):
        raise EntrypointRefused("bind_host_not_allowed", bind_host)
    if not security.origin_allowed(bind_origin, origins):
        raise EntrypointRefused("bind_origin_not_allowed", bind_origin)

    arguments = [
        "-m",
        "axiom_mcp.http",
        "--name",
        name,
        "--host",
        settings.host,
        "--port",
        str(settings.port),
    ]
    if allow_non_loopback_bind:
        arguments.append("--allow-non-loopback-bind")
    for entry in hosts:
        arguments.extend(("--allow-host", entry))
    for entry in origins:
        arguments.extend(("--allow-origin", entry))
    return tuple(arguments)


def launch_plan(
    mode: str,
    *,
    install_root: str | os.PathLike[str],
    name: str | None = None,
    host: Any = None,
    port: Any = None,
    allow_hosts: Sequence[str] = (),
    allow_origins: Sequence[str] = (),
    allow_non_loopback_bind: bool = False,
    cwd: str | os.PathLike[str] | None = None,
    parent: Mapping[str, str] | None = None,
    probe: Runner | None = None,
    activation_script: str | None = None,
    resolve_from_path: bool = False,
) -> LaunchPlan:
    """Build one entrypoint's launch plan, or refuse with the reason.

    ``activation_script`` and ``resolve_from_path`` exist only so the refusal is
    explicit and testable rather than an unstated assumption: the first needs a
    shell and the second can select a different environment than the locked one.
    Supplying either is refused, never honoured.
    """

    if activation_script:
        raise EntrypointRefused("activation_script_refused", str(activation_script)[:DETAIL_LIMIT])
    if resolve_from_path:
        raise EntrypointRefused("path_lookup_refused", version.COMPONENT)
    if mode not in MODES:
        raise EntrypointRefused("mode_unknown", str(mode))

    runtime = resolve_runtime(install_root, mode=mode, parent=parent, probe=probe)
    server_name = name or version.COMPONENT
    if mode == MODE_STDIO:
        if (
            host is not None
            or port is not None
            or allow_hosts
            or allow_origins
            or allow_non_loopback_bind
        ):
            raise EntrypointRefused("http_parameters_on_stdio", server_name)
        arguments = ("-m", "axiom_mcp.stdio", "--name", server_name)
    else:
        arguments = http_arguments(
            name=server_name,
            host=host,
            port=port,
            allow_hosts=allow_hosts,
            allow_origins=allow_origins,
            allow_non_loopback_bind=allow_non_loopback_bind,
        )

    working_directory = (
        pathlib.Path(runtime.install_root) if cwd is None else paths.bind_native_root(cwd)
    )
    if not working_directory.is_dir():
        raise EntrypointRefused("cwd_missing", str(working_directory))
    environment = scoped_environment(
        venv=runtime.venv,
        shared=runtime.shared_base_packages,
        mode=mode,
        parent=parent,
    )
    return LaunchPlan(
        mode=mode,
        executable=runtime.interpreter,
        arguments=arguments,
        cwd=str(working_directory),
        environment=tuple(sorted(environment.items())),
        runtime=runtime,
    )


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


def write_lock(
    install_root: str | os.PathLike[str],
    plans: Sequence[LaunchPlan],
    *,
    clock: Callable[[], float] | None = None,
) -> pathlib.Path:
    """Record the locked entrypoints for one install root.

    A lock with two plans from two different environments is refused rather than
    written, because the document names one runtime and a plan that did not come
    from it would be a claim the lock cannot support.
    """

    ordered = tuple(plans)
    if not ordered:
        raise EntrypointRefused("no_plans")
    modes = sorted(plan.mode for plan in ordered)
    if len(set(modes)) != len(modes):
        raise EntrypointRefused("plan_mode_duplicated", ",".join(modes))
    first = ordered[0].runtime
    for plan in ordered[1:]:
        runtime = plan.runtime
        if (
            runtime.interpreter != first.interpreter
            or runtime.lockfile_sha256 != first.lockfile_sha256
        ):
            raise EntrypointRefused("runtime_mismatch", plan.mode)
    root = pathlib.Path(paths.bind_native_root(install_root))
    document = {
        "layout": LOCK_LAYOUT_VERSION,
        "component": version.COMPONENT,
        "runtime": first.as_dict(),
        "plans": [plan.as_dict() for plan in ordered],
        "created_at": float(clock() if clock is not None else time.time()),
    }
    path = root / ENTRYPOINT_LOCK_FILENAME
    _write_json_atomic(path, document)
    return path


def read_lock(install_root: str | os.PathLike[str]) -> dict[str, Any] | None:
    """Load a recorded lock, or None when this install root has none."""

    path = pathlib.Path(install_root) / ENTRYPOINT_LOCK_FILENAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EntrypointRefused("lock_corrupt", str(path)) from exc
    if not isinstance(payload, dict):
        raise EntrypointRefused("lock_corrupt", str(path))
    return dict(payload)


def _runtime_reasons(recorded: Mapping[str, Any], current: RuntimeLock) -> list[str]:
    reasons: list[str] = []
    if str(recorded.get("interpreter", "")) != current.interpreter:
        reasons.append("lock_interpreter_mismatch")
    if str(recorded.get("lockfile_sha256", "")) != current.lockfile_sha256:
        reasons.append("lock_lockfile_mismatch")
    if str(recorded.get("sdk_version", "")) != current.sdk_version:
        reasons.append("lock_sdk_mismatch")
    return reasons


def entrypoint_lock_reasons(
    document: Mapping[str, Any],
    plans: Sequence[LaunchPlan],
) -> list[str]:
    """Every reason a recorded lock no longer describes these plans.

    The comparison is by digest, so a lock edited by hand, an environment replaced
    underneath a version directory, and a plan whose argument vector changed all
    report as drift instead of being partly trusted.
    """

    reasons: list[str] = []
    layout = document.get("layout")
    if layout != LOCK_LAYOUT_VERSION:
        reasons.append(f"lock_layout_unsupported:{layout}")
    component = str(document.get("component", ""))
    if component != version.COMPONENT:
        reasons.append(f"lock_component_mismatch:{component or 'absent'}")

    recorded_plans = document.get("plans")
    if not isinstance(recorded_plans, list):
        reasons.append("lock_plans_absent")
        return reasons
    index = {
        str(entry.get("mode")): entry for entry in recorded_plans if isinstance(entry, Mapping)
    }
    for plan in plans:
        entry = index.pop(plan.mode, None)
        if entry is None:
            reasons.append(f"lock_plan_missing:{plan.mode}")
            continue
        if str(entry.get("digest", "")) != plan_digest(plan):
            reasons.append(f"lock_plan_digest_mismatch:{plan.mode}")
    for mode in sorted(index):
        reasons.append(f"lock_plan_unexpected:{mode}")

    recorded_runtime = document.get("runtime")
    if isinstance(recorded_runtime, Mapping) and plans:
        reasons.extend(_runtime_reasons(recorded_runtime, plans[0].runtime))
    return reasons


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="axiom-mcp-entrypoints", description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    def add_plan_options(target: argparse.ArgumentParser) -> None:
        target.add_argument("--install-root", required=True, metavar="PATH")
        target.add_argument("--name", default=None, metavar="NAME")
        target.add_argument("--host", default=None, metavar="HOST")
        target.add_argument("--port", default=None, type=int, metavar="PORT")
        target.add_argument("--allow-host", action="append", default=[], metavar="HOST")
        target.add_argument("--allow-origin", action="append", default=[], metavar="ORIGIN")
        target.add_argument("--allow-non-loopback-bind", action="store_true")
        target.add_argument("--cwd", default=None, metavar="PATH")

    plan_parser = subcommands.add_parser("plan", help="Print the locked launch document.")
    plan_parser.add_argument("--mode", choices=MODES, required=True)
    plan_parser.add_argument(
        "--write-lock",
        action="store_true",
        help="Also record entrypoints.json in the install root.",
    )
    add_plan_options(plan_parser)

    verify_parser = subcommands.add_parser("verify", help="Re-verify a recorded lock.")
    verify_parser.add_argument("--mode", choices=MODES, action="append", required=True)
    add_plan_options(verify_parser)
    return parser


def _plan_arguments(args: argparse.Namespace, mode: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "install_root": args.install_root,
        "name": args.name,
        "host": args.host,
        "port": args.port,
        "allow_hosts": args.allow_host,
        "allow_origins": args.allow_origin,
        "allow_non_loopback_bind": args.allow_non_loopback_bind,
        "cwd": args.cwd,
    }


def _emit(document: Mapping[str, Any]) -> str:
    """Render one machine-readable document for a reader on the other side.

    ``ensure_ascii`` stays on deliberately. With it off, a path holding Thai or any
    other non-ASCII character leaves this process as raw characters in whatever code
    page the host's console or pipe happens to use, and the reader decodes mojibake -
    which is the very path this surface exists to deliver intact. As an escape
    sequence the document is ASCII on the wire and carries the exact path.
    """

    return json.dumps(document)


def main(argv: Sequence[str] | None = None) -> int:
    """Print one launch document, or the reasons a recorded lock has drifted.

    ``verify`` answers about a recorded lock, so an install root that holds no lock
    is reported as ``lock_absent`` before any runtime is resolved: a missing record
    is the answer, not a launch failure.
    """

    parser = _build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
        if args.command == "plan":
            plan = launch_plan(**_plan_arguments(args, args.mode))
            document = host_launch_document(plan)
            if args.write_lock:
                document["lock"] = str(write_lock(args.install_root, (plan,)))
            print(_emit(document))
            return EXIT_SUCCESS
        root = pathlib.Path(paths.bind_native_root(args.install_root))
        if not root.is_dir():
            raise EntrypointRefused("install_root_missing", str(root))
        recorded = read_lock(root)
        if recorded is None:
            print(_emit({"ok": False, "reasons": ["lock_absent"]}))
            return EXIT_VALIDATION
        plans = tuple(launch_plan(**_plan_arguments(args, mode)) for mode in args.mode)
        reasons = entrypoint_lock_reasons(recorded, plans)
        print(
            _emit(
                {
                    "ok": not reasons,
                    "modes": [plan.mode for plan in plans],
                    "reasons": reasons,
                }
            )
        )
        return EXIT_SUCCESS if not reasons else EXIT_VALIDATION
    except EntrypointRefused as exc:
        print(_emit({"ok": False, "code": exc.code, "detail": exc.detail}), file=sys.stderr)
        return EXIT_VALIDATION
    except SystemExit as exc:  # --help writes and exits zero; usage errors exit two
        return int(exc.code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
