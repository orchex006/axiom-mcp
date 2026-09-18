"""Bounded, ordered acquisition of the two solution guards on any supported platform.

The contract in :mod:`axiom_mcp.guard.protocol` fixes the order, the byte range and the
bounded wait. This module is the part that must behave identically on POSIX and Windows, so
the platform-specific code is only the primitive: open one file and try one non-blocking
lock. Everything a caller can observe - admission before data, the reverse release order,
the refusal to upgrade a held lock, the refusal to acquire the same guard twice in one
execution path, the bounded retry budget, and the release of every already-acquired guard
when the wait fails - is implemented once, here, and is therefore the same on both
platforms.

Locks are OS-owned, not content-owned. Nothing here reads a lock file's contents as
ownership, and no cleanup handler is required for a crash: the process dying releases the
primitive. The lock files stay in place - they are stable empty files and removing them is a
separate maintenance action that requires every participating process to have stopped.

Scope of the guarantee: this protects cooperating readers and writers of the ecosystem. A
Git checkout, an editor or an antivirus process does not hold the guard, so the readers that
use it must still treat missing or corrupt files as recoverable.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from axiom_mcp.guard import protocol
from axiom_mcp.guard.errors import (
    GuardBusy,
    GuardCancelled,
    GuardError,
    GuardTimeout,
    GuardUnsupported,
)

__all__ = [
    "HeldLock",
    "LockHandle",
    "LockMode",
    "PlatformBackend",
    "SolutionGuard",
    "load_backend",
    "platform_backend_name",
    "resolve_timeout_ms",
]

# How many times a handle may be re-opened when the file behind the path is replaced between
# the open and the identity check. Both languages must lock the verified identity, so a path
# whose identity moved is re-opened instead of being locked as if it were the named file.
IDENTITY_OPEN_ATTEMPTS = 3
IDENTITY_REOPEN_PAUSE_S = 0.01

POSIX_PLATFORMS = (
    "linux",
    "darwin",
    "freebsd",
    "openbsd",
    "netbsd",
    "dragonfly",
    "sunos",
    "aix",
    "cygwin",
)


class LockMode(Enum):
    """The two modes the protocol declares, on both platforms."""

    SHARED = "shared"
    EXCLUSIVE = "exclusive"


class LockHandle(Protocol):
    """One opened guard file, as the platform primitive sees it."""

    def acquire(self, mode: LockMode) -> None:
        """Try once, non-blocking. Raise :class:`GuardBusy` when the lock is held elsewhere."""

    def release(self) -> None:
        """Release the lock this handle holds."""

    def close(self) -> None:
        """Close the handle. A handle never leaks into a child process."""

    def identity(self) -> str:
        """Stable identity of the file this handle refers to."""


class PlatformBackend(Protocol):
    """A platform module: the declaration plus the one operation the engine needs."""

    backend_name: str
    primitive: str
    scope: str

    def open(self, path: Path) -> LockHandle:
        """Open (creating if absent) the guard file at ``path`` without truncating it."""


@dataclass(frozen=True)
class HeldLock:
    """One guard this process currently holds."""

    name: str
    mode: LockMode
    handle: LockHandle


def platform_backend_name(platform: str | None = None) -> str:
    """Return the backend name declared for a platform, or raise :class:`GuardUnsupported`."""
    name = sys.platform if platform is None else platform
    if name.startswith("win"):
        return "windows"
    if name.startswith(POSIX_PLATFORMS):
        return "posix"
    raise GuardUnsupported(
        f"no guard backend is declared for platform {name!r}; the protocol requires an "
        "agreed primitive per platform",
        platform=name,
    )


def load_backend(name: str | None = None) -> PlatformBackend:
    """Import the backend module for ``name`` or for this platform.

    A missing or unusable backend is refused with :class:`GuardUnsupported` rather than
    silently falling back, because falling back is how one language ends up locking with a
    primitive the other does not share and both then believe they are safe.
    """
    target = name or platform_backend_name()
    module_name = f"axiom_mcp.guard.locks_{target}"
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise GuardUnsupported(
            f"guard backend {module_name} is unavailable on this interpreter: {exc}",
            platform=target,
        ) from exc
    return module


def resolve_timeout_ms(timeout_ms: int | None) -> int:
    """Validate a caller's bounded-wait budget against the protocol limits.

    A budget above the protocol maximum is refused instead of clamped: silently shortening a
    caller's wait hides the defect that produced it, and silently lengthening it reintroduces
    the unbounded blocking the protocol forbids.
    """
    if timeout_ms is None:
        return protocol.DEFAULT_TIMEOUT_MS
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
        raise GuardError(
            "invalid_timeout",
            f"timeout_ms must be an integer number of milliseconds, got {timeout_ms!r}",
        )
    if timeout_ms <= 0:
        raise GuardError(
            "invalid_timeout",
            f"timeout_ms must be positive, got {timeout_ms}; unbounded blocking is not permitted",
        )
    if timeout_ms > protocol.MAX_TIMEOUT_MS:
        raise GuardError(
            "invalid_timeout",
            f"timeout_ms {timeout_ms} exceeds the protocol maximum {protocol.MAX_TIMEOUT_MS}",
        )
    return timeout_ms


class SolutionGuard:
    """The one guard for one bound workspace instance.

    The default operation holds a single solution guard. A future multi-instance operation
    must sort stable instance ids and follow one declared order; V2 does not nest
    cross-instance guards, so this class deliberately exposes no nested-instance path.
    """

    def __init__(
        self,
        guard_dir: str | os.PathLike[str],
        *,
        timeout_ms: int | None = None,
        cancel: Callable[[], bool] | None = None,
        backend: PlatformBackend | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._directory = Path(guard_dir)
        self._paths = protocol.lock_paths(self._directory)
        self._timeout_ms = resolve_timeout_ms(timeout_ms)
        self._cancel = cancel
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._backend = backend or load_backend()
        self._held: dict[str, HeldLock] = {}
        self._closed = False

    def __repr__(self) -> str:
        return (
            f"SolutionGuard(guard_dir={str(self._directory)!r}, timeout_ms={self._timeout_ms}, "
            f"held={list(self._held)!r})"
        )

    @property
    def guard_dir(self) -> Path:
        return self._directory

    @property
    def timeout_ms(self) -> int:
        return self._timeout_ms

    @property
    def backend_name(self) -> str:
        return str(getattr(self._backend, "backend_name", "unknown"))

    @property
    def held_names(self) -> tuple[str, ...]:
        """Guards currently held, in acquisition order."""
        return tuple(self._held)

    def held_mode(self, name: str) -> LockMode | None:
        held = self._held.get(name)
        return None if held is None else held.mode

    def ensure_directory(self) -> Path:
        """Create the guard directory privately; it lives outside Git."""
        os.makedirs(self._directory, mode=0o700, exist_ok=True)
        return self._directory

    def acquire(self, name: str, mode: LockMode) -> HeldLock:
        """Acquire one guard under this guard's bounded budget."""
        return self._acquire_one(name, mode)

    def release(self, name: str) -> None:
        """Release one guard and close its handle."""
        held = self._held.pop(name, None)
        if held is None:
            raise GuardError("not_held", f"{name} is not held by this guard", lock=name)
        self._release_held(held)

    def release_all(self) -> None:
        """Release every held guard in the declared reverse acquisition order."""
        order = [name for name in protocol.RELEASE_ORDER if name in self._held]
        order.extend(name for name in reversed(list(self._held)) if name not in order)
        failures: list[str] = []
        for name in order:
            held = self._held.pop(name, None)
            if held is None:
                continue
            try:
                self._release_held(held)
            except OSError as exc:
                failures.append(f"{name}: {exc}")
        if failures:
            raise GuardError("release_failed", "; ".join(failures))

    def close(self) -> None:
        self.release_all()
        self._closed = True

    def __enter__(self) -> SolutionGuard:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @contextlib.contextmanager
    def ordered(
        self,
        specs: Sequence[tuple[str, LockMode]],
        *,
        attempts: int = 1,
        release_early: Sequence[str] = (),
    ) -> Iterator[SolutionGuard]:
        """Acquire ``specs`` in order, yielding while they are held.

        A failed or cancelled wait releases everything and retries from the start of the
        acquisition order, never by upgrading a held lock. ``release_early`` implements the
        one place the protocol releases before the end of the operation: the reader drops
        admission once it holds data, so a publisher may take admission while the reader is
        still copying its pinned generation.
        """
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
            raise GuardError(
                "invalid_attempts",
                f"attempts must be a positive integer, got {attempts!r}",
            )
        failure: GuardTimeout | GuardCancelled | None = None
        for _ in range(attempts):
            try:
                for name, mode in specs:
                    self._acquire_one(name, mode)
                for name in release_early:
                    self.release(name)
            except (GuardTimeout, GuardCancelled) as exc:
                self.release_all()
                failure = exc
                continue
            try:
                yield self
            finally:
                self.release_all()
            return
        if failure is None:  # pragma: no cover - defensive: the loop ran at least once
            raise GuardError("attempts_exhausted", "no acquisition attempt was made")
        raise failure

    def reader(self, *, attempts: int = 1) -> contextlib.AbstractContextManager[SolutionGuard]:
        """Hold the guard the way a snapshot reader does.

        Acquire admission shared, acquire data shared while still holding admission, release
        admission, and hold data across the bounded copy. The caller then releases data
        before it parses anything, so parsing and network response time are outside the wait
        a publisher can observe.
        """
        return self.ordered(
            (
                ("admission.lock", LockMode.SHARED),
                ("data.lock", LockMode.SHARED),
            ),
            attempts=attempts,
            release_early=("admission.lock",),
        )

    def publisher(self, *, attempts: int = 1) -> contextlib.AbstractContextManager[SolutionGuard]:
        """Hold the guard the way a publisher or GC does: admission exclusive, then data."""
        return self.ordered(
            (
                ("admission.lock", LockMode.EXCLUSIVE),
                ("data.lock", LockMode.EXCLUSIVE),
            ),
            attempts=attempts,
        )

    def _acquire_one(self, name: str, mode: LockMode) -> HeldLock:
        path = self._paths.get(name)
        if path is None:
            raise GuardError(
                "unknown_lock",
                f"{name!r} is not one of the declared guard files "
                f"({', '.join(protocol.LOCK_FILE_NAMES)})",
                lock=name,
            )
        if self._closed:
            raise GuardError("closed", "this guard was closed", lock=name)
        held = self._held.get(name)
        if held is not None:
            if held.mode is not mode:
                raise GuardError(
                    "lock_upgrade",
                    f"{name} is already held {held.mode.value}; release and retry from the "
                    "start of the acquisition order instead of upgrading",
                    lock=name,
                )
            raise GuardError(
                "recursive_acquire",
                f"{name} is already held by this guard in this execution path",
                lock=name,
            )
        position = protocol.ACQUISITION_ORDER.index(name)
        for already in self._held:
            if protocol.ACQUISITION_ORDER.index(already) > position:
                raise GuardError(
                    "order_violation",
                    f"{already} is held; the acquisition order is "
                    f"{', '.join(protocol.ACQUISITION_ORDER)} on every platform",
                    lock=name,
                )
        self.ensure_directory()
        handle = self._open_verified(path, name)
        started = self._clock()
        deadline = started + (self._timeout_ms / 1000.0)
        delay = protocol.RETRY_INITIAL_MS / 1000.0
        try:
            while True:
                if self._cancel is not None and self._cancel():
                    raise GuardCancelled(name, elapsed_ms=self._elapsed_ms(started))
                try:
                    handle.acquire(mode)
                except GuardBusy:
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        raise GuardTimeout(
                            name,
                            elapsed_ms=self._elapsed_ms(started),
                            timeout_ms=self._timeout_ms,
                        ) from None
                    self._sleep(min(delay, remaining))
                    delay = min(delay * 2.0, protocol.RETRY_MAX_MS / 1000.0)
                    continue
                break
        except BaseException:
            handle.close()
            raise
        entry = HeldLock(name=name, mode=mode, handle=handle)
        self._held[name] = entry
        return entry

    def _open_verified(self, path: Path, name: str) -> LockHandle:
        last: tuple[str, str] | None = None
        for _ in range(IDENTITY_OPEN_ATTEMPTS):
            handle = self._backend.open(path)
            try:
                opened = handle.identity()
                current = protocol.file_identity(path)
            except OSError as exc:
                handle.close()
                raise GuardError(
                    "identity_unreadable",
                    f"cannot verify the identity of {path}: {exc}",
                    lock=name,
                ) from exc
            if opened == current:
                return handle
            handle.close()
            last = (opened, current)
            self._sleep(IDENTITY_REOPEN_PAUSE_S)
        raise GuardError(
            "identity_mismatch",
            f"{path} changed identity while it was opened ({last!r}); refusing to lock a "
            "file that is not the named guard file",
            lock=name,
        )

    def _release_held(self, held: HeldLock) -> None:
        try:
            held.handle.release()
        finally:
            held.handle.close()

    def _elapsed_ms(self, started: float) -> int:
        return int(round((self._clock() - started) * 1000.0))
