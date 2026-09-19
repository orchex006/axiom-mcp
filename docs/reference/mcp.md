# MCP tools and response reference

Owner: `axiom-mcp`. Normative sources live in `axiom-specs`:
`repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md` sections 2, 4, 5, 6, 7 and 9 define the
transport, the tool catalog and the `graph_query` contract;
`contracts/schemas/query-request.schema.json` and
`contracts/schemas/query-response.schema.json` define the request and response shapes;
`contracts/control-api-v1.md` fixes the canonical codes and their HTTP status;
`contracts/redaction-policy.md` fixes what may cross the response boundary. This page is a
reference for that contract, not a record of a running gateway.

**Status: the tool layer is landing task by task.** The tree contains the transports, the
error model, the native guard engine, the registry, the manifest validator, the bounded
query engine and the CLI. `src/axiom_mcp/tools/` now exists: `catalog.py` holds the
canonical six-tool catalog, `context.py` holds the closed-argument parser and the
re-authorizing `ToolContext`, and `status.py` implements `graph_status` and `query.py` implements the
`graph_query` dispatcher. The rows below record which tools are real at this revision; every remaining shape is a **contract, not an
observation**, until the registering task named in the catalog lands.

## Transports

| Surface | Channel | Contract this revision relies on |
| --- | --- | --- |
| Streamable HTTP | `POST /mcp` | The official SDK transport mounted at an exact path with no redirect; a real `initialize` handshake, event stream and session id. See [http-transport.md](../http-transport.md). |
| stdio | process stdin/stdout | Protocol frames on stdout only; every diagnostic on stderr with no banner. See [stdio-transport.md](../stdio-transport.md). |
| Health | `GET /healthz` | Minimal process health. It makes **no** readiness claim. |
| Readiness | `GET /readyz` | Query and control availability, reported separately; `503` with `NOT_READY` while a plane is down. |

A plain REST route that returns a hand-written `initialize`-shaped body is not a transport.
The two supported surfaces carry the same query core, so an HTTP client and a stdio client
must receive equivalent results for the same request.

## Tool catalog

Tool names and the capability each requires are fixed by
`repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md` section 4 and transcribed in
`src/axiom_mcp/security.py`. An unknown tool name is treated as a read at the transport
boundary, so the catalog owner answers `NOT_FOUND` rather than the boundary widening
access.

| Tool | Purpose | Capability | Side effect | Registered by | Status |
| --- | --- | --- | --- | --- | --- |
| `graph_status` | Solution/project freshness, coverage, generations, capabilities | `read` | none | C-028 | Implemented (`tools/status.py`) |
| `graph_query` | Graph operation: `search`, `context`, `neighbors`, `callers`, `dependencies`, `impact`, `path`, `changes` | `read` | none | C-029 | Implemented (`tools/query.py`) |
| `graph_reconcile` | Enqueue a graphd reconcile job for a scope | `reconcile` | Enqueue graphd job | C-030 | Specified, not yet available |
| `graph_job` | Job status or explicit cancel | `reconcile` | Status read; `cancel` is an explicit write | C-031 | Specified, not yet available |
| `graph_verify` | Bounded verification request against an expected fingerprint or barrier | `checkpoint` | Bounded verification request | C-032 | Specified, not yet available |
| `graph_version` | Selected components and their compatibility | `read` | none | C-033 | Specified, not yet available |

`graph_reconcile`, `graph_job` and `graph_verify` are the mutation paths. They reach beyond
the read-only plane and the gateway re-authorizes the incoming capability instead of
inheriting admin authority. Update, bootstrap and install are deliberately **not** MCP
tools: they travel through the CLI or the skill workflow, so no MCP tool downloads or
executes an update by default.

## `graph_query` request

`contracts/schemas/query-request.schema.json` is normative. The schema is closed:
`additionalProperties` is `false`, so an extra key is a `VALIDATION_ERROR` rather than a
silently ignored input.

