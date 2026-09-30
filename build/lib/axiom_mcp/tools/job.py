"""C-031: ``graph_job`` - bounded job status, or an explicit capability-checked cancel.

``repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md`` section 4 fixes the tool as "job_id; status/cancel
ตาม action", and ``contracts/control-api-v1.md`` says ``GET /v1/jobs/{id}`` returns
``job.schema.json`` "plus documented progress units" and that ``POST /v1/jobs/{id}/cancel`` is
idempotent and capability-scoped.

This module is the MCP side of that contract, and three properties are enforced here rather than
trusted to the daemon:

* **Bounded.** The handler makes *exactly one* control-plane call per request. It never loops and
  never sleeps, so a client cannot make the gateway spin on a queue; the daemon owns waiting and
  the handler passes a capped ``wait_ms``. ```MAX_JOB_CALLS``` is asserted by a test that counts
  the recorded calls.
* **Ownership verified.** A job answer names its ``solution_id``; a solution this token is not
  scoped to is ``NOT_FOUND``, the same answer an unknown job gets, so the tool cannot be used to
  probe another tenant's job ids.
* **Cancel optional by capability, and checked before the daemon.** A cancel is refused with
  ``FORBIDDEN`` *before* the control plane is consulted when the caller lacks ``reconcile``.
  A status read must ask the daemon who owns the job, so ownership for a read is enforced on the
  answer instead - the asymmetry is deliberate and is stated in ``docs/reference/mcp.md``.

The daemon's answer is projected through a closed allowlist. ``job.schema.json`` is normative, so
a state outside its enum is ``DAEMON_UNAVAILABLE`` rather than echoed through, and a percentage
field is dropped with a named warning: ``control-api-v1.md`` says "Do not invent percentages", and
this tool does not relay them either.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from axiom_mcp import security
from axiom_mcp.errors import AxiomError
from axiom_mcp.query.envelope import SCHEMA_VERSION
from axiom_mcp.registry import RegistryError
from axiom_mcp.tools.context import (
    ToolContext,
    closed_arguments,
    optional_int,
    require_text,
)

__all__ = [
    "JOB_ACTIONS",
    "JOB_FIELDS",
    "JOB_STATES",
    "MAX_JOB_CALLS",
    "PROGRESS_UNITS",
    "graph_job",
]

#: The two actions the catalog names.
JOB_ACTIONS = ("status", "cancel")

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

#: The documented progress units. A percentage is not one of them.
PROGRESS_UNITS = (
    "units_done",
    "units_total",
    "projects_done",
    "projects_total",
    "files_scanned",
    "shards_written",
)

#: The handler issues one bounded control-plane call per request. Not a loop, not a spin.
MAX_JOB_CALLS = 1

#: The response keys and nothing else.
JOB_FIELDS = ("schema_version", "action", "solution_id", "job", "warnings")

_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ARGUMENTS = ("job_id", "action", "wait_ms")
MAX_WAIT_MS = 30_000
NON_TERMINAL_STATES = ("PENDING", "LEASED", "RUNNING", "RETRY_WAIT", "CANCEL_REQUESTED")


def _job_id_argument(arguments: Mapping[str, Any]) -> str:
    """Return a syntactically bounded job id and nothing path-shaped."""
    value = require_text(arguments, "job_id")
    if not _JOB_ID.fullmatch(value):
        raise AxiomError(
            "VALIDATION_ERROR",
            "job_id must match ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
            details={"field": "job_id"},
        )
    return value


def _action_argument(arguments: Mapping[str, Any]) -> str:
    """Return the requested action, defaulting to a status read."""
    value = arguments.get("action")
    if value is None:
        return "status"
    if not isinstance(value, str) or value not in JOB_ACTIONS:
        raise AxiomError(
            "VALIDATION_ERROR",
            f"action must be one of {list(JOB_ACTIONS)}",
            details={"field": "action"},
        )
    return value


def _non_negative_int(value: Any) -> int | None:
    """Return ``value`` only when it is a real non-negative integer, never a bool."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _job_view(answer: Mapping[str, Any], warnings: list[str]) -> dict[str, Any]:
    """Project a job answer through ``job.schema.json`` plus the documented progress units."""
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
    solution_id = answer.get("solution_id")
    if not isinstance(solution_id, str) or not solution_id.strip():
        raise AxiomError(
            "DAEMON_UNAVAILABLE", "the control plane returned a job without its solution"
        )
    kind = answer.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        raise AxiomError("DAEMON_UNAVAILABLE", "the control plane returned a job without its kind")
    view: dict[str, Any] = {
        "job_id": job_id.strip(),
        "kind": kind.strip(),
        "state": state,
        "solution_id": solution_id.strip(),
    }
    for key in ("attempt", "fence", "target_event_seq", "retry_after_ms"):
        number = _non_negative_int(answer.get(key))
        if number is not None:
            view[key] = number
    error_code = answer.get("error_code")
    if isinstance(error_code, str) and error_code.strip():
        view["error_code"] = error_code.strip()[:128]
    progress = answer.get("progress")
    if isinstance(progress, Mapping):
        units: dict[str, int] = {}
        for key, value in progress.items():
            number = _non_negative_int(value)
            if number is None:
                continue
            name = str(key)
            if name in PROGRESS_UNITS:
                units[name] = number
            elif "percent" in name or name in {"pct", "percentage"}:
                warnings.append(f"percentage_not_carried:{name}")
        if units:
            view["progress"] = units
    return view


def graph_job(arguments: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read or cancel one job, with ownership verified on the answer."""
    closed_arguments(arguments, _ARGUMENTS)
    job_id = _job_id_argument(arguments)
    action = _action_argument(arguments)
    wait_ms = optional_int(arguments, "wait_ms", default=0, minimum=0, maximum=MAX_WAIT_MS)
    if action == "cancel" and wait_ms:
        raise AxiomError(
            "VALIDATION_ERROR",
            "wait_ms applies to a status read; a cancel is idempotent and does not wait",
            details={"field": "wait_ms"},
        )
    if action == "cancel":
        # Capability first: a read-only token never causes a control-plane write.
        context.principal.require(security.CAPABILITY_RECONCILE)

    control = context.require_control()
    warnings: list[str] = []
    try:
        answer = control.job(job_id, action=action, wait_ms=wait_ms)
    except AxiomError:
        raise
    except Exception as exc:  # noqa: BLE001 - any transport failure is daemon unavailability
        raise AxiomError("DAEMON_UNAVAILABLE", "the graphd control plane is unreachable") from exc
    if not isinstance(answer, Mapping):
        raise AxiomError(
            "DAEMON_UNAVAILABLE", "the control plane answered with a non-object payload"
        )

    job = _job_view(answer, warnings)
    solution_id = job["solution_id"]
    if not context.principal.visible(solution_id):
        # The same answer an unknown job gets, so job ids are not an enumeration oracle.
        raise AxiomError("NOT_FOUND", "no such job is visible to this caller")
    try:
        context.registry.solution(solution_id)
    except RegistryError as exc:
        raise AxiomError("NOT_FOUND", str(exc)) from exc
    job["terminal"] = job["state"] not in NON_TERMINAL_STATES

    return {
        "schema_version": SCHEMA_VERSION,
        "action": action,
        "solution_id": solution_id,
        "job": job,
        "warnings": warnings,
    }
