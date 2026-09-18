"""C-009 regression test: a logical reference is the only way to name a file.

The positive cases build a registry under an absolute ``AXIOM_HOME`` and prove
that a logical ``(solution_id, project_id)`` resolves to the contract path
``<repo-root>/.axiom/graph/<solution-id>/<project-id>``, that the guard directory
is the native ABI's ``instances/<id>/solution.guard`` with the two stable lock
files, and that an offline checkpoint lane is reachable without a daemon.

The negative and boundary cases are the point of the slice: a caller-supplied
string may not choose a file. An absolute path, a backslash path, a drive-letter
path, a leading slash and a ``..`` segment are each refused, an unregistered
logical id is refused instead of being turned into a filesystem guess, a symlink
planted inside a lane cannot escape it, a relative ``AXIOM_HOME`` is refused, a
declared guard directory that is not the ABI path is refused, and a missing
registry resolves nothing rather than scanning the disk.
"""

from __future__ import annotations

import json
import os
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from axiom_mcp import registry

HOST_IDS = ("solution_id", "project_id", "repo_id")


def document(
    *,
    home: Path,
    repo_root: Path,
    solution_id: str = "alpha",
    project_id: str = "auth-api",
    repo_id: str = "auth-repo",
    catalog_host_repo: str | None = None,
    guard_directory: str | None = None,
    schema_version: Any = 1,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    instance: dict[str, Any] = {"instance_id": "local-1"}
    if guard_directory is not None:
        instance["guard_directory"] = guard_directory
    body: dict[str, Any] = {
        "schema_version": schema_version,
        "axiom_home": str(home),
        "instances": [instance],
        "solutions": [
            {
                "solution_id": solution_id,
                "instance_id": "local-1",
                "repositories": [
                    {
                        "repo_id": repo_id,
                        "repo_root": str(repo_root),
                        "projects": [{"project_id": project_id}],
                    }
                ],
            }
        ],
    }
    if catalog_host_repo is not None:
        body["solutions"][0]["catalog_host_repo"] = catalog_host_repo
    if extra:
        body.update(extra)
    return body


def build(tmp_path: Path, **kwargs: Any) -> tuple[registry.SnapshotRegistry, Path, Path]:
    home = tmp_path / "home"
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    body = document(home=home, repo_root=repo_root, **kwargs)
    return registry.parse_registry(body, home=home), home, repo_root


def test_logical_reference_resolves_through_the_trusted_binding(tmp_path: Path) -> None:
    loaded, _home, repo_root = build(tmp_path)
    location = loaded.location("alpha", "auth-api")
    assert location.root == repo_root / ".axiom" / "graph" / "alpha" / "auth-api" / "live"
    assert location.pointer == location.root / "current.json"
    assert location.generations_root == location.root / "generations"
    assert loaded.project("alpha", "auth-api").repo_root == repo_root


def test_guard_directory_and_lock_files_match_the_native_abi(tmp_path: Path) -> None:
    loaded, home, _repo = build(tmp_path)
    guard = loaded.guard_directory("local-1")
    assert guard == home / "instances" / "local-1" / "solution.guard"
    assert loaded.lock_path("local-1", registry.ADMISSION_ROLE) == guard / "admission.lock"
    assert loaded.lock_path("local-1", registry.DATA_ROLE) == guard / "data.lock"
    assert set(loaded.lock_paths("local-1")) == {"admission", "data"}
    assert registry.LOCK_FILE_NAMES == {"admission": "admission.lock", "data": "data.lock"}


def test_catalog_location_uses_the_catalog_host_repo(tmp_path: Path) -> None:
    loaded, _home, repo_root = build(tmp_path)
    catalog = loaded.catalog_location("alpha", registry.CHECKPOINT_LANE)
    assert catalog.root == (repo_root / ".axiom" / "graph" / "alpha" / "_catalog" / "checkpoint")
    assert catalog.pointer == catalog.root / "current.json"


def test_checkpoint_lane_is_reachable_without_a_daemon(tmp_path: Path) -> None:
    loaded, _home, repo_root = build(tmp_path)
    checkpoint = loaded.location("alpha", "auth-api", "checkpoint")
    assert checkpoint.lane == "checkpoint"
    assert checkpoint.root == repo_root / ".axiom" / "graph" / "alpha" / "auth-api" / "checkpoint"
    generation = "a" * 64
    assert checkpoint.generation_root(generation) == (checkpoint.generations_root / generation)


def test_missing_registry_resolves_nothing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    loaded = registry.load_registry(
        tmp_path / "home" / "config" / "registry.json", env={registry.AXIOM_HOME_ENV: str(home)}
    )
    assert loaded.is_empty
    assert loaded.solutions == ()
    with pytest.raises(registry.UnknownBinding):
        loaded.location("alpha", "auth-api")


def test_registry_path_is_under_config(tmp_path: Path) -> None:
    assert registry.registry_path(tmp_path) == tmp_path / "config" / "registry.json"


def test_loaded_registry_round_trips_through_a_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    body = document(home=home, repo_root=repo_root)
    (home / "config" / "registry.json").write_text(json.dumps(body), encoding="utf-8", newline="\n")
    loaded = registry.load_registry(env={registry.AXIOM_HOME_ENV: str(home)})
    assert loaded.location("alpha", "auth-api").root.name == "live"
    assert Path(loaded.axiom_home) == home


class AxiomHomeDefaultTests(unittest.TestCase):
    def test_defaults_follow_the_canonical_layout(self) -> None:
        cases = (
            (
                "win32",
                {"LOCALAPPDATA": r"C:\Users\demo\AppData\Local"},
                Path(r"C:\Users\demo\AppData\Local") / "Axiom",
            ),
            (
                "linux",
                {"HOME": "/home/demo", "XDG_STATE_HOME": "/home/demo/.state"},
                Path("/home/demo/.state") / "axiom",
            ),
            (
                "linux",
                {"HOME": "/home/demo"},
                Path("/home/demo/.local/state/axiom"),
            ),
            (
                "darwin",
                {"HOME": "/Users/demo"},
                Path("/Users/demo/Library/Application Support/Axiom"),
            ),
        )
        for platform, env, expected in cases:
            with self.subTest(platform=platform, env=sorted(env)):
                assert registry.default_axiom_home(env=env, platform=platform) == expected


class UntrustedReferenceTests(unittest.TestCase):
    def test_a_caller_supplied_string_cannot_choose_a_file(self) -> None:
        refused = (
            "C:/windows/system32/drivers/etc/hosts",
            "C:\\windows\\system32\\hosts",
            "/etc/passwd",
            "\\\\server\\share\\graph.json",
            "../outside.json",
            "generations/../../outside.json",
            "a/../../b.json",
            "",
            "nodes.json/../../../../etc/hosts",
        )
        for value in refused:
            with self.subTest(reference=value):
                with pytest.raises(registry.UntrustedPath):
                    registry.portable_relative(value)

    def test_a_portable_reference_is_accepted(self) -> None:
        accepted = ("nodes.json", "generations/" + "a" * 64 + "/nodes.json", "a/b/c.json")
        for value in accepted:
            with self.subTest(reference=value):
                assert registry.portable_relative(value) == value


def test_a_resolved_reference_stays_inside_the_lane(tmp_path: Path) -> None:
    loaded, _home, _repo = build(tmp_path)
    location = loaded.location("alpha", "auth-api")
    location.root.mkdir(parents=True)
    assert location.resolve("nodes.json") == location.root.resolve() / "nodes.json"


def test_an_unregistered_logical_id_is_refused(tmp_path: Path) -> None:
    loaded, _home, _repo = build(tmp_path)
    with pytest.raises(registry.UnknownBinding):
        loaded.location("alpha", "not-registered")
    with pytest.raises(registry.UnknownBinding):
        loaded.location("not-registered", "auth-api")
    with pytest.raises(registry.UnknownBinding):
        loaded.solution("not-registered")


def test_a_symlink_planted_in_the_lane_cannot_escape_it(tmp_path: Path) -> None:
    loaded, _home, _repo = build(tmp_path)
    location = loaded.location("alpha", "auth-api")
    location.root.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    link = location.root / "escape.json"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create a symbolic link without a privilege")
    with pytest.raises(registry.UntrustedPath):
        location.resolve("escape.json")


def test_relative_axiom_home_override_is_refused() -> None:
    with pytest.raises(registry.RelativeAxiomHome):
        registry.axiom_home(env={registry.AXIOM_HOME_ENV: "relative/home"})
    with pytest.raises(registry.RelativeAxiomHome):
        registry.load_registry(env={registry.AXIOM_HOME_ENV: "relative/home"})


def test_a_declared_guard_directory_outside_the_abi_path_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    for bad in (str(tmp_path / "elsewhere" / "solution.guard"), str(home / "instances" / "other")):
        with pytest.raises(registry.RegistryUnreadable):
            registry.parse_registry(
                document(home=home, repo_root=repo_root, guard_directory=bad), home=home
            )
    good = str(home / "instances" / "local-1" / "solution.guard")
    loaded = registry.parse_registry(
        document(home=home, repo_root=repo_root, guard_directory=good), home=home
    )
    assert loaded.guard_directory("local-1") == Path(good)


def test_repo_root_inside_axiom_home_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    inside = home / "instances" / "local-1"
    with pytest.raises(registry.RegistryUnreadable):
        registry.parse_registry(document(home=home, repo_root=inside), home=home)


def test_a_relative_repo_root_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    body = document(home=home, repo_root=tmp_path / "repo")
    body["solutions"][0]["repositories"][0]["repo_root"] = "repo"
    with pytest.raises(registry.RegistryUnreadable):
        registry.parse_registry(body, home=home)


def test_an_unknown_registry_major_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    for bad in (2, 0, "1", None):
        with pytest.raises(registry.RegistryError):
            registry.parse_registry(
                document(home=home, repo_root=repo_root, schema_version=bad), home=home
            )


def test_a_registry_axiom_home_that_disagrees_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    body = document(home=tmp_path / "other-home", repo_root=repo_root)
    with pytest.raises(registry.RegistryUnreadable):
        registry.parse_registry(body, home=home)


def test_duplicate_bindings_are_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    body = document(home=home, repo_root=repo_root)
    body["solutions"].append(body["solutions"][0])
    with pytest.raises(registry.RegistryUnreadable):
        registry.parse_registry(body, home=home)

    body = document(home=home, repo_root=repo_root)
    body["solutions"][0]["repositories"][0]["projects"].append({"project_id": "auth-api"})
    with pytest.raises(registry.RegistryUnreadable):
        registry.parse_registry(body, home=home)

    body = document(home=home, repo_root=repo_root)
    body["instances"].append({"instance_id": "local-1"})
    with pytest.raises(registry.RegistryUnreadable):
        registry.parse_registry(body, home=home)


def test_a_malformed_registry_is_refused_not_ignored(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    target = home / "config" / "registry.json"
    for text in ("{not json", "[]", '{"schema_version": 1, "solutions": "no"}'):
        target.write_text(text, encoding="utf-8", newline="\n")
        with pytest.raises(registry.RegistryError):
            registry.load_registry(env={registry.AXIOM_HOME_ENV: str(home)})


def test_an_unregistered_catalog_host_repo_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    with pytest.raises(registry.RegistryUnreadable):
        registry.parse_registry(
            document(home=home, repo_root=repo_root, catalog_host_repo="other-repo"), home=home
        )


def test_generation_root_refuses_a_non_digest_id(tmp_path: Path) -> None:
    loaded, _home, _repo = build(tmp_path)
    location = loaded.location("alpha", "auth-api")
    for bad in ("latest", "../" + "a" * 64, "A" * 64, "a" * 63, ""):
        with pytest.raises(registry.UntrustedPath):
            location.generation_root(bad)


def test_an_unknown_guard_role_is_refused(tmp_path: Path) -> None:
    loaded, _home, _repo = build(tmp_path)
    with pytest.raises(registry.RegistryError):
        loaded.lock_path("local-1", "other")
