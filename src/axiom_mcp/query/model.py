"""The shared bounded-query model: pinned nodes, pinned edges and one graph view.

``contracts/schemas/node.schema.json`` and ``contracts/schemas/edge.schema.json`` fix what a
graph fact is, and ``docs/12-SNAPSHOT-READ-WRITE-PROTOCOL.md`` fixes where it is read from: the
``nodes/*`` and ``edges/*`` shards of one pinned generation, already copied into memory by
:class:`axiom_mcp.read_session.ReadSession`. Every query operation in this package (``search``,
``context``, ``neighbors``, ``callers``, ``impact``, ``path``, ``changes``) starts from the same
two facts, so they are defined once here instead of once per operation:

* a node is identity plus a *source location* - never a source body. ``repo-seeds/axiom-mcp/
  docs/17-FASTAPI-MCP.md`` section 6 makes "source positions/symbols instead of full source" the
  default projection, so this model has no field that could carry a body and a query cannot
  return one by accident.
* an edge carries ``resolution`` and ``evidence``. An edge whose ``resolution`` is
  ``unresolved`` has no ``target_id`` and therefore no further node to walk to. The traversal
  modules treat that as *incomplete scope* rather than as an absent fact, which is what makes the
  reverse closures in ``impact`` and ``callers`` conservative instead of merely bounded.

A :class:`Graph` is one project's immutable generation. A :class:`GraphSet` is the pinned catalog
view: several projects' graphs indexed together, so an edge whose ``target_project_id`` is
another project still resolves to that other project's node.

The query limits are the canonical bounds from ``contracts/schemas/query-request.schema.json``:
``depth`` 0..8 (default 2), ``max_nodes`` 1..500 (default 50), ``max_edges`` 0..1000 (default
100), ``max_bytes`` 512..32768 (default 8192). They are enforced here rather than trusted from a
caller, and :func:`bounded_int` refuses a value the request schema would itself have rejected, so
an out-of-contract bound is a validation error instead of a silently clamped query.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from axiom_mcp.read_session import LoadedSnapshot

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
PORTABLE_RELATIVE_RE = re.compile(r"^(?!/)(?!.*\\)(?!.*(?:^|/)\.\.(?:/|$))(?![A-Za-z]:).+$")

NODE_KINDS = frozenset(
    {
        "Solution",
        "Project",
        "File",
        "Namespace",
        "Class",
        "Interface",
        "Function",
        "Method",
        "Property",
        "ApiEndpoint",
        "DataStore",
        "Table",
        "StoredProcedure",
        "QueueTopic",
        "ExternalService",
        "TestCase",
        "UnresolvedTarget",
    }
)

EDGE_KINDS = frozenset(
    {
        "CONTAINS",
        "IMPORTS",
        "REFERENCES",
        "CALLS",
        "INHERITS",
        "IMPLEMENTS",
        "EXPOSES",
        "CALLS_ENDPOINT",
        "READS",
        "WRITES",
        "EXECUTES_PROCEDURE",
        "DEPENDS_ON",
        "PUBLISHES",
        "SUBSCRIBES",
        "TESTS",
    }
)

RESOLUTIONS = ("exact_static", "inferred_static", "annotated", "unresolved")
STATIC_RESOLUTIONS = frozenset({"exact_static", "annotated"})
IDENTITY_QUALITIES = ("semantic_key", "syntax_key", "annotation_key")
DIRECTIONS = ("incoming", "outgoing", "both")
PROJECTIONS = ("identity", "relations", "source_locations", "coverage")

UNRESOLVED = "unresolved"
CONTAINS = "CONTAINS"
CALL_KINDS = frozenset({"CALLS", "CALLS_ENDPOINT"})

MAX_DEPTH = 8
MAX_NODES = 500
MIN_BYTES = 512
MAX_BYTES = 32768
MAX_EDGES = 1000
DEFAULT_DEPTH = 2
DEFAULT_MAX_NODES = 50
DEFAULT_MAX_EDGES = 100
DEFAULT_MAX_BYTES = 8192

MATCH_RANKS = ("id", "qualified_name", "name", "folded_qualified_name", "folded_name", "prefix")


class QueryModelError(ValueError):
    """A query input the contract does not allow."""


class NodeInvalid(QueryModelError):
    """A node document that is not a pinned graph fact."""


class EdgeInvalid(QueryModelError):
    """An edge document that is not a pinned graph fact."""


class LimitRejected(QueryModelError):
    """A bound outside the canonical request ranges."""


class TargetNotFound(QueryModelError):
    """A target selector that names no pinned node."""


class TargetAmbiguous(QueryModelError):
    """A target selector that names more than one pinned node."""

    def __init__(self, target: str, candidate_ids: Sequence[str]) -> None:
        self.target = target
        self.candidate_ids = tuple(candidate_ids)
        super().__init__(
            f"target {target!r} is ambiguous: {len(self.candidate_ids)} pinned nodes match"
        )


def _text(value: Any, *, what: str, error: type[QueryModelError] = QueryModelError) -> str:
    if not isinstance(value, str) or not value:
        raise error(f"{what} must be a non-empty string, got {value!r}")
    return value


def sha256_text(value: Any, *, what: str, error: type[QueryModelError] = QueryModelError) -> str:
    if not isinstance(value, str) or not SHA256_RE.match(value):
        raise error(f"{what} must be a lowercase sha256 digest, got {value!r}")
    return value


def identifier_text(
    value: Any, *, what: str, error: type[QueryModelError] = QueryModelError
) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.match(value):
        raise error(f"{what} must match {IDENTIFIER_RE.pattern}, got {value!r}")
    return value


def _has_only(document: Mapping[str, Any], allowed: Iterable[str], *, what: str) -> None:
    extra = sorted(set(document) - set(allowed))
    if extra:
        raise QueryModelError(f"{what} carries unknown keys {extra}")


def bounded_int(
    value: Any,
    name: str,
    *,
    low: int,
    high: int,
    default: int | None = None,
) -> int:
    """Accept an in-range integer bound, or the documented default when it is ``None``.

    A rejected bound raises :class:`LimitRejected` rather than being clamped: a caller who asked
    for ``depth=99`` must be told that the contract maximum is 8, because silently running with 8
    would report a result under a bound the caller did not choose.
    """
    if value is None:
        if default is None:
            raise LimitRejected(f"{name} is required")
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise LimitRejected(f"{name} must be an integer, got {value!r}")
    if value < low or value > high:
        raise LimitRejected(f"{name} {value} is outside the canonical range {low}..{high}")
    return value


def choice(value: Any, name: str, allowed: Sequence[str], *, default: str | None = None) -> str:
    """Accept one value from a fixed allowlist, or the documented default."""
    if value is None:
        if default is None:
            raise LimitRejected(f"{name} is required")
        return default
    if not isinstance(value, str) or value not in allowed:
        raise LimitRejected(f"{name} must be one of {list(allowed)}, got {value!r}")
    return value


def choice_list(
    values: Any,
    name: str,
    allowed: Sequence[str],
    *,
    default: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Normalise an optional allowlist of contract values to a deterministic tuple."""
    if values is None:
        return tuple(default) if default is not None else ()
    if isinstance(values, str) or isinstance(values, (bytes, Mapping)):
        raise LimitRejected(f"{name} must be a sequence of values, got {values!r}")
    if not isinstance(values, Iterable):
        raise LimitRejected(f"{name} must be a sequence of values, got {values!r}")
    selected = tuple(values)
    for item in selected:
        if not isinstance(item, str) or item not in allowed:
            raise LimitRejected(f"{name} value {item!r} is not in the canonical allowlist")
    return tuple(sorted(set(selected)))


