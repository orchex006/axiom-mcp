"""Host, Origin and scoped-token policy for the axiom-mcp gateway.

This module owns the *policy*: which ``Host`` and ``Origin`` values the HTTP
transport may serve, and which capability a bearer token must carry before the
gateway answers. It does not own the transport. The SDK applies the same
allowlists to the MCP route, and :class:`SecurityMiddleware` here applies them,
plus authentication, to the composed ASGI application.

Three statements are kept apart on purpose, because collapsing them is how a
security control becomes decorative:

* binding to loopback is not authentication - a loopback server is still
  reachable by a local malicious page through the browser, so ``Host`` and
  ``Origin`` are validated even when the bind address is ``127.0.0.1``;
* holding a token is not holding a capability - a token that may read a
  solution may not enqueue reconciliation work or cancel a job;
* the graphd control audience is not the MCP audience - presenting the daemon
  control credential to the MCP endpoint is refused instead of being used to
  elevate the caller, because the gateway must re-authorize the incoming
  capability rather than inherit admin authority.

Token *values* never live in configuration. A configuration entry names an
environment variable or a credential file; the registry resolves the reference
and stores only a SHA-256 digest, so the plaintext is not retained after
registration and no error message can echo it back.

The canonical error codes and their HTTP status live in
``axiom-specs/contracts/control-api-v1.md``; this module re-uses those codes and
invents none.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from starlette.datastructures import Headers

from axiom_mcp.http import MCP_ENDPOINT_PATH

MCP_AUDIENCE = "axiom-mcp"
CONTROL_AUDIENCE = "axiom-graphd-control"

CAPABILITY_READ = "read"
CAPABILITY_RECONCILE = "reconcile"
CAPABILITY_CHECKPOINT = "checkpoint"

KNOWN_CAPABILITIES: frozenset[str] = frozenset(
    {CAPABILITY_READ, CAPABILITY_RECONCILE, CAPABILITY_CHECKPOINT}
)

# Reconcile and checkpoint reach beyond the read-only plane. Cancelling a job is
# an explicit write, so it travels with the reconcile capability rather than
# inventing a fourth one.
MUTATION_CAPABILITIES: frozenset[str] = frozenset({CAPABILITY_RECONCILE, CAPABILITY_CHECKPOINT})

# The capability a registered MCP tool requires. Tool names come from the
# canonical catalog in repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md section 4; the
# names are repeated here because the transport must decide before the tool layer
# exists, and an unknown tool is treated as a read so the catalog owner still
# answers NOT_FOUND rather than this layer widening access.
TOOL_CAPABILITY: dict[str, str] = {
    "graph_status": CAPABILITY_READ,
    "graph_query": CAPABILITY_READ,
    "graph_version": CAPABILITY_READ,
    "graph_reconcile": CAPABILITY_RECONCILE,
    "graph_job": CAPABILITY_RECONCILE,
    "graph_verify": CAPABILITY_CHECKPOINT,
}

# Canonical code -> HTTP status, from contracts/control-api-v1.md.
ERROR_STATUS: dict[str, int] = {
    "VALIDATION_ERROR": 400,
    "UNAUTHENTICATED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "CONFLICT": 409,
    "INCOMPATIBLE_INPUT": 422,
    "RATE_LIMITED": 429,
    "NOT_READY": 503,
}

DEFAULT_MAX_BODY_BYTES = 262144

__all__ = [
    "CAPABILITY_CHECKPOINT",
    "CAPABILITY_READ",
    "CAPABILITY_RECONCILE",
    "CONTROL_AUDIENCE",
    "DEFAULT_MAX_BODY_BYTES",
    "ERROR_STATUS",
    "KNOWN_CAPABILITIES",
    "MCP_AUDIENCE",
    "MUTATION_CAPABILITIES",
    "TOOL_CAPABILITY",
    "EnvTokenReference",
    "FileTokenReference",
    "SecurityError",
    "SecurityMiddleware",
    "SecurityPolicy",
    "ScopedToken",
    "TokenRegistry",
    "call_scope",
    "host_allowed",
    "install_security",
    "loopback_policy",
    "normalize_host_header",
    "normalize_origin",
    "origin_allowed",
    "parse_authorization",
    "required_capability",
    "token_reference_from_config",
]


class SecurityError(RuntimeError):
    """A policy or authorization failure carrying a canonical code."""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if code not in ERROR_STATUS:
            raise ValueError(f"unregistered security error code: {code}")
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = dict(details or {})
        super().__init__(f"{code}:{message}" if message else code)

    @property
    def status(self) -> int:
        return ERROR_STATUS[self.code]

    def as_dict(self, *, request_id: str | None = None) -> dict[str, Any]:
        """Render the canonical error envelope. Never carries a token value."""
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": dict(self.details),
            "request_id": request_id if request_id is not None else uuid.uuid4().hex,
        }


def _has_wildcard(value: str) -> bool:
    return "*" in value


def normalize_host_header(value: str) -> str:
    """Normalise a ``Host`` header to ``host[:port]`` lower case.

    Returns an empty string for a value that cannot be a plain host header, so a
    caller can never smuggle userinfo, a path or a second authority component
    into the comparison.
    """
    candidate = value.strip().lower()
    if not candidate:
        return ""
    if candidate.startswith("["):
        end = candidate.find("]")
        if end == -1:
            return ""
        host = candidate[: end + 1]
        rest = candidate[end + 1 :]
        if not rest:
            return host
        if rest.startswith(":") and rest[1:].isdigit():
            return f"{host}:{rest[1:]}"
        return ""
    if any(char in candidate for char in "@/\\?#"):
        return ""
    if candidate.count(":") > 1:
        return ""
    host, _, port = candidate.partition(":")
    if not host:
        return ""
    if port and not port.isdigit():
        return ""
    return candidate


def _split_host_port(value: str) -> tuple[str, str]:
    if value.startswith("["):
        end = value.find("]")
        host = value[: end + 1]
        rest = value[end + 1 :]
        port = rest[1:] if rest.startswith(":") else ""
        return host, port
    if ":" in value:
        host, _, port = value.partition(":")
        return host, port
    return value, ""


def host_allowed(host_header: str, allowed_hosts: Sequence[str] | frozenset[str]) -> bool:
    """Return True when the ``Host`` header matches the allowlist.

    An allowlist entry without a port admits any port on that host; an entry with
    a port admits only that port. A missing or malformed header is never allowed,
    and a wildcard entry is ignored rather than honoured.
    """
    normalized = normalize_host_header(host_header)
    if not normalized:
        return False
    host, port = _split_host_port(normalized)
    for entry in allowed_hosts:
        candidate = str(entry).strip().lower()
        if not candidate or _has_wildcard(candidate):
            continue
        entry_norm = normalize_host_header(candidate)
        if not entry_norm:
            continue
        entry_host, entry_port = _split_host_port(entry_norm)
        if entry_host != host:
            continue
        if not entry_port or entry_port == port:
            return True
    return False


def normalize_origin(value: str) -> str:
    """Normalise an ``Origin`` header to ``scheme://host[:port]`` lower case.

    Returns an empty string for ``null``, for a non-HTTP scheme, for a value
    carrying a path, query, fragment or userinfo, and for any wildcard.
    """
    candidate = value.strip().lower()
    if not candidate or candidate == "null" or _has_wildcard(candidate):
        return ""
    scheme, separator, rest = candidate.partition("://")
    if not separator or scheme not in {"http", "https"}:
        return ""
    if not rest or any(char in rest for char in "/?#@"):
        return ""
    host, port = _split_host_port(rest)
    if not host:
        return ""
    if port and not port.isdigit():
        return ""
    return f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"


def origin_allowed(origin: str | None, allowed_origins: Sequence[str] | frozenset[str]) -> bool:
    """Return True when the ``Origin`` header is absent or on the allowlist.

    An absent origin is permitted because non-browser clients do not send one;
    a present origin must match an allowlist entry exactly, so a second port or a
    lookalike host is refused instead of being folded into the match.
    """
    if origin is None or not origin.strip():
        return True
    normalized = normalize_origin(origin)
    if not normalized:
        return False
    for entry in allowed_origins:
        candidate = normalize_origin(str(entry))
        if candidate and candidate == normalized:
            return True
    return False


@dataclass(frozen=True)
class SecurityPolicy:
    """Host and Origin allowlists for one gateway instance.

    Both lists are required. ``build`` refuses an empty list and refuses an entry
    containing ``*``, so a caller cannot widen the policy by accident and this
    module never invents a permissive default.
    """

    allowed_hosts: frozenset[str]
    allowed_origins: frozenset[str]

    def __post_init__(self) -> None:
        if not self.allowed_hosts:
            raise SecurityError("VALIDATION_ERROR", "host allowlist must not be empty")
        if not self.allowed_origins:
            raise SecurityError("VALIDATION_ERROR", "origin allowlist must not be empty")
        for host in self.allowed_hosts:
            if _has_wildcard(host) or not normalize_host_header(host):
                raise SecurityError("VALIDATION_ERROR", "host allowlist entry is not a plain host")
        for origin in self.allowed_origins:
            if _has_wildcard(origin) or not normalize_origin(origin):
                raise SecurityError("VALIDATION_ERROR", "origin allowlist entry is not an origin")

    @classmethod
    def build(
        cls,
        *,
        allowed_hosts: Sequence[str],
        allowed_origins: Sequence[str],
    ) -> SecurityPolicy:
        hosts = frozenset(str(h).strip().lower() for h in allowed_hosts if str(h).strip())
        origins = frozenset(str(o).strip().lower() for o in allowed_origins if str(o).strip())
        return cls(allowed_hosts=hosts, allowed_origins=origins)

    def allows(self, host_header: str | None, origin: str | None) -> bool:
        return host_allowed(host_header or "", self.allowed_hosts) and origin_allowed(
            origin, self.allowed_origins
        )


def loopback_policy(
    port: int,
    *,
    allowed_hosts: Sequence[str] | None = None,
    allowed_origins: Sequence[str] | None = None,
) -> SecurityPolicy:
    """Build the explicit loopback allowlist for ``port``.

    The loopback interface is named rather than pattern-matched and the port is
    pinned, so the policy still rejects a lookalike host, a loopback host on
    another port and an origin on another port.
    """
    hosts = (
        list(allowed_hosts)
        if allowed_hosts is not None
        else [f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"]
    )
    origins = (
        list(allowed_origins)
        if allowed_origins is not None
        else [
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
            f"http://[::1]:{port}",
        ]
    )
    return SecurityPolicy.build(allowed_hosts=hosts, allowed_origins=origins)


@dataclass(frozen=True)
class ScopedToken:
    """One authenticated principal, scoped to solutions and capabilities."""

    token_id: str
    audience: str
    capabilities: frozenset[str]
    solution_ids: frozenset[str]
    project_ids: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if not self.token_id.strip():
            raise SecurityError("VALIDATION_ERROR", "token_id must not be empty")
        if not self.audience.strip():
            raise SecurityError("VALIDATION_ERROR", "token audience must not be empty")
        if not self.capabilities:
            raise SecurityError("VALIDATION_ERROR", "token must carry at least one capability")
        unknown = self.capabilities - KNOWN_CAPABILITIES
        if unknown:
            raise SecurityError(
                "VALIDATION_ERROR",
                "token carries an unregistered capability",
                details={"unknown_capabilities": sorted(unknown)},
            )
        if not self.solution_ids or any(not s.strip() for s in self.solution_ids):
            raise SecurityError("VALIDATION_ERROR", "token must be scoped to at least one solution")
        if self.project_ids is not None and any(not p.strip() for p in self.project_ids):
            raise SecurityError("VALIDATION_ERROR", "project scope entries must not be empty")

    @property
    def read_only(self) -> bool:
        return not (self.capabilities & MUTATION_CAPABILITIES)

    def allows(
        self,
        capability: str,
        *,
        solution_id: str | None = None,
        project_id: str | None = None,
    ) -> bool:
        """Return True only when capability *and* scope both admit the request."""
        if capability not in self.capabilities:
            return False
        if solution_id is not None and solution_id not in self.solution_ids:
            return False
        if project_id is not None and self.project_ids is not None:
            return project_id in self.project_ids
        return True


@dataclass(frozen=True)
class EnvTokenReference:
    """A token held in an environment variable, never in portable config."""

    variable: str
    kind: str = field(default="env", init=False)

    def __post_init__(self) -> None:
        if not self.variable.strip():
            raise SecurityError("VALIDATION_ERROR", "token reference variable must not be empty")

    def resolve(self, env: Mapping[str, str] | None = None) -> str | None:
        source = os.environ if env is None else env
        value = source.get(self.variable)
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


@dataclass(frozen=True)
class FileTokenReference:
    """A token held in an owner-only credential file."""

    path: Path
    kind: str = field(default="file", init=False)

    def __post_init__(self) -> None:
        if not str(self.path).strip():
            raise SecurityError("VALIDATION_ERROR", "token reference path must not be empty")

    def resolve(self, env: Mapping[str, str] | None = None) -> str | None:
        del env
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return None
        lines = text.splitlines()
        first = lines[0].strip() if lines else ""
        return first or None


TokenReference = EnvTokenReference | FileTokenReference


def token_reference_from_config(config: Mapping[str, Any]) -> TokenReference:
    """Build a reference from configuration, refusing a literal token value."""
    for forbidden in ("token", "value", "secret"):
        if forbidden in config:
            raise SecurityError(
                "VALIDATION_ERROR",
                "configuration must carry a token reference, not a token value",
            )
    kind = str(config.get("kind", "")).strip().lower()
    if kind == "env":
        return EnvTokenReference(str(config.get("variable", "")))
    if kind == "file":
        return FileTokenReference(Path(str(config.get("path", ""))))
    raise SecurityError("VALIDATION_ERROR", "token reference kind must be 'env' or 'file'")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class TokenRegistry:
    """Resolve token references into principals and answer authorization.

    The registry is bound to one audience. A token minted for another audience is
    not registered here, so presenting a graphd control credential to the MCP
    endpoint fails as unauthenticated rather than borrowing daemon authority.
    """

    def __init__(self, *, expected_audience: str = MCP_AUDIENCE) -> None:
        if not expected_audience.strip():
            raise SecurityError("VALIDATION_ERROR", "registry audience must not be empty")
        self._expected_audience = expected_audience
        self._token_ids_by_digest: dict[str, str] = {}
        self._tokens_by_id: dict[str, ScopedToken] = {}

    @property
    def expected_audience(self) -> str:
        return self._expected_audience

    @property
    def size(self) -> int:
        return len(self._tokens_by_id)

    def register(
        self,
        reference: TokenReference,
        scope: ScopedToken,
        *,
        env: Mapping[str, str] | None = None,
    ) -> str:
        """Resolve ``reference`` and register ``scope`` under its digest."""
        if scope.audience != self._expected_audience:
            raise SecurityError(
                "VALIDATION_ERROR",
                "registered token audience does not match the registry audience",
            )
        value = reference.resolve(env)
        if not value:
            raise SecurityError("UNAUTHENTICATED", "token reference did not resolve to a value")
        digest = _digest(value)
        if digest in self._token_ids_by_digest:
            raise SecurityError("VALIDATION_ERROR", "token reference is already registered")
        self._token_ids_by_digest[digest] = scope.token_id
        self._tokens_by_id[scope.token_id] = scope
        return scope.token_id

    def authenticate(self, presented: str | None) -> ScopedToken:
        """Return the principal for ``presented`` or raise UNAUTHENTICATED."""
        if presented is None or not presented.strip():
            raise SecurityError("UNAUTHENTICATED", "a bearer token is required")
        token_id = self._token_ids_by_digest.get(_digest(presented.strip()))
        if token_id is None:
            raise SecurityError("UNAUTHENTICATED", "the presented bearer token is not recognised")
        return self._tokens_by_id[token_id]

    def authorize(
        self,
        presented: str | None,
        capability: str,
        *,
        solution_id: str | None = None,
        project_id: str | None = None,
    ) -> ScopedToken:
        """Authenticate, then require ``capability`` inside the token's scope."""
        token = self.authenticate(presented)
        if not token.allows(capability, solution_id=solution_id, project_id=project_id):
            raise SecurityError("FORBIDDEN", "the bearer token does not permit this capability")
        return token

    def authorize_headers(
        self,
        headers: Mapping[str, str],
        capability: str,
        *,
        solution_id: str | None = None,
        project_id: str | None = None,
    ) -> ScopedToken:
        return self.authorize(
            parse_authorization(headers),
            capability,
            solution_id=solution_id,
            project_id=project_id,
        )


