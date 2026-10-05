"""Targeted regression tests for G-011 ``contracts/redaction-policy.md``.

This module is also the policy's executable reference evaluator. It is pure
standard library so the rules can be rerun without installing anything, and it
never echoes a matched value: a violation report carries only the category and
the character span, so the detector cannot itself become a leak.

Covered prohibited classes: source secrets (private-key blocks, cloud access
keys, provider tokens, bearer tokens, JWT, password assignments, credential
URLs, ODBC-style connection strings) and absolute local paths (Windows drive
paths, POSIX absolute paths, UNC shares). Covered retainable metadata is the
allowlisted query metadata: stable ids, repository-relative paths, symbol
identity, line ranges, digests, counts, coverage and error codes.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "contracts" / "redaction-policy.md"
FIXTURES = ROOT / "tests" / "fixtures" / "redaction"
MANIFEST = FIXTURES / "README.md"

REDACTION_PLACEHOLDER = "<redacted:{category}>"
BACKSLASH = chr(92)
FORWARD_SLASH = chr(47)

# Detection table. Order is report order, not priority: every class is reported.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("cloud_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("provider_token", re.compile(r"\b(?:gh[pousr]|glpat|xox[baprs])_[A-Za-z0-9_-]{16,}\b")),
    ("provider_token", re.compile(r"\bsk-[A-Za-z0-9]{24,}\b")),
    ("bearer_token", re.compile(r"\bBearer[ \t]+[A-Za-z0-9._~+/=-]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b")),
    ("password_assignment", re.compile(r"\b(?:password|passwd|pwd|secret|token|api[_-]?key)\b[ \t]*[:=][ \t]*[^\s,;]{6,}", re.I)),
    ("credential_url", re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]{1,64}:[^/\s@]{1,64}@", re.I)),
    ("odbc_connection_string", re.compile(r"\b(?:Data Source|Server|Initial Catalog)\b[ \t]*=[^;\n]+;[\s\S]{0,200}?\b(?:Password|Pwd)\b[ \t]*=[^;\s]+", re.I)),
    ("windows_absolute_path", re.compile(r"(?<![\w$])[A-Za-z]:[\\/]+(?:[^\\/\s\"'<>|]+[\\/]+)*[^\\/\s\"'<>|]*")),
    ("unc_path", re.compile(r"\\\\+[A-Za-z0-9._$-]{1,64}\\\\+[^\\/\s]{1,64}")),
    ("posix_absolute_path", re.compile(r"(?<![\w:/.-])/(?:home|Users|root|usr|etc|var|opt|private|mnt|Volumes|tmp|srv)(?:/[^\s\"',;:)\]}]*)?")),
)

ABSOLUTE_PATH_CLASSES = frozenset({"windows_absolute_path", "posix_absolute_path", "unc_path"})

# Metadata that stays usable for graph queries after redaction.
ALLOWED_METADATA_KEYS = frozenset(
    {
        "schema_version",
        "solution_id",
        "project_id",
        "repo",
        "path",
        "language",
        "symbol_kind",
        "symbol_name",
        "line_start",
        "line_end",
        "content_sha256",
        "source_fingerprint",
        "generation_id",
        "bytes",
        "nodes_count",
        "edges_count",
        "coverage",
        "analysis_profile",
        "timestamp_utc",
        "error_code",
        "request_id",
        "redaction_notice",
    }
)


def find_violations(text: str) -> list[dict]:
    """Return ``{"category", "span"}`` records. Never returns the matched bytes."""
    violations: list[dict] = []
    for category, pattern in PATTERNS:
        for match in pattern.finditer(text):
            violations.append({"category": category, "span": [match.start(), match.end()]})
    violations.sort(key=lambda row: (row["span"][0], row["span"][1]))
    return violations


def categories(text: str) -> set[str]:
    return {row["category"] for row in find_violations(text)}


def is_clean(text: str) -> bool:
    return not find_violations(text)


def repository_relative_path(raw: str, root: str | None) -> str | None:
    """Map an absolute path inside ``root`` to its repository-relative form."""
    if root is None:
        return None
    root_norm = str(root).replace(BACKSLASH, FORWARD_SLASH).rstrip(FORWARD_SLASH)
    candidate = raw.replace(BACKSLASH, FORWARD_SLASH)
    if not root_norm:
        return None
    lowered_root = root_norm.lower()
    lowered_candidate = candidate.lower()
    if lowered_candidate == lowered_root:
        return "."
    prefix = lowered_root + FORWARD_SLASH
    if lowered_candidate.startswith(prefix):
        return candidate[len(root_norm) + 1:]
    return None


def redact_text(text: str, root: str | None = None) -> str:
    """Replace every prohibited span; an in-root absolute path becomes relative."""
    rows = find_violations(text)
    out = text
    for row in sorted(rows, key=lambda r: r["span"][0], reverse=True):
        start, end = row["span"]
        replacement = None
        if row["category"] in ABSOLUTE_PATH_CLASSES:
            relative = repository_relative_path(text[start:end], root)
            if relative is not None:
                replacement = relative
        if replacement is None:
            replacement = REDACTION_PLACEHOLDER.format(category=row["category"])
        out = out[:start] + replacement + out[end:]
    return out


def metadata_violations(value, path: str = "$") -> list[dict]:
    """Apply the metadata allowlist and the text scan to a query metadata object."""
    problems: list[dict] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key not in ALLOWED_METADATA_KEYS:
                problems.append({"category": "metadata_key_not_allowlisted", "path": f"{path}.{key}"})
                continue
            problems.extend(metadata_violations(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            problems.extend(metadata_violations(item, f"{path}[{index}]"))
    elif isinstance(value, str):
        for row in find_violations(value):
            problems.append({"category": row["category"], "path": path, "span": row["span"]})
    elif value is None or isinstance(value, (bool, int, float)):
        pass
    else:
        problems.append({"category": "metadata_value_not_scalar", "path": path})
    return problems


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class ReferenceEvaluatorTests(unittest.TestCase):
    """Each prohibited class is rejected; allowlisted metadata is accepted."""

    PROHIBITED = (
        ("violating.private-key.json", "private_key_block"),
        ("violating.provider-token.json", "provider_token"),
        ("violating.password-assignment.json", "password_assignment"),
        ("violating.credential-url.json", "credential_url"),
        ("violating.connection-string.json", "odbc_connection_string"),
        ("violating.cloud-access-key.json", "cloud_access_key"),
        ("violating.bearer-token.json", "bearer_token"),
        ("violating.windows-path.json", "windows_absolute_path"),
        ("violating.posix-path.json", "posix_absolute_path"),
        ("violating.unc-path.json", "unc_path"),
    )

    def test_each_prohibited_fixture_is_rejected(self):
        for name, expected in self.PROHIBITED:
            with self.subTest(name=name):
                self.assertIn(expected, categories(read_fixture(name)))

    def test_allowlisted_metadata_fixture_is_accepted(self):
        text = read_fixture("allowlisted.metadata.json")
        self.assertEqual(find_violations(text), [])
        self.assertEqual(metadata_violations(json.loads(text)), [])

    def test_violation_report_never_echoes_the_matched_value(self):
        text = read_fixture("violating.provider-token.json")
        report = json.dumps(find_violations(text))
        self.assertNotIn("ghp_", report)
        self.assertNotIn("match", {key for row in find_violations(text) for key in row})

    def test_redaction_removes_every_prohibited_span(self):
        for name, _ in self.PROHIBITED:
            with self.subTest(name=name):
                self.assertTrue(is_clean(redact_text(read_fixture(name))))

    def test_in_root_absolute_path_becomes_reusable_relative_path(self):
        root = "C:" + BACKSLASH + "repo" + BACKSLASH + "project"
        raw = root + BACKSLASH + "src" + BACKSLASH + "main.rs"
        self.assertEqual(repository_relative_path(raw, root), "src/main.rs")

    def test_out_of_root_absolute_path_is_dropped(self):
        root = "C:" + BACKSLASH + "repo" + BACKSLASH + "project"
        foreign = "D:" + BACKSLASH + "elsewhere" + BACKSLASH + "main.rs"
        self.assertIsNone(repository_relative_path(foreign, root))
        self.assertTrue(is_clean(redact_text(foreign, root)))


class BoundaryTests(unittest.TestCase):
    """Boundary shapes that must stay usable, and confinements that must hold."""

    def test_repository_relative_path_with_os_like_segment_is_accepted(self):
        self.assertFalse(categories("docs/Users/guide.md"))

    def test_repo_relative_source_path_is_accepted(self):
        self.assertFalse(categories("tests/fixtures/redaction/allowlisted.metadata.json"))

    def test_digests_are_not_mistaken_for_tokens(self):
        text = '{"content_sha256": "' + "a1" * 32 + '", "generation_id": "' + "b2" * 32 + '"}'
        self.assertFalse(categories(text))

    def test_api_route_is_not_a_local_absolute_path(self):
        self.assertFalse(categories("operation context target /api/v1/graph/context"))

    def test_plain_url_without_credentials_is_accepted(self):
        self.assertFalse(categories("see https://example.invalid/docs/23-SECURITY-AND-TRUST.md"))

    def test_drive_relative_path_is_not_treated_as_absolute(self):
        self.assertFalse(categories("C:notes.txt"))

    def test_boundary_source_body_key_is_not_allowlisted(self):
        text = read_fixture("boundary.metadata-key-not-allowlisted.json")
        problems = metadata_violations(json.loads(text))
        self.assertIn("metadata_key_not_allowlisted", {row["category"] for row in problems})
        # The rejection is the allowlist, not a text match on the body.
        self.assertEqual(find_violations(text), [])

    def test_nested_allowlisted_metadata_is_accepted(self):
        value = json.loads(read_fixture("allowlisted.metadata.json")) | {
            "analysis_profile": "default",
        }
        self.assertEqual(metadata_violations(value), [])

    def test_manifest_does_not_carry_prohibited_samples(self):
        self.assertTrue(is_clean(MANIFEST.read_text(encoding="utf-8")))

    def test_policy_document_is_free_of_prohibited_samples(self):
        self.assertTrue(is_clean(POLICY_PATH.read_text(encoding="utf-8")))

    def test_rejection_samples_are_confined_and_declared(self):
        manifest = MANIFEST.read_text(encoding="utf-8")
        samples = sorted(p.name for p in FIXTURES.glob("*.json"))
        self.assertTrue(samples, "no redaction fixtures were found")
        for name in samples:
            with self.subTest(name=name):
                self.assertIn(name, manifest, f"{name} is not declared in the rejection manifest")
        accepted = {"allowlisted.metadata.json", "boundary.metadata-key-not-allowlisted.json"}
        for name in samples:
            if name in accepted:
                continue
            self.assertTrue(find_violations(read_fixture(name)), f"{name} is declared violating but is clean")


class PolicyDocumentTests(unittest.TestCase):
    """The policy states the same rules the evaluator executes."""

    def setUp(self):
        self.text = POLICY_PATH.read_text(encoding="utf-8")
        self.lowered = self.text.lower()

    def test_policy_lists_every_prohibited_class(self):
        for category, _ in PATTERNS:
            with self.subTest(category=category):
                self.assertIn(category, self.text)

    def test_policy_names_windows_posix_and_unc_absolute_paths(self):
        for token in ("Windows drive", "POSIX absolute", "UNC"):
            self.assertIn(token.lower(), self.lowered)

    def test_policy_states_allowlisted_metadata_stays_usable(self):
        self.assertIn("allowlist", self.lowered)
        for key in ("solution_id", "project_id", "path", "content_sha256", "line_start"):
            self.assertIn(key, self.text)

    def test_policy_points_at_the_executable_evaluator(self):
        self.assertIn("tests/test_redaction_policy.py", self.text)
        for function in ("find_violations", "redact_text", "metadata_violations", "repository_relative_path"):
            self.assertIn(function, self.text)

    def test_policy_states_the_rejection_sample_confinement(self):
        self.assertIn("rejection sample", self.lowered)
        self.assertIn("synthetic", self.lowered)
        self.assertIn("reserved", self.lowered)

    def test_policy_cross_references_security_and_agent_governance(self):
        self.assertIn("docs/23-SECURITY-AND-TRUST.md", self.text)
        self.assertIn("AGENTS.md", self.text)

    def test_policy_uses_normative_keywords(self):
        for keyword in ("MUST", "MUST NOT", "SHOULD"):
            self.assertIn(keyword, self.text)


if __name__ == "__main__":
    unittest.main()