# Changelog — axiom-mcp

## Unreleased

- **V2-019** Add the cross-language reader guard adapter. Add
  `src/axiom_mcp/guard/adapter.py`, `tests/test_guard_protocol.py`, `tests/test_guard_adapter.py`
  and `tests/test_guard_cross_language.py`, plus a `descriptor()` accessor on both platform
  handles and the package export of the adapter. The card proposes `src/axiom_mcp/guard.py`, but
  C-010 landed the guard as the package `src/axiom_mcp/guard/`, so the adapter is a module inside
  that package rather than a second top-level module; the justified path change is recorded in
  the task evidence, as C-011 recorded its own. `abi_descriptor()` publishes the frozen surface
  a second language needs - the two lock files with their roles, both platform rows, the
  acquisition and release order, the bounded wait, crash release and the interoperability
  requirement - in the contract's own field names, with `abi_json()`/`abi_sha256()` to pin this
  build, and `python -m axiom_mcp.guard.adapter abi` to print it from an independent process.
  The holder *process contract* (argv, one-JSON-line events, exit codes) is documented in the
  same place, and `AXIOM_GUARD_HOLDER_ARGV` points the tests at any ABI-conformant holder - the
  Rust daemon included - instead of the in-repo probe; a holder that cannot be started is
  reported as `holder_unavailable` rather than silently replaced. `ReaderGuardAdapter` is the
  reader shape of section 5: admission shared, data shared while admission is still held,
  admission released early, the bounded bytes copied by `read_pinned` under `data.lock`, data
  released, and parsing only after the guard is gone; `verify_surface()` refuses a guard
  directory or lock file that is a link, because two languages following one name to different
  targets are not one guard. Tests use real second processes for the exclusive holder, the
  bounded timeout, a killed holder, and normal release with closed handles; the contract-fidelity
  test recomputes the frozen-field digest and rejects a document mutated in memory. The Rust
  holder, the POSIX `flock` primitive and the Rust↔Python proof on a POSIX host remain
  unverified here: the foreign-holder leg is skipped with that reason, and the local Windows
  cross-language run was made with a non-Python `LockFileEx` holder (PowerShell/.NET), not with
  the Rust daemon.
- **C-012** Pin one catalog vector per query and never fall back to a project's latest
  generation. Add `src/axiom_mcp/catalog.py`, `tests/test_catalog.py` and vendored pinned
  fixtures under `tests/fixtures/solution/demo-solution/` (byte-for-byte from
  `axiom-specs/examples/snapshots/.axiom/graph/demo-solution`), plus a catalog section in
  `docs/snapshot-reader-core.md`. `load_solution_catalog` reads the lane pointer once, requires
  the generation directory name to equal the digest of the catalog bytes inside it and the
  pointer's `generation_id` to match those same bytes, then validates a member as exactly
  `(project_id, generation_id, source_fingerprint)`. A member carrying `name`, `project_name`,
  `path` or `directory`, a duplicate member, a non-canonical byte form and an unknown schema
  major are refused; the major is reported before the byte form. `pin_catalog` resolves each
  member through its pinned `generation_id` - never through the project lane's `current.json` -
  and requires the member manifest to hash to that generation and carry the pinned source
  fingerprint. An absent, unreadable or mismatched member is recorded `missing` with a reason
  and the vector is reported `partial` rather than substituted; `require_complete()` raises
  `CatalogMemberMissing` so a caller under `require_complete_solution` rejects a partial answer
  instead of receiving one that looks complete. AC1 is observed by the regression that
  publishes a newer valid generation into a project lane and repoints that lane's
  `current.json` at it: the answer stays on the catalog's generation. The negative and
  boundary cases are a member with no pinned generation, a name-only member, a duplicate
  member, an altered pinned generation, an unknown major, non-canonical bytes, a renamed
  generation directory and an absent catalog pointer.
- **C-011** Add the Python Windows shared guard. Add `src/axiom_mcp/guard/locks_windows.py`,
  `tests/test_guard_windows.py` and a Windows section in `docs/snapshot-reader-core.md`. The
  card proposes `src/axiom_mcp/locks_windows.py`, but C-010 landed the guard as the package
  `src/axiom_mcp/guard/`, so the Windows primitive is the sibling of `locks_posix.py` inside
  that package rather than a second top-level module; the justified path change is recorded in
  the task evidence. The module implements exactly the frozen block and nothing more: it opens
  each lock file with read/write access, `OPEN_ALWAYS` and share mode
  `FILE_SHARE_READ | FILE_SHARE_WRITE`, which excludes `FILE_SHARE_DELETE`, tries one
  non-blocking `LockFileEx` over byte range offset 0 length 1 carrying
  `LOCKFILE_FAIL_IMMEDIATELY`, and releases with a matching `UnlockFileEx` before closing. The
  acquisition order, the bounded wait, the refusal to upgrade and the release of every acquired
  guard remain in `src/axiom_mcp/guard/engine.py` and are therefore identical on both platforms;
  only the primitive is different, because Windows has no `flock`. Contention is reported as
  `GuardBusy` from `ERROR_LOCK_VIOLATION` or `ERROR_SHARING_VIOLATION`, so the engine retries
  inside its deadline instead of surfacing either as a caller-visible error. Identity is read from
  the open descriptor with `os.fstat` and compared against the path, so the engine still detects a
  file replaced between open and lock and never locks an unrelated file. `ctypes` over `kernel32`
  is used rather than a wrapper package: the primitive is four calls, and a dependency for them
  would put the guard behind a package registry the release path does not need. AC1 is observed on
  an actual Windows host: an exclusive `LockFileEx` holder in a real second process makes both a
  shared and an exclusive attempt in this process time out, two shared holders coexist, the file is
  acquirable again after release, an open guard handle refuses a deny-everything opener with
  `ERROR_SHARING_VIOLATION`, the held file cannot be removed while the handle is open because
  delete is not shared, and the frozen byte range, open mode and share mode are asserted against
  the contract constants. The negative and boundary cases are a second handle conflicting inside
  one process, an upgrade attempt and a recursive acquisition being refused with deterministic
  reasons, an identity read after close failing as `identity_unreadable`, and a reader denied on
  `data.lock` releasing the admission it already held.
