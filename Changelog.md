# Changelog — axiom-mcp

## Unreleased

- **test** (V2-031) Record the native query and host-process matrix for the mandatory targets and
  state plainly which legs did not run. `tests/native/` now holds the executable evidence:
  `capture_native_matrix.py` (a real `graph_query` over the shipped `demo-solution` bundle through
  the native guard; a real `python -m axiom_mcp.stdio` process driven through a JSON-RPC handshake,
  an injected stdout-noise failure case, a bounded silent-server read deadline and an interrupt
  delivery; and the composed gateway served by uvicorn on a real loopback socket with
  host/origin/credential/capability/oversize checks and a stalled-body client timeout),
  `stdio_noise_probe.py`, `http_security_server.py`, `capture_gates.py` and `tests/native/README.md`.
  Windows 11 x64 (Python 3.13.14, guard backend `windows`) ran all six legs to completion and exited
  0; Ubuntu 26.04.1 LTS x64 **on WSL2** (provisioned CPython 3.13.15, guard backend `posix`) passed
  five of six. The POSIX interrupt leg **failed** as a real finding: `SIGINT` delivered to the idle
  stdio process was never serviced inside the bounded window, while `SIGINT` after a later frame
  exited `-2` and `SIGTERM` exited `-15`, so CP-08's bounded graceful drain is not implemented in
  `src/axiom_mcp/stdio.py`; the check was recorded as a failure rather than weakened. Required
  checks on Windows x64: `python -m pytest tests -q` exit 0 (`753 passed, 2 skipped, 230 subtests
  passed`), `python -m ruff check .` exit 0, `python -m ruff format --check .` exit 0 (`107 files
  already formatted`). On WSL2 the required pytest exited **2**, not green:
  `tests/test_guard_windows.py` imports `locks_windows` -> `msvcrt` before its `importorskip`, so
  collection aborts; the two ruff gates exited 0 and an explicitly non-canonical diagnostic
  (`--ignore=tests/test_guard_windows.py`) exited 1 with `12 failed, 738 passed, 1 skipped, 7
  errors`. Recorded and not changed: **no released core fixture set exists** - the only bundle
  self-labels `generator_version: "synthetic-fixture-v2-not-runtime"` (read off the real manifests,
  and asserted by `tests/test_catalog.py`), so the released-core certification is `not_run` rather
  than claimed; `macos-arm64` and `macos-x64` are `not_run` because no macOS host exists and a
  container or cross-build may not substitute; `linux-x64` stays `not_run` because a WSL2 run is not
  native Linux evidence per `compatibility/platform-matrix.json`; and the POSIX guard backend stays
  outside this matrix. 0 of 4 mandatory targets are certified by this task.

- **docs** (I-005) Publish the installation and upgrade guide and make the README a real package
  README. The card recorded that `README.md` was empty and that no installation guide existed; the
  first half was stale, but the gap behind it was real and sharper. `README.md` was repository
  facing, and the **built wheel carried no README at all**: `readme = "README.md"` places the text
  inside `dist-info/METADATA` only, and the measured baseline artifact
  (`axiom_mcp-0.0.0.dev0-py3-none-any.whl`, 209274 bytes, sha256 `b610139a...0ad6`, 54 entries) held
  **zero** `.md` entries, so a person who installed only the package had nothing to read. Added
  `docs/install-and-upgrade.md`: prerequisites that *reference* `pyproject.toml` rather than
  restate it, what the payload contains, the scratch and versioned-environment install paths,
  `doctor` verification with its exit-code table, the environment variables the server actually
  reads, both transports (stdio and Streamable HTTP), the host hand-off through
  `axiom-mcp-entrypoints`, upgrade, rollback, an observed refusal table, and explicit
  "not implemented" and "unverified on this host" sections. Rewrote `README.md` as a package
  README that installs, verifies, launches and links out instead of duplicating normative text, and
  stated in it that the `docs/` tree does not travel with the wheel so its links resolve against a
  checkout. The README now ships: `src/axiom_mcp/README.md` is declared through
  `[tool.setuptools.package-data]`, and `tests/test_package_readme.py` fails if the packaged copy
  drifts from the repository README or if the declaration is dropped. `docs/README.md` indexes the
  new guide under Operations. Verified on Windows x64, Python 3.13.14, `mcp==1.28.1`: the rebuilt
  wheel (`axiom_mcp-0.0.0.dev0-py3-none-any.whl`, 212431 bytes, sha256 `1373f899...0ca6`, 55
  entries) contains `axiom_mcp/README.md` (sha256 `d9b210f9...ba77`), and a fresh scratch install
  exposes it at `site-packages/axiom_mcp/README.md` with the same digest; from a directory outside
  the checkout, `axiom-mcp version` exits 0, `axiom-mcp doctor` exits 4 (`not_ready`, correct for a
  bare install), `axiom-mcp update check` and `--offline` exit 0, and
  `axiom-mcp update apply --plan <missing>` exits 2. Required checks on the final bytes:
  `python -m pytest tests -q` exit 0 with `753 passed, 2 skipped, 230 subtests passed`,
  `python -m ruff check .` exit 0 with `All checks passed!`, and `python -m ruff format --check .`
  exit 0 with `152 files already formatted`. Recorded and not changed: `release/package.py` stays
  operator tooling outside the wheel; no Linux, macOS or POSIX-guard leg was run, and no host
  product was launched through the entrypoint, so those stay `unverified` in the guide rather than
  passing.
