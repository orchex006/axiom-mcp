"""C-019: the context projection is exactly what was requested, at exactly the requested depth.

AC1 has two halves:

* positive - a requested projection is honoured section by section (identity alone carries no file
  positions, ``source_locations`` adds them, ``coverage`` adds the pinned coverage block, and
  ``relations`` adds the incident edge ids of the returned neighbourhood), and ``depth`` is the
  hop count it claims to be;
* boundary - a budget that stops the expansion is recorded. The graph below is a chain with a
  branch, so ``max_nodes=2`` from the head cannot reach the tail, and the answer must say so with
  ``truncated`` and the budget's name rather than quietly returning a short neighbourhood.

The negative cases are an ambiguous target (candidates, no invented neighbourhood) and a target
that matches nothing (``TargetNotFound``); an out-of-range depth is a validation error, not a
clamp.
"""

from __future__ import annotations

import pytest
from test_query_support import edge_document, generation_id, node_document

from axiom_mcp.query.context import (
    STATUS_AMBIGUOUS_TARGET,
    STATUS_OK,
    context,
    normalise_projection,
)
from axiom_mcp.query.model import (
    Graph,
    LimitRejected,
    TargetNotFound,
    graph_from_documents,
)

COVERAGE = {
    "status": "partial",
    "input_files": 4,
    "processed_files": 4,
    "unresolved_references": 1,
    "unsupported_patterns": ["dynamic dispatch"],
}


def scope() -> Graph:
    """A -> B -> C, A -> D, plus an unresolved edge out of C and a same-named pair."""
    head = node_document("HeadAsync", qualified_name="App.HeadAsync", start_line=1, end_line=4)
    middle = node_document(
        "MiddleAsync", qualified_name="App.MiddleAsync", start_line=6, end_line=9
    )
    tail = node_document("TailAsync", qualified_name="App.TailAsync", start_line=11, end_line=14)
    branch = node_document(
        "BranchAsync", qualified_name="App.BranchAsync", start_line=16, end_line=19
    )
    twin = node_document("TwinAsync", qualified_name="App.TwinAsync", start_line=21, end_line=24)
    other_twin = node_document(
        "TwinAsync", qualified_name="Other.TwinAsync", start_line=26, end_line=29
    )
    edges = (
        edge_document(head["id"], target_id=middle["id"]),
        edge_document(middle["id"], target_id=tail["id"]),
        edge_document(head["id"], target_id=branch["id"], kind="REFERENCES"),
        edge_document(tail["id"], resolution="unresolved", unresolved_target="Unknown.Sink"),
    )
    return graph_from_documents(
        "auth-api",
        generation_id("c019"),
        nodes=(head, middle, tail, branch, twin, other_twin),
        edges=edges,
        coverage=COVERAGE,
    )


def test_depth_is_honoured_literally() -> None:
    """AC1: depth 0, 1 and 2 return the neighbourhood of exactly that many hops."""
    pinned = scope()
    alone = context(pinned, "App.HeadAsync", depth=0)
    assert alone.visited == 1
    assert alone.edges == ()
    assert alone.depth_reached == 0

    one_hop = context(pinned, "App.HeadAsync", depth=1)
    assert one_hop.visited == 3
    assert one_hop.depth_reached == 1
    assert one_hop.truncated is False

    two_hops = context(pinned, "App.HeadAsync", depth=2)
    assert two_hops.visited == 4
    assert two_hops.depth_reached == 2
    assert two_hops.status == STATUS_OK
    assert {edge["kind"] for edge in two_hops.edges} == {"CALLS", "REFERENCES"}


def test_the_projection_decides_which_sections_appear() -> None:
    """AC1: identity alone, then each requested section, and nothing else."""
    pinned = scope()
    identity_only = context(pinned, "App.HeadAsync", depth=1, projection=["identity"])
    assert identity_only.projection == ("identity",)
    head = next(node for node in identity_only.nodes if node["name"] == "HeadAsync")
    assert set(head) == {
        "id",
        "project_id",
        "kind",
        "name",
        "qualified_name",
        "language",
        "identity_quality",
    }
    assert "source" not in head

    located = context(pinned, "App.HeadAsync", depth=1)
    assert located.projection == ("identity", "source_locations")
    head = next(node for node in located.nodes if node["name"] == "HeadAsync")
    assert head["source"] == {
        "file": "src/Auth.Api/AuthController.cs",
        "start_line": 1,
        "end_line": 4,
    }

    related = context(pinned, "App.HeadAsync", depth=1, projection=["identity", "relations"])
    head = next(node for node in related.nodes if node["name"] == "HeadAsync")
    assert sorted(head["relations"]["outgoing"]) == sorted(
        edge["id"] for edge in related.edges if edge["source_id"] == head["id"]
    )
    assert head["relations"]["incoming"] == []

    covered = context(pinned, "App.HeadAsync", depth=1, projection=["identity", "coverage"])
    head = next(node for node in covered.nodes if node["name"] == "HeadAsync")
    assert head["coverage"] == COVERAGE


def test_a_budget_that_stops_the_expansion_is_recorded() -> None:
    """AC1 boundary: a capped neighbourhood reports ``truncated`` and the budget that capped it."""
    capped = context(scope(), "App.HeadAsync", depth=2, max_nodes=2)
    assert capped.visited == 2
    assert capped.truncated is True
    assert capped.reasons == ("max_nodes",)
    assert capped.frontier, "the nodes where expansion stopped are named"
    assert capped.depth_reached < 2

    edged = context(scope(), "App.HeadAsync", depth=2, max_edges=0)
    assert edged.visited == 1
    assert edged.edges == ()
    assert edged.truncated is True
    assert edged.reasons == ("max_edges",)

    complete = context(scope(), "App.HeadAsync", depth=2, max_nodes=10, max_edges=10)
    assert complete.truncated is False
    assert complete.reasons == ()
    assert complete.frontier == ()


def test_an_unresolved_edge_is_reported_as_incomplete_scope() -> None:
    """Boundary: an unresolved edge is not followed and not hidden."""
    result = context(scope(), "App.HeadAsync", depth=3)
    assert result.truncated is False, "an unresolved edge is not a budget truncation"
    assert any(warning.startswith("unresolved_edges:") for warning in result.warnings)


def test_an_ambiguous_target_returns_candidates_without_a_neighbourhood() -> None:
    """Negative: two symbols share the name, so no neighbourhood is invented."""
    result = context(scope(), "TwinAsync")
    assert result.status == STATUS_AMBIGUOUS_TARGET
    assert result.target is None
    assert len(result.candidates) == 2
    assert result.nodes == ()
    assert result.edges == ()
    assert result.truncated is False
    assert any(warning.startswith("ambiguous_target:") for warning in result.warnings)


def test_an_unknown_target_and_an_out_of_range_depth_are_refused() -> None:
    """Negative and boundary: nothing is guessed and nothing is clamped."""
    with pytest.raises(TargetNotFound):
        context(scope(), "DoesNotExistAsync")
    with pytest.raises(LimitRejected):
        context(scope(), "App.HeadAsync", depth=9)
    with pytest.raises(LimitRejected):
        context(scope(), "App.HeadAsync", projection=["everything"])
    with pytest.raises(LimitRejected):
        normalise_projection([])
