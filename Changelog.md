# Changelog — axiom-mcp

## Unreleased

- **C-008** Implement the `axiom-mcp update check` and `axiom-mcp update apply --plan`
  commands. Add `src/axiom_mcp/update.py`, `tests/test_update.py`,
  `docs/update-plan-delegation.md` and `release/update_spike.py`, and register the two
  subcommands in `src/axiom_mcp/cli.py`. `check` reports the canonical fields - installed,
  available, compatible, channel, schema_range, update_policy, source_origin and
  needs_restart - and keeps one rule: an unknown answer is never rendered as up to date, so
  an unconfigured, offline or unreadable source leaves `available` null and names the reason
  instead of reporting `current`. An origin is trusted only when it is the canonical
  repository or when the owner added it to `AXIOM_MCP_UPDATE_ALLOWED_ORIGINS`; an unlisted
  origin is blocked and never contacted. `apply` validates one plan with the pinned
  specification's own evaluator (`tools/update_plan_contract.py`), reporting its reasons and
  digest verbatim, and refuses an unverifiable document rather than assuming acceptance. It
  then requires an approved state, a target of this component, and an absolute install root
  outside the running interpreter, its site-packages and this package: a plan that would
  rewrite the running installation is refused as `in_place_upgrade_prohibited`, and the
  delegated command is guarded against `pip`, `pip3`, `uv`, `easy_install`, `python -m pip`
  and self-invocation. An accepted plan is reported as the exact argument list
  `axiom update apply --plan PATH` for the external updater; the running process performs no
  install, download or environment mutation.
- **C-007** Implement the `axiom-mcp version` and `axiom-mcp doctor` commands. Add
  `src/axiom_mcp/cli.py`, `tests/test_cli.py`, `docs/cli-version-and-doctor.md` and
  `release/cli_spike.py`. `version` renders exactly the eight fields
  `contracts/schemas/version-report.schema.json` requires and adds no key of its own.
  `doctor` reports six sections - runtime pins, the locked SDK surface, the advertised
  protocol revisions, the canonical version dimensions including the accepted graph schema
  major, the credential scope the gateway enforces, and data-plane readiness - and keeps
  two states apart that are easy to conflate: a runtime that is not the pinned one exits
  `9` as incompatible, while a healthy runtime with an unwired data plane exits `4` as not
  ready. Readiness never consults `/healthz`: the report states its basis and carries
  `process_health_is_readiness: false`, because a listening socket is not a gateway that can
  answer a query. The credential section names a reference rather than a value (configuration
  carrying a literal token is refused), distinguishes an unconfigured machine from an
  unresolvable reference from a resolvable-but-unregistered credential, reports the granted
  scope through the C-005 registry, and never prints a token - an out-of-root credential path
  is redacted by the C-006 policy. An invalid flag or an unregistered subcommand exits `2`
  instead of being silently ignored, and `--json` writes a single object to stdout with
  diagnostics on stderr.

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
