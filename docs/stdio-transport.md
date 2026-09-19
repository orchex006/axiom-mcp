# stdio transport

Owner: `axiom-mcp`. Contract: `repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md` sections 1, 2 and 10, at the pinned
`axiom-specs` revision. This document records implementation, not policy.

## Why this module exists

On stdio the process has exactly one machine-readable channel: **stdout**. The MCP stdio transport reads
newline-delimited JSON-RPC frames from stdin and writes them to stdout. A single human-readable line written to stdout -
a banner, a log line, a progress message - is not "extra output"; it is a protocol corruption that the client reports as
a parse error instead of an `initialize` result. The contract therefore requires that stdio carries **no banner**.

`src/axiom_mcp/stdio.py` makes that impossible by construction rather than by review.

## The two channels

| Stream | Carries | Written by |
|---|---|---|
| stdout | JSON-RPC protocol frames only | `ProtocolBuffer`, reached through `StdoutGuard.buffer` |
| stderr | startup banner, diagnostics, shutdown counters | `Diagnostics` |

`Diagnostics` is the only human-facing writer. `Diagnostics.line` and `Diagnostics.event` always target stderr, and
`Diagnostics.banner` renders `banner_lines()` through it. Nothing in this module ever prints to stdout as text.

## How stdout is protected

`serve_stdio` replaces `sys.stdout` with a `StdoutGuard` for the lifetime of the session:

- **Text writes are diverted.** `StdoutGuard.write` forwards the text to the diagnostics stream, increments
  `violations`, records a bounded `last_violation`, and returns `len(data)`. It never raises, so a stray `print()` cannot
  crash the server - it is recorded instead of silently corrupting the stream.
- **The binary buffer is the only real path.** `StdoutGuard.buffer` is a `ProtocolBuffer`, an `io.RawIOBase` wrapping the
  real stdout buffer. It counts `protocol_writes` and `protocol_bytes`. This is the object the SDK's `stdio_server`
  wraps in a UTF-8 `TextIOWrapper` when `run_stdio_async` starts.
- **`fileno()` is refused** on both the guard and its buffer, so a caller cannot obtain a raw descriptor and write
  around the guard.
- **Teardown is best-effort.** Flushing a stdout already closed at interpreter exit is not an error, but normal writes
  stay strict. The guard never closes the process stdout.

## The pinned SDK path

`FastMCP.run_stdio_async` calls `mcp.server.stdio.stdio_server()`, which - in `mcp==1.28.1`, verified by
`release/stdio_spike.py` - wraps `sys.stdin.buffer` and `sys.stdout.buffer` in `TextIOWrapper(..., encoding="utf-8")`
**at call time**. Because the guard is installed before `run_stdio_async` is awaited, the SDK binds to
`StdoutGuard.buffer`, and every protocol frame flows through `ProtocolBuffer`. The guard is removed in a `finally`
block, so the original `sys.stdout` is restored even when the transport raises.

Two SDK facts this design depends on, both recorded rather than assumed:

1. the SDK re-reads `sys.stdin.buffer` / `sys.stdout.buffer` per call, so replacing `sys.stdout` before the call is
   sufficient - no SDK internals are patched;
2. protocol frames are one JSON document per line, so the spike can prove stdout is protocol-only by parsing every line.

## Observed result

`python release/stdio_spike.py` runs a real `initialize` with in-memory process streams and prints JSON. On the pinned
runtime it records:

```
shutdown_event: axiom-mcp: stdio_stop protocol_bytes=272 protocol_writes=1 violations=0
stdout_line_count: 1        stdout_parse_errors: []      stdout_restored: true
stderr_has_banner: true     initialize_response_protocol: 2025-11-25
diverted_write: {violations: 1, stdout_bytes: "", fileno_error: "UnsupportedOperation"}
```

The banner is present on stderr, stdout held exactly one frame, that frame parsed and negotiated the pinned protocol
revision, the session recorded zero violations, and a deliberate stray text write produced zero stdout bytes while
being counted.

## Ownership boundaries

This module owns **which stream carries protocol and which carries diagnostics**. It does not own:

- the tool catalog or the query data plane - those belong to the query gateway;
- the protocol revision the server negotiates - the SDK does, and the negotiated value is read back, never hardcoded;
- HTTP host, origin or token policy - those belong to `axiom_mcp.security` (C-005);
- the public `axiom-mcp` user-facing command - that belongs to `axiom_mcp.cli` (C-007). `python -m axiom_mcp.stdio`
  exists so the transport can be run and verified on its own.

## Verification

`python -m axiom_mcp.stdio --name <name>` is also the argv a locked launch plan names
(`docs/locked-entrypoints.md`), so `main` is covered end to end by that plan's launch leg as well.

`tests/test_stdio_transport.py` is the targeted regression test. `python -m pytest tests -q` is the required repository
check; `python -m ruff check .` and `python -m ruff format --check .` cover lint and formatting.
