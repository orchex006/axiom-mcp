"""POSIX ``flock`` backend: whole-file shared and exclusive locks on the two guard files.

The contract fixes the family (``flock``), the scope (whole file) and the modes
(``LOCK_SH``/``LOCK_EX``), so this module implements exactly that and nothing more: it opens
the guard file without truncating it, tries one *non-blocking* lock, and releases with
``LOCK_UN`` before closing. Mixing in a different ``fcntl`` lock family is forbidden because
the two families do not interoperate, and a Python-only lock would be invisible to Rust.

Lock state belongs to the open file description, so two descriptors opened in one process
conflict with each other exactly as two processes do. That is why the engine refuses a
second acquisition of the same guard in one execution path instead of treating it as free.

Handles are opened ``O_CLOEXEC`` so an inherited descriptor cannot carry a lock into a child
process, and the guard file is kept at private permissions because a file another user can
replace is not a lock anyone can rely on.
"""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path

from axiom_mcp.guard import protocol
from axiom_mcp.guard.engine import LockHandle, LockMode
from axiom_mcp.guard.errors import GuardBusy, GuardError

__all__ = [
    "FILE_MODE",
    "OPEN_FLAGS",
    "PosixLockHandle",
    "backend_name",
    "open",
    "primitive",
    "scope",
]

backend_name = "posix"
primitive = "flock"
scope = "whole-file"

FILE_MODE = 0o600
OPEN_FLAGS = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)

# flock reports contention as EAGAIN on Linux and as EACCES on some BSD-derived systems; both
# mean "another holder has it", which is a retry, not an error the caller sees.
CONTENTION_ERRNOS = (errno.EACCES, errno.EAGAIN)

_MODE_FLAGS = {
    LockMode.SHARED: fcntl.LOCK_SH,
    LockMode.EXCLUSIVE: fcntl.LOCK_EX,
}


class PosixLockHandle(LockHandle):
    """One ``flock``-backed descriptor for one guard file."""

    __slots__ = ("_fd", "_path")

    def __init__(self, path: Path, fd: int) -> None:
        self._path = path
        self._fd = fd

    def __repr__(self) -> str:
        return f"PosixLockHandle(path={str(self._path)!r}, fd={self._fd})"

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self, mode: LockMode) -> None:
        try:
            operation = _MODE_FLAGS[mode]
        except KeyError:
            raise GuardError("unknown_mode", f"unsupported lock mode {mode!r}") from None
        try:
            fcntl.flock(self._fd, operation | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in CONTENTION_ERRNOS:
                raise GuardBusy(self._path.name) from None
            raise

    def release(self) -> None:
        fcntl.flock(self._fd, fcntl.LOCK_UN)

    def close(self) -> None:
        os.close(self._fd)

    def identity(self) -> str:
        return protocol.identity_of_stat(os.fstat(self._fd))


def open(path: Path) -> PosixLockHandle:
    """Open (never truncate, never unlink) the guard file at ``path``."""
    target = Path(path)
    fd = os.open(target, OPEN_FLAGS, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)
    except OSError:  # pragma: no cover - a filesystem without fchmod support
        pass
    return PosixLockHandle(target, fd)
