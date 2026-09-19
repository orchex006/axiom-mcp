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

## C-012 — Pinned catalog vector (`src/axiom_mcp/catalog.py`)

The catalog is the read-model commit point, so a query resolves it once and then reads every
member from the exact generation that vector names. `load_solution_catalog(location)` reads the
lane pointer a single time, loads `generations/<generation_id>/manifest.json`, and refuses a
directory whose name does not equal the digest of the catalog bytes inside it. The pointer's
`generation_id` is checked against those same bytes, so a catalog renamed under an unchanged
directory, or a pointer that quotes a digest the bytes do not have, is rejected rather than
read.

A member is exactly `(project_id, generation_id, source_fingerprint)`. The reader enforces the
exact key set and rejects a member that carries `name`, `project_name`, `path` or `directory`,
because a member resolved by name has no pinned generation and would have to be matched to a
project's newest generation - which is the latest-per-project fallback
`docs/14-MULTI-PROJECT-SOLUTIONS.md` section 4 and `tools/catalog_contract.py` prohibit.
Duplicate `project_id` members, a non-canonical byte form and an unknown schema major are all
refused, the major before the byte form so a document that is both wrong reports its version.

`pin_catalog(catalog, open_member=...)` turns the validated catalog into a `CatalogVector`.
The default opener `project_member_opener(location_for)` reads
`generations/<generation_id>/manifest.json` for the member - never the project lane's
`current.json` - and requires the manifest to hash to the pinned `generation_id` and to declare
the pinned `source_fingerprint`. A member whose generation is absent, unreadable or not the
pinned one is recorded as `missing` with a reason; it does not fail the whole read and it is
never silently substituted. The vector is then reported `partial`, and
`require_complete()` raises `CatalogMemberMissing` so a caller under `require_complete_solution`
rejects the answer explicitly instead of receiving a partial graph that looks complete.

The regression that matters most is
`test_pinned_vector_ignores_a_newer_project_generation`: a newer, fully valid generation is
published into a project lane and that lane's `current.json` is repointed at it, yet the answer
stays on the catalog's generation. A reader that consulted the lane pointer would break both
the query's identity and SNP-03's single-vector rule. The negative slices cover a member with
no `generation_id`, a member resolved by name, a duplicate member, an altered pinned
generation (reported `partial`, not substituted), an unknown major, non-canonical bytes, a
renamed generation directory and an absent catalog pointer.

`tests/fixtures/solution/demo-solution/` is vendored byte-for-byte from
`axiom-specs/examples/snapshots/.axiom/graph/demo-solution`; the test asserts each vendored
manifest still hashes to the generation directory that holds it.

## C-014 — Bounded shard load (`src/axiom_mcp/shards.py`)

`docs/12-SNAPSHOT-READ-WRITE-PROTOCOL.md` section 5 step 5 fixes the traversal rule - traverse
from the manifest indexes only, never glob JSON, keep every file under the bound graph root
after canonicalization, and refuse a symlink escape - and `docs/11-GRAPH-DATA-CONTRACT.md`
section 6 fixes each role's shard location and the 16 MiB hard cap. This module is where those
two rules are enforced *before* a JSON parser is involved, which is the difference between a
cap and a post-hoc check.

Four checks, in this order, and the order is the point:

| Step | Check | Refuses |
| --- | --- | --- |
| 1 | the entry's role owns the directory its path claims | a `nodes` entry pointing at `edges/000000.json`, an unknown role, a nested path |
| 2 | declared `bytes` fit the caller's cap, and the plan's declared total fits the plan budget | a shard or plan over budget, before the file is opened |
| 3 | the path is not a symlink, is a regular file, and resolves inside its generation directory | a leaf symlink, a symlinked parent, a directory where a shard should be |
| 4 | the read is bounded to `cap + 1` bytes | a shard that grew on disk, without ever materialising it |

