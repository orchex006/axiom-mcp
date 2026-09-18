"""C-010: the POSIX guard, observed between two independent processes.

Every case here runs the real ``flock`` primitive on the real filesystem; nothing is mocked
at the lock layer. The two-process requirement is met by launching
``python -m axiom_mcp.guard.interop`` as a child, so the exclusion is observed between
processes and not between two objects in one interpreter.

The module is skipped where ``fcntl`` does not exist, which is why the POSIX half of this
slice is recorded from a Linux container in the task evidence.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from axiom_mcp.guard import protocol
from axiom_mcp.guard.engine import LockMode, SolutionGuard, load_backend, resolve_timeout_ms
from axiom_mcp.guard.errors import GuardCancelled, GuardError, GuardTimeout

pytest.importorskip("fcntl", reason="the POSIX flock backend only exists where fcntl exists")

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
    return subprocess.Popen(  # noqa: S603 - a fixed argv to this interpreter
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
    done = subprocess.run(  # noqa: S603 - a fixed argv to this interpreter
        [sys.executable, "-m", "axiom_mcp.guard.interop", *args],
        capture_output=True,
        text=True,
        env=interop_env(),
        timeout=30,
    )
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def test_backend_is_the_declared_primitive() -> None:
    backend = load_backend()
    assert backend.backend_name == "posix"
    assert backend.primitive == "flock"
    assert backend.scope == "whole-file"


def test_exclusive_holder_in_another_process_excludes_a_shared_reader(tmp_path: Path) -> None:
    guard_dir = tmp_path / "solution.guard"
    holder = spawn(
        "hold", "--dir", str(guard_dir), "--locks", DATA, "--mode", "exclusive", "--hold-ms", "2000"
    )
    try:
        wait_for_event(holder, "acquired")
        blocked = SolutionGuard(guard_dir, timeout_ms=300)
        started = time.monotonic()
        with pytest.raises(GuardTimeout) as info:
            blocked.acquire(DATA, LockMode.SHARED)
        elapsed = time.monotonic() - started
        assert info.value.reason == "lock_timeout"
        assert 0.25 <= elapsed <= 1.5, elapsed
        assert blocked.held_names == (), "a failed wait must not retain a guard"
    finally:
        holder.wait(timeout=15)

    after = SolutionGuard(guard_dir, timeout_ms=500)
    after.acquire(DATA, LockMode.SHARED)
    assert after.held_names == (DATA,)
    after.release_all()
    assert after.held_names == ()


def test_two_shared_holders_coexist_between_processes(tmp_path: Path) -> None:
    guard_dir = tmp_path / "solution.guard"
    holder = spawn(
        "hold", "--dir", str(guard_dir), "--locks", DATA, "--mode", "shared", "--hold-ms", "1500"
    )
    try:
        wait_for_event(holder, "acquired")
        reader = SolutionGuard(guard_dir, timeout_ms=500)
        reader.acquire(DATA, LockMode.SHARED)
        assert reader.held_mode(DATA) is LockMode.SHARED
        reader.release_all()
    finally:
        holder.wait(timeout=15)


def test_cancellation_releases_the_acquired_guard(tmp_path: Path) -> None:
    guard_dir = tmp_path / "solution.guard"
    holder = spawn(
        "hold", "--dir", str(guard_dir), "--locks", DATA, "--mode", "exclusive", "--hold-ms", "2000"
    )
    try:
        wait_for_event(holder, "acquired")
        stop_at = time.monotonic() + 0.3
        guard = SolutionGuard(guard_dir, timeout_ms=3000, cancel=lambda: time.monotonic() > stop_at)
        with pytest.raises(GuardCancelled) as info:
            with guard.reader():
                pytest.fail("the reader must not enter while data.lock is held elsewhere")
        assert info.value.reason == "cancelled"
        assert guard.held_names == ()

        # admission was acquired and then released by the cancellation path: a guard whose
        # caller is no longer cancelling can take it exclusively now, which flock would refuse
        # if the shared lock were still held by this process.
        follower = SolutionGuard(guard_dir, timeout_ms=1000)
        follower.acquire(ACQUIRE, LockMode.EXCLUSIVE)
        assert follower.held_names == (ACQUIRE,)
        follower.release_all()
    finally:
        holder.wait(timeout=15)


def test_reader_releases_admission_while_it_holds_data(tmp_path: Path) -> None:
    guard_dir = tmp_path / "solution.guard"
    guard = SolutionGuard(guard_dir, timeout_ms=1000)
    with guard.reader():
        assert guard.held_names == (DATA,)
        code, out = run_probe(
            "try",
            "--dir",
            str(guard_dir),
            "--lock",
            ACQUIRE,
            "--mode",
            "exclusive",
            "--timeout-ms",
            "500",
        )
        assert code == 0, out
        code, out = run_probe(
            "try",
            "--dir",
            str(guard_dir),
            "--lock",
            DATA,
            "--mode",
            "exclusive",
            "--timeout-ms",
            "300",
        )
        assert code == 3, out
    assert guard.held_names == ()


def test_upgrade_recursion_and_order_violations_are_refused(tmp_path: Path) -> None:
    guard_dir = tmp_path / "solution.guard"
    guard = SolutionGuard(guard_dir, timeout_ms=1000)
    with guard.reader():
        with pytest.raises(GuardError) as upgrade:
            guard.acquire(DATA, LockMode.EXCLUSIVE)
        assert upgrade.value.reason == "lock_upgrade"
        with pytest.raises(GuardError) as recursive:
            guard.acquire(DATA, LockMode.SHARED)
        assert recursive.value.reason == "recursive_acquire"
        with pytest.raises(GuardError) as order:
            guard.acquire(ACQUIRE, LockMode.SHARED)
        assert order.value.reason == "order_violation"
        assert guard.held_names == (DATA,), "a refused acquisition must not change what is held"
    with pytest.raises(GuardError) as unknown:
        guard.acquire("other.lock", LockMode.SHARED)
    assert unknown.value.reason == "unknown_lock"


def test_bounded_wait_never_blocks_on_the_holder(tmp_path: Path) -> None:
    guard_dir = tmp_path / "solution.guard"
    holder = spawn(
        "hold", "--dir", str(guard_dir), "--locks", DATA, "--mode", "exclusive", "--hold-ms", "3000"
    )
    try:
        wait_for_event(holder, "acquired")
        guard = SolutionGuard(guard_dir, timeout_ms=50)
        started = time.monotonic()
        with pytest.raises(GuardTimeout):
            guard.acquire(DATA, LockMode.SHARED)
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"the attempt blocked for {elapsed:.3f}s instead of retrying"
    finally:
        holder.kill()
        holder.wait(timeout=15)


def test_process_kill_releases_the_lock_without_a_cleanup_handler(tmp_path: Path) -> None:
    guard_dir = tmp_path / "solution.guard"
    holder = spawn("hold", "--dir", str(guard_dir), "--mode", "exclusive", "--hold-ms", "30000")
    wait_for_event(holder, "acquired")
    holder.kill()
    holder.wait(timeout=15)

    guard = SolutionGuard(guard_dir, timeout_ms=2000)
    with guard.publisher():
        assert guard.held_names == (ACQUIRE, DATA)
    assert guard.held_names == ()
    assert sorted(p.name for p in guard.guard_dir.iterdir()) == sorted(protocol.LOCK_FILE_NAMES)


def test_guard_files_are_private_and_never_truncated(tmp_path: Path) -> None:
    guard_dir = tmp_path / "solution.guard"
    guard_dir.mkdir()
    admission = guard_dir / ACQUIRE
    admission.write_text("a recorded pid is not ownership\n", encoding="utf-8")
    kept = admission.read_bytes()
    guard = SolutionGuard(guard_dir, timeout_ms=500)
    guard.acquire(ACQUIRE, LockMode.SHARED)
    guard.release_all()
    assert admission.read_bytes() == kept
    assert (admission.stat().st_mode & 0o777) == 0o600
    assert [item.name for item in guard_dir.iterdir()] == [ACQUIRE]

    # the publisher path opens both declared files, and the one written before the lock is
    # still byte-for-byte what it was: an open handle is never truncated and never unlinked.
    with guard.publisher():
        assert guard.held_names == (ACQUIRE, DATA)
    assert admission.read_bytes() == kept
    assert sorted(item.name for item in guard_dir.iterdir()) == sorted(protocol.LOCK_FILE_NAMES)


def test_a_replaced_guard_file_is_not_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard_dir = tmp_path / "solution.guard"
    guard = SolutionGuard(guard_dir, timeout_ms=200)
    monkeypatch.setattr(protocol, "file_identity", lambda path: "0:0")
    with pytest.raises(GuardError) as info:
        guard.acquire(DATA, LockMode.SHARED)
    assert info.value.reason == "identity_mismatch"
    assert guard.held_names == ()


def test_timeout_budget_is_bounded(tmp_path: Path) -> None:
    assert resolve_timeout_ms(None) == protocol.DEFAULT_TIMEOUT_MS
    assert resolve_timeout_ms(protocol.MAX_TIMEOUT_MS) == protocol.MAX_TIMEOUT_MS
    for rejected in (0, -1, protocol.MAX_TIMEOUT_MS + 1, True, "500", 1.5):
        with pytest.raises(GuardError) as info:
            resolve_timeout_ms(rejected)  # type: ignore[arg-type]
        assert info.value.reason == "invalid_timeout", rejected


def test_guard_directory_and_lock_paths_follow_the_declared_layout(tmp_path: Path) -> None:
    directory = protocol.guard_directory(tmp_path, "instance-7")
    assert directory == tmp_path / "instances" / "instance-7" / "solution.guard"
    assert list(protocol.lock_paths(directory)) == list(protocol.LOCK_FILE_NAMES)
    for rejected in ("", "  ", ".", "..", "a/b", "a\\b", " x "):
        with pytest.raises(ValueError):
            protocol.guard_directory(tmp_path, rejected)
    same = protocol.file_identity(tmp_path)
    assert same == protocol.file_identity(tmp_path)
    other = protocol.file_identity(REPO_ROOT / "pyproject.toml")
    assert other != same
