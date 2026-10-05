"""Package pinned native dependencies without forking the portable owner wheel."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from verify_windows_inputs import metadata


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(wheel: Path, lock: Path, lane: str, revision: str, output: Path):
    if output.exists():
        raise ValueError("new output required")
    package, version, tags = metadata(wheel)
    if package != "axiom-mcp" or tags != {"py3-none-any"} or len(revision) != 40:
        raise ValueError("immutable portable owner wheel required")
    output.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="mcp-inputs-") as temporary:
        stage = Path(temporary)
        house = stage / "wheelhouse"
        house.mkdir()
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--require-hashes",
                "--only-binary=:all:",
                "-r",
                str(lock),
                "-d",
                str(house),
            ],
            check=True,
        )
        shutil.copyfile(wheel, house / wheel.name)
        shutil.copyfile(wheel, stage / wheel.name)
        lock_name = lane + "-py313-requirements.txt"
        complete_lock = lock.read_text(encoding="utf-8").rstrip() + (
            f"\naxiom-mcp=={version} --hash=sha256:{digest(wheel)}\n"
        )
        (stage / lock_name).write_text(complete_lock, encoding="utf-8", newline="\n")
        rows = []
        for path in sorted(house.glob("*.whl")):
            name, item_version, _ = metadata(path)
            rows.append(
                {
                    "name": path.name,
                    "package": name,
                    "version": item_version,
                    "sha256": digest(path),
                    "bytes": path.stat().st_size,
                }
            )
        manifest = {
            "task_id": "K-614",
            "kind": "release_inputs",
            "platform": lane,
            "python": "3.13",
            "source_revision": revision,
            "owner_version": version,
            "owner_wheel": {
                "name": wheel.name,
                "sha256": digest(wheel),
                "bytes": wheel.stat().st_size,
            },
            "requirements_sha256": digest(stage / lock_name),
            "wheels": rows,
        }
        (stage / "input-manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        bundle = output / (lane + "-py313-inputs.tar")
        with tarfile.open(bundle, "w") as archive:
            for path in sorted(p for p in stage.rglob("*") if p.is_file()):
                info = tarfile.TarInfo(path.relative_to(stage).as_posix())
                info.size = path.stat().st_size
                info.mode = 0o644
                with path.open("rb") as stream:
                    archive.addfile(info, stream)
        # Provisioners consume a flat wheelhouse archive and dependency-only lock separately.
        house_archive = output / ("axiom-mcp-" + version + "-" + lane + "-wheelhouse.tar.gz")
        with tarfile.open(house_archive, "w:gz") as archive:
            for path in sorted(house.glob("*.whl")):
                if path.name == wheel.name:
                    continue
                info = tarfile.TarInfo("wheelhouse/" + path.name)
                info.size = path.stat().st_size
                info.mode = 0o644
                with path.open("rb") as stream:
                    archive.addfile(info, stream)
        shutil.copyfile(lock, output / lock_name)
        receipt = {
            "platform": lane,
            "source_revision": revision,
            "version": version,
            "inputs": {"name": bundle.name, "sha256": digest(bundle)},
            "wheelhouse": {"name": house_archive.name, "sha256": digest(house_archive)},
            "dependency_lock": {"name": lock_name, "sha256": digest(output / lock_name)},
            "owner_wheel_sha256": digest(wheel),
        }
        (output / (lane + "-runtime-inputs.json")).write_text(
            json.dumps(receipt, indent=2) + "\n", encoding="utf-8", newline="\n"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--lane", choices=["windows-x64", "linux-x64", "macos-x64"], required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    build(
        args.wheel.resolve(),
        args.lock.resolve(),
        args.lane,
        args.source_revision,
        args.out.resolve(),
    )
