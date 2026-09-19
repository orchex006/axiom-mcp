# Query transports — JSON-first, stdio and HTTP

Owner: `axiom-mcp`. Contract: `repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md` sections 2, 4, 5, 6,
7 and 9 at the pinned `axiom-specs` revision; `docs/12-SNAPSHOT-READ-WRITE-PROTOCOL.md` fixes the
read protocol the transports carry; `contracts/control-api-v1.md` fixes the canonical codes and
their status. This document records implementation, not policy.

Companion documents: [stdio-transport.md](stdio-transport.md) (the JSON-only stdio transport),
[http-transport.md](http-transport.md) (the Streamable HTTP transport),
[locked-entrypoints.md](locked-entrypoints.md) (the launch document and the refusal table),
[query-engine.md](query-engine.md) (the bounded query core both transports sit on) and
[guides/snapshots.md](guides/snapshots.md) (the snapshot race and read-only semantics).

## What "JSON-first" means here

The processed-JSON snapshot is the read model of the ecosystem, and it stays directly usable
without an MCP process. `src/axiom_mcp/query/` answers every operation from generations that
were already copied, verified and released to memory; it never parses application source, never
opens a database and never calls the daemon (`docs/query-engine.md`). The same property holds
one level down: the published JSON files are ordinary files any tool can open.

What JSON-first does **not** mean is that consistency is optional. A raw reader that opens
several files itself holds no reader registration and no lock, so nothing coordinates it with a
collector or with a Git checkout, and it is not covered by the coordinated reader guarantee. A
direct reader stays correct only by pinning one immutable generation, validating every manifest
and shard hash, and reporting a missing or collected generation instead of combining a second
generation into the result - re-reading `current.json` per shard is the `SNP-03` failure. The
full statement of that rule is [guides/snapshots.md](guides/snapshots.md); this document does not
restate the protocol, it records which surfaces carry it.

Three facts are shared by every surface below, and they are the reason a transport cannot
change an answer:

| Fact | Where it is enforced |
| --- | --- |
| One request uses exactly one pinned generation vector | `src/axiom_mcp/read_session.py` (`ReadSession.load` copies under the shared guard and releases before rendering) |
| A cursor is bound to the snapshot, query and scope it was issued for | `src/axiom_mcp/query/cursor.py` |
| A generation that has been collected answers `SNAPSHOT_EXPIRED`, never current data | `src/axiom_mcp/recovery.py` (`require_generation`) |

## The two supported transports, one query core

| Surface | Launched as | Wire shape | Documented in |
| --- | --- | --- | --- |
| stdio | `-m axiom_mcp.stdio --name <name>` | newline-delimited JSON-RPC frames on stdout only; banner and diagnostics on stderr | [stdio-transport.md](stdio-transport.md) |
| Streamable HTTP | `-m axiom_mcp.http --name <name> --host <host> --port <port> ...` | the one MCP endpoint at `/mcp`, event stream plus session id | [http-transport.md](http-transport.md) |

`serve_stdio` installs a `StdoutGuard` for the session, so a stray text write is recorded as a
violation instead of corrupting a protocol frame; `build_gateway` registers `/mcp` through the
pinned SDK mount and adds `/healthz` (minimal process health, no readiness claim) and `/readyz`
(query and control availability, reported separately). Both transports are built over the same
tool catalog and the same query engine, so the tool layer does not branch on transport.
[reference/mcp.md](reference/mcp.md) states the same requirement from the consumer side: an
HTTP client and a stdio client must receive equivalent results for the same request.

A transport is not a synonym for the protocol. A plain REST route that returns a hand-written
`initialize`-shaped JSON body answers HTTP 200 with `application/json`, issues no session id and
never completes an `initialize`; that failure mode is recorded by
`release/http_transport_spike.py` and proven non-conformant in `tests/test_http_transport.py`.

## Locked launch: one interpreter, one mode

A host does not activate a shell to choose a transport. `src/axiom_mcp/entrypoints.py` resolves
the installed version environment, verifies the pinned interpreter and SDK, and renders one
absolute executable plus an argv list. `host_launch_document(plan)` returns exactly
`entrypoint`, `component`, `command`, `args`, `cwd`, `env` and `digest`, and no string form of a
plan is produced anywhere - a joined command line is the failure that document prevents.

The transport is a field of that plan, not a runtime guess. `plan --mode stdio` renders the stdio
argv; `plan --mode http` validates the HTTP opt-in and renders the HTTP argv. The digest covers
the mode, the executable, the argument vector, the working directory, the whole launch
environment and the verified runtime identity (`interpreter`, `python_version`, `sdk_pin`,
`sdk_version`, `lockfile_sha256`), so an edited lock, a replaced environment or a changed argv
reports as drift on `verify`. An install root that holds no recorded lock answers `lock_absent`
with exit `2` before any runtime is resolved.

