"""The shared context every tool handler runs inside.

A tool handler is deliberately not given free rein over the machine. It receives a
:class:`ToolContext` that carries three things and nothing more:

* the authenticated :class:`ToolPrincipal`, so a handler re-authorizes the *incoming* caller
  instead of inheriting the gateway's own authority (``17-FASTAPI-MCP.md`` section 8);
* the machine-local :class:`~axiom_mcp.registry.SnapshotRegistry`, the only thing that may turn
  a logical ``solution_id``/``project_id`` into a file path;
* a :class:`SnapshotSource` for reading pinned generations, and an optional control plane for
  the delegation tools.

Two rules are enforced here rather than left to each handler:

* **An unauthorized solution is invisible.** A caller that names a solution its token is not
  scoped to gets ``NOT_FOUND``, the same answer an unregistered solution gets. A ``FORBIDDEN``
  would confirm that the solution exists, which is the enumeration oracle the spec's
  "unauthorized solutions are invisible" rule exists to close.
* **The request is a closed object.** ``additionalProperties: false`` is the request schema's
  rule, so an unexpected key is a ``VALIDATION_ERROR`` instead of an ignored input. That is what
  makes "there is no arbitrary SQL field" a property of the parser rather than a promise.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from axiom_mcp import security
from axiom_mcp.errors import AxiomError
from axiom_mcp.guard.engine import SolutionGuard
from axiom_mcp.query.model import identifier_text
from axiom_mcp.read_session import LoadedSnapshot, ReadLimits, ReadSession
from axiom_mcp.registry import RegistryError, SnapshotLocation, SnapshotRegistry

__all__ = [
    "ControlPlane",
    "GuardedSnapshotSource",
    "SnapshotSource",
    "ToolContext",
    "ToolPrincipal",
    "closed_arguments",
    "identifier_argument",
    "optional_bool",
    "optional_int",
    "project_scope",
    "require_text",
]


def closed_arguments(arguments: Mapping[str, Any], allowed: Iterable[str]) -> None:
    """Refuse any key the handler does not declare, naming the offending keys.

    The message names only the *keys*, never their values, so refusing a request
    can never echo a value back to the caller that sent it.
    """
    permitted = set(allowed)
    unexpected = sorted(set(arguments) - permitted)
    if unexpected:
        raise AxiomError(
            "VALIDATION_ERROR",
            "the request carries fields this tool does not accept",
            details={"unexpected_fields": unexpected},
        )


def require_text(arguments: Mapping[str, Any], key: str) -> str:
    """Return a required non-empty string argument, or refuse the request."""
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AxiomError(
            "VALIDATION_ERROR",
            f"{key} is required and must be a non-empty string",
            details={"field": key},
        )
    return value.strip()


def identifier_argument(arguments: Mapping[str, Any], key: str) -> str:
    """Return a required identifier argument, validated by the shared identifier rule."""
    raw = require_text(arguments, key)
    try:
        return identifier_text(raw, what=key)
    except ValueError as exc:
        raise AxiomError("VALIDATION_ERROR", str(exc), details={"field": key}) from exc


def optional_int(
    arguments: Mapping[str, Any],
    key: str,
    *,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    """Return an optional bounded integer argument, or refuse the request."""
    value = arguments.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise AxiomError(
            "VALIDATION_ERROR",
            f"{key} must be an integer",
            details={"field": key},
        )
    if minimum is not None and value < minimum:
        raise AxiomError(
            "LIMIT_EXCEEDED",
            f"{key} must be at least {minimum}",
            details={"field": key, "minimum": minimum},
        )
    if maximum is not None and value > maximum:
        raise AxiomError(
            "LIMIT_EXCEEDED",
            f"{key} must be at most {maximum}",
            details={"field": key, "maximum": maximum},
        )
    return value


def optional_bool(arguments: Mapping[str, Any], key: str, *, default: bool = False) -> bool:
    """Return an optional boolean argument, or refuse the request."""
    value = arguments.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise AxiomError("VALIDATION_ERROR", f"{key} must be a boolean", details={"field": key})
    return value


def project_scope(arguments: Mapping[str, Any]) -> tuple[str, ...] | None:
    """Return the requested project scope, refusing both selectors at once.

    ``None`` means "no project selector": the caller asked for the whole solution
    the token is authorized for, which is a different request from naming projects.
    """
    single = arguments.get("project_id")
    many = arguments.get("project_ids")
    if single is not None and many is not None:
        raise AxiomError(
            "VALIDATION_ERROR",
            "project_id and project_ids must not be combined",
            details={"fields": ["project_id", "project_ids"]},
        )
    if single is not None:
        return (identifier_argument(arguments, "project_id"),)
    if many is not None:
        if isinstance(many, (str, bytes)) or not isinstance(many, Sequence):
            raise AxiomError(
                "VALIDATION_ERROR",
                "project_ids must be a list of project identifiers",
                details={"field": "project_ids"},
            )
        seen: list[str] = []
        for item in many:
            if not isinstance(item, str) or not item.strip():
                raise AxiomError(
                    "VALIDATION_ERROR",
                    "project_ids entries must be non-empty strings",
                    details={"field": "project_ids"},
                )
            try:
                resolved = identifier_text(item, what="project_id")
            except ValueError as exc:
                raise AxiomError(
                    "VALIDATION_ERROR", str(exc), details={"field": "project_ids"}
                ) from exc
            if resolved not in seen:
                seen.append(resolved)
        if not seen:
            raise AxiomError(
                "VALIDATION_ERROR",
                "project_ids must name at least one project",
                details={"field": "project_ids"},
            )
        return tuple(seen)
    return None


@dataclass(frozen=True)
class ToolPrincipal:
    """The authenticated caller, re-authorized inside each handler.

    This mirrors :class:`~axiom_mcp.security.ScopedToken` but is a plain value
    object: a handler needs the *decision*, not the credential machinery, and
    keeping the token value out of the handler is what makes it impossible for a
    tool result to leak it.
    """

    token_id: str
    capabilities: frozenset[str]
    solution_ids: frozenset[str]
    project_ids: frozenset[str] | None = None

    @classmethod
    def of(cls, token: security.ScopedToken) -> ToolPrincipal:
        return cls(
            token_id=token.token_id,
            capabilities=frozenset(token.capabilities),
            solution_ids=frozenset(token.solution_ids),
            project_ids=None if token.project_ids is None else frozenset(token.project_ids),
        )

    def visible(self, solution_id: str) -> bool:
        """True only when this principal may be told the solution exists at all."""
        return solution_id in self.solution_ids

    def require(
        self,
        capability: str,
        *,
        solution_id: str | None = None,
        project_ids: Sequence[str] | None = None,
    ) -> None:
        """Refuse the call unless capability and scope both admit it."""
        if capability not in self.capabilities:
            raise AxiomError(
                "FORBIDDEN",
                "the caller is not permitted this capability",
                details={"required_capability": capability},
            )
        if solution_id is not None and not self.visible(solution_id):
            # Invisible, not forbidden: a distinct answer would confirm existence.
            raise AxiomError("NOT_FOUND", "no such solution is visible to this caller")
        if solution_id is not None and self.project_ids is not None and project_ids:
            outside = sorted(set(project_ids) - self.project_ids)
            if outside:
                raise AxiomError(
                    "FORBIDDEN",
                    "the caller is not scoped to every requested project",
                    details={"project_scope_exceeded": outside},
                )


class SnapshotSource(Protocol):
    """A bounded source of verified, pinned, in-memory generations."""

    def load(self, location: SnapshotLocation) -> LoadedSnapshot:
        """Copy and verify the generation the lane currently points at."""
        ...


@dataclass(frozen=True)
class GuardedSnapshotSource:
    """The production source: a real read session over the instance's native guard.

    The guard directory is the one ``contracts/native-reader-writer-guards.md``
    section 1 fixes, and the directory is created here because a reader that
    cannot create its own lock files would have to be run after a publisher - a
    dependency the read protocol does not have.
    """

    guard_dir: Path
    timeout_ms: int = 5000
    limits: ReadLimits | None = None

    def load(self, location: SnapshotLocation) -> LoadedSnapshot:
        guard = SolutionGuard(self.guard_dir, timeout_ms=self.timeout_ms)
        guard.ensure_directory()
        try:
            session = ReadSession(guard, location, limits=self.limits)
            return session.load()
        finally:
            guard.close()


class ControlPlane(Protocol):
    """The control-plane surface the delegation tools use.

    Declared as a protocol so the tool layer cannot reach into a concrete client's
    internals - and so a test can prove the delegation tools make no filesystem
    access of their own by handing them a recorder that would notice.
    """

    def status(self, solution_id: str) -> Any:
        """Return daemon availability for one solution."""
        ...

    def reconcile(self, request: Mapping[str, Any]) -> Any:
        """Enqueue a reconcile and return its job handle."""
        ...

    def job(self, job_id: str, *, action: str, wait_ms: int = 0) -> Any:
        """Read or cancel one job."""
        ...

    def verify(self, request: Mapping[str, Any]) -> Any:
        """Submit one bounded verification request."""
        ...


@dataclass(frozen=True)
class ToolContext:
    """Everything a handler may use, and nothing else."""

    registry: SnapshotRegistry
    principal: ToolPrincipal
    source: SnapshotSource
    control: ControlPlane | None = None
    clock: Callable[[], float] = field(default=time.monotonic)

    def solution_projects(self, solution_id: str) -> tuple[str, ...]:
        """Every project of a visible solution, in registry order."""
        binding = self.registry.solution(solution_id)
        ordered: list[str] = []
        for repo in binding.repositories:
            for project_id in repo.project_ids:
                if project_id not in ordered:
                    ordered.append(project_id)
        return tuple(ordered)

    def resolve_projects(
        self, solution_id: str, requested: Sequence[str] | None
    ) -> tuple[str, ...]:
        """Resolve the requested scope, refusing an unknown or unauthorized project.

        An unknown project and a project outside the token's scope both answer
        ``NOT_FOUND`` for the same reason an unauthorized solution does.
        """
        available = self.solution_projects(solution_id)
        if requested is None:
            if self.principal.project_ids is not None:
                return tuple(p for p in available if p in self.principal.project_ids)
            return available
        for project_id in requested:
            if project_id not in available:
                raise AxiomError("NOT_FOUND", "no such project is visible to this caller")
            if (
                self.principal.project_ids is not None
                and project_id not in self.principal.project_ids
            ):
                raise AxiomError("NOT_FOUND", "no such project is visible to this caller")
        return tuple(requested)

    def open_solution(self, solution_id: str) -> tuple[SnapshotRegistry, str]:
        """Authorize ``solution_id`` for a read and return the registry and id."""
        self.principal.require(security.CAPABILITY_READ, solution_id=solution_id)
        try:
            self.registry.solution(solution_id)
        except RegistryError as exc:
            raise AxiomError("NOT_FOUND", str(exc)) from exc
        return self.registry, solution_id

    def load_project(self, solution_id: str, project_id: str, lane: str) -> LoadedSnapshot:
        """Load one project's pinned generation through the trusted binding."""
        self.principal.require(
            security.CAPABILITY_READ, solution_id=solution_id, project_ids=(project_id,)
        )
        try:
            location = self.registry.location(solution_id, project_id, lane)
        except RegistryError as exc:
            raise AxiomError("NOT_FOUND", str(exc)) from exc
        try:
            return self.source.load(location)
        except FileNotFoundError as exc:
            raise AxiomError(
                "SNAPSHOT_UNAVAILABLE",
                "the registered lane has no published generation",
                details={"project_id": project_id},
            ) from exc

    def require_control(self) -> ControlPlane:
        """Return the control plane, or refuse with the canonical unavailability code."""
        if self.control is None:
            raise AxiomError(
                "DAEMON_UNAVAILABLE",
                "the graphd control plane is not configured for this gateway",
            )
        return self.control
