"""V2-016 - the wire-path / native-binding boundary of CP-02 and CP-03.

Positive, negative and failure-boundary legs. The negative legs are driven with a root that does
not exist on disk, so only a purely lexical refusal can have produced them: that is how "refused
before any file is opened" is proven rather than asserted.
"""

from __future__ import annotations

import os
import pathlib
import sys
from pathlib import Path

import pytest

from axiom_mcp import paths, registry

HOST_IDENTITY = f"{sys.platform} {os.name} python {sys.version.split()[0]}"

PORTABLE = (
    "nodes/000000.json",
    "generations/" + "a" * 64 + "/nodes.json",
    "coverage.json",
    "a/b/c.json",
    "\u0e44\u0e1f\u0e25\u0e4c \u0e44\u0e17\u0e22's & (\u0e23\u0e48\u0e32\u0e07) v2.json",
    "a b/c d.json",
)

HOSTILE = (
    "../outside.json",
    "a/../../b.json",
    "nodes/..",
    "/etc/passwd",
    "//server/share/nodes.json",
    "C:/Windows/system32/config.json",
    "C:relative.json",
    "\\\\server\\share\\nodes.json",
    "\\\\?\\C:\\devices\\nodes.json",
    "nodes\\000000.json",
    "",
    ".",
    "..",
    "a//b",
    "a/./b",
    "a/",
    "./a",
    "con",
    "CON.json",
    "lpt3.dat",
    "com\u00b9",
    "aux ",
    "a/b.",
    "a/b ",
    "a/b.txt:stream",
    "a/<b>.json",
    "a/b|c.json",
    "a/b?.json",
    "a/b*.json",
    "a/b\x00c.json",
    "a/b\tc.json",
    "a/\x07b.json",
    "a/\u0085b.json",
    'a/b"c.json',
)


@pytest.mark.parametrize("reference", PORTABLE)
def test_a_portable_wire_path_is_accepted_and_keeps_its_spelling(reference: str) -> None:
    parsed = paths.parse_wire_path(reference)
    assert parsed.spelling == reference
    assert parsed.as_posix() == reference
    assert str(parsed) == reference
    assert parsed.parts == tuple(reference.split("/"))


@pytest.mark.parametrize("reference", HOSTILE)
def test_an_injected_wire_reference_is_refused_before_any_file_is_opened(reference: str) -> None:
    # The bound root does not exist, so a filesystem-backed check could not have produced this
    # refusal: the reference was refused lexically, before the root was even looked at.
    missing_root = Path("l33-does-not-exist") / "lane"
    with pytest.raises(paths.WirePathRejected):
        paths.resolve_wire_path(missing_root, reference)


def test_a_wire_path_that_is_not_text_is_refused() -> None:
    for value in (None, 7, b"nodes.json", Path("nodes.json"), ("nodes", "000000.json")):
        with pytest.raises(paths.WirePathRejected):
            paths.parse_wire_path(value)


def test_a_native_root_is_normalized_natively_not_by_the_wire_rule(tmp_path: Path) -> None:
    # Not created on disk: binding a root does not require the directory to exist yet, so this leg
    # can name a Windows-reserved component that the wire rule refuses. Applying the portable
    # profile here would be the bug this module exists to prevent.
    reserved = tmp_path / "con"
    assert paths.bind_native_root(str(reserved)) == reserved.resolve()
    with pytest.raises(paths.WirePathRejected):
        paths.parse_wire_path("con")

    spaced = tmp_path / "a b"
    spaced.mkdir()
    assert paths.bind_native_root(f"{spaced}{os.sep}") == spaced.resolve()
    assert paths.bind_native_root(str(spaced / "nested" / "..")) == spaced.resolve()


def test_a_relative_or_unusable_native_root_is_refused() -> None:
    for value in ("", "relative/root", Path("relative/root"), 7, None, b"/root"):
        with pytest.raises(paths.NativeBindingRejected):
            paths.bind_native_root(value)


def test_a_native_root_that_is_a_file_is_refused(tmp_path: Path) -> None:
    file_root = tmp_path / "not-a-directory.json"
    file_root.write_text("{}", encoding="utf-8")
    with pytest.raises(paths.NativeBindingRejected):
        paths.bind_native_root(file_root)


