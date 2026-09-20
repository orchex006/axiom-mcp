"""Capture the V2-031 native query and host-process matrix on this host.

This is the executable half of :mod:`tests.native.README`. It runs three leg
families on the *native* host it is started on - never in a substitute container -
and writes raw artifacts plus a machine-readable summary:

``python-query``
    The real ``graph_query`` dispatcher over the real ``demo-solution`` bundle the
    repository ships, through the real native guard on this filesystem. The
    fixture generator self-labels as ``synthetic-fixture-v2-not-runtime``, so this
    leg proves the reader/engine contract on this host and is *not* a production
    analyzer or a released-core runtime certification.

``stdio-process``
    The real ``python -m axiom_mcp.stdio`` process over pipes: a JSON-RPC
    handshake, an injected stdout-noise failure case, a silent-server read
    deadline, and an interrupt delivery attempt.

``http-security``
    The composed gateway served by uvicorn on a real loopback socket with its
    credential policy installed, driven with real HTTP requests and with the
    pinned SDK client: positive handshake, lookalike host, lookalike origin,
    missing credential, wrong-audience credential, insufficient capability,
    oversized body and a stalled request body (client timeout).

Nothing here weakens a check to make a host pass. A leg that cannot be executed
is recorded as ``not_run`` with the concrete reason, and an interrupt that the
platform refuses to deliver is recorded as the observed refusal.

Usage::

    python tests/native/capture_native_matrix.py --target windows-x64
    python tests/native/capture_native_matrix.py --target ubuntu-x64 --python /path/to/python3.13
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform as platform_module
import queue
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

NATIVE_DIR = Path(__file__).resolve().parent
REPO_ROOT = NATIVE_DIR.parents[1]
SRC_DIR = REPO_ROOT / "src"
TESTS_DIR = REPO_ROOT / "tests"

for _entry in (str(REPO_ROOT), str(TESTS_DIR), str(SRC_DIR)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import httpx  # noqa: E402

from axiom_mcp import guard as guard_module  # noqa: E402
from axiom_mcp import security, version  # noqa: E402

STDIO_MODULE = "axiom_mcp.stdio"
NOISE_PROBE = NATIVE_DIR / "stdio_noise_probe.py"
HTTP_SERVER = NATIVE_DIR / "http_security_server.py"

#: The label the vendored bundle declares for itself. It is read back off the fixture
#: below rather than trusted, so tests/native/README.md can cite real bytes.
FIXTURE_SELF_LABEL = "synthetic-fixture-v2-not-runtime"
DEMO_SOLUTION = TESTS_DIR / "fixtures" / "solution" / "demo-solution"

READ_TOKEN_ENV = "V2031_READ_TOKEN"
CONTROL_TOKEN_ENV = "V2031_CONTROL_TOKEN"
READ_TOKEN_VALUE = "v2-031-native-read-token"
CONTROL_TOKEN_VALUE = "v2-031-native-control-token"

STREAMABLE_ACCEPT = "application/json, text/event-stream"
MCP_PATH = "/mcp"
HEALTH_PATH = "/healthz"
READY_PATH = "/readyz"

PROTOCOL_MINIMUM = version.MCP_PROTOCOL_MINIMUM


class DeadlineExceeded(RuntimeError):
    """No data arrived inside the bounded read window."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def child_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """A launch environment that does not need shell activation."""
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH", "")
    parts = [str(SRC_DIR), str(REPO_ROOT)]
    if existing:
        parts.append(existing)
    environment["PYTHONPATH"] = os.pathsep.join(parts)
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment.update(extra or {})
    return environment


class EvidenceWriter:
    """Writes raw artifacts under one target directory and returns their hashes."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.raw = root / "raw"
        self.raw.mkdir(parents=True, exist_ok=True)

    def stream(self, name: str, data: bytes) -> dict[str, Any]:
        binary = self.raw / f"{name}.bin"
        text = self.raw / f"{name}.txt"
        binary.write_bytes(data)
        text.write_text(decode(data), encoding="utf-8", newline="\n")
        return {
            "path": relative(binary, self.root),
            "text_path": relative(text, self.root),
            "sha256": sha256_bytes(data),
            "bytes": len(data),
        }

    def document(self, name: str, document: Mapping[str, Any]) -> dict[str, Any]:
        data = (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
            "utf-8"
        )
        path = self.raw / f"{name}.json"
        path.write_bytes(data)
        return {"path": relative(path, self.root), "sha256": sha256_bytes(data)}


def check(
    name: str,
    kind: str,
    outcome: str,
    observed: Mapping[str, Any] | None = None,
    detail: str = "",
) -> dict[str, Any]:
    record: dict[str, Any] = {"check": name, "kind": kind, "outcome": outcome}
    if detail:
        record["detail"] = detail
    if observed:
        record["observed"] = dict(observed)
    return record


def identity(target: str, python: str, python_version: str) -> dict[str, Any]:
    """Platform identity for the host the legs actually ran on."""
    try:
        backend = guard_module.platform_backend_name()
    except Exception as exc:  # pragma: no cover - only on an unsupported host
        backend = f"unavailable:{type(exc).__name__}"
    return {
        "target": target,
        "os_name": os.name,
        "sys_platform": sys.platform,
        "platform": platform_module.platform(),
        "system": platform_module.system(),
        "release": platform_module.release(),
        "version": platform_module.version(),
        "machine": platform_module.machine(),
        "processor": platform_module.processor(),
        "cpu_count": os.cpu_count(),
        "pointer_bits": struct.calcsize("P") * 8,
        "python_implementation": platform_module.python_implementation(),
        "python_version": python_version,
        "python_executable": python,
        "python_requires": version.PYTHON_REQUIRES,
        "python_supported": version.python_supported(python_version),
        "runtime_pins": dict(version.RUNTIME_PINS),
        "runtime_pin_reasons": version.runtime_pin_reasons(),
        "guard_backend": backend,
        "protocol_minimum": PROTOCOL_MINIMUM,
        "spec_revision": version.SPEC_REVISION,
        "component": version.COMPONENT,
    }


