"""C-027: the response envelope that keeps generations, freshness and coverage honest.

``repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md`` section 7 separates two facts a caller must never
read as one: whether the pinned data is *current* (``freshness``) and how much of the requested
scope it actually *covers* (``coverage``). A generation that is complete for the profile can still
be stale, and a fresh generation can still be partial, so they are separate keys here and are never
collapsed into one another. The contract's ``verification`` block records *why* a freshness claim
is believed.

The one direction this module refuses to travel is the flattering one:

* freshness that was not reported, or reported outside the contract's allowlist, is ``unknown`` -
  never ``fresh``, because an absence of evidence is not currency;
* a ``fresh`` claim backed only by a watcher hint is downgraded to ``unknown`` with a warning,
  because a hint is not verification;
* a pinned member with no coverage block, or with a coverage status outside the allowlist, is
  refused outright: ``complete_for_profile``/``partial``/``unsupported`` cannot be honestly
  asserted from nothing, and guessing the flattering one would overstate the answer.

``catalog_generation_id`` is a digest of the *exact* member generations that were read, so a
response can be pinned back to what answered it and a re-issued query over the same generations
reproduces it. Members that could not be pinned are never folded into that list - they widen the
gap between what was asked and what was answered, which is why they cap coverage at ``partial``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from axiom_mcp.query.model import (
    Graph,
    GraphSet,
    QueryModelError,
    as_graph_set,
    identifier_text,
)

#: The response schema major this envelope is written against.
SCHEMA_VERSION = 1

#: Contract allowlist for ``freshness`` (``contracts/schemas/query-response.schema.json``).
FRESHNESS = ("fresh", "stale", "updating", "unknown", "invalid")

#: Contract allowlist for ``coverage`` (``contracts/schemas/coverage.schema.json``).
COVERAGE = ("complete_for_profile", "partial", "unsupported")

#: Coverage severities, least to most limiting. An aggregate keeps the most limiting member.
COVERAGE_SEVERITY = ("complete_for_profile", "partial", "unsupported")

#: Contract allowlist for ``verification.mode``.
VERIFICATION_MODES = ("inventory_hash", "watcher_hint", "none")

FRESHNESS_UNKNOWN = "unknown"
FRESHNESS_FRESH = "fresh"
COVERAGE_COMPLETE = "complete_for_profile"
COVERAGE_PARTIAL = "partial"
VERIFICATION_NONE = "none"
VERIFICATION_INVENTORY = "inventory_hash"
VERIFICATION_WATCHER = "watcher_hint"

#: Every key a success envelope may carry, and nothing else.
ENVELOPE_KEYS = frozenset(
    {
        "schema_version",
        "solution_id",
        "catalog_generation_id",
        "project_generations",
        "freshness",
        "coverage",
        "verification",
        "nodes",
        "edges",
        "truncated",
        "next_cursor",
        "warnings",
    }
)

_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


class EnvelopeInvalid(QueryModelError):
    """The envelope cannot be built without asserting something unevidenced or off-contract."""


def _fingerprint(value: Any, *, what: str) -> str:
    if not isinstance(value, str) or not _FINGERPRINT_RE.match(value):
        raise EnvelopeInvalid(f"{what} must be a 64-character lowercase hex digest, got {value!r}")
    return value


@dataclass(frozen=True)
class Verification:
    """Why a freshness claim is believed, in the contract's own vocabulary.

    ``inventory_hash`` is the strong mode: the source inventory was re-hashed, so the claim carries
    the ``source_fingerprint`` it recomputed. ``watcher_hint`` is a hint from a watcher and carries
    the time it was taken but proves nothing on its own. ``none`` carries neither, and a
    verification that contradicts its own mode is refused rather than stored.
    """

    mode: str = VERIFICATION_NONE
    verified_at: str | None = None
    source_fingerprint: str | None = None

    def __post_init__(self) -> None:
        mode = self.mode
        if not isinstance(mode, str) or mode not in VERIFICATION_MODES:
            raise EnvelopeInvalid(
                f"verification mode must be one of {list(VERIFICATION_MODES)}, got {mode!r}"
            )
        if self.verified_at is not None and (
            not isinstance(self.verified_at, str) or not self.verified_at
        ):
            raise EnvelopeInvalid("verification verified_at must be a non-empty string")
        if self.source_fingerprint is not None:
            _fingerprint(self.source_fingerprint, what="verification source_fingerprint")
        if mode == VERIFICATION_NONE:
            if self.verified_at is not None or self.source_fingerprint is not None:
                raise EnvelopeInvalid(
                    "verification mode 'none' cannot carry verified_at or source_fingerprint"
                )
        elif mode == VERIFICATION_INVENTORY:
            if self.source_fingerprint is None:
                raise EnvelopeInvalid(
                    "verification mode 'inventory_hash' must carry the source_fingerprint it "
                    "recomputed"
                )
        elif self.verified_at is None:
            raise EnvelopeInvalid(
                "verification mode 'watcher_hint' must carry the verified_at time of the hint"
            )

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {"mode": self.mode}
        if self.verified_at is not None:
            document["verified_at"] = self.verified_at
        if self.source_fingerprint is not None:
            document["source_fingerprint"] = self.source_fingerprint
        return document


def resolve_freshness(
    reported: str | None, verification: Verification
) -> tuple[str, tuple[str, ...]]:
    """Resolve the freshness to report, never upgrading an unknown claim to ``fresh``.

    Returns the status and the warnings that explain a downgrade, so a caller can see that a
    ``fresh`` claim was made and why it was not repeated.
    """
    if reported is None:
        return FRESHNESS_UNKNOWN, (
            "freshness was not reported, so it is unknown rather than fresh",
        )
    if not isinstance(reported, str) or reported not in FRESHNESS:
        return FRESHNESS_UNKNOWN, (
            f"freshness {reported!r} is not one of {list(FRESHNESS)}, so it is unknown; an "
            "unrecognised status is never read as fresh",
        )
    if reported == FRESHNESS_FRESH and verification.mode != VERIFICATION_INVENTORY:
        return FRESHNESS_UNKNOWN, (
            "freshness was reported as fresh but its verification mode is "
            f"{verification.mode!r}; only an inventory_hash verification supports fresh, so it is "
            "reported as unknown",
        )
    return reported, ()


def aggregate_coverage(
    scope: Graph | GraphSet | Iterable[Graph],
) -> tuple[str, tuple[str, ...]]:
    """Aggregate the pinned coverage blocks into the response's single coverage status.

    A member is kept only when it pins a coverage block whose status is in the allowlist; anything
    else is refused, because the alternative is to invent a status. The aggregate is the most
    limiting member, and a member that could not be pinned at all caps the answer at ``partial``:
    the caller asked about more than was answered.
    """
    graphs = as_graph_set(scope)
    warnings: list[str] = []
    worst = 0
    for graph in graphs.graphs:
        status = graph.coverage_status
        if not graph.coverage or status not in COVERAGE:
            raise EnvelopeInvalid(
                f"project {graph.project_id!r} pins no usable coverage status (got {status!r}); a "
                "response cannot assert coverage it has no evidence for"
            )
        worst = max(worst, COVERAGE_SEVERITY.index(status))
    if graphs.missing_projects:
        worst = max(worst, COVERAGE_SEVERITY.index(COVERAGE_PARTIAL))
        warnings.append(
            "coverage is partial because these requested projects are not pinned: "
            + ", ".join(repr(project) for project in graphs.missing_projects)
        )
    elif not graphs.project_ids:
        warnings.append("the pinned scope holds no projects, so this response covers nothing")
    return COVERAGE_SEVERITY[worst], tuple(warnings)


def catalog_generation_id(scope: Graph | GraphSet | Iterable[Graph]) -> str:
    """A deterministic digest of the exact member generations pinned into this response."""
    graphs = as_graph_set(scope)
    payload = json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "generations": [list(pair) for pair in graphs.generations()],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _items(values: Iterable[Mapping[str, Any]], *, what: str) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    for index, item in enumerate(values):
        if not isinstance(item, Mapping):
            raise EnvelopeInvalid(f"{what}[{index}] must be a mapping, got {type(item).__name__}")
        collected.append(dict(item))
    return collected


def _warnings(values: Iterable[str], *more: Iterable[str]) -> list[str]:
    collected: list[str] = []
    for group in (values, *more):
        for item in group:
            if not isinstance(item, str) or not item:
                raise EnvelopeInvalid(f"a warning must be a non-empty string, got {item!r}")
            collected.append(item)
    return collected


def build_envelope(
    *,
    solution_id: str,
    scope: Graph | GraphSet | Iterable[Graph],
    freshness: str | None,
    verification: Verification | None = None,
    nodes: Iterable[Mapping[str, Any]] = (),
    edges: Iterable[Mapping[str, Any]] = (),
    truncated: bool = False,
    next_cursor: str | None = None,
    warnings: Iterable[str] = (),
) -> dict[str, Any]:
    """Assemble one success envelope: exact generations, freshness, coverage and verification.

    The document carries only the contract's success keys. ``freshness`` and ``coverage`` are
    resolved independently, and neither is inferred from the other: a complete-for-profile answer
    over a generation with no freshness evidence is `freshness=unknown` and
    ``coverage=complete_for_profile``.
    """
    graphs = as_graph_set(scope)
    try:
        solution = identifier_text(solution_id, what="solution_id")
    except QueryModelError as exc:
        raise EnvelopeInvalid(str(exc)) from exc
    proof = Verification() if verification is None else verification
    if not isinstance(proof, Verification):
        raise EnvelopeInvalid(f"verification must be a Verification, got {type(proof).__name__}")
    status, freshness_warnings = resolve_freshness(freshness, proof)
    coverage, coverage_warnings = aggregate_coverage(graphs)
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "solution_id": solution,
        "catalog_generation_id": catalog_generation_id(graphs),
        "project_generations": [
            {"project_id": project, "generation_id": generation}
            for project, generation in graphs.generations()
        ],
        "freshness": status,
        "coverage": coverage,
        "verification": proof.as_document(),
        "nodes": _items(nodes, what="nodes"),
        "edges": _items(edges, what="edges"),
        "truncated": bool(truncated),
        "warnings": _warnings(warnings, freshness_warnings, coverage_warnings),
    }
    if next_cursor is not None:
        if not isinstance(next_cursor, str) or not next_cursor:
            raise EnvelopeInvalid("next_cursor must be a non-empty string when present")
        document["next_cursor"] = next_cursor
    return document


__all__ = [
    "COVERAGE",
    "COVERAGE_COMPLETE",
    "COVERAGE_PARTIAL",
    "COVERAGE_SEVERITY",
    "ENVELOPE_KEYS",
    "FRESHNESS",
    "FRESHNESS_FRESH",
    "FRESHNESS_UNKNOWN",
    "SCHEMA_VERSION",
    "VERIFICATION_INVENTORY",
    "VERIFICATION_MODES",
    "VERIFICATION_NONE",
    "VERIFICATION_WATCHER",
    "EnvelopeInvalid",
    "Verification",
    "aggregate_coverage",
    "build_envelope",
    "catalog_generation_id",
    "resolve_freshness",
]
