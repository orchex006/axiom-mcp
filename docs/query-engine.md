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
