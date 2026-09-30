"""C-019: context projection - the requested neighbourhood, projected as the caller asked.

``repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md`` section 5 makes ``projection`` a fixed allowlist
(``identity``, ``relations``, ``source_locations``, ``coverage``) and section 6 makes the byte
budget a hard contract, with source positions rather than full source as the default. This module
makes the projection executable rather than advisory:

* a requested projection is exactly what the answer contains. The node document always carries
  identity, and only a requested ``source_locations``/``relations``/``coverage`` adds its section,
  so a caller who asked for identity alone cannot receive file positions by accident.
* ``depth`` is the number of hops and is honoured literally: ``depth=0`` is the target alone, and
  ``depth=n`` never walks further. The walk itself is :func:`axiom_mcp.query.model.bounded_walk`,
  so a cycle terminates on the visited set and the node/edge budgets bound the answer.
* limits that stop the expansion are recorded. ``truncated`` is true and ``reasons`` names the
  budget (``max_nodes`` or ``max_edges``) plus the nodes at which expansion stopped, so a partial
  neighbourhood can never be read as a complete one.

An ambiguous ``target`` follows the same rule as search: the candidates are returned with
``status="ambiguous_target"`` and no neighbourhood is invented for an identity the server picked.
A target that matches nothing is a ``TargetNotFound`` error, which the envelope maps to the
contract's ``NOT_FOUND``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from axiom_mcp.query.model import (
    DEFAULT_DEPTH,
    DEFAULT_MAX_EDGES,
    DEFAULT_MAX_NODES,
    DIRECTIONS,
    EDGE_KINDS,
    MAX_DEPTH,
    PROJECTIONS,
    Graph,
    GraphSet,
    LimitRejected,
    Node,
    TargetAmbiguous,
    Walk,
    as_graph_set,
    bounded_int,
    bounded_walk,
    choice,
    choice_list,
    narrow_scope,
)

STATUS_OK = "ok"
STATUS_AMBIGUOUS_TARGET = "ambiguous_target"

DEFAULT_PROJECTION = ("identity", "source_locations")
IDENTITY_FIELDS = (
    "id",
    "project_id",
    "kind",
    "name",
    "qualified_name",
    "language",
    "identity_quality",
)


def normalise_projection(projection: Sequence[str] | None) -> tuple[str, ...]:
    """Return the requested projection in canonical order.

    ``identity`` is always present because every other section hangs off it, and the remaining
    sections keep the contract's order so the same request always serialises the same way.
    """
    if projection is None:
        return DEFAULT_PROJECTION
    requested = choice_list(projection, "projection", PROJECTIONS)
    if not requested:
        raise LimitRejected("projection must name at least one section")
    selected = set(requested) | {"identity"}
    return tuple(name for name in PROJECTIONS if name in selected)


def project_node(
    node: Node,
    projection: Sequence[str],
    *,
    relations: Mapping[str, Sequence[str]] | None = None,
    coverage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project one node into exactly the requested sections."""
    sections = set(projection)
    document: dict[str, Any] = {name: getattr(node, name) for name in IDENTITY_FIELDS}
    if "source_locations" in sections:
        document["source"] = node.source.as_document()
    if "relations" in sections:
        document["relations"] = {
            "incoming": sorted((relations or {}).get("incoming", ())),
            "outgoing": sorted((relations or {}).get("outgoing", ())),
        }
    if "coverage" in sections:
        document["coverage"] = dict(coverage or {})
    return document


@dataclass(frozen=True)
class ContextResult:
    """The projected neighbourhood of one target, with its truncation record."""

    target: str | None
    status: str
    projection: tuple[str, ...]
    depth: int
    nodes: tuple[Mapping[str, Any], ...]
    edges: tuple[Mapping[str, Any], ...]
    candidates: tuple[Mapping[str, Any], ...]
    visited: int
    depth_reached: int
    truncated: bool
    reasons: tuple[str, ...]
    frontier: tuple[str, ...]
    warnings: tuple[str, ...]

    def as_document(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "target": self.target,
            "projection": list(self.projection),
            "depth": self.depth,
            "depth_reached": self.depth_reached,
            "nodes": [dict(item) for item in self.nodes],
            "edges": [dict(item) for item in self.edges],
            "candidates": [dict(item) for item in self.candidates],
            "visited": self.visited,
            "truncated": self.truncated,
            "reasons": list(self.reasons),
            "frontier": list(self.frontier),
            "warnings": list(self.warnings),
        }