@dataclass(frozen=True)
class SourceLocation:
    """A portable source position: file plus a 1-based line span, never a body."""

    file: str
    start_line: int
    end_line: int

    def __post_init__(self) -> None:
        if not isinstance(self.file, str) or not PORTABLE_RELATIVE_RE.match(self.file):
            raise NodeInvalid(f"source file {self.file!r} is not a portable relative path")
        for name in ("start_line", "end_line"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise NodeInvalid(f"source {name} must be a positive integer, got {value!r}")
        if self.end_line < self.start_line:
            raise NodeInvalid(
                f"source end_line {self.end_line} precedes start_line {self.start_line}"
            )

    def as_document(self) -> dict[str, Any]:
        return {"file": self.file, "start_line": self.start_line, "end_line": self.end_line}


def source_from_document(document: Any, *, what: str) -> SourceLocation:
    if not isinstance(document, Mapping):
        raise NodeInvalid(f"{what} source must be an object, got {document!r}")
    _has_only(document, ("file", "start_line", "end_line"), what=f"{what} source")
    missing = [key for key in ("file", "start_line", "end_line") if key not in document]
    if missing:
        raise NodeInvalid(f"{what} source is missing {missing}")
    return SourceLocation(
        file=document["file"],
        start_line=document["start_line"],
        end_line=document["end_line"],
    )


@dataclass(frozen=True)
class Node:
    """One pinned graph node, in the field names of ``node.schema.json``."""

    id: str
    project_id: str
    kind: str
    name: str
    qualified_name: str
    language: str
    source: SourceLocation
    identity_quality: str
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        sha256_text(self.id, what="node id", error=NodeInvalid)
        identifier_text(self.project_id, what="node project_id", error=NodeInvalid)
        if self.kind not in NODE_KINDS:
            raise NodeInvalid(f"node kind {self.kind!r} is not in the canonical allowlist")
        _text(self.name, what="node name", error=NodeInvalid)
        _text(self.qualified_name, what="node qualified_name", error=NodeInvalid)
        _text(self.language, what="node language", error=NodeInvalid)
        if not isinstance(self.source, SourceLocation):
            raise NodeInvalid("node source must be a SourceLocation")
        if self.identity_quality not in IDENTITY_QUALITIES:
            raise NodeInvalid(
                f"node identity_quality {self.identity_quality!r} is not in the allowlist"
            )
        if not isinstance(self.attributes, Mapping):
            raise NodeInvalid("node attributes must be an object")
        object.__setattr__(self, "attributes", dict(self.attributes))

    def as_document(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "kind": self.kind,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "language": self.language,
            "source": self.source.as_document(),
            "identity_quality": self.identity_quality,
            "attributes": dict(self.attributes),
        }


NODE_FIELDS = (
    "id",
    "project_id",
    "kind",
    "name",
    "qualified_name",
    "language",
    "source",
    "identity_quality",
    "attributes",
)


def node_from_document(document: Any) -> Node:
    """Parse one node document strictly: every contract field, and nothing else."""
    if not isinstance(document, Mapping):
        raise NodeInvalid(f"node must be an object, got {document!r}")
    _has_only(document, NODE_FIELDS, what="node")
    missing = [key for key in NODE_FIELDS if key not in document]
    if missing:
        raise NodeInvalid(f"node is missing {missing}")
    return Node(
        id=document["id"],
        project_id=document["project_id"],
        kind=document["kind"],
        name=document["name"],
        qualified_name=document["qualified_name"],
        language=document["language"],
        source=source_from_document(document["source"], what=f"node {document['id']!r}"),
        identity_quality=document["identity_quality"],
        attributes=document["attributes"],
    )


@dataclass(frozen=True)
class Edge:
    """One pinned graph edge; exactly one of ``target_id``/``unresolved_target`` is set."""

    id: str
    source_id: str
    target_project_id: str
    kind: str
    resolution: str
    evidence: tuple[Mapping[str, Any], ...]
    analyzer_id: str
    target_id: str | None = None
    unresolved_target: str | None = None

    def __post_init__(self) -> None:
        sha256_text(self.id, what="edge id", error=EdgeInvalid)
        sha256_text(self.source_id, what="edge source_id", error=EdgeInvalid)
        identifier_text(self.target_project_id, what="edge target_project_id", error=EdgeInvalid)
        if self.kind not in EDGE_KINDS:
            raise EdgeInvalid(f"edge kind {self.kind!r} is not in the canonical allowlist")
        if self.resolution not in RESOLUTIONS:
            raise EdgeInvalid(f"edge resolution {self.resolution!r} is not in the allowlist")
        _text(self.analyzer_id, what="edge analyzer_id", error=EdgeInvalid)
        if not self.evidence:
            raise EdgeInvalid("edge evidence must carry at least one item")
        object.__setattr__(self, "evidence", tuple(dict(item) for item in self.evidence))
        if (self.target_id is None) == (self.unresolved_target is None):
            raise EdgeInvalid(
                "an edge carries exactly one of target_id or unresolved_target, never both or "
                "neither"
            )
        if self.target_id is not None:
            sha256_text(self.target_id, what="edge target_id", error=EdgeInvalid)
        if self.unresolved_target is not None:
            _text(self.unresolved_target, what="edge unresolved_target", error=EdgeInvalid)
        if self.resolution == UNRESOLVED and self.unresolved_target is None:
            raise EdgeInvalid("an unresolved edge must carry unresolved_target")
        if self.resolution != UNRESOLVED and self.target_id is None:
            raise EdgeInvalid(f"a {self.resolution} edge must carry target_id")

    @property
    def resolved(self) -> bool:
        return self.target_id is not None

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "id": self.id,
            "source_id": self.source_id,
            "target_project_id": self.target_project_id,
            "kind": self.kind,
            "resolution": self.resolution,
            "evidence": [dict(item) for item in self.evidence],
            "analyzer_id": self.analyzer_id,
        }
        if self.target_id is not None:
            document["target_id"] = self.target_id
        else:
            document["unresolved_target"] = self.unresolved_target
        return document