- **docs** (W10-B) Write the repository README and the documentation index, and correct a stale
  status line in the development contract. `README.md` was a **0-byte file** at this revision even
  though `pyproject.toml` declares `readme = "README.md"`, so the distribution would have shipped an
  empty long description; it now states the owner scope (the MCP server surface, and explicitly not
  the graph runtime, ecosystem contracts or skills), the repository layout across the 49 modules in
  `src/axiom_mcp/` and the 44 modules in `tests/`, the three required checks established by `C-001`,
  the pinned dependency set, the six-tool catalog and the four transport surfaces, the unapproved
  draft nature of `spec.lock.json` with the offline and `--release` verification commands, and the
  platform status - `compatibility/platform-matrix.json` is `required_not_certified` with **0 of 4**
  mandatory targets certified, and the legs this Windows host cannot execute (POSIX guard backend,
  cross-language Rust-holder exclusion, real host-process launches) are named as recorded `not_run`
  rather than as passing. `docs/README.md` did not exist at all; it now indexes the 18 implementation
  guides by area (transports and protocol, query and read path, errors/contracts/compatibility,
  operations) and states that canonical contracts stay in `axiom-specs` and are resolved through the
  pinned revision. `Development.md` said "repository นี้ยังไม่มี implementation" and that the first
  task must create the required checks; both were stale for the 49-module tree and `C-001`, so the
  paragraph now records the real state and keeps the obligation to record unrun checks as unverified.
  Verification on the delivered revision: `python -m pytest tests -q` exit 0 with
  `750 passed, 2 skipped, 230 subtests passed`, `python -m ruff check .` exit 0 with
  `All checks passed!`, and `python -m ruff format --check .` exit 0 with `102 files already
  formatted`; every relative link in the two new files resolves inside this repository. Recorded and
  not changed: no `VERSION` file is added, because no contract requires one and this component
  already declares its version in `pyproject.toml`; no Docker, no native host launch and no
  certification is claimed.
- **docs** (W9-4) Enforce the snapshot guide's limits and correct the remaining stale one. The
  guard-limits correction recorded by lane L5 had already landed as `48b9e36`, so the "Windows
  backend is absent" sentence is not in `docs/guides/snapshots.md` at this revision; the recorded
  follow-up was therefore delivered as the checkable statement plus enforcement it asked for. That
  same "Unverified / limits" list still claimed **no reader-session or cache runtime exists**,
  which is stale for the same reason: `src/axiom_mcp/read_session.py` (copies under the shared
  guard, releases before any response work, records `guard_held_during_copy` and
  `parsed_after_guard_release`) and `src/axiom_mcp/cache.py` (bounded LRU keyed by immutable
  generation identity) ship and are tested. The paragraph now names both modules and what they
  guarantee, and keeps the part that is genuinely not implemented - the writer's half - as
  `axiom-graphd` work. New `tests/test_snapshot_guide_truth.py` reads the guide and compares it
  with the backends' own `primitive`/`scope` declarations and `guard.protocol.WINDOWS_BYTE_RANGE`
  (normalized, so "byte range" and "byte-range" compare equal), asserts the nine modules the guide
  points at are named and present, and fails if a stale claim reappears; the negative leg rewrites
  the guide back to the old wording and shows the checker rejects it. The backends are read as
  source declarations rather than imported, because `locks_posix` needs `fcntl` and `locks_windows`
  needs `msvcrt`, so the check runs on every platform instead of skipping on the one whose
  paragraph it protects. Documentation and test only; no product code changed.

- **V2-027** Add `docs/query-transports.md`, the page that records the JSON-first read model and
  the two supported transports without restating the snapshot protocol. It states what JSON-first
  does and does not mean: the processed-JSON snapshot stays directly readable with no MCP process,
  but a raw multi-file reader holds no guard and stays correct only by pinning one immutable
  generation - so the page links `docs/guides/snapshots.md` instead of duplicating it. It records
  the three facts every surface inherits (one pinned generation vector per request in
  `read_session.py`, a cursor bound to snapshot/query/scope in `query/cursor.py`, and a collected
  generation answering `SNAPSHOT_EXPIRED` in `recovery.py`), the stdio and Streamable HTTP launch
  forms, and the locked `plan`/`verify` surfaces that select a mode - the document's fields, the
  digest inputs, the refusal codes that decide whether a transport starts at all, and the rule that
  the HTTP allowlist must admit the address about to be bound. The states AC1 requires to stay
  distinguishable are tabulated with canonical code, retryability, contracted HTTP status and CLI
  exit code: `DAEMON_UNAVAILABLE` (503/`8`), `SNAPSHOT_UNAVAILABLE` (503/`4`), `SNAPSHOT_CORRUPT`
  (no contracted status/`8`), `SNAPSHOT_EXPIRED` (410/`4`), `PROJECT_UNAVAILABLE` (503/`3`),
  incomplete coverage as a truthful `coverage` key rather than an error code, and `NOT_READY`
  (503/`4`) - so an unreachable daemon is never reported as a missing snapshot. Cross-linked from
  `docs/stdio-transport.md`, `docs/http-transport.md`, `docs/locked-entrypoints.md` and
  `docs/reference/mcp.md`. Deviation recorded: the card's own path in specs is named as
  `tasks/C/V2-027.md`, which does not exist at `origin/main`; the real card is `tasks/V2/V2-027.md`,
  and the deliverable path it names, `docs/query-transports.md`, did not exist and was created.
  Documentation only; no product code changed.

