"""The bounded query engine: one processed-JSON read model, ten operations.

``repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md`` section 5 fixes what a ``graph_query`` call may
do; each module here implements one bounded piece of it over the pinned generations that
:mod:`axiom_mcp.read_session` copied into memory:

* :mod:`axiom_mcp.query.model` - the shared pinned node/edge model and the graph index;
* :mod:`axiom_mcp.query.search` - C-018 indexed symbol search;
* :mod:`axiom_mcp.query.context` - C-019 context projection;
* :mod:`axiom_mcp.query.neighbors` - C-020 dependency and neighbour traversal;
* :mod:`axiom_mcp.query.callers` - C-021 cross-project caller lookup;
* :mod:`axiom_mcp.query.impact` - C-022 conservative impact closure;
* :mod:`axiom_mcp.query.path` - C-023 shortest bounded path;
* :mod:`axiom_mcp.query.changes` - C-024 generation change comparison;
* :mod:`axiom_mcp.query.budget` - C-025 response byte-budget packer;
* :mod:`axiom_mcp.query.cursor` - C-026 cursors bound to a snapshot and a query;
* :mod:`axiom_mcp.query.envelope` - C-027 the response envelope with freshness and coverage.

Nothing in this package reads files, opens a database or talks to the daemon: a caller hands in
already-loaded pinned generations, and every operation is bounded by the canonical request limits
before it expands anything.
"""
