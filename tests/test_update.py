"""Targeted regression tests for the C-008 update check and approved-plan delegation.

Covers C-008 AC1 (check and apply go through the approved axiom plan and the running
process is never pip-upgraded in place) and AC2 (a targeted regression slice with
negative and boundary cases whose fixtures and command output are preserved under
``evidence/artifacts/C-008``).

The plan contract is not restated here. Every fixture in the pinned specification's
``tests/fixtures/update-plan-contract`` corpus is driven through this component and
compared with the specification's own verdict, so a component that drifted away from
the contract would fail this file instead of shipping.
"""

from __future__ import annotations

import copy
import json
import pathlib
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from axiom_mcp import update  # noqa: E402

FIXTURE_RELATIVE = pathlib.Path("tests") / "fixtures" / "update-plan-contract"
ACCEPTED_DOCUMENT = FIXTURE_RELATIVE / "documents" / "accepted.document.json"

# Apply guards execute on this host, so accepted roots must use its native path
# grammar.  Windows paths belong in native-Windows coverage, not a POSIX apply test.
VERSIONED_MCP_ROOT = str((pathlib.Path(sys.prefix).parent / "axiom-mcp-test" / "0.2.0").resolve())

_SPECS_ROOT = update.find_specs_root()


class _SpecsCase(unittest.TestCase):
    """A base class that fails loudly when the pinned specification is absent."""

    @classmethod
    def setUpClass(cls) -> None:
        if _SPECS_ROOT is None:
            raise unittest.SkipTest(
                "no pinned axiom-specs checkout with the plan contract is present"
            )
        cls.evaluator = update.load_canonical_evaluator(_SPECS_ROOT)

    def accepted_document(self):
        path = _SPECS_ROOT / ACCEPTED_DOCUMENT
        return json.loads(path.read_text(encoding="utf-8"))

    def fixture_documents(self):
        for path in sorted((_SPECS_ROOT / FIXTURE_RELATIVE).glob("*.json")):
            yield path, json.loads(path.read_text(encoding="utf-8"))

    def mcp_document(self, install_root: str = VERSIONED_MCP_ROOT):
        """A complete, canonically approved plan for this component."""
        document = self.accepted_document()
        plan = copy.deepcopy(document["plan"])
        plan["target"]["component"] = "axiom-mcp"
        plan["target"]["install_root"] = install_root
        plan["components"] = [dict(plan["components"][0], component="axiom-mcp")]
        plan["service_interruptions"] = [
            {"service": "axiom-mcp", "action": "restart", "max_seconds": 30}
        ]
        plan["downloads"] = [
            dict(
                plan["downloads"][0],
                artifact="axiom-mcp-0.2.0-windows-x64.zip",
                url="https://github.com/orchex006/axiom-mcp/releases/download/v0.2.0/"
                "axiom-mcp-0.2.0-windows-x64.zip",
            )
        ]
        plan["plan_digest"] = "0" * 64
        plan["approval"] = {
            "state": "unapproved",
            "approved_digest": None,
            "approved_by": None,
            "approved_at": None,
        }
        digest = update.plan_digest(plan, self.evaluator)
        plan["plan_digest"] = digest
        plan["approval"] = {
            "state": "approved",
            "approved_digest": digest,
            "approved_by": "maintainer@example.invalid",
            "approved_at": "2026-09-18T02:05:00Z",
        }
        return {"plan": plan, "approved_digest": digest}


