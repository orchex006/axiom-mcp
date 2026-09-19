"""C-032 regression tests: ``graph_verify``.

The daemon is not in this repository, so the recorded delegation is again the honest test. The
cases that carry the task are:

* :func:`test_each_verification_dimension_is_reported_separately` - all four modes come back as
  four separate claims instead of one "verified" boolean;
* :func:`test_an_archive_cannot_assert_current_filesystem_state` - the AC1 refusal, asserted with
  the exact code and detail;
* :func:`test_a_missing_dimension_is_not_run_with_a_warning` and
  :func:`test_an_unrecognised_mode_becomes_unknown` - absence and novelty never become a pass.

The negative and boundary cases cover a missing basis, a missing fingerprint *and* barrier, a
malformed fingerprint, an unexpected field, a bounded ``target_event_seq``, an unknown project, a
read-only token, an invisible solution, an asynchronous job answer, an unusable observed
fingerprint, a missing control plane and an unreachable daemon.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from axiom_mcp import security
from axiom_mcp.errors import AxiomError
from axiom_mcp.tools.verify import (
    DIMENSIONS,
    VERIFY_FIELDS,
    graph_verify,
)
from tests.test_tools_support import (
    AUTH_API,
    AUTH_API_FINGERPRINT,
    SOLUTION,
    FakeControl,
    context_for,
)


@dataclass
class RecordingSource:
    """A snapshot source that fails loudly if the verify tool tries to read one."""

    calls: list[str] = field(default_factory=list)

    def load(self, location: Any) -> Any:
        self.calls.append(str(location))
        raise AssertionError("a delegation tool must not read a snapshot")


def a_verification(**overrides):
    block: dict[str, Any] = {
        "hash": {"mode": "recomputed", "matched": True, "observed": AUTH_API_FINGERPRINT},
        "schema": {"mode": "declared"},
        "catalog": {"mode": "matched"},
        "source": {"mode": "archive_contents"},
    }
    block.update(overrides)
    return {"basis": "archive", "verification": block}


def call(context, **arguments):
    return graph_verify(dict(arguments), context)


def refusing(context, code, **arguments):
    with pytest.raises(AxiomError) as caught:
        graph_verify(dict(arguments), context)
    assert caught.value.code == code
    return caught.value


def test_each_verification_dimension_is_reported_separately(tmp_path: Path) -> None:
    control = FakeControl(verify_result=a_verification())
    context = context_for(tmp_path, control=control)
    answer = call(
        context,
        solution_id=SOLUTION,
        project_id=AUTH_API,
        expected_fingerprint=AUTH_API_FINGERPRINT,
    )
    assert tuple(answer) == VERIFY_FIELDS
    assert answer["basis"] == "archive"
    assert tuple(answer["verification"]) == DIMENSIONS
    assert answer["verification"]["hash"] == {
        "mode": "recomputed",
        "matched": True,
        "observed": AUTH_API_FINGERPRINT,
    }
    assert answer["verification"]["schema"] == {"mode": "declared"}
    assert answer["verification"]["catalog"] == {"mode": "matched"}
    assert answer["verification"]["source"] == {"mode": "archive_contents"}
    assert answer["warnings"] == []
    assert answer["job"] is None
    assert control.calls[0].payload == {
        "solution_id": SOLUTION,
        "project_ids": [AUTH_API],
        "expected_fingerprint": AUTH_API_FINGERPRINT,
    }


def test_an_archive_cannot_assert_current_filesystem_state(tmp_path: Path) -> None:
    control = FakeControl(verify_result=a_verification(source={"mode": "current"}))
    context = context_for(tmp_path, control=control)
    error = refusing(
        context,
        "DAEMON_UNAVAILABLE",
        solution_id=SOLUTION,
        expected_fingerprint=AUTH_API_FINGERPRINT,
    )
    assert error.details["basis"] == "archive"
    assert error.details["dimension"] == "source"


def test_a_working_tree_basis_may_assert_current(tmp_path: Path) -> None:
    answer_doc = a_verification(source={"mode": "current"})
    answer_doc["basis"] = "working_tree"
    control = FakeControl(verify_result=answer_doc)
    context = context_for(tmp_path, control=control)
    answer = call(context, solution_id=SOLUTION, expected_fingerprint=AUTH_API_FINGERPRINT)
    assert answer["basis"] == "working_tree"
    assert answer["verification"]["source"] == {"mode": "current"}


def test_a_missing_dimension_is_not_run_with_a_warning(tmp_path: Path) -> None:
    control = FakeControl(verify_result=a_verification(schema=None))
    context = context_for(tmp_path, control=control)
    answer = call(context, solution_id=SOLUTION, expected_fingerprint=AUTH_API_FINGERPRINT)
    assert answer["verification"]["schema"] == {"mode": "not_run"}
    assert answer["warnings"] == ["dimension_not_reported:schema"]


def test_an_unrecognised_mode_becomes_unknown(tmp_path: Path) -> None:
    control = FakeControl(verify_result=a_verification(catalog={"mode": "probably-fine"}))
    context = context_for(tmp_path, control=control)
    answer = call(context, solution_id=SOLUTION, expected_fingerprint=AUTH_API_FINGERPRINT)
    assert answer["verification"]["catalog"] == {"mode": "unknown"}
    assert answer["warnings"] == ["unrecognised_mode:catalog"]


def test_an_unusable_observed_fingerprint_is_dropped(tmp_path: Path) -> None:
    control = FakeControl(
        verify_result=a_verification(
            hash={"mode": "recomputed", "observed": "not-a-fingerprint", "matched": False}
        )
    )
    context = context_for(tmp_path, control=control)
    answer = call(context, solution_id=SOLUTION, expected_fingerprint=AUTH_API_FINGERPRINT)
    assert answer["verification"]["hash"] == {"mode": "recomputed", "matched": False}
    assert answer["warnings"] == ["unusable_fingerprint:hash.observed"]


def test_a_missing_basis_is_refused(tmp_path: Path) -> None:
    control = FakeControl(verify_result={"verification": a_verification()["verification"]})
    context = context_for(tmp_path, control=control)
    refusing(
        context,
        "DAEMON_UNAVAILABLE",
        solution_id=SOLUTION,
        expected_fingerprint=AUTH_API_FINGERPRINT,
    )


def test_an_asynchronous_answer_reports_a_job_only(tmp_path: Path) -> None:
    control = FakeControl(verify_result={"job_id": "verify-0001", "state": "RUNNING"})
    context = context_for(tmp_path, control=control)
    answer = call(context, solution_id=SOLUTION, target_event_seq=41)
    assert answer["job"] == {"job_id": "verify-0001", "state": "RUNNING"}
    assert answer["verification"] is None
    assert answer["basis"] is None


def test_the_request_is_bounded_to_a_fingerprint_or_a_barrier(tmp_path: Path) -> None:
    control = FakeControl(verify_result=a_verification())
    context = context_for(tmp_path, control=control)
    refusing(context, "VALIDATION_ERROR", solution_id=SOLUTION)
    assert control.calls == []


@pytest.mark.parametrize("fingerprint", ["abc", "z" * 64, "0" * 63, "0" * 65])
def test_a_malformed_fingerprint_is_refused(tmp_path: Path, fingerprint: str) -> None:
    control = FakeControl()
    context = context_for(tmp_path, control=control)
    refusing(
        context,
        "VALIDATION_ERROR",
        solution_id=SOLUTION,
        expected_fingerprint=fingerprint,
    )
    assert control.calls == []


def test_an_upper_case_fingerprint_is_lowercased(tmp_path: Path) -> None:
    control = FakeControl(verify_result=a_verification())
    context = context_for(tmp_path, control=control)
    call(context, solution_id=SOLUTION, expected_fingerprint=AUTH_API_FINGERPRINT.upper())
    assert control.calls[0].payload["expected_fingerprint"] == AUTH_API_FINGERPRINT


@pytest.mark.parametrize("barrier", [-1, 2**53])
def test_target_event_seq_is_bounded(tmp_path: Path, barrier: int) -> None:
    control = FakeControl()
    context = context_for(tmp_path, control=control)
    refusing(context, "LIMIT_EXCEEDED", solution_id=SOLUTION, target_event_seq=barrier)
    assert control.calls == []


def test_a_read_only_token_is_forbidden_before_the_daemon(tmp_path: Path) -> None:
    control = FakeControl()
    context = context_for(tmp_path, caps=frozenset({security.CAPABILITY_READ}), control=control)
    error = refusing(
        context, "FORBIDDEN", solution_id=SOLUTION, expected_fingerprint=AUTH_API_FINGERPRINT
    )
    assert error.details["required_capability"] == security.CAPABILITY_CHECKPOINT
    assert control.calls == []


def test_an_invisible_solution_is_not_found(tmp_path: Path) -> None:
    control = FakeControl()
    context = context_for(tmp_path, solution_ids=frozenset({"other-solution"}), control=control)
    refusing(context, "NOT_FOUND", solution_id=SOLUTION, expected_fingerprint=AUTH_API_FINGERPRINT)
    assert control.calls == []


def test_an_unknown_project_is_not_found(tmp_path: Path) -> None:
    control = FakeControl()
    context = context_for(tmp_path, control=control)
    refusing(
        context,
        "NOT_FOUND",
        solution_id=SOLUTION,
        project_id="no-such-project",
        expected_fingerprint=AUTH_API_FINGERPRINT,
    )
    assert control.calls == []


def test_an_unexpected_field_is_refused(tmp_path: Path) -> None:
    control = FakeControl()
    context = context_for(tmp_path, control=control)
    error = refusing(
        context,
        "VALIDATION_ERROR",
        solution_id=SOLUTION,
        expected_fingerprint=AUTH_API_FINGERPRINT,
        trust_me=True,
    )
    assert error.details["unexpected_fields"] == ["trust_me"]
    assert control.calls == []


def test_the_tool_reads_no_snapshot(tmp_path: Path) -> None:
    control = FakeControl(verify_result=a_verification())
    spy = RecordingSource()
    context = replace(context_for(tmp_path, control=control), source=spy)
    call(context, solution_id=SOLUTION, expected_fingerprint=AUTH_API_FINGERPRINT)
    assert spy.calls == []
    assert [record.operation for record in control.calls] == ["verify"]


def test_a_missing_control_plane_is_daemon_unavailable(tmp_path: Path) -> None:
    context = context_for(tmp_path, control=None)
    refusing(
        context,
        "DAEMON_UNAVAILABLE",
        solution_id=SOLUTION,
        expected_fingerprint=AUTH_API_FINGERPRINT,
    )


def test_an_unreachable_daemon_is_reported_as_unavailable(tmp_path: Path) -> None:
    control = FakeControl(failures={"verify": RuntimeError("connection refused")})
    context = context_for(tmp_path, control=control)
    error = refusing(
        context,
        "DAEMON_UNAVAILABLE",
        solution_id=SOLUTION,
        expected_fingerprint=AUTH_API_FINGERPRINT,
    )
    assert error.retryable is True
