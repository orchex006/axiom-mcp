# Changelog — axiom-mcp

## Unreleased

- **C-004** Implement the Streamable HTTP transport assembly. Add
  `src/axiom_mcp/http.py` (`build_gateway`, which registers the one MCP endpoint at `/mcp`
  through the C-002 mount and adds `/healthz` as minimal process health that makes no
  readiness claim and `/readyz` as a separate query/control availability report returning
  `NOT_READY` while a plane is down; `observe_streamable_http` and `parse_sse_events`, which
  record the real wire result instead of assuming streaming; `HttpTransportSettings`, which
  defaults to a loopback bind and refuses an unacknowledged non-loopback bind; and the
  `uvicorn` serving path), `tests/test_http_transport.py`, `docs/http-transport.md` and the
  `release/http_transport_spike.py` verification artifact. A plain REST route that returns a
  hand-written `initialize`-shaped JSON body is proven non-conformant in the same test that
  proves the real gateway conforms.

- **C-003** Implement the JSON-only stdio transport. Add `src/axiom_mcp/stdio.py`
  (a `StdoutGuard` that replaces `sys.stdout` for the session, diverts every text write to
  the diagnostics stream while counting it as a violation, and exposes only a counted binary
  `ProtocolBuffer` as the real stdout path; `Diagnostics` and `banner_lines` that always
  target stderr; `serve_stdio`, which installs the guard, runs the SDK's `run_stdio_async`,
  restores the original stdout in a `finally` block and reports the real
  violations/protocol-writes/protocol-bytes counts), `tests/test_stdio_transport.py`,
  `docs/stdio-transport.md` and the `release/stdio_spike.py` verification artifact. A stray
  text write cannot break `initialize`: it is recorded instead of corrupting the stream. The
  spike records a real handshake with `violations=0`, one parseable protocol frame on stdout
  and the banner on stderr.

- **C-002** Mount a real MCP Streamable HTTP server in FastAPI. Add
  `src/axiom_mcp/sdk_compat.py` (explicit lifespan composition so the SDK session
  manager starts and stops exactly once, re-parenting the SDK's own Streamable HTTP
  handler onto the contract path `/mcp` so the endpoint is an exact match with no
  redirect and nothing is mounted at `/`, an exact-match `LifecycleRecorder`, and a
  real in-process `initialize` handshake that reports the negotiated protocol),
  `tests/test_sdk_mount.py` and `docs/sdk-mount-lifecycle.md`, plus the
  `release/mcp_mount_spike.py` verification artifact. The spike records the failure
  this task exists to prevent: a plain Starlette mount serves HTTP but answers
  `initialize` with HTTP 500 because mounting does not run the sub-application
  lifespan.

- **C-001** Pin the Python runtime and the official MCP SDK. Add `pyproject.toml`
  (`requires-python >=3.13,<3.14`, `mcp==1.28.1` plus exact FastAPI/Uvicorn/Starlette/
  Pydantic/anyio/httpx pins, the `axiom-mcp` console entry point, pytest and ruff
  configuration), `src/axiom_mcp/version.py` (canonical version dimensions and the
  compatibility-spike predicates), `src/axiom_mcp/__init__.py`, `tests/test_runtime_pin.py`
  and `docs/runtime-compatibility.md`. The first implementation in this repository also
  establishes the required check `python -m pytest tests -q` with recorded output and exit
  code, and `.gitignore`/`.gitattributes` for cache exclusion and LF discipline.

- Add `AGENTS.md` and a verified `spec.lock.json`: the pin records the immutable `axiom-specs` revision and seven contract digests, and `tools/spec-lock-check.py` accepts it with exit code 0. Implementation tasks are no longer blocked by the missing governance pin.

- Add `Development.md`: component development contract covering repository identity, preflight gate, required checks, evidence and completion requirements, branch/merge/release policy, and the rule that verified work finished in a worktree must reach the owner's primary checkout.

### Notes

- The runtime pin is enforced, not asserted: `tests/test_runtime_pin.py` compares
  `pyproject.toml`, `axiom_mcp.version` and the installed interpreter/SDK, and a boundary
  test proves a legacy-only SDK surface fails the spike.
- The release gate stays closed. No tag, release branch or publish was created.
