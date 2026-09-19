"""V2-019: the guard as a cross-language reader guard, observed between real processes.

The contract (``contracts/native-reader-writer-guards.md`` section 8) does not accept a library
name, a linked crate or a shared wrapper as proof of interoperability: the exclusion must be
observed in two actual processes over the same files. This module drives exactly that through
the adapter's holder process contract, and it covers the four lifecycle states the task card
names - two independent processes, a bounded timeout, a crashed holder and released handles.

The Rust leg is real but *external*: when ``AXIOM_GUARD_HOLDER_ARGV`` names an ABI-conformant
holder (the ``axiom-graphd`` daemon or a holder built from it), the foreign scenario runs both
directions and its output is the cross-language evidence. When it is not configured the foreign
scenario is skipped with that reason, and the task evidence records the Rust/POSIX targets as
unverified rather than implying they passed - this host cannot build the Rust daemon here and the
POSIX primitive cannot run on Windows at all.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from axiom_mcp.guard import protocol
from axiom_mcp.guard.adapter import (
    DEFAULT_HOLDER_ARGV,
    HOLDER_ARGV_ENV,
    ReaderGuardAdapter,
    holder_is_foreign,
    resolve_holder_argv,
    spawn_holder,
    wait_for_event,
)
from axiom_mcp.guard.engine import LockMode, load_backend
from axiom_mcp.guard.errors import GuardError, GuardTimeout
from axiom_mcp.guard.interop import EXIT_OK, EXIT_TIMEOUT

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
ACQUIRE = "admission.lock"
DATA = "data.lock"
BOTH = f"{ACQUIRE},{DATA}"


def python_holder_env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + existing if existing else "")
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop(HOLDER_ARGV_ENV, None)
    return env


def run_python_probe(*args: str) -> tuple[int, str]:
    done = subprocess.run(
        [*DEFAULT_HOLDER_ARGV, *args],
        capture_output=True,
        text=True,
        env=python_holder_env(),
        timeout=60,
    )
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def spawn_python_holder(*args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [*DEFAULT_HOLDER_ARGV, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=python_holder_env(),
    )


def terminate(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=30)


def test_two_independent_processes_share_the_reader_pair(tmp_path: Path) -> None:
    """Positive: the reader pair is shared, so two real readers coexist on the same files."""
    guard_dir = tmp_path / "solution.guard"
    first = spawn_python_holder(
        "hold", "--dir", str(guard_dir), "--locks", BOTH, "--mode", "shared", "--hold-ms", "1500"
    )
    second = spawn_python_holder(
        "hold", "--dir", str(guard_dir), "--locks", BOTH, "--mode", "shared", "--hold-ms", "1500"
    )
    try:
        first_acquired = wait_for_event(first, "acquired")
        second_acquired = wait_for_event(second, "acquired")
        assert first_acquired["pid"] != second_acquired["pid"]
        assert first_acquired["locks"] == [ACQUIRE, DATA]
        assert first_acquired["backend"] in {"windows", "posix"}
        assert set(protocol.lock_paths(guard_dir)) == set(protocol.LOCK_FILE_NAMES)
    finally:
        assert first.wait(timeout=30) == EXIT_OK
        assert second.wait(timeout=30) == EXIT_OK


def test_exclusive_holder_in_another_process_excludes_both_modes(tmp_path: Path) -> None:
    """Negative: a foreign-style exclusive holder is not taken by a reader in this process."""
    guard_dir = tmp_path / "solution.guard"
    holder = spawn_python_holder(
        "hold", "--dir", str(guard_dir), "--locks", DATA, "--mode", "exclusive", "--hold-ms", "2500"
    )
    try:
        wait_for_event(holder, "acquired")
        for mode in ("shared", "exclusive"):
            code, output = run_python_probe(
                "try",
                "--dir",
                str(guard_dir),
                "--lock",
                DATA,
                "--mode",
                mode,
                "--timeout-ms",
                "400",
            )
            assert code == EXIT_TIMEOUT, f"{mode}: {output}"
    finally:
        assert holder.wait(timeout=30) == EXIT_OK


def test_bounded_timeout_is_measured_and_releases_the_partial_acquisition(tmp_path: Path) -> None:
    """Failure boundary: the wait is bounded, and the guard taken before it is released."""
    guard_dir = tmp_path / "solution.guard"
    holder = spawn_python_holder(
        "hold", "--dir", str(guard_dir), "--locks", DATA, "--mode", "exclusive", "--hold-ms", "2500"
    )
    try:
        wait_for_event(holder, "acquired")
        adapter = ReaderGuardAdapter(guard_dir, timeout_ms=300)
        started = time.monotonic()
        with pytest.raises(GuardTimeout) as info:
            with adapter.reader():
                pytest.fail("the reader must not enter while data.lock is held elsewhere")
        elapsed = time.monotonic() - started
        assert info.value.reason == "lock_timeout"
        assert info.value.timeout_ms == 300
        assert 0.25 <= elapsed <= 2.0
        assert adapter.guard.held_names == ()

        code, output = run_python_probe(
            "try",
            "--dir",
            str(guard_dir),
            "--lock",
            ACQUIRE,
            "--mode",
            "exclusive",
            "--timeout-ms",
            "400",
        )
        assert code == EXIT_OK, output
    finally:
        assert holder.wait(timeout=30) == EXIT_OK


def test_crash_release_leaves_the_guards_acquirable(tmp_path: Path) -> None:
    """AC1: killing the holder releases the OS-owned lock with no cleanup handler."""
    guard_dir = tmp_path / "solution.guard"
    holder = spawn_python_holder(
        "hold",
        "--dir",
        str(guard_dir),
        "--locks",
        BOTH,
        "--mode",
        "exclusive",
        "--hold-ms",
        "60000",
    )
    wait_for_event(holder, "acquired")
    try:
        code, output = run_python_probe(
            "try",
            "--dir",
            str(guard_dir),
            "--lock",
            DATA,
            "--mode",
            "shared",
            "--timeout-ms",
            "300",
        )
        assert code == EXIT_TIMEOUT, output
    finally:
        terminate(holder)
    assert holder.returncode != EXIT_OK, "a killed holder must not report a clean release"

    for lock in (ACQUIRE, DATA):
        code, output = run_python_probe(
            "try",
            "--dir",
            str(guard_dir),
            "--lock",
            lock,
            "--mode",
            "exclusive",
            "--timeout-ms",
            "600",
        )
        assert code == EXIT_OK, f"{lock}: {output}"

    for name, path in protocol.lock_paths(guard_dir).items():
        assert path.is_file(), name
        assert path.read_bytes() == b"", f"{name} content is not ownership and must stay empty"


def test_released_handles_are_reacquirable_and_closed_handles_refuse_identity(
    tmp_path: Path,
) -> None:
    """AC1: normal release closes every handle and the guard files stay usable."""
    guard_dir = tmp_path / "solution.guard"
    holder = spawn_python_holder(
        "hold", "--dir", str(guard_dir), "--locks", BOTH, "--mode", "exclusive", "--hold-ms", "50"
    )
    released = wait_for_event(holder, "released")
    assert set(released["locks"]) == {ACQUIRE, DATA}
    assert holder.wait(timeout=30) == EXIT_OK

    for lock in (ACQUIRE, DATA):
        code, output = run_python_probe(
            "try",
            "--dir",
            str(guard_dir),
            "--lock",
            lock,
            "--mode",
            "exclusive",
            "--timeout-ms",
            "600",
        )
        assert code == EXIT_OK, f"{lock}: {output}"

    backend = load_backend()
    handle = backend.open(protocol.lock_paths(guard_dir)[DATA])
    try:
        handle.acquire(LockMode.EXCLUSIVE)
        handle.release()
    finally:
        handle.close()
    with pytest.raises(GuardError) as info:
        handle.identity()
    assert info.value.reason == "identity_unreadable"


def test_open_guard_descriptors_are_not_inheritable(tmp_path: Path) -> None:
    """Section 7: a guard handle must not be inherited by a child process."""
    guard_dir = tmp_path / "solution.guard"
    surface = ReaderGuardAdapter(guard_dir).verify_surface()
    backend = load_backend(surface["backend"])
    for name in protocol.LOCK_FILE_NAMES:
        handle = backend.open(protocol.lock_paths(guard_dir)[name])
        try:
            descriptor = handle.descriptor()
            assert descriptor >= 0
            assert os.get_inheritable(descriptor) is False, f"{name} descriptor is inheritable"
        finally:
            handle.close()


def test_foreign_holder_excludes_and_is_excluded_by_python(tmp_path: Path) -> None:
    """AC1 cross-language: exclusion observed in both directions with a foreign holder.

    The holder named by ``AXIOM_GUARD_HOLDER_ARGV`` must implement the adapter's process
    contract. Without it this scenario cannot run on this host, and the skip reason is the
    evidence that the Rust leg is unverified rather than passing.
    """
    if not holder_is_foreign():
        pytest.skip(
            f"no foreign (Rust) ABI-conformant holder is configured: set {HOLDER_ARGV_ENV} to a "
            "JSON argv array to run the cross-language exclusion in both directions; until then "
            "the Rust holder and the POSIX target are unverified on this host"
        )

    guard_dir = tmp_path / "solution.guard"

    foreign = spawn_holder(
        "hold", "--dir", str(guard_dir), "--locks", DATA, "--mode", "exclusive", "--hold-ms", "2500"
    )
    try:
        wait_for_event(foreign, "acquired")
        for mode in ("shared", "exclusive"):
            code, output = run_python_probe(
                "try",
                "--dir",
                str(guard_dir),
                "--lock",
                DATA,
                "--mode",
                mode,
                "--timeout-ms",
                "400",
            )
            assert code == EXIT_TIMEOUT, (
                f"foreign exclusive did not exclude python {mode}: {output}"
            )
    finally:
        assert foreign.wait(timeout=30) == EXIT_OK

    python = spawn_python_holder(
        "hold",
        "--dir",
        str(guard_dir),
        "--locks",
        DATA,
        "--mode",
        "exclusive",
        "--hold-ms",
        "15000",
    )
    try:
        wait_for_event(python, "acquired")
        for mode in ("shared", "exclusive"):
            done = subprocess.run(
                [
                    *resolve_holder_argv(),
                    "try",
                    "--dir",
                    str(guard_dir),
                    "--lock",
                    DATA,
                    "--mode",
                    mode,
                    "--timeout-ms",
                    "400",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            assert done.returncode == EXIT_TIMEOUT, (
                f"python exclusive did not exclude the foreign holder in {mode}: "
                f"{(done.stdout or '') + (done.stderr or '')}"
            )
    finally:
        assert python.wait(timeout=30) == EXIT_OK


def test_lock_files_persist_as_stable_empty_files(tmp_path: Path) -> None:
    """A stable empty lock file stays after release; content is not ownership."""
    guard_dir = tmp_path / "solution.guard"
    holder = spawn_python_holder(
        "hold", "--dir", str(guard_dir), "--locks", BOTH, "--mode", "exclusive", "--hold-ms", "50"
    )
    assert wait_for_event(holder, "acquired")["event"] == "acquired"
    assert holder.wait(timeout=30) == EXIT_OK
    for name, path in protocol.lock_paths(guard_dir).items():
        assert path.is_file(), name
        assert path.stat().st_size == 0, name
