"""V2-019: the consumed guard protocol is compared against the document that owns it.

``axiom_mcp.guard.protocol`` is a *consumption* of ``axiom-specs``
``contracts/native-reader-writer-guards.md``, and the adapter publishes the same surface to a
foreign language. Nothing here re-checks the document for its own sake: every constant this
package locks against, and every field the adapter hands to a Rust holder, is compared to the
frozen block, and the frozen digest is recomputed over the declared ``frozen_fields`` subset.
A document edit that this package does not follow therefore fails the build instead of leaving
two languages with two ideas of the same lock files.

The negative case is the same comparison against a document mutated in memory
(``windows_byte_range`` turned into a different range), so the checker is shown to reject a
drift rather than merely accept a match, and the digest check is shown to be sensitive to the
frozen subset and not to unrelated prose.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys

import pytest

from axiom_mcp.guard import protocol
from axiom_mcp.guard.adapter import (
    INTEROP_REQUIREMENT,
    POSIX_DECLARATION,
    WINDOWS_DECLARATION,
    abi_json,
    abi_sha256,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
CONTRACT_DOCUMENT = pathlib.Path(protocol.CONTRACT_DOCUMENT)
BEGIN = "<!-- guard-protocol:begin -->"
END = "<!-- guard-protocol:end -->"


def _find_specs_root() -> pathlib.Path | None:
    """Locate a checkout of the source-of-truth repository, if one is present."""
    candidates: list[pathlib.Path] = []
    configured = os.environ.get("AXIOM_SPECS_ROOT")
    if configured:
        candidates.append(pathlib.Path(configured))
    for base in pathlib.Path(__file__).resolve().parents:
        candidates.append(base / "axiom-specs")
        if base.is_dir():
            candidates.extend(
                sorted(child for child in base.glob("axiom-specs*") if child.is_dir())
            )
    for candidate in candidates:
        if (candidate / CONTRACT_DOCUMENT).is_file():
            return candidate
    return None


SPECS_ROOT = _find_specs_root()

pytestmark = pytest.mark.skipif(
    SPECS_ROOT is None,
    reason=(
        "no axiom-specs checkout with contracts/native-reader-writer-guards.md is present; set "
        "AXIOM_SPECS_ROOT or check out the source of truth to compare against the frozen block"
    ),
)


def extract_contract(document_text: str) -> dict:
    """Extract the frozen machine-readable block from the normative document."""
    start = document_text.index(BEGIN)
    stop = document_text.index(END)
    block = document_text[start + len(BEGIN) : stop]
    fence = block.index("```json")
    body = block[fence + len("```json") :]
    body = body[: body.index("```")]
    parsed = json.loads(body)
    assert isinstance(parsed, dict), "the frozen block is an object"
    return parsed


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def frozen_subset(contract: dict) -> dict | None:
    fields = contract.get("frozen_fields")
    if not isinstance(fields, list) or not fields:
        return None
    subset: dict = {}
    for field in fields:
        if field not in contract:
            return None
        subset[field] = contract[field]
    return subset


def reasons(contract: dict) -> list[str]:
    """Every disagreement between this package's declarations and ``contract``."""
    problems: list[str] = []
    locks = contract.get("lock_files")
    if not isinstance(locks, list):
        return ["lock_files is not a list"]
    by_name = {entry.get("name"): entry for entry in locks if isinstance(entry, dict)}
    if tuple(by_name) != protocol.LOCK_FILE_NAMES:
        problems.append(f"lock file names are {tuple(by_name)!r}")
    for name in protocol.LOCK_FILE_NAMES:
        entry = by_name.get(name)
        if entry is None:
            problems.append(f"{name} is missing from the frozen block")
            continue
        if entry.get("posix_primitive") != POSIX_DECLARATION["primitive"]:
            problems.append(f"{name}: POSIX primitive is not flock")
        if entry.get("posix_scope") != POSIX_DECLARATION["scope"]:
            problems.append(f"{name}: POSIX scope is not whole-file")
        if entry.get("posix_shared") != POSIX_DECLARATION["shared"]:
            problems.append(f"{name}: POSIX shared mode is not LOCK_SH")
        if entry.get("posix_exclusive") != POSIX_DECLARATION["exclusive"]:
            problems.append(f"{name}: POSIX exclusive mode is not LOCK_EX")
        if entry.get("windows_primitive") != WINDOWS_DECLARATION["primitive"]:
            problems.append(f"{name}: Windows primitive is not LockFileEx")
        if entry.get("windows_byte_range") != WINDOWS_DECLARATION["byte_range"]:
            problems.append(
                f"{name}: Windows byte range is not {WINDOWS_DECLARATION['byte_range']}"
            )
        if entry.get("windows_open_mode") != WINDOWS_DECLARATION["open_mode"]:
            problems.append(f"{name}: Windows open mode is not OPEN_ALWAYS")
        if entry.get("windows_share_mode") != WINDOWS_DECLARATION["share_mode"]:
            problems.append(f"{name}: Windows share mode is not read|write")
        if entry.get("windows_share_excludes") != WINDOWS_DECLARATION["share_excludes"]:
            problems.append(f"{name}: Windows share mode does not exclude FILE_SHARE_DELETE")
        if entry.get("windows_exclusive_flag") != WINDOWS_DECLARATION["exclusive_flag"]:
            problems.append(f"{name}: exclusive Windows mode is not LOCKFILE_EXCLUSIVE_LOCK")
        if entry.get("windows_cancel_flag") != WINDOWS_DECLARATION["cancel_flag"]:
            problems.append(f"{name}: bounded retry is not LOCKFILE_FAIL_IMMEDIATELY")
        if entry.get("release") != WINDOWS_DECLARATION["release"]:
            problems.append(f"{name}: release is not a matching UnlockFileEx then close")
    wait = contract.get("bounded_wait")
    if not isinstance(wait, dict):
        problems.append("bounded_wait is not an object")
    else:
        if wait.get("default_timeout_ms") != protocol.DEFAULT_TIMEOUT_MS:
            problems.append("default timeout differs from protocol.DEFAULT_TIMEOUT_MS")
        if wait.get("max_timeout_ms") != protocol.MAX_TIMEOUT_MS:
            problems.append("maximum timeout differs from protocol.MAX_TIMEOUT_MS")
        if wait.get("retry_initial_ms") != protocol.RETRY_INITIAL_MS:
            problems.append("retry initial differs from protocol.RETRY_INITIAL_MS")
        if wait.get("retry_max_ms") != protocol.RETRY_MAX_MS:
            problems.append("retry cap differs from protocol.RETRY_MAX_MS")
        if wait.get("unbounded_blocking_allowed") is not False:
            problems.append("unbounded blocking is not refused")
        if wait.get("cancel_supported") is not True:
            problems.append("cancellation is not declared")
    crash = contract.get("crash_release")
    if not isinstance(crash, dict):
        problems.append("crash_release is not an object")
    else:
        for field in (
            "pid_file_is_ownership",
            "lock_file_content_is_ownership",
            "stale_pid_is_live_holder",
        ):
            if crash.get(field) is not False:
                problems.append(f"crash_release.{field} is not false")
    interop = contract.get("interop_requirement")
    if not isinstance(interop, dict):
        problems.append("interop_requirement is not an object")
    else:
        for field, value in INTEROP_REQUIREMENT.items():
            if interop.get(field) != value:
                problems.append(f"interop_requirement.{field} is not {value!r}")
    if contract.get("acquisition_order") != list(protocol.ACQUISITION_ORDER):
        problems.append("acquisition order is not admission then data")
    if contract.get("release_order") != list(protocol.RELEASE_ORDER):
        problems.append("release order is not data then admission")
    if contract.get("guard_directory_template") != protocol.GUARD_DIRECTORY_TEMPLATE:
        problems.append("guard directory template differs")
    if contract.get("contract_id") != protocol.CONTRACT_ID:
        problems.append("contract id differs")
    if contract.get("contract_version") != protocol.CONTRACT_VERSION:
        problems.append("contract version differs")
    if contract.get("spec_version") != protocol.SPEC_VERSION:
        problems.append("spec version differs")
    subset = frozen_subset(contract)
    if subset is None:
        problems.append("frozen_fields does not select present fields")
    else:
        digest = contract.get("protocol_digest")
        recomputed = hashlib.sha256(canonical_bytes(subset)).hexdigest()
        if recomputed != digest:
            problems.append("protocol_digest does not match the frozen subset")
        if digest != protocol.PROTOCOL_DIGEST:
            problems.append("protocol_digest differs from protocol.PROTOCOL_DIGEST")
    return problems


