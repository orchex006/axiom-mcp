"""Command line surface for axiom-mcp: version and doctor.

Both commands answer a question an operator asks before trusting the gateway,
and both are deliberately boring: a single JSON object on stdout in machine
mode, prose on stderr free of secrets, and a stable exit code.

doctor is the interesting one. The canonical contract separates
/healthz (minimal process health) from /readyz (query and control
availability), and the failure this module exists to prevent is a supervisor
that treats a listening port as proof the gateway can answer. So doctor
never consults /healthz at all: it reports the runtime pins, the pinned SDK
surface, the advertised protocol revisions, the canonical version dimensions,
the credential scope the gateway will enforce, and the data-plane readiness
separately, and it states in the report itself which basis it used.

Two rules shape the credential section. A reference is configuration and a
token is a secret, so the registry file names a credential (an environment
variable or a file) rather than carrying it - consistent with the C-005 rule
that config never holds a literal - and no report ever contains a token value,
only a declared scope and a count. And an unresolvable reference is reported as
a specific reason instead of collapsing into a generic failure, because the
variable you named is not set and the token is not registered are different
problems with different fixes.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from axiom_mcp import errors, http, security, version

# Canonical CLI exit codes from docs/16-CLI-AND-CONTROL-API.md section 6.
EXIT_SUCCESS = 0
EXIT_VALIDATION = 2
EXIT_NOT_FOUND = 3
EXIT_NOT_READY = 4
EXIT_AUTHORIZATION = 5
EXIT_CONFLICT = 6
EXIT_TIMEOUT = 7
EXIT_IO = 8
EXIT_INCOMPATIBLE = 9
EXIT_LOCK_UNAVAILABLE = 10
EXIT_PARTIAL = 20

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"

VERDICT_READY = "ready"
VERDICT_NOT_READY = "not_ready"

SECTION_ORDER = ("runtime", "sdk_surface", "protocol", "dimensions", "credentials", "readiness")

INCOMPATIBILITY_SECTIONS = frozenset({"runtime", "sdk_surface", "protocol", "dimensions"})

BUILD_REVISION_ENV = "AXIOM_MCP_BUILD_REVISION"
CREDENTIAL_REFERENCE_ENV = "AXIOM_MCP_TOKEN_REFERENCE"
CREDENTIAL_REGISTRY_ENV = "AXIOM_MCP_TOKEN_REGISTRY"

UNKNOWN_BUILD_REVISION = "unknown"

# Canonical error code to CLI exit code. The contract fixes both lists; this
# table is this component's reading of which bucket each code belongs in, and it
# is total so a code can never fall through to a zero exit.
CODE_TO_EXIT: dict[str, int] = {
    "VALIDATION_ERROR": EXIT_VALIDATION,
    "UNSUPPORTED_OPERATION": EXIT_VALIDATION,
    "LIMIT_EXCEEDED": EXIT_VALIDATION,
    "NOT_FOUND": EXIT_NOT_FOUND,
    "PROJECT_UNAVAILABLE": EXIT_NOT_FOUND,
    "NOT_READY": EXIT_NOT_READY,
    "SNAPSHOT_EXPIRED": EXIT_NOT_READY,
    "SNAPSHOT_UNAVAILABLE": EXIT_NOT_READY,
    "UNAUTHENTICATED": EXIT_AUTHORIZATION,
    "FORBIDDEN": EXIT_AUTHORIZATION,
    "CONFLICT": EXIT_CONFLICT,
    "RATE_LIMITED": EXIT_TIMEOUT,
    "DAEMON_UNAVAILABLE": EXIT_IO,
    "SNAPSHOT_CORRUPT": EXIT_IO,
    "INTERNAL_ERROR": EXIT_IO,
    "INCOMPATIBLE_INPUT": EXIT_INCOMPATIBLE,
}


class UsageError(RuntimeError):
    """An invalid invocation, reported with the canonical validation exit code."""


@dataclass(frozen=True)
class Section:
    """One diagnosed area, with the reasons it is not healthy."""

    name: str
    status: str
    details: Mapping[str, Any] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "reasons": list(self.reasons),
        }
        if self.details:
            payload["details"] = dict(self.details)
        return payload


@dataclass(frozen=True)
class CredentialScope:
    """What the gateway will require of a caller, and what it found configured.

    A token value never appears here. The fields describe configuration (which
    reference was named, whether it resolved) and authorization (which
    capabilities and solution scope the gateway enforces, and when a registry is
    supplied, which scope the resolved credential actually carries).
    """

    configured: bool
    reference_kind: str | None = None
    reference_name: str | None = None
    resolved: bool = False
    reason: str | None = None
    registry_size: int | None = None
    registered: bool | None = None
    granted_capabilities: tuple[str, ...] = ()
    solution_ids: tuple[str, ...] = ()
    project_ids: tuple[str, ...] | None = None

    def as_dict(self, root: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "configured": self.configured,
            "reference_kind": self.reference_kind,
            "reference_name": (
                None
                if self.reference_name is None
                else errors.redact_text(self.reference_name, root)
            ),
            "resolved": self.resolved,
            "reason": self.reason,
        }
        if self.registry_size is not None:
            payload["registry_size"] = self.registry_size
            payload["registered"] = self.registered
        if self.registered:
            payload["granted_capabilities"] = list(self.granted_capabilities)
            payload["solution_ids"] = list(self.solution_ids)
            payload["project_ids"] = None if self.project_ids is None else list(self.project_ids)
        return payload


@dataclass(frozen=True)
class DoctorEnvironment:
    """Every fact doctor observes, so a test can supply them instead of the host."""

    python_version: tuple[int, int, int]
    sdk_version: str | None
    runtime_versions: Mapping[str, str | None]
    advertised_protocols: tuple[str, ...]
    surface_reasons: tuple[str, ...]
    credential: CredentialScope
    build_revision: str = UNKNOWN_BUILD_REVISION
    query_plane_available: bool = False
    query_plane_detail: str | None = None
    control_plane_available: bool = False
    control_plane_detail: str | None = None
    root: str | None = None


def _git_revision(repo_root: str | pathlib.Path | None = None) -> str | None:
    """Read a short revision when running inside a checkout; never raise."""
    root = (
        pathlib.Path(repo_root)
        if repo_root is not None
        else pathlib.Path(__file__).resolve().parents[2]
    )
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def resolve_build_revision(
    environ: Mapping[str, str] | None = None,
    *,
    repo_root: str | pathlib.Path | None = None,
) -> str:
    """Return the build revision, preferring an explicit setting over a probe.

    A release artifact carries no checkout, so the environment variable is the
    authoritative source; the probe is a convenience for a developer tree and
    yields the documented unknown marker rather than an empty field.
    """
    source = os.environ if environ is None else environ
    explicit = (source.get(BUILD_REVISION_ENV) or "").strip()
    if explicit:
        return explicit
    return _git_revision(repo_root) or UNKNOWN_BUILD_REVISION


def load_registry(
    path: str | pathlib.Path,
    environ: Mapping[str, str] | None = None,
) -> tuple[security.TokenRegistry, list[str]]:
    """Build a registry from a file that names references instead of tokens.

    Returns the registry and the list of entries that could not be registered.
    An entry that fails is skipped rather than aborting the load, so one stale
    credential does not hide the state of the others.
    """
    registry = security.TokenRegistry()
    skipped: list[str] = []
    try:
        document = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return registry, ["credential_registry_unreadable"]
    entries = document.get("tokens") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        return registry, ["credential_registry_malformed"]
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            skipped.append(f"credential_registry_entry_invalid:{index}")
            continue
        try:
            reference = security.token_reference_from_config(entry)
            scope = security.ScopedToken(
                token_id=str(entry.get("token_id", "")).strip(),
                audience=str(entry.get("audience", security.MCP_AUDIENCE)).strip(),
                capabilities=frozenset(entry.get("capabilities", ())),
                solution_ids=frozenset(entry.get("solution_ids", ())),
                project_ids=(
                    None if entry.get("project_ids") is None else frozenset(entry["project_ids"])
                ),
            )
            registry.register(reference, scope, env=environ)
        except (security.SecurityError, TypeError, ValueError):
            skipped.append(f"credential_registry_entry_rejected:{index}")
    return registry, skipped


def credential_scope(
    environ: Mapping[str, str] | None = None,
    *,
    registry_path: str | pathlib.Path | None = None,
) -> CredentialScope:
    """Report the configured credential reference and, if registered, its scope."""
    source = os.environ if environ is None else environ
    raw = (source.get(CREDENTIAL_REFERENCE_ENV) or "").strip()
    if not raw:
        return CredentialScope(configured=False, reason="credential_reference_not_configured")
    try:
        config = json.loads(raw)
    except json.JSONDecodeError:
        return CredentialScope(configured=True, reason="credential_reference_not_json")
    if not isinstance(config, dict):
        return CredentialScope(configured=True, reason="credential_reference_not_json")
    try:
        reference = security.token_reference_from_config(config)
    except security.SecurityError:
        return CredentialScope(configured=True, reason="credential_reference_invalid")
    kind = getattr(reference, "kind", None)
    name = str(getattr(reference, "variable", "") or getattr(reference, "path", ""))
    token = reference.resolve(source)
    if not token:
        return CredentialScope(
            configured=True,
            reference_kind=kind,
            reference_name=name,
            reason="credential_reference_unresolved",
        )

    configured_registry = (
        registry_path or (source.get(CREDENTIAL_REGISTRY_ENV) or "").strip() or None
    )
    if configured_registry is None:
        return CredentialScope(
            configured=True,
            reference_kind=kind,
            reference_name=name,
            resolved=True,
            reason="credential_registry_not_configured",
        )
    registry, skipped = load_registry(configured_registry, source)
    if skipped:
        return CredentialScope(
            configured=True,
            reference_kind=kind,
            reference_name=name,
            resolved=True,
            reason=skipped[0],
        )
    scope = CredentialScope(
        configured=True,
        reference_kind=kind,
        reference_name=name,
        resolved=True,
        registry_size=registry.size,
    )
    try:
        principal = registry.authenticate(token)
    except security.SecurityError:
        return CredentialScope(
            **{**scope.__dict__, "registered": False, "reason": "credential_not_registered"}
        )
    return CredentialScope(
        configured=True,
        reference_kind=kind,
        reference_name=name,
        resolved=True,
        registry_size=registry.size,
        registered=True,
        granted_capabilities=tuple(sorted(principal.capabilities)),
        solution_ids=tuple(sorted(principal.solution_ids)),
        project_ids=None if principal.project_ids is None else tuple(sorted(principal.project_ids)),
    )


def collect_environment(
    environ: Mapping[str, str] | None = None,
    *,
    registry_path: str | pathlib.Path | None = None,
    repo_root: str | pathlib.Path | None = None,
    resolver: Any = None,
) -> DoctorEnvironment:
    """Observe the real machine. Every value here is a fact, not a judgement."""
    runtime_versions = version.installed_versions()
    sdk_version = runtime_versions.get(version.SDK_PACKAGE)
    try:
        advertised = version.advertised_protocol_versions(resolver)
    except Exception:  # noqa: BLE001 - an unimportable SDK is a reportable fact
        advertised = ()
    try:
        surface = tuple(version.sdk_surface_reasons(resolver=resolver))
    except Exception as exc:  # noqa: BLE001 - same reasoning as above
        surface = (f"sdk_surface_probe_failed:{type(exc).__name__}",)
    readiness = http.default_readiness()
    return DoctorEnvironment(
        python_version=(sys.version_info.major, sys.version_info.minor, sys.version_info.micro),
        sdk_version=sdk_version,
        runtime_versions=runtime_versions,
        advertised_protocols=tuple(advertised),
        surface_reasons=surface,
        credential=credential_scope(environ, registry_path=registry_path),
        build_revision=resolve_build_revision(environ, repo_root=repo_root),
        query_plane_available=readiness.query.available,
        query_plane_detail=readiness.query.detail,
        control_plane_available=readiness.control.available,
        control_plane_detail=readiness.control.detail,
        root=str(pathlib.Path(__file__).resolve().parents[2]),
    )


# Credentials that are merely absent are a warning, not a failure: a machine
# that has not been wired to a registry yet is a normal state, while a reference
# that was named and does not resolve is a misconfiguration.
CREDENTIAL_WARN_REASONS = frozenset(
    {"credential_reference_not_configured", "credential_registry_not_configured"}
)

DIMENSION_KEYS = (
    "graph_schema",
    "queue_schema",
    "control_api",
    "workspace_layout",
    "solution_config",
    "bootstrap_manifest",
)


def _runtime_section(env: DoctorEnvironment) -> Section:
    reasons = list(version.sdk_compatibility_reasons(env.sdk_version, env.python_version))
    for name, pinned in version.RUNTIME_PINS.items():
        installed = env.runtime_versions.get(name)
        if installed is None:
            reasons.append(f"runtime_not_installed:{name}")
        elif installed != pinned:
            reasons.append(f"runtime_version_unsupported:{name}:{installed}")
    details = {
        "python": ".".join(str(part) for part in env.python_version),
        "python_requires": version.PYTHON_REQUIRES,
        "sdk_package": version.SDK_PACKAGE,
        "sdk_pin": version.SDK_PIN,
        "sdk_installed": env.sdk_version,
        "runtime_pins": dict(version.RUNTIME_PINS),
        "runtime_installed": dict(env.runtime_versions),
    }
    return Section("runtime", STATUS_FAIL if reasons else STATUS_OK, details, tuple(reasons))


def _sdk_surface_section(env: DoctorEnvironment) -> Section:
    reasons = list(env.surface_reasons)
    details = {"locked_names": len(version.SDK_SURFACE), "probed": True}
    return Section("sdk_surface", STATUS_FAIL if reasons else STATUS_OK, details, tuple(reasons))


def _protocol_section(env: DoctorEnvironment) -> Section:
    reasons = version.protocol_support_reasons(env.advertised_protocols)
    details = {
        "minimum": version.MCP_PROTOCOL_MINIMUM,
        "advertised": list(env.advertised_protocols),
        "advertisement_module": version.PROTOCOL_ADVERTISEMENT_MODULE,
    }
    return Section("protocol", STATUS_FAIL if reasons else STATUS_OK, details, tuple(reasons))


def _dimensions_section(env: DoctorEnvironment) -> Section:
    del env
    reasons: list[str] = []
    details: dict[str, Any] = {
        "spec_version": version.SPEC_VERSION,
        "spec_revision": version.SPEC_REVISION,
        "accepted_graph_schema_major": version.GRAPH_SCHEMA,
    }
    for key in DIMENSION_KEYS:
        value = version.SPEC_DIMENSIONS.get(key)
        details[key] = value
        if not isinstance(value, int) or value < 1:
            reasons.append(f"dimension_unusable:{key}")
    return Section("dimensions", STATUS_FAIL if reasons else STATUS_OK, details, tuple(reasons))


def _credentials_section(env: DoctorEnvironment) -> Section:
    scope = env.credential
    details: dict[str, Any] = dict(scope.as_dict(env.root))
    details["required_audience"] = security.MCP_AUDIENCE
    details["control_audience"] = security.CONTROL_AUDIENCE
    details["enforced_capabilities"] = sorted(security.KNOWN_CAPABILITIES)
    details["mutation_capabilities"] = sorted(security.MUTATION_CAPABILITIES)
    details["tool_capability_map"] = dict(security.TOOL_CAPABILITY)
    reasons: list[str] = []
    status = STATUS_OK
    if not scope.configured or scope.reason in CREDENTIAL_WARN_REASONS:
        status = STATUS_WARN
    elif scope.reason is not None:
        reasons.append(scope.reason)
        status = STATUS_FAIL
    return Section("credentials", status, details, tuple(reasons))


def _readiness_section(env: DoctorEnvironment) -> Section:
    reasons: list[str] = []
    if not env.query_plane_available:
        reasons.append("query_plane_unavailable")
    if not env.control_plane_available:
        reasons.append("control_plane_unavailable")
    details = {
        "basis": "runtime_pins_and_data_plane_probes",
        "process_health_is_readiness": False,
        "health_path": http.HEALTH_PATH,
        "ready_path": http.READY_PATH,
        "mcp_endpoint_path": http.MCP_ENDPOINT_PATH,
        "query_plane_available": env.query_plane_available,
        "query_plane_detail": env.query_plane_detail,
        "control_plane_available": env.control_plane_available,
        "control_plane_detail": env.control_plane_detail,
    }
    return Section("readiness", STATUS_FAIL if reasons else STATUS_OK, details, tuple(reasons))


def doctor_sections(env: DoctorEnvironment) -> tuple[Section, ...]:
    """Return every section in the documented report order."""
    built = {
        "runtime": _runtime_section(env),
        "sdk_surface": _sdk_surface_section(env),
        "protocol": _protocol_section(env),
        "dimensions": _dimensions_section(env),
        "credentials": _credentials_section(env),
        "readiness": _readiness_section(env),
    }
    return tuple(built[name] for name in SECTION_ORDER)


def doctor_exit_code(sections: Sequence[Section]) -> int:
    """Map the failed sections onto the canonical exit codes."""
    failed = {section.name for section in sections if section.status == STATUS_FAIL}
    if failed & INCOMPATIBILITY_SECTIONS:
        return EXIT_INCOMPATIBLE
    if "credentials" in failed:
        return EXIT_AUTHORIZATION
    if failed:
        return EXIT_NOT_READY
    return EXIT_SUCCESS


def doctor_report(env: DoctorEnvironment) -> dict[str, Any]:
    """Build the machine-readable doctor report.

    ``readiness`` is reported on its own axis. A caller that wants a simple
    go/no-go reads ``ok``; a caller that wants to know *why* reads ``reasons``.
    """
    sections = doctor_sections(env)
    reasons = [f"{section.name}:{reason}" for section in sections for reason in section.reasons]
    return {
        "component": version.COMPONENT,
        "version": version.VERSION,
        "build_revision": env.build_revision,
        "spec_version": version.SPEC_VERSION,
        "spec_revision": version.SPEC_REVISION,
        "ok": not reasons,
        "verdict": VERDICT_NOT_READY if reasons else VERDICT_READY,
        "exit_code": doctor_exit_code(sections),
        "warnings": [s.name for s in sections if s.status == STATUS_WARN],
        "reasons": reasons,
        "sections": [section.as_dict() for section in sections],
    }


def format_doctor_text(report: Mapping[str, Any]) -> str:
    """Render the report for a human without losing the machine facts."""
    lines = [
        f"{report['component']} {report['version']} (build {report['build_revision']})",
        f"spec {report['spec_version']} @ {report['spec_revision']}",
        f"verdict: {report['verdict']} (exit {report['exit_code']})",
    ]
    for section in report["sections"]:
        line = f"  {section['name']:<12} {section['status']}"
        if section["reasons"]:
            line += "  " + ", ".join(section["reasons"])
        lines.append(line)
    if report["warnings"]:
        lines.append(f"warnings: {', '.join(report['warnings'])}")
    return "\n".join(lines)


def format_version_text(report: Mapping[str, Any]) -> str:
    """Render the version report as one ``key: value`` line per contract field."""
    return "\n".join(f"{field}: {report[field]}" for field in version.VERSION_REPORT_FIELDS)


def exit_code_for_error(error: errors.AxiomError) -> int:
    """Map a canonical code onto the CLI exit code, defaulting to I/O internal."""
    return CODE_TO_EXIT.get(error.code, EXIT_IO)


class _Parser(argparse.ArgumentParser):
    """An argument parser that reports a bad invocation as a value, not a raise."""

    def error(self, message: str) -> Any:
        raise UsageError(message)


def build_parser() -> argparse.ArgumentParser:
    """Build the ``axiom-mcp`` parser.

    Only the subcommands this revision implements are registered. The remaining
    canonical subcommands belong to later tasks, and a name that is not
    registered fails as a validation error rather than being silently ignored -
    the contract requires an invalid flag or command to error.
    """
    parser = _Parser(
        prog=version.COMPONENT,
        description="axiom-mcp: processed-JSON query gateway over the official MCP SDK.",
    )
    subcommands = parser.add_subparsers(dest="command", metavar="COMMAND")

    version_parser = subcommands.add_parser(
        "version", help="Report component identity and versions."
    )
    version_parser.add_argument(
        "--json", action="store_true", help="Emit a single JSON object on stdout."
    )

    doctor_parser = subcommands.add_parser(
        "doctor", help="Diagnose runtime pins, SDK surface, scope and readiness."
    )
    doctor_parser.add_argument(
        "--json", action="store_true", help="Emit a single JSON object on stdout."
    )
    doctor_parser.add_argument(
        "--registry",
        default=None,
        metavar="PATH",
        help="Credential registry that names token references; no token value is read.",
    )
    return parser


def _run_version(*, as_json: bool, environ: Mapping[str, str] | None = None) -> int:
    report = version.version_report(resolve_build_revision(environ), "not_checked")
    if as_json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(format_version_text(report))
    return EXIT_SUCCESS


def _run_doctor(
    *,
    as_json: bool,
    environ: Mapping[str, str] | None = None,
    registry_path: str | None = None,
) -> int:
    environment = collect_environment(environ, registry_path=registry_path)
    report = doctor_report(environment)
    if as_json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(format_doctor_text(report))
    return int(report["exit_code"])


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``axiom-mcp`` command line and return its canonical exit code."""
    parser = build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except UsageError as exc:
        print(f"{version.COMPONENT}: {exc}", file=sys.stderr)
        return EXIT_VALIDATION
    except SystemExit as exc:  # --help and --version write and exit zero
        return int(exc.code or 0)
    if args.command == "version":
        return _run_version(as_json=args.json)
    if args.command == "doctor":
        return _run_doctor(as_json=args.json, registry_path=args.registry)
    parser.print_help(sys.stderr)
    return EXIT_VALIDATION


if __name__ == "__main__":
    raise SystemExit(main())
