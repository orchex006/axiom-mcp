"""C-022: the reverse closure is conservative, and a bounded or partial view is never exhaustive.

AC1 has two halves:

* positive - the closure is what reaches the target, split into ``proven`` (exact/annotated edges)
  and ``potential`` (everything else, kept but labelled). A node reached only through an inferred
  edge is in the closure yet not proven, which is the "conservative" part.
* boundary - an answer is ``exhaustive`` only when nothing cut it. A depth bound, a node/edge
  budget, an unpinned member, a project whose pinned coverage is not complete, and an unresolved
  edge that names a node in the closure each force ``exhaustive`` false with a named reason. This
  is the "a bounded traversal is not labelled exhaustive" half.

The negative cases are a node the target *depends on* (outgoing, so not impacted by changing the
target) and an ambiguous selector (candidates, no closure).
"""

from __future__ import annotations

import pytest
from test_query_support import edge_document, generation_id, node_document

from axiom_mcp.query.impact import STATUS_AMBIGUOUS_TARGET, impact
from axiom_mcp.query.model import GraphSet, LimitRejected, graph_from_documents

COMPLETE = {"status": "complete"}


def chain_scope(
    *, coverage: dict | None = COMPLETE, middle_resolution: str = "exact_static"
) -> GraphSet:
    """leaf <- middle <- root, plus a node the leaf *calls* (outgoing) and an unresolved name."""
    leaf = node_document("LeafAsync", qualified_name="App.LeafAsync")
    middle = node_document("MiddleAsync", qualified_name="App.MiddleAsync")
    root = node_document("RootAsync", qualified_name="App.RootAsync")
    store = node_document("Store", qualified_name="App.Store", kind="DataStore")
    edges = (
        edge_document(
            middle["id"], target_id=leaf["id"], kind="CALLS", resolution=middle_resolution
        ),
        edge_document(root["id"], target_id=middle["id"], kind="CALLS"),
        edge_document(leaf["id"], target_id=store["id"], kind="CALLS"),
    )
    return GraphSet(
        (
            graph_from_documents(
                "auth-api",
                generation_id("c022"),
                nodes=(leaf, middle, root, store),
                edges=edges,
                coverage=coverage,
            ),
        )
    )


def names(result) -> list[str]:
    return sorted(node["name"] for node in result.nodes)


def test_the_reverse_closure_lists_what_reaches_the_target() -> None:
    """AC1 positive: the closure is the reverse direction, and proven when the edges are exact."""
    result = impact(chain_scope(), "App.LeafAsync", depth=2)

    assert names(result) == ["MiddleAsync", "RootAsync"]
    assert result.proven == tuple(sorted(node["id"] for node in result.nodes))
    assert result.potential == ()
    assert result.direction == "incoming"
    assert result.exhaustive is True
    assert result.incomplete_reasons == ()


def test_an_inferred_edge_is_included_but_not_proven() -> None:
    """AC1 positive: a conservative closure keeps the inferred node and labels it potential."""
    result = impact(chain_scope(middle_resolution="inferred_static"), "App.LeafAsync", depth=2)

    assert names(result) == ["MiddleAsync", "RootAsync"]
    assert result.potential == tuple(sorted(node["id"] for node in result.nodes)), (
        "every node behind the inferred edge is potential, including the root behind middle"
    )
    assert result.proven == ()
    assert not set(result.potential) & set(result.proven)
    assert result.exhaustive is True, "an inferred-but-resolved edge does not make scope partial"


def test_a_depth_bound_is_not_exhaustive() -> None:
    """AC1 boundary: the traversal is bounded by depth and must not be read as the whole answer."""
    one = impact(chain_scope(), "App.LeafAsync", depth=1)
    assert names(one) == ["MiddleAsync"]
    assert one.exhaustive is False
    assert "depth_limited" in one.incomplete_reasons

    two = impact(chain_scope(), "App.LeafAsync", depth=2)
    assert two.exhaustive is True, "nothing reaches the target beyond depth 2 here"


