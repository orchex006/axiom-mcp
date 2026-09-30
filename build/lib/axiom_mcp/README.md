# axiom-mcp

Python query gateway for the Axiom Graph Ecosystem: a FastAPI + official MCP SDK service that
answers graph operations over processed JSON without touching the graph runtime.

This is the **package README**. It is written for a consumer who has the installed artifact, so it
carries only what that consumer needs: how to install, how to verify, how to launch, and where the
rest of the documentation and the normative contracts live. The full procedure — the versioned
environment layout, the refusal table, upgrade and rollback — is in
[Installation and upgrade](docs/install-and-upgrade.md).

## Install

The interpreter line and the dependency set are declared in `pyproject.toml`; this README does not
restate them, so the two cannot drift.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install axiom_mcp-<version>-py3-none-any.whl
```

To build the artifact from a checkout:

```powershell
python -m build --wheel --no-isolation
```

`axiom-mcp` is position 2 of the ecosystem install order: the `axiom-graphd` core release installs
first, the gateway second, and the `axiom-skills` bundle third. No prerequisite of this component
requires elevation, WSL, Docker, Bash or Node.js.

## Verify

```powershell
axiom-mcp version          # the runtime, SDK and dimension pins this build reports
axiom-mcp doctor           # six sections; exit 4 means "not ready", which is correct for a bare install
```

`doctor` reports `runtime`, `sdk_surface`, `protocol`, `dimensions`, `credentials` and `readiness`
separately, and it never treats a listening socket as readiness. Exit codes: `0` ready (possibly
with warnings), `4` not ready, `5` credential unusable or unregistered, `9` incompatible, `2`
invalid invocation. Incompatibility is checked before not-ready.

## Run

stdio is the default and carries protocol only on stdout, with every diagnostic on stderr:

```powershell
python -m axiom_mcp.stdio --name axiom-mcp
```

Streamable HTTP is an explicit opt-in that requires a `Host` and `Origin` allowlist; it binds
loopback by default and refuses a non-loopback bind without an explicit acknowledgement:

```powershell
python -m axiom_mcp.http --name axiom-mcp --host 127.0.0.1 --port 8765 `
  --allow-host 127.0.0.1:8765 --allow-origin http://127.0.0.1:8765
```

A host starts the gateway without a shell by consuming the launch document this surface renders —
one absolute interpreter, an argv list, a working directory, a scoped environment and a digest:

```powershell
axiom-mcp-entrypoints plan   --mode stdio --install-root <ABSOLUTE-ROOT> --write-lock
axiom-mcp-entrypoints verify --mode stdio --install-root <ABSOLUTE-ROOT>
```

Neither transport identifies the host product: admission is by an allowlisted address and a
scoped bearer token, not by a host name.

## Upgrading

A running process is never pip-upgraded in place. `axiom-mcp update check` reports what is
installed and what is available — and never renders an unknown answer as up to date —
and `axiom-mcp update apply --plan <plan.json>` validates an approved plan and delegates it to the
external updater. See [update-plan-delegation](docs/update-plan-delegation.md).

## Documentation

[docs/README.md](docs/README.md) indexes the guides owned by this component. Start with
[Installation and upgrade](docs/install-and-upgrade.md) and then:

| Topic | Document |
| --- | --- |
| Install, upgrade, rollback, refusals | [install-and-upgrade.md](docs/install-and-upgrade.md) |
| Versioned environments and the lockfile | [release-packaging.md](docs/release-packaging.md) |
| CLI `version` and `doctor` | [cli-version-and-doctor.md](docs/cli-version-and-doctor.md) |
| Locked native entrypoints | [locked-entrypoints.md](docs/locked-entrypoints.md) |
| Update check and approved-plan delegation | [update-plan-delegation.md](docs/update-plan-delegation.md) |
| stdio and Streamable HTTP transports | [stdio-transport.md](docs/stdio-transport.md), [http-transport.md](docs/http-transport.md) |
| Host, Origin and bearer-token policy | [security-host-origin-auth.md](docs/security-host-origin-auth.md) |
| Tool catalog and response shapes | [reference/mcp.md](docs/reference/mcp.md) |
| Runtime and SDK pin | [runtime-compatibility.md](docs/runtime-compatibility.md) |

This README ships inside the installed package as `axiom_mcp/README.md`. The `docs/` tree does
not: it is repository documentation, so the links above resolve against a checkout of this
repository rather than inside a package-only install. Read this file for the install, verify and
launch path, and follow the links when you have the source tree.

Documentation for code is owned here and changes in the same branch as the code. Shared normative
contracts stay canonical in `axiom-specs` and are resolved through the pinned revision — the
immutable pin and its contract digests are recorded in `spec.lock.json` — never as a local editable
copy.

## Not implemented in this revision

The CLI registers `version`, `doctor` and `update check|apply`; the other subcommands in the
canonical CLI contract belong to later tasks, and an unregistered subcommand exits `2`. No daemon,
publication or GC runtime exists here — the writer side is `axiom-graphd`. No ecosystem-level
`uninstall` verb is defined by any owner document. `release/package.py` is operator tooling and is
not part of the installed distribution. [Installation and upgrade](docs/install-and-upgrade.md)
section 11 carries the complete list.

## Development

This repository's required checks are:

```bash
python -m pytest tests -q
python -m ruff check .
python -m ruff format --check .
```

Every task runs all three against the final bytes and records the real command and exit code; a
check that did not run is recorded as unverified and never reported as passing.

## Ownership and platform status

This component owns the MCP server surface — the tool catalog, the bounded query engine, the
snapshot registry and reader, the reader/writer guard, redaction and the error envelope, the two
transports, and the `version`/`doctor`/`update` CLI with the locked launch entrypoints. It does
**not** own the graph runtime (`axiom-graphd`), the ecosystem contracts (`axiom-specs`) or the
skills (`axiom-skills`): public commands, schema and layout versions, the control API, guard
semantics and migration decisions change in `axiom-specs` first.

The native target set and its certification status are owned by `axiom-specs`
(`compatibility/platform-matrix.json`). Support is never inferred from compilation or from a
passing test run alone, and where a leg could not be executed on this host it is recorded as
`not_run` in the pages under `docs/` rather than as passing.
