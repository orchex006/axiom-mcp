"""C-032: ``graph_verify`` - a bounded verification request that reports each mode apart.

``repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md`` section 4 fixes the tool as "solution/project,
expected_fingerprint/target barrier" with the side effect "bounded verification request", and
``contracts/control-api-v1.md`` names ``VerifyCheckpoint`` as a job kind.

The card's AC1 is the rule this module is built around: **hash, schema, catalog and source
verification modes are reported separately, and an archive verification cannot assert current
filesystem state.** Collapsing four different claims into one "verified" boolean is exactly how a
consumer ends up believing an archived checkpoint proved something about the live working tree, so

* the answer always carries all four dimensions separately, each with its own ``mode``;
* every answer carries its ``basis`` (``archive`` or ``working_tree``), and a dimension that the
  daemon did not report is ``not_run`` with a named warning rather than silently treated as a pass;
* an unrecognised mode is reported as ``unknown`` with a warning, never upgraded;
* an answer whose ``basis`` is ``archive`` while its ``source`` dimension claims ``current`` is
  refused as
  ``DAEMON_UNAVAILABLE``, because an archived checkpoint cannot speak for the live filesystem.

Like the other delegation tools this module reads no snapshot, parses no source and writes no
shard: it authorizes, delegates through the :class:`~axiom_mcp.tools.context.ControlPlane`
protocol, and projects the daemon's answer through a closed allowlist.
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
    identifier_argument,
    optional_int,
    project_scope,
)

__all__ = [
    "BASES",
    "DIMENSION_MODES",
    "DIMENSIONS",
    "VERIFY_FIELDS",
    "graph_verify",
]

#: Where a verification was computed. A claim is only ever about one of these.
BASES = ("archive", "working_tree")

#: The four verification dimensions, reported separately and never merged.
DIMENSIONS = ("hash", "schema", "catalog", "source")

#: The modes each dimension may report. An unrecognised value becomes ``unknown``.
DIMENSION_MODES: Mapping[str, tuple[str, ...]] = {
    "hash": ("recomputed", "verified", "mismatch", "not_run", "unknown"),
    "schema": ("declared", "missing", "mismatch", "not_run", "unknown"),
    "catalog": ("matched", "mismatch", "not_run", "unknown"),
    "source": ("archive_contents", "current", "not_run", "unknown"),
}

#: The response keys and nothing else.
VERIFY_FIELDS = (
    "schema_version",
    "solution_id",
    "project_ids",
    "basis",
    "verification",
    "job",
    "warnings",
)

#: ``job.schema.json`` is normative for an asynchronous answer.
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

_FINGERPRINT = re.compile(r"^[0-9a-fA-F]{64}$")
_ARGUMENTS = (
    "solution_id",
    "project_id",
    "project_ids",
    "expected_fingerprint",
    "target_event_seq",
)
MAX_EVENT_SEQ = 2**53 - 1


def _fingerprint_argument(arguments: Mapping[str, Any]) -> str | None:
    """Return a validated lowercase fingerprint, refusing anything else."""
    value = arguments.get("expected_fingerprint")
    if value is None:
        return None
    if not isinstance(value, str) or not _FINGERPRINT.fullmatch(value.strip()):
        raise AxiomError(
            "VALIDATION_ERROR",
            "expected_fingerprint must be 64 hexadecimal characters",
            details={"field": "expected_fingerprint"},
        )
    return value.strip().lower()


def _non_negative_int(value: Any) -> int | None:
    """Return ``value`` only when it is a real non-negative integer, never a bool."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _dimension(name: str, raw: Any, warnings: list[str]) -> dict[str, Any]:
    """Project one verification dimension, never upgrading what the daemon actually said."""
    if not isinstance(raw, Mapping):
        warnings.append(f"dimension_not_reported:{name}")
        return {"mode": "not_run"}
    mode = raw.get("mode")
    allowed = DIMENSION_MODES[name]
    if not isinstance(mode, str) or mode not in allowed:
        warnings.append(f"unrecognised_mode:{name}")
        return {"mode": "unknown"}
    view: dict[str, Any] = {"mode": mode}
    for key in ("expected", "observed"):
        value = raw.get(key)
        if isinstance(value, str) and _FINGERPRINT.fullmatch(value.strip()):
            view[key] = value.strip().lower()
        elif value is not None:
            warnings.append(f"unusable_fingerprint:{name}.{key}")
    matched = raw.get("matched")
    if isinstance(matched, bool):
        view["matched"] = matched
    return view


