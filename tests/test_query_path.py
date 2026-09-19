"""C-023: a found path is real pinned edges, and "ran out of budget" is not "no path".

AC1 has two halves:

* positive - a shortest path is returned in order, and every edge on it is a pinned edge whose
  resolution connects consecutive nodes. A shorter path is chosen over a longer one.
* boundary - when no path is found, ``budget_exhausted`` (a node/edge budget stopped the search) and
  ``proven_absent`` (the frontier emptied without being cut) are different answers. A caller must
  not read the first as "there is no path".

The negative cases are a direction that rules the path out, a depth bound that stops short of a
longer path, an unresolved reference that could supply the missing hop, and an ambiguous endpoint.
"""

from __future__ import annotations

import pytest
from test_query_support import edge_document, generation_id, node_document

from axiom_mcp.query.model import GraphSet, LimitRejected, graph_from_documents
from axiom_mcp.query.path import STATUS_AMBIGUOUS_TARGET, path

COMPLETE = {"status": "complete"}


def linear_scope() -> GraphSet:
    """A -> B -> C -> D (CALLS) with a direct A -> D seen only at depth 1 through a shortcut."""
    a = node_document("Async", qualified_name="App.Async")
    b = node_document("Bsync", qualified_name="App.Bsync")
    c = node_document("Csync", qualified_name="App.Csync")
    d = node_document("Dsync", qualified_name="App.Dsync")
    edges = (
        edge_document(a["id"], target_id=b["id"], kind="CALLS"),
        edge_document(b["id"], target_id=c["id"], kind="CALLS"),
        edge_document(c["id"], target_id=d["id"], kind="CALLS"),
    )
    return GraphSet(
        (
            graph_from_documents(
                "auth-api",
                generation_id("c023-linear"),
                nodes=(a, b, c, d),
                edges=edges,
                coverage=COMPLETE,
            ),
        )
    )


def shortcut_scope() -> GraphSet:
    """A -> B -> C and A -> C, so the shortest A..C path is the single hop."""
    a = node_document("Async", qualified_name="App.Async")
    b = node_document("Bsync", qualified_name="App.Bsync")
    c = node_document("Csync", qualified_name="App.Csync")
    short = edge_document(a["id"], target_id=c["id"], kind="CALLS")
    edges = (
        edge_document(a["id"], target_id=b["id"], kind="CALLS"),
        edge_document(b["id"], target_id=c["id"], kind="CALLS"),
        short,
    )
    return GraphSet(
        (
            graph_from_documents(
                "auth-api",
                generation_id("c023-short"),
                nodes=(a, b, c),
                edges=edges,
                coverage=COMPLETE,
            ),
        )
    )


def test_a_found_path_is_a_chain_of_valid_pinned_edges() -> None:
    """AC1 positive: the path is ordered, and every edge is a real pinned edge of the scope."""
    pinned = linear_scope()
    result = path(pinned, "App.Async", "App.Dsync", depth=3)

    assert result.found is True
    assert result.length == 3
    assert [node["name"] for node in result.nodes] == ["Async", "Bsync", "Csync", "Dsync"]

    pinned_edges = {edge.id: edge for edge in pinned.edges()}
    previous = result.target
    for edge in result.edges:
        real = pinned_edges[edge["id"]]
        assert real.resolved is True
        assert edge["source_id"] == previous, "each edge continues the chain"
        previous = edge["target_id"]
    assert previous == result.target_to


def test_the_shortest_path_wins() -> None:
    """AC1 positive: a one-hop path is not reported as the two-hop path that also exists."""
    result = path(shortcut_scope(), "App.Async", "App.Csync", depth=2)

    assert result.found is True
    assert result.length == 1
    assert [node["name"] for node in result.nodes] == ["Async", "Csync"]


def test_a_budget_stop_is_not_a_proven_absence() -> None:
    """AC1 boundary: a stopped search is 'not found, not disproved', never 'no path'."""
    capped = path(linear_scope(), "App.Async", "App.Dsync", depth=3, max_edges=1)

    assert capped.found is False
    assert capped.budget_exhausted is True
    assert capped.proven_absent is False
    assert "budget_exhausted" in capped.incomplete_reasons
    assert any(warning.startswith("budget_exhausted:") for warning in capped.warnings)