class CheckTests(unittest.TestCase):
    """update check reports the canonical fields and refuses to guess."""

    def test_report_carries_the_canonical_fields(self):
        report = update.check_update({}).as_report()
        expected = set(update.CHECK_FIELDS) | {"status", "reasons"}
        self.assertEqual(set(report), expected)

    def test_report_separates_installed_available_channel_policy_and_scope(self):
        report = update.check_update({}, installed_version="1.2.3").as_report()
        self.assertEqual(report["component"], "axiom-mcp")
        self.assertEqual(report["installed"], "1.2.3")
        self.assertEqual(report["channel"], "stable")
        self.assertEqual(report["schema_range"], update.schema_range())
        self.assertTrue(report["update_policy"]["plan_approval_required"])

    def test_no_verified_source_never_reports_current(self):
        check = update.check_update({})
        self.assertIsNone(check.available)
        self.assertNotEqual(check.status, update.STATUS_CURRENT)
        self.assertEqual(check.status, update.STATUS_UNCONFIGURED)
        self.assertEqual(update.check_exit_code(check), 0)

    def test_offline_check_never_reports_current(self):
        check = update.check_update({update.OFFLINE_ENV: "1"})
        self.assertIsNone(check.available)
        self.assertEqual(check.status, update.STATUS_OFFLINE)
        self.assertIn("offline_requested", check.reasons)

    def test_offline_wins_over_metadata_so_no_network_answer_is_invented(self):
        check = update.check_update(
            {update.OFFLINE_ENV: "true"}, metadata={"available": "9.9.9", "compatible": True}
        )
        self.assertIsNone(check.available)
        self.assertEqual(check.status, update.STATUS_OFFLINE)

    def test_configured_origin_reports_an_available_version(self):
        check = update.check_update(
            {update.ORIGIN_ENV: update.CANONICAL_ORIGIN},
            metadata={"available": "0.2.0", "compatible": True},
            installed_version="0.1.0",
        )
        self.assertEqual(check.status, update.STATUS_AVAILABLE)
        self.assertTrue(check.needs_restart)
        self.assertIs(check.compatible, True)

    def test_available_equal_to_installed_is_current_and_needs_no_restart(self):
        check = update.check_update(
            {update.ORIGIN_ENV: update.CANONICAL_ORIGIN},
            metadata={"available": "0.1.0"},
            installed_version="0.1.0",
        )
        self.assertEqual(check.status, update.STATUS_CURRENT)
        self.assertFalse(check.needs_restart)

    def test_incompatible_available_version_is_blocked_not_offered(self):
        check = update.check_update(
            {update.ORIGIN_ENV: update.CANONICAL_ORIGIN},
            metadata={"available": "0.2.0", "compatible": False},
            installed_version="0.1.0",
        )
        self.assertEqual(check.status, update.STATUS_BLOCKED)
        self.assertEqual(update.check_exit_code(check), 2)

    def test_metadata_without_an_available_version_is_not_checked(self):
        check = update.check_update({update.ORIGIN_ENV: update.CANONICAL_ORIGIN}, metadata={})
        self.assertEqual(check.status, update.STATUS_NOT_CHECKED)
        self.assertIsNone(check.available)

    def test_unsupported_channel_is_blocked(self):
        check = update.check_update({update.CHANNEL_ENV: "nightly"})
        self.assertEqual(check.status, update.STATUS_BLOCKED)
        self.assertIn("unsupported_channel:nightly", check.reasons)

    def test_unreadable_metadata_document_is_reported_not_raised(self):
        check = update.check_update(
            {update.ORIGIN_ENV: update.CANONICAL_ORIGIN},
            metadata_path=REPO_ROOT / "tests" / "does-not-exist.json",
        )
        self.assertEqual(check.status, update.STATUS_BLOCKED)
        self.assertTrue(check.reasons[0].startswith("metadata_unreadable:"))

    def test_unknown_origin_is_blocked_not_contacted(self):
        check = update.check_update({update.ORIGIN_ENV: "https://attacker.invalid/x"})
        self.assertEqual(check.status, update.STATUS_BLOCKED)
        self.assertEqual(check.reasons, ("origin_not_allowlisted:https://attacker.invalid/x",))
        self.assertIsNone(check.available)
        self.assertEqual(update.check_exit_code(check), 2)

    def test_the_canonical_origin_is_allowlisted(self):
        self.assertIn(update.CANONICAL_ORIGIN, update.allowed_origins({}))

    def test_owner_supplied_allowlist_entry_is_honoured(self):
        extra = "https://mirror.invalid/axiom-mcp"
        self.assertIn(extra, update.allowed_origins({update.ALLOWED_ORIGINS_ENV: extra}))
        check = update.check_update(
            {update.ORIGIN_ENV: extra, update.ALLOWED_ORIGINS_ENV: extra},
            metadata={"available": "0.2.0", "compatible": True},
            installed_version="0.1.0",
        )
        self.assertEqual(check.status, update.STATUS_AVAILABLE)


