"""Compile the test-only holder against artifacts of the pinned native CLI source."""

import argparse
import json
import subprocess
from pathlib import Path


def build(core: Path, output: Path):
    result = subprocess.run(
        [
            "cargo",
            "+1.85.0",
            "build",
            "--locked",
            "--release",
            "-p",
            "axiom-cli",
            "--message-format=json",
        ],
        cwd=core,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=900,
        check=True,
    )
    artifacts = {}
    linked_paths = []
    for line in result.stdout.splitlines():
        message = json.loads(line)
        if message.get("reason") == "build-script-executed":
            linked_paths.extend(message.get("linked_paths", []))
        if message.get("reason") == "compiler-artifact":
            name = message["target"]["name"]
            for file in message["filenames"]:
                if file.endswith(".rlib"):
                    artifacts[name] = file
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "rustc",
            "+1.85.0",
            "--edition=2021",
            "-C",
            "lto=thin",
            "-C",
            "codegen-units=1",
            "-C",
            "opt-level=3",
            str(Path(__file__).with_name("rust_guard_holder.rs")),
            "--extern",
            "axiom_platform=" + artifacts["axiom_platform"],
            "--extern",
            "graph_core=" + artifacts["graph_core"],
            "-L",
            "dependency=" + str(core / "target/release/deps"),
            *[arg for path in linked_paths for arg in ("-L", path)],
            "-o",
            str(output),
        ],
        check=True,
        timeout=60,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    build(args.core.resolve(), args.out.resolve())
