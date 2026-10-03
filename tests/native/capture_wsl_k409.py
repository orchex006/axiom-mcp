#!/usr/bin/env python3
"""Capture installed MCP query/process/security on an existing non-root WSL2 host.

Use a fresh task-owned home; never modify the existing K-408 installation.
The CLI provisioner and watcher probe are immutable K-408 handoff inputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

NATIVE = Path(__file__).resolve().parent


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", required=True, type=Path)
    parser.add_argument("--kits", required=True, type=Path)
    parser.add_argument("--cli-owner", required=True, type=Path)
    parser.add_argument("--spec-root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if (
        sys.platform != "linux"
        or os.getuid() == 0
        or platform.machine() != "x86_64"
        or "microsoft" not in platform.release().lower()
        or "wsl2" not in platform.release().lower()
    ):
        raise SystemExit("requires actual non-root WSL2 Linux x64 execution")
    args.out.mkdir(parents=True, exist_ok=False)
    home = Path(tempfile.mkdtemp(prefix="axiom-k409-", dir=Path.home()))
    state = home / ".local/share/axiom"
    env = dict(
        os.environ,
        HOME=str(home),
        AXIOM_HOME=str(state),
        AXIOM_CLI_INSTALL_ROOT=str(state),
        PATH="/usr/bin:/bin",
        PYTHONNOUSERSITE="1",
        PIP_CONFIG_FILE=os.devnull,
    )
    env.pop("PYTHONPATH", None)
    records: list[dict] = []

    def scrub(value):
        if isinstance(value, str):
            return value.replace(str(home), "<test-home>").replace(
                str(Path.home()), "<wsl-user-home>"
            )
        if isinstance(value, list):
            return [scrub(row) for row in value]
        if isinstance(value, dict):
            return {key: scrub(row) for key, row in value.items()}
        return value

    def run(name, argv, extra=None, json_body=True):
        command = [str(part) for part in argv]
        result = subprocess.run(
            command,
            env=dict(env, **(extra or {})),
            capture_output=True,
            text=True,
            timeout=300,
        )
        records.append(
            scrub(
                {
                    "id": name,
                    "argv": command,
                    "exit_code": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
            )
        )
        (args.out / "commands.json").write_text(json.dumps(records, indent=2) + "\n")
        print(f"{name}: exit_code={result.returncode}", flush=True)
        if result.returncode:
            raise AssertionError(f"{name}: {(result.stdout + result.stderr)[-1800:]}")
        return json.loads(result.stdout) if json_body else result.stdout

    upstream = json.loads(
        (args.spec_root / "evidence/artifacts/K-408/scope-result.json").read_text()
    )
    expected = {row["sha256"] for row in upstream["artifacts"]}
    # K-408 reused these exact kit/runtime bytes; its runtime report records
    # their consumption, while K-406/K-404 own the immutable input archives.
    prior = json.loads((args.spec_root / "evidence/artifacts/K-406/scope-result.json").read_text())
    expected.update(row["sha256"] for row in prior["artifacts"])
    consumed = []
    for path in sorted(args.inputs.iterdir()):
        if path.is_file():
            digest = sha(path)
            if digest not in expected:
                raise AssertionError(f"input not pinned by K-408: {path.name}")
            consumed.append({"name": path.name, "sha256": digest})
    # Recheck every unpacked kit member against the hash-verified archive.
    for version in ("0.1.0", "0.1.1"):
        kit = args.kits / version
        archive = args.inputs / f"axiom-{version}-container-linux-x64-update-kit.tar.gz"
        with tarfile.open(archive, "r:gz") as source:
            for member in source.getmembers():
                if not member.isfile() or ".." in Path(member.name).parts:
                    raise AssertionError("unsafe candidate kit archive member")
                if (
                    sha(kit / member.name)
                    != hashlib.sha256(source.extractfile(member).read()).hexdigest()
                ):
                    raise AssertionError(f"kit bytes changed: {version}/{member.name}")
    provisioner = args.cli_owner / "packaging/linux/Provision-McpRuntime.py"
    watcher = args.cli_owner / "tests/linux/k406_query_watch.py"
    inputs = {
        "runtime": "cpython-3.13.15-linux-x64-candidate.tar.gz",
        "wheelhouse": "wheelhouse-linux-x64-py313.tar.gz",
        "wheel": "axiom_mcp-0.1.0-py3-none-any.whl",
        "lock": "container-linux-x64-py313-requirements.txt",
    }
    argv = [
        "/usr/bin/python3",
        provisioner,
        "provision",
        "--root",
        state / "mcp-runtime",
        "--version",
        "0.1.0",
        "--source-revision",
        "4ccd3df6eed29a280adb6424d0e595c304cd26e6",
    ]
    for key, name in inputs.items():
        path = args.inputs / name
        argv.extend(["--" + key, path, "--" + key + "-sha256", sha(path)])
    runtime = run("runtime-provision", argv)
    python = runtime["python"]
    env["PATH"] = str(Path(python).parent) + ":/usr/bin:/bin"
    a = args.kits / "0.1.0"
    b = args.kits / "0.1.1"
    cli_a = a / "release/axiom-cli"
    plan = run(
        "engine-install-plan", [cli_a, "install", "--from", a / "release", "--dry-run", "--json"]
    )
    run(
        "engine-install-apply",
        [
            cli_a,
            "install",
            "--from",
            a / "release",
            "--apply",
            "--approve-digest",
            plan["details"]["plan_digest"],
            "--json",
        ],
    )
    install = [
        "/bin/sh",
        a / "Install-AxiomCli.sh",
        "--release-set",
        a / "cli-release/release-set.json",
    ]
    plan = run("cli-install-plan", [*install, "--plan", "--json"])
    run(
        "cli-install-apply",
        [*install, "--apply", "--approve-digest", plan["plan_digest"], "--json"],
    )
    queries = []
    for name, symbol, setup in (
        ("a", "K409WatcherA", True),
        ("restart", "K409WatcherRestart", False),
    ):
        argv = ["/usr/bin/python3", watcher, "--home", home, "--symbol", symbol]
        if setup:
            argv.append("--setup")
        run("watcher-" + name, argv)
        queries.append(
            run(
                "mcp-query-" + name,
                [
                    python,
                    NATIVE / "installed_query_probe.py",
                    "--axiom-home",
                    state,
                    "--repo",
                    home / "demo-repo",
                    "--symbol",
                    symbol,
                ],
            )
        )
    preserved = {
        name: sha(path)
        for name, path in (
            ("bindings", state / "config/bindings.json"),
            ("user-data", home / "user-data.txt"),
        )
    }
    cli_b = b / "release/axiom-cli"
    update_env = {"AXIOM_CLI_COMPOSITE_KIT": str(b)}
    plan_file = home / "plan-b.json"
    plan = run(
        "update-plan",
        [cli_b, "update", "plan", "--to", "0.1.1", "--out", plan_file, "--json"],
        extra=update_env,
    )
    run(
        "update-apply",
        [
            cli_b,
            "update",
            "apply",
            "--plan",
            plan_file,
            "--approve-digest",
            plan["plan_digest"],
            "--json",
        ],
        extra=update_env,
    )
    run("watcher-b", ["/usr/bin/python3", watcher, "--home", home, "--symbol", "K409WatcherB"])
    queries.append(
        run(
            "mcp-query-b",
            [
                python,
                NATIVE / "installed_query_probe.py",
                "--axiom-home",
                state,
                "--repo",
                home / "demo-repo",
                "--symbol",
                "K409WatcherB",
            ],
        )
    )
    run(
        "matrix",
        [
            python,
            NATIVE / "capture_native_matrix.py",
            "--target",
            "wsl2-linux-x64",
            "--task-id",
            "K-409",
            "--python",
            python,
            "--installed-repo",
            home / "demo-repo",
            "--installed-axiom-home",
            state,
            "--installed-symbol",
            "K409WatcherB",
            "--out",
            args.out / "matrix",
        ],
        json_body=False,
    )
    guard = run("native-guard", [python, NATIVE / "installed_guard_probe.py"])
    run(
        "update-rollback",
        [cli_b, "update", "rollback", "--transaction", "previous", "--json"],
        extra=update_env,
    )
    run(
        "watcher-rollback",
        ["/usr/bin/python3", watcher, "--home", home, "--symbol", "K409WatcherRollback"],
    )
    queries.append(
        run(
            "mcp-query-rollback",
            [
                python,
                NATIVE / "installed_query_probe.py",
                "--axiom-home",
                state,
                "--repo",
                home / "demo-repo",
                "--symbol",
                "K409WatcherRollback",
            ],
        )
    )
    after = {
        name: sha(path)
        for name, path in (
            ("bindings", state / "config/bindings.json"),
            ("user-data", home / "user-data.txt"),
        )
    }
    if preserved != after:
        raise AssertionError("restart/update/rollback changed per-user bindings or data")
    report = {
        "task_id": "K-409",
        "lane": "wsl2-linux-x64",
        "status": "local_verified",
        "certified": False,
        "uid": os.getuid(),
        "kernel": platform.release(),
        "distribution": Path("/etc/os-release").read_text(),
        "arch": platform.machine(),
        "runtime": runtime,
        "inputs": consumed,
        "queries": queries,
        "guard": guard,
        "preservation": {"before": preserved, "after": after},
        "source_sha256": sha(home / "demo-repo/src/TokenSource.cs"),
        "harness_sha256": sha(Path(__file__)),
        "cli_probe_sha256": sha(watcher),
        "provisioner_sha256": sha(provisioner),
        "commands": records,
    }
    (args.out / "native-report.json").write_text(json.dumps(scrub(report), indent=2) + "\n")
    print(json.dumps({"status": "local_verified", "uid": os.getuid(), "certified": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
