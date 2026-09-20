# axiom-mcp

Python query gateway for the Axiom Graph Ecosystem: a FastAPI + official MCP SDK
service that answers graph operations over processed JSON without touching the
graph runtime. Specification baseline: `2.0.0-draft.1`.

**Owner scope:** the MCP server surface - the six-tool catalog, the bounded query
engine, the snapshot registry and reader, the native reader/writer guard,
redaction and the error envelope, the stdio and Streamable HTTP transports, and
the `version`/`doctor`/`update` CLI plus the locked launch entrypoints.

It does **not** own the graph runtime (`axiom-graphd`), ecosystem contracts
(`axiom-specs`) or skills (`axiom-skills`). Public commands, schema/layout
versions, the control API, shared reader/writer guard semantics and migration
decisions change in `axiom-specs` first; this repository implements that contract
and must not redefine it locally.

## Repository layout

```text
pyproject.toml           package metadata, the pinned dependency set, ruff/pytest config
spec.lock.json           immutable axiom-specs pin plus contract digests
src/axiom_mcp/           gateway package (49 Python modules)
  cli.py                 `version`, `doctor` and `update check|apply`
  entrypoints.py         locked native launch surface (`plan`, `verify`); starts no server
  http.py                Streamable HTTP transport mounted at `POST /mcp`
  stdio.py               stdio transport: protocol frames on stdout, diagnostics on stderr
  security.py            capability model; an unknown tool is a read, never a widening
  errors.py              canonical error envelope and the redaction boundary
  manifest.py            canonical manifest bytes, digests and shard closure
  registry.py, catalog.py, shards.py, cache.py
                         snapshot registry, catalog vectors and shard loading
  read_session.py        one pinned generation vector per request
  recovery.py            collection answering `SNAPSHOT_EXPIRED` instead of a wrong answer
  paths.py               portable wire paths versus native bindings
  guard/                 native reader/writer guard: engine, protocol, POSIX and Windows backends
  query/                 bounded query engine: search, context, neighbors, callers, dependencies,
                         impact, path, changes, cursor, budget, envelope
  tools/                 the six-tool catalog: status, query, reconcile, job, verify, version
  version.py             the runtime/SDK/dimension pins this component reports
  lifecycle.py, control_client.py, update.py, sdk_compat.py
release/                 operator packaging and compatibility spikes (not part of the distribution)
docs/                    implementation documentation owned by this component
tests/                   44 test modules
```

## Build and verify

```bash
python -m pytest tests -q
python -m ruff check .
python -m ruff format --check .
```

These are this repository's required checks, established by `C-001`. Every later
task runs them against the final bytes and records the real command and exit code;
a check that did not run is recorded as unverified and never reported as passing.

The runtime is pinned by `pyproject.toml` (`requires-python = ">=3.13,<3.14"`)
together with an exact dependency set (`mcp==1.28.1`, `fastapi==0.139.2`,
`uvicorn==0.51.0`, `starlette==1.3.1`, `pydantic==2.13.4`, `anyio==4.14.2`,
`httpx==0.28.1`); [runtime-compatibility](docs/runtime-compatibility.md) records
that pin.

## Interfaces

| Surface | Contract this revision implements |
| --- | --- |
| Tool catalog | `graph_status`, `graph_query`, `graph_reconcile`, `graph_job`, `graph_verify`, `graph_version` - see [MCP tools and response reference](docs/reference/mcp.md) |
| Streamable HTTP | `POST /mcp`, official SDK transport at an exact path with no redirect |
| stdio | Protocol frames on stdout only, every diagnostic on stderr, no banner |
| Health | `GET /healthz` - minimal process health, makes **no** readiness claim |
| Readiness | `GET /readyz` - query and control availability reported separately, `503` with `NOT_READY` while a plane is down |
| CLI | `axiom-mcp version`, `axiom-mcp doctor`, `axiom-mcp update check\|apply` - see [CLI: version and doctor](docs/cli-version-and-doctor.md) |
| Locked launch | `axiom-mcp-entrypoints plan\|verify` - see [Locked native entrypoints](docs/locked-entrypoints.md) |

Update, bootstrap and install are deliberately **not** MCP tools: they travel
through the CLI or the skill workflow, so no MCP tool downloads or executes an
update by default.

## Specification pin

`spec.lock.json` pins the immutable `axiom-specs` revision this gateway was
implemented against, plus a SHA256 digest for each contract it is built against.
It is currently an **unapproved draft pin**: no released specification revision
exists, so the pin is not owner approval and carries no release coverage.
`spec.lock.example.json` is a shape example only and is never a valid pin.

Validate the pin offline against a checkout of the pinned content:

```bash
python tools/spec-lock-check.py --lock spec.lock.json --spec-root <axiom-specs-checkout>
```

Default mode must accept (`immutable revision and pinned digests verified`).
`--release` deliberately still rejects with
`release-coverage-missing:conformance/fixture-index.json`, because release
coverage is only meaningful against a released revision.

## Platform support status

Windows x64, Linux x64, macOS arm64 and macOS x64 are required native targets.
No target is certified: `compatibility/platform-matrix.json` is
`required_not_certified` with **0 of 4** mandatory targets certified, there are no
released distributions, and this repository holds no native install/run/uninstall
evidence. Support is never inferred from compilation or from a passing test run
alone. Where this Windows host could not execute a leg - the POSIX guard backend,
the cross-language Rust-holder exclusion, and real host-process launches - the
pages under `docs/` record it as explicitly `not_run` rather than as passing.

## Documentation

[docs/README.md](docs/README.md) indexes the implementation guides for this
component. Docs for code live with the code owner and change in the same branch;
shared normative contracts stay canonical in `axiom-specs` and are resolved
through the pinned revision, never as a local editable copy.
