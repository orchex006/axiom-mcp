"""JSON-only stdio transport for the axiom-mcp gateway.

On stdio the process has exactly one machine-readable channel: stdout. A single
human-readable line written there corrupts the JSON-RPC stream and the client
sees a parse error instead of an `initialize` result. This module makes that
impossible by construction rather than by review:

* every diagnostic, including the startup banner, is written to stderr;
* ``sys.stdout`` is replaced by :class:`StdoutGuard`, whose text API forwards to
  stderr and counts a violation, while its binary ``buffer`` - the channel the
  official SDK's ``run_stdio_async`` writes protocol frames to - is the only path
  that reaches the real stdout;
* the guard reports ``violations`` and ``protocol_writes`` on shutdown, so the
  evidence shows that stdout carried protocol and nothing else.

The guard is deliberately conservative: it never closes the process stdout, and
it refuses ``fileno()`` so that a stray writer cannot bypass it through a raw
descriptor obtained from the guard object.

No policy is defined here. The tool catalog, the protocol revision and the
transport security of the HTTP surface belong to other modules; this module only
owns which byte stream carries protocol and which carries diagnostics.
"""

from __future__ import annotations

import argparse
import io
import sys
from collections.abc import Sequence
from typing import Any, TextIO

from mcp.server.fastmcp import FastMCP

from axiom_mcp import version

COMPONENT = "axiom-mcp"
BANNER_PREFIX = f"{COMPONENT}: "

__all__ = [
    "BANNER_PREFIX",
    "COMPONENT",
    "Diagnostics",
    "ProtocolBuffer",
    "StdoutGuard",
    "build_stdio_server",
    "banner_lines",
    "main",
    "serve_stdio",
]


