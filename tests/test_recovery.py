"""C-017: missing, corrupt and archived snapshots are handled by policy, never by guessing.

The fixtures are the shipped ``demo-solution`` bundle: a catalog generation and the two project
generations it pins (``auth-api``, ``web-app``). Copying the bundle and removing or superseding a
member gives a real partial vector, a real absent generation and a real "lane moved on" state, so
the policy cases run against actual pinned bytes rather than stubs.

The cases that carry the task:

* :func:`test_a_missing_member_is_partial_or_an_error_by_policy` - the same partial vector is
  served as ``partial`` under ``allow_partial`` and refused as ``PROJECT_UNAVAILABLE`` with no
  data under ``require_complete``;
* :func:`test_snapshot_only_mode_never_fabricates_live_freshness` - snapshot-only and pinned reads
  report ``freshness=unknown`` and refuse a caller-supplied live reading;
* :func:`test_a_corrupt_or_unavailable_generation_is_retried_then_reported` - a bounded retry of
  the whole catalog, then ``SNAPSHOT_CORRUPT``/``SNAPSHOT_UNAVAILABLE`` with no partial answer;
* :func:`test_a_collected_generation_is_expired_and_never_answered_from_current` - a superseded
  pinned generation is ``SNAPSHOT_EXPIRED`` on the first attempt, with the newer lane data unused.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from axiom_mcp import manifest as manifest_module
from axiom_mcp.catalog import (
    CatalogInvalid,
    CatalogLocation,
    CatalogMemberMissing,
    CatalogVector,
    load_solution_catalog,
    pin_catalog,
    project_member_opener,
)
from axiom_mcp.errors import AxiomError
from axiom_mcp.manifest import DigestMismatch, ManifestUnreadable
from axiom_mcp.recovery import (
    ALLOW_PARTIAL,
    ALLOW_STALE,
    FAILED,
    PARTIAL,
    PINNED,
    PROJECT_UNAVAILABLE,
    REQUIRE_COMPLETE,
    SERVED,
    SNAPSHOT_CORRUPT,
    SNAPSHOT_EXPIRED,
    SNAPSHOT_ONLY,
    SNAPSHOT_UNAVAILABLE,
    GenerationCollected,
    RecoveryLimits,
    RecoveryRejected,
    SnapshotGone,
    canonical_code_for,
    classify_absence,
    read_with_recovery,
    require_generation,
)
from axiom_mcp.registry import SnapshotLocation
from axiom_mcp.shards import ShardTooLarge, ShardUnreadable

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SOLUTION = REPO_ROOT / "tests" / "fixtures" / "solution" / "demo-solution"

AUTH_API = "auth-api"
WEB_APP = "web-app"
AUTH_API_GENERATION = "7319237fdf1ce4ff27ea377c8bc3ae8cf1f115cffc71de369a373e1870ba9d1a"
CATALOG_FILENAME = "manifest.json"
COVERAGE_LANE = "checkpoint"


def lane_root(base: Path, project_id: str) -> Path:
    return base / project_id / COVERAGE_LANE


def location_for(base: Path):
    def resolve(project_id: str) -> SnapshotLocation:
        return SnapshotLocation(
            solution_id="demo-solution",
            project_id=project_id,
            lane=COVERAGE_LANE,
            root=lane_root(base, project_id),
        )

    return resolve


def pinned_vector(base: Path) -> CatalogVector:
    catalog = load_solution_catalog(
        CatalogLocation(
            solution_id="demo-solution", lane=COVERAGE_LANE, root=lane_root(base, "_catalog")
        )
    )
    return pin_catalog(catalog, open_member=project_member_opener(location_for(base)))


def bundle(tmp_path: Path) -> Path:
    base = tmp_path / "demo-solution"
    shutil.copytree(FIXTURE_SOLUTION, base)
    return base


def supersede_auth_api(base: Path) -> str:
    """Publish a valid newer auth-api generation, repoint the lane and collect the old one."""
    lane = lane_root(base, AUTH_API)
    pinned = lane / "generations" / AUTH_API_GENERATION
    document = json.loads((pinned / CATALOG_FILENAME).read_text(encoding="utf-8"))
    document["generator_version"] = "newer-fixture-generation"
    raw = manifest_module.canonical_bytes(document)
    newer = manifest_module.sha256_hex(raw)
    directory = lane / "generations" / newer
    directory.mkdir(parents=True)
    (directory / CATALOG_FILENAME).write_bytes(raw)
    for name in ("nodes", "edges"):
        shutil.copytree(pinned / name, directory / name)
    (lane / "current.json").write_bytes(
        manifest_module.canonical_bytes(
            {"schema_version": 1, "generation_id": newer, "manifest_sha256": newer}
        )
    )
    shutil.rmtree(pinned)
    return newer


def test_a_missing_member_is_partial_or_an_error_by_policy(tmp_path: Path) -> None:
    """AC1: the same partial vector is partial or refused, by the query's coverage policy."""
    base = bundle(tmp_path)
    shutil.rmtree(
        lane_root(base, WEB_APP)
        / "generations"
        / "f570a6d6c7ae1bcffcec7a9c8cb3248dbadab432a30aec09a3223c5c5784c73f"
    )
    vector = pinned_vector(base)
    assert not vector.is_complete
    assert [pin.member.project_id for pin in vector.missing] == [WEB_APP]

    partial = read_with_recovery(lambda: vector, policy=ALLOW_PARTIAL)
    assert partial.status == PARTIAL and partial.ok and partial.partial
    assert partial.coverage == "partial"
    assert partial.generations() == {AUTH_API: AUTH_API_GENERATION}
    assert partial.freshness == "unknown"
    assert partial.as_document()["status"] == "partial"
    assert any(WEB_APP in warning for warning in partial.warnings)

    refused = read_with_recovery(
        lambda: vector, policy=REQUIRE_COMPLETE, limits=RecoveryLimits(max_attempts=1)
    )
    assert refused.status == FAILED and not refused.ok
    assert refused.vector is None
    assert refused.generations() == {}, "a refused answer must carry no data at all"
    assert refused.error is not None and refused.error.code == PROJECT_UNAVAILABLE
    assert refused.as_document()["error"]["code"] == PROJECT_UNAVAILABLE
    assert "web-app" in refused.error.message


