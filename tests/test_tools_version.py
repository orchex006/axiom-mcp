"""C-033 regression tests: ``graph_version``.

The tool reports and never acts, so the load-bearing cases are:

* :func:`test_a_graph_query_cannot_trigger_installation` - the AC1 tripwire. It replaces
  :func:`axiom_mcp.update.apply_plan` with a function that fails the test if it is ever
  reached, then asserts a full ``graph_version`` call leaves it untouched and that the
  handler has no ``install``/``apply`` keyword at all;
* :func:`test_only_this_component_is_observable` and
  :func:`test_unobservable_components_say_so` - a component this process cannot see is
  reported as unobserved rather than guessed;
* :func:`test_update_is_not_checked_unless_asked` - the read-only update check runs only on
  ``check_update: true``.

The negative and boundary legs cover an unknown component name, a non-list and an empty
``components``, a non-boolean ``check_update``, an unexpected field, a token without
``read``, a pin mismatch becoming a warning, and a duplicate request that is deduplicated.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from axiom_mcp import update, version
from axiom_mcp.errors import AxiomError
from axiom_mcp.tools.version import (
    KNOWN_COMPONENTS,
    VERSION_FIELDS,
    graph_version,
)
from tests.test_tools_support import FakeControl, context_for


def call(context, *, environ=None, **arguments):
    return graph_version(dict(arguments), context, environ=environ)


def refusing(context, code, **arguments):
    with pytest.raises(AxiomError) as caught:
        call(context, **arguments)
    assert caught.value.code == code
    return caught.value


def component(answer: dict[str, Any], name: str) -> dict[str, Any]:
    return next(entry for entry in answer["components"] if entry["component"] == name)


def test_reports_the_closed_field_set(tmp_path):
    answer = call(context_for(tmp_path))
    assert tuple(answer) == VERSION_FIELDS


def test_only_this_component_is_observable(tmp_path):
    answer = call(context_for(tmp_path))
    this = component(answer, "axiom-mcp")
    assert this["observed"] is True
    assert this["version"] == version.VERSION
    assert this["spec_version"] == version.SPEC_VERSION
    assert this["spec_revision"] == version.SPEC_REVISION


def test_unobservable_components_say_so(tmp_path):
    answer = call(context_for(tmp_path))
    for name in ("axiom-specs", "axiom-skills"):
        entry = component(answer, name)
        assert entry["observed"] is False
        assert entry["reason"] == "not_observable_from_this_component"


def test_graphd_is_observed_only_when_control_is_wired(tmp_path):
    unwired = call(context_for(tmp_path / "a"))
    assert component(unwired, "axiom-graphd")["observed"] is False
    assert component(unwired, "axiom-graphd")["reason"] == "control_client_not_wired"

    wired = call(context_for(tmp_path / "b", control=FakeControl()))
    entry = component(wired, "axiom-graphd")
    assert entry["observed"] is True
    assert entry["control_api"] == version.CONTROL_API


def test_installation_block_claims_no_install_path(tmp_path):
    answer = call(context_for(tmp_path))
    assert answer["installation"] == {
        "implicit_install": False,
        "reason": "no_install_path_on_the_mcp_surface",
        "applies_via": "cli_or_skill_workflow",
    }


def test_a_graph_query_cannot_trigger_installation(tmp_path, monkeypatch):
    """AC1: a version read must never reach the update applier."""

    def tripwire(*_args, **_kwargs):  # pragma: no cover - only runs on a regression
        raise AssertionError("graph_version must never apply an update plan")

    monkeypatch.setattr(update, "apply_plan", tripwire)
    monkeypatch.setattr(update, "delegation_argv", tripwire)

    context = context_for(tmp_path)
    answer = call(context, check_update=True)
    assert answer["installation"]["implicit_install"] is False

    parameters = set(inspect.signature(graph_version).parameters)
    assert "install" not in parameters
    assert "apply" not in parameters
    assert "install" not in inspect.signature(graph_version).parameters


def test_update_is_not_checked_unless_asked(tmp_path):
    answer = call(context_for(tmp_path))
    block = answer["update"]
    assert block["status"] == "not_checked"
    assert block["available"] is None
    assert block["needs_restart"] is False
    assert block["reasons"] == ["update_check_not_requested"]


def test_check_update_true_uses_the_read_only_check(tmp_path, monkeypatch):
    recorded: list[Any] = []

    def fake_check(environ=None, **kwargs):
        recorded.append(environ)
        return update.UpdateCheck(
            installed=version.VERSION,
            available="9.9.9",
            compatible=True,
            channel="stable",
            source_origin="https://example.invalid/axiom",
            status=update.STATUS_AVAILABLE,
            needs_restart=True,
            reasons=("newer_release_available",),
        )

    monkeypatch.setattr("axiom_mcp.tools.version.update.check_update", fake_check)
    answer = call(context_for(tmp_path), environ={"AXIOM_CHANNEL": "stable"}, check_update=True)
    block = answer["update"]
    assert recorded == [{"AXIOM_CHANNEL": "stable"}]
    assert block["status"] == update.STATUS_AVAILABLE
    assert block["available"] == "9.9.9"
    assert block["needs_restart"] is True
    assert block["reasons"] == ["newer_release_available"]


def test_unknown_component_is_refused_with_allowed(tmp_path):
    problem = refusing(context_for(tmp_path), "VALIDATION_ERROR", components=["axiom-nope"])
    assert problem.details["allowed"] == list(KNOWN_COMPONENTS)


def test_components_must_be_a_list_not_a_string(tmp_path):
    refusing(context_for(tmp_path), "VALIDATION_ERROR", components="axiom-mcp")


def test_empty_component_list_is_refused(tmp_path):
    refusing(context_for(tmp_path), "VALIDATION_ERROR", components=[])


def test_duplicate_components_are_deduplicated(tmp_path):
    answer = call(context_for(tmp_path), components=["axiom-mcp", "axiom-mcp", "axiom-specs"])
    names = [entry["component"] for entry in answer["components"]]
    assert names == ["axiom-mcp", "axiom-specs"]


def test_check_update_must_be_a_boolean(tmp_path):
    refusing(context_for(tmp_path), "VALIDATION_ERROR", check_update="yes")


def test_unexpected_field_is_refused(tmp_path):
    refusing(context_for(tmp_path), "VALIDATION_ERROR", solution_id="alpha")


def test_read_capability_is_required(tmp_path):
    refusing(context_for(tmp_path, caps=frozenset()), "FORBIDDEN")


def test_a_pin_mismatch_becomes_a_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(version, "runtime_pin_reasons", lambda: ["pinned_sdk_mismatch"])
    answer = call(context_for(tmp_path))
    assert answer["compatibility"]["compatible"] is False
    assert answer["warnings"] == ["runtime_pin_mismatch"]
