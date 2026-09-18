"""C-001 regression test: the runtime pin is real, not a claim.

The test compares three sources that can only agree if the pin is honest:
``pyproject.toml`` (what an installer would resolve), ``axiom_mcp.version``
(what the component reports) and the installed interpreter/SDK (what actually
runs). The negative and boundary cases prove the compatibility spike rejects a
runtime and an SDK surface that drift from the recorded pin instead of trusting
a legacy method name.
"""

from __future__ import annotations

import importlib.metadata
import pathlib
import sys
import tomllib
import types

import pytest

from axiom_mcp import version

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"


@pytest.fixture(scope="module")
def project() -> dict:
    return tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))


def test_pyproject_locks_the_python_line_and_component_version(project: dict) -> None:
    assert project["project"]["requires-python"] == version.PYTHON_REQUIRES
    assert project["project"]["version"] == version.VERSION
    assert version.python_supported() is True


def test_pyproject_pins_every_runtime_dependency_exactly(project: dict) -> None:
    declared = {
        item.split("==", 1)[0]: item.split("==", 1)[1]
        for item in project["project"]["dependencies"]
    }
    assert declared == version.RUNTIME_PINS
    assert f"{version.SDK_PACKAGE}=={version.SDK_PIN}" in project["project"]["dependencies"]
    dev = {
        item.split("==", 1)[0]: item.split("==", 1)[1]
        for item in project["project"]["optional-dependencies"]["dev"]
    }
    assert dev == version.DEV_PINS


def test_pyproject_declares_the_console_entry_point(project: dict) -> None:
    assert project["project"]["scripts"]["axiom-mcp"] == "axiom_mcp.cli:main"


def test_pyproject_configures_the_required_check(project: dict) -> None:
    pytest_config = project["tool"]["pytest"]["ini_options"]
    assert pytest_config["testpaths"] == ["tests"]
    assert "src" in pytest_config["pythonpath"]
    assert project["tool"]["ruff"]["target-version"] == "py313"


def test_installed_sdk_version_equals_the_pin() -> None:
    installed = importlib.metadata.version(version.SDK_PACKAGE)
    assert installed == version.SDK_PIN
    assert version.sdk_compatibility_reasons() == []


def test_recorded_sdk_surface_still_matches_the_installed_sdk() -> None:
    assert version.sdk_surface_reasons() == []


def test_installed_sdk_advertises_the_minimum_protocol_revision() -> None:
    advertised = version.advertised_protocol_versions()
    assert version.MCP_PROTOCOL_MINIMUM in advertised
    assert version.protocol_support_reasons(advertised) == []


def test_every_installed_runtime_distribution_matches_its_pin() -> None:
    assert version.runtime_pin_reasons() == []
    installed = version.installed_versions()
    assert installed == version.RUNTIME_PINS


def test_version_report_matches_the_canonical_contract_shape() -> None:
    report = version.version_report("deadbeef", update_status="not_checked")
    assert tuple(report) == version.VERSION_REPORT_FIELDS
    assert report["component"] == "axiom-mcp"
    assert report["graph_schema"] == 1
    assert report["control_api"] == 1
    assert report["queue_schema"] == 1
    with pytest.raises(ValueError):
        version.version_report("deadbeef", update_status="pretend-latest")
    with pytest.raises(ValueError):
        version.version_report("")


# --- negative and boundary cases -------------------------------------------


def test_sdk_compatibility_rejects_a_different_sdk_and_python() -> None:
    older = version.sdk_compatibility_reasons("1.28.0", "3.13.14")
    newer = version.sdk_compatibility_reasons("2.0.0", "3.13.14")
    wrong_python = version.sdk_compatibility_reasons("1.28.1", "3.12.9")
    too_new_python = version.sdk_compatibility_reasons("1.28.1", "3.14.0")
    assert "sdk_version_unsupported:1.28.0" in older
    assert "sdk_version_unsupported:2.0.0" in newer
    assert "python_version_unsupported:3.12.9" in wrong_python
    assert "python_version_unsupported:3.14.0" in too_new_python
    assert all([older, newer, wrong_python, too_new_python])


def test_sdk_compatibility_boundary_accepts_the_locked_edges() -> None:
    assert version.sdk_compatibility_reasons("1.28.1", "3.13.0") == []
    assert version.sdk_compatibility_reasons("1.28.1", (3, 13, 99)) == []
    assert version.python_supported((3, 13)) is True
    assert version.python_supported((3, 12)) is False
    assert version.python_supported((3, 14)) is False


def test_protocol_support_reasons_reject_a_missing_revision() -> None:
    assert version.protocol_support_reasons([]) == ["protocol_advertisement_empty:2025-11-25"]
    assert version.protocol_support_reasons(["2024-11-05", "2025-06-18"]) == [
        "protocol_revision_missing:2025-11-25"
    ]
    assert version.protocol_support_reasons(["2025-11-25", "2025-06-18"]) == []


def test_spike_detects_an_sdk_that_only_exposes_legacy_names() -> None:
    """A legacy-only SDK must fail the spike instead of being trusted."""

    class LegacyFastMCP:
        def sse_app(self) -> None:  # pragma: no cover - shape probe only
            return None

    legacy = types.SimpleNamespace(FastMCP=LegacyFastMCP)
    stub = types.SimpleNamespace()

    def resolver(name: str) -> object:
        if name == "mcp.server.fastmcp":
            return legacy
        return stub

    reasons = version.sdk_surface_reasons(resolver=resolver)
    assert "sdk_attribute_missing:mcp.server.fastmcp:FastMCP.streamable_http_app" in reasons
    assert "sdk_attribute_missing:mcp.server.fastmcp:FastMCP.run_stdio_async" in reasons
    assert not any("sse_app" in reason for reason in reasons)


def test_spike_detects_a_missing_pinned_parameter() -> None:
    class FastMCP:
        def __init__(self, name: str | None = None) -> None:
            self.name = name

        def streamable_http_app(self) -> object:
            return object()

        def run_stdio_async(self) -> None:
            return None

        @property
        def session_manager(self) -> object:
            return object()

    def resolver(name: str) -> object:
        if name == "mcp.server.fastmcp":
            return types.SimpleNamespace(FastMCP=FastMCP)
        if name == "mcp.server.transport_security":
            return types.SimpleNamespace(
                TransportSecuritySettings=type(
                    "TransportSecuritySettings",
                    (),
                    {
                        "model_fields": {
                            "enable_dns_rebinding_protection": None,
                            "allowed_hosts": None,
                        }
                    },
                )
            )
        return types.SimpleNamespace(
            SUPPORTED_PROTOCOL_VERSIONS=(), LATEST_PROTOCOL_VERSION="", Server=object
        )

    reasons = version.sdk_surface_reasons(resolver=resolver)
    assert "sdk_parameter_missing:mcp.server.fastmcp:FastMCP.__init__:lifespan" in reasons
    assert (
        "sdk_field_missing:mcp.server.transport_security:TransportSecuritySettings:allowed_origins"
        in reasons
    )


def test_python_supported_rejects_unparsable_input() -> None:
    assert version.python_supported("") is False
    assert version.python_supported("3") is False
    assert version.python_supported(sys.version.split()[0]) is True
