"""V2-024 regression test: native entrypoints are locked to a version environment.

A host does not activate a shell to start this gateway; it launches one absolute
interpreter with an argument vector. The positive cases therefore drive the real
thing: a real virtual environment is created by :mod:`venv` at a path holding a
space, an apostrophe, an ampersand, parentheses and Thai characters, and the plan
under test is asked for its interpreter, its argv and its environment. The planned
argv is then started for real - a JSON-RPC ``initialize`` over the process pipes -
which is the only way to prove that a path with spaces needs no quoting, no shell
and no activation script.

The negative cases prove the lock is load-bearing rather than decorative: an
interpreter outside the version environment, a bare ``PATH`` lookup, an activation
script, a Python line the distribution does not support, a non-isolated prefix, an
SDK that is not the pin, a probe that does not answer and a hand-edited lockfile
are each refused with the canonical code.

The refusal legs drive the same production code with a scripted interpreter probe,
because creating a broken virtual environment per case would cost minutes and prove
the same branches; every scripted leg says so in its own docstring. The one real
environment that must fail to resolve - a virtual environment that does not share
the base interpreter's packages - is real, so the containment claim is not made
only against a script.

Windows reparse-point and ACL cases, macOS, and a real non-loopback bind are
recorded as ``not_run`` in ``docs/locked-entrypoints.md``; this host cannot produce
them.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tomllib
import venv
from collections.abc import Callable, Mapping

import httpx
import pytest

from axiom_mcp import entrypoints, http, sdk_compat, security, version

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
PACKAGE_PATH = REPO_ROOT / "release" / "package.py"

# A path that breaks every naive launcher: a space, an ampersand, parentheses, an
# apostrophe and Thai text. ``\u0e17\u0e14\u0e2a\u0e2d\u0e1a`` is Thai for "test".
AWKWARD_DIR = "axiom mcp (locked) & 'v2' \u0e17\u0e14\u0e2a\u0e2d\u0e1a"

# The version the real environment is locked to, and the version the scripted
# environments use. They differ so a leg can never be satisfied by the other leg.
LOCKED_VERSION = "9.9.9"
ISOLATED_VERSION = "8.8.8"
SCRIPTED_VERSION = "1.0.0"

SUPPORTED_PYTHON = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

BASE_URL = "http://127.0.0.1:8765"
HOST_HEADER = "127.0.0.1:8765"
ORIGIN_HEADER = "http://127.0.0.1:8765"
TOKEN_ENV_NAME = "V2_024_TEST_TOKEN"
TOKEN_SECRET = "v2-024-test-secret"

INITIALIZE_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": version.MCP_PROTOCOL_MINIMUM,
        "capabilities": {},
        "clientInfo": {"name": "v2-024-test", "version": "0.0.0"},
    },
}


# --- real environment helpers ----------------------------------------------


def venv_scripts(venv_dir: pathlib.Path) -> pathlib.Path:
    return venv_dir / ("Scripts" if os.name == "nt" else "bin")


def venv_site_packages(venv_dir: pathlib.Path) -> pathlib.Path:
    if os.name == "nt":
        return venv_dir / "Lib" / "site-packages"
    matches = sorted((venv_dir / "lib").glob("python*/site-packages"))
    assert matches, f"no site-packages directory under {venv_dir}"
    return matches[0]


def build_version_environment(
    root: pathlib.Path,
    *,
    version_name: str,
    share_system_site: bool,
    link_source: bool,
) -> dict[str, pathlib.Path]:
    """Create a real version environment plus the layout the packaging tool owns.

    The layout document is written by this test rather than by ``release/package.py``
    because the packager builds a wheel and installs it with pip; the shape of the
    document is the part under test here, and the packaging tool owns its own
    regression test. ``link_source`` writes a ``.pth`` pointing at the repository
    ``src`` tree, which is what an editable install produces, so the planned argv can
    be started for real.
    """
    version_dir = root / entrypoints.VERSIONS_DIRNAME / version_name
    venv_dir = version_dir / entrypoints.VENV_DIRNAME
    venv.EnvBuilder(system_site_packages=share_system_site, with_pip=False).create(venv_dir)

    interpreter = entrypoints.venv_python(venv_dir)
    assert interpreter.is_file(), interpreter
    if link_source:
        site = venv_site_packages(venv_dir)
        site.mkdir(parents=True, exist_ok=True)
        (site / "axiom_mcp_locked.pth").write_text(str(SRC_ROOT) + "\n", encoding="utf-8")

    lockfile = version_dir / entrypoints.LOCKFILE_FILENAME
    lockfile.write_text(
        json.dumps(
            {
                "layout": entrypoints.LOCK_LAYOUT_VERSION,
                "version": version_name,
                "artifact": {
                    "name": "axiom_mcp-0.0.0.dev0-py3-none-any.whl",
                    "sha256": "0" * 64,
                    "size": 1,
                },
                "python": {
                    "executable": str(interpreter),
                    "version": SUPPORTED_PYTHON,
                    "marker": version.VERSION,
                },
                "venv": entrypoints.VENV_DIRNAME,
                "frozen": [f"{version.SDK_PACKAGE}=={version.SDK_PIN}"],
                "created_at": 0.0,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    write_pointer(root, version_name)
    return {
        "root": root,
        "version_dir": version_dir,
        "venv": venv_dir,
        "interpreter": interpreter,
        "lockfile": lockfile,
    }


# --- scripted environment helpers ------------------------------------------


def write_pointer(root: pathlib.Path, active: str | None) -> pathlib.Path:
    path = root / entrypoints.CURRENT_FILENAME
    if active is None:
        path.unlink(missing_ok=True)
        return path
    path.write_text(
        json.dumps({"layout": entrypoints.LOCK_LAYOUT_VERSION, "active": active, "updated_at": 0.0})
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def write_lockfile(version_dir: pathlib.Path, **overrides: object) -> pathlib.Path:
    document: dict[str, object] = {
        "layout": entrypoints.LOCK_LAYOUT_VERSION,
        "version": version_dir.name,
        "artifact": {
            "name": "axiom_mcp-0.0.0.dev0-py3-none-any.whl",
            "sha256": "0" * 64,
            "size": 1,
        },
        "python": {
            "executable": str(entrypoints.venv_python(version_dir / entrypoints.VENV_DIRNAME)),
            "version": SUPPORTED_PYTHON,
            "marker": version.VERSION,
        },
        "venv": entrypoints.VENV_DIRNAME,
        "frozen": [f"{version.SDK_PACKAGE}=={version.SDK_PIN}"],
        "created_at": 0.0,
    }
    document.update(overrides)
    path = version_dir / entrypoints.LOCKFILE_FILENAME
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def scripted_environment(
    root: pathlib.Path,
    *,
    version_name: str = SCRIPTED_VERSION,
    with_interpreter: bool = True,
    with_metadata: bool = True,
    activate: bool = True,
) -> dict[str, pathlib.Path]:
    """A directory-shaped version environment.

    Nothing about it is simulated except the interpreter *binary*: every decision
    under test - the containment check, the metadata read, the refusal table and the
    lock document - is made by the real code over real files.
    """
    version_dir = root / entrypoints.VERSIONS_DIRNAME / version_name
    venv_dir = version_dir / entrypoints.VENV_DIRNAME
    interpreter = entrypoints.venv_python(venv_dir)
    if with_interpreter:
        interpreter.parent.mkdir(parents=True, exist_ok=True)
        interpreter.write_bytes(b"")
    else:
        venv_dir.mkdir(parents=True, exist_ok=True)
    if with_metadata:
        (venv_dir / "pyvenv.cfg").write_text(
            "home = /nonexistent\ninclude-system-site-packages = false\n", encoding="utf-8"
        )
    lockfile = write_lockfile(version_dir)
    if activate:
        write_pointer(root, version_name)
    return {
        "root": root,
        "version_dir": version_dir,
        "venv": venv_dir,
        "interpreter": interpreter,
        "lockfile": lockfile,
    }


def probe_report(
    venv_dir: pathlib.Path,
    *,
    prefix: str | None = None,
    base_prefix: str | None = None,
    python_version: str = SUPPORTED_PYTHON,
    sdk: str | None = version.SDK_PIN,
    sdk_path: str | None = None,
) -> dict[str, object]:
    """What a locked interpreter answers about itself, as a scripted probe would."""
    resolved = str(pathlib.Path(venv_dir).resolve())
    return {
        "version": python_version,
        "prefix": resolved if prefix is None else prefix,
        "base_prefix": resolved + "-base" if base_prefix is None else base_prefix,
        "executable": str(entrypoints.venv_python(venv_dir)),
        "sdk": sdk,
        "sdk_path": (
            str(pathlib.Path(venv_dir) / "Lib" / "site-packages" / "mcp" / "__init__.py")
            if sdk_path is None
            else sdk_path
        ),
    }


def runner_returning(
    report: Mapping[str, object] | None,
    *,
    returncode: int = 0,
    stderr: str = "",
) -> Callable[..., entrypoints.CommandResult]:
    """A scripted interpreter probe for an interpreter that is never executed."""
    stdout = "" if report is None else json.dumps(report) + "\n"

    def run(*args: object, **kwargs: object) -> entrypoints.CommandResult:
        argv = args[0] if args else kwargs.get("argv", ())
        return entrypoints.CommandResult(
            argv=tuple(str(item) for item in argv),
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    return run


def scripted_probe(environment: Mapping[str, pathlib.Path]) -> Callable[..., object]:
    return runner_returning(probe_report(environment["venv"]))


def refused(code: str, call: Callable[[], object]) -> entrypoints.EntrypointRefused:
    with pytest.raises(entrypoints.EntrypointRefused) as info:
        call()
    assert info.value.code == code, info.value.detail
    return info.value


def parent_environment(**overrides: str) -> dict[str, str]:
    """A caller environment that keeps only the Windows loader's own variables."""
    base: dict[str, str] = {}
    for name in ("SYSTEMROOT", "WINDIR"):
        value = os.environ.get(name)
        if value:
            base[name] = value
    base.update(overrides)
    return base


