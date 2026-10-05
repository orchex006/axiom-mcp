"""Build a Windows MCP wheel from exact committed source in a clean staging tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT)


def build(output: Path, revision: str, *, stamp_revision: bool = False) -> dict:
    if revision != git("rev-parse", "HEAD").decode().strip():
        raise ValueError("source revision differs from checked-out commit")
    if git("status", "--porcelain=v1", "--untracked-files=normal").strip():
        raise ValueError("source checkout is dirty")
    if output == ROOT or ROOT in output.parents or output.exists():
        raise ValueError("output must be a new directory outside the source checkout")
    tracked = git("ls-files", "-z", "--", "pyproject.toml", "README.md", "src/axiom_mcp")
    names = [name.decode("utf-8") for name in tracked.split(b"\0") if name]
    if not names or "pyproject.toml" not in names or "README.md" not in names:
        raise ValueError("committed package source is incomplete")
    epoch = git("show", "-s", "--format=%ct", revision).decode().strip()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="axiom-mcp-source-", dir=output.parent) as tmp:
        stage = Path(tmp) / "source"
        for name in names:
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(git("show", f"{revision}:{name}"))
        if stamp_revision:
            (stage / "src/axiom_mcp/_build_revision.py").write_text(
                f'BUILD_REVISION = "{revision}"\n', encoding="utf-8", newline="\n"
            )
        env = dict(os.environ, SOURCE_DATE_EPOCH=epoch)
        command = [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(output),
            str(stage),
        ]
        process = subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8")
        if process.returncode:
            raise ValueError(f"wheel build failed: {process.stderr[-300:]}")
    wheels = list(output.glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError("wheel build did not produce exactly one artifact")
    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as artifact:
        contents = set(artifact.namelist())
        packaged = {
            name.removeprefix("src/") for name in names if name.startswith("src/axiom_mcp/")
        }
        if not packaged <= contents:
            raise ValueError("wheel omits committed package files")
        for name in names:
            if name.startswith("src/axiom_mcp/") and artifact.read(
                name.removeprefix("src/")
            ) != git("show", f"{revision}:{name}"):
                raise ValueError(f"wheel differs from committed source: {name}")
        if stamp_revision and artifact.read("axiom_mcp/_build_revision.py") != (
            f'BUILD_REVISION = "{revision}"\n'.encode()
        ):
            raise ValueError("wheel build revision differs from immutable source")
    return {
        "wheel": wheel.name,
        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "source_revision": revision,
        "packaged_source_files": len(packaged),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--source-revision", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.out_dir.resolve(), args.source_revision), sort_keys=True))
        return 0
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        print(json.dumps({"ok": False, "reason": str(error)}))
        return 9


if __name__ == "__main__":
    raise SystemExit(main())
