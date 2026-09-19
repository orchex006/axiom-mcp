"""C-024: added/removed/modified facts between compatible generations, and explicit refusals.

AC1 has two halves:

* positive - two generations of the same project and schema are compared fact by fact: a new node
  is ``added``, a vanished one is ``removed``, and a node whose fields moved but whose pinned id
  stayed is ``modified`` with both the previous and the current fact. Edges are compared the same
  way, and the answer names the exact head and baseline generations it compared.
* boundary - the comparison is refused, explicitly, rather than guessed. A missing baseline, two
  sides that do not pin the same projects, an unknown schema major and two different schema majors
  each produce their own status and a warning, with no diff to misread.

The negative cases are a rename (a new id, so an add plus a remove - never a silent modification)
and an unchanged generation (an empty, comparable diff).
"""

from __future__ import annotations

from test_query_support import edge_document, generation_id, node_document

from axiom_mcp.query.changes import (
    STATUS_INCOMPATIBLE_SCHEMA,
    STATUS_INCOMPATIBLE_SCOPE,
    STATUS_MISSING_BASELINE,
    STATUS_OK,
    STATUS_UNKNOWN_SCHEMA,
    changes,
)
from axiom_mcp.query.model import GraphSet, graph_from_documents

SCHEMA = 1


def scope(
    seed: str,
    *,
    project: str = "auth-api",
    nodes: tuple,
    edges: tuple = (),
    schema: int | None = SCHEMA,
) -> GraphSet:
    return GraphSet(
        (
            graph_from_documents(
                project,
                generation_id(seed),
                nodes=nodes,
                edges=edges,
                schema_major=schema,
            ),
        )
    )


def names(facts) -> list[str]:
    return sorted(fact["name"] for fact in facts)


def test_added_removed_and_modified_are_reported_per_project() -> None:
    """AC1 positive: each fact lands in exactly one bucket, and a moved field is modified."""
    kept = node_document("Kept", qualified_name="App.Kept", start_line=1, end_line=2)
    moved = node_document("Moved", qualified_name="App.Moved", start_line=1, end_line=2)
    dropped = node_document("Dropped", qualified_name="App.Dropped")
    added = node_document("Added", qualified_name="App.Added")

    baseline = scope("c024-base", nodes=(kept, moved, dropped))
    head = scope(
        "c024-head",
        nodes=(
            kept,
            node_document("Moved", qualified_name="App.Moved", start_line=9, end_line=12),
            added,
        ),
    )
    result = changes(head, baseline)

    assert result.status == STATUS_OK
    assert result.comparable is True
    assert names(result.projects["auth-api"]["nodes"]["added"]) == ["Added"]
    assert names(result.projects["auth-api"]["nodes"]["removed"]) == ["Dropped"]
    modified = result.projects["auth-api"]["nodes"]["modified"]
    assert [item["id"] for item in modified] == [moved["id"]]
    assert modified[0]["before"]["source"]["start_line"] == 1
    assert modified[0]["after"]["source"]["start_line"] == 9
    assert result.totals["added_nodes"] == 1
    assert result.totals["removed_nodes"] == 1
    assert result.totals["modified_nodes"] == 1
    assert result.head_generations == (("auth-api", generation_id("c024-head")),)
    assert result.baseline_generations == (("auth-api", generation_id("c024-base")),)


def test_edge_facts_are_compared_too() -> None:
    """AC1 positive: an edge that appears between the generations is an added fact."""
    a = node_document("Async", qualified_name="App.Async")
    b = node_document("Bsync", qualified_name="App.Bsync")
    call = edge_document(a["id"], target_id=b["id"], kind="CALLS")

    baseline = scope("c024-edges-base", nodes=(a, b))
    head = scope("c024-edges-head", nodes=(a, b), edges=(call,))
    result = changes(head, baseline)

    assert [edge["kind"] for edge in result.projects["auth-api"]["edges"]["added"]] == ["CALLS"]
    assert result.projects["auth-api"]["edges"]["removed"] == ()
    assert result.totals["added_edges"] == 1


def test_a_missing_baseline_is_explicit() -> None:
    """AC1 boundary: no baseline is its own status, never an implicit comparison."""
    a = node_document("Async", qualified_name="App.Async")
    result = changes(scope("c024-nobase", nodes=(a,)), None)

    assert result.status == STATUS_MISSING_BASELINE
    assert result.comparable is False
    assert result.projects == {}
    assert any(warning.startswith("missing_baseline:") for warning in result.warnings)


def test_unknown_schema_is_explicit() -> None:
    """AC1 boundary: an undeclared schema major refuses a field-by-field comparison."""
    a = node_document("Async", qualified_name="App.Async")
    head = scope("c024-unknown-head", nodes=(a,), schema=None)
    baseline = scope("c024-unknown-base", nodes=(a,), schema=None)
    result = changes(head, baseline)

    assert result.status == STATUS_UNKNOWN_SCHEMA
    assert result.comparable is False
    assert result.schema == {"head": None, "baseline": None}
    assert result.projects == {}


def test_different_schema_majors_are_incompatible() -> None:
    """AC1 boundary: two different schema majors are not comparable, by name."""
    a = node_document("Async", qualified_name="App.Async")
    result = changes(
        scope("c024-major-head", nodes=(a,), schema=2),
        scope("c024-major-base", nodes=(a,), schema=1),
    )

    assert result.status == STATUS_INCOMPATIBLE_SCHEMA
    assert result.comparable is False
    assert result.schema == {"head": 2, "baseline": 1}


def test_different_project_sets_are_incompatible() -> None:
    """AC1 boundary: a diff across different member sets is not a fact."""
    a = node_document("Async", project_id="auth-api", qualified_name="App.Async")
    b = node_document("Async", project_id="web-app", qualified_name="App.Async")
    result = changes(
        scope("c024-scope-head", nodes=(a,)),
        scope("c024-scope-base", nodes=(b,), project="web-app"),
    )

    assert result.status == STATUS_INCOMPATIBLE_SCOPE
    assert result.comparable is False
    assert any(warning.startswith("incompatible_scope:") for warning in result.warnings)


def test_a_rename_is_an_add_plus_a_remove_not_a_modification() -> None:
    """Negative: the pinned id is the identity, so a renamed symbol is not a silent edit."""
    before = node_document("OldName", qualified_name="App.OldName")
    after = node_document("NewName", qualified_name="App.NewName")
    result = changes(
        scope("c024-rename-head", nodes=(after,)), scope("c024-rename-base", nodes=(before,))
    )

    assert names(result.projects["auth-api"]["nodes"]["added"]) == ["NewName"]
    assert names(result.projects["auth-api"]["nodes"]["removed"]) == ["OldName"]
    assert result.projects["auth-api"]["nodes"]["modified"] == ()


def test_an_unchanged_generation_is_an_empty_comparable_diff() -> None:
    """Negative: identical generations are comparable with nothing to report, not an error."""
    a = node_document("Async", qualified_name="App.Async")
    result = changes(scope("c024-same-head", nodes=(a,)), scope("c024-same-base", nodes=(a,)))

    assert result.status == STATUS_OK
    assert result.comparable is True
    assert result.totals == {
        "added_nodes": 0,
        "removed_nodes": 0,
        "modified_nodes": 0,
        "added_edges": 0,
        "removed_edges": 0,
        "modified_edges": 0,
    }
