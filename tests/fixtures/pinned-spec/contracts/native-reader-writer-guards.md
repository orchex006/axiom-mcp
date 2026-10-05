# Native reader/writer guard ABI - 1

Owner: axiom-specs. Implements the protocol requirements in `contracts/cross-platform-v2.md` and the snapshot read/write design. These are normative implementation requirements; this package ships no native adapter.

## 1. Stable identity and discovery

A registered solution instance has exactly two stable lock files, `admission.lock` and `data.lock`, in its trusted private guard directory:

```text
<AXIOM_HOME>/instances/<workspace-instance-id>/solution.guard/admission.lock
<AXIOM_HOME>/instances/<workspace-instance-id>/solution.guard/data.lock
```

Registry metadata maps logical solution + bound instance to that exact directory. Every language resolves the same verified file identity; no process invents a separate lock namespace. Do not unlink, truncate, recreate or replace either file while the instance can be used. Private permissions/ACLs prevent other users replacing them. Cross-`AXIOM_HOME` duplicate publishers must be rejected by output ownership registration; an override is not a bypass.

## 2. Normative lock table

| Lock file | Role | POSIX primitive | POSIX shared | POSIX exclusive | Windows primitive | Windows byte range | Windows open mode | Windows cancel flag | Release |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `admission.lock` | admission | `flock` | `LOCK_SH` | `LOCK_EX` | `LockFileEx` | offset 0, length 1 | `OPEN_ALWAYS` | `LOCKFILE_FAIL_IMMEDIATELY` | matching `UnlockFileEx`, then close handles |
| `data.lock` | data | `flock` | `LOCK_SH` | `LOCK_EX` | `LockFileEx` | offset 0, length 1 | `OPEN_ALWAYS` | `LOCKFILE_FAIL_IMMEDIATELY` | matching `UnlockFileEx`, then close handles |

The table is normative for names, byte ranges and primitives. `tools/guard_contract.py` re-reads this table and rejects the document when a row disagrees with the machine-readable block in section 3.

## 3. Frozen machine-readable protocol

The block below is the frozen protocol source. `tools/guard_contract.py` extracts it from this file, re-derives the digest over the declared `frozen_fields` and rejects the document when the digest, the lock table or any required normative statement drifts. The block and the prose are one artifact: change them together or not at all.

<!-- guard-protocol:begin -->
```json
{
  "contract_id": "axiom-native-reader-writer-guards",
  "contract_version": 1,
  "spec_version": "2.0.0-draft.1",
  "owner": "axiom-specs",
  "guard_directory_template": "<AXIOM_HOME>/instances/<workspace-instance-id>/solution.guard",
  "lock_files": [
    {
      "name": "admission.lock",
      "role": "admission",
      "posix_primitive": "flock",
      "posix_scope": "whole-file",
      "posix_shared": "LOCK_SH",
      "posix_exclusive": "LOCK_EX",
      "windows_primitive": "LockFileEx",
      "windows_byte_range": "offset 0, length 1",
      "windows_open_mode": "OPEN_ALWAYS",
      "windows_share_mode": "FILE_SHARE_READ | FILE_SHARE_WRITE",
      "windows_share_excludes": "FILE_SHARE_DELETE",
      "windows_exclusive_flag": "LOCKFILE_EXCLUSIVE_LOCK",
      "windows_cancel_flag": "LOCKFILE_FAIL_IMMEDIATELY",
      "release": "UnlockFileEx matching range, then close every handle"
    },
    {
      "name": "data.lock",
      "role": "data",
      "posix_primitive": "flock",
      "posix_scope": "whole-file",
      "posix_shared": "LOCK_SH",
      "posix_exclusive": "LOCK_EX",
      "windows_primitive": "LockFileEx",
      "windows_byte_range": "offset 0, length 1",
      "windows_open_mode": "OPEN_ALWAYS",
      "windows_share_mode": "FILE_SHARE_READ | FILE_SHARE_WRITE",
      "windows_share_excludes": "FILE_SHARE_DELETE",
      "windows_exclusive_flag": "LOCKFILE_EXCLUSIVE_LOCK",
      "windows_cancel_flag": "LOCKFILE_FAIL_IMMEDIATELY",
      "release": "UnlockFileEx matching range, then close every handle"
    }
  ],
  "acquisition_order": [
    "admission.lock",
    "data.lock"
  ],
  "release_order": [
    "data.lock",
    "admission.lock"
  ],
  "no_upgrade": true,
  "no_recursive_acquire": true,
  "single_guard_default": true,
  "bounded_wait": {
    "policy": "bounded-retry",
    "unbounded_blocking_allowed": false,
    "cancel_supported": true,
    "default_timeout_ms": 5000,
    "max_timeout_ms": 60000,
    "retry_initial_ms": 25,
    "retry_max_ms": 250,
    "on_timeout": "release every acquired guard in reverse acquisition order, report a bounded lock-timeout and retry from the start of the acquisition order"
  },
  "crash_release": {
    "mechanism": "process termination releases OS-owned locks",
    "pid_file_is_ownership": false,
    "lock_file_content_is_ownership": false,
    "stale_pid_is_live_holder": false,
    "stable_empty_files_may_persist": true,
    "removal_requires_all_processes_stopped": true
  },
  "interop_requirement": {
    "languages": [
      "rust",
      "python"
    ],
    "distinct_processes": true,
    "same_primitive_per_platform": true,
    "library_name_alone_insufficient": true,
    "windows_and_posix_required": true,
    "required_scenarios": [
      "independent publisher",
      "independent reader",
      "gc process",
      "process kill",
      "handle closure",
      "timeout or cancellation",
      "sharing violation",
      "reader flood",
      "catalog vectors",
      "missing shards",
      "old pointer preservation"
    ]
  },
  "prose_anchors": [
    "<AXIOM_HOME>/instances/<workspace-instance-id>/solution.guard",
    "FILE_SHARE_READ | FILE_SHARE_WRITE",
    "FILE_SHARE_DELETE",
    "LOCKFILE_EXCLUSIVE_LOCK",
    "LOCKFILE_FAIL_IMMEDIATELY",
    "UnlockFileEx",
    "No lock upgrade from shared",
    "acquisition order is admission then data",
    "release order is data then admission",
    "default acquire timeout is 5000 ms",
    "at most 60000 ms",
    "retry interval starts at 25 ms and is capped at 250 ms",
    "unbounded blocking is not permitted",
    "process termination releases OS-owned locks",
    "PID files alone are not locks",
    "Rust and Python must demonstrate the same primitive in two actual processes"
  ],
  "frozen_fields": [
    "lock_files",
    "acquisition_order",
    "release_order",
    "bounded_wait",
    "crash_release",
    "interop_requirement"
  ],
  "digest_algorithm": "sha256 of canonical JSON (sort_keys, separators (',',':'), ensure_ascii) over the frozen_fields subset",
  "protocol_digest": "06fadbdcd8ef4fba9573b10f838f060bc23652286ba757061249737b1d717f8a"
}
```
<!-- guard-protocol:end -->

