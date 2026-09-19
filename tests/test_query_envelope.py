"""C-027 regression tests: exact generations, and freshness that is never upgraded to fresh."""

from __future__ import annotations

import pytest

from axiom_mcp.query.envelope import (
    COVERAGE,
    ENVELOPE_KEYS,
    FRESHNESS,
    EnvelopeInvalid,
    Verification,
    aggregate_coverage,
    build_envelope,
    catalog_generation_id,
    resolve_freshness,
)
from axiom_mcp.query.model import GraphSet, graph_from_documents
from tests.test_query_support import generation_id, node_document

SOLUTION = "auth-suite"
FINGERPRINT = "a" * 64


def pinned(
    project_id: str = "auth-api",
    *,
    seed: str | None = None,
    coverage_status: str | None = "complete_for_profile",
) -> object:
    coverage = None
    if coverage_status is not None:
        coverage = {
            "status": coverage_status,
            "input_files": 1,
            "processed_files": 1,
            "unresolved_references": 0,
            "unsupported_patterns": [],
        }
    return graph_from_documents(
        project_id,
        generation_id(seed or project_id),
        nodes=[node_document("Login", project_id=project_id)],
        edges=[],
        coverage=coverage,
    )


def strong() -> Verification:
    return Verification(mode="inventory_hash", source_fingerprint=FINGERPRINT)


def test_a_response_reports_the_exact_pinned_generations() -> None:
    scope = GraphSet((pinned("auth-api"), pinned("billing-api")))
    document = build_envelope(
        solution_id=SOLUTION,
        scope=scope,
        freshness="fresh",
        verification=strong(),
    )
    assert document["schema_version"] == 1
    assert document["project_generations"] == [
        {"project_id": "auth-api", "generation_id": generation_id("auth-api")},
        {"project_id": "billing-api", "generation_id": generation_id("billing-api")},
    ]
    assert set(document) <= ENVELOPE_KEYS
    assert set(document["verification"]) == {"mode", "source_fingerprint"}


def test_the_catalog_generation_digest_tracks_the_member_generations() -> None:
    one = GraphSet((pinned("auth-api"), pinned("billing-api")))
    reordered = GraphSet((pinned("billing-api"), pinned("auth-api")))
    assert catalog_generation_id(one) == catalog_generation_id(reordered)
    moved = GraphSet((pinned("auth-api", seed="later"), pinned("billing-api")))
    assert catalog_generation_id(one) != catalog_generation_id(moved)


def test_freshness_and_coverage_are_separate_facts() -> None:
    stale_but_complete = build_envelope(
        solution_id=SOLUTION,
        scope=GraphSet((pinned("auth-api"),)),
        freshness="stale",
        verification=strong(),
    )
    assert stale_but_complete["freshness"] == "stale"
    assert stale_but_complete["coverage"] == "complete_for_profile"

    unknown_and_partial = build_envelope(
        solution_id=SOLUTION,
        scope=GraphSet((pinned("auth-api", coverage_status="partial"),)),
        freshness=None,
    )
    assert unknown_and_partial["freshness"] == "unknown"
    assert unknown_and_partial["coverage"] == "partial"
    assert unknown_and_partial["verification"] == {"mode": "none"}


def test_fresh_requires_a_recomputed_inventory_hash() -> None:
    document = build_envelope(
        solution_id=SOLUTION,
        scope=GraphSet((pinned(),)),
        freshness="fresh",
        verification=strong(),
    )
    assert document["freshness"] == "fresh"
    assert document["verification"]["source_fingerprint"] == FINGERPRINT


def test_an_absent_freshness_claim_stays_unknown() -> None:
    status, warnings = resolve_freshness(None, Verification())
    assert status == "unknown"
    assert status in FRESHNESS
    assert "unknown rather than fresh" in warnings[0]


def test_an_unrecognised_freshness_is_unknown_never_fresh() -> None:
    for reported in ("FRESH", "fresh ", "current", 7):
        status, warnings = resolve_freshness(reported, strong())  # type: ignore[arg-type]
        assert status == "unknown"
        assert "never read as fresh" in warnings[0]


def test_a_watcher_hint_cannot_upgrade_fresh() -> None:
    hint = Verification(mode="watcher_hint", verified_at="2026-09-19T08:00:00Z")
    status, warnings = resolve_freshness("fresh", hint)
    assert status == "unknown"
    assert "watcher_hint" in warnings[0]
    document = build_envelope(
        solution_id=SOLUTION,
        scope=GraphSet((pinned(),)),
        freshness="fresh",
        verification=hint,
    )
    assert document["freshness"] == "unknown"
    assert document["coverage"] == "complete_for_profile"
    assert any("watcher_hint" in warning for warning in document["warnings"])


def test_a_project_without_a_coverage_block_is_refused() -> None:
    scope = GraphSet((pinned("auth-api", coverage_status=None),))
    with pytest.raises(EnvelopeInvalid, match="no usable coverage status"):
        aggregate_coverage(scope)


def test_an_off_allowlist_coverage_status_is_refused() -> None:
    scope = GraphSet((pinned("auth-api", coverage_status="complete"),))
    with pytest.raises(EnvelopeInvalid, match="no usable coverage status"):
        aggregate_coverage(scope)
    assert "complete" not in COVERAGE


def test_a_verification_that_contradicts_its_mode_is_refused() -> None:
    with pytest.raises(EnvelopeInvalid, match="cannot carry"):
        Verification(mode="none", source_fingerprint=FINGERPRINT)
    with pytest.raises(EnvelopeInvalid, match="must carry the source_fingerprint"):
        Verification(mode="inventory_hash")
    with pytest.raises(EnvelopeInvalid, match="must carry the verified_at"):
        Verification(mode="watcher_hint")
    with pytest.raises(EnvelopeInvalid, match="64-character lowercase hex"):
        Verification(mode="inventory_hash", source_fingerprint="A" * 64)
    with pytest.raises(EnvelopeInvalid, match="mode must be one of"):
        Verification(mode="trust_me")


def test_a_missing_member_caps_coverage_at_partial_and_is_named() -> None:
    scope = GraphSet((pinned("auth-api"),), missing_projects=("billing-api",))
    document = build_envelope(
        solution_id=SOLUTION, scope=scope, freshness="fresh", verification=strong()
    )
    assert document["coverage"] == "partial"
    assert [item["project_id"] for item in document["project_generations"]] == ["auth-api"]
    assert any("billing-api" in warning for warning in document["warnings"])


def test_the_envelope_is_refused_rather_than_padded() -> None:
    scope = GraphSet((pinned(),))
    with pytest.raises(EnvelopeInvalid, match="solution_id"):
        build_envelope(
            solution_id="Auth Suite", scope=scope, freshness="fresh", verification=strong()
        )
    with pytest.raises(EnvelopeInvalid, match="non-empty string"):
        build_envelope(solution_id=SOLUTION, scope=scope, freshness="fresh", next_cursor="")
    with pytest.raises(EnvelopeInvalid, match="must be a mapping"):
        build_envelope(solution_id=SOLUTION, scope=scope, freshness="fresh", nodes=["Login"])
    with pytest.raises(EnvelopeInvalid, match="non-empty string"):
        build_envelope(solution_id=SOLUTION, scope=scope, freshness="fresh", warnings=[""])
