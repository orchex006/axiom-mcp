# Installation and upgrade guide — axiom-mcp

Owner: `axiom-mcp`. Contract: the ecosystem installation contract
(`contracts/ecosystem-installation-contract.md`) and the version, check, update and release
contract (`docs/20-VERSION-CHECK-UPDATE-RELEASE.md`) at the pinned `axiom-specs` revision.
This document records how to install, verify, upgrade and roll back **this** component; it
implements that contract and does not redefine it.

Companion documents: [release-packaging.md](release-packaging.md) (the packager that owns the
install-root layout), [locked-entrypoints.md](locked-entrypoints.md) (the launch document),
[cli-version-and-doctor.md](cli-version-and-doctor.md) (`version` and `doctor`),
[update-plan-delegation.md](update-plan-delegation.md) (`update check|apply`),
[runtime-compatibility.md](runtime-compatibility.md) (the interpreter and SDK pin),
[query-transports.md](query-transports.md) (stdio versus Streamable HTTP).

`axiom-mcp` is position 2 of the fixed ecosystem install order: the `axiom-graphd` core release
installs first and carries the `axiom` CLI, the gateway installs second and consumes the installed
core and the registered solution, and the `axiom-skills` bundle wires hosts third. Installing the
gateway before the core release has no supported state to reach. This guide covers the gateway's
own leg of that order.

## 1. Prerequisites

The interpreter range and the dependency set are **declared in package metadata**, not here. This
guide references them so the two cannot drift:

| Prerequisite | Declared in | Read it with |
| --- | --- | --- |
| Python interpreter line | `pyproject.toml` `requires-python` (mirrored as `axiom_mcp.version.PYTHON_REQUIRES`) | `python -c "import tomllib,pathlib;print(tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['requires-python'])"` |
| Pinned dependency set | `pyproject.toml` `dependencies` (mirrored as `axiom_mcp.version.RUNTIME_PINS`) | `python -c "import tomllib,pathlib;print(tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['dependencies'])"` |
| Console entrypoints | `pyproject.toml` `[project.scripts]` | `axiom-mcp --help`, `axiom-mcp-entrypoints --help` |
| Native target set | `axiom-specs` `compatibility/platform-matrix.json` | not owned here |

No prerequisite of this component requires administrator or root elevation, WSL, Docker, Bash or
Node.js. `tests/test_runtime_pin.py` fails the build when `pyproject.toml` and
`axiom_mcp.version` disagree, so the metadata above is the single source for both.

Two limits apply to the table above. The interpreter was exercised on Windows x64 with Python
3.13.14; no Linux or macOS leg was run for this guide (see section 12). `release/package.py` is
operator tooling and is deliberately **not** part of the installed wheel, so a package-only
consumer has the two console scripts and the library, not the packager.

## 2. What the payload contains

The built wheel carries the `axiom_mcp` package, its `dist-info` metadata, both console
entrypoint declarations, and — since this revision — the package README:

```text
axiom_mcp/                     the gateway package (library + CLI modules)
axiom_mcp/README.md            the package README (ships in the payload, see section 7)
axiom_mcp-<version>.dist-info/ METADATA, WHEEL, entry_points.txt, RECORD
```

The versioned install-root layout that `release/package.py` builds — `current.json`,
`history.jsonl` and `versions/<version>/{lockfile.json,artifact/,venv/}` — is owned by
[release-packaging.md](release-packaging.md); it is referenced here rather than restated.

The payload carries the package README but **not this guide**. `docs/` is repository documentation
and is not packaged, so the relative links in this file resolve against a checkout of this
repository rather than inside a package-only install; a package-only consumer reads
`axiom_mcp/README.md`, which points here by repository path.

## 3. Install

### 3.1 Scratch environment (verified)

