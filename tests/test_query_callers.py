"""C-021: a caller lookup reads every pinned member, and never reads a local miss as "no callers".

AC1 has two halves:

* positive - the only caller of a target lives in *another* project. A per-project reverse index
  would return an empty list with confidence; the global index ``GraphSet`` builds over every
  pinned member finds it, and ``searched_projects``/``cross_project``/``per_project`` say where the
  answer came from.
* boundary - the same lookup against a scope that is *told* a member exists but does not pin it
  must not read as "no callers". ``complete`` is false, ``missing_projects`` names the member and
  the warning says the absence is not proven. That is the misreading the card forbids.

The negative cases are a target whose only incoming edges are of a kind that is filtered out (a
real, complete "no callers" answer), an ambiguous target (candidates, no callers), and a budget
that stops the walk (truncated, so incomplete).
"""

from __future__ import annotations

import pytest
from test_query_support import edge_document, generation_id, node_document

from axiom_mcp.query.callers import (
    CALLER_KINDS,
    STATUS_AMBIGUOUS_TARGET,
    callers,
)
from axiom_mcp.query.model import GraphSet, LimitRejected, graph_from_documents

AUTH = "auth-api"
WEB = "web-app"


def auth_project() -> object:
    handler = node_document(
        "LoginHandler", project_id=AUTH, qualified_name="App.LoginHandler", kind="Class"
    )
    return graph_from_documents(AUTH, generation_id("c021-auth"), nodes=(handler,), edges=())


def web_project() -> object:
    page = node_document("WebLogin", project_id=WEB, qualified_name="App.WebLogin")
    handler = node_document(
        "LoginHandler", project_id=AUTH, qualified_name="App.LoginHandler", kind="Class"
    )
    calls = edge_document(page["id"], target_id=handler["id"], target_project_id=AUTH, kind="CALLS")
    return graph_from_documents(WEB, generation_id("c021-web"), nodes=(page,), edges=(calls,))


def cross_project_scope() -> GraphSet:
    return GraphSet((auth_project(), web_project()))


def test_a_caller_in_another_project_is_found() -> None:
    """AC1 positive: the global reverse index sees a call the local project cannot."""
    result = callers(cross_project_scope(), "App.LoginHandler")

    assert [node["name"] for node in result.nodes] == ["WebLogin"]
    assert result.per_project == {WEB: 1}
    assert result.cross_project == (WEB,)
    assert result.searched_projects == (AUTH, WEB)
    assert result.complete is True
    assert [edge["kind"] for edge in result.edges] == ["CALLS"]


def test_local_absence_is_not_proof_of_no_callers() -> None:
    """AC1 boundary: a member the scope does not pin makes the empty answer incomplete."""
    partial = GraphSet((auth_project(),), missing_projects=(WEB,))
    result = callers(partial, "App.LoginHandler")

    assert result.nodes == ()
    assert result.searched_projects == (AUTH,)
    assert result.missing_projects == (WEB,)
    assert result.complete is False
    assert "missing_members" in result.incomplete_reasons
    assert any(warning.startswith("incomplete_search:") for warning in result.warnings)


def test_a_filtered_out_kind_is_a_real_no_caller_answer() -> None:
    """Negative: with a complete scope, no caller of the requested kind is a real answer."""
    hidden = node_document("WebLogin", project_id=WEB, qualified_name="App.WebLogin")
    handler = node_document(
        "LoginHandler", project_id=AUTH, qualified_name="App.LoginHandler", kind="Class"
    )
    reads = edge_document(
        hidden["id"], target_id=handler["id"], target_project_id=AUTH, kind="READS"
    )
    web = graph_from_documents(WEB, generation_id("c021-reads"), nodes=(hidden,), edges=(reads,))
    result = callers(GraphSet((auth_project(), web)), "App.LoginHandler", edge_kinds=["CALLS"])

    assert result.nodes == ()
    assert result.edges == ()
    assert result.complete is True, "a complete scope with no matching caller is not incomplete"
    assert result.per_kind == {}


def test_the_target_does_not_caller_its_own_callees() -> None:
    """Negative: the walk is incoming-only, so what the target calls is not a caller of it."""
    handler = node_document(
        "LoginHandler", project_id=AUTH, qualified_name="App.LoginHandler", kind="Class"
    )
    store = node_document("SessionStore", project_id=AUTH, qualified_name="App.SessionStore")
    calls = edge_document(handler["id"], target_id=store["id"], kind="CALLS")
    scope = GraphSet(
        (
            graph_from_documents(
                AUTH,
                generation_id("c021-outgoing"),
                nodes=(handler, store),
                edges=(calls,),
            ),
        )
    )
    result = callers(scope, "App.LoginHandler")

    assert result.direction == "incoming"
    assert result.nodes == ()
    assert result.complete is True


