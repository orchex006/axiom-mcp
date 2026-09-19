"""C-030 regression tests: ``graph_reconcile``.

The positive cases run the real registry with a recording control plane, because the component
under test *is* the delegation boundary: the daemon is not in this repository, so the honest way
to test the tool is to assert exactly what it asked the daemon for and what it did *not* do
itself. The cases that carry the task are:

* :func:`test_an_authorized_request_enqueues_and_returns_a_job` - the daemon is asked for a
  reconcile and its job handle comes back through a closed projection;
* :func:`test_the_tool_reads_no_snapshot_and_writes_nothing` - a source spy that raises on any
  read is never touched, which is the executable form of AC1's "Python never parses source or
  writes graph shards";
* :func:`test_a_read_only_token_is_forbidden_before_the_daemon` - a read token is refused
  before the control plane is consulted at all.

The negative and boundary cases cover an unexpected field, a missing or over-long reason, a
bounded ``wait_timeout_ms``, ``scope=project`` without project ids, a project selector with
``scope=dirty``, an unknown project, a project outside the token's scope, an invisible solution,
a daemon that answers an unusable payload, an unknown job state, and an unreachable daemon.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from axiom_mcp import security
from axiom_mcp.errors import AxiomError
from axiom_mcp.tools.reconcile import (
    RECONCILE_FIELDS,
    graph_reconcile,
)
from tests.test_tools_support import (
    AUTH_API,
    AUTH_API_GENERATION,
    SOLUTION,
    WEB_APP,
    FakeControl,
    context_for,
)


@dataclass
class RecordingSource:
    """A snapshot source that fails loudly if a delegation tool tries to read one."""

    calls: list[str] = field(default_factory=list)

    def load(self, location: Any) -> Any:
        self.calls.append(str(location))
        raise AssertionError("a delegation tool must not read a snapshot")


def call(context, **arguments):
    return graph_reconcile(dict(arguments), context)


def refusing(context, code, **arguments):
    with pytest.raises(AxiomError) as caught:
        graph_reconcile(dict(arguments), context)
    assert caught.value.code == code
    return caught.value


def test_an_authorized_request_enqueues_and_returns_a_job(tmp_path: Path) -> None:
    control = FakeControl(
        reconcile_result={
            "job_id": "job-0007",
            "state": "PENDING",
            "target_event_seq": 12,
            "retry_after_ms": 250,
            "queue_depth": 9,
        }
    )
    context = context_for(tmp_path, control=control)
    answer = call(
        context,
        solution_id=SOLUTION,
        scope="project",
        project_ids=[AUTH_API],
        reason="agent-checkpoint",
        wait_timeout_ms=1500,
    )
    assert tuple(answer) == RECONCILE_FIELDS
    assert answer["job"] == {
        "job_id": "job-0007",
        "state": "PENDING",
        "target_event_seq": 12,
        "retry_after_ms": 250,
    }
    assert answer["publication"] is None
    assert answer["project_ids"] == [AUTH_API]
    # The daemon's unmodelled key never crosses the boundary.
    assert "queue_depth" not in answer["job"]
    assert control.calls[0].operation == "reconcile"
    assert control.calls[0].payload == {
        "solution_id": SOLUTION,
        "scope": "project",
        "project_ids": [AUTH_API],
        "reason": "agent-checkpoint",
        "wait_timeout_ms": 1500,
    }


def test_the_tool_reads_no_snapshot_and_writes_nothing(tmp_path: Path) -> None:
    control = FakeControl(reconcile_result={"job_id": "job-0008", "state": "RUNNING"})
    spy = RecordingSource()
    context = replace(context_for(tmp_path, control=control), source=spy)
    call(context, solution_id=SOLUTION, reason="bounded-checkpoint")
    assert spy.calls == []
    assert [record.operation for record in control.calls] == ["reconcile"]


def test_a_read_only_token_is_forbidden_before_the_daemon(tmp_path: Path) -> None:
    control = FakeControl()
    context = context_for(tmp_path, caps=frozenset({security.CAPABILITY_READ}), control=control)
    error = refusing(context, "FORBIDDEN", solution_id=SOLUTION, reason="no-capability")
    assert error.details["required_capability"] == security.CAPABILITY_RECONCILE
    assert control.calls == []


def test_an_invisible_solution_is_not_found(tmp_path: Path) -> None:
    control = FakeControl()
    context = context_for(tmp_path, solution_ids=frozenset({"other-solution"}), control=control)
    refusing(context, "NOT_FOUND", solution_id=SOLUTION, reason="scoped-away")
    assert control.calls == []


def test_a_missing_control_plane_is_daemon_unavailable(tmp_path: Path) -> None:
    context = context_for(tmp_path, control=None)
    error = refusing(context, "DAEMON_UNAVAILABLE", solution_id=SOLUTION, reason="no-daemon")
    assert error.retryable is True


def test_an_unexpected_field_is_refused(tmp_path: Path) -> None:
    control = FakeControl()
    context = context_for(tmp_path, control=control)
    error = refusing(
        context,
        "VALIDATION_ERROR",
        solution_id=SOLUTION,
        reason="closed-request",
        command="rm -rf /",
    )
    assert error.details["unexpected_fields"] == ["command"]
    assert control.calls == []


def test_a_missing_reason_is_a_validation_error(tmp_path: Path) -> None:
    control = FakeControl()
    context = context_for(tmp_path, control=control)
    refusing(context, "VALIDATION_ERROR", solution_id=SOLUTION)
    assert control.calls == []


def test_an_over_long_reason_is_a_limit_error(tmp_path: Path) -> None:
    control = FakeControl()
    context = context_for(tmp_path, control=control)
    refusing(context, "LIMIT_EXCEEDED", solution_id=SOLUTION, reason="x" * 201)
    assert control.calls == []


@pytest.mark.parametrize("wait_ms", [-1, 30001])
def test_wait_timeout_is_bounded(tmp_path: Path, wait_ms: int) -> None:
    control = FakeControl()
    context = context_for(tmp_path, control=control)
    refusing(
        context, "LIMIT_EXCEEDED", solution_id=SOLUTION, reason="bounded", wait_timeout_ms=wait_ms
    )
    assert control.calls == []


def test_scope_project_requires_explicit_projects(tmp_path: Path) -> None:
    control = FakeControl()
    context = context_for(tmp_path, control=control)
    refusing(context, "VALIDATION_ERROR", solution_id=SOLUTION, scope="project", reason="ambiguous")
    assert control.calls == []


def test_a_project_selector_with_dirty_scope_is_refused(tmp_path: Path) -> None:
    control = FakeControl()
    context = context_for(tmp_path, control=control)
    refusing(
        context,
        "VALIDATION_ERROR",
        solution_id=SOLUTION,
        scope="dirty",
        project_ids=[AUTH_API],
        reason="contradiction",
    )
    assert control.calls == []


def test_an_unknown_scope_is_refused(tmp_path: Path) -> None:
    control = FakeControl()
    context = context_for(tmp_path, control=control)
    refusing(context, "VALIDATION_ERROR", solution_id=SOLUTION, scope="everything", reason="typo")
    assert control.calls == []


def test_an_unknown_project_is_not_found(tmp_path: Path) -> None:
    control = FakeControl()
    context = context_for(tmp_path, control=control)
    refusing(
        context,
        "NOT_FOUND",
        solution_id=SOLUTION,
        scope="project",
        project_id="no-such-project",
        reason="typo",
    )
    assert control.calls == []


def test_a_project_outside_the_token_scope_is_invisible(tmp_path: Path) -> None:
    control = FakeControl()
    context = context_for(tmp_path, project_ids=frozenset({AUTH_API}), control=control)
    refusing(
        context,
        "NOT_FOUND",
        solution_id=SOLUTION,
        scope="project",
        project_ids=[WEB_APP],
        reason="out-of-scope",
    )
    assert control.calls == []


def test_full_scope_reaches_the_daemon(tmp_path: Path) -> None:
    control = FakeControl(reconcile_result={"job_id": "job-0009", "state": "LEASED"})
    context = context_for(tmp_path, control=control)
    answer = call(context, solution_id=SOLUTION, scope="full", reason="owner-request")
    assert answer["scope"] == "full"
    assert answer["project_ids"] == []
    assert control.calls[0].payload["scope"] == "full"
    assert "project_ids" not in control.calls[0].payload


def test_a_publication_answer_is_reported_without_a_job(tmp_path: Path) -> None:
    control = FakeControl(
        reconcile_result={
            "catalog_generation_id": AUTH_API_GENERATION,
            "verification": {"mode": "inventory_hash"},
            "dirty": {AUTH_API: 2, "web-app": "not-a-count"},
        }
    )
    context = context_for(tmp_path, control=control)
    answer = call(context, solution_id=SOLUTION, reason="already-satisfied")
    assert answer["job"] is None
    assert answer["publication"] == {
        "catalog_generation_id": AUTH_API_GENERATION,
        "verification": {"mode": "inventory_hash"},
        "dirty": {AUTH_API: 2},
    }


def test_a_daemon_that_answers_nothing_usable_is_unavailable(tmp_path: Path) -> None:
    control = FakeControl(reconcile_result={})
    context = context_for(tmp_path, control=control)
    refusing(context, "DAEMON_UNAVAILABLE", solution_id=SOLUTION, reason="empty-answer")


def test_a_non_object_daemon_answer_is_unavailable(tmp_path: Path) -> None:
    control = FakeControl(reconcile_result=None)
    context = context_for(tmp_path, control=control)
    refusing(context, "DAEMON_UNAVAILABLE", solution_id=SOLUTION, reason="bad-answer")


def test_an_unknown_job_state_is_refused(tmp_path: Path) -> None:
    control = FakeControl(reconcile_result={"job_id": "job-0010", "state": "FROBNICATE"})
    context = context_for(tmp_path, control=control)
    refusing(context, "DAEMON_UNAVAILABLE", solution_id=SOLUTION, reason="non-conformant")


def test_an_unreachable_daemon_is_reported_as_unavailable(tmp_path: Path) -> None:
    control = FakeControl(failures={"reconcile": RuntimeError("connection refused")})
    context = context_for(tmp_path, control=control)
    error = refusing(context, "DAEMON_UNAVAILABLE", solution_id=SOLUTION, reason="offline")
    assert error.retryable is True
