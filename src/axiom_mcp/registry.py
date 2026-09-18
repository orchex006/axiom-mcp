"""Trusted local solution bindings: the only thing that may produce a file path.

A query is allowed to name a *logical* target - a solution id, a project id, a
lane, a generation id and a generation-relative shard path. It is never allowed
to name a file. This module is the boundary where that rule is enforced: a
logical reference is resolved through the machine-local bindings the owner
registered under ``AXIOM_HOME``, and anything that would let a caller-chosen
string select an arbitrary JSON file on disk is refused before it becomes a path.

Three canonical sources are consumed here rather than re-invented:

* ``SOURCE-OF-TRUST.md`` section 5 fixes the machine-local layout, including
  ``config/registry.json`` and ``instances/<local-instance-id>/solution.guard``,
  and states that absolute bindings live in machine-local state and never in a
  graph snapshot.
* ``docs/12-SNAPSHOT-READ-WRITE-PROTOCOL.md`` section 2 fixes the path contract
  ``P = <repo-root>/.axiom/graph/<solution-id>/<project-id>`` and
  ``C = <catalog-repo>/.axiom/graph/<solution-id>/_catalog``, each with a
  ``live/`` and a ``checkpoint/`` lane holding ``current.json`` and
  ``generations/<hash>/``.
* ``contracts/native-reader-writer-guards.md`` section 1 fixes the guard
  directory identity to exactly ``<AXIOM_HOME>/instances/<workspace-instance-id>/
  solution.guard`` with the two stable lock files ``admission.lock`` and
  ``data.lock``.

The portable-relative rule used for request-side references is the same rule
``contracts/schemas/project-manifest.schema.json`` applies to a manifest file
entry: no absolute prefix, no backslash, no ``..`` segment and no drive letter.
It is applied here to a *reference*, which is what keeps a model-supplied string
from escaping the registered lane.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

AXIOM_HOME_ENV = "AXIOM_HOME"

REGISTRY_SCHEMA_VERSION = 1
REGISTRY_DIRNAME = "config"
REGISTRY_FILENAME = "registry.json"

INSTANCES_DIRNAME = "instances"
GUARD_DIRNAME = "solution.guard"
ADMISSION_ROLE = "admission"
DATA_ROLE = "data"
LOCK_ROLES: tuple[str, ...] = (ADMISSION_ROLE, DATA_ROLE)
LOCK_FILE_NAMES: Mapping[str, str] = {
    ADMISSION_ROLE: "admission.lock",
    DATA_ROLE: "data.lock",
}

GRAPH_OUTPUT_ROOT = ".axiom/graph"
CATALOG_DIRNAME = "_catalog"
LIVE_LANE = "live"
CHECKPOINT_LANE = "checkpoint"
LANES: tuple[str, ...] = (LIVE_LANE, CHECKPOINT_LANE)

CURRENT_POINTER_NAME = "current.json"
GENERATIONS_DIRNAME = "generations"
STAGING_DIRNAME = ".staging"

GENERATION_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")

# The portable relative rule from contracts/schemas/project-manifest.schema.json,
# applied to a request-side reference: not absolute, not a Windows path, no
# backslash, and no ".." segment in any position.
PORTABLE_RELATIVE_RE = re.compile(r"^(?!/)(?!.*\\)(?!.*(?:^|/)\.\.(?:/|$))(?![A-Za-z]:).+$")


class RegistryError(ValueError):
    """A binding, a reference or a registry document the gateway must refuse."""


class RelativeAxiomHome(RegistryError):
    """An explicit ``AXIOM_HOME`` override that is not an absolute path."""


class RegistryUnreadable(RegistryError):
    """A registry document that is missing a required member or malformed."""


class UnsupportedRegistryMajor(RegistryError):
    """A registry document whose ``schema_version`` major is not supported."""


class UnknownBinding(RegistryError):
    """A logical solution or project id that no trusted binding provides."""


class UntrustedPath(RegistryError):
    """A caller-supplied string that would escape the registered lane."""


def _is_absolute(text: str) -> bool:
    """Return True when ``text`` is absolute on either supported path syntax."""
    return PurePosixPath(text).is_absolute() or PureWindowsPath(text).is_absolute()


def _portable_id(value: Any, *, what: str) -> str:
    """Validate a logical id against the canonical id pattern."""
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise RegistryUnreadable(f"{what} is not a portable id: {value!r}")
    return value


def default_axiom_home(env: Mapping[str, str] | None = None, platform: str | None = None) -> Path:
    """Return the default ``AXIOM_HOME`` for the given platform.

    The three defaults are the ones ``SOURCE-OF-TRUST.md`` section 5 fixes, not
    an invention of this module: ``%LOCALAPPDATA%\\Axiom`` on Windows,
    ``$HOME/Library/Application Support/Axiom`` on macOS and
    ``${XDG_STATE_HOME:-$HOME/.local/state}/axiom`` elsewhere.
    """
    environment = dict(os.environ if env is None else env)
    target = sys.platform if platform is None else platform

    def home_dir() -> Path:
        home = environment.get("HOME") or environment.get("USERPROFILE")
        if not home:
            raise RegistryError("no HOME or USERPROFILE to derive AXIOM_HOME from")
        return Path(home)

    if target.startswith("win"):
        local = environment.get("LOCALAPPDATA")
        if not local:
            local = str(home_dir() / "AppData" / "Local")
        return Path(local) / "Axiom"
    if target == "darwin":
        return home_dir() / "Library" / "Application Support" / "Axiom"
    state = environment.get("XDG_STATE_HOME")
    if not state:
        state = str(home_dir() / ".local" / "state")
    return Path(state) / "axiom"


def axiom_home(env: Mapping[str, str] | None = None, platform: str | None = None) -> Path:
    """Resolve ``AXIOM_HOME``; an explicit override must be absolute.

    ``SOURCE-OF-TRUST.md`` section 5 permits an explicit ``AXIOM_HOME`` only as
    an absolute path on a supported local filesystem, so a relative override is
    refused rather than being silently resolved against the working directory.
    """
    environment = dict(os.environ if env is None else env)
    override = environment.get(AXIOM_HOME_ENV)
    if not override:
        return default_axiom_home(env=environment, platform=platform)
    if not _is_absolute(override):
        raise RelativeAxiomHome(f"{AXIOM_HOME_ENV} must be an absolute path, got {override!r}")
    return Path(override)


def registry_path(home: Path | str | None = None) -> Path:
    """Return ``<AXIOM_HOME>/config/registry.json``."""
    base = Path(home) if home is not None else axiom_home()
    return base / REGISTRY_DIRNAME / REGISTRY_FILENAME


def portable_relative(relative: str) -> str:
    """Validate a caller-supplied relative reference and return it unchanged.

    Refused: an empty string, an absolute POSIX path, a drive-qualified or
    otherwise absolute Windows path, any backslash and any ``..`` segment.
    """
    if not isinstance(relative, str) or not PORTABLE_RELATIVE_RE.match(relative):
        raise UntrustedPath(f"reference is not a portable relative path: {relative!r}")
    return relative


def generation_directory_name(generation_id: str) -> str:
    """Validate a generation id and return it as a directory name."""
    if not isinstance(generation_id, str) or not GENERATION_ID_RE.match(generation_id):
        raise UntrustedPath(f"generation id is not a 64 character hex digest: {generation_id!r}")
    return generation_id


def _lane(value: str) -> str:
    if value not in LANES:
        raise RegistryUnreadable(f"unknown lane: {value!r}")
    return value


@dataclass(frozen=True)
class SnapshotLocation:
    """The resolved directory of one lane of one project's graph output.

    ``root`` is ``<repo-root>/.axiom/graph/<solution-id>/<project-id>/<lane>``.
    Callers do not build paths themselves: :meth:`pointer` and
    :meth:`generation_root` are the only ways to name a file below it, and
    :meth:`resolve` refuses a reference that is not portable or that escapes
    ``root`` after symbolic links are followed.
    """

    solution_id: str
    project_id: str
    lane: str
    root: Path

    @property
    def pointer(self) -> Path:
        """The lane's ``current.json`` pointer."""
        return self.root / CURRENT_POINTER_NAME

    @property
    def generations_root(self) -> Path:
        """The lane's ``generations/`` directory."""
        return self.root / GENERATIONS_DIRNAME

    @property
    def staging_root(self) -> Path:
        """The lane's ``.staging/`` directory (never a read source)."""
        return self.root / STAGING_DIRNAME

    def generation_root(self, generation_id: str) -> Path:
        """The immutable ``generations/<generation-id>/`` directory."""
        return self.generations_root / generation_directory_name(generation_id)

    def resolve(self, relative: str) -> Path:
        """Resolve a portable generation-relative reference inside this lane.

        The reference is validated before it is joined, and the joined path is
        re-checked after symbolic links are resolved, so a symlink planted in
        the lane cannot be used to read a file outside it.
        """
        portable_relative(relative)
        root = self.root.resolve()
        candidate = root.joinpath(*PurePosixPath(relative).parts)
        resolved = candidate.resolve()
        if resolved != root and root not in resolved.parents:
            raise UntrustedPath(f"reference {relative!r} resolves outside the registered lane")
        return resolved