HTTP is opt-in in the strict sense: the transport allowlist must admit the address the gateway is
about to bind, because a gateway whose own allowlist refuses it answers nothing. The refusal
codes and the exact meaning of each one live in
[locked-entrypoints.md](locked-entrypoints.md#the-refusal-table); the ones that decide whether a
transport starts at all are `mode_unknown`, `http_parameters_on_stdio`,
`transport_allowlist_required`, `wildcard_allowlist_refused`, `bind_host_not_allowed` and
`bind_origin_not_allowed`.

## Telling the failure states apart

The three states this component must never conflate - daemon unavailable, snapshot missing and
incomplete coverage - are named separately, and so are the neighbouring cases that would otherwise
be read as one of them. A host that treats "the daemon did not answer" as "the snapshot is
missing" will either retry a permanent condition or serve a partial answer as a complete one.

| State | Canonical code | Retryable | HTTP status | CLI exit | What it actually means |
| --- | --- | --- | --- | --- | --- |
| Daemon unreachable | `DAEMON_UNAVAILABLE` | yes | 503 | `8` (`EXIT_IO`) | The control plane could not be reached. A read can still be served from the pinned generation, which is the degraded reader service the control client documents. |
| Snapshot missing | `SNAPSHOT_UNAVAILABLE` | yes | 503 | `4` (`EXIT_NOT_READY`) | No usable pinned generation: the required generation or pointer is absent. Retry against the whole catalog, bounded; never mix a second generation in. |
| Snapshot corrupt | `SNAPSHOT_CORRUPT` | no | (not in the contracted map) | `8` (`EXIT_IO`) | The bytes are present but do not validate. A pinned generation whose coverage block is unusable is corrupt, not a complete answer. |
| Generation collected | `SNAPSHOT_EXPIRED` | no | 410 | `4` (`EXIT_NOT_READY`) | The pinned generation has moved past; the correct answer is the expiry, never current data. |
| Member missing | `PROJECT_UNAVAILABLE` | yes | 503 | `3` (`EXIT_NOT_FOUND`) | A catalog member is missing. Coverage is capped at `partial`; an invented empty graph is prohibited. |
| Coverage incomplete | `coverage: "partial"` / `"unsupported"` | - | - | not mapped | The request succeeded over less than the requested scope. This is a successful answer with a truthful coverage key, not an error code. |
| Not ready yet | `NOT_READY` | yes | 503 | `4` (`EXIT_NOT_READY`) | A plane is still coming up; `/readyz` reports it separately from `/healthz`. |

`freshness` and `coverage` stay two keys because they answer two questions: a generation can be
complete for its profile and still stale, and a fresh generation can still be partial
(`src/axiom_mcp/query/envelope.py`). `freshness` is `unknown` unless a report carries a
verification mode that earns `fresh`; an unusable coverage block is refused rather than
described, and a member that could not be pinned at all caps the aggregate at `partial`. A snapshot-only read forces `freshness=unknown`; an immutable generation
is not evidence that the source is fresh.

## What is not true at this revision

- **No transport may invent a status.** The canonical code set is the schema enum plus
  `PROJECT_UNAVAILABLE`, `SNAPSHOT_UNAVAILABLE` and `SNAPSHOT_CORRUPT`; `src/axiom_mcp/errors.py`
  rejects any code outside it, so a failure surfaces as the code that describes it rather than as
  a generic transport error.
- **`SNAPSHOT_CORRUPT` has no contracted HTTP status.** The status map lists only the codes
  `contracts/control-api-v1.md` names; an unlisted code keeps its surface and is not given an
  invented status.
- **Coverage is not yet an exit code.** `cli.py` declares `EXIT_PARTIAL = 20`, but no code path
  maps a `partial` coverage status to it at this revision; the coverage key in the response is
  the signal a host should read.
- **The `axiom-mcp` snapshot-reader CLI is not registered at this revision.** The reading library
  exists (`read_session.py`, `cache.py`), but `src/axiom_mcp/cli.py` registers only `version` and
  `doctor`; the remaining subcommands in `docs/16-CLI-AND-CONTROL-API.md` section 3 belong to
  later tasks, and an unregistered subcommand exits `2` instead of being ignored.
- **No daemon, publication or GC runtime exists in this repository.** `axiom-mcp` reads snapshots
  and drives the control API; the writer side is `axiom-graphd`.

## Verification

`tests/test_stdio_transport.py`, `tests/test_http_transport.py`, `tests/test_entrypoints.py`,
`tests/test_read_session.py`, `tests/test_query_envelope.py` and `tests/test_errors.py` are the
targeted regressions for the claims above. `python -m pytest tests -q` is the required repository
check; `python -m ruff check .` and `python -m ruff format --check .` cover lint and formatting.

## See also

- [stdio-transport.md](stdio-transport.md) - stdout reserved for protocol, diagnostics on stderr.
- [http-transport.md](http-transport.md) - the Streamable HTTP transport and its probes.
- [locked-entrypoints.md](locked-entrypoints.md) - the launch document, digest and refusal table.
- [reference/mcp.md](reference/mcp.md) - tool catalog, response shape and error surfaces.
- [guides/snapshots.md](guides/snapshots.md) - pin one generation; raw multi-file reads are not safe.
