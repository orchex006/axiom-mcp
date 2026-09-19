"""Shared builders for the C-028..C-036 tool-surface tests.

The tools are tested against the real thing wherever the real thing exists: the shipped
``demo-solution`` bundle, the real :class:`~axiom_mcp.guard.engine.SolutionGuard` on this
filesystem, the real registry validator, and the real query engine. A fake is used only where the
component under test is deliberately a protocol boundary - the graphd control plane, which is not
in this repository - and each fake records what it was asked so a test can assert the delegation
shape instead of trusting it.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axiom_mcp import security
from axiom_mcp.read_session import ReadLimits
from axiom_mcp.registry import CHECKPOINT_LANE, LIVE_LANE, SnapshotRegistry, load_registry
from axiom_mcp.tools.context import (
    GuardedSnapshotSource,
    ToolContext,
    ToolPrincipal,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SOLUTION = REPO_ROOT / "tests" / "fixtures" / "solution" / "demo-solution"

SOLUTION = "demo-solution"
AUTH_API = "auth-api"
WEB_APP = "web-app"
LANE = "checkpoint"
INSTANCE = "local-instance"

AUTH_API_GENERATION = "7319237fdf1ce4ff27ea377c8bc3ae8cf1f115cffc71de369a373e1870ba9d1a"
WEB_APP_GENERATION = "f570a6d6c7ae1bcffcec7a9c8cb3248dbadab432a30aec09a3223c5c5784c73f"
AUTH_API_FINGERPRINT = "3f0a6b7c1d2e4f5081927a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f"

ALL_CAPABILITIES = frozenset(
    {security.CAPABILITY_READ, security.CAPABILITY_RECONCILE, security.CAPABILITY_CHECKPOINT}
)


def install_bundle(tmp_path: Path) -> Path:
    """Place the shipped bundle where the registry contract says a repo keeps it.

    The bundle is copied byte for byte to ``<repo-root>/.axiom/graph/demo-solution``, so the
    tools resolve it through the same ``P = <repo-root>/.axiom/graph/<solution>/<project>``
    contract a real registration uses. Nothing is rewritten, so the vendored manifests still
    hash to their directory names and a fixture edit fails loudly.

    The vendored bundle is the Git-tracked ``checkpoint`` lane. ``docs/guides/snapshots.md``
    says the ``live`` lane is the working set a query answers from and that a snapshot-only
    gateway reads a checkpoint exactly like a live lane, so every ``checkpoint`` lane is also
    published as a ``live`` lane with the same bytes. The generation directory therefore still
    hashes to its own manifest, and the default-lane behaviour is exercised against real bytes.
    """
    repo = tmp_path / "registered-repo"
    graph = repo / ".axiom" / "graph" / SOLUTION
    graph.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE_SOLUTION, graph)
    for checkpoint in sorted(graph.rglob(CHECKPOINT_LANE)):
        if checkpoint.is_dir() and not (checkpoint.parent / LIVE_LANE).exists():
            shutil.copytree(checkpoint, checkpoint.parent / LIVE_LANE)
    return repo


def write_registry(
    home: Path, repo: Path, *, projects: Sequence[str] = (AUTH_API, WEB_APP)
) -> Path:
    """Write a valid machine-local registry binding ``SOLUTION`` to ``repo``."""
    config = home / "config"
    config.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": 1,
        "axiom_home": str(home),
        "instances": [{"instance_id": INSTANCE}],
        "solutions": [
            {
                "solution_id": SOLUTION,
                "instance_id": INSTANCE,
                "catalog_host_repo": "primary",
                "repositories": [
                    {
                        "repo_id": "primary",
                        "repo_root": str(repo),
                        "projects": [{"project_id": pid} for pid in projects],
                    }
                ],
            }
        ],
    }
    path = config / "registry.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def load_registry_for(home: Path, repo: Path) -> SnapshotRegistry:
    write_registry(home, repo)
    return load_registry(
        home / "config" / "registry.json",
        env={"AXIOM_HOME": str(home)},
        platform="windows",
    )


def guard_dir(home: Path) -> Path:
    return home / "instances" / INSTANCE / "solution.guard"


def principal(
    *,
    capabilities: frozenset[str] = ALL_CAPABILITIES,
    solution_ids: frozenset[str] = frozenset({SOLUTION}),
    project_ids: frozenset[str] | None = None,
    token_id: str = "token-1",
) -> ToolPrincipal:
    return ToolPrincipal(
        token_id=token_id,
        capabilities=capabilities,
        solution_ids=solution_ids,
        project_ids=project_ids,
    )


def context_for(
    tmp_path: Path,
    *,
    caps: frozenset[str] = ALL_CAPABILITIES,
    solution_ids: frozenset[str] = frozenset({SOLUTION}),
    project_ids: frozenset[str] | None = None,
    control: Any | None = None,
    limits: ReadLimits | None = None,
) -> ToolContext:
    """Build a context over a real copied bundle and the real native guard."""
    repo = install_bundle(tmp_path)
    home = tmp_path / "axiom-home"
    registry = load_registry_for(home, repo)
    return ToolContext(
        registry=registry,
        principal=principal(capabilities=caps, solution_ids=solution_ids, project_ids=project_ids),
        source=GuardedSnapshotSource(guard_dir(home), limits=limits),
        control=control,
    )


@dataclass
class RecordedCall:
    """One control-plane call, recorded so a test can assert the delegation shape."""

    operation: str
    payload: Mapping[str, Any]


@dataclass
class FakeControl:
    """A recording stand-in for the graphd control plane.

    It also fails on demand, which is how the degraded-reader and bounded-retry behaviour is
    exercised without a daemon. ``touches_filesystem`` exists so a test can prove the delegation
    tools performed no file access of their own: any handler that opened a path through this fake
    would have to go through it, and it refuses.
    """

    calls: list[RecordedCall] = field(default_factory=list)
    statuses: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    jobs: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    reconcile_result: Mapping[str, Any] = field(
        default_factory=lambda: {"job_id": "job-0001", "state": "queued"}
    )
    verify_result: Mapping[str, Any] = field(
        default_factory=lambda: {"job_id": "verify-0001", "state": "queued"}
    )
    failures: dict[str, BaseException] = field(default_factory=dict)

    def _record(self, operation: str, payload: Mapping[str, Any]) -> None:
        self.calls.append(RecordedCall(operation=operation, payload=dict(payload)))
        problem = self.failures.get(operation)
        if problem is not None:
            raise problem

    def status(self, solution_id: str) -> Any:
        self._record("status", {"solution_id": solution_id})
        return self.statuses.get(solution_id, {"available": False, "reason": "daemon_offline"})

    def reconcile(self, request: Mapping[str, Any]) -> Any:
        self._record("reconcile", request)
        return dict(self.reconcile_result)

    def job(self, job_id: str, *, action: str, wait_ms: int = 0) -> Any:
        self._record("job", {"job_id": job_id, "action": action, "wait_ms": wait_ms})
        return dict(self.jobs.get(job_id, {"job_id": job_id, "state": "unknown"}))

    def verify(self, request: Mapping[str, Any]) -> Any:
        self._record("verify", request)
        return dict(self.verify_result)
