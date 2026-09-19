# Portable wire paths and native bindings (V2-016)

`src/axiom_mcp/paths.py` is the one boundary where the two things this gateway calls "a path" are
normalized. It implements `contracts/cross-platform-v2.md` CP-02 and CP-03: serialize paths only
at contract boundaries, validate the lexical path *and* the native resolved handle before access,
and never trust a string-prefix containment test.

## The two normalizations

| | wire path | native binding |
| --- | --- | --- |
| what it is | a repository-relative UTF-8 reference from a request or a published manifest | an absolute path this machine owns: `AXIOM_HOME`, a registered `repo_root`, a lane root |
| function | `parse_wire_path` | `bind_native_root` |
| separator | `/` only - a backslash is refused | the host's own separator |
| syntax | relative; no drive, no UNC, no device prefix | host-native absolute |
| `.` / `..` | refused (traversal) | resolved natively - a binding is normalized, not judged |
| reserved names, `:`, `?`, `*`, trailing dot/space | refused (CP-03 default portable profile) | not applied |
| filesystem access | none | `Path.resolve()` once, to normalize the binding |
| spelling | preserved byte for byte, never rewritten | the resolved native spelling |

Neither function accepts the other's input, and a wire path is never resolved against the working
directory. That is the point of the split: applying the portable profile to a native root would
refuse a legitimate directory on this machine, and applying native syntax to a wire reference
would let a caller name the drive, the UNC share or the parent directory.

## Resolution order

`resolve_wire_path(root, reference)` always runs the same four steps in the same order:

1. validate the reference lexically (`parse_wire_path`) - no filesystem access, so a traversal,
   drive-qualified or UNC injection is refused before any read is attempted;
2. bind and resolve the native root (`bind_native_root`);
3. join the reference's own validated segments;
4. require the *resolved* candidate to sit inside the *resolved* root.

Step 4 compares resolved paths rather than string prefixes, because `/<root>` is a prefix of
`/<root>extra` and of nothing else that matters. An intermediate symbolic link that leaves the
root is refused at step 4 even though the reference itself was a relative, portable string.

## Where it is used

- `registry.SnapshotLocation.resolve` and `registry.CatalogLocation.resolve` are the only places a
  caller- or manifest-supplied reference becomes a file path. Both delegate here, so a reader
  refuses an injection before it opens a file.
- `shards.shard_path` adds its own stricter rule on top: a published generation holds regular
  files, so a *shard* that is a symbolic link is refused outright, in-root or not.
- `registry.portable_relative` keeps the frozen reference rule
  (`contracts/schemas/project-manifest.schema.json`). It is unchanged, including its acceptance of
  `a//b` and `a/./b`; the portable-profile refusals above belong to the native resolution boundary,
  not to that published pattern.

## What is deliberately not here

- **Path identity and collision detection.** Storing a casefolded or decomposed spelling would
  silently rename a source path, which CP-03 forbids. Case-only and NFC/NFD hazard detection is the
  graphd path-key layer's business; this module preserves the caller's spelling exactly.
- **A volume case-sensitivity probe, a long-path probe or reparse-point/ACL handling.** Each needs
  native fixtures; no claim about them is made from a unit test here.
- **Existence.** Resolution is not an open: a reference to a file that does not exist resolves to a
  path, and the caller's bounded read reports the real error.

## Verification

```text
python -m pytest tests/test_paths.py -q
python -m pytest tests -q
python -m ruff check .
python -m ruff format --check .
```

Native Windows/macOS evidence for real reparse-point and ACL behaviour is `not_run` unless it is
recorded with the platform identity and artifact hashes in the task evidence. The symlink legs in
`tests/test_paths.py` skip themselves on a host that cannot create a symbolic link without a
privilege, so they never turn into an unearned pass.
