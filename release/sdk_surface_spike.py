"""Record the installed MCP SDK surface the runtime pin is locked to.

Run from the repository root:

    python release/sdk_surface_spike.py

The output is the C-001 compatibility-spike artifact. It intentionally prints
only resolved names and versions: it is evidence for the pin, not a runtime
dependency, and it imports the SDK in a fresh process.
"""

from __future__ import annotations

import importlib.metadata
import inspect
import json
import sys

import mcp.server.streamable_http as streamable_http
import mcp.types as mcp_types
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings


def main() -> int:
    report = {
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
        "sdk_distribution": "mcp",
        "sdk_version": importlib.metadata.version("mcp"),
        "sdk_requires_python": importlib.metadata.metadata("mcp").get("Requires-Python"),
        "sdk_latest_protocol_version": mcp_types.LATEST_PROTOCOL_VERSION,
        "sdk_streamable_supported_protocol_versions": sorted(
            streamable_http.SUPPORTED_PROTOCOL_VERSIONS
        ),
        "fastmcp_init_params": sorted(inspect.signature(FastMCP.__init__).parameters),
        "fastmcp_public_methods": sorted(m for m in dir(FastMCP) if not m.startswith("_")),
        "transport_security_fields": sorted(TransportSecuritySettings.model_fields),
        "streamable_http_app_signature": str(inspect.signature(FastMCP.streamable_http_app)),
        "session_manager_type": type(FastMCP.session_manager).__name__,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
