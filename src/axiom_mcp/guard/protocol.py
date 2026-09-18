"""The frozen native reader/writer guard protocol, as constants this package consumes.

The protocol is owned by ``axiom-specs``; the normative source is
``contracts/native-reader-writer-guards.md`` in the specification repository, and the
``contract_id`` and ``protocol_digest`` below pin which revision this package was built
against. This module restates no rule of its own: it names the same two lock files, the same
acquisition and release order, the same Windows byte range and the same bounded-wait budget
that Rust is required to implement, so that the Python side joins the existing protocol
instead of inventing a parallel one.

Consumed, not forked: ``tests/test_guard_protocol.py`` compares every constant here against
the canonical document and runs that document's own reference evaluator when a
specification checkout is discoverable, so a protocol change this module does not follow
fails the build rather than drifting silently.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "ACQUISITION_ORDER",
    "CONTRACT_DOCUMENT",
    "CONTRACT_ID",
    "CONTRACT_VERSION",
    "DEFAULT_TIMEOUT_MS",
    "GUARD_DIRECTORY_NAME",
    "GUARD_DIRECTORY_TEMPLATE",
    "INSTANCES_DIRECTORY_NAME",
    "LOCK_FILE_NAMES",
    "MAX_TIMEOUT_MS",
    "PROTOCOL_DIGEST",
    "RELEASE_ORDER",
    "RETRY_INITIAL_MS",
    "RETRY_MAX_MS",
    "SPEC_VERSION",
    "WINDOWS_BYTE_RANGE",
    "WINDOWS_OPEN_MODE",
    "WINDOWS_SHARE_MODE_EXCLUDES",
    "file_identity",
    "guard_directory",
    "identity_of_stat",
    "lock_paths",
]

CONTRACT_ID = "axiom-native-reader-writer-guards"
CONTRACT_VERSION = 1
CONTRACT_DOCUMENT = "contracts/native-reader-writer-guards.md"
SPEC_VERSION = "2.0.0-draft.1"
PROTOCOL_DIGEST = "06fadbdcd8ef4fba9573b10f838f060bc23652286ba757061249737b1d717f8a"

GUARD_DIRECTORY_TEMPLATE = "<AXIOM_HOME>/instances/<workspace-instance-id>/solution.guard"
INSTANCES_DIRECTORY_NAME = "instances"
GUARD_DIRECTORY_NAME = "solution.guard"

# The normative lock table lists admission before data, and both the acquisition order and
# the reverse release order are part of the frozen block rather than a local choice.
LOCK_FILE_NAMES = ("admission.lock", "data.lock")
ACQUISITION_ORDER = LOCK_FILE_NAMES
RELEASE_ORDER = tuple(reversed(LOCK_FILE_NAMES))

# bounded_wait: default 5000 ms, no single wait above 60000 ms, retry 25 ms doubling to a
# 250 ms cap, cancellation supported, unbounded blocking not permitted.
DEFAULT_TIMEOUT_MS = 5000
MAX_TIMEOUT_MS = 60000
RETRY_INITIAL_MS = 25
RETRY_MAX_MS = 250

# windows_byte_range "offset 0, length 1", windows_open_mode "OPEN_ALWAYS", share mode
# FILE_SHARE_READ | FILE_SHARE_WRITE, which excludes FILE_SHARE_DELETE.
WINDOWS_BYTE_RANGE = (0, 1)
WINDOWS_OPEN_MODE = "OPEN_ALWAYS"
WINDOWS_SHARE_MODE_EXCLUDES = "FILE_SHARE_DELETE"


def guard_directory(axiom_home: str | os.PathLike[str], workspace_instance_id: str) -> Path:
    """Resolve the one trusted private guard directory for a bound workspace instance.

    The template above is normative: every language resolves the same verified file identity
    from this path, so the instance id is checked rather than pasted into a path.
    """
    if not isinstance(workspace_instance_id, str) or not workspace_instance_id.strip():
        raise ValueError("workspace_instance_id must be a non-empty string")
    if workspace_instance_id != workspace_instance_id.strip():
        raise ValueError("workspace_instance_id must not carry surrounding whitespace")
    if workspace_instance_id in {".", ".."} or any(
        separator in workspace_instance_id for separator in ("/", "\\", os.sep)
    ):
        raise ValueError(
            f"workspace_instance_id must be a single path segment: {workspace_instance_id!r}"
        )
    return (
        Path(axiom_home) / INSTANCES_DIRECTORY_NAME / workspace_instance_id / GUARD_DIRECTORY_NAME
    )


def lock_paths(guard_dir: str | os.PathLike[str]) -> dict[str, Path]:
    """Map each declared lock name to its path, in acquisition order."""
    directory = Path(guard_dir)
    return {name: directory / name for name in LOCK_FILE_NAMES}


def identity_of_stat(stat_result: os.stat_result) -> str:
    """Stable file identity of an already-resolved path or open handle.

    Device plus file index is the identity both languages agree on: a path that was replaced
    behind a caller keeps its name and changes this value, which is how a stale handle is
    detected instead of being used to lock an unrelated file.
    """
    return f"{int(stat_result.st_dev):x}:{int(stat_result.st_ino):x}"


def file_identity(path: str | os.PathLike[str]) -> str:
    """Stable identity of the file currently reachable at ``path``."""
    return identity_of_stat(os.stat(path))
