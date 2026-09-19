"""C-015: required bytes are copied under the shared guard, and the guard is gone before the
response is built or streamed.

The fixtures are the vendored ``auth-api`` live lane. The guard is the real
:class:`~axiom_mcp.guard.engine.SolutionGuard` on the real filesystem, and the publisher that
has to be kept out is a second *process* (``python -m axiom_mcp.guard.interop``), because an
in-process second object would not prove anything about the primitive on Windows.

Two cases carry the task:

* :func:`test_bytes_are_copied_while_the_data_guard_is_held` asserts, from inside the injected
  byte source, that every read the copy performs happens while ``data.lock`` is held - the
  "required bytes enter bounded memory under shared guard" half of AC1;
* :func:`test_a_publisher_is_blocked_during_the_copy_and_free_during_the_stream` runs the copy
  slowly in a worker thread, proves an exclusive publisher times out while it is in flight, and
  then proves the same publisher acquires the lane while the response is still only half
  streamed - the "slow clients do not prevent publication indefinitely" half.

The remaining cases are the negative and boundary ones: a response attempted while a lock is
held is refused, the pointer/manifest/plan caps refuse an oversized copy before it is read, and
a shard removed between the pointer read and the shard read surfaces as an error with the guard
released rather than as a partial answer.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from axiom_mcp.guard.engine import SolutionGuard
from axiom_mcp.read_session import (
    BoundedCopyExceeded,
    GuardStillHeld,
    LoadedSnapshot,
    ReadLimits,
    ReadSession,
    ReadSessionError,
)
from axiom_mcp.registry import SnapshotLocation
from axiom_mcp.shards import ShardLimits, ShardTooLarge, ShardUnreadable, read_bounded

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
FIXTURE_LANE = REPO_ROOT / "tests" / "fixtures" / "generation" / "auth-api"
AUTH_API_GENERATION = "7319237fdf1ce4ff27ea377c8bc3ae8cf1f115cffc71de369a373e1870ba9d1a"
COVERAGE_PATH = "coverage.json"
EDGES = "edges/000000.json"
NODES = "nodes/000000.json"
SHARD_BYTES = 173 + 3 + 340
EXIT_OK = 0
EXIT_TIMEOUT = 3


def interop_env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + existing if existing else "")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_probe(
    guard_dir: Path, *, mode: str = "exclusive", timeout_ms: int = 400
) -> tuple[int, str]:
    done = subprocess.run(  # noqa: S603 - a fixed argv to this interpreter
        [
            sys.executable,
            "-m",
            "axiom_mcp.guard.interop",
            "try",
            "--dir",
            str(guard_dir),
            "--lock",
            "data.lock",
            "--mode",
            mode,
            "--timeout-ms",
            str(timeout_ms),
        ],
        capture_output=True,
        text=True,
        env=interop_env(),
        timeout=60,
    )
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def lane_copy(tmp_path: Path) -> Path:
    base = tmp_path / "auth-api"
    shutil.copytree(FIXTURE_LANE, base)
    return base


def location_for(base: Path) -> SnapshotLocation:
    return SnapshotLocation(
        solution_id="demo-solution", project_id="auth-api", lane="live", root=base
    )


def open_session(
    tmp_path: Path,
    base: Path,
    *,
    read=read_bounded,
    limits: ReadLimits | None = None,
    roles: tuple[str, ...] | None = None,
    timeout_ms: int = 5000,
) -> ReadSession:
    guard = SolutionGuard(tmp_path / "solution.guard", timeout_ms=timeout_ms)
    guard.ensure_directory()
    return ReadSession(guard, location_for(base), limits=limits, roles=roles, read=read)


def loaded(tmp_path: Path) -> tuple[ReadSession, LoadedSnapshot]:
    session = open_session(tmp_path, FIXTURE_LANE)
    return session, session.load()


def test_bytes_are_copied_while_the_data_guard_is_held(tmp_path: Path) -> None:
    """Positive: every copy read happens under the shared data guard, and only it."""
    observed: list[tuple[str, tuple[str, ...]]] = []
    holder: dict[str, ReadSession] = {}

    def watched_read(path: Path, limit: int) -> bytes:
        observed.append((path.name, holder["session"].guard.held_names))
        return read_bounded(path, limit)

    session = open_session(tmp_path, FIXTURE_LANE, read=watched_read)
    holder["session"] = session
    snapshot = session.load()

    assert [name for name, _ in observed] == [
        "current.json",
        "manifest.json",
        "coverage.json",
        "000000.json",
        "000000.json",
    ]
    assert all(held == ("data.lock",) for _, held in observed), observed
    assert session.guard.held_names == (), "the reader must release the data guard before returning"
    assert snapshot.guard_held_during_copy is True
    assert snapshot.parsed_after_guard_release is True
    assert snapshot.generation_id == AUTH_API_GENERATION
    assert snapshot.freshness == "unknown", "a manifest hash is not live freshness"
    assert snapshot.verification == "manifest_hash"
    assert snapshot.bytes_copied == snapshot_bytes(snapshot)
    assert [item.path for item in snapshot.shards] == [COVERAGE_PATH, EDGES, NODES]
    assert snapshot.records == 2
    assert snapshot.coverage_status == "partial"
    assert snapshot.shard(NODES).document[0]["id"] is not None


def snapshot_bytes(snapshot: LoadedSnapshot) -> int:
    pointer = (FIXTURE_LANE / "current.json").stat().st_size
    manifest = (FIXTURE_LANE / "generations" / AUTH_API_GENERATION / "manifest.json").stat().st_size
    return pointer + manifest + SHARD_BYTES


def test_reader_honours_a_narrowed_role_set_and_budget(tmp_path: Path) -> None:
    """Boundary: only the requested roles are copied, and the declared plan is respected."""
    session = open_session(tmp_path, FIXTURE_LANE, roles=("nodes",))
    snapshot = session.load()
    assert [item.path for item in snapshot.shards] == [NODES]
    assert snapshot.bytes_copied == snapshot_bytes(snapshot) - (173 + 3)
    assert session.guard.held_names == ()


def test_a_publisher_is_blocked_during_the_copy_and_free_during_the_stream(tmp_path: Path) -> None:
    """AC1: a slow consumer cannot keep a publisher out of the lane once the copy is done."""
    base = lane_copy(tmp_path)
    guard_dir = tmp_path / "solution.guard"
    entered = threading.Event()
    release = threading.Event()
    outcome: dict[str, object] = {}

    def slow_read(path: Path, limit: int) -> bytes:
        if path.name != "current.json":
            entered.set()
            release.wait(timeout=10)
            time.sleep(0.6)
        return read_bounded(path, limit)

    session = open_session(tmp_path, base, read=slow_read, timeout_ms=5000)

    def worker() -> None:
        try:
            outcome["snapshot"] = session.load()
        except BaseException as exc:  # noqa: BLE001 - reported to the test below
            outcome["error"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(timeout=10), "the copy never started"
    try:
        blocked, output = run_probe(guard_dir, mode="exclusive", timeout_ms=400)
        assert blocked == EXIT_TIMEOUT, (
            f"a publisher acquired the lane during the copy (exit {blocked}): {output}"
        )
    finally:
        release.set()
    thread.join(timeout=30)
    assert "error" not in outcome, outcome.get("error")
    snapshot = outcome["snapshot"]
    assert isinstance(snapshot, LoadedSnapshot)
    assert session.guard.held_names == ()

    stream = session.stream(snapshot, chunk_bytes=64)
    first = next(stream)
    assert first, "the response must be streaming"
    acquired, output = run_probe(guard_dir, mode="exclusive", timeout_ms=2000)
    assert acquired == EXIT_OK, (
        f"a publisher was still blocked while the response streamed (exit {acquired}): {output}"
    )
    rest = list(stream)
    payload = first + b"".join(rest)
    assert all(0 < len(chunk) <= 64 for chunk in (first, *rest)), "every chunk is bounded"
    assert json.loads(payload.decode("utf-8"))["generation_id"] == AUTH_API_GENERATION
    assert session.guard.held_names == ()


def test_response_is_refused_while_the_read_lock_is_held(tmp_path: Path) -> None:
    """Negative: rendering inside the guard is refused instead of blocking a publisher."""
    session, snapshot = loaded(tmp_path)
    with session.guard.reader():
        assert session.guard.held_names == ("data.lock",)
        with pytest.raises(GuardStillHeld) as info:
            session.render(snapshot)
        assert "read locks are released" in str(info.value)
        with pytest.raises(GuardStillHeld):
            next(session.stream(snapshot))
    assert session.guard.held_names == ()
    assert session.render(snapshot)["generation_id"] == AUTH_API_GENERATION


def test_caps_refuse_an_oversized_pointer_manifest_or_plan(tmp_path: Path) -> None:
    """Boundary: an oversized copy is refused by the cap, before it is read."""
    session = open_session(tmp_path, FIXTURE_LANE, limits=ReadLimits(max_pointer_bytes=8))
    with pytest.raises(BoundedCopyExceeded) as info:
        session.load()
    assert "the lane pointer" in str(info.value)
    assert session.guard.held_names == (), "a failed copy must not leave a lock held"

    session = open_session(tmp_path, FIXTURE_LANE, limits=ReadLimits(max_manifest_bytes=64))
    with pytest.raises(BoundedCopyExceeded) as info:
        session.load()
    assert "manifest" in str(info.value)

    def pointer_manifest_only(path: Path, limit: int) -> bytes:
        if path.name in {"current.json", "manifest.json"}:
            return read_bounded(path, limit)
        raise AssertionError("the plan budget should have refused the copy before any shard read")

    session = open_session(
        tmp_path,
        FIXTURE_LANE,
        read=pointer_manifest_only,
        limits=ReadLimits(shard_limits=ShardLimits(max_shard_bytes=340, max_total_bytes=400)),
    )
    with pytest.raises(ShardTooLarge) as info:
        session.load()
    assert "above the plan budget 400" in str(info.value)


def test_shard_removed_after_the_pointer_read_is_an_error_not_a_partial_answer(
    tmp_path: Path,
) -> None:
    """Negative: a generation collected mid-copy fails bounded, with the guard released."""
    base = lane_copy(tmp_path)
    target = base / "generations" / AUTH_API_GENERATION / NODES
    shard_seen = threading.Event()

    def vanishing_read(path: Path, limit: int) -> bytes:
        if path.name == "000000.json" and path.parent.name == "nodes":
            target.unlink()
            shard_seen.set()
        return read_bounded(path, limit)

    session = open_session(tmp_path, base, read=vanishing_read)
    with pytest.raises(ShardUnreadable) as info:
        session.load()
    assert info.value.args[0].startswith("shard nodes/000000.json is unreadable")
    assert shard_seen.is_set()
    assert session.guard.held_names == ()
    assert not target.exists(), "the collected shard stays collected; nothing is substituted"


def test_session_refuses_an_invalid_configuration(tmp_path: Path) -> None:
    """Boundary: the session refuses caps and chunk sizes it cannot honour."""
    with pytest.raises(ReadSessionError):
        ReadLimits(max_pointer_bytes=0)
    with pytest.raises(ReadSessionError):
        ReadLimits(max_manifest_bytes=True)
    session, snapshot = loaded(tmp_path)
    with pytest.raises(ReadSessionError):
        next(session.stream(snapshot, chunk_bytes=0))
    with pytest.raises(ReadSessionError) as info:
        snapshot.shard("architecture/summary.json")
    assert info.value.args[0].endswith("architecture/summary.json'")
