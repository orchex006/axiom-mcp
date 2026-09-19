# Bounded query engine

Owner: `axiom-mcp`. Contract: `repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md` section 5 (request and
response shape) and section 6 (structured result and byte budget); canonical schemas live in
`axiom-specs/contracts/schemas/query-request.schema.json` and `query-response.schema.json`.

The engine answers `graph_query` from the processed JSON snapshot only. It never parses
application source, never opens a database, never calls the daemon and never reads a file: a
caller hands in generations that `docs/snapshot-reader-core.md` already copied, verified and
released, and every operation is bounded before it expands anything.

## Shape of the package

| Module | Operation | Task |
| --- | --- | --- |
| `query/model.py` | pinned node/edge model, graph index, canonical limits | shared by all |
| `query/search.py` | `search` - indexed symbol lookup | C-018 |
| `query/context.py` | `context` - projected neighbourhood | C-019 |
| `query/neighbors.py` | `neighbors`, `dependencies` - edge-kind traversal | C-020 |
| `query/callers.py` | `callers` - cross-project reverse lookup | C-021 |
| `query/impact.py` | `impact` - conservative reverse closure | C-022 |
| `query/path.py` | `path` - shortest bounded path | C-023 |
| `query/changes.py` | `changes` - generation comparison | C-024 |
| `query/budget.py` | response byte-budget packer | C-025 |
| `query/cursor.py` | cursor bound to snapshot, query and scope | C-026 |
| `query/envelope.py` | response envelope, freshness, coverage, verification | C-027 |

## Limits

`depth` defaults to 2 and is capped at 8; `max_nodes` defaults to 50 (cap 500); `max_edges`
defaults to 100 (cap 1000); `max_bytes` defaults to 8192 (range 512..32768). A bound outside those
ranges is a validation error, not a clamp: running a caller's query under a bound the caller did
not choose would misreport what was actually searched.

## Search (C-018)

`search_symbols` answers a free-text query over one pinned scope. Exact id, exact qualified name
and exact name (and their case-folded forms) are index lookups; a prefix or substring query pays
for a scan bounded by `max_nodes`. A candidate carries the pinned node id, project, kind, names,
language, a portable source span and the match kind - never a source body. The node model has no
field that could hold one, so a body cannot be returned by accident.

An ambiguous *name* is not resolved by the server: every candidate at the best rank is returned,
`ambiguous` is true and a warning says so. `limit` caps the answer and sets `truncated` with a
warning when it does.

## Context (C-019)

`context` projects the neighbourhood of one target. `depth` is the hop count and is honoured
literally: 0 is the target alone, and `n` never walks further. `projection` is the contract's
allowlist - `identity` (always present), `source_locations`, `relations` (the incident edge ids of
the returned neighbourhood) and `coverage` (the pinned coverage block of the node's generation).
A section that was not requested does not appear.

Expansion runs on `model.bounded_walk`: a visited set makes a cycle terminate, and the node/edge
budgets stop expansion. When a budget stops it, `truncated` is true, `reasons` names the budget and
`frontier` names the nodes at which expansion stopped, so a partial neighbourhood cannot be read as
a complete one. An ambiguous target returns its candidates with `status="ambiguous_target"` and no
invented neighbourhood.
## Neighbours and dependencies (C-020)

`neighbors` walks a target's incident edges; `dependencies` is the outgoing dependency-kind case of
the same traversal, not a second implementation. `direction` is `outgoing` (edges whose source is
the target), `incoming` (edges whose target is it) or `both` (the union). `edge_kinds` is an
allowlist over the pinned edge kinds. Both filters apply to the *edge*, so they compose: a caller
asking for `incoming` + `CALLS` never receives an outgoing `REFERENCES` edge, and a kind that is
filtered out never contributes a hop, so a filtered-out path cannot secretly extend the walk.

The walk is breadth-first over `model.bounded_walk`, so a cycle terminates by construction: each
node is expanded once and each edge reported once, regardless of `depth`. When a budget stops the
walk, `truncated` is true and `reasons` names `max_nodes` or `max_edges`; when the frontier simply
empties, `truncated` stays false - "cut short" and "nothing further" are not the same answer.

An edge with `resolution="unresolved"` (no resolved target in this generation) is returned in
`unresolved` and is never followed: a list that silently omitted it would look complete when the
generation cannot claim it is. `per_kind` counts the returned edges by kind, and an ambiguous
target returns its candidates with `status="ambiguous_target"` and no invented neighbourhood.
## Callers (C-021)

`callers` returns the pinned nodes that reach a target. It is defined against the whole pinned
scope, not the target's own project: `GraphSet` indexes each member's incoming edges under their
target id regardless of which project holds the source, so the common case - an endpoint in one
project called from another - is found. `searched_projects` names every member the answer actually
read; `per_project` and `cross_project` say where the callers came from. The walk is
incoming-only, so `direction` is not a parameter: by definition a caller is at the source of an
edge that reaches the target.

Absence needs care. `complete` is false - with a matching entry in `incomplete_reasons` and a
warning - when the scope was *told* about a member it does not pin (`missing_projects`), when a
budget stopped the walk, or when an unresolved edge names the target without a resolved source.
An empty caller list with `complete` false reads "none found in what was searched", never "no
callers exist". An unresolved edge names the target by text and so cannot be filed in the reverse
index; it is reported in `unresolved` and never followed or counted. `edge_kinds` defaults to every
pinned kind except `CONTAINS`, because a container is not a caller.
## Impact (C-022)

`impact` returns the bounded reverse closure of a target - what has to be re-examined if the target
changes. It is conservative, and it says so. `nodes` is the closure; `proven` is the subset
reachable through edges whose resolution is exact or annotated; `potential` is the rest, kept
because a reader wants the wider net but labelled because it rests on inference. The direction is
incoming-only: the target's own dependencies are not impact.

Nothing about the answer is allowed to read as exhaustive unless it is. `exhaustive` is true only
when every one of these held: the depth bound was probed one hop further and found no more (at the
maximum depth of 8 the bound is admitted as limiting), no node or edge budget stopped expansion, no
member the caller named is unpinned, no unresolved edge names a node in the closure, and every
searched project's pinned coverage is `complete`. Otherwise `exhaustive` is false and
`incomplete_reasons` names what cut the view (`depth_limited`, `truncated`, `missing_members`,
`unresolved_edges`, `coverage_not_complete`). The result is static potential impact: not proof of
runtime damage, and not proof that no other node is impacted.