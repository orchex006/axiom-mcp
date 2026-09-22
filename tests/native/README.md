# V2-031 - native query and host process matrix

This directory is the executable evidence for task **V2-031** ("Run native query and
host process matrix") in repository `axiom-mcp`. It contains the harness
(`capture_native_matrix.py`, `capture_gates.py`), the two boundary probes
(`stdio_noise_probe.py`, `http_security_server.py`), and the raw artifacts captured
on the hosts this wave actually had:

```text
tests/native/
├── capture_native_matrix.py     # python-query / stdio-process / http-security legs
├── capture_gates.py             # the three Development.md required checks
├── stdio_noise_probe.py         # real serve_stdio + injected stdout noise
├── http_security_server.py      # composed gateway on a real loopback socket
└── evidence/
    ├── windows-x64/  raw/* + SHA256SUMS
    └── ubuntu-x64/   raw/* + SHA256SUMS
```

Read the card (`axiom-specs/tasks/V2/V2-031.md`), the native-target matrix
(`axiom-specs/tasks/V2/V2-029.md` -> `axiom-specs/compatibility/platform-matrix.json`)
and `axiom-specs/contracts/cross-platform-v2.md` (CP-01, CP-08, CP-09, CP-10) before
reading any status below. Everything in this file is a record of commands that were
executed on the named host, or an explicit `not_run`.

## 1. Status summary

| Mandatory target | Host actually used | Legs executed | Certification status | Reason it is not `verified` |
| --- | --- | --- | --- | --- |
| `windows-x64` | native Windows 11 x64 | 6/6 passed | **not certified** | no released core fixture set; legs ran on the repository's synthetic bundle |
| `linux-x64` | Ubuntu x64 on **WSL2** | 5/6 passed, `stdio-interrupt` failed | **not_run** | a WSL2 run is not a native Linux execution (platform-matrix `rules.wsl_is_windows_evidence` / `evidence_status.statement`) |
| `macos-arm64` | none | 0 | **not_run** | no macOS host exists on this machine |
| `macos-x64` | native macOS 26.6.2 x64 | 6/6 passed | **not certified** | legs used the repository's synthetic bundle, not released core fixtures |
| released core fixtures | none | 0 | **not_run** | the only bundle in the repository self-labels `synthetic-fixture-v2-not-runtime` |

No released core fixture set exists on this checkout or on either host, so AC1's
"against released core fixtures" clause remains unverified (section 6). The local
macOS x64 execution is development evidence only and does not certify a target.

Nothing below weakens a check to make a host pass. A leg that could not be executed is
`not_run` with its reason, and a signal the POSIX host refused to service is recorded
as the observed refusal rather than converted into a pass.

## 2. How to reproduce

The harness runs only on the native host it is started on. It never spawns a
container, and it never claims to be running elsewhere.

```powershell
# Windows x64 (native)
C:\Users\006006\AppData\Local\Programs\Python\Python313\python.exe tests/native/capture_native_matrix.py --target windows-x64
C:\Users\006006\AppData\Local\Programs\Python\Python313\python.exe tests/native/capture_gates.py --target windows-x64
```

```bash
# Ubuntu x64 (WSL2 -> wsl -d Ubuntu -u thanatmet-t -- bash -lc '<cmd>')
~/.axiom-runtime/venv/bin/python tests/native/capture_native_matrix.py \
  --target ubuntu-x64 --python ~/.axiom-runtime/venv/bin/python
~/.axiom-runtime/venv/bin/python tests/native/capture_gates.py \
  --target ubuntu-x64 --python ~/.axiom-runtime/venv/bin/python
```

`capture_native_matrix.py` exits `0` only when every leg passed; `capture_gates.py`
exits `0` only when the three canonical gates passed. A non-zero exit is the real
result and is recorded, not repaired. Each run refreshes `SHA256SUMS` for its target
directory, so a later run overwrites the digests quoted here - that is intended: the
digests in this document identify the artifacts captured at the timestamps below.

The evidence tree is committed byte-for-byte. `.gitattributes` marks
`tests/native/evidence/**` as `-text`, because the Windows stderr/stdout captures
legitimately contain CR bytes; without that rule `* text=auto` would normalize them on
commit and every digest in `SHA256SUMS` would stop matching the committed bytes.

## 3. Target `windows-x64` - native

### Platform identity

| Field | Value |
| --- | --- |
| Target id | `windows-x64` |
| OS build string | `Windows-11-10.0.26200-SP0` |
| `release` / `version` | `11` / `10.0.26200` |
| Architecture | `AMD64` (`machine`), 64-bit pointers |
| Processor | `Intel64 Family 6 Model 186 Stepping 3, GenuineIntel`, 12 CPUs |
| Interpreter | `C:\Users\006006\AppData\Local\Programs\Python\Python313\python.exe` |
| Interpreter version | CPython `3.13.14` (`python_requires` `>=3.13,<3.14`, supported) |
| Guard backend | `windows` |
| Pinned SDK | `mcp==1.28.1`, protocol minimum `2025-11-25` |
| Pinned deps | anyio `4.14.2`, fastapi `0.139.2`, httpx `0.28.1`, pydantic `2.13.4`, starlette `1.3.1`, uvicorn `0.51.0` |
| Spec revision | `80f44e836ced442e8f3ea33d167bd369ed6796bc` |
| Captured (UTC) | `2026-09-20T07:06:58Z` (matrix), `2026-09-20T06:59:27Z` (gates) |

### Commands and real exit codes

```text
capture_native_matrix.py --target windows-x64    -> exit 0   (all 6 legs passed)
capture_gates.py --target windows-x64            -> exit 0   (3 canonical gates green)
```

### Leg results

| Leg | Kind of checks | Real result |
| --- | --- | --- |
| `python-query` | positive + negative + boundary | passed - real `graph_query` over the `demo-solution` bundle through the native `windows` guard |
| `stdio-handshake` | positive | passed - 2 JSON-RPC frames, 0 non-protocol lines, exit `0` |
| `stdio-noise` | failure (stdout-noise) | passed - 3 injected writes, `violations=3`, 0 leaked to stdout, exit `0` |
| `stdio-silence-deadline` | boundary (timeout) | passed - 2.0 s read deadline expired with 0 unsolicited bytes, exit `0` |
| `stdio-interrupt` | failure (interrupt) | passed - `CTRL_BREAK_EVENT` delivered, exit `3221225786` |
| `http-security` | positive + negative + boundary + failure | passed - 11 checks on a real loopback socket |

Recorded `observed` values (verbatim from `raw/matrix.json`):

- `python-query`
  - `graph_query.search.envelope` (positive): `schema_version=1`, `coverage=partial`,
    `freshness=unknown`, 2 nodes, generations `auth-api`/`7319237f...` and
    `web-app`/`f570a6d6...`, `envelope_keys_within_contract=true`.
  - `graph_query.callers.envelope` (positive): 1 node, 1 edge, `truncated=false`.
  - `graph_query.unknown_operation` (negative): refusal `UNSUPPORTED_OPERATION`.
  - `graph_query.unscoped_solution` (negative): refusal `NOT_FOUND`.
  - `graph_query.arbitrary_sql_refused` (negative): a request carrying `sql` is
    refused by name with `VALIDATION_ERROR`, not executed.
  - `graph_query.out_of_contract_limit` (boundary): the out-of-contract `limit` field
    is refused with `VALIDATION_ERROR` and `details.unexpected_fields=["limit"]`.
- `stdio-handshake`
  - `jsonrpc_frames=2`, `non_protocol_lines=0`, `first_non_protocol_line=null`.
  - `initialize`: `protocol_version=2025-11-25`, `server_version=1.28.1`.
  - `tools_list_answer`: `tools=0` (no tools are registered in this build slice).
  - `stdio_stop`: `protocol_bytes=328 protocol_writes=2 violations=0`, exit `0`.
- `stdio-noise`
  - `injected_lines=3`, `diverted_writes=3`, `leaked_to_stdout=0`;
    `stdio_stop ... violations=3`; exit `0`.
- `stdio-silence-deadline`: `read_deadline_seconds=2.0`,
  `deadline_expired_without_data=true`, `unsolicited_bytes=0`; exit `0`.
- `stdio-interrupt`: `delivered=CTRL_BREAK_EVENT`, `exit_code=3221225786`
  (`0xC000013A`), `terminated_by_harness=false`.
- `http-security`
  - `/healthz` -> `200`, `/readyz` -> `503` with `error="NOT_READY"`
    (`query`/`control` planes not wired) - readiness is reported, not claimed.
  - SDK client handshake -> `2025-11-25` (the pinned minimum).
  - `403` for a lookalike `Host` and a lookalike `Origin`.
  - `401` for a missing credential and for a credential with the wrong audience
    (a graphd control token presented to the read plane).
  - `403` for a read token calling a mutating tool, and `403` for a read token
    naming a solution it is not scoped to.
  - `400` `VALIDATION_ERROR` for an oversized request body (boundary).
  - stalled request body: client timeout at 6.0 s with `bytes_received=0` (failure).
  - launcher teardown exit `1`, `terminated_by_harness=false`.

### Evidence artifacts (SHA256, from `evidence/windows-x64/SHA256SUMS`)

```text
c07be42e0cc51edd25606efd23fe50d479efeb307f834a67483cff007cb6a7a3  raw/identity.json
46047cb17bed7ab665a6c35196a1557418ca6897b985adf1a68d23bbd81cd42d  raw/matrix.json
b17eb5bae89d79525dcfbe80619f6bd5b68844d430b21fb62d19957e09b16430  raw/gates.json
be8fceb44cdc9c22dc6f5343a59cc612e996350d27a7d1f88cbab9479bc92096  raw/python-query-summary.bin
98216c8812723458c0fdc162d4f599f386011885783ece5be63c89a0ec604e7d  raw/python-query-fixture-provenance.json
cc64ae5bfe8d9be74345ea234ac8f07a7acdf189a38a9425b0e5cbdab61d7cb6  raw/python-query-search-envelope.json
0191e132cf1fd9d79392b3b8a7454e5e2fbb43f9be69469fedf37b59ffb25447  raw/python-query-callers-envelope.json
e0c77675c25bd3fc55d52d1dabe42983e4d0fb5a5b67456eed32a711c376bc48  raw/stdio-handshake-stdout.bin
1f24c964ea14a9b742d9d93f63ccad99dd349a1945fcb35da63cf44f8ae2ec56  raw/stdio-handshake-stderr.bin
9be767fbd38697b1cf1bf4e6d7582d85a514793fa48866daf441f45b15afb6cd  raw/stdio-noise-stdout.bin
b198e342da76b1758712b246795f8be87760da112ca6e42f4d6d29f0625e8fa4  raw/stdio-noise-stderr.bin
ee51146e3359bef7cfb296891a80198ea0ca2b6e2636fb9b4821c6ac272268ef  raw/stdio-silence-stderr.bin
c77dbeac170fd8f4bae45be5d8e2d20e418498c26787772ff4655878253ed6da  raw/stdio-interrupt-stderr.bin
39bc5ca9856b2841ae1e840423506236a9fc12f78d7d91fe8ecfcb7ef55d2eab  raw/http-launcher-stderr.bin
e98353b09c78157dfc29e7303ce26c39f2ec83b332c1a1dfae1afde3a0d4435c  raw/gates-pytest-stdout.bin
82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18  raw/gates-ruff-check-stdout.bin
e540c8427c224fd685bda71cb704448f5f98f01496a5ad22e5c656a56a0955ae  raw/gates-ruff-format-check-stdout.bin
```

The empty `stdio-silence-stdout.bin` and `stdio-interrupt-stdout.bin` both hash to
`e3b0c442...b7852b855` (the SHA256 of zero bytes): the transports wrote nothing
unsolicited to stdout on either leg.

## 4. Target `linux-x64` - Ubuntu x64 on WSL2 (observed, not native Linux evidence)

### Platform identity

| Field | Value |
| --- | --- |
| Target id (recorded) | `ubuntu-x64` |
| Distro | `Ubuntu 26.04.1 LTS` (`VERSION_ID=26.04`, codename `resolute`) |
| Kernel build string | `Linux-6.18.33.2-microsoft-standard-WSL2-x86_64-with-glibc2.43` |
| `release` / `version` | `6.18.33.2-microsoft-standard-WSL2` / `#1 SMP PREEMPT_DYNAMIC Thu Jun 18 21:54:43 UTC 2026` |
| Substrate | **WSL2** (`uname -a` -> `...-microsoft-standard-WSL2 ...`) |
| libc | `ldd (Ubuntu GLIBC 2.43-2ubuntu2.3) 2.43` |
| Architecture | `x86_64`, 64-bit pointers, 12 CPUs |
| Interpreter | `/home/thanatmet-t/.axiom-runtime/venv/bin/python` |
| Interpreter version | CPython `3.13.15` (provisioned; see section 9) |
| Guard backend | `posix` |
| Captured (UTC) | `2026-09-20T07:07:43Z` |

### Commands and real exit codes

```text
capture_native_matrix.py --target ubuntu-x64 --python ~/.axiom-runtime/venv/bin/python  -> exit 1  (5/6 legs passed)
capture_gates.py         --target ubuntu-x64 --python ~/.axiom-runtime/venv/bin/python  -> exit 1  (canonical pytest exited 2)
```

### Leg results

| Leg | Real result |
| --- | --- |
| `python-query` | passed - same envelope shape as Windows x64 |
| `stdio-handshake` | passed - `protocol_bytes=326`, `violations=0`, exit `0` |
| `stdio-noise` | passed - `violations=3`, 0 leaked to stdout, exit `0` |
| `stdio-silence-deadline` | passed - 2.0 s deadline, 0 unsolicited bytes, exit `0` |
| `stdio-interrupt` | **failed** - `SIGINT` while idle was not serviced (see below) |
| `http-security` | passed - all 11 checks, same status codes as Windows |

The interpreter and platform differ, but the deterministic `python-query` summary
artifact is byte-identical to the Windows artifact
(`be8fceb44cdc...9bc92096` on both hosts) - a useful cross-host determinism check on
the reader/engine contract.

### The `stdio-interrupt` failure (real observed finding)

`http-security` and the noise/silence legs behave the same as Windows. The interrupt
leg does not. Three deliveries were attempted on the idle server:

| Delivery | Observed | Recorded outcome |
| --- | --- | --- |
| `SIGINT` (idle, no pending input) | never serviced; after the 10.0 s window the harness had to `process.kill()` | **fail** - `exit_code=null`, `terminated_by_harness=true` |
| `SIGINT` then a `tools/list` frame 1.0 s later | exit `-2` with `KeyboardInterrupt` on stderr | pass |
| `SIGTERM` | exit `-15` | pass |

Reading: `axiom-mcp`'s stdio transport has no POSIX signal handler (there is none in
`src/axiom_mcp/stdio.py`; the shared drain logic in `src/axiom_mcp/lifecycle.py` is not
wired into stdio). A `SIGINT` delivered to an idle process blocked on its stdin read is
therefore not serviced; the interrupt only escaped as `KeyboardInterrupt` once a later
frame caused the read to return, and `SIGTERM` terminated the process through the
default action rather than a graceful drain. CP-08 asks for a bounded graceful drain on
`SIGINT`/`SIGTERM`; this is a **gap that was found, not a check that was weakened**.
The card status is not changed here.

