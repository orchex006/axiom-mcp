#!/usr/bin/env python3
"""Compose the installed MCP wheel's query handler on its real stdio transport."""

from __future__ import annotations

import argparse
import functools
import json
import sys
from pathlib import Path

import anyio

from axiom_mcp import security, stdio
from axiom_mcp.registry import load_registry
from axiom_mcp.tools.context import GuardedSnapshotSource, ToolContext, ToolPrincipal
from axiom_mcp.tools.query import graph_query


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--axiom-home", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    args = parser.parse_args()
    home = args.axiom_home.resolve(strict=True)
    repo = args.repo.resolve(strict=True)
    if not Path(stdio.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
        raise SystemExit("stdio server did not import the installed wheel")
    config = home / "config/registry.json"
    if config.exists() or config.is_symlink():
        raise SystemExit("registry is already occupied")
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "axiom_home": str(home),
                "instances": [{"instance_id": "local-instance"}],
                "solutions": [
                    {
                        "solution_id": "demo-solution",
                        "instance_id": "local-instance",
                        "catalog_host_repo": "primary",
                        "repositories": [
                            {
                                "repo_id": "primary",
                                "repo_root": str(repo),
                                "projects": [{"project_id": "demo-project"}],
                            }
                        ],
                    }
                ],
            }
        )
        + "\n"
    )
    try:
        registry = load_registry(config, env={"AXIOM_HOME": str(home)}, platform="darwin")
        principal = ToolPrincipal(
            token_id="local-k107",
            capabilities=frozenset({security.CAPABILITY_READ}),
            solution_ids=frozenset({"demo-solution"}),
        )
        context = ToolContext(
            registry=registry,
            principal=principal,
            source=GuardedSnapshotSource(home / "instances/local-instance/solution.guard"),
        )
        server = stdio.build_stdio_server("k-107-installed-wheel-query")

        @server.tool(name="graph_query")
        def query(solution_id: str, operation: str, query: str) -> dict:
            return graph_query(
                {"solution_id": solution_id, "operation": operation, "query": query},
                context,
            )

        return anyio.run(functools.partial(stdio.serve_stdio, server))
    finally:
        config.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
