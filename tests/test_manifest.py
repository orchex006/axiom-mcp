"""C-013 regression test: canonical bytes, manifest digests and shard closure.

The positive cases pin this module against bytes it did not produce. Two corpora are
vendored as fixtures and their SHA-256 values are asserted in this file, so a silent
edit of a fixture is a test failure rather than a quiet weakening of the evidence:

* ``fixtures/canonical/`` holds the four ``metadata``-form canonical vectors from
  ``axiom-specs`` (``conformance/fixtures/canonical/``, pinned specification revision
  ``80f44e836ced442e8f3ea33d167bd369ed6796bc``). Each ``input.json`` is deliberately
  non-canonical and each ``expected.canonical.json`` is the pinned expected byte
  sequence, so re-serializing the input proves this module agrees with the canonical
  rule byte for byte. The two ``identity-tuple`` vectors of that corpus are a different
  serialization form (``docs/11-GRAPH-DATA-CONTRACT.md`` section 10, no trailing LF)
  that this reader does not implement, so they are not vendored here.
* ``fixtures/generation/auth-api/`` holds one shipped example generation from
  ``axiom-specs`` ``examples/snapshots/.axiom/graph/demo-solution``. Its ``manifest.json``
  hashes to its own generation directory name, so the vendored bytes carry their own
  independent identity claim instead of a number this module computed.

The negative and boundary cases are the point of the slice. AC1 asks that altered
bytes, an unknown schema major and duplicate entries fail; this file additionally
refuses non-canonical byte forms, a self-hashing manifest, a shard whose length,
digest or record count disagrees with the entry that pins it, and a generation
directory whose name is not the digest of the manifest inside it.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from axiom_mcp import manifest, registry

FIXTURES = Path(__file__).parent / "fixtures"
CANONICAL_FIXTURES = FIXTURES / "canonical"
GENERATION_FIXTURES = FIXTURES / "generation" / "auth-api"

GENERATION_ID = "7319237fdf1ce4ff27ea377c8bc3ae8cf1f115cffc71de369a373e1870ba9d1a"
POINTER_DIGEST = "16053ddc4d52833d419e4b85b9cfc2066273705473ff77365cdfc06c84b21b8e"

# name -> (pinned expected SHA256, pinned expected length) from the corpus manifest.json.
PINNED_VECTORS: dict[str, tuple[str, int]] = {
    "metadata-basic": (
        "bc3d1004721eae3f34fa303448e7ee63bc0720546573e49673a40a5bac3e334e",
        142,
    ),
    "metadata-key-order-and-omission": (
        "67696241a4c70ffa33ba85ab07abfe10fc1af3ed93fe6ffc24ae5b6e6aae72dc",
        284,
    ),
    "metadata-null-vs-omitted": (
        "aebf0710c053ffb5dde1969a6557cabf2d3a3bfc3a4b4a4488d1435d9d734195",
        24,
    ),
    "metadata-unicode-preserved": (
        "7a6aee6c6696f15f94209940e16e8ff449e878f76143018968200dd4d6893cb5",
        70,
    ),
}

# relative to fixtures/generation/auth-api/, pinned so a fixture edit is detectable.
PINNED_GENERATION_FILES: dict[str, str] = {
    "current.json": POINTER_DIGEST,
    f"generations/{GENERATION_ID}/manifest.json": GENERATION_ID,
    f"generations/{GENERATION_ID}/coverage.json": (
        "2b563fc3c70606fe1795ca53f5cc13e72d3c2cc4292f79854394c7fb88408d8c"
    ),
    f"generations/{GENERATION_ID}/edges/000000.json": (
        "37517e5f3dc66819f61f5a7bb8ace1921282415f10551d2defa5c3eb0985b570"
    ),
    f"generations/{GENERATION_ID}/nodes/000000.json": (
        "4866453ac30b4e503fc63298438b72174624f4fb0d1a22026b0adaac9e33939b"
    ),
}

A64 = "a" * 64
B64 = "b" * 64
C64 = "c" * 64
D64 = "d" * 64
ZERO_DIGEST = "0" * 64


def canonical(value: Any) -> bytes:
    return manifest.canonical_bytes(value)


def read_generation_file(relative: str = "manifest.json") -> bytes:
    if relative == "manifest.json":
        relative = f"generations/{GENERATION_ID}/manifest.json"
    return (GENERATION_FIXTURES / relative).read_bytes()


def pointer_document(generation_id: str = GENERATION_ID) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generation_id": generation_id,
        "manifest_sha256": generation_id,
    }


def manifest_document(**overrides: Any) -> dict[str, Any]:
    """A minimal, self-consistent, canonical manifest with one empty-array shard."""
    shard = canonical([])
    document: dict[str, Any] = {
        "schema_version": 1,
        "solution_id": "solo-solution",
        "project_id": "solo-api",
        "analysis_profile": "default",
        "generator_version": "unit-test-not-runtime",
        "analyzer_set_hash": A64,
        "source_fingerprint": B64,
        "config_fingerprint": C64,
        "dependency_fingerprint": D64,
        "coverage": {
            "status": "partial",
            "input_files": 1,
            "processed_files": 1,
            "unresolved_references": 0,
            "unsupported_patterns": [],
        },
        "files": [
            {
                "path": "nodes/000000.json",
                "role": "nodes",
                "sha256": manifest.sha256_hex(shard),
                "bytes": len(shard),
                "records": 0,
            }
        ],
    }
    document.update(overrides)
    return document


def with_file_entry(**overrides: Any) -> dict[str, Any]:
    document = manifest_document()
    document["files"][0].update(overrides)
    return document


class CanonicalBytesTest(unittest.TestCase):
    def test_canonical_bytes_match_the_pinned_specs_vectors(self) -> None:
        for name, (digest, length) in PINNED_VECTORS.items():
            with self.subTest(vector=name):
                source = CANONICAL_FIXTURES / name / "input.json"
                expected_path = CANONICAL_FIXTURES / name / "expected.canonical.json"
                expected = expected_path.read_bytes()
                self.assertEqual(len(expected), length, "vendored fixture length drifted")
                self.assertEqual(
                    manifest.sha256_hex(expected), digest, "vendored fixture digest drifted"
                )
                value = json.loads(source.read_text(encoding="utf-8"))
                rendered = canonical(value)
                self.assertEqual(rendered, expected)
                self.assertTrue(manifest.is_canonical_bytes(expected))
                self.assertFalse(manifest.is_canonical_bytes(source.read_bytes()))

    def test_canonical_serialization_refuses_floats_and_non_string_keys(self) -> None:
        cases: dict[str, Any] = {
            "float": {"schema_version": 1, "coverage": 0.5},
            "nested_float": {"schema_version": 1, "files": [{"bytes": 1.5}]},
            "non_string_key": {"schema_version": 1, 7: "seven"},
        }
        for name, value in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(manifest.CanonicalSerializationError) as caught:
                    canonical(value)
                self.assertIn("canonical serialization rejected", str(caught.exception))
        self.assertEqual(
            manifest.canonicalization_reasons({"a": [1, 2.5]}),
            ["float_not_allowed_in_canonical_metadata"],
        )

    def test_canonical_bytes_are_utf8_with_one_trailing_lf_and_no_bom(self) -> None:
        rendered = canonical({"b": 1, "a": "x"})
        self.assertTrue(rendered.endswith(b"\n"))
        self.assertFalse(rendered.startswith(manifest.UTF8_BOM))
        self.assertEqual(rendered, b'{"a":"x","b":1}\n')
        self.assertEqual(
            manifest.sha256_hex(rendered), manifest.canonical_sha256({"a": "x", "b": 1})
        )


class GenerationClosureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pointer, self.loaded, self.directory = manifest.load_generation(
            GENERATION_FIXTURES / "current.json"
        )

    def test_pinned_generation_bytes_are_what_they_claim_to_be(self) -> None:
        for relative, digest in PINNED_GENERATION_FILES.items():
            with self.subTest(file=relative):
                self.assertEqual(
                    manifest.sha256_hex((GENERATION_FIXTURES / relative).read_bytes()), digest
                )

    def test_generation_identity_is_the_digest_of_the_manifest_bytes(self) -> None:
        self.assertEqual(self.pointer.generation_id, GENERATION_ID)
        self.assertEqual(self.pointer.manifest_sha256, GENERATION_ID)
        self.assertEqual(self.loaded.generation_id, GENERATION_ID)
        self.assertEqual(self.directory.name, self.loaded.generation_id)
        self.assertEqual(
            manifest.generation_id(self.loaded.json_document()),
            self.loaded.generation_id,
            "generation_id must be recomputable from the manifest document alone",
        )
        self.assertEqual(self.loaded.solution_id, "demo-solution")
        self.assertEqual(self.loaded.project_id, "auth-api")
        self.assertEqual(self.loaded.analysis_profile, "default")
        self.assertEqual(self.loaded.coverage_status, "partial")
        self.assertEqual(self.loaded.schema_version, manifest.SUPPORTED_SCHEMA_MAJOR)

    def test_closure_verifies_every_declared_shard(self) -> None:
        report = self.loaded.verify_closure(
            lambda relative: (self.directory / relative).read_bytes()
        )
        self.assertEqual(report.paths, ("coverage.json", "edges/000000.json", "nodes/000000.json"))
        self.assertEqual(report.entries, 3)
        self.assertEqual(report.bytes, self.loaded.total_bytes)
        self.assertEqual(report.records, self.loaded.total_records)
        self.assertEqual(report.records, 2)
        self.assertEqual(self.loaded.roles(), ("coverage", "edges", "nodes"))
        self.assertEqual(
            [entry.path for entry in self.loaded.entries_for_role("nodes")], ["nodes/000000.json"]
        )
        self.assertEqual(self.loaded.entries_for_role("symbol_index"), ())
        self.assertEqual(self.loaded.entry("edges/000000.json").records, 0)
        with self.assertRaises(manifest.ManifestInvalid):
            self.loaded.entry("nodes/999999.json")

    def test_an_unreadable_shard_is_reported_by_name_only(self) -> None:
        def exploding(relative: str) -> bytes:
            raise OSError(2, "missing")

        with self.assertRaises(manifest.ManifestUnreadable) as caught:
            self.loaded.verify_closure(exploding)
        self.assertIn("coverage.json", str(caught.exception))
        self.assertNotIn(":", str(caught.exception).split("coverage.json")[0])

    def test_an_empty_array_shard_reports_zero_records(self) -> None:
        document = self.loaded.verify_shard(
            self.loaded.entry("edges/000000.json"),
            (self.directory / "edges/000000.json").read_bytes(),
        )
        self.assertEqual(document, [])


class ManifestRefusalTest(unittest.TestCase):
    """AC1 negatives plus the contract violations that would make a read unsafe."""

    def setUp(self) -> None:
        self.document = self.fresh_document()
        self.pointer, _, _ = manifest.load_generation(GENERATION_FIXTURES / "current.json")

    def fresh_document(self) -> dict[str, Any]:
        """A fresh, unaliased copy of the shipped example manifest document."""
        return manifest.load_manifest_bytes(read_generation_file()).json_document()

    def test_altered_manifest_bytes_fail_the_digest_closure(self) -> None:
        altered = dict(self.document, generator_version="tampered-after-publication")
        other = manifest.load_manifest_bytes(canonical(altered))
        with self.assertRaises(manifest.DigestMismatch) as caught:
            other.verify_pointer(self.pointer)
        self.assertIn("does not match the canonical manifest bytes", str(caught.exception))
        self.assertNotEqual(other.generation_id, self.pointer.generation_id)
        self.assertEqual(
            manifest.load_pointer_bytes(read_generation_file("current.json")).generation_id,
            self.pointer.generation_id,
        )

    def test_unknown_schema_major_is_refused_before_anything_else(self) -> None:
        for version in (0, 2, 7):
            with self.subTest(schema_version=version):
                with self.assertRaises(manifest.UnsupportedSchemaMajor):
                    manifest.load_manifest_bytes(
                        canonical(manifest_document(schema_version=version))
                    )
        for version in ("1", None, True):
            with self.subTest(schema_version=version):
                with self.assertRaises(manifest.ManifestInvalid):
                    manifest.load_manifest_bytes(
                        canonical(manifest_document(schema_version=version))
                    )
        with self.assertRaises(manifest.UnsupportedSchemaMajor):
            manifest.load_pointer_bytes(canonical(pointer_document() | {"schema_version": 2}))
        with self.assertRaises(manifest.ManifestInvalid):
            manifest.load_pointer_bytes(canonical(pointer_document() | {"schema_version": "1"}))

    def test_duplicate_manifest_entries_are_refused(self) -> None:
        duplicate_role = self.fresh_document()
        duplicate_role["files"] = [
            *duplicate_role["files"],
            dict(duplicate_role["files"][1], path="nodes/000001.json"),
        ]
        with self.assertRaises(manifest.DuplicateManifestEntry) as caught:
            manifest.load_manifest_bytes(canonical(duplicate_role))
        self.assertIn("duplicate file role", str(caught.exception))

        duplicate_path = self.fresh_document()
        duplicate_path["files"] = [
            *duplicate_path["files"],
            dict(duplicate_path["files"][1], role="symbol_index"),
        ]
        with self.assertRaises(manifest.DuplicateManifestEntry) as caught:
            manifest.load_manifest_bytes(canonical(duplicate_path))
        self.assertIn("duplicate file path", str(caught.exception))

    def test_non_canonical_byte_forms_are_refused(self) -> None:
        document = manifest_document()
        reversed_order = {
            "files": document["files"],
            "coverage": document["coverage"],
            "schema_version": 1,
            **{key: value for key, value in document.items() if key not in {"files", "coverage"}},
        }
        cases: dict[str, bytes] = {
            "indented": (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
            "spaced_separators": (
                json.dumps(document, sort_keys=True, separators=(", ", ": "), ensure_ascii=False)
                + "\n"
            ).encode("utf-8"),
            "no_trailing_lf": json.dumps(
                document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8"),
            "insertion_order_kept": (
                json.dumps(
                    reversed_order, sort_keys=False, separators=(",", ":"), ensure_ascii=False
                )
                + "\n"
            ).encode("utf-8"),
            "escaped_unicode": (
                json.dumps(
                    dict(document, generator_version="\u0e44\u0e17\u0e22"),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
                + "\n"
            ).encode("utf-8"),
        }
        for name, raw in cases.items():
            with self.subTest(form=name):
                self.assertFalse(manifest.is_canonical_bytes(raw))
                with self.assertRaises(manifest.NotCanonicalBytes):
                    manifest.load_manifest_bytes(raw)

        preserved = dict(document, generator_version="\u0e44\u0e17\u0e22")
        raw = canonical(preserved)
        self.assertIn("\u0e44\u0e17\u0e22".encode("utf-8"), raw)
        self.assertEqual(manifest.load_manifest_bytes(raw).generator_version, "\u0e44\u0e17\u0e22")

    def test_bom_and_invalid_utf8_are_unreadable(self) -> None:
        body = canonical(manifest_document())
        for name, raw in {
            "bom": manifest.UTF8_BOM + body,
            "invalid_utf8": b'{"schema_version":1,"x":"\xff"}\n',
            "not_bytes": None,
        }.items():
            with self.subTest(case=name):
                with self.assertRaises(manifest.ManifestUnreadable):
                    manifest.load_manifest_bytes(raw)  # type: ignore[arg-type]

    def test_manifest_contract_violations_are_refused(self) -> None:
        cases: dict[str, dict[str, Any]] = {
            "self_hash_field": manifest_document() | {"manifest_sha256": A64},
            "unknown_top_level_key": manifest_document() | {"name": "auth-api"},
            "missing_field": {
                key: value
                for key, value in manifest_document().items()
                if key != "dependency_fingerprint"
            },
            "bad_solution_id": manifest_document(solution_id="Auth-API"),
            "empty_generator_version": manifest_document(generator_version=""),
            "bad_fingerprint": manifest_document(source_fingerprint="A" * 64),
            "unsupported_coverage_status": manifest_document(
                coverage={
                    "status": "best_effort",
                    "input_files": 1,
                    "processed_files": 1,
                    "unresolved_references": 0,
                    "unsupported_patterns": [],
                }
            ),
            "processed_exceeds_input": manifest_document(
                coverage={
                    "status": "partial",
                    "input_files": 1,
                    "processed_files": 2,
                    "unresolved_references": 0,
                    "unsupported_patterns": [],
                }
            ),
            "coverage_missing_key": manifest_document(
                coverage={
                    "status": "partial",
                    "input_files": 1,
                    "processed_files": 1,
                    "unsupported_patterns": [],
                }
            ),
            "negative_count": manifest_document(
                coverage={
                    "status": "partial",
                    "input_files": -1,
                    "processed_files": -1,
                    "unresolved_references": 0,
                    "unsupported_patterns": [],
                }
            ),
            "boolean_count": manifest_document(
                coverage={
                    "status": "partial",
                    "input_files": True,
                    "processed_files": True,
                    "unresolved_references": 0,
                    "unsupported_patterns": [],
                }
            ),
            "empty_files": manifest_document(files=[]),
            "unsupported_role": with_file_entry(role="manifest"),
            "unknown_entry_key": with_file_entry(extra="value"),
            "missing_entry_key": {"path": "nodes/000000.json", "role": "nodes"},
            "shard_zero_bytes": with_file_entry(bytes=0),
            "shard_too_large": with_file_entry(bytes=manifest.MAX_SHARD_BYTES + 1),
            "negative_records": with_file_entry(records=-1),
            "uppercase_digest": with_file_entry(sha256="A" * 64),
            "short_digest": with_file_entry(sha256="0" * 63),
        }
        for name, document in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(manifest.ManifestError) as caught:
                    manifest.load_manifest_bytes(canonical(document))
                self.assertIsInstance(caught.exception, manifest.ManifestInvalid)

        self.assertIn(
            "own hash",
            str(
                self._refusal(manifest_document() | {"generation_id": A64}),
            ),
        )

    def _refusal(self, document: dict[str, Any]) -> manifest.ManifestError:
        try:
            manifest.load_manifest_bytes(canonical(document))
        except manifest.ManifestError as exc:
            return exc
        raise AssertionError("document was accepted but should have been refused")

    def test_non_portable_manifest_paths_are_refused(self) -> None:
        hostile = (
            "/etc/passwd",
            "C:/Windows/system32/config.json",
            "nodes" + chr(92) + "000000.json",
            "../nodes/000000.json",
            "nodes/../../000000.json",
            "nodes/..",
            "",
        )
        for path in hostile:
            with self.subTest(path=path):
                with self.assertRaises(manifest.ManifestInvalid):
                    manifest.load_manifest_bytes(canonical(with_file_entry(path=path)))
        accepted = manifest.load_manifest_bytes(
            canonical(with_file_entry(path="nodes/000000.json"))
        )
        self.assertEqual(accepted.files[0].path, "nodes/000000.json")

    def test_the_portable_path_rule_matches_the_registry_rule(self) -> None:
        self.assertEqual(
            manifest.PORTABLE_RELATIVE_RE.pattern, registry.PORTABLE_RELATIVE_RE.pattern
        )
        for reference in ("a/b.json", "nodes/000000.json", "a.json", "a//b", "a/./b"):
            with self.subTest(accepted=reference):
                self.assertEqual(registry.portable_relative(reference), reference)


class ShardRefusalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pointer, self.loaded, self.directory = manifest.load_generation(
            GENERATION_FIXTURES / "current.json"
        )
        self.entry = self.loaded.entry("nodes/000000.json")
        self.shard = (self.directory / self.entry.path).read_bytes()

    def test_length_is_checked_before_the_digest(self) -> None:
        with self.assertRaises(manifest.ByteSizeMismatch):
            self.loaded.verify_shard(self.entry, self.shard[:-1])
        with self.assertRaises(manifest.ByteSizeMismatch):
            self.loaded.verify_shard(self.entry, self.shard + b" ")

    def test_same_length_substitution_fails_the_digest(self) -> None:
        substituted = self.shard.replace(b"ApiEndpoint", b"ApiEndpoinx", 1)
        self.assertEqual(len(substituted), len(self.shard))
        with self.assertRaises(manifest.DigestMismatch):
            self.loaded.verify_shard(self.entry, substituted)

    def test_digest_is_checked_before_the_shard_is_parsed(self) -> None:
        unparsable = b"{" * len(self.shard)
        with self.assertRaises(manifest.DigestMismatch):
            self.loaded.verify_shard(self.entry, unparsable)

    def test_a_shard_that_is_neither_array_nor_object_is_refused(self) -> None:
        scalar = canonical(1)
        entry = manifest.ManifestEntry(
            path=self.entry.path,
            role=self.entry.role,
            sha256=manifest.sha256_hex(scalar),
            bytes=len(scalar),
            records=1,
        )
        with self.assertRaises(manifest.ManifestInvalid):
            self.loaded.verify_shard(entry, scalar)

    def test_record_count_mismatch_is_refused(self) -> None:
        entry = manifest.ManifestEntry(
            path=self.entry.path,
            role=self.entry.role,
            sha256=self.entry.sha256,
            bytes=self.entry.bytes,
            records=self.entry.records + 1,
        )
        with self.assertRaises(manifest.RecordCountMismatch) as caught:
            self.loaded.verify_shard(entry, self.shard)
        self.assertIn("records but the manifest declares", str(caught.exception))


class PointerAndDirectoryTest(unittest.TestCase):
    def test_a_valid_pointer_round_trips(self) -> None:
        pointer = manifest.load_pointer_bytes(read_generation_file("current.json"))
        self.assertEqual(pointer.generation_id, GENERATION_ID)
        self.assertEqual(pointer.directory_name, GENERATION_ID)
        self.assertEqual(
            pointer.as_document(),
            {
                "schema_version": 1,
                "generation_id": GENERATION_ID,
                "manifest_sha256": GENERATION_ID,
            },
        )
        self.assertTrue(manifest.is_canonical_bytes(pointer.raw))

    def test_pointer_violations_are_refused(self) -> None:
        cases: dict[str, dict[str, Any]] = {
            "digest_pair_disagrees": pointer_document() | {"manifest_sha256": ZERO_DIGEST},
            "unknown_key": pointer_document() | {"path": "nodes/000000.json"},
            "missing_key": {
                "schema_version": 1,
                "generation_id": GENERATION_ID,
            },
            "uppercase_digest": pointer_document("A" * 64),
            "short_digest": pointer_document("0" * 63),
        }
        for name, document in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(manifest.ManifestError) as caught:
                    manifest.load_pointer_bytes(canonical(document))
                self.assertIsInstance(caught.exception, manifest.ManifestInvalid)
        with self.assertRaises(manifest.NotCanonicalBytes):
            manifest.load_pointer_bytes(
                json.dumps(pointer_document(), sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            )

    def test_a_generation_directory_that_is_not_the_manifest_digest_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lane = Path(temporary) / "checkpoint"
            wrong = lane / "generations" / ZERO_DIGEST
            wrong.mkdir(parents=True)
            shutil.copyfile(
                GENERATION_FIXTURES / f"generations/{GENERATION_ID}/manifest.json",
                wrong / "manifest.json",
            )
            (lane / "current.json").write_bytes(canonical(pointer_document(ZERO_DIGEST)))
            with self.assertRaises(manifest.DigestMismatch) as caught:
                manifest.load_generation(lane / "current.json")
            self.assertIn("generation directory", str(caught.exception))

            shutil.rmtree(wrong)
            correct = lane / "generations" / GENERATION_ID
            correct.mkdir(parents=True)
            shutil.copyfile(
                GENERATION_FIXTURES / f"generations/{GENERATION_ID}/manifest.json",
                correct / "manifest.json",
            )
            (lane / "current.json").write_bytes(canonical(pointer_document()))
            pointer, loaded, directory = manifest.load_generation(lane / "current.json")
            self.assertEqual(
                (pointer.generation_id, loaded.generation_id, directory.name),
                (
                    GENERATION_ID,
                    GENERATION_ID,
                    GENERATION_ID,
                ),
            )

    def test_a_missing_pointer_or_manifest_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "current.json"
            with self.assertRaises(manifest.ManifestUnreadable) as caught:
                manifest.load_generation(missing)
            self.assertIn("current.json", str(caught.exception))
            self.assertNotIn(temporary, str(caught.exception))


class BoundaryTest(unittest.TestCase):
    def test_a_single_file_manifest_is_accepted(self) -> None:
        loaded = manifest.load_manifest_bytes(canonical(manifest_document()))
        self.assertEqual(len(loaded.files), 1)
        self.assertEqual(loaded.files[0].role, "nodes")
        self.assertEqual(loaded.total_records, 0)
        self.assertEqual(loaded.byte_size, len(canonical(loaded.json_document())))
        self.assertEqual(loaded.generation_id, manifest.sha256_hex(loaded.data))
        report = loaded.verify_closure(lambda relative: canonical([]))
        self.assertEqual((report.entries, report.bytes, report.records), (1, 3, 0))

    def test_processed_files_equal_to_input_files_is_accepted(self) -> None:
        coverage = {
            "status": "complete_for_profile",
            "input_files": 3,
            "processed_files": 3,
            "unresolved_references": 0,
            "unsupported_patterns": ["synthetic"],
        }
        loaded = manifest.load_manifest_bytes(canonical(manifest_document(coverage=coverage)))
        self.assertEqual(loaded.coverage_status, "complete_for_profile")

    def test_the_maximum_shard_size_is_inclusive(self) -> None:
        for size in (1, manifest.MAX_SHARD_BYTES):
            with self.subTest(bytes=size):
                loaded = manifest.load_manifest_bytes(canonical(with_file_entry(bytes=size)))
                self.assertEqual(loaded.files[0].bytes, size)

    def test_sha256_helper_agrees_with_hashlib(self) -> None:
        payload = b"axiom"
        self.assertEqual(manifest.sha256_hex(payload), hashlib.sha256(payload).hexdigest())
        self.assertTrue(manifest.is_sha256("0" * 64))
        self.assertFalse(manifest.is_sha256("0" * 63))
        self.assertFalse(manifest.is_sha256(None))


if __name__ == "__main__":
    unittest.main()