- **D-011** Document the snapshot race and read-only semantics. Add `docs/guides/snapshots.md`,
  the reader-facing guide to the publication protocol in
  `axiom-specs/docs/12-SNAPSHOT-READ-WRITE-PROTOCOL.md` and the lock ABI in
  `axiom-specs/contracts/native-reader-writer-guards.md`. The guide states the eight
  invariants (SNP-01..SNP-08) and then the one rule the rest exists to protect: a reader holds
  the shared guard, reads the pointer once and pins exactly one generation vector for the whole
  request, so an answer can never be half old and half new. It walks the three nested integrity
  claims (pointer names the generation and carries the manifest digest; the manifest lists every
  shard with path, role, SHA-256, byte size and record count; each shard is pinned by that
  entry), the exact guard shape (`SolutionGuard.reader()` acquires `admission.lock` then
  `data.lock` shared, releases admission early, copies the bounded bytes, releases data, and only
  then parses), the bounded-wait constants (default 5000 ms, maximum single wait 60000 ms, retry
  25 ms doubling to a 250 ms cap, no upgrade and no recursive acquire), the recovery table for
  missing, corrupt and collected generations (`SNAPSHOT_UNAVAILABLE` retryable,
  `SNAPSHOT_CORRUPT` and `SNAPSHOT_EXPIRED` not), retention and disk-full behaviour that never
  deletes the current generation, and the crash-recovery matrix. It states plainly that raw
  multi-file reads are not safe - they hold no reader registration and no lock, so nothing
  coordinates them with GC or a Git checkout - and that a direct reader keeps consistency only
  by pinning one generation and validating every hash. The unimplemented surfaces are named
  rather than implied: the Windows guard backend is absent at this revision, cross-process Rust
  and Python interop is unverified, and no GC, daemon or reader-session runtime exists here.
- **D-007** Write the MCP tools and response reference. Add `docs/reference/mcp.md`, the
  reference for the tool and response contract the specification fixes. It opens with an
  explicit status note that the tool layer is **not implemented at this revision** - there is no
  `src/axiom_mcp/tools/` package and nothing registers a tool, so a client that lists tools sees
  an empty catalog - and every shape is labelled a contract rather than an observation. The page
  records the two protocol transports (`/mcp` Streamable HTTP and stdio) with `/healthz` and
  `/readyz` kept apart, the six-tool catalog with purpose, required capability, side effect and
  the registering task (C-028..C-033), the complete `graph_query` request with its closed schema,
  scope rule (`project_id` XOR `project_ids`) and per-operation requirements, the budgets with
  their defaults and maxima (`depth` 0..8 default 2, `max_nodes` 1..500 default 50, `max_edges`
  0..1000 default 100, `max_bytes` 512..32768 default 8192), the fifteen edge kinds, the
  `consistency`, `direction` and `projection` allowlists, the response keys with the freshness,
  coverage and verification enums, the capability map and audience separation, both error
  surfaces with the canonical envelope and code table plus the JSON-RPC and HTTP mappings, and
  the redaction and detail bounds. Two self-contained sample queries are contract examples and
  are noted as such; neither names a file, and the page states that `graph_query` has no `path`,
  `file` or `root` input, so a client cannot read an arbitrary file by naming one.
- **C-013** Validate manifest digests and canonical JSON. Add
  `src/axiom_mcp/manifest.py`, `tests/test_manifest.py`, `docs/manifest-validation.md` and
  vendored pinned test fixtures under `tests/fixtures/`. A published generation is three
  nested integrity claims and this module is where each becomes executable: the pointer
  names one generation and carries the manifest digest, the manifest lists every shard with
  its path, role, SHA-256, byte size and record count, and each shard is pinned by the entry
  that names it. Canonical bytes follow `docs/11-GRAPH-DATA-CONTRACT.md` section 5 - UTF-8
  without a BOM, LF newlines, lexicographically sorted keys, compact separators, one
  trailing LF, no floats and no non-string keys, Unicode preserved as supplied - so a
  document that is valid JSON but not canonical is refused instead of silently
  re-serialized, because the digest identifying a generation is a digest over those bytes.
  AC1 is the refusal set: altered manifest bytes fail the pointer closure as
  `DigestMismatch`, an unknown schema major fails as `UnsupportedSchemaMajor` before any
  formatting complaint, and a duplicate file path or duplicate role fails as
  `DuplicateManifestEntry`; a canonical example passes, including one shipped example
  generation whose `manifest.json` hashes to its own directory name. Shard verification
  checks length, then digest, then parse, then record count, so a truncated shard is
  reported as truncated and unparsable bytes never reach the JSON parser before they are
  known to be the published ones; `verify_closure` applies that to every declared shard
  through a caller-supplied read function. The two canonical test corpora are vendored with
  their pinned SHA-256 asserted in the test, and the portable-relative path pattern is
  asserted equal to the registry's, so neither the fixture bytes nor the two copies of that
  rule can drift apart unnoticed.
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
