"""C-003 regression test: stdout carries protocol and nothing else.

The positive cases prove the startup banner and every diagnostic land on stderr,
that the binary ``buffer`` is the only path that reaches the real stdout, and
that a full JSON-RPC ``initialize`` over swapped process streams completes with
zero guard violations. The negative and boundary cases prove the guard is
load-bearing: a stray text write is diverted and counted instead of corrupting
the stream, ``fileno()`` is refused so a raw descriptor cannot bypass the guard,
and the original ``sys.stdout`` is restored even when the transport raises.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import pathlib
import signal
import stat
import sys
from collections.abc import Iterator

import pytest

from axiom_mcp import stdio, version

INITIALIZE_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": version.MCP_PROTOCOL_MINIMUM,
        "capabilities": {},
        "clientInfo": {"name": "c-003-test", "version": "0.0.0"},
    },
}
INITIALIZED_NOTIFICATION = {"jsonrpc": "2.0", "method": "notifications/initialized"}


class FakeProcessStream:
    """Stand-in for a process std stream: only the attributes the SDK reads."""

    def __init__(self, buffer: io.BytesIO) -> None:
        self.buffer = buffer


def request_payload(*messages: dict) -> bytes:
    return "".join(json.dumps(message) + "\n" for message in messages).encode("utf-8")


def make_guard(
    diagnostics: stdio.Diagnostics | None = None,
) -> tuple[stdio.StdoutGuard, io.BytesIO, io.StringIO]:
    sink = io.BytesIO()
    stderr = io.StringIO()
    guard = stdio.StdoutGuard(sink, diagnostics or stdio.Diagnostics(stderr))
    return guard, sink, stderr


def install_streams(stdin_bytes: bytes) -> tuple[FakeProcessStream, FakeProcessStream, io.BytesIO]:
    real_stdout = FakeProcessStream(io.BytesIO())
    sys.stdin = FakeProcessStream(io.BytesIO(stdin_bytes))  # type: ignore[assignment]
    sys.stdout = real_stdout  # type: ignore[assignment]
    return real_stdout, sys.stdin, real_stdout.buffer


@pytest.fixture
def restore_streams() -> Iterator[None]:
    original_in, original_out = sys.stdin, sys.stdout
    try:
        yield
    finally:
        sys.stdin, sys.stdout = original_in, original_out


def test_banner_and_diagnostics_are_written_to_the_diagnostics_stream() -> None:
    stderr = io.StringIO()
    diagnostics = stdio.Diagnostics(stderr)

    diagnostics.banner(protocol=version.MCP_PROTOCOL_MINIMUM)

    text = stderr.getvalue()
    assert "axiom-mcp stdio transport" in text
    assert f"version={version.VERSION}" in text
    assert f"protocol={version.MCP_PROTOCOL_MINIMUM}" in text
    assert "stdout=protocol-only" in text
    # The banner is diagnostic text, never a protocol frame.
    assert '"jsonrpc"' not in text


def test_diagnostics_default_to_stderr() -> None:
    assert stdio.Diagnostics().stream is sys.stderr


def test_guard_diverts_text_writes_to_stderr_and_counts_them() -> None:
    guard, sink, stderr = make_guard()

    written = guard.write("hello protocol\n")

    assert written == len("hello protocol\n")
    assert sink.getvalue() == b""
    assert guard.violations == 1
    assert guard.protocol_writes == 0
    assert "stdout_write_diverted" in stderr.getvalue()
    assert guard.last_violation == "hello protocol"


def test_guard_binary_buffer_is_the_only_path_to_stdout() -> None:
    guard, sink, _ = make_guard()

    written = guard.buffer.write(b"{}\n")
    guard.buffer.flush()

    assert written == 3
    assert sink.getvalue() == b"{}\n"
    assert guard.protocol_writes == 1
    assert guard.protocol_bytes == 3
    assert guard.violations == 0


def test_empty_text_write_is_a_noop_and_counts_nothing() -> None:
    guard, sink, _ = make_guard()

    assert guard.write("") == 0
    assert guard.violations == 0
    assert guard.last_violation is None
    assert sink.getvalue() == b""


def test_repeated_violations_are_counted_and_bounded() -> None:
    guard, _, _ = make_guard()

    guard.write("first\n")
    guard.write("x" * 500)

    assert guard.violations == 2
    assert guard.last_violation is not None
    assert len(guard.last_violation) == 200


def test_fileno_is_unavailable_on_the_guard_and_its_buffer() -> None:
    guard, _, _ = make_guard()

    with pytest.raises(io.UnsupportedOperation):
        guard.fileno()
    with pytest.raises(io.UnsupportedOperation):
        guard.buffer.fileno()


def test_summary_reports_the_real_counts() -> None:
    guard, _, _ = make_guard()

    guard.write("stray")
    guard.buffer.write(b"line\n")

    assert guard.summary() == {"violations": 1, "protocol_writes": 1, "protocol_bytes": 5}


def test_full_initialize_handshake_keeps_stdout_protocol_only(restore_streams: None) -> None:
    real_stdout, _, sink = install_streams(
        request_payload(INITIALIZE_REQUEST, INITIALIZED_NOTIFICATION)
    )
    diagnostics = stdio.Diagnostics(io.StringIO())
    server = stdio.build_stdio_server("c-003-test")

    exit_code = asyncio.run(stdio.serve_stdio(server, banner=True, diagnostics=diagnostics))

    assert exit_code == 0
    # The guard is uninstalled: the process stdout is the original object again.
    assert sys.stdout is real_stdout
    assert sink.getvalue(), "the handshake must have produced protocol traffic"
    frames = [json.loads(line) for line in sink.getvalue().decode("utf-8").splitlines() if line]
    responses = [frame for frame in frames if frame.get("id") == 1]
    assert len(responses) == 1
    assert responses[0]["result"]["serverInfo"]["name"] == "c-003-test"
    assert responses[0]["result"]["protocolVersion"] == version.MCP_PROTOCOL_MINIMUM

    text = diagnostics.stream.getvalue()
    assert "axiom-mcp stdio transport" in text
    stop = [line for line in text.splitlines() if "stdio_stop" in line]
    assert len(stop) == 1
    assert "violations=0" in stop[0]
    assert "protocol_writes=0" not in stop[0]


def test_guard_is_restored_when_the_transport_raises(restore_streams: None) -> None:
    real_stdout, _, _ = install_streams(b"")
    diagnostics = stdio.Diagnostics(io.StringIO())

    class ExplodingServer:
        async def run_stdio_async(self) -> None:
            raise RuntimeError("transport failed")

    with pytest.raises(RuntimeError, match="transport failed"):
        asyncio.run(
            stdio.serve_stdio(ExplodingServer(), banner=False, diagnostics=diagnostics)  # type: ignore[arg-type]
        )

    assert sys.stdout is real_stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal delivery is covered by the native matrix")
def test_idle_stdio_server_is_cancelled_when_a_posix_interrupt_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idle receive wait must not postpone a delivered SIGINT indefinitely."""

    class OneInterrupt:
        def __enter__(self) -> OneInterrupt:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def __aiter__(self) -> OneInterrupt:
            return self

        async def __anext__(self) -> signal.Signals:
            return signal.SIGINT

    class IdleServer:
        async def run_stdio_async(self) -> None:
            await asyncio.Event().wait()

    import anyio

    monkeypatch.setattr(anyio, "open_signal_receiver", lambda *signals: OneInterrupt())
    diagnostics = stdio.Diagnostics(io.StringIO())

    asyncio.run(stdio._run_stdio_until_finished(IdleServer(), diagnostics))  # type: ignore[arg-type]

    assert "stdio_interrupt signal=SIGINT" in diagnostics.stream.getvalue()


