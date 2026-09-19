"""C-031 regression tests: ``graph_job``.

The daemon is not in this repository, so the honest test is again the recorded delegation: the
cases that carry the task are

* :func:`test_exactly_one_call_is_made_even_with_a_long_wait` - the polling bound is not a promise
  in a docstring, it is one recorded call;
* :func:`test_a_read_only_token_cannot_cancel_and_the_daemon_is_never_called` - the mutation
  capability is checked before the control plane is consulted;
* :func:`test_another_solutions_job_is_not_found` - job ownership is verified on the answer, and an
  invisible job is indistinguishable from an unknown one.

The negative and boundary cases cover a path-shaped job id, an unknown action, `wait_ms` on a
cancel, a bounded `wait_ms`, an unexpected field, an unknown job state, a job without its
solution, a percentage field, an unreachable daemon and a missing control plane.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom_mcp import security
from axiom_mcp.errors import AxiomError
from axiom_mcp.tools.job import (
    JOB_FIELDS,
    MAX_JOB_CALLS,
    PROGRESS_UNITS,
    graph_job,
)
from tests.test_tools_support import SOLUTION, FakeControl, context_for


def a_job(**overrides):
    job = {
        "job_id": "job-0001",
        "kind": "ParseBatch",
        "state": "RUNNING",
        "solution_id": SOLUTION,
        "attempt": 0,
        "fence": 3,
        "target_event_seq": 41,
    }
    job.update(overrides)
    return job


def call(context, **arguments):
    return graph_job(dict(arguments), context)


def refusing(context, code, **arguments):
    with pytest.raises(AxiomError) as caught:
        graph_job(dict(arguments), context)
    assert caught.value.code == code
    return caught.value


def test_a_status_read_returns_the_bounded_job_view(tmp_path: Path) -> None:
    control = FakeControl(jobs={"job-0001": a_job(queue_depth=4, retry_after_ms=500)})
    context = context_for(tmp_path, control=control)
    answer = call(context, job_id="job-0001")
    assert tuple(answer) == JOB_FIELDS
    assert answer["action"] == "status"
    assert answer["solution_id"] == SOLUTION
    assert answer["job"] == {
        "job_id": "job-0001",
        "kind": "ParseBatch",
        "state": "RUNNING",
        "solution_id": SOLUTION,
        "attempt": 0,
        "fence": 3,
        "target_event_seq": 41,
        "retry_after_ms": 500,
        "terminal": False,
    }
    assert control.calls[0].operation == "job"
    assert control.calls[0].payload == {"job_id": "job-0001", "action": "status", "wait_ms": 0}


def test_exactly_one_call_is_made_even_with_a_long_wait(tmp_path: Path) -> None:
    control = FakeControl(jobs={"job-0001": a_job()})
    context = context_for(tmp_path, control=control)
    answer = call(context, job_id="job-0001", wait_ms=30000)
    assert MAX_JOB_CALLS == 1
    assert len(control.calls) == 1
    assert control.calls[0].payload["wait_ms"] == 30000
    assert answer["job"]["state"] == "RUNNING"


def test_a_terminal_job_is_marked_terminal(tmp_path: Path) -> None:
    control = FakeControl(jobs={"job-0001": a_job(state="SUCCEEDED")})
    context = context_for(tmp_path, control=control)
    assert call(context, job_id="job-0001")["job"]["terminal"] is True


def test_a_read_only_token_cannot_cancel_and_the_daemon_is_never_called(tmp_path: Path) -> None:
    control = FakeControl(jobs={"job-0001": a_job()})
    context = context_for(tmp_path, caps=frozenset({security.CAPABILITY_READ}), control=control)
    error = refusing(context, "FORBIDDEN", job_id="job-0001", action="cancel")
    assert error.details["required_capability"] == security.CAPABILITY_RECONCILE
    assert control.calls == []


def test_a_cancel_reaches_the_daemon_with_the_checked_capability(tmp_path: Path) -> None:
    control = FakeControl(jobs={"job-0001": a_job(state="CANCEL_REQUESTED")})
    context = context_for(tmp_path, control=control)
    answer = call(context, job_id="job-0001", action="cancel")
    assert answer["action"] == "cancel"
    assert answer["job"]["state"] == "CANCEL_REQUESTED"
    assert control.calls[0].payload == {"job_id": "job-0001", "action": "cancel", "wait_ms": 0}


def test_another_solutions_job_is_not_found(tmp_path: Path) -> None:
    control = FakeControl(jobs={"job-0001": a_job(solution_id="other-solution")})
    context = context_for(tmp_path, control=control)
    refusing(context, "NOT_FOUND", job_id="job-0001")


def test_a_job_for_an_unregistered_solution_is_not_found(tmp_path: Path) -> None:
    control = FakeControl(jobs={"job-0001": a_job(solution_id="ghost-solution")})
    context = context_for(
        tmp_path, solution_ids=frozenset({SOLUTION, "ghost-solution"}), control=control
    )
    refusing(context, "NOT_FOUND", job_id="job-0001")


def test_an_unknown_job_state_is_refused(tmp_path: Path) -> None:
    control = FakeControl(jobs={"job-0001": a_job(state="FROBNICATE")})
    context = context_for(tmp_path, control=control)
    refusing(context, "DAEMON_UNAVAILABLE", job_id="job-0001")


def test_a_job_without_its_solution_is_unavailable(tmp_path: Path) -> None:
    job = a_job()
    del job["solution_id"]
    control = FakeControl(jobs={"job-0001": job})
    context = context_for(tmp_path, control=control)
    refusing(context, "DAEMON_UNAVAILABLE", job_id="job-0001")


def test_a_percentage_is_dropped_with_a_warning(tmp_path: Path) -> None:
    control = FakeControl(
        jobs={"job-0001": a_job(progress={"units_done": 3, "percent": 40, "shards_written": 2})}
    )
    context = context_for(tmp_path, control=control)
    answer = call(context, job_id="job-0001")
    assert answer["job"]["progress"] == {"units_done": 3, "shards_written": 2}
    assert answer["warnings"] == ["percentage_not_carried:percent"]
    assert "percent" not in PROGRESS_UNITS


@pytest.mark.parametrize(
    "job_id",
    ["../etc/passwd", "a/b", "", "x" * 129, "job 1"],
)
def test_a_path_shaped_or_over_long_job_id_is_refused(tmp_path: Path, job_id: str) -> None:
    control = FakeControl()
    context = context_for(tmp_path, control=control)
    refusing(context, "VALIDATION_ERROR", job_id=job_id)
    assert control.calls == []


def test_an_unknown_action_is_refused(tmp_path: Path) -> None:
    control = FakeControl()
    context = context_for(tmp_path, control=control)
    refusing(context, "VALIDATION_ERROR", job_id="job-0001", action="delete")
    assert control.calls == []


def test_wait_ms_on_a_cancel_is_refused(tmp_path: Path) -> None:
    control = FakeControl()
    context = context_for(tmp_path, control=control)
    refusing(context, "VALIDATION_ERROR", job_id="job-0001", action="cancel", wait_ms=1000)
    assert control.calls == []


@pytest.mark.parametrize("wait_ms", [-1, 30001])
def test_wait_ms_is_bounded(tmp_path: Path, wait_ms: int) -> None:
    control = FakeControl()
    context = context_for(tmp_path, control=control)
    refusing(context, "LIMIT_EXCEEDED", job_id="job-0001", wait_ms=wait_ms)
    assert control.calls == []


def test_an_unexpected_field_is_refused(tmp_path: Path) -> None:
    control = FakeControl()
    context = context_for(tmp_path, control=control)
    error = refusing(context, "VALIDATION_ERROR", job_id="job-0001", shell="rm -rf /")
    assert error.details["unexpected_fields"] == ["shell"]
    assert control.calls == []


def test_an_unknown_job_is_daemon_unavailable_not_invented(tmp_path: Path) -> None:
    control = FakeControl(jobs={})
    context = context_for(tmp_path, control=control)
    refusing(context, "DAEMON_UNAVAILABLE", job_id="job-0001")


def test_an_unreachable_daemon_is_reported_as_unavailable(tmp_path: Path) -> None:
    control = FakeControl(failures={"job": RuntimeError("connection refused")})
    context = context_for(tmp_path, control=control)
    error = refusing(context, "DAEMON_UNAVAILABLE", job_id="job-0001")
    assert error.retryable is True


def test_a_missing_control_plane_is_daemon_unavailable(tmp_path: Path) -> None:
    context = context_for(tmp_path, control=None)
    refusing(context, "DAEMON_UNAVAILABLE", job_id="job-0001")
