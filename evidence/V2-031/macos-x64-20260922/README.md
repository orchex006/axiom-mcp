# Local macOS x64 verification

CPython 3.13.15; the original final pytest record is 760 passed, 2 skipped, 230 subtests passed, with one dependency warning. The later catalog-vector regression record (`catalog-final-pytest.log`) is 762 passed, 2 skipped, 230 subtests passed; it is the current final suite for the catalog change. Final fixture-safety adjustments were followed by 57 entrypoint tests. Ruff check and format check passed. Native six-leg matrix passed after the final stdio change.

Native evidence is a redacted review copy; hashes and byte lengths describe these redacted copies. Original hashes are retained in redaction-index.json. Fixture provenance remains synthetic-fixture-v2-not-runtime; no cross-platform or released-core certification is claimed. The later combined local lifecycle separately queried analyzer output through the installed MCP wheel; that development proof is distinct from this synthetic fixture and still does not supply released-fixture or four-lane certification.

Reproduce with the repository pinned environment: `python -m pytest tests -q`, `ruff check .`, `ruff format --check .`, and `python tests/native/capture_native_matrix.py --target macos-x64 --python .venv/bin/python --out <evidence-dir> --deadline 30`.