Honesty caveat on the harness itself: the failed `SIGINT` row reports
`terminated_by_harness=true` with `hard_stop.exit_code=-9`, which is the harness's own
`SIGKILL` after its bounded wait, so it masks whatever the process would otherwise have
reported. The distinction that matters is the one above: `SIGINT` alone was never
serviced within the window; `SIGINT` plus a later frame and `SIGTERM` both produced a
real exit status.

### Evidence artifacts (SHA256, from `evidence/ubuntu-x64/SHA256SUMS`)

```text
23ab7eeef33ea989becbdd673c6a7b311e98a1b9e6a1077b774a533bacfd0ca3  raw/identity.json
c72923425e802d15530b7815f90c652420e684f11b1c13c1c493a80b700d2049  raw/matrix.json
3308843e6f575c060b4ce72da4ea131d28bfc6e3fe321a13f0313ece090a536a  raw/gates.json
be8fceb44cdc9c22dc6f5343a59cc612e996350d27a7d1f88cbab9479bc92096  raw/python-query-summary.bin
98216c8812723458c0fdc162d4f599f386011885783ece5be63c89a0ec604e7d  raw/python-query-fixture-provenance.json
fd7f141c3e30533f97b5326c455731312b5754ee2e4a561ad2c75fe7a4b7cb3a  raw/stdio-handshake-stdout.bin
94a4ff078187ebad216daed4076a3035aab45bc2b889c5bbab4c03669967f32f  raw/stdio-handshake-stderr.bin
2ba4589ca1afac70bd06c2fadd5d13b97a03542dd649343d2a13e565441b30b5  raw/stdio-noise-stdout.bin
f350024c64621d9294bb7454a3665908347703f74da22f01b40fceb5941b610f  raw/stdio-noise-stderr.bin
a27bb34a7013adba9240e7b850d71cf64429ea21078c977a51376c73631e5d70  raw/stdio-silence-stderr.bin
9e43994880a103e0b10101b6134b77df4d4d71b289fe3a5761cb0a713a40b675  raw/stdio-interrupt-stderr.bin
93180a9136840542949cda0015ac0ff665b3b968f999cbd023fef365f4ee50f6  raw/http-launcher-stderr.bin
1c20e87524fb3e5dd0f76cb7b106aeb6a5ba49675e11722af7f390e8b2a6e55f  raw/gates-pytest-stdout.bin
48a89a9f585983477e7af09bfeecd0893cf9d08192882d5dbaab423760dbfd4c  raw/gates-pytest-ignoring-windows-guard-tests-stdout.bin  (diagnostic, not a required check)
```

