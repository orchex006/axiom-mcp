"""Public composition preserves caller scopes and the canonical tool surface."""

import hashlib
import json
from pathlib import Path

import anyio
from mcp import types

from axiom_mcp import security, stdio, update
from axiom_mcp.runtime import Runtime, compose
from axiom_mcp.tools import names
from tests.test_tools_support import install_bundle, write_registry


def configured(tmp_path, monkeypatch):
    repo = install_bundle(tmp_path)
    home = tmp_path / "home"
    write_registry(home, repo)
    monkeypatch.setenv("AXIOM_HOME", str(home))
    token_file = tmp_path / "credential"
    token_file.write_text("fixture-token", encoding="utf-8")
    reference = {"kind": "file", "path": str(token_file)}
    credentials = tmp_path / "credentials.json"
    credentials.write_text(
        json.dumps(
            {
                "tokens": [
                    {
                        **reference,
                        "token_id": "reader",
                        "audience": security.MCP_AUDIENCE,
                        "capabilities": ["read"],
                        "solution_ids": ["demo-solution"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AXIOM_MCP_TOKEN_REGISTRY", str(credentials))
    monkeypatch.setenv("AXIOM_MCP_TOKEN_REFERENCE", json.dumps(reference))
    return Runtime.configured()


def call(server, name, arguments):
    async def invoke():
        result = await server._mcp_server.request_handlers[types.CallToolRequest](
            types.CallToolRequest(
                params=types.CallToolRequestParams(name=name, arguments=arguments)
            )
        )
        return result.root

    return anyio.run(invoke)


def test_six_registered_tools_and_actual_guarded_query(tmp_path, monkeypatch):
    runtime = configured(tmp_path, monkeypatch)
    server = compose(stdio.build_stdio_server(), runtime, transport="stdio")

    async def discover():
        result = await server._mcp_server.request_handlers[types.ListToolsRequest](
            types.ListToolsRequest()
        )
        return result.root.tools

    assert {t.name for t in anyio.run(discover)} == set(names())
    result = call(
        server,
        "graph_query",
        {"solution_id": "demo-solution", "operation": "search", "query": "login"},
    )
    assert not result.isError and result.structuredContent["nodes"]
    assert runtime.readiness().query.available
    assert not runtime.readiness().control.available
    assert call(server, "graph_reconcile", {"solution_id": "demo-solution"}).isError
    invisible = call(
        server,
        "graph_query",
        {"solution_id": "unregistered", "operation": "search", "query": "login"},
    )
    assert invisible.structuredContent["code"] == "NOT_FOUND"


def test_bare_transport_discovery_grants_no_tool_authority(monkeypatch, tmp_path):
    monkeypatch.setenv("AXIOM_HOME", str(tmp_path / "empty"))
    monkeypatch.delenv("AXIOM_MCP_TOKEN_REGISTRY", raising=False)
    monkeypatch.delenv("AXIOM_MCP_TOKEN_REFERENCE", raising=False)
    server = compose(stdio.build_stdio_server(), Runtime.configured(), transport="stdio")
    assert call(server, "graph_version", {}).structuredContent["code"] == "UNAUTHENTICATED"


def test_generated_evaluator_bytes_are_the_provenance_not_a_policy_fork():
    root = Path(update.__file__).with_name("_canonical")
    manifest = json.loads((root / "provenance.json").read_bytes())
    assert (
        hashlib.sha256((root / "update_plan_contract.py").read_bytes()).hexdigest()
        == manifest["sha256"]
    )
    assert len(manifest["spec_revision"]) == 40
    assert callable(update.load_canonical_evaluator().evaluate)
    fixtures = Path(__file__).parent / "fixtures"
    provenance = json.loads((fixtures / "canonical-update-provenance.json").read_bytes())
    for name, digest in provenance["files"].items():
        assert (
            hashlib.sha256((fixtures / "canonical-update-plan" / name).read_bytes()).hexdigest()
            == digest
        )


def test_cli_core_plan_needs_explicit_exact_approval(monkeypatch):
    body = {"kind": "ecosystem-update-plan", "schema_version": 1, "ecosystem": {}}
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    document = {**body, "plan_digest": digest}
    recorded = []
    monkeypatch.setattr(update, "run_core", lambda argv: recorded.append(argv) or 7)
    import pytest

    with pytest.raises(update.PlanRejected):
        update.execute_core_plan(document, "plan.json", None, as_json=True)
    assert not recorded
    assert update.execute_core_plan(document, "plan.json", digest, as_json=True) == 7
    assert recorded[0][-3:] == ["--approve-digest", digest, "--json"]
    with pytest.raises(update.PlanRejected):
        update.execute_core_plan(
            {**document, "schema_version": 2}, "plan.json", digest, as_json=False
        )
