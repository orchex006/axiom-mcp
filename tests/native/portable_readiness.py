"""Actual installed wheel process/RPC/guard proof; no licensed AI host claim."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import anyio
import httpx
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from tests.test_tools_support import install_bundle, write_registry  # noqa: E402

TOOLS = {
    "graph_status",
    "graph_query",
    "graph_reconcile",
    "graph_job",
    "graph_verify",
    "graph_version",
}


def run(python: Path, wheel: Path, core: Path, source: str, out: Path) -> None:
    assert len(source) == 40 and wheel.name.endswith("-py3-none-any.whl")
    cases = []

    def record(name, result, expected=0):
        observed = int(result.isError) if hasattr(result, "isError") else 0
        assert observed == expected, (name, result)
        data = (
            result.model_dump_json() if hasattr(result, "model_dump_json") else json.dumps(result)
        )
        cases.append(
            {
                "id": name,
                "exit_code": observed,
                "expected_exit_code": expected,
                "stdout_sha256": hashlib.sha256(data.encode()).hexdigest(),
                "outcome_basis": "RPC isError or asserted process proof",
            }
        )
        print("PASS: " + name, flush=True)

    with tempfile.TemporaryDirectory(prefix="axiom mcp native ") as temporary:
        root = Path(temporary)
        repo = install_bundle(root)
        home = root / "home"
        write_registry(home, repo)
        credential = root / "credential"
        credential.write_text("native-fixture-token", encoding="utf-8")
        credential.chmod(0o600)
        ref = {"kind": "file", "path": str(credential)}
        config = root / "tokens.json"
        config.write_text(
            json.dumps(
                {
                    "tokens": [
                        {
                            **ref,
                            "token_id": "native-reader",
                            "audience": "axiom-mcp",
                            "capabilities": ["read"],
                            "solution_ids": ["demo-solution"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        env = {
            **os.environ,
            "AXIOM_HOME": str(home),
            "AXIOM_MCP_TOKEN_REGISTRY": str(config),
            "AXIOM_MCP_TOKEN_REFERENCE": json.dumps(ref),
            "AXIOM_MCP_BUILD_REVISION": source,
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
        }
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env.pop("AXIOM_CONTROL_TOKEN_REFERENCE", None)
        provenance = subprocess.run(
            [
                str(python),
                "-c",
                "import axiom_mcp,sys,json; from pathlib import Path; "
                "p=Path(axiom_mcp.__file__).resolve(); "
                "assert p.is_relative_to(Path(sys.prefix).resolve()); "
                "print(json.dumps({'installed':True}))",
            ],
            env=env,
            cwd=root,
            capture_output=True,
            timeout=15,
        )
        assert provenance.returncode == 0, provenance.stderr
        record("wheel provenance", {"installed": True, "source": source})

        async def exercise(session, transport):
            await session.initialize()
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == TOOLS
            record(transport + " six tools", tools)
            query = await session.call_tool(
                "graph_query",
                {"solution_id": "demo-solution", "operation": "search", "query": "login"},
            )
            assert query.structuredContent["nodes"], query
            record(transport + " registered query", query)
            if transport == "stdio":
                record(
                    "scope refusal",
                    await session.call_tool(
                        "graph_query",
                        {"solution_id": "invisible", "operation": "search", "query": "login"},
                    ),
                    1,
                )
                assert (
                    await session.call_tool("graph_reconcile", {"solution_id": "demo-solution"})
                ).isError
            assert not (await session.call_tool("graph_version", {})).isError

        async def stdio_probe():
            with anyio.fail_after(40):
                parameters = StdioServerParameters(
                    command=str(python),
                    args=["-m", "axiom_mcp.stdio", "--no-banner"],
                    env=env,
                    cwd=str(root),
                )
                async with (
                    stdio_client(parameters) as (read, write),
                    ClientSession(read, write) as session,
                ):
                    await exercise(session, "stdio")
            record("stdio shutdown", {"pipe_closed": True})

        anyio.run(stdio_probe)

        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        process = subprocess.Popen(
            [
                str(python),
                "-m",
                "axiom_mcp.http",
                "--port",
                str(port),
                "--allow-host",
                f"127.0.0.1:{port}",
                "--allow-origin",
                f"http://127.0.0.1:{port}",
            ],
            env=env,
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        url = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    if httpx.get(url + "/healthz", timeout=1).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert process.poll() is None and time.monotonic() < deadline, "HTTP startup failed"
                time.sleep(0.1)
            assert httpx.post(url + "/mcp", json={}, timeout=5).status_code == 401
            record("http auth refusal", {"status": 401})
            for name, tool, arguments in [
                (
                    "http scope refusal",
                    "graph_query",
                    {"solution_id": "invisible", "operation": "search"},
                ),
                ("http mutation refusal", "graph_reconcile", {"solution_id": "demo-solution"}),
            ]:
                response = httpx.post(
                    url + "/mcp",
                    headers={"Authorization": "Bearer native-fixture-token"},
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": tool, "arguments": arguments},
                    },
                    timeout=5,
                )
                assert response.status_code == 403
                record(name, {"status": response.status_code})

            async def http_probe():
                with anyio.fail_after(40):
                    async with httpx.AsyncClient(
                        headers={"Authorization": "Bearer native-fixture-token"}
                    ) as client:
                        async with (
                            streamable_http_client(url + "/mcp", http_client=client) as (
                                read,
                                write,
                                _,
                            ),
                            ClientSession(read, write) as session,
                        ):
                            await exercise(session, "http")

            anyio.run(http_probe)
        finally:
            process.terminate()
            process.wait(timeout=15)

        guard = home / "instances/local-instance/solution.guard"
        # The helper's instance name follows the trusted fixture registry, not a guessed path.
        binding = json.loads((home / "config/registry.json").read_bytes())["solutions"][0]
        guard = home / "instances" / binding["instance_id"] / "solution.guard"
        holder_code = (
            "import sys; from axiom_mcp.guard.engine import SolutionGuard; "
            "g=SolutionGuard(sys.argv[1]); g.ensure_directory(); "
            "c=g.publisher(); c.__enter__(); print('held',flush=True); sys.stdin.read()"
        )
        holder = subprocess.Popen(
            [str(python), "-c", holder_code, str(guard)],
            env=env,
            cwd=root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert holder.stdout.readline().strip() == b"held"
            reader = subprocess.run(
                [
                    str(python),
                    "-m",
                    "axiom_mcp.guard.interop",
                    "reader",
                    "--dir",
                    str(guard),
                    "--timeout-ms",
                    "100",
                ],
                env=env,
                cwd=root,
                capture_output=True,
                timeout=10,
            )
            assert reader.returncode != 0
            record("native guard exclusion", {"refused": True, "exit": reader.returncode})
        finally:
            holder.kill()
            holder.wait(timeout=10)
        reader = subprocess.run(
            [
                str(python),
                "-m",
                "axiom_mcp.guard.interop",
                "reader",
                "--dir",
                str(guard),
                "--timeout-ms",
                "100",
            ],
            env=env,
            cwd=root,
            capture_output=True,
            timeout=10,
        )
        assert reader.returncode == 0, reader.stderr
        record("native guard recovery", {"exit": reader.returncode})

        # Core owns activation/rollback; verify real process dispatch and refusal.
        document = {"kind": "ecosystem-update-plan", "schema_version": 1, "ecosystem": {}}
        digest = hashlib.sha256(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        document["plan_digest"] = digest
        plan = root / "update.json"
        plan.write_text(json.dumps(document), encoding="utf-8")
        refusal = subprocess.run(
            [str(python), "-m", "axiom_mcp.cli", "update", "apply", "--plan", str(plan)],
            env=env,
            cwd=root,
            capture_output=True,
            timeout=10,
        )
        assert refusal.returncode == 5
        record("stale update refusal", {"exit": refusal.returncode})
        env["AXIOM_CORE_COMMAND"] = str(core)
        approved = subprocess.run(
            [
                str(python),
                "-m",
                "axiom_mcp.cli",
                "update",
                "apply",
                "--plan",
                str(plan),
                "--approve-digest",
                digest,
            ],
            env=env,
            cwd=root,
            capture_output=True,
            timeout=10,
        )
        assert approved.returncode == 2
        record(
            "approved update delegation",
            {
                "exit": 2,
                "boundary": "real core rejects invalid ecosystem schema",
                "core_sha256": hashlib.sha256(core.read_bytes()).hexdigest(),
            },
        )

    lane = {"Windows": "windows-x64", "Linux": "linux-x64", "Darwin": "macos-x64"}[
        platform.system()
    ]
    report = {
        "platform": lane,
        "execution": "native",
        "source_revision": source,
        "public_forms": sorted(
            {c["id"] for c in cases} - {"http scope refusal", "http mutation refusal"}
        ),
        "cases": cases,
        "installed_wheel_verified": True,
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "negative_cases_verified": True,
        "fixture_scope": "canonical processed snapshot; no licensed AI host",
        "background": "host-owned stdio child; foreground HTTP; CLI owns supervision",
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    run(
        args.python.absolute(),
        args.wheel.resolve(),
        args.core.resolve(),
        args.source_revision,
        args.out.resolve(),
    )
