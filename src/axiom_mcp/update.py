"""Version check and approved-plan update delegation for axiom-mcp.

The canonical contract fixes two command surfaces for this component,
``update check`` and ``update apply --plan``, and fixes one prohibition: a
running process is never pip-upgraded in place. A Python package lives in a
versioned virtual environment, and the swap is performed by an external updater
process after this process stops.

So this module performs no install, no download and no environment mutation.
``check`` reports the canonical facts and refuses to guess; ``apply`` validates
one plan document against the evaluator the specification already ships -
``tools/update_plan_contract.py`` at the pinned revision - and then hands the
approved plan to the external ``axiom`` updater as an argument list.

Two refusals are the point of the slice. A plan that is not bound to a recorded
approval never reaches the updater, and a plan whose install root would land on
the running interpreter or its site-packages is refused as an in-place upgrade,
because that is exactly the operation the contract forbids and the shape it
takes when it happens by accident.

The plan contract is consumed, never re-derived: this module loads the
specification evaluator by path and reports its reasons verbatim, so a
specification change this component does not follow shows up as a failing
conformance test instead of a silent divergence.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import sys
import sysconfig
import types
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from axiom_mcp import version

COMPONENT = version.COMPONENT

CHANNELS: tuple[str, ...] = ("stable", "prerelease")

# Result fields the canonical contract requires every component to separate.
CHECK_FIELDS: tuple[str, ...] = (
    "component",
    "installed",
    "available",
    "compatible",
    "channel",
    "schema_range",
    "update_policy",
    "source_origin",
    "needs_restart",
)

STATUS_NOT_CHECKED = "not_checked"
STATUS_CURRENT = "current"
STATUS_AVAILABLE = "available"
STATUS_OFFLINE = "offline"
STATUS_UNCONFIGURED = "unconfigured"
STATUS_BLOCKED = "blocked"

CHANNEL_ENV = "AXIOM_MCP_UPDATE_CHANNEL"
ORIGIN_ENV = "AXIOM_MCP_UPDATE_ORIGIN"
METADATA_ENV = "AXIOM_MCP_UPDATE_METADATA"
OFFLINE_ENV = "AXIOM_MCP_OFFLINE"
AUTO_CHECK_ENV = "AXIOM_MCP_UPDATE_AUTO_CHECK"
AUTO_APPLY_ENV = "AXIOM_MCP_UPDATE_AUTO_APPLY"

# An origin is only trusted when it is the canonical repository or when the owner
# added it to the configured allowlist. A guessed owner is refused, never contacted.
CANONICAL_ORIGIN = "https://github.com/orchex006/axiom-mcp"
ALLOWED_ORIGINS: tuple[str, ...] = (CANONICAL_ORIGIN,)
ALLOWED_ORIGINS_ENV = "AXIOM_MCP_UPDATE_ALLOWED_ORIGINS"

ORCHESTRATOR = "axiom"
ORCHESTRATOR_SUBCOMMAND: tuple[str, ...] = ("update", "apply", "--plan")

PIP_PROGRAMS = frozenset({"pip", "pip3", "uv", "uvx", "easy_install"})

EVALUATOR_RELATIVE = pathlib.Path("tools") / "update_plan_contract.py"
EVALUATOR_MARKER = pathlib.Path("tests") / "test_update_plan_contract.py"

_SPECS_ROOT_ENV = "AXIOM_SPECS_ROOT"


class UpdateUnavailable(RuntimeError):
    """The delegation target is absent, so nothing may be certified or applied."""


class PlanRejected(RuntimeError):
    """One plan document was refused. The named reasons are the canonical ones."""

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = tuple(str(reason) for reason in reasons)
        detail = ", ".join(self.reasons) if self.reasons else "no reason recorded"
        super().__init__("plan rejected: " + detail)


class InPlaceUpgradeRefused(PlanRejected):
    """The plan would rewrite the running interpreter instead of a staged root."""


def _flag(value: str | None, *, default: bool) -> bool:
    """Read a documented on/off setting, falling back to its default."""
    if value is None or value.strip() == "":
        return default
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on", "enabled"}:
        return True
    if lowered in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def allowed_origins(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """The canonical origin plus any origin the owner added to the allowlist."""
    env = os.environ if environ is None else environ
    configured = env.get(ALLOWED_ORIGINS_ENV) or ""
    extra = [item.strip() for item in configured.split(",") if item.strip()]
    ordered: list[str] = list(ALLOWED_ORIGINS)
    for item in extra:
        if item not in ordered:
            ordered.append(item)
    return tuple(ordered)


def schema_range() -> dict[str, int]:
    """The schema majors this component accepts, from the canonical dimensions."""
    return {
        "graph_schema": version.GRAPH_SCHEMA,
        "queue_schema": version.QUEUE_SCHEMA,
        "control_api": version.CONTROL_API,
    }


def update_policy(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The update policy in force, mirroring the canonical check policy."""
    env = os.environ if environ is None else environ
    return {
        "auto_check": _flag(env.get(AUTO_CHECK_ENV), default=True),
        "auto_apply": env.get(AUTO_APPLY_ENV) or "disabled",
        "plan_approval_required": True,
        "offline": _flag(env.get(OFFLINE_ENV), default=False),
    }