EDGE_FIELDS = (
    "id",
    "source_id",
    "target_project_id",
    "kind",
    "resolution",
    "evidence",
    "analyzer_id",
)


def edge_from_document(document: Any) -> Edge:
    """Parse one edge document strictly: the contract fields plus exactly one target form."""
    if not isinstance(document, Mapping):
        raise EdgeInvalid(f"edge must be an object, got {document!r}")
    _has_only(document, (*EDGE_FIELDS, "target_id", "unresolved_target"), what="edge")
    missing = [key for key in EDGE_FIELDS if key not in document]
    if missing:
        raise EdgeInvalid(f"edge is missing {missing}")
    return Edge(
        id=document["id"],
        source_id=document["source_id"],
        target_project_id=document["target_project_id"],
        kind=document["kind"],
        resolution=document["resolution"],
        evidence=tuple(document["evidence"]),
        analyzer_id=document["analyzer_id"],
        target_id=document.get("target_id"),
        unresolved_target=document.get("unresolved_target"),
    )


def _select_kinds(edges: Iterable[Edge], kinds: tuple[str, ...] | None) -> tuple[Edge, ...]:
    if kinds is None:
        return tuple(edges)
    return tuple(edge for edge in edges if edge.kind in kinds)


def _direct(
    edges: Mapping[str, tuple[Edge, ...]], node_id: str, kinds: tuple[str, ...] | None
) -> tuple[Edge, ...]:
    return _select_kinds(edges.get(node_id, ()), kinds)


