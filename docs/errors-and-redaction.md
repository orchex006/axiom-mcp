# Errors and redaction

`src/axiom_mcp/errors.py` owns two responsibilities that are easy to conflate and
expensive to conflate wrongly: deciding **which surface** a failure belongs on,
and making sure **nothing prohibited** reaches that surface.

## Two surfaces, because there are two failures

MCP has a protocol layer and a tool layer. A single "error" shape would force one
of two bad outcomes: a host retrying a malformed request forever, or a host
treating an unavailable snapshot as a transport bug.

| Surface | When | Rendered as |
| --- | --- | --- |
| `protocol` | The request never became a meaningful call: malformed input, unknown method, missing or insufficient authentication. | A JSON-RPC error object. The JSON-RPC code stays generic (`-32602` etc.); the canonical Axiom code travels in `error.data.code`. |
| `tool_result` | The call ran and failed for a domain reason: the snapshot expired, the project is missing, the daemon is unavailable. | A tool result with `isError: true`, a human-readable `content` entry and a `structuredContent.error` body. |

The surface is decided by the code, not by the call site, so the same failure
renders the same way wherever it is raised. `VALIDATION_ERROR`,
`INCOMPATIBLE_INPUT`, `UNAUTHENTICATED`, `FORBIDDEN` and `UNSUPPORTED_OPERATION`
are protocol-surface codes; every other canonical code is a tool-result code. A
caller may override the surface explicitly with `AxiomError(..., surface=...)`,
but an unrecognised surface is a programming error and raises.

## Canonical codes

The code set is `query-response.schema.json`'s `queryError.code` enum plus the
three codes the read protocol adds for the states the enum does not name:
`PROJECT_UNAVAILABLE`, `SNAPSHOT_UNAVAILABLE` and `SNAPSHOT_CORRUPT`. The module
does not accept a code outside that set - `AxiomError("NOT_A_REAL_CODE", ...)`
raises `ValueError` - because a free-form code is exactly how two components
start disagreeing about what happened.

`tests/test_errors.py` loads the schema from the pinned specification checkout and
asserts that the module's set is the enum plus exactly those three codes, so an
upstream enum change is caught here instead of drifting.

HTTP status mapping follows `contracts/control-api-v1.md` for the codes that
contract names; anything unnamed keeps its canonical surface and reports `500`
rather than acquiring an invented status.

## The envelope

```
{code, message, retryable, details, request_id}
```

`request_id` is optional and is included when the error carries one or when the
renderer is given one. `retryable` is not hand-set per call site: it comes from
the canonical retryable set (`RATE_LIMITED`, `NOT_READY`, `DAEMON_UNAVAILABLE`,
`PROJECT_UNAVAILABLE`, `SNAPSHOT_UNAVAILABLE`), so a host can make a retry
decision without parsing prose.

## Redaction

Redaction is not re-derived here. `contracts/redaction-policy.md` is the policy,
and it ships an executable reference evaluator at
`axiom-specs/tests/test_redaction_policy.py`. This module transcribes the
detection table, the `<redacted:{category}>` placeholder, the metadata allowlist
and the path-relativisation rule, and then proves agreement:

* `tests/test_errors.py::CanonicalPolicyConformanceTests` loads the canonical
  evaluator by path and compares, case by case, the detection table (category,
  pattern and flags), the metadata allowlist, the absolute-path class set, and
  `find_violations` / `redact_text` / `metadata_violations` over **every**
  canonical fixture.
* `release/errors_spike.py` records the same comparison as a stored artifact, so
  the agreement is reproducible outside the test runner.

That is what keeps this a consumer of the policy rather than an editable fork: a
change to the policy that this module does not follow turns into a failing test,
not a silent divergence.

### What is removed

Source secrets (private-key blocks, cloud access keys, provider tokens, bearer
tokens, JWTs, password assignments, credential URLs, ODBC-style connection
strings) and absolute local paths (Windows drive paths, POSIX absolute paths, UNC
shares) are replaced with a class placeholder. An absolute path that is *inside*
the configured repository root becomes a repository-relative path instead of
being dropped, because that form is still useful for triage; an out-of-root path
is dropped entirely.

### What survives

Stable identifiers, repository-relative paths, symbol identity, line ranges,
digests, counts, coverage and error codes - the allowlisted query metadata. A
SHA-256 digest and an API route are deliberately not mistaken for secrets, and
`tests/test_errors.py` pins that.

### Where redaction is applied

`AxiomError.__init__` redacts the message and the detail tree **on construction**,
so a caller cannot hand an unredacted structure to a renderer. Redaction runs
*before* any bound is applied, so truncation can never expose a fragment of a
value the policy removes. Details are bounded in depth (6), width (64 items) and
string length (2048); each bound reports itself with a named placeholder rather
than silently dropping content.

## No tracebacks

`AxiomError` records the *type* of a cause and nothing else. There is no field
that can hold a formatted traceback, and `from_exception` maps an unexpected
exception to a fixed sentence plus `details.exception_type` unless the caller
supplies a safe message. A stack trace is a leak vector: it carries absolute
paths, environment layout and occasionally argument values.

## Verification

```
python -m pytest tests -q
python -m ruff check .
python -m ruff format --check .
python release/errors_spike.py
```

`AXIOM_SPECS_ROOT` may be set to point the conformance tests at a specific
specification checkout; otherwise a sibling `axiom-specs*` checkout is discovered
automatically.
