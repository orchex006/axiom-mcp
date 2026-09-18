"""Record the C-004 Streamable HTTP wire evidence against the pinned SDK.

Run from the repository root:

    python release/http_transport_spike.py

The output is JSON on stdout and is stored as the C-004 compatibility artifact.
It records what the transport actually did on the wire - the event-stream
response, the session header, the negotiated protocol, the tool plane, and the
two probe bodies - and it records the failure this task exists to prevent: a
REST-only route answers HTTP 200 with a plausible body and still is not an MCP
transport.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from axiom_mcp import http, sdk_compat, version  # noqa: E402

ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*"]
ALLOWED_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*"]
BASE_URL = http.DEFAULT_BASE_URL

INITIALIZE_PAYLOAD = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": version.MCP_PROTOCOL_MINIMUM,
        "capabilities": {},
        "clientInfo": {"name": "c-004-spike", "version": version.VERSION},
    },
}
TOOLS_LIST_PAYLOAD = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


def build_server() -> Any:
    server = sdk_compat.build_server(
        "c-004-spike", allowed_hosts=ALLOWED_HOSTS, allowed_origins=ALLOWED_ORIGINS
    )

    @server.tool()
    def ping() -> str:
        """Return a fixed marker so a call proves the tool plane is live."""
        return "pong"

    return server


async def probe_gateway() -> dict[str, Any]:
    import httpx

    gateway = http.build_gateway(build_server())
    report: dict[str, Any] = {}
    async with sdk_compat.open_lifespan(gateway.app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway.app), base_url=BASE_URL, timeout=30.0
        ) as client:
            health = await client.get("/healthz")
            ready = await client.get("/readyz")
        report["healthz"] = {"status": health.status_code, "body": health.json()}
        report["readyz"] = {"status": ready.status_code, "body": ready.json()}

        initialize = await http.observe_streamable_http(gateway.app, payload=INITIALIZE_PAYLOAD)
        report["initialize"] = initialize.as_dict()
        report["initialize_is_event_stream"] = initialize.is_event_stream
        report["initialize_session_issued"] = bool(initialize.session_id)

        tools_list = await http.observe_streamable_http(
            gateway.app, payload=TOOLS_LIST_PAYLOAD, session_id=initialize.session_id
        )
        report["tools_list"] = tools_list.as_dict()
        report["tools_list_round_trips_session"] = tools_list.status_code == 200

        no_stream = await http.observe_streamable_http(
            gateway.app, payload=INITIALIZE_PAYLOAD, accept="application/json"
        )
        report["accept_without_event_stream"] = {
            "status": no_stream.status_code,
            "content_type": no_stream.content_type,
            "is_event_stream": no_stream.is_event_stream,
        }

        get_without_session = await http.observe_streamable_http(
            gateway.app, method="GET", accept="text/event-stream"
        )
        report["get_without_session"] = {
            "status": get_without_session.status_code,
            "body": get_without_session.body[:160],
        }
    return report


async def probe_rest_only() -> dict[str, Any]:
    rest_app = FastAPI(title="rest-only")

    @rest_app.post("/mcp")
    async def rest_mcp() -> JSONResponse:
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "protocolVersion": version.MCP_PROTOCOL_MINIMUM,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "rest-only", "version": "0.0.0"},
                },
            }
        )

    async with sdk_compat.open_lifespan(rest_app):
        observation = await http.observe_streamable_http(rest_app, payload=INITIALIZE_PAYLOAD)
    return {
        "status": observation.status_code,
        "content_type": observation.content_type,
        "is_event_stream": observation.is_event_stream,
        "session_id": observation.session_id,
        "body_prefix": observation.body[:160],
        "conformant": False,
        "reason": (
            "a REST 200 JSON body is not a Streamable HTTP event stream and issues no session"
        ),
    }


def main() -> int:
    report: dict[str, Any] = {
        "component": version.COMPONENT,
        "version": version.VERSION,
        "sdk": f"{version.SDK_PACKAGE}=={version.SDK_PIN}",
        "endpoint_path": http.MCP_ENDPOINT_PATH,
        "streamable_accept": http.STREAMABLE_ACCEPT,
        "gateway": asyncio.run(probe_gateway()),
        "rest_only_route": asyncio.run(probe_rest_only()),
    }
    gateway = report["gateway"]
    report["transport_conformant"] = bool(
        gateway["initialize_is_event_stream"]
        and gateway["initialize_session_issued"]
        and gateway["tools_list_round_trips_session"]
        and report["rest_only_route"]["is_event_stream"] is False
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
