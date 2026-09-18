# Runtime compatibility — axiom-mcp

Local implementation documentation for the pinned Python runtime and the official
MCP SDK. The canonical contract stays in `axiom-specs`; this file only records how
this component implements the pin.

## Pinned surface

| Item | Pinned value | Source |
| --- | --- | --- |
| Python | `>=3.13,<3.14` (verified on 3.13.14) | `pyproject.toml` / `axiom_mcp.version` |
| Official MCP SDK | `mcp==1.28.1` | `pyproject.toml` / `axiom_mcp.version` |
| FastAPI / Uvicorn / Starlette | `0.139.2` / `0.51.0` / `1.3.1` | `pyproject.toml` |
| Pydantic / anyio / httpx | `2.13.4` / `4.14.2` / `0.28.1` | `pyproject.toml` |
| Minimum protocol revision | `2025-11-25` | `axiom_mcp.version.MCP_PROTOCOL_MINIMUM` |
| Spec pin | `2.0.0-draft.1` at `80f44e836ced442e8f3ea33d167bd369ed6796bc` | `spec.lock.json` |

Graph payload schema `1`, control API `1` and queue schema `1` are independent
integer-major dimensions copied from `contracts/version-dimensions.json`; they are
reported, never invented here.

## Compatibility spike (C-001)

The SDK surface used by later transports is recorded in the spike artifact
`evidence/artifacts/C-001/sdk-compat-spike.txt` and enforced by
`tests/test_runtime_pin.py`. The spike resolved, on the installed SDK:

- `mcp.server.fastmcp.FastMCP.streamable_http_app`, `FastMCP.run_stdio_async`,
  `FastMCP.session_manager`, and the `FastMCP.__init__` parameters `lifespan`,
  `streamable_http_path`, `host`, `port`, `json_response`, `stateless_http`,
  `transport_security`;
- `mcp.server.transport_security.TransportSecuritySettings` fields
  `enable_dns_rebinding_protection`, `allowed_hosts`, `allowed_origins`;
- `mcp.server.streamable_http.SUPPORTED_PROTOCOL_VERSIONS` (advertised:
  `2024-11-05`, `2025-03-26`, `2025-06-18`, `2025-11-25`) and
  `mcp.types.LATEST_PROTOCOL_VERSION` (`2025-11-25`).

Legacy entry points are deliberately not assumed. `sdk_surface_reasons()`
returns a reason whenever a pinned name, parameter or field disappears, and a
boundary test feeds the spike a legacy-only SDK shape to prove the drift is
detected instead of trusted. There is no `latest` fallback for the protocol
revision: `protocol_support_reasons()` fails when the minimum is not advertised.

## Version drift policy

A different SDK patch release, a Python version outside the pinned line, or a
missing pinned name is a pin failure, not a warning:

```text
python -m pytest tests -q
```

`axiom_mcp.version.runtime_pin_reasons()` is the shared predicate that `axiom-mcp
doctor` reports (C-007). Re-pinning requires a new spike, new artifacts and an
updated `spec.lock.json` review; it is never inferred from a build timestamp.
