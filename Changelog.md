# Changelog — axiom-mcp

## Unreleased

- Add `AGENTS.md` and a verified `spec.lock.json`: the pin records the immutable `axiom-specs` revision and seven contract digests, and `tools/spec-lock-check.py` accepts it with exit code 0. Implementation tasks are no longer blocked by the missing governance pin.

- Add `Development.md`: component development contract covering repository identity, preflight gate, required checks, evidence and completion requirements, branch/merge/release policy, and the rule that verified work finished in a worktree must reach the owner's primary checkout.

### Notes

- The repository still contains no implementation. The canonical pin now exists and is verified, so implementation tasks are unblocked; the first one must add the required check `python -m pytest tests -q`.
- `AGENTS.md` is now present and composes with the workspace `AGENTS.md` and this `Development.md`.
