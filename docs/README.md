# axiom-mcp documentation

These guides are owned and released with `axiom-mcp`. They describe the intended
behavior of the gateway and record what has actually been executed; they are not
a record of a running gateway or a certificate for any target. Resolve
cross-component normative references through the pinned `axiom-specs` revision -
canonical contracts live there and must not be forked as an editable copy here.
`pack_path`-style relative links are navigation inside this repository only.

## Transports and protocol

- [Streamable HTTP transport](http-transport.md)
- [stdio transport](stdio-transport.md)
- [Query transports - JSON-first, stdio and HTTP](query-transports.md)
- [How axiom-mcp places the official MCP SDK inside FastAPI](sdk-mount-lifecycle.md)
- [Host, Origin and scoped bearer authentication](security-host-origin-auth.md)

## Query and read path

- [Bounded query engine](query-engine.md)
- [Snapshot reader core](snapshot-reader-core.md)
- [How axiom-mcp turns a logical reference into a snapshot path](snapshot-registry.md)
- [Snapshot race and read-only guide](guides/snapshots.md)
- [Portable wire paths and native bindings (V2-016)](portable-paths.md)

## Errors, contracts and compatibility

- [MCP tools and response reference](reference/mcp.md)
- [Errors and redaction](errors-and-redaction.md)
- [Manifest validation - canonical bytes, digests and shard closure](manifest-validation.md)
- [Runtime compatibility](runtime-compatibility.md)

## Operations

- [Installation and upgrade](install-and-upgrade.md)
- [CLI: version and doctor](cli-version-and-doctor.md)
- [Locked native entrypoints](locked-entrypoints.md)
- [Update check and approved-plan delegation](update-plan-delegation.md)
- [Release packaging into versioned environments](release-packaging.md)

## Verification evidence

- [Native query and host process matrix (V2-031)](../tests/native/README.md) - what was executed on
  each host, the real exit codes, and the targets that stay `not_run`.

## Ownership

Documentation for code is owned here and changes in the same branch as the code.
Cross-component navigation is aggregated from pinned revisions; it does not
require a separate documentation repository or a second version train.
