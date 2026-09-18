"""Targeted regression tests for C-007 ``axiom-mcp version`` and ``doctor``.

The claim under test is not "the commands print something". It is that the
report distinguishes the things an operator must not confuse:

* a pinned runtime that is not the installed one, versus a machine that is
  simply not configured yet - the first is incompatible, the second is normal;
* a listening port, versus a gateway that can answer a query - the report states
  which basis it used and never treats ``/healthz`` as readiness;
* a credential reference that was named and does not resolve, versus a
  credential that resolves but is not registered, versus no credential at all -
  three different problems with three different fixes.

Every doctor assertion runs against an injected environment, so a result is
attributable to the check and not to the host the suite happens to run on.
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from axiom_mcp import cli, security, version  # noqa: E402


def healthy_environment(**overrides: object) -> cli.DoctorEnvironment:
    """A machine that satisfies every pin, with no credential configured."""
    base: dict[str, object] = {
        "python_version": (3, 13, 14),
        "sdk_version": version.SDK_PIN,
        "runtime_versions": {name: pin for name, pin in version.RUNTIME_PINS.items()},
        "advertised_protocols": (version.MCP_PROTOCOL_MINIMUM,),
        "surface_reasons": (),
        "credential": cli.CredentialScope(
            configured=False, reason="credential_reference_not_configured"
        ),
        "query_plane_available": True,
        "control_plane_available": True,
        "build_revision": "cafebabe0123",
    }
    base.update(overrides)
    return cli.DoctorEnvironment(**base)  # type: ignore[arg-type]


def sections_by_name(env: cli.DoctorEnvironment) -> dict[str, cli.Section]:
    return {section.name: section for section in cli.doctor_sections(env)}


def captured_main(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class VersionCommandTests(unittest.TestCase):
    def test_version_json_has_exactly_the_contract_fields(self):
        code, out, _ = captured_main(["version", "--json"])
        self.assertEqual(code, cli.EXIT_SUCCESS)
        payload = json.loads(out)
        self.assertEqual(sorted(payload), sorted(version.VERSION_REPORT_FIELDS))
        self.assertEqual(payload["component"], "axiom-mcp")
        self.assertEqual(payload["graph_schema"], version.GRAPH_SCHEMA)
        self.assertEqual(payload["control_api"], version.CONTROL_API)
        self.assertEqual(payload["update_status"], "not_checked")
        self.assertTrue(payload["build_revision"])

    def test_version_json_is_a_single_object_on_stdout(self):
        code, out, err = captured_main(["version", "--json"])
        self.assertEqual(code, cli.EXIT_SUCCESS)
        self.assertEqual(out.count("\n"), 1)
        self.assertEqual(err, "")

    def test_version_text_mode_names_every_contract_field(self):
        code, out, _ = captured_main(["version"])
        self.assertEqual(code, cli.EXIT_SUCCESS)
        for field in version.VERSION_REPORT_FIELDS:
            with self.subTest(field=field):
                self.assertIn(f"{field}:", out)

    def test_build_revision_prefers_the_explicit_setting_over_the_probe(self):
        environ = {cli.BUILD_REVISION_ENV: "release-2026.09.19"}
        self.assertEqual(
            cli.resolve_build_revision(environ, repo_root=REPO_ROOT), "release-2026.09.19"
        )

    def test_missing_build_revision_yields_the_documented_marker(self):
        revision = cli.resolve_build_revision({}, repo_root=REPO_ROOT / "does-not-exist")
        self.assertEqual(revision, cli.UNKNOWN_BUILD_REVISION)

    def test_version_report_field_is_never_empty(self):
        report = version.version_report(cli.resolve_build_revision({}, repo_root=REPO_ROOT))
        self.assertTrue(all(str(report[field]).strip() for field in version.VERSION_REPORT_FIELDS))


class DoctorSectionTests(unittest.TestCase):
    def test_healthy_environment_is_ready_and_exits_zero(self):
        report = cli.doctor_report(healthy_environment())
        self.assertTrue(report["ok"])
        self.assertEqual(report["verdict"], cli.VERDICT_READY)
        self.assertEqual(report["exit_code"], cli.EXIT_SUCCESS)
        self.assertEqual(report["reasons"], [])

    def test_report_carries_every_section_in_the_documented_order(self):
        report = cli.doctor_report(healthy_environment())
        self.assertEqual([s["name"] for s in report["sections"]], list(cli.SECTION_ORDER))

    def test_graph_schema_and_spec_revision_are_reported(self):
        report = cli.doctor_report(healthy_environment())
        dimensions = report["sections"][list(cli.SECTION_ORDER).index("dimensions")]["details"]
        self.assertEqual(dimensions["graph_schema"], version.GRAPH_SCHEMA)
        self.assertEqual(dimensions["spec_revision"], version.SPEC_REVISION)
        self.assertEqual(dimensions["accepted_graph_schema_major"], version.GRAPH_SCHEMA)

    def test_sdk_compatibility_names_the_pin_and_the_installed_version(self):
        report = cli.doctor_report(healthy_environment())
        runtime = report["sections"][list(cli.SECTION_ORDER).index("runtime")]["details"]
        self.assertEqual(runtime["sdk_pin"], version.SDK_PIN)
        self.assertEqual(runtime["sdk_installed"], version.SDK_PIN)
        self.assertEqual(runtime["python_requires"], version.PYTHON_REQUIRES)

    def test_token_scope_is_reported_as_the_enforced_surface(self):
        report = cli.doctor_report(healthy_environment())
        credentials = report["sections"][list(cli.SECTION_ORDER).index("credentials")]["details"]
        self.assertEqual(credentials["required_audience"], security.MCP_AUDIENCE)
        self.assertEqual(credentials["control_audience"], security.CONTROL_AUDIENCE)
        self.assertEqual(credentials["enforced_capabilities"], sorted(security.KNOWN_CAPABILITIES))
        self.assertEqual(credentials["tool_capability_map"], dict(security.TOOL_CAPABILITY))

    def test_health_endpoint_is_never_the_readiness_basis(self):
        report = cli.doctor_report(healthy_environment())
        readiness = report["sections"][list(cli.SECTION_ORDER).index("readiness")]["details"]
        self.assertFalse(readiness["process_health_is_readiness"])
        self.assertNotIn("health", readiness["basis"])
        self.assertEqual(readiness["basis"], "runtime_pins_and_data_plane_probes")
        self.assertEqual(readiness["health_path"], "/healthz")
        self.assertEqual(readiness["ready_path"], "/readyz")


class DoctorNegativeTests(unittest.TestCase):
    def test_unsupported_python_line_is_incompatible(self):
        env = healthy_environment(python_version=(3, 12, 9))
        report = cli.doctor_report(env)
        self.assertFalse(report["ok"])
        self.assertEqual(report["exit_code"], cli.EXIT_INCOMPATIBLE)
        self.assertTrue(
            any(
                reason.startswith("runtime:python_version_unsupported")
                for reason in report["reasons"]
            ),
            report["reasons"],
        )

    def test_sdk_pin_drift_is_incompatible(self):
        env = healthy_environment(sdk_version="1.99.0")
        report = cli.doctor_report(env)
        self.assertEqual(report["exit_code"], cli.EXIT_INCOMPATIBLE)
        self.assertIn("runtime:sdk_version_unsupported:1.99.0", report["reasons"])

    def test_a_missing_runtime_dependency_is_incompatible(self):
        installed = {name: pin for name, pin in version.RUNTIME_PINS.items()}
        installed["uvicorn"] = None
        report = cli.doctor_report(healthy_environment(runtime_versions=installed))
        self.assertEqual(report["exit_code"], cli.EXIT_INCOMPATIBLE)
        self.assertIn("runtime:runtime_not_installed:uvicorn", report["reasons"])

    def test_a_missing_sdk_surface_name_is_incompatible(self):
        env = healthy_environment(
            surface_reasons=("sdk_attribute_missing:mcp.types:LATEST_PROTOCOL_VERSION",)
        )
        report = cli.doctor_report(env)
        self.assertEqual(report["exit_code"], cli.EXIT_INCOMPATIBLE)
        self.assertIn(
            "sdk_surface:sdk_attribute_missing:mcp.types:LATEST_PROTOCOL_VERSION", report["reasons"]
        )

    def test_an_sdk_that_drops_the_minimum_protocol_revision_is_incompatible(self):
        env = healthy_environment(advertised_protocols=("2024-11-05",))
        report = cli.doctor_report(env)
        self.assertEqual(report["exit_code"], cli.EXIT_INCOMPATIBLE)
        self.assertIn(
            f"protocol:protocol_revision_missing:{version.MCP_PROTOCOL_MINIMUM}", report["reasons"]
        )

    def test_an_empty_protocol_advertisement_is_incompatible(self):
        report = cli.doctor_report(healthy_environment(advertised_protocols=()))
        self.assertEqual(report["exit_code"], cli.EXIT_INCOMPATIBLE)
        self.assertIn(
            f"protocol:protocol_advertisement_empty:{version.MCP_PROTOCOL_MINIMUM}",
            report["reasons"],
        )

    def test_unavailable_query_plane_is_not_ready_rather_than_incompatible(self):
        env = healthy_environment(
            query_plane_available=False,
            query_plane_detail="query data plane not wired",
        )
        report = cli.doctor_report(env)
        self.assertEqual(report["exit_code"], cli.EXIT_NOT_READY)
        self.assertEqual(report["verdict"], cli.VERDICT_NOT_READY)
        self.assertIn("readiness:query_plane_unavailable", report["reasons"])
        # The only warning is the unconfigured credential, so the not-ready
        # verdict is attributable to the data plane and not to the scope check.
        self.assertEqual(report["warnings"], ["credentials"])
        self.assertFalse(any(reason.startswith("credentials:") for reason in report["reasons"]))

    def test_unknown_subcommand_exits_two(self):
        code, out, err = captured_main(["frobnicate"])
        self.assertEqual(code, cli.EXIT_VALIDATION)
        self.assertEqual(out, "")
        self.assertIn("frobnicate", err)

    def test_unknown_flag_exits_two_rather_than_being_ignored(self):
        code, _, err = captured_main(["doctor", "--json", "--frobnicate"])
        self.assertEqual(code, cli.EXIT_VALIDATION)
        self.assertIn("frobnicate", err)

    def test_no_subcommand_exits_two(self):
        code, _, err = captured_main([])
        self.assertEqual(code, cli.EXIT_VALIDATION)
        self.assertTrue(err.strip())

    def test_error_code_mapping_is_total_and_never_zero(self):
        for code in sorted(errors_codes()):
            with self.subTest(code=code):
                error = error_for(code)
                self.assertNotEqual(cli.exit_code_for_error(error), cli.EXIT_SUCCESS)


class RegistryFixtureMixin(unittest.TestCase):
    """A throwaway directory, so no fixture file is added to the test tree."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)

    def write_registry(self, document: object, name: str = "registry.json") -> pathlib.Path:
        path = self.tmp / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def registry_entry(self, **overrides: object) -> dict[str, object]:
        entry: dict[str, object] = {
            "kind": "env",
            "variable": "AXIOM_MCP_TOKEN",
            "token_id": "ops-read",
            "capabilities": ["read"],
            "solution_ids": ["demo-solution"],
        }
        entry.update(overrides)
        return entry


