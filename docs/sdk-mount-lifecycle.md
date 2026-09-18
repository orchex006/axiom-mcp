"""How axiom-mcp places the official MCP SDK inside FastAPI.

Owner: `axiom-mcp`. Contract path: `/mcp` (Streamable HTTP). Verified against
`mcp==1.28.1`, the SDK pin recorded in `pyproject.toml` and `axiom_mcp.version`.

## The problem the spike found

The SDK exposes Streamable HTTP as a Starlette sub-application:

```python
subapp = server.streamable_http_app()
app.mount("/mcp", subapp)          # serves HTTP, but is not a working server
```

Starlette runs the lifespan of the **top-level** application only. `Mount`
composes routing, not startup or shutdown, so the SDK session manager is never
entered. The route answers, and then the first protocol request fails with
`Task group is not initialized`. The recorded evidence is
`plain_mount_without_composed_lifespan.status = 500` in the C-002 artifact: a
mount that looks correct and cannot answer `initialize`.

## What this repository does instead

`axiom_mcp.sdk_compat.mount_streamable_http` performs two explicit steps.

1. **Compose the lifespan.** The parent `router.lifespan_context` is wrapped so
   that the SDK sub-application's lifespan is entered inside the parent's. The
   session manager therefore starts once when the application starts and stops
   once when it stops. A `LifecycleRecorder` counts both transitions so the
   evidence asserts `started_exactly_once` instead of asserting intent.
2. **Re-parent the SDK's own handler.** The handler object is read out of the
   SDK's route table (`streamable_http_app()`). It is not re-implemented: the
   same ASGI callable is registered as a `Route` at `/mcp`.

Re-parenting exists because of the path semantics of `Mount`. `Mount("/mcp")`
compiles to the regex `^/mcp/(?P<path>.*)$`, so:

| approach | effective MCP URL | client-visible behaviour |
|---|---|---|
| `mount("/mcp", subapp)` with the default SDK path `/mcp` | `/mcp/mcp` | wrong contract URL |
| `mount("/mcp", subapp)` with SDK path `/` | `/mcp/` | `/mcp` returns 307 |
| **`Route("/mcp", endpoint=handler)` as done here** | **`/mcp`** | **exact match, no redirect** |

The third option is also the only one that does not mount at `/`, which would
shadow every route registered after it - unacceptable in an application that
still has to add health, readiness and tool routes.

## Contract properties

- `MCP_ENDPOINT_PATH` is `/mcp`. The mount path must match
  `^/[A-Za-z0-9._~-]+$`: one segment directly under the root, no trailing
  slash. A nested path is rejected rather than silently creating a second
  endpoint.
- `/mcp/` is not an endpoint. It returns a slash-normalizing `307` back to
  `/mcp`, which is recorded in the evidence rather than assumed.
- Registering MCP twice on one application raises `mcp_already_mounted`. A
  second session manager would start a second set of resources that nothing
  stops.
- Mounting must happen before the application starts, because the lifespan has
  to be installed on the router first. `open_lifespan` runs the composed
  lifespan for tests and for embedding.

## What this module deliberately does not own

- **Host, Origin and token policy.** `build_transport_security` requires both
  allowlists and refuses to invent one; the SDK's own loopback default stays in
  place when none is given. Deciding the policy is `axiom_mcp.security`'s job.
  The mount does not widen what it was handed: a disallowed `Origin` is still
  rejected through the mounted route.
- **The tool catalog.** The gateway's tools are registered on the `FastMCP`
  instance by the query layer; this module only places the server.
- **Protocol revision choice.** The negotiated revision is read back from a
  real `initialize` and checked against what the installed SDK advertises. No
  revision is hardcoded here; `axiom_mcp.version.MCP_PROTOCOL_MINIMUM` is the
  floor the pin was verified against.

## Evidence

- `tests/test_sdk_mount.py` - 16 test functions (19 collected cases): contract path
  without redirect, single
  start/stop, negotiated protocol, `tools/list` and `tools/call`, sibling route
  isolation, the plain-mount failure mode, double-mount refusal, invalid mount
  paths, empty/partial allowlists, unbalanced recorder transitions, non-unique
  route extraction, non-Starlette sub-application, disallowed `Origin`.
- `release/mcp_mount_spike.py` - records the same facts as JSON against the
  installed SDK for the task evidence.

## Limitations

Verified on this host only (Python 3.13.14, win32, `mcp==1.28.1`). The
in-process ASGI client opens no socket, so binding, TLS, `uvicorn` workers and
real network client behaviour are not covered here; loopback binding and worker
count belong to C-004 and C-005. The SDK's public surface used here
(`streamable_http_app`, `session_manager`, `Route.endpoint`,
`streamable_http_client`) is pinned by `axiom_mcp.version.SDK_SURFACE` or
covered by tests, so a future SDK that renames them fails a test rather than
producing a silent regression.
