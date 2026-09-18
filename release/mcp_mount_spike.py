"""Record the C-002 mount and lifecycle evidence against the pinned SDK.

Run from the repository root:

    python release/mcp_mount_spike.py

The output is JSON on stdout and is stored as the C-002 compatibility artifact.
It records what the mounted stack actually did: the contract path answering
without a redirect, the isolated route, the session manager starting and
stopping exactly once, and the protocol revision a real handshake negotiated.

It also records the failure mode this task exists to prevent: a plain Starlette
mount serves HTTP but cannot answer a protocol request, because mounting does
not run the sub-application lifespan.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from axiom_mcp import sdk_compat, version  # noqa: E402

BASE_URL = "http://127.0.0.1:8000"
ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*"]
ALLOWED_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*"]
INITIALIZE_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": version.MCP_PROTOCOL_MINIMUM,
        "capabilities": {},
        "clientInfo": {"name": "c-002-spike", "version": "0.0.0"},
    },
}
INITIALIZE_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _server(name: str):
    server = sdk_compat.build_server(
        name, allowed_hosts=ALLOWED_HOSTS, allowed_origins=ALLOWED_ORIGINS
    )

    @server.tool()
    def ping() -> str:
        """Return a fixed marker so a call proves the tool plane is live."""
        return "pong"

    return server


async def _composed_mount() -> dict:
    app = FastAPI(title="c-002-spike")
    mounted = sdk_compat.mount_streamable_http(app, _server("c-002-spike"))
    app.add_api_route("/probe", lambda: {"ok": True}, methods=["GET"])

    record: dict = {
        "path": mounted.path,
        "recorder_before": {"starts": mounted.recorder.starts, "stops": mounted.recorder.stops},
    }
    async with sdk_compat.open_lifespan(app):
        record["recorder_during"] = {
            "starts": mounted.recorder.starts,
            "stops": mounted.recorder.stops,
            "running": mounted.recorder.running,
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            timeout=30.0,
            headers=INITIALIZE_HEADERS,
            follow_redirects=False,
        ) as client:
            exact = await client.post("/mcp", json=INITIALIZE_BODY)
            slashed = await client.post("/mcp/", json=INITIALIZE_BODY)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL, timeout=10.0
        ) as plain:
            probe = await plain.get("/probe")
        record["contract_path_status"] = exact.status_code
        record["trailing_slash_status"] = slashed.status_code
        record["trailing_slash_location"] = slashed.headers.get("location")
        record["sibling_route_status"] = probe.status_code

        handshake = await sdk_compat.initialize_handshake(app)

        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with sdk_compat.asgi_http_client(app, base_url=BASE_URL) as client:
            async with streamable_http_client(f"{BASE_URL}/mcp", http_client=client) as (
                read_stream,
                write_stream,
                _session_id,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    called = await session.call_tool("ping", {})
        record["tools_listed"] = [tool.name for tool in listed.tools]
        record["tool_call_text"] = called.content[0].text if called.content else None

    record["recorder_after"] = {
        "starts": mounted.recorder.starts,
        "stops": mounted.recorder.stops,
        "balanced": mounted.recorder.balanced,
        "started_exactly_once": mounted.recorder.started_exactly_once,
    }
    record["handshake"] = {
        "protocol_version": handshake.protocol_version,
        "negotiated_is_advertised": handshake.negotiated_is_advertised,
        "meets_pinned_minimum": handshake.meets_pinned_minimum,
        "advertised": list(handshake.advertised),
        "server_name": handshake.server_name,
        "server_version": handshake.server_version,
        "capabilities": list(handshake.capabilities),
    }
    return record


async def _plain_mount_failure() -> dict:
    server = _server("c-002-raw")
    subapp = server.streamable_http_app()
    app = FastAPI(title="c-002-raw")
    app.mount("/mcp", subapp)
    async with sdk_compat.open_lifespan(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url=BASE_URL,
            timeout=10.0,
            headers=INITIALIZE_HEADERS,
        ) as client:
            response = await client.post("/mcp/mcp", json=INITIALIZE_BODY)
    return {"effective_url": "/mcp/mcp", "status": response.status_code}


async def main() -> dict:
    return {
        "python": sys.version.split()[0],
        "sdk": version.installed_versions(["mcp"]),
        "sdk_pin": version.SDK_PIN,
        "protocol_minimum": version.MCP_PROTOCOL_MINIMUM,
        "composed_mount": await _composed_mount(),
        "plain_mount_without_composed_lifespan": await _plain_mount_failure(),
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main()), indent=2, sort_keys=True))
