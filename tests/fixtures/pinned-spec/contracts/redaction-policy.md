# Redaction policy - source, fixtures and published metadata

Owner: `axiom-specs`. Spec `2.0.0-draft.1`. Status: draft contract, not a released component.
Normative keywords follow [normative-terms.md](normative-terms.md).

## 1. Purpose and scope

Axiom copies derived information out of an authorized source boundary into graph payloads, MCP
query responses, diagnostics bundles, conformance fixtures and task evidence. This policy fixes
which bytes MUST NOT cross that boundary and which metadata MUST stay available so that queries
remain useful after redaction.

The policy applies to:

- processed graph payload published under the project workspace graph output directory;
- MCP query responses and control API responses, including error `details`;
- diagnostics and support bundles;
- shared conformance bundles and test fixtures;
- evidence artifacts that embed source text, configuration or command output.

It does not re-define the threat model or the root allowlist; those stay canonical in
[docs/23-SECURITY-AND-TRUST.md](../docs/23-SECURITY-AND-TRUST.md) and the workspace
[AGENTS.md](../AGENTS.md). Where this policy and those sources disagree, the stricter rule applies
and the conflict MUST be reported instead of resolved silently.

## 2. Prohibited content

The following classes MUST NOT appear in any in-scope artifact. Class ids are normative and are the
ids the reference evaluator reports.

| Class id | Prohibited content | Reason |
|---|---|---|
| `private_key_block` | Private key material, including any PEM-style private key block. | Unrecoverable credential disclosure. |
| `cloud_access_key` | Long-lived cloud access key identifiers. | Credential disclosure. |
| `provider_token` | Provider-issued tokens such as personal access tokens, app tokens or bot tokens. | Credential disclosure. |
| `bearer_token` | Authorization bearer values. | Session/credential disclosure. |
| `jwt` | Three-part JSON web tokens. | Session/credential disclosure. |
| `password_assignment` | Password, passphrase, secret, token or API key assigned a value. | Credential disclosure. |
| `credential_url` | URL carrying user information and a secret. | Credential disclosure in an addressable form. |
| `odbc_connection_string` | Connection strings that carry authentication. | Credential disclosure. |
| `windows_absolute_path` | Windows drive-letter absolute local paths. | Machine-local identity and user name leakage; not portable. |
| `posix_absolute_path` | POSIX absolute local paths under a system or user root. | Machine-local identity and user name leakage; not portable. |
| `unc_path` | UNC share paths. | Host and share topology leakage; not portable. |

Rules:

- An in-scope artifact MUST NOT be persisted while it still contains a prohibited span.
- A detector, error message, log line or violation report MUST NOT persist the matched value. A
  report MUST identify the class and the location, not echo the bytes.
- Redaction MUST NOT be weakened by moving the same value into a different field, a comment, a
  fixture, a test expectation or an evidence attachment.
- A prohibited span MAY be preserved only as a non-invertible digest when a change-detection use
  case requires it; the raw value MUST NOT be stored.

## 3. Allowlisted metadata

After redaction the following metadata remains usable for graph queries. These keys are the
allowlist; any other key is rejected by the metadata check.

| Key | Meaning |
|---|---|
| `schema_version` | Contract version of the payload. |
| `solution_id` | Registered solution identity. |
| `project_id` | Registered project identity. |
| `repo` | Registered repository identity. |
| `path` | Repository-relative file path. |
| `language` | Detected or configured language. |
| `symbol_kind` | Symbol category such as function, type or test. |
| `symbol_name` | Symbol name. |
| `line_start` | First source line of the symbol or evidence range. |
| `line_end` | Last source line of the symbol or evidence range. |
| `content_sha256` | Digest of the analyzed content, for freshness and change detection. |
| `source_fingerprint` | Digest of the analyzed source set. |
| `generation_id` | Immutable published generation identity. |
| `bytes` | Analyzed size in bytes. |
| `nodes_count` | Number of graph nodes for the subject. |
| `edges_count` | Number of graph edges for the subject. |
| `coverage` | Coverage classification of the analysis. |
| `analysis_profile` | Analysis profile that produced the payload. |
| `timestamp_utc` | UTC observation time. |
| `error_code` | Stable error identifier. |
| `request_id` | Correlation identifier for a request. |
| `redaction_notice` | Statement that redaction was applied. |

Rules:

- `path` MUST be repository-relative. An absolute local path that resolves inside an authorized,
  registered source root SHOULD be replaced by its repository-relative form so the key stays
  queryable; a path that resolves outside every authorized root MUST be dropped.
- Source bodies, comments, string literals, environment values, raw configuration dumps and
  unregistered absolute paths MUST NOT be copied into allowlisted keys.
- A digest of a redacted value is metadata, not the redacted value; storing the digest MUST NOT be
  used as a way to retain a secret.

## 4. Executable reference evaluator

The rules above are executable. `tests/test_redaction_policy.py` is the reference evaluator and is
pure standard library: it performs no network, install or credential access, and it is
deterministic. It exposes:

- `find_violations` - reports every prohibited span as a class id plus a character span, never the
  matched bytes;
- `categories` and `is_clean` - convenience predicates over the same scan;
- `repository_relative_path` - maps an absolute path inside an authorized root to its
  repository-relative form, and reports nothing for paths outside it;
- `redact_text` - replaces every prohibited span, using the repository-relative form where the path
  is in root and a placeholder otherwise;
- `metadata_violations` - applies the section 3 key allowlist and the text scan to a query metadata
  object.

Run the evaluator and its fixtures with:

```text
python -m pytest tests/test_redaction_policy.py -q
```

A change to a detection class, a pattern or the metadata allowlist MUST update the evaluator, the
fixtures and this document in the same change.

## 5. Rejection samples

Negative fixtures MUST live under `tests/fixtures/redaction/`, MUST be declared in the manifest
[tests/fixtures/redaction/README.md](../tests/fixtures/redaction/README.md), and MUST use synthetic
reserved placeholders only. The reserved placeholders are the account `example`, the reserved
domain `example.invalid`, and the host and directory names `fileserver`, `share`, `project` and
`repo`. No rejection sample may contain a live credential or a real machine-local path.

Rejection samples are inputs to the evaluator, not sanctioned payloads. They MUST NOT be copied
into `conformance/` bundles, published graph metadata or query responses, and they MUST NOT be
treated as an allowlist extension. A sample whose prohibited class overlaps another class may be
reported under more than one class; every reported class MUST be removed before persistence.

## 6. Verification requirements

A redaction change MUST provide:

- a targeted positive run proving the allowlisted metadata fixture is accepted and stays usable;
- at least one negative case proving a prohibited fixture is rejected, and a boundary case for a
  shape that must remain accepted, such as a repository-relative path, a digest or a route;
- preserved command output and fixture snapshots under the task evidence directory, with the
  digests recorded in the task completion evidence.

## 7. Relationship to other contracts

- [docs/23-SECURITY-AND-TRUST.md](../docs/23-SECURITY-AND-TRUST.md) requires a field allowlist for
  configuration extraction, skipping environment files, credentials, private keys, connection
  strings with authentication, secret manifests and credential-bearing URLs.
- [contracts/control-api-v1.md](control-api-v1.md) requires error JSON with redacted `details` and
  forbids returning raw source, tokens, stack traces or unregistered absolute paths.
- The workspace [AGENTS.md](../AGENTS.md) forbids carrying bytecode, absolute paths, tokens or
  machine-local state into fixtures or Git.
- [Development.md](../Development.md) section 5 requires a secrets check as part of the completion
  gate; this policy defines what that check MUST reject.