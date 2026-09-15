#!/usr/bin/env python3
"""Classify current changes and choose the smallest verification action."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from path_security import validate_existing_project_path  # noqa: E402
from worktree_fingerprint import (  # noqa: E402
    WORKTREE_ENTRIES_VERSION,
    WORKTREE_FINGERPRINT_VERSION,
    git_state_entries,
    git_state_fingerprint,
    normalize_scopes,
)


PLAN_VERSION = 1
CLAIM_PROOF_PATTERN = re.compile(r"^ah-claim-[0-9a-f]{32}$")
NON_VERIFICATION_CATEGORIES = frozenset({"documentation", "release-metadata", "governance-state"})
RELEASE_METADATA_PATHS = frozenset(
    {
        ".agents/plugins/marketplace.json",
        ".claude-plugin/marketplace.json",
        ".claude-plugin/plugin.json",
        ".codex-plugin/plugin.json",
        ".zcode-plugin/plugin.json",
        "tenetora.plugin.json",
    }
)
BEHAVIOR_CONTRACT_ROOTS = frozenset(
    {
        "AGENTS.md",
        "CLAUDE.md",
        "CLAUDE.local.md",
    }
)
SOURCE_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".go",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".py",
        ".rb",
        ".rs",
        ".swift",
        ".ts",
        ".tsx",
    }
)
BUILD_NAMES = frozenset(
    {
        "Dockerfile",
        "Makefile",
        "Pipfile",
        "Pipfile.lock",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pyproject.toml",
        "requirements.txt",
        "setup.cfg",
        "setup.py",
        "tox.ini",
        "yarn.lock",
    }
)


def existing_project_path(raw: str) -> Path:
    try:
        return validate_existing_project_path(raw)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(
        prog="tenetora verification-plan",
        description="Classify current changes and choose reuse, targeted, or rerun verification.",
        add_help=False,
    )
    command_parser.add_argument("--help", action="help", help="Show this help message and exit.")
    command_parser.add_argument("-p", "--path", default=".", type=existing_project_path, metavar="<project-dir>")
    command_parser.add_argument(
        "--git-path",
        default=None,
        type=existing_project_path,
        metavar="<git-worktree>",
        help="Git worktree whose changes should be classified. Defaults to --path.",
    )
    command_parser.add_argument(
        "--scope",
        action="append",
        default=[],
        metavar="<project-relative-path>",
        help="Limit the plan to one agent's project-relative scope; repeatable.",
    )
    command_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return command_parser


def run_git(root: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        text=text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def git_root(root: Path) -> Path | None:
    result = run_git(root, "rev-parse", "--show-toplevel")
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve(strict=False)


def _decode_paths(raw: bytes) -> list[str]:
    paths: list[str] = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        value = os.fsdecode(item)
        candidate = Path(value)
        if candidate.is_absolute() or candidate.drive or ".." in candidate.parts:
            raise RuntimeError("Git reported a path outside the worktree")
        paths.append(value.replace("\\", "/"))
    return sorted(set(paths))


def changed_paths(root: Path) -> list[str]:
    tracked = run_git(root, "diff", "--name-only", "-z", "HEAD", text=False)
    if tracked.returncode != 0:
        raise RuntimeError("Unable to compare the worktree with HEAD")
    untracked = run_git(root, "ls-files", "--others", "--exclude-standard", "-z", text=False)
    if untracked.returncode != 0:
        raise RuntimeError("Unable to inspect untracked worktree paths")
    return sorted(set(_decode_paths(tracked.stdout) + _decode_paths(untracked.stdout)))


def classify_path(relative: str) -> str:
    normalized = relative.replace("\\", "/")
    path = Path(normalized)
    name = path.name
    lower = normalized.lower()
    if lower in RELEASE_METADATA_PATHS:
        return "release-metadata"
    if lower.startswith(".tenetora/state/") or lower.startswith(".tenetora/changes/"):
        return "governance-state"
    if lower.startswith(".tenetora/"):
        return "governance-contract"
    if (
        name.upper().startswith("CHANGELOG")
        or "release-notes" in lower
        or name.upper() == "VERSION"
    ):
        return "release-metadata"
    if name in BEHAVIOR_CONTRACT_ROOTS or lower.startswith("skills/"):
        return "behavior-contract"
    if lower.startswith("tests/") or name.startswith("test_") or name.endswith("_test.py"):
        return "test"
    if name in BUILD_NAMES or lower.startswith((".github/", ".gitlab/")) or name in {".gitlab-ci.yml", "Jenkinsfile"}:
        return "build-contract"
    if lower.startswith("docs/") or name.upper().startswith("README") or path.suffix.lower() in {
        ".md",
        ".mdx",
        ".rst",
    }:
        return "documentation"
    if path.suffix.lower() in SOURCE_SUFFIXES:
        return "source"
    return "unknown"


def _path_in_scope(relative: str, scopes: tuple[str, ...]) -> bool:
    if not scopes:
        return True
    return any(relative == scope or relative.startswith(scope + "/") for scope in scopes)


def _project_root_fingerprint(root: Path) -> str:
    return hashlib.sha256(str(root.resolve(strict=False)).encode("utf-8", errors="surrogatepass")).hexdigest()


def _load_claim_events(root: Path) -> list[dict[str, Any]]:
    path = root / ".tenetora" / "state" / "governance-trail.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    events = payload.get("events") if isinstance(payload, dict) else None
    project_fingerprint = _project_root_fingerprint(root)
    return (
        [
            item
            for item in events
            if isinstance(item, dict)
            and item.get("project_root_fingerprint") == project_fingerprint
        ]
        if isinstance(events, list)
        else []
    )


def _event_scope(event: dict[str, Any]) -> tuple[str, ...] | None:
    raw = event.get("verification_scope", [])
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        return None
    try:
        return normalize_scopes(raw)
    except ValueError:
        return None


def _scope_covers(requested: tuple[str, ...], recorded: tuple[str, ...]) -> bool:
    if not requested:
        return not recorded
    if not recorded:
        return True
    return all(
        any(
            requested_scope == recorded_scope
            or requested_scope.startswith(recorded_scope + "/")
            for recorded_scope in recorded
        )
        for requested_scope in requested
    )


def _latest_claim_from_events(
    events: Sequence[dict[str, Any]],
    requested_scope: tuple[str, ...],
) -> tuple[dict[str, Any] | None, str | None]:
    for event in reversed(events):
        if event.get("type") != "verification-claim":
            continue
        event_scope = _event_scope(event)
        if event_scope is None:
            continue
        if not _scope_covers(requested_scope, event_scope):
            continue
        passed = event.get("status") == "pass" and event.get("verification_status") == "passed"
        if passed and CLAIM_PROOF_PATTERN.fullmatch(str(event.get("claim_proof") or "")):
            return event, None
        return None, "latest matching verification claim failed or was skipped"
    return None, "no passed verification claim is recorded"


def latest_claim(root: Path, requested_scope: tuple[str, ...]) -> tuple[dict[str, Any] | None, str | None]:
    return _latest_claim_from_events(_load_claim_events(root), requested_scope)


def _claim_summary(
    root: Path,
    event: dict[str, Any] | None,
    considered_paths: set[str] | None = None,
) -> dict[str, Any]:
    if event is None:
        return {
            "eligible": False,
            "reason": "no passed verification claim is available",
            "claim_proof": None,
            "changed_after_claim_paths": [],
            "post_claim_paths_known": False,
        }
    event_scope = _event_scope(event) or ()
    recorded = event.get("git_state_fingerprint")
    version = event.get("git_state_fingerprint_version")
    current = git_state_fingerprint(root, event_scope)
    recorded_entries = event.get("git_state_entries")
    entries_version = event.get("git_state_entries_version")
    entries_supported = (
        entries_version == WORKTREE_ENTRIES_VERSION
        and isinstance(recorded_entries, dict)
        and all(isinstance(path, str) and isinstance(entry, str) for path, entry in recorded_entries.items())
    )
    current_entries = git_state_entries(root, event_scope) if entries_supported else None
    changed_after_claim = []
    if entries_supported and current_entries is not None:
        changed_after_claim = sorted(
            path
            for path in set(recorded_entries) | set(current_entries)
            if recorded_entries.get(path) != current_entries.get(path)
        )
        if considered_paths is not None:
            changed_after_claim = [path for path in changed_after_claim if path in considered_paths]
    common = {
        "claim_proof": event.get("claim_proof"),
        "claim_kind": event.get("claim_kind"),
        "verification_scope": list(event_scope),
        "changed_after_claim_paths": changed_after_claim,
        "post_claim_paths_known": False,
    }
    if version != WORKTREE_FINGERPRINT_VERSION or not isinstance(recorded, str) or not re.fullmatch(r"[0-9a-f]{64}", recorded):
        return {
            "eligible": False,
            "reason": "the claim has no supported worktree fingerprint",
            **common,
        }
    if current is None:
        return {
            "eligible": False,
            "reason": "the current worktree fingerprint is unavailable",
            **common,
        }
    if current == recorded and entries_supported and current_entries is not None:
        return {
            "eligible": True,
            "freshness": "fresh",
            "reason": "the verified content in the claim scope is unchanged",
            **{**common, "post_claim_paths_known": True},
        }
    if considered_paths is not None and entries_supported and current_entries is not None and not changed_after_claim:
        return {
            "eligible": True,
            "freshness": "fresh-selected-paths",
            "reason": "the selected commit paths are unchanged; unrelated worktree paths are outside this plan",
            **{**common, "post_claim_paths_known": True},
        }
    if current == recorded and not entries_supported:
        return {
            "eligible": True,
            "freshness": "fresh-legacy",
            "reason": "the verified content is unchanged; the claim predates per-path freshness metadata",
            **{**common, "post_claim_paths_known": True},
        }
    if not entries_supported:
        return {
            "eligible": False,
            "freshness": "unsupported",
            "reason": "the claim has no supported per-path snapshot; changed content requires new verification",
            **common,
        }
    if changed_after_claim and all(classify_path(path) in NON_VERIFICATION_CATEGORIES for path in changed_after_claim):
        return {
            "eligible": True,
            "freshness": "stale-but-non-impacting-change",
            "reason": "only documentation, release metadata, or volatile governance state changed after the claim",
            **{**common, "post_claim_paths_known": True},
        }
    return {
        "eligible": False,
        "freshness": "stale",
        "reason": "a verification-impacting path changed after the claim",
        **{**common, "post_claim_paths_known": True},
    }


def _diff_check_command(paths: Sequence[str]) -> str:
    return "git diff --check HEAD -- " + " ".join(shlex.quote(path) for path in paths)


def _is_changelog_path(path: str) -> bool:
    return Path(path).name.upper().startswith("CHANGELOG")


def _changelog_contract_command(worktree: Path) -> str | None:
    modules = [
        name
        for name in ("test_private_changelog", "test_release_notes")
        if (worktree / "tests" / f"{name}.py").is_file()
    ]
    return f"python3 -m unittest {' '.join(f'tests.{name}' for name in modules)}" if modules else None


def build_plan(
    root: Path,
    git_worktree: Path | None = None,
    scopes: Sequence[str] | None = None,
    changed_paths_override: Sequence[str] | None = None,
) -> dict[str, Any]:
    project = root.resolve(strict=False)
    worktree = (git_worktree or root).resolve(strict=False)
    actual_git_root = git_root(worktree)
    if actual_git_root is None:
        return {"version": PLAN_VERSION, "status": "error", "code": "git-repository-missing", "message": "The plan requires a Git worktree."}
    try:
        normalized_scope = normalize_scopes(scopes)
    except ValueError as exc:
        return {"version": PLAN_VERSION, "status": "error", "code": "verification-scope-invalid", "message": str(exc)}
    if changed_paths_override is not None:
        changed = sorted(set(str(path).replace("\\", "/") for path in changed_paths_override))
    else:
        raw_changed = changed_paths(actual_git_root)
        if actual_git_root != project:
            try:
                prefix = actual_git_root.relative_to(project).as_posix()
            except ValueError:
                prefix = ""
            changed = sorted(f"{prefix}/{path}" if prefix else path for path in raw_changed)
        else:
            changed = raw_changed
    current_in_scope = [path for path in changed if _path_in_scope(path, normalized_scope)]
    external = [path for path in changed if path not in current_in_scope]
    event, event_reason = latest_claim(project, normalized_scope)
    claim = _claim_summary(
        project,
        event,
        set(changed) if changed_paths_override is not None else None,
    )
    changed_after_claim = claim.get("changed_after_claim_paths", [])
    if not claim.get("post_claim_paths_known"):
        in_scope = current_in_scope
    else:
        current_paths = set(current_in_scope)
        in_scope = [
            path
            for path in changed_after_claim
            if path in current_paths and _path_in_scope(path, normalized_scope)
        ]
    categories = {path: classify_path(path) for path in in_scope}
    impact = sorted(path for path, category in categories.items() if category not in NON_VERIFICATION_CATEGORIES)
    non_impact = sorted(path for path, category in categories.items() if category in NON_VERIFICATION_CATEGORIES)
    required_checks: list[dict[str, Any]] = []
    if non_impact:
        required_checks.append(
            {
                "kind": "targeted",
                "paths": non_impact,
                "command": _diff_check_command(non_impact),
                "reason": "Confirm the changed documentation or release metadata is internally clean.",
            }
        )
    changelog_paths = [path for path in in_scope if _is_changelog_path(path)]
    changelog_command = _changelog_contract_command(actual_git_root)
    if changelog_paths and changelog_command:
        required_checks.append(
            {
                "kind": "targeted",
                "paths": changelog_paths,
                "command": changelog_command,
                "reason": "Changelog changes need the bounded-window and release-note contract checks.",
            }
        )
    release_metadata_paths = [
        path
        for path in in_scope
        if categories[path] == "release-metadata" and not _is_changelog_path(path)
    ]
    if release_metadata_paths:
        required_checks.append(
            {
                "kind": "targeted",
                "paths": release_metadata_paths,
                "command": "python3 -m unittest tests.test_version_single_source tests.test_plugin_manifest tests.test_package_release",
                "reason": "Version and package metadata need their focused contract checks.",
            }
        )
    if impact:
        required_checks.append(
            {
                "kind": "rerun",
                "paths": impact,
                "command": None,
                "reason": "Run the smallest project tests/build checks that cover these paths; do not reuse an unrelated claim.",
            }
        )
    if impact:
        decision = "rerun"
        message = "Verification-impacting paths changed; existing evidence cannot cover the current worktree."
    elif not current_in_scope:
        decision = "reuse"
        message = "No current worktree changes require verification; reuse prior evidence or skip verification."
    elif claim["eligible"] and not in_scope:
        decision = "reuse"
        message = "The verified content is unchanged; reuse the matching verification claim."
    elif claim["eligible"]:
        decision = "targeted"
        message = "Only non-verification paths changed after the claim; reuse implementation evidence and run the listed targeted checks."
    elif event is None and event_reason == "no passed verification claim is recorded":
        decision = "targeted"
        message = "Only non-verification paths changed and no verification-impacting evidence is needed; run only the listed targeted checks."
    else:
        decision = "rerun"
        message = "Verification freshness cannot be proven for the current worktree; run the relevant verification again."
    return {
        "version": PLAN_VERSION,
        "status": "pass",
        "code": f"verification-{decision}",
        "decision": decision,
        "message": message,
        "project_root": str(project),
        "git_root": str(actual_git_root),
        "scope": list(normalized_scope),
        "changed_paths": changed,
        "in_scope_paths": in_scope,
        "external_paths": external,
        "changed_after_claim_paths": changed_after_claim,
        "categories": categories,
        "verification_impact_paths": impact,
        "non_verification_paths": non_impact,
        "claim_reuse": claim,
        "claim_event_reason": event_reason,
        "required_checks": required_checks,
    }


def build_staged_plan(
    root: Path,
    git_worktree: Path,
    staged_paths: Sequence[str],
) -> dict[str, Any]:
    """Aggregate the freshest covering claim for each staged project path."""

    paths = sorted(set(str(path).replace("\\", "/") for path in staged_paths))
    if not paths:
        return build_plan(root, git_worktree, changed_paths_override=[])
    actual_git_root = git_root(git_worktree) or git_worktree.resolve(strict=False)
    events = _load_claim_events(root)
    groups: dict[str, dict[str, Any]] = {}
    for path in paths:
        event, reason = _latest_claim_from_events(events, normalize_scopes([path]))
        key = str(event.get("claim_proof")) if event is not None else f"missing:{reason}"
        group = groups.setdefault(key, {"event": event, "reason": reason, "paths": []})
        group["paths"].append(path)

    path_claims: list[dict[str, Any]] = []
    changed_after_claim: list[str] = []
    categories: dict[str, str] = {}
    decisions: list[str] = []
    for group in groups.values():
        group_paths = sorted(set(str(path) for path in group["paths"]))
        summary = _claim_summary(root, group["event"], set(group_paths))
        changed = set(str(path) for path in summary.get("changed_after_claim_paths", []))
        path_snapshot_known = bool(summary.get("post_claim_paths_known"))
        valid_claim = CLAIM_PROOF_PATTERN.fullmatch(str(summary.get("claim_proof") or "")) is not None
        no_claim = group["event"] is None and group.get("reason") == "no passed verification claim is recorded"
        changed_after_claim.extend(changed)
        for path in group_paths:
            category = classify_path(path)
            categories[path] = category
            if path in changed:
                category = classify_path(path)
                path_decision = (
                    "targeted"
                    if valid_claim and path_snapshot_known and category in NON_VERIFICATION_CATEGORIES
                    else "rerun"
                )
            elif valid_claim and (summary.get("eligible") or path_snapshot_known):
                path_decision = "reuse"
            elif no_claim and category in NON_VERIFICATION_CATEGORIES:
                path_decision = "targeted"
            else:
                path_decision = "rerun"
            decisions.append(path_decision)
            path_claims.append(
                {
                    "path": path,
                    "decision": path_decision,
                    "eligible": path_decision in {"reuse", "targeted"},
                    "claim_proof": summary.get("claim_proof"),
                    "freshness": summary.get("freshness"),
                    "reason": summary.get("reason") or group.get("reason"),
                }
            )

    decision = "rerun" if "rerun" in decisions else "targeted" if "targeted" in decisions else "reuse"
    rerun_paths = sorted(item["path"] for item in path_claims if item["decision"] == "rerun")
    impact = sorted(path for path in rerun_paths if categories.get(path) not in NON_VERIFICATION_CATEGORIES)
    targeted_paths = sorted(item["path"] for item in path_claims if item["decision"] == "targeted")
    non_impact = sorted(path for path in targeted_paths if categories.get(path) in NON_VERIFICATION_CATEGORIES)
    changed_after_claim = sorted(set(changed_after_claim))
    required_checks: list[dict[str, Any]] = []
    if non_impact:
        required_checks.append(
            {
                "kind": "targeted",
                "paths": non_impact,
                "command": _diff_check_command(non_impact),
                "reason": "Confirm the staged documentation or release metadata is internally clean.",
            }
        )
    changelog_paths = [path for path in non_impact if _is_changelog_path(path)]
    changelog_command = _changelog_contract_command(actual_git_root)
    if changelog_paths and changelog_command:
        required_checks.append(
            {
                "kind": "targeted",
                "paths": changelog_paths,
                "command": changelog_command,
                "reason": "Changelog changes need the bounded-window and release-note contract checks.",
            }
        )
    release_paths = [
        path
        for path in non_impact
        if categories.get(path) == "release-metadata" and not _is_changelog_path(path)
    ]
    if release_paths:
        required_checks.append(
            {
                "kind": "targeted",
                "paths": release_paths,
                "command": "python3 -m unittest tests.test_version_single_source tests.test_plugin_manifest tests.test_package_release",
                "reason": "Version and package metadata need their focused contract checks.",
            }
        )
    if rerun_paths:
        required_checks.append(
            {
                "kind": "rerun",
                "paths": rerun_paths,
                "command": None,
                "reason": "Run the smallest project tests/build checks that cover these paths; do not reuse an unrelated claim.",
            }
        )

    claim_proofs = sorted(
        {
            str(item["claim_proof"])
            for item in path_claims
            if CLAIM_PROOF_PATTERN.fullmatch(str(item.get("claim_proof") or ""))
        }
    )
    if decision == "reuse":
        message = "Every staged path is covered by unchanged verification evidence; reuse the matching claims."
    elif decision == "targeted":
        message = (
            "Implementation evidence remains reusable; run only the listed checks for staged non-verification paths."
            if claim_proofs
            else "No verification-impacting staged path requires a full suite; run only the listed targeted checks."
        )
    else:
        message = "At least one staged path lacks fresh covering evidence or changed after verification; rerun only its affected checks."
    project = root.resolve(strict=False)
    return {
        "version": PLAN_VERSION,
        "status": "pass",
        "code": f"verification-{decision}",
        "decision": decision,
        "message": message,
        "project_root": str(project),
        "git_root": str(actual_git_root or git_worktree.resolve(strict=False)),
        "scope": [],
        "changed_paths": paths,
        "in_scope_paths": paths,
        "external_paths": [],
        "changed_after_claim_paths": changed_after_claim,
        "categories": categories,
        "verification_impact_paths": impact,
        "non_verification_paths": non_impact,
        "claim_reuse": {
            "eligible": bool(claim_proofs) and all(item["eligible"] for item in path_claims),
            "claim_proofs": claim_proofs,
            "path_claims": path_claims,
        },
        "claim_event_reason": None,
        "required_checks": required_checks,
    }


def print_text(payload: dict[str, Any]) -> None:
    chinese = os.environ.get("TENETORA_LANG", "").strip().lower() in {"zh", "zh-cn", "zh-hans", "chinese", "中文"}
    labels = {
        "reuse": ("复用", "reuse"),
        "targeted": ("定向检查", "targeted"),
        "rerun": ("重新验证", "rerun"),
    }
    decision = str(payload.get("decision") or "error")
    label = labels.get(decision, (decision, decision))[0 if chinese else 1]
    print(f"Tenetora verification plan: {label}")
    print(payload.get("message", ""))
    changed = payload.get("changed_paths", [])
    if changed:
        print(("变更路径" if chinese else "Changed paths") + ": " + ", ".join(str(item) for item in changed))
    for check in payload.get("required_checks", []):
        print(("定向检查" if chinese else "Targeted check") + ": " + str(check.get("command") or check.get("reason")))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = args.path.resolve(strict=False)
    payload = build_plan(root, args.git_path, args.scope)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_text(payload)
    return 0 if payload.get("status") == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