| Field | Type | Rule |
| --- | --- | --- |
| `solution_id` | string | Required. `^[a-z][a-z0-9-]{0,62}$`. |
| `operation` | enum | Required. One of `search`, `context`, `neighbors`, `callers`, `dependencies`, `impact`, `path`, `changes`. |
| `project_id` | string | One project. Must **not** be combined with `project_ids`. |
| `project_ids` | array | 1-64 unique ids matching the id pattern. Must not be combined with `project_id`. Omitted scope means the registered solution within the caller's authorization. |
| `target` | string | Required for `context`, `neighbors`, `callers`, `dependencies`, `impact` and `path`. |
| `target_to` | string | Required for `path`. |
| `query` | string | Required for `search`. |
| `depth` | integer | `0`-`8`, default `2`. |
| `max_nodes` | integer | `1`-`500`, default `50`. |
| `max_edges` | integer | `0`-`1000`, default `100`. |
| `max_bytes` | integer | `512`-`32768`, default `8192`, metadata included. |
| `consistency` | enum | `allow_stale`, `require_fresh` or `pinned`, default `allow_stale`. |
| `catalog_generation_id` | string | `^[0-9a-f]{64}$`. Required when `consistency` is `pinned`. |
| `baseline_catalog_generation_id` | string | `^[0-9a-f]{64}$`. Required for `changes`. |
| `cursor` | string | Opaque continuation token. |
| `edge_kinds` | array | Unique subset of the fifteen edge kinds below. |
| `direction` | enum | `incoming`, `outgoing` or `both`, default `both`. |
| `projection` | array | Unique subset of `identity`, `relations`, `source_locations`, `coverage`. |

The complete `edge_kinds` allowlist, in schema order: `CONTAINS`, `IMPORTS`,
`REFERENCES`, `CALLS`, `INHERITS`, `IMPLEMENTS`, `EXPOSES`, `CALLS_ENDPOINT`, `READS`,
`WRITES`, `EXECUTES_PROCEDURE`, `DEPENDS_ON`, `PUBLISHES`, `SUBSCRIBES`, `TESTS`.

`max_bytes` is a hard cap that includes metadata. When the result would exceed it, the
gateway truncates to fit and sets `truncated`; it does not exceed the cap to answer in
full. Traversal uses a visited set and hard expansion and wall-time budgets. There is no
arbitrary SQL, Cypher, code evaluation or unbounded regular expression anywhere in the
query path. That is enforced by the request parser (`tools/context.closed_arguments`)
rather than promised: `sql`, `cypher`, `command` and `script` are not fields of the request,
a request carrying one is a `VALIDATION_ERROR` that names only the key, and a selector that
cannot apply to the requested operation (`target` on `search`, `direction` on `changes`) is
refused rather than silently ignored.

### Budgets are ceilings, not hints

`depth`, `max_nodes`, `max_edges` and `max_bytes` are validated against the schema before
the query runs. A value above its maximum is refused with `LIMIT_EXCEEDED`; the gateway does not
clamp an over-budget request to the maximum and continue, because that would answer a
different question than the one asked. A budget that a caller leaves unset takes the
documented default.

## `graph_query` response

`contracts/schemas/query-response.schema.json` is normative. A successful response carries:
`schema_version`, `solution_id`, `catalog_generation_id`, `project_generations`,
`freshness`, `coverage`, `verification`, `nodes`, `edges`, `truncated`, `warnings`, and
optionally `next_cursor`.

| Field | Shape |
| --- | --- |
| `schema_version` | const `1`. |
| `project_generations` | Array of `{project_id, generation_id}`, both ids validated by pattern. |
| `freshness` | One of `fresh`, `stale`, `updating`, `unknown`, `invalid`. |
| `coverage` | One of `complete_for_profile`, `partial`, `unsupported`. |
| `verification` | Object with required `mode` in `inventory_hash`, `watcher_hint`, `none`; optional `verified_at` and `source_fingerprint`. |
| `nodes` | Array of node objects; each requires `id`, `project_id`, `kind`, `name`, `qualified_name`, `language`, `source`, `identity_quality`, `attributes`, with `additionalProperties` false. |
| `edges` | Array of edge objects; each requires `id`, `source_id`, `target_project_id`, `kind`, `resolution`, `evidence`, `analyzer_id`, and either `target_id` or `unresolved_target` exactly once. |
| `truncated` | Boolean. |
| `warnings` | Array of non-empty strings. |

