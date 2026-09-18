"""C-012: one pinned catalog vector per query, and no latest-per-project fallback.

The fixtures are the shipped ``examples/snapshots/.axiom/graph/demo-solution`` bundle, copied
byte for byte from ``axiom-specs``: a catalog generation and two project generations it pins.
Each vendored manifest must still hash to the directory name it sits in, so a fixture edit
that would silently change the pinned identity fails the test rather than the implementation.

The case that matters most is :func:`test_pinned_vector_ignores_a_newer_project_generation`:
the fixture is copied to a temporary directory, a *newer* valid generation is published into
one project lane and that lane's ``current.json`` is repointed at it. A reader that fell back
to the lane pointer would answer from the newer generation; the pinned vector must not move,
because SNP-03 requires one exact generation vector per query and the catalog - not the
project lane - is the read-model commit point.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from axiom_mcp import manifest as manifest_module
from axiom_mcp.catalog import (
    CATALOG_FILENAME,
    CatalogInvalid,
    CatalogLocation,
    CatalogMemberMissing,
    CatalogUnreadable,
    UnsupportedCatalogMajor,
    load_catalog,
    load_catalog_bytes,
    load_pinned_catalog_generation,
    load_solution_catalog,
    pin_catalog,
    project_member_opener,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SOLUTION = REPO_ROOT / "tests" / "fixtures" / "solution" / "demo-solution"

AUTH_API = "auth-api"
WEB_APP = "web-app"
AUTH_API_GENERATION = "7319237fdf1ce4ff27ea377c8bc3ae8cf1f115cffc71de369a373e1870ba9d1a"
WEB_APP_GENERATION = "f570a6d6c7ae1bcffcec7a9c8cb3248dbadab432a30aec09a3223c5c5784c73f"
CATALOG_GENERATION = "5b34dcfe81740645e0bf1dadf71c1ad7cbc642c0c36d35f9db5492efa853648d"
COVERAGE_LANE = "checkpoint"


def lane_root(base: Path, project_id: str) -> Path:
    return base / project_id / COVERAGE_LANE


def catalog_location(base: Path) -> CatalogLocation:
    return CatalogLocation(
        solution_id="demo-solution",
        lane=COVERAGE_LANE,
        root=lane_root(base, "_catalog"),
    )


def location_for(base: Path):
    from axiom_mcp.registry import SnapshotLocation

    def resolve(project_id: str) -> SnapshotLocation:
        return SnapshotLocation(
            solution_id="demo-solution",
            project_id=project_id,
            lane=COVERAGE_LANE,
            root=lane_root(base, project_id),
        )

    return resolve


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_vendored_fixture_bytes_are_pinned_to_their_identities() -> None:
    """Every vendored manifest hashes to the generation directory that holds it."""
    for project_id, generation_id in (
        (AUTH_API, AUTH_API_GENERATION),
        (WEB_APP, WEB_APP_GENERATION),
        ("_catalog", CATALOG_GENERATION),
    ):
        directory = lane_root(FIXTURE_SOLUTION, project_id) / "generations" / generation_id
        assert directory.name == sha256_of(directory / CATALOG_FILENAME)


def test_load_catalog_pins_every_member_across_the_shipped_bundle() -> None:
    catalog, directory = load_pinned_catalog_generation(catalog_location(FIXTURE_SOLUTION))
    assert catalog.solution_id == "demo-solution"
    assert catalog.analysis_profile == "default"
    assert catalog.coverage == "partial"
    assert catalog.generation_id == CATALOG_GENERATION
    assert directory.name == CATALOG_GENERATION
    assert [(m.project_id, m.generation_id) for m in catalog.vector()] == [
        (AUTH_API, AUTH_API_GENERATION),
        (WEB_APP, WEB_APP_GENERATION),
    ]
    assert catalog.member(WEB_APP).source_fingerprint == (
        "7bc3e8c73744a9a07e9293255241c29ddd857f872b391f5d7e29f238ab002c25"
    )


def test_pinned_vector_resolves_both_members_to_their_exact_generation() -> None:
    catalog = load_solution_catalog(catalog_location(FIXTURE_SOLUTION))
    vector = pin_catalog(catalog, open_member=project_member_opener(location_for(FIXTURE_SOLUTION)))
    assert vector.is_complete
    assert vector.effective_coverage == "partial"
    assert vector.generations() == (
        (AUTH_API, AUTH_API_GENERATION),
        (WEB_APP, WEB_APP_GENERATION),
    )
    for pin in vector.available:
        assert pin.manifest is not None
        assert pin.manifest.generation_id == pin.member.generation_id
        assert pin.manifest.source_fingerprint == pin.member.source_fingerprint
    vector.require_complete()


def publish_newer_generation(base: Path, project_id: str) -> str:
    """Publish a valid newer generation into a lane and repoint that lane's pointer at it."""
    lane = lane_root(base, project_id)
    pinned_id = AUTH_API_GENERATION if project_id == AUTH_API else WEB_APP_GENERATION
    pinned = lane / "generations" / pinned_id
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
    return newer


