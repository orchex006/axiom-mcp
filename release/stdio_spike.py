"""C-003 verification spike: prove stdout carried protocol and only protocol.

This is not a unit test and not a claim. It runs a real JSON-RPC ``initialize``
through ``axiom_mcp.stdio.serve_stdio`` with the process streams swapped for
in-memory pipes, then reports what actually happened: the banner and diagnostics
on stderr, the protocol frames on stdout, the guard's own counters, and the two
negative facts (a stray text write is diverted, and ``fileno`` is refused).

Run:  python release/stdio_spike.py
"""

from __future__ import annotations

import asyncio
import io
import json
import pathlib
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from axiom_mcp import stdio, version  # noqa: E402

INITIALIZE_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": version.MCP_PROTOCOL_MINIMUM,
        "capabilities": {},
        "clientInfo": {"name": "c-003-spike", "version": version.VERSION},
    },
}
INITIALIZED_NOTIFICATION = {"jsonrpc": "2.0", "method": "notifications/initialized"}


class FakeProcessStream:
    """Stand-in for a process std stream: only the attributes the SDK reads."""

    def __init__(self, buffer: io.BytesIO) -> None:
        self.buffer = buffer


def payload(*messages: dict[str, Any]) -> bytes:
    return "".join(json.dumps(message) + "\n" for message in messages).encode("utf-8")


def probe_diverted_write() -> dict[str, Any]:
    sink = io.BytesIO()
    stderr = io.StringIO()
    guard = stdio.StdoutGuard(sink, stdio.Diagnostics(stderr))
    returned = guard.write("this must never reach stdout\n")
    fileno_error: str | None = None
    try:
        guard.fileno()
    except io.UnsupportedOperation as exc:
        fileno_error = type(exc).__name__
    return {
        "write_returned": returned,
        "stdout_bytes": sink.getvalue().decode("utf-8"),
        "violations": guard.violations,
        "stderr_contains_divert_note": "stdout_write_diverted" in stderr.getvalue(),
        "fileno_error": fileno_error,
    }


def probe_handshake() -> dict[str, Any]:
    original_in, original_out = sys.stdin, sys.stdout
    real_stdout = FakeProcessStream(io.BytesIO())
    stdin = FakeProcessStream(io.BytesIO(payload(INITIALIZE_REQUEST, INITIALIZED_NOTIFICATION)))
    stderr = io.StringIO()
    try:
        sys.stdin, sys.stdout = stdin, real_stdout  # type: ignore[assignment]
        server = stdio.build_stdio_server("c-003-spike")
        exit_code = asyncio.run(
            stdio.serve_stdio(server, banner=True, diagnostics=stdio.Diagnostics(stderr))
        )
    finally:
        sys.stdin, sys.stdout = original_in, original_out

    raw = real_stdout.buffer.getvalue()
    lines = [line for line in raw.decode("utf-8").splitlines() if line]
    frames: list[dict[str, Any]] = []
    parse_errors: list[str] = []
    for line in lines:
        try:
            frames.append(json.loads(line))
        except json.JSONDecodeError as exc:
            parse_errors.append(f"{type(exc).__name__}: {exc}")

    diagnostics = stderr.getvalue()
    shutdown = [line for line in diagnostics.splitlines() if "stdio_stop" in line]
    responses = [frame for frame in frames if frame.get("id") == 1]
    return {
        "exit_code": exit_code,
        "stdout_restored": sys.stdout is original_out,
        "stdout_line_count": len(lines),
        "stdout_parse_errors": parse_errors,
        "stdout_methods": [frame.get("method") or "result" for frame in frames],
        "initialize_response_protocol": (
            responses[0]["result"]["protocolVersion"] if responses else None
        ),
        "initialize_server_name": (
            responses[0]["result"]["serverInfo"]["name"] if responses else None
        ),
        "stderr_has_banner": "axiom-mcp stdio transport" in diagnostics,
        "stderr_line_count": len(diagnostics.splitlines()),
        "shutdown_event": shutdown[0] if shutdown else None,
    }


def main() -> int:
    report = {
        "component": version.COMPONENT,
        "version": version.VERSION,
        "sdk": f"{version.SDK_PACKAGE}=={version.SDK_PIN}",
        "protocol_pinned_minimum": version.MCP_PROTOCOL_MINIMUM,
        "banner_lines": list(stdio.banner_lines(protocol=version.MCP_PROTOCOL_MINIMUM)),
        "handshake": probe_handshake(),
        "diverted_write": probe_diverted_write(),
    }
    handshake = report["handshake"]
    report["stdout_carried_only_protocol"] = bool(
        handshake["stdout_line_count"] >= 1
        and not handshake["stdout_parse_errors"]
        and handshake["initialize_response_protocol"] == version.MCP_PROTOCOL_MINIMUM
        and handshake["stderr_has_banner"] is True
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
