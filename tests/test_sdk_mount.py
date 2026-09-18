"""C-002 regression test: the MCP server is really mounted and really live.

The positive cases prove a real MCP session works through the FastAPI mount and
that the composed lifespan starts and stops the SDK session manager exactly
once. The negative and boundary cases prove the composition is load-bearing:
a plain Starlette mount, which does not start the sub-application lifespan,
serves HTTP but cannot answer a protocol request - so a passing handshake here
is evidence the lifespan was composed, not an accident of the SDK.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from axiom_mcp import sdk_compat, version

ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*"]
ALLOWED_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*"]
BASE_URL = "http://127.0.0.1:8000"

INITIALIZE_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": version.MCP_PROTOCOL_MINIMUM,
        "capabilities": {},
        "clientInfo": {"name": "c-002-test", "version": "0.0.0"},
    },
}
INITIALIZE_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def build_mounted_app(*, tool: bool = True) -> tuple[FastAPI, sdk_compat.MountedMcp]:
    server = sdk_compat.build_server(
        "c-002-test", allowed_hosts=ALLOWED_HOSTS, allowed_origins=ALLOWED_ORIGINS
    )
    if tool:

        @server.tool()
        def ping() -> str:
            """Return a fixed marker so a call proves the tool plane is live."""
            return "pong"

    app = FastAPI(title="c-002-test")
    mounted = sdk_compat.mount_streamable_http(app, server)
    return app, mounted


def asgi_client(app: FastAPI, *, raise_app_exceptions: bool = True) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions),
        base_url=BASE_URL,
        timeout=30.0,
        headers=INITIALIZE_HEADERS,
        follow_redirects=False,
    )


def test_mount_places_the_sdk_handler_on_the_contract_path() -> None:
    app, mounted = build_mounted_app()

    assert mounted.path == sdk_compat.MCP_ENDPOINT_PATH
    assert mounted.path == "/mcp"
    routes = [r for r in app.router.routes if getattr(r, "path", None) == "/mcp"]
    assert len(routes) == 1
    assert routes[0].endpoint is mounted.handler
    assert sdk_compat.mounted_mcp(app) is mounted


def test_contract_path_answers_without_a_trailing_slash_redirect() -> None:
    async def scenario() -> tuple[int, int]:
        app, _ = build_mounted_app()
        async with sdk_compat.open_lifespan(app):
            async with asgi_client(app) as client:
                exact = await client.post("/mcp", json=INITIALIZE_BODY)
                slashed = await client.post("/mcp/", json=INITIALIZE_BODY)
        return exact.status_code, slashed.status_code, slashed.headers.get("location", "")

    exact_status, slashed_status, redirect_target = asyncio.run(scenario())

    assert exact_status not in (307, 308), "the contract path must be an exact match"
    assert exact_status == 200
    # The reverse form is not an endpoint; it is only a slash-normalizing redirect
    # back to the contract path, and the redirect target is asserted, not assumed.
    assert slashed_status == 307
    assert redirect_target.endswith("/mcp")


def test_composed_lifespan_starts_and_stops_the_session_manager_once() -> None:
    async def scenario() -> tuple[int, int, int, int]:
        app, mounted = build_mounted_app()
        before = mounted.recorder.starts
        async with sdk_compat.open_lifespan(app):
            during = mounted.recorder.starts
            assert mounted.recorder.running is True
        after = mounted.recorder.stops
        return before, during, after, mounted.recorder.starts

    before, during, stops, starts = asyncio.run(scenario())

    assert (before, during, stops, starts) == (0, 1, 1, 1)


def test_handshake_records_the_protocol_the_server_negotiated() -> None:
    async def scenario() -> sdk_compat.HandshakeResult:
        app, _ = build_mounted_app()
        async with sdk_compat.open_lifespan(app):
            return await sdk_compat.initialize_handshake(app)

    handshake = asyncio.run(scenario())

    assert handshake.protocol_version == version.MCP_PROTOCOL_MINIMUM
    assert handshake.meets_pinned_minimum is True
    assert handshake.negotiated_is_advertised is True
    assert handshake.advertised == version.advertised_protocol_versions()
    assert handshake.server_name == "c-002-test"
    assert "tools" in handshake.capabilities


def test_mounted_server_serves_tools_list_and_call() -> None:
    async def scenario() -> tuple[list[str], str | None]:
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        app, _ = build_mounted_app()
        async with sdk_compat.open_lifespan(app):
            async with sdk_compat.asgi_http_client(app, base_url=BASE_URL) as client:
                async with streamable_http_client(f"{BASE_URL}/mcp", http_client=client) as (
                    read_stream,
                    write_stream,
                    _,
                ):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        listed = await session.list_tools()
                        called = await session.call_tool("ping", {})
        text = called.content[0].text if called.content else None
        return [tool.name for tool in listed.tools], text

    names, text = asyncio.run(scenario())

    assert names == ["ping"]
    assert text == "pong"


def test_a_plain_fastapi_route_still_resolves_next_to_the_mount() -> None:
    async def scenario() -> int:
        app, _ = build_mounted_app()
        app.add_api_route("/probe", lambda: {"ok": True}, methods=["GET"])
        async with sdk_compat.open_lifespan(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=BASE_URL, timeout=10.0
            ) as client:
                response = await client.get("/probe")
        return response.status_code

    assert asyncio.run(scenario()) == 200


def test_a_plain_starlette_mount_cannot_answer_a_protocol_request() -> None:
    """Boundary: mounting without composing the lifespan is not enough."""

    async def scenario() -> int:
        server = sdk_compat.build_server(
            "c-002-raw", allowed_hosts=ALLOWED_HOSTS, allowed_origins=ALLOWED_ORIGINS
        )
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
        return response.status_code

    assert asyncio.run(scenario()) >= 500


def test_mount_refuses_a_second_mount_on_the_same_app() -> None:
    app, _ = build_mounted_app()
    with pytest.raises(sdk_compat.SdkCompatError) as caught:
        sdk_compat.mount_streamable_http(app, sdk_compat.build_server("second"))
    assert caught.value.code == "mcp_already_mounted"


@pytest.mark.parametrize("path", ["", "mcp", "/mcp/", "/mcp/x"])
def test_mount_rejects_a_path_outside_the_contract_shape(path: str) -> None:
    app = FastAPI()
    with pytest.raises(sdk_compat.SdkCompatError) as caught:
        sdk_compat.mount_streamable_http(app, sdk_compat.build_server("bad-path"), path=path)
    assert caught.value.code == "mount_path_invalid"


def test_transport_security_requires_both_allowlists() -> None:
    with pytest.raises(sdk_compat.SdkCompatError) as caught:
        sdk_compat.build_transport_security(allowed_hosts=[], allowed_origins=ALLOWED_ORIGINS)
    assert caught.value.code == "transport_security_allowlist_empty"

    with pytest.raises(sdk_compat.SdkCompatError) as second:
        sdk_compat.build_transport_security(allowed_hosts=ALLOWED_HOSTS, allowed_origins=["  "])
    assert second.value.code == "transport_security_allowlist_empty"


def test_build_server_rejects_a_partial_allowlist() -> None:
    with pytest.raises(sdk_compat.SdkCompatError) as caught:
        sdk_compat.build_server("partial", allowed_hosts=ALLOWED_HOSTS)
    assert caught.value.code == "transport_security_partial_allowlist"


def test_lifecycle_recorder_rejects_unbalanced_transitions() -> None:
    recorder = sdk_compat.LifecycleRecorder()

    with pytest.raises(sdk_compat.SdkCompatError) as early_stop:
        recorder.note_stop()
    assert early_stop.value.code == "lifecycle_not_running"

    recorder.note_start()
    with pytest.raises(sdk_compat.SdkCompatError) as double_start:
        recorder.note_start()
    assert double_start.value.code == "lifecycle_already_running"

    recorder.note_stop()
    assert recorder.balanced is True
    assert recorder.started_exactly_once is True


def test_extract_handler_reports_a_non_unique_route() -> None:
    server = sdk_compat.build_server("ambiguous")
    subapp = server.streamable_http_app()
    duplicate = list(subapp.routes) + list(subapp.routes)

    class FakeSubapp:
        routes = duplicate

    with pytest.raises(sdk_compat.SdkCompatError) as caught:
        sdk_compat.extract_streamable_http_handler(FakeSubapp(), "/mcp")  # type: ignore[arg-type]
    assert caught.value.code == "streamable_http_route_not_unique"


def test_create_streamable_http_subapp_rejects_a_non_starlette_return() -> None:
    class FakeServer:
        def streamable_http_app(self) -> object:
            return object()

    with pytest.raises(sdk_compat.SdkCompatError) as caught:
        sdk_compat.create_streamable_http_subapp(FakeServer())  # type: ignore[arg-type]
    assert caught.value.code == "streamable_http_app_not_starlette"


def test_the_sdk_transport_still_rejects_a_disallowed_origin() -> None:
    """Boundary: the mount must not widen the allowlist it was given."""

    async def scenario() -> tuple[int, int]:
        server = sdk_compat.build_server(
            "c-002-origin",
            allowed_hosts=["127.0.0.1:*"],
            allowed_origins=["http://127.0.0.1:*"],
        )
        app = FastAPI()
        sdk_compat.mount_streamable_http(app, server)
        async with sdk_compat.open_lifespan(app):
            async with asgi_client(app, raise_app_exceptions=False) as client:
                allowed = await client.post(
                    "/mcp",
                    json=INITIALIZE_BODY,
                    headers={**INITIALIZE_HEADERS, "Origin": "http://127.0.0.1:8000"},
                )
                denied = await client.post(
                    "/mcp",
                    json=INITIALIZE_BODY,
                    headers={**INITIALIZE_HEADERS, "Origin": "https://evil.example"},
                )
        return allowed.status_code, denied.status_code

    allowed_status, denied_status = asyncio.run(scenario())

    assert allowed_status == 200
    assert 400 <= denied_status < 500


def test_handshake_result_serializes_for_evidence() -> None:
    result = sdk_compat.HandshakeResult(
        protocol_version=version.MCP_PROTOCOL_MINIMUM,
        server_name="c-002-test",
        server_version="0.0.0",
        capabilities=("tools",),
        advertised=version.advertised_protocol_versions(),
    )
    payload = json.loads(json.dumps(result.__dict__, default=list))

    assert payload["protocol_version"] == "2025-11-25"
    assert payload["capabilities"] == ["tools"]