@pytest.mark.skipif(os.name == "nt", reason="the descriptor reader is POSIX-only")
def test_cancellable_stdin_preserves_split_utf8_lines_and_final_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The native reader must not split a codepoint or drop an unterminated EOF line."""

    class DescriptorStream:
        def fileno(self) -> int:
            return 42

    chunks = iter([b'{"name":"\xe0\xb8', b'\x97\xe0\xb8\x94"}\nlast', b""])

    async def readable(fd: int) -> None:
        assert fd == 42

    monkeypatch.setattr("anyio.wait_readable", readable)
    monkeypatch.setattr(stdio.os, "fstat", lambda fd: type("Stat", (), {"st_mode": stat.S_IFIFO})())
    monkeypatch.setattr(stdio.os, "read", lambda fd, size: next(chunks))
    reader = stdio._CancellableStdin(DescriptorStream())  # type: ignore[arg-type]

    async def collect() -> list[str]:
        return [await reader.__anext__(), await reader.__anext__()]

    assert asyncio.run(collect()) == ['{"name":"ทด"}\n', "last"]
    with pytest.raises(StopAsyncIteration):
        asyncio.run(reader.__anext__())


@pytest.mark.skipif(os.name == "nt", reason="the descriptor reader is POSIX-only")
def test_cancellable_stdin_reads_redirected_regular_file_to_eof(tmp_path: pathlib.Path) -> None:
    """A redirected regular file is always readable without a selector registration."""
    source = tmp_path / "stdin.jsonl"
    source.write_bytes(b'{"name":"\xe0\xb8\x97\xe0\xb8\x94"}\nfinal')
    with source.open("rb") as stream:
        reader = stdio._CancellableStdin(stream)  # type: ignore[arg-type]

        async def collect() -> list[str]:
            return [await reader.__anext__(), await reader.__anext__()]

        assert asyncio.run(collect()) == ['{"name":"ทด"}\n', "final"]
        with pytest.raises(StopAsyncIteration):
            asyncio.run(reader.__anext__())


def test_serve_stdio_without_a_banner_still_reports_shutdown(restore_streams: None) -> None:
    install_streams(b"")
    diagnostics = stdio.Diagnostics(io.StringIO())

    exit_code = asyncio.run(
        stdio.serve_stdio(
            stdio.build_stdio_server("c-003-quiet"), banner=False, diagnostics=diagnostics
        )
    )

    text = diagnostics.stream.getvalue()
    assert exit_code == 0
    assert "axiom-mcp stdio transport" not in text
    assert "stdio_stop" in text


def test_build_stdio_server_names_the_server() -> None:
    server = stdio.build_stdio_server("c-003-named")
    assert server.name == "c-003-named"


def test_banner_lines_never_contain_a_protocol_frame() -> None:
    for line in stdio.banner_lines(protocol=version.MCP_PROTOCOL_MINIMUM):
        assert "jsonrpc" not in line
        assert line.startswith(stdio.BANNER_PREFIX) or line
    assert any("stderr=diagnostics" in line for line in stdio.banner_lines())