@dataclass(frozen=True)
class UpdateCheck:
    """One update check result: the canonical fields plus the stated basis."""

    installed: str
    available: str | None
    compatible: bool | None
    channel: str
    source_origin: str | None
    status: str
    needs_restart: bool
    reasons: tuple[str, ...] = ()
    policy: Mapping[str, Any] | None = None

    def as_report(self) -> dict[str, Any]:
        return {
            "component": COMPONENT,
            "installed": self.installed,
            "available": self.available,
            "compatible": self.compatible,
            "channel": self.channel,
            "schema_range": schema_range(),
            "update_policy": dict(self.policy or {}),
            "source_origin": self.source_origin,
            "needs_restart": self.needs_restart,
            "status": self.status,
            "reasons": list(self.reasons),
        }


def check_update(
    environ: Mapping[str, str] | None = None,
    *,
    metadata: Mapping[str, Any] | None = None,
    metadata_path: str | pathlib.Path | None = None,
    installed_version: str | None = None,
) -> UpdateCheck:
    """Answer the check question without guessing.

    The one rule this function exists to enforce is that an absent or
    unreachable answer is never rendered as up to date. ``available`` stays
    ``None`` and the status names the actual condition, so a caller cannot read
    ``current`` out of a machine that never checked.
    """
    env = os.environ if environ is None else environ
    installed = installed_version or version.VERSION
    policy = update_policy(env)

    channel = (env.get(CHANNEL_ENV) or "stable").strip()
    if channel not in CHANNELS:
        return UpdateCheck(
            installed=installed,
            available=None,
            compatible=None,
            channel=channel,
            source_origin=None,
            status=STATUS_BLOCKED,
            needs_restart=False,
            reasons=(f"unsupported_channel:{channel}",),
            policy=policy,
        )

    origin = (env.get(ORIGIN_ENV) or "").strip() or None

    if _flag(env.get(OFFLINE_ENV), default=False):
        return UpdateCheck(
            installed=installed,
            available=None,
            compatible=None,
            channel=channel,
            source_origin=origin,
            status=STATUS_OFFLINE,
            needs_restart=False,
            reasons=("offline_requested",),
            policy=policy,
        )

    if origin is None:
        return UpdateCheck(
            installed=installed,
            available=None,
            compatible=None,
            channel=channel,
            source_origin=None,
            status=STATUS_UNCONFIGURED,
            needs_restart=False,
            reasons=("no_verified_metadata_source",),
            policy=policy,
        )
    if origin not in allowed_origins(env):
        return UpdateCheck(
            installed=installed,
            available=None,
            compatible=None,
            channel=channel,
            source_origin=origin,
            status=STATUS_BLOCKED,
            needs_restart=False,
            reasons=(f"origin_not_allowlisted:{origin}",),
            policy=policy,
        )

    document = metadata
    if document is None:
        candidate = metadata_path if metadata_path is not None else env.get(METADATA_ENV)
        if candidate:
            try:
                document = json.loads(pathlib.Path(candidate).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                return UpdateCheck(
                    installed=installed,
                    available=None,
                    compatible=None,
                    channel=channel,
                    source_origin=origin,
                    status=STATUS_BLOCKED,
                    needs_restart=False,
                    reasons=(f"metadata_unreadable:{type(exc).__name__}",),
                    policy=policy,
                )
    if document is None:
        return UpdateCheck(
            installed=installed,
            available=None,
            compatible=None,
            channel=channel,
            source_origin=origin,
            status=STATUS_UNCONFIGURED,
            needs_restart=False,
            reasons=("no_verified_metadata_source",),
            policy=policy,
        )

    available = document.get("available")
    if not isinstance(available, str) or available.strip() == "":
        return UpdateCheck(
            installed=installed,
            available=None,
            compatible=None,
            channel=channel,
            source_origin=origin,
            status=STATUS_NOT_CHECKED,
            needs_restart=False,
            reasons=("metadata_names_no_available_version",),
            policy=policy,
        )

    declared = document.get("compatible")
    compatible = declared if isinstance(declared, bool) else None
    if compatible is False:
        status = STATUS_BLOCKED
    elif available == installed:
        status = STATUS_CURRENT
    else:
        status = STATUS_AVAILABLE
    return UpdateCheck(
        installed=installed,
        available=available,
        compatible=compatible,
        channel=channel,
        source_origin=origin,
        status=status,
        needs_restart=status in {STATUS_AVAILABLE, STATUS_BLOCKED},
        reasons=() if status != STATUS_BLOCKED else ("available_version_incompatible",),
        policy=policy,
    )


def check_exit_code(check: UpdateCheck) -> int:
    """A blocked check is an invalid configuration; every other answer is a fact."""
    return 2 if check.status == STATUS_BLOCKED else 0


def find_specs_root(start: pathlib.Path | None = None) -> pathlib.Path | None:
    """Locate the pinned specification checkout that owns the plan contract."""
    candidates: list[pathlib.Path] = []
    configured = os.environ.get(_SPECS_ROOT_ENV)
    if configured:
        candidates.append(pathlib.Path(configured))
    anchor = start or pathlib.Path(__file__).resolve()
    for base in anchor.parents:
        candidates.append(base / "axiom-specs")
        candidates.append(base / "axiom-specs-c")
        if base.is_dir():
            candidates.extend(
                sorted(child for child in base.glob("axiom-specs*") if child.is_dir())
            )
    for candidate in candidates:
        if (candidate / EVALUATOR_MARKER).is_file():
            return candidate
    return None


def load_canonical_evaluator(specs_root: pathlib.Path | None = None) -> types.ModuleType:
    """Load the specification's own plan evaluator. Absence is an error, never a pass."""
    root = specs_root or find_specs_root()
    if root is None:
        raise UpdateUnavailable("canonical_update_plan_evaluator_not_found")
    path = root / EVALUATOR_RELATIVE
    if not path.is_file():
        raise UpdateUnavailable(f"canonical_update_plan_evaluator_missing:{path}")
    spec = importlib.util.spec_from_file_location("axiom_canonical_update_plan", path)
    if spec is None or spec.loader is None:
        raise UpdateUnavailable("canonical_update_plan_evaluator_unloadable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_document(
    document: Any,
    evaluator: types.ModuleType | None = None,
    *,
    approved_digest: str | None = None,
) -> dict[str, Any]:
    """Return the canonical verdict for one plan document.

    The verdict, its reason names and the digest are the specification's, so this
    component cannot quietly disagree with it.
    """
    module = evaluator or load_canonical_evaluator()
    if approved_digest is not None and isinstance(document, dict):
        candidate = dict(document)
        candidate["approved_digest"] = approved_digest
        return dict(module.evaluate(candidate))
    return dict(module.evaluate(document))


def plan_digest(plan: Mapping[str, Any], evaluator: types.ModuleType | None = None) -> str:
    """The canonical plan digest: every planner input, excluding the digest and approval."""
    module = evaluator or load_canonical_evaluator()
    return str(module.plan_digest(dict(plan)))


def protected_roots(prefix: str | pathlib.Path | None = None) -> tuple[pathlib.Path, ...]:
    """Directories an update must never write into while this process runs."""
    roots: list[pathlib.Path] = []
    for value in (prefix, sys.prefix, sys.base_prefix):
        if value:
            roots.append(pathlib.Path(str(value)))
    try:
        roots.append(pathlib.Path(sysconfig.get_paths()["purelib"]))
    except (KeyError, OSError):
        pass
    package = pathlib.Path(__file__).resolve().parent
    roots.append(package)
    roots.append(package.parent)
    unique: list[pathlib.Path] = []
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved not in unique:
            unique.append(resolved)
    return tuple(unique)


def _is_within(candidate: pathlib.Path, root: pathlib.Path) -> bool:
    return candidate == root or root in candidate.parents


def guard_install_root(
    install_root: str,
    protected: Sequence[pathlib.Path] | None = None,
) -> None:
    """Refuse an install root that would rewrite the running installation."""
    text = str(install_root or "").strip()
    if text == "":
        raise InPlaceUpgradeRefused(["install_root_empty"])
    candidate = pathlib.Path(text)
    if not candidate.is_absolute():
        raise InPlaceUpgradeRefused([f"install_root_not_absolute:{text}"])
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate
    roots = tuple(protected) if protected is not None else protected_roots()
    for root in roots:
        if _is_within(resolved, root) or _is_within(root, resolved):
            raise InPlaceUpgradeRefused([f"in_place_upgrade_prohibited:{root}"])


def delegation_argv(
    plan_path: str | pathlib.Path,
    *,
    orchestrator: str = ORCHESTRATOR,
) -> list[str]:
    """The external updater invocation for one approved plan."""
    return [orchestrator, *ORCHESTRATOR_SUBCOMMAND, str(plan_path)]


def guard_delegation(argv: Sequence[str]) -> None:
    """Refuse any delegated command that would install into the running environment."""
    reasons: list[str] = []
    if not argv:
        raise InPlaceUpgradeRefused(["delegation_argv_empty"])
    head = pathlib.PurePath(str(argv[0]).replace("\\", "/")).name.lower()
    if head in PIP_PROGRAMS or head.startswith("pip") or head in {"python", "python3", "py"}:
        reasons.append(f"delegated_program_cannot_install:{head}")
    tokens = [str(item).strip().lower() for item in argv]
    for index, token in enumerate(tokens):
        if token in PIP_PROGRAMS:
            reasons.append(f"pip_token_in_delegation:{token}")
        following = tokens[index + 1] if index + 1 < len(tokens) else ""
        if token in {"-m", "--module"} and following in PIP_PROGRAMS:
            reasons.append(f"module_install_in_delegation:{following}")
        if "pip install" in token or "pip upgrade" in token:
            reasons.append(f"pip_phrase_in_delegation:{token}")
    if reasons:
        raise InPlaceUpgradeRefused(reasons)


@dataclass(frozen=True)
class ApplyDecision:
    """What this process decided to do about one approved plan."""

    plan_id: str
    plan_digest: str
    target_component: str
    install_root: str
    argv: tuple[str, ...]
    executed: bool
    outcome: str
    reasons: tuple[str, ...] = ()

    def as_report(self) -> dict[str, Any]:
        return {
            "component": COMPONENT,
            "plan_id": self.plan_id,
            "plan_digest": self.plan_digest,
            "target_component": self.target_component,
            "install_root": self.install_root,
            "delegation_argv": list(self.argv),
            "executed": self.executed,
            "outcome": self.outcome,
            "reasons": list(self.reasons),
        }


def apply_plan(
    document: Any,
    *,
    plan_path: str | pathlib.Path | None = None,
    evaluator: types.ModuleType | None = None,
    approved_digest: str | None = None,
    runner: Callable[[Sequence[str]], int] | None = None,
    orchestrator: str = ORCHESTRATOR,
    protected: Sequence[pathlib.Path] | None = None,
) -> ApplyDecision:
    """Validate one approved plan and delegate the swap to the external updater.

    The running process never upgrades itself: this function returns an argument
    list for the updater and either hands it to an injected runner or reports the
    delegation unexecuted. It refuses a plan that is not approved, a plan that
    targets another component, and a plan whose root is the running install.
    """
    verdict = validate_document(document, evaluator, approved_digest=approved_digest)
    if not verdict.get("ok"):
        raise PlanRejected(list(verdict.get("reasons") or ["plan_rejected"]))

    plan = document["plan"]
    approval = plan.get("approval") or {}
    if approval.get("state") != "approved":
        raise PlanRejected(["plan_not_approved"])

    target = plan.get("target") or {}
    component = str(target.get("component") or "")
    if component != COMPONENT:
        raise PlanRejected([f"target_is_not_this_component:{component}"])

    install_root = str(target.get("install_root") or "")
    guard_install_root(install_root, protected)

    if plan_path is None:
        raise UpdateUnavailable("plan_path_required_for_delegation")
    argv = delegation_argv(plan_path, orchestrator=orchestrator)
    guard_delegation(argv)

    digest = plan_digest(plan, evaluator)
    executed = False
    outcome = "delegated_not_executed"
    if runner is not None:
        exit_code = int(runner(argv))
        executed = True
        outcome = "delegated_ok" if exit_code == 0 else f"delegated_failed:{exit_code}"
    return ApplyDecision(
        plan_id=str(plan.get("plan_id") or ""),
        plan_digest=digest,
        target_component=component,
        install_root=install_root,
        argv=tuple(argv),
        executed=executed,
        outcome=outcome,
    )


def apply_exit_code(error: PlanRejected) -> int:
    """A missing approval is an authorization answer; every other refusal is validation."""
    return 5 if "plan_not_approved" in error.reasons else 2


def orchestrator_available(orchestrator: str = ORCHESTRATOR) -> bool:
    """Whether the external updater this process delegates to is on PATH."""
    return shutil.which(orchestrator) is not None
