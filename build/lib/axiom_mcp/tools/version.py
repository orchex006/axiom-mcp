"""C-033: ``graph_version`` - compatibility and update availability, and never an install.

``repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md`` section 4 fixes the tool as "selected components
and compatibility" with side effect "read", and section 4's closing paragraph is the rule the card
restates: "Expose update/bootstrap/install through CLI/skill workflow only. Do not add an MCP tool
that downloads or executes an update by default."

So this tool *reports* and never *acts*:

* it re-uses :func:`axiom_mcp.version.version_report` for the pinned dimensions, so the numbers
  come from the same module the CLI and the runtime pin test use rather than a second copy;
* it reports compatibility ``reasons`` as the pin checker produced them, and an empty reason list
  is the only thing that makes ``compatible`` true;
* it reports update ``status`` through :func:`axiom_mcp.update.check_update`, which is a read-only
  check that never installs, and only when the caller asks for it (``check_update: true``);
* the answer carries an explicit ``installation`` block saying that no install path exists on this
  surface, and ``tests/test_tools_version.py` proves it by replacing
  :func:`axiom_mcp.update.apply_plan` with a tripwire that fails the test if this tool reaches it.

A component this process cannot observe is reported as unobserved rather than guessed: only
``axiom-mcp` is observable from inside it, and a request for an unregistered component name is a
``VALIDATION_ERROR` instead of an invented row.
"""

from __future__ import annotations

import platform
from collections.abc import Mapping, Sequence
from typing import Any

from axiom_mcp import security, update, version
from axiom_mcp.errors import AxiomError
from axiom_mcp.query.envelope import SCHEMA_VERSION
from axiom_mcp.tools.context import ToolContext, closed_arguments, optional_bool

__all__ = ["KNOWN_COMPONENTS", "OBSERVABLE_COMPONENTS", "VERSION_FIELDS", "graph_version"]

#: The workspace components the catalog may name. Task/specs additions are explicit here.
KNOWN_COMPONENTS = ("axiom-mcp", "axiom-graphd", "axiom-specs", "axiom-skills")

#: Only the component this process *is* can be observed from inside it.
OBSERVABLE_COMPONENTS = ("axiom-mcp",)

#: The response keys and nothing else.
VERSION_FIELDS = (
    "schema_version",
    "components",
    "dimensions",
    "compatibility",
    "update",
    "installation",
    "warnings",
)

_ARGUMENTS = ("components", "check_update")


def _components_argument(arguments: Mapping[str, Any]) -> list[str]:
    """Return the requested component names, defaulting to the whole known set."""
    value = arguments.get("components")
    if value is None:
        return list(KNOWN_COMPONENTS)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise AxiomError(
            "VALIDATION_ERROR",
            "components must be a list of registered component names",
            details={"allowed": list(KNOWN_COMPONENTS)},
        )
    seen: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in KNOWN_COMPONENTS:
            raise AxiomError(
                "VALIDATION_ERROR",
                "components must name registered components",
                details={"allowed": list(KNOWN_COMPONENTS)},
            )
        if item not in seen:
            seen.append(item)
    if not seen:
        raise AxiomError(
            "VALIDATION_ERROR",
            "components must not be empty",
            details={"allowed": list(KNOWN_COMPONENTS)},
        )
    return seen


def _component_entry(name: str, *, control_wired: bool) -> dict[str, Any]:
    """Report one component's identity, or say honestly that it cannot be observed."""
    if name in OBSERVABLE_COMPONENTS:
        reasons = version.runtime_pin_reasons()
        return {
            "component": name,
            "observed": True,
            "version": version.VERSION,
            "spec_version": version.SPEC_VERSION,
            "spec_revision": version.SPEC_REVISION,
            "compatible": not reasons,
            "reasons": reasons,
        }
    if name == "axiom-graphd":
        entry: dict[str, Any] = {
            "component": name,
            "observed": control_wired,
            "control_api": version.CONTROL_API,
        }
        if not control_wired:
            entry["reason"] = "control_client_not_wired"
        return entry
    return {
        "component": name,
        "observed": False,
        "reason": "not_observable_from_this_component",
    }


def _compatibility() -> dict[str, Any]:
    """Report runtime/SDK compatibility as the pin checker computed it."""
    pin_reasons = version.runtime_pin_reasons()
    protocol = version.advertised_protocol_versions()
    sdk_reasons = version.sdk_compatibility_reasons() + version.protocol_support_reasons(protocol)
    installed = version.installed_versions([version.SDK_PACKAGE])[version.SDK_PACKAGE]
    return {
        "compatible": not pin_reasons,
        "runtime": {
            "python": platform.python_version(),
            "supported": version.python_supported(),
            "pinned_python": version.PYTHON_REQUIRES,
            "pinned_sdk": version.SDK_PIN,
        },
        "sdk": {
            "installed": installed,
            "protocol_versions": list(protocol),
            "protocol_minimum": version.MCP_PROTOCOL_MINIMUM,
            "reasons": sdk_reasons,
        },
        "reasons": pin_reasons,
    }


def _update_block(environ: Mapping[str, str] | None, requested: bool) -> dict[str, Any]:
    """Report update availability from the read-only check, and only when asked."""
    if not requested:
        return {
            "status": "not_checked",
            "installed": version.VERSION,
            "available": None,
            "compatible": None,
            "needs_restart": False,
            "reasons": ["update_check_not_requested"],
        }
    check = update.check_update(environ)
    return {
        "status": check.status,
        "installed": check.installed,
        "available": check.available,
        "compatible": check.compatible,
        "needs_restart": check.needs_restart,
        "channel": check.channel,
        "source_origin": check.source_origin,
        "reasons": list(check.reasons),
    }


def graph_version(
    arguments: Mapping[str, Any],
    context: ToolContext,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Report selected components, their compatibility and update availability."""
    closed_arguments(arguments, _ARGUMENTS)
    components = _components_argument(arguments)
    check_update = optional_bool(arguments, "check_update", default=False)

    context.principal.require(security.CAPABILITY_READ)

    dimensions = version.version_report(version.SPEC_REVISION, update_status="not_checked")
    compatibility = _compatibility()
    warnings: list[str] = []
    if not compatibility["compatible"]:
        warnings.append("runtime_pin_mismatch")

    return {
        "schema_version": SCHEMA_VERSION,
        "components": [
            _component_entry(name, control_wired=context.control is not None) for name in components
        ],
        "dimensions": dimensions,
        "compatibility": compatibility,
        "update": _update_block(environ, check_update),
        "installation": {
            "implicit_install": False,
            "reason": "no_install_path_on_the_mcp_surface",
            "applies_via": "cli_or_skill_workflow",
        },
        "warnings": warnings,
    }
