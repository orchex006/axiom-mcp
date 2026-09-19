"""Readiness and graceful shutdown for the composed axiom-mcp gateway.

Readiness and health are different claims, and this module keeps them apart.
The healthz probe answers "the process is up"; readiness answers "this gateway
can actually answer a query". A gateway that reported ready before its SDK was
initialized, or while its query plane was unusable, would be lying to its
supervisor, so readiness here requires both and reports the query and control
planes separately.

Shutdown is bounded for the same reason it is honest. An in-flight query is
given a deadline to finish; when the deadline expires the remaining work is
counted as cancelled and the guard is released anyway, because a restart that
inherited a stranded lock would be a worse failure than a cancelled query. Every
path that admits a query therefore releases the guard through the same context
manager, whether the query completed, raised, or was cut short.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from axiom_mcp.errors import AxiomError
from axiom_mcp.guard.engine import SolutionGuard

__all__ = [
    "CONTROL_COMPONENT",
    "DEFAULT_DRAIN_TIMEOUT_S",
    "MAX_DRAIN_TIMEOUT_S",
    "NOT_READY",
    "QUERY_COMPONENT",
    "ComponentReadiness",
    "DrainOutcome",
    "Lifecycle",
    "ReadinessReport",
    "default_readiness",
]

# The two planes are named here, not derived from the transport, so a reader of
# a readiness report can match a name to the component that owns it.
QUERY_COMPONENT = "query"
CONTROL_COMPONENT = "control"

# Canonical error code from contracts/error-codes.json (HTTP 503): the process
# is up but a required capability is not available yet.
NOT_READY = "NOT_READY"

DEFAULT_DRAIN_TIMEOUT_S = 5.0
MAX_DRAIN_TIMEOUT_S = 60.0

# Default detail strings. They are shared with the transport-only default so a
# caller cannot tell "no probe was ever wired" from "the probe says unavailable"
# by accident: both say the plane is not wired.
QUERY_NOT_WIRED = "query data plane not wired"
CONTROL_NOT_WIRED = "graphd control client not wired"

SDK_NOT_INITIALIZED = "sdk_not_initialized"


@dataclass(frozen=True)
class ComponentReadiness:
    """One plane's availability, reported separately from the other."""

    name: str
    available: bool
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"available": self.available}
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class ReadinessReport:
    """Query and control readiness, kept distinct on purpose."""

    query: ComponentReadiness
    control: ComponentReadiness

    @property
    def ready(self) -> bool:
        return self.query.available and self.control.available

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "query": self.query.as_dict(),
            "control": self.control.as_dict(),
            "error": None if self.ready else NOT_READY,
        }


def default_readiness() -> ReadinessReport:
    """Transport-only default: the process serves, but no data plane is wired.

    This reports unavailable rather than assuming a capability exists, so the
    readiness probe cannot claim readiness the gateway has not earned.
    """
    return ReadinessReport(
        query=ComponentReadiness(QUERY_COMPONENT, False, QUERY_NOT_WIRED),
        control=ComponentReadiness(CONTROL_COMPONENT, False, CONTROL_NOT_WIRED),
    )


@dataclass(frozen=True)
class DrainOutcome:
    """What a bounded drain observed.

    finished counts queries that completed before the drain returned; remaining
    is what was still in flight when the deadline expired, and cancelled is how
    many of those the drain cut short. guards_held is read after the release
    attempt, so an empty tuple is the proof a restart cannot inherit a stranded
    lock.
    """

    drained: bool
    finished: int
    cancelled: int
    remaining: int
    elapsed_s: float
    guards_held: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "drained": self.drained,
            "finished": self.finished,
            "cancelled": self.cancelled,
            "remaining": self.remaining,
            "elapsed_s": self.elapsed_s,
            "guards_held": list(self.guards_held),
        }


def _probe_query(probe: Callable[[], Any] | None) -> tuple[bool, str | None]:
    """Ask a query probe for (available, detail), never letting it raise through.

    A probe that throws is a probe that failed, not evidence of availability, so
    the exception is reported as its type name and the plane stays unavailable.
    """
    if probe is None:
        return False, QUERY_NOT_WIRED
    try:
        result = probe()
    except Exception as exc:  # noqa: BLE001 - a probe's failure is data, not fatal
        return False, f"query_probe_failed:{type(exc).__name__}"
    if isinstance(result, ComponentReadiness):
        return result.available, result.detail
    return bool(result), None


