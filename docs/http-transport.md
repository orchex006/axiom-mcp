# Streamable HTTP transport

Owner: `axiom-mcp`. Contract: `repo-seeds/axiom-mcp/docs/17-FASTAPI-MCP.md` sections 2, 6 and 9, at the pinned
`axiom-specs` revision. This document records implementation, not policy.

## Surfaces

| Path | Purpose | Contract |
|---|---|---|
| `/mcp` | The one MCP Streamable HTTP endpoint | real MCP transport, not REST |
| `/healthz` | Minimal process health | deliberately **no** readiness claim |
| `/readyz` | Query and control availability, reported separately | `503` with `NOT_READY` while a plane is down |

`build_gateway` composes these into one FastAPI application. It registers `/mcp` through
`axiom_mcp.sdk_compat.mount_streamable_http`, so the endpoint path, the lifespan composition and the handler identity are
the ones C-002 verified; this module adds only the two probes. Nothing is registered at `/`, so a root mount cannot
shadow a later route.

## Why streaming is observed, not assumed

The conformance question is not "does `/mcp` exist" but "does `/mcp` behave as the Streamable HTTP transport". The
pinned SDK answers that on the wire, and `observe_streamable_http` records it directly:

```
POST /mcp   Accept: application/json, text/event-stream
-> 200  content-type: text/event-stream
        mcp-session-id: <issued>
        event: message
        data: {"jsonrpc":"2.0","id":1,"result":{...}}
```

Three facts are asserted rather than inferred: the response is an **event stream** (not a single JSON document), a
**session id** is issued, and the frames parse back into the JSON-RPC messages the caller sent requests for. `GET`
without a session is refused with HTTP 400, and a client that does not accept `text/event-stream` is refused with HTTP
406 - the transport never silently downgrades to plain JSON.

`parse_sse_events` reads only the `event` and `data` fields and ignores comment/keep-alive lines, so a keep-alive cannot
be mistaken for a protocol message.

## A REST-only route is not conformance

`release/http_transport_spike.py` records the failure this task exists to prevent. A plain FastAPI `POST /mcp` route
that returns a hand-written body shaped exactly like an `initialize` result:

- answers HTTP 200 with `content-type: application/json`;
- issues **no** `mcp-session-id` and **no** event stream;
- never completes an MCP `initialize` for a real client, while the same client against the real gateway completes
  immediately.

The test proves both directions in one place: the REST route must fail and the real gateway must succeed, so the
negative result is attributable to the REST shape and not to a client that cannot work here. The bounded client attempt
runs in its own daemon thread: a bound that cancels an anyio task group in place raises a cancel-scope error instead of
reporting non-completion, and the harness must not disguise its own limitation as a product failure.

## Serving

`HttpTransportSettings` defaults to `127.0.0.1:8765`. A non-loopback bind requires an explicit
`allow_non_loopback_bind=True` acknowledgement and is otherwise refused with `non_loopback_bind_refused`: without an
authenticated surface (C-005), a remote bind would expose an unauthenticated gateway. `uvicorn_config` disables the
access log and lowers the log level, because on this component the diagnostic channel must not be the protocol channel.

`python -m axiom_mcp.http` requires `--allow-host` and `--allow-origin` explicitly; there is no permissive default. The
allowlists are passed to the SDK unchanged and never widened here.

## Ownership boundaries

This module owns **transport assembly and wire observation**. It does not own:

- the tool catalog or query answers - the query gateway;
- Host/Origin allowlist policy and token scope - `axiom_mcp.security` (C-005);
- readiness inputs - C-007 and the query/control planes supply the probes; the default here reports both planes
  unavailable rather than claiming readiness the gateway has not earned;
- the protocol revision - the SDK negotiates it and the spike reads it back (`2025-11-25` on the pin).

## Verification

`tests/test_http_transport.py` is the targeted regression test. `python -m pytest tests -q` is the required repository
check; `python -m ruff check .` and `python -m ruff format --check .` cover lint and formatting.
