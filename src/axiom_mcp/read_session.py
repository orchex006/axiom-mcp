"""The read session: bytes are copied under the shared guard, then the guard is released.

``docs/12-SNAPSHOT-READ-WRITE-PROTOCOL.md`` section 3.1 fixes the shape of an MCP read: take
the shared guard, read the catalog/pointer once, validate the manifest, load only the shards
the query needs, verify their hashes and complete the query inputs *into memory*, then release.
The network response is computed and sent after the guard is released. Section 5 repeats it as
reader steps 2 to 7. ``repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md`` section 9 adds the part a
publisher cares about: a cancellation or a slow client must not hold guards, and file I/O must
be bounded off the event loop.

So this module splits one read into two phases that cannot be confused with each other:

* :meth:`ReadSession.load` copies the pointer, the manifest and the required shards into memory
  while the guard is held, and releases the guard before it returns. The copy is bounded by
  :class:`ReadLimits` and :class:`~axiom_mcp.shards.ShardLimits`, so the window in which a
  publisher can be blocked is bounded by bytes rather than by how slow a client is.
* :meth:`ReadSession.render` and :meth:`ReadSession.stream` build and emit the response after
  the guard is gone. Both refuse to run while this session still holds a lock, so "the response
  is computed outside the guard" is enforced rather than documented.

Parsing happens in the second phase, on the bytes that were copied and hashed in the first.
That is deliberate: the protocol requires the *query inputs* to be in memory before the guard
is released, and once the pinned bytes are in memory their digest is fixed, so parsing them
afterwards cannot observe a newer generation. The snapshot records both facts -
``guard_held_during_copy`` and ``parsed_after_guard_release`` - so a caller, a test or an
evidence file can see which phase did the work instead of assuming it.

Freshness is not invented here. This session never talks to the daemon, so the snapshot carries
``freshness="unknown"`` and ``verification="manifest_hash"``: a hash-verified pinned generation
is evidence that the bytes are the published ones, not evidence that they are newer than the
source tree.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from axiom_mcp import manifest as manifest_module
from axiom_mcp import shards as shards_module
from axiom_mcp.guard.engine import SolutionGuard
from axiom_mcp.manifest import MANIFEST_FILENAME, Manifest
from axiom_mcp.registry import SnapshotLocation
from axiom_mcp.shards import CopiedShard, Shard, ShardLimits, read_bounded

VERIFICATION_MANIFEST_HASH = "manifest_hash"
FRESHNESS_UNKNOWN = "unknown"
DEFAULT_CHUNK_BYTES = 4096


class ReadSessionError(ValueError):
    """A read session that cannot copy, verify or render a generation safely."""


class BoundedCopyExceeded(ReadSessionError):
    """A pointer, manifest or shard whose bytes exceed the session's hard copy budget."""


class GuardStillHeld(ReadSessionError):
    """A response operation attempted while this session still holds a read lock."""


