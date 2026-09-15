#!/usr/bin/env python3
"""Validate baseline .tenetora structure and common safety checks."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import alignment_state
import delegation_state
from path_security import validate_existing_project_path


def use_chinese() -> bool:
    return os.environ.get("TENETORA_LANG", "").strip().lower() in {"zh", "zh-cn", "zh-hans", "chinese", "中文"}


def localized_failure(failure: str) -> str:
    if not use_chinese():
        return failure
    replacements = (
        ("missing ", "缺少 "),
        ("invalid json ", "JSON 无效 "),
        ("AGENTS.md does not point to .tenetora/README.md", "AGENTS.md 未指向 .tenetora/README.md"),
        ("secret-pattern", "敏感信息模式"),
        ("stale-content-pattern", "过期内容模式"),
    )
    for source, target in replacements:
        if source in failure:
            return failure.replace(source, target, 1)
    return failure


REQUIRED = [
    ".tenetora/README.md",
    ".tenetora/agents/generic.md",
    ".tenetora/rules/project.md",
    ".tenetora/rules/subagent-dispatch.md",
    ".tenetora/workflows/task-start.md",
    ".tenetora/workflows/verification.md",
    ".tenetora/workflows/decision-alignment.md",
    ".tenetora/skills/decision-interview.md",
    ".tenetora/wiki/project-map.md",
    ".tenetora/wiki/technology.md",
    ".tenetora/wiki/architecture.md",
    ".tenetora/docs/architecture/overview.md",
    ".tenetora/docs/conventions/README.md",
    ".tenetora/guardrails/quality-gates.md",
    ".tenetora/guardrails/ci.md",
    ".tenetora/guardrails/checks/run-all.sh",
    ".tenetora/guardrails/checks/secret-scan.sh",
    ".tenetora/guardrails/checks/local-path-scan.sh",
    ".tenetora/guardrails/checks/stale-doc-scan.sh",
    ".tenetora/state/features.json",
    ".tenetora/automation/worktree-verify.sh",
    ".tenetora/templates/feature-design.md",
    ".tenetora/templates/implementation-plan.md",
    ".tenetora/templates/alignment-handoff.md",
    ".tenetora/templates/subagents/code-reviewer.spec.md",
    ".tenetora/templates/subagents/security-auditor.spec.md",
    ".tenetora/templates/subagents/codebase-scout.spec.md",
    ".tenetora/templates/subagents/implementer.spec.md",
    "AGENTS.md",
]

SECRET_PATTERNS = [
    re.compile(r"glpat-[A-Za-z0-9._-]+"),
    re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
    re.compile(r"\b[A-Z0-9_]*(TOKEN|SECRET|PASSWORD)\b\s*[:=]\s*['\"]?[^'\"\s${}]+", re.I),
]
STALE_PATTERNS = [
    re.compile(r"<card|</span>|</p>|h[e]rness|READ[E]ME"),
    re.compile(r"(?im)^\s*(?:[-*]\s*)?(?:TODO|TBD)(?:\s*:|\s*$)"),
    re.compile(r"(?i)\b(?:TODO|TBD)\b\s*(?:here|later|placeholder|待补充|待定|补充|完善)"),
]


def existing_project_path(raw: str) -> Path:
    try:
        return validate_existing_project_path(raw)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def scan_text(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    findings = []
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            findings.append("secret-pattern")
            break
    if any(pattern.search(text) for pattern in STALE_PATTERNS):
        findings.append("stale-content-pattern")
    return findings


def current_evidence_files(root: Path) -> list[Path]:
    pointer = root / ".tenetora" / "state" / "current-evidence.json"
    if pointer.exists():
        try:
            payload = json.loads(pointer.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        current = payload.get("current", {}) if isinstance(payload, dict) else {}
        rel = current.get("evidence") if isinstance(current, dict) else None
        if isinstance(rel, str):
            path = root / rel
            try:
                path.relative_to(root)
            except ValueError:
                path = root / ".tenetora" / "changes" / "__invalid__"
            if path.is_file():
                return [path]
    return sorted((root / ".tenetora" / "changes").glob("*-extraction-evidence.json"))


def alignment_text_values(payload: dict[str, object]) -> list[tuple[str, str]]:
    fields = (
        "goal",
        "scope",
        "non_goals",
        "decisions",
        "constraints",
        "assumptions",
        "accepted_risks",
        "approval_boundaries",
        "acceptance_criteria",
        "verification",
        "open_questions",
        "current_question",
        "blocked_reason",
    )
    values: list[tuple[str, str]] = []
    for field in fields:
        raw = payload.get(field)
        if isinstance(raw, str) and raw.strip():
            values.append((field, raw))
        elif isinstance(raw, list):
            values.extend((field, item) for item in raw if isinstance(item, str) and item.strip())
    return values


def validate_alignment(root: Path) -> list[str]:
    failures: list[str] = []
    internal_ignore = root / ".tenetora" / ".gitignore"
    ignore_text = internal_ignore.read_text(encoding="utf-8", errors="ignore") if internal_ignore.is_file() else ""
    normalized_ignores = {line.strip().rstrip("/") for line in ignore_text.splitlines() if line.strip()}
    for entry in (
        "state/alignment-session.json",
        "state/alignment-sessions/",
        "state/alignment-history/",
        "state/alignment-lifecycle.json",
        "state/.*.lock",
    ):
        if entry.rstrip("/") not in normalized_ignores:
            failures.append(f".tenetora/.gitignore does not ignore {entry}")
    try:
        sessions = alignment_state.load_all_sessions(root)
        current = alignment_state.load_current(root)
        lifecycle = alignment_state.load_lifecycle(root)
    except alignment_state.AlignmentStateError as exc:
        return [f"invalid alignment state: {exc}"]

    records = [
        (f"session[{payload.get('session_id', 'legacy')}]", payload)
        for payload in sessions
    ]
    records.append(("current", current))
    records.extend(
        (f"lifecycle[{session_id}]", record)
        for session_id, record in lifecycle.get("records", {}).items()
    )
    for label, payload in records:
        if payload is None:
            continue
        for field, value in alignment_text_values(payload):
            try:
                alignment_state.ensure_safe_text(value, f"{label}.{field}")
            except alignment_state.AlignmentStateError as exc:
                failures.append(str(exc))
        expected = str(payload.get("goal_fingerprint", ""))
        actual = alignment_state.goal_fingerprint(
            str(payload.get("goal", "")),
            list(payload.get("scope", [])),
            list(payload.get("non_goals", [])),
            list(payload.get("acceptance_criteria", [])),
        )
        if alignment_state.is_stale(expected, actual):
            failures.append(f"stale {label} alignment goal fingerprint")

    if current is not None:
        expected_hash = str(current.get("handoff_hash", ""))
        if not expected_hash or expected_hash != alignment_state.handoff_hash(current):
            failures.append("invalid current alignment handoff hash")
        relative = current.get("handoff_relative_path")
        if relative:
            path = Path(str(relative))
            if path.is_absolute() or ".." in path.parts:
                failures.append("current alignment handoff path must be project-relative")
            elif not (root / path).is_file():
                failures.append(f"missing current alignment handoff document {path.as_posix()}")

    task_start = root / ".tenetora" / "workflows" / "task-start.md"
    if task_start.is_file():
        text = task_start.read_text(encoding="utf-8", errors="ignore")
        if "decision-alignment.md" not in text:
            failures.append("task-start workflow does not consume decision-alignment.md")
        if "tenetora guard --action alignment" not in text:
            failures.append(
                "task-start workflow does not consume tenetora guard --action alignment"
            )
    return failures


def validate_subagent_governance(root: Path) -> list[str]:
    failures: list[str] = []
    internal_ignore = root / ".tenetora" / ".gitignore"
    ignore_text = internal_ignore.read_text(encoding="utf-8", errors="ignore") if internal_ignore.is_file() else ""
    normalized = {line.strip().rstrip("/") for line in ignore_text.splitlines() if line.strip()}
    if ".cache" not in normalized and ".cache/subagents" not in normalized:
        failures.append(".tenetora/.gitignore does not ignore .cache/subagents")
    if "state/delegation-state.json" not in normalized:
        failures.append(".tenetora/.gitignore does not ignore state/delegation-state.json")

    task_start = root / ".tenetora" / "workflows" / "task-start.md"
    if task_start.is_file() and "subagent-dispatch.md" not in task_start.read_text(encoding="utf-8", errors="ignore"):
        failures.append("task-start workflow does not consume subagent-dispatch.md")

    state_file = root / delegation_state.STATE_REL
    if state_file.is_file():
        try:
            delegation_state.load_state(root)
        except delegation_state.DelegationError as exc:
            failures.append(f"invalid delegation state: {exc}")

    loop_file = root / ".tenetora" / "state" / "loop-state.json"
    if loop_file.is_file():
        try:
            loop_payload = json.loads(loop_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            failures.append(f"invalid loop state: {exc}")
        else:
            review = loop_payload.get("review_cycle") if isinstance(loop_payload, dict) else None
            if not isinstance(review, dict):
                failures.append("loop state is missing review_cycle")
            else:
                if review.get("max_review_rounds") != 3 or review.get("max_fix_rounds") != 2:
                    failures.append("loop review_cycle limits must be 3 review rounds and 2 fix rounds")
                if int(review.get("round", 0)) > 3 or int(review.get("fix_rounds", 0)) > 2:
                    failures.append("loop review_cycle exceeds bounded round limits")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--help", action="help", help="Show this help message and exit.")
    parser.add_argument(
        "-p",
        "--path",
        default=".",
        type=existing_project_path,
        metavar="<project-dir>",
        help="Project directory to validate. Defaults to the current directory.",
    )
    args = parser.parse_args()
    root = args.path

    failures = []
    for rel in REQUIRED:
        if not (root / rel).exists():
            failures.append(f"missing {rel}")

    evidence_files = current_evidence_files(root)
    if not evidence_files:
        failures.append("missing .tenetora/changes/*-extraction-evidence.json")
    for evidence_file in evidence_files:
        try:
            json.loads(evidence_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            rel = evidence_file.relative_to(root).as_posix()
            failures.append(f"invalid json {rel}: {exc}")

    failures.extend(validate_alignment(root))
    failures.extend(validate_subagent_governance(root))

    for json_rel in [".mcp.json", ".claude/settings.json"]:
        path = root / json_rel
        if path.exists():
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                failures.append(f"invalid json {json_rel}: {exc}")

    for directory in [root / ".tenetora"]:
        if directory.exists():
            for path in directory.rglob("*"):
                if path.is_file():
                    for finding in scan_text(path):
                        failures.append(f"{finding} {path.relative_to(root).as_posix()}")

    agents = root / "AGENTS.md"
    if agents.exists() and ".tenetora/README.md" not in agents.read_text(encoding="utf-8", errors="ignore"):
        failures.append("AGENTS.md does not point to .tenetora/README.md")

    if failures:
        print("治理目录验证失败：" if use_chinese() else "Harness validation failed:")
        for failure in failures:
            print(f"- {localized_failure(failure)}")
        return 1
    print("治理目录验证通过" if use_chinese() else "Harness validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
