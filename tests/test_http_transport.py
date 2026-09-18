"""C-004 regression test: the MCP endpoint is Streamable HTTP, not REST.

The positive cases drive a real MCP client - initialize, tools/list, tools/call -
through the composed ASGI stack and inspect the raw wire response, proving the
endpoint really streams ``text/event-stream`` frames with a session header rather
than returning a single JSON document. The negative cases prove the shape is
load-bearing: a hand-written REST route that returns an MCP-looking JSON body is
refused by the same client that succeeds against the real gateway, a client that
does not accept ``text/event-stream`` is refused, the probes never claim MCP
readiness, and an unacknowledged non-loopback bind is rejected.
"""

from __future__ import annotations

import asyncio
import threading

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from axiom_mcp import http, sdk_compat, version

ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*"]
ALLOWED_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*"]
BASE_URL = http.DEFAULT_BASE_URL

# Bound for the negative client attempt: a REST-only route never answers the
# handshake, so the proof must be a bounded non-completion, not a hang.
CLIENT_ATTEMPT_SECONDS = 6.0

INITIALIZE_PAYLOAD = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": version.MCP_PROTOCOL_MINIMUM,
        "capabilities": {},
        "clientInfo": {"name": "c-004-test", "version": "0.0.0"},
    },
}
TOOLS_LIST_PAYLOAD = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


def build_server(*, tool: bool = True) -> object:
    server = sdk_compat.build_server(
        "c-004-test", allowed_hosts=ALLOWED_HOSTS, allowed_origins=ALLOWED_ORIGINS
    )
    if tool:

        @server.tool()
        def ping() -> str:
            """Return a fixed marker so a call proves the tool plane is live."""
            return "pong"

    return server


def build_gateway(**kwargs: object) -> http.GatewayApp:
    return http.build_gateway(build_server(), **kwargs)  # type: ignore[arg-type]


async def raw_get(app: FastAPI, path: str, accept: str) -> tuple[int, str]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL, timeout=30.0
    ) as client:
        response = await client.get(path, headers={"Accept": accept})
    return response.status_code, response.text


def open_handshake(app: FastAPI, *, path: str = http.MCP_ENDPOINT_PATH) -> object:
    """Return an async context manager yielding a live MCP client session."""

    class _Handshake:
        async def __aenter__(self) -> ClientSession:
            self._lifespan = sdk_compat.open_lifespan(app)
            await self._lifespan.__aenter__()
            self._client = sdk_compat.asgi_http_client(
                app,
                base_url=BASE_URL,
                headers={"Accept": http.STREAMABLE_ACCEPT, "Content-Type": "application/json"},
            )
            await self._client.__aenter__()
            self._transport = streamable_http_client(f"{BASE_URL}{path}", http_client=self._client)
            read_stream, write_stream, _session_id = await self._transport.__aenter__()
            self._session = ClientSession(read_stream, write_stream)
            await self._session.__aenter__()
            return self._session

        async def __aexit__(self, *exc: object) -> None:
            await self._session.__aexit__(*exc)
            await self._transport.__aexit__(*exc)
            await self._client.__aexit__(*exc)
            await self._lifespan.__aexit__(*exc)

    return _Handshake()


def test_gateway_exposes_one_mcp_endpoint_and_two_probes() -> None:
    gateway = build_gateway()

    mcp_routes = [r for r in gateway.app.router.routes if getattr(r, "path", None) == "/mcp"]
    assert len(mcp_routes) == 1
    assert mcp_routes[0].endpoint is gateway.mounted.handler
    paths = {getattr(r, "path", None) for r in gateway.app.router.routes}
    assert {"/mcp", "/healthz", "/readyz"} <= paths
    assert gateway.settings.loopback_only is True


def test_healthz_is_minimal_process_health_and_claims_no_readiness() -> None:
    async def scenario() -> tuple[int, dict]:
        gateway = build_gateway()
        async with sdk_compat.open_lifespan(gateway.app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=gateway.app), base_url=BASE_URL, timeout=30.0
            ) as client:
                response = await client.get("/healthz")
        return response.status_code, response.json()

    status, body = asyncio.run(scenario())

    assert status == 200
    assert body == {"status": "ok"}
    # The probe must not be readable as an MCP readiness claim.
    assert "ready" not in body
    assert "mcp" not in body


def test_readyz_reports_query_and_control_separately_when_unavailable() -> None:
    async def scenario() -> tuple[int, dict]:
        gateway = build_gateway()
        async with sdk_compat.open_lifespan(gateway.app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=gateway.app), base_url=BASE_URL, timeout=30.0
            ) as client:
                response = await client.get("/readyz")
        return response.status_code, response.json()

    status, body = asyncio.run(scenario())

    assert status == 503
    assert body["ready"] is False
    assert body["error"] == http.NOT_READY
    assert body["query"] == {"available": False, "detail": "query data plane not wired"}
    assert body["control"] == {"available": False, "detail": "graphd control client not wired"}


