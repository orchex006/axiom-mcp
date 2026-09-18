"""Targeted regression tests for C-006 structured error mapping.

Two claims are checked here, and they are different claims:

* the *shape* is canonical - a protocol failure renders as a JSON-RPC error with
  the canonical code in ``error.data.code``, a domain failure renders as a tool
  result with ``isError: true``, and the envelope is exactly
  ``{code, message, retryable, details, request_id}``;
* the *content* is safe - no prohibited class survives in the message or
  anywhere in the detail tree, and no formatted traceback can be produced.

The second claim is not asserted against hand-written expectations. The
canonical policy ships an executable reference evaluator
(``axiom-specs/tests/test_redaction_policy.py``), so the conformance test loads
that module by path and compares the detection table, the metadata allowlist and
every fixture outcome case by case. That is what makes this module a consumer of
the policy rather than a fork of it: a drift becomes a failing test instead of a
silent divergence.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import pathlib
import sys
import types
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from axiom_mcp import errors  # noqa: E402

SCHEMA_PATH = pathlib.Path("contracts") / "schemas" / "query-response.schema.json"

# The three codes the read protocol adds on top of the queryError enum, named in
# docs/12-SNAPSHOT-READ-WRITE-PROTOCOL.md and docs/14-MULTI-PROJECT-SOLUTIONS.md.
PROTOCOL_EXTRA_CODES = frozenset(
    {"PROJECT_UNAVAILABLE", "SNAPSHOT_UNAVAILABLE", "SNAPSHOT_CORRUPT"}
)

PROHIBITED_SAMPLES = (
    ("private_key_block", "-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n"),
    ("provider_token", "push failed for ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"),
    ("password_assignment", "connection rejected: password=hunter2secret"),
    ("credential_url", "fetching https://svc:s3cr3t-value@internal.example/v1/health"),
    ("cloud_access_key", "signing with AKIAIOSFODNN7EXAMPLE"),
    ("bearer_token", "authorization header was Bearer abcdefghijklmnopqrstuvwxyz012345"),
    ("jwt", "claims eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1g"),
    ("odbc_connection_string", "Data Source=graph01;Initial Catalog=axiom;Password=Sup3rSecret"),
)

RETAINABLE_SAMPLES = (
    "src/axiom_mcp/errors.py",
    "docs/errors-and-redaction.md",
    "content_sha256=9f2c1d4b7a6e5038c1d4b7a6e50381f2c1d4b7a6e50381f2c1d4b7a6e50381f2",
    "https://graphd.internal.example/v1/query/status",
    "POST /mcp",
)


def _find_specs_root() -> pathlib.Path | None:
    """Locate a checkout of the source-of-truth repository, if one is present."""
    candidates: list[pathlib.Path] = []
    configured = os.environ.get("AXIOM_SPECS_ROOT")
    if configured:
        candidates.append(pathlib.Path(configured))
    for base in pathlib.Path(__file__).resolve().parents:
        candidates.append(base / "axiom-specs")
        candidates.append(base / "axiom-specs-c")
        if base.is_dir():
            candidates.extend(
                sorted(child for child in base.glob("axiom-specs*") if child.is_dir())
            )
    for candidate in candidates:
        if (candidate / "tests" / "test_redaction_policy.py").is_file():
            return candidate
    return None


_SPECS_ROOT = _find_specs_root()


def _load_canonical_evaluator() -> types.ModuleType:
    assert _SPECS_ROOT is not None
    path = _SPECS_ROOT / "tests" / "test_redaction_policy.py"
    spec = importlib.util.spec_from_file_location("canonical_redaction_policy", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canonical_fixtures() -> list[pathlib.Path]:
    assert _SPECS_ROOT is not None
    return sorted((_SPECS_ROOT / "tests" / "fixtures" / "redaction").glob("*.json"))


def _all_strings(value: object) -> list[str]:
    """Every string reachable in a structure, so a leak cannot hide in a nested key."""
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            found.append(str(key))
            found.extend(_all_strings(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_all_strings(item))
    return found


class CanonicalPolicyConformanceTests(unittest.TestCase):
    """Consume the canonical evaluator instead of re-stating its rules here."""

    @classmethod
    def setUpClass(cls) -> None:
        if _SPECS_ROOT is None:
            raise unittest.SkipTest(
                "canonical evaluator not discoverable; set AXIOM_SPECS_ROOT "
                "to an axiom-specs checkout"
            )
        cls.canonical = _load_canonical_evaluator()

    def test_detection_table_matches_the_canonical_evaluator(self):
        ours = [(category, pattern.pattern, pattern.flags) for category, pattern in errors.PATTERNS]
        theirs = [
            (category, pattern.pattern, pattern.flags)
            for category, pattern in self.canonical.PATTERNS
        ]
        self.assertEqual(ours, theirs)

    def test_metadata_allowlist_matches_the_canonical_evaluator(self):
        self.assertEqual(errors.ALLOWED_METADATA_KEYS, self.canonical.ALLOWED_METADATA_KEYS)
        self.assertEqual(errors.ABSOLUTE_PATH_CLASSES, self.canonical.ABSOLUTE_PATH_CLASSES)
        # The evaluator reports classes in pattern order without de-duplicating;
        # this module exposes the de-duplicated report order.
        derived = tuple(dict.fromkeys(category for category, _ in self.canonical.PATTERNS))
        self.assertEqual(errors.PROHIBITED_CLASSES, derived)

    def test_every_canonical_fixture_agrees_case_by_case(self):
        fixtures = _canonical_fixtures()
        self.assertGreater(len(fixtures), 0, "canonical fixture directory is empty")
        root = str(REPO_ROOT)
        for fixture in fixtures:
            with self.subTest(fixture=fixture.name):
                text = fixture.read_text(encoding="utf-8")
                self.assertEqual(errors.find_violations(text), self.canonical.find_violations(text))
                self.assertEqual(errors.categories(text), self.canonical.categories(text))
                self.assertEqual(
                    errors.redact_text(text, root), self.canonical.redact_text(text, root)
                )

    def test_metadata_evaluation_agrees_on_every_canonical_fixture(self):
        for fixture in _canonical_fixtures():
            with self.subTest(fixture=fixture.name):
                try:
                    value = json.loads(fixture.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue
                self.assertEqual(
                    errors.metadata_violations(value), self.canonical.metadata_violations(value)
                )


class CanonicalCodeTests(unittest.TestCase):
    """The code set is the schema enum plus the two documented read-protocol additions."""

    def _schema_enum(self) -> list[str]:
        path = None
        if _SPECS_ROOT is not None:
            path = _SPECS_ROOT / SCHEMA_PATH
        if path is None or not path.is_file():
            self.skipTest("query-response.schema.json not discoverable")
        document = json.loads(path.read_text(encoding="utf-8"))
        return list(document["$defs"]["queryError"]["properties"]["code"]["enum"])

    def test_canonical_codes_are_the_schema_enum_plus_the_read_protocol_codes(self):
        enum = self._schema_enum()
        self.assertTrue(
            set(enum) <= errors.CANONICAL_CODES,
            f"schema codes missing from the module: {sorted(set(enum) - errors.CANONICAL_CODES)}",
        )
        self.assertEqual(errors.CANONICAL_CODES - set(enum), PROTOCOL_EXTRA_CODES)

    def test_unknown_code_is_rejected_instead_of_rendered(self):
        with self.assertRaises(ValueError):
            errors.AxiomError("NOT_A_REAL_CODE", "boom")

    def test_http_status_mapping_matches_the_control_contract(self):
        expected = {
            "VALIDATION_ERROR": 400,
            "UNAUTHENTICATED": 401,
            "FORBIDDEN": 403,
            "NOT_FOUND": 404,
            "CONFLICT": 409,
            "INCOMPATIBLE_INPUT": 422,
            "RATE_LIMITED": 429,
            "SNAPSHOT_EXPIRED": 410,
            "NOT_READY": 503,
            "DAEMON_UNAVAILABLE": 503,
            "PROJECT_UNAVAILABLE": 503,
            "SNAPSHOT_UNAVAILABLE": 503,
        }
        for code, status in expected.items():
            with self.subTest(code=code):
                self.assertEqual(errors.to_http_status(code), status)
        self.assertEqual(errors.to_http_status("INTERNAL_ERROR"), 500)


class SurfaceTests(unittest.TestCase):
    """A malformed request and an unavailable snapshot are not the same failure."""

    def test_malformed_request_renders_a_jsonrpc_protocol_error(self):
        error = errors.AxiomError(
            "VALIDATION_ERROR", "unknown field 'limit_ms'", request_id="req-1"
        )
        self.assertEqual(error.surface, errors.SURFACE_PROTOCOL)
        rendered = errors.render(error, rpc_id=7)
        self.assertEqual(rendered["jsonrpc"], "2.0")
        self.assertEqual(rendered["id"], 7)
        self.assertEqual(rendered["error"]["code"], errors.JSONRPC_INVALID_PARAMS)
        self.assertEqual(rendered["error"]["data"]["code"], "VALIDATION_ERROR")
        self.assertEqual(rendered["error"]["data"]["request_id"], "req-1")

    def test_domain_failure_renders_a_tool_result_with_the_canonical_code(self):
        error = errors.AxiomError("SNAPSHOT_EXPIRED", "generation 41 is past its retention window")
        self.assertEqual(error.surface, errors.SURFACE_TOOL_RESULT)
        rendered = errors.render(error)
        self.assertTrue(rendered["isError"])
        self.assertIn("SNAPSHOT_EXPIRED", rendered["content"][0]["text"])
        self.assertEqual(rendered["structuredContent"]["error"]["code"], "SNAPSHOT_EXPIRED")
        self.assertNotIn("jsonrpc", rendered)

    def test_envelope_is_exactly_the_canonical_fields(self):
        payload = errors.error_payload(
            errors.AxiomError("NOT_FOUND", "no such project", request_id="req-9")
        )
        self.assertEqual(sorted(payload), ["code", "details", "message", "request_id", "retryable"])
        self.assertFalse(payload["retryable"])

    def test_retryable_codes_follow_the_canonical_set(self):
        for code in sorted(errors.RETRYABLE_CODES):
            with self.subTest(code=code):
                self.assertTrue(errors.AxiomError(code, "busy").retryable)
        for code in sorted(errors.CANONICAL_CODES - errors.RETRYABLE_CODES):
            with self.subTest(code=code):
                self.assertFalse(errors.AxiomError(code, "terminal").retryable)

    def test_unknown_surface_is_rejected(self):
        with self.assertRaises(ValueError):
            errors.AxiomError("NOT_FOUND", "no such project", surface="stdout")


class RedactionBoundaryTests(unittest.TestCase):
    """Nothing prohibited survives on either the message or the detail tree."""

    def _error_for(self, message: str, details: dict) -> errors.AxiomError:
        return errors.AxiomError("INTERNAL_ERROR", message, details=details)

    def test_each_prohibited_sample_is_removed_from_message_and_details(self):
        for category, sample in PROHIBITED_SAMPLES:
            with self.subTest(category=category):
                error = self._error_for(
                    f"operation failed: {sample}",
                    {"nested": {"deeper": [sample, {"raw": sample}]}, "note": sample},
                )
                self.assertTrue(errors.is_clean(error.message), error.message)
                for text in _all_strings(error.details):
                    self.assertTrue(errors.is_clean(text), text)
                for text in _all_strings(error.as_dict()):
                    self.assertTrue(errors.is_clean(text), text)
                self.assertEqual(errors.categories(error.message), set())

    def test_out_of_root_absolute_path_is_removed_not_relativised(self):
        outside = "C:" + chr(92) + "Users" + chr(92) + "other" + chr(92) + "secrets.json"
        error = self._error_for(f"cannot open {outside}", {"path": outside})
        self.assertNotIn(outside, error.message)
        self.assertNotIn(outside, json.dumps(error.details))
        self.assertIn("<redacted:windows_absolute_path>", error.message)
        self.assertEqual(error.details["path"], "<redacted:windows_absolute_path>")

    def test_repository_relative_path_digest_route_and_url_survive(self):
        for sample in RETAINABLE_SAMPLES:
            with self.subTest(sample=sample):
                self.assertTrue(errors.is_clean(sample), sample)
                error = errors.AxiomError("NOT_FOUND", f"missing {sample}")
                self.assertIn(sample, error.message)

    def test_in_root_absolute_path_becomes_a_reusable_relative_path(self):
        relative = "src/axiom_mcp/errors.py"
        inside = str(REPO_ROOT) + os.sep + relative.replace("/", os.sep)
        error = errors.AxiomError("NOT_FOUND", f"missing {inside}", root=str(REPO_ROOT))
        self.assertIn(relative, error.message)
        self.assertNotIn(str(REPO_ROOT), error.message)

    def test_arbitrary_exception_converts_without_a_traceback(self):
        try:
            raise RuntimeError(
                "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 leaked from C:\\Users\\other\\x.py"
            )
        except RuntimeError as exc:
            error = errors.from_exception(exc)
        self.assertEqual(error.code, "INTERNAL_ERROR")
        self.assertEqual(error.message, "internal error")
        self.assertEqual(error.cause_type, "RuntimeError")
        self.assertFalse(hasattr(error, "traceback"))
        self.assertFalse(
            hasattr(error, "__traceback__") and error.__dict__.get("traceback") is not None
        )
        for text in _all_strings(error.as_dict()):
            self.assertFalse(errors.contains_traceback(text), text)
            self.assertTrue(errors.is_clean(text), text)

    def test_exception_can_be_attached_to_a_caller_supplied_code(self):
        try:
            raise ValueError("project id is empty")
        except ValueError as exc:
            error = errors.from_exception(
                exc, code="VALIDATION_ERROR", details={"field": "project_id"}
            )
        self.assertEqual(error.code, "VALIDATION_ERROR")
        self.assertEqual(error.message, "project id is empty")
        self.assertEqual(error.details["exception_type"], "ValueError")
        self.assertEqual(error.details["field"], "project_id")

    def test_detail_bounds_are_enforced(self):
        deep: object = "leaf"
        for _ in range(errors.DEFAULT_MAX_DETAIL_DEPTH + 3):
            deep = {"nested": deep}
        wide = [f"value-{index}" for index in range(errors.DEFAULT_MAX_DETAIL_ITEMS + 10)]
        long_text = "x" * (errors.DEFAULT_MAX_DETAIL_STRING + 500)
        redacted = errors.redact_details({"deep": deep, "wide": wide, "long": long_text})
        self.assertIn("<redacted:detail_depth_exceeded>", json.dumps(redacted))
        self.assertIn("<redacted:detail_items_truncated>", json.dumps(redacted))
        self.assertEqual(len(redacted["long"]), errors.DEFAULT_MAX_DETAIL_STRING)

    def test_redaction_happens_before_truncation(self):
        sample = RETAINABLE_SAMPLES[0] + " " + PROHIBITED_SAMPLES[1][1]
        redacted = errors.redact_details({"text": sample}, max_string=len(sample))
        self.assertTrue(errors.is_clean(redacted["text"]), redacted["text"])

    def test_non_scalar_metadata_values_are_reported_by_the_allowlist(self):
        problems = errors.metadata_violations({"path": "src/app.py", "coverage": object()})
        self.assertEqual(
            problems, [{"category": "metadata_value_not_scalar", "path": "$.coverage"}]
        )

    def test_not_allowlisted_metadata_key_is_reported(self):
        problems = errors.metadata_violations({"path": "src/app.py", "raw_stdout": "text"})
        self.assertEqual(
            problems, [{"category": "metadata_key_not_allowlisted", "path": "$.raw_stdout"}]
        )

    def test_allowlisted_metadata_is_accepted(self):
        metadata = {
            "schema_version": "2",
            "project_id": "proj-1",
            "path": "src/axiom_mcp/errors.py",
            "line_start": 10,
            "line_end": 12,
            "content_sha256": "0" * 64,
            "nodes_count": 3,
            "coverage": "partial",
            "error_code": "NOT_FOUND",
        }
        self.assertEqual(errors.metadata_violations(metadata), [])
        self.assertTrue(errors.is_clean(json.dumps(metadata)))

    def test_metadata_string_is_scanned_not_only_key_checked(self):
        problems = errors.metadata_violations({"path": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"})
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]["category"], "provider_token")
        self.assertEqual(problems[0]["path"], "$.path")
        self.assertNotIn("ghp_", json.dumps(problems))

    def test_violation_report_never_echoes_the_matched_value(self):
        for _category, sample in PROHIBITED_SAMPLES:
            with self.subTest(sample=sample):
                report = json.dumps(errors.find_violations(sample))
                self.assertNotIn(sample, report)

    def test_placeholder_is_the_canonical_one(self):
        self.assertEqual(errors.REDACTION_PLACEHOLDER, "<redacted:{category}>")
        self.assertEqual(
            errors.REDACTION_PLACEHOLDER.format(category="provider_token"),
            "<redacted:provider_token>",
        )

    def test_contained_substring_of_a_digest_is_not_mistaken_for_a_secret(self):
        body = json.dumps(
            {
                "path": "src/axiom_mcp/errors.py",
                "content_sha256": "ab" * 32,
                "request_id": "00000000-0000-4000-8000-000000000000",
            }
        )
        self.assertTrue(errors.is_clean(body), body)


class RenderSafetyTests(unittest.TestCase):
    """A rendered payload is safe even when the caller passes a hostile detail."""

    def test_rendered_tool_result_is_clean_end_to_end(self):
        error = errors.AxiomError(
            "DAEMON_UNAVAILABLE",
            "cannot reach daemon at http://svc:s3cr3t@127.0.0.1:8765/v1",
            details={
                "endpoint": "C:" + chr(92) + "Users" + chr(92) + "other" + chr(92) + "cfg.json"
            },
        )
        rendered = errors.tool_result_error(error)
        blob = io.StringIO()
        json.dump(rendered, blob)
        for text in _all_strings(json.loads(blob.getvalue())):
            self.assertTrue(errors.is_clean(text), text)

    def test_error_summary_omits_free_text(self):
        error = errors.AxiomError("INTERNAL_ERROR", "prefix ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345")
        summary = errors.ErrorSummary.of(error)
        self.assertEqual(summary.code, "INTERNAL_ERROR")
        self.assertEqual(summary.surface, errors.SURFACE_TOOL_RESULT)
        self.assertFalse(summary.retryable)
        self.assertNotIn("message", {field for field in summary.__dataclass_fields__})

    def test_message_falls_back_to_the_code_when_it_redacts_to_nothing(self):
        error = errors.AxiomError("FORBIDDEN", "C:" + chr(92) + "Users" + chr(92) + "other")
        self.assertEqual(error.message, "<redacted:windows_absolute_path>")
        empty = errors.AxiomError("FORBIDDEN", "")
        self.assertEqual(empty.message, "FORBIDDEN")


if __name__ == "__main__":
    unittest.main()