@dataclass(frozen=True)
class ReadLimits:
    """The hard byte caps one bounded copy runs under."""

    max_pointer_bytes: int = 64 * 1024
    max_manifest_bytes: int = 4 * 1024 * 1024
    shard_limits: ShardLimits = ShardLimits()

    def __post_init__(self) -> None:
        for name in ("max_pointer_bytes", "max_manifest_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ReadSessionError(f"{name} must be a positive integer, got {value!r}")
        if not isinstance(self.shard_limits, ShardLimits):
            raise ReadSessionError("shard_limits must be a ShardLimits value")


@dataclass(frozen=True)
class CopiedGeneration:
    """Everything one query needs, copied into memory while the guard was held."""

    solution_id: str
    project_id: str
    lane: str
    generation_id: str
    pointer_bytes: bytes
    manifest: Manifest
    shards: tuple[CopiedShard, ...]
    bytes_copied: int


@dataclass(frozen=True)
class LoadedSnapshot:
    """A verified, parsed, immutable answer input, with no lock held any more."""

    solution_id: str
    project_id: str
    lane: str
    generation_id: str
    analysis_profile: str
    source_fingerprint: str
    coverage_status: str
    records: int
    shards: tuple[Shard, ...]
    bytes_copied: int
    copy_ms: int
    guard_held_during_copy: bool
    parsed_after_guard_release: bool
    verification: str = VERIFICATION_MANIFEST_HASH
    freshness: str = FRESHNESS_UNKNOWN

    def shard(self, path: str) -> Shard:
        for item in self.shards:
            if item.path == path:
                return item
        raise ReadSessionError(f"the loaded snapshot holds no shard {path!r}")

    def as_document(self) -> dict[str, Any]:
        """The response skeleton: identity, provenance and budgets, never the whole graph."""
        return {
            "solution_id": self.solution_id,
            "project_id": self.project_id,
            "generation_id": self.generation_id,
            "lane": self.lane,
            "analysis_profile": self.analysis_profile,
            "source_fingerprint": self.source_fingerprint,
            "coverage": self.coverage_status,
            "freshness": self.freshness,
            "verification": self.verification,
            "shards": [
                {"path": item.path, "role": item.role, "records": item.records}
                for item in self.shards
            ],
        }


class ReadSession:
    """One bounded copy and one unguarded response, for one pinned generation.

    ``read`` is the bounded byte source. It defaults to
    :func:`axiom_mcp.shards.read_bounded` and receives ``(path, limit)``; a gateway that reads
    from a different store passes its own function. It is called only while the guard is held,
    so an injected source is also a place to observe the copy window.
    """

    def __init__(
        self,
        guard: SolutionGuard,
        location: SnapshotLocation,
        *,
        limits: ReadLimits | None = None,
        roles: tuple[str, ...] | None = None,
        read: Callable[[Path, int], bytes] = read_bounded,
        clock: Callable[[], float] = time.monotonic,
        attempts: int = 1,
    ) -> None:
        if not isinstance(location, SnapshotLocation):
            raise ReadSessionError("a resolved snapshot location is required to open a session")
        if not isinstance(guard, SolutionGuard):
            raise ReadSessionError("a SolutionGuard is required to open a read session")
        self._guard = guard
        self._location = location
        self._limits = limits or ReadLimits()
        self._roles = roles
        self._read = read
        self._clock = clock
        self._attempts = attempts

    @property
    def guard(self) -> SolutionGuard:
        return self._guard

    @property
    def location(self) -> SnapshotLocation:
        return self._location

    @property
    def limits(self) -> ReadLimits:
        return self._limits

    def _bounded(self, path: Path, cap: int, what: str) -> bytes:
        payload = self._read(path, cap)
        if len(payload) > cap:
            raise BoundedCopyExceeded(f"{what} is larger than the hard cap {cap} bytes")
        return bytes(payload)

    def _copy_generation(self) -> CopiedGeneration:
        """Copy pointer, manifest and required shards. Caller holds the shared guard."""
        pointer_bytes = self._bounded(
            self._location.pointer, self._limits.max_pointer_bytes, "the lane pointer"
        )
        pointer = manifest_module.load_pointer_bytes(pointer_bytes)
        generation_dir = self._location.generation_root(pointer.directory_name)
        manifest_bytes = self._bounded(
            generation_dir / MANIFEST_FILENAME,
            self._limits.max_manifest_bytes,
            f"manifest {pointer.directory_name[:12]}",
        )
        manifest = manifest_module.load_manifest_bytes(
            manifest_bytes, what=f"manifest {pointer.directory_name[:12]}"
        )
        if generation_dir.name != manifest.generation_id:
            raise manifest_module.DigestMismatch(
                f"generation directory {generation_dir.name} does not match the manifest bytes "
                f"{manifest.generation_id}"
            )
        manifest.verify_pointer(pointer)
        copied = shards_module.copy_shards(
            self._location,
            manifest,
            roles=self._roles,
            limits=self._limits.shard_limits,
            read=self._read,
        )
        return CopiedGeneration(
            solution_id=manifest.solution_id,
            project_id=manifest.project_id,
            lane=self._location.lane,
            generation_id=manifest.generation_id,
            pointer_bytes=pointer_bytes,
            manifest=manifest,
            shards=copied,
            bytes_copied=len(pointer_bytes) + len(manifest_bytes) + sum(i.bytes for i in copied),
        )

    def load(self) -> LoadedSnapshot:
        """Copy the required bytes under the shared guard, then release it and parse.

        The guard is released by the reader context before this method returns, and the release
        is checked: a session that still holds a lock after the copy raises instead of handing
        back a snapshot whose response would silently block a publisher.
        """
        started = self._clock()
        with self._guard.reader(attempts=self._attempts):
            held = self._guard.held_names
            if "data.lock" not in held:
                raise ReadSessionError(
                    f"the reader context did not hold the data guard; held={list(held)}"
                )
            copied = self._copy_generation()
        if self._guard.held_names:
            raise ReadSessionError(
                f"the read session still holds {list(self._guard.held_names)} after the copy"
            )
        verified = shards_module.verify_copied(copied.manifest, copied.shards)
        return LoadedSnapshot(
            solution_id=copied.solution_id,
            project_id=copied.project_id,
            lane=copied.lane,
            generation_id=copied.generation_id,
            analysis_profile=copied.manifest.analysis_profile,
            source_fingerprint=copied.manifest.source_fingerprint,
            coverage_status=copied.manifest.coverage_status,
            records=sum(item.records for item in verified),
            shards=verified,
            bytes_copied=copied.bytes_copied,
            copy_ms=max(int((self._clock() - started) * 1000), 0),
            guard_held_during_copy=True,
            parsed_after_guard_release=True,
        )

    def _require_released(self) -> None:
        held = self._guard.held_names
        if held:
            raise GuardStillHeld(
                f"the response must be built after the read locks are released; held={list(held)}"
            )

    def render(self, snapshot: LoadedSnapshot) -> dict[str, Any]:
        """Build the response body. Refuses to run while a read lock is still held."""
        self._require_released()
        return snapshot.as_document()

    def stream(
        self, snapshot: LoadedSnapshot, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES
    ) -> Iterator[bytes]:
        """Yield the rendered response in bounded chunks, with no lock held.

        A client that stops reading only stops this generator: the guard was released before the
        first chunk, so a stalled consumer cannot keep a publisher out of the lane.
        """
        if isinstance(chunk_bytes, bool) or not isinstance(chunk_bytes, int) or chunk_bytes < 1:
            raise ReadSessionError(f"chunk_bytes must be a positive integer, got {chunk_bytes!r}")
        document = self.render(snapshot)
        payload = json.dumps(
            document, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        for offset in range(0, len(payload), chunk_bytes):
            yield payload[offset : offset + chunk_bytes]


__all__ = [
    "BoundedCopyExceeded",
    "CopiedGeneration",
    "DEFAULT_CHUNK_BYTES",
    "FRESHNESS_UNKNOWN",
    "GuardStillHeld",
    "LoadedSnapshot",
    "ReadLimits",
    "ReadSession",
    "ReadSessionError",
    "VERIFICATION_MANIFEST_HASH",
]
