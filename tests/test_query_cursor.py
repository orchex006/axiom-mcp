"""C-026: a cursor is bound to its query, generation, scope, capability and expiry.

AC1 has two halves:

* positive - a cursor issued for one request resolves for that same request, carries its offset for
  paging, and reports the generation, scope and expiry it is bound to. Scope binding is
  order-insensitive and deduplicated, because authorization is about the set of projects.
* boundary - reuse against anything else is rejected with a named reason: another scope, another
  query or operation, another capability, another generation, an expiry that has passed, and a
  cursor this store never issued (including one from another store).

The negative cases are an out-of-range TTL and a malformed generation id, both refused explicitly
rather than defaulted.
"""

from __future__ import annotations

import pytest

from axiom_mcp.query.cursor import (
    CursorError,
    CursorStore,
    query_hash,
)
from axiom_mcp.query.model import LimitRejected, QueryModelError

GENERATION = "a" * 64
OTHER_GENERATION = "b" * 64


def issued(store: CursorStore, *, capability: str = "read", ttl: int = 300, now: float = 100.0):
    return store.issue(
        catalog_generation_id=GENERATION,
        operation="search",
        query={"query": "login", "limit": 20},
        scope=["auth-api", "web-app"],
        capability=capability,
        offset=20,
        ttl_seconds=ttl,
        now=now,
    )


def test_a_cursor_resolves_for_the_request_it_was_issued_for() -> None:
    """AC1 positive: the same binding resolves, carrying the offset for the next page."""
    store = CursorStore()
    record = issued(store)
    resolved = store.resolve(
        record.cursor_id,
        catalog_generation_id=GENERATION,
        operation="search",
        query={"limit": 20, "query": "login"},
        scope=["web-app", "auth-api", "auth-api"],
        capability="read",
        now=110.0,
    )

    assert resolved == record
    assert resolved.offset == 20
    assert resolved.expires_at == 100.0 + 300
    assert resolved.scope == ("auth-api", "web-app")


def test_scope_is_part_of_the_binding() -> None:
    """AC1 boundary: a cursor cannot be reused against another authorized project set."""
    store = CursorStore()
    record = issued(store)
    with pytest.raises(CursorError) as error:
        store.resolve(
            record.cursor_id,
            catalog_generation_id=GENERATION,
            operation="search",
            query={"query": "login", "limit": 20},
            scope=["auth-api"],
            now=110.0,
        )
    assert error.value.reason == "scope"


def test_query_and_operation_are_part_of_the_binding() -> None:
    """AC1 boundary: another query, or another operation, is the same rejection."""
    store = CursorStore()
    record = issued(store)
    with pytest.raises(CursorError) as different_query:
        store.resolve(
            record.cursor_id,
            catalog_generation_id=GENERATION,
            operation="search",
            query={"query": "logout", "limit": 20},
            scope=["auth-api", "web-app"],
            now=110.0,
        )
    assert different_query.value.reason == "query"

    with pytest.raises(CursorError) as different_operation:
        store.resolve(
            record.cursor_id,
            catalog_generation_id=GENERATION,
            operation="context",
            query={"query": "login", "limit": 20},
            scope=["auth-api", "web-app"],
            now=110.0,
        )
    assert different_operation.value.reason == "query"


def test_capability_is_part_of_the_binding() -> None:
    """AC1 boundary: a read cursor does not authorize a reconcile request."""
    store = CursorStore()
    record = issued(store)
    with pytest.raises(CursorError) as error:
        store.resolve(
            record.cursor_id,
            catalog_generation_id=GENERATION,
            operation="search",
            query={"query": "login", "limit": 20},
            scope=["auth-api", "web-app"],
            capability="reconcile",
            now=110.0,
        )
    assert error.value.reason == "capability"


def test_generation_is_part_of_the_binding() -> None:
    """AC1 boundary: a pinned walk does not continue against another generation."""
    store = CursorStore()
    record = issued(store)
    with pytest.raises(CursorError) as error:
        store.resolve(
            record.cursor_id,
            catalog_generation_id=OTHER_GENERATION,
            operation="search",
            query={"query": "login", "limit": 20},
            scope=["auth-api", "web-app"],
            now=110.0,
        )
    assert error.value.reason == "generation"


def test_an_expired_cursor_is_rejected() -> None:
    """AC1 boundary: expiry is enforced, and it is reported as its own reason."""
    store = CursorStore()
    record = issued(store, ttl=10, now=100.0)
    with pytest.raises(CursorError) as error:
        store.resolve(
            record.cursor_id,
            catalog_generation_id=GENERATION,
            operation="search",
            query={"query": "login", "limit": 20},
            scope=["auth-api", "web-app"],
            now=200.0,
        )
    assert error.value.reason == "expired"


def test_an_unknown_or_foreign_cursor_is_rejected() -> None:
    """Negative: an id this store never issued resolves nowhere, including another store's."""
    first = CursorStore()
    second = CursorStore()
    record = issued(first)
    with pytest.raises(CursorError) as foreign:
        second.resolve(
            record.cursor_id,
            catalog_generation_id=GENERATION,
            operation="search",
            query={"query": "login", "limit": 20},
            scope=["auth-api", "web-app"],
            now=110.0,
        )
    assert foreign.value.reason == "unknown"

    with pytest.raises(CursorError) as unknown:
        first.resolve(
            "0" * 64,
            catalog_generation_id=GENERATION,
            operation="search",
            query={"query": "login", "limit": 20},
            scope=["auth-api", "web-app"],
            now=110.0,
        )
    assert unknown.value.reason == "unknown"


def test_re_resolving_the_same_binding_is_paging_not_reuse() -> None:
    """Negative: the identical binding may be resolved again; only a different one is refused."""
    store = CursorStore()
    record = issued(store)
    arguments = {
        "catalog_generation_id": GENERATION,
        "operation": "search",
        "query": {"query": "login", "limit": 20},
        "scope": ["auth-api", "web-app"],
    }
    assert store.resolve(record.cursor_id, now=110.0, **arguments) == record
    assert store.resolve(record.cursor_id, now=120.0, **arguments) == record
    assert len(store) == 1


def test_two_issues_do_not_collide() -> None:
    """Boundary: two cursors for the same request are distinct ids, so one cannot page the other."""
    store = CursorStore()
    first = issued(store)
    second = issued(store)

    assert first.cursor_id != second.cursor_id
    assert len(store) == 2


def test_out_of_range_ttl_and_bad_generation_are_refused() -> None:
    """Negative: an unvalidated TTL or generation is refused, never defaulted silently."""
    store = CursorStore()
    with pytest.raises(LimitRejected):
        issued(store, ttl=0)
    with pytest.raises(LimitRejected):
        issued(store, ttl=10_000)
    with pytest.raises(QueryModelError):
        store.issue(catalog_generation_id="not-a-digest", operation="search")


def test_the_query_hash_is_key_order_independent() -> None:
    """AC1: the query binding is over values, so key order cannot fake a different request."""
    assert query_hash("search", {"a": 1, "b": 2}) == query_hash("search", {"b": 2, "a": 1})
    assert query_hash("search", {"a": 1}) != query_hash("search", {"a": 2})
