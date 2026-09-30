"""C-022: a conservative, bounded impact closure that never claims to be exhaustive.

``repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md`` section 5 is explicit twice over: a traversal runs
under a visited set and hard budgets, and "partial impact is static potential impact, not proof of
runtime damage or of absence of other callers". A closure that returned a tidy node list would
invite the reader to treat it as the answer, so this operation separates three things:

* the **closure** - every pinned node that reaches the target through the requested edge kinds
  within the depth and budgets. It is incoming-only, because impact flows from the changed symbol
  back to what depends on it;
* the **proven** subset of that closure, reachable through edges whose ``resolution`` is exact or
  annotated (:data:`~axiom_mcp.query.model.STATIC_RESOLUTIONS`). Everything else is **potential**:
  it is included conservatively - a reader wants the wider net - but it is *labelled* as resting on
  inference, so nobody mistakes it for a proven dependency;
* the **incompleteness** of the whole answer. ``exhaustive`` is true only when nothing cut the view:
  no budget truncated the walk, no member the caller named is unpinned, no unresolved reference
  names a node in the closure, and every searched project's pinned coverage is ``complete``. Any
  one of those makes ``exhaustive`` false and names the reason, so a bounded traversal can never be
  read as exhaustive.

An unresolved edge that names a closure node is the sharpest case: it has no target id, so the
incoming walk cannot reach it, yet it may be exactly the impacted node that is missing. It is
reported in ``unresolved`` and forces ``exhaustive`` false rather than silently disappearing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from axiom_mcp.query.callers import CALLER_KINDS
from axiom_mcp.query.context import (
    DEFAULT_PROJECTION,
    normalise_projection,
    project_node,
)
from axiom_mcp.query.model import (
    DEFAULT_DEPTH,
    DEFAULT_MAX_EDGES,
    DEFAULT_MAX_NODES,
    EDGE_KINDS,
    MAX_DEPTH,
    MAX_EDGES,
    MAX_NODES,
    STATIC_RESOLUTIONS,
    Graph,
    GraphSet,
    TargetAmbiguous,
    as_graph_set,
    bounded_int,
    bounded_walk,
    choice_list,
    narrow_scope,
    unresolved_references,
)

STATUS_OK = "ok"
STATUS_AMBIGUOUS_TARGET = "ambiguous_target"

_INCOMPLETE_TRUNCATED = "truncated"
_INCOMPLETE_DEPTH = "depth_limited"
_INCOMPLETE_MISSING = "missing_members"
_INCOMPLETE_UNRESOLVED = "unresolved_edges"
_INCOMPLETE_COVERAGE = "coverage_not_complete"


@dataclass(frozen=True)
class ImpactResult:
    """The conservative reverse closure, split into proven and potential and labelled bounded."""

    target: str | None
    status: str
    operation: str
    direction: str
    edge_kinds: tuple[str, ...]
    depth: int
    nodes: tuple[Mapping[str, Any], ...]
    proven: tuple[str, ...]
    potential: tuple[str, ...]
    edges: tuple[Mapping[str, Any], ...]
    unresolved: tuple[Mapping[str, Any], ...]
    candidates: tuple[Mapping[str, Any], ...]
    per_kind: Mapping[str, int]
    per_project: Mapping[str, int]
    searched_projects: tuple[str, ...]
    missing_projects: tuple[str, ...]
    coverage: Mapping[str, str]
    visited: int
    depth_reached: int
    truncated: bool
    reasons: tuple[str, ...]
    frontier: tuple[str, ...]
    exhaustive: bool
    incomplete_reasons: tuple[str, ...]
    warnings: tuple[str, ...]

    def as_document(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "operation": self.operation,
            "target": self.target,
            "direction": self.direction,
            "edge_kinds": list(self.edge_kinds),
            "depth": self.depth,
            "depth_reached": self.depth_reached,
            "nodes": [dict(item) for item in self.nodes],
            "proven": list(self.proven),
            "potential": list(self.potential),
            "edges": [dict(item) for item in self.edges],
            "unresolved": [dict(item) for item in self.unresolved],
            "candidates": [dict(item) for item in self.candidates],
            "per_kind": dict(self.per_kind),
            "per_project": dict(self.per_project),
            "searched_projects": list(self.searched_projects),
            "missing_projects": list(self.missing_projects),
            "coverage": dict(self.coverage),
            "visited": self.visited,
            "truncated": self.truncated,
            "reasons": list(self.reasons),
            "frontier": list(self.frontier),
            "exhaustive": self.exhaustive,
            "incomplete_reasons": list(self.incomplete_reasons),
            "warnings": list(self.warnings),
        }


def _per_kind(edges: Iterable[Any]) -> Mapping[str, int]:
    counts: dict[str, int] = {}
    for edge in edges:
        counts[edge.kind] = counts.get(edge.kind, 0) + 1
    return counts


def impact(
    scope: Graph | GraphSet | Iterable[Graph],
    target: str,
    *,
    project_ids: Sequence[str] | None = None,
    depth: int | None = DEFAULT_DEPTH,
    edge_kinds: Sequence[str] | None = None,
    projection: Sequence[str] | None = DEFAULT_PROJECTION,
    max_nodes: int | None = None,
    max_edges: int | None = None,
) -> ImpactResult:
    """Return the bounded reverse closure of ``target``, labelled with how complete it is."""
    pinned = narrow_scope(as_graph_set(scope), project_ids)
    sections = normalise_projection(projection)
    hops = bounded_int(depth, "depth", low=0, high=MAX_DEPTH, default=DEFAULT_DEPTH)
    kinds = choice_list(edge_kinds, "edge_kinds", tuple(sorted(EDGE_KINDS)), default=CALLER_KINDS)
    node_budget = bounded_int(
        max_nodes, "max_nodes", low=1, high=MAX_NODES, default=DEFAULT_MAX_NODES
    )
    edge_budget = bounded_int(
        max_edges, "max_edges", low=0, high=MAX_EDGES, default=DEFAULT_MAX_EDGES
    )

    searched = pinned.searched_projects
    missing = pinned.missing_projects
    coverage = {pid: pinned.graph(pid).coverage_status for pid in searched}

    try:
        resolved = pinned.resolve(target)
    except TargetAmbiguous as ambiguous:
        return ImpactResult(
            target=None,
            status=STATUS_AMBIGUOUS_TARGET,
            operation="impact",
            direction="incoming",
            edge_kinds=tuple(kinds),
            depth=hops,
            nodes=(),
            proven=(),
            potential=(),
            edges=(),
            unresolved=(),
            candidates=tuple(
                project_node(pinned.node(node_id), sections)
                for node_id in sorted(ambiguous.candidate_ids)
            ),
            per_kind={},
            per_project={},
            searched_projects=searched,
            missing_projects=missing,
            coverage=coverage,
            visited=0,
            depth_reached=0,
            truncated=False,
            reasons=(),
            frontier=(),
            exhaustive=False,
            incomplete_reasons=("ambiguous_target",),
            warnings=(
                f"ambiguous_target: {len(ambiguous.candidate_ids)} pinned nodes match "
                f"{target!r}; candidates are returned instead of one pick",
            ),
        )

    walk = bounded_walk(
        pinned,
        (resolved.id,),
        depth=hops,
        direction="incoming",
        edge_kinds=tuple(kinds),
        max_nodes=node_budget,
        max_edges=edge_budget,
    )
    exact_walk = bounded_walk(
        pinned,
        (resolved.id,),
        depth=hops,
        direction="incoming",
        edge_kinds=tuple(kinds),
        resolutions=tuple(sorted(STATIC_RESOLUTIONS)),
        max_nodes=node_budget,
        max_edges=edge_budget,
    )

    # A depth-bounded walk must never read as exhaustive: probe one hop further (or, at the
    # maximum depth, admit that the bound cannot be shown to be the end) before saying so.
    if hops >= MAX_DEPTH:
        depth_limited = True
    else:
        deeper = bounded_walk(
            pinned,
            (resolved.id,),
            depth=hops + 1,
            direction="incoming",
            edge_kinds=tuple(kinds),
            max_nodes=node_budget,
            max_edges=edge_budget,
        )
        depth_limited = len(deeper.nodes) > len(walk.nodes)

    closure_nodes = tuple(node for node in walk.nodes if node.id != resolved.id)
    proven_ids = tuple(sorted(node.id for node in exact_walk.nodes if node.id != resolved.id))
    proven_set = set(proven_ids)
    potential_ids = tuple(sorted(node.id for node in closure_nodes if node.id not in proven_set))

    named_unresolved: dict[str, Any] = {}
    for node in (resolved, *closure_nodes):
        for edge in unresolved_references(pinned, node):
            named_unresolved.setdefault(edge.id, edge)
    unresolved_edges = (
        *walk.unresolved,
        *(named_unresolved[key] for key in sorted(named_unresolved)),
    )

    per_project: dict[str, int] = {}
    for node in closure_nodes:
        per_project[node.project_id] = per_project.get(node.project_id, 0) + 1

    incomplete: list[str] = []
    warnings: list[str] = []
    if depth_limited:
        incomplete.append(_INCOMPLETE_DEPTH)
        warnings.append(
            f"depth_limited: the closure stops at depth {hops} and more nodes reach the target "
            "beyond it; raise depth (maximum 8) to widen the view"
        )
    if walk.truncated:
        incomplete.append(_INCOMPLETE_TRUNCATED)
        warnings.append(
            f"truncated: expansion stopped at {'/'.join(walk.reasons)}; the closure beyond that "
            "point was not seen"
        )
    if missing:
        incomplete.append(_INCOMPLETE_MISSING)
        warnings.append(
            f"incomplete_scope: {len(missing)} requested member(s) are not pinned in this scope "
            f"({', '.join(missing)}); a short closure is not proof there is no further impact"
        )
    if unresolved_edges:
        incomplete.append(_INCOMPLETE_UNRESOLVED)
        warnings.append(
            f"unresolved_edges: {len(unresolved_edges)} pinned edge(s) name a node in this closure "
            "without a resolved source and were not followed; they may add impact"
        )
    partial_coverage = tuple(
        sorted(pid for pid, status in coverage.items() if status != "complete")
    )
    if partial_coverage:
        incomplete.append(_INCOMPLETE_COVERAGE)
        warnings.append(
            f"coverage: {len(partial_coverage)} searched member(s) do not pin complete coverage "
            f"({', '.join(partial_coverage)}); the graph itself may be missing nodes or edges"
        )
    if potential_ids:
        warnings.append(
            f"potential: {len(potential_ids)} node(s) rest on non-exact edges and are kept "
            "included; only `proven` is backed by exact or annotated resolutions"
        )
    warnings.append(
        "not_exhaustive"
        if incomplete
        else "static_potential_impact: a bounded static closure, not proof of runtime damage"
    )

    return ImpactResult(
        target=resolved.id,
        status=STATUS_OK,
        operation="impact",
        direction="incoming",
        edge_kinds=tuple(kinds),
        depth=walk.depth,
        nodes=tuple(
            project_node(node, sections, coverage=pinned.graph(node.project_id).coverage_document())
            for node in closure_nodes
        ),
        proven=proven_ids,
        potential=potential_ids,
        edges=tuple(edge.as_document() for edge in walk.edges),
        unresolved=tuple(edge.as_document() for edge in unresolved_edges),
        candidates=(),
        per_kind=dict(_per_kind(walk.edges)),
        per_project=per_project,
        searched_projects=searched,
        missing_projects=missing,
        coverage=coverage,
        visited=walk.visited,
        depth_reached=walk.depth_reached,
        truncated=walk.truncated,
        reasons=walk.reasons,
        frontier=walk.frontier,
        exhaustive=not incomplete,
        incomplete_reasons=tuple(incomplete),
        warnings=tuple(warnings),
    )


__all__ = [
    "STATUS_AMBIGUOUS_TARGET",
    "STATUS_OK",
    "ImpactResult",
    "impact",
]
