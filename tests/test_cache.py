"""C-016: the cache is keyed by immutable generation identity, and LRU bounds its memory.

The positive case caches the vendored ``auth-api`` shard bytes under the key derived from the
generation's own manifest, proves a hit returns exactly those bytes, and proves the same bytes
are a *miss* under a different ``generation_id`` - which is the invariant "pointer changes
cannot return cached data under the wrong generation label" stated as a test.

The negative and boundary cases are the ones that make the invariant enforced rather than
documented: ``put`` refuses bytes that do not hash to the keyed digest, ``get`` drops an entry
whose bytes no longer match its label, a payload larger than the whole budget is refused
without evicting anything, and ``max_entries``/``max_bytes`` actually evict in LRU order.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from axiom_mcp import manifest as manifest_module
from axiom_mcp.cache import (
    DEFAULT_SHARD_SCHEMA,
    CacheError,
    CacheKeyRejected,
    CacheMismatch,
    SnapshotCache,
    cache_key,
    cache_key_for,
    lookup,
)
from axiom_mcp.registry import SnapshotLocation
from axiom_mcp.shards import copy_shards

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_LANE = REPO_ROOT / "tests" / "fixtures" / "generation" / "auth-api"
AUTH_API_GENERATION = "7319237fdf1ce4ff27ea377c8bc3ae8cf1f115cffc71de369a373e1870ba9d1a"
OTHER_GENERATION = hashlib.sha256(b"a newer generation").hexdigest()
NODES = "nodes/000000.json"
COVERAGE = "coverage.json"


def lane(base: Path = FIXTURE_LANE) -> SnapshotLocation:
    return SnapshotLocation(
        solution_id="demo-solution", project_id="auth-api", lane="live", root=base
    )


def pinned_shards(base: Path = FIXTURE_LANE):
    """The generation's copied shards, exactly as a guard-held read would copy them."""
    location = lane(base)
    _, manifest, _ = manifest_module.load_generation(location.pointer)
    return manifest, copy_shards(location, manifest)


def keys_for(manifest, shards, *, generation_id: str):
    return {
        shard.path: cache_key_for(
            shard, generation_id=generation_id, profile=manifest.analysis_profile
        )
        for shard in shards
    }


def test_identity_is_the_pointer_free_generation_identity() -> None:
    """AC1: the key is (schema, profile, generation_id, shard_sha256), never a pointer."""
    manifest, shards = pinned_shards()
    node = next(shard for shard in shards if shard.path == NODES)
    key = cache_key_for(node, generation_id=manifest.generation_id, profile="full")
    assert key.identity == (DEFAULT_SHARD_SCHEMA, "full", manifest.generation_id, node.sha256)
    assert key.path == NODES
    assert key.as_document() == {
        "schema": DEFAULT_SHARD_SCHEMA,
        "profile": "full",
        "generation_id": manifest.generation_id,
        "shard_sha256": node.sha256,
        "path": NODES,
    }
    # A pointer-shaped or name-shaped value cannot be a digest, so it cannot become a key.
    with pytest.raises(CacheKeyRejected):
        cache_key(
            generation_id="live/auth-api",
            profile="full",
            path=NODES,
            sha256=node.sha256,
        )
    with pytest.raises(CacheKeyRejected):
        cache_key(
            generation_id=manifest.generation_id,
            profile="",
            path=NODES,
            sha256=node.sha256,
        )


def test_a_hit_returns_the_keyed_bytes_and_a_foreign_generation_misses() -> None:
    """AC1 positive: pinned bytes hit; the same bytes under another generation do not."""
    manifest, shards = pinned_shards()
    cache = SnapshotCache()
    here = keys_for(manifest, shards, generation_id=manifest.generation_id)
    there = keys_for(manifest, shards, generation_id=OTHER_GENERATION)
    by_path = {shard.path: shard for shard in shards}

    for path, key in here.items():
        assert cache.put(key, by_path[path].raw) is True
    for path, key in here.items():
        assert cache.get(key) == by_path[path].raw

    assert here[NODES] != there[NODES]
    assert here[NODES].identity != there[NODES].identity
    # Same bytes, same digest, different generation label: a miss, never a relabel.
    assert cache.get(there[NODES]) is None
    assert cache.bytes_used == sum(shard.bytes for shard in shards)
    stats = cache.stats()
    assert stats.hits == len(shards) and stats.misses == 1
    assert stats.mismatches == 0


def test_a_pointer_change_loads_the_new_generation_instead_of_the_cached_one() -> None:
    """AC1: a republished pointer derives a new key, so the old entry cannot be served."""
    manifest, shards = pinned_shards()
    node = next(shard for shard in shards if shard.path == NODES)
    cache = SnapshotCache()
    old_key = cache_key_for(node, generation_id=manifest.generation_id, profile="full")
    cache.put(old_key, node.raw)

    # The pointer now names generation B; the caller pins B before it builds any key.
    new_key = cache_key_for(node, generation_id=OTHER_GENERATION, profile="full")
    calls: list[str] = []

    def loader() -> bytes:
        calls.append(new_key.generation_id)
        return node.raw

    payload, from_cache = lookup(cache, new_key, loader)
    assert calls == [OTHER_GENERATION], "a changed generation must reload, not hit A"
    assert from_cache is False
    assert payload == node.raw
    assert cache.get(old_key) == node.raw, "the old generation stays addressable by its own key"
    assert cache.get(new_key) == node.raw
    assert cache.bytes_used == 2 * len(node.raw)


