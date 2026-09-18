"""Record the C-007 ``version`` and ``doctor`` evidence on this host.

Run from the repository root:

    python release/cli_spike.py

The output is JSON on stdout and is stored as the C-007 compatibility artifact.
It records what the command line actually did - the real exit code of each
invocation, the real stdout, and the sections doctor actually failed - rather
than asserting the commands exist. The invocations run as child processes so the
console-script wiring is exercised, not just the module functions.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from axiom_mcp import cli, security, version  # noqa: E402


def invoke(argv: list[str], env: dict[str, str] | None = None) -> dict[str, Any]:
    """Run the CLI as a child process and record code, stdout and stderr."""
    proc = subprocess.run(
        [sys.executable, "-m", "axiom_mcp.cli", *argv],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env={**_base_env(), **(env or {})},
    )
    result: dict[str, Any] = {"argv": argv, "exit_code": proc.returncode}
    stdout = proc.stdout.strip()
    try:
        result["stdout_json"] = json.loads(stdout)
    except json.JSONDecodeError:
        result["stdout_text"] = stdout
    if proc.stderr.strip():
        result["stderr"] = proc.stderr.strip()
    return result


def _base_env() -> dict[str, str]:
    """Run the child with the repository source on its path.

    The console script installs the package; a source checkout does not, so the
    spike points PYTHONPATH at src instead of pretending the entry point is
    installed. The argv and exit-code behaviour it observes is the real one.
    """
    import os

    return {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}


def registry_probe() -> dict[str, Any]:
    """Show the credential scope path end to end with a throwaway registry."""
    secret = "release-spike-token-value"
    with tempfile.TemporaryDirectory() as tmp:
        registry = pathlib.Path(tmp) / "registry.json"
        registry.write_text(
            json.dumps(
                {
                    "tokens": [
                        {
                            "kind": "env",
                            "variable": "AXIOM_MCP_TOKEN",
                            "token_id": "spike-read",
                            "capabilities": ["read", "reconcile"],
                            "solution_ids": ["demo-solution"],
                            "project_ids": ["auth-api"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        reference = json.dumps({"kind": "env", "variable": "AXIOM_MCP_TOKEN"})
        scope = cli.credential_scope(
            {cli.CREDENTIAL_REFERENCE_ENV: reference, "AXIOM_MCP_TOKEN": secret},
            registry_path=registry,
        )
        report = cli.doctor_report(cli.collect_environment({}, registry_path=registry))
        return {
            "declared_scope": scope.as_dict(str(REPO_ROOT)),
            "token_value_present_in_report": secret in json.dumps(report),
            "enforced_audience": security.MCP_AUDIENCE,
            "control_audience_separate": security.CONTROL_AUDIENCE,
        }


def main() -> int:
    report = {
        "task": "C-007",
        "package_version": version.VERSION,
        "python": sys.version,
        "console_script_target": "axiom-mcp",
        "invocations": [
            invoke(["version", "--json"]),
            invoke(["version"]),
            invoke(["doctor", "--json"]),
            invoke(["doctor"]),
            invoke(["doctor", "--frobnicate"]),
            invoke(["frobnicate"]),
            invoke([]),
            invoke(
                [
                    "doctor",
                    "--json",
                    "--registry",
                    str(pathlib.Path(tempfile.gettempdir()) / "axiom-spike-missing.json"),
                ]
            ),
        ],
        "credential_scope_probe": registry_probe(),
        "exit_code_table": cli.CODE_TO_EXIT,
        "sections": list(cli.SECTION_ORDER),
        "readiness_contract": {
            "health_path": "/healthz",
            "ready_path": "/readyz",
            "process_health_is_readiness": False,
        },
    }
    json.dump(report, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
