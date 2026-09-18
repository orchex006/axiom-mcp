# Snapshot race and read-only guide

Owner: `axiom-mcp`. Normative sources live in `axiom-specs`:
`docs/12-SNAPSHOT-READ-WRITE-PROTOCOL.md` fixes the publication, read and race-condition
protocol; `contracts/native-reader-writer-guards.md` fixes the cooperative lock ABI;
`SOURCE-OF-TRUST.md` section 5 fixes the machine-local layout. This guide explains how a
reader stays correct while a publisher is writing, and it consumes those documents rather
than redefining them.

**Scope of the guarantee.** The protocol protects cooperating readers and writers of the
ecosystem. A Git checkout, an editor, an antivirus process or any third-party process does
not hold the guard, so a reader must still treat a missing or corrupt file as a recoverable
state. The implementation half of this guide - the native guard engine, the registry, the
manifest validator - is described against the code at this revision, and the sections that
are specified but not yet implemented say so explicitly.

## Invariants

| Id | Invariant |
| --- | --- |
| SNP-01 | A published generation is immutable. A published file is never reopened for truncate or write. |
| SNP-02 | The current pointer references only a generation that is complete, schema-valid and checksum-valid. |
| SNP-03 | One query uses exactly one generation vector. It never re-reads `current.json` per shard and admits a second generation. |
| SNP-04 | A SQLite commit and a filesystem publish are not one distributed transaction; a durable outbox plus idempotent recovery bridges them. |
| SNP-05 | Readers and GC must coordinate across processes. An atomic rename alone does not stop a generation being collected mid-read. |
| SNP-06 | A solution catalog carries a `(project_id, generation_id, source_fingerprint)` vector for every available member; a missing project is reported `partial`, never an invented empty graph. |
| SNP-07 | Publishing several repositories is not atomic; the catalog pointer is the read-model commit point, not a transaction over source repositories. |
| SNP-08 | Identical inputs, toolchain and profile produce identical canonical content bytes; timestamp and local sequence are not part of a generation's identity. |

## Pin one generation

This is the rule the rest of the guide exists to protect. A reader acquires the guard, reads
the catalog pointer **once**, and pins that generation in the request context. A
project-only query pins the project pointer once. From then on the request references one
exact generation vector, and every shard is read from that pinned generation - never from a
freshly re-read `current.json`.

A lazy-load of further shards either continues to hold the guard, or reopens it against
**the same exact generation** after re-checking availability. Switching to the latest
generation in the middle of a request is the failure mode: it is how one answer ends up
half old and half new.

The pinned generation is identified by `catalog_generation_id`, which is the lowercase
SHA-256 of the generation's canonical manifest. A pinned query that names a generation
which has already been collected returns `SNAPSHOT_EXPIRED` rather than silently reading
current data. An immutable historical generation does not imply live source freshness; the
response reports freshness separately.

## Why live publication is a race

Three facts make a naive reader wrong, and each maps to one invariant:

- **Commit and publish are two systems.** A SQLite write transaction and the filesystem
  publish are not one distributed transaction (SNP-04). A generation can therefore exist in
  the database and not yet on disk, or on disk and not yet pointed at. The durable outbox
  plus idempotent recovery is what makes that window recoverable.
- **A rename is not a reservation.** Moving a staging directory into place is atomic for the
  directory entry, but it does not stop the collector from removing the generation a moment
  later (SNP-05). Only a shared guard held across the copy coordinates the reader with GC.
- **Repositories publish independently.** A multi-repository solution is not published as
  one atomic batch (SNP-07). The catalog pointer is the read-model commit point; individual
  project pointers can advance while the catalog still pins an older vector, and a
  solution-level query legitimately continues to use that old vector until the catalog job
  republishes.

The writer's own ordering is designed around the same facts: parsing, SQLite transactions
and staging I/O all happen **outside** the exclusive guard, and the exclusive guard is held
only for the final checks, the current-pointer replacement, the catalog replacement, and GC
selection and deletion. No process holds a SQLite write transaction while waiting for the
guard.

## Pointer, manifest and shard closure

A published generation is three nested integrity claims, and the reader checks all three.
See [manifest-validation.md](../manifest-validation.md).

1. **Pointer.** `current.json` names exactly one generation and carries the digest of that
   generation's manifest. Its field set is exactly
   `{schema_version, generation_id, manifest_sha256}`; in V2 the generation directory name,
   the pointer's `generation_id` and the pointer's `manifest_sha256` are all the same
   string.
2. **Manifest.** The manifest lists every shard with `path`, `role`, `sha256`, `bytes` and
   `records`, and its own identity is the SHA-256 over its canonical bytes.
