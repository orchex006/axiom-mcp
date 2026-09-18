"""Mount the official MCP SDK's Streamable HTTP app inside FastAPI.

The SDK exposes the Streamable HTTP transport as a Starlette application
(``FastMCP.streamable_http_app()``) whose own lifespan starts and stops the
session manager. Starlette's ``Mount`` does **not** compose a sub-application's
lifespan, so a plain ``app.mount("/mcp", sub_app)`` serves HTTP by accident
while leaving the session manager unstarted: the first protocol request fails
with "Task group is not initialized" (recorded in the C-002 spike artifact).

This module therefore does two explicit things instead of assuming them:

1. it composes the parent lifespan with the SDK sub-application's lifespan, so
   the session manager starts exactly once and stops exactly once;
2. it re-parents the SDK's own Streamable HTTP handler onto the contract path
   ``/mcp``, so the contract endpoint is an exact match with no trailing-slash
   redirect and without mounting anything at ``/`` (a root mount would shadow
   every route registered after it).

Neither the transport security policy nor the tool catalog is defined here:
this module places the SDK app the pin in ``axiom_mcp.version`` was verified
against and reports what it observed. Host, Origin and token policy belong to
``axiom_mcp.security``; the tool catalog belongs to the query gateway.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Route

from axiom_mcp import version

MCP_ENDPOINT_PATH = "/mcp"

# The contract exposes exactly one MCP endpoint directly under the root, so a
# relocated handler must be a single path segment. A nested or empty path would
# silently produce a second, unreviewed endpoint.
_MOUNT_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9._~-]+$")

_MOUNT_ATTRIBUTE = "axiom_mcp_mount"


class SdkCompatError(RuntimeError):
    """A pinned SDK assumption failed, with a stable machine-readable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


@dataclass
class LifecycleRecorder:
    """Count how many times the composed MCP lifespan was entered and left."""

    starts: int = 0
    stops: int = 0

    def note_start(self) -> None:
        if self.starts != self.stops:
            raise SdkCompatError("lifecycle_already_running", f"starts={self.starts}")
        self.starts += 1

    def note_stop(self) -> None:
        if self.starts != self.stops + 1:
            raise SdkCompatError("lifecycle_not_running", f"starts={self.starts}")
        self.stops += 1

    @property
    def running(self) -> bool:
        return self.starts != self.stops

    @property
    def balanced(self) -> bool:
        return self.starts == self.stops

    @property
    def started_exactly_once(self) -> bool:
        return self.starts == 1 and self.stops == 1


@dataclass(frozen=True)
class HandshakeResult:
    """What the mounted server actually negotiated, not what was requested."""

    protocol_version: str
    server_name: str
    server_version: str
    capabilities: tuple[str, ...]
    advertised: tuple[str, ...]
    instructions: str | None = None

    @property
    def negotiated_is_advertised(self) -> bool:
        return self.protocol_version in self.advertised

    @property
    def meets_pinned_minimum(self) -> bool:
        return self.protocol_version == version.MCP_PROTOCOL_MINIMUM


@dataclass
class MountedMcp:
    """The SDK objects a mount placed into a FastAPI application."""

    server: FastMCP
    subapp: Starlette
    handler: Any
    path: str
    recorder: LifecycleRecorder = field(default_factory=LifecycleRecorder)


def build_transport_security(
    *,
    allowed_hosts: Sequence[str],
    allowed_origins: Sequence[str],
    enable_dns_rebinding_protection: bool = True,
) -> TransportSecuritySettings:
    """Build SDK transport security from explicit allowlists.

    The lists are required: this helper refuses to invent a permissive default,
    and it never widens an allowlist by adding ``*``. Deciding which hosts and
    origins are allowed is ``axiom_mcp.security``'s job.
    """
    hosts = [h for h in allowed_hosts if h.strip()]
    origins = [o for o in allowed_origins if o.strip()]
    if not hosts or not origins:
        raise SdkCompatError("transport_security_allowlist_empty")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=enable_dns_rebinding_protection,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def build_server(
    name: str,
    *,
    instructions: str | None = None,
    allowed_hosts: Sequence[str] | None = None,
    allowed_origins: Sequence[str] | None = None,
    json_response: bool = False,
    stateless_http: bool = False,
    log_level: str = "WARNING",
) -> FastMCP:
    """Create the pinned SDK server.

    When allowlists are given they are applied verbatim. When they are omitted
    the SDK's own loopback default is left in place rather than replaced with a
    locally invented policy; a public bind therefore still has to pass an
    explicit allowlist.
    """
    if (allowed_hosts is None) != (allowed_origins is None):
        raise SdkCompatError("transport_security_partial_allowlist")
    transport_security = None
    if allowed_hosts is not None and allowed_origins is not None:
        transport_security = build_transport_security(
            allowed_hosts=allowed_hosts, allowed_origins=allowed_origins
        )
    return FastMCP(
        name=name,
        instructions=instructions,
        json_response=json_response,
        stateless_http=stateless_http,
        log_level=log_level,
        transport_security=transport_security,
    )


def create_streamable_http_subapp(server: FastMCP) -> Starlette:
    """Return the SDK's Streamable HTTP sub-application for ``server``.

    Calling this is also what creates the session manager; the SDK creates it
    lazily and raises before this call, so a later failure to enter the
    composed lifespan is a real lifecycle failure and not an ordering accident.
    """
    subapp = server.streamable_http_app()
    if not isinstance(subapp, Starlette):
        raise SdkCompatError("streamable_http_app_not_starlette", type(subapp).__name__)
    return subapp


