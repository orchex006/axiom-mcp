"""C-024: compare two pinned generations into added / removed / modified facts.

``repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md`` section 5 makes ``baseline_catalog_generation_id``
required for ``changes``: the question is always "what changed since *that* generation", and a
comparison against an implicit "before" is not an answer. Section 7 adds the other half - an
unsupported major or a corrupt generation must be rejected, not read with a warning - so a diff is
only computed when both sides are known to be comparable:

* the compared sides are two pinned scopes (a head and a baseline), each carrying generation ids
  per project and an optional ``schema_major``. The comparison is refused, explicitly, when the
  baseline is missing, when the two sides do not pin the same projects, and when either side's
  schema major is unknown or the two differ. Refusing is the point: a diff across an unknown
  schema looks like a fact and is not one;
* when the sides are comparable, each project reports ``added``, ``removed`` and ``modified`` nodes
  (matched by pinned node id, so a rename is an add plus a remove, not a silent modification) and
  the same for edges. ``modified`` carries both the previous and the current fact so a reader sees
  what actually moved.

Nothing here guesses a baseline. An answer always names the exact generations it compared.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from axiom_mcp.query.model import (
    Graph,
    GraphSet,
    as_graph_set,
    identifier_text,
)

STATUS_OK = "ok"
STATUS_MISSING_BASELINE = "missing_baseline"
STATUS_INCOMPATIBLE_SCOPE = "incompatible_scope"
STATUS_UNKNOWN_SCHEMA = "unknown_schema"
STATUS_INCOMPATIBLE_SCHEMA = "incompatible_schema"

_IDENTITY_FIELDS = ("id", "project_id")


def _fact(node_or_edge: Any) -> Mapping[str, Any]:
    """The comparable part of a node or edge fact: everything but its own identity and project."""
    document = node_or_edge.as_document()
    return {key: value for key, value in document.items() if key not in _IDENTITY_FIELDS}


def _serialise(fact: Any) -> Any:
    """Render a node/edge fact or an already-plain document; never a Python object repr."""
    if isinstance(fact, Mapping):
        return dict(fact)
    return fact.as_document()


def _diff_kind(head_items: Iterable[Any], baseline_items: Iterable[Any]) -> Mapping[str, Any]:
    head = {item.id: item for item in head_items}
    baseline = {item.id: item for item in baseline_items}
    added = tuple(_serialise(head[key]) for key in sorted(set(head) - set(baseline)))
    removed = tuple(_serialise(baseline[key]) for key in sorted(set(baseline) - set(head)))
    modified: list[Mapping[str, Any]] = []
    for key in sorted(set(head) & set(baseline)):
        before = _fact(baseline[key])
        after = _fact(head[key])
        if before != after:
            modified.append({"id": key, "before": before, "after": after})
    return {"added": added, "removed": removed, "modified": tuple(modified)}


@dataclass(frozen=True)
class ChangeResult:
    """The comparable diff between a head and a baseline, or why no diff was computed."""

    status: str
    operation: str
    comparable: bool
    head_generations: tuple[tuple[str, str], ...]
    baseline_generations: tuple[tuple[str, str], ...]
    schema: Mapping[str, int | None]
    projects: Mapping[str, Mapping[str, Any]]
    totals: Mapping[str, int]
    warnings: tuple[str, ...] = field(default=())

    def as_document(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "operation": self.operation,
            "comparable": self.comparable,
            "head_generations": [list(item) for item in self.head_generations],
            "baseline_generations": [list(item) for item in self.baseline_generations],
            "schema": dict(self.schema),
            "projects": {
                project: {kind: [dict(fact) for fact in facts] for kind, facts in change.items()}
                for project, change in self.projects.items()
            },
            "totals": dict(self.totals),
            "warnings": list(self.warnings),
        }


def changes(
    head: Graph | GraphSet | Iterable[Graph],
    baseline: Graph | GraphSet | Iterable[Graph] | None,
    *,
    project_ids: Sequence[str] | None = None,
) -> ChangeResult:
    """Compare ``head`` against ``baseline``, refusing to invent a comparison that is not valid."""
    head_scope = as_graph_set(head)
    if project_ids is not None:
        chosen = tuple(identifier_text(pid, what="project_id") for pid in project_ids)
        head_scope = GraphSet(head_scope.graph(pid) for pid in chosen)
    if baseline is None:
        return ChangeResult(
            status=STATUS_MISSING_BASELINE,
            operation="changes",
            comparable=False,
            head_generations=head_scope.generations(),
            baseline_generations=(),
            schema={"head": _schema_of(head_scope), "baseline": None},
            projects={},
            totals={},
            warnings=(
                "missing_baseline: changes requires a baseline generation to compare against; "
                "no diff was computed",
            ),
        )

    baseline_scope = as_graph_set(baseline)
    head_projects = head_scope.project_ids
    baseline_projects = baseline_scope.project_ids
    if head_projects != baseline_projects:
        return ChangeResult(
            status=STATUS_INCOMPATIBLE_SCOPE,
            operation="changes",
            comparable=False,
            head_generations=head_scope.generations(),
            baseline_generations=baseline_scope.generations(),
            schema={"head": _schema_of(head_scope), "baseline": _schema_of(baseline_scope)},
            projects={},
            totals={},
            warnings=(
                "incompatible_scope: the two generations do not pin the same projects "
                f"(head={', '.join(head_projects) or 'none'}, "
                f"baseline={', '.join(baseline_projects) or 'none'}); a diff across different "
                "member sets is not a fact",
            ),
        )

    head_schema = _schema_of(head_scope)
    baseline_schema = _schema_of(baseline_scope)
    if head_schema is None or baseline_schema is None:
        return ChangeResult(
            status=STATUS_UNKNOWN_SCHEMA,
            operation="changes",
            comparable=False,
            head_generations=head_scope.generations(),
            baseline_generations=baseline_scope.generations(),
            schema={"head": head_schema, "baseline": baseline_schema},
            projects={},
            totals={},
            warnings=(
                "unknown_schema: at least one side does not declare a schema major; the fact "
                "shapes of an unknown schema cannot be compared field by field",
            ),
        )
    if head_schema != baseline_schema:
        return ChangeResult(
            status=STATUS_INCOMPATIBLE_SCHEMA,
            operation="changes",
            comparable=False,
            head_generations=head_scope.generations(),
            baseline_generations=baseline_scope.generations(),
            schema={"head": head_schema, "baseline": baseline_schema},
            projects={},
            totals={},
            warnings=(
                f"incompatible_schema: head schema major {head_schema} does not match baseline "
                f"{baseline_schema}; a cross-major diff is not comparable",
            ),
        )

    projects: dict[str, Mapping[str, Any]] = {}
    totals = {
        "added_nodes": 0,
        "removed_nodes": 0,
        "modified_nodes": 0,
        "added_edges": 0,
        "removed_edges": 0,
        "modified_edges": 0,
    }
    for project in head_projects:
        head_graph = head_scope.graph(project)
        baseline_graph = baseline_scope.graph(project)
        nodes = _diff_kind(head_graph.nodes, baseline_graph.nodes)
        edges = _diff_kind(head_graph.edges, baseline_graph.edges)
        projects[project] = {"nodes": nodes, "edges": edges}
        totals["added_nodes"] += len(nodes["added"])
        totals["removed_nodes"] += len(nodes["removed"])
        totals["modified_nodes"] += len(nodes["modified"])
        totals["added_edges"] += len(edges["added"])
        totals["removed_edges"] += len(edges["removed"])
        totals["modified_edges"] += len(edges["modified"])

    return ChangeResult(
        status=STATUS_OK,
        operation="changes",
        comparable=True,
        head_generations=head_scope.generations(),
        baseline_generations=baseline_scope.generations(),
        schema={"head": head_schema, "baseline": baseline_schema},
        projects=projects,
        totals=totals,
        warnings=(),
    )


def _schema_of(scope: GraphSet) -> int | None:
    """The shared schema major of every pinned member, or ``None`` when it is unknown."""
    majors = {graph.schema_major for graph in scope.graphs}
    if len(majors) != 1:
        return None
    return majors.pop()


__all__ = [
    "STATUS_INCOMPATIBLE_SCHEMA",
    "STATUS_INCOMPATIBLE_SCOPE",
    "STATUS_MISSING_BASELINE",
    "STATUS_OK",
    "STATUS_UNKNOWN_SCHEMA",
    "ChangeResult",
    "changes",
]