## 5. macOS targets

The macOS x64 matrix ran locally with CPython 3.13.15 and pinned dependencies. Its
six legs passed, while the synthetic fixture provenance keeps it out of
certification evidence.

| Target | Status | Concrete reason |
| --- | --- | --- |
| `macos-arm64` | `not_run` | no macOS / Apple-silicon host is reachable from this checkout; a macOS `arm64` run requires native hardware |
| `macos-x64` | local development evidence | native macOS 26.6.2 x64, CPython 3.13.15; all six legs passed against the synthetic bundle, so released-fixture certification is `not_run` |

A container, a cross-build or a "should work on macOS" argument is not macOS
evidence. `macos-arm64` stays `not_run`; macOS x64 is not certified.

## 6. Released core fixtures - `not_run`

AC1 says the checks must run "against released core fixtures". No such fixture set
exists in this checkout or on either host. The only solution bundle in the repository
(`tests/fixtures/solution/demo-solution`) self-labels as synthetic, and that label is
read off the real manifest bytes rather than assumed. From
`raw/python-query-fixture-provenance.json` (identical on both hosts,
sha256 `98216c88...604e7d`):

```json
{
  "synthetic_label": "synthetic-fixture-v2-not-runtime",
  "observed_generator_versions": ["synthetic-fixture-v2-not-runtime"],
  "all_observed_are_synthetic": true,
  "fixture_root": "tests/fixtures/solution/demo-solution",
  "released_core_certification": "not_run",
  "reason": "The envelope's own project generations resolve to manifests that self-label as
             \"synthetic-fixture-v2-not-runtime\", so this leg exercises the real reader and
             engine on a synthetic fixture and is not a released-core runtime certification."
}
```

