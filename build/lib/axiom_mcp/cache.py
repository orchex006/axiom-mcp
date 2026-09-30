"""An immutable-generation cache: bounded LRU keyed by identity, never by a pointer.

``docs/12-SNAPSHOT-READ-WRITE-PROTOCOL.md`` SNP-03 says one query reads one exact generation,
and the read model is a pinned vector of ``(project_id, generation_id, source_fingerprint)``;
nothing in a read may follow ``current.json`` again. A cache in front of that read must not
reintroduce the bug the pin removed. The question a cache answers is "have I already got the
bytes for *this immutable generation*", and the only way to answer it correctly is to key the
entry by the immutable identity itself, never by the lane, the project name or a pointer.

The identity this module uses is :class:`CacheKey`: the payload's document ``schema``, the
``analysis_profile`` the bytes were computed for, the generation's ``generation_id`` (the
sha256 that names its directory), and the ``shard_sha256`` the manifest declares for those
bytes. The manifest entry ``path`` rides along as part of the key. A pointer is deliberately
absent: when the lane pointer moves to a newer generation the derived key changes, so the old
entry cannot be hit and cannot be relabelled as the new generation.

Two invariants make "cached data under the wrong generation label" impossible rather than
merely unlikely:

* :meth:`SnapshotCache.put` refuses bytes whose sha256 is not the key's ``shard_sha256``, so a
  caller cannot insert bytes under a label they do not hash to.
* :meth:`SnapshotCache.get` re-hashes the stored bytes before returning them, so an entry that
  was corrupted or mis-keyed in memory is dropped and reported as a miss, never handed back.

Memory is bounded twice over: ``max_entries`` bounds the number of live entries and
``max_bytes`` bounds their total payload size. Both are enforced by evicting the
least-recently-used entry until the limits hold, so an unbounded working set cannot grow the
process. A payload that is itself larger than ``max_bytes`` is refused without evicting
anything, because it can never fit and evicting the whole cache for it would only lose data.

Only ``hashlib``, ``collections`` and the standard library are used; the reader works with no
daemon running.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from axiom_mcp import manifest as manifest_module
from axiom_mcp.shards import CopiedShard

DEFAULT_SHARD_SCHEMA = f"graph-data.v{manifest_module.SUPPORTED_SCHEMA_MAJOR}"
DEFAULT_MAX_ENTRIES = 512
DEFAULT_MAX_BYTES = 64 * 1024 * 1024


class CacheError(ValueError):
    """A cache configuration, key or payload the cache must refuse."""


class CacheMismatch(CacheError):
    """Payload bytes whose digest is not the digest the key names."""


class CacheKeyRejected(CacheError):
    """A key that is not an immutable generation identity."""


@dataclass(frozen=True)
class CacheKey:
    """One immutable cache identity: schema, profile, generation and shard digest.

    No field here is a pointer or a name that can change under a running lane. ``generation_id``
    and ``shard_sha256`` are the sha256 values the manifest closes over, so two reads derive the
    same key only when they would read the same bytes of the same generation.
    """

    schema: str
    profile: str
    generation_id: str
    shard_sha256: str
    path: str

    def __post_init__(self) -> None:
        for name in ("schema", "profile", "path"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise CacheKeyRejected(f"{name} must be a non-empty string")
        for name in ("generation_id", "shard_sha256"):
            value = getattr(self, name)
            if not manifest_module.is_sha256(value):
                raise CacheKeyRejected(f"{name} is not a lowercase sha256 digest: {value!r}")

    @property
    def identity(self) -> tuple[str, str, str, str]:
        """The pointer-free identity tuple ``(schema, profile, generation_id, shard_sha256)``."""
        return (self.schema, self.profile, self.generation_id, self.shard_sha256)

    def as_document(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "profile": self.profile,
            "generation_id": self.generation_id,
            "shard_sha256": self.shard_sha256,
            "path": self.path,
        }


def cache_key(
    *,
    generation_id: str,
    profile: str,
    path: str,
    sha256: str,
    schema: str = DEFAULT_SHARD_SCHEMA,
) -> CacheKey:
    """Build one key from an immutable generation and a manifest entry, never from a pointer."""
    return CacheKey(
        schema=schema,
        profile=profile,
        generation_id=generation_id,
        shard_sha256=sha256,
        path=path,
    )


def cache_key_for(
    shard: CopiedShard,
    *,
    generation_id: str,
    profile: str,
    schema: str = DEFAULT_SHARD_SCHEMA,
) -> CacheKey:
    """Build the key for a copied shard from its own manifest-declared identity."""
    return cache_key(
        generation_id=generation_id,
        profile=profile,
        path=shard.path,
        sha256=shard.sha256,
        schema=schema,
    )


@dataclass(frozen=True)
class CacheStats:
    """A snapshot of the counters, so a caller can prove the bounds and the hit behaviour."""

    hits: int
    misses: int
    evictions: int
    mismatches: int
    entries: int
    bytes_used: int


class SnapshotCache:
    """A bounded LRU of generation-pinned shard bytes, keyed by :class:`CacheKey`."""

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        for name, value in (("max_entries", max_entries), ("max_bytes", max_bytes)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise CacheError(f"{name} must be a positive integer, got {value!r}")
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._entries: OrderedDict[CacheKey, bytes] = OrderedDict()
        self._bytes_used = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._mismatches = 0

    @property
    def max_entries(self) -> int:
        return self._max_entries

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def entries(self) -> int:
        return len(self._entries)

    @property
    def bytes_used(self) -> int:
        return self._bytes_used

    def stats(self) -> CacheStats:
        return CacheStats(
            hits=self._hits,
            misses=self._misses,
            evictions=self._evictions,
            mismatches=self._mismatches,
            entries=len(self._entries),
            bytes_used=self._bytes_used,
        )

    def contains(self, key: CacheKey) -> bool:
        """True when a live entry names this exact immutable identity; never re-labels."""
        return key in self._entries

    def put(self, key: CacheKey, payload: bytes | bytearray) -> bool:
        """Store one generation-pinned payload. Refuses bytes that are not the keyed bytes.

        Returns ``False`` when the payload can never fit ``max_bytes`` and was therefore not
        stored; the existing entries are left untouched in that case. Raises
        :class:`CacheMismatch` when the payload's digest is not the key's ``shard_sha256``,
        because storing it would create exactly the mislabelled entry this cache forbids.
        """
        if not isinstance(key, CacheKey):
            raise CacheKeyRejected("a CacheKey is required to store a payload")
        data = bytes(payload)
        if hashlib.sha256(data).hexdigest() != key.shard_sha256:
            raise CacheMismatch(
                f"payload for {key.path!r} does not hash to the keyed digest "
                f"{key.shard_sha256[:12]}; refusing to store it under the wrong label"
            )
        if len(data) > self._max_bytes:
            return False
        previous = self._entries.pop(key, None)
        if previous is not None:
            self._bytes_used -= len(previous)
        self._entries[key] = data
        self._bytes_used += len(data)
        self._evict()
        return True

    def get(self, key: CacheKey) -> bytes | None:
        """Return the keyed bytes, or ``None`` on a miss.

        The stored bytes are re-hashed against the key before they are returned. An entry that
        no longer hashes to its label is dropped and reported as a miss, so no caller ever
        receives cached data under a generation label it does not actually match.
        """
        if not isinstance(key, CacheKey):
            raise CacheKeyRejected("a CacheKey is required to look a payload up")
        payload = self._entries.get(key)
        if payload is None:
            self._misses += 1
            return None
        if hashlib.sha256(payload).hexdigest() != key.shard_sha256:
            del self._entries[key]
            self._bytes_used -= len(payload)
            self._mismatches += 1
            self._misses += 1
            return None
        self._entries.move_to_end(key)
        self._hits += 1
        return payload

    def invalidate(self, key: CacheKey) -> bool:
        """Drop exactly one entry; returns whether it was present."""
        payload = self._entries.pop(key, None)
        if payload is None:
            return False
        self._bytes_used -= len(payload)
        return True

    def evict_generation(self, generation_id: str) -> int:
        """Drop every entry pinned to one generation and report how many were dropped."""
        doomed = [key for key in self._entries if key.generation_id == generation_id]
        for key in doomed:
            self.invalidate(key)
        return len(doomed)

    def clear(self) -> None:
        self._entries.clear()
        self._bytes_used = 0

    def _evict(self) -> None:
        while self._entries and (
            len(self._entries) > self._max_entries or self._bytes_used > self._max_bytes
        ):
            _key, payload = self._entries.popitem(last=False)
            self._bytes_used -= len(payload)
            self._evictions += 1


def lookup(
    cache: SnapshotCache,
    key: CacheKey,
    loader: Callable[[], bytes | bytearray],
) -> tuple[bytes, bool]:
    """Return ``(payload, from_cache)``, filling the cache from ``loader`` on a miss.

    The loader runs only on a miss and its result is stored under the same key it was loaded
    for, so a caller that has already pinned a generation never has to re-derive the label.
    """
    payload = cache.get(key)
    if payload is not None:
        return payload, True
    loaded = bytes(loader())
    cache.put(key, loaded)
    return loaded, False


__all__ = [
    "CacheError",
    "CacheKey",
    "CacheKeyRejected",
    "CacheMismatch",
    "CacheStats",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_SHARD_SCHEMA",
    "SnapshotCache",
    "cache_key",
    "cache_key_for",
    "lookup",
]
