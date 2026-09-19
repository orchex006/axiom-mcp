"""C-034: the concrete graphd control client.

The delegation tools talk to the daemon through the :class:`~axiom_mcp.tools.context.ControlPlane`
protocol. This module is the production implementation of that protocol: a bounded synchronous
HTTP client for ``contracts/control-api-v1.md``.

Three properties are load-bearing and are enforced here rather than documented:

* **The audience is preserved.** The daemon credential and the MCP credential are different
  audiences (``security.CONTROL_AUDIENCE`` vs ``security.MCP_AUDIENCE``). The client refuses a
  token minted for any other audience before it opens a socket, so an MCP token can never be
  replayed to the daemon and a control credential can never be used to answer an MCP call.
* **Retries are bounded.** Only ``429`` and ``503`` (and transport failures) are retried, the
  attempt budget is capped at :data:`MAX_ATTEMPTS_LIMIT`, and the backoff is capped at
  :data:`MAX_BACKOFF_S`; a ``Retry-After`` header is honoured but clamped. A non-retryable 4xx
  is raised on the first response, so a bad request is never amplified into a retry storm.
* **An outage is a first-class answer, not a crash.** A transport failure becomes a retryable
  ``DAEMON_UNAVAILABLE``; ``graph_reconcile``/``graph_job``/``graph_verify`` surface it, while a
  read stays served from the pinned generation (the ``allow_stale`` default), which is the
  degraded reader service AC1 requires.

The control API shapes are the supplement's: ``GET /v1/solutions/{id}/status``,
``POST /v1/solutions/{id}/reconcile``, ``GET /v1/jobs/{id}``, ``POST /v1/jobs/{id}/cancel``.
``POST /v1/solutions/{id}/verify`` follows the reconcile envelope; the supplement freezes the
codes and statuses and leaves the verify route to the G/B OpenAPI deliverable.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from axiom_mcp import security
from axiom_mcp.errors import CANONICAL_CODES, AxiomError

__all__ = [
    "CONTROL_API_RETRY_STATUSES",
    "DEFAULT_BACKOFF_S",
    "DEFAULT_CONTROL_BASE_URL",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_TIMEOUT_S",
    "MAX_ATTEMPTS_LIMIT",
    "MAX_BACKOFF_S",
    "ControlClientSettings",
    "HttpControlClient",
]

#: The daemon control listener default. It is deliberately not the MCP port.
DEFAULT_CONTROL_BASE_URL = "http://127.0.0.1:8766"
DEFAULT_TIMEOUT_S = 5.0
DEFAULT_MAX_ATTEMPTS = 3

#: The ceiling on the retry budget. A caller may ask for fewer attempts, never more.
MAX_ATTEMPTS_LIMIT = 5
DEFAULT_BACKOFF_S = 0.25
MAX_BACKOFF_S = 2.0

#: Only these upstream statuses are worth retrying; every other 4xx is terminal.
CONTROL_API_RETRY_STATUSES = frozenset({429, 503})

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

#: ``HTTP status -> canonical code`` for a daemon that answered without a canonical body.
_STATUS_CODE_FALLBACK: Mapping[int, str] = {
    400: "VALIDATION_ERROR",
    401: "UNAUTHENTICATED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    422: "INCOMPATIBLE_INPUT",
    429: "RATE_LIMITED",
    503: "NOT_READY",
}

_JSON_HEADERS = {"Accept": "application/json"}


def _positive_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and value > 0


@dataclass(frozen=True)
class ControlClientSettings:
    """Where and how the control client talks to the daemon, with hard ceilings.

    ``max_attempts`` is bounded by :data:`MAX_ATTEMPTS_LIMIT` so no configuration can turn the
    client into an unbounded retry loop; a caller that wants a different budget must change the
    constant, which is a reviewed code change rather than a config value.
    """

    base_url: str = DEFAULT_CONTROL_BASE_URL
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff_s: float = DEFAULT_BACKOFF_S
    max_backoff_s: float = MAX_BACKOFF_S

    def __post_init__(self) -> None:
        if not isinstance(self.base_url, str) or not self.base_url.startswith(
            ("http://", "https://")
        ):
            raise AxiomError(
                "VALIDATION_ERROR",
                "control base_url must be an absolute http(s) URL",
                details={"field": "base_url"},
            )
        if not _positive_number(self.timeout_s):
            raise AxiomError(
                "VALIDATION_ERROR",
                "timeout_s must be a positive number",
                details={"field": "timeout_s"},
            )
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts <= MAX_ATTEMPTS_LIMIT
        ):
            raise AxiomError(
                "LIMIT_EXCEEDED",
                f"max_attempts must be between 1 and {MAX_ATTEMPTS_LIMIT}",
                details={"field": "max_attempts", "maximum": MAX_ATTEMPTS_LIMIT},
            )
        if self.backoff_s < 0 or not _positive_number(self.max_backoff_s):
            raise AxiomError(
                "VALIDATION_ERROR",
                "backoff_s must be non-negative and max_backoff_s positive",
                details={"field": "backoff_s"},
            )


class HttpControlClient:
    """A bounded, audience-checked implementation of the ``ControlPlane`` protocol."""

    def __init__(
        self,
        token: security.ScopedToken,
        *,
        settings: ControlClientSettings | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        # The audience boundary is checked before anything else: presenting an MCP or admin
        # credential to the daemon (or the reverse) is refused, not silently attempted.
        if token.audience != security.CONTROL_AUDIENCE:
            raise AxiomError(
                "FORBIDDEN",
                "the control client requires a graphd control-audience credential",
                details={"required_audience": security.CONTROL_AUDIENCE},
            )
        self._token = token
        self._settings = settings if settings is not None else ControlClientSettings()
        self._client = client
        self._sleep = sleep if sleep is not None else time.sleep
        self._owned: httpx.Client | None = None

    @property
    def audience(self) -> str:
        """The audience this client is allowed to present. Always the control audience."""
        return self._token.audience

    @property
    def settings(self) -> ControlClientSettings:
        return self._settings

    def create_transport(self) -> httpx.Client:
        """Build the owned HTTP transport with the configured, bounded timeout."""
        return httpx.Client(timeout=self._settings.timeout_s)

    def close(self) -> None:
        if self._owned is not None:
            self._owned.close()
            self._owned = None

    def status(self, solution_id: str) -> Any:
        solution = self._identifier(solution_id, "solution_id")
        return self._request("GET", f"/v1/solutions/{solution}/status")

    def reconcile(self, request: Mapping[str, Any]) -> Any:
        solution = self._identifier(request.get("solution_id"), "solution_id")
        body = {
            key: request[key]
            for key in ("scope", "project_ids", "reason", "wait_timeout_ms")
            if key in request
        }
        return self._request("POST", f"/v1/solutions/{solution}/reconcile", body=body)

    def job(self, job_id: str, *, action: str, wait_ms: int = 0) -> Any:
        job = self._identifier(job_id, "job_id")
        if action == "cancel":
            return self._request("POST", f"/v1/jobs/{job}/cancel")
        return self._request("GET", f"/v1/jobs/{job}", params={"wait_ms": wait_ms})

    def verify(self, request: Mapping[str, Any]) -> Any:
        solution = self._identifier(request.get("solution_id"), "solution_id")
        body = {key: value for key, value in request.items() if key != "solution_id"}
        return self._request("POST", f"/v1/solutions/{solution}/verify", body=body)

    # -- internals ---------------------------------------------------------------------------

    def _identifier(self, value: Any, field: str) -> str:
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            raise AxiomError(
                "VALIDATION_ERROR",
                f"{field} must be a bounded identifier",
                details={"field": field},
            )
        return quote(value, safe="")

    def _transport(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        if self._owned is None:
            self._owned = self.create_transport()
        return self._owned

    def _headers(self) -> dict[str, str]:
        return {**_JSON_HEADERS, "Authorization": f"Bearer {self._token.token_id}"}

    def _delay(self, response: httpx.Response | None, attempt: int) -> float:
        if response is not None:
            header = (response.headers.get("retry-after") or "").strip()
            if header.isdigit():
                return min(float(header), self._settings.max_backoff_s)
        raw = self._settings.backoff_s * (2 ** (attempt - 1))
        return min(raw, self._settings.max_backoff_s)

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self._settings.base_url.rstrip("/") + path
        headers = self._headers()
        last: AxiomError | None = None
        attempts = 0
        while attempts < self._settings.max_attempts:
            attempts += 1
            try:
                response = self._transport().request(
                    method, url, headers=headers, json=body, params=params
                )
            except httpx.HTTPError as exc:
                last = AxiomError(
                    "DAEMON_UNAVAILABLE",
                    "the graphd control plane is unreachable",
                    retryable=True,
                    cause=exc,
                )
            else:
                if response.status_code < 400:
                    return self._payload(response)
                error = self._error_from(response)
                if (
                    response.status_code in CONTROL_API_RETRY_STATUSES
                    and attempts < self._settings.max_attempts
                ):
                    self._sleep(self._delay(response, attempts))
                    last = error
                    continue
                raise error
            if attempts < self._settings.max_attempts:
                self._sleep(self._delay(None, attempts))
        assert last is not None  # the loop always assigns before it can be exhausted
        raise last

    def _error_from(self, response: httpx.Response) -> AxiomError:
        document: Any = None
        try:
            document = response.json()
        except ValueError:
            document = None
        code: str | None = None
        details: dict[str, Any] = {}
        if isinstance(document, Mapping):
            candidate = document.get("code")
            if isinstance(candidate, str) and candidate in CANONICAL_CODES:
                code = candidate
            raw_details = document.get("details")
            if isinstance(raw_details, Mapping):
                details = dict(raw_details)
        if code is None:
            code = _STATUS_CODE_FALLBACK.get(response.status_code, "DAEMON_UNAVAILABLE")
        return AxiomError(
            code,
            f"the graphd control plane refused the request with HTTP {response.status_code}",
            details=details,
        )

    def _payload(self, response: httpx.Response) -> dict[str, Any]:
        try:
            document = response.json()
        except ValueError as exc:
            raise AxiomError(
                "DAEMON_UNAVAILABLE",
                "the graphd control plane answered with a non-JSON body",
                cause=exc,
            ) from exc
        if not isinstance(document, Mapping):
            raise AxiomError(
                "DAEMON_UNAVAILABLE",
                "the graphd control plane answered with a non-object payload",
            )
        return dict(document)