Only then does `Manifest.verify_shard` run - length, digest, parse, record count - so the bytes
that are parsed are the bytes that were hashed, and the hash is the one the manifest declares.
`ShardLimits` may lower the cap for one query but refuses a value above the canonical 16 MiB,
because widening the contract cap locally is exactly the silent weakening the contract forbids;
a plan is refused for its declared size before the first read, so an oversized manifest cannot
turn into disk traffic first.

Consequences worth knowing when reading the code:

* **`copy_shards` is separate from `verify_copied` on purpose.** C-015 needs the copy to happen
  under the shared guard and the parse to happen after the guard is released, so the copy and
  the verification are two functions rather than one call.
* **`read_bounded` is the only read path.** It returns at most `limit + 1` bytes, so a caller
  that sees more than `limit` knows the file is over the cap without ever holding it.
* **A manifest is not trusted just because it was validated.** `shard_path` re-checks the role
  and the path, and a forged `ManifestEntry` that is not a permitted role/path pair is refused
  even though it never came from `load_manifest_bytes`.

`tests/test_shards.py` proves the positive path (only the required roles are read, exactly one
bounded read each) and the negative ones: a leaf symlink, a symlinked parent that leaves the
lane, a symlinked parent that stays in the lane but outside the generation, an over-declared
shard whose file is a directory, an oversized shard whose bytes are not valid JSON, a plan that
is over budget passing a reader that fails if it is ever called, a forged manifest entry, and
substituted bytes of the same size.

## C-015 ? Read session: copy under guard, respond after release (`src/axiom_mcp/read_session.py`)

`docs/12-SNAPSHOT-READ-WRITE-PROTOCOL.md` section 3.1 fixes the shape of a read: take the shared
guard, copy the pointer, the manifest and only the shards the query needs into memory, then
release before building the response. A consumer that stops reading must not keep a publisher
out of the lane, so the release cannot depend on the client finishing.

`ReadSession` splits one read into two phases that cannot be confused:

* `ReadSession.load` enters `SolutionGuard.reader(...)`, checks that `data.lock` is held, copies
  the bounded bytes with `shards.copy_shards` (so the copy window is bounded by `ReadLimits` and
  `ShardLimits`, not by a client), leaves the reader context, then verifies the hashes
  (`verify_copied`) with no lock held. Before returning it asserts no lock is still held, so a
  session that would silently block a publisher raises instead.
* `ReadSession.render` and `ReadSession.stream` build and emit the response. Both call
  `_require_released()` first and raise `GuardStillHeld` if any lock is held, so "the response is
  computed outside the guard" is enforced rather than documented. `stream` pads the payload to
  a JSON body and yields it in `chunk_bytes`-sized pieces; a stalled consumer only stalls this
  generator.

The returned `LoadedSnapshot` records which phase did the work - `guard_held_during_copy` and
`parsed_after_guard_release` - so a caller, a test or an evidence file can see it instead of
assuming it. Freshness is not invented: this module never talks to the daemon, so the snapshot
carries `freshness="unknown"` and `verification="manifest_hash"`. A hash-verified pinned
generation proves the bytes are the published ones; it is not evidence that they are newer than
the source tree.

`tests/test_read_session.py` proves both halves of AC1: an injected byte source observes every
copy read happening while `data.lock` is held and none after, and a second *process*
(`python -m axiom_mcp.guard.interop`) times out while the copy is in flight but acquires the
lane while the response is still only half streamed. The negative and boundary cases refuse a
response attempted while a lock is held, refuse oversized pointer/manifest/plan copies before
anything is read, and surface a shard removed mid-copy as an error with the guard released
rather than as a partial answer.

## C-016 ? Cache keyed by immutable generation identity (`src/axiom_mcp/cache.py`)

A cache in front of a pinned read must not undo the pin. SNP-03 says one query reads one exact
generation and never follows `current.json` again; a cache keyed by lane, project name or
pointer would answer a later query with an earlier generation's bytes and label them as the
new one. So the key is the immutable identity itself.

