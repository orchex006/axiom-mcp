#!/usr/bin/env python3
"""Prove the installed MCP reader is excluded by graph-core's Windows lock."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

HOLDER_SOURCE = r"""use graph_core::locks::{LockMode, SolutionGuard};
use std::io::Write;
use std::path::Path;
use std::time::Duration;

fn main() {
    let path = std::env::args().nth(1).expect("guard path");
    let _guard = SolutionGuard::acquire(Path::new(&path), LockMode::Exclusive)
        .expect("graph-core Windows exclusive guard");
    println!("{{\"event\":\"acquired\",\"backend\":\"graph-core\"}}");
    std::io::stdout().flush().expect("flush acquisition");
    std::thread::sleep(Duration::from_secs(60));
}
"""


def line_with_deadline(stream: object, seconds: float) -> str:
    result: queue.Queue[str] = queue.Queue(maxsize=1)
    threading.Thread(target=lambda: result.put(stream.readline()), daemon=True).start()
    try:
        return result.get(timeout=seconds)
    except queue.Empty as error:
        raise TimeoutError("Rust holder did not report acquisition") from error


def git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=repo)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graphd-root", type=Path, required=True)
    parser.add_argument("--graphd-source-revision", required=True)
    parser.add_argument("--installed-python", type=Path, required=True)
    parser.add_argument("--axiom-home", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    args = parser.parse_args()
    graphd = args.graphd_root.resolve(strict=True)
    home = args.axiom_home.resolve(strict=True)
    scratch = args.scratch.resolve(strict=True)
    python = args.installed_python.resolve(strict=True)
    if not home.is_relative_to(scratch) or not python.is_relative_to(home):
        raise ValueError("installed Python and Axiom home must remain inside scratch")
    guard = home / "instances/default/solution.guard/data.lock"
    if guard.is_symlink() or not guard.is_file() or not guard.resolve().is_relative_to(home):
        raise ValueError("installed graphd guard file is missing or unsafe")
    revision = args.graphd_source_revision
    if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise ValueError("graphd source revision must be immutable")
    for name in (
        "Cargo.toml",
        "crates/graph-core/src/locks.rs",
        "crates/graph-core/src/locks_windows.rs",
    ):
        if git(graphd, "show", f"{revision}:{name}") != (graphd / name).read_bytes().replace(
            b"\r\n", b"\n"
        ):
            raise ValueError("current graph-core source differs from candidate revision: " + name)
    project = scratch / "k307-rust-holder"
    if project.exists():
        raise ValueError("Rust holder scratch project already exists")
    (project / "src").mkdir(parents=True)
    dependency = (graphd / "crates/graph-core").as_posix()
    (project / "Cargo.toml").write_text(
        '[package]\nname = "k307-rust-holder"\nversion = "0.0.0"\nedition = "2021"\n'
        f'\n[dependencies]\ngraph-core = {{ path = "{dependency}" }}\n',
        encoding="utf-8",
    )
    (project / "src/main.rs").write_text(HOLDER_SOURCE, encoding="utf-8")
    env = os.environ.copy()
    env.update(TEMP=str(scratch), TMP=str(scratch), PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTHONPATH", None)
    build = subprocess.run(
        [
            "cargo",
            "build",
            "--offline",
            "--release",
            "--manifest-path",
            str(project / "Cargo.toml"),
            "--target-dir",
            str(project / "target"),
        ],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    (scratch / "rust-guard-build.txt").write_text(
        f"exit={build.returncode}\nstdout:\n{build.stdout}\nstderr:\n{build.stderr}",
        encoding="utf-8",
    )
    if build.returncode:
        raise RuntimeError("graph-core holder did not build: " + build.stderr[-500:])
    binary = project / "target/release/k307-rust-holder.exe"
    holder = subprocess.Popen(
        [str(binary), str(guard)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )
    try:
        acquired = json.loads(line_with_deadline(holder.stdout, 10))
        if acquired != {"event": "acquired", "backend": "graph-core"}:
            raise AssertionError("Rust holder acquisition report differs")
        refused = subprocess.run(
            [
                str(python),
                "-m",
                "axiom_mcp.guard.interop",
                "try",
                "--dir",
                str(guard.parent),
                "--lock",
                "data.lock",
                "--mode",
                "shared",
                "--timeout-ms",
                "300",
            ],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        if refused.returncode != 3 or "lock_timeout" not in refused.stdout:
            raise AssertionError("installed MCP reader ignored the Rust writer")
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.wait(timeout=10)
        holder.stdout.close()
        holder.stderr.close()
    released = subprocess.run(
        [
            str(python),
            "-m",
            "axiom_mcp.guard.interop",
            "try",
            "--dir",
            str(guard.parent),
            "--lock",
            "data.lock",
            "--mode",
            "shared",
            "--timeout-ms",
            "1000",
        ],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    if released.returncode != 0:
        raise AssertionError("installed MCP reader did not acquire after Rust writer exited")
    print(
        json.dumps(
            {
                "status": "passed",
                "backend": "windows",
                "rust_core_revision": revision,
                "rust_holder_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "holder": acquired,
                "refused_exit_code": refused.returncode,
                "refused": json.loads(refused.stdout),
                "released_exit_code": released.returncode,
                "released": [json.loads(line) for line in released.stdout.splitlines()],
                "installed_python": str(python),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
