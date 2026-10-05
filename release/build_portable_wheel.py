"""Build the single portable MCP wheel from immutable clean Git bytes."""

import argparse
import json
from pathlib import Path

from build_windows_wheel import build

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    args = parser.parse_args()
    proof = build(args.out_dir.resolve(), args.source_revision, stamp_revision=True)
    proof["runtime_inputs_required"] = True
    if not proof["wheel"].endswith("-py3-none-any.whl"):
        raise ValueError("MCP owner wheel must be portable")
    (args.out_dir / "wheel-provenance.json").write_text(
        json.dumps(proof, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(json.dumps(proof))