- **V2-024** Add `src/axiom_mcp/entrypoints.py`, the locked launch surface a host consumes
  without a shell. A plan is an absolute interpreter plus an argv list, never a command string,
  so a path holding a space, an ampersand, parentheses, an apostrophe and Thai text arrives as
  one argument; `--activation-script` and `--resolve-from-path` are refused rather than honoured.
  The surface reads the packager layout it does not own (`current.json` -> `versions/<v>/` ->
  `lockfile.json`) and mirrors that layout's names, because `release/` is not installed; a test
  reads both definitions so a mirror that drifts fails instead of becoming a silent second
  definition. Resolution verifies rather than believes: the recorded interpreter must resolve
  inside that version's own `venv`, and it is then asked what it is - prefix, base prefix,
  supported Python line and SDK version - with a bare interpreter, an interpreter outside the
  environment, a non-isolated prefix, an unsupported Python line and an SDK that is not
  `mcp==1.28.1` all refused with stable codes. The launch environment is built from scratch
  (no `PYTHONPATH`, `PYTHONHOME` or login `PATH`), so a plan digest is reproducible;
  `PYTHONNOUSERSITE` is set only for an environment that does not declare sharing, and the host
  facts an interpreter cannot start without - the user-site anchors and, on Windows,
  `SystemRoot` plus the loader directory - survive a caller that supplies a partial environment.
  Three behaviours were measured here instead of assumed: without `SystemRoot` importing the SDK
  dies in `asyncio/windows_events.py` with `OSError: [WinError 10106]`; a sharing environment's
  user site is located through `APPDATA`, so dropping it makes a complete environment look
  `sdk_not_installed`; and `pyvenv.cfg` created at a non-ASCII path can hold cp874 or cp1252
  bytes, so the one-boolean metadata read is deliberately lossy. HTTP stays opt-in and is refused
  unless its allowlist admits the address about to be bound. AC1 and AC2 are proven in
  `tests/test_entrypoints.py` (57 legs) with real `venv` environments at that awkward path and a
  real JSON-RPC `initialize` over the launched plan pipes. That launch exposed a live defect in
  the entrypoint it distributes: `axiom_mcp.stdio.main` passed `banner=` to `anyio.run`, which
  forwards only positional arguments, so `python -m axiom_mcp.stdio` - the documented way to run
  the transport on its own, and the exact argv this plan names - died with `TypeError: run() got
  an unexpected keyword argument 'banner'` before serving anything. `serve_stdio` was correct and
  directly tested, which is why it stayed latent; `main` now binds the keyword with
  `functools.partial`. Its `main` is declared in `pyproject.toml` as the
  `axiom-mcp-entrypoints` console script, and the emitted document keeps `ensure_ascii`
  on: reading a plan back as a process showed the Thai part of the interpreter path
  arriving as mojibake, because `ensure_ascii=False` hands raw characters to a stdout
  whose pipe encoding is the host code page. Documented in `docs/locked-entrypoints.md`. Native Windows reparse-point
  and ACL behaviour, macOS, and a real non-loopback bind remain `not_run` here.
- **V2-016** Add `src/axiom_mcp/paths.py`, the boundary where a *wire path* and a *native binding*
  are normalized by two different rules. A wire path is a repository-relative UTF-8 reference with
  `/` separators, validated lexically with no filesystem access, so a traversal, drive-qualified,
  UNC, device, backslash, control-character, alternate-data-stream `:`, trailing dot/space or
  Windows-reserved component (the CP-03 default portable profile) is refused before any file is
  opened. A native binding is an absolute path of this host, normalized natively - `..` and
  symbolic links are resolved, and the portable profile is deliberately *not* applied to it, so a
  legitimate directory that happens to be named `con` is bound rather than refused. Neither
  function accepts the other's input, and a wire path is never resolved against the working
  directory. `resolve_wire_path` joins them in a fixed order and checks containment on the
  *resolved* paths, never on a string prefix; `registry.SnapshotLocation.resolve` and
  `registry.CatalogLocation.resolve` - the only places a caller- or manifest-supplied reference
  becomes a file path - now delegate to it, so a reader refuses an injection before opening a file.
  `registry.portable_relative` keeps the frozen manifest pattern unchanged, including its
  acceptance of `a//b` and `a/./b`. Documented in `docs/portable-paths.md`; 52 regression legs in
  `tests/test_paths.py` (positive, negative and failure-boundary) include a symlink escape that is
  refused after native resolution and a negative table driven with a nonexistent root, which is what
  proves the refusal is lexical and precedes any filesystem access. Native reparse-point/ACL
  behaviour on Windows and macOS remains `not_run` here.
