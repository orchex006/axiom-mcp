"""C-018: indexed symbol search over pinned generations, without a source-body dump.

``repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md`` section 5 makes ``search`` the one operation that
starts from free text rather than a ``target``, and requires that an ambiguous symbol name returns
*candidates* rather than a silent pick. It also makes the byte budget a hard contract and the
default projection "source positions/symbols instead of full source" (section 6). So a search
answer here is a bounded list of stable identities - node id, project, kind, names, language and a
portable source span - and nothing else:

* the candidate list can never contain a body, because :class:`~axiom_mcp.query.model.Node` has no
  field that could hold one and :meth:`SymbolCandidate.as_document` copies only contract fields;
* matching runs against an index, not a scan: :func:`build_symbol_index` keys exact id, exact
  qualified name and exact name (plus their case-folded forms), so the exact and the ambiguous
  cases are answered without walking the graph, and only a prefix/substring query pays for a
  bounded scan of the pinned nodes.
* the scan is bounded by ``max_nodes`` and the answer is bounded by ``limit``, and a capped answer
  says so with ``truncated`` instead of quietly dropping the tail.

The match kinds are fixed and ranked, so the same pinned scope always ranks the same candidate
first. ``ambiguous`` reports that the *best* rank was shared, which is the case section 5 requires
to be visible rather than resolved by the server.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from axiom_mcp.query.model import (
    DEFAULT_MAX_NODES,
    MAX_NODES,
    NODE_KINDS,
    Graph,
    GraphSet,
    LimitRejected,
    Node,
    SourceLocation,
    as_graph_set,
    bounded_int,
    choice_list,
    narrow_scope,
)

MATCH_ID = "id"
MATCH_EXACT_QUALIFIED_NAME = "exact_qualified_name"
MATCH_EXACT_NAME = "exact_name"
MATCH_FOLDED_QUALIFIED_NAME = "folded_qualified_name"
MATCH_FOLDED_NAME = "folded_name"
MATCH_PREFIX = "prefix"
MATCH_SUBSTRING = "substring"

MATCH_KINDS = (
    MATCH_ID,
    MATCH_EXACT_QUALIFIED_NAME,
    MATCH_EXACT_NAME,
    MATCH_FOLDED_QUALIFIED_NAME,
    MATCH_FOLDED_NAME,
    MATCH_PREFIX,
    MATCH_SUBSTRING,
)

RANK = {kind: index for index, kind in enumerate(MATCH_KINDS)}

LIMIT_HINT_MAX = MAX_NODES


@dataclass(frozen=True)
class SymbolCandidate:
    """One matched symbol: identity, source location and how it matched."""

    node_id: str
    project_id: str
    kind: str
    name: str
    qualified_name: str
    language: str
    source: SourceLocation
    identity_quality: str
    match: str

    def as_document(self) -> dict[str, Any]:
        return {
            "id": self.node_id,
            "project_id": self.project_id,
            "kind": self.kind,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "language": self.language,
            "source": self.source.as_document(),
            "identity_quality": self.identity_quality,
            "match": self.match,
        }


@dataclass(frozen=True)
class SymbolIndex:
    """Exact-match indexes over one pinned scope, plus the scan order for weaker matches."""

    project_ids: tuple[str, ...]
    node_count: int
    by_id: Mapping[str, str]
    by_qualified_name: Mapping[str, tuple[str, ...]]
    by_name: Mapping[str, tuple[str, ...]]
    by_folded_qualified_name: Mapping[str, tuple[str, ...]]
    by_folded_name: Mapping[str, tuple[str, ...]]
    scan_order: tuple[str, ...]

    def exact(self, query: str) -> tuple[tuple[str, str], ...]:
        """Return ``(node_id, match kind)`` for every exact key that hits, best rank first.

        A node that hits both an exact and a case-folded key is reported once, at its best rank:
        serving the same identity twice would look like two candidates to a caller that counts
        them, and the ambiguity rule is about *distinct* identities at the same rank.
        """
        best: dict[str, str] = {}
        ordered: list[tuple[str, str]] = []

        def note(node_id: str, match: str) -> None:
            previous = best.get(node_id)
            if previous is not None and RANK[previous] <= RANK[match]:
                return
            if previous is None:
                ordered.append((node_id, match))
            best[node_id] = match

        if query in self.by_id:
            note(self.by_id[query], MATCH_ID)
        for node_id in self.by_qualified_name.get(query, ()):
            note(node_id, MATCH_EXACT_QUALIFIED_NAME)
        for node_id in self.by_name.get(query, ()):
            note(node_id, MATCH_EXACT_NAME)
        folded = query.casefold()
        for node_id in self.by_folded_qualified_name.get(folded, ()):
            note(node_id, MATCH_FOLDED_QUALIFIED_NAME)
        for node_id in self.by_folded_name.get(folded, ()):
            note(node_id, MATCH_FOLDED_NAME)
        return tuple((node_id, best[node_id]) for node_id, _ in ordered)


def _append(index: dict[str, list[str]], key: str, node_id: str) -> None:
    index.setdefault(key, []).append(node_id)


def build_symbol_index(scope: Graph | GraphSet | Iterable[Graph]) -> SymbolIndex:
    """Index one pinned scope by identity, qualified name and name.

    The index is built from the pinned nodes only. It is deterministic: keys map to node ids
    sorted by ``(project_id, qualified_name, id)``, so a lookup with several hits always lists
    them in the same order.
    """
    pinned = as_graph_set(scope)
    by_id: dict[str, str] = {}
    by_qualified: dict[str, list[str]] = {}
    by_name: dict[str, list[str]] = {}
    by_folded_qualified: dict[str, list[str]] = {}
    by_folded_name: dict[str, list[str]] = {}
    for node in pinned.nodes:
        by_id[node.id] = node.id
        _append(by_qualified, node.qualified_name, node.id)
        _append(by_name, node.name, node.id)
        _append(by_folded_qualified, node.qualified_name.casefold(), node.id)
        _append(by_folded_name, node.name.casefold(), node.id)
    order = {node.id: (node.project_id, node.qualified_name, node.id) for node in pinned.nodes}

    def sorted_map(index: Mapping[str, list[str]]) -> dict[str, tuple[str, ...]]:
        return {
            key: tuple(sorted(value, key=lambda node_id: order[node_id]))
            for key, value in index.items()
        }

    return SymbolIndex(
        project_ids=pinned.project_ids,
        node_count=len(pinned.nodes),
        by_id=by_id,
        by_qualified_name=sorted_map(by_qualified),
        by_name=sorted_map(by_name),
        by_folded_qualified_name=sorted_map(by_folded_qualified),
        by_folded_name=sorted_map(by_folded_name),
        scan_order=tuple(sorted(by_id, key=lambda node_id: order[node_id])),
    )


@dataclass(frozen=True)
class SymbolSearchResult:
    """The bounded answer to one ``search`` operation."""

    query: str
    candidates: tuple[SymbolCandidate, ...]
    scanned: int
    pinned_nodes: int
    limit: int
    max_nodes: int
    truncated: bool
    warnings: tuple[str, ...]

    @property
    def ambiguous(self) -> bool:
        """True when more than one candidate shares the best match rank."""
        if not self.candidates:
            return False
        best = RANK[self.candidates[0].match]
        return sum(1 for item in self.candidates if RANK[item.match] == best) > 1

    def as_document(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "candidates": [item.as_document() for item in self.candidates],
            "scanned": self.scanned,
            "pinned_nodes": self.pinned_nodes,
            "limit": self.limit,
            "truncated": self.truncated,
            "ambiguous": self.ambiguous,
            "warnings": list(self.warnings),
        }


def _candidate(node: Node, match: str) -> SymbolCandidate:
    return SymbolCandidate(
        node_id=node.id,
        project_id=node.project_id,
        kind=node.kind,
        name=node.name,
        qualified_name=node.qualified_name,
        language=node.language,
        source=node.source,
        identity_quality=node.identity_quality,
        match=match,
    )


def search_symbols(
    scope: Graph | GraphSet | Iterable[Graph],
    query: str,
    *,
    project_ids: Sequence[str] | None = None,
    kinds: Sequence[str] | None = None,
    limit: int | None = None,
    max_nodes: int | None = None,
) -> SymbolSearchResult:
    """Search a pinned scope for symbols, bounded by ``limit`` and ``max_nodes``.

    Exact id, qualified-name and name queries are answered from the index. A weaker query pays for
    a scan bounded by ``max_nodes``; the scan never expands beyond the pinned nodes, so a search
    cannot walk edges or grow with the size of the repository beyond that bound.
    """
    if not isinstance(query, str) or not query.strip():
        raise LimitRejected("query must be a non-empty string")
    pinned = narrow_scope(as_graph_set(scope), project_ids)
    selection = tuple(sorted(set(kinds))) if kinds is not None else ()
    if selection:
        choice_list(kinds, "kinds", tuple(sorted(NODE_KINDS)))
    ceiling = bounded_int(max_nodes, "max_nodes", low=1, high=MAX_NODES, default=DEFAULT_MAX_NODES)
    allowance = bounded_int(limit, "limit", low=1, high=MAX_NODES, default=ceiling)
    allowance = min(allowance, ceiling)

    index = build_symbol_index(pinned)
    wanted = query.strip()
    folded = wanted.casefold()
    ranked: dict[str, str] = {}
    for node_id, match in index.exact(wanted):
        ranked.setdefault(node_id, match)
    scanned = 0
    if len(ranked) < allowance:
        for node_id in index.scan_order:
            if scanned >= ceiling:
                break
            node = pinned.node(node_id)
            if selection and node.kind not in selection:
                continue
            scanned += 1
            match = _weaker_match(node, folded)
            if match is not None:
                ranked.setdefault(node_id, match)

    candidates = [
        _candidate(pinned.node(node_id), match)
        for node_id, match in ranked.items()
        if not selection or pinned.node(node_id).kind in selection
    ]
    candidates.sort(
        key=lambda item: (
            RANK[item.match],
            item.project_id,
            item.qualified_name,
            item.node_id,
        )
    )
    truncated = len(candidates) > allowance
    warnings: list[str] = []
    if truncated:
        warnings.append(f"limit: the answer is capped at {allowance} candidates")
    if not candidates:
        warnings.append("no_symbol_matched")
    result = SymbolSearchResult(
        query=wanted,
        candidates=tuple(candidates[:allowance]),
        scanned=scanned,
        pinned_nodes=index.node_count,
        limit=allowance,
        max_nodes=ceiling,
        truncated=truncated,
        warnings=tuple(warnings),
    )
    if result.ambiguous:
        return SymbolSearchResult(
            query=result.query,
            candidates=result.candidates,
            scanned=result.scanned,
            pinned_nodes=result.pinned_nodes,
            limit=result.limit,
            max_nodes=result.max_nodes,
            truncated=result.truncated,
            warnings=(
                *result.warnings,
                f"ambiguous: {len(result.candidates)} candidates share rank "
                f"{result.candidates[0].match}; candidates are returned instead of one pick",
            ),
        )
    return result


def _weaker_match(node: Node, folded: str) -> str | None:
    if node.qualified_name.casefold().startswith(folded) or node.name.casefold().startswith(folded):
        return MATCH_PREFIX
    if folded in node.qualified_name.casefold() or folded in node.name.casefold():
        return MATCH_SUBSTRING
    return None


__all__ = [
    "MATCH_EXACT_NAME",
    "MATCH_EXACT_QUALIFIED_NAME",
    "MATCH_FOLDED_NAME",
    "MATCH_FOLDED_QUALIFIED_NAME",
    "MATCH_ID",
    "MATCH_KINDS",
    "MATCH_PREFIX",
    "MATCH_SUBSTRING",
    "SymbolCandidate",
    "SymbolIndex",
    "SymbolSearchResult",
    "build_symbol_index",
    "search_symbols",
]