def test_the_two_normalizations_are_independent(tmp_path: Path) -> None:
    absolute = str(tmp_path)
    assert paths.bind_native_root(absolute) == tmp_path.resolve()
    with pytest.raises(paths.WirePathRejected):
        paths.parse_wire_path(absolute)
    with pytest.raises(paths.NativeBindingRejected):
        paths.bind_native_root("nodes/000000.json")
    assert paths.parse_wire_path("nodes/000000.json").parts == ("nodes", "000000.json")


def test_a_wire_path_resolves_inside_the_bound_native_root(tmp_path: Path) -> None:
    root = tmp_path / "lane"
    target = root / "proj" / "nodes" / "000000.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")
    resolved = paths.resolve_wire_path(root, "proj/nodes/000000.json")
    assert resolved == root.resolve() / "proj" / "nodes" / "000000.json"
    assert resolved.read_text(encoding="utf-8") == "{}"

    thai_dir = "\u0e42\u0e1b\u0e23\u0e40\u0e08\u0e01\u0e15\u0e4c"
    thai_name = "\u0e42\u0e2b\u0e19\u0e14.json"
    thai = root / thai_dir / thai_name
    thai.parent.mkdir(parents=True)
    thai.write_text("{}", encoding="utf-8")
    assert paths.resolve_wire_path(root, f"{thai_dir}/{thai_name}") == thai.resolve()


def test_a_missing_file_resolves_to_a_path_instead_of_an_error(tmp_path: Path) -> None:
    resolved = paths.resolve_wire_path(tmp_path, "does/not/exist.json")
    assert resolved == tmp_path.resolve() / "does" / "not" / "exist.json"
    assert not resolved.exists()


def test_a_symlink_that_leaves_the_root_is_refused_after_native_resolution(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.json").write_text("{}", encoding="utf-8")
    try:
        os.symlink(outside, root / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create a symbolic link without a privilege")
    with pytest.raises(paths.PathEscape):
        paths.resolve_wire_path(root, "link/secret.json")


def test_a_symlink_that_stays_inside_the_root_resolves(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    inside = root / "real.json"
    inside.write_text("{}", encoding="utf-8")
    try:
        os.symlink(inside, root / "alias.json")
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create a symbolic link without a privilege")
    # This boundary enforces containment, not "no symlink"; shards.shard_path is the layer that
    # refuses a symlink outright when it opens a published file.
    assert paths.resolve_wire_path(root, "alias.json") == inside.resolve()


def test_an_unresolvable_root_is_translated_rather_than_leaked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def refuse(self: Path, strict: bool = False) -> Path:
        raise OSError("simulated resolution failure")

    monkeypatch.setattr(pathlib.Path, "resolve", refuse)
    with pytest.raises(paths.PathUnresolvable):
        paths.bind_native_root(str(tmp_path))


def test_the_registry_lane_resolution_uses_this_boundary(tmp_path: Path) -> None:
    root = tmp_path / "repo" / ".axiom" / "graph" / "alpha" / "auth-api" / "live"
    root.mkdir(parents=True)
    (root / "nodes.json").write_text("{}", encoding="utf-8")
    location = registry.SnapshotLocation(
        solution_id="alpha", project_id="auth-api", lane="live", root=root
    )
    assert location.resolve("nodes.json") == root.resolve() / "nodes.json"
    for hostile in ("../outside.json", "/etc/passwd", "C:/Windows/system32/hosts", "a//b"):
        with pytest.raises(registry.UntrustedPath):
            location.resolve(hostile)
    # The frozen reference rule is unchanged - it still accepts these spellings; what refuses them
    # is the stricter native boundary, once the reference has to name a real file.
    for reference in ("a//b", "a/./b"):
        assert registry.portable_relative(reference) == reference


def test_the_registry_catalog_resolution_uses_this_boundary(tmp_path: Path) -> None:
    root = tmp_path / "catalog" / "checkpoint"
    root.mkdir(parents=True)
    location = registry.CatalogLocation(solution_id="alpha", lane="checkpoint", root=root)
    assert location.resolve("nodes.json") == root.resolve() / "nodes.json"
    with pytest.raises(registry.UntrustedPath):
        location.resolve("../escape.json")