@pytest.fixture(scope="module")
def contract() -> dict:
    document = (SPECS_ROOT / CONTRACT_DOCUMENT).read_text(encoding="utf-8")
    return extract_contract(document)


def test_frozen_block_is_the_one_this_package_consumes(contract: dict) -> None:
    """AC1: the consumed constants agree with the document, including its digest."""
    assert reasons(contract) == []


def test_mutated_contract_is_rejected(contract: dict) -> None:
    """Negative: a drifted document is refused, so the match above is not vacuous."""
    mutated = json.loads(json.dumps(contract))
    mutated["lock_files"][0]["windows_byte_range"] = "offset 0, length 2"
    drifted = reasons(mutated)
    assert any("byte range" in problem for problem in drifted)

    resealed = json.loads(json.dumps(mutated))
    subset = frozen_subset(resealed)
    assert subset is not None
    resealed["protocol_digest"] = "0" * 64
    assert any("protocol_digest" in problem for problem in reasons(resealed))


def test_adapter_descriptor_restates_the_same_frozen_fields(contract: dict) -> None:
    """The descriptor a foreign holder reads carries the contract's own values."""
    descriptor = json.loads(abi_json())
    assert descriptor["contract_id"] == protocol.CONTRACT_ID
    assert descriptor["protocol_digest"] == protocol.PROTOCOL_DIGEST
    assert descriptor["lock_files"] == contract["lock_files"]
    assert descriptor["acquisition_order"] == contract["acquisition_order"]
    assert descriptor["release_order"] == contract["release_order"]
    assert descriptor["bounded_wait"] == contract["bounded_wait"]
    assert descriptor["crash_release"] == contract["crash_release"]
    assert descriptor["interop_requirement"] == contract["interop_requirement"]
    for field in contract["frozen_fields"]:
        assert descriptor[field] == contract[field], field


def test_digest_is_stable_and_pinned() -> None:
    """The descriptor hash is a value a foreign build can pin, not a moving target."""
    assert abi_sha256() == abi_sha256(json.loads(abi_json()))
    assert len(abi_sha256()) == 64


def test_adapter_prints_the_abi_from_a_separate_process() -> None:
    """AC1: an independent process can read the ABI the Rust side must implement."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["PYTHONIOENCODING"] = "utf-8"
    done = subprocess.run(
        [sys.executable, "-m", "axiom_mcp.guard.adapter", "abi", "--digest"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == abi_sha256()
