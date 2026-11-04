#!/usr/bin/env python3
"""Aggregate release-readiness signals without performing release actions."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from claim_evidence import validate_claim  # noqa: E402
from current_state import consistency as current_state_consistency  # noqa: E402
from governance_trail import load_retained_trail
from verification_plan import build_plan


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Aggregate read-only release readiness checks.")
    result.add_argument("--path", default=".", type=Path)
    result.add_argument("--json", action="store_true")
    result.add_argument("--require-independent-review", action="store_true", help="Block readiness unless a passed independent review result is recorded.")
    result.add_argument("--release-record", type=Path, help="Check an immutable source-bound preflight record for preview retirement and public projection contracts.")
    return result


def git_clean(root: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return result.returncode == 0 and not result.stdout.strip()


def review_debt(root: Path) -> dict[str, Any]:
    history = load_retained_trail(root)
    if history.get("invalid") or history.get("conflicts"):
        return {"status": "fail", "message": "Review evidence cannot be read consistently.", "code": "review-trail-invalid"}
    latest: dict[str, dict[str, Any]] = {}
    positions: dict[str, int] = {}
    starts: dict[str, int] = {}
    debts: dict[str, tuple[int, dict[str, Any]]] = {}
    non_review_unavailable: set[str] = set()
    for index, event in enumerate(history['events']):
        if event.get("type") != "subagent-dispatch":
            continue
        dispatch_id = event.get("dispatch_id")
        if isinstance(dispatch_id, str) and dispatch_id.startswith("ah-dispatch-"):
            latest[dispatch_id] = event
            positions[dispatch_id] = index
            if event.get('status') == 'started' and event.get('association') == 'review-cycle':
                starts.setdefault(dispatch_id, index)
            if event.get("status") == "unavailable":
                if event.get('role') in {'codebase-scout', 'security-auditor', 'implementer'}:
                    non_review_unavailable.add(dispatch_id)
                else:
                    # Unknown legacy roles remain conservative; this code-review
                    # gate does not invent obligations for known other roles.
                    debts[dispatch_id] = (index, event)
    # Historical discharge survives task archival. The current-HEAD review
    # requirement remains a separate gate, rather than recreating old debt.
    from delegation_state import review_debt_context_matches
    candidates = [
        (key, latest[key]) for key in sorted(latest, key=lambda key: positions[key], reverse=True)
        if latest[key].get('role') == 'code-reviewer' and latest[key].get('association') == 'review-cycle'
        and latest[key].get('status') == 'passed' and latest[key].get('completed_at')
        and latest[key].get('result_source') == 'model-reviewed-result'
    ]
    resolutions = {}
    for debt_id, (debt_position, debt) in debts.items():
        for replacement_id, replacement in candidates:
            if starts.get(replacement_id, -1) <= debt_position or not review_debt_context_matches(debt, replacement):
                continue
            same_subject = debt['subject_fingerprint'] == replacement.get('subject_fingerprint')
            bindings = replacement.get('resolved_review_debt', [])
            explicit = isinstance(bindings, list) and {'dispatch_id': debt_id, 'subject_fingerprint': debt['subject_fingerprint']} in bindings
            scope_covers = (not replacement.get('review_scope_bound') or
                            (same_subject and debt.get('review_scope_bound') and debt.get('review_scope') == replacement.get('review_scope')))
            if scope_covers and (same_subject or (explicit and not replacement.get('review_scope_bound'))):
                resolutions[debt_id] = replacement_id
                break
    unavailable = sorted(set(debts) - set(resolutions))
    unavailable_total = len(debts)
    cleared = len(resolutions)
    return {
        "status": "fail" if unavailable else "pass",
        "unavailable_total": unavailable_total,
        "cleared": cleared,
        "unavailable": len(unavailable),
        "dispatches": len(latest),
        "non_review_unavailable": len(non_review_unavailable),
        "resolutions": resolutions,
        "open_dispatch_ids": unavailable,
        "message": (
            "Review debt remains for unavailable independent reviews."
            if unavailable
            else "No open unavailable independent review is recorded."
        ),
    }


def independent_review(root: Path, *, required: bool) -> dict[str, Any]:
    # Reuse the production Git guard's subject and semantic-result checks.
    # A historical scout/general dispatch is never review of this HEAD/tree.
    from guard_action import independent_review_check

    result = independent_review_check(root, root, "push")
    accepted = result.get("code") in {"independent-review-passed", "independent-review-passed-soft-boundary"}
    return {**result, "required": required, "passed": int(accepted), "status": result["status"] if accepted else "fail" if required else "warn"}


def version_consistency(root: Path) -> dict[str, Any]:
    version_file = root / "skills" / "tenetora" / "VERSION"
    if not version_file.is_file():
        return {"status": "not-applicable", "message": "No Tenetora release version source is present."}
    version = version_file.read_text(encoding="utf-8").strip()
    mismatches: list[str] = []
    for relative, key in (
        (".claude-plugin/plugin.json", "version"),
        (".codex-plugin/plugin.json", "version"),
        (".zcode-plugin/plugin.json", "version"),
        ("tenetora.plugin.json", "version"),
    ):
        path = root / relative
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            mismatches.append(f"{relative}: invalid JSON")
            continue
        if str(payload.get(key, "")) != version:
            mismatches.append(f"{relative}: {payload.get(key)!r} != {version!r}")
    return {
        "status": "fail" if mismatches else "pass",
        "version": version,
        "mismatches": mismatches,
        "message": "Version-bearing manifests agree." if not mismatches else "Version-bearing manifests disagree.",
    }


def changelog_consistency(root: Path) -> dict[str, Any]:
    version_file = root / "skills" / "tenetora" / "VERSION"
    changelog = root / "CHANGELOG.md"
    if not version_file.is_file() or not changelog.is_file():
        return {"status": "not-applicable", "message": "No version source and changelog pair is present."}
    version = version_file.read_text(encoding="utf-8").strip()
    text = changelog.read_text(encoding="utf-8", errors="replace")
    matched = bool(re.search(rf"^##\s+{re.escape(version)}(?:\s|$)", text, re.MULTILINE))
    return {
        "status": "pass" if matched else "fail",
        "version": version,
        "message": "Current version has a changelog section." if matched else "Current version is missing from CHANGELOG.md.",
        "path": "CHANGELOG.md",
    }


def current_state_check(root: Path) -> dict[str, Any]:
    result = current_state_consistency(root)
    if result["status"] == "missing":
        result = dict(result)
        result["status"] = "warn"
        result["message"] = "Derived current state is missing; rebuild it before release when the project uses current-state tracking."
    elif result["status"] != "pass":
        result = {**result, "status": "fail"}
    return result


def release_record_checks(root: Path, path: Path) -> list[dict[str, Any]]:
    """Consume real CI/operator evidence; never infer remote retirement locally."""
    names = ('preview-retirement', 'projection-contracts')
    try:
        path = path if path.is_absolute() else root / path
        data = json.loads(path.read_text(encoding='utf-8'))
        head = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
        stamp = dt.datetime.fromisoformat(data['observed_at'].replace('Z', '+00:00'))
        age = (dt.datetime.now(dt.timezone.utc) - stamp).total_seconds()
        if data.get('version') != 1 or data.get('commit') != head or not 0 <= age <= 86400:
            raise ValueError('record schema, source commit, or freshness does not match')
        checks = data['checks']
        policy = json.loads((root / 'release/readiness-policy.json').read_text(encoding='utf-8'))
        if policy.get('version') != 1:
            raise ValueError('release preflight policy is invalid')
        results = []
        for name in names:
            check = checks[name]
            expected = [sys.executable if arg == '{python}' else arg for arg in policy['checks'][name]]
            if shlex.split(check['command']) != expected:
                raise ValueError('recorded command differs from the project-owned release policy')
            artifact = root / check['log']
            if Path(check['log']).is_absolute() or '..' in Path(check['log']).parts or not artifact.resolve().is_relative_to(root.resolve()):
                raise ValueError('evidence log must stay within the project')
            if type(check['exit_code']) is not int or check['exit_code'] != 0 or hashlib.sha256(artifact.read_bytes()).hexdigest() != check['log_sha256']:
                raise ValueError('check failed or its raw log changed')
            evidence = validate_claim(root, check['claim_proof'], check['command'], [])
            results.append({'name': name, 'status': evidence['status'], 'code': evidence['code'], 'message': evidence.get('message', 'Source-bound check and immutable raw log are verified.'), 'evidence': 'recorded execution; remote state observed at record time'})
        return results
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError) as exc:
        return [{'name': name, 'status': 'fail', 'code': 'release-evidence-missing-or-invalid', 'message': str(exc)} for name in names]


def build_report(root: Path, *, require_independent_review: bool = False, release_record: Path | None = None) -> dict[str, Any]:
    plan = build_plan(root)
    checks = [
        {
            "name": "worktree",
            "status": "pass" if git_clean(root) else "fail",
            "message": "Worktree is clean." if git_clean(root) else "Worktree has uncommitted changes.",
        },
        {
            "name": "verification",
            "status": "pass" if plan.get("decision") == "reuse" and plan.get("claim_reuse", {}).get("eligible") else "fail",
            "decision": plan.get("decision"),
            "message": plan.get("message"),
            "claim_reuse": plan.get("claim_reuse"),
        },
        {"name": "version-consistency", **version_consistency(root)},
        {"name": "changelog", **changelog_consistency(root)},
        {"name": "current-state", **current_state_check(root)},
        {"name": "review-debt", **review_debt(root)},
        {"name": "independent-review", **independent_review(root, required=require_independent_review)},
    ]
    if release_record is not None:
        checks.extend(release_record_checks(root, release_record))
    else:
        checks.extend({'name': name, 'status': 'unverified', 'message': 'Supply --release-record from the release preflight runner; local readiness does not prove remote publication readiness.'} for name in ('preview-retirement', 'projection-contracts'))
    blocking = [check for check in checks if check.get("status") == "fail"]
    warnings = [check for check in checks if check.get("status") == "warn"]
    return {
        "version": 1,
        "status": "blocked" if blocking else "ready" if not warnings else "ready-with-warnings",
        "scope": "release-preflight" if release_record is not None else "local-only",
        "checks": checks,
        "release_actions": "none; this command is read-only",
    }


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = args.path.expanduser().resolve()
    report = build_report(root, require_independent_review=args.require_independent_review, release_record=args.release_record)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"release-readiness: {report['status']}")
        for check in report["checks"]:
            print(f"- {check['name']}: {check['status']} — {check.get('message', '')}")
    return 1 if report["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