class Lifecycle:
    """Readiness plus bounded, guard-releasing shutdown for one gateway.

    The object is small on purpose: it tracks whether the SDK was initialized,
    whether the query and control planes are usable, how many queries are in
    flight, and whether a drain has started. It owns no protocol and no policy;
    it is the piece that turns "we are shutting down" into "and here is the
    proof that no guard was left behind".
    """

    def __init__(
        self,
        *,
        guard: SolutionGuard | None = None,
        query_probe: Callable[[], Any] | None = None,
        drain_timeout_s: float = DEFAULT_DRAIN_TIMEOUT_S,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if isinstance(drain_timeout_s, bool) or not isinstance(drain_timeout_s, (int, float)):
            raise AxiomError("LIMIT_EXCEEDED", "drain_timeout_s must be a number of seconds")
        if not 0 < drain_timeout_s <= MAX_DRAIN_TIMEOUT_S:
            raise AxiomError(
                "LIMIT_EXCEEDED",
                f"drain_timeout_s must be in (0, {MAX_DRAIN_TIMEOUT_S}], got {drain_timeout_s!r}",
            )
        self._guard = guard
        self._query_probe = query_probe
        self._drain_timeout_s = float(drain_timeout_s)
        self._clock = clock or time.monotonic
        self._condition = threading.Condition()
        self._in_flight = 0
        self._finished = 0
        self._cancelled = 0
        self._draining = False
        self._shutdown = False
        self._outcome: DrainOutcome | None = None
        self._sdk_initialized = False
        self._query_available = False
        self._query_detail: str | None = QUERY_NOT_WIRED
        self._control_available = False
        self._control_detail: str | None = CONTROL_NOT_WIRED

    # -- readiness ---------------------------------------------------------

    def mark_sdk_initialized(self) -> None:
        """Record that the pinned SDK finished its lifespan startup."""
        with self._condition:
            self._sdk_initialized = True
            self._condition.notify_all()

    def mark_query_available(self, available: bool = True, detail: str | None = None) -> None:
        with self._condition:
            self._query_available = bool(available)
            self._query_detail = None if available else detail
            self._condition.notify_all()

    def mark_control_available(self, available: bool = True, detail: str | None = None) -> None:
        with self._condition:
            self._control_available = bool(available)
            self._control_detail = None if available else detail
            self._condition.notify_all()

    def readiness(self) -> ReadinessReport:
        """Report both planes; never claim ready before the SDK is initialized."""
        with self._condition:
            sdk_initialized = self._sdk_initialized
            query_available = self._query_available
            query_detail = self._query_detail
            control_available = self._control_available
            control_detail = self._control_detail
        if not sdk_initialized:
            query = ComponentReadiness(QUERY_COMPONENT, False, SDK_NOT_INITIALIZED)
        elif self._query_probe is not None:
            available, detail = _probe_query(self._query_probe)
            query = ComponentReadiness(QUERY_COMPONENT, available, detail)
        else:
            query = ComponentReadiness(QUERY_COMPONENT, query_available, query_detail)
        control = ComponentReadiness(CONTROL_COMPONENT, control_available, control_detail)
        return ReadinessReport(query=query, control=control)

    # -- admission ---------------------------------------------------------

    @property
    def in_flight(self) -> int:
        with self._condition:
            return self._in_flight

    @property
    def draining(self) -> bool:
        with self._condition:
            return self._draining

    @property
    def guard(self) -> SolutionGuard | None:
        return self._guard

    @contextlib.contextmanager
    def query(self) -> Iterator[Any]:
        """Admit one query, or refuse it because a drain has started.

        The guard is taken through guard.reader() and released by that same
        context manager, so a query that raises or is cut short by a drain still
        leaves no lock behind.
        """
        with self._condition:
            if self._draining:
                raise AxiomError(
                    "NOT_READY",
                    "the gateway is draining and is not accepting new queries",
                )
            self._in_flight += 1
        try:
            if self._guard is None:
                yield None
            else:
                with self._guard.reader():
                    yield self._guard
        finally:
            with self._condition:
                self._in_flight -= 1
                self._finished += 1
                self._condition.notify_all()

    # -- drain and shutdown ------------------------------------------------

    def start_drain(self) -> None:
        """Refuse new queries and wake any drain waiter. Idempotent."""
        with self._condition:
            self._draining = True
            self._condition.notify_all()

    def _resolve_budget(self, timeout_s: float | None) -> float:
        if timeout_s is None:
            return self._drain_timeout_s
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
            raise AxiomError("LIMIT_EXCEEDED", "drain timeout must be a number of seconds")
        if not 0 < timeout_s <= MAX_DRAIN_TIMEOUT_S:
            raise AxiomError(
                "LIMIT_EXCEEDED",
                f"drain timeout must be in (0, {MAX_DRAIN_TIMEOUT_S}], got {timeout_s!r}",
            )
        return float(timeout_s)

    def _wait_for_queries(self, budget: float) -> int:
        """Wait up to the budget in seconds; return how many queries remain."""
        deadline = self._clock() + budget
        with self._condition:
            while self._in_flight:
                remaining_time = deadline - self._clock()
                if remaining_time <= 0:
                    break
                self._condition.wait(timeout=remaining_time)
            return self._in_flight

    def drain(self, timeout_s: float | None = None) -> DrainOutcome:
        """Wait for in-flight queries; on timeout cancel and release what remains."""
        self.start_drain()
        budget = self._resolve_budget(timeout_s)
        started = self._clock()
        remaining = self._wait_for_queries(budget)
        elapsed = self._clock() - started
        cancelled = 0
        if remaining:
            with self._condition:
                cancelled = remaining
                self._cancelled += remaining
                self._condition.notify_all()
            if self._guard is not None:
                self._guard.release_all()
        with self._condition:
            finished = self._finished
        guards_held = () if self._guard is None else self._guard.held_names
        return DrainOutcome(
            drained=remaining == 0,
            finished=finished,
            cancelled=cancelled,
            remaining=remaining,
            elapsed_s=elapsed,
            guards_held=guards_held,
        )

    def shutdown(self, timeout_s: float | None = None) -> DrainOutcome:
        """Drain once, release every remaining guard, and remember the outcome.

        Repeated calls return the first outcome instead of draining again, so a
        signal handler that fires twice cannot double-count cancellations.
        """
        with self._condition:
            if self._shutdown and self._outcome is not None:
                return self._outcome
        outcome = self.drain(timeout_s)
        if self._guard is not None:
            self._guard.release_all()
            guards_held = self._guard.held_names
            if guards_held != outcome.guards_held:
                outcome = DrainOutcome(
                    drained=outcome.drained,
                    finished=outcome.finished,
                    cancelled=outcome.cancelled,
                    remaining=outcome.remaining,
                    elapsed_s=outcome.elapsed_s,
                    guards_held=guards_held,
                )
        with self._condition:
            self._shutdown = True
            self._outcome = outcome
        return outcome