`CacheKey` is `(schema, profile, generation_id, shard_sha256)` plus the manifest entry `path`.
`generation_id` is the sha256 that names the generation directory and `shard_sha256` is the
manifest-declared digest of the bytes, so two reads derive the same key only when they would
read the very same bytes of the very same generation. When a publisher republishes and the
pointer moves, the caller pins the new generation before it derives any key, so the new key
differs and the old entry cannot be hit. `cache_key_for(shard, ...)` builds the key from the
copied shard's own declared digest, never from anything mutable.

Two invariants make "cached data under the wrong generation label" impossible rather than
merely unlikely:

* `SnapshotCache.put` refuses bytes whose sha256 is not the key's `shard_sha256`
  (`CacheMismatch`), so nothing can be stored under a label its bytes do not hash to.
* `SnapshotCache.get` re-hashes the stored bytes against the key before returning them; an entry
  that no longer matches is dropped and reported as a miss, never handed back.

Memory is bounded twice: `max_entries` bounds the live entry count and `max_bytes` bounds their
total payload size, both enforced by evicting least-recently-used entries. A payload that is
itself larger than `max_bytes` is refused without evicting anything, because it could never fit
and clearing live entries for it would only lose data. `evict_generation` invalidates by
immutable generation, which is the only safe invalidation unit here.

`tests/test_cache.py` proves the positive path (a hit returns exactly the pinned bytes and the
same bytes under another `generation_id` are a miss), the pointer-change case (a changed
generation reloads instead of hitting the old entry), and the negative/boundary ones
(mis-digest `put` refused, tampered entry dropped on `get`, both LRU limits enforced in LRU
order, an over-budget payload refused without eviction, and invalid limits/keys refused).

## C-017 ? Missing, corrupt and archived snapshots (`src/axiom_mcp/recovery.py`)

`docs/12-SNAPSHOT-READ-WRITE-PROTOCOL.md` section 5 ends with the rule this module implements:
if a required generation is missing or corrupt, drop the partial request, retry bounded against
the whole catalog, and if it still fails return `SNAPSHOT_UNAVAILABLE`/`SNAPSHOT_CORRUPT` and ask
control to reconcile. Half a new answer and half an old one is not allowed. The owner seed
`repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md` section 7 adds that a daemon-offline checkpoint may
be served with `freshness=unknown` in snapshot-only mode, and section 5 that a collected
generation is `SNAPSHOT_EXPIRED` rather than a read of current data; `docs/14-MULTI-PROJECT-SOLUTIONS.md`
section 6 makes a missing member `PROJECT_UNAVAILABLE` with partial coverage, never an empty graph.

`read_with_recovery` runs a caller-supplied loader that pins one exact catalog vector. Each
attempt returns the whole vector or raises, so a failed attempt leaves nothing behind and a
served answer can never mix two attempts. Attempts are bounded by `RecoveryLimits`; the retry
applies to the recoverable codes (missing member, unavailable or corrupt generation) and never to
`SNAPSHOT_EXPIRED`, because reading again cannot bring a collected generation back.

Policy decides a partial vector: under `allow_partial` it is served as `partial` with the members
that resolved, and under `require_complete` it is refused as `PROJECT_UNAVAILABLE` with no data at
all. Freshness is never fabricated: snapshot-only mode forces `freshness=unknown` and refuses a
caller-supplied live reading, and a `pinned`-consistency read is treated the same way because an
immutable historical generation is not evidence that the source is fresh.

`require_generation`/`classify_absence` make the archived case honest: if the lane still names the
pinned generation, its absence is `SNAPSHOT_UNAVAILABLE`; if the lane has moved on to a different
generation, the pinned one was collected and the answer is `SNAPSHOT_EXPIRED`, never the newer
data.

`tests/test_recovery.py` runs against the copied `demo-solution` bundle and proves the positive
path (a missing member is partial or refused by policy, and a flaky read recovers on the bounded
retry), the freshness rule (snapshot-only reports `unknown` and rejects a fabricated live value),
and the negative/boundary ones (a corrupt or unavailable generation retried then reported with no
partial data, a superseded generation expired on the first attempt with the newer lane data
unused, an absent-while-still-pinned generation reported unavailable, and invalid
configurations/loaders refused).
