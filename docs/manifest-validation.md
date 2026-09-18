# Manifest validation - canonical bytes, digests and shard closure

Owner: `axiom-mcp`. Task: C-013. Implementation: `src/axiom_mcp/manifest.py`.

This module turns three integrity claims about a published generation into checks the
reader actually performs, instead of assumptions it hopes hold:

1. the pointer `current.json` names exactly one generation and carries the digest of the
   manifest that generation is made of;
2. the manifest lists every shard of the generation - path, role, SHA-256, byte size and
   record count - and its own identity is `sha256` over its canonical bytes;
3. each shard is pinned by the manifest entry that names it.

Nothing here is invented. The canonical byte rule is
`docs/11-GRAPH-DATA-CONTRACT.md` section 5 in `axiom-specs`; the structural shape is
`contracts/schemas/project-manifest.schema.json` and `contracts/schemas/pointer.schema.json`;
the `(project_id, generation_id, source_fingerprint)` vector and the refusal to substitute
a project's newest generation come from `docs/12-SNAPSHOT-READ-WRITE-PROTOCOL.md`
sections 1 and 5 (SNP-03, SNP-06); and the `generations/<hash>/` directory name is the
path contract in section 2. The module uses only `json`, `hashlib` and `re`, so it works
offline and in a snapshot-only gateway with no daemon running.

## Canonical bytes

`canonical_text` / `canonical_bytes` produce UTF-8 without a BOM, LF newlines,
lexicographically sorted object keys, compact separators, one trailing LF for a metadata
document, and Unicode preserved exactly as supplied. `canonicalization_reasons` refuses a
float and a non-string object key, which is the rule the specs-side reference serializer
`tools/canonical_json.py` enforces. `generation_id(manifest)` is
`sha256(canonical_bytes(manifest))`, so a manifest never references its own hash and the
generation directory name, the pointer's `generation_id` and the pointer's
`manifest_sha256` are all the same string in V2.

`is_canonical_bytes(raw)` answers whether arbitrary bytes are exactly the canonical
encoding of their own value. A published document that is valid JSON but not canonical -
indented, spaced, missing the trailing LF, written with `\u` escapes, or keeping its
insertion order - is refused as `NotCanonicalBytes` rather than silently re-serialized,
because the digest that identifies a generation is a digest over those bytes.

## Order of checks

The order is part of the contract this module implements:

| Where | Order | Why |
| --- | --- | --- |
| any document | schema major, then canonical form, then fields | a canonical document from an unknown major reports the version, not a formatting complaint |
| pointer | field set, then the digest pair | V2 fixes `generation_id == manifest_sha256`, so an unequal pair is a pointer defect, not a digest mismatch |
| shard | declared length, then digest, then parse, then record count | a truncated shard is reported as truncated; untrusted bytes never reach the JSON parser before they are known to be the published ones |
| generation load | manifest bytes, then directory name, then the pointer closure | a directory that does not hold the digest it is named for is refused before the pointer is trusted |

## What is refused

`load_manifest_bytes` refuses an unknown schema major (`UnsupportedSchemaMajor`), bytes
that are not the canonical encoding of their own value (`NotCanonicalBytes`), a missing,
unknown or self-hashing field, a duplicate file path or duplicate role
(`DuplicateManifestEntry`), an unsupported role, a path that is not portable and relative,
a digest that is not a lowercase sha256, a shard size outside `1..16777216`, a coverage
status outside the enum, a coverage count that is not a non-negative integer, and a
`processed_files` that exceeds `input_files` - all as `ManifestInvalid`.

`load_pointer_bytes` refuses a foreign schema major, non-canonical bytes, a field set that
is not exactly `{schema_version, generation_id, manifest_sha256}`, a digest that is not a
lowercase sha256, and a `manifest_sha256` that disagrees with `generation_id`.

`Manifest.verify_shard` refuses a shard whose length disagrees with its entry
(`ByteSizeMismatch`), whose digest disagrees (`DigestMismatch`), that is neither a JSON
array nor a JSON object, or whose record count disagrees (`RecordCountMismatch`).
`Manifest.verify_pointer` refuses a pointer that does not close over the manifest bytes.
`Manifest.verify_closure` applies all of that to every declared shard through a
caller-supplied read function and returns a `ClosureReport`; that is the rule a caller
needs in order to be able to say "this generation is fully loaded" honestly.

Failure messages name a file name, never a full path, so a host that renders an error to
a user does not leak the machine-local layout.

## Record count rule

A shard's `records` field is its element count when the shard is a JSON array, and `1`
when the shard is a single JSON object. That is the rule the shipped
`examples/snapshots` generations support: their `nodes`/`edges` shards are arrays and
their `coverage.json` is one object. A shard that is neither an array nor an object is
refused instead of guessed at. A future role whose shard is an object *map* would need a
canonical count rule from `axiom-specs` before this reader could count it.

## Tests and fixtures

`tests/test_manifest.py` pins this module against bytes it did not produce, and asserts
each fixture's SHA-256 so a silent fixture edit fails the suite:

* `tests/fixtures/canonical/` vendors the four `metadata`-form canonical vectors from
  `axiom-specs` `conformance/fixtures/canonical/` (pinned specification revision
  `1bf754298ba1bb266a77f13cded4bfe69484906f`). Their `input.json` is deliberately
  non-canonical and their `expected.canonical.json` is the pinned expected byte sequence.
  The two `identity-tuple` vectors of that corpus are a different form that this reader
  does not implement and are not vendored.
* `tests/fixtures/generation/auth-api/` vendors one shipped example generation. Its
  `manifest.json` hashes to its own generation directory name
  `7319237fdf1ce4ff27ea377c8bc3ae8cf1f115cffc71de369a373e1870ba9d1a`, so the fixture
  carries an identity claim this module did not compute.

## Limitations

Vendored fixtures are byte copies for test input; they are not a promoted shared contract
fixture, and no `axiom-specs` fixture index, ownership file or digest is changed by this
repository. Verified on Windows with the repository's own interpreter only: no Linux,
macOS, CI, installed-wheel or clean-venv run was performed, and the release gate stays
closed. Locking, caching, recovery and reader-session behaviour are other tasks; this
module reads bytes and verifies digests, and does not take the shared guard itself.