def test_put_refuses_bytes_that_are_not_the_keyed_bytes() -> None:
    """Negative: an entry can never be created under a label its bytes do not hash to."""
    manifest, shards = pinned_shards()
    node = next(shard for shard in shards if shard.path == NODES)
    cache = SnapshotCache()
    key = cache_key_for(node, generation_id=manifest.generation_id, profile="full")
    with pytest.raises(CacheMismatch) as info:
        cache.put(key, b"not the published shard bytes")
    assert "refusing to store it under the wrong label" in str(info.value)
    assert cache.entries == 0 and cache.bytes_used == 0
    assert cache.stats().mismatches == 0


def test_get_drops_an_entry_whose_bytes_no_longer_match_the_label() -> None:
    """Negative: in-process tampering is a miss, not a mislabelled answer."""
    manifest, shards = pinned_shards()
    node = next(shard for shard in shards if shard.path == NODES)
    cache = SnapshotCache()
    key = cache_key_for(node, generation_id=manifest.generation_id, profile="full")
    cache.put(key, node.raw)
    # Simulate corruption between put and get, which no public API can cause on purpose.
    # Same length, so the cache's byte accounting stays exactly as the public API left it.
    cache._entries[key] = b"\x00" * len(node.raw)
    assert cache.get(key) is None
    assert cache.contains(key) is False
    assert cache.stats().mismatches == 1 and cache.bytes_used == 0


def test_lru_limits_bound_entries_and_bytes() -> None:
    """AC1 boundary: both LRU limits hold, and the least recently used entry goes first."""
    manifest, shards = pinned_shards()
    by_path = {shard.path: shard for shard in shards}
    here = keys_for(manifest, shards, generation_id=manifest.generation_id)

    bounded = SnapshotCache(max_entries=2, max_bytes=10_000)
    for path in (COVERAGE, NODES, "edges/000000.json"):
        bounded.put(here[path], by_path[path].raw)
    assert bounded.entries == 2 and bounded.bytes_used <= bounded.max_bytes
    assert bounded.get(here[COVERAGE]) is None, "the first inserted entry is evicted first"
    assert bounded.get(here[NODES]) is not None

    biggest = max(shard.bytes for shard in shards)
    tight = SnapshotCache(max_bytes=biggest)
    for path in (COVERAGE, "edges/000000.json", NODES):
        tight.put(here[path], by_path[path].raw)
    assert tight.bytes_used <= biggest and tight.entries == 1
    assert tight.stats().evictions >= 2


def test_a_payload_larger_than_the_budget_does_not_evict_anything() -> None:
    """Boundary: a payload that can never fit is refused, and the live entries survive."""
    manifest, shards = pinned_shards()
    node = next(shard for shard in shards if shard.path == NODES)
    cache = SnapshotCache(max_bytes=64)
    small = cache_key(
        generation_id=manifest.generation_id,
        profile="full",
        path="coverage.json",
        sha256=hashlib.sha256(b"tiny").hexdigest(),
    )
    cache.put(small, b"tiny")
    assert cache.entries == 1 and cache.bytes_used == 4

    big_key = cache_key_for(node, generation_id=manifest.generation_id, profile="full")
    assert cache.put(big_key, node.raw) is False
    assert cache.entries == 1 and cache.bytes_used == 4
    assert cache.stats().evictions == 0 and cache.get(small) == b"tiny"


def test_evict_generation_drops_only_the_named_generation() -> None:
    """Boundary: invalidation is by immutable generation, not by lane or name."""
    manifest, shards = pinned_shards()
    node = next(shard for shard in shards if shard.path == NODES)
    cache = SnapshotCache()
    old_key = cache_key_for(node, generation_id=manifest.generation_id, profile="full")
    new_key = cache_key_for(node, generation_id=OTHER_GENERATION, profile="full")
    cache.put(old_key, node.raw)
    cache.put(new_key, node.raw)

    assert cache.evict_generation(manifest.generation_id) == 1
    assert cache.get(old_key) is None
    assert cache.get(new_key) == node.raw
    assert cache.evict_generation("no-such-generation") == 0


def test_invalid_configuration_and_keys_are_refused() -> None:
    """Boundary: the cache refuses limits and keys it cannot honour."""
    with pytest.raises(CacheError):
        SnapshotCache(max_entries=0)
    with pytest.raises(CacheError):
        SnapshotCache(max_bytes=True)
    with pytest.raises(CacheError):
        SnapshotCache(max_bytes=-1)
    cache = SnapshotCache()
    with pytest.raises(CacheKeyRejected):
        cache.get("not a key")  # type: ignore[arg-type]
    with pytest.raises(CacheKeyRejected):
        cache.put("not a key", b"x")  # type: ignore[arg-type]
