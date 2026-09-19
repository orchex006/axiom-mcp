"""Recovery for missing, corrupt and archived snapshots, without inventing freshness.

``docs/12-SNAPSHOT-READ-WRITE-PROTOCOL.md`` section 5 step 6 reads the required shards of a
pinned generation; the paragraph after step 8 is the rule this module implements: if a required
generation is missing or corrupt, drop the partial request, retry bounded against the *whole
catalog*, and if it still fails return ``SNAPSHOT_UNAVAILABLE`` or ``SNAPSHOT_CORRUPT`` and ask
control to reconcile. It is explicit that half a new answer and half an old one is not allowed.

Three further rules from the owner seed ``repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md`` shape the
classification:

* section 5 - an immutable historical generation does not imply live source freshness, and if an
  old generation has been collected the answer is ``SNAPSHOT_EXPIRED`` rather than a read of
  whatever ``current.json`` names now;
* section 7 - when the daemon is offline but a checkpoint is valid, a query may be served with
  ``freshness=unknown`` in snapshot-only mode, and an unsupported major or corruption must be
  rejected rather than served with a warning.

``docs/14-MULTI-PROJECT-SOLUTIONS.md`` section 6 adds the member rule: a missing member is
``PROJECT_UNAVAILABLE`` with partial coverage, never an empty graph.

So this module refuses to guess. :func:`read_with_recovery` runs a caller-supplied loader that
pins one exact catalog vector; each attempt returns a whole vector or raises, so a failed attempt
leaves nothing behind and a served answer can never mix two attempts. The number of attempts is
bounded by :class:`RecoveryLimits`. Failures are mapped to the canonical codes by
:func:`canonical_code_for`, and :func:`require_generation` distinguishes a generation that is
merely absent from one the lane has moved past, which is the difference between
``SNAPSHOT_UNAVAILABLE`` and ``SNAPSHOT_EXPIRED``.

Freshness is never fabricated. Snapshot-only mode forces ``freshness=unknown`` and refuses a
caller that tries to hand it a live freshness reading; a pinned-consistency read is treated the
same way, because an immutable generation is not evidence that the source is fresh.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axiom_mcp import manifest as manifest_module
from axiom_mcp.catalog import (
    CatalogError,
    CatalogInvalid,
    CatalogMemberMissing,
    CatalogUnreadable,
    CatalogVector,
    UnsupportedCatalogMajor,
)
from axiom_mcp.errors import RETRYABLE_CODES, AxiomError, error_payload
from axiom_mcp.manifest import (
    ByteSizeMismatch,
    CanonicalSerializationError,
    DigestMismatch,
    DuplicateManifestEntry,
    ManifestError,
    ManifestInvalid,
    ManifestUnreadable,
    NotCanonicalBytes,
    RecordCountMismatch,
    UnsupportedSchemaMajor,
)
from axiom_mcp.registry import RegistryError, SnapshotLocation
from axiom_mcp.shards import (
    ShardError,
    ShardPathRejected,
    ShardSizeMismatch,
    ShardTooLarge,
    ShardUnreadable,
)

ALLOW_STALE = "allow_stale"
REQUIRE_FRESH = "require_fresh"
PINNED = "pinned"
CONSISTENCIES: tuple[str, ...] = (ALLOW_STALE, REQUIRE_FRESH, PINNED)

ALLOW_PARTIAL = "allow_partial"
REQUIRE_COMPLETE = "require_complete"
COVERAGE_POLICIES: tuple[str, ...] = (ALLOW_PARTIAL, REQUIRE_COMPLETE)

LIVE_MODE = "live"
SNAPSHOT_ONLY = "snapshot_only"
MODES: tuple[str, ...] = (LIVE_MODE, SNAPSHOT_ONLY)

FRESHNESS_STATUSES: tuple[str, ...] = ("fresh", "stale", "updating", "unknown", "invalid")
FRESHNESS_UNKNOWN = "unknown"

VERIFICATION_MANIFEST_HASH = "manifest_hash"
VERIFICATION_NONE = "none"

SERVED = "served"
PARTIAL = "partial"
FAILED = "failed"
STATUSES: tuple[str, ...] = (SERVED, PARTIAL, FAILED)

SNAPSHOT_UNAVAILABLE = "SNAPSHOT_UNAVAILABLE"
SNAPSHOT_CORRUPT = "SNAPSHOT_CORRUPT"
SNAPSHOT_EXPIRED = "SNAPSHOT_EXPIRED"
PROJECT_UNAVAILABLE = "PROJECT_UNAVAILABLE"

# A missing or corrupt generation is retried within the bound (protocol section 5); so is a
# missing member, which errors.RETRYABLE_CODES already marks retryable. A collected generation is
# not retried, because reading again cannot bring it back.
RECOVERABLE_CODES = frozenset(RETRYABLE_CODES) | {SNAPSHOT_CORRUPT}


class RecoveryError(ValueError):
    """A recovery configuration or loader the layer must refuse."""


class RecoveryRejected(RecoveryError):
    """A policy, mode, consistency or freshness value the layer cannot honour."""


class GenerationCollected(Exception):
    """The loader's signal that the pinned generation is no longer published.

    A collected generation is ``SNAPSHOT_EXPIRED``: it must not be silently replaced with
    whatever the lane currently points at.
    """

    def __init__(self, generation_id: str, message: str | None = None) -> None:
        self.generation_id = generation_id
        super().__init__(
            message
            or f"pinned generation {generation_id[:12]} has been collected; "
            "it cannot be served against the current data"
        )


class SnapshotGone(Exception):
    """An absent generation directory, carrying the canonical code it must map to."""

    def __init__(self, code: str, generation_id: str, message: str) -> None:
        self.code = code
        self.generation_id = generation_id
        super().__init__(message)


def classify_absence(location: SnapshotLocation, generation_id: str) -> str:
    """Return the canonical code for a generation directory that does not exist.

    If the lane still names this generation, the bytes are simply unavailable. If the lane names
    a *different* generation, the pinned one has been superseded and collected, which is
    ``SNAPSHOT_EXPIRED`` and must never be answered from the newer data.
    """
    if not isinstance(location, SnapshotLocation):
        raise RecoveryRejected("a resolved snapshot location is required to classify absence")
    try:
        pointer = manifest_module.load_pointer(location.pointer)
    except ManifestError:
        return SNAPSHOT_UNAVAILABLE
    if pointer.directory_name == generation_id:
        return SNAPSHOT_UNAVAILABLE
    return SNAPSHOT_EXPIRED


def require_generation(location: SnapshotLocation, generation_id: str) -> Path:
    """Return the pinned generation directory, or raise the honest canonical failure."""
    directory = location.generation_root(generation_id)
    if directory.is_dir():
        return directory
    code = classify_absence(location, generation_id)
    if code == SNAPSHOT_EXPIRED:
        raise SnapshotGone(
            code,
            generation_id,
            f"pinned generation {generation_id[:12]} is no longer published; the lane has "
            "moved on, so this is expired rather than a read of the current generation",
        )
    raise SnapshotGone(
        code,
        generation_id,
        f"generation {generation_id[:12]} is not present in the lane",
    )


def canonical_code_for(exc: BaseException) -> str:
    """Map a read failure to the canonical code that describes it."""
    if isinstance(exc, GenerationCollected):
        return SNAPSHOT_EXPIRED
    if isinstance(exc, SnapshotGone):
        return exc.code
    if isinstance(exc, AxiomError):
        return exc.code
    if isinstance(exc, CatalogMemberMissing):
        return PROJECT_UNAVAILABLE
    if isinstance(exc, (CatalogUnreadable, ManifestUnreadable, ShardUnreadable)):
        return SNAPSHOT_UNAVAILABLE
    if isinstance(exc, (RegistryError, OSError)):
        return SNAPSHOT_UNAVAILABLE
    if isinstance(exc, (CatalogInvalid, UnsupportedCatalogMajor)):
        return SNAPSHOT_CORRUPT
    if isinstance(
        exc,
        (
            ManifestInvalid,
            UnsupportedSchemaMajor,
            DigestMismatch,
            NotCanonicalBytes,
            ByteSizeMismatch,
            RecordCountMismatch,
            CanonicalSerializationError,
            DuplicateManifestEntry,
        ),
    ):
        return SNAPSHOT_CORRUPT
    if isinstance(exc, (ShardPathRejected, ShardTooLarge, ShardSizeMismatch)):
        return SNAPSHOT_CORRUPT
    if isinstance(exc, (CatalogError, ManifestError, ShardError)):
        return SNAPSHOT_CORRUPT
    return "INTERNAL_ERROR"


@dataclass(frozen=True)
class RecoveryLimits:
    """How many whole-catalog attempts one bounded reconcile may make."""

    max_attempts: int = 2

    def __post_init__(self) -> None:
        value = self.max_attempts
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise RecoveryRejected(f"max_attempts must be a positive integer, got {value!r}")


@dataclass(frozen=True)
class RecoveryResult:
    """One answer: a whole pinned vector, a partial vector, or a canonical error and no data."""

    status: str
    mode: str
    consistency: str
    coverage: str
    freshness: str
    verification: str
    attempts: int
    vector: CatalogVector | None = None
    error: AxiomError | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.status != FAILED

    @property
    def partial(self) -> bool:
        return self.status == PARTIAL

    def generations(self) -> dict[str, str]:
        """``project_id -> generation_id`` for the members actually served."""
        return dict(self.vector.generations()) if self.vector is not None else {}

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "status": self.status,
            "coverage": self.coverage,
            "freshness": self.freshness,
            "verification": self.verification,
            "mode": self.mode,
            "consistency": self.consistency,
            "attempts": self.attempts,
            "project_generations": self.generations(),
            "warnings": list(self.warnings),
        }
        if self.error is not None:
            document["error"] = error_payload(self.error)
        return document


def _require_choice(value: str, allowed: tuple[str, ...], name: str) -> None:
    if value not in allowed:
        raise RecoveryRejected(f"{name} must be one of {list(allowed)}, got {value!r}")


def _resolve_freshness(mode: str, consistency: str, runtime_freshness: str | None) -> str:
    """Return the freshness to report, refusing any value the layer cannot honestly support."""
    if runtime_freshness is not None:
        _require_choice(runtime_freshness, FRESHNESS_STATUSES, "runtime_freshness")
    if mode == SNAPSHOT_ONLY and runtime_freshness not in (None, FRESHNESS_UNKNOWN):
        raise RecoveryRejected(
            "snapshot-only mode cannot report a live freshness reading "
            f"{runtime_freshness!r}; it must report {FRESHNESS_UNKNOWN!r}"
        )
    if consistency == PINNED and runtime_freshness not in (None, FRESHNESS_UNKNOWN):
        raise RecoveryRejected(
            "a pinned historical generation does not imply live source freshness; "
            f"report {FRESHNESS_UNKNOWN!r} rather than {runtime_freshness!r}"
        )
    if mode == SNAPSHOT_ONLY or consistency == PINNED:
        return FRESHNESS_UNKNOWN
    return runtime_freshness or FRESHNESS_UNKNOWN


def _to_axiom_error(code: str, exc: BaseException) -> AxiomError:
    details: dict[str, Any] = {"error_code": code}
    generation_id = getattr(exc, "generation_id", None)
    if isinstance(generation_id, str) and generation_id:
        details["generation_id"] = generation_id
    project_id = getattr(exc, "project_id", None)
    if isinstance(project_id, str) and project_id:
        details["project_id"] = project_id
    return AxiomError(code, str(exc) or code, details=details, cause=exc)


def _missing_members(vector: CatalogVector) -> tuple[str, ...]:
    return tuple(pin.member.project_id for pin in vector.missing)


def read_with_recovery(
    load: Callable[[], CatalogVector],
    *,
    policy: str = ALLOW_PARTIAL,
    mode: str = LIVE_MODE,
    consistency: str = ALLOW_STALE,
    limits: RecoveryLimits | None = None,
    runtime_freshness: str | None = None,
) -> RecoveryResult:
    """Run ``load`` under a bounded retry and return one whole answer or one canonical failure.

    ``load`` must pin one exact catalog vector - the whole vector, from one catalog generation -
    and raise on any failure. Because each attempt is all-or-nothing, a retry can never leave a
    half-read vector behind, and a failure returns ``vector=None`` with a canonical error.
    """
    if not callable(load):
        raise RecoveryRejected("a callable loader is required")
    _require_choice(policy, COVERAGE_POLICIES, "policy")
    _require_choice(mode, MODES, "mode")
    _require_choice(consistency, CONSISTENCIES, "consistency")
    effective_limits = limits or RecoveryLimits()
    freshness = _resolve_freshness(mode, consistency, runtime_freshness)
    verification = VERIFICATION_MANIFEST_HASH if mode == SNAPSHOT_ONLY else VERIFICATION_NONE

    attempts = 0
    last_error: AxiomError | None = None
    for index in range(1, effective_limits.max_attempts + 1):
        attempts = index
        vector: CatalogVector | None = None
        try:
            vector = load()
            if not isinstance(vector, CatalogVector):
                raise RecoveryRejected(
                    f"the loader must return one pinned CatalogVector, got {type(vector).__name__}"
                )
            if vector.missing and policy == REQUIRE_COMPLETE:
                names = ", ".join(_missing_members(vector))
                raise CatalogMemberMissing(
                    names,
                    f"require_complete_solution rejected a partial vector; unavailable "
                    f"members: {names}",
                )
        except RecoveryRejected:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:  # noqa: BLE001 - classified into a canonical code
            code = canonical_code_for(exc)
            last_error = _to_axiom_error(code, exc)
            if code in RECOVERABLE_CODES and index < effective_limits.max_attempts:
                continue
            break
        if vector.missing:
            names = ", ".join(_missing_members(vector))
            return RecoveryResult(
                status=PARTIAL,
                mode=mode,
                consistency=consistency,
                coverage=vector.effective_coverage,
                freshness=freshness,
                verification=verification,
                attempts=attempts,
                vector=vector,
                warnings=(
                    f"project(s) {names} are unavailable; this answer covers only the "
                    "available members, never an empty graph for the missing ones",
                ),
            )
        return RecoveryResult(
            status=SERVED,
            mode=mode,
            consistency=consistency,
            coverage=vector.effective_coverage,
            freshness=freshness,
            verification=verification,
            attempts=attempts,
            vector=vector,
        )

    if last_error is None:
        raise RecoveryRejected("the bounded retry ended without a result or a classified failure")
    return RecoveryResult(
        status=FAILED,
        mode=mode,
        consistency=consistency,
        coverage="unsupported",
        freshness=freshness,
        verification=VERIFICATION_NONE,
        attempts=attempts,
        vector=None,
        error=last_error,
        warnings=("the request was dropped whole; no partial or mixed-generation data is served",),
    )


__all__ = [
    "ALLOW_PARTIAL",
    "ALLOW_STALE",
    "CONSISTENCIES",
    "COVERAGE_POLICIES",
    "FAILED",
    "FRESHNESS_STATUSES",
    "FRESHNESS_UNKNOWN",
    "GenerationCollected",
    "LIVE_MODE",
    "MODES",
    "PARTIAL",
    "PINNED",
    "PROJECT_UNAVAILABLE",
    "RECOVERABLE_CODES",
    "REQUIRE_COMPLETE",
    "REQUIRE_FRESH",
    "RecoveryError",
    "RecoveryLimits",
    "RecoveryRejected",
    "RecoveryResult",
    "SERVED",
    "SNAPSHOT_CORRUPT",
    "SNAPSHOT_EXPIRED",
    "SNAPSHOT_ONLY",
    "SNAPSHOT_UNAVAILABLE",
    "STATUSES",
    "SnapshotGone",
    "VERIFICATION_MANIFEST_HASH",
    "VERIFICATION_NONE",
    "canonical_code_for",
    "classify_absence",
    "read_with_recovery",
    "require_generation",
]
