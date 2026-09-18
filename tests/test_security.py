"""C-005 regression test: loopback is not authentication.

The positive cases build a real gateway bound to ``127.0.0.1`` and drive a real
MCP client through the composed ASGI stack with an allowlisted ``Host``, an
allowlisted ``Origin`` and a bearer token, proving the extra policy does not
break the transport. The negative and boundary cases prove the policy is
load-bearing: a lookalike ``Host``, an ``Origin`` on a second port, a missing
token, a token minted for the graphd control audience, a read-only token asking
for reconciliation, an out-of-scope solution, an oversized body and a malformed
authorization scheme are each refused with the canonical code, and no response
ever echoes the presented credential.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path

import httpx
import pytest

from axiom_mcp import http, sdk_compat, security

BASE_URL = "http://127.0.0.1:8765"
HOST = "127.0.0.1:8765"
ORIGIN = "http://127.0.0.1:8765"

READ_SECRET = "read-token-value"
MUTATE_SECRET = "mutate-token-value"
CONTROL_SECRET = "graphd-control-token-value"

READ_SCOPE = security.ScopedToken(
    token_id="read-agent",
    audience=security.MCP_AUDIENCE,
    capabilities=frozenset({security.CAPABILITY_READ}),
    solution_ids=frozenset({"alpha"}),
    project_ids=frozenset({"auth-api"}),
)
MUTATE_SCOPE = security.ScopedToken(
    token_id="reconcile-agent",
    audience=security.MCP_AUDIENCE,
    capabilities=frozenset({security.CAPABILITY_READ, security.CAPABILITY_RECONCILE}),
    solution_ids=frozenset({"alpha"}),
)
CONTROL_SCOPE = security.ScopedToken(
    token_id="daemon-control",
    audience=security.CONTROL_AUDIENCE,
    capabilities=frozenset({security.CAPABILITY_READ, security.CAPABILITY_RECONCILE}),
    solution_ids=frozenset({"alpha"}),
)


def build_registry() -> security.TokenRegistry:
    registry = security.TokenRegistry()
    registry.register(
        security.EnvTokenReference("AXIOM_MCP_READ_TOKEN"),
        READ_SCOPE,
        env={"AXIOM_MCP_READ_TOKEN": READ_SECRET},
    )
    registry.register(
        security.EnvTokenReference("AXIOM_MCP_RECONCILE_TOKEN"),
        MUTATE_SCOPE,
        env={"AXIOM_MCP_RECONCILE_TOKEN": MUTATE_SECRET},
    )
    return registry


def build_gateway() -> http.GatewayApp:
    policy = security.SecurityPolicy.build(
        allowed_hosts=[HOST, "localhost:8765"],
        allowed_origins=[ORIGIN, "http://localhost:8765"],
    )
    settings = http.HttpTransportSettings(host="127.0.0.1", port=8765)
    server = sdk_compat.build_server(
        "c-005-test",
        allowed_hosts=[HOST, "localhost:8765"],
        allowed_origins=[ORIGIN, "http://localhost:8765"],
    )
    return http.build_gateway(
        server, settings=settings, security=policy, authenticator=build_registry()
    )


async def raw_request(
    app: object,
    *,
    method: str = "GET",
    path: str = "/healthz",
    host: str = HOST,
    origin: str | None = None,
    authorization: str | None = None,
    payload: Mapping[str, object] | None = None,
) -> tuple[int, str]:
    headers: dict[str, str] = {"Host": host}
    if origin is not None:
        headers["Origin"] = origin
    if authorization is not None:
        headers["Authorization"] = authorization
    if payload is not None:
        headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL, timeout=30.0
    ) as client:
        response = await client.request(method, path, headers=headers, json=payload)
    return response.status_code, response.text


def error_code(body: str) -> str | None:
    try:
        document = json.loads(body)
    except json.JSONDecodeError:
        return None
    return document.get("code") if isinstance(document, dict) else None


def tool_call(name: str, arguments: Mapping[str, object]) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": name, "arguments": dict(arguments)},
    }


# --- allowlist units -------------------------------------------------------


def test_host_allowlist_entry_without_a_port_admits_any_port() -> None:
    assert security.host_allowed("127.0.0.1:8765", ["127.0.0.1"])
    assert security.host_allowed("127.0.0.1", ["127.0.0.1"])
    assert security.host_allowed("LOCALHOST:1234", ["localhost"])


def test_host_allowlist_entry_with_a_port_admits_only_that_port() -> None:
    assert security.host_allowed("127.0.0.1:8765", ["127.0.0.1:8765"])
    assert not security.host_allowed("127.0.0.1:8766", ["127.0.0.1:8765"])
    assert not security.host_allowed("evil.example:8765", ["127.0.0.1:8765"])


@pytest.mark.parametrize(
    "host",
    ["", "   ", "127.0.0.1:8a", "user@127.0.0.1", "127.0.0.1/path", "127.0.0.1:1:2", "[::1"],
)
def test_malformed_host_headers_are_never_allowed(host: str) -> None:
    assert not security.host_allowed(host, ["127.0.0.1", "::1"])


def test_a_wildcard_entry_cannot_widen_the_host_allowlist() -> None:
    assert not security.host_allowed("anything.example", ["*"])
    assert not security.host_allowed("anything.example", ["*.example"])


def test_ipv6_loopback_host_with_a_port_is_admitted() -> None:
    assert security.host_allowed("[::1]:8765", ["[::1]:8765"])
    assert not security.host_allowed("[::1]:9999", ["[::1]:8765"])


def test_origin_must_match_exactly() -> None:
    assert security.origin_allowed(ORIGIN, [ORIGIN])
    assert not security.origin_allowed("http://127.0.0.1:8766", [ORIGIN])
    assert not security.origin_allowed("http://127.0.0.1.evil.example:8765", [ORIGIN])
    assert not security.origin_allowed("https://127.0.0.1:8765", [ORIGIN])


@pytest.mark.parametrize("origin", ["null", "file:///etc/passwd", "127.0.0.1:8765"])
def test_unusable_origins_are_refused(origin: str) -> None:
    assert not security.origin_allowed(origin, [ORIGIN])


def test_an_absent_origin_is_permitted_for_non_browser_clients() -> None:
    assert security.origin_allowed(None, [ORIGIN])
    assert security.origin_allowed("", [ORIGIN])


def test_policy_rejects_an_empty_or_wildcarded_allowlist() -> None:
    with pytest.raises(security.SecurityError) as empty:
        security.SecurityPolicy.build(allowed_hosts=[], allowed_origins=[ORIGIN])
    assert empty.value.code == "VALIDATION_ERROR"
    with pytest.raises(security.SecurityError):
        security.SecurityPolicy.build(allowed_hosts=["*"], allowed_origins=[ORIGIN])
    with pytest.raises(security.SecurityError):
        security.SecurityPolicy.build(allowed_hosts=[HOST], allowed_origins=["*"])


def test_loopback_policy_names_the_interface_instead_of_a_pattern() -> None:
    policy = security.loopback_policy(8765)
    assert policy.allows("127.0.0.1:8765", ORIGIN)
    assert not policy.allows("127.0.0.1:8766", ORIGIN)
    assert not policy.allows("evil.example:8765", ORIGIN)


# --- token units -----------------------------------------------------------


def test_parse_authorization_returns_none_when_the_header_is_absent() -> None:
    assert security.parse_authorization({}) is None


@pytest.mark.parametrize("value", ["", "Basic abc", "Bearer", "Bearer   ", "token abc"])
def test_parse_authorization_refuses_a_non_bearer_scheme_without_echoing_it(value: str) -> None:
    with pytest.raises(security.SecurityError) as raised:
        security.parse_authorization({"Authorization": value})
    assert raised.value.code == "UNAUTHENTICATED"
    # The refusal is a fixed string derived from the policy, never from the value.
    assert str(raised.value) == "UNAUTHENTICATED:authorization must use the Bearer scheme"


def test_scoped_token_refuses_an_unregistered_capability() -> None:
    with pytest.raises(security.SecurityError) as raised:
        security.ScopedToken(
            token_id="bad",
            audience=security.MCP_AUDIENCE,
            capabilities=frozenset({"admin"}),
            solution_ids=frozenset({"alpha"}),
        )
    assert raised.value.code == "VALIDATION_ERROR"


def test_scoped_token_requires_at_least_one_solution() -> None:
    with pytest.raises(security.SecurityError):
        security.ScopedToken(
            token_id="bad",
            audience=security.MCP_AUDIENCE,
            capabilities=frozenset({security.CAPABILITY_READ}),
            solution_ids=frozenset(),
        )


def test_token_reference_from_config_refuses_a_literal_value() -> None:
    with pytest.raises(security.SecurityError) as raised:
        security.token_reference_from_config({"kind": "env", "token": "literal"})
    assert raised.value.code == "VALIDATION_ERROR"


def test_token_reference_from_config_rejects_an_unknown_kind() -> None:
    with pytest.raises(security.SecurityError):
        security.token_reference_from_config({"kind": "inline"})


def test_environment_and_file_references_resolve(tmp_path: Path) -> None:
    env = security.EnvTokenReference("AXIOM_MCP_READ_TOKEN")
    assert env.resolve({"AXIOM_MCP_READ_TOKEN": "  abc  "}) == "abc"
    assert env.resolve({}) is None

    credential = tmp_path / "token.txt"
    credential.write_text("file-secret\nignored-trailing-line\n", encoding="utf-8")
    assert security.FileTokenReference(credential).resolve() == "file-secret"
    assert security.FileTokenReference(tmp_path / "missing.txt").resolve() is None


def test_registry_refuses_a_reference_that_does_not_resolve() -> None:
    registry = security.TokenRegistry()
    with pytest.raises(security.SecurityError) as raised:
        registry.register(security.EnvTokenReference("AXIOM_MCP_ABSENT"), READ_SCOPE, env={})
    assert raised.value.code == "UNAUTHENTICATED"


def test_registry_refuses_a_token_minted_for_another_audience() -> None:
    registry = security.TokenRegistry()
    with pytest.raises(security.SecurityError) as raised:
        registry.register(
            security.EnvTokenReference("AXIOM_CONTROL_TOKEN"),
            CONTROL_SCOPE,
            env={"AXIOM_CONTROL_TOKEN": CONTROL_SECRET},
        )
    assert raised.value.code == "VALIDATION_ERROR"


def test_registry_refuses_a_duplicate_credential() -> None:
    registry = build_registry()
    with pytest.raises(security.SecurityError) as raised:
        registry.register(
            security.EnvTokenReference("AXIOM_MCP_OTHER"),
            READ_SCOPE,
            env={"AXIOM_MCP_OTHER": READ_SECRET},
        )
    assert raised.value.code == "VALIDATION_ERROR"


def test_graphd_control_credential_is_not_accepted_by_the_mcp_registry() -> None:
    mcp_registry = build_registry()
    control_registry = security.TokenRegistry(expected_audience=security.CONTROL_AUDIENCE)
    control_registry.register(
        security.EnvTokenReference("AXIOM_CONTROL_TOKEN"),
        CONTROL_SCOPE,
        env={"AXIOM_CONTROL_TOKEN": CONTROL_SECRET},
    )
    assert control_registry.size == 1
    with pytest.raises(security.SecurityError) as raised:
        mcp_registry.authenticate(CONTROL_SECRET)
    assert raised.value.code == "UNAUTHENTICATED"


def test_authorize_denies_a_read_token_the_reconcile_capability() -> None:
    registry = build_registry()
    assert registry.authorize(READ_SECRET, security.CAPABILITY_READ).token_id == "read-agent"
    with pytest.raises(security.SecurityError) as raised:
        registry.authorize(READ_SECRET, security.CAPABILITY_RECONCILE)
    assert raised.value.code == "FORBIDDEN"


def test_authorize_denies_an_out_of_scope_solution_and_project() -> None:
    registry = build_registry()
    with pytest.raises(security.SecurityError) as raised:
        registry.authorize(READ_SECRET, security.CAPABILITY_READ, solution_id="beta")
    assert raised.value.code == "FORBIDDEN"
    with pytest.raises(security.SecurityError) as raised:
        registry.authorize(READ_SECRET, security.CAPABILITY_READ, project_id="billing")
    assert raised.value.code == "FORBIDDEN"
    assert registry.authorize(READ_SECRET, security.CAPABILITY_READ, project_id="auth-api")


def test_tool_capability_mapping_covers_the_canonical_catalog() -> None:
    assert security.required_capability(tool_call("graph_reconcile", {})) == "reconcile"
    assert security.required_capability(tool_call("graph_verify", {})) == "checkpoint"
    assert security.required_capability(tool_call("graph_query", {})) == "read"
    assert security.required_capability({"method": "initialize"}) is None
    assert security.required_capability(tool_call("not_registered", {})) == "read"


def test_call_scope_reads_single_and_bounded_project_scope() -> None:
    payload = tool_call("graph_query", {"solution_id": "alpha", "project_ids": ["a", "b"]})
    assert security.call_scope(payload) == ("alpha", ("a", "b"))
    assert security.call_scope(tool_call("graph_query", {"solution_id": "alpha"})) == ("alpha", ())


# --- end-to-end through the composed gateway -------------------------------


def test_loopback_gateway_serves_an_authenticated_mcp_handshake() -> None:
    gateway = build_gateway()
    assert gateway.settings.host == "127.0.0.1"
    assert http.is_loopback_host(gateway.settings.host)
    assert gateway.security is not None

    async def scenario() -> str:
        async with sdk_compat.open_lifespan(gateway.app):
            handshake = await sdk_compat.initialize_handshake(
                gateway.app,
                base_url=BASE_URL,
                headers={"Authorization": f"Bearer {READ_SECRET}"},
            )
        return handshake.server_name

    assert asyncio.run(scenario()) == "c-005-test"


def test_loopback_gateway_still_rejects_a_disallowed_host() -> None:
    gateway = build_gateway()
    status, body = asyncio.run(raw_request(gateway.app, host="evil.example:8765"))
    assert status == 403
    assert error_code(body) == "FORBIDDEN"


def test_loopback_gateway_still_rejects_a_host_sharing_only_the_port() -> None:
    gateway = build_gateway()
    status, body = asyncio.run(raw_request(gateway.app, host="127.0.0.1:9999"))
    assert status == 403
    assert error_code(body) == "FORBIDDEN"


def test_loopback_gateway_still_rejects_a_disallowed_origin() -> None:
    gateway = build_gateway()
    status, body = asyncio.run(raw_request(gateway.app, origin="http://127.0.0.1:9999"))
    assert status == 403
    assert error_code(body) == "FORBIDDEN"


def test_probes_need_no_token_but_still_need_an_allowed_host() -> None:
    gateway = build_gateway()
    allowed, _ = asyncio.run(raw_request(gateway.app, path="/healthz"))
    assert allowed == 200
    rejected, body = asyncio.run(raw_request(gateway.app, path="/healthz", host="evil.example"))
    assert rejected == 403
    assert error_code(body) == "FORBIDDEN"


def test_unauthenticated_mcp_request_is_denied() -> None:
    gateway = build_gateway()
    status, body = asyncio.run(raw_request(gateway.app, method="POST", path="/mcp", payload={}))
    assert status == 401
    assert error_code(body) == "UNAUTHENTICATED"


def test_control_credential_is_refused_on_the_mcp_endpoint() -> None:
    gateway = build_gateway()
    status, body = asyncio.run(
        raw_request(
            gateway.app,
            method="POST",
            path="/mcp",
            payload={},
            authorization=f"Bearer {CONTROL_SECRET}",
        )
    )
    assert status == 401
    assert error_code(body) == "UNAUTHENTICATED"


def test_unauthenticated_mutation_is_denied_before_the_tool_plane() -> None:
    gateway = build_gateway()
    payload = tool_call("graph_reconcile", {"solution_id": "alpha", "scope": "full"})
    status, body = asyncio.run(
        raw_request(gateway.app, method="POST", path="/mcp", payload=payload)
    )
    assert status == 401
    assert error_code(body) == "UNAUTHENTICATED"


def test_a_read_token_cannot_invoke_a_mutating_tool() -> None:
    gateway = build_gateway()
    payload = tool_call("graph_reconcile", {"solution_id": "alpha"})
    status, body = asyncio.run(
        raw_request(
            gateway.app,
            method="POST",
            path="/mcp",
            payload=payload,
            authorization=f"Bearer {READ_SECRET}",
        )
    )
    assert status == 403
    assert error_code(body) == "FORBIDDEN"


def test_a_read_token_cannot_read_an_unscoped_solution() -> None:
    gateway = build_gateway()
    payload = tool_call("graph_query", {"solution_id": "beta", "operation": "search"})
    status, body = asyncio.run(
        raw_request(
            gateway.app,
            method="POST",
            path="/mcp",
            payload=payload,
            authorization=f"Bearer {READ_SECRET}",
        )
    )
    assert status == 403
    assert error_code(body) == "FORBIDDEN"


def test_a_reconcile_token_still_cannot_read_an_unscoped_project() -> None:
    scoped = security.ScopedToken(
        token_id="project-scoped",
        audience=security.MCP_AUDIENCE,
        capabilities=frozenset({security.CAPABILITY_READ}),
        solution_ids=frozenset({"alpha"}),
        project_ids=frozenset({"auth-api"}),
    )
    registry = security.TokenRegistry()
    registry.register(
        security.EnvTokenReference("AXIOM_MCP_PROJECT_TOKEN"),
        scoped,
        env={"AXIOM_MCP_PROJECT_TOKEN": "project-secret"},
    )
    policy = security.SecurityPolicy.build(allowed_hosts=[HOST], allowed_origins=[ORIGIN])
    server = sdk_compat.build_server("c-005-test", allowed_hosts=[HOST], allowed_origins=[ORIGIN])
    app = http.build_gateway(
        server,
        settings=http.HttpTransportSettings(host="127.0.0.1", port=8765),
        security=policy,
        authenticator=registry,
    ).app
    payload = tool_call("graph_query", {"solution_id": "alpha", "project_id": "billing"})
    status, body = asyncio.run(
        raw_request(
            app,
            method="POST",
            path="/mcp",
            payload=payload,
            authorization="Bearer project-secret",
        )
    )
    assert status == 403
    assert error_code(body) == "FORBIDDEN"


def test_an_oversized_body_is_refused_before_parsing() -> None:
    gateway = build_gateway()
    oversized = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"blob": "x" * 400000},
    }
    status, body = asyncio.run(
        raw_request(
            gateway.app,
            method="POST",
            path="/mcp",
            payload=oversized,
            authorization=f"Bearer {READ_SECRET}",
        )
    )
    assert status == 400
    assert error_code(body) == "VALIDATION_ERROR"


def test_no_response_echoes_the_presented_credential() -> None:
    gateway = build_gateway()
    for authorization in (f"Bearer {CONTROL_SECRET}", "Basic not-a-bearer"):
        status, body = asyncio.run(
            raw_request(
                gateway.app, method="POST", path="/mcp", payload={}, authorization=authorization
            )
        )
        assert status in {401, 403}
        assert CONTROL_SECRET not in body
        assert "not-a-bearer" not in body
        assert json.loads(body)["request_id"]
