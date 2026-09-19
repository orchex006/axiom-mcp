"""C-014: bounded shard loading, with the cap and the path allowlist enforced before parsing.

The fixtures are the vendored ``auth-api`` live lane: a valid ``current.json`` pointing at one
generation whose manifest pins ``nodes/000000.json``, ``edges/000000.json`` and
``coverage.json``. Every test that mutates the lane copies it first, so the vendored bytes stay
pinned to the generation directory that holds them.

The load-bearing cases are the negative ones, because each of them would otherwise be a way to
read something the protocol forbids:

* :func:`test_symlinked_shard_is_rejected_before_it_is_read` plants a symlink at the leaf of a
  shard path that points outside the lane - the classic escape the protocol's "no symlink
  escape" rule names;
* :func:`test_symlinked_parent_outside_the_lane_is_rejected_before_it_is_read` and
  :func:`test_symlinked_parent_inside_the_lane_is_rejected_before_it_is_read` plant the symlink
  one level higher, where the leaf looks like a regular file and only the resolved path
  reveals the escape - once past the lane root, once merely outside the generation directory;
* :func:`test_oversize_declared_shard_is_rejected_before_any_read` proves the *order* - the
  declared size is checked before the file is touched, by making the file something a read
  could never succeed on;
* :func:`test_plan_budget_is_enforced_before_the_first_read` passes a reader that fails if it
  is ever called, so a plan-level refusal cannot be mistaken for a late failure;
* :func:`test_oversized_unparsable_shard_is_refused_as_oversize_not_as_json` shows the cap
  fires on a file whose bytes are *not* valid JSON, which is what "before parsing" means.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from axiom_mcp import manifest as manifest_module
from axiom_mcp.manifest import ManifestEntry
from axiom_mcp.registry import SnapshotLocation
from axiom_mcp.shards import (
    COVERAGE_PATH,
    ROLE_DIRECTORIES,
    ShardError,
    ShardLimits,
    ShardPathRejected,
    ShardSizeMismatch,
    ShardTooLarge,
    copy_shards,
    load_shard,
    load_shards,
    read_bounded,
    role_permitted_path,
    shard_path,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_LANE = REPO_ROOT / "tests" / "fixtures" / "generation" / "auth-api"
AUTH_API_GENERATION = "7319237fdf1ce4ff27ea377c8bc3ae8cf1f115cffc71de369a373e1870ba9d1a"
NODES = "nodes/000000.json"
EDGES = "edges/000000.json"
COVERAGE_BYTES = 173
DECLARED_TOTAL_BYTES = COVERAGE_BYTES + 3 + 340


def lane(base: Path) -> SnapshotLocation:
    return SnapshotLocation(
        solution_id="demo-solution", project_id="auth-api", lane="live", root=base
    )


def copied_lane(tmp_path: Path, name: str = "auth-api") -> Path:
    base = tmp_path / name
    shutil.copytree(FIXTURE_LANE, base)
    return base


def generation_dir(base: Path) -> Path:
    return base / "generations" / AUTH_API_GENERATION


def loaded_manifest(base: Path):
    location = lane(base)
    _, manifest, _ = manifest_module.load_generation(location.pointer)
    return location, manifest


def never_read(_path: Path, _limit: int) -> bytes:
    raise AssertionError("the reader was called although the plan should have been refused")


def symlinked_parent(base: Path, target: Path) -> None:
    real_dir = generation_dir(base) / "nodes"
    shutil.copytree(real_dir, target)
    shutil.rmtree(real_dir)
    os.symlink(target, real_dir, target_is_directory=True)


def test_required_shards_load_within_the_declared_bounds() -> None:
    """Positive: only the requested roles are read, verified and parsed."""
    location, manifest = loaded_manifest(FIXTURE_LANE)
    calls: list[tuple[str, int]] = []

    def counting_read(path: Path, limit: int) -> bytes:
        calls.append((path.name, limit))
        return read_bounded(path, limit)

    shards = load_shards(location, manifest, roles=("nodes", "coverage"), read=counting_read)
    assert [item.path for item in shards] == [COVERAGE_PATH, NODES]
    assert [item.role for item in shards] == ["coverage", "nodes"]
    assert len(calls) == 2, "one bounded read per required shard, and no read of the others"
    assert all(limit == manifest_module.MAX_SHARD_BYTES for _, limit in calls)
    assert shards[1].records == 1 and isinstance(shards[1].document, list)
    assert shards[1].sha256 == manifest.entry(NODES).sha256

    everything = load_shards(location, manifest)
    assert [item.path for item in everything] == [COVERAGE_PATH, EDGES, NODES]
    assert sum(item.bytes for item in everything) == manifest.total_bytes
    assert manifest.total_bytes == DECLARED_TOTAL_BYTES


def test_shard_path_stays_inside_its_generation_directory() -> None:
    """Positive: a manifest path resolves to a regular file inside its own generation."""
    location, manifest = loaded_manifest(FIXTURE_LANE)
    resolved = shard_path(location, manifest, manifest.entry(NODES))
    assert resolved == generation_dir(FIXTURE_LANE) / "nodes" / "000000.json"
    assert resolved.is_file()
    assert role_permitted_path("nodes", NODES)
    assert role_permitted_path("coverage", COVERAGE_PATH)
    assert not role_permitted_path("nodes", EDGES)
    assert not role_permitted_path("nodes", "nodes/nested/000000.json")
    assert not role_permitted_path("coverage", "coverage/000000.json")
    assert set(ROLE_DIRECTORIES) <= set(manifest_module.FILE_ROLES)


def test_symlinked_shard_is_rejected_before_it_is_read(tmp_path: Path) -> None:
    """Negative: a symlink at a shard path that points outside the lane is refused."""
    base = copied_lane(tmp_path)
    location, manifest = loaded_manifest(base)
    target = generation_dir(base) / NODES
    outside = tmp_path / "outside.json"
    outside.write_bytes(target.read_bytes())
    target.unlink()
    os.symlink(outside, target)

    with pytest.raises(ShardPathRejected) as info:
        load_shard(location, manifest, manifest.entry(NODES), read=never_read)
    assert "symbolic link" in str(info.value)
    assert outside.is_file(), "the escape target is untouched; the read never happened"


def test_symlinked_parent_outside_the_lane_is_rejected_before_it_is_read(tmp_path: Path) -> None:
    """Negative: a symlinked parent leaves the lane although the leaf is a regular file."""
    base = copied_lane(tmp_path)
    location, manifest = loaded_manifest(base)
    symlinked_parent(base, tmp_path / "escaped-nodes")
    assert (generation_dir(base) / NODES).is_file(), "the leaf itself is a regular file"

    with pytest.raises(ShardPathRejected) as info:
        load_shard(location, manifest, manifest.entry(NODES), read=never_read)
    assert "escapes the registered lane" in str(info.value)


def test_symlinked_parent_inside_the_lane_is_rejected_before_it_is_read(tmp_path: Path) -> None:
    """Negative: a parent pointing inside the lane but outside the generation is refused."""
    base = copied_lane(tmp_path, name="auth-api-borrowed")
    location, manifest = loaded_manifest(base)
    symlinked_parent(base, base / "borrowed")
    assert (generation_dir(base) / NODES).is_file(), "the leaf is still a regular file"

    with pytest.raises(ShardPathRejected) as info:
        load_shard(location, manifest, manifest.entry(NODES), read=never_read)
    assert "outside its generation directory" in str(info.value)


def test_oversize_declared_shard_is_rejected_before_any_read(tmp_path: Path) -> None:
    """Boundary: the declared size is checked before the file is opened."""
    base = copied_lane(tmp_path)
    location, manifest = loaded_manifest(base)
    coverage = generation_dir(base) / COVERAGE_PATH
    coverage.unlink()
    coverage.mkdir()

    with pytest.raises(ShardTooLarge) as info:
        load_shard(
            location,
            manifest,
            manifest.entry(COVERAGE_PATH),
            limits=ShardLimits(max_shard_bytes=COVERAGE_BYTES - 1),
            read=never_read,
        )
    assert f"declares {COVERAGE_BYTES} bytes, above the hard cap {COVERAGE_BYTES - 1}" in str(
        info.value
    )

    exact = load_shards(location, manifest, roles=("edges",), limits=ShardLimits(max_shard_bytes=3))
    assert len(exact) == 1, "a shard exactly at the cap is still loadable"


def test_oversized_unparsable_shard_is_refused_as_oversize_not_as_json(tmp_path: Path) -> None:
    """Negative: a shard that grew on disk is refused by the cap, before any parse."""
    base = copied_lane(tmp_path)
    location, manifest = loaded_manifest(base)
    (generation_dir(base) / NODES).write_bytes(b"{" * 4096)

    with pytest.raises(ShardTooLarge) as info:
        load_shard(
            location,
            manifest,
            manifest.entry(NODES),
            limits=ShardLimits(max_shard_bytes=512),
        )
    assert "larger than the hard cap 512" in str(info.value)


def test_plan_budget_is_enforced_before_the_first_read(tmp_path: Path) -> None:
    """Boundary: a plan whose declared bytes exceed the budget never touches the disk."""
    base = copied_lane(tmp_path)
    location, manifest = loaded_manifest(base)

    with pytest.raises(ShardTooLarge) as info:
        copy_shards(
            location,
            manifest,
            limits=ShardLimits(max_shard_bytes=340, max_total_bytes=400),
            read=never_read,
        )
    assert "above the plan budget 400" in str(info.value)

    with pytest.raises(ShardTooLarge) as info:
        copy_shards(location, manifest, limits=ShardLimits(max_shards=1), read=never_read)
    assert "above the hard cap 1" in str(info.value)

    with pytest.raises(ShardError) as info:
        copy_shards(location, manifest, roles=("pictures",), read=never_read)
    assert "unknown shard role" in str(info.value)


def test_forged_manifest_entry_cannot_escape_the_lane(tmp_path: Path) -> None:
    """Negative: a manifest entry that is not a permitted role/path pair is refused."""
    base = copied_lane(tmp_path)
    location, manifest = loaded_manifest(base)
    (tmp_path / "outside.json").write_bytes(json.dumps([]).encode("utf-8"))

    forged = ManifestEntry(
        path="../outside.json", role="nodes", sha256="0" * 64, bytes=2, records=0
    )
    with pytest.raises(ShardPathRejected) as info:
        load_shard(location, manifest, forged, read=never_read)
    assert "not a permitted path" in str(info.value)

    mismatched = ManifestEntry(path=EDGES, role="nodes", sha256="0" * 64, bytes=3, records=0)
    with pytest.raises(ShardPathRejected) as info:
        load_shard(location, manifest, mismatched, read=never_read)
    assert "not a permitted path" in str(info.value)

    unknown_role = ManifestEntry(path=NODES, role="pictures", sha256="0" * 64, bytes=340, records=1)
    with pytest.raises(ShardPathRejected) as info:
        load_shard(location, manifest, unknown_role, read=never_read)
    assert "is not a role of" in str(info.value)


def test_substituted_shard_bytes_are_refused_by_digest(tmp_path: Path) -> None:
    """Negative: same size, different bytes is a digest failure on the copied bytes."""
    base = copied_lane(tmp_path)
    location, manifest = loaded_manifest(base)
    target = generation_dir(base) / NODES
    target.write_bytes(target.read_bytes().replace(b"auth-api", b"auth-apx"))

    with pytest.raises(manifest_module.DigestMismatch):
        load_shard(location, manifest, manifest.entry(NODES))

    with pytest.raises(ShardSizeMismatch) as info:
        load_shard(location, manifest, manifest.entry(EDGES), read=lambda path, limit: b"")
    assert "0 bytes on disk" in str(info.value)


def test_shard_limits_refuse_a_cap_above_the_canonical_hard_cap() -> None:
    """Boundary: a local limit may tighten the contract cap, never widen it."""
    with pytest.raises(ShardError) as info:
        ShardLimits(max_shard_bytes=manifest_module.MAX_SHARD_BYTES + 1)
    assert "exceeds the canonical hard cap" in str(info.value)

    with pytest.raises(ShardError) as info:
        ShardLimits(max_shard_bytes=1024, max_total_bytes=512)
    assert "must not be smaller" in str(info.value)

    with pytest.raises(ShardError):
        ShardLimits(max_shards=0)
