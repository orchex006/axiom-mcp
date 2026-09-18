"""Streamable HTTP transport for the axiom-mcp gateway.

The contract exposes exactly one MCP endpoint - ``/mcp`` - as a real MCP
Streamable HTTP transport, and two operational probes that are *not* the MCP
endpoint:

* ``/healthz`` answers minimal process health. It deliberately carries no
  readiness claim, because "the process is up" and "queries can be answered" are
  different statements and conflating them is how a health probe becomes a lie.
* ``/readyz`` reports the query and control planes separately, so a caller can
  tell which plane is unavailable.

This module assembles the transport; it does not define protocol or policy. The
MCP endpoint is the pinned SDK's own handler, placed by
:mod:`axiom_mcp.sdk_compat`. The tool catalog belongs to the query gateway, and
the host/origin allowlist and token scope policy belong to
:mod:`axiom_mcp.security` (C-005); the allowlists accepted here are passed
through unchanged and are never widened.

Streaming semantics are observed, not assumed: :func:`observe_streamable_http`
performs a raw request against the composed ASGI stack and reports the real
status, content type, session header and parsed ``text/event-stream`` frames.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP

from axiom_mcp import sdk_compat, version

MCP_ENDPOINT_PATH = "/mcp"
HEALTH_PATH = "/healthz"
READY_PATH = "/readyz"

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
LOOPBACK_BIND_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# Canonical error code from contracts/error-codes.json: the process is up but a
# required capability is not available yet.
NOT_READY = "NOT_READY"

# The SDK requires a client to accept *both* representations; a client that only
# accepts JSON is refused with HTTP 406 rather than silently downgraded.
STREAMABLE_ACCEPT = "application/json, text/event-stream"

_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9._~-]+$")

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "HEALTH_PATH",
    "MCP_ENDPOINT_PATH",
    "NOT_READY",
    "READY_PATH",
    "STREAMABLE_ACCEPT",
    "ComponentReadiness",
    "GatewayApp",
    "HttpTransportError",
    "HttpTransportSettings",
    "ReadinessReport",
    "StreamObservation",
    "build_gateway",
    "is_loopback_host",
    "main",
    "observe_streamable_http",
    "parse_sse_events",
    "serve",
    "serve_async",
]


class HttpTransportError(RuntimeError):
    """A transport configuration or observation failure with a stable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


def is_loopback_host(host: str) -> bool:
    """Return True when ``host`` names only the loopback interface."""
    candidate = host.strip().lower()
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    if candidate in LOOPBACK_BIND_HOSTS:
        return True
    return candidate.startswith("127.")


@dataclass(frozen=True)
class HttpTransportSettings:
    """Where the transport listens. Defaults bind loopback only."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    path: str = MCP_ENDPOINT_PATH
    health_path: str = HEALTH_PATH
    ready_path: str = READY_PATH
    allow_non_loopback_bind: bool = False

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise HttpTransportError("bind_host_empty")
        if not isinstance(self.port, int) or isinstance(self.port, bool):
            raise HttpTransportError("bind_port_not_an_integer", repr(self.port))
        if not 0 <= self.port <= 65535:
            raise HttpTransportError("bind_port_out_of_range", str(self.port))
        for name, value in (
            ("path", self.path),
            ("health_path", self.health_path),
            ("ready_path", self.ready_path),
        ):
            if not _PATH_PATTERN.fullmatch(value):
                raise HttpTransportError("transport_path_invalid", f"{name}:{value}")
        if len({self.path, self.health_path, self.ready_path}) != 3:
            raise HttpTransportError("transport_path_conflict")
        # A non-loopback bind without an explicit acknowledgement is refused here
        # rather than silently exposing an unauthenticated gateway.
        if not is_loopback_host(self.host) and not self.allow_non_loopback_bind:
            raise HttpTransportError("non_loopback_bind_refused", self.host)

    @property
    def loopback_only(self) -> bool:
        return is_loopback_host(self.host)


@dataclass(frozen=True)
class ComponentReadiness:
    """One plane's availability, reported separately from the other."""

    name: str
    available: bool
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"available": self.available}
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class ReadinessReport:
    """Query and control readiness, kept distinct on purpose."""

    query: ComponentReadiness
    control: ComponentReadiness

    @property
    def ready(self) -> bool:
        return self.query.available and self.control.available

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "query": self.query.as_dict(),
            "control": self.control.as_dict(),
            "error": None if self.ready else NOT_READY,
        }


def default_readiness() -> ReadinessReport:
    """Transport-only default: the process serves, but no data plane is wired.

    A later task owns the real probes. This default reports unavailable rather
    than assuming a capability exists, so ``/readyz`` cannot claim readiness the
    gateway has not earned.
    """
    return ReadinessReport(
        query=ComponentReadiness("query", False, "query data plane not wired"),
        control=ComponentReadiness("control", False, "graphd control client not wired"),
    )


@dataclass
class GatewayApp:
    """The composed gateway: one MCP endpoint plus the two operational probes."""

    app: FastAPI
    mounted: sdk_compat.MountedMcp
    settings: HttpTransportSettings
    readiness: Callable[[], ReadinessReport]