This is the shortest verified path, and the one the verification record in section 13 used.
There is no install script and no activation step; the interpreter is named by absolute path.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install axiom_mcp-0.0.0.dev0-py3-none-any.whl
```

```text
Successfully installed ... axiom-mcp-0.0.0.dev0 ...
```

From the repository, build the artifact first:

```powershell
python -m build --wheel --no-isolation
```

### 3.2 Versioned environment (verified)

The update contract requires a package to live in a versioned virtual environment rather than
being pip-upgraded in place. `release/package.py` builds that environment. The install root
**must be absolute**; a relative root is refused before anything is written (section 10).

```powershell
python release/package.py install --install-root <ABSOLUTE-ROOT> --version <VERSION> --artifact <WHEEL>
python release/package.py status  --install-root <ABSOLUTE-ROOT>
```

Observed for `--version 0.0.0.dev0` on the wheel built in section 3.1:

```json
{"action": "install", "active": "0.0.0.dev0", "artifact_sha256": "b610139a…0ad6", "layout": 1, ...}
```

**This step installs the artifact only.** `install_artifact` runs
`pip install --no-index --no-deps`, because reproducibility is the point: pip may use the file it
was given and nothing else, so a packaging run cannot silently pull a different dependency
version than the artifact was built with. The consequence is measured, not assumed — after a
packager install the version environment contains the artifact and `pip` and nothing else:

```text
site-packages: axiom_mcp  axiom_mcp-0.0.0.dev0.dist-info  pip  pip-26.1.2.dist-info
pip freeze --all: axiom-mcp @ file:///<root>/…whl#sha256=b610139a…  pip==26.1.2
```

### 3.3 Supply the pinned dependency set

Because section 3.2 resolves no dependencies, the pinned set from `pyproject.toml` must be
present in the version environment before the gateway can import. Until it is, the surfaces
refuse rather than pretend (both observed, section 10):

```text
axiom-mcp-entrypoints plan --mode stdio --install-root <ROOT>
-> {"ok": false, "code": "sdk_not_installed", "detail": "mcp"}          (exit 2)

<ROOT>/versions/<VERSION>/venv/Scripts/python.exe -m axiom_mcp.cli version
-> ModuleNotFoundError: No module named 'httpx'                          (exit 1)
```

Whichever installer owns the ecosystem leg provisions that set; this repository declares it in
`pyproject.toml` and does not invent a second list.

## 4. Verify

Two commands answer the operator's question. Both are machine-readable with `--json`, and both
are deliberately boring: no secrets on stdout, a stable exit code.

```powershell
axiom-mcp version
axiom-mcp version --json
axiom-mcp doctor
```

Observed on the scratch install of section 3.1:

```text
axiom-mcp 0.0.0.dev0 (build c77d834e1a94)
spec 2.0.0-draft.1 @ 80f44e836ced442e8f3ea33d167bd369ed6796bc
verdict: not_ready (exit 4)
  runtime      ok
  sdk_surface  ok
  protocol     ok
  dimensions   ok
  credentials  warn
  readiness    fail  query_plane_unavailable, control_plane_unavailable
