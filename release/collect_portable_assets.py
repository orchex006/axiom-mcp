"""Collect one portable wheel only after all three native proofs bind its bytes."""

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path

LANES = {"windows-x64", "linux-x64", "macos-x64"}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def collect(root: Path, source: str, output: Path):
    if not re.fullmatch(r"[0-9a-f]{40}", source) or output.exists():
        raise ValueError("immutable source and new output required")
    proofs = list(root.rglob("wheel-provenance.json"))
    if len(proofs) != 1:
        raise ValueError("exactly one owner wheel proof required")
    proof = json.loads(proofs[0].read_bytes())
    name = proof.get("wheel", "")
    if Path(name).name != name or not name.endswith("-py3-none-any.whl"):
        raise ValueError("portable wheel name required")
    wheel = proofs[0].parent / name
    if proof.get("source_revision") != source or sha(wheel) != proof.get("sha256"):
        raise ValueError("wheel source/hash mismatch")
    reports = {}
    for path in root.rglob("native-report.json"):
        report = json.loads(path.read_bytes())
        lane = report.get("platform")
        if lane in reports or lane not in LANES:
            raise ValueError("duplicate/unsupported native lane")
        if (
            report.get("execution") != "native"
            or report.get("source_revision") != source
            or report.get("wheel_sha256") != proof["sha256"]
            or report.get("installed_wheel_verified") is not True
            or report.get("negative_cases_verified") is not True
        ):
            raise ValueError("native proof mismatch")
        cases = report.get("cases", [])
        if not cases or any(c.get("exit_code") != c.get("expected_exit_code") for c in cases):
            raise ValueError("failed/missing native cases")
        reports[lane] = path
    if set(reports) != LANES:
        raise ValueError("three native lanes required")
    output.mkdir(parents=True)
    shutil.copyfile(wheel, output / name)
    shutil.copyfile(proofs[0], output / "wheel-provenance.json")
    for lane, path in reports.items():
        shutil.copyfile(path, output / (lane + "-native-report.json"))
    (output / "SOURCE-REVISION.txt").write_text(source + "\n", encoding="utf-8", newline="\n")
    (output / "RELEASE-NOTES.md").write_text(
        "# Axiom MCP\n\nOne portable Python 3.13 wheel; native Windows x64, Linux x64 and Mac "
        "Intel installed-wheel checks passed. Mac ARM deferred. No certification/signing gate. "
        "Install with pip so native dependencies resolve for the current OS. Stdio is host-owned; "
        "HTTP is foreground and supervised by the CLI. No licensed AI host claim.\n",
        encoding="utf-8",
        newline="\n",
    )
    (output / "SHA256SUMS").write_text(
        "".join(sha(p) + "  " + p.name + "\n" for p in sorted(output.iterdir())),
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    collect(args.input, args.source_revision, args.out)