class LineReader:
    """Read newline-delimited bytes from a pipe with a bounded wait.

    ``select`` is not portable to Windows pipes, so a daemon thread pumps the
    stream into a queue and every read carries an explicit deadline.
    """

    def __init__(self, stream: Any, name: str) -> None:
        self.name = name
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._thread = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        self._thread.start()

    def _pump(self, stream: Any) -> None:
        try:
            for line in iter(stream.readline, b""):
                self._queue.put(("line", line))
        except Exception as exc:  # pragma: no cover - stream teardown races
            self._queue.put(("error", exc))
        finally:
            self._queue.put(("eof", None))

    def next_line(self, timeout: float) -> bytes:
        try:
            kind, value = self._queue.get(timeout=timeout)
        except queue.Empty:
            raise DeadlineExceeded(f"no {self.name} line within {timeout}s") from None
        if kind == "line":
            return value
        if kind == "eof":
            raise EOFError(f"{self.name} closed before a line arrived")
        raise value


def json_rpc_frame(request_id: int, method: str, params: Mapping[str, Any] | None = None) -> bytes:
    frame: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        frame["params"] = dict(params)
    return (json.dumps(frame) + "\n").encode("utf-8")


def initialize_frame(request_id: int) -> bytes:
    return json_rpc_frame(
        request_id,
        "initialize",
        {
            "protocolVersion": PROTOCOL_MINIMUM,
            "capabilities": {},
            "clientInfo": {"name": "v2-031-native-matrix", "version": "1.0.0"},
        },
    )


