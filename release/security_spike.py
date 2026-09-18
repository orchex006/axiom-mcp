"""Record the C-005 host/origin/scope evidence against the pinned SDK.

Run from the repository root:

    python release/security_spike.py

The output is JSON on stdout and is stored as the C-005 compatibility artifact.
It records what a real loopback gateway actually did - which host and origin
values were served, which were refused, and what an unauthenticated or
under-scoped call received - rather than asserting that the policy exists.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
from typing import Any

import httpx

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from axiom_mcp import http, sdk_compat, security, version  # noqa: E402

HOST = "127.0.0.1:8765"
ORIGIN = "http://127.0.0.1:8765"
BASE_URL = "http://127.0.0.1:8765"

READ_SECRET = "spike-read-token"
CONTROL_SECRET = "spike-control-token"


def build_gateway() -> http.GatewayApp:
    policy = security.SecurityPolicy.build(
        allowed_hosts=[HOST, "localhost:8765"],
        allowed_origins=[ORIGIN],
    )
    registry = security.TokenRegistry()
    registry.register(
        security.EnvTokenReference("AXIOM_MCP_READ_TOKEN"),
        security.ScopedToken(
            token_id="spike-read",
            audience=security.MCP_AUDIENCE,
            capabilities=frozenset({security.CAPABILITY_READ}),
            solution_ids=frozenset({"alpha"}),
        ),
        env={"AXIOM_MCP_READ_TOKEN": READ_SECRET},
    )
    server = sdk_compat.build_server("c-005-spike", allowed_hosts=[HOST], allowed_origins=[ORIGIN])
    return http.build_gateway(
        server,
        settings=http.HttpTransportSettings(host="127.0.0.1", port=8765),
        security=policy,
        authenticator=registry,
    )


async def request(
    gateway: http.GatewayApp,
    *,
    method: str = "GET",
    path: str = "/healthz",
    host: str = HOST,
    origin: str | None = None,
    authorization: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers: dict[str, str] = {"Host": host}
    if origin is not None:
        headers["Origin"] = origin
    if authorization is not None:
        headers["Authorization"] = authorization
    if payload is not None:
        headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway.app), base_url=BASE_URL, timeout=30.0
    ) as client:
        response = await client.request(method, path, headers=headers, json=payload)
    document: Any
    try:
        document = json.loads(response.text)
    except json.JSONDecodeError:
        document = None
    return {
        "status": response.status_code,
        "code": document.get("code") if isinstance(document, dict) else None,
    }


def tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 11,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


async def probe() -> dict[str, Any]:
    gateway = build_gateway()
    async with sdk_compat.open_lifespan(gateway.app):
        handshake = await sdk_compat.initialize_handshake(
            gateway.app, base_url=BASE_URL, headers={"Authorization": f"Bearer {READ_SECRET}"}
        )
        served = await request(gateway, path="/healthz")
        refused_host = await request(gateway, path="/healthz", host="evil.example:8765")
        refused_origin = await request(gateway, path="/healthz", origin="http://127.0.0.1:9999")
        no_token = await request(gateway, method="POST", path="/mcp", payload={})
        control_token = await request(
            gateway,
            method="POST",
            path="/mcp",
            payload={},
            authorization=f"Bearer {CONTROL_SECRET}",
        )
        read_token_mutation = await request(
            gateway,
            method="POST",
            path="/mcp",
            payload=tool_call("graph_reconcile", {"solution_id": "alpha"}),
            authorization=f"Bearer {READ_SECRET}",
        )
        unscoped_solution = await request(
            gateway,
            method="POST",
            path="/mcp",
            payload=tool_call("graph_query", {"solution_id": "beta"}),
            authorization=f"Bearer {READ_SECRET}",
        )
    return {
        "bind_host": gateway.settings.host,
        "loopback_only": http.is_loopback_host(gateway.settings.host),
        "allowed_hosts": sorted(gateway.security.allowed_hosts) if gateway.security else [],
        "allowed_origins": sorted(gateway.security.allowed_origins) if gateway.security else [],
        "authenticated_handshake_server": handshake.server_name,
        "probe_with_allowed_host": served,
        "probe_with_disallowed_host": refused_host,
        "probe_with_disallowed_origin": refused_origin,
        "mcp_without_token": no_token,
        "mcp_with_control_token": control_token,
        "read_token_mutation": read_token_mutation,
        "read_token_unscoped_solution": unscoped_solution,
    }


def main() -> int:
    result = asyncio.run(probe())
    result["policy_enforced"] = bool(
        result["probe_with_allowed_host"]["status"] == 200
        and result["probe_with_disallowed_host"]["status"] == 403
        and result["probe_with_disallowed_origin"]["status"] == 403
        and result["mcp_without_token"]["code"] == "UNAUTHENTICATED"
        and result["mcp_with_control_token"]["code"] == "UNAUTHENTICATED"
        and result["read_token_mutation"]["code"] == "FORBIDDEN"
        and result["read_token_unscoped_solution"]["code"] == "FORBIDDEN"
    )
    report = {
        "component": version.COMPONENT,
        "version": version.VERSION,
        "sdk": f"{version.SDK_PACKAGE}=={version.SDK_PIN}",
        "audiences": {"mcp": security.MCP_AUDIENCE, "control": security.CONTROL_AUDIENCE},
        "capabilities": sorted(security.KNOWN_CAPABILITIES),
        "observed": result,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if result["policy_enforced"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
