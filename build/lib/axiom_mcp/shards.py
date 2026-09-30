"""Bounded shard loading: caps and lane containment are enforced before any parse.

``docs/12-SNAPSHOT-READ-WRITE-PROTOCOL.md`` section 5 step 5 fixes the traversal rule this
module makes executable: a reader walks only the manifest's indexes, never globs JSON, and
every file must stay under the bound graph root after canonicalization and must not be a
symlink escape. ``docs/11-GRAPH-DATA-CONTRACT.md`` section 6 fixes the other half: one shard
targets 4 MiB, its hard cap is 16 MiB, and each shard path is a fixed location under the
generation for the role it plays. Composition - "required bytes enter bounded memory" - is
``docs/12-SNAPSHOT-READ-WRITE-PROTOCOL.md`` section 3.1 and
``repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md`` section 3.

The order of the checks is the whole point of this module, so it is stated once, here, and the
tests pin it:

1. the entry's role must own the directory its path claims;
2. the declared byte size must fit the caller's hard cap, and the plan's declared total must
   fit the plan budget - both before the file is opened;
3. the path must resolve inside the generation directory, must not be a symlink and must be a
   regular file;
4. the bytes are read through a *bounded* read of ``cap + 1`` at most, so a shard that grew
   on disk cannot be materialised into memory, and a file larger than its declared size is
   refused before any JSON parser sees it.

Only then is the shard handed to :meth:`axiom_mcp.manifest.Manifest.verify_shard`, which
applies the canonical rule - length, then digest, then parse, then record count. Cap, path and
containment are therefore enforced before parsing rather than as post-hoc validation, and the
digest of the bytes that were actually parsed is the digest the manifest declares.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from axiom_mcp.manifest import FILE_ROLES, MAX_SHARD_BYTES, Manifest, ManifestEntry
from axiom_mcp.registry import (
    GENERATIONS_DIRNAME,
    SnapshotLocation,
    UntrustedPath,
    portable_relative,
)

# The fixed shard location of each role, from docs/11-GRAPH-DATA-CONTRACT.md section 6.
# A role that declares a path outside its own directory is not the role it claims to be, so it
# is refused rather than read as an exotic path the reader did not expect.
ROLE_DIRECTORIES: Mapping[str, str] = {
    "nodes": "nodes/",
    "edges": "edges/",
    "symbol_index": "indexes/symbols/",
    "outgoing_index": "indexes/outgoing/",
    "incoming_index": "indexes/incoming/",
    "architecture_summary": "architecture/",
}

# coverage.json is a single file at the generation root, not a bucket directory.
COVERAGE_ROLE = "coverage"
COVERAGE_PATH = "coverage.json"


class ShardError(ValueError):
    """A shard the reader must refuse before it is parsed."""


class ShardPathRejected(ShardError):
    """A shard whose role, path or resolved location is outside what the lane may serve."""


class ShardTooLarge(ShardError):
    """A shard or a shard plan that exceeds the caller's hard byte cap."""


class ShardSizeMismatch(ShardError):
    """A shard whose on-disk size is not the size the manifest declares."""


class ShardUnreadable(ShardError):
    """A shard that could not be opened or read as a regular file."""