The two resolved manifests are
`tests/fixtures/solution/demo-solution/auth-api/checkpoint/generations/7319237f.../manifest.json`
and `.../web-app/checkpoint/generations/f570a6d6.../manifest.json`, and both declare
`generator_version: "synthetic-fixture-v2-not-runtime"`. The repository's own test
suite asserts the same string (`tests/test_catalog.py`), so this is the intended label
of the shipped fixture, not an accident. Because the bundle is synthetic, the
`python-query` leg proves the reader/engine contract (positive, negative and boundary
behavior) on this host; it does **not** certify any released core fixture and is
recorded as `released_core_fixture.status = not_run` in both `matrix.json` files.

## 7. Acceptance mapping

**AC1 - "Execute Python query, stdio process and HTTP security checks on every
mandatory target against released core fixtures, including timeout/interrupt/stdout-noise
failure cases."**

| Requirement | Windows x64 (native) | Ubuntu x64 (WSL2) | macOS arm64/x64 |
| --- | --- | --- | --- |
| Python query check | executed (passed) | executed (passed) | not_run |
| stdio process check | executed (passed) | executed (5 legs, interrupt failed) | not_run |
| HTTP security check | executed (passed) | executed (passed) | not_run |
| timeout failure case (`stdio-silence-deadline`) | executed | executed | not_run |
| interrupt failure case (`stdio-interrupt`) | executed (passed) | executed (**failed**) | not_run |
| stdout-noise failure case (`stdio-noise`) | executed | executed | not_run |
| released core fixture | not_run (synthetic bundle only) | not_run | not_run |