@dataclass(frozen=True)
class CatalogLocation:
    """The resolved directory of a solution's catalog lane.

    ``root`` is ``<catalog-repo>/.axiom/graph/<solution-id>/_catalog/<lane>``.
    """

    solution_id: str
    lane: str
    root: Path

    @property
    def pointer(self) -> Path:
        return self.root / CURRENT_POINTER_NAME

    @property
    def generations_root(self) -> Path:
        return self.root / GENERATIONS_DIRNAME

    def generation_root(self, generation_id: str) -> Path:
        return self.generations_root / generation_directory_name(generation_id)

    def resolve(self, relative: str) -> Path:
        portable_relative(relative)
        root = self.root.resolve()
        candidate = root.joinpath(*PurePosixPath(relative).parts)
        resolved = candidate.resolve()
        if resolved != root and root not in resolved.parents:
            raise UntrustedPath(
                f"reference {relative!r} resolves outside the registered catalog lane"
            )
        return resolved


@dataclass(frozen=True)
class ProjectBinding:
    """A trusted binding from a logical project to one local checkout."""

    solution_id: str
    project_id: str
    repo_id: str
    repo_root: Path

    @property
    def graph_root(self) -> Path:
        """``<repo-root>/.axiom/graph/<solution-id>/<project-id>``."""
        return self.repo_root / GRAPH_OUTPUT_ROOT / self.solution_id / self.project_id

    def location(self, lane: str = LIVE_LANE) -> SnapshotLocation:
        return SnapshotLocation(
            solution_id=self.solution_id,
            project_id=self.project_id,
            lane=_lane(lane),
            root=self.graph_root / _lane(lane),
        )


