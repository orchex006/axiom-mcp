"""V2-019: the cross-language adapter surface, exercised with real second processes.

Every claim here is observed on the real lock files with the real primitive: the probes are
``python -m axiom_mcp.guard.interop`` children started from this test process, and the reader
under test is the adapter a Python reader uses. Nothing is mocked at the lock layer, and no
claim is made about the Rust leg that this host cannot run - that leg is confined to
``tests/test_guard_cross_language.py`` and recorded as unverified when no foreign holder is
configured.
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
from axiom_mcp.guard.adapter import (
    DEFAULT_HOLDER_ARGV,
    HOLDER_ARGV_ENV,
    ReaderGuardAdapter,
    abi_json,
    abi_sha256,
    holder_is_foreign,
    resolve_holder_argv,
)
from axiom_mcp.guard.errors import GuardError, GuardTimeout
from axiom_mcp.guard.interop import EXIT_OK, EXIT_TIMEOUT

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
ACQUIRE = "admission.lock"
DATA = "data.lock"


def probe_env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + existing if existing else "")
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop(HOLDER_ARGV_ENV, None)
    return env


def run_probe(*args: str) -> tuple[int, str]:
    """Run the in-repo holder probe in a real second process and return exit code and output."""
    done = subprocess.run(
        [*DEFAULT_HOLDER_ARGV, *args],
        capture_output=True,
        text=True,
        env=probe_env(),
        timeout=60,
    )
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def symlink_or_skip(target: Path, link: Path, *, directory: bool) -> None:
    """Create a link, skipping the case where this host does not allow one."""
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - host capability
        pytest.skip(
            f"this host cannot create a symlink to exercise the untrusted-path refusal: {exc}"
        )


def test_abi_names_the_local_backend_and_declares_both_platforms(tmp_path: Path) -> None:
    adapter = ReaderGuardAdapter(tmp_path / "solution.guard")
    descriptor = adapter.abi()
    assert descriptor["local_backend"] in {"windows", "posix"}
    assert descriptor["local_backend"] == adapter.backend_name
    assert [entry["name"] for entry in descriptor["lock_files"]] == list(protocol.LOCK_FILE_NAMES)
    assert {entry["posix_primitive"] for entry in descriptor["lock_files"]} == {"flock"}
    assert {entry["windows_primitive"] for entry in descriptor["lock_files"]} == {"LockFileEx"}
    assert descriptor["interop_requirement"]["languages"] == ["rust", "python"]
    assert json.loads(abi_json())["local_backend"] == adapter.backend_name
    assert len(abi_sha256(descriptor)) == 64


def test_verify_surface_creates_the_two_declared_files_with_one_identity(tmp_path: Path) -> None:
    """AC1: both languages lock the same verified identity, not two files with one name."""
    guard_dir = tmp_path / "solution.guard"
    first = ReaderGuardAdapter(guard_dir)
    surface = first.verify_surface()
    assert surface["backend"] == first.backend_name
    assert set(surface["lock_files"]) == set(protocol.LOCK_FILE_NAMES)
    for name, record in surface["lock_files"].items():
        path = guard_dir / name
        assert path.is_file()
        assert record["identity"] == protocol.file_identity(path)
        assert record["path"] == str(path)
    assert protocol.lock_paths(guard_dir)[ACQUIRE].name == ACQUIRE

    second = ReaderGuardAdapter(guard_dir)
    assert second.verify_surface()["lock_files"] == surface["lock_files"]


def test_verify_surface_refuses_a_guard_directory_that_is_not_a_directory(tmp_path: Path) -> None:
    """Negative: a guard directory that is a file cannot hold the two lock files."""
    not_a_directory = tmp_path / "solution.guard"
    not_a_directory.write_text("not a directory", encoding="utf-8")
    with pytest.raises(GuardError) as info:
        ReaderGuardAdapter(not_a_directory).verify_surface()
    assert info.value.reason == "guard_dir_untrusted"


def test_verify_surface_refuses_a_linked_guard_directory(tmp_path: Path) -> None:
    """Negative: two languages following one name to different targets is not one guard."""
    real = tmp_path / "real-guard"
    real.mkdir()
    link = tmp_path / "solution.guard"
    symlink_or_skip(real, link, directory=True)
    with pytest.raises(GuardError) as info:
        ReaderGuardAdapter(link).verify_surface()
    assert info.value.reason == "guard_dir_untrusted"


def test_verify_surface_refuses_a_linked_lock_file(tmp_path: Path) -> None:
    """Negative: a lock file that is a link is not the file the contract names."""
    guard_dir = tmp_path / "solution.guard"
    guard_dir.mkdir()
    target = tmp_path / "elsewhere.lock"
    target.write_bytes(b"")
    symlink_or_skip(target, guard_dir / DATA, directory=False)
    with pytest.raises(GuardError) as info:
        ReaderGuardAdapter(guard_dir).verify_surface()
    assert info.value.reason == "guard_file_untrusted"
    assert info.value.lock == DATA


def test_read_pinned_holds_data_releases_admission_early_and_releases_data(
    tmp_path: Path,
) -> None:
    """AC1: the reader shape of the contract, observed from other processes while it runs."""
    guard_dir = tmp_path / "solution.guard"
    adapter = ReaderGuardAdapter(guard_dir, timeout_ms=1000)
    observed: dict[str, tuple[int, str]] = {}

    def pin() -> str:
        observed["admission_exclusive"] = run_probe(
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
        observed["data_exclusive"] = run_probe(
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
        observed["data_shared"] = run_probe(
            "try",
            "--dir",
            str(guard_dir),
            "--lock",
            DATA,
            "--mode",
            "shared",
            "--timeout-ms",
            "400",
        )
        return "pinned-bytes"

    pinned = adapter.read_pinned(pin)
    assert pinned == "pinned-bytes"
    assert observed["admission_exclusive"][0] == EXIT_OK, observed["admission_exclusive"][1]
    assert observed["data_exclusive"][0] == EXIT_TIMEOUT, observed["data_exclusive"][1]
    assert observed["data_shared"][0] == EXIT_OK, observed["data_shared"][1]
    assert adapter.guard.held_names == ()

    after, output = run_probe(
        "try", "--dir", str(guard_dir), "--lock", DATA, "--mode", "exclusive", "--timeout-ms", "600"
    )
    assert after == EXIT_OK, output


def test_read_pinned_releases_everything_when_the_pin_raises(tmp_path: Path) -> None:
    """Boundary: a failing pin leaves no guard held, so the next reader is not starved."""
    guard_dir = tmp_path / "solution.guard"
    adapter = ReaderGuardAdapter(guard_dir, timeout_ms=1000)

    def failing_pin() -> None:
        raise RuntimeError("the pinned read failed")

    with pytest.raises(RuntimeError, match="the pinned read failed"):
        adapter.read_pinned(failing_pin)
    assert adapter.guard.held_names == ()

    for lock in (ACQUIRE, DATA):
        code, output = run_probe(
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


def test_read_pinned_calls_the_pin_exactly_once(tmp_path: Path) -> None:
    guard_dir = tmp_path / "solution.guard"
    calls: list[int] = []

    def pin() -> int:
        calls.append(len(calls))
        return 7

    assert ReaderGuardAdapter(guard_dir).read_pinned(pin) == 7
    assert calls == [0]


def test_read_pinned_refuses_a_non_callable_pin(tmp_path: Path) -> None:
    """Negative: the pin is refused before a guard is taken."""
    guard_dir = tmp_path / "solution.guard"
    adapter = ReaderGuardAdapter(guard_dir)
    with pytest.raises(GuardError) as info:
        adapter.read_pinned("not callable")  # type: ignore[arg-type]
    assert info.value.reason == "invalid_pin"
    assert adapter.guard.held_names == ()
    assert not (guard_dir / DATA).exists()


def test_reader_is_the_contract_reader_and_times_out_like_one(tmp_path: Path) -> None:
    """A holder in another process bounds the adapter's reader, which releases what it took."""
    guard_dir = tmp_path / "solution.guard"
    probe = subprocess.Popen(
        [
            *DEFAULT_HOLDER_ARGV,
            "hold",
            "--dir",
            str(guard_dir),
            "--locks",
            DATA,
            "--mode",
            "exclusive",
            "--hold-ms",
            "2000",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=probe_env(),
    )
    try:
        assert probe.stdout is not None
        announcement = json.loads(probe.stdout.readline())
        assert announcement["event"] == "acquired"
        adapter = ReaderGuardAdapter(guard_dir, timeout_ms=400)
        started = time.monotonic()
        with pytest.raises(GuardTimeout) as info:
            with adapter.reader():
                pytest.fail("the reader must not enter while data.lock is held elsewhere")
        elapsed = time.monotonic() - started
        assert 0.3 <= elapsed <= 2.0
        assert info.value.reason == "lock_timeout"
        assert adapter.guard.held_names == ()
    finally:
        probe.wait(timeout=30)
    assert probe.returncode == EXIT_OK


def test_holder_argv_resolution_refuses_an_unusable_holder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative: a configured holder that cannot be started is reported, not substituted."""
    monkeypatch.delenv(HOLDER_ARGV_ENV, raising=False)
    assert resolve_holder_argv() == DEFAULT_HOLDER_ARGV
    assert holder_is_foreign() is False

    monkeypatch.setenv(HOLDER_ARGV_ENV, "not json")
    with pytest.raises(GuardError) as invalid:
        resolve_holder_argv()
    assert invalid.value.reason == "holder_argv_invalid"

    monkeypatch.setenv(HOLDER_ARGV_ENV, json.dumps(["axiom-holder-that-does-not-exist"]))
    with pytest.raises(GuardError) as missing:
        resolve_holder_argv()
    assert missing.value.reason == "holder_unavailable"

    monkeypatch.setenv(HOLDER_ARGV_ENV, json.dumps([sys.executable, "-c", "pass"]))
    resolved = resolve_holder_argv()
    assert resolved[0] == sys.executable
    assert resolved[1:] == ("-c", "pass")
    assert holder_is_foreign() is True