@dataclass(frozen=True)
class Graph:
    """One project's pinned generation, indexed once for bounded traversal."""

    project_id: str
    generation_id: str
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
    coverage: Mapping[str, Any] | None = None
    _by_id: Mapping[str, Node] = field(init=False, repr=False, compare=False)
    _outgoing: Mapping[str, tuple[Edge, ...]] = field(init=False, repr=False, compare=False)
    _incoming: Mapping[str, tuple[Edge, ...]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        identifier_text(self.project_id, what="graph project_id")
        sha256_text(self.generation_id, what="graph generation_id")
        nodes = tuple(self.nodes)
        edges = tuple(self.edges)
        by_id: dict[str, Node] = {}
        for node in nodes:
            if node.project_id != self.project_id:
                raise NodeInvalid(
                    f"node {node.id[:12]} declares project {node.project_id!r}, not "
                    f"{self.project_id!r}"
                )
            if node.id in by_id:
                raise NodeInvalid(f"duplicate node id {node.id[:12]} in one generation")
            by_id[node.id] = node
        outgoing: dict[str, list[Edge]] = {}
        incoming: dict[str, list[Edge]] = {}
        seen_edges: set[str] = set()
        for edge in edges:
            if edge.id in seen_edges:
                raise EdgeInvalid(f"duplicate edge id {edge.id[:12]} in one generation")
            seen_edges.add(edge.id)
            if edge.source_id not in by_id:
                raise EdgeInvalid(
                    f"edge {edge.id[:12]} starts at {edge.source_id[:12]}, which this generation "
                    f"does not pin"
                )
            outgoing.setdefault(edge.source_id, []).append(edge)
            if edge.target_id is not None:
                incoming.setdefault(edge.target_id, []).append(edge)
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "edges", edges)
        object.__setattr__(self, "_by_id", by_id)
        object.__setattr__(
            self, "_outgoing", {key: tuple(value) for key, value in outgoing.items()}
        )
        object.__setattr__(
            self, "_incoming", {key: tuple(value) for key, value in incoming.items()}
        )

    def node(self, node_id: str) -> Node:
        found = self._by_id.get(node_id)
        if found is None:
            raise TargetNotFound(
                f"generation {self.generation_id[:12]} pins no node {node_id[:12]}"
            )
        return found

    def has_node(self, node_id: str) -> bool:
        return node_id in self._by_id

    def outgoing(self, node_id: str, kinds: tuple[str, ...] | None = None) -> tuple[Edge, ...]:
        return _direct(self._outgoing, node_id, kinds)

    def incoming(self, node_id: str, kinds: tuple[str, ...] | None = None) -> tuple[Edge, ...]:
        return _direct(self._incoming, node_id, kinds)

    @property
    def coverage_status(self) -> str:
        """The pinned coverage status, or ``unknown`` when this view has no coverage block."""
        if not self.coverage:
            return "unknown"
        return str(self.coverage.get("status", "unknown"))

    def coverage_document(self) -> Mapping[str, Any]:
        """The contract fields of the pinned coverage block, and nothing else."""
        if not self.coverage:
            return {}
        return {
            key: self.coverage[key]
            for key in (
                "status",
                "input_files",
                "processed_files",
                "unresolved_references",
                "unsupported_patterns",
            )
            if key in self.coverage
        }

    def as_set(self) -> GraphSet:
        return GraphSet((self,))