- **C-036** Add `release/package.py`, the versioned-environment packager for `axiom-mcp`.
  It stages a built artifact under an install root, creates one isolated virtual
  environment per version, installs the artifact with no network and no dependency
  resolution (`pip install --no-index --no-deps`), and records exactly what the
  environment contained - artifact name, size and SHA256, interpreter and the
  `pip freeze --all` set - in a per-version `lockfile.json`. `rollback` is deliberately
  narrow: it picks an explicit version or the previous install from `history.jsonl`,
  proves the staged artifact still hashes to the locked digest, recreates the venv or
  reinstalls the artifact if the frozen set has drifted (recording the repair), refuses
  with `environment_not_restored` if the frozen set still does not match, and only then
  moves `current.json`. A moved install root is still recognised as the same environment
  because frozen direct-URL lines are compared with the absolute directory stripped.
  Mutations are confined to the install root, and the running interpreter and the
  package own source tree are refused up front through the same guard the update planner
  uses. AC1 is proven on this host with the real thing in `tests/test_release_package.py`:
  two wheels built by the real build backend, installed by the real pip into two isolated
  venvs, each venv own interpreter observed to import its own build, then a real rollback
  restoring the earlier version; the drift, tamper, missing-target and failure-boundary
  legs are driven by a scripted runner and are recorded as scripted, not as real pip
  runs. Documentation in `docs/release-packaging.md`.
- **C-035** Implement readiness and guard-releasing shutdown in `src/axiom_mcp/lifecycle.py`,
  and make the HTTP transport reuse those types instead of its own copy. Readiness is not health:
  `Lifecycle.readiness()` refuses to report ready until the SDK is initialized and the query
  plane is usable, keeps the query and control planes distinct, and lets an optional query probe
  decide the query plane - a probe that raises is reported as `query_probe_failed:<Type>` rather
  than being swallowed as availability. `Lifecycle.query()` is the single admission point: it takes
  the real native guard through `guard.reader()` and releases it in the same `with` block, so a
  query that completes, raises, or is cut short leaves no lock behind. `drain()` waits a bounded
  time for in-flight queries and, when the budget expires, counts the remainder as cancelled and
  releases the guard anyway; `shutdown()` drains once, releases everything and is idempotent.
  Out-of-range drain budgets are refused with `LIMIT_EXCEEDED` instead of being silently clamped.
  AC1's guard leg is proven with the real `SolutionGuard` on this filesystem: a query is admitted in
  a thread, shutdown runs while it is in flight, and the test asserts the guard ends with
  `held_names == ()` - no stranded lock.
- **C-034** Implement the concrete graphd control client in `src/axiom_mcp/control_client.py`.
  It is a bounded synchronous client for `contracts/control-api-v1.md` and the production
  implementation of the `ControlPlane` protocol the delegation tools already use. AC1's audience
  half is enforced before a socket opens: the client refuses any credential whose audience is not
  `axiom-graphd-control`, so an MCP token can never be replayed to the daemon and a control
  credential can never answer an MCP call. Retries are bounded and cannot be widened by
  configuration - only `429`, `503` and transport failures are retried, `max_attempts` is capped at
  `MAX_ATTEMPTS_LIMIT` (5), the backoff doubles from `0.25` s to a `2` s cap, and a `Retry-After`
  header is honoured but clamped - while every other 4xx is raised on the first response so a
  malformed request is never amplified into a retry storm. A canonical code in the daemon's
  `{code,message,retryable,details,request_id}` body wins over the status fallback, a bounded
  identifier pattern keeps a path-shaped id off the wire, and a transport failure becomes a
  retryable `DAEMON_UNAVAILABLE`. AC1's outage half is proven against the real registry, guard and
  query engine: with the daemon unreachable `graph_reconcile` reports `DAEMON_UNAVAILABLE` while
  `graph_query` still answers from the pinned generation with `freshness: unknown` - degraded
  reader service, not a failed gateway. Adds 26 regression tests (positive, negative and
  failure-boundary legs) over `httpx.MockTransport` in `tests/test_control_client.py`; no socket is
  opened and no live daemon is claimed.
- **C-033** Register the `graph_version` tool in `src/axiom_mcp/tools/version.py`. The tool
  reports selected components and their compatibility under the `read` capability and is the one
  surface that can never act. AC1 is enforced by a tripwire rather than promised: a regression test
  replaces `axiom_mcp.update.apply_plan` (and `delegation_argv`) with a function that fails the
  test if it is reached, and asserts that a full call leaves it untouched while the handler exposes
  no `install`/`apply` keyword. Compatibility reuses the same pin checker the CLI and the
  runtime-pin test use, so `compatible` is true only when the checker returned no reason, and a
  mismatch raises a `runtime_pin_mismatch` warning; `update` stays `not_checked` until
  `check_update: true`, and then runs the read-only `update.check_update` and relays its fields
  verbatim. A component this process cannot observe is reported as unobserved rather than guessed
  (`axiom-graphd` only when a control client is wired to this process, `axiom-specs`/`axiom-skills`
  as `not_observable_from_this_component`), an unknown component name is a `VALIDATION_ERROR`
  naming `allowed`, and every answer carries `installation.implicit_install: false` with
  `applies_via: cli_or_skill_workflow`. Adds 16 regression tests (positive, negative and
  failure-boundary legs) in `tests/test_tools_version.py`.