def run_in(
    interpreter: pathlib.Path, code: str, environment: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(interpreter), "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=dict(environment),
        timeout=entrypoints.DEFAULT_TIMEOUT_S,
        check=False,
    )


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(scope="module")
def packager() -> object:
    spec = importlib.util.spec_from_file_location("axiom_release_package_v2_024", PACKAGE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def locked(tmp_path_factory: pytest.TempPathFactory) -> dict[str, pathlib.Path]:
    """A real install root whose own path holds spaces, an ampersand and Thai text."""
    root = tmp_path_factory.mktemp("locked") / AWKWARD_DIR
    return build_version_environment(
        root, version_name=LOCKED_VERSION, share_system_site=True, link_source=True
    )


@pytest.fixture(scope="module")
def isolated(tmp_path_factory: pytest.TempPathFactory) -> dict[str, pathlib.Path]:
    """A real environment that does not share the base interpreter's packages."""
    root = tmp_path_factory.mktemp("isolated") / "isolated root (v2-024)"
    return build_version_environment(
        root, version_name=ISOLATED_VERSION, share_system_site=False, link_source=False
    )


@pytest.fixture(scope="module")
def plans(locked: Mapping[str, pathlib.Path]) -> dict[str, entrypoints.LaunchPlan]:
    return {
        entrypoints.MODE_STDIO: entrypoints.launch_plan(
            entrypoints.MODE_STDIO, install_root=locked["root"]
        ),
        entrypoints.MODE_HTTP: entrypoints.launch_plan(
            entrypoints.MODE_HTTP,
            install_root=locked["root"],
            allow_hosts=[HOST_HEADER, "localhost:8765"],
            allow_origins=[ORIGIN_HEADER, "http://localhost:8765"],
            port=8765,
        ),
    }


# --- the installed layout agrees with its owner -----------------------------


def test_the_installed_layout_names_agree_with_the_packaging_tool(packager: object) -> None:
    """The layout names are mirrored because ``release/`` is not installed.

    A copy that could drift would be a second, silent definition of the layout.
    """
    assert entrypoints.VERSIONS_DIRNAME == packager.VERSIONS_DIRNAME
    assert entrypoints.VENV_DIRNAME == packager.VENV_DIRNAME
    assert entrypoints.LOCKFILE_FILENAME == packager.LOCKFILE_FILENAME
    assert entrypoints.CURRENT_FILENAME == packager.CURRENT_FILENAME
    assert entrypoints.LOCK_LAYOUT_VERSION == packager.LAYOUT_VERSION


# --- the real environment is the locked one ---------------------------------


def test_the_locked_runtime_is_the_version_environment(locked: Mapping[str, pathlib.Path]) -> None:
    runtime = entrypoints.resolve_runtime(locked["root"], mode=entrypoints.MODE_STDIO)

    assert pathlib.Path(runtime.venv) == locked["venv"].resolve()
    assert pathlib.Path(runtime.interpreter) == locked["interpreter"].resolve()
    assert locked["venv"].resolve() in locked["interpreter"].resolve().parents
    assert runtime.isolated
    assert runtime.shared_base_packages is True
    assert runtime.python_version == SUPPORTED_PYTHON
    assert runtime.sdk_package == version.SDK_PACKAGE
    assert runtime.sdk_pin == version.SDK_PIN
    assert runtime.sdk_version == version.SDK_PIN
    assert runtime.lockfile_sha256 == entrypoints.sha256_file(locked["lockfile"])
    # The awkward characters are part of the recorded interpreter, not of an argument.
    assert AWKWARD_DIR in runtime.interpreter


def test_the_real_shared_environment_declares_that_it_shares_base_packages(
    locked: Mapping[str, pathlib.Path],
) -> None:
    metadata = entrypoints.read_venv_metadata(locked["venv"])

    assert entrypoints.shared_base_packages(metadata) is True


def test_a_real_environment_that_cannot_see_the_sdk_is_refused(
    isolated: Mapping[str, pathlib.Path],
) -> None:
    """A real environment, not a script: it genuinely cannot import the pinned SDK."""
    metadata = entrypoints.read_venv_metadata(isolated["venv"])
    assert entrypoints.shared_base_packages(metadata) is False

    refusal = refused("sdk_not_installed", lambda: entrypoints.resolve_runtime(isolated["root"]))

    assert refusal.detail == version.SDK_PACKAGE


def test_python_no_user_site_is_what_removes_the_ambient_sdk(
    locked: Mapping[str, pathlib.Path],
) -> None:
    """Why the plan sets ``PYTHONNOUSERSITE``: without it the ambient SDK is reachable."""
    scoped = entrypoints.scoped_environment(
        venv=locked["venv"], shared=True, mode=entrypoints.MODE_STDIO, parent={}
    )
    isolated_prefix = run_in(
        locked["interpreter"],
        "import mcp, sys; print(sys.prefix != sys.base_prefix)",
        scoped,
    )
    without_user_site = run_in(
        locked["interpreter"], "import mcp", {**scoped, "PYTHONNOUSERSITE": "1"}
    )

    assert isolated_prefix.returncode == 0, isolated_prefix.stderr
    assert isolated_prefix.stdout.strip() == "True"
    assert without_user_site.returncode != 0
    assert version.SDK_PACKAGE in without_user_site.stderr


# --- the plan is an argv array, never a shell string ------------------------


def test_the_stdio_plan_is_an_argv_array_with_an_absolute_interpreter(
    plans: Mapping[str, entrypoints.LaunchPlan],
) -> None:
    plan = plans[entrypoints.MODE_STDIO]
    document = entrypoints.host_launch_document(plan)

    assert pathlib.Path(document["command"]).is_absolute()
    assert AWKWARD_DIR in document["command"]
    assert document["command"] == plan.executable
    assert document["args"] == ["-m", "axiom_mcp.stdio", "--name", version.COMPONENT]
    assert isinstance(document["args"], list)
    assert all(AWKWARD_DIR not in argument for argument in document["args"])
    assert document["entrypoint"] == entrypoints.MODE_STDIO
    assert document["component"] == version.COMPONENT
    assert document["digest"] == entrypoints.plan_digest(plan)


def test_the_host_document_never_offers_a_shell_string(
    plans: Mapping[str, entrypoints.LaunchPlan],
) -> None:
    document = entrypoints.host_launch_document(plans[entrypoints.MODE_STDIO])
    rendered = json.dumps(document)

    assert set(document) == {"entrypoint", "component", "command", "args", "cwd", "env", "digest"}
    assert isinstance(document["args"], list)
    # A joined command line is the failure this document exists to prevent.
    assert f"{document['command']} -m" not in rendered
    assert f"{document['command']} " not in rendered


def test_the_plan_environment_is_sorted_so_the_digest_can_be_written_down(
    plans: Mapping[str, entrypoints.LaunchPlan],
) -> None:
    plan = plans[entrypoints.MODE_STDIO]

    assert list(plan.environment) == sorted(plan.environment)
    assert dict(plan.environment) == plan.environment_map


def test_the_locked_stdio_plan_starts_and_answers_without_an_activation_script(
    plans: Mapping[str, entrypoints.LaunchPlan],
) -> None:
    plan = plans[entrypoints.MODE_STDIO]

    completed = subprocess.run(
        plan.argv,
        input=json.dumps(INITIALIZE_REQUEST) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=plan.environment_map,
        cwd=plan.cwd,
        timeout=entrypoints.DEFAULT_TIMEOUT_S,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    frames = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    responses = [frame for frame in frames if frame.get("id") == 1]
    assert len(responses) == 1
    assert responses[0]["result"]["serverInfo"]["name"] == version.COMPONENT
    assert responses[0]["result"]["protocolVersion"] == version.MCP_PROTOCOL_MINIMUM


# --- the launched environment is built from scratch -------------------------


def test_the_plan_digest_is_stable_across_host_shells(tmp_path: pathlib.Path) -> None:
    """The environment is built from scratch, so a different shell cannot move the digest."""
    root = tmp_path / "digest root"
    root.mkdir()
    environment = scripted_environment(root)
    probe = runner_returning(probe_report(environment["venv"]))

    first = entrypoints.launch_plan(
        entrypoints.MODE_STDIO,
        install_root=root,
        probe=probe,
        parent=parent_environment(PATH="C:\\one", PYTHONPATH="C:\\evil", PYTHONHOME="C:\\evil"),
    )
    second = entrypoints.launch_plan(
        entrypoints.MODE_STDIO,
        install_root=root,
        probe=probe,
        parent=parent_environment(PATH="D:\\two", HOME="elsewhere"),
    )

    assert entrypoints.plan_digest(first) == entrypoints.plan_digest(second)

    environment_map = first.environment_map
    assert "PYTHONPATH" not in environment_map
    assert "PYTHONHOME" not in environment_map
    assert environment_map["VIRTUAL_ENV"] == str(environment["venv"].resolve())
    assert environment_map[entrypoints.ENVIRONMENT_MARKER] == entrypoints.MODE_STDIO
    assert environment_map["PYTHONNOUSERSITE"] == "1"
    assert environment_map["PATH"].split(os.pathsep)[0] == str(venv_scripts(environment["venv"]))


def test_a_shared_environment_does_not_suppress_its_own_base_user_site(
    tmp_path: pathlib.Path,
) -> None:
    """Suppressing the user site would contradict the environment's own ``pyvenv.cfg``."""
    environment = scripted_environment(tmp_path)

    shared = entrypoints.scoped_environment(
        venv=environment["venv"], shared=True, mode=entrypoints.MODE_STDIO, parent={}
    )
    unshared = entrypoints.scoped_environment(
        venv=environment["venv"], shared=False, mode=entrypoints.MODE_STDIO, parent={}
    )

    assert "PYTHONNOUSERSITE" not in shared
    assert unshared["PYTHONNOUSERSITE"] == "1"


# --- the refusal table ------------------------------------------------------


def test_a_missing_install_root_is_refused(tmp_path: pathlib.Path) -> None:
    refused("install_root_missing", lambda: entrypoints.resolve_runtime(tmp_path / "absent"))


def test_an_unknown_mode_is_refused(tmp_path: pathlib.Path) -> None:
    environment = scripted_environment(tmp_path)

    refused(
        "mode_unknown",
        lambda: entrypoints.resolve_runtime(
            environment["root"], mode="websocket", probe=scripted_probe(environment)
        ),
    )


def test_an_install_root_without_an_active_pointer_is_refused(tmp_path: pathlib.Path) -> None:
    environment = scripted_environment(tmp_path, activate=False)

    refused(
        "current_missing",
        lambda: entrypoints.resolve_runtime(environment["root"], probe=scripted_probe(environment)),
    )


def test_an_active_pointer_that_is_not_a_version_name_is_refused(tmp_path: pathlib.Path) -> None:
    environment = scripted_environment(tmp_path)
    write_pointer(environment["root"], "../../escape")

    refused(
        "version_invalid",
        lambda: entrypoints.resolve_runtime(environment["root"], probe=scripted_probe(environment)),
    )


def test_a_version_directory_without_a_lockfile_is_refused(tmp_path: pathlib.Path) -> None:
    environment = scripted_environment(tmp_path)
    environment["lockfile"].unlink()

    refused("lockfile_missing", lambda: entrypoints.resolve_runtime(environment["root"]))


def test_a_lockfile_that_is_not_readable_json_is_refused(tmp_path: pathlib.Path) -> None:
    environment = scripted_environment(tmp_path)
    environment["lockfile"].write_text("{not json", encoding="utf-8")

    refused("lockfile_corrupt", lambda: entrypoints.resolve_runtime(environment["root"]))


def test_a_lockfile_naming_another_version_is_refused(tmp_path: pathlib.Path) -> None:
    environment = scripted_environment(tmp_path)
    write_lockfile(environment["version_dir"], version="7.7.7")

    refusal = refused(
        "lockfile_version_mismatch", lambda: entrypoints.resolve_runtime(environment["root"])
    )

    assert "7.7.7" in refusal.detail


def test_a_version_environment_without_an_interpreter_is_refused(tmp_path: pathlib.Path) -> None:
    environment = scripted_environment(tmp_path, with_interpreter=False)

    refused("interpreter_missing", lambda: entrypoints.resolve_runtime(environment["root"]))


def test_an_interpreter_outside_the_version_environment_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    environment = scripted_environment(tmp_path)
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    outside = ambient / ("python.exe" if os.name == "nt" else "python")
    outside.write_bytes(b"")
    write_lockfile(
        environment["version_dir"],
        python={
            "executable": str(outside),
            "version": SUPPORTED_PYTHON,
            "marker": version.VERSION,
        },
    )

    refused(
        "interpreter_outside_environment",
        lambda: entrypoints.resolve_runtime(environment["root"]),
    )


def test_a_version_environment_without_metadata_is_refused(tmp_path: pathlib.Path) -> None:
    environment = scripted_environment(tmp_path, with_metadata=False)

    refused("venv_metadata_missing", lambda: entrypoints.resolve_runtime(environment["root"]))


def test_a_probe_that_fails_is_refused(tmp_path: pathlib.Path) -> None:
    environment = scripted_environment(tmp_path)

    refusal = refused(
        "interpreter_probe_failed",
        lambda: entrypoints.resolve_runtime(
            environment["root"], probe=runner_returning(None, returncode=1, stderr="boom\n")
        ),
    )

    assert refusal.detail == "boom"


def test_a_probe_that_is_not_a_report_is_refused(tmp_path: pathlib.Path) -> None:
    environment = scripted_environment(tmp_path)

    refused(
        "interpreter_probe_corrupt",
        lambda: entrypoints.resolve_runtime(environment["root"], probe=runner_returning(None)),
    )


def test_a_python_line_the_distribution_does_not_support_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    environment = scripted_environment(tmp_path)

    refused(
        "interpreter_version_unsupported",
        lambda: entrypoints.resolve_runtime(
            environment["root"],
            probe=runner_returning(probe_report(environment["venv"], python_version="3.11.9")),
        ),
    )


def test_an_interpreter_that_is_not_isolated_is_refused(tmp_path: pathlib.Path) -> None:
    environment = scripted_environment(tmp_path)
    resolved = str(environment["venv"].resolve())

    refused(
        "interpreter_not_isolated",
        lambda: entrypoints.resolve_runtime(
            environment["root"],
            probe=runner_returning(probe_report(environment["venv"], base_prefix=resolved)),
        ),
    )


def test_an_interpreter_reporting_another_prefix_is_refused(tmp_path: pathlib.Path) -> None:
    environment = scripted_environment(tmp_path)

    refused(
        "interpreter_prefix_mismatch",
        lambda: entrypoints.resolve_runtime(
            environment["root"],
            probe=runner_returning(
                probe_report(environment["venv"], prefix=str((tmp_path / "elsewhere").resolve()))
            ),
        ),
    )


def test_an_environment_without_the_pinned_sdk_is_refused(tmp_path: pathlib.Path) -> None:
    environment = scripted_environment(tmp_path)

    refused(
        "sdk_not_installed",
        lambda: entrypoints.resolve_runtime(
            environment["root"],
            probe=runner_returning(probe_report(environment["venv"], sdk=None)),
        ),
    )


def test_an_sdk_that_is_not_the_pin_is_refused(tmp_path: pathlib.Path) -> None:
    environment = scripted_environment(tmp_path)

    refusal = refused(
        "sdk_pin_mismatch",
        lambda: entrypoints.resolve_runtime(
            environment["root"],
            probe=runner_returning(probe_report(environment["venv"], sdk="1.27.0")),
        ),
    )

    assert version.SDK_PIN in refusal.detail


def test_a_lockfile_with_a_corrupt_frozen_section_is_refused(tmp_path: pathlib.Path) -> None:
    environment = scripted_environment(tmp_path)
    write_lockfile(environment["version_dir"], frozen=f"{version.SDK_PACKAGE}=={version.SDK_PIN}")

    refused(
        "lockfile_corrupt",
        lambda: entrypoints.resolve_runtime(environment["root"], probe=scripted_probe(environment)),
    )


# --- launch plan refusals ---------------------------------------------------


def test_an_activation_script_is_refused(tmp_path: pathlib.Path) -> None:
    environment = scripted_environment(tmp_path)

    refused(
        "activation_script_refused",
        lambda: entrypoints.launch_plan(
            entrypoints.MODE_STDIO,
            install_root=environment["root"],
            activation_script="activate.ps1",
        ),
    )


def test_a_path_lookup_is_refused(tmp_path: pathlib.Path) -> None:
    environment = scripted_environment(tmp_path)

    refused(
        "path_lookup_refused",
        lambda: entrypoints.launch_plan(
            entrypoints.MODE_STDIO, install_root=environment["root"], resolve_from_path=True
        ),
    )


def test_http_parameters_on_the_stdio_entrypoint_are_refused(tmp_path: pathlib.Path) -> None:
    environment = scripted_environment(tmp_path)

    refused(
        "http_parameters_on_stdio",
        lambda: entrypoints.launch_plan(
            entrypoints.MODE_STDIO,
            install_root=environment["root"],
            probe=scripted_probe(environment),
            port=8765,
        ),
    )


def test_a_working_directory_that_does_not_exist_is_refused(tmp_path: pathlib.Path) -> None:
    environment = scripted_environment(tmp_path)

    refused(
        "cwd_missing",
        lambda: entrypoints.launch_plan(
            entrypoints.MODE_STDIO,
            install_root=environment["root"],
            probe=scripted_probe(environment),
            cwd=tmp_path / "absent",
        ),
    )


# --- the HTTP opt-in --------------------------------------------------------


def http_call(
    environment: Mapping[str, pathlib.Path], **overrides: object
) -> Callable[[], entrypoints.LaunchPlan]:
    arguments: dict[str, object] = {
        "install_root": environment["root"],
        "probe": scripted_probe(environment),
        "port": 8765,
        "allow_hosts": [HOST_HEADER],
        "allow_origins": [ORIGIN_HEADER],
    }
    arguments.update(overrides)
    return lambda: entrypoints.launch_plan(entrypoints.MODE_HTTP, **arguments)


def test_the_http_entrypoint_binds_loopback_behind_an_explicit_allowlist(
    tmp_path: pathlib.Path,
) -> None:
    environment = scripted_environment(tmp_path)

    plan = http_call(environment)()

    assert plan.argv[1:3] == ("-m", "axiom_mcp.http")
    assert "--allow-non-loopback-bind" not in plan.argv
    assert plan.environment_map[entrypoints.ENVIRONMENT_MARKER] == entrypoints.MODE_HTTP
    host_position = list(plan.argv).index("--allow-host")
    assert plan.argv[host_position + 1] == HOST_HEADER


def test_a_transport_without_an_allowlist_is_refused(tmp_path: pathlib.Path) -> None:
    environment = scripted_environment(tmp_path)

    refused(
        "transport_allowlist_required",
        http_call(environment, allow_hosts=[], allow_origins=[]),
    )


def test_a_wildcard_allowlist_entry_is_refused(tmp_path: pathlib.Path) -> None:
    environment = scripted_environment(tmp_path)

    refused("wildcard_allowlist_refused", http_call(environment, allow_hosts=["*"]))


def test_a_non_loopback_bind_without_an_acknowledgement_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    environment = scripted_environment(tmp_path)

    refused(
        "non_loopback_bind_refused",
        http_call(environment, host="0.0.0.0", allow_hosts=["0.0.0.0:8765"]),
    )


def test_an_allowlist_that_does_not_admit_the_bind_host_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    """A gateway whose own allowlist refuses it would answer nothing."""
    environment = scripted_environment(tmp_path)

    refusal = refused(
        "bind_host_not_allowed", http_call(environment, allow_hosts=["127.0.0.1:9999"])
    )

    assert HOST_HEADER in refusal.detail


def test_an_allowlist_that_does_not_admit_the_bind_origin_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    environment = scripted_environment(tmp_path)

    refused(
        "bind_origin_not_allowed",
        http_call(environment, allow_origins=["http://127.0.0.1:9999"]),
    )


async def raw_request(
    app: object,
    *,
    method: str = "GET",
    path: str = http.HEALTH_PATH,
    host: str = HOST_HEADER,
    origin: str | None = None,
    authorization: str | None = None,
    payload: Mapping[str, object] | None = None,
) -> tuple[int, str]:
    headers: dict[str, str] = {"Host": host}
    if origin is not None:
        headers["Origin"] = origin
    if authorization is not None:
        headers["Authorization"] = authorization
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL, timeout=30.0
    ) as client:
        response = await client.request(method, path, headers=headers, json=payload)
    return response.status_code, response.text


def error_code(body: str) -> str | None:
    try:
        document = json.loads(body)
    except json.JSONDecodeError:
        return None
    return document.get("code") if isinstance(document, dict) else None


def build_test_gateway() -> http.GatewayApp:
    """The middleware the planned allowlist is handed to, built from that allowlist."""
    policy = security.SecurityPolicy.build(
        allowed_hosts=[HOST_HEADER], allowed_origins=[ORIGIN_HEADER]
    )
    registry = security.TokenRegistry()
    registry.register(
        security.EnvTokenReference(TOKEN_ENV_NAME),
        security.ScopedToken(
            token_id="v2-024-test",
            audience=security.MCP_AUDIENCE,
            capabilities=frozenset({security.CAPABILITY_READ}),
            solution_ids=frozenset({"v2-024-solution"}),
        ),
        env={TOKEN_ENV_NAME: TOKEN_SECRET},
    )
    server = sdk_compat.build_server(
        "v2-024-test", allowed_hosts=[HOST_HEADER], allowed_origins=[ORIGIN_HEADER]
    )
    return http.build_gateway(
        server,
        settings=http.HttpTransportSettings(host="127.0.0.1", port=8765),
        security=policy,
        authenticator=registry,
    )


def test_the_planned_allowlist_is_the_one_the_middleware_enforces(tmp_path: pathlib.Path) -> None:
    """The plan hands the transport security the same allowlist, so it must mean the same thing."""
    environment = scripted_environment(tmp_path)
    plan = http_call(environment)()

    assert list(plan.argv).count("--allow-host") == 1
    assert list(plan.argv).count("--allow-origin") == 1

    gateway = build_test_gateway()
    allowed, _ = asyncio.run(raw_request(gateway.app, host=HOST_HEADER, origin=ORIGIN_HEADER))
    rejected_host, host_body = asyncio.run(raw_request(gateway.app, host="127.0.0.1:9999"))
    rejected_origin, origin_body = asyncio.run(
        raw_request(gateway.app, origin="http://127.0.0.1:9999")
    )
    unauthenticated, auth_body = asyncio.run(
        raw_request(gateway.app, method="POST", path=http.MCP_ENDPOINT_PATH, payload={})
    )

    assert allowed == 200
    assert rejected_host == 403
    assert error_code(host_body) == "FORBIDDEN"
    assert rejected_origin == 403
    assert error_code(origin_body) == "FORBIDDEN"
    assert unauthenticated == 401
    assert error_code(auth_body) == "UNAUTHENTICATED"


# --- the recorded lock ------------------------------------------------------


def test_a_recorded_lock_describes_the_plans_until_something_moves(
    tmp_path: pathlib.Path, plans: Mapping[str, entrypoints.LaunchPlan]
) -> None:
    root = tmp_path / "lock root"
    root.mkdir()
    ordered = (plans[entrypoints.MODE_STDIO], plans[entrypoints.MODE_HTTP])

    written = entrypoints.write_lock(root, ordered)

    assert written == root / entrypoints.ENTRYPOINT_LOCK_FILENAME
    recorded = entrypoints.read_lock(root)
    assert recorded is not None
    assert recorded["layout"] == entrypoints.LOCK_LAYOUT_VERSION
    assert entrypoints.entrypoint_lock_reasons(recorded, ordered) == []

    tampered = dict(recorded)
    tampered["plans"] = [dict(entry) for entry in recorded["plans"]]
    tampered["plans"][0]["digest"] = "0" * 64
    # The recorded http entry is still expected here, so only the edited plan drifts.
    assert entrypoints.entrypoint_lock_reasons(tampered, ordered) == [
        f"lock_plan_digest_mismatch:{entrypoints.MODE_STDIO}",
    ]
    # A recorded entry no current plan claims is reported rather than ignored.
    assert entrypoints.entrypoint_lock_reasons(tampered, ordered[:1]) == [
        f"lock_plan_digest_mismatch:{entrypoints.MODE_STDIO}",
        f"lock_plan_unexpected:{entrypoints.MODE_HTTP}",
    ]


def test_an_install_root_without_a_lock_reads_as_absent(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "no lock"
    root.mkdir()

    assert entrypoints.read_lock(root) is None


def test_a_lock_without_plans_is_refused(tmp_path: pathlib.Path) -> None:
    refused("no_plans", lambda: entrypoints.write_lock(tmp_path, ()))


def test_two_plans_for_one_mode_are_refused(
    tmp_path: pathlib.Path, plans: Mapping[str, entrypoints.LaunchPlan]
) -> None:
    duplicated = (plans[entrypoints.MODE_STDIO], plans[entrypoints.MODE_STDIO])

    refusal = refused("plan_mode_duplicated", lambda: entrypoints.write_lock(tmp_path, duplicated))

    assert entrypoints.MODE_STDIO in refusal.detail


def test_plans_from_two_runtimes_are_refused(tmp_path: pathlib.Path) -> None:
    first_root = tmp_path / "first root"
    second_root = tmp_path / "second root"
    first = scripted_environment(first_root)
    second = scripted_environment(second_root)
    stdio_plan = entrypoints.launch_plan(
        entrypoints.MODE_STDIO, install_root=first_root, probe=scripted_probe(first)
    )
    http_plan = http_call(second)()

    refused(
        "runtime_mismatch",
        lambda: entrypoints.write_lock(tmp_path, (stdio_plan, http_plan)),
    )


# --- the module's own console surface ---------------------------------------


def test_the_module_cli_prints_one_launch_document(
    locked: Mapping[str, pathlib.Path], capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = entrypoints.main(
        ["plan", "--mode", entrypoints.MODE_STDIO, "--install-root", str(locked["root"])]
    )

    assert exit_code == entrypoints.EXIT_SUCCESS
    document = json.loads(capsys.readouterr().out)
    assert document["command"] == str(locked["interpreter"].resolve())
    assert document["args"] == ["-m", "axiom_mcp.stdio", "--name", version.COMPONENT]


def test_the_module_cli_records_and_then_verifies_the_lock(
    locked: Mapping[str, pathlib.Path], capsys: pytest.CaptureFixture[str]
) -> None:
    recorded = entrypoints.main(
        [
            "plan",
            "--mode",
            entrypoints.MODE_STDIO,
            "--install-root",
            str(locked["root"]),
            "--write-lock",
        ]
    )
    capsys.readouterr()

    verified = entrypoints.main(
        ["verify", "--mode", entrypoints.MODE_STDIO, "--install-root", str(locked["root"])]
    )

    assert recorded == entrypoints.EXIT_SUCCESS
    assert verified == entrypoints.EXIT_SUCCESS
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_the_module_cli_reports_an_absent_lock_as_a_validation_failure(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "cli root"
    root.mkdir()

    exit_code = entrypoints.main(
        ["verify", "--mode", entrypoints.MODE_STDIO, "--install-root", str(root)]
    )

    assert exit_code == entrypoints.EXIT_VALIDATION
    assert json.loads(capsys.readouterr().out) == {"ok": False, "reasons": ["lock_absent"]}


def test_the_module_cli_reports_a_refusal_with_its_code(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = entrypoints.main(
        ["plan", "--mode", entrypoints.MODE_STDIO, "--install-root", str(tmp_path / "absent")]
    )

    payload = json.loads(capsys.readouterr().err)
    assert exit_code == entrypoints.EXIT_VALIDATION
    assert payload["ok"] is False
    assert payload["code"] == "install_root_missing"


def test_the_module_entrypoint_is_reachable_as_a_process(
    locked: Mapping[str, pathlib.Path],
) -> None:
    """The exact target ``pyproject.toml`` declares is also reachable as a module.

    A console script and ``python -m axiom_mcp.entrypoints`` call the same ``main``,
    so proving the module entry answers is what makes the declared script honest.
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "axiom_mcp.entrypoints",
            "plan",
            "--mode",
            entrypoints.MODE_STDIO,
            "--install-root",
            str(locked["root"]),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONPATH": str(SRC_ROOT)},
        cwd=str(REPO_ROOT),
        timeout=entrypoints.DEFAULT_TIMEOUT_S,
        check=False,
    )

    assert completed.returncode == entrypoints.EXIT_SUCCESS, completed.stderr
    document = json.loads(completed.stdout)
    assert document["command"] == str(locked["interpreter"].resolve())
    assert document["args"] == ["-m", "axiom_mcp.stdio", "--name", version.COMPONENT]


def test_the_declared_console_script_names_the_module_entrypoint() -> None:
    """A declared script whose target does not exist installs a command that answers nothing."""
    with open(REPO_ROOT / "pyproject.toml", "rb") as handle:
        document = tomllib.load(handle)

    scripts = document["project"]["scripts"]

    assert scripts["axiom-mcp-entrypoints"] == "axiom_mcp.entrypoints:main"
    assert scripts["axiom-mcp"].split(":")[0].startswith("axiom_mcp.")


# --- the metadata read and the remaining helpers ----------------------------


@pytest.mark.parametrize(
    ("encoding", "home"),
    [
        ("cp874", "C:\\Python313\\\u0e17\u0e14\u0e2a\u0e2d\u0e1a"),
        ("cp1252", "C:\\Python313\\caf\xe9"),
    ],
)
def test_version_metadata_survives_a_non_utf8_code_page(
    tmp_path: pathlib.Path, encoding: str, home: str
) -> None:
    """A virtual environment created at a non-ASCII path records the host code page."""
    venv_dir = tmp_path / "venv"
    venv_dir.mkdir()
    payload = f"home = {home}\ninclude-system-site-packages = true\n"
    assert home.encode(encoding) != home.encode("utf-8")
    (venv_dir / "pyvenv.cfg").write_bytes(payload.encode(encoding))

    metadata = entrypoints.read_venv_metadata(venv_dir)

    assert entrypoints.shared_base_packages(metadata) is True
    assert metadata["home"]


def test_an_empty_metadata_document_is_refused(tmp_path: pathlib.Path) -> None:
    venv_dir = tmp_path / "venv"
    venv_dir.mkdir()
    (venv_dir / "pyvenv.cfg").write_bytes(b"")

    refused("venv_metadata_corrupt", lambda: entrypoints.read_venv_metadata(venv_dir))


def test_a_version_name_that_is_not_a_directory_name_is_refused() -> None:
    refused("version_invalid", lambda: entrypoints.validate_version("../escape"))
    refused("version_invalid", lambda: entrypoints.validate_version(""))


def test_the_version_directory_is_derived_from_the_locked_layout(tmp_path: pathlib.Path) -> None:
    directory = entrypoints.version_directory(tmp_path, SCRIPTED_VERSION)

    assert directory == tmp_path / entrypoints.VERSIONS_DIRNAME / SCRIPTED_VERSION


def test_the_interpreter_of_a_venv_is_named_per_platform(tmp_path: pathlib.Path) -> None:
    interpreter = entrypoints.venv_python(tmp_path / "venv")

    assert interpreter.name == ("python.exe" if os.name == "nt" else "python")
    assert interpreter.parent == tmp_path / "venv" / venv_scripts(tmp_path / "venv")
