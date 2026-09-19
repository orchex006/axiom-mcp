"""C-025: the byte cap counts the metadata, and an item that cannot fit says so.

AC1 has two halves:

* positive - the serialized document, including every metadata key, is measured in real UTF-8
  bytes and stays within the cap; items are trimmed from the tail and the accounting says how many
  and from where.
* boundary - a single item too large to fit is dropped with an explicit ``oversized_item`` reason,
  not partially serialised, and metadata that cannot fit at all raises ``BudgetExceeded`` rather
  than returning a document that breaks the contract.

The negative cases are a non-JSON-serialisable document and an out-of-range cap, both refused
explicitly. The Thai-text case is here because section 6 warns a token estimate is not a byte
measurement: the cap is checked on encoded bytes, not characters.
"""

from __future__ import annotations

import json

import pytest

from axiom_mcp.query.budget import (
    BudgetExceeded,
    BudgetInvalid,
    pack_response,
)
from axiom_mcp.query.model import LimitRejected

METADATA = {
    "schema_version": 1,
    "solution_id": "demo-solution",
    "catalog_generation_id": "a" * 64,
    "operation": "search",
    "truncated": False,
}


def encoded_size(document) -> int:
    return len(
        json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    )


def node(index: int, name_length: int = 20) -> dict:
    return {"id": f"{index:064d}", "name": "n" * name_length, "kind": "Method"}


def test_a_document_that_fits_is_returned_untouched() -> None:
    """AC1 positive: a small document is not trimmed and its size is reported honestly."""
    document = {**METADATA, "nodes": [node(1), node(2)], "edges": []}
    result = pack_response(document, max_bytes=4096)

    assert result.truncated is False
    assert result.dropped == {}
    assert result.document["nodes"] == document["nodes"]
    assert result.byte_size == encoded_size(result.document)
    assert result.byte_size <= 4096


def test_items_are_trimmed_until_the_metadata_and_the_rest_fit() -> None:
    """AC1 positive: trimming stops as soon as the whole document fits inside the cap."""
    document = {**METADATA, "nodes": [node(i) for i in range(200)], "edges": []}
    cap = 2048
    result = pack_response(document, max_bytes=cap)

    assert result.truncated is True
    assert result.dropped["nodes"] > 0
    assert len(result.document["nodes"]) == len(document["nodes"]) - result.dropped["nodes"]
    assert result.byte_size <= cap, "the hard cap includes the metadata"
    assert result.document["schema_version"] == 1
    assert result.document["catalog_generation_id"] == "a" * 64
    assert any(warning.startswith("max_bytes:") for warning in result.warnings)


def test_the_cap_is_measured_in_utf8_bytes_not_characters() -> None:
    """AC1: Thai text is multi-byte, so the cap must be checked on encoded bytes."""
    document = {**METADATA, "nodes": [{"id": "x", "name": "\u0e01\u0e02\u0e03\u0e04"}], "edges": []}
    result = pack_response(document, max_bytes=4096)
    serialised = json.dumps(
        result.document, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )

    assert result.byte_size == len(serialised.encode("utf-8"))
    assert result.byte_size > len(serialised), "the Thai characters are multi-byte in UTF-8"


def test_an_oversized_single_item_is_dropped_explicitly() -> None:
    """AC1 boundary: one item too large to fit is dropped by name, never half-written."""
    document = {**METADATA, "nodes": [node(1, name_length=4000)], "edges": []}
    result = pack_response(document, max_bytes=512)

    assert result.document["nodes"] == []
    assert result.truncated is True
    assert result.dropped == {"nodes": 1}
    assert any(warning.startswith("oversized_item:") for warning in result.warnings)
    assert result.byte_size <= 512


def test_metadata_that_cannot_fit_raises_rather_than_returning_over_cap() -> None:
    """AC1 boundary: packing cannot fix oversized metadata, so it is an explicit error."""
    document = {**METADATA, "solution_id": "s" * 5000, "nodes": [], "edges": []}
    with pytest.raises(BudgetExceeded):
        pack_response(document, max_bytes=512)


def test_a_non_json_document_is_refused() -> None:
    """Negative: an unmeasurable document is a validation error, not a silent pass."""
    with pytest.raises(BudgetInvalid):
        pack_response({**METADATA, "nodes": [object()]}, max_bytes=1024)


def test_an_out_of_range_cap_is_rejected_not_clamped() -> None:
    """Negative: a cap the caller did not choose is refused, never silently widened."""
    document = {**METADATA, "nodes": []}
    with pytest.raises(LimitRejected):
        pack_response(document, max_bytes=100)
    with pytest.raises(LimitRejected):
        pack_response(document, max_bytes=999_999)


def test_trimmable_collections_are_trimmed_in_order() -> None:
    """AC1: nodes are spent before edges, so less important facts go first."""
    document = {
        **METADATA,
        "nodes": [node(i) for i in range(200)],
        "edges": [node(i, name_length=40) for i in range(200)],
    }
    cap = 4096
    result = pack_response(document, max_bytes=cap)

    assert result.byte_size <= cap
    assert result.dropped.get("nodes", 0) > 0
    if result.document["nodes"]:
        assert not result.document["edges"], "edges are only kept once nodes are trimmed empty"