A failed query never carries a partial result envelope. The response is either a complete
result or the `error` variant:

```json
{
  "schema_version": 1,
  "solution_id": "alpha",
  "error": {
    "code": "SNAPSHOT_UNAVAILABLE",
    "message": "no readable generation is currently published",
    "retryable": true
  }
}
```

Provenance is carried in each node's `source` location and each edge's `evidence` and
`analyzer_id`. The schema deliberately has no competing `result` or `snapshot` envelope: a
consumer that finds one is reading a non-conformant server, not an alternate shape.

### Consistency at this revision

`allow_stale` (the default) answers from the pinned generation. A named
`catalog_generation_id` is always honoured as a pin: when the lane no longer publishes that
vector the answer is `SNAPSHOT_EXPIRED` rather than a read of whatever is current, and a
`pinned` request without the field is a `VALIDATION_ERROR`. `require_fresh` needs the
`reconcile` capability - a read-only token gets `FORBIDDEN` before the control plane is
consulted at all - and with the capability the request enqueues a bounded reconcile and
answers `NOT_READY` with the job id, because the request itself cannot prove the reconcile
finished. Every `graph_query` answer is `freshness=unknown` with `verification.mode=none`: a
pinned generation proves which bytes answered, not that the source tree was re-read.

`changes` compares the head against a baseline generation. The baseline vector cannot be
pinned through the read session at this revision, so a named baseline that is *not* the
current one answers `missing_baseline` with a `baseline_not_pinnable` warning instead of a
diff against whatever is current, while a baseline equal to the head is a comparable (empty)
diff. Facts are head-side only: an `added` fact is a head document and a `modified` fact is
rebuilt as one from the head side of the before/after pair, so every node still carries its
own `source`; `removed` facts belong to the baseline generation and are not carried.

### Freshness, coverage and verification are three different claims

They are separate fields on purpose, because collapsing them is how a caller treats a
stale-but-valid answer as fresh.

- `freshness` describes the relationship between the pinned generation and the live source
  inventory: `fresh`, `stale`, `updating`, `unknown` or `invalid`.
- `coverage` describes how much of the requested profile the generation actually answers:
  `complete_for_profile`, `partial` or `unsupported`. A partial solution reports `partial`
  and a partial result; it never fabricates an empty graph for a missing project.
- `verification.mode` names the basis for the freshness claim: `inventory_hash`
  (content-addressed), `watcher_hint` (an advisory hint, not a proof) or `none`.

An immutable historical generation does **not** imply live source freshness. A pinned query
answers from exactly that generation and reports its freshness honestly.

## Capability and authorization

The gateway authorizes before it resolves a path. Every call carries a bearer token with a
declared audience and capability set:

| Term | Value |
| --- | --- |
| MCP audience | `axiom-mcp` |
| graphd control audience | `axiom-graphd-control` |
| Capabilities | `read`, `reconcile`, `checkpoint` |
| Mutation capabilities | `reconcile`, `checkpoint` |

Three statements are kept apart rather than collapsed:

- binding to loopback is **not** authentication, so `Host` and `Origin` are validated even
  on a `127.0.0.1` bind;
- holding a token is **not** holding a capability, so `graph_query` with a read token is
  allowed and `graph_reconcile` with the same token is refused with `FORBIDDEN`;
- the graphd control audience is **not** the MCP audience, so presenting the daemon control
  credential to `/mcp` is refused instead of being used to elevate the caller.

A token is scoped to solutions and projects. A call whose `solution_id` or `project_id` is
outside the token's scope is refused with `FORBIDDEN` - the gateway does not fall back to a
broader scope, and it does not reveal whether the out-of-scope solution exists. See
[security-host-origin-auth.md](../security-host-origin-auth.md) for the full policy.

## Error surfaces

MCP has a protocol layer and a tool layer, and a failure belongs on exactly one of them.
The surface is decided by the canonical code, not by the call site.

