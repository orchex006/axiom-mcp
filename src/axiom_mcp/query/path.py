"""C-023: shortest bounded path between two pinned nodes.

``repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md`` section 5 pairs ``path`` with ``target_to`` and
requires that traversal uses a visited set and hard budgets. The subtle requirement is the
negative one: "no path" and "the search ran out of budget" are different answers, and collapsing
them would let a caller conclude a dependency does not exist when the server simply stopped
looking. So this operation reports three distinct outcomes:

* ``found`` - a shortest path within the bounds. Every edge on it is a real pinned edge of the
  scope, resolved to the next node, and the path is returned in order;
* ``proven_absent`` - the search finished *without* being cut: the frontier emptied before the
  depth bound, no node or edge budget stopped it, no requested member is unpinned, and no
  unresolved reference names a node the search visited. Only here may a caller read "there is no
  path";
* everything else - ``budget_exhausted`` (a node/edge budget stopped the search) or a depth-capped
  or otherwise incomplete search. Here the honest answer is "not found, and not disproved", and
  ``incomplete_reasons`` names why.

``direction`` defaults to ``both`` (the contract's default), so a path may follow an edge in either
orientation; pass ``outgoing`` for a dependency-only path. An unresolved edge is never a hop, but a
path through one cannot be ruled out, so an unresolved reference naming a visited node makes the
absence provisional rather than proven.
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
    as_graph_set,
    bounded_int,
    choice,
    choice_list,
    incident_edges,
    narrow_scope,
    other_end,
    unresolved_references,
)

STATUS_OK = "ok"
STATUS_AMBIGUOUS_TARGET = "ambiguous_target"

_INCOMPLETE_BUDGET = "budget_exhausted"
_INCOMPLETE_DEPTH = "depth_limited"
_INCOMPLETE_MISSING = "missing_members"
_INCOMPLETE_UNRESOLVED = "unresolved_edges"
_INCOMPLETE_TARGET = "unresolved_target"


@dataclass(frozen=True)
class PathResult:
    """A shortest bounded path, or an explicit statement of why absence is not proven."""

    target: str | None
    target_to: str | None
    status: str
    operation: str
    direction: str
    edge_kinds: tuple[str, ...]
    depth: int
    found: bool
    length: int
    nodes: tuple[Mapping[str, Any], ...]
    edges: tuple[Mapping[str, Any], ...]
    candidates: tuple[Mapping[str, Any], ...]
    unresolved: tuple[Mapping[str, Any], ...]
    searched_projects: tuple[str, ...]
    missing_projects: tuple[str, ...]
    visited: int
    depth_reached: int
    budget_exhausted: bool
    proven_absent: bool
    reasons: tuple[str, ...]
    frontier: tuple[str, ...]
    incomplete_reasons: tuple[str, ...]
    warnings: tuple[str, ...]

    def as_document(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "operation": self.operation,
            "target": self.target,
            "target_to": self.target_to,
            "direction": self.direction,
            "edge_kinds": list(self.edge_kinds),
            "depth": self.depth,
            "depth_reached": self.depth_reached,
            "found": self.found,
            "length": self.length,
            "nodes": [dict(item) for item in self.nodes],
            "edges": [dict(item) for item in self.edges],
            "candidates": [dict(item) for item in self.candidates],
            "unresolved": [dict(item) for item in self.unresolved],
            "searched_projects": list(self.searched_projects),
            "missing_projects": list(self.missing_projects),
            "visited": self.visited,
            "budget_exhausted": self.budget_exhausted,
            "proven_absent": self.proven_absent,
            "reasons": list(self.reasons),
            "frontier": list(self.frontier),
            "incomplete_reasons": list(self.incomplete_reasons),
            "warnings": list(self.warnings),
        }


def _ambiguous(
    operation: str,
    pinned: GraphSet,
    sections: tuple[str, ...],
    label: str,
    ambiguous: TargetAmbiguous,
    *,
    other: str | None,
    target: str,
    target_to: str | None,
    direction: str,
    kinds: tuple[str, ...],
    hops: int,
    searched: tuple[str, ...],
    missing: tuple[str, ...],
) -> PathResult:
    return PathResult(
        target=None,
        target_to=None,
        status=STATUS_AMBIGUOUS_TARGET,
        operation=operation,
        direction=direction,
        edge_kinds=kinds,
        depth=hops,
        found=False,
        length=0,
        nodes=(),
        edges=(),
        candidates=tuple(
            project_node(pinned.node(node_id), sections)
            for node_id in sorted(ambiguous.candidate_ids)
        ),
        unresolved=(),
        searched_projects=searched,
        missing_projects=missing,
        visited=0,
        depth_reached=0,
        budget_exhausted=False,
        proven_absent=False,
        reasons=(),
        frontier=(),
        incomplete_reasons=("ambiguous_target",),
        warnings=(
            f"ambiguous_target: {len(ambiguous.candidate_ids)} pinned nodes match {label!r} "
            f"({target!r} -> {target_to!r}); candidates are returned instead of one pick",
        ),
    )


def path(
    scope: Graph | GraphSet | Iterable[Graph],
    target: str,
    target_to: str,
    *,
    project_ids: Sequence[str] | None = None,
    depth: int | None = DEFAULT_DEPTH,
    direction: str | None = None,
    edge_kinds: Sequence[str] | None = None,
    projection: Sequence[str] | None = DEFAULT_PROJECTION,
    max_nodes: int | None = None,
    max_edges: int | None = None,
) -> PathResult:
    """Return a shortest bounded path from ``target`` to ``target_to``, and how sure it is."""
    pinned = narrow_scope(as_graph_set(scope), project_ids)
    sections = normalise_projection(projection)
    hops = bounded_int(depth, "depth", low=0, high=MAX_DEPTH, default=DEFAULT_DEPTH)
    heading = choice(direction, "direction", DIRECTIONS, default="both")
    kinds = choice_list(edge_kinds, "edge_kinds", tuple(sorted(EDGE_KINDS)))
    node_budget = bounded_int(
        max_nodes, "max_nodes", low=1, high=MAX_NODES, default=DEFAULT_MAX_NODES
    )
    edge_budget = bounded_int(
        max_edges, "max_edges", low=0, high=MAX_EDGES, default=DEFAULT_MAX_EDGES
    )

    searched = pinned.searched_projects
    missing = pinned.missing_projects

    try:
        start = pinned.resolve(target)
    except TargetAmbiguous as ambiguous:
        return _ambiguous(
            "path",
            pinned,
            sections,
            target,
            ambiguous,
            other=target_to,
            target=target,
            target_to=target_to,
            direction=heading,
            kinds=tuple(kinds),
            hops=hops,
            searched=searched,
            missing=missing,
        )
    try:
        goal = pinned.resolve(target_to)
    except TargetAmbiguous as ambiguous:
        return _ambiguous(
            "path",
            pinned,
            sections,
            target_to,
            ambiguous,
            other=target,
            target=target,
            target_to=target_to,
            direction=heading,
            kinds=tuple(kinds),
            hops=hops,
            searched=searched,
            missing=missing,
        )

    parent: dict[str, tuple[str, Any] | None] = {start.id: None}
    visited: list[str] = [start.id]
    seen_edges: set[str] = set()
    unresolved: dict[str, Any] = {}
    reasons: set[str] = set()
    budget_exhausted = False
    found = False
    depth_reached = 0
    current = [start.id]

    if start.id == goal.id:
        found = True
    else:
        for level in range(1, hops + 1):
            if not current:
                break
            next_level: list[str] = []
            for node_id in current:
                for edge in incident_edges(pinned, node_id, heading, tuple(kinds) or None):
                    if edge.id in seen_edges:
                        continue
                    if len(seen_edges) >= edge_budget:
                        reasons.add("max_edges")
                        budget_exhausted = True
                        break
                    seen_edges.add(edge.id)
                    if not edge.resolved:
                        unresolved.setdefault(edge.id, edge)
                        continue
                    if len(visited) >= node_budget:
                        reasons.add("max_nodes")
                        budget_exhausted = True
                        break
                    far = other_end(edge, node_id, heading)
                    if far is None:
                        continue
                    if far == goal.id:
                        parent[far] = (node_id, edge)
                        visited.append(far)
                        found = True
                        break
                    if far in parent:
                        continue
                    parent[far] = (node_id, edge)
                    visited.append(far)
                    next_level.append(far)
                if found or budget_exhausted:
                    break
            if found or budget_exhausted:
                depth_reached = level
                break
            depth_reached = level
            current = next_level

    if found:
        ordered_ids: list[str] = []
        ordered_edges: list[Any] = []
        cursor: str | None = goal.id
        while cursor is not None:
            ordered_ids.append(cursor)
            step = parent.get(cursor)
            if step is None:
                break
            previous, edge = step
            ordered_edges.append(edge)
            cursor = previous
        ordered_ids.reverse()
        ordered_edges.reverse()
        path_nodes = tuple(pinned.node(node_id) for node_id in ordered_ids)
        path_edges = tuple(ordered_edges)
        return PathResult(
            target=start.id,
            target_to=goal.id,
            status=STATUS_OK,
            operation="path",
            direction=heading,
            edge_kinds=tuple(kinds),
            depth=hops,
            found=True,
            length=len(path_edges),
            nodes=tuple(
                project_node(
                    node, sections, coverage=pinned.graph(node.project_id).coverage_document()
                )
                for node in path_nodes
            ),
            edges=tuple(edge.as_document() for edge in path_edges),
            candidates=(),
            unresolved=tuple(edge.as_document() for edge in unresolved.values()),
            searched_projects=searched,
            missing_projects=missing,
            visited=len(visited),
            depth_reached=len(path_edges),
            budget_exhausted=False,
            proven_absent=False,
            reasons=(),
            frontier=(),
            incomplete_reasons=(),
            warnings=(),
        )

    # No path found. Separate "the search was cut short" from "the search finished empty".
    relevant_unresolved = {
        edge.id: edge
        for node_id in visited
        for edge in unresolved_references(pinned, pinned.node(node_id))
    }
    for edge in unresolved.values():
        relevant_unresolved.setdefault(edge.id, edge)
    depth_limited = bool(current) and depth_reached >= hops and not budget_exhausted

    incomplete: list[str] = []
    warnings: list[str] = []
    if budget_exhausted:
        incomplete.append(_INCOMPLETE_BUDGET)
        warnings.append(
            f"budget_exhausted: the search stopped at {'/'.join(sorted(reasons))} before the "
            "frontier emptied; absence of a path is not proven"
        )

    if missing:
        incomplete.append(_INCOMPLETE_MISSING)
        warnings.append(
            f"incomplete_scope: {len(missing)} requested member(s) are not pinned in this scope "
            f"({', '.join(missing)}); a missing member could hold a path"
        )
    if relevant_unresolved:
        incomplete.append(_INCOMPLETE_UNRESOLVED)
        warnings.append(
            f"unresolved_edges: {len(relevant_unresolved)} unresolved edge(s) name a node this "
            "search visited; one of them could be the missing hop"
        )
    if depth_limited:
        incomplete.append(_INCOMPLETE_DEPTH)
        warnings.append(
            f"depth_limited: the search stopped at depth {hops} with a non-empty frontier; "
            "a longer path cannot be excluded"
        )
    if not incomplete:
        warnings.append(
            f"no_path: the frontier emptied within depth {hops} over {len(visited)} node(s); "
            "no valid pinned path exists in this scope under these filters"
        )

    return PathResult(
        target=start.id,
        target_to=goal.id,
        status=STATUS_OK,
        operation="path",
        direction=heading,
        edge_kinds=tuple(kinds),
        depth=hops,
        found=False,
        length=0,
        nodes=(),
        edges=(),
        candidates=(),
        unresolved=tuple(edge.as_document() for edge in relevant_unresolved.values()),
        searched_projects=searched,
        missing_projects=missing,
        visited=len(visited),
        depth_reached=depth_reached,
        budget_exhausted=budget_exhausted,
        proven_absent=not incomplete,
        reasons=tuple(sorted(reasons)),
        frontier=tuple(sorted(current)),
        incomplete_reasons=tuple(incomplete),
        warnings=tuple(warnings),
    )


__all__ = [
    "STATUS_AMBIGUOUS_TARGET",
    "STATUS_OK",
    "PathResult",
    "path",
]
