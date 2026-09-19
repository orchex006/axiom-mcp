"""C-021: cross-project caller lookup over every pinned member of the scope.

``repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md`` section 5 requires that a caller lookup answer the
whole pinned scope, and warns that a partial impact view is not proof of the absence of other
callers. A caller is exactly the case where a *local* lookup looks complete and is wrong: the only
caller of an endpoint often lives in another project, and a per-project reverse index would return
an empty list with full confidence.

So this operation is defined against the whole :class:`~axiom_mcp.query.model.GraphSet`:

* the reverse index it walks is the global one ``GraphSet`` builds over *every* pinned member -
  each member's incoming edges are indexed under their target id regardless of which project holds
  the source, so a call from another member is found, not missed;
* ``searched_projects`` names every member the answer actually read, and ``cross_project`` names
  the members that contributed a caller from outside the target's own project;
* ``missing_projects`` carries members the caller named that this scope does not pin. When it is
  non-empty, ``complete`` is false and a warning says the absence is not proven. An empty caller
  list with ``complete`` false therefore reads as "none found in what was searched", never as "no
  callers exist" - which is precisely the misreading the contract forbids.

``direction`` is not a parameter here: a caller is by definition at the *source* of an edge that
reaches the target, so the walk is incoming-only. ``edge_kinds`` defaults to every pinned kind
except ``CONTAINS``, because a container is not a caller. A budget that stops the walk, an
unresolved incoming edge, and an unpinned member all make the answer incomplete rather than empty.
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
    CONTAINS,
    DEFAULT_DEPTH,
    DEFAULT_MAX_EDGES,
    DEFAULT_MAX_NODES,
    EDGE_KINDS,
    MAX_DEPTH,
    MAX_EDGES,
    MAX_NODES,
    Graph,
    GraphSet,
    Node,
    TargetAmbiguous,
    as_graph_set,
    bounded_int,
    bounded_walk,
    choice_list,
    narrow_scope,
)

#: Every pinned edge kind except containment: a container is not a caller.
CALLER_KINDS = tuple(sorted(EDGE_KINDS - {CONTAINS}))

STATUS_OK = "ok"
STATUS_AMBIGUOUS_TARGET = "ambiguous_target"

_INCOMPLETE_MISSING = "missing_members"
_INCOMPLETE_TRUNCATED = "truncated"
_INCOMPLETE_UNRESOLVED = "unresolved_edges"


@dataclass(frozen=True)
class CallersResult:
    """The bounded reverse-closure answer for one target, with the scope it actually read."""

    target: str | None
    status: str
    operation: str
    direction: str
    edge_kinds: tuple[str, ...]
    depth: int
    nodes: tuple[Mapping[str, Any], ...]
    edges: tuple[Mapping[str, Any], ...]
    unresolved: tuple[Mapping[str, Any], ...]
    candidates: tuple[Mapping[str, Any], ...]
    per_kind: Mapping[str, int]
    per_project: Mapping[str, int]
    searched_projects: tuple[str, ...]
    missing_projects: tuple[str, ...]
    cross_project: tuple[str, ...]
    visited: int
    depth_reached: int
    truncated: bool
    reasons: tuple[str, ...]
    frontier: tuple[str, ...]
    complete: bool
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
            "edges": [dict(item) for item in self.edges],
            "unresolved": [dict(item) for item in self.unresolved],
            "candidates": [dict(item) for item in self.candidates],
            "per_kind": dict(self.per_kind),
            "per_project": dict(self.per_project),
            "searched_projects": list(self.searched_projects),
            "missing_projects": list(self.missing_projects),
            "cross_project": list(self.cross_project),
            "visited": self.visited,
            "truncated": self.truncated,
            "reasons": list(self.reasons),
            "frontier": list(self.frontier),
            "complete": self.complete,
            "incomplete_reasons": list(self.incomplete_reasons),
            "warnings": list(self.warnings),
        }


def _per_kind(edges: Iterable[Any]) -> Mapping[str, int]:
    counts: dict[str, int] = {}
    for edge in edges:
        counts[edge.kind] = counts.get(edge.kind, 0) + 1
    return counts


def _unresolved_references(pinned: GraphSet, node: Node) -> tuple[Any, ...]:
    """Pinned unresolved edges whose ``unresolved_target`` names ``node``.

    An unresolved edge has no target id, so the reverse index cannot file it under the node it
    refers to and an incoming walk can never see it. That is exactly the case the card guards:
    a reference the generation could not resolve *may* be a caller, so it is reported - never
    counted, never followed - instead of vanishing and leaving a falsely empty answer. The match
    is the folded name, the folded qualified name, or any dotted suffix of the qualified name.
    """
    folded_name = node.name.casefold()
    folded_qualified = node.qualified_name.casefold()
    found: list[Any] = []
    for edge in pinned.edges():
        if edge.resolved:
            continue
        named = (edge.unresolved_target or "").casefold()
        if named in (folded_name, folded_qualified) or named.endswith(f".{folded_name}"):
            found.append(edge)
    return tuple(found)


def callers(
    scope: Graph | GraphSet | Iterable[Graph],
    target: str,
    *,
    project_ids: Sequence[str] | None = None,
    depth: int | None = DEFAULT_DEPTH,
    edge_kinds: Sequence[str] | None = None,
    projection: Sequence[str] | None = DEFAULT_PROJECTION,
    max_nodes: int | None = None,
    max_edges: int | None = None,
) -> CallersResult:
    """Return the pinned nodes that reach ``target``, searching every pinned member.

    ``project_ids`` narrows the search only when the caller asks: the default is the whole pinned
    scope, because narrowing to the target's own project is exactly the mistake this operation
    exists to prevent. A narrowed search still reports ``searched_projects`` and any member it was
    told to search but does not pin.
    """
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

    try:
        resolved = pinned.resolve(target)
    except TargetAmbiguous as ambiguous:
        return CallersResult(
            target=None,
            status=STATUS_AMBIGUOUS_TARGET,
            operation="callers",
            direction="incoming",
            edge_kinds=tuple(kinds),
            depth=hops,
            nodes=(),
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
            cross_project=(),
            visited=0,
            depth_reached=0,
            truncated=False,
            reasons=(),
            frontier=(),
            complete=False,
            incomplete_reasons=(_INCOMPLETE_MISSING,) if missing else ("ambiguous_target",),
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

    named_unresolved = _unresolved_references(pinned, resolved)
    unresolved_edges = (*walk.unresolved, *named_unresolved)

    caller_nodes = tuple(node for node in walk.nodes if node.id != resolved.id)
    per_project: dict[str, int] = {}
    for node in caller_nodes:
        per_project[node.project_id] = per_project.get(node.project_id, 0) + 1
    cross_project = tuple(
        sorted(project for project in per_project if project != resolved.project_id)
    )

    incomplete: list[str] = []
    warnings: list[str] = []
    if missing:
        incomplete.append(_INCOMPLETE_MISSING)
        warnings.append(
            f"incomplete_search: {len(missing)} requested member(s) are not pinned in this scope "
            f"({', '.join(missing)}); an empty or short caller list is not proof there are no "
            "callers"
        )
    if walk.truncated:
        incomplete.append(_INCOMPLETE_TRUNCATED)
        warnings.append(
            f"truncated: expansion stopped at {'/'.join(walk.reasons)}; more callers may exist "
            "beyond the searched region"
        )
    if unresolved_edges:
        incomplete.append(_INCOMPLETE_UNRESOLVED)
        warnings.append(
            f"unresolved_edges: {len(unresolved_edges)} pinned edge(s) name {resolved.name!r} "
            "without a resolved source and were not followed; they may be callers this "
            "generation cannot place"
        )
    if cross_project:
        warnings.append(
            f"cross_project: {len(cross_project)} member(s) outside {resolved.project_id!r} hold "
            f"callers of this node ({', '.join(cross_project)})"
        )

    return CallersResult(
        target=resolved.id,
        status=STATUS_OK,
        operation="callers",
        direction="incoming",
        edge_kinds=tuple(kinds),
        depth=walk.depth,
        nodes=tuple(
            project_node(node, sections, coverage=pinned.graph(node.project_id).coverage_document())
            for node in caller_nodes
        ),
        edges=tuple(edge.as_document() for edge in walk.edges),
        unresolved=tuple(edge.as_document() for edge in unresolved_edges),
        candidates=(),
        per_kind=dict(_per_kind(walk.edges)),
        per_project=per_project,
        searched_projects=searched,
        missing_projects=missing,
        cross_project=cross_project,
        visited=walk.visited,
        depth_reached=walk.depth_reached,
        truncated=walk.truncated,
        reasons=walk.reasons,
        frontier=walk.frontier,
        complete=not incomplete,
        incomplete_reasons=tuple(incomplete),
        warnings=tuple(warnings),
    )


__all__ = [
    "CALLER_KINDS",
    "STATUS_AMBIGUOUS_TARGET",
    "STATUS_OK",
    "CallersResult",
    "callers",
]