def test_require_complete_retries_the_whole_catalog_within_the_bound(tmp_path: Path) -> None:
    """Boundary: the retry bound is real, and a partial vector is never returned under it."""
    base = bundle(tmp_path)
    shutil.rmtree(
        lane_root(base, WEB_APP)
        / "generations"
        / "f570a6d6c7ae1bcffcec7a9c8cb3248dbadab432a30aec09a3223c5c5784c73f"
    )
    vector = pinned_vector(base)
    calls: list[int] = []

    def loader() -> CatalogVector:
        calls.append(1)
        return vector

    outcome = read_with_recovery(
        loader, policy=REQUIRE_COMPLETE, limits=RecoveryLimits(max_attempts=3)
    )
    assert len(calls) == 3 and outcome.attempts == 3, "the retry is bounded and counted"
    assert outcome.status == FAILED and outcome.vector is None


def test_snapshot_only_mode_never_fabricates_live_freshness(tmp_path: Path) -> None:
    """AC1: snapshot-only reports unknown freshness and refuses a fabricated live reading."""
    vector = pinned_vector(FIXTURE_SOLUTION)
    assert vector.is_complete

    snapshot_only = read_with_recovery(lambda: vector, mode=SNAPSHOT_ONLY)
    assert snapshot_only.status == SERVED
    assert snapshot_only.freshness == "unknown"
    assert snapshot_only.verification == "manifest_hash"
    assert snapshot_only.mode == SNAPSHOT_ONLY
    assert snapshot_only.generations() == {
        AUTH_API: AUTH_API_GENERATION,
        WEB_APP: "f570a6d6c7ae1bcffcec7a9c8cb3248dbadab432a30aec09a3223c5c5784c73f",
    }

    with pytest.raises(RecoveryRejected) as info:
        read_with_recovery(lambda: vector, mode=SNAPSHOT_ONLY, runtime_freshness="fresh")
    assert "cannot report a live freshness reading" in str(info.value)

    accepted = read_with_recovery(lambda: vector, mode=SNAPSHOT_ONLY, runtime_freshness="unknown")
    assert accepted.freshness == "unknown"

    with pytest.raises(RecoveryRejected) as pinned_info:
        read_with_recovery(lambda: vector, consistency=PINNED, runtime_freshness="fresh")
    assert "does not imply live source freshness" in str(pinned_info.value)

    live = read_with_recovery(lambda: vector, consistency=ALLOW_STALE, runtime_freshness="stale")
    assert live.freshness == "stale"


def test_a_corrupt_or_unavailable_generation_is_retried_then_reported() -> None:
    """AC1: bounded retry of the whole catalog, then a canonical failure with no partial data."""
    good = pinned_vector(FIXTURE_SOLUTION)
    attempts: list[str] = []

    def flaky() -> CatalogVector:
        attempts.append("try")
        if len(attempts) == 1:
            raise ManifestUnreadable("manifest is unreadable: PermissionError")
        return good

    recovered = read_with_recovery(flaky, limits=RecoveryLimits(max_attempts=2))
    assert recovered.status == SERVED and recovered.attempts == 2
    assert recovered.generations() == dict(good.generations())

    def corrupt() -> CatalogVector:
        raise CatalogInvalid("manifest.json is not in canonical form")

    refused = read_with_recovery(corrupt, limits=RecoveryLimits(max_attempts=2))
    assert refused.status == FAILED and refused.attempts == 2
    assert refused.error is not None and refused.error.code == SNAPSHOT_CORRUPT
    assert refused.vector is None and refused.generations() == {}

    def absent() -> CatalogVector:
        raise ManifestUnreadable("manifest is unreadable: FileNotFoundError")

    gone = read_with_recovery(absent, limits=RecoveryLimits(max_attempts=2))
    assert gone.error is not None and gone.error.code == SNAPSHOT_UNAVAILABLE
    assert gone.vector is None and gone.generations() == {}