def graph_from_documents(
    project_id: str,
    generation_id: str,
    *,
    nodes: Iterable[Mapping[str, Any]],
    edges: Iterable[Mapping[str, Any]],
    coverage: Mapping[str, Any] | None = None,
) -> Graph:
    """Build one pinned generation from already-parsed shard documents."""
    return Graph(
        project_id=project_id,
        generation_id=generation_id,
        nodes=tuple(node_from_document(item) for item in nodes),
        edges=tuple(edge_from_document(item) for item in edges),
        coverage=coverage,
    )


def graph_from_snapshot(snapshot: LoadedSnapshot) -> Graph:
    """Build the graph view of a :class:`~axiom_mcp.read_session.LoadedSnapshot`.

    Only the ``nodes`` and ``edges`` roles are read. A generation that pins neither is an empty
    graph rather than an error, because "this project has no symbols yet" is a real answer.
    """
    nodes: list[Mapping[str, Any]] = []
    edges: list[Mapping[str, Any]] = []
    coverage: Mapping[str, Any] | None = None
    for shard in snapshot.shards:
        if shard.role == "nodes":
            nodes.extend(shard.document)
        elif shard.role == "edges":
            edges.extend(shard.document)
        elif shard.role == "coverage":
            coverage = dict(shard.document)
    return graph_from_documents(
        snapshot.project_id,
        snapshot.generation_id,
        nodes=nodes,
        edges=edges,
        coverage=coverage,
    )


def _rank(node: Node, selector: str) -> int:
    folded = selector.casefold()
    if node.id == selector:
        return 0
    if node.qualified_name == selector:
        return 1
    if node.name == selector:
        return 2
    if node.qualified_name.casefold() == folded:
        return 3
    if node.name.casefold() == folded:
        return 4
    if node.qualified_name.casefold().startswith(folded) or node.name.casefold().startswith(folded):
        return 5
    if folded in node.qualified_name.casefold() or folded in node.name.casefold():
        return 6
    return -1


