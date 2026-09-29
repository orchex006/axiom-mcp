#!/usr/bin/env python3
"""Native Mac Intel installed-wheel query across A, B, restart and rollback."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

PROBE = Path(__file__).with_name("installed_query_probe.py")
MATRIX = Path(__file__).with_name("capture_native_matrix.py")
LABEL = "com.axiom.axiom-graphd"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract(archive: Path, root: Path) -> None:
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        if not members or any(
            not row.isfile() or row.name.startswith("/") or ".." in Path(row.name).parts
            for row in members
        ):
            raise ValueError("candidate archive has an unsafe member")
        source.extractall(root, filter="data")


def call(argv: list[str], env: dict[str, str], trace: list[dict], expected: int = 0) -> dict:
    result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=100)
    row = {
        "argv": argv,
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    trace.append(row)
    if result.returncode != expected:
        raise AssertionError(f"command returned {result.returncode}, expected {expected}: {row}")
    try:
        return json.loads(result.stdout if expected == 0 else result.stderr)
    except json.JSONDecodeError:
        return {"text": result.stdout if expected == 0 else result.stderr}


def distribution(
    release: Path, env: dict[str, str], trace: list[dict], action: str, apply: bool
) -> dict:
    argv = [
        "/bin/sh",
        str(release / "runtime/bootstrap.sh"),
        action,
        "--release-set",
        str(release),
        "--dry-run",
    ]
    plan = call(argv, env, trace)
    if not apply:
        return plan
    argv[-1] = "--apply"
    argv.extend(["--approve-digest", plan["plan_digest"]])
    return call(argv, env, trace)


def catalog(repo: Path, previous: str | None = None, timeout: float = 30.0) -> str:
    pointer = repo / ".axiom/graph/demo-solution/_catalog/live/current.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pointer.is_file():
            generation = json.loads(pointer.read_text())["generation_id"]
            if generation != previous:
                return generation
        time.sleep(0.2)
    raise AssertionError("real-source catalog did not advance")


def daemon(root: Path) -> Path:
    pointer = json.loads((root / "installs/ecosystem/current").read_text())
    rows = [row for row in pointer["activated"] if row["component"] == "axiom-graphd"]
    if len(rows) != 1:
        raise AssertionError("no unique installed daemon")
    binary = Path(rows[0]["destination"])
    if sha(binary) != rows[0]["sha256"]:
        raise AssertionError("installed daemon bytes changed")
    return binary


def installed_python(root: Path) -> Path:
    pointer = json.loads((root / "mcp-runtime/current.json").read_text())
    version = root / "mcp-runtime/versions" / pointer["generation"]
    executable = version / "venv/bin/axiom-mcp"
    if sha(executable) != pointer["executable_sha256"]:
        raise AssertionError("installed MCP executable bytes changed")
    python = version / "venv/bin/python"
    if not python.is_file():
        raise AssertionError("versioned MCP Python is missing")
    return python


def probe(root: Path, repo: Path, symbol: str, env: dict[str, str], trace: list[dict]) -> dict:
    clean = dict(env)
    clean.pop("PYTHONPATH", None)
    return call(
        [
            str(installed_python(root)),
            str(PROBE),
            "--axiom-home",
            str(root),
            "--repo",
            str(repo),
            "--symbol",
            symbol,
        ],
        clean,
        trace,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-a", required=True, type=Path)
    parser.add_argument("--sha-a", required=True)
    parser.add_argument("--candidate-b", required=True, type=Path)
    parser.add_argument("--sha-b", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if (platform.system(), platform.machine()) != ("Darwin", "x86_64") or os.getuid() == 0:
        raise SystemExit("native non-root Mac x64 required")
    for path, expected in ((args.candidate_a, args.sha_a), (args.candidate_b, args.sha_b)):
        if sha(path) != expected:
            raise SystemExit("candidate archive differs from its owner handoff SHA")
    if LABEL in subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout:
        raise SystemExit("canonical LaunchAgent label is already registered")
    out = args.out.resolve()
    if out.exists():
        raise SystemExit("output already exists")
    with tempfile.TemporaryDirectory(prefix="axiom-k107-native-") as name:
        root_temp = Path(name).resolve()
        a, b = root_temp / "A", root_temp / "B"
        a.mkdir()
        b.mkdir()
        extract(args.candidate_a, a)
        extract(args.candidate_b, b)
        home = root_temp / "home"
        (home / "Library/LaunchAgents").mkdir(parents=True)
        (home / "user-data.txt").write_text("preserve K-107 user data\n")
        root = home / ".local/share/axiom"
        repo = root_temp / "real-source"
        source = repo / "src/TokenSource.cs"
        source.parent.mkdir(parents=True)
        source.write_text(
            'namespace Demo;\npublic class TokenSource { public string Issue() => "ok"; }\n'
        )
        (repo / "Demo.csproj").write_text(
            '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
            "<TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>\n"
        )
        env = {
            **os.environ,
            "HOME": str(home),
            "SHELL": "/bin/zsh",
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        }
        graph_env = dict(env, AXIOM_HOME=str(root))
        trace: list[dict] = []
        try:
            distribution(a, env, trace, "install", True)
            (root / "config/bindings.json").write_text(
                json.dumps({"bindings": {"demo-repo": str(repo)}}) + "\n"
            )
            solution = root_temp / "solution.json"
            solution.write_text(
                json.dumps(
                    {
                        "id": "demo-solution",
                        "profile": "default",
                        "catalog_host_repo": "demo-repo",
                        "projects": [{"id": "demo-project", "repo_id": "demo-repo", "path": "src"}],
                    }
                )
                + "\n"
            )
            engine = home / ".local/bin/axiom"
            call(
                [str(engine), "service", "stop", "--component", "axiom-graphd", "--json"],
                graph_env,
                trace,
            )
            call(
                [
                    str(daemon(root)),
                    "solution",
                    "register",
                    "--config",
                    str(solution),
                    "--apply",
                    "--json",
                ],
                graph_env,
                trace,
            )
            call(
                [str(engine), "service", "start", "--component", "axiom-graphd", "--json"],
                graph_env,
                trace,
            )
            a_generation = catalog(repo)
            a_query = probe(root, repo, "TokenSource", env, trace)
            call(
                [str(engine), "service", "stop", "--component", "axiom-graphd", "--json"],
                graph_env,
                trace,
            )
            call(
                [str(engine), "service", "start", "--component", "axiom-graphd", "--json"],
                graph_env,
                trace,
            )
            restart_query = probe(root, repo, "TokenSource", env, trace)
            distribution(b, env, trace, "update", True)
            source.write_text(source.read_text() + "public sealed class K107WatcherB { }\n")
            b_generation = catalog(repo, a_generation)
            b_query = probe(root, repo, "K107WatcherB", env, trace)
            matrix_dir = root_temp / "process-matrix"
            matrix_env = dict(env)
            matrix_env.pop("PYTHONPATH", None)
            matrix_result = call(
                [
                    str(installed_python(root)),
                    str(MATRIX),
                    "--target",
                    "macos-x64",
                    "--python",
                    str(installed_python(root)),
                    "--installed-repo",
                    str(repo),
                    "--installed-axiom-home",
                    str(root),
                    "--out",
                    str(matrix_dir),
                ],
                matrix_env,
                trace,
            )
            distribution(b, env, trace, "rollback", True)
            rollback_query = probe(root, repo, "K107WatcherB", env, trace)
            if (home / "user-data.txt").read_text() != "preserve K-107 user data\n":
                raise AssertionError("user data changed")
            report = {
                "task_id": "K-107",
                "status": "local_verified",
                "lane": "macos-x64",
                "certified": False,
                "candidate_a_sha256": args.sha_a,
                "candidate_b_sha256": args.sha_b,
                "A_catalog_generation": a_generation,
                "B_catalog_generation": b_generation,
                "installed_query_A": a_query,
                "installed_query_after_restart": restart_query,
                "installed_query_B": b_query,
                "installed_process_matrix": matrix_result,
                "installed_query_after_rollback": rollback_query,
                "user_data_preserved": True,
            }
            distribution(a, env, trace, "uninstall", True)
        finally:
            engine = home / ".local/bin/axiom"
            if engine.is_file():
                for action in ("stop", "uninstall"):
                    subprocess.run(
                        [str(engine), "service", action, "--component", "axiom-graphd", "--json"],
                        env=graph_env,
                        capture_output=True,
                        text=True,
                    )
            subprocess.run(
                ["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
                capture_output=True,
                text=True,
            )
        out.mkdir(parents=True)
        shutil.copytree(matrix_dir, out / "process-matrix")
        (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        (out / "transcript.json").write_text(
            json.dumps(trace, indent=2).replace(name, "<isolated-test-root>") + "\n"
        )
        print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
