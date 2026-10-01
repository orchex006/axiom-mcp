"""Verify the complete Windows x64 Python 3.13 offline MCP input set."""

from __future__ import annotations

import argparse
import email
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def metadata(path: Path) -> tuple[str, str, set[str]]:
    with zipfile.ZipFile(path) as wheel:
        names = wheel.namelist()
        entries = [name for name in names if name.endswith(".dist-info/METADATA")]
        tags = [name for name in names if name.endswith(".dist-info/WHEEL")]
        if len(entries) != 1 or len(tags) != 1 or len(names) != len(set(names)):
            raise ValueError("wheel metadata is missing or ambiguous")
        if any(name.startswith("/") or "\\" in name or ".." in name.split("/") for name in names):
            raise ValueError("unsafe wheel member")
        item = email.message_from_bytes(wheel.read(entries[0]))
        tag_lines = wheel.read(tags[0]).decode("utf-8").splitlines()
        return (
            item["Name"],
            item["Version"],
            {line.removeprefix("Tag: ") for line in tag_lines if line.startswith("Tag: ")},
        )


def verify(root: Path, *, python_version: tuple[int, int] = (3, 13)) -> dict:
    if python_version != (3, 13) or sys.platform != "win32":
        raise ValueError("Windows x64 CPython 3.13 is required")
    manifest = json.loads((root / "input-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("platform") != "windows-x64" or manifest.get("python") != "3.13":
        raise ValueError("input platform or interpreter mismatch")
    allowed = {"py3-none-any", "cp313-cp313-win_amd64", "cp311-abi3-win_amd64"}
    rows = manifest.get("wheels")
    if not isinstance(rows, list) or not rows:
        raise ValueError("wheel inventory is empty")
    recorded = set()
    pinned = {}
    for row in rows:
        name = row.get("name")
        if not isinstance(name, str) or not name.endswith(".whl") or Path(name).name != name:
            raise ValueError("unsafe wheel path")
        if name in recorded:
            raise ValueError("duplicate wheel")
        recorded.add(name)
        path = root / "wheelhouse" / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"missing wheel: {name}")
        if digest(path) != row.get("sha256") or path.stat().st_size != row.get("bytes"):
            raise ValueError(f"wheel digest/size mismatch: {name}")
        package, version, tags = metadata(path)
        if not tags or not tags <= allowed or not tags & allowed:
            raise ValueError(f"unsupported wheel tag: {name}")
        if package != row.get("package") or version != row.get("version"):
            raise ValueError(f"wheel metadata mismatch: {name}")
        key = re.sub(r"[-_.]+", "-", package).lower()
        if key in pinned:
            raise ValueError(f"duplicate package: {package}")
        pinned[key] = (version, row["sha256"])
    actual = {path.name for path in (root / "wheelhouse").glob("*.whl")}
    if actual != recorded:
        raise ValueError("undeclared wheel in wheelhouse")
    lines = (root / "windows-x64-py313-requirements.txt").read_text(encoding="utf-8").splitlines()
    parsed = {}
    for line in lines:
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(
            r"([A-Za-z0-9._-]+)==([A-Za-z0-9._-]+) --hash=sha256:([0-9a-f]{64})", line
        )
        if match is None:
            raise ValueError("requirements line is not exact and hash-pinned")
        key = re.sub(r"[-_.]+", "-", match[1]).lower()
        if key in parsed:
            raise ValueError("duplicate requirement")
        parsed[key] = (match[2], match[3])
    if parsed != pinned:
        raise ValueError("requirements and wheel inventory differ")
    owner = manifest.get("owner_wheel", {})
    owner_path = root / owner.get("name", "")
    if (
        owner_path.is_symlink()
        or not owner_path.is_file()
        or digest(owner_path) != owner.get("sha256")
    ):
        raise ValueError("owner wheel missing or corrupt")
    package, version, tags = metadata(owner_path)
    if package != "axiom-mcp" or version != manifest.get("owner_version") or not tags <= allowed:
        raise ValueError("owner wheel metadata incompatible")
    return {
        "ok": True,
        "platform": "windows-x64",
        "python": "3.13",
        "dependencies": len(rows),
        "owner_wheel_sha256": owner["sha256"],
        "source_revision": manifest["source_revision"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.input_dir), sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
        print(json.dumps({"ok": False, "reason": str(error)}))
        return 9


if __name__ == "__main__":
    raise SystemExit(main())