def parse_authorization(headers: Mapping[str, str]) -> str | None:
    """Return the bearer credential, or None when no header is present.

    A malformed scheme raises UNAUTHENTICATED without echoing the presented
    value, so a bad credential cannot be reflected into a log or a response.
    """
    raw: str | None = None
    for key, value in headers.items():
        if key.lower() == "authorization":
            raw = value
            break
    if raw is None:
        return None
    scheme, _, credentials = raw.partition(" ")
    if scheme.strip().lower() != "bearer" or not credentials.strip():
        raise SecurityError("UNAUTHENTICATED", "authorization must use the Bearer scheme")
    return credentials.strip()


def required_capability(payload: Mapping[str, Any]) -> str | None:
    """Return the capability a JSON-RPC request needs, or None for non-calls."""
    if payload.get("method") != "tools/call":
        return None
    params = payload.get("params")
    name = params.get("name") if isinstance(params, Mapping) else None
    if not isinstance(name, str) or not name:
        return None
    return TOOL_CAPABILITY.get(name, CAPABILITY_READ)


def call_scope(payload: Mapping[str, Any]) -> tuple[str | None, tuple[str, ...]]:
    """Extract the declared solution and project scope from a tool call."""
    params = payload.get("params")
    if not isinstance(params, Mapping):
        return None, ()
    arguments = params.get("arguments")
    if not isinstance(arguments, Mapping):
        return None, ()
    solution_id = arguments.get("solution_id")
    projects: list[str] = []
    single = arguments.get("project_id")
    if isinstance(single, str) and single:
        projects.append(single)
    many = arguments.get("project_ids")
    if isinstance(many, Sequence) and not isinstance(many, (str, bytes)):
        projects.extend(str(item) for item in many if str(item))
    return (solution_id if isinstance(solution_id, str) and solution_id else None, tuple(projects))


