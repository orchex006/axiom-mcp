"""The canonical tool catalog: which tools exist, what they need, and which task adds each.

``repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md`` section 4 fixes six tools and
``src/axiom_mcp/security.py`` already enforces their capability map at the transport boundary.
This module is the other half of that agreement: the descriptor the tool layer registers with
the SDK. Keeping the two in one place is what stops a tool from being registered with a weaker
capability than the boundary already demanded for its name - :func:`spec_for` refuses an unknown
name and ``tests/test_tools_catalog.py`` asserts this table and ``security.TOOL_CAPABILITY``
agree entry by entry, so a drift is a test failure rather than a silent privilege change.

Annotations are recorded here because section 4 requires them to be *true* rather than
decorative: ``readOnlyHint`` is set only for the three tools that touch no daemon state,
``destructiveHint`` is false everywhere (nothing here deletes graph data - a reconcile enqueue
and an explicit cancel are additive or state-narrowing), and ``openWorldHint`` is true exactly
for the tools that reach the graphd control plane. The SDK treats them as hints; the real
authorization stays in :mod:`axiom_mcp.security` and :mod:`axiom_mcp.tools.context`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from axiom_mcp import security

__all__ = [
    "TOOL_SPECS",
    "ToolSpec",
    "UnknownTool",
    "names",
    "spec_for",
]


class UnknownTool(ValueError):
    """A tool name the canonical catalog does not define."""


@dataclass(frozen=True)
class ToolSpec:
    """One catalog entry, in the vocabulary both the spec and the SDK use."""

    name: str
    capability: str
    summary: str
    registering_task: str
    read_only: bool
    destructive: bool
    open_world: bool

    def annotations(self) -> dict[str, bool]:
        """The SDK ``ToolAnnotations`` fields this catalog asserts."""
        return {
            "readOnlyHint": self.read_only,
            "destructiveHint": self.destructive,
            "openWorldHint": self.open_world,
        }


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="graph_status",
        capability=security.CAPABILITY_READ,
        summary="Solution/project freshness, coverage, generations and capabilities",
        registering_task="C-028",
        read_only=True,
        destructive=False,
        open_world=False,
    ),
    ToolSpec(
        name="graph_query",
        capability=security.CAPABILITY_READ,
        summary=(
            "One bounded graph operation: search, context, neighbors, dependencies, callers, "
            "impact, path or changes"
        ),
        registering_task="C-029",
        read_only=True,
        destructive=False,
        open_world=False,
    ),
    ToolSpec(
        name="graph_reconcile",
        capability=security.CAPABILITY_RECONCILE,
        summary="Enqueue a graphd reconcile job for a scope and return its job id",
        registering_task="C-030",
        read_only=False,
        destructive=False,
        open_world=True,
    ),
    ToolSpec(
        name="graph_job",
        capability=security.CAPABILITY_RECONCILE,
        summary="Bounded job status polling, or an explicit capability-checked cancel",
        registering_task="C-031",
        read_only=False,
        destructive=False,
        open_world=True,
    ),
    ToolSpec(
        name="graph_verify",
        capability=security.CAPABILITY_CHECKPOINT,
        summary="Bounded verification request against an expected fingerprint or barrier",
        registering_task="C-032",
        read_only=False,
        destructive=False,
        open_world=True,
    ),
    ToolSpec(
        name="graph_version",
        capability=security.CAPABILITY_READ,
        summary="Selected components, their compatibility and update availability",
        registering_task="C-033",
        read_only=True,
        destructive=False,
        open_world=True,
    ),
)

_BY_NAME: Mapping[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}


def names() -> tuple[str, ...]:
    """Every registered tool name, in catalog order."""
    return tuple(spec.name for spec in TOOL_SPECS)


def spec_for(name: str) -> ToolSpec:
    """Return the catalog entry for ``name`` or raise :class:`UnknownTool`.

    An unknown name is refused instead of defaulting to a read spec: a default
    would let a typo register a tool the transport boundary classifies as a read
    while the tool itself does something else.
    """
    try:
        return _BY_NAME[name]
    except KeyError:
        raise UnknownTool(f"{name!r} is not in the canonical tool catalog") from None