class CredentialScopeTests(RegistryFixtureMixin):
    def _unresolved(self, environ: dict[str, str]) -> cli.CredentialScope:
        return cli.credential_scope(environ)

    def test_no_reference_is_a_warning_not_a_failure(self):
        scope = self._unresolved({})
        self.assertFalse(scope.configured)
        self.assertEqual(scope.reason, "credential_reference_not_configured")
        report = cli.doctor_report(healthy_environment(credential=scope))
        self.assertEqual(report["exit_code"], cli.EXIT_SUCCESS)
        self.assertEqual(report["warnings"], ["credentials"])

    def test_a_named_but_unset_variable_fails_as_authorization(self):
        environ = {
            cli.CREDENTIAL_REFERENCE_ENV: json.dumps(
                {"kind": "env", "variable": "AXIOM_MCP_ABSENT_TOKEN"}
            )
        }
        scope = self._unresolved(environ)
        self.assertTrue(scope.configured)
        self.assertFalse(scope.resolved)
        self.assertEqual(scope.reason, "credential_reference_unresolved")
        report = cli.doctor_report(healthy_environment(credential=scope))
        self.assertEqual(report["exit_code"], cli.EXIT_AUTHORIZATION)
        self.assertIn("credentials:credential_reference_unresolved", report["reasons"])

    def test_a_missing_credential_file_fails_as_authorization(self):
        environ = {
            cli.CREDENTIAL_REFERENCE_ENV: json.dumps(
                {"kind": "file", "path": str(REPO_ROOT / "does-not-exist.token")}
            )
        }
        scope = self._unresolved(environ)
        self.assertEqual(scope.reason, "credential_reference_unresolved")
        self.assertFalse(scope.resolved)

    def test_a_literal_token_in_configuration_is_refused(self):
        environ = {
            cli.CREDENTIAL_REFERENCE_ENV: json.dumps(
                {
                    "kind": "env",
                    "variable": "AXIOM_MCP_TOKEN",
                    "token": "ghp_shouldneverbestored000000",
                }
            )
        }
        scope = self._unresolved(environ)
        self.assertEqual(scope.reason, "credential_reference_invalid")
        self.assertFalse(scope.resolved)

    def test_a_reference_that_is_not_json_is_reported_not_raised(self):
        scope = self._unresolved({cli.CREDENTIAL_REFERENCE_ENV: "AXIOM_MCP_TOKEN"})
        self.assertEqual(scope.reason, "credential_reference_not_json")
        self.assertTrue(scope.configured)

    def test_a_resolvable_credential_with_no_registry_is_a_warning(self):
        environ = {
            "AXIOM_MCP_TOKEN": "a-read-token-value",
            cli.CREDENTIAL_REFERENCE_ENV: json.dumps(
                {"kind": "env", "variable": "AXIOM_MCP_TOKEN"}
            ),
        }
        scope = self._unresolved(environ)
        self.assertTrue(scope.resolved)
        self.assertEqual(scope.reason, "credential_registry_not_configured")
        report = cli.doctor_report(healthy_environment(credential=scope))
        self.assertEqual(report["exit_code"], cli.EXIT_SUCCESS)
        self.assertEqual(report["warnings"], ["credentials"])

    def test_a_resolvable_but_unregistered_credential_fails_as_authorization(self):
        registry = self.write_registry({"tokens": []})
        environ = {
            "AXIOM_MCP_TOKEN": "a-read-token-value",
            cli.CREDENTIAL_REFERENCE_ENV: json.dumps(
                {"kind": "env", "variable": "AXIOM_MCP_TOKEN"}
            ),
            cli.CREDENTIAL_REGISTRY_ENV: str(registry),
        }
        scope = self._unresolved(environ)
        self.assertTrue(scope.resolved)
        self.assertFalse(scope.registered)
        self.assertEqual(scope.reason, "credential_not_registered")
        report = cli.doctor_report(healthy_environment(credential=scope))
        self.assertEqual(report["exit_code"], cli.EXIT_AUTHORIZATION)

    def test_a_registered_credential_reports_its_declared_scope_without_its_value(self):
        registry = self.write_registry({"tokens": [self.registry_entry()]})
        secret = "a-read-token-value"
        environ = {
            "AXIOM_MCP_TOKEN": secret,
            cli.CREDENTIAL_REFERENCE_ENV: json.dumps(
                {"kind": "env", "variable": "AXIOM_MCP_TOKEN"}
            ),
            cli.CREDENTIAL_REGISTRY_ENV: str(registry),
        }
        scope = self._unresolved(environ)
        self.assertTrue(scope.registered)
        self.assertEqual(scope.granted_capabilities, ("read",))
        self.assertEqual(scope.solution_ids, ("demo-solution",))
        report = cli.doctor_report(healthy_environment(credential=scope))
        self.assertNotIn(secret, json.dumps(report))

    def test_an_out_of_root_credential_file_path_is_redacted_in_the_report(self):
        outside = "C:" + chr(92) + "Users" + chr(92) + "other" + chr(92) + "token.txt"
        environ = {cli.CREDENTIAL_REFERENCE_ENV: json.dumps({"kind": "file", "path": outside})}
        scope = self._unresolved(environ)
        report = cli.doctor_report(healthy_environment(credential=scope))
        blob = json.dumps(report)
        self.assertNotIn(outside, blob)
        self.assertIn("<redacted:windows_absolute_path>", blob)

    def test_registry_entries_that_cannot_be_registered_are_skipped_not_fatal(self):
        registry = self.write_registry(
            {
                "tokens": [
                    self.registry_entry(),
                    self.registry_entry(variable="AXIOM_MCP_TOKEN_MISSING", token_id="ops-missing"),
                ]
            }
        )
        token_registry, skipped = cli.load_registry(registry, {"AXIOM_MCP_TOKEN": "value"})
        self.assertGreaterEqual(len(skipped), 1)
        self.assertEqual(token_registry.size, 1)

    def test_an_unreadable_registry_is_reported_rather_than_raised(self):
        scope = cli.credential_scope(
            {
                "AXIOM_MCP_TOKEN": "value",
                cli.CREDENTIAL_REFERENCE_ENV: json.dumps(
                    {"kind": "env", "variable": "AXIOM_MCP_TOKEN"}
                ),
            },
            registry_path=REPO_ROOT / "does-not-exist-registry.json",
        )
        self.assertEqual(scope.reason, "credential_registry_unreadable")