def test_a_collected_generation_is_expired_and_never_answered_from_current(
    tmp_path: Path,
) -> None:
    """AC1/negative: a superseded pinned generation expires instead of reading current data."""
    base = bundle(tmp_path)
    newer = supersede_auth_api(base)
    lane = lane_root(base, AUTH_API)
    assert (lane / "generations" / newer / CATALOG_FILENAME).is_file()
    assert not (lane / "generations" / AUTH_API_GENERATION).exists()

    with pytest.raises(SnapshotGone) as info:
        require_generation(lane_resource(base, AUTH_API), AUTH_API_GENERATION)
    assert info.value.code == SNAPSHOT_EXPIRED
    assert classify_absence(lane_resource(base, AUTH_API), AUTH_API_GENERATION) == (
        SNAPSHOT_EXPIRED
    )

    calls: list[str] = []

    def collected() -> CatalogVector:
        calls.append("try")
        raise GenerationCollected(AUTH_API_GENERATION)

    expired = read_with_recovery(collected, limits=RecoveryLimits(max_attempts=3))
    assert len(calls) == 1, "an expired generation is not retried"
    assert expired.attempts == 1
    assert expired.status == FAILED
    assert expired.error is not None and expired.error.code == SNAPSHOT_EXPIRED
    assert expired.generations() == {}, "the newer lane generation is not substituted"


def lane_resource(base: Path, project_id: str) -> SnapshotLocation:
    return SnapshotLocation(
        solution_id="demo-solution",
        project_id=project_id,
        lane=COVERAGE_LANE,
        root=lane_root(base, project_id),
    )


def test_an_absent_generation_in_a_still_current_lane_is_unavailable(tmp_path: Path) -> None:
    """Boundary: absent-while-still-pinned is SNAPSHOT_UNAVAILABLE, not expired."""
    base = bundle(tmp_path)
    shutil.rmtree(lane_root(base, AUTH_API) / "generations" / AUTH_API_GENERATION)
    location = lane_resource(base, AUTH_API)
    assert classify_absence(location, AUTH_API_GENERATION) == SNAPSHOT_UNAVAILABLE
    with pytest.raises(SnapshotGone) as info:
        require_generation(location, AUTH_API_GENERATION)
    assert info.value.code == SNAPSHOT_UNAVAILABLE


def test_canonical_codes_are_mapped_from_the_real_error_types() -> None:
    """AC2: the code a caller sees is derived from the failure, not from a string search."""
    cases: tuple[tuple[BaseException, str], ...] = (
        (GenerationCollected(AUTH_API_GENERATION), SNAPSHOT_EXPIRED),
        (SnapshotGone(SNAPSHOT_EXPIRED, AUTH_API_GENERATION, "x"), SNAPSHOT_EXPIRED),
        (SnapshotGone(SNAPSHOT_UNAVAILABLE, AUTH_API_GENERATION, "x"), SNAPSHOT_UNAVAILABLE),
        (CatalogMemberMissing(WEB_APP, "absent"), PROJECT_UNAVAILABLE),
        (ManifestUnreadable("unreadable"), SNAPSHOT_UNAVAILABLE),
        (ShardUnreadable("unreadable"), SNAPSHOT_UNAVAILABLE),
        (OSError("sharing violation"), SNAPSHOT_UNAVAILABLE),
        (CatalogInvalid("bad bytes"), SNAPSHOT_CORRUPT),
        (DigestMismatch("wrong digest"), SNAPSHOT_CORRUPT),
        (ShardTooLarge("over budget"), SNAPSHOT_CORRUPT),
        (AxiomError(SNAPSHOT_EXPIRED, "gone"), SNAPSHOT_EXPIRED),
        (ValueError("unexpected"), "INTERNAL_ERROR"),
    )
    for exc, expected in cases:
        assert canonical_code_for(exc) == expected, (exc, expected)


def test_invalid_configuration_and_a_bad_loader_are_refused() -> None:
    """Boundary: the layer refuses policies, freshness values and loaders it cannot honour."""
    with pytest.raises(RecoveryRejected):
        RecoveryLimits(max_attempts=0)
    with pytest.raises(RecoveryRejected):
        RecoveryLimits(max_attempts=True)
    vector = pinned_vector(FIXTURE_SOLUTION)
    with pytest.raises(RecoveryRejected):
        read_with_recovery(lambda: vector, policy="whatever")
    with pytest.raises(RecoveryRejected):
        read_with_recovery(lambda: vector, mode="whatever")
    with pytest.raises(RecoveryRejected):
        read_with_recovery(lambda: vector, consistency="whatever")
    with pytest.raises(RecoveryRejected):
        read_with_recovery(lambda: vector, runtime_freshness="bogus")
    with pytest.raises(RecoveryRejected):
        read_with_recovery(lambda: vector, runtime_freshness="fresh-ish")
    with pytest.raises(RecoveryRejected):
        read_with_recovery("not callable")  # type: ignore[arg-type]
    with pytest.raises(RecoveryRejected):
        read_with_recovery(lambda: "not a vector")  # type: ignore[return-value]