3. **Shard.** Each shard is pinned by the manifest entry that names it.

Canonical bytes are UTF-8 without a BOM, LF newlines, lexicographically sorted keys, compact
separators, exactly one trailing LF, no floats and no non-string keys, with Unicode
preserved as supplied. A document that is valid JSON but not canonical is refused rather
than silently re-serialised, because the digest that identifies a generation is a digest
over those exact bytes.

The order of checks is part of the contract: the schema major is checked before the
canonical form, the pointer's field set before its digest pair, and a shard's declared
length, then its digest, then its parse, then its record count - so a truncated shard is
reported as truncated and untrusted bytes never reach the JSON parser before they are known
to be the published ones.

Bounds are part of the claim: a shard whose declared byte size is outside `1..16777216`, whose digest is not a lowercase
sha256, or whose path is not portable and relative is refused as `ManifestInvalid`, and the manifest file itself is
`manifest.json`.

Paths are resolved by the trusted registry, never by the caller. A query names a logical
solution, project, lane and generation reference; an absolute POSIX path, a drive-qualified
Windows path, a UNC path, a backslash, an empty reference or any `..` segment is refused as
`UntrustedPath`, and containment is re-checked after symbolic links are resolved so a
symlink planted inside a lane cannot escape it. `.staging/` is never a read source.

## Guarded reading

`src/axiom_mcp/guard/` implements the Python side of the frozen protocol in
`contracts/native-reader-writer-guards.md`. It contains no policy of its own: it uses the
same two lock files, the same order and the same bounded wait that the Rust side is
required to implement, so a publisher written in one language and a reader written in the
other stay safe over the same files.

The two stable lock files are `admission.lock` and `data.lock`. The acquisition order is
**admission, then data**, on every platform; the release order is the reverse. A reader
holds the guard like this:

```
acquire admission.lock  SHARED
acquire data.lock       SHARED   (while still holding admission)
release admission.lock           (early: the reader no longer needs it)
... copy the bounded pointer/manifest/shard bytes from the pinned generation ...
release data.lock
... parse and respond (the guard is no longer held) ...
```

`SolutionGuard.reader()` is the context manager for that shape; `SolutionGuard.publisher()`
takes both locks **exclusive** for the bounded publication or guarded-deletion phase. A
publisher under the guard performs only the bounded catalog/pointer publication or guarded
deletion phase. Analysis, staging writes and SQLite work happen outside it.

The rules the engine enforces once, identically on both platforms:

| Rule | Value |
| --- | --- |
| Acquisition order | `admission.lock`, then `data.lock` |
| Release order | `data.lock`, then `admission.lock` |
| Default acquire timeout | `5000` ms |
| Maximum single wait | `60000` ms |
| Retry interval | `25` ms, doubling to a `250` ms cap |
| Cancellation | supported, checked between attempts |
| Lock upgrade | not permitted - release and retry from the start |
| Recursive acquire | not permitted in one execution path |

A timeout or cancellation releases every already-acquired guard in reverse acquisition
order and reports a bounded lock-timeout; the kernel is not assumed to be starvation-free.
Locks are OS-owned, not content-owned: a crashed process releases them because the OS does,
a lock file's contents never prove ownership, and the stable empty lock files stay in place -
removing them is a separate maintenance action that requires every participating process to
have stopped. A path whose identity changed between open and the identity check is reopened
rather than locked as if it were the named file.

## Generations and immutability

A published generation under `generations/<generation-id>/` is immutable (SNP-01). The
directory name is the generation's own manifest digest, so a directory that does not hold
the digest it is named for is refused before the pointer is trusted. Re-publishing an
existing generation verifies its contents match before reusing it instead of overwriting it.
The live and checkpoint lanes each hold their own `current.json`, `generations/` and
`.staging/`, and staging always lives on the same filesystem or volume as the generation and
pointer it will publish.

## Recovery and bounded retry

If a required generation is missing or corrupt, the reader drops the partial request and
retries the whole catalog read within a bounded budget. If it still cannot complete, it
returns `SNAPSHOT_UNAVAILABLE` or `SNAPSHOT_CORRUPT` and requests reconciliation through the
control plane according to policy.

| Condition | Code | Retryable | HTTP |
| --- | --- | --- | --- |
| Required generation cannot be read within the retry budget | `SNAPSHOT_UNAVAILABLE` | yes | 503 |
| A file is present but fails validation | `SNAPSHOT_CORRUPT` | no | 500 (unnamed by the control contract) |
| A pinned generation has been collected | `SNAPSHOT_EXPIRED` | no | 410 |