AC1 is therefore **not satisfied**: two mandatory targets have no run, one mandatory
target has a failing leg, and the released-core-fixture clause is unmet everywhere.

**AC2 - "Run positive, negative and failure-boundary checks; attach actual output,
platform identity and hashes. Unperformed native tests remain unverified."**

Attached per target, under `evidence/<target>/`:

- actual output: stdout/stderr captured verbatim as `.bin` plus a UTF-8 `.txt` twin;
- platform identity: `raw/identity.json` (OS build, arch, interpreter, guard backend,
  pins, spec revision);
- hashes: `SHA256SUMS` over every artifact, plus the digests quoted in sections 3-4;
- check kinds: every leg reports each check as `positive`, `negative`, `boundary` or
  `failure` in `raw/matrix.json`;
- unperformed tests: macOS and released-core legs are `not_run` with reasons, never
  reported as passing.

AC2 is satisfied for the two hosts that were available, and is explicitly unverified
elsewhere.

## 8. Repository required checks (`Development.md`)

`Development.md` names three required checks. Each was run as a real subprocess from
the repository root with `capture_gates.py` and captured verbatim.

### Windows x64 (native) - all three green

| Gate | Command | Real exit code | Observed |
| --- | --- | --- | --- |
| pytest | `python -m pytest tests -q` | `0` | `753 passed, 2 skipped, 230 subtests passed in 94.42s` |
| ruff check | `python -m ruff check .` | `0` | `All checks passed!` |
| ruff format | `python -m ruff format --check .` | `0` | `107 files already formatted` |

