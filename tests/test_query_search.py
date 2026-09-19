"""C-018: indexed symbol search returns stable identities and locations, and never a body.

AC1 has two halves, and both are asserted here rather than assumed:

* positive - an exact qualified-name query is answered from the index with the pinned node id and
  its portable source span, and repeating the query against the same scope returns the same
  candidate in the same order, while an ambiguous *name* returns every candidate instead of one
  silent pick;
* negative - the answer carries no source body. The check is structural, not textual: the
  candidate document has exactly the contract fields, the model has no field that could hold a
  body, and a search whose query matches nothing still answers with an explicit
  ``no_symbol_matched`` warning rather than an empty success.

The boundary cases are the caps: ``limit`` truncates with ``truncated`` and a warning, and a bound
outside the canonical request range is rejected instead of clamped.
"""

from __future__ import annotations

import json

import pytest
from test_query_support import generation_id, node_document

from axiom_mcp.query.model import (
    GraphSet,
    LimitRejected,
    graph_from_documents,
)
from axiom_mcp.query.search import (
    MATCH_EXACT_NAME,
    MATCH_EXACT_QUALIFIED_NAME,
    MATCH_ID,
    MATCH_PREFIX,
    build_symbol_index,
    search_symbols,
)

FORBIDDEN_KEYS = ("body", "content", "text", "source_text", "source_body", "embedding", "snippet")


def scope() -> GraphSet:
    """A three-symbol project plus a same-named method in a second project."""
    login = node_document(
        "LoginAsync",
        qualified_name="Auth.Api.AuthController.LoginAsync",
        start_line=10,
        end_line=22,
    )
    logout = node_document(
        "LogoutAsync",
        qualified_name="Auth.Api.AuthController.LogoutAsync",
        start_line=24,
        end_line=30,
    )
    handler = node_document(
        "LoginAsync",
        project_id="web-app",
        qualified_name="WebApp.Handlers.LoginAsync",
        language="typescript",
        file="src/handlers/login.ts",
        start_line=5,
        end_line=9,
    )
    auth = graph_from_documents(
        "auth-api", generation_id("c018-auth"), nodes=(login, logout), edges=()
    )
    web = graph_from_documents("web-app", generation_id("c018-web"), nodes=(handler,), edges=())
    return GraphSet((auth, web))


def test_exact_qualified_name_search_is_indexed_and_stable() -> None:
    """AC1 positive: the pinned id and source span come back, and the order is stable."""
    pinned = scope()
    first = search_symbols(pinned, "Auth.Api.AuthController.LoginAsync")
    second = search_symbols(pinned, "Auth.Api.AuthController.LoginAsync")

    assert [item.match for item in first.candidates] == [MATCH_EXACT_QUALIFIED_NAME]
    candidate = first.candidates[0]
    assert (
        candidate.node_id
        == node_document("LoginAsync", qualified_name="Auth.Api.AuthController.LoginAsync")["id"]
    )
    assert candidate.project_id == "auth-api"
    assert candidate.source.as_document() == {
        "file": "src/Auth.Api/AuthController.cs",
        "start_line": 10,
        "end_line": 22,
    }
    assert [item.node_id for item in first.candidates] == [
        item.node_id for item in second.candidates
    ]
    assert first.as_document() == second.as_document()


def test_the_index_answers_id_name_and_qualified_name_without_a_scan() -> None:
    """AC1: the exact cases are index lookups, so no bounded scan is consumed by them."""
    index = build_symbol_index(scope())
    login = index.exact("Auth.Api.AuthController.LoginAsync")
    assert [match for _, match in login] == [MATCH_EXACT_QUALIFIED_NAME]

    by_id = index.exact(login[0][0])
    assert by_id[0][1] == MATCH_ID

    by_name = index.exact("LogoutAsync")
    assert [match for _, match in by_name] == [MATCH_EXACT_NAME]

    filled = search_symbols(scope(), "LogoutAsync", limit=1)
    assert filled.scanned == 0, "an exact answer that fills the limit needs no node scan"
    assert filled.candidates[0].match == MATCH_EXACT_NAME

    widened = search_symbols(scope(), "LogoutAsync")
    assert widened.scanned > 0, "a query that is not yet full widens through the bounded scan"


def test_an_ambiguous_name_returns_candidates_instead_of_one_pick() -> None:
    """AC1: two pinned symbols share a name, so both come back and the answer says so."""
    result = search_symbols(scope(), "LoginAsync")
    assert result.ambiguous is True
    assert {item.project_id for item in result.candidates} == {"auth-api", "web-app"}
    assert all(item.match == MATCH_EXACT_NAME for item in result.candidates)
    assert any(warning.startswith("ambiguous:") for warning in result.warnings)
    assert [item.qualified_name for item in result.candidates] == sorted(
        item.qualified_name for item in result.candidates
    )


def test_the_answer_has_no_source_body_slot() -> None:
    """AC1 negative: the candidate document is the contract fields, and nothing else."""
    result = search_symbols(scope(), "LoginAsync")
    document = result.as_document()
    assert set(document["candidates"][0]) == {
        "id",
        "project_id",
        "kind",
        "name",
        "qualified_name",
        "language",
        "source",
        "identity_quality",
        "match",
    }
    payload = json.dumps(document)
    for key in FORBIDDEN_KEYS:
        assert f'"{key}"' not in payload, key
    assert "class AuthController" not in payload


def test_a_prefix_query_scans_only_within_the_node_bound() -> None:
    """Boundary: a weaker query pays for a bounded scan and reports how much it scanned."""
    result = search_symbols(scope(), "Auth.Api.Auth", max_nodes=2)
    assert {item.match for item in result.candidates} == {MATCH_PREFIX}
    assert result.scanned <= 2
    assert result.pinned_nodes == 3


def test_a_limit_truncates_explicitly_and_an_empty_match_is_stated() -> None:
    """Negative and boundary: truncation is reported, and no match is not a silent success."""
    capped = search_symbols(scope(), "Auth.Api.Auth", limit=1)
    assert capped.truncated is True
    assert len(capped.candidates) == 1
    assert any(warning.startswith("limit:") for warning in capped.warnings)

    missing = search_symbols(scope(), "DeleteEverythingAsync")
    assert missing.candidates == ()
    assert missing.truncated is False
    assert missing.warnings == ("no_symbol_matched",)


def test_bounds_outside_the_request_contract_are_rejected() -> None:
    """Boundary: an out-of-range bound is a validation error, never a clamp."""
    with pytest.raises(LimitRejected):
        search_symbols(scope(), "LoginAsync", max_nodes=501)
    with pytest.raises(LimitRejected):
        search_symbols(scope(), "LoginAsync", limit=0)
    with pytest.raises(LimitRejected):
        search_symbols(scope(), "LoginAsync", kinds=("NotAKind",))
