"""C-030: ``graph_reconcile`` - enqueue graphd work without doing any of it here.

``repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md`` section 4 fixes the tool's input as
``solution, projects, scope, wait_timeout_ms, reason`` and its side effect as "enqueue graphd
job". The card's AC1 is the reason this boundary is drawn where it is: an authorized request
causes *Rust* work to be queued and returns a job handle, while the Python gateway never parses
a source file and never writes a graph shard. This module therefore imports neither the reader,
the query engine nor the shard writer - it authorizes, delegates through the
:class:`~axiom_mcp.tools.context.ControlPlane` protocol, and projects the daemon's answer
through a closed allowlist.

Three refusals are deliberate, and each has a test:

* a caller without the ``reconcile`` capability is ``FORBIDDEN`` before the control plane is
  consulted, because holding a read token is not holding a mutation capability;
* a solution outside the token's scope is ``NOT_FOUND`` - the same answer an unregistered
  solution gets - so the tool cannot be used as a solution-enumeration oracle;
* a request with no audit ``reason``, or a project selector combined with ``scope != project``,
  is a ``VALIDATION_ERROR`` instead of a guess about what the caller meant.

The daemon's answer is never passed through verbatim. :func:`_job_view` reads only the fields
``contracts/schemas/job.schema.json`` names, and a state outside that schema's enum is refused as
an unusable daemon answer rather than echoed back to the caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from axiom_mcp import security
from axiom_mcp.errors import AxiomError
from axiom_mcp.query.envelope import SCHEMA_VERSION, VERIFICATION_MODES
from axiom_mcp.registry import RegistryError
from axiom_mcp.tools.context import (
    ToolContext,
    closed_arguments,
    identifier_argument,
    optional_int,
    project_scope,
    require_text,
)

__all__ = ["RECONCILE_FIELDS", "RECONCILE_SCOPES", "graph_reconcile"]

#: The scopes ``contracts/control-api-v1.md`` defines. ``scope=project`` requires explicit
#: project ids; ``scope=full`` is explicit owner-authorized work.
RECONCILE_SCOPES = ("dirty", "project", "full")

#: The response keys and nothing else.
RECONCILE_FIELDS = (
    "schema_version",
    "solution_id",
    "scope",
    "project_ids",
    "job",
    "publication",
    "warnings",
)

#: ``contracts/schemas/job.schema.json`` is normative; an unknown state is not passed through.
JOB_STATES = (
    "PENDING",
    "LEASED",
    "RUNNING",
    "RETRY_WAIT",
    "SUCCEEDED",
    "FAILED",
    "CANCEL_REQUESTED",
    "CANCELLED",
    "SUPERSEDED",
)

_ARGUMENTS = (
    "solution_id",
    "project_id",
    "project_ids",
    "scope",
    "wait_timeout_ms",
    "reason",
)

#: An audit reason is required and bounded: it travels to the daemon's journal, not into an
#: unbounded request body.
MAX_REASON_LENGTH = 200

#: A bounded ``wait_timeout_ms``. Long parsing never holds the request open by default, and the
#: MCP tool is not allowed to park an HTTP request on a queue.
MAX_WAIT_MS = 30_000


def _scope_argument(arguments: Mapping[str, Any]) -> str:
    """Return the requested reconcile scope, defaulting to ``dirty``."""
    value = arguments.get("scope")
    if value is None:
        return "dirty"
    if not isinstance(value, str) or value not in RECONCILE_SCOPES:
        raise AxiomError(
            "VALIDATION_ERROR",
            f"scope must be one of {list(RECONCILE_SCOPES)}",
            details={"field": "scope"},
        )
    return value


def _reason_argument(arguments: Mapping[str, Any]) -> str:
    """Return the required audit reason, refusing an over-long one."""
    reason = require_text(arguments, "reason")
    if len(reason) > MAX_REASON_LENGTH:
        raise AxiomError(
            "LIMIT_EXCEEDED",
            f"reason must be at most {MAX_REASON_LENGTH} characters",
            details={"field": "reason", "maximum": MAX_REASON_LENGTH},
        )
    return reason


def _non_negative_int(value: Any) -> int | None:
    """Return ``value`` only when it is a real non-negative integer, never a bool."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _job_view(answer: Mapping[str, Any]) -> dict[str, Any]:
    """Project a queued-job answer through the fields ``job.schema.json`` fixes."""
    job_id = answer.get("job_id")
    if not isinstance(job_id, str) or not job_id.strip():
        raise AxiomError("DAEMON_UNAVAILABLE", "the control plane did not return a job id")
    state = answer.get("state")
    if not isinstance(state, str) or state not in JOB_STATES:
        raise AxiomError(
            "DAEMON_UNAVAILABLE",
            "the control plane returned an unknown job state",
            details={"state": str(state)[:64]},
        )
    view: dict[str, Any] = {"job_id": job_id.strip(), "state": state}
    for key in ("target_event_seq", "retry_after_ms"):
        number = _non_negative_int(answer.get(key))
        if number is not None:
            view[key] = number
    return view


