"""Injected stdout-noise boundary probe for the axiom-mcp stdio transport.

The stdio transport contract is that stdout carries protocol frames and nothing
else. A stray text write is the classic way that contract breaks - a library, a
logging handler or an accidental ``print`` writes one line to stdout and every
client parse after it fails.

This probe runs the real :func:`axiom_mcp.stdio.serve_stdio` and deliberately
writes plain text to ``sys.stdout`` from a background thread while the JSON-RPC
session is live. The guard must divert those writes to the diagnostics stream and
count them, so the captured stdout still parses as JSON-RPC and the captured
stderr carries one ``stdout_write_diverted`` event per injected line.

It exists so a native process leg can exercise the stdout-noise failure case
without editing the transport that is under test.
"""

from __future__ import annotations

import argparse
import functools
import sys
import threading
import time
from collections.abc import Sequence

import anyio

from axiom_mcp import stdio


def _inject_noise(lines: int, delay: float) -> None:
    """Write plain text to ``sys.stdout`` after the guard has been installed."""
    time.sleep(delay)
    for index in range(lines):
        sys.stdout.write(f"probe-noise {index}\n")
        try:
            sys.stdout.flush()
        except (ValueError, OSError):
            return


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="v2-031-stdio-noise-probe",
        description="Run the real stdio transport while injecting stdout noise.",
    )
    parser.add_argument("--name", default="v2-031-stdio-noise")
    parser.add_argument("--noise-lines", type=int, default=3)
    parser.add_argument("--noise-delay", type=float, default=1.0)
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    server = stdio.build_stdio_server(args.name)
    noise = threading.Thread(
        target=_inject_noise,
        args=(max(1, args.noise_lines), max(0.0, args.noise_delay)),
        name="v2-031-noise",
        daemon=True,
    )
    noise.start()
    return anyio.run(functools.partial(stdio.serve_stdio, server))


if __name__ == "__main__":
    raise SystemExit(main())