def _replay_receive(
    chunks: Sequence[bytes],
    receive: Callable[[], Awaitable[dict[str, Any]]],
) -> Callable[[], Awaitable[dict[str, Any]]]:
    buffered = list(chunks)

    async def replay() -> dict[str, Any]:
        if buffered:
            body = buffered.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(buffered)}
        return await receive()

    return replay


async def _buffer_body(
    receive: Callable[[], Awaitable[dict[str, Any]]],
    limit: int,
) -> tuple[bytes | None, Callable[[], Awaitable[dict[str, Any]]]]:
    """Buffer a bounded request body and replay it to the wrapped application."""
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            break
        body = message.get("body") or b""
        chunks.append(body)
        total += len(body)
        if total > limit:
            return None, _replay_receive(chunks, receive)
        if not message.get("more_body", False):
            break
    return b"".join(chunks), _replay_receive(chunks, receive)


def _decode_json(body: bytes) -> Mapping[str, Any] | None:
    if not body.strip():
        return None
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return document if isinstance(document, Mapping) else None


async def _send_error(
    send: Callable[[dict[str, Any]], Awaitable[None]],
    error: SecurityError,
) -> None:
    request_id = uuid.uuid4().hex
    payload = json.dumps(error.as_dict(request_id=request_id)).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": error.status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
                (b"x-request-id", request_id.encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class SecurityMiddleware:
    """Pure ASGI policy middleware.

    It is deliberately not ``BaseHTTPMiddleware``: that class buffers the whole
    response, which would stall the MCP endpoint's ``text/event-stream`` frames.
    This middleware reads headers from the scope, buffers only a bounded request
    body so a JSON-RPC call can be classified, and streams the response straight
    through.
    """

    def __init__(
        self,
        app: Any,
        *,
        policy: SecurityPolicy,
        authenticator: TokenRegistry | None = None,
        protected_path: str = MCP_ENDPOINT_PATH,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    ) -> None:
        if not isinstance(policy, SecurityPolicy):
            raise SecurityError("VALIDATION_ERROR", "policy must be a SecurityPolicy")
        if max_body_bytes <= 0:
            raise SecurityError("VALIDATION_ERROR", "max_body_bytes must be positive")
        self.app = app
        self.policy = policy
        self.authenticator = authenticator if authenticator is not None else TokenRegistry()
        self.protected_path = protected_path
        self.max_body_bytes = max_body_bytes

    def _is_protected(self, path: str) -> bool:
        return path == self.protected_path or path.startswith(self.protected_path.rstrip("/") + "/")

    def _check_host_origin(self, headers: Headers) -> SecurityError | None:
        if not host_allowed(headers.get("host") or "", self.policy.allowed_hosts):
            return SecurityError("FORBIDDEN", "the Host header is not on the transport allowlist")
        if not origin_allowed(headers.get("origin"), self.policy.allowed_origins):
            return SecurityError("FORBIDDEN", "the Origin header is not on the transport allowlist")
        return None

    async def _authorize(
        self,
        scope: Mapping[str, Any],
        headers: Headers,
        receive: Callable[[], Awaitable[dict[str, Any]]],
    ) -> tuple[SecurityError | None, Callable[[], Awaitable[dict[str, Any]]]]:
        try:
            presented = parse_authorization(headers)
            token = self.authenticator.authenticate(presented)
        except SecurityError as error:
            return error, receive
        method = str(scope.get("method") or "GET").upper()
        if method not in {"POST", "PUT", "PATCH"}:
            return None, receive
        body, replay = await _buffer_body(receive, self.max_body_bytes)
        if body is None:
            problem = SecurityError("VALIDATION_ERROR", "request body exceeds the configured bound")
            return problem, replay
        payload = _decode_json(body)
        if payload is None:
            return None, replay
        capability = required_capability(payload)
        if capability is None:
            return None, replay
        solution_id, project_ids = call_scope(payload)
        if solution_id is not None and solution_id not in token.solution_ids:
            problem = SecurityError("FORBIDDEN", "the bearer token is not scoped to this solution")
            return problem, replay
        if token.project_ids is not None:
            for project_id in project_ids:
                if project_id not in token.project_ids:
                    problem = SecurityError(
                        "FORBIDDEN", "the bearer token is not scoped to this project"
                    )
                    return problem, replay
        if capability not in token.capabilities:
            problem = SecurityError("FORBIDDEN", "the bearer token lacks the required capability")
            return problem, replay
        return None, replay

    async def __call__(
        self,
        scope: Mapping[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        problem = self._check_host_origin(headers)
        if problem is None and self._is_protected(str(scope.get("path") or "")):
            problem, receive = await self._authorize(scope, headers, receive)
        if problem is not None:
            await _send_error(send, problem)
            return
        await self.app(scope, receive, send)


def install_security(
    app: Any,
    *,
    policy: SecurityPolicy,
    authenticator: TokenRegistry | None = None,
    protected_path: str = MCP_ENDPOINT_PATH,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> Any:
    """Install :class:`SecurityMiddleware` on ``app`` and return ``app``."""
    app.add_middleware(
        SecurityMiddleware,
        policy=policy,
        authenticator=authenticator,
        protected_path=protected_path,
        max_body_bytes=max_body_bytes,
    )
    return app