def test_pinned_vector_ignores_a_newer_project_generation(tmp_path: Path) -> None:
    """AC1: the catalog pins the vector; a newer project generation is not a fallback.

    This is the "latest-per-project fallback is prohibited" case. The lane pointer now names
    a newer, fully valid generation, and the answer must still be the catalog's generation -
    otherwise both the query's identity and SNP-03's single-vector rule would be broken.
    """
    base = tmp_path / "demo-solution"
    shutil.copytree(FIXTURE_SOLUTION, base)
    newer = publish_newer_generation(base, AUTH_API)
    assert newer != AUTH_API_GENERATION
    assert (
        manifest_module.load_pointer(lane_root(base, AUTH_API) / "current.json").generation_id
        == newer
    )

    catalog = load_solution_catalog(catalog_location(base))
    assert catalog.generation_id == CATALOG_GENERATION, "the catalog bytes must not move"
    vector = pin_catalog(catalog, open_member=project_member_opener(location_for(base)))
    assert vector.generations() == (
        (AUTH_API, AUTH_API_GENERATION),
        (WEB_APP, WEB_APP_GENERATION),
    )
    auth = next(pin for pin in vector.available if pin.member.project_id == AUTH_API)
    assert auth.manifest is not None
    assert auth.manifest.generator_version == "synthetic-fixture-v2-not-runtime"
    assert auth.generation_dir is not None
    assert auth.generation_dir.name == AUTH_API_GENERATION


def test_catalog_member_without_a_generation_is_refused() -> None:
    """Negative: a member with no pinned generation would have to be resolved by name."""
    document = json.loads(
        (
            lane_root(FIXTURE_SOLUTION, "_catalog")
            / "generations"
            / CATALOG_GENERATION
            / CATALOG_FILENAME
        ).read_text(encoding="utf-8")
    )
    del document["projects"][0]["generation_id"]
    with pytest.raises(CatalogInvalid) as info:
        load_catalog_bytes(manifest_module.canonical_bytes(document))
    assert "generation_id" in str(info.value)


def test_catalog_member_resolved_by_name_is_refused() -> None:
    """Negative: ``tools/catalog_contract.py`` prohibits name-only member resolution."""
    document = json.loads(
        (
            lane_root(FIXTURE_SOLUTION, "_catalog")
            / "generations"
            / CATALOG_GENERATION
            / CATALOG_FILENAME
        ).read_text(encoding="utf-8")
    )
    document["projects"][0]["name"] = AUTH_API
    with pytest.raises(CatalogInvalid) as info:
        load_catalog_bytes(manifest_module.canonical_bytes(document))
    assert "name-only" in str(info.value)


def test_duplicate_member_is_refused() -> None:
    document = json.loads(
        (
            lane_root(FIXTURE_SOLUTION, "_catalog")
            / "generations"
            / CATALOG_GENERATION
            / CATALOG_FILENAME
        ).read_text(encoding="utf-8")
    )
    document["projects"].append(dict(document["projects"][0]))
    with pytest.raises(CatalogInvalid) as info:
        load_catalog_bytes(manifest_module.canonical_bytes(document))
    assert "duplicate member" in str(info.value)


def test_corrupt_pinned_generation_is_partial_not_substituted(tmp_path: Path) -> None:
    """Negative: an altered generation is reported missing; nothing is read in its place."""
    base = tmp_path / "demo-solution"
    shutil.copytree(FIXTURE_SOLUTION, base)
    target = lane_root(base, AUTH_API) / "generations" / AUTH_API_GENERATION / CATALOG_FILENAME
    target.write_bytes(target.read_bytes().replace(b"auth-api", b"auth-apx"))

    catalog = load_solution_catalog(catalog_location(base))
    vector = pin_catalog(catalog, open_member=project_member_opener(location_for(base)))
    assert not vector.is_complete
    assert [pin.member.project_id for pin in vector.missing] == [AUTH_API]
    assert vector.effective_coverage == "partial", "a missing member must not look complete"
    assert vector.generations() == ((WEB_APP, WEB_APP_GENERATION),)
    assert vector.missing[0].reason is not None
    with pytest.raises(CatalogMemberMissing):
        vector.require_complete()


def test_unknown_catalog_major_and_non_canonical_bytes_are_refused() -> None:
    """Boundary: the major is reported before formatting, and bytes must be canonical."""
    pinned = (
        lane_root(FIXTURE_SOLUTION, "_catalog")
        / "generations"
        / CATALOG_GENERATION
        / CATALOG_FILENAME
    ).read_bytes()
    document = json.loads(pinned.decode("utf-8"))

    document["schema_version"] = 2
    with pytest.raises(UnsupportedCatalogMajor) as info:
        load_catalog_bytes(manifest_module.canonical_bytes(document))
    assert "not supported" in str(info.value)

    document["schema_version"] = 1
    pretty = json.dumps(document, indent=2).encode("utf-8")
    with pytest.raises(CatalogInvalid) as info:
        load_catalog_bytes(pretty)
    assert "canonical" in str(info.value)


def test_catalog_generation_directory_must_match_its_bytes(tmp_path: Path) -> None:
    """Boundary: a renamed generation directory is refused, not read as the pinned one."""
    base = tmp_path / "demo-solution"
    shutil.copytree(FIXTURE_SOLUTION, base)
    lane = lane_root(base, "_catalog")
    (lane / "generations" / ("0" * 64)).mkdir(parents=True)
    (lane / "generations" / ("0" * 64) / CATALOG_FILENAME).write_bytes(
        (lane / "generations" / CATALOG_GENERATION / CATALOG_FILENAME).read_bytes()
    )
    (lane / "current.json").write_bytes(
        manifest_module.canonical_bytes(
            {"schema_version": 1, "generation_id": "0" * 64, "manifest_sha256": "0" * 64}
        )
    )
    with pytest.raises(CatalogInvalid) as info:
        load_pinned_catalog_generation(catalog_location(base))
    assert "does not match" in str(info.value)


def test_load_catalog_reports_a_missing_catalog_pointer(tmp_path: Path) -> None:
    """Negative: an absent catalog is an unreadable catalog, not an empty one."""
    location = catalog_location(tmp_path / "absent-solution")
    with pytest.raises(CatalogUnreadable) as info:
        load_catalog(location.pointer)
    assert "unreadable" in str(info.value)
