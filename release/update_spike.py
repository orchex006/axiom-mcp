"""Record what the C-008 update commands actually did on this host.

Every row is a real child-process invocation with its observed exit code, plus the
plans that were handed to it. The spike exists so the delegation boundary is
evidence rather than a claim: it shows an approved plan for this component reaching
the external updater as an argument list, and it shows the refusals - an altered
plan, a plan for another component, and a plan whose install root is the running
interpreter.

Run from the repository root:  python release/update_spike.py
"""

from __future__ import annotations

import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from axiom_mcp import update  # noqa: E402

VERSIONED_ROOT = "C:/Users/demo/.axiom/components/axiom-mcp/0.2.0"
FIXTURES = pathlib.Path("tests") / "fixtures" / "update-plan-contract"


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env.pop(update.METADATA_ENV, None)
    env.pop(update.ORIGIN_ENV, None)
    env.pop(update.OFFLINE_ENV, None)
    env.pop(update.ALLOWED_ORIGINS_ENV, None)
    return env


def invoke(argv: list[str], extra_env: dict[str, str] | None = None) -> dict:
    env = child_env()
    env.update(extra_env or {})
    proc = subprocess.run(
        ["python", "-m", "axiom_mcp.cli", *argv],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
    )
    return {
        "argv": argv,
        "exit_code": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
    }


def specs_root() -> pathlib.Path:
    root = update.find_specs_root()
    if root is None:
        raise SystemExit("no pinned axiom-specs checkout with the plan contract is present")
    return root


def mcp_document(evaluator, install_root: str = VERSIONED_ROOT):
    root = specs_root()
    accepted = json.loads(
        (root / FIXTURES / "documents" / "accepted.document.json").read_text(encoding="utf-8")
    )
    plan = copy.deepcopy(accepted["plan"])
    plan["target"]["component"] = "axiom-mcp"
    plan["target"]["install_root"] = install_root
    plan["components"] = [dict(plan["components"][0], component="axiom-mcp")]
    plan["service_interruptions"] = [
        {"service": "axiom-mcp", "action": "restart", "max_seconds": 30}
    ]
    plan["downloads"] = [
        dict(
            plan["downloads"][0],
            artifact="axiom-mcp-0.2.0-windows-x64.zip",
            url="https://github.com/orchex006/axiom-mcp/releases/download/v0.2.0/"
            "axiom-mcp-0.2.0-windows-x64.zip",
        )
    ]
    plan["plan_digest"] = "0" * 64
    plan["approval"] = {
        "state": "unapproved",
        "approved_digest": None,
        "approved_by": None,
        "approved_at": None,
    }
    digest = update.plan_digest(plan, evaluator)
    plan["plan_digest"] = digest
    plan["approval"] = {
        "state": "approved",
        "approved_digest": digest,
        "approved_by": "maintainer@example.invalid",
        "approved_at": "2026-09-18T02:05:00Z",
    }
    return {"plan": plan, "approved_digest": digest}


def write_plan(directory: pathlib.Path, name: str, document) -> pathlib.Path:
    path = directory / name
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def main() -> int:
    evaluator = update.load_canonical_evaluator()
    report: dict = {
        "task": "C-008",
        "component": update.COMPONENT,
        "python": sys.version,
        "specs_root": str(specs_root()),
        "invocations": [],
        "plans": [],
    }

    with tempfile.TemporaryDirectory() as tmp:
        directory = pathlib.Path(tmp)
        approved = mcp_document(evaluator)
        approved_path = write_plan(directory, "approved-mcp-plan.json", approved)

        altered = copy.deepcopy(approved)
        altered["plan"]["components"][0]["target_version"] = "9.9.9"
        altered_path = write_plan(directory, "altered-after-approval.json", altered)

        in_place = mcp_document(evaluator, install_root=str(pathlib.Path(sys.prefix) / "axiom-mcp"))
        in_place_path = write_plan(directory, "in-place-root.json", in_place)

        other = json.loads(
            (specs_root() / FIXTURES / "documents" / "accepted.document.json").read_text(
                encoding="utf-8"
            )
        )
        other_path = write_plan(directory, "other-component.json", other)

        metadata_path = directory / "verified-metadata.json"
        metadata_path.write_text(
            json.dumps({"channel": "stable", "available": "0.2.0", "compatible": True}) + "\n",
            encoding="utf-8",
            newline="\n",
        )

        report["invocations"].append(invoke(["update", "check", "--json"]))
        report["invocations"].append(invoke(["update", "check", "--offline"]))
        report["invocations"].append(invoke(["update"]))
        report["invocations"].append(invoke(["update", "frobnicate"]))
        report["invocations"].append(invoke(["update", "apply"]))
        report["invocations"].append(
            invoke(["update", "apply", "--plan", str(approved_path), "--json"])
        )
        report["invocations"].append(invoke(["update", "apply", "--plan", str(altered_path)]))
        report["invocations"].append(invoke(["update", "apply", "--plan", str(in_place_path)]))
        report["invocations"].append(invoke(["update", "apply", "--plan", str(other_path)]))

        report["invocations"].append(
            invoke(
                ["update", "check", "--json", "--metadata", str(metadata_path)],
                {update.ORIGIN_ENV: update.CANONICAL_ORIGIN},
            )
        )
        report["invocations"].append(
            invoke(
                ["update", "check", "--json"],
                {update.ORIGIN_ENV: "https://attacker.invalid/axiom-mcp"},
            )
        )

    report["plans"] = [
        {"name": "approved-mcp-plan", "plan_digest": approved["approved_digest"]},
        {"name": "altered-after-approval", "recorded_digest": altered["plan"]["plan_digest"]},
        {"name": "in-place-root", "install_root": in_place["plan"]["target"]["install_root"]},
        {"name": "other-component", "target": other["plan"]["target"]["component"]},
    ]
    json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