def test_a_budget_stop_is_not_exhaustive() -> None:
    """AC1 boundary: a budget that stops the walk is named, not hidden."""
    capped = impact(chain_scope(), "App.LeafAsync", depth=8, max_edges=0)

    assert capped.nodes == ()
    assert capped.truncated is True
    assert capped.exhaustive is False
    assert "truncated" in capped.incomplete_reasons

    with pytest.raises(LimitRejected):
        impact(chain_scope(), "App.LeafAsync", depth=9)


def test_partial_coverage_is_not_exhaustive() -> None:
    """AC1 boundary: a project whose pinned coverage is not complete cannot prove a closure."""
    result = impact(chain_scope(coverage={"status": "partial"}), "App.LeafAsync", depth=8)

    assert result.coverage == {"auth-api": "partial"}
    assert result.exhaustive is False
    assert "coverage_not_complete" in result.incomplete_reasons
    assert any(warning.startswith("coverage:") for warning in result.warnings)


def test_an_unpinned_member_is_not_exhaustive() -> None:
    """AC1 boundary: a member the caller named but the scope does not pin is reported."""
    leaf = node_document("LeafAsync", qualified_name="App.LeafAsync")
    partial = GraphSet(
        (graph_from_documents("auth-api", generation_id("c022-partial"), nodes=(leaf,), edges=()),),
        missing_projects=("web-app",),
    )
    result = impact(partial, "App.LeafAsync", depth=1)

    assert result.nodes == ()
    assert result.missing_projects == ("web-app",)
    assert result.exhaustive is False
    assert "missing_members" in result.incomplete_reasons


def test_an_unresolved_reference_that_names_a_closure_node_is_reported() -> None:
    """AC1 boundary: an unresolved edge that may add impact is reported and blocks exhaustive."""
    leaf = node_document("LeafAsync", qualified_name="App.LeafAsync")
    middle = node_document("MiddleAsync", qualified_name="App.MiddleAsync")
    legacy = node_document("LegacyCaller", qualified_name="Legacy.Caller")
    calls = edge_document(middle["id"], target_id=leaf["id"], kind="CALLS")
    unknown = edge_document(
        legacy["id"],
        kind="REFERENCES",
        resolution="unresolved",
        unresolved_target="App.MiddleAsync",
    )
    scope = GraphSet(
        (
            graph_from_documents(
                "auth-api",
                generation_id("c022-unresolved"),
                nodes=(leaf, middle, legacy),
                edges=(calls, unknown),
                coverage=COMPLETE,
            ),
        )
    )
    result = impact(scope, "App.LeafAsync", depth=1)

    assert [edge["resolution"] for edge in result.unresolved] == ["unresolved"]
    assert result.exhaustive is False
    assert "unresolved_edges" in result.incomplete_reasons


def test_what_the_target_depends_on_is_not_impact() -> None:
    """Negative: impact is the reverse direction, so the target's own callee is not included."""
    result = impact(chain_scope(), "App.LeafAsync", depth=2)

    assert "Store" not in names(result)
    assert result.nodes and all(node["name"] != "Store" for node in result.nodes)


def test_an_ambiguous_selector_returns_candidates_not_a_closure() -> None:
    """Negative: an ambiguous target yields candidates and a definitely non-exhaustive answer."""
    one = node_document("TwinAsync", qualified_name="App.TwinAsync")
    two = node_document("TwinAsync", qualified_name="Other.TwinAsync")
    scope = GraphSet(
        (graph_from_documents("auth-api", generation_id("c022-twin"), nodes=(one, two), edges=()),)
    )
    result = impact(scope, "TwinAsync")

    assert result.status == STATUS_AMBIGUOUS_TARGET
    assert len(result.candidates) == 2
    assert result.nodes == ()
    assert result.exhaustive is False
