"""C-029 regression tests: ``graph_query``.

The positive cases run the real dispatcher over the real shipped bundle - the vendored
generations, the real registry and the real native guard - and assert the *envelope*, not just that
"something" came back. The cases that carry the task are:

* :func:`test_an_arbitrary_sql_or_shell_field_is_refused_by_name` - the AC1 claim ("arbitrary
  SQL/Cypher/shell are not accepted") is a property of the closed request parser, so it is
  asserted on the parser: ``sql``, ``cypher``, ``command`` and ``script`` are refused *by name*;
* :func:`test_every_documented_operation_answers_the_contract_envelope` - one bounded dispatcher
  really does expose all eight documented operations, and every one of them answers with the same
  generation vector;
* :func:`test_require_fresh_without_reconcile_authority_is_forbidden_and_calls_nothing` - the
  gateway refuses a freshness proof for a read-only token *before* it consults the control plane,
  so a read-only caller can never make the gateway mutate on its behalf;
* :func:`test_a_cursor_is_accepted_only_for_the_request_it_was_issued_for` - a cursor binds
  generation, query, scope and capability, and each drift has its own named refusal.

The negative and boundary legs cover an unknown operation, an unknown field, a selector from
another operation, an unauthorized and an unregistered solution, an out-of-scope and an unknown
project, a pinned generation that is not published, a projection that would drop ``source``,
out-of-contract bounds, an unreadable cursor, and a pinned/``require_fresh`` pair.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_query_support import generation_id, node_document

from axiom_mcp.errors import AxiomError
from axiom_mcp.query.changes import changes as changes_engine
from axiom_mcp.query.envelope import ENVELOPE_KEYS, FRESHNESS, FRESHNESS_UNKNOWN, VERIFICATION_NONE
from axiom_mcp.query.model import GraphSet, graph_from_documents
from axiom_mcp.tools.query import QUERY_OPERATIONS, _change_facts, graph_query
from tests.test_tools_support import (
    AUTH_API,
    AUTH_API_GENERATION,
    SOLUTION,
    WEB_APP,
    WEB_APP_GENERATION,
    FakeControl,
    context_for,
)

#: The two pinned nodes of the vendored bundle (``tests/fixtures/solution/demo-solution``).
AUTH_ENDPOINT = "7ff3a347ce2f66d59e181e6658f5c3786a98e865988a965e8e92f0c6fd07ceca"
WEB_METHOD = "8401d98ce5207c31bdfaf1621785475ac8a0707d0a3ee1f9b160f89c6c313904"

#: The generation vector of that bundle, as the dispatcher's own envelope computes it.
PINNED_GENERATION = "5ef9f9c55d2ab829cde223e18f023fe85808efd3656b14bcd7ad393806116a9d"

REQUIRED_ENVELOPE_KEYS = frozenset(
    {
        "schema_version",
        "solution_id",
        "catalog_generation_id",
        "project_generations",
        "freshness",
        "coverage",
        "verification",
        "nodes",
        "edges",
        "truncated",
        "warnings",
    }
)


def call(context, **arguments):
    """Dispatch one request through the real handler over a real context."""
    return graph_query(dict(arguments), context)


def arguments(operation: str, **extra):
    """A minimal well-formed request for one operation."""
    request = {"solution_id": SOLUTION, "operation": operation}
    request.update(extra)
    return request


def operation_request(operation: str):
    """The minimal valid arguments for each documented operation."""
    if operation == "search":
        return arguments(operation, query="login")
    if operation == "path":
        return arguments(operation, target=WEB_METHOD, target_to=AUTH_ENDPOINT)
    if operation == "changes":
        return arguments(operation)
    return arguments(operation, target=WEB_METHOD)


def assert_closed_envelope(document) -> None:
    """Every answer is the contract envelope: declared keys only, and the required ones present."""
    assert set(document) <= set(ENVELOPE_KEYS)
    assert REQUIRED_ENVELOPE_KEYS <= set(document)
    assert document["schema_version"] == 1
    assert document["solution_id"] == SOLUTION
    assert document["catalog_generation_id"] == PINNED_GENERATION
    assert document["freshness"] in FRESHNESS
    assert document["verification"] == {"mode": VERIFICATION_NONE}
    assert document["coverage"] == "partial"
    assert document["project_generations"] == [
        {"project_id": AUTH_API, "generation_id": AUTH_API_GENERATION},
        {"project_id": WEB_APP, "generation_id": WEB_APP_GENERATION},
    ]


def refuse(code: str, request, context, **details) -> AxiomError:
    """Assert one request is refused with ``code`` and the expected named details."""
    with pytest.raises(AxiomError) as caught:
        graph_query(request, context)
    assert caught.value.code == code
    for key, value in details.items():
        assert caught.value.details.get(key) == value, caught.value.details
    return caught.value


# --- positive legs ---------------------------------------------------------------------------


def test_every_documented_operation_answers_the_contract_envelope(tmp_path: Path) -> None:
    """AC1: the one dispatcher exposes every documented operation, all on the same vector."""
    context = context_for(tmp_path)
    assert len(QUERY_OPERATIONS) == 8
    for operation in QUERY_OPERATIONS:
        document = graph_query(operation_request(operation), context)
        assert_closed_envelope(document)
        # An immutable pinned generation is not evidence about the source tree.
        assert document["freshness"] == FRESHNESS_UNKNOWN
        assert any("freshness was not reported" in note for note in document["warnings"])


def test_search_returns_the_pinned_nodes_with_their_source_locations(tmp_path: Path) -> None:
    """AC2 positive: a search answers from the pinned snapshot, with real source positions."""
    context = context_for(tmp_path)
    document = call(context, **arguments("search", query="login"))

    assert_closed_envelope(document)
    assert len(document["nodes"]) == 2
    assert {node["id"] for node in document["nodes"]} == {AUTH_ENDPOINT, WEB_METHOD}
    for node in document["nodes"]:
        assert node["source"]["file"]
        assert node["source"]["start_line"] >= 1
        assert node["source"]["end_line"] >= node["source"]["start_line"]
    # Two distinct identities match, so the answer names the ambiguity instead of picking one.
    assert any("ambiguous" in note for note in document["warnings"])


def test_context_walks_exactly_the_requested_depth(tmp_path: Path) -> None:
    """AC2 positive: a neighbourhood request returns the pinned edge as well as the pinned node."""
    context = context_for(tmp_path)
    document = call(context, **arguments("context", target=WEB_METHOD, depth=1))

    assert_closed_envelope(document)
    assert document["nodes"]
    assert document["edges"]
    assert document["truncated"] is False


def test_path_proves_the_route_between_two_pinned_nodes(tmp_path: Path) -> None:
    """AC2 positive: a found path is reported as found, not as an unproven absence."""
    context = context_for(tmp_path)
    document = call(context, **arguments("path", target=WEB_METHOD, target_to=AUTH_ENDPOINT))

    assert_closed_envelope(document)
    assert {node["id"] for node in document["nodes"]} == {WEB_METHOD, AUTH_ENDPOINT}
    assert len(document["edges"]) == 1
    assert document["truncated"] is False


def test_changes_without_a_baseline_computes_no_diff(tmp_path: Path) -> None:
    """AC2 boundary: no baseline means no diff, and the reason travels with the answer."""
    context = context_for(tmp_path)
    document = call(context, **arguments("changes"))

    assert_closed_envelope(document)
    assert document["nodes"] == []
    assert document["edges"] == []
    assert any("baseline_not_named" in note for note in document["warnings"])


def test_a_pinned_request_against_the_published_vector_is_served(tmp_path: Path) -> None:
    """AC2 positive: ``pinned`` is honoured when the lane still carries that vector."""
    context = context_for(tmp_path)
    plain = call(context, **arguments("search", query="login"))
    pinned = call(
        context,
        **arguments(
            "search",
            query="login",
            consistency="pinned",
            catalog_generation_id=plain["catalog_generation_id"],
        ),
    )

    assert pinned["catalog_generation_id"] == plain["catalog_generation_id"]
    assert pinned["nodes"] == plain["nodes"]
    # A pinned read is still not fresh: an immutable generation proves the bytes, not the source.
    assert pinned["freshness"] == FRESHNESS_UNKNOWN


def test_a_named_baseline_is_accepted_and_no_diff_is_invented(tmp_path: Path) -> None:
    """AC2 boundary: a baseline that *is* the pinned vector is accepted, and no diff is faked.

    The vendored bundle declares no schema major, so the engine answers ``unknown_schema`` and the
    answer says exactly that; what is asserted here is that the tool does not relabel the engine's
    refusal as a baseline problem of its own, and still invents no facts.
    """
    context = context_for(tmp_path)
    head = call(context, **arguments("search", query="login"))
    document = call(
        context,
        **arguments(
            "changes",
            baseline_catalog_generation_id=head["catalog_generation_id"],
        ),
    )

    assert_closed_envelope(document)
    assert document["nodes"] == []
    assert document["edges"] == []
    assert not any("baseline_not_pinnable" in note for note in document["warnings"])
    assert any(
        "not_comparable: changes reported unknown_schema" in note for note in document["warnings"]
    )


def test_a_comparable_diff_yields_the_added_and_modified_facts() -> None:
    """AC2 positive: the diff post-processing is exercised against real engine output.

    The vendored bundle cannot reach this path (it declares no schema major, so ``changes``
    refuses with ``unknown_schema`` there), so the tool layer's own extraction is asserted against
    a real comparable :class:`~axiom_mcp.query.changes.ChangeResult` built from the documented
    shard shape.
    """
    kept = node_document("Kept", qualified_name="App.Kept")
    moved = node_document("Moved", qualified_name="App.Moved")
    dropped = node_document("Dropped", qualified_name="App.Dropped")
    added = node_document("Added", qualified_name="App.Added")

    def scope(seed, nodes):
        return GraphSet(
            (
                graph_from_documents(
                    "auth-api", generation_id(seed), nodes=nodes, edges=(), schema_major=1
                ),
            )
        )

    baseline = scope("c029-base", (kept, moved, dropped))
    head = scope(
        "c029-head",
        (
            kept,
            node_document("Moved", qualified_name="App.Moved", start_line=9, end_line=12),
            added,
        ),
    )
    result = changes_engine(head, baseline)
    assert result.comparable is True

    nodes, edges = _change_facts(result)
    assert sorted(node["name"] for node in nodes) == ["Added", "Moved"]
    assert edges == ()
    # Every fact is a pinned head document, so every node still carries its own source location.
    for node in nodes:
        assert node["project_id"] == "auth-api"
        assert node["id"]
        assert node["source"]["file"]
    # A removed fact belongs to the baseline generation, which this answer does not pin.
    assert "Dropped" not in {node["name"] for node in nodes}


# --- negative and failure-boundary legs --------------------------------------------------------


@pytest.mark.parametrize("field", ["sql", "cypher", "command", "script"])
def test_an_arbitrary_sql_or_shell_field_is_refused_by_name(tmp_path: Path, field: str) -> None:
    """AC1 negative: the execution-surface fields are not fields, and are refused by name."""
    context = context_for(tmp_path)
    request = arguments("search", query="login")
    request[field] = "SELECT 1"
    refuse("VALIDATION_ERROR", request, context, unexpected_fields=[field])


def test_an_unknown_operation_is_refused(tmp_path: Path) -> None:
    """AC1 negative: nothing outside the documented set can reach the engine."""
    context = context_for(tmp_path)
    error = refuse(
        "UNSUPPORTED_OPERATION",
        {"solution_id": SOLUTION, "operation": "DROP TABLE nodes"},
        context,
    )
    assert error.details["allowed"] == list(QUERY_OPERATIONS)


def test_an_unknown_field_is_refused(tmp_path: Path) -> None:
    """AC2 negative: an unrecognized key is a validation error, never an ignored input."""
    context = context_for(tmp_path)
    request = arguments("search", query="login", sql_limit=1)
    refuse("VALIDATION_ERROR", request, context, unexpected_fields=["sql_limit"])


def test_a_selector_from_another_operation_is_refused(tmp_path: Path) -> None:
    """AC2 negative: a selector that cannot apply is refused rather than silently ignored."""
    context = context_for(tmp_path)
    request = arguments("search", query="login", target=WEB_METHOD)
    refuse("VALIDATION_ERROR", request, context, unexpected_fields=["target"])


def test_pinned_without_a_generation_is_refused(tmp_path: Path) -> None:
    """AC2 negative: ``pinned`` must name the vector it pins."""
    context = context_for(tmp_path)
    request = arguments("search", query="login", consistency="pinned")
    refuse("VALIDATION_ERROR", request, context, field="catalog_generation_id")


def test_a_generation_this_lane_does_not_publish_is_snapshot_expired(tmp_path: Path) -> None:
    """AC2 negative: a stale pin is refused, never answered from whatever is current."""
    context = context_for(tmp_path)
    request = arguments(
        "search", query="login", consistency="pinned", catalog_generation_id="0" * 64
    )
    refuse("SNAPSHOT_EXPIRED", request, context, field="catalog_generation_id")


def test_an_unauthorized_solution_is_invisible(tmp_path: Path) -> None:
    """AC2 negative: an unauthorized solution answers exactly like an unregistered one."""
    invisible = refuse(
        "NOT_FOUND",
        arguments("search", query="login"),
        context_for(tmp_path / "a", solution_ids=frozenset({"other-solution"})),
    )
    registered = context_for(tmp_path / "b")
    unknown = refuse(
        "NOT_FOUND",
        {"solution_id": "never-registered", "operation": "search", "query": "login"},
        registered,
    )
    assert invisible.code == unknown.code == "NOT_FOUND"


def test_a_project_outside_the_token_scope_is_invisible(tmp_path: Path) -> None:
    """AC2 negative: a project the token does not hold is NOT_FOUND, not a widened answer."""
    context = context_for(tmp_path, project_ids=frozenset({AUTH_API}))
    request = arguments("search", query="login", project_id=WEB_APP)
    refuse("NOT_FOUND", request, context)


def test_an_unknown_project_is_not_found(tmp_path: Path) -> None:
    """AC2 negative: a project the solution does not register is NOT_FOUND."""
    context = context_for(tmp_path)
    refuse("NOT_FOUND", arguments("search", query="login", project_id="ghost"), context)


def test_a_projection_that_drops_source_locations_is_refused(tmp_path: Path) -> None:
    """AC2 negative: the response contract requires ``source`` on every node it carries."""
    context = context_for(tmp_path)
    request = arguments("context", target=WEB_METHOD, projection=["identity"])
    refuse("VALIDATION_ERROR", request, context, field="projection")


def test_bounds_outside_the_contract_are_refused(tmp_path: Path) -> None:
    """AC2 boundary: a bound outside the canonical range is refused, not clamped."""
    context = context_for(tmp_path)
    refuse("LIMIT_EXCEEDED", arguments("search", query="login", max_nodes=9999), context)

    with pytest.raises(AxiomError) as caught:
        graph_query(arguments("context", target=WEB_METHOD, depth=99), context)
    assert caught.value.code == "LIMIT_EXCEEDED"
    assert caught.value.details["field"] == "depth"

    with pytest.raises(AxiomError) as too_small:
        graph_query(arguments("search", query="login", max_bytes=1), context)
    assert too_small.value.code == "LIMIT_EXCEEDED"
    assert too_small.value.details["field"] == "max_bytes"


def test_a_direction_outside_the_contract_is_refused(tmp_path: Path) -> None:
    """AC2 negative: the direction allowlist is enforced at the tool boundary."""
    context = context_for(tmp_path)
    request = arguments("neighbors", target=WEB_METHOD, direction="sideways")
    refuse("VALIDATION_ERROR", request, context, field="direction")


def test_require_fresh_without_reconcile_authority_is_forbidden_and_calls_nothing(
    tmp_path: Path,
) -> None:
    """AC1 negative: authorization precedes availability, so a read-only token never mutates."""
    control = FakeControl()
    context = context_for(tmp_path, caps=frozenset({"read"}), control=control)
    request = arguments("search", query="login", consistency="require_fresh")
    refuse("FORBIDDEN", request, context)
    # The refusal happened before the control plane was consulted at all.
    assert control.calls == []


def test_require_fresh_with_authority_enqueues_one_reconcile_and_serves_nothing(
    tmp_path: Path,
) -> None:
    """AC2 boundary: the honest answer is NOT_READY plus the job handle, never a stale snapshot."""
    control = FakeControl()
    context = context_for(tmp_path, control=control)
    request = arguments("search", query="login", consistency="require_fresh")
    error = refuse("NOT_READY", request, context, job_id="job-0001")

    assert error.retryable is True
    assert [(recorded.operation, dict(recorded.payload)) for recorded in control.calls] == [
        (
            "reconcile",
            {
                "solution_id": SOLUTION,
                "project_ids": [AUTH_API, WEB_APP],
                "scope": "solution",
                "reason": "require_fresh",
            },
        )
    ]


def test_require_fresh_without_a_control_plane_is_daemon_unavailable(tmp_path: Path) -> None:
    """AC2 boundary: a snapshot-only gateway cannot prove freshness and says so."""
    context = context_for(tmp_path)
    request = arguments("search", query="login", consistency="require_fresh")
    refuse("DAEMON_UNAVAILABLE", request, context)


def test_a_cursor_is_accepted_only_for_the_request_it_was_issued_for(tmp_path: Path) -> None:
    """AC2 negative: the cursor binding is checked on every use, with the drift named."""
    context = context_for(tmp_path)
    head = call(context, **arguments("search", query="login"))
    generation = head["catalog_generation_id"]
    scope = (AUTH_API, WEB_APP)

    good = context.cursors.issue(
        catalog_generation_id=generation,
        operation="search",
        query={"query": "login"},
        scope=scope,
        capability="read",
    )
    document = call(context, **arguments("search", query="login", cursor=good.cursor_id))
    assert document["nodes"] == head["nodes"]

    drift = context.cursors.issue(
        catalog_generation_id=generation,
        operation="search",
        query={"query": "login"},
        scope=scope,
        capability="read",
    )
    refuse(
        "VALIDATION_ERROR",
        arguments("search", query="AuthService", cursor=drift.cursor_id),
        context,
        field="cursor",
        reason="query",
    )

    moved = context.cursors.issue(
        catalog_generation_id="1" * 64,
        operation="search",
        query={"query": "login"},
        scope=scope,
        capability="read",
    )
    refuse(
        "SNAPSHOT_EXPIRED",
        arguments("search", query="login", cursor=moved.cursor_id),
        context,
        field="cursor",
        reason="generation",
    )

    stale = context.cursors.issue(
        catalog_generation_id=generation,
        operation="search",
        query={"query": "login"},
        scope=scope,
        capability="read",
        ttl_seconds=1,
        now=0.0,
    )
    refuse(
        "CONFLICT",
        arguments("search", query="login", cursor=stale.cursor_id),
        context,
        field="cursor",
        reason="expired",
    )


def test_a_cursor_that_is_not_a_digest_is_not_found(tmp_path: Path) -> None:
    """AC2 failure boundary: an unreadable cursor id is NOT_FOUND, not a leaked engine error."""
    context = context_for(tmp_path)
    request = arguments("search", query="login", cursor="not-a-digest")
    error = refuse("NOT_FOUND", request, context, field="cursor")
    assert error.details["reason"] == "unknown"
