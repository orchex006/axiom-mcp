"""The registered MCP tool surface.

``repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md`` section 4 fixes six tools; each module here
implements one of them as a plain function over a
:class:`~axiom_mcp.tools.context.ToolContext`, and :mod:`axiom_mcp.tools.catalog` holds the
descriptor the SDK is given. Handlers stay plain functions so a test can call one directly with a
real context - the SDK registration is a thin adapter, not the place behaviour lives.

The six tools are split by what they are allowed to do:

* ``graph_status`` (C-028) and ``graph_version`` (C-033) report state;
* ``graph_query`` (C-029) runs one bounded operation over pinned generations;
* ``graph_reconcile`` (C-030), ``graph_job`` (C-031) and ``graph_verify`` (C-032) delegate to the
  graphd control plane and never read a source file or write a graph shard themselves.
"""

from __future__ import annotations

from axiom_mcp.tools.catalog import TOOL_SPECS, ToolSpec, UnknownTool, names, spec_for
from axiom_mcp.tools.context import (
    GuardedSnapshotSource,
    SnapshotSource,
    ToolContext,
    ToolPrincipal,
)
from axiom_mcp.tools.job import graph_job
from axiom_mcp.tools.query import graph_query
from axiom_mcp.tools.reconcile import graph_reconcile
from axiom_mcp.tools.status import graph_status
from axiom_mcp.tools.verify import graph_verify

__all__ = [
    "TOOL_SPECS",
    "GuardedSnapshotSource",
    "SnapshotSource",
    "ToolContext",
    "ToolPrincipal",
    "ToolSpec",
    "UnknownTool",
    "graph_job",
    "graph_query",
    "graph_reconcile",
    "graph_status",
    "graph_verify",
    "names",
    "spec_for",
]