def test_readyz_reports_a_single_unavailable_plane_separately() -> None:
    """Boundary: one plane up, one down - the report must not collapse them."""

    def readiness() -> http.ReadinessReport:
        return http.ReadinessReport(
            query=http.ComponentReadiness("query", True),
            control=http.ComponentReadiness("control", False, "daemon not running"),
        )

    async def scenario() -> tuple[int, dict]:
        gateway = build_gateway(readiness=readiness)
        async with sdk_compat.open_lifespan(gateway.app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=gateway.app), base_url=BASE_URL, timeout=30.0
            ) as client:
                response = await client.get("/readyz")
        return response.status_code, response.json()

    status, body = asyncio.run(scenario())

    assert status == 503
    assert body["ready"] is False
    assert body["error"] == http.NOT_READY
    assert body["query"] == {"available": True}
    assert body["control"] == {"available": False, "detail": "daemon not running"}


def test_readyz_answers_200_when_both_planes_are_available() -> None:
    def readiness() -> http.ReadinessReport:
        return http.ReadinessReport(
            query=http.ComponentReadiness("query", True),
            control=http.ComponentReadiness("control", True),
        )

    async def scenario() -> tuple[int, dict]:
        gateway = build_gateway(readiness=readiness)
        async with sdk_compat.open_lifespan(gateway.app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=gateway.app), base_url=BASE_URL, timeout=30.0
            ) as client:
                response = await client.get("/readyz")
        return response.status_code, response.json()

    status, body = asyncio.run(scenario())

    assert status == 200
    assert body["ready"] is True
    assert body["error"] is None


def test_initialize_is_served_as_an_event_stream_with_a_session() -> None:
    async def scenario() -> http.StreamObservation:
        gateway = build_gateway()
        async with sdk_compat.open_lifespan(gateway.app):
            return await http.observe_streamable_http(gateway.app, payload=INITIALIZE_PAYLOAD)

    observation = asyncio.run(scenario())

    assert observation.status_code == 200
    assert observation.is_event_stream is True, observation.content_type
    assert observation.session_id
    assert observation.events and observation.events[0][0] == "message"
    messages = observation.messages
    assert len(messages) == 1
    assert messages[0]["id"] == 1
    assert messages[0]["result"]["protocolVersion"] == version.MCP_PROTOCOL_MINIMUM
    assert messages[0]["result"]["serverInfo"]["name"] == "c-004-test"


