"""Record the V2-031 repository gates on the native host under evidence.

``Development.md`` names three required checks for axiom-mcp::

    python -m pytest tests -q
    python -m ruff check .
    python -m ruff format --check .

Each one is executed as a real subprocess from the repository root with its
stdout, stderr and real exit code kept verbatim, and ``SHA256SUMS`` is refreshed
for the target directory afterwards.

Nothing is repaired, reformatted or retried: a gate that fails is recorded as a
failure with its own output. On POSIX an extra, explicitly non-canonical
diagnostic repeats the pytest run with ``tests/test_guard_windows.py`` ignored, so
that a collection error caused by one Windows-only module can be told apart from
the rest of the suite.

Usage::

    python tests/native/capture_gates.py --target windows-x64
    python tests/native/capture_gates.py --target ubuntu-x64 --python /path/to/python3.13
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

NATIVE_DIR = Path(__file__).resolve().parent
REPO_ROOT = NATIVE_DIR.parents[1]

if str(NATIVE_DIR) not in sys.path:
    sys.path.insert(0, str(NATIVE_DIR))

from capture_native_matrix import (  # noqa: E402
    EvidenceWriter,
    identity,
    python_version_of,
    utc_now,
    write_hashes,
)

CANONICAL_GATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pytest", ("-m", "pytest", "tests", "-q")),
    ("ruff-check", ("-m", "ruff", "check", ".")),
    ("ruff-format-check", ("-m", "ruff", "format", "--check", ".")),
)

#: Only meaningful on POSIX, where one Windows-only test module can abort collection.
POSIX_DIAGNOSTIC: tuple[str, tuple[str, ...]] = (
    "pytest-ignoring-windows-guard-tests",
    ("-m", "pytest", "tests", "-q", "--ignore=tests/test_guard_windows.py"),
)

TAIL_LINES = 12


def tail_of(*streams: bytes) -> list[str]:
    """The last lines of each stream, so a summary reads without the bulk."""
    lines: list[str] = []
    for stream in streams:
        text = stream.decode("utf-8", errors="replace")
        lines.extend(line for line in text.splitlines()[-TAIL_LINES:] if line.strip())
    return lines


def run_gate(
    python: str,
    name: str,
    argv: tuple[str, ...],
    evidence: EvidenceWriter,
    *,
    diagnostic: bool,
) -> dict[str, Any]:
    command = " ".join([python, *argv])
    started = time.monotonic()
    completed = subprocess.run(
        [python, *argv],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=False,
    )
    duration = round(time.monotonic() - started, 3)
    return {
        "gate": name,
        "diagnostic": diagnostic,
        "command": command,
        "exit_code": completed.returncode,
        "duration_seconds": duration,
        "tail": tail_of(completed.stdout, completed.stderr),
        "stdout": evidence.stream(f"gates-{name}-stdout", completed.stdout),
        "stderr": evidence.stream(f"gates-{name}-stderr", completed.stderr),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="v2-031-native-gates",
        description="Record the axiom-mcp required checks on this native host.",
    )
    parser.add_argument("--target", required=True, help="Target id, e.g. windows-x64.")
    parser.add_argument("--out", default=None, help="Evidence directory for this target.")
    parser.add_argument("--python", default=sys.executable, help="Interpreter that runs the gates.")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    out = Path(args.out) if args.out is not None else NATIVE_DIR / "evidence" / args.target
    out.mkdir(parents=True, exist_ok=True)
    evidence = EvidenceWriter(out)

    gates = [
        run_gate(args.python, name, gate_argv, evidence, diagnostic=False)
        for name, gate_argv in CANONICAL_GATES
    ]
    if os.name != "nt":
        name, gate_argv = POSIX_DIAGNOSTIC
        gates.append(run_gate(args.python, name, gate_argv, evidence, diagnostic=True))

    canonical_failures = [
        item["gate"] for item in gates if not item["diagnostic"] and item["exit_code"] != 0
    ]
    document = {
        "task": "V2-031",
        "repository": "axiom-mcp",
        "target": args.target,
        "generated_utc": utc_now(),
        "interpreter": args.python,
        "identity": identity(args.target, args.python, python_version_of(args.python)),
        "gates": gates,
        "canonical_failures": canonical_failures,
        "passed": not canonical_failures,
        "note": (
            "Required checks are the three named by Development.md. A non-zero exit is recorded as "
            "a failure, not repaired; a diagnostic entry is not a required check."
        ),
    }
    evidence.document("gates", document)
    sums = write_hashes(out)
    print(
        json.dumps(
            {
                "target": args.target,
                "gate_exit_codes": {item["gate"]: item["exit_code"] for item in gates},
                "canonical_failures": canonical_failures,
                "sha256sums": str(sums),
            }
        )
    )
    return 0 if not canonical_failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
