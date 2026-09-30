"""Process-level guard probe: the second process in a two-process test.

This module exists so that exclusion can be observed between two *independent processes*
rather than between two objects in one interpreter. It is test support, not gateway surface:
it opens the declared guard files through the same engine the reader uses, reports what
happened as one JSON line per event, and exits with a code a test can assert on.

Exit codes: ``0`` acquired and released, ``3`` bounded timeout, ``4`` cancelled, ``5`` any
other guard failure, ``6`` usage error.

Usage::

    python -m axiom_mcp.guard.interop hold --dir DIR --locks admission.lock,data.lock \\
        --mode exclusive --hold-ms 2000
    python -m axiom_mcp.guard.interop try  --dir DIR --lock data.lock --mode shared \\
        --timeout-ms 300
    python -m axiom_mcp.guard.interop reader --dir DIR --hold-ms 500
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence

from axiom_mcp.guard.engine import LockMode, SolutionGuard
from axiom_mcp.guard.errors import (
    GuardCancelled,
    GuardError,
    GuardTimeout,
    GuardUnsupported,
)

__all__ = [
    "EXIT_CANCELLED",
    "EXIT_ERROR",
    "EXIT_OK",
    "EXIT_TIMEOUT",
    "EXIT_USAGE",
    "build_parser",
    "main",
]

EXIT_OK = 0
EXIT_TIMEOUT = 3
EXIT_CANCELLED = 4
EXIT_ERROR = 5
EXIT_USAGE = 6


def emit(event: str, **fields: object) -> None:
    """Write one machine-readable event line and flush it, so a parent can wait on it."""
    payload = {"event": event, "pid": os.getpid(), "platform": sys.platform}
    payload.update(fields)
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


def _mode(value: str) -> LockMode:
    return LockMode(value)


def _exit_for(exc: GuardError) -> int:
    if isinstance(exc, GuardTimeout):
        return EXIT_TIMEOUT
    if isinstance(exc, GuardCancelled):
        return EXIT_CANCELLED
    return EXIT_ERROR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="axiom_mcp.guard.interop", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    hold = sub.add_parser("hold", help="acquire guards, announce it, hold, then release")
    hold.add_argument("--dir", required=True)
    hold.add_argument("--locks", default=",".join(("admission.lock", "data.lock")))
    hold.add_argument("--mode", default="exclusive", choices=[m.value for m in LockMode])
    hold.add_argument("--hold-ms", type=int, default=1000)
    hold.add_argument("--timeout-ms", type=int, default=None)

    probe = sub.add_parser("try", help="try to acquire one guard under a bounded wait")
    probe.add_argument("--dir", required=True)
    probe.add_argument("--lock", default="data.lock")
    probe.add_argument("--mode", default="shared", choices=[m.value for m in LockMode])
    probe.add_argument("--timeout-ms", type=int, default=500)

    reader = sub.add_parser("reader", help="hold the reader pair, announcing each step")
    reader.add_argument("--dir", required=True)
    reader.add_argument("--hold-ms", type=int, default=500)
    reader.add_argument("--timeout-ms", type=int, default=None)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    guard = SolutionGuard(args.dir, timeout_ms=getattr(args, "timeout_ms", None))
    if args.command == "hold":
        names = [n for n in args.locks.split(",") if n]
        try:
            for name in names:
                guard.acquire(name, _mode(args.mode))
        except GuardError as exc:
            emit("failed", reason=exc.reason, message=str(exc), exit=EXIT_ERROR)
            guard.release_all()
            return EXIT_ERROR
        emit("acquired", locks=list(guard.held_names), mode=args.mode, backend=guard.backend_name)
        time.sleep(max(args.hold_ms, 0) / 1000.0)
        guard.release_all()
        emit("released", locks=names)
        return EXIT_OK
    if args.command == "try":
        try:
            guard.acquire(args.lock, _mode(args.mode))
        except GuardError as exc:
            emit("failed", reason=exc.reason, message=str(exc), lock=args.lock)
            return _exit_for(exc)
        emit("acquired", locks=list(guard.held_names), mode=args.mode, backend=guard.backend_name)
        time.sleep(0.05)
        guard.release_all()
        emit("released", locks=[args.lock])
        return EXIT_OK
    if args.command == "reader":
        emit("started", locks=list(guard.held_names))
        try:
            with guard.reader():
                emit("acquired", locks=list(guard.held_names), mode="shared")
                time.sleep(max(args.hold_ms, 0) / 1000.0)
        except GuardError as exc:
            emit("failed", reason=exc.reason, message=str(exc))
            return _exit_for(exc)
        emit("released", locks=[])
        return EXIT_OK
    emit("failed", reason="usage", message=f"unknown command {args.command!r}")
    return EXIT_USAGE


def _guarded_main() -> int:  # pragma: no cover - process entry point
    try:
        return main()
    except GuardUnsupported as exc:
        emit("failed", reason=exc.reason, message=str(exc))
        return EXIT_ERROR
    except GuardError as exc:
        emit("failed", reason=exc.reason, message=str(exc))
        return _exit_for(exc)


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(_guarded_main())
