"""C-034 regression tests: the concrete graphd control client.

The daemon is not in this repository, so every test drives the client through
``httpx.MockTransport`` - no socket is opened - and asserts the *wire* the client produced: the
URL, the verb, the bounded body, and the credential's ``Authorization`` header.

The cases that carry the task are:

* :func:`test_the_control_audience_is_the_only_one_that_travels` and
  :func:`test_an_mcp_audience_credential_is_refused_before_the_wire` - the audience boundary;
* :func:`test_a_transient_503_is_retried_inside_the_budget` and
  :func:`test_a_non_retryable_4xx_is_not_retried` - bounded retries with no 4xx amplification;
* :func:`test_a_daemon_outage_leaves_the_reader_serving_stale_snapshots` - AC1's second half,
  proven against the real registry, guard and query engine: an outage is a retryable
  ``DAEMON_UNAVAILABLE`` for the mutation path while a read still answers from the pinned
  generation.

The negative and boundary legs cover a path-shaped identifier, an unknown error status, a
non-JSON answer, an unbounded retry budget, an unlimited/zero timeout, a ``Retry-After`` beyond
its clamp, and a transport timeout.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from axiom_mcp import control_client as ctrl
from axiom_mcp import security
from axiom_mcp.errors import AxiomError
from axiom_mcp.query.envelope import FRESHNESS_UNKNOWN
from axiom_mcp.tools.query import graph_query
from axiom_mcp.tools.reconcile import graph_reconcile
from tests.test_tools_support import AUTH_API, SOLUTION, context_for

CONNECT_ERROR = "connection refused"


def control_token(**overrides) -> security.ScopedToken:
    """A credential minted for the daemon audience, which is the only one this client accepts."""
    body: dict[str, Any] = {
        "token_id": "control-credential-1",
        "audience": security.CONTROL_AUDIENCE,
        "capabilities": frozenset({security.CAPABILITY_RECONCILE}),
        "solution_ids": frozenset({SOLUTION}),
    }
    body.update(overrides)
    return security.ScopedToken(**body)


@dataclass
class Wire:
    """A scripted upstream: the last item repeats once the script runs out."""

    items: list[Any]
    calls: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        item = self.items[min(len(self.calls) - 1, len(self.items) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def count(self) -> int:
        return len(self.calls)


def client_for(wire: Wire, **settings_kwargs: Any):
    """Build a real client over the scripted wire, with sleeps recorded instead of taken."""
    settings = ctrl.ControlClientSettings(**settings_kwargs) if settings_kwargs else None
    waits: list[float] = []
    transport = httpx.MockTransport(wire.handler)
    http = httpx.Client(transport=transport)
    client = ctrl.HttpControlClient(
        control_token(), settings=settings, client=http, sleep=waits.append
    )
    return client, waits, wire


def refusing(callable_, code: str) -> AxiomError:
    with pytest.raises(AxiomError) as caught:
        callable_()
    assert caught.value.code == code
    return caught.value


def test_status_reads_the_solution_status_endpoint():
    wire = Wire([httpx.Response(200, json={"solution_id": SOLUTION, "fresh": True})])
    client, waits, wire = client_for(wire)
    assert client.status(SOLUTION) == {"solution_id": SOLUTION, "fresh": True}
    assert wire.calls[0].method == "GET"
    assert wire.calls[0].url.path == f"/v1/solutions/{SOLUTION}/status"
    assert waits == []


def test_reconcile_posts_only_the_closed_body():
    wire = Wire([httpx.Response(202, json={"job_id": "job-1", "state": "queued"})])
    client, _, wire = client_for(wire)
    client.reconcile(
        {
            "solution_id": SOLUTION,
            "scope": "project",
            "project_ids": [AUTH_API],
            "reason": "agent-checkpoint",
            "wait_timeout_ms": 10,
            "not_a_control_field": "dropped",
        }
    )
    assert wire.calls[0].method == "POST"
    assert wire.calls[0].url.path == f"/v1/solutions/{SOLUTION}/reconcile"
    assert json.loads(wire.calls[0].content) == {
        "scope": "project",
        "project_ids": [AUTH_API],
        "reason": "agent-checkpoint",
        "wait_timeout_ms": 10,
    }


def test_job_status_and_cancel_use_distinct_verbs_and_paths():
    wire = Wire([httpx.Response(200, json={"job_id": "job-1", "state": "RUNNING"})])
    client, _, wire = client_for(wire)
    client.job("job-1", action="status", wait_ms=250)
    client.job("job-1", action="cancel")
    assert wire.calls[0].method == "GET"
    assert wire.calls[0].url.path == "/v1/jobs/job-1"
    assert wire.calls[0].url.params["wait_ms"] == "250"
    assert wire.calls[1].method == "POST"
    assert wire.calls[1].url.path == "/v1/jobs/job-1/cancel"


def test_verify_posts_to_the_verify_route():
    wire = Wire([httpx.Response(202, json={"job_id": "verify-1"})])
    client, _, wire = client_for(wire)
    client.verify({"solution_id": SOLUTION, "project_ids": [AUTH_API], "target_event_seq": 4})
    assert wire.calls[0].method == "POST"
    assert wire.calls[0].url.path == f"/v1/solutions/{SOLUTION}/verify"
    assert json.loads(wire.calls[0].content) == {"project_ids": [AUTH_API], "target_event_seq": 4}


def test_the_control_audience_is_the_only_one_that_travels():
    client, _, wire = client_for(Wire([httpx.Response(200, json={})]))
    client.status(SOLUTION)
    assert client.audience == security.CONTROL_AUDIENCE
    assert client.audience != security.MCP_AUDIENCE
    assert wire.calls[0].headers["authorization"] == "Bearer control-credential-1"


def test_an_mcp_audience_credential_is_refused_before_the_wire():
    wire = Wire([httpx.Response(200, json={})])
    transport = httpx.MockTransport(wire.handler)
    problem = refusing(
        lambda: ctrl.HttpControlClient(
            control_token(audience=security.MCP_AUDIENCE), client=httpx.Client(transport=transport)
        ),
        "FORBIDDEN",
    )
    assert problem.details["required_audience"] == security.CONTROL_AUDIENCE
    assert wire.count == 0


def test_a_transient_503_is_retried_inside_the_budget():
    wire = Wire(
        [
            httpx.Response(503, json={"code": "NOT_READY"}),
            httpx.Response(503, json={"code": "NOT_READY"}),
            httpx.Response(200, json={"fresh": True}),
        ]
    )
    client, waits, wire = client_for(wire)
    assert client.status(SOLUTION) == {"fresh": True}
    assert wire.count == 3
    assert waits == [0.25, 0.5]


def test_a_permanent_outage_stops_at_the_budget():
    client, waits, wire = client_for(Wire([httpx.Response(503, json={"code": "NOT_READY"})]))
    problem = refusing(lambda: client.status(SOLUTION), "NOT_READY")
    assert problem.retryable is True
    assert wire.count == 3
    assert waits == [0.25, 0.5]


def test_rate_limiting_is_relayed_and_bounded():
    client, waits, wire = client_for(
        Wire([httpx.Response(429, json={"code": "RATE_LIMITED"})]), max_attempts=2
    )
    problem = refusing(lambda: client.status(SOLUTION), "RATE_LIMITED")
    assert problem.retryable is True
    assert wire.count == 2
    assert waits == [0.25]


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "VALIDATION_ERROR"),
        (401, "UNAUTHENTICATED"),
        (403, "FORBIDDEN"),
        (404, "NOT_FOUND"),
        (409, "CONFLICT"),
        (422, "INCOMPATIBLE_INPUT"),
    ],
)
def test_a_non_retryable_4xx_is_not_retried(status: int, code: str):
    client, waits, wire = client_for(Wire([httpx.Response(status, json={})]), max_attempts=5)
    refusing(lambda: client.status(SOLUTION), code)
    assert wire.count == 1, "a terminal 4xx must not be amplified into retries"
    assert waits == []


def test_the_daemons_canonical_code_wins_over_the_status_fallback():
    wire = Wire([httpx.Response(403, json={"code": "DAEMON_UNAVAILABLE", "message": "busy"})])
    client, _, _ = client_for(wire)
    refusing(lambda: client.status(SOLUTION), "DAEMON_UNAVAILABLE")


def test_a_connection_failure_is_a_retryable_outage():
    client, waits, wire = client_for(Wire([httpx.ConnectError(CONNECT_ERROR)]))
    problem = refusing(lambda: client.status(SOLUTION), "DAEMON_UNAVAILABLE")
    assert problem.retryable is True
    assert problem.cause_type == "ConnectError"
    assert wire.count == 3, "an outage is retried, but only inside the budget"
    assert waits == [0.25, 0.5]


def test_a_transport_timeout_is_an_outage_not_a_crash():
    client, waits, wire = client_for(Wire([httpx.ReadTimeout("slow")]), max_attempts=1)
    problem = refusing(lambda: client.status(SOLUTION), "DAEMON_UNAVAILABLE")
    assert problem.retryable is True
    assert wire.count == 1
    assert waits == []


@pytest.mark.parametrize("attempts", [0, ctrl.MAX_ATTEMPTS_LIMIT + 1])
def test_the_retry_budget_has_a_hard_ceiling(attempts: int):
    problem = refusing(lambda: ctrl.ControlClientSettings(max_attempts=attempts), "LIMIT_EXCEEDED")
    assert problem.details["maximum"] == ctrl.MAX_ATTEMPTS_LIMIT


def test_the_timeout_is_bounded_and_reaches_the_transport():
    client = ctrl.HttpControlClient(
        control_token(), settings=ctrl.ControlClientSettings(timeout_s=2.5)
    )
    transport = client.create_transport()
    try:
        assert transport.timeout.read == 2.5
    finally:
        transport.close()
    refusing(lambda: ctrl.ControlClientSettings(timeout_s=0), "VALIDATION_ERROR")


def test_retry_after_is_honoured_but_clamped():
    wire = Wire([httpx.Response(503, json={}, headers={"retry-after": "999"})])
    client, waits, _ = client_for(wire, max_attempts=2)
    refusing(lambda: client.status(SOLUTION), "NOT_READY")
    assert waits == [client.settings.max_backoff_s]


def test_a_path_shaped_identifier_never_reaches_the_wire():
    client, _, wire = client_for(Wire([httpx.Response(200, json={})]))
    for bad in ("../etc/passwd", "", "auth-api/../other", "x" * 129, 7):
        refusing(lambda bad=bad: client.status(bad), "VALIDATION_ERROR")
    assert wire.count == 0


def test_a_non_json_answer_is_a_daemon_defect_not_a_retry():
    wire = Wire([httpx.Response(200, text="not json", headers={"content-type": "text/plain"})])
    client, _, wire = client_for(wire)
    refusing(lambda: client.status(SOLUTION), "DAEMON_UNAVAILABLE")
    assert wire.count == 1


def test_an_unknown_error_status_becomes_daemon_unavailable():
    client, _, wire = client_for(Wire([httpx.Response(500, text="boom")]))
    refusing(lambda: client.status(SOLUTION), "DAEMON_UNAVAILABLE")
    assert wire.count == 1


def test_a_daemon_outage_leaves_the_reader_serving_stale_snapshots(tmp_path):
    """AC1: the mutation path reports the outage while a read keeps its pinned generation."""
    control, _, wire = client_for(Wire([httpx.ConnectError(CONNECT_ERROR)]))
    context = context_for(tmp_path, control=control)

    refusing(
        lambda: graph_reconcile({"solution_id": SOLUTION, "reason": "outage"}, context),
        "DAEMON_UNAVAILABLE",
    )
    assert wire.count == 3

    answer = graph_query(
        {"solution_id": SOLUTION, "operation": "search", "query": "login"}, context
    )
    assert answer["nodes"], "a degraded reader still answers from the pinned generation"
    assert answer["freshness"] == FRESHNESS_UNKNOWN