@dataclass(frozen=True)
class RepositoryBinding:
    """One logical repository and the projects it hosts."""

    repo_id: str
    repo_root: Path
    project_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SolutionBinding:
    """One logical solution, its bound instance and its repositories."""

    solution_id: str
    instance_id: str
    catalog_host_repo: str
    repositories: tuple[RepositoryBinding, ...]

    @property
    def repo_ids(self) -> tuple[str, ...]:
        return tuple(repo.repo_id for repo in self.repositories)

    def repository(self, repo_id: str) -> RepositoryBinding:
        for repo in self.repositories:
            if repo.repo_id == repo_id:
                return repo
        raise UnknownBinding(f"solution {self.solution_id!r} has no repository {repo_id!r}")

    @property
    def catalog_repository(self) -> RepositoryBinding:
        return self.repository(self.catalog_host_repo)


@dataclass(frozen=True)
class SnapshotRegistry:
    """The machine-local bindings, and the only source of snapshot file paths."""

    axiom_home: Path
    solutions: tuple[SolutionBinding, ...] = ()

    @property
    def is_empty(self) -> bool:
        """True when no solution is registered, so nothing can be resolved."""
        return not self.solutions

    def solution(self, solution_id: str) -> SolutionBinding:
        for entry in self.solutions:
            if entry.solution_id == solution_id:
                return entry
        raise UnknownBinding(f"no trusted binding for solution {solution_id!r}")

    def project(self, solution_id: str, project_id: str) -> ProjectBinding:
        """Resolve a logical project to its trusted local binding."""
        entry = self.solution(solution_id)
        for repo in entry.repositories:
            if project_id in repo.project_ids:
                return ProjectBinding(
                    solution_id=solution_id,
                    project_id=project_id,
                    repo_id=repo.repo_id,
                    repo_root=repo.repo_root,
                )
        raise UnknownBinding(
            f"no trusted binding for project {project_id!r} in solution {solution_id!r}"
        )

    def location(
        self, solution_id: str, project_id: str, lane: str = LIVE_LANE
    ) -> SnapshotLocation:
        """Resolve a logical project and lane to its snapshot location."""
        return self.project(solution_id, project_id).location(lane)

    def catalog_location(self, solution_id: str, lane: str = LIVE_LANE) -> CatalogLocation:
        """Resolve a logical solution and lane to its catalog location."""
        entry = self.solution(solution_id)
        checked_lane = _lane(lane)
        root = (
            entry.catalog_repository.repo_root
            / GRAPH_OUTPUT_ROOT
            / entry.solution_id
            / CATALOG_DIRNAME
            / checked_lane
        )
        return CatalogLocation(solution_id=solution_id, lane=checked_lane, root=root)

    def instance_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(entry.instance_id for entry in self.solutions))

    def guard_directory(self, instance_id: str) -> Path:
        """The exact guard directory the native ABI fixes for an instance."""
        return self.axiom_home / INSTANCES_DIRNAME / instance_id / GUARD_DIRNAME

    def lock_path(self, instance_id: str, role: str) -> Path:
        """The exact lock file the native ABI fixes for one role."""
        if role not in LOCK_ROLES:
            raise RegistryError(f"unknown guard role: {role!r}")
        return self.guard_directory(instance_id) / LOCK_FILE_NAMES[role]

    def lock_paths(self, instance_id: str) -> dict[str, Path]:
        """Both stable lock files, keyed by role."""
        return {role: self.lock_path(instance_id, role) for role in LOCK_ROLES}


