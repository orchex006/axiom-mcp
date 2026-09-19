"""C-020: edge-kind and direction filters compose, and a cycle terminates inside the bounds.

AC1 has two halves:

* positive - ``direction`` selects which side of the target an edge must touch
  (``outgoing`` only edges whose source is the target, ``incoming`` only edges whose target is it,
  ``both`` the union), ``edge_kinds`` keeps only the requested kinds, and the two compose: a
  caller asking for ``incoming`` + ``CALLS`` never receives an outgoing ``REFERENCES`` edge.
  ``dependencies`` is the outgoing dependency-kind case of the same traversal.
* boundary - the graph below ends in a cycle, and a walk of maximum depth over it terminates:
  every node is expanded once and every edge is reported once, so depth cannot loop.

The negative and boundary cases are a kind filter that matches nothing (no hops, no error), an
unresolved edge (reported, never followed, never hidden), an ambiguous target, and budgets that
stop expansion with ``truncated`` plus the budget's name.
"""

from __future__ import annotations

from test_query_support import edge_document, generation_id, node_document

from axiom_mcp.query.model import Graph, LimitRejected, graph_from_documents
from axiom_mcp.query.neighbors import DEPENDENCY_KINDS, dependencies, neighbors


def cycle_scope() -> Graph:
    """a -> b -> c -> a, plus a -> d (REFERENCES) and an unresolved edge out of d."""
    a = node_document("AlphaAsync", qualified_name="App.AlphaAsync")
    b = node_document("BetaAsync", qualified_name="App.BetaAsync")
    c = node_document("GammaAsync", qualified_name="App.GammaAsync")
    d = node_document("DeltaStore", qualified_name="App.DeltaStore", kind="Class")
    edges = (
        edge_document(a["id"], target_id=b["id"]),
        edge_document(b["id"], target_id=c["id"]),
        edge_document(c["id"], target_id=a["id"]),
        edge_document(a["id"], target_id=d["id"], kind="REFERENCES"),
        edge_document(d["id"], kind="READS", resolution="unresolved", unresolved_target="Db.Table"),
    )
    return graph_from_documents("auth-api", generation_id("c020"), nodes=(a, b, c, d), edges=edges)


def scope_ids() -> dict[str, str]:
    pinned = cycle_scope()
    return {node.name: node.id for node in pinned.nodes}


def test_a_cycle_terminates_within_the_bounds() -> None:
    """AC1 boundary: the cycle is walked once and the walk ends, with no loop and no truncation."""
    result = neighbors(cycle_scope(), "App.AlphaAsync", depth=8, max_nodes=50, max_edges=50)
    assert result.visited == 4
    assert result.truncated is False
    assert result.depth_reached == 1, "every node is one hop from the target; the cycle adds none"
    assert len({edge["id"] for edge in result.edges}) == len(result.edges)
    assert result.per_kind == {"CALLS": 3, "REFERENCES": 1, "READS": 1}


def test_direction_and_edge_kind_compose() -> None:
    """AC1: the filters apply to the edge, and they apply together."""
    pinned = cycle_scope()
    ids = scope_ids()

    outgoing = neighbors(pinned, "App.AlphaAsync", depth=1, direction="outgoing")
    assert {edge["kind"] for edge in outgoing.edges} == {"CALLS", "REFERENCES"}
    assert all(edge["source_id"] == ids["AlphaAsync"] for edge in outgoing.edges)

    incoming = neighbors(pinned, "App.AlphaAsync", depth=1, direction="incoming")
    assert {edge["kind"] for edge in incoming.edges} == {"CALLS"}
    assert all(edge["target_id"] == ids["AlphaAsync"] for edge in incoming.edges)

    both = neighbors(pinned, "App.AlphaAsync", depth=1, direction="both")
    assert len(both.edges) == 3

    only_references = neighbors(
        pinned, "App.AlphaAsync", depth=2, direction="both", edge_kinds=["REFERENCES"]
    )
    assert {edge["kind"] for edge in only_references.edges} == {"REFERENCES"}
    assert only_references.visited == 2, "a filtered-out kind must not contribute a hop"

    dependencies_only = dependencies(pinned, "App.AlphaAsync", depth=2)
    assert dependencies_only.direction == "outgoing"
    assert set(dependencies_only.edge_kinds) == set(DEPENDENCY_KINDS)
    assert all(edge["target_id"] != ids["AlphaAsync"] for edge in dependencies_only.edges), (
        "dependencies are what the target reaches, never what reaches it"
    )
    first_hop = dependencies(pinned, "App.AlphaAsync", depth=1)
    assert all(edge["source_id"] == ids["AlphaAsync"] for edge in first_hop.edges)


def test_an_unresolved_edge_is_reported_and_not_followed() -> None:
    """Boundary: the unresolved edge is in the answer, and it is not a hop."""
    result = neighbors(cycle_scope(), "App.DeltaStore", depth=1, direction="outgoing")
    assert [edge["resolution"] for edge in result.unresolved] == ["unresolved"]
    assert result.edges == ()
    assert result.visited == 1
    assert result.truncated is False
    assert any(warning.startswith("unresolved_edges:") for warning in result.warnings)


def test_a_kind_filter_that_matches_nothing_is_not_an_error() -> None:
    """Negative: an empty neighbourhood is a real answer, not a failure and not a truncation."""
    result = neighbors(cycle_scope(), "App.DeltaStore", depth=2, edge_kinds=["INHERITS"])
    assert result.edges == ()
    assert result.visited == 1
    assert result.truncated is False
    assert result.per_kind == {}


def test_an_ambiguous_target_returns_candidates() -> None:
    """Negative: the same-name rule as search and context applies here too."""
    twin = node_document("TwinAsync", qualified_name="App.TwinAsync")
    other = node_document("TwinAsync", qualified_name="Other.TwinAsync")
    pinned = graph_from_documents(
        "auth-api", generation_id("c020-twins"), nodes=(twin, other), edges=()
    )
    result = neighbors(pinned, "TwinAsync")
    assert result.status == "ambiguous_target"
    assert len(result.candidates) == 2
    assert result.edges == ()


def test_budgets_stop_expansion_and_say_so() -> None:
    """Boundary: a capped traversal is truncated with a named reason, and bad bounds are refused."""
    capped = neighbors(cycle_scope(), "App.AlphaAsync", depth=8, max_nodes=2)
    assert capped.visited == 2
    assert capped.truncated is True
    assert capped.reasons == ("max_nodes",)

    no_edges = neighbors(cycle_scope(), "App.AlphaAsync", depth=8, max_edges=0)
    assert no_edges.edges == ()
    assert no_edges.truncated is True
    assert no_edges.reasons == ("max_edges",)

    with __import__("pytest").raises(LimitRejected):
        neighbors(cycle_scope(), "App.AlphaAsync", depth=9)
    with __import__("pytest").raises(LimitRejected):
        neighbors(cycle_scope(), "App.AlphaAsync", edge_kinds=["NOT_A_KIND"])