@dataclass(frozen=True)
class ShardLimits:
    """The hard caps a bounded shard load runs under.

    ``max_shard_bytes`` may be lowered for one query but never raised above the canonical hard
    cap, because raising it would let a single shard exceed the contract's 16 MiB limit;
    refusing is safer than silently clamping a caller who asked for too much.
    """

    max_shard_bytes: int = MAX_SHARD_BYTES
    max_shards: int = 512
    max_total_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        for name in ("max_shard_bytes", "max_shards", "max_total_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ShardError(f"{name} must be a positive integer, got {value!r}")
        if self.max_shard_bytes > MAX_SHARD_BYTES:
            raise ShardError(
                f"max_shard_bytes {self.max_shard_bytes} exceeds the canonical hard cap "
                f"{MAX_SHARD_BYTES} declared by docs/11-GRAPH-DATA-CONTRACT.md section 6"
            )
        if self.max_total_bytes < self.max_shard_bytes:
            raise ShardError(
                "max_total_bytes must not be smaller than max_shard_bytes, or a single shard "
                "could never be loaded"
            )


@dataclass(frozen=True)
class CopiedShard:
    """One shard's bytes, copied into memory under whatever guard the caller holds."""

    path: str
    role: str
    sha256: str
    bytes: int
    records: int
    raw: bytes


@dataclass(frozen=True)
class Shard:
    """A copied shard that has been verified against its manifest entry and parsed."""

    path: str
    role: str
    sha256: str
    bytes: int
    records: int
    document: Any


def role_permitted_path(role: str, path: str) -> bool:
    """True when ``path`` is the fixed location ``role`` owns in a generation."""
    if role == COVERAGE_ROLE:
        return path == COVERAGE_PATH
    directory = ROLE_DIRECTORIES.get(role)
    if directory is None:
        return False
    if not path.startswith(directory):
        return False
    remainder = path[len(directory) :]
    return remainder.endswith(".json") and "/" not in remainder and remainder != ".json"


def _require_role_path(entry: ManifestEntry) -> None:
    if entry.role not in FILE_ROLES:
        raise ShardPathRejected(
            f"shard {entry.path} declares role {entry.role!r}, which is not a role of "
            "docs/11-GRAPH-DATA-CONTRACT.md section 6"
        )
    if not role_permitted_path(entry.role, entry.path):
        raise ShardPathRejected(
            f"shard {entry.path} is not a permitted path for role {entry.role!r}"
        )


def _require_declared_cap(entry: ManifestEntry, limits: ShardLimits) -> None:
    if entry.bytes > limits.max_shard_bytes:
        raise ShardTooLarge(
            f"shard {entry.path} declares {entry.bytes} bytes, above the hard cap "
            f"{limits.max_shard_bytes}"
        )


def read_bounded(path: Path, limit: int) -> bytes:
    """Read at most ``limit + 1`` bytes from ``path``.

    A caller that receives more than ``limit`` bytes knows the file is over the cap without
    ever holding the whole file, which is what keeps a runaway shard from becoming memory.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ShardError(f"limit must be a non-negative integer, got {limit!r}")
    try:
        with Path(path).open("rb") as handle:
            return handle.read(limit + 1)
    except OSError as exc:
        raise ShardUnreadable(f"{Path(path).name} is unreadable: {type(exc).__name__}") from exc


def shard_path(location: SnapshotLocation, manifest: Manifest, entry: ManifestEntry) -> Path:
    """Resolve one manifest entry to a regular file inside the generation directory.

    The requested path is refused when it is a symlink at all - a published generation holds
    regular files, so a symlink is either a mutation or an escape attempt - and the resolved
    path is required to stay inside the resolved generation directory, so an intermediate
    symlink cannot redirect the read anywhere else in the lane either.
    """
    if not isinstance(location, SnapshotLocation):
        raise ShardPathRejected("a resolved snapshot location is required to load a shard")
    _require_role_path(entry)
    generation_dir = location.generation_root(manifest.generation_id)
    relative = f"{GENERATIONS_DIRNAME}/{manifest.generation_id}/{entry.path}"
    try:
        portable_relative(relative)
    except UntrustedPath as exc:
        raise ShardPathRejected(f"shard {entry.path} is not a portable path: {exc}") from exc
    candidate = generation_dir.joinpath(*PurePosixPath(entry.path).parts)
    try:
        info = os.lstat(candidate)
    except OSError as exc:
        raise ShardUnreadable(f"shard {entry.path} is unreadable: {type(exc).__name__}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ShardPathRejected(
            f"shard {entry.path} is a symbolic link; a published generation holds regular files"
        )
    if not stat.S_ISREG(info.st_mode):
        raise ShardPathRejected(f"shard {entry.path} is not a regular file")
    try:
        resolved = location.resolve(relative)
    except UntrustedPath as exc:
        raise ShardPathRejected(f"shard {entry.path} escapes the registered lane: {exc}") from exc
    generation_root = generation_dir.resolve()
    if generation_root not in resolved.parents:
        raise ShardPathRejected(f"shard {entry.path} resolves outside its generation directory")
    return resolved


def _selected_entries(
    manifest: Manifest, roles: tuple[str, ...] | None
) -> tuple[ManifestEntry, ...]:
    if roles is None:
        entries = manifest.files
    else:
        unknown = [role for role in roles if role not in FILE_ROLES]
        if unknown:
            raise ShardError(f"unknown shard role(s): {sorted(unknown)}")
        wanted = set(roles)
        entries = tuple(entry for entry in manifest.files if entry.role in wanted)
    if not entries:
        raise ShardError(
            f"manifest {manifest.generation_id[:12]} declares no shard for the requested roles"
        )
    return tuple(sorted(entries, key=lambda item: item.path))


def _require_plan_cap(entries: tuple[ManifestEntry, ...], limits: ShardLimits) -> None:
    if len(entries) > limits.max_shards:
        raise ShardTooLarge(
            f"plan selects {len(entries)} shards, above the hard cap {limits.max_shards}"
        )
    total = sum(entry.bytes for entry in entries)
    if total > limits.max_total_bytes:
        raise ShardTooLarge(
            f"plan declares {total} bytes, above the plan budget {limits.max_total_bytes}"
        )


def copy_shard(
    location: SnapshotLocation,
    manifest: Manifest,
    entry: ManifestEntry,
    *,
    limits: ShardLimits | None = None,
    read: Callable[[Path, int], bytes] = read_bounded,
) -> CopiedShard:
    """Copy one shard into memory, enforcing the cap and the path rules before reading it."""
    effective = limits or ShardLimits()
    _require_role_path(entry)
    _require_declared_cap(entry, effective)
    resolved = shard_path(location, manifest, entry)
    try:
        payload = read(resolved, effective.max_shard_bytes)
    except ShardUnreadable as exc:
        raise ShardUnreadable(f"shard {entry.path} is unreadable: {exc}") from exc
    if len(payload) > effective.max_shard_bytes:
        raise ShardTooLarge(
            f"shard {entry.path} is larger than the hard cap {effective.max_shard_bytes} bytes"
        )
    if len(payload) != entry.bytes:
        raise ShardSizeMismatch(
            f"shard {entry.path} is {len(payload)} bytes on disk but the manifest declares "
            f"{entry.bytes}"
        )
    return CopiedShard(
        path=entry.path,
        role=entry.role,
        sha256=entry.sha256,
        bytes=entry.bytes,
        records=entry.records,
        raw=bytes(payload),
    )


def copy_shards(
    location: SnapshotLocation,
    manifest: Manifest,
    *,
    roles: tuple[str, ...] | None = None,
    limits: ShardLimits | None = None,
    read: Callable[[Path, int], bytes] = read_bounded,
) -> tuple[CopiedShard, ...]:
    """Copy only the shards the query needs, within one declared plan budget.

    The plan's declared total is checked before the first file is opened, so a manifest that
    claims more bytes than the budget is refused without touching the disk at all.
    """
    effective = limits or ShardLimits()
    entries = _selected_entries(manifest, roles)
    _require_plan_cap(entries, effective)
    return tuple(
        copy_shard(location, manifest, entry, limits=effective, read=read) for entry in entries
    )


def verify_copied(manifest: Manifest, copied: tuple[CopiedShard, ...]) -> tuple[Shard, ...]:
    """Verify pinned bytes against their manifest entry and parse them.

    Verification runs on the in-memory copy, so the bytes that are hashed, parsed and counted
    are exactly the bytes that were copied under the guard.
    """
    verified: list[Shard] = []
    for item in copied:
        entry = manifest.entry(item.path)
        document = manifest.verify_shard(entry, item.raw)
        verified.append(
            Shard(
                path=item.path,
                role=item.role,
                sha256=item.sha256,
                bytes=item.bytes,
                records=item.records,
                document=document,
            )
        )
    return tuple(verified)


def load_shard(
    location: SnapshotLocation,
    manifest: Manifest,
    entry: ManifestEntry,
    *,
    limits: ShardLimits | None = None,
    read: Callable[[Path, int], bytes] = read_bounded,
) -> Shard:
    """Copy, verify and parse one shard."""
    effective = limits or ShardLimits()
    copied = copy_shard(location, manifest, entry, limits=effective, read=read)
    return verify_copied(manifest, (copied,))[0]


def load_shards(
    location: SnapshotLocation,
    manifest: Manifest,
    *,
    roles: tuple[str, ...] | None = None,
    limits: ShardLimits | None = None,
    read: Callable[[Path, int], bytes] = read_bounded,
) -> tuple[Shard, ...]:
    """Copy, verify and parse the shards the query needs, within one plan budget."""
    effective = limits or ShardLimits()
    copied = copy_shards(location, manifest, roles=roles, limits=effective, read=read)
    return verify_copied(manifest, copied)


__all__ = [
    "COVERAGE_PATH",
    "COVERAGE_ROLE",
    "CopiedShard",
    "ROLE_DIRECTORIES",
    "Shard",
    "ShardError",
    "ShardLimits",
    "ShardPathRejected",
    "ShardSizeMismatch",
    "ShardTooLarge",
    "ShardUnreadable",
    "copy_shard",
    "copy_shards",
    "load_shard",
    "load_shards",
    "read_bounded",
    "role_permitted_path",
    "shard_path",
    "verify_copied",
]