def extract_streamable_http_handler(subapp: Starlette, path: str) -> Any:
    """Return the SDK's own ASGI handler for ``path``.

    The handler object is reused as-is. This module does not re-implement the
    transport; it relocates the SDK's handler onto the contract path.
    """
    matches = [route for route in subapp.routes if getattr(route, "path", None) == path]
    if len(matches) != 1:
        raise SdkCompatError("streamable_http_route_not_unique", f"{path}:{len(matches)}")
    handler = getattr(matches[0], "endpoint", None) or getattr(matches[0], "app", None)
    if handler is None:
        raise SdkCompatError("streamable_http_handler_missing", path)
    return handler


def compose_lifespan(
    subapp: Starlette,
    recorder: LifecycleRecorder,
    previous: Callable[[Any], Any] | None = None,
) -> Callable[[Any], Any]:
    """Compose the mounted MCP lifespan with any existing parent lifespan.

    Starlette runs ``router.lifespan_context`` of the top-level application
    only, so the mounted lifespan has to be entered here explicitly.
    """
    inner = previous if previous is not None else _noop_lifespan

    @asynccontextmanager
    async def lifespan(app: Any) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(inner(app))
            await stack.enter_async_context(subapp.router.lifespan_context(subapp))
            recorder.note_start()
            try:
                yield
            finally:
                recorder.note_stop()

    return lifespan


@asynccontextmanager
async def _noop_lifespan(app: Any) -> AsyncIterator[None]:
    yield


def mount_streamable_http(
    app: FastAPI,
    server: FastMCP,
    *,
    path: str = MCP_ENDPOINT_PATH,
    recorder: LifecycleRecorder | None = None,
) -> MountedMcp:
    """Mount a real MCP Streamable HTTP server on ``app`` at ``path``.

    Must be called before the application starts: the composed lifespan has to
    be installed on the router first. Registering MCP twice on one application
    is refused, because a second session manager would start a second set of
    resources that nothing stops.
    """
    if not _MOUNT_PATH_PATTERN.fullmatch(path):
        raise SdkCompatError("mount_path_invalid", path)
    if getattr(app.state, _MOUNT_ATTRIBUTE, None) is not None:
        raise SdkCompatError("mcp_already_mounted", path)

    subapp = create_streamable_http_subapp(server)
    handler = extract_streamable_http_handler(subapp, server.settings.streamable_http_path)
    recorder = recorder if recorder is not None else LifecycleRecorder()
    app.router.lifespan_context = compose_lifespan(
        subapp, recorder, previous=app.router.lifespan_context
    )
    app.router.routes.append(Route(path, endpoint=handler))

    mounted = MountedMcp(
        server=server, subapp=subapp, handler=handler, path=path, recorder=recorder
    )
    setattr(app.state, _MOUNT_ATTRIBUTE, mounted)
    return mounted


def mounted_mcp(app: FastAPI) -> MountedMcp | None:
    """Return the mount registered on ``app``, if any."""
    return getattr(app.state, _MOUNT_ATTRIBUTE, None)


@asynccontextmanager
async def open_lifespan(app: FastAPI) -> AsyncIterator[FastAPI]:
    """Run an application's composed lifespan for the duration of the block."""
    async with app.router.lifespan_context(app):
        yield app


def asgi_http_client(
    app: FastAPI,
    *,
    base_url: str = "http://127.0.0.1:8000",
    timeout: httpx.Timeout | float | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.AsyncClient:
    """Return an ``httpx`` client bound to the mounted app in this process.

    No socket is opened: the MCP client talks to the mounted ASGI stack
    directly, so a handshake here exercises the same route, session manager and
    lifespan a network client would, without binding a port.
    """
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=base_url,
        timeout=timeout if timeout is not None else 30.0,
        headers=headers,
    )


async def initialize_handshake(
    app: FastAPI,
    *,
    path: str = MCP_ENDPOINT_PATH,
    base_url: str = "http://127.0.0.1:8000",
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
) -> HandshakeResult:
    """Perform a real MCP ``initialize`` against the mounted server.

    The request travels through the mounted ASGI application, so the recorded
    protocol revision is the one the server actually negotiated. The caller
    must already be inside the application's lifespan: the session manager has
    to be running for the transport to answer.
    """
    url = f"{base_url}{path}"
    async with asgi_http_client(app, base_url=base_url, timeout=timeout, headers=headers) as client:
        async with streamable_http_client(url, http_client=client) as (
            read_stream,
            write_stream,
            _session_id,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                result = await session.initialize()

    advertised = version.advertised_protocol_versions()
    handshake = HandshakeResult(
        protocol_version=result.protocolVersion,
        server_name=result.serverInfo.name,
        server_version=result.serverInfo.version,
        capabilities=tuple(sorted(result.capabilities.model_dump(exclude_none=True))),
        advertised=advertised,
        instructions=result.instructions,
    )
    reasons = version.protocol_support_reasons(advertised)
    if reasons:
        raise SdkCompatError("protocol_advertisement_invalid", ",".join(reasons))
    if not handshake.negotiated_is_advertised:
        raise SdkCompatError("protocol_negotiated_not_advertised", handshake.protocol_version)
    return handshake
