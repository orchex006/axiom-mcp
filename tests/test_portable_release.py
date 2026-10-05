"""One wheel cannot be published with stale/missing/native-mismatched proof."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "portable_collector", Path(__file__).resolve().parents[1] / "release/collect_portable_assets.py"
)
COLLECTOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COLLECTOR)
SOURCE = "a" * 40


def inputs(root):
    package = root / "package"
    package.mkdir(parents=True)
    wheel = package / "axiom_mcp-0.1.1-py3-none-any.whl"
    wheel.write_bytes(b"synthetic collector fixture, not an installable product")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    (package / "wheel-provenance.json").write_text(
        json.dumps({"wheel": wheel.name, "sha256": digest, "source_revision": SOURCE})
    )
    for lane in COLLECTOR.LANES:
        directory = root / lane
        directory.mkdir()
        (directory / "native-report.json").write_text(
            json.dumps(
                {
                    "platform": lane,
                    "execution": "native",
                    "source_revision": SOURCE,
                    "wheel_sha256": digest,
                    "installed_wheel_verified": True,
                    "negative_cases_verified": True,
                    "cases": [{"exit_code": 0, "expected_exit_code": 0}],
                }
            )
        )


def test_one_portable_wheel_has_source_and_checksums(tmp_path):
    inputs(tmp_path / "inputs")
    COLLECTOR.collect(tmp_path / "inputs", SOURCE, tmp_path / "out")
    assert len(list((tmp_path / "out").glob("*.whl"))) == 1
    assert len(list((tmp_path / "out").glob("*-native-report.json"))) == 3


@pytest.mark.parametrize("failure", ["source", "wheel", "missing", "failed"])
def test_invalid_proof_refuses_before_creating_output(tmp_path, failure):
    root = tmp_path / "inputs"
    inputs(root)
    report = root / "macos-x64/native-report.json"
    if failure == "missing":
        report.unlink()
    elif failure == "wheel":
        next(root.rglob("*.whl")).write_bytes(b"changed")
    else:
        data = json.loads(report.read_bytes())
        if failure == "source":
            data["source_revision"] = "b" * 40
        else:
            data["cases"][0]["exit_code"] = 2
        report.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        COLLECTOR.collect(root, SOURCE, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_existing_output_is_preserved(tmp_path):
    inputs(tmp_path / "inputs")
    out = tmp_path / "human"
    out.mkdir()
    (out / "keep").write_bytes(b"preserve")
    with pytest.raises(ValueError):
        COLLECTOR.collect(tmp_path / "inputs", SOURCE, out)
    assert (out / "keep").read_bytes() == b"preserve"
