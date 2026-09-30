"""C-029: ``graph_query`` - one bounded dispatcher over the documented operations.

``repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md`` section 5 fixes the ``graph_query`` contract, and
``contracts/schemas/query-request.schema.json`` is its normative request shape. This module is the
dispatcher that turns one such request into exactly one bounded engine call, and the rules it
enforces are the ones that keep a query from becoming an execution surface:

* **the request is closed, and closed per operation.** ``additionalProperties`` is ``false``, so an
  unexpected key is a ``VALIDATION_ERROR`` whose message names the key and never its value. That is
  what makes "no arbitrary SQL, Cypher, code evaluation or shell" a property of the parser rather
  than a promise: ``sql``, ``cypher`` and ``command`` are not fields, and there is no fallthrough
  that would hand one to an interpreter. A selector that cannot apply to the requested operation -
  ``direction`` on ``search``, ``target_to`` on ``callers`` - is refused rather than ignored,
  because an ignored selector is a request the caller did not get.
* **one operation, one engine call.** ``operation`` maps onto exactly one function in
  :mod:`axiom_mcp.query`, and an operation outside the documented set is ``UNSUPPORTED_OPERATION``.
  The scope is loaded through the trusted registry and the bounded read session, and every
  expansion is bounded by the canonical limits before it happens.
* **the answer is the contract envelope.** The response is assembled by
  :func:`~axiom_mcp.query.envelope.build_envelope` and packed by
  :func:`~axiom_mcp.query.budget.pack_response`, so it carries exactly the contract's keys, its
  ``catalog_generation_id`` is a digest of the generations that actually answered, and the byte cap
  is measured over the whole document.

Two honesty rules are enforced here rather than left to a caller:

* **``pinned`` refuses to drift.** A pinned request must name a ``catalog_generation_id`` (the
  schema requires it), and when the lane no longer holds that vector the answer is
  ``SNAPSHOT_EXPIRED`` instead of a read of whatever is current. A pinned read is still
  ``freshness=unknown``: an immutable generation is not evidence that the source is fresh.
* **``require_fresh`` will not serve stale data as fresh.** A freshness proof needs a bounded
  reconcile, so the caller must hold the ``reconcile`` capability - a read-only token asking for a
  proof is refused with ``FORBIDDEN`` rather than the gateway inheriting mutation authority on its
  behalf. With the capability, the reconcile is enqueued and the request answers ``NOT_READY``
  carrying the job handle: the pending-job branch of section 7, because this request cannot prove
  that the reconcile finished.

The query lane is the ``live`` lane, which ``docs/guides/snapshots.md`` fixes as the working set a
query answers from; a snapshot-only gateway reads a ``checkpoint`` lane exactly like a live one, and
the request schema has no lane field because the lane is not the caller's choice.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from axiom_mcp import security
from axiom_mcp.catalog import load_solution_catalog, pin_catalog, project_member_opener
from axiom_mcp.errors import AxiomError
from axiom_mcp.query import callers as callers_operation
from axiom_mcp.query import changes as changes_operation
from axiom_mcp.query import context as context_operation
from axiom_mcp.query import impact as impact_operation
from axiom_mcp.query import neighbors as neighbors_operation
from axiom_mcp.query import path as path_operation
from axiom_mcp.query import search as search_operation
from axiom_mcp.query.budget import BudgetExceeded, BudgetInvalid, pack_response
from axiom_mcp.query.cursor import CursorError
from axiom_mcp.query.envelope import (
    EnvelopeInvalid,
    Verification,
    build_envelope,
    catalog_generation_id,
)
from axiom_mcp.query.model import (
    DEFAULT_DEPTH,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_EDGES,
    DEFAULT_MAX_NODES,
    DIRECTIONS,
    EDGE_KINDS,
    MAX_BYTES,
    MAX_DEPTH,
    MAX_EDGES,
    MAX_NODES,
    MIN_BYTES,
    PROJECTIONS,
    GraphSet,
    LimitRejected,
    QueryModelError,
    TargetAmbiguous,
    TargetNotFound,
    graph_from_snapshot,
    sha256_text,
)
from axiom_mcp.read_session import LoadedSnapshot
from axiom_mcp.recovery import ALLOW_STALE, CONSISTENCIES, PINNED, REQUIRE_FRESH
from axiom_mcp.registry import LIVE_LANE
from axiom_mcp.tools.context import (
    ToolContext,
    closed_arguments,
    identifier_argument,
    optional_int,
    project_scope,
    require_text,
)

__all__ = ["QUERY_LANE", "QUERY_OPERATIONS", "graph_query"]

#: The lane a query answers from. ``docs/guides/snapshots.md``: the live lane is the working set a
#: query answers from, and an offline checkpoint lane resolves exactly like a live one.
QUERY_LANE = LIVE_LANE

#: The documented operations (``contracts/schemas/query-request.schema.json``). Nothing outside
#: this tuple can reach the engine.
QUERY_OPERATIONS: tuple[str, ...] = (
    "search",
    "context",
    "neighbors",
    "callers",
    "dependencies",
    "impact",
    "path",
    "changes",
)

#: Arguments every operation may carry.
_GLOBAL_ARGUMENTS = (
    "solution_id",
    "operation",
    "project_id",
    "project_ids",
    "max_bytes",
    "consistency",
    "cursor",
    "catalog_generation_id",
)

#: Arguments each operation actually consumes. A selector outside its operation is refused so an
#: ignored input cannot look like an honoured one.
_OPERATION_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "search": ("query", "max_nodes"),
    "context": (
        "target",
        "depth",
        "direction",
        "edge_kinds",
        "projection",
        "max_nodes",
        "max_edges",
    ),
    "neighbors": (
        "target",
        "depth",
        "direction",
        "edge_kinds",
        "projection",
        "max_nodes",
        "max_edges",
    ),
    "dependencies": (
        "target",
        "depth",
        "edge_kinds",
        "projection",
        "max_nodes",
        "max_edges",
    ),
    "callers": ("target", "depth", "edge_kinds", "projection", "max_nodes", "max_edges"),
    "impact": ("target", "depth", "edge_kinds", "projection", "max_nodes", "max_edges"),
    "path": (
        "target",
        "target_to",
        "depth",
        "direction",
        "edge_kinds",
        "projection",
        "max_nodes",
        "max_edges",
    ),
    "changes": ("baseline_catalog_generation_id",),
}

#: Operations whose answer is a projection, and therefore must ask for source locations: the
#: response contract requires ``source`` on every node it carries.
_PROJECTION_OPERATIONS = ("context", "neighbors", "dependencies", "callers", "impact", "path")


def _operation(arguments: Mapping[str, Any]) -> str:
    """Return one documented operation, or refuse the request."""
    value = require_text(arguments, "operation")
    if value not in QUERY_OPERATIONS:
        raise AxiomError(
            "UNSUPPORTED_OPERATION",
            "operation is not one of the documented query operations",
            details={"allowed": list(QUERY_OPERATIONS)},
        )
    return value


def _choice_list(
    arguments: Mapping[str, Any], key: str, allowed: Sequence[str]
) -> tuple[str, ...] | None:
    """Return an optional allowlisted list argument, or refuse the request."""
    value = arguments.get(key)
    if value is None:
        return None
    if (
        isinstance(value, str)
        or isinstance(value, (bytes, Mapping))
        or not isinstance(value, Sequence)
    ):
        raise AxiomError(
            "VALIDATION_ERROR",
            f"{key} must be an array of contract values",
            details={"field": key},
        )
    chosen: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in allowed:
            raise AxiomError(
                "VALIDATION_ERROR",
                f"{key} carries a value outside the canonical allowlist",
                details={"field": key, "allowed": sorted(allowed)},
            )
        if item not in chosen:
            chosen.append(item)
    return tuple(chosen)


def _consistency(arguments: Mapping[str, Any]) -> str:
    """Return the requested consistency, defaulting to ``allow_stale``."""
    value = arguments.get("consistency")
    if value is None:
        return ALLOW_STALE
    if not isinstance(value, str) or value not in CONSISTENCIES:
        raise AxiomError(
            "VALIDATION_ERROR",
            f"consistency must be one of {list(CONSISTENCIES)}",
            details={"field": "consistency"},
        )
    return value


def _pinned_generation(arguments: Mapping[str, Any], consistency: str) -> str | None:
    """Return the pinned catalog generation a ``pinned`` request must name."""
    value = arguments.get("catalog_generation_id")
    if value is None:
        if consistency == PINNED:
            raise AxiomError(
                "VALIDATION_ERROR",
                "consistency 'pinned' requires catalog_generation_id",
                details={"field": "catalog_generation_id"},
            )
        return None
    try:
        return sha256_text(value, what="catalog_generation_id")
    except QueryModelError as exc:
        raise AxiomError(
            "VALIDATION_ERROR", str(exc), details={"field": "catalog_generation_id"}
        ) from exc


def _require_fresh(context: ToolContext, solution_id: str, projects: Sequence[str]) -> None:
    """Enqueue a bounded reconcile and refuse to serve a snapshot as fresh.

    Authorization precedes availability: a token that may not mutate the scope is refused before
    the gateway reports anything about the control plane, and the gateway never borrows its own
    control authority to elevate the caller.
    """
    if security.CAPABILITY_RECONCILE not in context.principal.capabilities:
        raise AxiomError(
            "FORBIDDEN",
            "require_fresh needs reconcile authority: a freshness proof is a bounded reconcile, "
            "and the gateway does not mutate on behalf of a read-only token",
        )
    control = context.require_control()
    request = {
        "solution_id": solution_id,
        "project_ids": list(projects),
        "scope": "solution",
        "reason": "require_fresh",
    }
    try:
        handle = control.reconcile(request)
    except AxiomError:
        raise
    except Exception as exc:  # noqa: BLE001 - any transport failure is unavailability
        raise AxiomError(
            "DAEMON_UNAVAILABLE",
            "the graphd control plane could not accept a reconcile for this request",
        ) from exc
    job_id = handle.get("job_id") if isinstance(handle, Mapping) else None
    raise AxiomError(
        "NOT_READY",
        "require_fresh enqueued a bounded reconcile; this request cannot prove the reconcile "
        "finished, so no snapshot is served as fresh",
        retryable=True,
        details={"job_id": job_id} if isinstance(job_id, str) and job_id else {},
    )


def _resolve_cursor(
    context: ToolContext,
    arguments: Mapping[str, Any],
    *,
    operation: str,
    generation: str,
    projects: Sequence[str],
    query: Mapping[str, Any],
) -> None:
    """Re-check a presented cursor against the request it is only valid for."""
    value = arguments.get("cursor")
    if value is None:
        return
    cursor_id = require_text(arguments, "cursor")
    try:
        context.cursors.resolve(
            cursor_id,
            catalog_generation_id=generation,
            operation=operation,
            query=query,
            scope=projects,
            capability=security.CAPABILITY_READ,
        )
    except CursorError as exc:
        codes = {
            "unknown": "NOT_FOUND",
            "expired": "CONFLICT",
            "generation": "SNAPSHOT_EXPIRED",
            "scope": "FORBIDDEN",
            "query": "VALIDATION_ERROR",
            "capability": "FORBIDDEN",
        }
        raise AxiomError(
            codes.get(exc.reason, "VALIDATION_ERROR"),
            str(exc),
            details={"field": "cursor", "reason": exc.reason},
        ) from exc
    except QueryModelError as exc:
        # An id that is not even a digest cannot be in this store, so it is unreadable rather
        # than a binding that drifted; it gets the same NOT_FOUND as an unknown well-formed id,
        # and a handler never leaks a non-AxiomError to the transport.
        raise AxiomError(
            "NOT_FOUND",
            "this store has no such cursor",
            details={"field": "cursor", "reason": "unknown"},
        ) from exc


def _load_scope(context: ToolContext, solution_id: str, projects: Sequence[str]) -> GraphSet:
    """Load one catalog vector, then only the exact member generations it pins."""
    catalog = load_solution_catalog(context.registry.catalog_location(solution_id, QUERY_LANE))
    vector = pin_catalog(
        catalog,
        open_member=project_member_opener(
            lambda project_id: context.registry.location(solution_id, project_id, QUERY_LANE)
        ),
    )
    selected = {pin.member.project_id: pin for pin in vector.pins}
    snapshots: list[LoadedSnapshot] = []
    for project_id in projects:
        pin = selected.get(project_id)
        if pin is None or not pin.available:
            raise AxiomError(
                "SNAPSHOT_UNAVAILABLE",
                "the catalog-pinned generation is unavailable",
                details={"project_id": project_id},
            )
        snapshots.append(
            context.load_pinned_project(
                solution_id, project_id, QUERY_LANE, pin.member.generation_id
            )
        )
    return GraphSet(graph_from_snapshot(snapshot) for snapshot in snapshots)


def _ambiguous_warning(target: str, count: int) -> str:
    return (
        f"ambiguous_target: {count} pinned nodes match {target!r}; the answer lists them rather "
        "than choosing one"
    )


def _truncation_warning(reasons: Sequence[str]) -> str:
    named = ", ".join(reasons) if reasons else "an expansion bound"
    return f"truncated: the answer stopped at {named} and is not a complete closure"


def _budgets(arguments: Mapping[str, Any]) -> dict[str, int | None]:
    """Every canonical bound, refused rather than clamped (``query/model.bounded_int``)."""
    return {
        "depth": optional_int(
            arguments, "depth", default=DEFAULT_DEPTH, minimum=0, maximum=MAX_DEPTH
        ),
        "max_nodes": optional_int(
            arguments, "max_nodes", default=DEFAULT_MAX_NODES, minimum=1, maximum=MAX_NODES
        ),
        "max_edges": optional_int(
            arguments, "max_edges", default=DEFAULT_MAX_EDGES, minimum=0, maximum=MAX_EDGES
        ),
        "max_bytes": optional_int(
            arguments, "max_bytes", default=DEFAULT_MAX_BYTES, minimum=MIN_BYTES, maximum=MAX_BYTES
        ),
    }


_AnswerParts = tuple[
    tuple[Mapping[str, Any], ...], tuple[Mapping[str, Any], ...], bool, tuple[str, ...]
]


def _changes(scope: GraphSet, arguments: Mapping[str, Any]) -> Any:
    """Compare the pinned head against a baseline generation, as far as it can be pinned."""
    baseline = arguments.get("baseline_catalog_generation_id")
    if baseline is None:
        # No baseline was named, so there is nothing to compare against and no diff is invented.
        return changes_operation.changes(scope, None)
    try:
        baseline_id = sha256_text(baseline, what="baseline_catalog_generation_id")
    except QueryModelError as exc:
        raise AxiomError(
            "VALIDATION_ERROR", str(exc), details={"field": "baseline_catalog_generation_id"}
        ) from exc
    if baseline_id != catalog_generation_id(scope):
        # A different vector cannot be pinned through the read session at this revision, and a
        # diff against "whatever is current" would be a different question under the baseline's
        # identity. No diff is computed and the reason travels with the answer.
        return changes_operation.changes(scope, None)
    return changes_operation.changes(scope, scope)


def _not_comparable_reason(status: str, arguments: Mapping[str, Any]) -> str:
    """Name why ``changes`` computed no diff, in the engine's own vocabulary.

    A missing baseline is the tool's own restriction (the baseline vector cannot be pinned
    through the read session at this revision) rather than an engine status, so it is named
    apart from the engine's refusals; everything else is reported with the status that the
    engine actually returned, so a warning can never claim a cause the engine did not give.
    """
    if status != changes_operation.STATUS_MISSING_BASELINE:
        return (
            f"not_comparable: changes reported {status} and computed no diff, so the answer "
            "carries no facts"
        )
    if arguments.get("baseline_catalog_generation_id") is not None:
        return (
            "baseline_not_pinnable: the named baseline catalog generation cannot be pinned "
            "through the read session at this revision, so no diff was computed"
        )
    return (
        "baseline_not_named: changes requires a baseline generation to compare against, so no "
        "diff was computed"
    )


def _change_facts(
    result: Any,
) -> tuple[tuple[Mapping[str, Any], ...], tuple[Mapping[str, Any], ...]]:
    """The head-side facts of a comparable diff, as envelope nodes and edges.

    An ``added`` fact is already a head document. A ``modified`` fact is a before/after pair whose
    halves have had the identity fields stripped, so the head side plus the fact's own id rebuilds
    exactly the head document - which is what keeps every node this answer carries a pinned head
    fact with its own ``source``. ``removed`` facts are deliberately not returned: they belong to
    the *baseline* generation, and this answer pins only the head.
    """
    nodes: list[Mapping[str, Any]] = []
    edges: list[Mapping[str, Any]] = []
    for project, change in result.projects.items():
        for fact in change["nodes"]["added"]:
            nodes.append(dict(fact))
        for fact in change["nodes"]["modified"]:
            nodes.append({"id": fact["id"], "project_id": project, **fact["after"]})
        for fact in change["edges"]["added"]:
            edges.append(dict(fact))
        for fact in change["edges"]["modified"]:
            edges.append({"id": fact["id"], **fact["after"]})
    return tuple(nodes), tuple(edges)


def _answer(operation: str, arguments: Mapping[str, Any], scope: GraphSet) -> _AnswerParts:
    """Dispatch one operation to exactly one engine call and normalise its answer."""
    budgets = _budgets(arguments)
    projection = _choice_list(arguments, "projection", PROJECTIONS)
    edge_kinds = _choice_list(arguments, "edge_kinds", tuple(sorted(EDGE_KINDS)))
    direction = arguments.get("direction")
    if direction is not None and (not isinstance(direction, str) or direction not in DIRECTIONS):
        raise AxiomError(
            "VALIDATION_ERROR",
            f"direction must be one of {list(DIRECTIONS)}",
            details={"field": "direction"},
        )

    def call() -> Any:
        if operation == "search":
            return search_operation.search_symbols(
                scope, require_text(arguments, "query"), max_nodes=budgets["max_nodes"]
            )
        if operation == "changes":
            return _changes(scope, arguments)
        target = require_text(arguments, "target")
        if operation == "path":
            return path_operation.path(
                scope,
                target,
                require_text(arguments, "target_to"),
                depth=budgets["depth"],
                direction=direction,
                edge_kinds=edge_kinds,
                projection=projection,
                max_nodes=budgets["max_nodes"],
                max_edges=budgets["max_edges"],
            )
        if operation == "context":
            return context_operation.context(
                scope,
                target,
                depth=budgets["depth"],
                direction=direction,
                edge_kinds=edge_kinds,
                projection=projection,
                max_nodes=budgets["max_nodes"],
                max_edges=budgets["max_edges"],
            )
        if operation == "dependencies":
            return neighbors_operation.dependencies(
                scope,
                target,
                depth=budgets["depth"],
                edge_kinds=edge_kinds,
                projection=projection,
                max_nodes=budgets["max_nodes"],
                max_edges=budgets["max_edges"],
            )
        if operation == "neighbors":
            return neighbors_operation.neighbors(
                scope,
                target,
                depth=budgets["depth"],
                direction=direction,
                edge_kinds=edge_kinds,
                projection=projection,
                max_nodes=budgets["max_nodes"],
                max_edges=budgets["max_edges"],
            )
        if operation == "callers":
            return callers_operation.callers(
                scope,
                target,
                depth=budgets["depth"],
                edge_kinds=edge_kinds,
                projection=projection,
                max_nodes=budgets["max_nodes"],
                max_edges=budgets["max_edges"],
            )
        if operation == "impact":
            return impact_operation.impact(
                scope,
                target,
                depth=budgets["depth"],
                edge_kinds=edge_kinds,
                projection=projection,
                max_nodes=budgets["max_nodes"],
                max_edges=budgets["max_edges"],
            )

    try:
        result = call()
    except TargetAmbiguous as exc:
        raise AxiomError("NOT_FOUND", str(exc), details={"field": "target"}) from exc
    except TargetNotFound as exc:
        raise AxiomError("NOT_FOUND", str(exc), details={"field": "target"}) from exc
    except LimitRejected as exc:
        raise AxiomError("LIMIT_EXCEEDED", str(exc)) from exc
    except BudgetExceeded as exc:
        raise AxiomError("LIMIT_EXCEEDED", str(exc)) from exc
    except QueryModelError as exc:
        raise AxiomError("VALIDATION_ERROR", str(exc)) from exc

    if operation == "changes":
        if not result.comparable:
            reason = _not_comparable_reason(result.status, arguments)
            return (), (), False, tuple(result.warnings) + (reason,)
        nodes, edges = _change_facts(result)
        return nodes, edges, False, tuple(result.warnings)

    if operation == "search":
        nodes = tuple(scope.node(item.node_id).as_document() for item in result.candidates)
        warnings = list(result.warnings)
        if result.ambiguous:
            warnings.append(_ambiguous_warning(require_text(arguments, "query"), len(nodes)))
        return tuple(nodes), (), bool(result.truncated), tuple(warnings)

    nodes = tuple(result.nodes)
    edges = tuple(result.edges)
    warnings = list(result.warnings)
    status = getattr(result, "status", "ok")
    if status == "ambiguous_target":
        nodes = tuple(result.candidates)
        edges = ()
        warnings.append(_ambiguous_warning(str(result.target or ""), len(nodes)))
    if getattr(result, "truncated", False):
        warnings.append(_truncation_warning(tuple(getattr(result, "reasons", ()))))
    if operation == "callers" and not getattr(result, "complete", True):
        warnings.append("incomplete: the caller closure is not complete for the requested bounds")
    if operation == "impact" and not getattr(result, "exhaustive", True):
        warnings.append(
            "potential_impact: this is static potential impact within the bounds, not proof of "
            "runtime damage and not proof that no other caller exists"
        )
    if operation == "path" and not result.found and not result.proven_absent:
        warnings.append(
            "path_not_proven: no path was found inside the bounds, which is not proof of absence"
        )
        return nodes, edges, True, tuple(warnings)
    truncated = bool(getattr(result, "truncated", False)) or bool(
        getattr(result, "budget_exhausted", False)
    )
    return nodes, edges, truncated, tuple(warnings)


def graph_query(arguments: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
    """Answer one bounded ``graph_query`` request, or refuse it for a named reason."""
    operation = _operation(arguments)
    closed_arguments(arguments, _GLOBAL_ARGUMENTS + _OPERATION_ARGUMENTS[operation])
    solution_id = identifier_argument(arguments, "solution_id")
    consistency = _consistency(arguments)
    pinned = _pinned_generation(arguments, consistency)
    if operation in _PROJECTION_OPERATIONS and arguments.get("projection") is not None:
        sections = _choice_list(arguments, "projection", PROJECTIONS) or ()
        if "source_locations" not in sections:
            raise AxiomError(
                "VALIDATION_ERROR",
                "projection must include 'source_locations': the response contract requires a "
                "source location on every node it carries",
                details={"field": "projection"},
            )

    # Authorizes the read and answers NOT_FOUND for an invisible or unregistered solution.
    context.open_solution(solution_id)
    projects = context.resolve_projects(solution_id, project_scope(arguments))
    if not projects:
        raise AxiomError(
            "NOT_FOUND",
            "no project of this solution is visible to this caller",
            details={"solution_id": solution_id},
        )
    if consistency == REQUIRE_FRESH:
        _require_fresh(context, solution_id, projects)

    scope = _load_scope(context, solution_id, projects)
    generation = catalog_generation_id(scope)
    if pinned is not None and pinned != generation:
        raise AxiomError(
            "SNAPSHOT_EXPIRED",
            "the pinned catalog generation is no longer the one this lane publishes; the answer "
            "would be a different vector under the same identity",
            details={"field": "catalog_generation_id"},
        )

    budgets = _budgets(arguments)
    _resolve_cursor(
        context,
        arguments,
        operation=operation,
        generation=generation,
        projects=projects,
        query=_cursor_query(arguments, operation),
    )

    nodes, edges, truncated, warnings = _answer(operation, arguments, scope)
    try:
        document = build_envelope(
            solution_id=solution_id,
            scope=scope,
            freshness=None,
            verification=Verification(),
            nodes=nodes,
            edges=edges,
            truncated=truncated,
            warnings=warnings,
        )
    except EnvelopeInvalid as exc:
        raise AxiomError("SNAPSHOT_CORRUPT", str(exc)) from exc
    try:
        packed = pack_response(document, max_bytes=budgets["max_bytes"])
    except BudgetExceeded as exc:
        raise AxiomError("LIMIT_EXCEEDED", str(exc)) from exc
    except BudgetInvalid as exc:
        raise AxiomError("VALIDATION_ERROR", str(exc)) from exc
    answer = dict(packed.document)
    if packed.truncated:
        # Trimming is a truncation of the answer and must travel as one.
        answer["truncated"] = True
    return answer


def _cursor_query(arguments: Mapping[str, Any], operation: str) -> dict[str, Any]:
    """The operation's own parameters, which the cursor binding is a function of."""
    fields = _OPERATION_ARGUMENTS[operation]
    return {field: arguments[field] for field in fields if field in arguments}