def _candidates(
    pinned: GraphSet, node_ids: Iterable[str], projection: Sequence[str]
) -> tuple[dict[str, Any], ...]:
    return tuple(project_node(pinned.node(node_id), projection) for node_id in sorted(node_ids))


def _relations(walk: Walk) -> Mapping[str, Mapping[str, Sequence[str]]]:
    relations: dict[str, dict[str, list[str]]] = {}
    for edge in walk.edges:
        outgoing = relations.setdefault(edge.source_id, {"incoming": [], "outgoing": []})
        outgoing["outgoing"].append(edge.id)
        if edge.target_id is not None:
            incoming = relations.setdefault(edge.target_id, {"incoming": [], "outgoing": []})
            incoming["incoming"].append(edge.id)
    return relations


def context(
    scope: Graph | GraphSet | Iterable[Graph],
    target: str,
    *,
    project_ids: Sequence[str] | None = None,
    depth: int | None = None,
    direction: str | None = None,
    edge_kinds: Sequence[str] | None = None,
    projection: Sequence[str] | None = None,
    max_nodes: int | None = None,
    max_edges: int | None = None,
) -> ContextResult:
    """Project the neighbourhood of ``target`` inside the requested bounds."""
    pinned = narrow_scope(as_graph_set(scope), project_ids)
    sections = normalise_projection(projection)
    hops = bounded_int(depth, "depth", low=0, high=MAX_DEPTH, default=DEFAULT_DEPTH)
    headings = choice(direction, "direction", DIRECTIONS, default="both")
    kinds = choice_list(edge_kinds, "edge_kinds", tuple(sorted(EDGE_KINDS)), default=())
    node_budget = max_nodes if max_nodes is not None else DEFAULT_MAX_NODES
    edge_budget = max_edges if max_edges is not None else DEFAULT_MAX_EDGES

    try:
        resolved = pinned.resolve(target)
    except TargetAmbiguous as ambiguous:
        return ContextResult(
            target=None,
            status=STATUS_AMBIGUOUS_TARGET,
            projection=sections,
            depth=hops,
            nodes=(),
            edges=(),
            candidates=_candidates(pinned, ambiguous.candidate_ids, sections),
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
        direction=headings,
        edge_kinds=tuple(kinds) if kinds else None,
        max_nodes=node_budget,
        max_edges=edge_budget,
    )
    relations = _relations(walk)
    nodes = tuple(
        project_node(
            node,
            sections,
            relations=relations.get(node.id),
            coverage=pinned.graph(node.project_id).coverage_document(),
        )
        for node in walk.nodes
    )
    warnings: tuple[str, ...] = ()
    if walk.unresolved:
        warnings = (
            f"unresolved_edges: {len(walk.unresolved)} pinned edges in this neighbourhood have no "
            "resolved target, so the neighbourhood may be incomplete",
        )
    return ContextResult(
        target=resolved.id,
        status=STATUS_OK,
        projection=sections,
        depth=walk.depth,
        nodes=nodes,
        edges=tuple(edge.as_document() for edge in walk.edges),
        candidates=(),
        visited=walk.visited,
        depth_reached=walk.depth_reached,
        truncated=walk.truncated,
        reasons=walk.reasons,
        frontier=walk.frontier,
        warnings=warnings,
    )


__all__ = [
    "DEFAULT_PROJECTION",
    "IDENTITY_FIELDS",
    "STATUS_AMBIGUOUS_TARGET",
    "STATUS_OK",
    "ContextResult",
    "context",
    "normalise_projection",
    "project_node",
]
