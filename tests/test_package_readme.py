"""I-005: the package README must ship in the payload and must not drift from the source.

``pyproject.toml`` sets ``readme = "README.md"``, which only places the text inside
``dist-info/METADATA`` - a file a package-only consumer cannot read. ``src/axiom_mcp/README.md``
is therefore copied into the wheel through ``[tool.setuptools.package-data]``. There are exactly
two ways that arrangement breaks silently, and both are checked here: the packaged copy drifts
from the repository README while still shipping, or the packaging declaration is dropped while
the copy stays behind.
"""

from __future__ import annotations

import pathlib
import tomllib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPOSITORY_README = REPO_ROOT / "README.md"
PACKAGED_README = REPO_ROOT / "src" / "axiom_mcp" / "README.md"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"


@pytest.fixture(scope="module")
def project() -> dict:
    return tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))


def test_packaged_readme_is_byte_identical_to_the_repository_readme() -> None:
    assert PACKAGED_README.is_file(), "src/axiom_mcp/README.md is missing from the payload input"
    assert PACKAGED_README.read_bytes() == REPOSITORY_README.read_bytes(), (
        "src/axiom_mcp/README.md drifted from README.md; re-copy the repository README"
    )


def test_readme_is_declared_as_package_data(project: dict) -> None:
    declared = project["tool"]["setuptools"]["package-data"]["axiom_mcp"]
    assert "README.md" in declared, "the packaged README would not ship without this declaration"


def test_long_description_readme_still_points_at_the_repository_readme(project: dict) -> None:
    assert project["project"]["readme"] == "README.md"
