"""Guard failures, kept apart by what a caller can do about them.

Three situations look alike and are not: another holder won the race, the caller asked to
stop, and this platform has no agreed primitive. Collapsing them is how a bounded wait
becomes an unbounded one, so each one is its own type. Every failure also carries a
machine-readable ``reason``, so tests and evidence match on a stable code instead of on
prose that a later edit is free to reword.

A failed wait is not a partial acquisition. The engine releases every guard it already took
before it raises any of these, so a caught ``GuardTimeout`` never means "still holding".
"""

from __future__ import annotations

__all__ = [
    "GuardBusy",
    "GuardCancelled",
    "GuardError",
    "GuardTimeout",
    "GuardUnsupported",
]


class GuardError(RuntimeError):
    """Base guard failure carrying a stable reason code."""

    def __init__(
        self,
        reason: str,
        message: str = "",
        *,
        lock: str | None = None,
        elapsed_ms: int | None = None,
    ) -> None:
        self.reason = reason
        self.lock = lock
        self.elapsed_ms = elapsed_ms
        subject = f"{reason}:{lock}" if lock else reason
        super().__init__(f"{subject}: {message}" if message else subject)


class GuardBusy(GuardError):
    """One non-blocking attempt lost the race.

    Internal control flow: the engine catches this and retries inside its deadline. It never
    escapes an engine call, because losing one attempt is not a failure the caller acts on.
    """

    def __init__(self, lock: str | None = None, message: str = "the guard is held elsewhere"):
        super().__init__("busy", message, lock=lock)


class GuardTimeout(GuardError):
    """The bounded wait elapsed without acquiring the guard."""

    def __init__(
        self,
        lock: str | None = None,
        *,
        elapsed_ms: int | None = None,
        timeout_ms: int | None = None,
    ) -> None:
        self.timeout_ms = timeout_ms
        message = (
            f"bounded wait of {timeout_ms} ms elapsed without acquiring the guard; "
            "every acquired guard was released in reverse acquisition order"
        )
        super().__init__("lock_timeout", message, lock=lock, elapsed_ms=elapsed_ms)


class GuardCancelled(GuardError):
    """The caller cancelled the bounded wait."""

    def __init__(self, lock: str | None = None, *, elapsed_ms: int | None = None) -> None:
        message = (
            "the caller cancelled the bounded wait; every acquired guard was released in "
            "reverse acquisition order"
        )
        super().__init__("cancelled", message, lock=lock, elapsed_ms=elapsed_ms)


class GuardUnsupported(GuardError):
    """No agreed primitive is declared for this platform."""

    def __init__(self, message: str, *, platform: str | None = None) -> None:
        self.platform = platform
        super().__init__("unsupported_platform", message)
