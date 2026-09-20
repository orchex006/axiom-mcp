"""Real-socket HTTP security server used by the V2-031 native matrix.

``python -m axiom_mcp.http`` composes the transport without a credential
middleware, so a *credential* leg cannot be exercised through the module CLI
alone. This launcher builds the same composed gateway the C-005 security tests
build - the pinned SDK server, the ``axiom_mcp.security`` policy and the scoped
token registry - and then serves it over a real loopback TCP socket with
uvicorn, so the native matrix can send real HTTP requests over the network stack
instead of through an in-process ASGI transport.

Tokens are supplied through the environment and never on the command line, which
matches the contract rule that no token appears in an argv or a portable config.
The launcher prints one ``READY <origin>`` line to stdout so the harness knows the
socket is accepting connections; nothing else is written to stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import uvicorn  # noqa: E402

from axiom_mcp import http, sdk_compat, security  # noqa: E402

#: Environment variable names holding the presented secrets. A name is not a value.
READ_TOKEN_ENV = "V2031_READ_TOKEN"
CONTROL_TOKEN_ENV = "V2031_CONTROL_TOKEN"

#: The solution the MCP audience token is scoped to.
SCOPED_SOLUTION = "demo-solution"


def build_gateway(host: str, port: int, name: str):
    """Compose the real gateway: SDK server + transport security + token policy."""
    bind = f"{host}:{port}"
    origin = f"http://{bind}"
    policy = security.SecurityPolicy.build(allowed_hosts=[bind], allowed_origins=[origin])
    settings = http.HttpTransportSettings(host=host, port=port)
    server = sdk_compat.build_server(name, allowed_hosts=[bind], allowed_origins=[origin])

    registry = security.TokenRegistry()
    environment = dict(os.environ)
    registry.register(
        security.EnvTokenReference(READ_TOKEN_ENV),
        security.ScopedToken(
            token_id="v2-031-read",
            audience=security.MCP_AUDIENCE,
            capabilities=frozenset({security.CAPABILITY_READ}),
            solution_ids=frozenset({SCOPED_SOLUTION}),
        ),
        env=environment,
    )

    # A registry is bound to one audience, so the graphd control credential cannot be
    # registered here. It is registered in a control-audience registry instead, which
    # proves the credential the MCP endpoint must refuse is a real resolvable
    # credential rather than an unknown string.
    control_registry = security.TokenRegistry(expected_audience=security.CONTROL_AUDIENCE)
    control_registry.register(
        security.EnvTokenReference(CONTROL_TOKEN_ENV),
        security.ScopedToken(
            token_id="v2-031-control",
            audience=security.CONTROL_AUDIENCE,
            capabilities=frozenset({security.CAPABILITY_READ, security.CAPABILITY_RECONCILE}),
            solution_ids=frozenset({SCOPED_SOLUTION}),
        ),
        env=environment,
    )
    print(
        f"CONTROL_REGISTRY size={control_registry.size} "
        f"audience={control_registry.expected_audience}",
        file=sys.stderr,
        flush=True,
    )
    return http.build_gateway(server, settings=settings, security=policy, authenticator=registry)


async def serve(gateway, host: str, port: int) -> None:
    """Serve ``gateway`` until the process is stopped, announcing the bound origin."""
    settings = http.HttpTransportSettings(host=host, port=port)
    server = uvicorn.Server(http.uvicorn_config(gateway.app, settings))
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            await task
            return
        await asyncio.sleep(0.02)
    print(f"READY http://{host}:{port}", flush=True)
    await task


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="v2-031-http-security-server",
        description="Serve the composed axiom-mcp gateway with its credential policy.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--name", default="v2-031-native-http")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    for variable in (READ_TOKEN_ENV, CONTROL_TOKEN_ENV):
        if not os.environ.get(variable):
            parser.error(f"{variable} must be set in the environment")

    gateway = build_gateway(args.host, args.port, args.name)
    asyncio.run(serve(gateway, args.host, args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