def _mapping(value: Any, *, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RegistryUnreadable(f"{what} must be an object")
    return value


def _sequence(value: Any, *, what: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise RegistryUnreadable(f"{what} must be a list")
    return value


def parse_registry(
    document: Mapping[str, Any], *, home: Path | str | None = None
) -> SnapshotRegistry:
    """Validate a registry document and build a :class:`SnapshotRegistry`.

    Strict on purpose: an unknown major, a duplicate solution, repository or
    project id, a relative ``repo_root`` and a declared guard directory that is
    not the ABI path are all refused, because each one would make the resolution
    of a logical reference ambiguous or untrusted.
    """
    base = Path(home) if home is not None else axiom_home()
    body = _mapping(document, what="registry document")

    declared_home = body.get("axiom_home")
    if declared_home is not None:
        if not isinstance(declared_home, str) or not _is_absolute(declared_home):
            raise RegistryUnreadable("registry axiom_home must be an absolute path")
        if Path(declared_home) != base:
            raise RegistryUnreadable("registry axiom_home does not match the resolved AXIOM_HOME")

    version = body.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise RegistryUnreadable("registry schema_version must be an integer")
    if version != REGISTRY_SCHEMA_VERSION:
        raise UnsupportedRegistryMajor(
            f"unsupported registry schema major: {version} (supported: {REGISTRY_SCHEMA_VERSION})"
        )

    instances: dict[str, Path] = {}
    for entry in _sequence(body.get("instances", []), what="registry instances"):
        item = _mapping(entry, what="registry instance")
        instance_id = _portable_id(item.get("instance_id"), what="instance_id")
        if instance_id in instances:
            raise RegistryUnreadable(f"duplicate instance_id: {instance_id!r}")
        expected = base / INSTANCES_DIRNAME / instance_id / GUARD_DIRNAME
        declared = item.get("guard_directory")
        if declared is not None:
            if not isinstance(declared, str) or not _is_absolute(declared):
                raise RegistryUnreadable(
                    f"guard_directory for {instance_id!r} must be an absolute path"
                )
            if Path(declared) != expected:
                raise RegistryUnreadable(
                    f"guard_directory for {instance_id!r} is not the ABI path "
                    f"{INSTANCES_DIRNAME}/{instance_id}/{GUARD_DIRNAME}"
                )
        instances[instance_id] = expected

    solutions: list[SolutionBinding] = []
    seen_solutions: set[str] = set()
    for entry in _sequence(body.get("solutions", []), what="registry solutions"):
        item = _mapping(entry, what="registry solution")
        solution_id = _portable_id(item.get("solution_id"), what="solution_id")
        if solution_id in seen_solutions:
            raise RegistryUnreadable(f"duplicate solution_id: {solution_id!r}")
        seen_solutions.add(solution_id)

        instance_id = _portable_id(item.get("instance_id"), what="instance_id")
        if instance_id not in instances:
            raise RegistryUnreadable(f"solution {solution_id!r} names an unknown instance")

        repositories: list[RepositoryBinding] = []
        seen_repos: set[str] = set()
        seen_projects: set[str] = set()
        for raw_repo in _sequence(item.get("repositories", []), what="solution repositories"):
            repo = _mapping(raw_repo, what="solution repository")
            repo_id = _portable_id(repo.get("repo_id"), what="repo_id")
            if repo_id in seen_repos:
                raise RegistryUnreadable(f"duplicate repo_id in {solution_id!r}: {repo_id!r}")
            seen_repos.add(repo_id)
            root_value = repo.get("repo_root")
            if not isinstance(root_value, str) or not _is_absolute(root_value):
                raise RegistryUnreadable(f"repo_root for {repo_id!r} must be an absolute path")
            repo_root = Path(root_value)
            if base == repo_root or base in repo_root.parents:
                raise RegistryUnreadable(
                    f"repo_root for {repo_id!r} must not sit inside AXIOM_HOME"
                )
            project_ids: list[str] = []
            for raw_project in _sequence(repo.get("projects", []), what="repository projects"):
                project = _mapping(raw_project, what="repository project")
                project_id = _portable_id(project.get("project_id"), what="project_id")
                if project_id in seen_projects:
                    raise RegistryUnreadable(
                        f"duplicate project_id in {solution_id!r}: {project_id!r}"
                    )
                seen_projects.add(project_id)
                project_ids.append(project_id)
            repositories.append(
                RepositoryBinding(
                    repo_id=repo_id, repo_root=repo_root, project_ids=tuple(project_ids)
                )
            )
        if not repositories:
            raise RegistryUnreadable(f"solution {solution_id!r} registers no repository")

        catalog_host = item.get("catalog_host_repo")
        if catalog_host is None:
            catalog_host = repositories[0].repo_id
        catalog_host = _portable_id(catalog_host, what="catalog_host_repo")
        if catalog_host not in seen_repos:
            raise RegistryUnreadable(
                f"catalog_host_repo {catalog_host!r} is not a repository of {solution_id!r}"
            )

        solutions.append(
            SolutionBinding(
                solution_id=solution_id,
                instance_id=instance_id,
                catalog_host_repo=catalog_host,
                repositories=tuple(repositories),
            )
        )

    return SnapshotRegistry(axiom_home=base, solutions=tuple(solutions))


def load_registry(
    path: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> SnapshotRegistry:
    """Load the machine-local registry, or an empty registry when it is absent.

    A missing registry is not an error: it is the honest state of a machine that
    has not registered a solution, and it resolves nothing. Malformed content is
    an error, because guessing would be worse than refusing.
    """
    base = axiom_home(env=env, platform=platform)
    target = Path(path) if path is not None else base / REGISTRY_DIRNAME / REGISTRY_FILENAME
    if not target.exists():
        return SnapshotRegistry(axiom_home=base)
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryUnreadable(f"registry is unreadable: {type(exc).__name__}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RegistryUnreadable(f"registry is not valid JSON: {exc.msg}") from exc
    return parse_registry(document, home=base)


__all__ = [
    "ADMISSION_ROLE",
    "AXIOM_HOME_ENV",
    "CATALOG_DIRNAME",
    "CHECKPOINT_LANE",
    "DATA_ROLE",
    "GUARD_DIRNAME",
    "GRAPH_OUTPUT_ROOT",
    "LIVE_LANE",
    "LOCK_FILE_NAMES",
    "LOCK_ROLES",
    "CatalogLocation",
    "ProjectBinding",
    "RegistryError",
    "RegistryUnreadable",
    "RelativeAxiomHome",
    "RepositoryBinding",
    "SnapshotLocation",
    "SnapshotRegistry",
    "SolutionBinding",
    "UnknownBinding",
    "UnsupportedRegistryMajor",
    "UntrustedPath",
    "axiom_home",
    "default_axiom_home",
    "generation_directory_name",
    "load_registry",
    "parse_registry",
    "portable_relative",
    "registry_path",
]