| Surface | When | Rendered as |
| --- | --- | --- |
| `protocol` | The request never became a meaningful call: malformed input, an incompatible input, missing or insufficient authentication, an unsupported operation. | A JSON-RPC error object. The JSON-RPC code stays generic; the canonical Axiom code travels in `error.data.code`. |
| `tool_result` | The call ran and failed for a domain reason: the snapshot expired, the project is missing, the daemon is unavailable. | A tool result with `isError: true`, one human-readable `content` entry, and `structuredContent.error`. |

Protocol-surface codes: `VALIDATION_ERROR`, `INCOMPATIBLE_INPUT`, `UNAUTHENTICATED`,
`FORBIDDEN`, `UNSUPPORTED_OPERATION`. Every other canonical code is a tool-result code.

### The error envelope

Both surfaces carry the same body,
`{code, message, retryable, details, request_id}`, where `request_id` is present when the
error carries one. `retryable` is not hand-set per call site: it comes from the canonical
retryable set `RATE_LIMITED`, `NOT_READY`, `DAEMON_UNAVAILABLE`, `PROJECT_UNAVAILABLE`,
`SNAPSHOT_UNAVAILABLE`, so a host can decide whether to retry without parsing prose.

A protocol-surface failure renders as a JSON-RPC error whose `data` is the envelope above:

```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "error": {
    "code": -32602,
    "message": "max_nodes must be at most 500",
    "data": {
      "code": "VALIDATION_ERROR",
      "message": "max_nodes must be at most 500",
      "retryable": false,
      "details": {}
    }
  }
}
```

A tool-result failure renders as a tool result rather than a transport fault:

```json
{
  "content": [{ "type": "text", "text": "SNAPSHOT_UNAVAILABLE: no readable generation is currently published" }],
  "structuredContent": {
    "error": {
      "code": "SNAPSHOT_UNAVAILABLE",
      "message": "no readable generation is currently published",
      "retryable": true,
      "details": {}
    }
  },
  "isError": true
}
```

### Canonical codes

Sixteen codes are accepted; a code outside this set raises rather than rendering, because a
free-form code is how two components start disagreeing about what happened.

| Code | Default surface | Retryable | HTTP |
| --- | --- | --- | --- |
| `VALIDATION_ERROR` | protocol | no | 400 |
| `UNAUTHENTICATED` | protocol | no | 401 |
| `FORBIDDEN` | protocol | no | 403 |
| `UNSUPPORTED_OPERATION` | protocol | no | 500 (unnamed by the control contract) |
| `INCOMPATIBLE_INPUT` | protocol | no | 422 |
| `NOT_FOUND` | tool_result | no | 404 |
| `CONFLICT` | tool_result | no | 409 |
| `LIMIT_EXCEEDED` | tool_result | no | 500 (unnamed) |
| `RATE_LIMITED` | tool_result | yes | 429 |
| `NOT_READY` | tool_result | yes | 503 |
| `DAEMON_UNAVAILABLE` | tool_result | yes | 503 |
| `PROJECT_UNAVAILABLE` | tool_result | yes | 503 |
| `SNAPSHOT_UNAVAILABLE` | tool_result | yes | 503 |
| `SNAPSHOT_EXPIRED` | tool_result | no | 410 |
| `SNAPSHOT_CORRUPT` | tool_result | no | 500 (unnamed) |
| `INTERNAL_ERROR` | tool_result | no | 500 |

HTTP status follows `contracts/control-api-v1.md` for the codes that contract names. A code
the contract does not name keeps its canonical surface and reports `500` rather than
acquiring an invented status.

The JSON-RPC code a protocol error maps to:
`VALIDATION_ERROR` and `INCOMPATIBLE_INPUT` map to `-32602`; `UNSUPPORTED_OPERATION` and
`NOT_FOUND` map to `-32601`; `UNAUTHENTICATED` and `FORBIDDEN` map to `-32600`;
`INTERNAL_ERROR` maps to `-32603`; the parse and invalid-request codes `-32700` and
`-32600` complete the standard JSON-RPC set.

