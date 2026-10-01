#!/usr/bin/env python3
"""Prove installed-wheel Windows guard exclusion across native processes."""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path


def line_with_deadline(stream: object, seconds: float) -> str:
    result: queue.Queue[str] = queue.Queue(maxsize=1)
    worker = threading.Thread(target=lambda: result.put(stream.readline()), daemon=True)
    worker.start()
    try:
        return result.get(timeout=seconds)
    except queue.Empty as error:
        raise TimeoutError("installed holder did not report lock acquisition") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--axiom-home", required=True, type=Path)
    args = parser.parse_args()
    home = args.axiom_home.resolve(strict=True)
    guard = home / "instances/default/solution.guard"
    if guard.is_symlink() or not guard.is_dir() or not guard.resolve().is_relative_to(home):
        raise ValueError("graphd's installed native guard directory is missing or unsafe")
    import axiom_mcp

    module = Path(axiom_mcp.__file__).resolve(strict=True)
    if not module.is_relative_to(Path(sys.prefix).resolve(strict=True)):
        raise ValueError("guard module did not load from the installed wheel")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    argv = [sys.executable, "-m", "axiom_mcp.guard.interop"]
    holder = subprocess.Popen(
        [
            *argv,
            "hold",
            "--dir",
            str(guard),
            "--locks",
            "data.lock",
            "--mode",
            "exclusive",
            "--hold-ms",
            "60000",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )
    try:
        raw = line_with_deadline(holder.stdout, 10)
        acquired = json.loads(raw)
        if acquired.get("event") != "acquired" or acquired.get("backend") != "windows":
            raise AssertionError(f"Windows holder did not acquire: {acquired}")
        refused = subprocess.run(
            [
                *argv,
                "try",
                "--dir",
                str(guard),
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
        if refused.returncode != 3:
            raise AssertionError(f"reader was not refused while writer held: {refused.stdout}")
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.wait(timeout=10)
        holder.stdout.close()
        holder.stderr.close()
    released = subprocess.run(
        [
            *argv,
            "try",
            "--dir",
            str(guard),
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
        raise AssertionError(f"reader did not acquire after writer exited: {released.stdout}")
    print(
        json.dumps(
            {
                "status": "passed",
                "backend": "windows",
                "wheel_import_below_venv": True,
                "holder": acquired,
                "refused_exit_code": refused.returncode,
                "refused_output": refused.stdout.strip(),
                "holder_exit_code": holder.returncode,
                "released_exit_code": released.returncode,
                "released_output": released.stdout.strip(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
