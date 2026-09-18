"""Pinned runtime identity and canonical contract version dimensions.

Every value in this module is either pinned by ``pyproject.toml`` (the Python
runtime line and the official MCP SDK) or copied from the canonical contract in
``axiom-specs`` at the immutable revision recorded in ``spec.lock.json``.

This module never defines a contract. It reports the one it is pinned to, and
it fails loudly when the installed runtime no longer matches the pin. Names in
``SDK_SURFACE`` come from the recorded compatibility spike artifact, not from a
legacy document; assuming a legacy method name is therefore impossible without
a visible test failure.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import inspect
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

COMPONENT = "axiom-mcp"
VERSION = "0.0.0.dev0"

SPEC_VERSION = "2.0.0-draft.1"
SPEC_REPOSITORY = "axiom-specs"
SPEC_REVISION = "80f44e836ced442e8f3ea33d167bd369ed6796bc"

# Integer-major dimensions from contracts/version-dimensions.json (declared_value).
GRAPH_SCHEMA = 1
QUEUE_SCHEMA = 1
CONTROL_API = 1
WORKSPACE_LAYOUT = 2
SOLUTION_CONFIG = 2
BOOTSTRAP_MANIFEST = 2

SPEC_DIMENSIONS: dict[str, Any] = {
    "spec": SPEC_VERSION,
    "graph_schema": GRAPH_SCHEMA,
    "queue_schema": QUEUE_SCHEMA,
    "control_api": CONTROL_API,
    "workspace_layout": WORKSPACE_LAYOUT,
    "solution_config": SOLUTION_CONFIG,
    "bootstrap_manifest": BOOTSTRAP_MANIFEST,
}

PYTHON_REQUIRES = ">=3.13,<3.14"
PYTHON_MIN = (3, 13)
PYTHON_MAX_EXCLUSIVE = (3, 14)

SDK_PACKAGE = "mcp"
SDK_PIN = "1.28.1"
SDK_REQUIRES = f"=={SDK_PIN}"

RUNTIME_PINS: dict[str, str] = {
    "mcp": "1.28.1",
    "fastapi": "0.139.2",
    "uvicorn": "0.51.0",
    "starlette": "1.3.1",
    "pydantic": "2.13.4",
    "anyio": "4.14.2",
    "httpx": "0.28.1",
}

DEV_PINS: dict[str, str] = {
    "pytest": "9.1.1",
    "ruff": "0.15.22",
}

# Minimum protocol revision the pin must be able to negotiate. The SDK may
# advertise more; the component never substitutes "latest" for this value.
MCP_PROTOCOL_MINIMUM = "2025-11-25"

VERSION_REPORT_FIELDS = (
    "component",
    "version",
    "spec_version",
    "graph_schema",
    "control_api",
    "queue_schema",
    "build_revision",
    "update_status",
)

UPDATE_STATUS_VALUES = (
    "not_checked",
    "current",
    "available",
    "offline",
    "unconfigured",
    "blocked",
)

# The SDK surface this component locks to the pin. Recorded by the
# compatibility spike; a missing name or parameter is a pin failure.
SDK_SURFACE: tuple[dict[str, Any], ...] = (
    {"module": "mcp.server.fastmcp", "attribute": "FastMCP.streamable_http_app"},
    {"module": "mcp.server.fastmcp", "attribute": "FastMCP.run_stdio_async"},
    {"module": "mcp.server.fastmcp", "attribute": "FastMCP.session_manager"},
    {
        "module": "mcp.server.fastmcp",
        "attribute": "FastMCP.__init__",
        "params": (
            "lifespan",
            "streamable_http_path",
            "host",
            "port",
            "json_response",
            "stateless_http",
            "transport_security",
        ),
    },
    {
        "module": "mcp.server.transport_security",
        "attribute": "TransportSecuritySettings",
        "fields": ("enable_dns_rebinding_protection", "allowed_hosts", "allowed_origins"),
    },
    {"module": "mcp.server.streamable_http", "attribute": "SUPPORTED_PROTOCOL_VERSIONS"},
    {"module": "mcp.types", "attribute": "LATEST_PROTOCOL_VERSION"},
    {"module": "mcp.server.lowlevel", "attribute": "Server"},
)

PROTOCOL_ADVERTISEMENT_MODULE = "mcp.server.streamable_http"
PROTOCOL_ADVERTISEMENT_ATTRIBUTE = "SUPPORTED_PROTOCOL_VERSIONS"


def default_module_resolver(module: str) -> Any:
    """Import ``module``; the spike uses this resolver unless a test injects one."""
    return importlib.import_module(module)


def parse_version(text: str) -> tuple[int, ...]:
    """Parse a dotted numeric version, ignoring any suffix after the third part."""
    parts: list[int] = []
    for chunk in str(text).split("."):
        digits = ""
        for character in chunk:
            if character.isdigit():
                digits += character
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def python_supported(version: str | tuple[int, ...] | None = None) -> bool:
    """Return True when ``version`` is inside the pinned supported Python line."""
    if version is None:
        parsed = (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)
    elif isinstance(version, tuple):
        parsed = version
    else:
        parsed = parse_version(version)
    if len(parsed) < 2:
        return False
    trimmed = parsed[:2]
    return PYTHON_MIN <= trimmed < PYTHON_MAX_EXCLUSIVE


def sdk_compatibility_reasons(
    sdk_version: str | None = None,
    python_version: str | tuple[int, ...] | None = None,
) -> list[str]:
    """Return why the runtime is not the pinned one; empty means compatible.

    Exact SDK equality is required: a different patch release is rejected rather
    than silently accepted, because the pin is the surface the spike verified.
    """
    reasons: list[str] = []
    if not python_supported(python_version):
        shown = python_version
        if shown is None:
            shown = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        elif isinstance(shown, tuple):
            shown = ".".join(str(part) for part in shown)
        reasons.append(f"python_version_unsupported:{shown}")
    if sdk_version is None:
        try:
            sdk_version = importlib.metadata.version(SDK_PACKAGE)
        except importlib.metadata.PackageNotFoundError:
            reasons.append(f"sdk_not_installed:{SDK_PACKAGE}")
            return reasons
    if sdk_version != SDK_PIN:
        reasons.append(f"sdk_version_unsupported:{sdk_version}")
    return reasons


def protocol_support_reasons(
    advertised: Iterable[str],
    minimum: str = MCP_PROTOCOL_MINIMUM,
) -> list[str]:
    """Return why the advertised protocol revisions cannot meet the minimum."""
    values = {str(item) for item in advertised}
    if not values:
        return [f"protocol_advertisement_empty:{minimum}"]
    if minimum not in values:
        return [f"protocol_revision_missing:{minimum}"]
    return []


def advertised_protocol_versions(resolver: Callable[[str], Any] | None = None) -> tuple[str, ...]:
    """Read the protocol revisions the installed SDK actually advertises."""
    resolve = resolver or default_module_resolver
    module = resolve(PROTOCOL_ADVERTISEMENT_MODULE)
    values = getattr(module, PROTOCOL_ADVERTISEMENT_ATTRIBUTE, ())
    return tuple(sorted(str(item) for item in values))


def _resolve_attribute(root: Any, dotted: str) -> Any:
    node = root
    for part in dotted.split("."):
        if not hasattr(node, part):
            raise AttributeError(part)
        node = getattr(node, part)
    return node


def sdk_surface_reasons(
    modules: Mapping[str, Any] | None = None,
    resolver: Callable[[str], Any] | None = None,
) -> list[str]:
    """Return every pinned SDK name or parameter the runtime does not provide.

    ``modules`` injects a fake namespace so a boundary test can prove the spike
    detects drift, for example an SDK that only exposes a legacy entry point.
    """
    resolve = resolver or default_module_resolver
    cache: dict[str, Any] = dict(modules or {})
    reasons: list[str] = []
    for entry in SDK_SURFACE:
        module_name = entry["module"]
        attribute = entry["attribute"]
        try:
            module = cache[module_name] if module_name in cache else resolve(module_name)
        except Exception as exc:  # noqa: BLE001 - any import failure is a pin failure
            reasons.append(f"sdk_module_missing:{module_name}:{type(exc).__name__}")
            continue
        cache[module_name] = module
        try:
            target = _resolve_attribute(module, attribute)
        except AttributeError:
            reasons.append(f"sdk_attribute_missing:{module_name}:{attribute}")
            continue
        if entry.get("params"):
            try:
                signature = inspect.signature(target)
            except (TypeError, ValueError):
                reasons.append(f"sdk_signature_unavailable:{module_name}:{attribute}")
                continue
            for name in entry["params"]:
                if name not in signature.parameters:
                    reasons.append(f"sdk_parameter_missing:{module_name}:{attribute}:{name}")
        if entry.get("fields"):
            fields = getattr(target, "model_fields", None)
            if not isinstance(fields, Mapping):
                reasons.append(f"sdk_fields_unavailable:{module_name}:{attribute}")
                continue
            for name in entry["fields"]:
                if name not in fields:
                    reasons.append(f"sdk_field_missing:{module_name}:{attribute}:{name}")
    return reasons


def installed_versions(names: Sequence[str] | None = None) -> dict[str, str | None]:
    """Return the installed distribution versions for the pinned runtime names."""
    result: dict[str, str | None] = {}
    for name in names or tuple(RUNTIME_PINS):
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = None
    return result


def runtime_pin_reasons() -> list[str]:
    """Return why the installed runtime disagrees with the pins in this module."""
    reasons = list(sdk_compatibility_reasons())
    for name, pinned in RUNTIME_PINS.items():
        installed = installed_versions([name])[name]
        if installed is None:
            reasons.append(f"runtime_not_installed:{name}")
        elif installed != pinned:
            reasons.append(f"runtime_version_unsupported:{name}:{installed}")
    return reasons


def version_report(build_revision: str, update_status: str = "not_checked") -> dict[str, Any]:
    """Build a ``contracts/schemas/version-report.schema.json`` shaped report.

    The report carries exactly the five dimensions the canonical contract maps
    to this surface plus component identity; it adds no extra key.
    """
    if update_status not in UPDATE_STATUS_VALUES:
        raise ValueError(f"update_status not in contract enum: {update_status!r}")
    if not build_revision:
        raise ValueError("build_revision must not be empty")
    return {
        "component": COMPONENT,
        "version": VERSION,
        "spec_version": SPEC_VERSION,
        "graph_schema": GRAPH_SCHEMA,
        "control_api": CONTROL_API,
        "queue_schema": QUEUE_SCHEMA,
        "build_revision": str(build_revision),
        "update_status": update_status,
    }