def test_an_unresolved_incoming_edge_makes_the_answer_incomplete() -> None:
    """Boundary: an unresolved source is reported and prevents a confident empty answer."""
    handler = node_document(
        "LoginHandler", project_id=AUTH, qualified_name="App.LoginHandler", kind="Class"
    )
    legacy = node_document("LegacyCaller", project_id=AUTH, qualified_name="Legacy.Caller")
    unknown = edge_document(
        legacy["id"],
        kind="REFERENCES",
        resolution="unresolved",
        unresolved_target="App.LoginHandler",
    )
    scope = GraphSet(
        (
            graph_from_documents(
                AUTH,
                generation_id("c021-unresolved"),
                nodes=(handler, legacy),
                edges=(unknown,),
            ),
        )
    )
    result = callers(scope, "App.LoginHandler")

    assert [edge["resolution"] for edge in result.unresolved] == ["unresolved"]
    assert result.nodes == ()
    assert result.complete is False
    assert "unresolved_edges" in result.incomplete_reasons


def test_a_target_called_by_the_same_name_is_ambiguous_not_guessed() -> None:
    """Negative: an ambiguous selector returns candidates and no caller list."""
    first = node_document("TwinAsync", project_id=AUTH, qualified_name="App.TwinAsync")
    second = node_document("TwinAsync", project_id=WEB, qualified_name="Other.TwinAsync")
    scope = GraphSet(
        (
            graph_from_documents(AUTH, generation_id("c021-a"), nodes=(first,), edges=()),
            graph_from_documents(WEB, generation_id("c021-b"), nodes=(second,), edges=()),
        )
    )
    result = callers(scope, "TwinAsync")

    assert result.status == STATUS_AMBIGUOUS_TARGET
    assert len(result.candidates) == 2
    assert result.nodes == ()
    assert result.complete is False


def test_a_capped_walk_is_truncated_and_incomplete() -> None:
    """Boundary: a budget that stops the walk says so, and refuses an out-of-range bound."""
    capped = callers(cross_project_scope(), "App.LoginHandler", max_edges=0)
    assert capped.edges == ()
    assert capped.truncated is True
    assert capped.reasons == ("max_edges",)
    assert "truncated" in capped.incomplete_reasons

    with pytest.raises(LimitRejected):
        callers(cross_project_scope(), "App.LoginHandler", depth=9)
    with pytest.raises(LimitRejected):
        callers(cross_project_scope(), "App.LoginHandler", edge_kinds=["NOT_A_KIND"])


def test_depth_reaches_a_caller_of_a_caller() -> None:
    """AC1: depth is the caller-chain length, and one hop does not silently include two."""
    handler = node_document(
        "LoginHandler", project_id=AUTH, qualified_name="App.LoginHandler", kind="Class"
    )
    direct = node_document("WebLogin", project_id=WEB, qualified_name="App.WebLogin")
    indirect = node_document("Router", project_id=WEB, qualified_name="App.Router")
    first = edge_document(
        direct["id"], target_id=handler["id"], target_project_id=AUTH, kind="CALLS"
    )
    second = edge_document(
        indirect["id"], target_id=direct["id"], target_project_id=WEB, kind="CALLS"
    )
    scope = GraphSet(
        (
            graph_from_documents(AUTH, generation_id("c021-h"), nodes=(handler,), edges=()),
            graph_from_documents(
                WEB, generation_id("c021-c"), nodes=(direct, indirect), edges=(first, second)
            ),
        )
    )

    one = callers(scope, "App.LoginHandler", depth=1)
    assert [node["name"] for node in one.nodes] == ["WebLogin"]

    two = callers(scope, "App.LoginHandler", depth=2)
    assert sorted(node["name"] for node in two.nodes) == ["Router", "WebLogin"]
    assert two.complete is True


def test_the_default_kind_allowlist_excludes_containment() -> None:
    """AC1: the default caller kinds are every pinned kind except containment."""
    assert "CONTAINS" not in CALLER_KINDS
    assert "CALLS" in CALLER_KINDS and "REFERENCES" in CALLER_KINDS