def build_gateway(
    server: FastMCP,
    *,
    settings: HttpTransportSettings | None = None,
    readiness: Callable[[], ReadinessReport] | None = None,
) -> GatewayApp:
    """Compose the MCP transport and the probes into one ASGI application.

    The MCP endpoint is registered by :mod:`axiom_mcp.sdk_compat`, so the
    endpoint path, the lifespan composition and the handler identity are the
    same ones C-002 verified. This function adds only the probes.
    """
    settings = settings if settings is not None else HttpTransportSettings()
    probe = readiness if readiness is not None else default_readiness

    app = FastAPI(title=version.COMPONENT, version=version.VERSION)

    @app.get(settings.health_path)
    async def healthz() -> JSONResponse:
        # Minimal process health. Intentionally not a readiness claim.
        return JSONResponse({"status": "ok"})

    @app.get(settings.ready_path)
    async def readyz() -> JSONResponse:
        report = probe()
        return JSONResponse(report.as_dict(), status_code=200 if report.ready else 503)

    mounted = sdk_compat.mount_streamable_http(app, server, path=settings.path)
    return GatewayApp(app=app, mounted=mounted, settings=settings, readiness=probe)


def parse_sse_events(text: str) -> tuple[tuple[str, str], ...]:
    """Parse ``text/event-stream`` frames into ``(event, data)`` pairs.

    Only the two fields this transport uses are read. A frame without a ``data``
    line is not an event and is skipped, so a comment or keep-alive line cannot
    be mistaken for a protocol message.
    """
    events: list[tuple[str, str]] = []
    event_name = "message"
    data_lines: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw_line == "":
            if data_lines:
                events.append((event_name, "\n".join(data_lines)))
            event_name, data_lines = "message", []
            continue
        if raw_line.startswith(":"):
            continue
        field, _, value = raw_line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
    if data_lines:
        events.append((event_name, "\n".join(data_lines)))
    return tuple(events)


@dataclass(frozen=True)
class StreamObservation:
    """What the wire actually returned for one Streamable HTTP request."""

    method: str
    status_code: int
    content_type: str
    session_id: str | None
    body: str
    events: tuple[tuple[str, str], ...]

    @property
    def is_event_stream(self) -> bool:
        return self.content_type.split(";", 1)[0].strip() == "text/event-stream"

    @property
    def messages(self) -> tuple[dict[str, Any], ...]:
        """The JSON-RPC messages carried by the stream, in order."""
        parsed: list[dict[str, Any]] = []
        for _, data in self.events:
            try:
                document = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(document, dict):
                parsed.append(document)
        return tuple(parsed)

    @property
    def json_body(self) -> dict[str, Any] | None:
        try:
            document = json.loads(self.body)
        except json.JSONDecodeError:
            return None
        return document if isinstance(document, dict) else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "session_id": self.session_id,
            "is_event_stream": self.is_event_stream,
            "event_names": [name for name, _ in self.events],
            "messages": list(self.messages),
        }


async def observe_streamable_http(
    app: FastAPI,
    *,
    path: str = MCP_ENDPOINT_PATH,
    base_url: str = DEFAULT_BASE_URL,
    method: str = "POST",
    payload: Mapping[str, Any] | None = None,
    accept: str = STREAMABLE_ACCEPT,
    headers: Mapping[str, str] | None = None,
    session_id: str | None = None,
    timeout: float = 30.0,
) -> StreamObservation:
    """Send one request to the MCP endpoint and report the raw wire result.

    The caller must already be inside the application's lifespan. No socket is
    opened: the request travels through the composed ASGI stack, so this
    observes the same route, session manager and transport a network client
    would reach.
    """
    request_headers = {"Accept": accept}
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    if session_id is not None:
        request_headers["mcp-session-id"] = session_id
    request_headers.update(headers or {})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=base_url, timeout=timeout
    ) as client:
        response = await client.request(
            method, path, headers=request_headers, json=payload if payload is not None else None
        )

    content_type = response.headers.get("content-type", "")
    body = response.text
    events: tuple[tuple[str, str], ...] = ()
    if content_type.split(";", 1)[0].strip() == "text/event-stream":
        events = parse_sse_events(body)
    return StreamObservation(
        method=method,
        status_code=response.status_code,
        content_type=content_type,
        session_id=response.headers.get("mcp-session-id"),
        body=body,
        events=events,
    )


def uvicorn_config(app: FastAPI, settings: HttpTransportSettings) -> uvicorn.Config:
    """Build the uvicorn configuration for ``app`` from explicit settings."""
    return uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level="warning",
        access_log=False,
    )


async def serve_async(app: FastAPI, settings: HttpTransportSettings | None = None) -> None:
    """Serve ``app`` over HTTP until the server shuts down."""
    settings = settings if settings is not None else HttpTransportSettings()
    await uvicorn.Server(uvicorn_config(app, settings)).serve()


def serve(app: FastAPI, settings: HttpTransportSettings | None = None) -> None:
    """Blocking entry point used by the module CLI."""
    asyncio.run(serve_async(app, settings))


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axiom-mcp-http", description="Run the axiom-mcp Streamable HTTP transport."
    )
    parser.add_argument("--name", default=version.COMPONENT)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="HOST",
        help="Transport security Host allowlist entry. Repeatable; no implicit default.",
    )
    parser.add_argument(
        "--allow-origin",
        action="append",
        default=[],
        metavar="ORIGIN",
        help="Transport security Origin allowlist entry. Repeatable; no implicit default.",
    )
    parser.add_argument(
        "--allow-non-loopback-bind",
        action="store_true",
        help="Acknowledge binding a non-loopback address. Required for a remote bind.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the HTTP gateway.

    The allowlists are required rather than defaulted: inventing a permissive
    value here would silently widen the transport security that
    :mod:`axiom_mcp.security` owns.
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    if not args.allow_host or not args.allow_origin:
        parser.error("--allow-host and --allow-origin are required and have no default")

    settings = HttpTransportSettings(
        host=args.host,
        port=args.port,
        allow_non_loopback_bind=args.allow_non_loopback_bind,
    )
    server = sdk_compat.build_server(
        args.name, allowed_hosts=args.allow_host, allowed_origins=args.allow_origin
    )
    gateway = build_gateway(server, settings=settings)
    serve(gateway.app, settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
