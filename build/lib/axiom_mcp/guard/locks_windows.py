"""Windows ``LockFileEx`` backend: a mandatory one-byte lock on the two guard files.

The contract fixes the family (``LockFileEx``), the byte range (offset 0, length 1), the open
mode (``OPEN_ALWAYS``) and the share mode (``FILE_SHARE_READ | FILE_SHARE_WRITE``, which
excludes ``FILE_SHARE_DELETE``). This module implements exactly that and nothing more: it opens
the guard file with read/write access without truncating it, tries one *non-blocking* lock
carrying ``LOCKFILE_FAIL_IMMEDIATELY``, and releases with a matching ``UnlockFileEx`` before
closing. Windows has no ``flock``, so the primitive is necessarily different from POSIX; what
must not differ is the byte range, the acquisition order, the bounded wait and the release
path, and those live once in :mod:`axiom_mcp.guard.engine`.

Identity is taken from the open descriptor rather than from the path: the Win32 handle is
adopted as a C run-time descriptor and the file identity is read with ``os.fstat``, so it is
the same shape and the same source as :func:`axiom_mcp.guard.protocol.file_identity` uses for
the path. A path whose file changed identity between open and lock is therefore detected, and
the guard never locks an unrelated file.

A byte-range lock from a second handle in the same process over the same range is reported as
``ERROR_LOCK_VIOLATION`` exactly like a second process, so the primitive does conflict within
one process. The engine still refuses a second acquisition of the same guard in one execution
path rather than relying on that, so re-entry fails with a deterministic reason on both
platforms, and real exclusion is observed between independent processes.

``ctypes`` is used rather than a wrapper package: the primitive is four ``kernel32`` calls, and
adding a dependency for them would put the guard behind a package registry the release path
does not need.
"""

from __future__ import annotations

import ctypes
import msvcrt
import os
from ctypes import wintypes
from pathlib import Path

from axiom_mcp.guard import protocol
from axiom_mcp.guard.engine import LockHandle, LockMode
from axiom_mcp.guard.errors import GuardBusy, GuardError

__all__ = [
    "FILE_SHARE_MODE",
    "OPEN_MODE",
    "WindowsLockHandle",
    "backend_name",
    "open",
    "primitive",
    "scope",
]

backend_name = "windows"
primitive = "LockFileEx"
scope = "byte-range offset 0 length 1"

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_ALWAYS = 4
FILE_ATTRIBUTE_NORMAL = 0x00000080
LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# The share mode is part of the frozen block: read and write are shared, delete is not, so a
# second opener cannot replace or remove the guard file while a holder has it open.
FILE_SHARE_MODE = FILE_SHARE_READ | FILE_SHARE_WRITE
OPEN_MODE = "OPEN_ALWAYS"

# A byte-range lock is taken over exactly one byte at offset zero on every declared file.
BYTE_RANGE = protocol.WINDOWS_BYTE_RANGE
BYTE_OFFSET, BYTE_LENGTH = BYTE_RANGE

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _Overlapped(ctypes.Structure):
    """The x86-64 ``OVERLAPPED`` layout, with only the range fields set."""

    _fields_ = (
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", ctypes.c_void_p),
    )


_CreateFileW = kernel32.CreateFileW
_CreateFileW.restype = ctypes.c_void_p
_CreateFileW.argtypes = (
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
)

_LockFileEx = kernel32.LockFileEx
_LockFileEx.restype = wintypes.BOOL
_LockFileEx.argtypes = (
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.POINTER(_Overlapped),
)

_UnlockFileEx = kernel32.UnlockFileEx
_UnlockFileEx.restype = wintypes.BOOL
_UnlockFileEx.argtypes = (
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.POINTER(_Overlapped),
)


def create_file(path: str | os.PathLike[str], share_mode: int = FILE_SHARE_MODE) -> int:
    """Open ``path`` with the frozen disposition, access and share mode; return the handle.

    The share mode is a parameter only so that a test can ask for a mode the guard itself
    never uses (a delete-denying or a deny-everything opener) and observe the sharing
    violation. Production callers use the default.
    """
    ctypes.set_last_error(0)
    return _CreateFileW(
        str(path),
        GENERIC_READ | GENERIC_WRITE,
        share_mode,
        None,
        OPEN_ALWAYS,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )


class WindowsLockHandle(LockHandle):
    """One ``LockFileEx``-backed descriptor for one guard file."""

    __slots__ = ("_path", "_fd")

    def __init__(self, path: Path, fd: int) -> None:
        self._path = path
        self._fd = fd

    def __repr__(self) -> str:
        return f"WindowsLockHandle(path={str(self._path)!r}, fd={self._fd})"

    @property
    def path(self) -> Path:
        return self._path

    @property
    def handle(self) -> int:
        """The Win32 handle backing this descriptor, as ``LockFileEx`` wants it."""
        return msvcrt.get_osfhandle(self._fd)

    def acquire(self, mode: LockMode) -> None:
        if mode is LockMode.EXCLUSIVE:
            flags = LOCKFILE_FAIL_IMMEDIATELY | LOCKFILE_EXCLUSIVE_LOCK
        elif mode is LockMode.SHARED:
            flags = LOCKFILE_FAIL_IMMEDIATELY
        else:
            raise GuardError("unknown_mode", f"unsupported lock mode {mode!r}")
        overlapped = _Overlapped(Offset=BYTE_OFFSET, OffsetHigh=0, hEvent=None)
        ctypes.set_last_error(0)
        if not _LockFileEx(self.handle, flags, 0, BYTE_LENGTH, 0, ctypes.byref(overlapped)):
            error = ctypes.get_last_error()
            if error in (ERROR_LOCK_VIOLATION, ERROR_SHARING_VIOLATION):
                raise GuardBusy(self._path.name) from None
            raise GuardError(
                "lock_failed",
                f"LockFileEx failed on {self._path} with Windows error {error}",
                lock=self._path.name,
            )

    def release(self) -> None:
        overlapped = _Overlapped(Offset=BYTE_OFFSET, OffsetHigh=0, hEvent=None)
        ctypes.set_last_error(0)
        if not _UnlockFileEx(self.handle, 0, BYTE_LENGTH, 0, ctypes.byref(overlapped)):
            error = ctypes.get_last_error()
            raise GuardError(
                "unlock_failed",
                f"UnlockFileEx failed on {self._path} with Windows error {error}",
                lock=self._path.name,
            )

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def descriptor(self) -> int:
        """The descriptor adopted from the Win32 handle, or -1 after close.

        The Win32 handle was opened with no security attributes, so it is not inheritable; the
        CRT descriptor adopted from it is checked by the tests with ``os.get_inheritable``.
        """
        return self._fd

    def identity(self) -> str:
        try:
            return protocol.identity_of_stat(os.fstat(self._fd))
        except OSError as exc:
            raise GuardError(
                "identity_unreadable",
                f"cannot read the identity of {self._path}: {exc}",
                lock=self._path.name,
            ) from exc


def open(path: Path) -> WindowsLockHandle:
    """Open (never truncate, never unlink) the guard file at ``path`` with the frozen share mode."""
    target = Path(path)
    handle = create_file(target)
    if handle in (None, INVALID_HANDLE_VALUE):
        error = ctypes.get_last_error()
        raise GuardError(
            "open_failed",
            f"CreateFileW failed on {target} with Windows error {error}",
            lock=target.name,
        )
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
    except OSError as exc:
        kernel32.CloseHandle(handle)
        raise GuardError(
            "open_failed",
            f"cannot adopt the handle for {target}: {exc}",
            lock=target.name,
        ) from exc
    return WindowsLockHandle(target, fd)