warnings: credentials
```

`not_ready` with exit `4` is the **correct** answer for a bare install. `readiness` reports the
query and control planes, which a freshly installed gateway has not wired to a graphd data plane
yet; `doctor` states its basis in the report itself
(`"process_health_is_readiness": false`) and never treats a listening socket as readiness.

| Exit | Meaning |
| --- | --- |
| `0` | ready, or ready with warnings only |
| `4` | not ready — a data plane is unavailable |
| `5` | the configured credential is unusable or unregistered |
| `9` | incompatible — runtime, SDK surface, protocol or dimension failure |
| `2` | invalid invocation |

Incompatibility is checked before not-ready, so a machine with the wrong SDK reports `9` rather
than a less specific `4`. See [cli-version-and-doctor.md](cli-version-and-doctor.md) for the six
sections and the credential outcomes.

## 5. Configuration and environment the server reads

A reference is configuration; a token is a secret. No variable below carries a token value, and
configuration that carries a literal `token`, `value` or `secret` key is refused.

| Variable | Read by | Effect |
| --- | --- | --- |
| `AXIOM_HOME` | `paths.py`, `registry.py`, `guard/protocol.py` | the state root; an override must be an absolute path or it is refused. Default per `SOURCE-OF-TRUST.md` §5: `%LOCALAPPDATA%\Axiom` on Windows, `~/Library/Application Support/Axiom` on macOS, `${XDG_STATE_HOME:-~/.local/state}/axiom` elsewhere |
| `AXIOM_MCP_TOKEN_REFERENCE` | `cli.py` | selects the credential `doctor` diagnoses (an env variable or a file) |
| `AXIOM_MCP_TOKEN_REGISTRY` | `cli.py` | points at the credential registry (`doctor --registry` overrides) |
| `AXIOM_MCP_BUILD_REVISION` | `cli.py` | the revision `version` reports; authoritative for a released artifact, which carries no checkout |
| `AXIOM_MCP_UPDATE_CHANNEL` | `update.py` | `stable` (default) or `prerelease` |
| `AXIOM_MCP_UPDATE_ORIGIN` | `update.py` | the origin to check; must be allowlisted |
| `AXIOM_MCP_UPDATE_ALLOWED_ORIGINS` | `update.py` | extra allowlisted origins, comma separated |
| `AXIOM_MCP_UPDATE_METADATA` | `update.py` | path to a verified metadata document |
| `AXIOM_MCP_OFFLINE` | `update.py` | when set, no network is consulted and nothing is claimed |
| `AXIOM_MCP_UPDATE_AUTO_CHECK` / `AXIOM_MCP_UPDATE_AUTO_APPLY` | `update.py` | reported in `update_policy`; auto-apply defaults to `disabled` |
| `AXIOM_SPECS_ROOT` | `update.py` | the pinned specification checkout that carries the plan evaluator |
| `AXIOM_MCP_ENTRYPOINT` | `entrypoints.py` | set by a launch plan to record which transport was launched |
| `AXIOM_GUARD_HOLDER_ARGV` | `guard/adapter.py` | the argv of the external guard holder process |

## 6. Transports

Both transports below are implemented in this revision. stdio is the default; HTTP is an
explicit opt-in.

### 6.1 stdio (default)

```powershell
python -m axiom_mcp.stdio --name axiom-mcp
```

stdio carries **protocol only** on stdout and every diagnostic on stderr; there is no banner on
stdout. `serve_stdio` installs a `StdoutGuard` for the session, so a stray text write is diverted
and counted instead of corrupting the stream, and `fileno()` is refused on the guard and its
buffer. Verified by `release/stdio_spike.py`: one stdout line, zero parse errors, negotiated
protocol `2025-11-25`, `stdout_restored: true`, and a deliberate stray write producing zero
stdout bytes. See [stdio-transport.md](stdio-transport.md).

### 6.2 Streamable HTTP (opt-in)

```powershell
python -m axiom_mcp.http --name axiom-mcp --host 127.0.0.1 --port 8765 `
  --allow-host 127.0.0.1:8765 --allow-origin http://127.0.0.1:8765
```

`--allow-host` and `--allow-origin` are required; there is no permissive default. The default
bind is `127.0.0.1:8765`, and a non-loopback bind needs an explicit acknowledgement
(`--allow-non-loopback-bind`, or the equivalent plan flag) or it is refused. Surfaces:
`/mcp` (the one MCP endpoint), `/healthz` (minimal process health, no readiness claim) and
`/readyz` (query and control availability, reported separately). A `Host` or `Origin` outside the
allowlist is refused `FORBIDDEN` (403) even on loopback, and a missing or malformed `Host` is
refused too. See [http-transport.md](http-transport.md) and
[security-host-origin-auth.md](security-host-origin-auth.md).

Neither transport identifies the *host product*. The policy is by value — an allowlisted `Host`
and `Origin`, and a bearer token scoped to an audience and a capability — so a host is admitted or
refused by the addresses and credentials it presents, not by its name.

## 7. Hand-off to host wiring

A host does not activate a shell. `axiom-mcp-entrypoints` renders the launch document a host
configuration consumes: one absolute interpreter, an argv **list** (never a shell string), a
working directory and a scoped environment, plus a digest over all of it.

```powershell
axiom-mcp-entrypoints plan --mode stdio --install-root <ABSOLUTE-ROOT> --write-lock
axiom-mcp-entrypoints verify --mode stdio --install-root <ABSOLUTE-ROOT>
```

Observed against the version environment of section 3.2 after the dependency set was present:

