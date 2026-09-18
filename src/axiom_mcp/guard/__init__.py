"""The Python side of the frozen native reader/writer guard protocol.

Owner of the protocol is ``axiom-specs``. This package contains no policy of its own: it
implements the same two lock files, the same acquisition and release order and the same
bounded wait that Rust is required to implement, so a publisher written in one language and
a reader written in the other stay safe over the same files.

* :mod:`axiom_mcp.guard.protocol` - the consumed constants and the guard path layout.
* :mod:`axiom_mcp.guard.engine` - order, bounded wait, cancellation and release, once.
* :mod:`axiom_mcp.guard.locks_posix` - the POSIX ``flock`` primitive.
* :mod:`axiom_mcp.guard.locks_windows` - the Windows ``LockFileEx`` primitive.
* :mod:`axiom_mcp.guard.interop` - a process-level probe used by the tests.

Importing this package never imports a platform module it cannot use: the backend is
resolved by :func:`axiom_mcp.guard.engine.load_backend`, which refuses an unavailable
platform instead of substituting a primitive the other language does not share.
"""

from __future__ import annotations

from axiom_mcp.guard import protocol
from axiom_mcp.guard.engine import (
    HeldLock,
    LockHandle,
    LockMode,
    PlatformBackend,
    SolutionGuard,
    load_backend,
    platform_backend_name,
    resolve_timeout_ms,
)
from axiom_mcp.guard.errors import (
    GuardBusy,
    GuardCancelled,
    GuardError,
    GuardTimeout,
    GuardUnsupported,
)

__all__ = [
    "GuardBusy",
    "GuardCancelled",
    "GuardError",
    "GuardTimeout",
    "GuardUnsupported",
    "HeldLock",
    "LockHandle",
    "LockMode",
    "PlatformBackend",
    "SolutionGuard",
    "load_backend",
    "platform_backend_name",
    "protocol",
    "resolve_timeout_ms",
]