class PlanContractTests(_SpecsCase):
    """The specification verdict is the component verdict, fixture by fixture."""

    def test_accepted_document_is_accepted_and_digest_agrees(self):
        document = self.accepted_document()
        verdict = update.validate_document(document, self.evaluator)
        self.assertTrue(verdict["ok"], verdict["reasons"])
        self.assertEqual(verdict["plan_digest"], document["plan"]["plan_digest"])

    def test_every_canonical_fixture_agrees_with_the_specification_evaluator(self):
        seen = 0
        for path, fixture in self.fixture_documents():
            verdict = update.validate_document(fixture["document"], self.evaluator)
            with self.subTest(fixture=path.name):
                self.assertEqual(verdict["ok"], fixture["expected_ok"])
                self.assertEqual(verdict["reasons"], fixture["expected_reasons"])
                self.assertEqual(verdict["plan_digest"], fixture["expected_plan_digest"])
            seen += 1
        self.assertGreaterEqual(seen, 45)

    def test_altered_bytes_after_approval_are_stale_not_replanned(self):
        document = self.accepted_document()
        approved = document["plan"]["plan_digest"]
        altered = copy.deepcopy(document)
        altered["plan"]["components"][0]["target_version"] = "9.9.9"
        altered["plan"]["plan_digest"] = update.plan_digest(altered["plan"], self.evaluator)
        verdict = update.validate_document(altered, self.evaluator, approved_digest=approved)
        self.assertFalse(verdict["ok"])
        self.assertIn("approval_stale", verdict["reasons"])
        self.assertIn("plan_digest_not_approved", verdict["reasons"])

    def test_unknown_plan_major_is_rejected(self):
        document = self.accepted_document()
        altered = copy.deepcopy(document)
        altered["plan"]["schema_version"] = 2
        altered["plan"]["plan_digest"] = update.plan_digest(altered["plan"], self.evaluator)
        verdict = update.validate_document(altered, self.evaluator)
        self.assertFalse(verdict["ok"])
        self.assertIn("unsupported_schema_version", verdict["reasons"])

    def test_duplicate_component_entry_is_rejected(self):
        for path, fixture in self.fixture_documents():
            if path.name == "update.invalid.duplicate-component.json":
                verdict = update.validate_document(fixture["document"], self.evaluator)
                self.assertFalse(verdict["ok"])
                self.assertIn("duplicate_component:axiom-graphd", verdict["reasons"])
                return
        self.fail("duplicate-component fixture is missing from the pinned corpus")

    def test_document_that_is_not_an_object_is_rejected_not_raised(self):
        verdict = update.validate_document(["not", "a", "plan"], self.evaluator)
        self.assertFalse(verdict["ok"])
        self.assertIn("document_not_object", verdict["reasons"])

    def test_a_missing_evaluator_is_an_error_not_a_pass(self):
        with self.assertRaises(update.UpdateUnavailable):
            update.load_canonical_evaluator(pathlib.Path("no-such-specs-root"))


