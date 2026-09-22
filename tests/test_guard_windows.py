"""C-011: the Windows guard, observed between two independent processes.

Every case here runs the real ``LockFileEx`` primitive on the real Windows filesystem;
nothing is mocked at the lock layer. The two-process requirement is met by launching
``python -m axiom_mcp.guard.interop`` as a child, so exclusion is observed between processes
and not between two objects in one interpreter.

Two claims are Windows-specific and are therefore only provable here:

* the byte range, open mode and share mode are the frozen ones, so the shared mode excludes
  ``FILE_SHARE_DELETE`` - an open guard handle refuses both a deny-everything opener and a
  delete of the file it holds, which is what stops another process replacing the lock file
  out from under a holder;
* ``LockFileEx`` byte-range conflicts are reported as ``ERROR_LOCK_VIOLATION`` between
  handles in one process exactly as between processes, which is why the engine refuses a
  second acquisition of the same guard in one execution path instead of relying on it.

The module is skipped where ``msvcrt`` does not exist, so the Windows half of this slice is
recorded from this Windows host and never inferred from a POSIX run.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Skip the module before importing either the Windows-only backend or its stdlib
# dependency.  Importing ``locks_windows`` first imports ``msvcrt``, which makes
# collection fail on POSIX instead of reporting this as the intended skip.
pytest.importorskip("msvcrt", reason="the Windows LockFileEx backend only exists on Windows")

from axiom_mcp.guard import protocol
from axiom_mcp.guard.engine import LockMode, SolutionGuard, load_backend
from axiom_mcp.guard.errors import GuardBusy, GuardError, GuardTimeout
from axiom_mcp.guard.locks_windows import (
    ERROR_SHARING_VIOLATION,
    FILE_SHARE_MODE,
    INVALID_HANDLE_VALUE,
    create_file,
)
from axiom_mcp.guard.locks_windows import open as open_lock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
ACQUIRE = "admission.lock"
DATA = "data.lock"


def interop_env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + existing if existing else "")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def spawn(*args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "axiom_mcp.guard.interop", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=interop_env(),
    )


def wait_for_event(proc: subprocess.Popen[str], wanted: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    while time.monotonic() < deadline:
        line = proc.stdout.readline() if proc.stdout else ""
        if not line:
            raise AssertionError(f"child exited before {wanted}: {seen!r} code={proc.poll()}")
        seen.append(line.strip())
        payload = json.loads(line)
        if payload.get("event") == wanted:
            return payload
    raise AssertionError(f"no {wanted} event from the child; saw {seen!r}")


def run_probe(*args: str) -> tuple[int, str]:
    done = subprocess.run(
        [sys.executable, "-m", "axiom_mcp.guard.interop", *args],
        capture_output=True,
        text=True,
        env=interop_env(),
        timeout=30,
    )
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def test_backend_is_the_declared_primitive() -> None:
    backend = load_backend()
    assert backend.backend_name == "windows"
    assert backend.primitive == "LockFileEx"
    assert backend.scope == "byte-range offset 0 length 1"


def test_frozen_windows_block_is_the_contract_value() -> None:
    """The byte range, open mode and share mode are the frozen ones, not a local choice."""
    assert protocol.WINDOWS_BYTE_RANGE == (0, 1)
    assert protocol.WINDOWS_OPEN_MODE == "OPEN_ALWAYS"
    assert protocol.WINDOWS_SHARE_MODE_EXCLUDES == "FILE_SHARE_DELETE"
    assert FILE_SHARE_MODE & 0x00000004 == 0, "FILE_SHARE_DELETE must not be shared"


def test_exclusive_holder_excludes_shared_and_exclusive_in_another_process(
    tmp_path: Path,
) -> None:
    """AC1: a real Windows holder excludes a real second process in both modes."""
    guard_dir = tmp_path / "solution.guard"
    holder = spawn(
        "hold",
        "--dir",
        str(guard_dir),
        "--locks",
        DATA,
        "--mode",
        "exclusive",
        "--hold-ms",
        "2500",
    )
    try:
        acquired = wait_for_event(holder, "acquired")
        assert acquired["backend"] == "windows"

        blocked = SolutionGuard(guard_dir, timeout_ms=400)
        started = time.monotonic()
        with pytest.raises(GuardTimeout) as info:
            blocked.acquire(DATA, LockMode.SHARED)
        elapsed = time.monotonic() - started
        assert info.value.reason == "lock_timeout"
        assert 0.3 <= elapsed <= 2.0, elapsed
        assert blocked.held_names == (), "a failed wait must not retain a guard"

        code, output = run_probe(
            "try",
            "--dir",
            str(guard_dir),
            "--lock",
            DATA,
            "--mode",
            "exclusive",
            "--timeout-ms",
            "400",
        )
        assert code == 3, output
        assert json.loads(output.splitlines()[0])["reason"] == "lock_timeout"
    finally:
        holder.wait(timeout=20)

    after = SolutionGuard(guard_dir, timeout_ms=1000)
    after.acquire(DATA, LockMode.SHARED)
    assert after.held_names == (DATA,)
    after.release_all()
    assert after.held_names == ()


def test_two_shared_holders_coexist_between_processes(tmp_path: Path) -> None:
    guard_dir = tmp_path / "solution.guard"
    holder = spawn(
        "hold",
        "--dir",
        str(guard_dir),
        "--locks",
        DATA,
        "--mode",
        "shared",
        "--hold-ms",
        "1500",
    )
    try:
        wait_for_event(holder, "acquired")
        reader = SolutionGuard(guard_dir, timeout_ms=1000)
        reader.acquire(DATA, LockMode.SHARED)
        assert reader.held_mode(DATA) is LockMode.SHARED
        reader.release_all()
    finally:
        holder.wait(timeout=20)


def test_open_handle_sharing_refuses_delete_and_a_deny_all_opener(tmp_path: Path) -> None:
    """AC1: the frozen share mode is observable on an actual open Windows handle.

    The guard opens each lock file with read/write access and share mode
    ``FILE_SHARE_READ | FILE_SHARE_WRITE``, so a second opener that asks for anything else -
    here the deny-everything share mode - is refused with ``ERROR_SHARING_VIOLATION``, and
    the file cannot be removed while a holder has it open because delete is not shared.
    """
    guard_dir = tmp_path / "solution.guard"
    guard_dir.mkdir(parents=True)
    target = guard_dir / DATA
    handle = open_lock(target)
    try:
        handle.acquire(LockMode.EXCLUSIVE)

        ctypes.set_last_error(0)
        denied = create_file(target, share_mode=0)
        assert denied in (None, INVALID_HANDLE_VALUE)
        assert ctypes.get_last_error() == ERROR_SHARING_VIOLATION

        with pytest.raises(OSError) as info:
            os.remove(target)
        assert info.value.winerror == ERROR_SHARING_VIOLATION  # type: ignore[attr-defined]
    finally:
        handle.release()
        handle.close()

    os.remove(target)
    assert not target.exists()


def test_second_handle_in_one_process_conflicts_on_the_declared_byte_range(
    tmp_path: Path,
) -> None:
    """Boundary: ``LockFileEx`` excludes a second handle in one process, like a process.

    The engine therefore refuses a second acquisition of a guard in one execution path with
    a deterministic reason rather than depending on this primitive behaviour.
    """
    guard_dir = tmp_path / "solution.guard"
    guard_dir.mkdir(parents=True)
    target = guard_dir / DATA
    first = open_lock(target)
    second = open_lock(target)
    third = open_lock(target)
    try:
        first.acquire(LockMode.SHARED)
        third.acquire(LockMode.SHARED)
        assert first.identity() == third.identity()
        with pytest.raises(GuardBusy) as info:
            second.acquire(LockMode.EXCLUSIVE)
        assert info.value.reason == "busy"

        guard = SolutionGuard(guard_dir, timeout_ms=300)
        guard.acquire(DATA, LockMode.SHARED)
        with pytest.raises(GuardError) as info:
            guard.acquire(DATA, LockMode.EXCLUSIVE)
        assert info.value.reason == "lock_upgrade"
        guard.release_all()
        assert guard.held_names == ()
    finally:
        first.close()
        second.close()
        third.close()


def test_handle_identity_comes_from_the_open_descriptor(tmp_path: Path) -> None:
    """A handle reports the identity of the file it holds, the same value the path yields."""
    guard_dir = tmp_path / "solution.guard"
    guard_dir.mkdir(parents=True)
    target = guard_dir / DATA
    handle = open_lock(target)
    other = guard_dir / "other.lock"
    other.write_bytes(b"")
    try:
        assert handle.identity() == protocol.file_identity(target)
        assert handle.identity() != protocol.file_identity(other)
    finally:
        handle.close()
    with pytest.raises(GuardError) as info:
        handle.identity()
    assert info.value.reason == "identity_unreadable"


def test_bounded_wait_releases_admission_when_data_is_held(tmp_path: Path) -> None:
    """Boundary: a reader denied on ``data.lock`` releases the admission it already took."""
    guard_dir = tmp_path / "solution.guard"
    holder = spawn(
        "hold",
        "--dir",
        str(guard_dir),
        "--locks",
        DATA,
        "--mode",
        "exclusive",
        "--hold-ms",
        "2000",
    )
    try:
        wait_for_event(holder, "acquired")
        guard = SolutionGuard(guard_dir, timeout_ms=400)
        with pytest.raises(GuardTimeout):
            with guard.reader():
                pytest.fail("the reader must not enter while data.lock is held elsewhere")
        assert guard.held_names == ()

        follower = SolutionGuard(guard_dir, timeout_ms=1000)
        follower.acquire(ACQUIRE, LockMode.EXCLUSIVE)
        assert follower.held_names == (ACQUIRE,)
        follower.release_all()
    finally:
        holder.wait(timeout=20)
