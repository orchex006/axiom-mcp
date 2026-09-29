#!/usr/bin/env python3
"""Query one real-source catalog with the MCP wheel in this interpreter's venv."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from axiom_mcp import __file__ as package_file
from axiom_mcp import security, version
from axiom_mcp.registry import load_registry
from axiom_mcp.tools.context import GuardedSnapshotSource, ToolContext, ToolPrincipal
from axiom_mcp.tools.query import graph_query


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--axiom-home", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--symbol", default="TokenSource")
    args = parser.parse_args()
    home = args.axiom_home.resolve(strict=True)
    repo = args.repo.resolve(strict=True)
    wheel_module = Path(package_file).resolve(strict=True)
    venv = Path(sys.prefix).resolve(strict=True)
    if not wheel_module.is_relative_to(venv):
        raise SystemExit("MCP import did not come from the installed versioned environment")
    mismatch = {
        "actual_runtime_pin_reasons": version.runtime_pin_reasons(),
        "wrong_sdk": version.sdk_compatibility_reasons("1.28.0", "3.13.15"),
        "wrong_python": version.sdk_compatibility_reasons("1.28.1", "3.12.9"),
        "missing_protocol": version.protocol_support_reasons(["2024-11-05"]),
    }
    if (
        mismatch["actual_runtime_pin_reasons"]
        or "sdk_version_unsupported:1.28.0" not in mismatch["wrong_sdk"]
        or "python_version_unsupported:3.12.9" not in mismatch["wrong_python"]
        or "protocol_revision_missing:2025-11-25" not in mismatch["missing_protocol"]
    ):
        raise SystemExit("installed compatibility checker did not refuse mismatched versions")
    catalog = repo / ".axiom/graph/demo-solution/_catalog/live/current.json"
    pointer = json.loads(catalog.read_text())
    generation = pointer["generation_id"]
    manifest = catalog.parent / "generations" / generation / "manifest.json"
    if sha(manifest) != generation:
        raise SystemExit("real-source catalog generation does not match its manifest")
    config = home / "config/registry.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    document = {
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
    if config.exists() or config.is_symlink():
        raise SystemExit("installed MCP registry path is already occupied")
    config.write_text(json.dumps(document) + "\n")
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
        envelope = graph_query(
            {"solution_id": "demo-solution", "operation": "search", "query": args.symbol},
            context,
        )
        if args.symbol not in json.dumps(envelope) or not envelope.get("nodes"):
            raise SystemExit("installed MCP query omitted the real-source symbol")
        print(
            json.dumps(
                {
                    "status": "passed",
                    "wheel_import_below_venv": True,
                    "catalog_manifest_sha256": generation,
                    "catalog_pointer_sha256": sha(catalog),
                    "query_symbol": args.symbol,
                    "node_count": len(envelope["nodes"]),
                    "coverage": envelope.get("coverage"),
                    "verification": envelope.get("verification"),
                    "catalog_generation_id": envelope.get("catalog_generation_id"),
                    "version_mismatch": mismatch,
                },
                sort_keys=True,
            )
        )
    finally:
        config.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
