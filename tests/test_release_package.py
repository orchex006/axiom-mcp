"""C-036 regression test: versioned environments and lockfile rollback.

The AC1 proof drives the real thing on this host. Two wheels are built with the
real build backend, installed by the real pip into two isolated virtual
environments, and each environment's own interpreter is run to observe that it
imports its own version rather than the one installed afterwards. The same test
then rolls back and proves the earlier version is active again and still imports
its own build. Nothing in that leg is simulated.

A virtual environment costs about twenty seconds to create and populate on this
host, so the remaining legs drive the same production code with a scripted
runner: it creates a directory-shaped environment and answers pip's calls, while
every decision under test - the lockfile document, the digest check, the drift
repair, the pointer move, the history record and the no-partial-state cleanup -
is the real code. Each scripted leg says so in its own docstring, so a reader is
never asked to mistake a scripted environment for a real one.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE_PATH = REPO_ROOT / "release" / "package.py"
DISTRIBUTION = "demo-pkg"
MODULE = "demo_pkg"

PROJECT_TEMPLATE = """[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "demo-pkg"
version = "{version}"
requires-python = ">=3.13"

[tool.setuptools]
package-dir = {{ "" = "src" }}

[tool.setuptools.packages.find]
where = ["src"]
"""

MARKERS = {"1.0.0": "one", "1.1.0": "two"}


@pytest.fixture(scope="module")
def packager():
    spec = importlib.util.spec_from_file_location("axiom_release_package", PACKAGE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_project(root: pathlib.Path, version: str) -> None:
    package = root / "src" / MODULE
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(
        'MARKER = "' + MARKERS[version] + '"\n', encoding="utf-8", newline="\n"
    )
    (root / "pyproject.toml").write_text(
        PROJECT_TEMPLATE.format(version=version), encoding="utf-8", newline="\n"
    )


@pytest.fixture(scope="module")
def wheels(packager, tmp_path_factory):
    """Two real wheels for the same distribution, built once per module."""
    base = tmp_path_factory.mktemp("c036-wheels")
    built: dict[str, pathlib.Path] = {}
    for version in ("1.0.0", "1.1.0"):
        project = base / ("project-" + version)
        _write_project(project, version)
        built[version] = packager.build_wheel(project, base / ("dist-" + version))
    return built


def _interpreter(packager, root, version):
    return packager.venv_python(packager.version_directory(root, version) / "venv")


def _import_marker(packager, root, version):
    completed = subprocess.run(
        [
            str(_interpreter(packager, root, version)),
            "-c",
            "import " + MODULE + "; print(" + MODULE + ".MARKER)",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _frozen(packager, root, version):
    return packager.freeze(_interpreter(packager, root, version))


class ScriptedRunner:
    """A scripted environment: directories are real, pip is answered by this class.

    It records every command it was given so a test can assert the delegation
    shape, and it models the one piece of state a drift test needs: whether the
    distribution is currently installed in the environment.
    """

    def __init__(self, packager, *, frozen=("pip==0",), install_returncode=0):
        self.packager = packager
        self.calls: list[tuple[str, ...]] = []
        self.frozen = list(frozen)
        self.install_returncode = install_returncode
        self.installed = False

    @property
    def distribution_line(self) -> str:
        return DISTRIBUTION + " @ artifact/demo_pkg-0.0.0-py3-none-any.whl#sha256=0"

    def __call__(self, argv, **kwargs):
        argv = tuple(str(item) for item in argv)
        self.calls.append(argv)
        if "-m" in argv and "venv" in argv:
            interpreter = self.packager.venv_python(pathlib.Path(argv[-1]))
            interpreter.parent.mkdir(parents=True, exist_ok=True)
            interpreter.write_text("", encoding="utf-8")
            return self.packager.CommandResult(argv, 0, "", "")
        if "install" in argv:
            if self.install_returncode == 0:
                self.installed = True
            return self.packager.CommandResult(
                argv,
                self.install_returncode,
                "",
                "" if self.install_returncode == 0 else "ERROR: not a wheel",
            )
        if "freeze" in argv:
            lines = list(self.frozen)
            if self.installed:
                lines.append(self.distribution_line)
            return self.packager.CommandResult(argv, 0, "\n".join(lines) + "\n", "")
        return self.packager.CommandResult(argv, 0, "", "")


def _scripted_install(packager, tmp_path, version, runner, name=None):
    artifact = tmp_path / (name or ("demo_pkg-" + version + "-py3-none-any.whl"))
    if not artifact.exists():
        artifact.write_bytes(b"placeholder wheel bytes for the scripted runner\n")
    root = tmp_path / "root"
    return packager.install(root, version, artifact=artifact, runner=runner), root


# -- the real proof --------------------------------------------------------


def test_real_isolated_environments_and_rollback(packager, wheels, tmp_path):
    """AC1: real installs into isolated version envs, and a real rollback.

    This is the unscripted leg: real wheels, real virtual environments, real pip
    and real interpreters.
    """
    root = tmp_path / "root"
    for version in ("1.0.0", "1.1.0"):
        packager.install(root, version, artifact=wheels[version])

    for version in ("1.0.0", "1.1.0"):
        version_dir = packager.version_directory(root, version)
        lockfile = packager.read_lockfile(version_dir)
        assert lockfile["version"] == version
        assert lockfile["layout"] == packager.LAYOUT_VERSION
        assert lockfile["artifact"]["sha256"] == packager.sha256_file(
            version_dir / "artifact" / lockfile["artifact"]["name"]
        )
        assert any(item.startswith(DISTRIBUTION) for item in lockfile["frozen"])
        assert packager.normalize_frozen(_frozen(packager, root, version)) == (
            packager.normalize_frozen(lockfile["frozen"])
        )
        # Isolation is observed, not assumed: each environment imports its own build.
        assert _import_marker(packager, root, version) == MARKERS[version]

    assert packager.read_current(root)["active"] == "1.1.0"

    outcome = packager.rollback(root)
    assert outcome["version"] == "1.0.0"
    assert outcome["restored"] is True
    assert outcome["repairs"] == []
    assert packager.read_current(root)["active"] == "1.0.0"
    lockfile = packager.read_lockfile(packager.version_directory(root, "1.0.0"))
    assert packager.normalize_frozen(outcome["frozen"]) == packager.normalize_frozen(
        lockfile["frozen"]
    )
    assert _import_marker(packager, root, "1.0.0") == MARKERS["1.0.0"]
    assert packager.history(root)[-1]["action"] == "rollback"


# -- rollback behaviour (scripted runner) ---------------------------------


def test_rollback_without_drift_does_not_reinstall(packager, tmp_path):
    """Scripted runner leg: an unchanged environment is restored without a reinstall."""
    runner = ScriptedRunner(packager)
    _, root = _scripted_install(packager, tmp_path, "1.0.0", runner)
    _scripted_install(packager, tmp_path, "1.1.0", ScriptedRunner(packager))

    before = len(runner.calls)
    outcome = packager.rollback(root, runner=runner)
    assert outcome["version"] == "1.0.0"
    assert outcome["repairs"] == []
    assert packager.read_current(root)["active"] == "1.0.0"
    assert not any("--force-reinstall" in call for call in runner.calls[before:])


def test_rollback_reinstalls_an_environment_that_drifted(packager, tmp_path):
    """Scripted runner leg: the drifted environment is repaired back to the lockfile."""
    runner = ScriptedRunner(packager)
    _, root = _scripted_install(packager, tmp_path, "1.0.0", runner)
    _scripted_install(packager, tmp_path, "1.1.0", ScriptedRunner(packager))
    lockfile = packager.read_lockfile(packager.version_directory(root, "1.0.0"))

    runner.installed = False  # the environment drifts away from its lockfile
    outcome = packager.rollback(root, runner=runner)
    assert outcome["repairs"] == ["environment_reinstalled"]
    assert outcome["restored"] is True
    assert packager.normalize_frozen(outcome["frozen"]) == packager.normalize_frozen(
        lockfile["frozen"]
    )


def test_rollback_refuses_a_tampered_staged_artifact(packager, tmp_path):
    """Boundary: unverified bytes are refused instead of installed."""
    runner = ScriptedRunner(packager)
    _, root = _scripted_install(packager, tmp_path, "1.0.0", runner)
    _scripted_install(packager, tmp_path, "1.1.0", ScriptedRunner(packager))
    version_dir = packager.version_directory(root, "1.0.0")
    lockfile = packager.read_lockfile(version_dir)
    staged = version_dir / "artifact" / lockfile["artifact"]["name"]
    staged.write_bytes(b"not the bytes the lockfile recorded\n")

    with pytest.raises(packager.PackageError) as excinfo:
        packager.rollback(root, to_version="1.0.0")
    assert excinfo.value.code == "artifact_digest_mismatch"
    assert packager.read_current(root)["active"] == "1.1.0"


def test_rollback_refuses_when_no_earlier_version_exists(packager, tmp_path):
    root = tmp_path / "root"
    packager.activate(root, "1.0.0")
    packager.append_history(root, {"action": "install", "version": "1.0.0", "at": 1.0})
    with pytest.raises(packager.PackageError) as excinfo:
        packager.rollback(root)
    assert excinfo.value.code == "no_previous_version"


def test_rollback_refuses_when_the_target_environment_is_absent(packager, tmp_path):
    root = tmp_path / "root"
    packager.activate(root, "1.1.0")
    packager.append_history(root, {"action": "install", "version": "1.0.0", "at": 1.0})
    packager.append_history(root, {"action": "install", "version": "1.1.0", "at": 2.0})
    with pytest.raises(packager.PackageError) as excinfo:
        packager.rollback(root)
    assert excinfo.value.code == "rollback_target_missing"


# -- refusals before any environment exists -------------------------------


def test_a_missing_artifact_is_refused_before_an_environment_exists(packager, tmp_path):
    root = tmp_path / "root"
    with pytest.raises(packager.PackageError) as excinfo:
        packager.install(root, "9.9.9", artifact=tmp_path / "absent.whl")
    assert excinfo.value.code == "artifact_missing"
    assert not packager.version_directory(root, "9.9.9").exists()
    assert packager.read_current(root) is None


@pytest.mark.parametrize(
    "bad",
    ["", " ", "../escape", "..", "a/b", "a\\b", ".hidden", "-leading", "x" * 65],
)
def test_version_names_cannot_escape_the_versions_directory(packager, bad):
    with pytest.raises(packager.PackageError) as excinfo:
        packager.validate_version(bad)
    assert excinfo.value.code == "version_invalid"


def test_an_install_root_inside_protected_roots_is_refused(packager, tmp_path):
    protected = tmp_path / "running-install"
    protected.mkdir()
    with pytest.raises(packager.PackageError) as excinfo:
        packager.install(
            protected / "releases",
            "1.0.0",
            artifact=tmp_path / "absent.whl",
            protected=[protected],
        )
    assert excinfo.value.code == "install_root_invalid"

    # The default protection covers this repository's own source tree.
    with pytest.raises(packager.PackageError) as excinfo:
        packager.resolve_install_root(REPO_ROOT / "src" / "axiom_mcp" / "installed")
    assert excinfo.value.code == "install_root_invalid"


def test_a_failed_install_leaves_no_partial_version(packager, tmp_path):
    """Scripted runner leg: the install-failure path and its cleanup."""
    runner = ScriptedRunner(packager, install_returncode=1)
    with pytest.raises(packager.PackageError) as excinfo:
        _scripted_install(packager, tmp_path, "9.9.9", runner)
    assert excinfo.value.code == "install_failed"
    root = tmp_path / "root"
    assert not packager.version_directory(root, "9.9.9").exists()
    assert packager.read_current(root) is None


def test_a_failed_venv_creation_leaves_no_partial_version(packager, tmp_path):
    """Scripted runner leg: a venv that cannot be created leaves nothing behind."""

    class NoVenv(ScriptedRunner):
        def __call__(self, argv, **kwargs):
            argv = tuple(str(item) for item in argv)
            self.calls.append(argv)
            if "-m" in argv and "venv" in argv:
                return self.packager.CommandResult(argv, 1, "", "ERROR: venv failed")
            return super().__call__(argv, **kwargs)

    runner = NoVenv(packager)
    with pytest.raises(packager.PackageError) as excinfo:
        _scripted_install(packager, tmp_path, "9.9.9", runner)
    assert excinfo.value.code == "venv_creation_failed"
    assert not packager.version_directory(tmp_path / "root", "9.9.9").exists()


# -- reporting and CLI ----------------------------------------------------


def test_status_reports_versions_and_the_active_one(packager, tmp_path):
    _, root = _scripted_install(packager, tmp_path, "1.0.0", ScriptedRunner(packager))
    report = packager.status(root)
    assert report["active"] == "1.0.0"
    assert [item["version"] for item in report["versions"]] == ["1.0.0"]
    assert report["versions"][0]["locked"] and report["versions"][0]["venv"]
    assert len(report["history"]) == 1


def test_the_cli_reports_a_refusal_as_json_and_a_nonzero_exit(packager, tmp_path, capsys):
    code = packager.main(
        [
            "install",
            "--install-root",
            str(tmp_path / "root"),
            "--version",
            "1.0.0",
            "--artifact",
            str(tmp_path / "absent.whl"),
        ]
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["error"] == "artifact_missing"