def test_session_id_round_trips_to_a_followup_request() -> None:
    async def scenario() -> tuple[http.StreamObservation, http.StreamObservation]:
        gateway = build_gateway()
        async with sdk_compat.open_lifespan(gateway.app):
            first = await http.observe_streamable_http(gateway.app, payload=INITIALIZE_PAYLOAD)
            second = await http.observe_streamable_http(
                gateway.app, payload=TOOLS_LIST_PAYLOAD, session_id=first.session_id
            )
        return first, second

    first, second = asyncio.run(scenario())

    assert first.session_id
    assert second.status_code == 200
    assert second.is_event_stream is True
    tools = second.messages[0]["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["ping"]


def test_initialize_tools_list_and_tools_call_work_over_the_streaming_client() -> None:
    async def scenario() -> tuple[str, list[str], str | None]:
        gateway = build_gateway()
        async with open_handshake(gateway.app) as session:
            result = await session.initialize()
            listed = await session.list_tools()
            called = await session.call_tool("ping", {})
        content = called.content[0] if called.content else None
        text = getattr(content, "text", None)
        return result.protocolVersion, [tool.name for tool in listed.tools], text

    protocol, names, text = asyncio.run(scenario())

    assert protocol == version.MCP_PROTOCOL_MINIMUM
    assert names == ["ping"]
    assert text == "pong"


def test_post_that_does_not_accept_event_stream_is_refused() -> None:
    async def scenario() -> http.StreamObservation:
        gateway = build_gateway()
        async with sdk_compat.open_lifespan(gateway.app):
            return await http.observe_streamable_http(
                gateway.app, payload=INITIALIZE_PAYLOAD, accept="application/json"
            )

    observation = asyncio.run(scenario())

    assert observation.status_code == 406
    assert observation.is_event_stream is False


def test_get_without_a_session_is_refused() -> None:
    async def scenario() -> tuple[int, str]:
        gateway = build_gateway()
        async with sdk_compat.open_lifespan(gateway.app):
            return await raw_get(gateway.app, "/mcp", "text/event-stream")

    status, body = asyncio.run(scenario())

    assert status == 400
    assert "session" in body.lower()


async def _attempt_handshake(app: FastAPI) -> bool:
    try:
        async with open_handshake(app) as session:
            await session.initialize()
    except BaseException:  # noqa: BLE001 - any failure at all is the point
        return False
    return True


def handshake_completes(app: FastAPI, *, timeout: float) -> bool:
    """Attempt a real handshake with a hard bound, isolated in its own thread.

    A REST-only route never answers, so the client would otherwise wait forever.
    The attempt runs in a daemon thread with its own event loop: a bound that
    cancels an anyio task group in-place would raise a cancel-scope error instead
    of reporting non-completion, and the bound must not disguise itself as a
    product failure.
    """
    outcome: list[bool] = []

    def runner() -> None:
        try:
            outcome.append(asyncio.run(_attempt_handshake(app)))
        except BaseException:  # noqa: BLE001
            outcome.append(False)

    thread = threading.Thread(target=runner, name="c-004-handshake-probe", daemon=True)
    thread.start()
    thread.join(timeout)
    return bool(outcome) and outcome[0]


def test_a_plain_rest_route_is_not_mcp_conformant() -> None:
    """The boundary this task exists for: a REST shape is not a transport.

    The REST route answers HTTP 200 with a body that *looks* like an initialize
    result. It still fails conformance twice over: it issues no streamable
    session and no event stream, and a real MCP client cannot complete an
    initialize against it within the same bound in which it completes against
    the real gateway.
    """
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

    async def observe() -> tuple[http.StreamObservation, http.StreamObservation]:
        gateway = build_gateway()
        async with sdk_compat.open_lifespan(gateway.app):
            rest_observation = await http.observe_streamable_http(
                rest_app, payload=INITIALIZE_PAYLOAD
            )
            real_observation = await http.observe_streamable_http(
                gateway.app, payload=INITIALIZE_PAYLOAD
            )
        return rest_observation, real_observation

    rest_observation, real_observation = asyncio.run(observe())

    assert rest_observation.status_code == 200
    assert rest_observation.is_event_stream is False, "REST must not claim a stream"
    assert rest_observation.session_id is None, "REST must not issue a protocol session"
    assert real_observation.is_event_stream is True
    assert real_observation.session_id

    assert handshake_completes(rest_app, timeout=CLIENT_ATTEMPT_SECONDS) is False, (
        "a REST-only route must not satisfy an MCP client"
    )
    gateway = build_gateway()
    assert handshake_completes(gateway.app, timeout=CLIENT_ATTEMPT_SECONDS * 5) is True, (
        "the same client must succeed against the real transport, otherwise the "
        "negative case is not attributable"
    )


def test_non_loopback_bind_is_refused_without_an_explicit_acknowledgement() -> None:
    with pytest.raises(http.HttpTransportError) as caught:
        http.HttpTransportSettings(host="0.0.0.0")
    assert caught.value.code == "non_loopback_bind_refused"

    allowed = http.HttpTransportSettings(host="0.0.0.0", allow_non_loopback_bind=True)
    assert allowed.loopback_only is False


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"host": "  "}, "bind_host_empty"),
        ({"port": 70000}, "bind_port_out_of_range"),
        ({"port": "8765"}, "bind_port_not_an_integer"),
        ({"path": "mcp"}, "transport_path_invalid"),
        ({"health_path": "healthz"}, "transport_path_invalid"),
        ({"ready_path": "/mcp"}, "transport_path_conflict"),
    ],
)
def test_invalid_transport_settings_are_refused(kwargs: dict, code: str) -> None:
    with pytest.raises(http.HttpTransportError) as caught:
        http.HttpTransportSettings(**kwargs)
    assert caught.value.code == code


def test_loopback_host_detection() -> None:
    assert http.is_loopback_host("127.0.0.1") is True
    assert http.is_loopback_host("localhost") is True
    assert http.is_loopback_host("[::1]") is True
    assert http.is_loopback_host("127.0.0.5") is True
    assert http.is_loopback_host("0.0.0.0") is False
    assert http.is_loopback_host("10.0.0.7") is False


def test_main_requires_explicit_allowlists() -> None:
    with pytest.raises(SystemExit) as caught:
        http.main(["--port", "0"])
    assert caught.value.code == 2


def test_sse_parser_ignores_comments_and_keeps_data_lines() -> None:
    text = ': keep-alive\n\nevent: message\ndata: {"a": 1}\ndata: {"b": 2}\n\n'

    events = http.parse_sse_events(text)

    assert events == (("message", '{"a": 1}\n{"b": 2}'),)


def test_stream_observation_serializes_for_evidence() -> None:
    observation = http.StreamObservation(
        method="POST",
        status_code=200,
        content_type="text/event-stream; charset=utf-8",
        session_id="abc",
        body="",
        events=(("message", '{"jsonrpc": "2.0", "id": 1, "result": {"ok": true}}'),),
    )

    payload = observation.as_dict()

    assert payload["is_event_stream"] is True
    assert payload["event_names"] == ["message"]
    assert payload["messages"] == [{"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}]