- **C-032** Register the `graph_verify` tool in `src/axiom_mcp/tools/verify.py`. The tool submits
  a bounded verification request through the `ControlPlane` protocol against an
  `expected_fingerprint` (64 hexadecimal characters) or a `target_event_seq` barrier, and reports
  the four verification modes `hash`, `schema`, `catalog` and `source` **separately** instead of
  collapsing them into one "verified" boolean. AC1's second half is enforced, not documented: every
  answer names its `basis` (`archive` or `working_tree`), and an `archive` basis whose `source`
  dimension claims `current` is refused as `DAEMON_UNAVAILABLE` with
  `{dimension: "source", basis: "archive"}`, because an archived checkpoint cannot speak for the
  live filesystem. Absence and novelty never become a pass either: a dimension the daemon did not
  report is `not_run` with a `dimension_not_reported:<name>` warning and an unrecognised mode
  becomes `unknown` with an `unrecognised_mode:<name>` warning. The tool reads no snapshot (a
  source spy that raises on any read is asserted untouched), the `checkpoint` capability is
  required before the daemon is consulted, an invisible solution is `NOT_FOUND`, and an
  asynchronous answer returns the job handle with `verification: null` plus a
  `verification_incomplete` warning. Adds 23 regression tests (positive, negative and
  failure-boundary legs) in `tests/test_tools_verify.py`.

- **C-031** Register the `graph_job` tool in `src/axiom_mcp/tools/job.py`. `graph_job` reads or
  cancels one daemon job through the `ControlPlane` protocol and enforces three bounds itself.
  The handler makes exactly one control-plane call - it never loops and never sleeps, so a client
  cannot make the gateway spin on a queue, and the daemon keeps ownership of waiting - which a
  test asserts by counting recorded calls. Job ownership is verified on the answer: the job's
  `solution_id` must be visible to the token and registered locally, otherwise the answer is
  `NOT_FOUND`, the same answer an unknown job gets, so job ids are not an enumeration oracle.
  Cancel is capability-checked `before` the daemon - a read-only token gets `FORBIDDEN` and the
  control plane is never called - while a `status` read has to ask the daemon who owns the job and
  therefore enforces ownership on the answer; the asymmetry is documented. The request is closed,
  `job_id` must match `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`,
  so a path-shaped id never reaches the
  daemon, `wait_ms` is bounded to `0..30000` and is refused on a cancel. The answer is projected
  through a closed allowlist: the `job.schema.json` fields plus `retry_after_ms`, a derived
  `terminal`, and `progress` restricted to the documented units - a percentage field is dropped
  with a `percentage_not_carried` warning rather than relayed - and a state outside the schema enum
  is `DAEMON_UNAVAILABLE` instead of being echoed through. Adds 23 regression tests (positive,
  negative and failure-boundary legs) in `tests/test_tools_job.py`.

- **C-030** Register the `graph_reconcile` tool in `src/axiom_mcp/tools/reconcile.py`. The
  tool authorizes, then delegates through the `ControlPlane` protocol, and never reads a snapshot,
  parses a source file or writes a shard - which is AC1's "Python never parses source or writes
  graph shards" in executable form: `tests/test_tools_reconcile.py` swaps in a source spy that
  raises on any read and asserts it is never touched, and asserts the daemon was asked for exactly
  one reconcile. The request is closed and stricter than the transport contract in one place: the
  seed lists `solution,projects,scope,wait_timeout_ms,reason`, and this revision requires a
  bounded audit `reason` (<=200 characters) because the string travels to the daemon's journal.
  `scope` is `dirty` (default), `project` or `full`; `project` requires an explicit project
  selector and `scope != project` refuses one, so an ambiguous request is a `VALIDATION_ERROR`
  rather than a guess. `wait_timeout_ms` is bounded to `0..30000` so an MCP call cannot park a
  request on a queue. A read-only token is `FORBIDDEN` before the control plane is consulted; an
  invisible solution is `NOT_FOUND`; an unknown project is `NOT_FOUND`. The daemon's answer is
  never passed through verbatim: a job answer is projected to `{job_id, state, target_event_seq,
  retry_after_ms}` and a `state` outside `contracts/schemas/job.schema.json` is
  `DAEMON_UNAVAILABLE`, while an existing-publication answer is projected to
  `{catalog_generation_id, verification, dirty}`. An unreachable or unusable control plane is
  `DAEMON_UNAVAILABLE` with `retryable: true`. Adds 21 regression tests (positive, negative and
  failure-boundary legs) in `tests/test_tools_reconcile.py`.