**The reader never answers half old and half new.** A failed query carries no partial result
envelope; it carries the error variant. `SNAPSHOT_UNAVAILABLE` is retryable because the
generation may reappear; `SNAPSHOT_EXPIRED` is not, because continuing against current data
would answer a different question under the old identity.

## Retention and GC

The default `live` policy keeps at least the current plus the previous two generations with a
ten-minute grace, and it is a tunable policy rather than a performance guarantee. GC never
deletes the current generation, checkpoint references, an active catalog vector or pending
outbox references, no matter how full the budget is. GC acquires the exclusive guard before
revalidating and deleting, and a shared reader that is still loading keeps it waiting;
loaded in-memory caches must not depend on a file after the guard is released. A
generation's mtime alone never decides liveness.

When the disk quota is full, publication pauses and optional caches are shed, and the
component reports the condition instead of deleting the current generation to make room.
Size is checked before staging, with headroom reserved for the old and new generation
together. Checkpoint-lane pruning happens only during an explicit checkpoint or update
command and only for generated, owned, unreferenced files; live background GC never modifies
tracked checkpoint files.

## The checkpoint lane

Each lane has a `live/` and a `checkpoint/` directory. The checkpoint lane is a durable,
usually Git-tracked record; the live lane is the working set a query answers from. A
snapshot-only gateway can read a checkpoint with the local `SolutionGuard` and no graphd
process running, which is why an offline checkpoint lane resolves exactly like a live one.

## Crash recovery

The protocol fixes a recovery outcome for each crash point, so recovery is a decision rather
than a guess:

| Crash point | What remains | Recovery |
| --- | --- | --- |
| Before the DB commit | Leased job, dirty generation | The lease expires and the job retries. |
| After the DB commit, before staging | `PREPARED` outbox | Regenerate the exact revision or mark it superseded as a checked action. |
| While writing shards | Orphan staging | Validate and continue if supported, or delete owned staging and re-export. |
| After a generation completes, before the pointer | Immutable orphan | Verify and reuse; it is not served yet. |
| After the pointer replace, before the outbox ack | New pointer, old outbox | Verify hash and fencing order, then ack; do not roll back to the old generation. |
| A project published while the catalog is old | New project current, catalog still pins the old vector | The solution query continues on the old vector while the catalog job retries. |
| During GC | Some unreferenced files already deleted | Idempotent retry; the current generation and catalog are never touched. |
| Pointer corrupt after an external edit | Invalid hash | Quarantine, report and reconcile; never guess the generation. |

## Raw multi-file reads are not safe

Reading the JSON files directly is allowed, but it is **not** the same guarantee. A raw
reader that opens several files itself holds no reader registration and no lock, so nothing
coordinates it with GC or with a Git checkout, and it is not covered by the coordinated
reader availability guarantee. It is not safe to reconstitute a multi-file answer by
opening `current.json` again for each file - that is exactly the SNP-03 failure.

A direct reader retains consistency only by following all of these: pin one immutable
generation, validate every manifest and shard hash, and report a missing or collected
generation instead of combining another generation into the result. When that is not enough -
or when the guarantee matters - use a guard-aware path: the `axiom-graphd snapshot read`
helper, or the `axiom-mcp` snapshot-reader library or CLI, which holds the shared guard
across the bounded copy. `axiom-graphd snapshot export --generation <id> --out <directory>`
produces a standalone immutable copy the user owns for offline reading, validated on export;
an export is never a silent full-graph dump by default. Optional MCP is not optional
consistency.

## Unverified / limits

- **The native guard engine is the only part of this guide that exists as code**, and its
  Windows backend is **not present at this revision**: `src/axiom_mcp/guard/` ships
  `locks_posix.py` only. Windows reader/writer safety is specified in
  `contracts/native-reader-writer-guards.md` but is not available or verified here.
- **Cross-process interoperability is unverified here.** The required Rust holder versus
  Python holder exclusion in two real processes, on Windows and POSIX, has not been run.
- **The POSIX guard tests skip where `fcntl` is unavailable**, so this revision's Windows
  test run does not exercise the POSIX primitive.
- **No GC, daemon or publication runtime exists in this repository.** Retention, disk-full
  behaviour and outbox recovery are specified in the protocol; nothing here implements or
  has observed them.
- **No reader-session or cache runtime exists.** The pin-once reader algorithm, the shared
  guard copy and the closure check are specified; the manifest validator
  (`src/axiom_mcp/manifest.py`) and the registry (`src/axiom_mcp/registry.py`) are
  implemented and tested on their own.
- **Platform coverage.** Verified on Windows with this repository's interpreter only; no
  Linux, macOS, CI, installed-wheel or native certification was performed, and the release
  gate stays closed.