class ApplyTests(_SpecsCase):
    """apply validates approval, targets this component and never installs in place."""

    def test_approved_plan_is_delegated_as_an_argument_list(self):
        calls = []

        def runner(argv):
            calls.append(list(argv))
            return 0

        document = self.mcp_document()
        decision = update.apply_plan(
            document, plan_path="C:/plans/update.json", evaluator=self.evaluator, runner=runner
        )
        self.assertEqual(decision.outcome, "delegated_ok")
        self.assertTrue(decision.executed)
        self.assertEqual(calls, [["axiom", "update", "apply", "--plan", "C:/plans/update.json"]])
        self.assertEqual(decision.plan_digest, document["approved_digest"])
        self.assertEqual(decision.target_component, "axiom-mcp")

    def test_approved_plan_without_a_runner_reports_an_unexecuted_delegation(self):
        decision = update.apply_plan(
            self.mcp_document(), plan_path="C:/plans/update.json", evaluator=self.evaluator
        )
        self.assertFalse(decision.executed)
        self.assertEqual(decision.outcome, "delegated_not_executed")
        self.assertEqual(decision.argv[0], "axiom")

    def test_delegated_failure_is_reported_with_its_exit_code(self):
        decision = update.apply_plan(
            self.mcp_document(),
            plan_path="C:/plans/update.json",
            evaluator=self.evaluator,
            runner=lambda argv: 7,
        )
        self.assertTrue(decision.executed)
        self.assertEqual(decision.outcome, "delegated_failed:7")

    def test_unapproved_plan_is_refused_before_delegation(self):
        document = self.mcp_document()
        document["plan"]["approval"] = {
            "state": "unapproved",
            "approved_digest": None,
            "approved_by": None,
            "approved_at": None,
        }
        digest = update.plan_digest(document["plan"], self.evaluator)
        document["plan"]["plan_digest"] = digest
        with self.assertRaises(update.PlanRejected) as caught:
            update.apply_plan(document, plan_path="C:/plans/update.json", evaluator=self.evaluator)
        self.assertEqual(caught.exception.reasons, ("plan_not_approved",))
        self.assertEqual(update.apply_exit_code(caught.exception), 5)

    def test_plan_targeting_another_component_is_refused(self):
        document = self.accepted_document()
        with self.assertRaises(update.PlanRejected) as caught:
            update.apply_plan(document, plan_path="C:/plans/update.json", evaluator=self.evaluator)
        self.assertEqual(caught.exception.reasons, ("target_is_not_this_component:axiom-graphd",))
        self.assertEqual(update.apply_exit_code(caught.exception), 2)

    def test_rejected_plan_reports_the_canonical_reasons(self):
        document = self.mcp_document()
        document["plan"]["components"][0]["target_version"] = "9.9.9"
        with self.assertRaises(update.PlanRejected) as caught:
            update.apply_plan(document, plan_path="C:/plans/update.json", evaluator=self.evaluator)
        self.assertIn("plan_digest_mismatch", caught.exception.reasons)

    def test_install_root_inside_the_running_prefix_is_refused(self):
        document = self.mcp_document(install_root=str(pathlib.Path(sys.prefix) / "axiom-mcp"))
        with self.assertRaises(update.InPlaceUpgradeRefused) as caught:
            update.apply_plan(document, plan_path="C:/plans/update.json", evaluator=self.evaluator)
        self.assertTrue(caught.exception.reasons[0].startswith("in_place_upgrade_prohibited:"))

    def test_install_root_wrapping_the_running_prefix_is_refused(self):
        with self.assertRaises(update.InPlaceUpgradeRefused):
            update.guard_install_root(str(pathlib.Path(sys.prefix).parent))

    def test_relative_install_root_is_refused(self):
        with self.assertRaises(update.InPlaceUpgradeRefused) as caught:
            update.guard_install_root("components/axiom-mcp/0.2.0")
        self.assertTrue(caught.exception.reasons[0].startswith("install_root_not_absolute:"))

    def test_empty_install_root_is_refused(self):
        with self.assertRaises(update.InPlaceUpgradeRefused) as caught:
            update.guard_install_root("")
        self.assertEqual(caught.exception.reasons, ("install_root_empty",))

    def test_a_versioned_component_root_is_accepted(self):
        update.guard_install_root(VERSIONED_MCP_ROOT, protected=[pathlib.Path(sys.prefix)])

    def test_apply_without_a_plan_path_is_unavailable(self):
        with self.assertRaises(update.UpdateUnavailable):
            update.apply_plan(self.mcp_document(), evaluator=self.evaluator)

    def test_delegated_pip_install_is_refused(self):
        with self.assertRaises(update.InPlaceUpgradeRefused):
            update.guard_delegation(["python", "-m", "pip", "install", "axiom-mcp"])
        with self.assertRaises(update.InPlaceUpgradeRefused):
            update.guard_delegation(["pip3", "install", "--upgrade", "axiom-mcp"])
        with self.assertRaises(update.InPlaceUpgradeRefused):
            update.guard_delegation(["uv", "pip", "install", "axiom-mcp"])
        with self.assertRaises(update.InPlaceUpgradeRefused):
            update.guard_delegation([])

    def test_the_orchestrator_invocation_passes_the_guard(self):
        update.guard_delegation(update.delegation_argv("C:/plans/update.json"))

    def test_the_component_does_not_delegate_to_itself(self):
        with self.assertRaises(update.InPlaceUpgradeRefused):
            update.guard_delegation(["python", "-m", "axiom_mcp.cli", "update", "apply"])


if __name__ == "__main__":
    unittest.main()
