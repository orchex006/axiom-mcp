"""C-026: a server-held opaque cursor bound to query, generation, scope, capability and expiry.

``repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md`` section 5 is precise about pagination: "cursor binds
catalog generation, query hash, authorized scope and expiry", and "reuse with another
scope/query/capability must fail". A cursor is a promise that the next page is the continuation of
*that* request against *that* generation - so this module does not return a bare offset. It stores
a binding server-side and hands the caller an opaque id that only means something to this store.

:meth:`CursorStore.resolve` re-checks every part of the binding on every use and rejects the cursor
with a named reason when one differs:

* ``unknown`` - the store has no such cursor, or it was issued by another store;
* ``expired`` - the cursor's ``expires_at`` has passed;
* ``generation`` - a different catalog generation than the one it was issued against. An immutable
  historical generation does not imply live freshness, so continuing a pinned walk against another
  generation would silently mix two graphs;
* ``scope`` - a different authorized project set. Authorization is part of the binding, not a
  parameter the caller may widen on the next page;
* ``query`` - a different operation or parameters;
* ``capability`` - a different capability than the one the cursor was issued for.

Re-resolving with the *same* binding is allowed: that is what paging is. Only a binding that
differs is refused, and it is refused with the field that differed so the caller can fix it.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from axiom_mcp.query.model import (
    QueryModelError,
    bounded_int,
    choice,
    identifier_text,
    sha256_text,
)

#: Capabilities a cursor may be issued for, matching the gateway's read-only query surface.
CAPABILITIES = ("read", "verify", "reconcile")

STATUS_OK = "ok"

DEFAULT_TTL_SECONDS = 300
MIN_TTL_SECONDS = 1
MAX_TTL_SECONDS = 3600


class CursorError(QueryModelError):
    """A cursor could not be used for the request it was presented with."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def query_hash(operation: str, query: Mapping[str, Any] | None) -> str:
    """A stable hash of the operation and its parameters, independent of key order."""
    payload = {
        "operation": identifier_text(operation, what="operation"),
        "query": dict(query or {}),
    }
    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as error:
        raise CursorError(
            "query", f"the query parameters are not JSON-serialisable: {error}"
        ) from error
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CursorRecord:
    """One issued cursor and the binding it is only valid for."""

    cursor_id: str
    catalog_generation_id: str
    query_hash: str
    operation: str
    scope: tuple[str, ...]
    capability: str
    offset: int
    issued_at: float
    expires_at: float

    def as_document(self) -> dict[str, Any]:
        return {
            "cursor": self.cursor_id,
            "catalog_generation_id": self.catalog_generation_id,
            "query_hash": self.query_hash,
            "operation": self.operation,
            "scope": list(self.scope),
            "capability": self.capability,
            "offset": self.offset,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }

    def expired(self, now: float) -> bool:
        return now > self.expires_at


class CursorStore:
    """A server-held set of cursors. The opaque id means nothing outside this instance."""

    def __init__(self) -> None:
        self._records: dict[str, CursorRecord] = {}
        self._sequence = 0

    def __len__(self) -> int:
        return len(self._records)

    def issue(
        self,
        *,
        catalog_generation_id: str,
        operation: str,
        query: Mapping[str, Any] | None = None,
        scope: Iterable[str] = (),
        capability: str = "read",
        offset: int | None = 0,
        ttl_seconds: int | None = None,
        now: float | None = None,
    ) -> CursorRecord:
        """Issue a cursor bound to exactly this generation, query, scope and capability."""
        generation = sha256_text(catalog_generation_id, what="catalog_generation_id")
        chosen_capability = choice(capability, "capability", CAPABILITIES, default="read")
        chosen_scope = tuple(sorted({identifier_text(pid, what="project_id") for pid in scope}))
        chosen_offset = bounded_int(offset, "offset", low=0, high=1_000_000, default=0)
        ttl = bounded_int(
            ttl_seconds,
            "ttl_seconds",
            low=MIN_TTL_SECONDS,
            high=MAX_TTL_SECONDS,
            default=DEFAULT_TTL_SECONDS,
        )
        issued_at = float(time.time() if now is None else now)
        self._sequence += 1
        token_source = json.dumps(
            {
                "generation": generation,
                "operation": identifier_text(operation, what="operation"),
                "query": dict(query or {}),
                "scope": chosen_scope,
                "capability": chosen_capability,
                "offset": chosen_offset,
                "sequence": self._sequence,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        cursor_id = hashlib.sha256(token_source.encode("utf-8")).hexdigest()
        record = CursorRecord(
            cursor_id=cursor_id,
            catalog_generation_id=generation,
            query_hash=query_hash(operation, query),
            operation=identifier_text(operation, what="operation"),
            scope=chosen_scope,
            capability=chosen_capability,
            offset=chosen_offset,
            issued_at=issued_at,
            expires_at=issued_at + ttl,
        )
        self._records[cursor_id] = record
        return record

    def resolve(
        self,
        cursor_id: str,
        *,
        catalog_generation_id: str,
        operation: str,
        query: Mapping[str, Any] | None = None,
        scope: Sequence[str] = (),
        capability: str = "read",
        now: float | None = None,
    ) -> CursorRecord:
        """Return the record when the cursor is being used for the request it was issued for."""
        token = sha256_text(cursor_id, what="cursor")
        record = self._records.get(token)
        if record is None:
            raise CursorError("unknown", "this store has no such cursor")
        moment = float(time.time() if now is None else now)
        if record.expired(moment):
            raise CursorError(
                "expired",
                f"the cursor expired at {record.expires_at} and was used at {moment}",
            )
        generation = sha256_text(catalog_generation_id, what="catalog_generation_id")
        if generation != record.catalog_generation_id:
            raise CursorError(
                "generation",
                "the cursor was issued against another catalog generation; a pinned walk cannot "
                "continue against a different one",
            )
        expected_scope = tuple(sorted({identifier_text(pid, what="project_id") for pid in scope}))
        if expected_scope != record.scope:
            raise CursorError(
                "scope",
                f"the cursor is bound to scope {list(record.scope)}, not {list(expected_scope)}; "
                "authorization is part of the binding",
            )
        if query_hash(operation, query) != record.query_hash:
            raise CursorError(
                "query",
                "the cursor was issued for a different operation or parameters",
            )
        chosen_capability = choice(capability, "capability", CAPABILITIES, default="read")
        if chosen_capability != record.capability:
            raise CursorError(
                "capability",
                f"the cursor requires capability {record.capability!r}, not {chosen_capability!r}",
            )
        return record


__all__ = [
    "CAPABILITIES",
    "DEFAULT_TTL_SECONDS",
    "MAX_TTL_SECONDS",
    "MIN_TTL_SECONDS",
    "STATUS_OK",
    "CursorError",
    "CursorRecord",
    "CursorStore",
    "query_hash",
]
