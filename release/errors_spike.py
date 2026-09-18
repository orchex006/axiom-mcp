"""Record the C-006 error-mapping evidence against the canonical policy.

Run from the repository root:

    python release/errors_spike.py

The output is JSON on stdout and is stored as the C-006 compatibility artifact.
It records what the module actually did - which surface each code landed on,
what a rendered protocol error and tool result looked like, what a real secret
sample and a real out-of-root path became, and whether the canonical reference
evaluator agrees fixture by fixture - rather than asserting the policy exists.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from axiom_mcp import errors, version  # noqa: E402

SECRET_SAMPLE = "push rejected for ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
OUT_OF_ROOT = "C:" + chr(92) + "Users" + chr(92) + "other" + chr(92) + "secrets.json"


def find_specs_root() -> pathlib.Path | None:
    configured = os.environ.get("AXIOM_SPECS_ROOT")
    candidates = [pathlib.Path(configured)] if configured else []
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


def load_canonical(path: pathlib.Path) -> Any:
    spec = importlib.util.spec_from_file_location(
        "canonical_redaction_policy", path / "tests" / "test_redaction_policy.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def surface_map() -> dict[str, str]:
    return {code: errors.default_surface(code) for code in sorted(errors.CANONICAL_CODES)}


def conformance() -> dict[str, Any]:
    specs_root = find_specs_root()
    if specs_root is None:
        return {
            "evaluator_found": False,
            "reason": "set AXIOM_SPECS_ROOT to an axiom-specs checkout",
        }
    canonical = load_canonical(specs_root)
    fixtures = sorted((specs_root / "tests" / "fixtures" / "redaction").glob("*.json"))
    agreements: list[dict[str, Any]] = []
    for fixture in fixtures:
        text = fixture.read_text(encoding="utf-8")
        agreements.append(
            {
                "fixture": fixture.name,
                "violations_agree": errors.find_violations(text) == canonical.find_violations(text),
                "redaction_agrees": errors.redact_text(text, str(REPO_ROOT))
                == canonical.redact_text(text, str(REPO_ROOT)),
            }
        )
    return {
        "evaluator_found": True,
        "evaluator_path": str(specs_root / "tests" / "test_redaction_policy.py"),
        "detection_table_identical": [
            (category, pattern.pattern, pattern.flags) for category, pattern in errors.PATTERNS
        ]
        == [(c, p.pattern, p.flags) for c, p in canonical.PATTERNS],
        "metadata_allowlist_identical": errors.ALLOWED_METADATA_KEYS
        == canonical.ALLOWED_METADATA_KEYS,
        "fixture_count": len(fixtures),
        "all_fixtures_agree": all(
            row["violations_agree"] and row["redaction_agrees"] for row in agreements
        ),
        "fixtures": agreements,
    }


def main() -> int:
    protocol = errors.AxiomError("VALIDATION_ERROR", "unknown field 'limit_ms'", request_id="req-1")
    domain = errors.AxiomError(
        "SNAPSHOT_EXPIRED", f"generation 41 expired while reading {OUT_OF_ROOT}", request_id="req-2"
    )
    try:
        raise RuntimeError(f"unexpected: {SECRET_SAMPLE}")
    except RuntimeError as exc:
        converted = errors.from_exception(exc)

    report = {
        "task": "C-006",
        "package_version": version.VERSION,
        "python": sys.version,
        "canonical_codes": sorted(errors.CANONICAL_CODES),
        "surface_map": surface_map(),
        "retryable_codes": sorted(errors.RETRYABLE_CODES),
        "jsonrpc_mapping": errors.CODE_TO_JSONRPC,
        "http_status_mapping": errors.CODE_TO_HTTP_STATUS,
        "protocol_error_render": errors.render(protocol, rpc_id=7),
        "tool_result_render": errors.render(domain),
        "redaction": {
            "input_message": f"generation 41 expired while reading {OUT_OF_ROOT}",
            "output_message": domain.message,
            "secret_sample_before": SECRET_SAMPLE,
            "secret_sample_after": errors.redact_text(SECRET_SAMPLE),
            "out_of_root_after": errors.redact_text(OUT_OF_ROOT),
            "in_root_path_after": errors.redact_text(
                str(REPO_ROOT) + os.sep + "src" + os.sep + "axiom_mcp" + os.sep + "errors.py",
                str(REPO_ROOT),
            ),
            "converted_exception": converted.as_dict(),
        },
        "envelope_fields": sorted(
            errors.error_payload(errors.AxiomError("NOT_FOUND", "no such project"))
        ),
        "conformance": conformance(),
    }
    json.dump(report, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
