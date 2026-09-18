# Changelog — axiom-mcp

## Unreleased

- **C-009** Resolve registered snapshot locations. Add `src/axiom_mcp/registry.py`,
  `tests/test_registry.py` and `docs/snapshot-registry.md`. A query names a **logical** target - a
  solution id, a project id, a lane, a generation id and a generation-relative reference - and this
  module is the only place that becomes a filesystem path, by looking up a binding the owner registered
  under `AXIOM_HOME`. A caller-supplied string can therefore not choose a JSON file: an absolute POSIX
  path, a drive-qualified Windows path, a UNC path, a backslash, an empty reference and any `..` segment
  are refused as `UntrustedPath` before any join, and `SnapshotLocation.resolve` re-checks containment
  after symbolic links are resolved so a symlink planted inside a lane cannot escape it. The three
  consumed contracts are not re-defined here: the `AXIOM_HOME` defaults and the
  `config/registry.json` / `instances/<id>/solution.guard` layout come from `SOURCE-OF-TRUST.md` section
  5 and the native guard ABI, the `P`/`C` path contract comes from
  `docs/12-SNAPSHOT-READ-WRITE-PROTOCOL.md` section 2, and the portable-relative rule is the one
  `project-manifest.schema.json` applies to a manifest file entry. The parser is strict where the
  resolution would otherwise be ambiguous or untrusted - an unknown registry major, a duplicate
  solution/repository/project id, a relative `repo_root` or `axiom_home`, a `repo_root` inside
  `AXIOM_HOME` and a declared `guard_directory` that is not the ABI path are all refused - while a
  missing registry file is honestly an empty registry that resolves nothing instead of a disk scan.

- **C-006** Implement structured MCP error mapping. Add `src/axiom_mcp/errors.py`,
  `tests/test_errors.py`, `docs/errors-and-redaction.md` and `release/errors_spike.py`. The
  module separates a **protocol** failure (the request never became a call, rendered as a
  JSON-RPC error carrying the canonical code in `error.data.code`) from a **tool-result**
  failure (the call ran and failed for a domain reason, rendered with `isError: true` and a
  structured body), so a host can retry a rate limit and still refuse to retry a malformed
  request. The canonical code set is `query-response.schema.json`'s `queryError` enum plus
  `PROJECT_UNAVAILABLE`, `SNAPSHOT_UNAVAILABLE` and `SNAPSHOT_CORRUPT`, and a code outside it
  raises instead of rendering. Redaction is consumed rather than re-derived: the detection
  table, the `<redacted:{category}>` placeholder, the metadata allowlist and the path
  relativisation rule come from `contracts/redaction-policy.md`, and the conformance test
  loads that policy's executable reference evaluator by path and asserts agreement over every
  canonical fixture, so a policy change this module does not follow fails the build rather
  than drifting silently. `AxiomError` redacts the message and the whole detail tree on
  construction, before any depth/width/length bound is applied, and records only the *type*
  of a cause - there is no field that can hold a formatted traceback.

- **C-005** Implement the Host/Origin and scoped-authentication policy. Add
  `src/axiom_mcp/security.py` (an explicit `SecurityPolicy` that refuses an empty or
  wildcarded `Host`/`Origin` allowlist and admits a bare host entry only for any port on
  that host; `ScopedToken` plus `TokenRegistry` with the canonical `read`, `reconcile` and
  `checkpoint` capabilities, per-solution and per-project scope, and audience separation
  between `axiom-mcp` and `axiom-graphd-control`; `EnvTokenReference`/`FileTokenReference`
  so configuration names a credential instead of carrying a literal; a digest-only registry
  so a presented token is never retained or echoed; a pure-ASGI `SecurityMiddleware` that
  classifies a bounded JSON-RPC body to require the capability a tool call needs without
  buffering the `text/event-stream` response; and `build_gateway(security=..., authenticator=...)`
  wiring), `tests/test_security.py`, `docs/security-host-origin-auth.md` and the
  `release/security_spike.py` verification artifact. A loopback bind is proven not to be
  authentication: the same gateway that serves an authenticated handshake refuses a
  disallowed Host, a second-port Origin, a missing token, a graphd control token, a read
  token requesting reconciliation and a read token naming another solution.

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