def _job_view(answer: Mapping[str, Any]) -> dict[str, Any]:
    """Project an asynchronous verification answer through the job schema fields."""
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
    number = _non_negative_int(answer.get("retry_after_ms"))
    if number is not None:
        view["retry_after_ms"] = number
    return view


def graph_verify(arguments: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
    """Authorize a bounded verification request and report each verification mode apart."""
    closed_arguments(arguments, _ARGUMENTS)
    solution_id = identifier_argument(arguments, "solution_id")
    expected = _fingerprint_argument(arguments)
    barrier = optional_int(
        arguments, "target_event_seq", default=None, minimum=0, maximum=MAX_EVENT_SEQ
    )
    if expected is None and barrier is None:
        raise AxiomError(
            "VALIDATION_ERROR",
            "graph_verify needs an expected_fingerprint or a target_event_seq",
            details={"fields": ["expected_fingerprint", "target_event_seq"]},
        )
    requested = project_scope(arguments)

    # A checkpoint capability is a mutation-adjacent capability; it is checked before the daemon.
    context.principal.require(security.CAPABILITY_CHECKPOINT, solution_id=solution_id)
    try:
        context.registry.solution(solution_id)
    except RegistryError as exc:
        raise AxiomError("NOT_FOUND", str(exc)) from exc
    projects: tuple[str, ...] = ()
    if requested is not None:
        projects = context.resolve_projects(solution_id, requested)

    control = context.require_control()
    request: dict[str, Any] = {"solution_id": solution_id}
    if projects:
        request["project_ids"] = list(projects)
    if expected is not None:
        request["expected_fingerprint"] = expected
    if barrier is not None:
        request["target_event_seq"] = barrier
    try:
        answer = control.verify(request)
    except AxiomError:
        raise
    except Exception as exc:  # noqa: BLE001 - any transport failure is daemon unavailability
        raise AxiomError("DAEMON_UNAVAILABLE", "the graphd control plane is unreachable") from exc
    if not isinstance(answer, Mapping):
        raise AxiomError(
            "DAEMON_UNAVAILABLE", "the control plane answered with a non-object payload"
        )

    warnings: list[str] = []
    job: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    basis: str | None = None
    if "job_id" in answer:
        job = _job_view(answer)
        if "basis" in answer or "verification" in answer:
            warnings.append("verification_incomplete")
    else:
        raw_basis = answer.get("basis")
        if not isinstance(raw_basis, str) or raw_basis not in BASES:
            raise AxiomError(
                "DAEMON_UNAVAILABLE",
                "a verification answer must name whether it verified an archive or the "
                "working tree",
            )
        basis = raw_basis
        block = answer.get("verification")
        if not isinstance(block, Mapping):
            raise AxiomError(
                "DAEMON_UNAVAILABLE", "the control plane returned no verification dimensions"
            )
        verification = {name: _dimension(name, block.get(name), warnings) for name in DIMENSIONS}
        if basis == "archive" and verification["source"]["mode"] == "current":
            # An archived checkpoint cannot speak for the live filesystem. Refused rather than
            # relayed, because relaying it is exactly the overstatement AC1 forbids.
            raise AxiomError(
                "DAEMON_UNAVAILABLE",
                "an archive verification cannot assert current filesystem state",
                details={"dimension": "source", "basis": basis},
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "solution_id": solution_id,
        "project_ids": list(projects),
        "basis": basis,
        "verification": verification,
        "job": job,
        "warnings": warnings,
    }