class Diagnostics:
    """Human-readable output channel. Always stderr, never stdout."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr

    @property
    def stream(self) -> TextIO:
        return self._stream

    def line(self, text: str) -> None:
        self._stream.write(f"{BANNER_PREFIX}{text}\n")
        self._stream.flush()

    def event(self, name: str, **fields: Any) -> None:
        parts = " ".join(f"{key}={value}" for key, value in sorted(fields.items()))
        self.line(f"{name} {parts}".rstrip())

    def banner(self, *, protocol: str | None = None) -> None:
        for line in banner_lines(protocol=protocol):
            self.line(line)


def banner_lines(*, protocol: str | None = None) -> tuple[str, ...]:
    """Return the startup banner. It is diagnostic text and belongs on stderr."""
    return (
        "axiom-mcp stdio transport",
        f"component={COMPONENT} version={version.VERSION}",
        f"spec_version={version.SPEC_VERSION} spec_revision={version.SPEC_REVISION}",
        f"sdk={version.SDK_PACKAGE}=={version.SDK_PIN} protocol_min={version.MCP_PROTOCOL_MINIMUM}",
        f"protocol={protocol if protocol is not None else version.MCP_PROTOCOL_MINIMUM}",
        "stdout=protocol-only stderr=diagnostics",
    )


def _teardown_flush(stream: Any) -> None:
    """Best-effort flush used only while the guard is being torn down.

    At interpreter exit the process stdout may already be detached or closed, and
    an exception raised from a ``__del__`` is reported as an unraisable error that
    can mask the real exit status. Normal writes stay strict; teardown does not.
    """
    try:
        stream.flush()
    except (ValueError, OSError):
        return


class ProtocolBuffer(io.RawIOBase):
    """Binary passthrough to the real stdout that counts what went through it.

    The official SDK wraps ``sys.stdout.buffer`` in a ``TextIOWrapper`` and
    writes one JSON document per line. Delegating as a ``RawIOBase`` keeps that
    path working unchanged while making the traffic observable.
    """

    def __init__(self, target: Any, guard: StdoutGuard) -> None:
        self._target = target
        self._guard = guard

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def write(self, data: Any) -> int:
        written = self._target.write(data)
        self._guard.protocol_writes += 1
        if isinstance(data, bytes):
            self._guard.protocol_bytes += len(data)
        return written

    def flush(self) -> None:
        self._target.flush()

    def close(self) -> None:
        # The process stdout must survive the guard being replaced, so this never
        # closes the target; a target already closed at teardown is not an error.
        _teardown_flush(self._target)

    def fileno(self) -> int:
        raise io.UnsupportedOperation("fileno is not available on the guarded stdout buffer")


class StdoutGuard(io.TextIOBase):
    """A ``sys.stdout`` replacement that cannot leak text into the protocol stream.

    Text writes are diverted to the diagnostics stream and counted. The binary
    ``buffer`` attribute is the only path to the real stdout, and it is the path
    the pinned SDK transport uses.
    """

    def __init__(self, protocol_buffer: Any, diagnostics: Diagnostics) -> None:
        self._buffer = ProtocolBuffer(protocol_buffer, self)
        self._diagnostics = diagnostics
        self.violations = 0
        self.protocol_writes = 0
        self.protocol_bytes = 0
        self.last_violation: str | None = None

    @property
    def buffer(self) -> ProtocolBuffer:
        return self._buffer

    @property
    def diagnostics(self) -> Diagnostics:
        return self._diagnostics

    @property
    def encoding(self) -> str:
        return "utf-8"

    @property
    def errors(self) -> str:
        return "replace"

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def isatty(self) -> bool:
        return False

    def write(self, data: str) -> int:
        if not data:
            return 0
        self.violations += 1
        self.last_violation = data.rstrip("\n")[:200]
        self._diagnostics.line(f"stdout_write_diverted text={self.last_violation!r}")
        return len(data)

    def writelines(self, lines: Sequence[str]) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        self._diagnostics.stream.flush()
        self._buffer.flush()

    def close(self) -> None:
        # Never close the process stdout; a best-effort flush is the whole job.
        _teardown_flush(self._diagnostics.stream)
        self._buffer.close()

    def fileno(self) -> int:
        raise io.UnsupportedOperation("fileno is not available on the guarded stdout")

    def summary(self) -> dict[str, int]:
        return {
            "violations": self.violations,
            "protocol_writes": self.protocol_writes,
            "protocol_bytes": self.protocol_bytes,
        }


def build_stdio_server(name: str = COMPONENT, **kwargs: Any) -> FastMCP:
    """Build the stdio protocol server.

    The tool catalog is registered by the query layer, not here, so the transport
    slice can be exercised on its own. ``stdio_server_extra`` remains a real MCP
    server: it answers ``initialize`` and reports its tool list.
    """
    kwargs.setdefault("log_level", "WARNING")
    return FastMCP(name=name, **kwargs)


async def serve_stdio(
    server: FastMCP,
    *,
    banner: bool = True,
    diagnostics: Diagnostics | None = None,
    protocol: str | None = None,
) -> int:
    """Run ``server`` on stdio with stdout reserved for protocol frames.

    Returns 0 after the transport closes. The guard is installed for the whole
    session and uninstalled afterwards, and the shutdown event reports the real
    counts so evidence does not have to trust a claim.
    """
    diagnostics = diagnostics if diagnostics is not None else Diagnostics()
    real_stdout = sys.stdout
    guard = StdoutGuard(getattr(real_stdout, "buffer", None) or real_stdout, diagnostics)

    diagnostics.event(
        "stdio_start",
        component=COMPONENT,
        sdk=version.SDK_PIN,
        banner=banner,
    )
    if banner:
        diagnostics.banner(protocol=protocol)

    sys.stdout = guard
    try:
        await server.run_stdio_async()
    finally:
        sys.stdout = real_stdout
        guard.flush()
        diagnostics.event("stdio_stop", **guard.summary())
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry point for the stdio transport.

    The public user-facing command is owned by ``axiom_mcp.cli``; this entry
    exists so the transport can be run and verified on its own
    (``python -m axiom_mcp.stdio``).
    """
    parser = argparse.ArgumentParser(prog="axiom-mcp-stdio", description=__doc__)
    parser.add_argument("--name", default=COMPONENT)
    parser.add_argument("--no-banner", action="store_true")
    args = parser.parse_args(argv)

    import anyio

    server = build_stdio_server(args.name)
    return anyio.run(serve_stdio, server, banner=not args.no_banner)


if __name__ == "__main__":
    raise SystemExit(main())