Artifacts: `raw/gates-pytest-stdout.bin` (`e98353b0...3a0d4435c`),
`raw/gates-ruff-check-stdout.bin` (`82b3e6a6...56b4f18`),
`raw/gates-ruff-format-check-stdout.bin` (`e540c842...56a0955ae`), summarized in
`raw/gates.json` (`b17eb5ba...e09b16430`).

### Ubuntu x64 (WSL2) - required pytest did not pass

| Gate | Command | Real exit code | Observed |
| --- | --- | --- | --- |
| pytest | `python -m pytest tests -q` | **`2`** | collection error: `ERROR tests/test_guard_windows.py` -> `ModuleNotFoundError: No module named 'msvcrt'` |
| ruff check | `python -m ruff check .` | `0` | `All checks passed!` |
| ruff format | `python -m ruff format --check .` | `0` | `107 files already formatted` |
| pytest (diagnostic, **not a required check**) | `python -m pytest tests -q --ignore=tests/test_guard_windows.py` | `1` | `12 failed, 738 passed, 1 skipped, 7 errors, 230 subtests passed` |

The canonical POSIX pytest exit code is a **real failure**, recorded as such; the
diagnostic run is labelled `diagnostic: true` in `gates.json` and must not be read as a
required check. `capture_gates.py` therefore exited `1` on this host.