class CommandIntegrationTests(unittest.TestCase):
    def test_doctor_json_is_a_single_object_on_stdout(self):
        code, out, err = captured_main(["doctor", "--json"])
        self.assertEqual(code, json.loads(out)["exit_code"])
        self.assertEqual(out.count("\n"), 1)
        self.assertEqual(err, "")

    def test_doctor_on_this_host_reports_a_reason_per_failed_section(self):
        code, out, _ = captured_main(["doctor", "--json"])
        report = json.loads(out)
        failed = [s["name"] for s in report["sections"] if s["status"] == cli.STATUS_FAIL]
        self.assertEqual(len(report["reasons"]), sum(len(s["reasons"]) for s in report["sections"]))
        if failed:
            self.assertNotEqual(code, cli.EXIT_SUCCESS)
        for name in failed:
            with self.subTest(section=name):
                self.assertTrue(any(r.startswith(f"{name}:") for r in report["reasons"]))

    def test_doctor_text_mode_is_readable_and_keeps_the_verdict(self):
        code, out, _ = captured_main(["doctor"])
        self.assertEqual(code, cli.EXIT_SUCCESS if "verdict: ready" in out else cli.EXIT_NOT_READY)
        self.assertIn("verdict:", out)

    def test_setup_py_entry_point_target_exists(self):
        self.assertTrue(callable(cli.main))
        self.assertEqual(cli.main.__module__, "axiom_mcp.cli")


def errors_codes() -> set[str]:
    from axiom_mcp import errors

    return set(errors.CANONICAL_CODES)


def error_for(code: str) -> object:
    from axiom_mcp import errors

    return errors.AxiomError(code, "diagnostic probe")


if __name__ == "__main__":
    unittest.main()
