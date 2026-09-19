"""C-028: ``graph_status`` - what a solution currently has pinned, and what it may do.

The tool answers from two independent sources and keeps them apart, because
``repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md`` sections 4 and 7 make them different facts:

* the **snapshot** - the generations that are actually pinned right now, read through the trusted
  registry with the same bounded read session a query uses, so the answer cannot describe a lane
  the caller could not query;
* the **daemon** - freshness and job activity, which the read session never invents. It is
  consulted only when the caller asks for it, and an unreachable daemon is reported as an
  unavailable plane rather than as an error, because a snapshot-only gateway is a supported state
  (section 7: "graphd offline but checkpoint valid: query works with ``freshness=unknown``").

The freshness claim is the part that is easy to overstate, and this module refuses to. A read
session proves the bytes are the published ones, so it reports the read protocol's
``manifest_hash`` verification; that is *not* envelope-level evidence that the snapshot is newer
than the source tree. The aggregate ``freshness`` therefore goes through
:func:`~axiom_mcp.query.envelope.resolve_freshness` with a plain "no evidence" verification
unless the daemon supplies an ``inventory_hash`` verification that carries the fingerprint it
recomputed - so a daemon that merely says "fresh" is reported as ``unknown`` with the downgrade
named in ``warnings``.

An unauthorized solution and an unregistered one get the same ``NOT_FOUND`` answer. That is
deliberate: a distinguishable error would turn ``graph_status`` into a solution-enumeration
oracle for a token that is scoped to something else.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from axiom_mcp import security
from axiom_mcp.errors import AxiomError
from axiom_mcp.query.envelope import (
    SCHEMA_VERSION,
    EnvelopeInvalid,
    Verification,
    aggregate_coverage,
    catalog_generation_id,
    resolve_freshness,
)
from axiom_mcp.query.model import Graph, GraphSet, graph_from_snapshot
from axiom_mcp.read_session import LoadedSnapshot
from axiom_mcp.registry import LANES, LIVE_LANE
from axiom_mcp.tools.context import (
    ToolContext,
    closed_arguments,
    identifier_argument,
    optional_bool,
    project_scope,
)

__all__ = ["STATUS_FIELDS", "graph_status"]

#: Every key a ``graph_status`` answer carries, and nothing else.
STATUS_FIELDS = (
    "schema_version",
    "solution_id",
    "lane",
    "catalog_generation_id",
    "freshness",
    "coverage",
    "projects",
    "capabilities",
    "daemon",
    "warnings",
)

_ARGUMENTS = ("solution_id", "project_id", "project_ids", "lane", "include_daemon")


def _lane_argument(arguments: Mapping[str, Any]) -> str:
    """Return the requested lane, defaulting to the live lane."""
    value = arguments.get("lane")
    if value is None:
        return LIVE_LANE
    if not isinstance(value, str) or value not in LANES:
        raise AxiomError(
            "VALIDATION_ERROR",
            f"lane must be one of {list(LANES)}",
            details={"field": "lane"},
        )
    return value


def _project_entry(snapshot: LoadedSnapshot, coverage: str) -> dict[str, Any]:
    """One project's pinned facts, in the read protocol's own vocabulary."""
    return {
        "project_id": snapshot.project_id,
        "lane": snapshot.lane,
        "generation_id": snapshot.generation_id,
        "analysis_profile": snapshot.analysis_profile,
        "records": snapshot.records,
        "bytes_copied": snapshot.bytes_copied,
        "source_fingerprint": snapshot.source_fingerprint,
        # Read-protocol vocabulary (`manifest_hash`/`inventory_hash`), not the
        # envelope's. Recorded as observed rather than forced into the wrong enum.
        "verification": snapshot.verification,
        "freshness": snapshot.freshness,
        "coverage": coverage,
        "guard_held_during_copy": snapshot.guard_held_during_copy,
        "parsed_after_guard_release": snapshot.parsed_after_guard_release,
    }


def _daemon_section(control: Any, solution_id: str) -> dict[str, Any]:
    """Normalise a control-plane status answer without trusting its shape."""
    try:
        report = control.status(solution_id)
    except AxiomError as error:
        return {"available": False, "reason": error.code}
    except Exception as error:  # noqa: BLE001 - any transport failure is unavailability
        return {"available": False, "reason": type(error).__name__}
    if report is None:
        return {"available": False, "reason": "no_status_reported"}
    if not isinstance(report, Mapping):
        return {"available": False, "reason": "status_not_an_object"}
    section: dict[str, Any] = {"available": bool(report.get("available", False))}
    for key in (
        "reason",
        "freshness",
        "verification",
        "verification_source_fingerprint",
        "jobs_in_flight",
    ):
        if report.get(key) is not None:
            section[key] = report[key]
    return section


def _verification_from_daemon(daemon: Mapping[str, Any] | None) -> Verification:
    """Build the envelope verification the daemon actually evidenced, or none.

    Only an ``inventory_hash`` mode that carries a 64-hex fingerprint is accepted; anything else
    becomes the no-evidence verification, which is what downgrades a bare "fresh" claim.
    """
    if daemon is None or not daemon.get("available"):
        return Verification()
    mode = daemon.get("verification")
    fingerprint = daemon.get("verification_source_fingerprint")
    if mode == "inventory_hash" and isinstance(fingerprint, str):
        try:
            return Verification(mode="inventory_hash", source_fingerprint=fingerprint)
        except EnvelopeInvalid:
            return Verification()
    if mode == "watcher_hint" and isinstance(daemon.get("verified_at"), str):
        try:
            return Verification(mode="watcher_hint", verified_at=daemon["verified_at"])
        except EnvelopeInvalid:
            return Verification()
    return Verification()


def graph_status(arguments: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
    """Return the pinned generations, coverage and capabilities for one solution."""
    closed_arguments(arguments, _ARGUMENTS)
    solution_id = identifier_argument(arguments, "solution_id")
    lane = _lane_argument(arguments)
    include_daemon = optional_bool(arguments, "include_daemon", default=False)

    # Authorizes and answers NOT_FOUND for both an invisible and an unknown solution.
    context.open_solution(solution_id)
    requested = project_scope(arguments)
    projects = context.resolve_projects(solution_id, requested)
    if not projects:
        raise AxiomError(
            "NOT_FOUND",
            "no project of this solution is visible to this caller",
            details={"solution_id": solution_id},
        )

    graphs: list[Graph] = []
    snapshots: list[LoadedSnapshot] = []
    for project_id in projects:
        snapshot = context.load_project(solution_id, project_id, lane)
        snapshots.append(snapshot)
        graphs.append(graph_from_snapshot(snapshot))
    scope = GraphSet(graphs)

    try:
        coverage, coverage_warnings = aggregate_coverage(scope)
    except EnvelopeInvalid as exc:
        # A pinned generation whose coverage block is unusable is a snapshot defect,
        # not a status answer, and it is refused rather than reported as complete.
        raise AxiomError("SNAPSHOT_CORRUPT", str(exc)) from exc

    per_project_coverage: dict[str, str] = {}
    for graph in graphs:
        # aggregate_coverage already refused a member without a usable status.
        per_project_coverage[graph.project_id] = str(graph.coverage_status)

    daemon: dict[str, Any] | None = None
    reported_freshness: str | None = None
    if include_daemon:
        control = context.require_control()
        daemon = _daemon_section(control, solution_id)
        if daemon.get("available") and isinstance(daemon.get("freshness"), str):
            reported_freshness = daemon["freshness"]

    freshness, freshness_warnings = resolve_freshness(
        reported_freshness, _verification_from_daemon(daemon)
    )

    capabilities = {}
    for capability in sorted(security.KNOWN_CAPABILITIES):
        capabilities[capability] = capability in context.principal.capabilities

    return {
        "schema_version": SCHEMA_VERSION,
        "solution_id": solution_id,
        "lane": lane,
        "catalog_generation_id": catalog_generation_id(scope),
        "freshness": freshness,
        "coverage": coverage,
        "projects": [
            _project_entry(snapshot, per_project_coverage[snapshot.project_id])
            for snapshot in snapshots
        ],
        "capabilities": capabilities,
        "daemon": daemon,
        "warnings": _warnings(coverage_warnings, freshness_warnings),
    }


def _warnings(*groups: Iterable[str]) -> list[str]:
    """Concatenate warning groups, preserving order and dropping duplicates."""
    seen: list[str] = []
    for group in groups:
        for item in group:
            if item not in seen:
                seen.append(item)
    return seen
