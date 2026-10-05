#!/usr/bin/env python3
"""Offline semantic contract evaluator for the version/update plan.

JSON Schema (contracts/schemas/update-plan.schema.json) constrains the shape of one plan.
This module enforces the cross-record rules a schema alone cannot express, above all the
approval binding required by G-009 AC1: the plan digest must cover every planner input, so an
approved plan that is later changed - a different component version, a different migration set,
a different host or channel - can never reuse the old approval.

Public surface:
  canonical_plan_bytes(plan)  -> canonical digest input (UTF-8, sorted keys, compact, trailing LF)
  plan_digest(plan)           -> lowercase 64-hex digest of the plan body
  structural_reasons(plan)    -> named rejection reasons for one plan document
  approval_reasons(plan, approved_digest) -> staleness reasons against an externally recorded approval
  evaluate(document)          -> one verdict object
  check_path(path)            -> read, evaluate, return (ok, verdict)
  main(argv)                  -> CLI: exit 0 accepted, 2 rejected, 1 usage or IO problem

It reads only local files: no network, Git, shell, install or update operation is performed.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

SCHEMA_VERSION = 1
SPEC_VERSION = "2.0.0-draft.1"

PLAN_ID = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
REVISION = re.compile(r"^[0-9a-f]{40}$")
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
RELATIVE_PATH = re.compile(r"^(?!/)(?!.*\\)(?!.*(?:^|/)\.\.(?:/|$))(?![A-Za-z]:).+$")
PLACEHOLDER = re.compile(r"REPLACE|FILL_ME", re.I)

COMPONENTS = frozenset({"axiom-graphd", "axiom-mcp", "axiom", "axiom-cli", "skills"})
HOSTS = frozenset({"windows-x64", "linux-x64", "macos-arm64", "macos-x64"})
CHANNELS = frozenset({"stable", "prerelease"})
ACTIONS = frozenset({"install", "upgrade", "reinstall", "noop"})
INTERRUPTION_ACTIONS = frozenset({"stop", "restart", "none"})
APPROVAL_STATES = frozenset({"unapproved", "approved"})

DIGEST_EXCLUDED = frozenset({"plan_digest", "approval"})

PLAN_REQUIRED = (
    "schema_version", "spec_version", "plan_id", "created_at", "expires_at", "channel", "target",
    "components", "downloads", "migrations", "backup", "disk_headroom_bytes",
    "service_interruptions", "host_reconnect", "bootstrap_changes", "rollback", "trust", "approval",
    "plan_digest",
)
PLAN_KEYS = frozenset(PLAN_REQUIRED)
DOCUMENT_KEYS = frozenset({"plan", "approved_digest"})


def _is_count(value, minimum=0):
    return type(value) is int and value >= minimum


def _is_text(value):
    return isinstance(value, str) and value.strip() != ""


def _placeholders(value, path="$"):
    """Yield the JSON path of every string that still carries an unresolved placeholder."""
    if isinstance(value, str):
        if PLACEHOLDER.search(value):
            yield path
    elif isinstance(value, dict):
        for key in sorted(value):
            yield from _placeholders(value[key], "%s.%s" % (path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _placeholders(item, "%s[%d]" % (path, index))


def _blank_required(plan, item, keys, label, reasons):
    for key in keys:
        if key not in item:
            reasons.append("missing_required_field:%s.%s" % (label, key))


def canonical_plan_bytes(plan):
    """Canonical digest input: UTF-8, lexicographic keys, compact separators, trailing LF."""
    body = {key: value for key, value in plan.items() if key not in DIGEST_EXCLUDED}
    return (json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            + "\n").encode("utf-8")


def plan_digest(plan):
    return hashlib.sha256(canonical_plan_bytes(plan)).hexdigest()


def structural_reasons(plan):
    """Named rejection reasons for one plan document. An empty list means accepted."""
    reasons = []
    if not isinstance(plan, dict):
        return ["plan_not_object"]
    for key in sorted(plan):
        if key not in PLAN_KEYS:
            reasons.append("undeclared_plan_field:%s" % key)
    for key in PLAN_REQUIRED:
        if key not in plan:
            reasons.append("missing_required_field:%s" % key)
    for path in _placeholders(plan):
        reasons.append("unresolved_placeholder:%s" % path)
    if reasons:
        # Shape is already unusable; deeper checks would report noise.
        if any(r.startswith(("plan_not_object", "missing_required_field", "undeclared_plan_field"))
               for r in reasons):
            return reasons

    if plan.get("schema_version") != SCHEMA_VERSION:
        reasons.append("unsupported_schema_version")
    if not _is_text(plan.get("spec_version")) or not SEMVER.match(plan["spec_version"]):
        reasons.append("invalid_spec_version:%s" % plan.get("spec_version"))
    if not isinstance(plan.get("plan_id"), str) or not PLAN_ID.match(plan["plan_id"]):
        reasons.append("invalid_plan_id")
    for field in ("created_at", "expires_at"):
        value = plan.get(field)
        if not isinstance(value, str) or not TIMESTAMP.match(value):
            reasons.append("invalid_timestamp:%s" % field)
    created, expires = plan.get("created_at"), plan.get("expires_at")
    if (isinstance(created, str) and isinstance(expires, str)
            and TIMESTAMP.match(created) and TIMESTAMP.match(expires) and expires <= created):
        reasons.append("expiry_not_after_creation")
    if plan.get("channel") not in CHANNELS:
        reasons.append("unsupported_channel:%s" % plan.get("channel"))

    target = plan.get("target")
    if not isinstance(target, dict):
        reasons.append("target_not_object")
    else:
        if target.get("component") not in COMPONENTS:
            reasons.append("unsupported_target_component:%s" % target.get("component"))
        if target.get("host") not in HOSTS:
            reasons.append("unsupported_host:%s" % target.get("host"))
        if not _is_text(target.get("install_root")):
            reasons.append("missing_target_install_root")

    components = plan.get("components")
    if not isinstance(components, list):
        reasons.append("components_not_list")
    elif not components:
        reasons.append("components_empty")
    else:
        seen = set()
        for index, row in enumerate(components):
            label = "components[%d]" % index
            if not isinstance(row, dict):
                reasons.append("component_not_object")
                continue
            component = row.get("component")
            _blank_required(plan, row, ("component", "installed_version", "installed_revision",
                                        "target_version", "target_revision", "artifact_sha256",
                                        "action"), label, reasons)
            if component not in COMPONENTS:
                reasons.append("unsupported_component:%s" % component)
            if component in seen:
                reasons.append("duplicate_component:%s" % component)
            seen.add(component)
            if row.get("action") not in ACTIONS:
                reasons.append("unsupported_action:%s:%s" % (component, row.get("action")))
            for field in ("target_version",):
                value = row.get(field)
                if not isinstance(value, str) or not SEMVER.match(value):
                    reasons.append("invalid_version:%s.%s" % (component, field))
            installed = row.get("installed_version")
            if installed is not None and (not isinstance(installed, str) or not SEMVER.match(installed)):
                reasons.append("invalid_version:%s.installed_version" % component)
            for field in ("installed_revision", "target_revision"):
                value = row.get(field)
                if field == "installed_revision" and value is None:
                    continue
                if not isinstance(value, str) or not REVISION.match(value):
                    reasons.append("invalid_revision:%s.%s" % (component, field))
            sha = row.get("artifact_sha256")
            if not isinstance(sha, str) or not DIGEST.match(sha):
                reasons.append("invalid_digest:%s.artifact_sha256" % component)
            action, target_version = row.get("action"), row.get("target_version")
            if action in ACTIONS and isinstance(target_version, str) and SEMVER.match(target_version):
                if action == "install" and installed is not None:
                    reasons.append("action_version_mismatch:%s" % component)
                if action in ("upgrade", "reinstall") and installed is None:
                    reasons.append("action_version_mismatch:%s" % component)
                if action == "upgrade" and installed is not None and installed == target_version:
                    reasons.append("action_version_mismatch:%s" % component)
                if action == "noop" and installed != target_version:
                    reasons.append("action_version_mismatch:%s" % component)

    downloads = plan.get("downloads")
    if not isinstance(downloads, list):
        reasons.append("downloads_not_list")
    else:
        seen_artifacts = set()
        for row in downloads:
            if not isinstance(row, dict):
                reasons.append("download_not_object")
                continue
            artifact = row.get("artifact")
            if not _is_text(artifact):
                reasons.append("missing_download_artifact")
            else:
                if artifact in seen_artifacts:
                    reasons.append("duplicate_download:%s" % artifact)
                seen_artifacts.add(artifact)
            url = row.get("url")
            if not isinstance(url, str) or not url.startswith("https://"):
                reasons.append("download_url_not_https:%s" % artifact)
            sha = row.get("sha256")
            if not isinstance(sha, str) or not DIGEST.match(sha):
                reasons.append("invalid_digest:%s.sha256" % artifact)
            if not _is_count(row.get("size_bytes"), 1):
                reasons.append("invalid_size:%s" % artifact)

    migrations = plan.get("migrations")
    if not isinstance(migrations, list):
        reasons.append("migrations_not_list")
    else:
        seen_migrations = set()
        for row in migrations:
            if not isinstance(row, dict):
                reasons.append("migration_not_object")
                continue
            migration_id = row.get("migration_id")
            if not isinstance(migration_id, str) or not PLAN_ID.match(migration_id):
                reasons.append("invalid_migration_id:%s" % migration_id)
            else:
                if migration_id in seen_migrations:
                    reasons.append("duplicate_migration:%s" % migration_id)
                seen_migrations.add(migration_id)
            source, target_source = row.get("from_queue_schema"), row.get("to_queue_schema")
            if not _is_count(source) or not _is_count(target_source):
                reasons.append("invalid_queue_schema_step:%s" % migration_id)
            elif target_source <= source:
                reasons.append("migration_not_forward:%s" % migration_id)
            if row.get("reversible") is False and row.get("requires_backup") is not True:
                reasons.append("irreversible_migration_without_backup:%s" % migration_id)

    backup = plan.get("backup")
    if not isinstance(backup, dict):
        reasons.append("backup_not_object")
    else:
        if type(backup.get("required")) is not bool:
            reasons.append("invalid_backup_required")
        targets = backup.get("targets")
        if not isinstance(targets, list):
            reasons.append("backup_targets_not_list")
        else:
            for name in targets:
                if not isinstance(name, str) or not RELATIVE_PATH.match(name):
                    reasons.append("invalid_backup_target:%s" % name)
        needs_backup = [row.get("migration_id") for row in (migrations or [])
                        if isinstance(row, dict) and row.get("requires_backup") is True]
        if needs_backup and backup.get("required") is not True:
            reasons.append("backup_required_by_migration:%s" % ",".join(sorted(map(str, needs_backup))))

    if not _is_count(plan.get("disk_headroom_bytes")):
        reasons.append("invalid_disk_headroom_bytes")

    interruptions = plan.get("service_interruptions")
    if not isinstance(interruptions, list):
        reasons.append("service_interruptions_not_list")
    else:
        for row in interruptions:
            if not isinstance(row, dict):
                reasons.append("service_interruption_not_object")
                continue
            service = row.get("service")
            if service not in COMPONENTS:
                reasons.append("unsupported_service:%s" % service)
            if row.get("action") not in INTERRUPTION_ACTIONS:
                reasons.append("unsupported_interruption_action:%s:%s" % (service, row.get("action")))
            if not _is_count(row.get("max_seconds")):
                reasons.append("invalid_interruption_seconds:%s" % service)

    reconnect = plan.get("host_reconnect")
    if not isinstance(reconnect, dict):
        reasons.append("host_reconnect_not_object")
    else:
        if type(reconnect.get("required")) is not bool:
            reasons.append("invalid_host_reconnect_required")
        if not _is_text(reconnect.get("reason")):
            reasons.append("missing_host_reconnect_reason")

    changes = plan.get("bootstrap_changes")
    if not isinstance(changes, list):
        reasons.append("bootstrap_changes_not_list")
    else:
        for row in changes:
            if not isinstance(row, dict):
                reasons.append("bootstrap_change_not_object")
                continue
            repo_id = row.get("repo_id")
            if not isinstance(repo_id, str) or not PLAN_ID.match(repo_id):
                reasons.append("invalid_bootstrap_repo:%s" % repo_id)
            template_version = row.get("template_version")
            if not isinstance(template_version, str) or not SEMVER.match(template_version):
                reasons.append("invalid_bootstrap_template_version:%s" % repo_id)
            destinations = row.get("destinations")
            if not isinstance(destinations, list) or not destinations:
                reasons.append("missing_bootstrap_destinations:%s" % repo_id)
            else:
                for destination in destinations:
                    if not isinstance(destination, str) or not RELATIVE_PATH.match(destination):
                        reasons.append("invalid_bootstrap_destination:%s" % destination)

    rollback = plan.get("rollback")
    if not isinstance(rollback, dict):
        reasons.append("rollback_not_object")
    else:
        if type(rollback.get("supported")) is not bool:
            reasons.append("invalid_rollback_supported")
        if type(rollback.get("restores_previous_versions")) is not bool:
            reasons.append("invalid_rollback_restores")
        limits = rollback.get("limits")
        if not isinstance(limits, list):
            reasons.append("rollback_limits_not_list")
        elif rollback.get("supported") is True and any(
                not _is_text(limit) for limit in limits):
            reasons.append("invalid_rollback_limit")
        if rollback.get("supported") is not True and migrations:
            reasons.append("rollback_unsupported_with_migration")

    trust = plan.get("trust")
    if not isinstance(trust, dict):
        reasons.append("trust_not_object")
    else:
        if not _is_count(trust.get("metadata_version"), 1):
            reasons.append("invalid_trust_metadata_version")
        expiry = trust.get("metadata_expiry")
        if not isinstance(expiry, str) or not TIMESTAMP.match(expiry):
            reasons.append("invalid_trust_metadata_expiry")
        elif isinstance(created, str) and TIMESTAMP.match(created) and expiry <= created:
            reasons.append("trust_metadata_expired")
        trusted_root = trust.get("trust_root")
        if trusted_root is None or (isinstance(trusted_root, str) and not DIGEST.match(trusted_root)):
            reasons.append("unresolved_trust_root")
        if trust.get("signature_present") is not True:
            reasons.append("unsigned_plan")

    approval = plan.get("approval")
    if not isinstance(approval, dict):
        reasons.append("approval_not_object")
    else:
        state = approval.get("state")
        if state not in APPROVAL_STATES:
            reasons.append("unsupported_approval_state:%s" % state)
        elif state == "approved":
            if not _is_text(approval.get("approved_by")):
                reasons.append("approval_incomplete:approved_by")
            approved_at = approval.get("approved_at")
            if not isinstance(approved_at, str) or not TIMESTAMP.match(approved_at):
                reasons.append("approval_incomplete:approved_at")
            recorded = approval.get("approved_digest")
            if not isinstance(recorded, str) or not DIGEST.match(recorded):
                reasons.append("approval_incomplete:approved_digest")
            elif plan.get("plan_digest") != recorded:
                reasons.append("approval_digest_mismatch")
        else:
            if approval.get("approved_digest") is not None:
                reasons.append("unapproved_plan_carries_digest")
            if approval.get("approved_by") is not None:
                reasons.append("unapproved_plan_carries_approver")
            if approval.get("approved_at") is not None:
                reasons.append("unapproved_plan_carries_timestamp")

    recorded_digest = plan.get("plan_digest")
    if not isinstance(recorded_digest, str) or not DIGEST.match(recorded_digest):
        reasons.append("invalid_plan_digest")
    elif plan_digest(plan) != recorded_digest:
        reasons.append("plan_digest_mismatch")

    return reasons


def approval_reasons(plan, approved_digest):
    """Compare a plan against an approval recorded outside the plan.

    This is the AC1 boundary: an approval names one digest, and the digest covers every planner
    input (components, versions, revisions, artifact hashes, migrations, backup, host and
    channel), so re-using an old approval for a changed plan is rejected as stale.
    """
    if not isinstance(plan, dict):
        return ["plan_not_object"]
    reasons = []
    if not isinstance(approved_digest, str) or not DIGEST.match(approved_digest):
        return ["approved_digest_not_a_digest"]
    if plan_digest(plan) != approved_digest:
        reasons.append("approval_stale")
    if plan.get("plan_digest") != approved_digest:
        reasons.append("plan_digest_not_approved")
    return reasons


def evaluate(document):
    """Evaluate one document: {"plan": {...}, "approved_digest": "<hex>" (optional)}."""
    if not isinstance(document, dict):
        return {"ok": False, "reasons": ["document_not_object"], "structural_reasons": ["document_not_object"],
                "approval_reasons": [], "plan_digest": None}
    reasons = []
    for key in sorted(document):
        if key not in DOCUMENT_KEYS:
            reasons.append("undeclared_document_field:%s" % key)
    if "plan" not in document:
        return {"ok": False, "reasons": reasons + ["missing_required_field:plan"],
                "structural_reasons": ["missing_required_field:plan"], "approval_reasons": [],
                "plan_digest": None}
    plan = document["plan"]
    structural = reasons + structural_reasons(plan)
    approval = []
    if "approved_digest" in document:
        approval = approval_reasons(plan, document["approved_digest"])
    digest = plan_digest(plan) if isinstance(plan, dict) else None
    combined = structural + approval
    return {"ok": not combined, "reasons": combined, "structural_reasons": structural,
            "approval_reasons": approval, "plan_digest": digest}


def check_path(path):
    with Path(path).open(encoding="utf-8") as handle:
        document = json.load(handle)
    return evaluate(document)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = False
    if "--json" in argv:
        argv.remove("--json")
        as_json = True
    if argv[:1] == ["--help"] or argv[:1] == ["-h"]:
        print("usage: python tools/update_plan_contract.py [--json] <document.json>")
        return 0
    if len(argv) != 1:
        print("usage: python tools/update_plan_contract.py [--json] <document.json>", file=sys.stderr)
        return 1
    try:
        verdict = check_path(argv[0])
    except (OSError, ValueError) as exc:
        print("cannot read %s: %s" % (argv[0], exc), file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(verdict, indent=2, ensure_ascii=False))
    else:
        print(("accepted" if verdict["ok"] else "rejected") + " " + argv[0])
        for reason in verdict["reasons"]:
            print("  " + reason)
    return 0 if verdict["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