## 4. Primitive agreement

On supported local POSIX filesystems use `flock` over the whole file: `LOCK_SH` for shared and `LOCK_EX` for exclusive, with nonblocking attempts and bounded cancellable waits. Do not mix `flock` with a different `fcntl` lock family and assume interoperability. On Windows open the same files with read/write access, `OPEN_ALWAYS` semantics and share mode `FILE_SHARE_READ | FILE_SHARE_WRITE` without `FILE_SHARE_DELETE`, then use `LockFileEx` over byte range offset 0, length 1; the shared mode omits `LOCKFILE_EXCLUSIVE_LOCK` and the exclusive mode includes it. Use `LOCKFILE_FAIL_IMMEDIATELY` in the bounded retry adapter. Release with a matching `UnlockFileEx` range and close every handle.

## 5. Acquisition order and release order

Cooperative processes use one order on every platform: the acquisition order is admission then data, so every operation that needs both holds `admission.lock` first and `data.lock` second. Reader: acquire admission shared, acquire data shared while still holding admission, release admission, copy the bounded pointer/manifest/shard bytes from the pinned generation, close snapshot handles, release data, and only then parse or respond. Publisher or GC: acquire admission exclusive, acquire data exclusive while still holding admission, perform only the bounded catalog/pointer publication or guarded deletion phase, then the release order is data then admission. Stage expensive analysis and files before locking, and never block on a lock while holding a SQLite write transaction.

No lock upgrade from shared (`LOCK_SH`) to exclusive (`LOCK_EX`) is permitted; release and retry from the start for a changed operation. Never recursively acquire a lock on the same instance in one execution path. The default operation holds one solution guard; a future multi-instance operation must sort stable instance IDs and follow a single declared order, and V2 must not use cross-instance deadlock-prone nesting.

## 6. Bounded wait, timeout and retry

Every acquisition is bounded. The default acquire timeout is 5000 ms and no single wait may exceed at most 60000 ms; unbounded blocking is not permitted. The retry interval starts at 25 ms and is capped at 250 ms, with cancellation checked between attempts. A timeout or cancellation releases every already-acquired guard in reverse acquisition order, reports a bounded lock-timeout instead of claiming guaranteed starvation freedom, and retries from the start of the acquisition order rather than upgrading a held lock. Kernel scheduling and lock fairness before admission acquisition are not promised.

## 7. Crash release, stale content and ownership

Locks are OS-owned, not content-owned. A clean shutdown and a crashed process are the same case at the lock layer: process termination releases OS-owned locks even when no cleanup handler ran. PID files alone are not locks: a recorded process ID, a JSON holder record or any other lock-file content never grants, extends or proves ownership, and a stale PID must never be read as a live holder. Cancellation and error paths release in reverse acquisition order. Stable empty lock files may remain; removing them is a separate maintenance action permitted only after every participating process has stopped. Do not leak inherited lock handles into child processes.

## 8. Cross-language interoperability

Rust and Python must demonstrate the same primitive in two actual processes on every required execution lane under ADR-0015: a Rust holder must exclude a Python holder and a Python holder must exclude a Rust holder in both shared and exclusive modes over the same files. Naming a library, linking a crate or sharing a wrapper proves nothing; the exclusion must be observed in two real processes. Both languages implement the same acquisition order, the same bounded wait and the same release path, so a handoff between a publisher written in one language and a reader written in the other stays safe.

## 9. Unguarded direct readers

Direct JSON is allowed. Pin an immutable generation once, validate all manifest/shard hashes and report a missing or GC generation without combining another generation into the result. Such readers do not receive the coordinated reader availability guarantee. An explicit validated export gives an independent offline copy. Optional MCP is not optional consistency.

## 10. Release acceptance

Exercise independent Rust publisher + Python reader + GC processes on every required execution lane under ADR-0015. Include process kill, handle closure, timeout/cancellation, sharing violation, reader flood, catalog vectors, missing shards and old pointer preservation. Reference path/bootstrap tests in this spec repository do not implement or certify this ABI.