```json
{"entrypoint": "stdio", "component": "axiom-mcp",
 "command": "<ROOT>\\versions\\0.0.0.dev0\\venv\\Scripts\\python.exe",
 "args": ["-m", "axiom_mcp.stdio", "--name", "axiom-mcp"],
 "cwd": "<ROOT>",
 "env": {"AXIOM_MCP_ENTRYPOINT": "stdio", "PATH": "<venv>\\Scripts;C:\\WINDOWS\\system32",
         "VIRTUAL_ENV": "<venv>", "PYTHONNOUSERSITE": "1", "SYSTEMROOT": "C:\\WINDOWS", "WINDIR": "C:\\WINDOWS"},
 "digest": "20077ae69f664323cab0e22921a4872272ac0f834caed75148b84210baa30c73"}
```

```text
axiom-mcp-entrypoints verify --mode stdio --install-root <ROOT>
-> {"ok": true, "modes": ["stdio"], "reasons": []}   (exit 0)
```

`--write-lock` records `entrypoints.json` in the install root, and `verify` re-derives the plan
and reports drift reasons instead of trusting the record. The launch environment is built from
scratch — the caller's environment is not copied — so `PYTHONPATH`, `PYTHONHOME` and the login
shell's `PATH` cannot change what runs, and the digest is stable enough to write down. The
package README that ships in the payload (`axiom_mcp/README.md`) is what a package-only consumer
reads to reach this section.

## 8. Upgrade

An update is never an in-place `pip` upgrade of a running process.

```powershell
axiom-mcp update check
axiom-mcp update check --offline
axiom-mcp update apply --plan <PLAN.json>
```

`update check` never renders an unknown answer as up to date. Observed on the scratch install
with no metadata source configured:

```text
component: axiom-mcp   installed: 0.0.0.dev0
available: unknown     compatible: unknown
channel: stable        source_origin: unconfigured
needs_restart: False   status: unconfigured
reasons: no_verified_metadata_source
```

Statuses: `unconfigured`, `offline`, `not_checked`, `current`, `available`, `blocked`. An update
origin is trusted only when it is the canonical repository
(`https://github.com/orchex006/axiom-mcp`) or an owner-added entry in
`AXIOM_MCP_UPDATE_ALLOWED_ORIGINS`; anything else is reported `blocked` with
`origin_not_allowlisted:<value>` and is never contacted.

`update apply --plan` validates the plan with the specification's own evaluator at the pinned
revision and hands it to the external updater. It refuses a plan whose
`target.install_root` would land on the running installation
(`in_place_upgrade_prohibited:<root>`), a relative root (`install_root_not_absolute`), and a
delegated command that would pip-upgrade in place. When it accepts, it reports the exact argv it
would run — `["axiom", "update", "apply", "--plan", PATH]` — and does **not** run it.

The swap itself is the ecosystem entrypoint's job (`axiom update check|plan|apply|rollback`). At
this revision that verb set is owned by `axiom-graphd`; see section 11.

## 9. Rollback

```powershell
python release/package.py rollback --install-root <ABSOLUTE-ROOT> [--to <VERSION>]
python release/package.py status   --install-root <ABSOLUTE-ROOT>
```

Rollback is deliberately narrow. It chooses the target — an explicit version, or the previous
install recorded in `history.jsonl` — and then refuses if the version directory is absent, proves
the *staged* artifact still hashes to the digest its lockfile recorded, repairs a missing
environment or a drifted frozen set, and only then moves the active pointer.

Observed with two artifact-only versions installed in one root:

```json
{"action": "rollback", "active": "0.0.0.dev0", "restored": true, "repairs": [], ...}
```

Rollback never downloads, never guesses a version, and never installs bytes whose hash no longer
matches the lockfile.

## 10. Refusal behaviour (observed, not intended)

Every row below was executed on this host. Refusals print JSON on stdout (packager) or stderr
(entrypoints/CLI) and exit non-zero.

