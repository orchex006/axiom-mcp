# Local macOS x64 verification

CPython 3.13.15; full pytest: 760 passed, 2 skipped, 230 subtests passed, one dependency warning. Final fixture-safety adjustments were followed by 57 entrypoint tests. Ruff check and format check passed. Native six-leg matrix passed after the final stdio change.

Native evidence is a redacted review copy; hashes and byte lengths describe these redacted copies. Original hashes are retained in redaction-index.json. Fixture provenance remains synthetic-fixture-v2-not-runtime; no cross-platform or released-core certification is claimed.

Reproduce with the repository pinned environment: `python -m pytest tests -q`, `ruff check .`, `ruff format --check .`, and `python tests/native/capture_native_matrix.py --target macos-x64 --python .venv/bin/python --out <evidence-dir> --deadline 30`.