def test_a_completed_empty_search_proves_absence_within_the_bound() -> None:
    """AC1 boundary: only a search that finished without being cut may say 'no path'."""
    result = path(linear_scope(), "App.Async", "App.Dsync", depth=3, max_nodes=50, max_edges=50)
    assert result.found is True

    # Reverse it: D does not reach A in the outgoing direction.
    backward = path(linear_scope(), "App.Dsync", "App.Async", depth=3, direction="outgoing")
    assert backward.found is False
    assert backward.budget_exhausted is False
    assert backward.proven_absent is True
    assert backward.incomplete_reasons == ()
    assert any(warning.startswith("no_path:") for warning in backward.warnings)


def test_a_depth_bound_makes_absence_provisional() -> None:
    """Boundary: stopping at the depth bound is not the same as an emptied frontier."""
    short = path(linear_scope(), "App.Async", "App.Dsync", depth=1)

    assert short.found is False
    assert short.proven_absent is False
    assert "depth_limited" in short.incomplete_reasons


def test_an_unresolved_reference_makes_absence_provisional() -> None:
    """Boundary: a reference the generation could not resolve could be the missing hop."""
    a = node_document("Async", qualified_name="App.Async")
    b = node_document("Bsync", qualified_name="App.Bsync")
    legacy = node_document("Legacy", qualified_name="Legacy.Node")
    unknown = edge_document(
        a["id"], kind="REFERENCES", resolution="unresolved", unresolved_target="App.Bsync"
    )
    scope = GraphSet(
        (
            graph_from_documents(
                "auth-api",
                generation_id("c023-unresolved"),
                nodes=(a, b, legacy),
                edges=(unknown,),
                coverage=COMPLETE,
            ),
        )
    )
    result = path(scope, "App.Async", "App.Bsync", depth=2)

    assert result.found is False
    assert result.proven_absent is False
    assert "unresolved_edges" in result.incomplete_reasons


def test_direction_rules_out_the_opposite_edge() -> None:
    """Negative: an outgoing-only search does not follow an edge backwards."""
    forward = path(linear_scope(), "App.Async", "App.Dsync", depth=3, direction="outgoing")
    assert forward.found is True

    backward = path(linear_scope(), "App.Dsync", "App.Async", depth=3, direction="outgoing")
    assert backward.found is False
    assert backward.proven_absent is True

    both = path(linear_scope(), "App.Dsync", "App.Async", depth=3, direction="both")
    assert both.found is True


def test_an_ambiguous_endpoint_returns_candidates() -> None:
    """Negative: an ambiguous endpoint yields candidates and no invented path."""
    one = node_document("TwinAsync", qualified_name="App.TwinAsync")
    two = node_document("TwinAsync", qualified_name="Other.TwinAsync")
    scope = GraphSet(
        (graph_from_documents("auth-api", generation_id("c023-twin"), nodes=(one, two), edges=()),)
    )
    result = path(scope, "TwinAsync", "TwinAsync")

    assert result.status == STATUS_AMBIGUOUS_TARGET
    assert len(result.candidates) == 2
    assert result.found is False
    assert result.proven_absent is False


def test_an_out_of_range_bound_is_rejected_not_clamped() -> None:
    """Boundary: a bound the caller did not choose is a validation error, never a silent clamp."""
    with pytest.raises(LimitRejected):
        path(linear_scope(), "App.Async", "App.Dsync", depth=9)
    with pytest.raises(LimitRejected):
        path(linear_scope(), "App.Async", "App.Dsync", edge_kinds=["NOT_A_KIND"])
    with pytest.raises(LimitRejected):
        path(linear_scope(), "App.Async", "App.Dsync", direction="sideways")


def test_a_node_is_zero_hops_from_itself() -> None:
    """Boundary: the trivial path is a real answer, not an empty or missing one."""
    result = path(linear_scope(), "App.Async", "App.Async", depth=0)

    assert result.found is True
    assert result.length == 0
    assert [node["name"] for node in result.nodes] == ["Async"]