class GraphSet:
    """Several pinned projects indexed together, so cross-project edges still resolve.

    ``missing_projects`` records members the caller asked about whose generation is *not* pinned
    here (a collected or unreadable member). Pinned and missing are disjoint: a project cannot be
    both. Operations that can be read as "there is nothing" - a caller lookup above all - consult
    this so an absent member is reported as an incomplete search instead of an empty answer.
    """

    def __init__(self, graphs: Iterable[Graph], *, missing_projects: Iterable[str] = ()) -> None:
        by_project: dict[str, Graph] = {}
        nodes: dict[str, Node] = {}
        outgoing: dict[str, list[Edge]] = {}
        incoming: dict[str, list[Edge]] = {}
        for graph in graphs:
            if not isinstance(graph, Graph):
                raise QueryModelError(f"a Graph is required, got {graph!r}")
            existing = by_project.get(graph.project_id)
            if existing is not None:
                raise QueryModelError(
                    f"project {graph.project_id!r} is pinned twice "
                    f"({existing.generation_id[:12]} and {graph.generation_id[:12]})"
                )
            by_project[graph.project_id] = graph
            for node in graph.nodes:
                other = nodes.get(node.id)
                if other is not None and other.project_id != node.project_id:
                    raise QueryModelError(f"node id {node.id[:12]} is pinned by two projects")
                nodes[node.id] = node
            for edge in graph.edges:
                outgoing.setdefault(edge.source_id, []).append(edge)
                if edge.target_id is not None:
                    incoming.setdefault(edge.target_id, []).append(edge)
        absent = tuple(
            sorted({identifier_text(pid, what="project_id") for pid in missing_projects})
        )
        overlap = sorted(set(absent) & set(by_project))
        if overlap:
            raise QueryModelError(f"project {overlap[0]!r} is pinned and missing at the same time")
        self._by_project = by_project
        self._missing_projects = absent
        self._nodes = nodes
        self._outgoing = {key: tuple(value) for key, value in outgoing.items()}
        self._incoming = {key: tuple(value) for key, value in incoming.items()}

    @property
    def project_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_project))

    @property
    def missing_projects(self) -> tuple[str, ...]:
        """Members the caller named that this scope does not pin, sorted and deduplicated."""
        return self._missing_projects

    @property
    def searched_projects(self) -> tuple[str, ...]:
        """Every member a traversal over this scope actually reads."""
        return self.project_ids

    @property
    def graphs(self) -> tuple[Graph, ...]:
        return tuple(self._by_project[key] for key in sorted(self._by_project))

    def generations(self) -> tuple[tuple[str, str], ...]:
        return tuple((key, self._by_project[key].generation_id) for key in sorted(self._by_project))

    def graph(self, project_id: str) -> Graph:
        found = self._by_project.get(project_id)
        if found is None:
            raise TargetNotFound(f"no pinned generation for project {project_id!r}")
        return found

    def node(self, node_id: str) -> Node:
        found = self._nodes.get(node_id)
        if found is None:
            raise TargetNotFound(f"the pinned scope holds no node {node_id[:12]}")
        return found

    def has_node(self, node_id: str) -> bool:
        return node_id in self._nodes

    @property
    def nodes(self) -> tuple[Node, ...]:
        return tuple(self._nodes[key] for key in sorted(self._nodes))

    def edges(self) -> tuple[Edge, ...]:
        collected: dict[str, Edge] = {}
        for graph in self.graphs:
            for edge in graph.edges:
                collected.setdefault(edge.id, edge)
        return tuple(collected[key] for key in sorted(collected))

    def outgoing(self, node_id: str, kinds: tuple[str, ...] | None = None) -> tuple[Edge, ...]:
        return _direct(self._outgoing, node_id, kinds)

    def incoming(self, node_id: str, kinds: tuple[str, ...] | None = None) -> tuple[Edge, ...]:
        return _direct(self._incoming, node_id, kinds)

    def match(self, text: str) -> tuple[Node, ...]:
        """Rank pinned nodes against a selector: id, qualified name, then name.

        The order is deterministic and total, so the same pinned scope always answers a selector
        the same way. An ambiguous selector keeps every candidate: the contract requires
        candidates rather than a silent pick.
        """
        selector = _text(text, what="target selector")
        ranked: list[tuple[int, str, str, Node]] = []
        for node in self.nodes:
            rank = _rank(node, selector)
            if rank >= 0:
                ranked.append((rank, node.qualified_name, node.id, node))
        ranked.sort(key=lambda item: (item[0], item[1], item[2]))
        return tuple(item[3] for item in ranked)

    def resolve(self, text: str) -> Node:
        """Resolve a selector to exactly one node, or refuse to guess."""
        candidates = self.match(text)
        if not candidates:
            raise TargetNotFound(f"no pinned node matches {text!r}")
        best = _rank(candidates[0], text)
        tied = tuple(node for node in candidates if _rank(node, text) == best)
        if len(tied) > 1:
            raise TargetAmbiguous(text, [node.id for node in tied])
        return tied[0]


def unresolved_references(
    scope: Graph | GraphSet | Iterable[Graph], node: Node
) -> tuple[Edge, ...]:
    """Pinned unresolved edges whose ``unresolved_target`` names ``node``.

    An unresolved edge carries no target id, so the reverse index cannot file it under the node it
    refers to and an incoming walk can never reach it. Reporting these separately is what keeps a
    "no callers" or "no impact" answer honest: a reference the generation could not resolve *may*
    be exactly the caller or the impacted node that is missing from the resolved closure.

    The match is the folded name, the folded qualified name, or a dotted suffix of the qualified
    name, so ``Legacy.Caller`` also names ``Caller``. The comparison is deterministic and case
    insensitive because a qualified reference is written by a parser, not by a person.
    """
    pinned = as_graph_set(scope)
    folded_name = node.name.casefold()
    folded_qualified = node.qualified_name.casefold()
    found: list[Edge] = []
    for edge in pinned.edges():
        if edge.resolved:
            continue
        named = (edge.unresolved_target or "").casefold()
        if named in (folded_name, folded_qualified) or named.endswith(f".{folded_name}"):
            found.append(edge)
    return tuple(found)


def as_graph_set(scope: Graph | GraphSet | Iterable[Graph]) -> GraphSet:
    """Normalise the scope argument every query operation accepts."""
    if isinstance(scope, GraphSet):
        return scope
    if isinstance(scope, Graph):
        return scope.as_set()
    if isinstance(scope, Iterable):
        return GraphSet(tuple(scope))
    raise QueryModelError(f"a Graph, a GraphSet or a sequence of Graph is required, got {scope!r}")