### Redaction and bounds

Nothing prohibited reaches either surface. `AxiomError` redacts its message and detail tree
**on construction**, following `contracts/redaction-policy.md`: source secrets and absolute
local paths are replaced with `<redacted:{category}>`, an absolute path inside the
configured repository root becomes a repository-relative path, and the detail tree is
bounded to depth `6`, `64` items and `2048` characters per string. Redaction runs before the
bound, so truncation can never expose a fragment of a value the policy removes. A stack
trace never crosses the boundary: only the exception *type* is recorded. See
[errors-and-redaction.md](../errors-and-redaction.md).

## Sample queries

Both samples are **contract examples**: they are what a conformant client sends, and at
this revision no server answers them yet. Neither sample names a file. `graph_query` has no
`path`, `file` or `root` input - a caller supplies a logical `solution_id` and the gateway
resolves it through the trusted local registry, so a client cannot read an arbitrary file
by naming one. See [snapshot-registry.md](../snapshot-registry.md).

A search within one project:

```json
{
  "solution_id": "alpha",
  "operation": "search",
  "query": "apply discount",
  "project_id": "billing",
  "max_nodes": 25,
  "projection": ["identity", "source_locations"]
}
```

A pinned context query with explicit budgets:

```json
{
  "solution_id": "alpha",
  "operation": "context",
  "target": "auth:AuthService.login",
  "project_ids": ["auth-api", "billing"],
  "depth": 2,
  "max_nodes": 50,
  "max_edges": 100,
  "max_bytes": 8192,
  "consistency": "pinned",
  "catalog_generation_id": "7319237fdf1ce4ff27ea377c8bc3ae8cf1f115cffc71de369a373e1870ba9d1a",
  "direction": "both"
}
```

The pinned sample pins one exact catalog generation, so the answer is reproducible and does
not change while the query runs. A pinned query that names a generation which has been
collected returns `SNAPSHOT_EXPIRED` rather than continuing against current data.

## Contract versioning

`query-request` and `query-response` are versioned together at `schema_version` `1`, and
the enums in this page are read from the schemas rather than restated independently. A
request field the schema does not list is rejected, so an additive field is a schema
version change and not a silent extension. If an old generation has been collected, the
gateway returns `SNAPSHOT_EXPIRED`; it does not answer from a different generation under
the old identity.

## Unverified / not yet available

- **Tool registration and the SDK mount.** `src/axiom_mcp/tools/` exists and
  `graph_status` and `graph_query` are implemented, but the handlers are not yet wired into
  the SDK's tool registration, so a client that lists tools still sees an empty catalog.
  Registration is part of the remaining C-030..C-035 work.
- **`graph_version`, `graph_reconcile`, `graph_job`, `graph_verify`.** Each is specified and
  unobservable at this revision; only `graph_status` and `graph_query` are real.
- **The `changes` diff detail.** `graph_query` carries head-side added and modified facts;
  the removed facts and the engine's per-kind totals are not part of the envelope. The
  bundled `demo-solution` declares no schema major, so `changes` legitimately answers
  `unknown_schema` there and a comparable diff is only reachable for a generation that
  declares one.
- **Freshness, coverage and verification computation.** The fields are specified; the
  component that computes them (C-027) is not implemented.
- **Cursors and byte-budget packing.** Specified in C-026 and C-025; not implemented, so
  `next_cursor`, `truncated` and the budget behaviour are contract-only.
- **The daemon control plane.** `graph_reconcile`, `graph_job` and `graph_verify` delegate
  to a graphd control API that is not present in this repository; their delegation and
  timeout behaviour (C-034) is unverified.
- **Readiness inputs.** `/readyz` reports the query and control planes; at this revision the
  default reports both unavailable rather than claiming readiness the gateway has not
  earned.

Verified at this revision: the transports, the error model, the capability map and the
`graph_status` and `graph_query` handlers are real and covered by tests, exercised against
the shipped
`demo-solution` bundle, the real registry and the real native guard. No tool call has been
observed through a running gateway, because tool registration is not wired yet.