- **C-029** Register the `graph_query` tool in `src/axiom_mcp/tools/query.py`. One bounded
  dispatcher turns a closed request into exactly one engine call over the pinned generations read
  through the trusted registry, and answers with the contract envelope built by
  `query/envelope.build_envelope` and packed by `query/budget.pack_response`, so the byte cap is
  measured over the whole document and a trimmed answer says `truncated`. The request is closed
  *per operation*, which is what makes "no arbitrary SQL, Cypher, shell or code evaluation" a
  property of the parser rather than a promise: `sql`, `cypher`, `command` and `script` are not
  fields, an unknown key is a `VALIDATION_ERROR` naming only the key, an operation outside the
  documented eight is `UNSUPPORTED_OPERATION`, and a selector that cannot apply to the requested
  operation (`target` on `search`, `direction` on `changes`) is refused instead of ignored.
  `pinned` requires the `catalog_generation_id` it pins and answers `SNAPSHOT_EXPIRED` when the
  lane no longer publishes that vector; `require_fresh` needs the `reconcile` capability - a
  read-only token is `FORBIDDEN` before the control plane is consulted - and with it enqueues a
  bounded reconcile and answers `NOT_READY` carrying the job id rather than serving a snapshot as
  fresh. Every answer is `freshness=unknown` with `verification.mode=none`: a pinned generation
  proves which bytes answered, not that the source was re-read. A cursor is re-checked against
  generation, query, scope and capability on every use, each drift with its own named refusal,
  and an unreadable cursor id is `NOT_FOUND` rather than a leaked engine error. `changes` carries
  head-side added and modified facts; because the baseline vector cannot be pinned through the
  read session at this revision, a named non-current baseline answers `missing_baseline` with a
  `baseline_not_pinnable` warning instead of a diff against whatever is current. Adds 28
  regression tests (positive, negative and failure-boundary legs) in `tests/test_tools_query.py`.
  Documentation corrected: the budget rules claimed an over-budget value is a
  `VALIDATION_ERROR`; the code refuses it with `LIMIT_EXCEEDED`.

- **C-028** Register the `graph_status` tool. Add the `src/axiom_mcp/tools/` package: `catalog.py`
  (the six-tool catalog with its capability, read-only/destructive/open-world annotation and the
  registering task), `context.py` (the closed-argument parser and the `ToolContext` every handler
  runs inside) and `status.py` (`graph_status`). The answer is a closed object - an unexpected field
  is a `VALIDATION_ERROR` whose message names the key and never its value - and it reports the
  snapshot and the optional daemon half as two separate facts: the generations, records,
  fingerprints and coverage come from the pinned generations read through the trusted registry with
  the same bounded read session a query uses, and the daemon is consulted only when `include_daemon`
  asks for it. Freshness is never overstated: a read session's `manifest_hash` proves the bytes are
  the published ones and *not* that the snapshot is newer than the source tree, so a daemon that
  merely reports `fresh` without an `inventory_hash` verification carrying the fingerprint it
  recomputed is downgraded to `unknown` with the reason in `warnings`, and an unreachable daemon is
  an unavailable plane with the failure named rather than a failed status answer. An unauthorized
  solution and an unregistered one both answer `NOT_FOUND`, so the tool is not a solution
  enumeration oracle, and a project outside the token's scope is equally invisible. A pinned
  generation whose coverage block is unusable is `SNAPSHOT_CORRUPT`, not a complete answer.

- **docs** Correct the guard limits in `docs/guides/snapshots.md`. The guide claimed the
  Windows `LockFileEx` backend was not present and that `src/axiom_mcp/guard/` ships
  `locks_posix.py` only, which was stale after C-011 and V2-019: `locks_windows.py` ships and
  `tests/test_guard_windows.py` exercises it against the real primitive on this host, including
  exclusion between two independent processes. The unverified item is narrowed to what is
  actually unrun - the Rust holder versus Python holder exclusion, whose foreign-holder leg
  skips unless `AXIOM_GUARD_HOLDER_ARGV` names an ABI-conformant holder - and the POSIX `flock`
  primitive stays recorded as absent from this host's run. Documentation only; no product code
  changed.
- **C-027** Expose freshness coverage and verification. Add `src/axiom_mcp/query/envelope.py` and
  `tests/test_query_envelope.py`. `build_envelope` writes only the contract's success keys and keeps
  the two facts apart: `project_generations` lists the exact pinned member generations (and
  `catalog_generation_id` is a deterministic digest over exactly those), `freshness` and `coverage`
  are resolved independently, and `verification` records why the freshness claim is believed. The
  flattering direction is refused: a freshness that was not reported or is outside the allowlist is
  `unknown` rather than `fresh`; a `fresh` claim backed only by a watcher hint is downgraded to
  `unknown` with a warning, because only an `inventory_hash` verification carrying the
  `source_fingerprint` it recomputed supports `fresh`; a member pinning no usable coverage status is
  refused instead of being called complete; and a requested project that is not pinned caps coverage
  at `partial` and is named in the warnings. A `verification` block that contradicts its own mode is
  refused rather than stored.
- **C-026** Bind cursors to snapshot and query. Add `src/axiom_mcp/query/cursor.py` and
  `tests/test_query_cursor.py`. A cursor is only meaningful inside the request that produced it:
  `CursorStore.issue` binds it to the catalog generation, the operation and its parameters, the
  scope, the capability and an expiry, and `CursorStore.resolve` returns the record only for that
  exact binding. Every mismatch is refused with a named reason - `unknown`, `expired`, `generation`,
  `scope`, `query`, `capability` - so a cursor can never be replayed against another project,
  another generation or another query and be misread as paging. Re-resolving the identical binding
  is paging, not reuse; two issues never collide because the token includes a sequence; the query
  hash is key-order independent; and an out-of-range ttl plus an unparseable generation are refused
  rather than clamped.
