# Update check and approved-plan delegation

`axiom-mcp` exposes two of the canonical version commands described in
`docs/20-VERSION-CHECK-UPDATE-RELEASE.md`: `update check` and `update apply --plan`.
Both are implemented in `src/axiom_mcp/update.py` and wired into `src/axiom_mcp/cli.py`.

The module deliberately performs **no** install, download, unpack or environment
mutation. It answers one question and enforces one prohibition:

* what the installed component is, what is available, and on what basis that is known;
* a running process is never pip-upgraded in place.

## `update check`

Every result separates the fields the contract requires: `component`, `installed`,
`available`, `compatible`, `channel`, `schema_range`, `update_policy`,
`source_origin` and `needs_restart`, plus `status` and `reasons`.

The single rule this surface exists to enforce is that an unknown answer is never
rendered as up to date. When no verified metadata source is configured, `available`
stays `null` and the status says why:

| Status | Meaning |
|---|---|
| `unconfigured` | no verified metadata source is configured; nothing was checked |
| `offline` | `--offline` or `AXIOM_MCP_OFFLINE` was set; no network was consulted |
| `not_checked` | metadata was present but named no available version |
| `current` | the available version equals the installed version |
| `available` | a different available version exists and may be applied |
| `blocked` | the answer is unusable: an unlisted origin, an unsupported channel, unreadable metadata, or an available version declared incompatible |

`needs_restart` is true exactly when a change is pending, so a supervisor never has to
infer it from the status.

### Origin trust

An update origin is trusted only when it is the canonical repository
(`https://github.com/orchex006/axiom-mcp`) or when the owner added it to
`AXIOM_MCP_UPDATE_ALLOWED_ORIGINS` (comma separated). Any other origin is reported as
`blocked` with `origin_not_allowlisted:<value>` and is never contacted. There is no
"guess the owner" path: the specification's draft placeholders are refused rather than
resolved by a heuristic.

### Configuration

| Variable | Effect |
|---|---|
| `AXIOM_MCP_UPDATE_CHANNEL` | `stable` (default) or `prerelease` |
| `AXIOM_MCP_UPDATE_ORIGIN` | the origin to check against; must be allowlisted |
| `AXIOM_MCP_UPDATE_ALLOWED_ORIGINS` | extra allowlisted origins, comma separated |
| `AXIOM_MCP_UPDATE_METADATA` | path to a verified metadata document |
| `AXIOM_MCP_OFFLINE` | when set, no network is consulted and nothing is claimed |
| `AXIOM_MCP_UPDATE_AUTO_CHECK` | reported in `update_policy.auto_check` |
| `AXIOM_MCP_UPDATE_AUTO_APPLY` | reported in `update_policy.auto_apply`; default `disabled` |

`AXIOM_MCP_UPDATE_AUTO_APPLY` defaults to `disabled` and `plan_approval_required` is
always true, matching the contract's rule that a major version, a schema migration, a
host configuration rewrite or a trust-root change needs an explicit plan approval.

## `update apply --plan PATH`

`apply` reads one plan document and validates it with the specification's own evaluator,
`tools/update_plan_contract.py` at the pinned revision. The verdict, its reason names
and the plan digest are the specification's, reported verbatim, so this component cannot
quietly disagree with the contract. When the pinned checkout is absent the module raises
`UpdateUnavailable` instead of assuming acceptance: an unverifiable approval is not an
approval.

A plan reaches the external updater only when all of these hold:

1. the canonical evaluator accepts it, including the structural rules and the
   approval binding (an approved digest covers every planner input, so a plan changed
   after approval is rejected as `approval_stale` / `plan_digest_mismatch` rather than
   silently re-planned);
2. `approval.state` is `approved`;
3. `target.component` is `axiom-mcp`;
4. `target.install_root` is an absolute path outside the running interpreter, its
   `site-packages` and this package directory.

The fourth rule is the prohibition. A plan whose install root would land on the running
installation is refused as `in_place_upgrade_prohibited:<root>`; a relative root is
refused as `install_root_not_absolute`. The delegated command itself is guarded too: a
`pip`, `pip3`, `uv`, `easy_install` or `python -m pip` invocation - or this component
invoking itself - is refused as an in-place upgrade.

When the plan is accepted, `apply` reports the exact argument list it would run,
`["axiom", "update", "apply", "--plan", PATH]`, and does not run it. The swap is an
external updater's job after this process stops; that is what keeps a Python package in
a versioned virtual environment instead of overwriting the environment that is active.
A wrapper that does own the updater passes a runner, and the observed exit code is
reported as `delegated_ok` or `delegated_failed:<code>`.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | a check completed, or an approved plan was accepted for delegation |
| `2` | invalid invocation, unusable configuration, a rejected plan, or a refused in-place target |
| `3` | the canonical evaluator or the external updater is unavailable |
| `5` | the plan is not approved |

## Not in this slice

Download, signature and TUF metadata verification, cache and disk preflight, the
transaction journal, the swap itself and rollback belong to the updater and to later
tasks. This module does not implement them, and it never falls back to downloading an
untrusted script to repair itself.