| Surface | Input | Observed result | Exit |
| --- | --- | --- | --- |
| `release/package.py install` | relative `--install-root` | `{"error": "install_root_invalid", "detail": "install_root_not_absolute:…"}` | 1 |
| `release/package.py rollback` | `--to 9.9.9` (never installed) | `{"error": "rollback_target_missing", "detail": "…\\versions\\9.9.9"}` | 1 |
| `release/package.py rollback` | environment drifted by an out-of-band `pip install` | `{"error": "environment_not_restored", "detail": "0.0.0.dev0"}` | 1 |
| `axiom-mcp-entrypoints plan` | artifact-only environment | `{"ok": false, "code": "sdk_not_installed", "detail": "mcp"}` | 2 |
| `axiom-mcp-entrypoints verify` | install root with no recorded lock | `{"ok": false, "reasons": ["lock_absent"]}` | 2 |
| `axiom-mcp-entrypoints plan --mode http` | no `--allow-host`/`--allow-origin` | `{"ok": false, "code": "transport_allowlist_required", "detail": "127.0.0.1:8765"}` | 2 |
| `axiom-mcp update apply` | unreadable `--plan` path | `axiom-mcp: cannot read plan …: FileNotFoundError` | 2 |

The `environment_not_restored` row is the lockfile doing its job: the frozen set recorded at
install time is the contract, so an out-of-band install into a version environment is reported as
unrestorable drift instead of being silently accepted. Install into the environment through the
packager, not around it.

## 11. Not implemented at this revision

Truthful against the code, and repeated from [query-transports.md](query-transports.md):

- `src/axiom_mcp/cli.py` registers `version`, `doctor` and `update check|apply` only. The other
  subcommands in the canonical CLI contract belong to later tasks; an unregistered subcommand
  exits `2` instead of being ignored.
- `cli.py` declares `EXIT_PARTIAL = 20`, but no code path maps a `partial` coverage status to it
  at this revision; the `coverage` key in the response is the signal a host should read.
- `SNAPSHOT_CORRUPT` is an error code with no contracted HTTP status, so it keeps its surface
  rather than being given an invented one.
- No daemon, publication or GC runtime exists in this repository; `axiom-mcp` reads snapshots and
  drives the control API, while the writer side is `axiom-graphd`.
- No ecosystem-level `uninstall` verb is defined by any owner document (the ecosystem contract
  records it as `undeclared`), so this guide documents no uninstall command. Removing an install
  root and its version directories is the operator's action and is out of scope here.
- `release/package.py` is operator tooling and is not part of the installed distribution.

## 12. Unverified on this host

- **Linux and macOS.** Every leg in this guide ran on Windows x64. The native target set is
  `required_not_certified`; no target is certified by this guide.
- **The POSIX guard backend and the cross-language Rust-holder exclusion.** Not run here.
- **A real host-process launch.** The launch document and its digest were verified, and the locked
  environment was executed, but no host product was driven through it.

An untested leg is recorded as unverified here rather than reported as passing.

## 13. Verification record

Commands executed for this guide, with the observed exit codes. Windows x64, Python 3.13.14,
`mcp==1.28.1`:

| Command | Exit |
| --- | --- |
| `python -m build --wheel --no-isolation` | 0 |
| `python -m venv .venv` then `pip install <wheel>` | 0 |
| `axiom-mcp version` / `version --json` | 0 / 0 |
| `axiom-mcp doctor` | 4 (`not_ready`, expected on a bare install) |
| `axiom-mcp update check` / `--offline` | 0 / 0 |
| `axiom-mcp update apply --plan <missing>` | 2 |
| `release/package.py install` / `status` / `rollback` | 0 / 0 / 0 |
| `axiom-mcp-entrypoints plan --mode stdio --write-lock` | 0 |
| `axiom-mcp-entrypoints verify --mode stdio` | 0 |
| `release/stdio_spike.py`, `release/cli_spike.py`, `release/security_spike.py` | 0 |

Repository required checks for the final bytes of this change are recorded in `Changelog.md`.

## See also

- [README.md](../README.md) — the package README.
- [docs/README.md](README.md) — the component documentation index.
- [locked-entrypoints.md](locked-entrypoints.md), [release-packaging.md](release-packaging.md),
  [update-plan-delegation.md](update-plan-delegation.md) — the surfaces this guide sequences.