- **C-025** Implement response byte-budget packer. Add `src/axiom_mcp/query/budget.py` and
  `tests/test_query_budget.py`. `pack_response` measures the encoded compact JSON, UTF-8, of the
  whole document including every metadata key, so the cap cannot be spent separately from the
  facts. When it does not fit, whole items are trimmed from the end of `nodes`, `edges`,
  `unresolved`, `candidates` and finally `warnings`, and `dropped` reports exactly what went; a
  single item too large to fit is recorded as `oversized_item` rather than partially serialised,
  and metadata that cannot fit at all raises `BudgetExceeded` instead of returning an over-cap
  document. An unmeasurable document and an out-of-range `max_bytes` are refused explicitly.

- **C-024** Implement generation change comparison. Add `src/axiom_mcp/query/changes.py` and
  `tests/test_query_changes.py`, plus an optional `schema_major` on `Graph` and
  `graph_from_documents` in `src/axiom_mcp/query/model.py`. `changes(head, baseline)` reports
  `added`/`removed`/`modified` node and edge facts per project, matching by pinned id so a rename
  is an add plus a remove rather than a silent edit, and every answer names the exact head and
  baseline generations it compared. When the comparison is not valid it is refused with its own
  status instead of guessed: `missing_baseline`, `incompatible_scope` (different member sets),
  `unknown_schema` (an undeclared schema major on either side) and `incompatible_schema` (two
  different majors), each with a warning and no diff to misread.- **C-023** Implement shortest bounded path query. Add `src/axiom_mcp/query/path.py` and
  `tests/test_query_path.py`, plus public `incident_edges`/`other_end` helpers in
  `src/axiom_mcp/query/model.py` for operations that need their own walk. `path` returns the
  shortest bounded path from `target` to `target_to` (`direction` defaults to `both`), and every
  edge on it is a real pinned edge resolved to the next node. The negative answer is split so a
  budget stop can never be read as "no path": `budget_exhausted` (a `max_nodes`/`max_edges` stop)
  is distinct from `proven_absent`, which requires the frontier to empty without being cut, no
  unpinned requested member, no depth bound reached with a live frontier, and no unresolved
  reference naming a visited node. `incomplete_reasons` names whatever made the absence
  provisional.- **C-022** Implement conservative impact closure. Add `src/axiom_mcp/query/impact.py` and
  `tests/test_query_impact.py`. `impact` is the bounded reverse closure of a target, split into
  `proven` (reachable only through exact/annotated edges) and `potential` (everything else, kept
  but labelled), because section 5 requires conservative static impact rather than an authoritative
  answer. `exhaustive` is true only when nothing cut the view: a depth bound (probed one hop
  further, or admitted at the maximum depth), a `max_nodes`/`max_edges` stop, an unpinned member,
  an unresolved reference naming a closure node, and a searched project whose pinned coverage is
  not `complete` each force it false with a named reason. The shared `bounded_walk` gained an
  optional `resolutions` filter, and `model.unresolved_references` now backs both this operation
  and C-021's caller lookup.- **C-021** Implement cross-project caller lookup. Add `src/axiom_mcp/query/callers.py` and
  `tests/test_query_callers.py`, and record unpinned members on `GraphSet` (`missing_projects`,
  `searched_projects`) in `src/axiom_mcp/query/model.py`. A caller is found by the *global*
  reverse index `GraphSet` builds over every pinned member, so a call from another project is not
  missed; `searched_projects` names what the answer read, `per_project`/`cross_project` say where
  the callers came from, and the walk is incoming-only so `direction` is not a parameter.
  `complete`/`incomplete_reasons` mark an answer that must not be read as "no callers": an
  unpinned member, a budget-stopped walk, or an unresolved edge whose `unresolved_target` names the
  target (reported, never followed, never counted) all make it false with a warning. Default
  `edge_kinds` is every pinned kind except `CONTAINS`, because a container is not a caller.- **C-020** Implement dependency and neighbour traversal. Add `src/axiom_mcp/query/neighbors.py`
  and `tests/test_query_neighbors.py` over the shared `model.bounded_walk` primitive. `neighbors`
  takes `direction` (`outgoing`/`incoming`/`both`) and an `edge_kinds` allowlist that apply to the
  *edge*, so the two filters compose: `incoming` never returns an edge whose source is the target,
  and a filtered-out kind never contributes a hop. `dependencies` is not a second implementation -
  it is `neighbors(direction="outgoing")` with the dependency kinds as its default - so "what this
  depends on" and "what points at this" cannot disagree about the graph. A cycle terminates on the
  visited set, budgets stop expansion with `truncated` and the budget name in `reasons`, and an
  unresolved edge is reported in `unresolved` rather than followed or silently dropped.- **C-019** Implement context projection. Add `src/axiom_mcp/query/context.py` and
  `tests/test_query_context.py`, plus `Walk`/`bounded_walk` and an optional pinned `coverage`
  block on `Graph` in `src/axiom_mcp/query/model.py`. `context` honours `depth` literally,
  projects exactly the requested `identity`/`source_locations`/`relations`/`coverage` sections,
  and records `truncated` with the budget name and the nodes where expansion stopped when
  `max_nodes`/`max_edges` cut the neighbourhood short, so a partial answer cannot be read as a
  complete one. The breadth-first walk is shared (`bounded_walk`) and terminates on a cycle by
  visited set; unresolved edges are collected and reported rather than followed silently. An
  ambiguous target returns candidates with `status="ambiguous_target"` and no invented
  neighbourhood, a target that matches nothing is `TargetNotFound`, and an out-of-range depth is
  rejected instead of clamped.
