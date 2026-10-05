# Redaction fixture manifest

Owner: `axiom-specs`. Governed by [contracts/redaction-policy.md](../../../contracts/redaction-policy.md).
Every file in this directory is a declared sample for the rejection/acceptance fixtures of G-011.

## Reserved synthetic placeholders

Every sample uses non-functional, reserved placeholder values only: the account name `example`,
the domain `example.invalid` (RFC 2606 reserved), the host `fileserver`, and the directory names
`share`, `project` and `repo`. No live credential and no real machine-local path is stored here.
Rejection samples MUST NOT be copied into `conformance/` bundles, published graph metadata or
query responses; they exist only to prove rejection by the reference evaluator.

## Accepted samples

- `allowlisted.metadata.json` - allowlisted query metadata (stable ids, repository-relative path,
  symbol identity, line range, digests, counts, coverage, notice). Accepted by both the text scan
  and the metadata allowlist.
- `boundary.metadata-key-not-allowlisted.json` - boundary sample: clean text, but a source-body
  key is not on the metadata allowlist, so the metadata check rejects it.

## Rejection samples

- `violating.private-key.json` - private key block.
- `violating.cloud-access-key.json` - cloud access key.
- `violating.provider-token.json` - provider token.
- `violating.bearer-token.json` - bearer token.
- `violating.password-assignment.json` - password assignment.
- `violating.credential-url.json` - credential-bearing URL.
- `violating.connection-string.json` - connection string with credentials.
- `violating.windows-path.json` - Windows drive absolute path.
- `violating.posix-path.json` - POSIX absolute path.
- `violating.unc-path.json` - UNC share absolute path.