def narrow_scope(scope: GraphSet, project_ids: Iterable[str] | None) -> GraphSet:
    """Narrow a scope to an explicit project selection, preserving a deterministic order."""
    if project_ids is None:
        return scope
    chosen = tuple(identifier_text(pid, what="project_id") for pid in project_ids)
    return GraphSet(
        (scope.graph(pid) for pid in chosen),
        missing_projects=[pid for pid in scope.missing_projects if pid in chosen],
    )


def _other_end(edge: Edge, node_id: str, direction: str) -> str | None:
    """The node one hop away from ``node_id`` along ``edge``, or ``None`` for an unresolved edge."""
    if edge.target_id is None:
        return None
    if direction == "outgoing":
        return edge.target_id if edge.source_id == node_id else None
    if direction == "incoming":
        return edge.source_id if edge.target_id == node_id else None
    if edge.source_id == node_id:
        return edge.target_id
    if edge.target_id == node_id:
        return edge.source_id
    return None


def _restrict_resolutions(
    edges: Iterable[Edge], resolutions: frozenset[str] | None
) -> tuple[Edge, ...]:
    if resolutions is None:
        return tuple(edges)
    return tuple(edge for edge in edges if edge.resolution in resolutions)


def incident_edges(
    scope: Graph | GraphSet | Iterable[Graph],
    node_id: str,
    direction: str,
    edge_kinds: Sequence[str] | None = None,
) -> tuple[Edge, ...]:
    """Public, validated form of :func:`_incident` for operations that need their own walk."""
    pinned = as_graph_set(scope)
    heading = choice(direction, "direction", DIRECTIONS, default="both")
    kinds = (
        None
        if edge_kinds is None
        else choice_list(edge_kinds, "edge_kinds", tuple(sorted(EDGE_KINDS)))
    )
    return _incident(pinned, node_id, heading, kinds)


def other_end(edge: Edge, node_id: str, direction: str) -> str | None:
    """Public form of :func:`_other_end`: the node one hop away, or ``None`` if unresolved."""
    heading = choice(direction, "direction", DIRECTIONS, default="both")
    return _other_end(edge, node_id, heading)


def _incident(
    pinned: GraphSet,
    node_id: str,
    direction: str,
    kinds: tuple[str, ...] | None,
    resolutions: frozenset[str] | None = None,
) -> tuple[Edge, ...]:
    outgoing = (
        _restrict_resolutions(pinned.outgoing(node_id, kinds), resolutions)
        if direction in ("outgoing", "both")
        else ()
    )
    incoming = (
        _restrict_resolutions(pinned.incoming(node_id, kinds), resolutions)
        if direction in ("incoming", "both")
        else ()
    )
    if direction == "outgoing":
        return tuple(sorted(outgoing, key=lambda edge: (edge.kind, edge.id)))
    if direction == "incoming":
        return tuple(sorted(incoming, key=lambda edge: (edge.kind, edge.id)))
    merged: dict[str, Edge] = {}
    for edge in (*outgoing, *incoming):
        merged.setdefault(edge.id, edge)
    return tuple(merged[key] for key in sorted(merged, key=lambda key: (merged[key].kind, key)))


@dataclass(frozen=True)
class Walk:
    """The bounded result of a breadth-first walk over pinned edges.

    ``truncated`` is true when a budget stopped the walk, and ``reasons`` names which one. A walk
    that ends because the frontier emptied is *not* truncated: that is the difference between "the
    answer was cut short" and "there was nothing further to walk", and the operations built on
    this primitive report it instead of collapsing the two into one flag.
    """

    roots: tuple[str, ...]
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
    unresolved: tuple[Edge, ...]
    depth: int
    depth_reached: int
    truncated: bool
    reasons: tuple[str, ...]
    frontier: tuple[str, ...]

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(node.id for node in self.nodes)

    @property
    def visited(self) -> int:
        return len(self.nodes)


