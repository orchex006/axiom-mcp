#!/usr/bin/env python3
"""Observe installed MCP POSIX guard exclusion across independent processes."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="k407-installed-guard-") as temp:
        guard = Path(temp) / "solution.guard"
        command = [sys.executable, "-m", "axiom_mcp.guard.interop"]
        holder = subprocess.Popen(
            [
                *command,
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
        )
        try:
            acquired = json.loads(holder.stdout.readline())
            if acquired.get("event") != "acquired" or acquired.get("backend") != "posix":
                raise AssertionError(f"installed holder did not acquire POSIX guard: {acquired}")
            refused = subprocess.run(
                [
                    *command,
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
                capture_output=True,
                text=True,
                timeout=10,
            )
            if refused.returncode != 3:
                raise AssertionError(f"shared reader was not refused: {refused.stdout}")
        finally:
            holder.kill()
            holder.wait(timeout=10)
        released = subprocess.run(
            [
                *command,
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
            capture_output=True,
            text=True,
            timeout=10,
        )
        if released.returncode != 0:
            raise AssertionError(f"reader did not acquire after holder crash: {released.stdout}")
        print(
            json.dumps(
                {
                    "status": "passed",
                    "interpreter": sys.executable,
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
