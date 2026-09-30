"""C-020: dependency and neighbour traversal with consistent edge-kind and direction filters.

``repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md`` section 5 makes ``edge_kinds`` and ``direction``
fixed allowlists and requires that no expansion runs without a visited set and hard budgets. The
``neighbors`` and ``dependencies`` operations share one traversal here, so the two cannot drift:

* ``neighbors`` walks ``direction`` - ``outgoing``, ``incoming`` or ``both`` - and keeps only the
  requested ``edge_kinds``. The filter is applied to the *edge*, not to the node, so a direction
  filter and a kind filter compose: ``direction="incoming"`` never returns an edge whose source is
  the target, and a kind that is excluded never contributes a hop.
* ``dependencies`` is the outgoing case with the dependency kinds as its default. It is
  deliberately not a second implementation: it is :func:`neighbors` with
  ``direction="outgoing"`` and :data:`DEPENDENCY_KINDS`, so "what does this depend on" and "what
  points at this" cannot disagree about the graph.
* a cycle terminates because the walk is breadth-first over a visited set: each node is expanded
  once, so ``a -> b -> c -> a`` reaches depth without looping. The budgets still bound the answer,
  and a budget that stops expansion is reported with ``truncated`` and ``reasons``.

Edge documents carry ``resolution`` and ``evidence`` unchanged, and an unresolved edge is reported
in ``unresolved`` instead of being followed or dropped: a neighbour list that silently omitted it
would look complete when the generation cannot say it is.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from axiom_mcp.query.context import (
    DEFAULT_PROJECTION,
    normalise_projection,
    project_node,
)
from axiom_mcp.query.model import (
    DEFAULT_DEPTH,
    DEFAULT_MAX_EDGES,
    DEFAULT_MAX_NODES,
    DIRECTIONS,
    EDGE_KINDS,
    MAX_DEPTH,
    MAX_EDGES,
    MAX_NODES,
    Graph,
    GraphSet,
    TargetAmbiguous,
    Walk,
    as_graph_set,
    bounded_int,
    bounded_walk,
    choice,
    choice_list,
    narrow_scope,
)

DEPENDENCY_KINDS = (
    "IMPORTS",
    "REFERENCES",
    "CALLS",
    "INHERITS",
    "IMPLEMENTS",
    "CALLS_ENDPOINT",
    "READS",
    "WRITES",
    "EXECUTES_PROCEDURE",
    "DEPENDS_ON",
    "PUBLISHES",
    "SUBSCRIBES",
    "TESTS",
)

STATUS_OK = "ok"
STATUS_AMBIGUOUS_TARGET = "ambiguous_target"
DEFAULT_NEIGHBOUR_DEPTH = 1


@dataclass(frozen=True)
class NeighborResult:
    """The bounded neighbour or dependency answer for one target."""

    target: str | None
    status: str
    operation: str
    direction: str
    edge_kinds: tuple[str, ...] | None
    depth: int
    nodes: tuple[Mapping[str, Any], ...]
    edges: tuple[Mapping[str, Any], ...]
    unresolved: tuple[Mapping[str, Any], ...]
    candidates: tuple[Mapping[str, Any], ...]
    per_kind: Mapping[str, int]
    visited: int
    depth_reached: int
    truncated: bool
    reasons: tuple[str, ...]
    frontier: tuple[str, ...]
    warnings: tuple[str, ...]

    def as_document(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "operation": self.operation,
            "target": self.target,
            "direction": self.direction,
            "edge_kinds": list(self.edge_kinds) if self.edge_kinds is not None else None,
            "depth": self.depth,
            "depth_reached": self.depth_reached,
            "nodes": [dict(item) for item in self.nodes],
            "edges": [dict(item) for item in self.edges],
            "unresolved": [dict(item) for item in self.unresolved],
            "candidates": [dict(item) for item in self.candidates],
            "per_kind": dict(self.per_kind),
            "visited": self.visited,
            "truncated": self.truncated,
            "reasons": list(self.reasons),
            "frontier": list(self.frontier),
            "warnings": list(self.warnings),
        }


def _per_kind(walk: Walk) -> Mapping[str, int]:
    counts: dict[str, int] = {}
    for edge in walk.unresolved:
        counts[edge.kind] = counts.get(edge.kind, 0) + 1
    for edge in walk.edges:
        counts[edge.kind] = counts.get(edge.kind, 0) + 1
    return counts


def _neighbour_traversal(
    operation: str,
    scope: Graph | GraphSet | Iterable[Graph],
    target: str,
    *,
    project_ids: Sequence[str] | None,
    depth: int | None,
    direction: str,
    edge_kinds: Sequence[str] | None,
    projection: Sequence[str] | None,
    max_nodes: int | None,
    max_edges: int | None,
) -> NeighborResult:
    pinned = narrow_scope(as_graph_set(scope), project_ids)
    sections = normalise_projection(projection)
    hops = bounded_int(depth, "depth", low=0, high=MAX_DEPTH, default=DEFAULT_DEPTH)
    heading = choice(direction, "direction", DIRECTIONS, default="both")
    kinds = choice_list(edge_kinds, "edge_kinds", tuple(sorted(EDGE_KINDS)), default=())
    node_budget = bounded_int(
        max_nodes, "max_nodes", low=1, high=MAX_NODES, default=DEFAULT_MAX_NODES
    )
    edge_budget = bounded_int(
        max_edges, "max_edges", low=0, high=MAX_EDGES, default=DEFAULT_MAX_EDGES
    )

    try:
        resolved = pinned.resolve(target)
    except TargetAmbiguous as ambiguous:
        return NeighborResult(
            target=None,
            status=STATUS_AMBIGUOUS_TARGET,
            operation=operation,
            direction=heading,
            edge_kinds=tuple(kinds) if kinds else None,
            depth=hops,
            nodes=(),
            edges=(),
            unresolved=(),
            candidates=tuple(
                project_node(pinned.node(node_id), sections)
                for node_id in sorted(ambiguous.candidate_ids)
            ),
            per_kind={},
            visited=0,
            depth_reached=0,
            truncated=False,
            reasons=(),
            frontier=(),
            warnings=(
                f"ambiguous_target: {len(ambiguous.candidate_ids)} pinned nodes match "
                f"{target!r}; candidates are returned instead of one pick",
            ),
        )

    walk = bounded_walk(
        pinned,
        (resolved.id,),
        depth=hops,
        direction=heading,
        edge_kinds=tuple(kinds) if kinds else None,
        max_nodes=node_budget,
        max_edges=edge_budget,
    )
    warnings: list[str] = []
    if walk.unresolved:
        warnings.append(
            f"unresolved_edges: {len(walk.unresolved)} pinned edges out of this traversal have no "
            "resolved target and were not followed"
        )
    return NeighborResult(
        target=resolved.id,
        status=STATUS_OK,
        operation=operation,
        direction=heading,
        edge_kinds=tuple(kinds) if kinds else None,
        depth=walk.depth,
        nodes=tuple(
            project_node(node, sections, coverage=pinned.graph(node.project_id).coverage_document())
            for node in walk.nodes
        ),
        edges=tuple(edge.as_document() for edge in walk.edges),
        unresolved=tuple(edge.as_document() for edge in walk.unresolved),
        candidates=(),
        per_kind=dict(_per_kind(walk)),
        visited=walk.visited,
        depth_reached=walk.depth_reached,
        truncated=walk.truncated,
        reasons=walk.reasons,
        frontier=walk.frontier,
        warnings=tuple(warnings),
    )


def neighbors(
    scope: Graph | GraphSet | Iterable[Graph],
    target: str,
    *,
    project_ids: Sequence[str] | None = None,
    depth: int | None = DEFAULT_NEIGHBOUR_DEPTH,
    direction: str | None = None,
    edge_kinds: Sequence[str] | None = None,
    projection: Sequence[str] | None = DEFAULT_PROJECTION,
    max_nodes: int | None = None,
    max_edges: int | None = None,
) -> NeighborResult:
    """Return the bounded neighbourhood of ``target`` under the direction and kind filters."""
    return _neighbour_traversal(
        "neighbors",
        scope,
        target,
        project_ids=project_ids,
        depth=depth,
        direction=direction,
        edge_kinds=edge_kinds,
        projection=projection,
        max_nodes=max_nodes,
        max_edges=max_edges,
    )


def dependencies(
    scope: Graph | GraphSet | Iterable[Graph],
    target: str,
    *,
    project_ids: Sequence[str] | None = None,
    depth: int | None = DEFAULT_DEPTH,
    edge_kinds: Sequence[str] | None = DEPENDENCY_KINDS,
    projection: Sequence[str] | None = DEFAULT_PROJECTION,
    max_nodes: int | None = None,
    max_edges: int | None = None,
) -> NeighborResult:
    """Return what ``target`` depends on: the outgoing dependency kinds by default."""
    return _neighbour_traversal(
        "dependencies",
        scope,
        target,
        project_ids=project_ids,
        depth=depth,
        direction="outgoing",
        edge_kinds=edge_kinds,
        projection=projection,
        max_nodes=max_nodes,
        max_edges=max_edges,
    )


__all__ = [
    "DEFAULT_NEIGHBOUR_DEPTH",
    "DEPENDENCY_KINDS",
    "STATUS_AMBIGUOUS_TARGET",
    "STATUS_OK",
    "NeighborResult",
    "dependencies",
    "neighbors",
]
