"""C-025: pack a response inside a hard byte cap that counts the metadata too.

``repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md`` section 6 makes the byte cap a hard contract and
warns that a token estimate is not a byte measurement - a Thai string is not one byte per
character. So the cap here is measured on the *encoded* compact JSON, UTF-8, of the whole document
including every metadata key. There is no separate "payload" budget that metadata can quietly spend
from.

When the document does not fit, this packer trims whole items from the end of the trimmable
collections - ``nodes``, ``edges``, ``unresolved``, ``candidates``, then ``warnings`` - from least
to most important, and reports exactly what it dropped. Two cases are explicit rather than silent:

* if a collection held a single item that did not fit, that is recorded as ``oversized_item`` with
  the collection's name, because a caller must be able to tell a trimmed list from one that could
  never have contained the item;
* if the document still exceeds the cap with every trimmable collection empty, the metadata itself
  is oversized and :class:`BudgetExceeded` is raised. Packing cannot fix that by dropping facts,
  and returning an over-cap document would break the contract.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from axiom_mcp.query.model import (
    DEFAULT_MAX_BYTES,
    MAX_BYTES,
    MIN_BYTES,
    QueryModelError,
    bounded_int,
)

#: Collections trimmed, in order, when the document exceeds the cap.
TRIMMABLE = ("nodes", "edges", "unresolved", "candidates", "warnings")

STATUS_OK = "ok"


class BudgetInvalid(QueryModelError):
    """The document cannot be measured because it is not JSON-serialisable."""


class BudgetExceeded(QueryModelError):
    """The metadata alone exceeds the cap, so no trimming can produce a valid response."""


@dataclass(frozen=True)
class BudgetResult:
    """A document that fits the cap, plus the accounting of what it cost to get there."""

    document: Mapping[str, Any]
    byte_size: int
    max_bytes: int
    truncated: bool
    dropped: Mapping[str, int]
    warnings: tuple[str, ...]

    def as_document(self) -> dict[str, Any]:
        return dict(self.document)


def _encoded_size(document: Mapping[str, Any]) -> int:
    try:
        encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as error:
        raise BudgetInvalid(f"the document is not JSON-serialisable: {error}") from error
    return len(encoded.encode("utf-8"))


def _fits(document: Mapping[str, Any], max_bytes: int) -> bool:
    return _encoded_size(document) <= max_bytes


def pack_response(document: Mapping[str, Any], *, max_bytes: int | None = None) -> BudgetResult:
    """Return ``document`` packed inside ``max_bytes``, trimming items from the tail."""
    if not isinstance(document, Mapping):
        raise BudgetInvalid(f"a mapping document is required, got {document!r}")
    cap = bounded_int(
        max_bytes, "max_bytes", low=MIN_BYTES, high=MAX_BYTES, default=DEFAULT_MAX_BYTES
    )
    packed: dict[str, Any] = {key: value for key, value in document.items()}
    if _fits(packed, cap):
        return BudgetResult(
            document=packed,
            byte_size=_encoded_size(packed),
            max_bytes=cap,
            truncated=False,
            dropped={},
            warnings=(),
        )

    dropped: dict[str, int] = {}
    oversized: list[str] = []
    for key in TRIMMABLE:
        value = packed.get(key)
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            continue
        items = list(value)
        if not items:
            continue
        # Binary search the longest prefix that still fits with the later collections intact.
        low, high = 0, len(items)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = {**packed, key: items[:middle]}
            if _fits(candidate, cap):
                low = middle
            else:
                high = middle - 1
        if low < len(items):
            if low == 0 and len(items) == 1:
                oversized.append(key)
            dropped[key] = len(items) - low
            packed[key] = items[:low]

    if not _fits(packed, cap):
        raise BudgetExceeded(
            f"the document cannot fit max_bytes={cap} even with every trimmable collection "
            f"empty; its metadata alone needs more room"
        )

    warnings: list[str] = []
    trimmed = sorted(key for key in dropped)
    if trimmed:
        packed["truncated"] = True
        warnings.append(
            f"max_bytes: {sum(dropped.values())} item(s) dropped from "
            f"{', '.join(trimmed)} to stay inside {cap} bytes"
        )
    if oversized:
        warnings.append(
            f"oversized_item: a single item in {', '.join(sorted(oversized))} did not fit the "
            f"{cap}-byte cap and was dropped rather than partially serialised"
        )
    if warnings:
        existing = list(packed.get("warnings") or [])
        packed["warnings"] = existing + warnings
        if not _fits(packed, cap):
            # The warning text is itself part of the measured document; drop it rather than lie.
            packed["warnings"] = existing

    return BudgetResult(
        document=packed,
        byte_size=_encoded_size(packed),
        max_bytes=cap,
        truncated=bool(dropped),
        dropped=dropped,
        warnings=tuple(warnings),
    )


__all__ = [
    "STATUS_OK",
    "TRIMMABLE",
    "BudgetExceeded",
    "BudgetInvalid",
    "BudgetResult",
    "pack_response",
]