def bounded_walk(
    scope: Graph | GraphSet | Iterable[Graph],
    roots: Iterable[str],
    *,
    depth: int | None = None,
    direction: str | None = None,
    edge_kinds: Sequence[str] | None = None,
    resolutions: Sequence[str] | None = None,
    max_nodes: int | None = None,
    max_edges: int | None = None,
) -> Walk:
    """Breadth-first walk bounded by depth, node count and edge count.

    ``depth`` counts hops: 0 returns the roots alone. A visited set makes a cycle terminate by
    construction - a node is expanded once, so ``a -> b -> a`` cannot loop - and the node/edge
    budgets stop expansion even when the graph is larger than the requested answer. Unresolved
    edges (``resolution="unresolved"``, no ``target_id``) are collected rather than followed: they
    are exactly the "there may be more, and this generation cannot say where" case.
    """
    pinned = as_graph_set(scope)
    hops = bounded_int(depth, "depth", low=0, high=MAX_DEPTH, default=DEFAULT_DEPTH)
    heading = choice(direction, "direction", DIRECTIONS, default="both")
    kinds = (
        None
        if edge_kinds is None
        else choice_list(edge_kinds, "edge_kinds", tuple(sorted(EDGE_KINDS)))
    )
    node_budget = bounded_int(
        max_nodes, "max_nodes", low=1, high=MAX_NODES, default=DEFAULT_MAX_NODES
    )
    edge_budget = bounded_int(
        max_edges, "max_edges", low=0, high=MAX_EDGES, default=DEFAULT_MAX_EDGES
    )
    allowed_resolutions: frozenset[str] | None = None
    if resolutions is not None:
        selected_resolutions = choice_list(resolutions, "resolutions", RESOLUTIONS)
        if not selected_resolutions:
            raise LimitRejected("resolutions must name at least one resolution")
        allowed_resolutions = frozenset(selected_resolutions)

    ordered_roots: list[str] = []
    for root in roots:
        node_id = sha256_text(root, what="walk root")
        if node_id not in ordered_roots:
            ordered_roots.append(node_id)
    ordered_roots.sort()

    visited: dict[str, Node] = {}
    walked: dict[str, Edge] = {}
    unresolved: dict[str, Edge] = {}
    reasons: set[str] = set()
    blocked_at: set[str] = set()
    for node_id in ordered_roots:
        if len(visited) >= node_budget:
            reasons.add("max_nodes")
            blocked_at.add(node_id)
            break
        visited[node_id] = pinned.node(node_id)

    current = [node_id for node_id in ordered_roots if node_id in visited]
    depth_reached = 0
    for level in range(1, hops + 1):
        if not current:
            break
        discovered: list[str] = []
        for node_id in current:
            for edge in _incident(pinned, node_id, heading, kinds, allowed_resolutions):
                known = edge.id in walked or edge.id in unresolved
                if not known and len(walked) + len(unresolved) >= edge_budget:
                    reasons.add("max_edges")
                    blocked_at.add(node_id)
                    continue
                if edge.resolved:
                    walked.setdefault(edge.id, edge)
                else:
                    unresolved.setdefault(edge.id, edge)
                other = _other_end(edge, node_id, heading)
                if other is None or other in visited or other in discovered:
                    continue
                if len(visited) + len(discovered) >= node_budget:
                    reasons.add("max_nodes")
                    blocked_at.add(node_id)
                    continue
                discovered.append(other)
        for node_id in discovered:
            visited[node_id] = pinned.node(node_id)
        if discovered:
            depth_reached = level
        current = discovered

    return Walk(
        roots=tuple(ordered_roots),
        nodes=tuple(visited[key] for key in sorted(visited)),
        edges=tuple(walked[key] for key in sorted(walked)),
        unresolved=tuple(unresolved[key] for key in sorted(unresolved)),
        depth=hops,
        depth_reached=depth_reached,
        truncated=bool(reasons),
        reasons=tuple(sorted(reasons)),
        frontier=tuple(sorted(blocked_at)),
    )


__all__ = [
    "CALL_KINDS",
    "CONTAINS",
    "DEFAULT_DEPTH",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_EDGES",
    "DEFAULT_MAX_NODES",
    "DIRECTIONS",
    "EDGE_FIELDS",
    "EDGE_KINDS",
    "Edge",
    "EdgeInvalid",
    "Graph",
    "GraphSet",
    "IDENTITY_QUALITIES",
    "LimitRejected",
    "MATCH_RANKS",
    "MAX_BYTES",
    "MAX_DEPTH",
    "MAX_EDGES",
    "MAX_NODES",
    "MIN_BYTES",
    "NODE_FIELDS",
    "NODE_KINDS",
    "Node",
    "NodeInvalid",
    "PROJECTIONS",
    "QueryModelError",
    "RESOLUTIONS",
    "SHA256_RE",
    "STATIC_RESOLUTIONS",
    "SourceLocation",
    "TargetAmbiguous",
    "TargetNotFound",
    "UNRESOLVED",
    "Walk",
    "as_graph_set",
    "bounded_walk",
    "bounded_int",
    "choice",
    "choice_list",
    "edge_from_document",
    "graph_from_documents",
    "graph_from_snapshot",
    "identifier_text",
    "narrow_scope",
    "node_from_document",
    "sha256_text",
    "source_from_document",
]
