"""C-028 regression tests: ``graph_status``.

The positive cases read the real shipped bundle through the real registry and the real native
guard, so the generations, records and fingerprints asserted here are the vendored ones rather
than something the test wrote. The cases that carry the task are:

* :func:`test_a_bare_daemon_fresh_claim_is_downgraded_to_unknown` - a daemon that says "fresh"
  without an ``inventory_hash`` verification does not get to make the answer fresh;
* :func:`test_an_unauthorized_solution_is_invisible` and
  :func:`test_an_unregistered_solution_answers_identically` - the two answers are the *same*,
  which is what stops the tool being a solution-enumeration oracle.

The negative and boundary cases cover an unexpected field, both project selectors at once, a
project the solution does not register, an unknown lane, and a token scoped to a subset of the
projects.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom_mcp import security
from axiom_mcp.errors import AxiomError
from axiom_mcp.query.envelope import COVERAGE, FRESHNESS
from axiom_mcp.tools.status import STATUS_FIELDS, graph_status
from tests.test_tools_support import (
    AUTH_API,
    AUTH_API_FINGERPRINT,
    AUTH_API_GENERATION,
    SOLUTION,
    WEB_APP,
    WEB_APP_GENERATION,
    FakeControl,
    context_for,
)


def call(context, **arguments):
    return graph_status(dict(arguments), context)


def test_status_reports_the_pinned_generations_with_a_closed_schema(tmp_path: Path) -> None:
    """AC1: the answer is the pinned snapshot, and its key set is exactly the declared one."""
    context = context_for(tmp_path)
    document = call(context, solution_id=SOLUTION)

    assert tuple(document) == STATUS_FIELDS
    assert set(document) == set(STATUS_FIELDS)
    assert document["schema_version"] == 1
    assert document["solution_id"] == SOLUTION
    # The default lane is the working set a query answers from (docs/guides/snapshots.md).
    assert document["lane"] == "live"
    assert document["coverage"] in COVERAGE
    assert document["freshness"] in FRESHNESS

    generations = {entry["project_id"]: entry["generation_id"] for entry in document["projects"]}
    assert generations == {AUTH_API: AUTH_API_GENERATION, WEB_APP: WEB_APP_GENERATION}
    for entry in document["projects"]:
        assert entry["records"] > 0
        assert entry["bytes_copied"] > 0
        assert entry["verification"] == "manifest_hash"
        assert entry["coverage"] in COVERAGE
        assert entry["guard_held_during_copy"] is True
        assert entry["parsed_after_guard_release"] is True


def test_the_checkpoint_lane_resolves_exactly_like_the_live_lane(tmp_path: Path) -> None:
    """AC1 boundary: a snapshot-only gateway reads a checkpoint lane, so both lanes answer."""
    context = context_for(tmp_path)
    live = call(context, solution_id=SOLUTION)
    checkpoint = call(context, solution_id=SOLUTION, lane="checkpoint")

    assert checkpoint["lane"] == "checkpoint"
    assert [(entry["project_id"], entry["generation_id"]) for entry in checkpoint["projects"]] == [
        (entry["project_id"], entry["generation_id"]) for entry in live["projects"]
    ]
    assert checkpoint["catalog_generation_id"] == live["catalog_generation_id"]


def test_status_reports_capabilities_from_the_caller_not_the_process(tmp_path: Path) -> None:
    """AC1: the capability block describes the token, so a reader cannot be told it may write."""
    context = context_for(tmp_path, caps=frozenset({security.CAPABILITY_READ}))
    document = call(context, solution_id=SOLUTION)

    assert document["capabilities"] == {
        "checkpoint": False,
        "read": True,
        "reconcile": False,
    }


def test_daemon_status_is_absent_until_asked_for(tmp_path: Path) -> None:
    """AC1: the daemon half is optional, and an unasked plane is not reported as down."""
    control = FakeControl()
    context = context_for(tmp_path, control=control)
    document = call(context, solution_id=SOLUTION)

    assert document["daemon"] is None
    assert control.calls == []
    assert document["freshness"] == "unknown"


def test_daemon_availability_is_reported_when_requested(tmp_path: Path) -> None:
    """AC1: with the control plane asked, its availability travels in the answer."""
    control = FakeControl(
        statuses={SOLUTION: {"available": False, "reason": "daemon_offline", "jobs_in_flight": 0}}
    )
    context = context_for(tmp_path, control=control)
    document = call(context, solution_id=SOLUTION, include_daemon=True)

    assert document["daemon"] == {
        "available": False,
        "reason": "daemon_offline",
        "jobs_in_flight": 0,
    }
    assert [record.operation for record in control.calls] == ["status"]
    # A snapshot-only answer is a supported state, not an error.
    assert document["freshness"] == "unknown"


def test_a_bare_daemon_fresh_claim_is_downgraded_to_unknown(tmp_path: Path) -> None:
    """AC1 boundary: 'fresh' without inventory-hash evidence is reported as unknown, with a why."""
    control = FakeControl(
        statuses={
            SOLUTION: {"available": True, "freshness": "fresh", "verification": "watcher_hint"}
        }
    )
    context = context_for(tmp_path, control=control)
    document = call(context, solution_id=SOLUTION, include_daemon=True)

    assert document["freshness"] == "unknown"
    assert any("verification mode" in warning for warning in document["warnings"])


def test_an_evidenced_fresh_claim_is_reported_as_fresh(tmp_path: Path) -> None:
    """AC1 positive boundary: inventory-hash evidence with its fingerprint does support fresh."""
    control = FakeControl(
        statuses={
            SOLUTION: {
                "available": True,
                "freshness": "fresh",
                "verification": "inventory_hash",
                "verification_source_fingerprint": AUTH_API_FINGERPRINT,
            }
        }
    )
    context = context_for(tmp_path, control=control)
    document = call(context, solution_id=SOLUTION, include_daemon=True)

    assert document["freshness"] == "fresh"
    assert document["warnings"] == []


def test_a_daemon_failure_is_unavailability_not_a_tool_error(tmp_path: Path) -> None:
    """AC1 failure boundary: an unreachable daemon does not fail the snapshot answer."""
    control = FakeControl(failures={"status": RuntimeError("pipe closed")})
    context = context_for(tmp_path, control=control)
    document = call(context, solution_id=SOLUTION, include_daemon=True)

    assert document["daemon"] == {"available": False, "reason": "RuntimeError"}
    assert document["projects"]


def test_an_unauthorized_solution_is_invisible(tmp_path: Path) -> None:
    """AC1: a token scoped elsewhere gets NOT_FOUND, never a distinguishable FORBIDDEN."""
    context = context_for(tmp_path, solution_ids=frozenset({"some-other-solution"}))
    with pytest.raises(AxiomError) as raised:
        call(context, solution_id=SOLUTION)

    assert raised.value.code == "NOT_FOUND"
    assert "some-other-solution" not in raised.value.message


def test_an_unregistered_solution_answers_identically(tmp_path: Path) -> None:
    """AC1: unknown and unauthorized are the same answer, so neither is an existence oracle."""
    invisible = context_for(tmp_path / "a", solution_ids=frozenset({"other-solution"}))
    with pytest.raises(AxiomError) as unauthorized:
        call(invisible, solution_id=SOLUTION)

    registered = context_for(tmp_path / "b")
    with pytest.raises(AxiomError) as unknown:
        call(registered, solution_id="never-registered")

    assert unauthorized.value.code == unknown.value.code == "NOT_FOUND"


def test_a_token_without_read_capability_is_forbidden(tmp_path: Path) -> None:
    """AC2 negative: the capability check precedes the scope check and is a different answer."""
    context = context_for(tmp_path, caps=frozenset({security.CAPABILITY_RECONCILE}))
    with pytest.raises(AxiomError) as raised:
        call(context, solution_id=SOLUTION)

    assert raised.value.code == "FORBIDDEN"


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"solution_id": SOLUTION, "unexpected": 1}, "VALIDATION_ERROR"),
        (
            {"solution_id": SOLUTION, "project_id": AUTH_API, "project_ids": [AUTH_API]},
            "VALIDATION_ERROR",
        ),
        ({"solution_id": SOLUTION, "project_id": "not-registered"}, "NOT_FOUND"),
        ({"solution_id": SOLUTION, "lane": "staging"}, "VALIDATION_ERROR"),
        ({"solution_id": ""}, "VALIDATION_ERROR"),
        ({"solution_id": "Not A Solution"}, "VALIDATION_ERROR"),
    ],
)
def test_malformed_requests_are_refused_with_their_own_code(
    tmp_path: Path, arguments: dict[str, object], code: str
) -> None:
    """AC2 negative: each refusal names the real reason rather than a generic failure."""
    context = context_for(tmp_path)
    with pytest.raises(AxiomError) as raised:
        call(context, **arguments)

    assert raised.value.code == code


def test_project_scope_is_narrowed_to_the_token(tmp_path: Path) -> None:
    """AC2 boundary: a project-scoped token is answered for its projects only."""
    context = context_for(tmp_path, project_ids=frozenset({AUTH_API}))
    document = call(context, solution_id=SOLUTION)

    assert [entry["project_id"] for entry in document["projects"]] == [AUTH_API]


def test_naming_a_project_outside_the_token_scope_is_not_found(tmp_path: Path) -> None:
    """AC2 boundary: asking directly for a project the token cannot see is also invisible."""
    context = context_for(tmp_path, project_ids=frozenset({AUTH_API}))
    with pytest.raises(AxiomError) as raised:
        call(context, solution_id=SOLUTION, project_id=WEB_APP)

    assert raised.value.code == "NOT_FOUND"


def test_project_ids_are_deduplicated_and_ordered(tmp_path: Path) -> None:
    """AC2 boundary: a repeated selector is one project, and the answer keeps registry order."""
    context = context_for(tmp_path)
    document = call(context, solution_id=SOLUTION, project_ids=[WEB_APP, AUTH_API, WEB_APP])

    assert [entry["project_id"] for entry in document["projects"]] == [WEB_APP, AUTH_API]


def test_the_catalog_generation_digest_is_stable_across_calls(tmp_path: Path) -> None:
    """AC1 boundary: the digest is a function of the pinned members, not of call order."""
    context = context_for(tmp_path)
    first = call(context, solution_id=SOLUTION)
    second = call(context, solution_id=SOLUTION, project_ids=[WEB_APP, AUTH_API])

    assert first["catalog_generation_id"] == second["catalog_generation_id"]