- **C-018** Implement indexed symbol search. Add `src/axiom_mcp/query/` (the package docstring
  plus `model.py`) and `src/axiom_mcp/query/search.py`, `tests/test_query_search.py`,
  `tests/test_query_support.py` and `docs/query-engine.md`. The card names ten modules
  (`query/search.py` through `query/envelope.py`) but a package cannot be one module: the shared
  pinned node/edge model and the graph index C-018 needs are `src/axiom_mcp/query/model.py` plus
  `src/axiom_mcp/query/__init__.py`, both inside the card's `src/axiom_mcp/query/**` deliverable,
  and the justified path addition is recorded here and in the task evidence. The model has no
  source-body field at all, so `repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md` section 6's "source
  positions/symbols instead of full source" default cannot be violated by accident.
  `search_symbols` answers exact id/qualified-name/name from an index, widens to a
  `max_nodes`-bounded scan for prefix and substring queries, returns every candidate at the best
  rank instead of silently choosing one when a name is ambiguous, and reports `truncated` plus a
  warning when the limit caps the answer. Bounds outside the canonical request ranges raise
  `LimitRejected` rather than being clamped.
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
- **C-017** Handle missing, corrupt and archived snapshots. Add `src/axiom_mcp/recovery.py`,
  `tests/test_recovery.py` and a recovery section in `docs/snapshot-reader-core.md`.
  `read_with_recovery` runs a whole-catalog loader under a bounded retry, so each attempt is
  all-or-nothing and a failure carries no partial data. A missing member is `partial` under
  `allow_partial` and `PROJECT_UNAVAILABLE` with no data under `require_complete`. Missing or
  corrupt generations retry up to the bound and then report `SNAPSHOT_UNAVAILABLE`/
  `SNAPSHOT_CORRUPT`; a superseded (collected) generation is `SNAPSHOT_EXPIRED` on the first
  attempt and is never answered from the current lane data. Snapshot-only and pinned reads
  report `freshness=unknown` and refuse a caller-supplied live freshness, so freshness is never
  fabricated.

- **C-016** Cache by immutable generation identity. Add `src/axiom_mcp/cache.py`,
  `tests/test_cache.py` and a cache section in `docs/snapshot-reader-core.md`. `CacheKey` is the
  pointer-free identity `(schema, profile, generation_id, shard_sha256)` plus the manifest
  entry path, so a republished pointer derives a different key and an older entry can never be
  hit or relabelled. `SnapshotCache.put` refuses bytes that do not hash to the keyed digest and
  `SnapshotCache.get` re-hashes before returning, so mislabelled data cannot be stored or
  served. `max_entries` and `max_bytes` bound the cache by LRU eviction; a payload larger than
  the whole budget is refused without evicting live entries; `evict_generation` invalidates by
  generation.

- **C-015** Release read locks before response streaming. Add `src/axiom_mcp/read_session.py`,
  `tests/test_read_session.py` and a read-session section in `docs/snapshot-reader-core.md`.
  `ReadSession.load` copies the pointer, manifest and required shards through the bounded
  `copy_shards` path while `SolutionGuard.reader` holds `data.lock`, releases the guard, then
  verifies the hashes with no lock held and refuses to return if any lock is still held.
  `render` and `stream` raise `GuardStillHeld` if a lock is held, so the response is always built
  after the release; the client can stall without keeping a publisher out of the lane. The
  snapshot records `guard_held_during_copy`/`parsed_after_guard_release` and reports
  `freshness="unknown"` because no daemon freshness is observable from here.

- **C-014** Load bounded shards and reject traversal. Add `src/axiom_mcp/shards.py`,
  `tests/test_shards.py` and a bounded-shard section in `docs/snapshot-reader-core.md`. The
  load runs four checks in a fixed order, all of them before any JSON parser is involved: the
  entry's role must own the directory its path claims; the declared byte size must fit the
  caller's `ShardLimits` cap and the plan's declared total must fit the plan budget, both
  checked before the file is opened; the path must not be a symlink, must be a regular file
  and must resolve inside its own generation directory; and the read itself is bounded to
  `cap + 1` bytes, so a shard that grew on disk is refused without ever being materialised.
  Only then does `Manifest.verify_shard` apply the canonical length/digest/parse/record-count
  rule to the copied bytes. `ShardLimits` refuses a cap above the canonical 16 MiB instead of
  silently clamping it, and `copy_shards` is deliberately separate from `verify_copied` so
  C-015 can copy under the shared guard and parse after releasing it. AC1 is observed by the
  negative cases: a leaf symlink, a symlinked parent that leaves the lane, a symlinked parent
  that stays in the lane but outside the generation, an over-declared shard whose file is a
  directory, an oversized shard whose bytes are not valid JSON, a plan over budget passed a
  reader that fails if it is ever called, a forged manifest entry, and substituted bytes of
  the same size.
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