def _publication_view(answer: Mapping[str, Any]) -> dict[str, Any]:
    """Project a ``200`` answer (an existing publication satisfies the barrier)."""
    generation = answer.get("catalog_generation_id")
    if not isinstance(generation, str) or not generation.strip():
        raise AxiomError(
            "DAEMON_UNAVAILABLE",
            "the control plane answered neither a job handle nor a publication",
        )
    view: dict[str, Any] = {"catalog_generation_id": generation.strip()}
    verification = answer.get("verification")
    if isinstance(verification, Mapping):
        mode = verification.get("mode")
        if isinstance(mode, str) and mode in VERIFICATION_MODES:
            view["verification"] = {"mode": mode}
    dirty = answer.get("dirty")
    if isinstance(dirty, Mapping):
        counted = {
            str(key): number
            for key, value in dirty.items()
            if (number := _non_negative_int(value)) is not None
        }
        if counted:
            view["dirty"] = counted
    return view


def graph_reconcile(arguments: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
    """Authorize a reconcile request, delegate it, and return its job handle."""
    closed_arguments(arguments, _ARGUMENTS)
    solution_id = identifier_argument(arguments, "solution_id")
    scope = _scope_argument(arguments)
    reason = _reason_argument(arguments)
    wait_ms = optional_int(arguments, "wait_timeout_ms", default=0, minimum=0, maximum=MAX_WAIT_MS)
    requested = project_scope(arguments)
    if scope == "project" and not requested:
        raise AxiomError(
            "VALIDATION_ERROR",
            "scope=project requires an explicit project_id or project_ids",
            details={"field": "scope"},
        )
    if scope != "project" and requested:
        raise AxiomError(
            "VALIDATION_ERROR",
            "a project selector is only valid with scope=project",
            details={"fields": ["scope", "project_ids"]},
        )

    # Authorization first: an invisible solution never reaches the registry lookup, and a
    # read-only token never reaches the control plane.
    context.principal.require(security.CAPABILITY_RECONCILE, solution_id=solution_id)
    try:
        context.registry.solution(solution_id)
    except RegistryError as exc:
        raise AxiomError("NOT_FOUND", str(exc)) from exc

    projects: tuple[str, ...] = ()
    if requested is not None:
        projects = context.resolve_projects(solution_id, requested)

    control = context.require_control()
    request: dict[str, Any] = {
        "solution_id": solution_id,
        "scope": scope,
        "reason": reason,
        "wait_timeout_ms": wait_ms,
    }
    if projects:
        request["project_ids"] = list(projects)
    try:
        answer = control.reconcile(request)
    except AxiomError:
        raise
    except Exception as exc:  # noqa: BLE001 - any transport failure is daemon unavailability
        raise AxiomError("DAEMON_UNAVAILABLE", "the graphd control plane is unreachable") from exc
    if not isinstance(answer, Mapping):
        raise AxiomError(
            "DAEMON_UNAVAILABLE", "the control plane answered with a non-object payload"
        )

    job: dict[str, Any] | None = None
    publication: dict[str, Any] | None = None
    if "job_id" in answer:
        job = _job_view(answer)
    elif "catalog_generation_id" in answer:
        publication = _publication_view(answer)
    else:
        raise AxiomError(
            "DAEMON_UNAVAILABLE",
            "the control plane answered neither a job handle nor a publication",
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "solution_id": solution_id,
        "scope": scope,
        "project_ids": list(projects),
        "job": job,
        "publication": publication,
        "warnings": [],
    }