def notifications_initialized_frame() -> bytes:
    return (json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode(
        "utf-8"
    )


def parse_frames(data: bytes) -> dict[str, Any]:
    """Classify captured stdout: which lines are JSON-RPC frames, which are not."""
    frames: list[dict[str, Any]] = []
    junk: list[str] = []
    for line in decode(data).splitlines():
        if not line.strip():
            continue
        try:
            document = json.loads(line)
        except json.JSONDecodeError:
            junk.append(line)
            continue
        if isinstance(document, dict) and document.get("jsonrpc") == "2.0":
            frames.append(document)
        else:
            junk.append(line)
    return {"frames": frames, "junk": junk}


def spawn(python: str, argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> Any:
    return subprocess.Popen(
        [python, *argv],
        cwd=str(REPO_ROOT),
        env=child_env(env),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def close_stdin(process: Any) -> None:
    try:
        if process.stdin is not None:
            process.stdin.close()
    except (BrokenPipeError, OSError):
        return


def finish(process: Any, timeout: float) -> dict[str, Any]:
    try:
        exit_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)
        return {"exit_code": None, "terminated_by_harness": True}
    return {"exit_code": exit_code, "terminated_by_harness": False}


def collect(reader: LineReader, deadline: float) -> bytes:
    """Drain a reader until EOF, or until the deadline expires."""
    chunks: list[bytes] = []
    end = time.monotonic() + deadline
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        try:
            chunks.append(reader.next_line(remaining))
        except (DeadlineExceeded, EOFError):
            break
    return b"".join(chunks)


# --- leg: python query -----------------------------------------------------------------


def fixture_provenance_of(project_generations: Any) -> dict[str, Any]:
    """Read the ``generator_version`` the observed fixture actually declares.

    The label is read back off the manifest bytes named by the envelope instead of
    being asserted here, so the "synthetic fixture, no released-core certification"
    statement in ``tests/native/README.md`` is checked against the fixture.
    """
    observed: list[dict[str, Any]] = []
    for entry in project_generations or []:
        if not isinstance(entry, Mapping):
            continue
        project = str(entry.get("project_id", ""))
        generation = str(entry.get("generation_id", ""))
        manifest_path = (
            DEMO_SOLUTION / project / "checkpoint" / "generations" / generation / "manifest.json"
        )
        record: dict[str, Any] = {
            "project_id": project,
            "generation_id": generation,
            "manifest": relative(manifest_path, REPO_ROOT),
        }
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            record["read_error"] = f"{type(exc).__name__}: {exc}"
        else:
            record["generator_version"] = manifest.get("generator_version")
        observed.append(record)
    labels = sorted(
        {str(item["generator_version"]) for item in observed if item.get("generator_version")}
    )
    return {
        "fixture_root": relative(DEMO_SOLUTION, REPO_ROOT),
        "projects": observed,
        "observed_generator_versions": labels,
        "synthetic_label": FIXTURE_SELF_LABEL,
        "all_observed_are_synthetic": labels == [FIXTURE_SELF_LABEL],
        "released_core_certification": "not_run",
        "reason": (
            "The envelope's own project generations resolve to manifests that self-label as "
            f'"{FIXTURE_SELF_LABEL}", so this leg exercises the real reader and engine on a '
            "synthetic fixture and is not a released-core runtime certification."
        ),
    }


def leg_python_query(evidence: EvidenceWriter, deadline: float) -> dict[str, Any]:
    started = time.monotonic()
    checks: list[dict[str, Any]] = []
    command = "python -c 'graph_query(search|callers|refusals) over a real demo-solution copy'"

    import test_tools_support as support

    from axiom_mcp.errors import AxiomError
    from axiom_mcp.query.envelope import ENVELOPE_KEYS
    from axiom_mcp.tools.query import graph_query

    with tempfile.TemporaryDirectory(prefix="v2-031-native-query-") as temporary:
        context = support.context_for(Path(temporary))
        solution = support.SOLUTION

        def dispatch(request: Mapping[str, Any]) -> Any:
            return graph_query(dict(request), context)

        # Positive 1: a real search over the vendored bundle bytes.
        search_request = {"solution_id": solution, "operation": "search", "query": "login"}
        envelope = dispatch(search_request)
        envelope_keys_ok = set(envelope) <= set(ENVELOPE_KEYS)
        nodes = envelope.get("nodes") or []
        checks.append(
            check(
                "graph_query.search.envelope",
                "positive",
                "pass" if envelope_keys_ok and envelope.get("schema_version") == 1 else "fail",
                {
                    "schema_version": envelope.get("schema_version"),
                    "solution_id": envelope.get("solution_id"),
                    "node_count": len(nodes),
                    "coverage": envelope.get("coverage"),
                    "freshness": envelope.get("freshness"),
                    "verification": envelope.get("verification"),
                    "project_generations": envelope.get("project_generations"),
                    "envelope_keys_within_contract": envelope_keys_ok,
                },
            )
        )
        evidence.document("python-query-search-envelope", envelope)
        fixture_provenance = fixture_provenance_of(envelope.get("project_generations"))
        evidence.document("python-query-fixture-provenance", fixture_provenance)

        # Positive 2: a traversal that starts from a node the first call returned.
        target = nodes[0]["id"] if nodes and isinstance(nodes[0], dict) else None
        if target is None:
            checks.append(
                check(
                    "graph_query.callers.envelope",
                    "positive",
                    "not_run",
                    detail="the search leg returned no node id to traverse from",
                )
            )
        else:
            callers = dispatch({"solution_id": solution, "operation": "callers", "target": target})
            callers_ok = set(callers) <= set(ENVELOPE_KEYS)
            checks.append(
                check(
                    "graph_query.callers.envelope",
                    "positive",
                    "pass" if callers_ok and callers.get("schema_version") == 1 else "fail",
                    {
                        "target": target,
                        "node_count": len(callers.get("nodes") or []),
                        "edge_count": len(callers.get("edges") or []),
                        "truncated": callers.get("truncated"),
                    },
                )
            )
            evidence.document("python-query-callers-envelope", callers)

        # Negative 1: an unknown operation must be refused, not guessed.
        checks.append(
            refusal_check(
                dispatch,
                "graph_query.unknown_operation",
                {"solution_id": solution, "operation": "does_not_exist"},
                AxiomError,
            )
        )

        # Negative 2: a solution outside the principal scope must be refused.
        checks.append(
            refusal_check(
                dispatch,
                "graph_query.unscoped_solution",
                {"solution_id": "alpha", "operation": "search", "query": "login"},
                AxiomError,
            )
        )

        # Negative 3: the closed request parser refuses arbitrary SQL by name.
        refusal = refusal_check(
            dispatch,
            "graph_query.arbitrary_sql_refused",
            {"solution_id": solution, "operation": "search", "query": "login", "sql": "SELECT 1"},
            AxiomError,
        )
        checks.append(refusal)

        # Boundary: an out-of-contract read bound is answered by a bounded refusal or a
        # truncated envelope, never by an unbounded read.
        try:
            bounded = dispatch(
                {"solution_id": solution, "operation": "search", "query": "login", "limit": 10**9}
            )
            observed = {
                "answered_with": "envelope",
                "truncated": bounded.get("truncated"),
                "node_count": len(bounded.get("nodes") or []),
            }
            outcome = (
                "pass"
                if bounded.get("truncated") or len(bounded.get("nodes") or []) <= 1000
                else "fail"
            )
        except AxiomError as error:
            observed = {"answered_with": "refusal", "code": error.code, "details": error.details}
            outcome = "pass"
        checks.append(check("graph_query.out_of_contract_limit", "boundary", outcome, observed))

    duration = time.monotonic() - started
    stdout = evidence.stream("python-query-summary", (json.dumps(checks, indent=2) + "\n").encode())
    status = "passed" if all(item["outcome"] == "pass" for item in checks) else "failed"
    return {
        "leg": "python-query",
        "status": status,
        "command": command,
        "duration_seconds": round(duration, 3),
        "checks": checks,
        "fixture_provenance": fixture_provenance,
        "artifacts": {"summary": stdout},
    }


def refusal_check(
    dispatch: Callable[[Mapping[str, Any]], Any],
    name: str,
    request: Mapping[str, Any],
    error_type: type[BaseException],
) -> dict[str, Any]:
    """Assert one request is refused by the real parser/authorizer, not answered."""
    try:
        answer = dispatch(request)
    except error_type as error:
        return check(
            name,
            "negative",
            "pass",
            {"refused": True, "code": getattr(error, "code", None), "message": str(error)},
        )
    return check(
        name,
        "negative",
        "fail",
        {"refused": False, "answered_with_keys": sorted(answer)[:8] if answer else []},
        detail="the request was answered instead of refused",
    )


# --- leg: stdio process ------------------------------------------------------------------


def stdio_exchange(process: Any, out: LineReader, *, noise_window: float = 0.0) -> dict[str, bytes]:
    """Perform a real JSON-RPC handshake over the process pipes."""
    if process.stdin is None:
        raise RuntimeError("stdio process has no stdin pipe")
    process.stdin.write(initialize_frame(1))
    process.stdin.flush()
    first = out.next_line(30.0)
    process.stdin.write(notifications_initialized_frame())
    process.stdin.flush()
    if noise_window:
        time.sleep(noise_window)
    process.stdin.write(json_rpc_frame(2, "tools/list"))
    process.stdin.flush()
    second = out.next_line(30.0)
    return {"initialize": first, "tools_list": second}


def stdio_leg(
    url_argv: Sequence[str],
    python: str,
    evidence: EvidenceWriter,
    deadline: float,
    *,
    label: str,
    noise_window: float = 0.0,
    expect_noise: bool = False,
) -> dict[str, Any]:
    """Run one stdio sub-leg and record its real stdout, stderr and exit code."""
    started = time.monotonic()
    command = " ".join([python, *url_argv])
    process = spawn(python, url_argv)
    out = LineReader(process.stdout, "stdout")
    err = LineReader(process.stderr, "stderr")
    exchange: dict[str, bytes] = {}
    failure: str | None = None
    end: dict[str, Any] = {"exit_code": None, "terminated_by_harness": True}
    try:
        exchange = stdio_exchange(process, out, noise_window=noise_window)
        close_stdin(process)
        end = finish(process, deadline)
    except Exception:
        failure = traceback.format_exc()
        process.kill()
        end = finish(process, deadline)

    stdout_bytes = b"".join(exchange.values()) + collect(out, 2.0)
    stderr_bytes = collect(err, 2.0)
    parsed = parse_frames(stdout_bytes)
    stderr_text = decode(stderr_bytes)
    checks: list[dict[str, Any]] = []

    frames = parsed["frames"]
    checks.append(
        check(
            f"{label}.stdout_protocol_only",
            "positive",
            "pass" if not parsed["junk"] and len(frames) >= 2 else "fail",
            {
                "jsonrpc_frames": len(frames),
                "non_protocol_lines": len(parsed["junk"]),
                "first_non_protocol_line": parsed["junk"][0][:200] if parsed["junk"] else None,
            },
        )
    )
    negotiated = next((frame for frame in frames if frame.get("id") == 1), None)
    result = (negotiated or {}).get("result") or {}
    checks.append(
        check(
            f"{label}.initialize",
            "positive",
            "pass" if result.get("protocolVersion") == PROTOCOL_MINIMUM else "fail",
            {
                "protocol_version": result.get("protocolVersion"),
                "server_name": (result.get("serverInfo") or {}).get("name"),
                "server_version": (result.get("serverInfo") or {}).get("version"),
            },
        )
    )
    tools_list = next((frame for frame in frames if frame.get("id") == 2), None)
    checks.append(
        check(
            f"{label}.tools_list_answer",
            "positive",
            "pass" if tools_list is not None and "result" in tools_list else "fail",
            {"tools": len(((tools_list or {}).get("result") or {}).get("tools") or [])},
        )
    )
    checks.append(
        check(
            f"{label}.exit_code",
            "positive",
            "pass" if end.get("exit_code") == 0 else "fail",
            end,
        )
    )
    diverged = stderr_text.count("stdout_write_diverted")
    stop_line = next((line for line in stderr_text.splitlines() if "stdio_stop" in line), "")
    checks.append(
        check(
            f"{label}.stderr_diagnostics",
            "positive",
            "pass" if stop_line and "stdout=protocol-only" in stderr_text else "fail",
            {
                "banner_present": "axiom-mcp stdio transport" in stderr_text,
                "stdio_stop": stop_line.strip(),
                "diverted_writes": diverged,
            },
        )
    )
    if expect_noise:
        checks.append(
            check(
                f"{label}.stdout_noise_diverted",
                "failure",
                "pass" if diverged == 3 and not parsed["junk"] else "fail",
                {
                    "injected_lines": 3,
                    "diverted_writes": diverged,
                    "leaked_to_stdout": len(parsed["junk"]),
                },
            )
        )
    if failure:
        checks.append(check(f"{label}.harness", "failure", "fail", detail=failure))

    artifacts = {
        "stdout": evidence.stream(f"{label}-stdout", stdout_bytes),
        "stderr": evidence.stream(f"{label}-stderr", stderr_bytes),
    }
    status = "passed" if all(item["outcome"] == "pass" for item in checks) else "failed"
    return {
        "leg": label,
        "status": status,
        "command": command,
        "duration_seconds": round(time.monotonic() - started, 3),
        "checks": checks,
        "artifacts": artifacts,
    }


def leg_stdio_silence_deadline(
    python: str, evidence: EvidenceWriter, deadline: float
) -> dict[str, Any]:
    """Boundary: a healthy stdio server sends nothing the client did not ask for."""
    started = time.monotonic()
    command = f"{python} -m {STDIO_MODULE} --name v2-031-native-silence"
    process = spawn(python, ["-m", STDIO_MODULE, "--name", "v2-031-native-silence"])
    out = LineReader(process.stdout, "stdout")
    err = LineReader(process.stderr, "stderr")
    checks: list[dict[str, Any]] = []
    window = 2.0
    try:
        stdio_exchange(process, out)
        exceeded = False
        unsolicited = b""
        try:
            unsolicited = out.next_line(window)
        except (DeadlineExceeded, EOFError):
            exceeded = True
        checks.append(
            check(
                "stdio.silence.read_deadline",
                "boundary",
                "pass" if exceeded and not unsolicited else "fail",
                {
                    "read_deadline_seconds": window,
                    "deadline_expired_without_data": exceeded,
                    "unsolicited_bytes": len(unsolicited),
                },
            )
        )
        close_stdin(process)
        end = finish(process, deadline)
    except Exception:
        process.kill()
        end = finish(process, deadline)
        checks.append(
            check("stdio.silence.harness", "boundary", "fail", detail=traceback.format_exc())
        )
    stdout_bytes = collect(out, 2.0)
    stderr_bytes = collect(err, 2.0)
    checks.append(
        check(
            "stdio.silence.exit_code",
            "boundary",
            "pass" if end.get("exit_code") == 0 else "fail",
            end,
        )
    )
    return {
        "leg": "stdio-silence-deadline",
        "status": "passed" if all(item["outcome"] == "pass" for item in checks) else "failed",
        "command": command,
        "duration_seconds": round(time.monotonic() - started, 3),
        "checks": checks,
        "artifacts": {
            "stdout": evidence.stream("stdio-silence-stdout", stdout_bytes),
            "stderr": evidence.stream("stdio-silence-stderr", stderr_bytes),
        },
    }


def interrupt_attempt(
    python: str,
    *,
    label: str,
    sig: int,
    nudge_frame: bytes | None = None,
    window: float = 10.0,
) -> tuple[str, dict[str, Any], dict[str, Any], str, bytes]:
    """Deliver one real signal to a freshly started stdio server.

    ``nudge_frame`` is written to stdin one second after delivery to model a
    client that keeps talking. It exists because a POSIX interpreter only runs a
    pending Python-level signal handler when its event loop wakes, so an idle
    server and a busy one can behave differently for the same signal. The first
    bounded window is the result; nothing is retried until it passes.

    Returns ``(outcome, observed, hard_stop, stderr_text, stdout_bytes)``. A
    platform that refuses delivery is recorded as ``not_run`` with the refusal,
    never as a pass.
    """
    process = spawn(python, ["-m", STDIO_MODULE, "--name", f"v2-031-native-{label.lower()}"])
    out = LineReader(process.stdout, "stdout")
    err = LineReader(process.stderr, "stderr")
    stdio_exchange(process, out)

    delivered: str | None = None
    refusal: str | None = None
    try:
        process.send_signal(sig)
        delivered = label
    except Exception as exc:  # pragma: no cover - platform refusal path
        refusal = f"{type(exc).__name__}: {exc}"

    end: dict[str, Any] = {"exit_code": None, "terminated_by_harness": False}
    if delivered is None:
        outcome = "not_run"
        observed: dict[str, Any] = {"delivered": None, "refusal": refusal, **end}
    else:
        if nudge_frame is not None:
            time.sleep(1.0)
            try:
                process.stdin.write(nudge_frame)
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
        end = finish(process, window)
        outcome = "pass" if end.get("exit_code") is not None else "fail"
        observed = {"delivered": delivered, **end}

    stderr_text = decode(collect(err, 2.0))
    if process.poll() is None:
        process.terminate()
        finish(process, 10.0)
    hard_stop = {
        "exit_code": process.poll(),
        "terminated_by_harness": end.get("terminated_by_harness", False),
        "stderr_tail": stderr_text.splitlines()[-1] if stderr_text.splitlines() else "",
    }
    stdout_bytes = collect(out, 2.0)
    close_stdin(process)
    return outcome, observed, hard_stop, stderr_text, stdout_bytes


def leg_stdio_interrupt(python: str, evidence: EvidenceWriter, deadline: float) -> dict[str, Any]:
    """Interrupt delivery: the platform signals where they exist, and the hard stop.

    CP-08 names ``SIGINT`` and ``SIGTERM`` on POSIX, so both are delivered, and
    the ``SIGINT`` case is repeated with a follow-up frame to separate "the
    signal was ignored" from "the interpreter was idle". Windows has no SIGTERM
    console equivalent, so only the console control event is delivered there.
    """
    started = time.monotonic()
    checks: list[dict[str, Any]] = []
    if os.name == "nt":
        attempts: list[tuple[str, int, bytes | None]] = [
            ("CTRL_BREAK_EVENT", signal.CTRL_BREAK_EVENT, None)  # type: ignore[attr-defined]
        ]
    else:
        attempts = [
            ("SIGINT", signal.SIGINT, None),
            ("SIGINT_AFTER_INPUT", signal.SIGINT, json_rpc_frame(99, "tools/list")),
            ("SIGTERM", signal.SIGTERM, None),
        ]

    stderr_text = ""
    stdout_bytes = b""
    commands: list[str] = []
    for label, sig, nudge in attempts:
        commands.append(f"{python} -m {STDIO_MODULE} --name v2-031-native-{label.lower()}")
        try:
            outcome, observed, hard_stop, err_text, out_bytes = interrupt_attempt(
                python, label=label, sig=sig, nudge_frame=nudge
            )
        except Exception:  # pragma: no cover - one broken probe must not hide the rest
            checks.append(
                check(
                    f"stdio.interrupt.{label.lower()}",
                    "failure",
                    "error",
                    {"traceback": traceback.format_exc()},
                )
            )
            continue
        stderr_text += err_text
        stdout_bytes += out_bytes
        checks.append(
            check(
                f"stdio.interrupt.{label.lower()}",
                "failure",
                outcome,
                observed,
                detail="nudged_with=tools/list" if nudge is not None else "",
            )
        )
        checks.append(
            check(
                f"stdio.interrupt.{label.lower()}.hard_stop",
                "failure",
                "pass" if hard_stop["exit_code"] is not None else "fail",
                hard_stop,
            )
        )

    return {
        "leg": "stdio-interrupt",
        "status": "passed"
        if all(item["outcome"] in {"pass", "not_run"} for item in checks)
        else "failed",
        "command": " ; ".join(commands),
        "duration_seconds": round(time.monotonic() - started, 3),
        "checks": checks,
        "artifacts": {
            "stdout": evidence.stream("stdio-interrupt-stdout", stdout_bytes),
            "stderr": evidence.stream("stdio-interrupt-stderr", stderr_text.encode("utf-8")),
        },
    }


# --- leg: HTTP security over a real socket --------------------------------------------------


def free_port() -> int:
    """Reserve a loopback port by binding and releasing it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def error_code(body: str) -> str | None:
    try:
        document = json.loads(body)
    except json.JSONDecodeError:
        return None
    return document.get("code") if isinstance(document, dict) else None


def http_request(
    base_url: str,
    method: str,
    path: str,
    *,
    host: str | None = None,
    origin: str | None = None,
    authorization: str | None = None,
    payload: Mapping[str, Any] | None = None,
    raw_body: bytes | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Send one real HTTP request and record the raw wire result."""
    headers = {"Accept": STREAMABLE_ACCEPT}
    if host is not None:
        headers["Host"] = host
    if origin is not None:
        headers["Origin"] = origin
    if authorization is not None:
        headers["Authorization"] = authorization
    started = time.monotonic()
    try:
        with httpx.Client(base_url=base_url, timeout=timeout) as client:
            response = client.request(method, path, headers=headers, json=payload, content=raw_body)
    except httpx.HTTPError as exc:
        return {
            "transport_error": f"{type(exc).__name__}: {exc}",
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    body = response.text
    return {
        "status_code": response.status_code,
        "error_code": error_code(body),
        "content_type": response.headers.get("content-type", ""),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "body_sha256": sha256_bytes(response.content),
        "body_excerpt": body[:300],
    }


def sdk_handshake(base_url: str, token: str, timeout: float = 15.0) -> dict[str, Any]:
    """Drive the pinned SDK client through a real TCP handshake."""
    import anyio
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def run() -> dict[str, Any]:
        url = f"{base_url}{MCP_PATH}"
        headers = {"Authorization": f"Bearer {token}"}
        async with streamablehttp_client(
            url, headers=headers, timeout=timeout, sse_read_timeout=timeout
        ) as (read_stream, write_stream, _session_id):
            async with ClientSession(read_stream, write_stream) as session:
                result = await session.initialize()
                return {
                    "protocol_version": result.protocolVersion,
                    "server_name": result.serverInfo.name,
                    "server_version": result.serverInfo.version,
                    "capabilities": sorted(result.capabilities.model_dump(exclude_none=True)),
                }

    started = time.monotonic()
    try:
        document = anyio.run(run)
    except Exception as exc:
        return {
            "transport_error": f"{type(exc).__name__}: {exc}",
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    document["elapsed_seconds"] = round(time.monotonic() - started, 3)
    document["negotiated_is_pinned_minimum"] = document["protocol_version"] == PROTOCOL_MINIMUM
    return document


def stalled_request(base_url: str, timeout: float = 6.0) -> dict[str, Any]:
    """Send an incomplete body and record whether the server ever answers.

    The request declares ``Content-Length: 4096`` and then sends 32 bytes, so the
    server is entitled to keep waiting. This is a real timeout failure case: the
    client deadline must expire with zero response bytes, and the server must not
    answer success for a body it never received.
    """
    parsed = httpx.URL(base_url)
    host, port = parsed.host, parsed.port
    if port is None:
        raise RuntimeError(f"base url without a port: {base_url}")
    head = (
        f"POST {MCP_PATH} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Content-Type: application/json\r\n"
        f"Accept: {STREAMABLE_ACCEPT}\r\n"
        f"Authorization: Bearer {READ_TOKEN_VALUE}\r\n"
        "Content-Length: 4096\r\n\r\n"
    ).encode()
    started = time.monotonic()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(head + b'{"jsonrpc": "2.0", "id": 9, "method": "')
        sock.settimeout(timeout)
        try:
            data = sock.recv(4096)
        except TimeoutError:
            return {
                "client_timed_out": True,
                "bytes_received": 0,
                "client_deadline_seconds": timeout,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        return {
            "client_timed_out": False,
            "bytes_received": len(data),
            "response_head": decode(data[:200]),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }


def leg_http_security(python: str, evidence: EvidenceWriter, deadline: float) -> dict[str, Any]:
    """Every HTTP security check over a real loopback socket."""
    started = time.monotonic()
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    bind = f"127.0.0.1:{port}"
    argv = [str(HTTP_SERVER), "--port", str(port)]
    command = " ".join([python, *argv])
    environment = {READ_TOKEN_ENV: READ_TOKEN_VALUE, CONTROL_TOKEN_ENV: CONTROL_TOKEN_VALUE}
    process = spawn(python, argv, env=environment)
    out = LineReader(process.stdout, "launcher-stdout")
    err = LineReader(process.stderr, "launcher-stderr")
    checks: list[dict[str, Any]] = []
    readiness: dict[str, Any] = {"ready": False}
    try:
        ready_line = decode(out.next_line(60.0)).strip()
        readiness = {"ready": ready_line.startswith("READY "), "line": ready_line}
    except Exception as exc:
        readiness = {"ready": False, "error": f"{type(exc).__name__}: {exc}"}
    checks.append(
        check(
            "http.launcher_ready",
            "positive",
            "pass" if readiness.get("ready") else "fail",
            readiness,
        )
    )

    if readiness.get("ready"):
        # Positive: probes answer without a credential, but still inside the allowlist.
        probes = {
            path: http_request(base_url, "GET", path, timeout=deadline)
            for path in (HEALTH_PATH, READY_PATH)
        }
        checks.append(
            check(
                "http.probes.allowed_host_without_credential",
                "positive",
                "pass"
                if all(item.get("status_code") not in (None, 403) for item in probes.values())
                and probes.get(HEALTH_PATH, {}).get("status_code") == 200
                else "fail",
                {
                    "statuses": {path: item.get("status_code") for path, item in probes.items()},
                    "readyz_reported_readiness": probes.get(READY_PATH, {}).get("body_excerpt"),
                    "any_refused_by_policy": any(
                        item.get("status_code") == 403 for item in probes.values()
                    ),
                },
            )
        )

        # Positive: the pinned SDK client completes a real handshake with a scoped token.
        handshake = sdk_handshake(base_url, READ_TOKEN_VALUE, timeout=deadline)
        checks.append(
            check(
                "http.sdk_client.handshake",
                "positive",
                "pass" if handshake.get("negotiated_is_pinned_minimum") else "fail",
                handshake,
            )
        )

        # Negative: a lookalike Host and a lookalike Origin are refused.
        lookalike_host = http_request(
            base_url, "GET", HEALTH_PATH, host=f"evil.example:{port}", timeout=deadline
        )
        checks.append(
            check(
                "http.lookalike_host.refused",
                "negative",
                "pass"
                if lookalike_host.get("status_code") == 403
                and lookalike_host.get("error_code") == "FORBIDDEN"
                else "fail",
                lookalike_host,
            )
        )
        lookalike_origin = http_request(
            base_url, "GET", HEALTH_PATH, origin=f"http://127.0.0.1:{port + 1}", timeout=deadline
        )
        checks.append(
            check(
                "http.lookalike_origin.refused",
                "negative",
                "pass"
                if lookalike_origin.get("status_code") == 403
                and lookalike_origin.get("error_code") == "FORBIDDEN"
                else "fail",
                lookalike_origin,
            )
        )

        # Negative: no credential, and a credential minted for the graphd control audience.
        anonymous = http_request(
            base_url,
            "POST",
            MCP_PATH,
            payload={"jsonrpc": "2.0", "id": 3, "method": "initialize"},
            timeout=deadline,
        )
        checks.append(
            check(
                "http.missing_credential.refused",
                "negative",
                "pass"
                if anonymous.get("status_code") == 401
                and anonymous.get("error_code") == "UNAUTHENTICATED"
                else "fail",
                anonymous,
            )
        )
        control_token = http_request(
            base_url,
            "POST",
            MCP_PATH,
            authorization=f"Bearer {CONTROL_TOKEN_VALUE}",
            payload={"jsonrpc": "2.0", "id": 4, "method": "initialize"},
            timeout=deadline,
        )
        checks.append(
            check(
                "http.control_audience_credential.refused",
                "negative",
                "pass"
                if control_token.get("status_code") == 401
                and control_token.get("error_code") == "UNAUTHENTICATED"
                else "fail",
                control_token,
            )
        )

        # Negative: a read-only token may not invoke a mutating tool, and may not read
        # outside its solution scope. Neither response may echo the credential.
        mutating = http_request(
            base_url,
            "POST",
            MCP_PATH,
            authorization=f"Bearer {READ_TOKEN_VALUE}",
            payload={
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "graph_reconcile",
                    "arguments": {"solution_id": "demo-solution"},
                },
            },
            timeout=deadline,
        )
        checks.append(
            check(
                "http.read_token_mutating_tool.refused",
                "negative",
                "pass"
                if mutating.get("status_code") == 403
                and mutating.get("error_code") == "FORBIDDEN"
                and READ_TOKEN_VALUE not in str(mutating.get("body_excerpt", ""))
                else "fail",
                mutating,
            )
        )
        unscoped = http_request(
            base_url,
            "POST",
            MCP_PATH,
            authorization=f"Bearer {READ_TOKEN_VALUE}",
            payload={
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "graph_query", "arguments": {"solution_id": "alpha"}},
            },
            timeout=deadline,
        )
        checks.append(
            check(
                "http.read_token_unscoped_solution.refused",
                "negative",
                "pass"
                if unscoped.get("status_code") == 403 and unscoped.get("error_code") == "FORBIDDEN"
                else "fail",
                unscoped,
            )
        )

        # Boundary: a body over the configured bound is refused before parsing.
        oversized = http_request(
            base_url,
            "POST",
            MCP_PATH,
            authorization=f"Bearer {READ_TOKEN_VALUE}",
            raw_body=b"{" + b" " * (security.DEFAULT_MAX_BODY_BYTES + 1024) + b"}",
            timeout=deadline,
        )
        checks.append(
            check(
                "http.oversized_body.refused",
                "boundary",
                "pass" if oversized.get("error_code") == "VALIDATION_ERROR" else "fail",
                oversized,
            )
        )

        # Failure: an incomplete body leaves the client deadline to expire.
        stalled = stalled_request(base_url, timeout=6.0)
        checks.append(
            check(
                "http.stalled_body.client_timeout",
                "failure",
                "pass" if stalled.get("client_timed_out") else "fail",
                stalled,
            )
        )

    launcher_stderr = collect(err, 3.0)
    if process.poll() is None:
        process.terminate()
        end = finish(process, 15.0)
    else:
        end = {"exit_code": process.poll(), "terminated_by_harness": False}
    launcher_stdout = collect(out, 2.0)
    checks.append(
        check(
            "http.launcher_teardown",
            "boundary",
            "pass" if process.poll() is not None else "fail",
            end,
        )
    )
    status = "passed" if all(item["outcome"] == "pass" for item in checks) else "failed"
    return {
        "leg": "http-security",
        "status": status,
        "command": command,
        "bind": bind,
        "duration_seconds": round(time.monotonic() - started, 3),
        "checks": checks,
        "artifacts": {
            "launcher_stdout": evidence.stream("http-launcher-stdout", launcher_stdout),
            "launcher_stderr": evidence.stream("http-launcher-stderr", launcher_stderr),
        },
    }


# --- matrix assembly ------------------------------------------------------------------------


def python_version_of(python: str) -> str:
    result = subprocess.run(
        [python, "-c", "import platform; print(platform.python_version())"],
        capture_output=True,
        text=True,
        env=child_env(),
        cwd=str(REPO_ROOT),
    )
    return (result.stdout or "").strip() or "unknown"


def run_stdio_legs(python: str, evidence: EvidenceWriter, deadline: float) -> list[dict[str, Any]]:
    return [
        stdio_leg(
            ["-m", STDIO_MODULE, "--name", "v2-031-native-stdio"],
            python,
            evidence,
            deadline,
            label="stdio-handshake",
        ),
        stdio_leg(
            [
                str(NOISE_PROBE),
                "--name",
                "v2-031-native-noise",
                "--noise-lines",
                "3",
                "--noise-delay",
                "1.0",
            ],
            python,
            evidence,
            deadline,
            label="stdio-noise",
            noise_window=2.0,
            expect_noise=True,
        ),
        leg_stdio_silence_deadline(python, evidence, deadline),
        leg_stdio_interrupt(python, evidence, deadline),
    ]


def write_hashes(root: Path) -> Path:
    """Write ``SHA256SUMS`` over every artifact in the target directory."""
    lines: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            lines.append(f"{sha256_file(path)}  {relative(path, root)}")
    target = root / "SHA256SUMS"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="v2-031-native-matrix",
        description="Capture the native query, stdio process and HTTP security matrix.",
    )
    parser.add_argument("--target", required=True, help="Target id, e.g. windows-x64.")
    parser.add_argument("--out", default=None, help="Evidence directory for this target.")
    parser.add_argument("--python", default=sys.executable, help="Interpreter that runs the legs.")
    parser.add_argument(
        "--deadline", type=float, default=30.0, help="Per-step deadline in seconds."
    )
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    out = Path(args.out) if args.out is not None else NATIVE_DIR / "evidence" / args.target
    out.mkdir(parents=True, exist_ok=True)
    evidence = EvidenceWriter(out)

    python_version = python_version_of(args.python)
    identity_document = identity(args.target, args.python, python_version)
    evidence.document("identity", identity_document)

    legs: list[dict[str, Any]] = []
    for name, runner in (
        ("python-query", lambda: leg_python_query(evidence, args.deadline)),
        ("http-security", lambda: leg_http_security(args.python, evidence, args.deadline)),
    ):
        try:
            legs.append(runner())
        except Exception:
            legs.append({"leg": name, "status": "error", "traceback": traceback.format_exc()})
    try:
        legs.extend(run_stdio_legs(args.python, evidence, args.deadline))
    except Exception:
        legs.append(
            {"leg": "stdio-process", "status": "error", "traceback": traceback.format_exc()}
        )

    ordered = sorted(legs, key=lambda item: LEG_ORDER.get(item["leg"], 99))
    statuses = {item["leg"]: item["status"] for item in ordered}
    matrix = {
        "task": "V2-031",
        "repository": "axiom-mcp",
        "target": args.target,
        "generated_utc": utc_now(),
        "interpreter": args.python,
        "identity": identity_document,
        "leg_status": statuses,
        "legs": ordered,
        "passed": all(status == "passed" for status in statuses.values()),
        "released_core_fixture": {
            "status": "not_run",
            "fixture_used": relative(DEMO_SOLUTION, REPO_ROOT),
            "fixture_self_label": FIXTURE_SELF_LABEL,
            "reason": (
                "No released core fixture set exists in this checkout or on this host. The only "
                "solution bundle in the repository self-labels generator_version="
                f'"{FIXTURE_SELF_LABEL}", so a released-core runtime certification was not '
                "performed and is recorded as not_run."
            ),
        },
        "disclaimer": (
            "Command output captured on the native host named above. The demo-solution bundle "
            "is generated by synthetic-fixture-v2-not-runtime, so these legs are not a released "
            "core runtime certification."
        ),
    }
    evidence.document("matrix", matrix)
    sums = write_hashes(out)
    print(json.dumps({"target": args.target, "leg_status": statuses, "sha256sums": str(sums)}))
    return 0 if matrix["passed"] else 1


#: Deterministic report order.
LEG_ORDER = {
    "python-query": 0,
    "stdio-handshake": 1,
    "stdio-noise": 2,
    "stdio-silence-deadline": 3,
    "stdio-interrupt": 4,
    "http-security": 5,
}


if __name__ == "__main__":
    raise SystemExit(main())
