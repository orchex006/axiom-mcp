"""How axiom-mcp turns a logical reference into a snapshot path.

Owner: `axiom-mcp`. Consumes `SOURCE-OF-TRUST.md` section 5,
`docs/12-SNAPSHOT-READ-WRITE-PROTOCOL.md` section 2 and
`contracts/native-reader-writer-guards.md` section 1. Implements C-009.

## The rule

A query names a **logical** target - solution id, project id, lane, generation
id, generation-relative shard path. It never names a file. `axiom_mcp.registry`
is the only place that rule is turned into a path, and it does so by looking up
a binding the owner registered on this machine.

```python
from axiom_mcp import registry

loaded = registry.load_registry()                      # <AXIOM_HOME>/config/registry.json
location = loaded.location("alpha", "auth-api")        # -> .../.axiom/graph/alpha/auth-api/live
pointer = location.pointer                             # .../live/current.json
shard = location.resolve("nodes.json")                 # validated, contained
```

A caller that supplies `"C:/tmp/anything.json"`, `"..\\..\\secret.json"` or
`"/etc/passwd"` gets `UntrustedPath`, not a file handle.

## AXIOM_HOME

| OS | Default |
| --- | --- |
| Windows | `%LOCALAPPDATA%\\Axiom` |
| Linux | `${XDG_STATE_HOME:-$HOME/.local/state}/axiom` |
| macOS | `$HOME/Library/Application Support/Axiom` |

An explicit `AXIOM_HOME` override must be absolute; a relative one raises
`RelativeAxiomHome`. This module never resolves a relative override against the
working directory, because that would make the same query mean different files
depending on where the gateway was started.

## Registry document

`<AXIOM_HOME>/config/registry.json` carries absolute local bindings. Nothing in
it is portable, which is why it is machine-local state and never a graph
snapshot member.

```json
{
  "schema_version": 1,
  "axiom_home": "C:/Users/demo/AppData/Local/Axiom",
  "instances": [{ "instance_id": "local-1" }],
  "solutions": [
    {
      "solution_id": "alpha",
      "instance_id": "local-1",
      "catalog_host_repo": "auth-repo",
      "repositories": [
        {
          "repo_id": "auth-repo",
          "repo_root": "C:/src/auth",
          "projects": [{ "project_id": "auth-api" }]
        }
      ]
    }
  ]
}
```

Refused, and why:

- an unknown `schema_version` major - the reader would be guessing at a shape;
- a duplicate solution, repository or project id - the binding would be ambiguous;
- a relative `repo_root` or `axiom_home` - the resolution would depend on the cwd;
- a `repo_root` inside `AXIOM_HOME` - a local checkout is not machine state;
- a declared `guard_directory` that is not
  `<AXIOM_HOME>/instances/<id>/solution.guard` - the ABI fixes that path, so a
  different one is a second lock namespace under another name;
- malformed JSON - a registry that cannot be read is an error, not an empty one.

An **absent** registry file is not an error. It resolves nothing (`is_empty`),
which is the honest answer on a machine that has registered no solution.

## Guard directory and lock files

`contracts/native-reader-writer-guards.md` fixes exactly two stable lock files
per registered instance:

```text
<AXIOM_HOME>/instances/<workspace-instance-id>/solution.guard/admission.lock
<AXIOM_HOME>/instances/<workspace-instance-id>/solution.guard/data.lock
```

`guard_directory(instance_id)` and `lock_path(instance_id, role)` return those
paths and nothing else, so a lock backend never invents a namespace.

## Paths

```text
<repo-root>/.axiom/graph/<solution-id>/<project-id>/{live,checkpoint}/
    current.json
    generations/<generation-id>/
    .staging/<nonce>/
<repo-root>/.axiom/graph/<solution-id>/_catalog/{live,checkpoint}/...
```

`SnapshotLocation.resolve` joins the reference with POSIX parts, then re-checks
containment after symbolic links are resolved, so a symlink planted inside a
lane cannot be used to read a file outside it. `.staging/` is never a read
source: it is named here only so a caller can tell it apart from a generation.

## Not in this slice

This module resolves locations. It does not read, validate, cache or lock
anything; the shard reader, the manifest validator, the native guards and the
recovery policy are later slices in the same work package. It does not contact a
daemon, so an offline checkpoint lane resolves exactly like a live one.
"""