## 9. Findings and limitations

1. **POSIX interrupt handling now has local macOS x64 evidence.** The SDK's threaded
   stdin reader did not yield to cancellation while idle. The POSIX adapter retains
   the official SDK parser and uses a cancellable descriptor reader; the local matrix
   passed SIGINT and SIGTERM. This remains development evidence because its fixture is
   synthetic.
2. **A Windows-only test module aborted POSIX collection in the captured revision.**
   `tests/test_guard_windows.py` imported `axiom_mcp.guard.locks_windows`, which imports
   `msvcrt`, before `pytest.importorskip("msvcrt")`. The current branch moves the skip
   before those imports; the historical Ubuntu result remains evidence of the earlier
   failure until the required gates are rerun on a pinned POSIX host.
3. **POSIX guard/watcher code is exercised only partially here.** The repository's own
   `Development.md` already lists "POSIX guard backend" as a recorded unverified leg;
   this matrix does not change that.
4. **A pinned runtime had to be provisioned on the WSL2 host.** The distro's system
   `python3` is `3.14.4` and pip-less, outside the `>=3.13,<3.14` pin, so the legs ran on
   a provisioned CPython `3.13.15` (`~/.axiom-runtime/`, python-build-standalone) in a
   venv with the exact pinned dependencies. CP-01 expects a system Python not to be
   assumed; this records how the host was made runnable rather than pretending it was
   pinned by default.
5. **`pydantic_settings` warns on POSIX only.** Every stdio start on WSL2 printed
   `IncompleteFieldDefinitionWarning: Field 'lifespan' has an incomplete definition`.
   It is stderr diagnostics and does not corrupt the protocol stream, but it is a real
   platform-dependent observation and is left in the captured stderr.
6. **WSL2 is not bare-metal Linux.** The Ubuntu leg ran on a WSL2 kernel, not a native
   Linux install. Per `compatibility/platform-matrix.json`, a WSL2 run is not native
   evidence, so `linux-x64` is `not_run` for certification even though the legs ran and
   five of six passed.
7. **The evidence is host-state-specific.** `identity.json`, the http-security
   request ids and the `sha256` of dynamically generated response bodies differ between
   runs; only the digests recorded in `SHA256SUMS` and quoted above identify the
   artifacts captured at `2026-09-20T07:06:58Z` (Windows) and `2026-09-20T07:07:43Z`
   (Ubuntu).

## 10. Secret scan

The evidence contains no credential values and no `Authorization` header. The HTTP
leg registers two fixed, obviously synthetic tokens
(`v2-031-native-read-token`, `v2-031-native-control-token`) from environment variables
passed to the child process; the registration names live only in the helper scripts,
and neither value, nor a bearer header, appears in any `raw/` artifact. The captured
HTTP bodies carry only error codes, messages, retry flags and request ids.

## 11. Unverified / `not_run` summary

```text
[ ] target macos-arm64          not_run  - no macOS/Apple-silicon host available
[ ] target macos-x64            not certified - native local matrix passed, but fixture is synthetic
[ ] target linux-x64            not_run  - ran on WSL2; WSL2 is not native Linux evidence
[ ] released core fixtures      not_run  - only synthetic-fixture-v2-not-runtime exists in this checkout
[ ] stdio-interrupt on POSIX    local pass - macOS x64 matrix passed; released-fixture evidence remains unverified
[ ] POSIX guard backend         not_run     - covered elsewhere; not part of this matrix
[x] POSIX required pytest gate  passed      - macOS x64 final suite: 760 passed, 2 skipped, 230 subtests; Windows module skips cleanly
```

Certified mandatory targets after this task: **0 of 4**. This directory records real
executions and real gaps; it does not certify a platform and it does not flip any task
status.
