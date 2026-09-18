# Snapshot reader core

Owner: `axiom-mcp`. This page documents the local implementation of the processed-JSON
snapshot reader: the platform guards it takes, the catalog vector it pins, the bounded shard
bytes it loads, the read session that releases the guard before any response work, the cache
keyed by immutable generation identity, and the recovery policy for missing, corrupt and
archived snapshots.

The normative sources are consumed, not restated: the guard protocol is
`contracts/native-reader-writer-guards.md` in `axiom-specs`, the path contract and the
publication invariants are `docs/12-SNAPSHOT-READ-WRITE-PROTOCOL.md`, canonical bytes are
`docs/11-GRAPH-DATA-CONTRACT.md` section 5, the freshness/coverage and catalog rules are
`docs/11-GRAPH-DATA-CONTRACT.md` section 4 and `docs/14-MULTI-PROJECT-SOLUTIONS.md` section 4,
and the reader's own gateway behavior is `repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md`
sections 3, 5, 7 and 9. Where this page and a specification disagree, the specification wins
and the disagreement is a defect in this repository.

## C-011 — Windows shared guard (`src/axiom_mcp/guard/locks_windows.py`)

`src/axiom_mcp/guard/` is one package with one engine and one platform primitive per supported
platform. C-010 landed the package with `engine.py`, `protocol.py` and the POSIX `flock`
backend; this slice adds the Windows backend as its sibling. The card's proposed path
`src/axiom_mcp/locks_windows.py` would have split the guard across two layouts, so the module
lives inside the package instead.

The frozen block is implemented literally:

| Item | Value |
| --- | --- |
| Primitive | `LockFileEx` |
| Byte range | offset `0`, length `1` |
| Open mode | `OPEN_ALWAYS` |
| Access | `GENERIC_READ \| GENERIC_WRITE` |
| Share mode | `FILE_SHARE_READ \| FILE_SHARE_WRITE` (excludes `FILE_SHARE_DELETE`) |
| Contention flag | `LOCKFILE_FAIL_IMMEDIATELY` |
| Exclusive flag | `LOCKFILE_EXCLUSIVE_LOCK` |
| Release | matching `UnlockFileEx`, then close the handle |

Nothing else is platform-specific. Admission before data, the reverse release order, the
bounded cancellable wait, the refusal to upgrade a held lock and the refusal to acquire the
same guard twice in one execution path live once in `engine.py`, so the two platforms cannot
drift apart in the parts a caller can observe. Only the primitive differs, because Windows has
no `flock`; a Python-only or Rust-only lock would be invisible to the other language, which is
why the family is named in the contract rather than left to the implementation.

Consequences worth knowing when reading the code:

* **Contention is control flow, not an error.** A lost non-blocking attempt raises
  `GuardBusy`, which the engine catches and retries inside the deadline. It never reaches a
  caller; a caller sees `GuardTimeout` only when the bounded budget is actually exhausted.
* **Handle sharing is part of the guarantee.** Because the share mode excludes
  `FILE_SHARE_DELETE`, a second opener that asks for delete access, or for a share mode that
  conflicts with the holder's, fails with `ERROR_SHARING_VIOLATION`, and the held file cannot
  be removed or replaced while a holder has it open. That is what keeps the lock files stable
  under a concurrent publisher.
* **Identity comes from the descriptor.** `WindowsLockHandle.identity()` adopts the Win32
  handle as a C run-time descriptor and reads `os.fstat`, producing the same device-plus-file
  index value `protocol.file_identity` produces for the path. The engine compares the two, so a
  file replaced between open and lock is re-opened rather than locked as if it were the named
  file.
* **No wrapper package.** The primitive is four `kernel32` calls reached through `ctypes`.
  Adding a dependency for them would put a lock that both languages must share behind a package
  registry the release path does not otherwise need, and the guard must work in a snapshot-only
  gateway with no daemon running.

`tests/test_guard_windows.py` runs on Windows only and is skipped where `msvcrt` is absent, so
the Windows half of the slice is never inferred from a POSIX run. Two-process exclusion is
observed by launching `python -m axiom_mcp.guard.interop` as a child rather than by comparing
two objects in one interpreter, because a lock that only excludes itself proves nothing.
