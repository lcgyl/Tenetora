#!/usr/bin/env python3
"""Audit whether .tenetora can keep AI agents stable, reliable, controlled, and low-noise."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import alignment_state
import delegation_state
from harness_io import atomic_write_text
from path_security import harness_missing_message, validate_existing_project_path


ENGINEERING_CATEGORIES = ("stability", "reliability", "control", "content_density", "governance_effectiveness")
SCORE_HISTORY_REL = ".tenetora/state/score-history.json"
GOVERNANCE_TRAIL_REL = ".tenetora/state/governance-trail.json"
COMMIT_HOOK_STATE_REL = ".tenetora/state/commit-hooks.json"
HOOK_NAMES = ("pre-commit", "commit-msg")
INSTRUCTION_SEMANTIC_SCAN_EXEMPT_RELS = {GOVERNANCE_TRAIL_REL}
MODULE_EVIDENCE_INDEX_REL = ".tenetora/state/modules/index.json"
CLAIM_PROOF_PATTERN = re.compile(r"^ah-claim-[0-9a-f]{32}$")
ALIGNMENT_PROOF_PATTERN = re.compile(r"^ah-align-[0-9a-f]{32}$")


def use_chinese() -> bool:
    return os.environ.get("TENETORA_LANG", "").strip().lower() in {"zh", "zh-cn", "zh-hans", "chinese", "中文"}


def audit_issue_text(item: dict[str, object]) -> str:
    if not use_chinese():
        return str(item.get("message", ""))
    code = str(item.get("code") or "unknown")
    messages = {
        "subagent-soft-boundary": "子代理仅使用 prompt-only 约束，独立上下文没有权限隔离。",
        "stale-extraction-evidence": "源文件已在最近一次提取证据生成后发生变化。",
        "missing-evidence-derived-guardrail": "提取证据要求的 guardrail 尚未生成。",
        "missing-evidence-source": "最近的提取证据引用了当前无法读取的来源文件。",
        "verification-latest-not-completion": "最近的验证记录不是 completion claim，不能满足完成门禁。",
    }
    return messages.get(code, f"检测到治理问题 `{code}`，请检查对应路径和建议。")


def audit_recommendation_text(value: object) -> str:
    text = str(value)
    if not use_chinese():
        return text
    if "dedicated agent" in text or "host boundary" in text:
        return "优先使用专用 agent，或使用可声明为 hard/inherited 的宿主隔离边界。"
    if "refresh --strategy diff" in text:
        return "运行 `tenetora refresh --strategy diff`，并审查生成的更新产物。"
    if "Refresh the harness evidence" in text:
        return "刷新治理证据，并更新由此派生的 wiki、规则、工作流和 guardrail。"
    if "full verification" in text and "completion" in text:
        return "重新执行完整验证，并在声明完成前记录 `--claim-kind completion`。"
    return text

REQUIRED_FILES = {
    "stability": [
        ".tenetora/README.md",
        ".tenetora/agents/generic.md",
        ".tenetora/agents/researcher.md",
        ".tenetora/agents/planner.md",
        ".tenetora/agents/implementer.md",
        ".tenetora/agents/reviewer.md",
        ".tenetora/docs/architecture/overview.md",
        ".tenetora/docs/conventions/README.md",
        "AGENTS.md",
    ],
    "reliability": [
        ".tenetora/workflows/task-start.md",
        ".tenetora/workflows/verification.md",
        ".tenetora/workflows/completion.md",
        ".tenetora/workflows/decision-alignment.md",
        ".tenetora/wiki/project-map.md",
        ".tenetora/wiki/technology.md",
        ".tenetora/wiki/architecture.md",
        ".tenetora/guardrails/quality-gates.md",
        ".tenetora/guardrails/ci.md",
        ".tenetora/templates/implementation-plan.md",
        ".tenetora/templates/alignment-handoff.md",
        ".tenetora/templates/subagents/code-reviewer.spec.md",
        ".tenetora/templates/subagents/security-auditor.spec.md",
        ".tenetora/templates/subagents/codebase-scout.spec.md",
        ".tenetora/templates/subagents/implementer.spec.md",
    ],
    "control": [
        ".tenetora/rules/project.md",
        ".tenetora/rules/security.md",
        ".tenetora/rules/git.md",
        ".tenetora/rules/prompt-injection.md",
        ".tenetora/rules/subagent-dispatch.md",
        ".tenetora/workflows/untrusted-input.md",
        ".tenetora/skills/prompt-guard.md",
        ".tenetora/skills/decision-interview.md",
        ".tenetora/guardrails/checks/run-all.sh",
        ".tenetora/guardrails/checks/secret-scan.sh",
        ".tenetora/guardrails/checks/local-path-scan.sh",
        ".tenetora/guardrails/checks/stale-doc-scan.sh",
        ".tenetora/automation/worktree-verify.sh",
        ".tenetora/automation/environment-review.md",
    ],
}

CRITICAL_REQUIRED = {
    ".tenetora/README.md",
    ".tenetora/workflows/verification.md",
    ".tenetora/rules/security.md",
    ".tenetora/rules/git.md",
    ".tenetora/guardrails/checks/run-all.sh",
    ".tenetora/guardrails/checks/secret-scan.sh",
    ".tenetora/guardrails/checks/local-path-scan.sh",
    ".tenetora/guardrails/checks/stale-doc-scan.sh",
    "AGENTS.md",
}

SECRET_PATTERNS = [
    re.compile(r"glpat-[A-Za-z0-9._-]+"),
    re.compile(r"(ghp|github_pat)_[A-Za-z0-9_]+"),
    re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
    re.compile(r"\b[A-Z0-9_]*(TOKEN|SECRET|PASSWORD)\b\s*[:=]\s*['\"]?[^'\"\s${}]+", re.I),
    re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
]

STALE_PATTERNS = [
    re.compile(r"<card|</span>|</p>|h[e]rness|READ[E]ME"),
    re.compile(r"(?im)^\s*(?:[-*]\s*)?(?:TODO|TBD)(?:\s*:|\s*$)"),
    re.compile(r"(?i)\b(?:TODO|TBD)\b\s*(?:here|later|placeholder|待补充|待定|补充|完善)"),
]
CLAUDE_LOCAL_OMITTED_MARKERS = (
    "Content omitted because local-only.",
    "Content omitted because the source may contain credentials or local-only configuration.",
    "Content omitted because the local source is not targeted at an ignored harness location.",
)
LOCAL_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.-])(/Users/[^\s\"']+|/home/[^\s\"']+|[A-Za-z]:\\Users\\[^\s\"']+)"
)
SKIP_VERIFICATION_PATTERN = re.compile(r"\b(skip|bypass|ignore)\s+(all\s+)?(tests?|verification|checks?)\b", re.I)
CONTROL_BOUNDARY_PATTERN = re.compile(
    r"\b(do not|don't|never|must not|requires?\b.{0,50}\bapproval|ask\b.{0,50}\b(user|approval|confirmation)|"
    r"explicit\b.{0,30}\bapproval|unless\b.{0,50}\b(approv|confirm|ask))\b",
    re.I,
)
ALLOW_WITHOUT_BOUNDARY_PATTERN = re.compile(
    r"\b(may|can|allowed|should|must|always)\b.{0,120}\bwithout\s+(asking|approval|confirmation)\b",
    re.I,
)
UNCONTROLLED_ACTION_PATTERNS = [
    re.compile(r"\brm\s+-rf\b", re.I),
    re.compile(r"\bgit\s+reset\s+--hard\b", re.I),
    re.compile(r"\bgit\s+checkout\s+--\b", re.I),
    re.compile(r"\bgit\s+push\b", re.I),
    re.compile(r"\b(commit|push)\b.{0,80}\bwithout\s+(asking|approval|confirmation)\b", re.I),
    re.compile(r"\bwithout\s+(asking|approval|confirmation)\b.{0,80}\b(commit|push|delete|remove)\b", re.I),
]
LONG_RUNNING_SCRIPT_NAMES = {
    "dev",
    "preview",
    "serve",
    "server",
    "start",
    "watch",
}
TRANSIENT_HASH_SAMPLES = 2

PROVENANCE_PATHS = [
    ".tenetora/wiki/project-map.md",
    ".tenetora/wiki/technology.md",
    ".tenetora/wiki/architecture.md",
    ".tenetora/docs/architecture/overview.md",
    ".tenetora/docs/architecture/boundaries.md",
    ".tenetora/docs/architecture/data-flow.md",
    ".tenetora/docs/conventions/README.md",
    ".tenetora/guardrails/quality-gates.md",
    ".tenetora/guardrails/ci.md",
]
CONTENT_DENSITY_DIRS = [
    ".tenetora/agents",
    ".tenetora/rules",
    ".tenetora/workflows",
    ".tenetora/wiki",
    ".tenetora/docs",
    ".tenetora/guardrails",
]
PLACEHOLDER_ONLY_PATTERNS = [
    re.compile(r"\bUnknown:\s*(?:Detailed architecture requires source review|Data flow has not been confirmed|Project logging conventions have not been confirmed)", re.I),
    re.compile(r"\bUnknown:\s*(?:No project verification command was detected|No guardrail recipe could be derived)", re.I),
    re.compile(r"\bUnknown:\s*未识别[:：]", re.I),
    re.compile(r"未识别[:：]\s*项目尚无"),
    re.compile(r"\bFill this file\b", re.I),
    re.compile(r"\bplaceholder\b", re.I),
]
GOVERNANCE_CONTEXT_SLOTS = {
    "build": [
        (".tenetora/rules/build-and-deps.md", ".tenetora/rules/project.md"),
        (".tenetora/rules/dependency-change.md", ".tenetora/rules/project-meta.md"),
        (".tenetora/workflows/verification.md",),
    ],
    "test": [
        (".tenetora/rules/testing.md", ".tenetora/rules/verification-claims.md"),
        (".tenetora/workflows/verification.md",),
        (".tenetora/guardrails/quality-gates.md",),
    ],
    "commit": [
        (".tenetora/rules/git-safety.md", ".tenetora/rules/git.md"),
        (".tenetora/rules/security-boundary.md", ".tenetora/rules/security.md"),
        (".tenetora/guardrails/checks/secret-scan.sh",),
    ],
    "external-input": [
        (".tenetora/rules/prompt-injection.md",),
        (".tenetora/workflows/untrusted-input.md",),
        (".tenetora/skills/prompt-guard.md",),
    ],
    "change-impact": [
        (".tenetora/workflows/change-impact-preflight.md",),
    ],
}


def existing_project_path(raw: str) -> Path:
    try:
        return validate_existing_project_path(raw)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def issue(severity: str, category: str, code: str, path: str, message: str, recommendation: str) -> dict[str, str]:
    return {
        "severity": severity,
        "category": category,
        "code": code,
        "path": path,
        "message": message,
        "recommendation": recommendation,
    }


def add_unique(target: list[dict[str, str]], item: dict[str, str]) -> None:
    key = (item["category"], item["code"], item["path"], item["message"])
    if any(
        (existing["category"], existing["code"], existing["path"], existing["message"]) == key
        for existing in target
    ):
        return
    target.append(item)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def read_json_object(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(read_text(path))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def gitignore_has_entry(root: Path, entry: str) -> bool:
    path = root / ".gitignore"
    if not path.exists():
        return False
    normalized = entry.strip().rstrip("/")
    return any(line.strip().rstrip("/") == normalized for line in read_text(path).splitlines())


def claude_local_target(root: Path) -> str:
    if gitignore_has_entry(root, ".tenetora/"):
        return ".tenetora/agents/claude-local.md"
    return ".tenetora/local/agents/claude-local.md"


def claude_local_targets(root: Path) -> tuple[Path, Path]:
    return (
        root / ".tenetora" / "agents" / "claude-local.md",
        root / ".tenetora" / "local" / "agents" / "claude-local.md",
    )


def is_managed_claude_local(text: str) -> bool:
    return "TENETORA_ENTRYPOINT_START name=claude-local" in text


def is_legacy_managed_claude_local(text: str) -> bool:
    return "AGENT_HARNESS_ENTRYPOINT_START name=claude-local" in text


def is_managed_claude(text: str) -> bool:
    return bool(re.search(r"TENETORA_ENTRYPOINT_START name=claude(?:\s|>)", text))


def is_legacy_managed_claude(text: str) -> bool:
    return bool(re.search(r"AGENT_HARNESS_ENTRYPOINT_START name=claude(?:\s|>)", text))


def has_omitted_claude_local_content(text: str) -> bool:
    return "Migrated from `CLAUDE.local.md`" in text and any(marker in text for marker in CLAUDE_LOCAL_OMITTED_MARKERS)


def referenced_claude_local_paths(root: Path, text: str) -> list[Path]:
    paths: list[Path] = []
    for path in claude_local_targets(root):
        if relative(root, path) in text:
            paths.append(path)
    return paths


def harness_files(root: Path) -> list[Path]:
    harness = root / ".tenetora"
    if not harness.exists():
        return []
    return sorted(
        path for path in harness.rglob("*")
        if path.is_file() and ".cache" not in path.relative_to(harness).parts
    )


def scans_current_instruction_semantics(relative_path: str) -> bool:
    """Only current harness instructions can define the active agent policy."""

    normalized = relative_path.replace("\\", "/")
    if normalized in INSTRUCTION_SEMANTIC_SCAN_EXEMPT_RELS:
        return False
    return not normalized.startswith(
        (".tenetora/changes/update-candidates/", ".tenetora/changes/archive/")
    )


def harness_internal_gitignore_has(root: Path, entry: str) -> bool:
    path = root / ".tenetora" / ".gitignore"
    if not path.is_file():
        return False
    normalized = entry.strip().rstrip("/")
    return any(line.strip().rstrip("/") == normalized for line in read_text(path).splitlines())


def latest_evidence_files(root: Path) -> list[Path]:
    pointer = root / ".tenetora" / "state" / "current-evidence.json"
    if pointer.exists():
        try:
            payload = json.loads(read_text(pointer))
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


def load_latest_evidence(root: Path) -> dict[str, object] | None:
    evidence_files = latest_evidence_files(root)
    if not evidence_files:
        return None
    try:
        return json.loads(read_text(evidence_files[-1]))
    except json.JSONDecodeError:
        return None


def module_slug(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip().strip("/"))
    return cleaned.strip("-") or "root"


def load_module_evidence(root: Path, module: str) -> dict[str, object] | None:
    index_path = root / MODULE_EVIDENCE_INDEX_REL
    wanted = module.strip()
    if index_path.exists():
        try:
            payload = json.loads(read_text(index_path))
        except json.JSONDecodeError:
            payload = {}
        modules = payload.get("modules", []) if isinstance(payload, dict) else []
        if isinstance(modules, list):
            for item in modules:
                if not isinstance(item, dict):
                    continue
                names = {
                    str(item.get("module", "")).strip(),
                    str(item.get("display", "")).strip(),
                    str(item.get("slug", "")).strip(),
                }
                if wanted not in names:
                    continue
                evidence_rel = str(item.get("evidence", "")).strip()
                if not evidence_rel:
                    continue
                path = root / evidence_rel
                if not path.is_file():
                    return None
                try:
                    evidence = json.loads(read_text(path))
                except json.JSONDecodeError:
                    return None
                return evidence if isinstance(evidence, dict) else None
    fallback = root / ".tenetora" / "state" / "modules" / module_slug(wanted) / "evidence.json"
    if fallback.is_file():
        try:
            evidence = json.loads(read_text(fallback))
        except json.JSONDecodeError:
            return None
        return evidence if isinstance(evidence, dict) else None
    return None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_file_sha256(path: Path, samples: int = TRANSIENT_HASH_SAMPLES) -> tuple[str, bool]:
    last = ""
    for _ in range(max(1, samples)):
        current = file_sha256(path)
        if last and current == last:
            return current, True
        last = current
    return last, samples <= 1


def audit_required_files(root: Path, blocking: list[dict[str, str]], warnings: list[dict[str, str]]) -> None:
    if not (root / ".tenetora").exists():
        for category in ("stability", "reliability", "control"):
            add_unique(
                blocking,
                issue(
                    "block",
                    category,
                    "missing-harness",
                    ".tenetora",
                    ".tenetora directory is missing.",
                    "In your AI conversation, ask: Use Tenetora to initialize this project.",
                ),
            )
        return

    for category, paths in REQUIRED_FILES.items():
        for rel in paths:
            if (root / rel).exists():
                continue
            severity = "block" if rel in CRITICAL_REQUIRED else "warning"
            target = blocking if severity == "block" else warnings
            add_unique(
                target,
                issue(
                    severity,
                    category,
                    "missing-required-file",
                    rel,
                    f"Required harness file is missing: {rel}.",
                    "Re-run initialization or restore the missing harness document.",
                ),
            )

    evidence_files = latest_evidence_files(root)
    if not evidence_files:
        add_unique(
            blocking,
            issue(
                "block",
                "reliability",
                "missing-extraction-evidence",
                ".tenetora/changes",
                "No extraction evidence JSON was found.",
                "In your AI conversation, ask: Use Tenetora to update or repair this project's governance.",
            ),
        )
    for evidence_file in evidence_files:
        rel = relative(root, evidence_file)
        try:
            json.loads(read_text(evidence_file))
        except json.JSONDecodeError:
            add_unique(
                blocking,
                issue(
                    "block",
                    "reliability",
                    "invalid-extraction-evidence",
                    rel,
                    "Extraction evidence JSON is invalid.",
                    "Regenerate the evidence file or fix the JSON syntax.",
                ),
            )


def audit_content(root: Path, blocking: list[dict[str, str]], warnings: list[dict[str, str]]) -> None:
    for path in harness_files(root):
        rel = relative(root, path)
        text = read_text(path)
        scan_instruction_semantics = scans_current_instruction_semantics(rel)
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            add_unique(
                blocking,
                issue(
                    "block",
                    "control",
                    "secret-in-harness",
                    rel,
                    "Harness content appears to contain a secret or private credential.",
                    "Remove the value, rotate the affected credential, and store secrets outside tracked harness files.",
                ),
            )
        if LOCAL_PATH_PATTERN.search(text):
            add_unique(
                warnings,
                issue(
                    "warning",
                    "control",
                    "local-path-in-harness",
                    rel,
                    "Harness content contains a local absolute path.",
                    "Replace local-only paths with repository-relative paths or environment variables.",
                ),
            )
        if any(pattern.search(text) for pattern in STALE_PATTERNS):
            add_unique(
                warnings,
                issue(
                    "warning",
                    "stability",
                    "stale-content-pattern",
                    rel,
                    "Harness content contains a stale placeholder or copied markup pattern.",
                    "Replace placeholder content with current project guidance.",
                ),
            )
        if scan_instruction_semantics and SKIP_VERIFICATION_PATTERN.search(text):
            add_unique(
                blocking,
                issue(
                    "block",
                    "reliability",
                    "missing-verification-gate",
                    rel,
                    "Harness content allows agents to skip verification.",
                    "Require the smallest meaningful verification command before completion claims.",
                ),
            )
        if scan_instruction_semantics and has_uncontrolled_action(text):
            add_unique(
                blocking,
                issue(
                    "block",
                    "control",
                    "uncontrolled-autonomous-action",
                    rel,
                    "Harness content allows broad destructive, commit, push, or unapproved actions.",
                    "Require explicit user approval for destructive commands, commits, pushes, and broad filesystem changes.",
                ),
            )


def has_uncontrolled_action(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if ALLOW_WITHOUT_BOUNDARY_PATTERN.search(stripped):
            return True
        if any(pattern.search(stripped) for pattern in UNCONTROLLED_ACTION_PATTERNS):
            if CONTROL_BOUNDARY_PATTERN.search(stripped):
                continue
            return True
    return False


def mentions_guardrail_runner(text: str) -> bool:
    return "tenetora run-all" in text or ".tenetora/guardrails/checks/run-all.sh" in text


def audit_runtime_consumption(root: Path, warnings: list[dict[str, str]]) -> None:
    readme = root / ".tenetora" / "README.md"
    if readme.exists():
        text = read_text(readme)
        if "## Runtime Contract" not in text:
            add_unique(
                warnings,
                issue(
                    "warning",
                    "stability",
                    "runtime-contract-missing",
                    ".tenetora/README.md",
                    "README.md does not expose a Runtime Contract for agents to reload after context compaction.",
                    "Add a short Runtime Contract near the top of .tenetora/README.md.",
                ),
            )

    workflow_messages = {
        ".tenetora/workflows/task-start.md": (
            "Task start workflow does not route review, scoring, acceptance, or broad work through harness guardrails."
        ),
        ".tenetora/workflows/verification.md": (
            "Verification workflow does not route review, scoring, acceptance, or broad work through harness guardrails."
        ),
    }
    for rel, message in workflow_messages.items():
        workflow = root / rel
        if workflow.exists() and not mentions_guardrail_runner(read_text(workflow)):
            add_unique(
                warnings,
                issue(
                    "warning",
                    "control",
                    "guardrail-consumption-missing",
                    rel,
                    message,
                    "Document when to run tenetora run-all or .tenetora/guardrails/checks/run-all.sh, and require a reason when skipped.",
                ),
            )

    task_start = root / ".tenetora" / "workflows" / "task-start.md"
    if task_start.is_file():
        text = read_text(task_start)
        if "decision-alignment.md" not in text or "tenetora guard --action alignment" not in text:
            add_unique(
                warnings,
                issue(
                    "warning",
                    "control",
                    "alignment-consumption-missing",
                    ".tenetora/workflows/task-start.md",
                    "Task start does not route high-risk ambiguity through decision alignment and its guard.",
                    "Run `tenetora repair --apply --fix alignment-lifecycle`, then refresh and review the source-preserving task-start candidate.",
                ),
            )


def audit_alignment_state(
    root: Path,
    blocking: list[dict[str, str]],
    warnings: list[dict[str, str]],
) -> None:
    missing_ignores = [
        entry
        for entry in (
            "state/alignment-session.json",
            "state/alignment-sessions/",
            "state/alignment-history/",
            "state/alignment-lifecycle.json",
            "state/.*.lock",
        )
        if not harness_internal_gitignore_has(root, entry)
    ]
    if missing_ignores:
        add_unique(
            warnings,
            issue(
                "warning",
                "stability",
                "alignment-runtime-state-not-ignored",
                ".tenetora/.gitignore",
                "Volatile alignment session, history, or lock state is not fully ignored.",
                "Upgrade Tenetora or run `tenetora repair --apply --fix alignment-lifecycle`.",
            ),
        )
    try:
        sessions = alignment_state.load_all_sessions(root)
        current = alignment_state.load_current(root)
        lifecycle = alignment_state.load_lifecycle(root)
    except alignment_state.AlignmentStateError as exc:
        add_unique(
            blocking,
            issue(
                "block",
                "control",
                "alignment-state-invalid",
                ".tenetora/state",
                f"Decision alignment state is invalid: {exc}",
                "Inspect the damaged state, then resume or abandon it with `tenetora alignment`; realign before high-risk work.",
            ),
        )
        return

    open_goals = [
        record for record in lifecycle.get("records", {}).values() if record.get("status") == "open"
    ]
    if len(open_goals) > alignment_state.MAX_LIFECYCLE_RECORDS:
        add_unique(
            blocking,
            issue(
                "block",
                "control",
                "alignment-lifecycle-over-cap",
                ".tenetora/state/alignment-lifecycle.json",
                "Open alignment goals exceed the hard lifecycle cap.",
                "Archive or complete stale goals before starting another alignment.",
            ),
        )

    for session in sessions:
        status = str(session.get("status"))
        session_id = str(session.get("session_id") or "legacy")
        add_unique(
            warnings,
            issue(
                "warning",
                "control",
                "alignment-session-unresolved",
                f".tenetora/state/alignment-sessions/{session_id}.json",
                f"Decision alignment session is `{status}` and has not produced a confirmed handoff; no conversation was selected implicitly.",
                "Resume, confirm, accept an explicit risk, or abandon the session before restricted high-risk work.",
            ),
        )

    if current is None:
        return
    expected_fingerprint = str(current.get("goal_fingerprint", ""))
    actual_fingerprint = alignment_state.goal_fingerprint(
        str(current.get("goal", "")),
        list(current.get("scope", [])),
        list(current.get("non_goals", [])),
        list(current.get("acceptance_criteria", [])),
    )
    expected_hash = str(current.get("handoff_hash", ""))
    relative = current.get("handoff_relative_path")
    invalid_path = False
    if relative:
        path = Path(str(relative))
        invalid_path = path.is_absolute() or ".." in path.parts or not (root / path).is_file()
    if (
        alignment_state.is_stale(expected_fingerprint, actual_fingerprint)
        or not expected_hash
        or expected_hash != alignment_state.handoff_hash(current)
        or invalid_path
    ):
        add_unique(
            warnings,
            issue(
                "warning",
                "reliability",
                "alignment-handoff-stale",
                ".tenetora/state/current-alignment.json",
                "Current alignment fingerprint, handoff hash, or relative document pointer is stale.",
                "Ask the user to invoke `tenetora-align` for the current goal, then regenerate the handoff.",
            ),
        )


def audit_subagent_governance(
    root: Path,
    blocking: list[dict[str, str]],
    warnings: list[dict[str, str]],
) -> None:
    if not (
        harness_internal_gitignore_has(root, ".cache/")
        or harness_internal_gitignore_has(root, ".cache/subagents/")
    ):
        add_unique(
            blocking,
            issue(
                "block",
                "control",
                "subagent-report-cache-not-ignored",
                ".tenetora/.gitignore",
                "Temporary subagent reports are not excluded from shared harness content.",
                "Run `tenetora repair --apply --fix subagent-governance` before recording reports.",
            ),
        )
    if not harness_internal_gitignore_has(root, "state/delegation-state.json"):
        add_unique(
            warnings,
            issue(
                "warning",
                "stability",
                "delegation-runtime-state-not-ignored",
                ".tenetora/.gitignore",
                "Volatile delegation runtime state is not ignored.",
                "Run `tenetora repair --apply --fix subagent-governance`.",
            ),
        )

    state_file = root / delegation_state.STATE_REL
    if state_file.is_file():
        try:
            delegation = delegation_state.load_state(root)
        except delegation_state.DelegationError as exc:
            add_unique(
                blocking,
                issue(
                    "block",
                    "control",
                    "delegation-state-invalid",
                    delegation_state.STATE_REL,
                    f"Delegation state is invalid: {exc}",
                    "Inspect the damaged runtime state and rerun `tenetora delegation --status` after recovery.",
                ),
            )
        else:
            for dispatch in delegation.get("dispatches", []):
                if not isinstance(dispatch, dict):
                    continue
                role = str(dispatch.get("role") or "")
                contract_status = str(dispatch.get("contract_status") or "unknown")
                resolution_mode = str(dispatch.get("resolution_mode") or "unspecified")
                if role in {"security-auditor", "implementer"} and contract_status == "soft-boundary":
                    add_unique(
                        blocking,
                        issue(
                            "block",
                            "control",
                            "strict-subagent-soft-boundary",
                            delegation_state.STATE_REL,
                            f"{role} dispatch used prompt-only enforcement, which cannot satisfy its role contract.",
                            "Record the attempt unavailable, then use a hard or contract-matching inherited boundary.",
                        ),
                    )
                elif role in {"code-reviewer", "codebase-scout"} and contract_status == "soft-boundary":
                    add_unique(
                        warnings,
                        issue(
                            "warning",
                            "control",
                            "subagent-soft-boundary",
                            delegation_state.STATE_REL,
                            f"{role} used prompt-only enforcement; the independent context was not permission-isolated.",
                            "Prefer a dedicated agent or a host boundary that can be declared hard or inherited.",
                        ),
                    )
                if resolution_mode != "unspecified" and not dispatch.get("role_contract_hash"):
                    add_unique(
                        blocking,
                        issue(
                            "block",
                            "reliability",
                            "subagent-contract-hash-missing",
                            delegation_state.STATE_REL,
                            "An explicitly resolved dispatch has no role contract hash.",
                            "Rerun the dispatch through the current `tenetora delegation` CLI.",
                        ),
                    )

    loop = read_json_object(root / ".tenetora" / "state" / "loop-state.json")
    review = loop.get("review_cycle") if isinstance(loop, dict) else None
    if not isinstance(review, dict):
        add_unique(
            warnings,
            issue(
                "warning",
                "control",
                "subagent-review-cycle-missing",
                ".tenetora/state/loop-state.json",
                "Bounded subagent review-cycle state is missing.",
                "Run `tenetora repair --apply --fix subagent-governance`.",
            ),
        )
    elif (
        review.get("max_review_rounds") != 3
        or review.get("max_fix_rounds") != 2
        or int(review.get("round", 0)) > 3
        or int(review.get("fix_rounds", 0)) > 2
    ):
        add_unique(
            blocking,
            issue(
                "block",
                "control",
                "subagent-review-cycle-unbounded",
                ".tenetora/state/loop-state.json",
                "Subagent review-cycle limits are missing or exceed the supported 3 review / 2 repair boundary.",
                "Repair loop state before allowing implementer dispatch.",
            ),
        )

    report_dir = root / delegation_state.REPORT_DIR_REL
    reports = list(report_dir.glob("*.md")) if report_dir.is_dir() else []
    if len(reports) > delegation_state.MAX_REPORTS:
        add_unique(
            warnings,
            issue(
                "warning",
                "stability",
                "subagent-report-cache-over-limit",
                delegation_state.REPORT_DIR_REL,
                f"Subagent report cache contains {len(reports)} files; the supported limit is {delegation_state.MAX_REPORTS}.",
                "Run `tenetora delegation --cleanup`.",
            ),
        )


def audit_entrypoints(root: Path, blocking: list[dict[str, str]], warnings: list[dict[str, str]]) -> None:
    agents = root / "AGENTS.md"
    if agents.exists() and ".tenetora/README.md" not in read_text(agents):
        add_unique(
            blocking,
            issue(
                "block",
                "stability",
                "entrypoint-not-linked",
                "AGENTS.md",
                "AGENTS.md does not point agents to .tenetora/README.md.",
                "Keep AGENTS.md as a thin compatibility entry that directs tools into .tenetora.",
            ),
        )

    for rel in ["AGENTS.md", "CLAUDE.md", "CLAUDE.local.md"]:
        path = root / rel
        if path.exists() and has_managed_entrypoint_drift(read_text(path)):
            add_unique(
                warnings,
                issue(
                    "warning",
                    "stability",
                    "entrypoint-drift-risk",
                    rel,
                    "Managed entrypoint has content outside the Tenetora block and may drift from .tenetora.",
                    "Move stable rules into .tenetora and run tenetora refresh --migrate merge --entrypoints merge.",
                ),
            )

    audit_claude_entrypoint(root, warnings)
    audit_claude_local_entrypoint(root, warnings)

    for rel in [".cursor/rules", ".claude/rules"]:
        path = root / rel
        if path.exists() and path.is_dir() and not tool_rule_directory_is_mirrored(root, rel):
            add_unique(
                warnings,
                issue(
                    "warning",
                    "stability",
                    "tool-rule-drift-risk",
                    rel,
                    "Tool-specific rule directory exists and may drift from .tenetora.",
                    "Keep tool-specific rules thin or mirror the rule into .tenetora/rules with provenance.",
                ),
            )


def audit_claude_entrypoint(root: Path, warnings: list[dict[str, str]]) -> None:
    source = root / "CLAUDE.md"
    if not source.exists():
        return
    source_text = read_text(source)
    if is_legacy_managed_claude(source_text):
        add_unique(
            warnings,
            issue(
                "warning",
                "stability",
                "claude-entrypoint-legacy",
                "CLAUDE.md",
                "CLAUDE.md still uses the retired Agent Harness entrypoint marker.",
                "Run tenetora repair --apply --fix claude-entrypoint to rewrite only the thin adapter.",
            ),
        )
    elif source_text.strip() and not is_managed_claude(source_text):
        add_unique(
            warnings,
            issue(
                "warning",
                "stability",
                "claude-entrypoint-unmanaged",
                "CLAUDE.md",
                "CLAUDE.md contains substantive Claude instructions outside Tenetora governance.",
                "Run tenetora repair --check, then apply the reported claude-entrypoint repair if available.",
            ),
        )


def audit_claude_local_entrypoint(root: Path, warnings: list[dict[str, str]]) -> None:
    source = root / "CLAUDE.local.md"
    if not source.exists():
        return
    source_text = read_text(source)
    expected_target = claude_local_target(root)
    if is_legacy_managed_claude_local(source_text):
        add_unique(
            warnings,
            issue(
                "warning",
                "stability",
                "claude-local-entrypoint-legacy",
                "CLAUDE.local.md",
                "CLAUDE.local.md still uses the retired Agent Harness entrypoint marker.",
                "Run tenetora repair --apply --fix claude-local-entrypoint to rewrite only the thin adapter.",
            ),
        )
    elif not is_managed_claude_local(source_text) and source_text.strip():
        add_unique(
            warnings,
            issue(
                "warning",
                "stability",
                "claude-local-entrypoint-unmanaged",
                "CLAUDE.local.md",
                "CLAUDE.local.md contains substantive local instructions outside Tenetora governance.",
                "Run tenetora repair --check, then apply the reported claude-local-entrypoint repair if available.",
            ),
        )
    if (is_managed_claude_local(source_text) or is_legacy_managed_claude_local(source_text)) and expected_target not in source_text:
        add_unique(
            warnings,
            issue(
                "warning",
                "stability",
                "claude-local-target-mismatch",
                "CLAUDE.local.md",
                "CLAUDE.local.md routes to a harness target that does not match the current .tenetora git policy.",
                "Run tenetora repair --check, then apply the reported claude-local-entrypoint repair if available.",
            ),
        )

    relevant_paths = {root / expected_target}
    relevant_paths.update(referenced_claude_local_paths(root, source_text))
    for path in sorted(relevant_paths):
        if not path.exists():
            continue
        text = read_text(path)
        if has_omitted_claude_local_content(text):
            add_unique(
                warnings,
                issue(
                    "warning",
                    "reliability",
                    "claude-local-migration-incomplete",
                    relative(root, path),
                    "CLAUDE.local.md was routed into .tenetora but the migrated local content is still an omitted placeholder.",
                    "Run tenetora repair --check, then apply the reported claude-local-entrypoint repair if a backup is available.",
                ),
            )


def has_managed_entrypoint_drift(text: str) -> bool:
    if "TENETORA_ENTRYPOINT_START" not in text and "AGENT_HARNESS_ENTRYPOINT_START" not in text:
        return False
    match = re.search(r"<!-- (?:TENETORA|AGENT_HARNESS)_ENTRYPOINT_END name=[^>]+ -->", text)
    if not match:
        return True
    trailing = text[match.end():].strip()
    return bool(trailing)


def tool_rule_directory_is_mirrored(root: Path, rel: str) -> bool:
    source_dir = root / rel
    source_files = [
        relative(root, path)
        for path in sorted(source_dir.glob("*"))
        if path.is_file()
    ]
    if not source_files:
        return True

    rules_dir = root / ".tenetora" / "rules"
    if not rules_dir.exists():
        return False
    harness_rule_text = "\n".join(read_text(path) for path in sorted(rules_dir.glob("*.md")) if path.is_file())
    return all(
        f"Migrated from `{source}`" in harness_rule_text or f"Source: {source}" in harness_rule_text
        for source in source_files
    )


def audit_provenance(root: Path, warnings: list[dict[str, str]]) -> None:
    for rel in PROVENANCE_PATHS:
        path = root / rel
        if not path.exists():
            continue
        text = read_text(path)
        if not any(marker in text for marker in ("Source:", "Inference:", "Unknown:")):
            add_unique(
                warnings,
                issue(
                    "warning",
                    "reliability",
                    "missing-provenance-marker",
                    rel,
                    "Harness knowledge has no Source, Inference, or Unknown marker.",
                    "Add provenance so agents can distinguish facts, inferences, and unknowns.",
                ),
            )


def markdown_signal_lines(text: str) -> list[str]:
    signal = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if line.startswith("Source:"):
            continue
        if line.startswith("Inference:"):
            continue
        signal.append(line)
    return signal


def is_placeholder_only_markdown(text: str) -> bool:
    if not any(pattern.search(text) for pattern in PLACEHOLDER_ONLY_PATTERNS):
        return False
    signal = markdown_signal_lines(text)
    if len(signal) <= 3:
        return True
    bullet_count = sum(1 for line in signal if line.startswith(("-", "*", "1.")))
    return bullet_count == 0 and len("\n".join(signal)) < 240


def audit_content_density(root: Path, warnings: list[dict[str, str]]) -> None:
    for rel in CONTENT_DENSITY_DIRS:
        directory = root / rel
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*.md")):
            if ".tenetora/changes/" in relative(root, path):
                continue
            text = read_text(path)
            if not is_placeholder_only_markdown(text):
                continue
            add_unique(
                warnings,
                issue(
                    "warning",
                    "content_density",
                    "low-content-density",
                    relative(root, path),
                    "A runtime harness document is placeholder-only and adds little usable context for agents.",
                    "Replace it with project-specific guidance or remove it from the runtime harness.",
                ),
            )


def audit_verification_depth(root: Path, warnings: list[dict[str, str]]) -> None:
    evidence_files = latest_evidence_files(root)
    detected_commands: list[str] = []
    for evidence_file in evidence_files:
        try:
            payload = json.loads(read_text(evidence_file))
        except json.JSONDecodeError:
            continue
        commands = payload.get("verification_commands", [])
        if isinstance(commands, list):
            for item in commands:
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("command"), str)
                    and not is_long_running_command(item)
                ):
                    detected_commands.append(item["command"])

    if detected_commands:
        verification = root / ".tenetora" / "workflows" / "verification.md"
        text = read_text(verification) if verification.exists() else ""
        missing = [command for command in detected_commands if command not in text]
        if missing:
            add_unique(
                warnings,
                issue(
                    "warning",
                    "reliability",
                    "detected-command-not-documented",
                    ".tenetora/workflows/verification.md",
                    "Detected verification commands are not documented in the workflow.",
                    "Add detected project-native commands to .tenetora/workflows/verification.md.",
                ),
            )
    else:
        add_unique(
            warnings,
            issue(
                "warning",
                "reliability",
                "no-project-verification-command",
                ".tenetora/workflows/verification.md",
                "No project-native verification command was detected.",
                "Add real build, test, lint, or compile commands once the project has them.",
            ),
        )


def audit_project_map_quality(root: Path, warnings: list[dict[str, str]]) -> None:
    evidence = load_latest_evidence(root)
    if not evidence or evidence.get("classification") != "existing":
        return
    project_map = root / ".tenetora" / "wiki" / "project-map.md"
    if not project_map.exists():
        return
    text = read_text(project_map)
    required_markers = [
        "## Project Summary",
        "## Module",
        "Source:",
    ]
    has_command = "`" in text and any(
        isinstance(item, dict) and str(item.get("command", "")) in text
        for item in evidence.get("verification_commands", [])
    )
    if all(marker in text for marker in required_markers) and has_command:
        return
    add_unique(
        warnings,
        issue(
            "warning",
            "reliability",
            "weak-project-map-quality",
            ".tenetora/wiki/project-map.md",
            "Project map does not preserve extracted module structure, provenance, and verification hints.",
            "Refresh or edit .tenetora/wiki/project-map.md so agents can navigate modules with Source markers and commands.",
        ),
    )


def audit_evidence_drift(root: Path, warnings: list[dict[str, str]], module: str | None = None) -> None:
    evidence = load_module_evidence(root, module) if module else load_latest_evidence(root)
    if not evidence:
        if module:
            add_unique(
                warnings,
                issue(
                    "warning",
                    "reliability",
                    "missing-module-evidence",
                    f".tenetora/state/modules/{module_slug(module)}/evidence.json",
                    "Requested module evidence is missing or invalid.",
                    "Run tenetora refresh --strategy diff or init --write to regenerate module evidence state.",
                ),
            )
        return
    hashes = evidence.get("source_hashes", [])
    if not isinstance(hashes, list):
        return
    for item in hashes:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source", "")).strip()
        expected = str(item.get("sha256", "")).strip()
        source_kind = str(item.get("source_kind", "project-source")).strip() or "project-source"
        if source_kind != "project-source" or source.startswith(".tenetora/"):
            continue
        if not source or not expected:
            continue
        path = root / source
        if not path.exists():
            add_unique(
                warnings,
                issue(
                    "warning",
                    "reliability",
                    "missing-evidence-source",
                    source,
                    "A source file referenced by extraction evidence no longer exists.",
                    "Refresh the harness evidence and update derived wiki, rules, workflows, and guardrails.",
                ),
            )
            continue
        if not path.is_file():
            continue
        try:
            current, stable = stable_file_sha256(path)
        except OSError:
            continue
        if not stable:
            add_unique(
                warnings,
                issue(
                    "warning",
                    "control",
                    "transient-source-jitter",
                    source,
                    "A source file changed while evidence freshness was being checked.",
                    "Re-run audit after the IDE or formatter is idle; do not refresh evidence from an unstable file snapshot.",
                ),
            )
            continue
        if current != expected:
            add_unique(
                warnings,
                issue(
                    "warning",
                    "reliability",
                    "stale-extraction-evidence",
                    source,
                    "A source file has changed since the latest extraction evidence was generated.",
                    "Run tenetora refresh --strategy diff and review the generated update artifacts.",
                ),
            )


def audit_guardrail_recipes(root: Path, warnings: list[dict[str, str]]) -> None:
    evidence = load_latest_evidence(root)
    if not evidence:
        return
    recipes = evidence.get("guardrail_recipes", [])
    if not isinstance(recipes, list) or not recipes:
        return
    quality_gates = root / ".tenetora" / "guardrails" / "quality-gates.md"
    text = read_text(quality_gates) if quality_gates.exists() else ""
    for item in recipes:
        if not isinstance(item, dict):
            continue
        recipe_type = str(item.get("type", "")).strip()
        command = str(item.get("command", "")).strip()
        if not recipe_type:
            continue
        if recipe_type not in text or (command and command not in text):
            add_unique(
                warnings,
                issue(
                    "warning",
                    "control",
                    "guardrail-recipe-not-documented",
                    ".tenetora/guardrails/quality-gates.md",
                    "A guardrail recipe from extraction evidence is not documented in quality gates.",
                    "Regenerate or update .tenetora/guardrails/quality-gates.md so recipe sources, commands, and status are visible.",
                ),
            )
            return


def audit_changes_runtime_noise(root: Path, warnings: list[dict[str, str]]) -> None:
    changes = root / ".tenetora" / "changes"
    if not changes.exists():
        return
    top_level_files = [
        path
        for path in changes.iterdir()
        if path.is_file() and path.name not in {"README.md", "INDEX.md"}
    ]
    if len(top_level_files) <= 12:
        return
    add_unique(
        warnings,
        issue(
            "warning",
            "stability",
            "changes-runtime-noise",
            ".tenetora/changes",
            ".tenetora/changes has many top-level files, which makes current runtime context harder for agents to consume.",
            "Keep only current summaries at the top level and archive older reports under .tenetora/changes/archive/.",
        ),
    )


def load_governance_trail(root: Path) -> dict[str, object] | None:
    path = root / GOVERNANCE_TRAIL_REL
    if not path.is_file():
        return None
    try:
        payload = json.loads(read_text(path))
    except json.JSONDecodeError:
        return {"version": 1, "events": [], "invalid": True}
    if not isinstance(payload, dict):
        return {"version": 1, "events": [], "invalid": True}
    events = payload.get("events")
    if not isinstance(events, list):
        payload["events"] = []
        payload["invalid"] = True
    return payload


def configured_tenetora_git_hooks(root: Path) -> set[str]:
    configured: set[str] = set()
    for rel in (
        ".pre-commit-config.yaml",
        ".pre-commit-config.tenetora.yaml",
        ".pre-commit-config.agent-harness.yaml",
    ):
        text = read_text(root / rel)
        if "tenetora guard --action commit" in text:
            configured.add("pre-commit")
        if "tenetora guard --action commit" in text and (
            "--commit-message-file" in text or "stages: [commit-msg]" in text
        ):
            configured.add("commit-msg")
    return configured


def git_hooks_directory(root: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-path", "hooks"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        raw = Path(result.stdout.strip())
        return raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    fallback = root / ".git" / "hooks"
    return fallback if (root / ".git").exists() else None


def git_hook_installed(root: Path, hook_name: str) -> bool:
    hooks_dir = git_hooks_directory(root)
    if hooks_dir is None:
        return False
    hook = hooks_dir / hook_name
    if not hook.is_file():
        return False
    text = read_text(hook)
    if "# Tenetora managed hook" in text or "tenetora guard --action commit" in text:
        return True
    return "pre-commit" in text and hook_name in configured_tenetora_git_hooks(root)


def legacy_git_hook_runtime(root: Path) -> list[str]:
    paths: list[str] = []
    for rel in (
        ".pre-commit-config.yaml",
        ".pre-commit-config.agent-harness.yaml",
    ):
        text = read_text(root / rel)
        if "agent-harness guard --action commit" in text or "agent-harness-commit" in text:
            paths.append(rel)
    hooks_dir = git_hooks_directory(root)
    if hooks_dir is not None:
        for hook_name in HOOK_NAMES:
            hook = hooks_dir / hook_name
            text = read_text(hook)
            if (
                "# Agent Harness managed hook" in text
                or ".agent-harness/bin/agent-harness" in text
                or "agent-harness guard --action commit" in text
            ):
                paths.append(f".git/hooks/{hook_name}")
    return sorted(set(paths))


def audit_hook_visibility(root: Path, warnings: list[dict[str, str]]) -> None:
    if git_hooks_directory(root) is None:
        return
    legacy_paths = legacy_git_hook_runtime(root)
    if legacy_paths:
        add_unique(
            warnings,
            issue(
                "warning",
                "governance_effectiveness",
                "legacy-commit-hook-runtime",
                ", ".join(legacy_paths),
                "Retired Agent Harness Git hook configuration is present and does not count as an active Tenetora guard.",
                "Run `tenetora hooks --install` to back up and rewrite managed hooks with the canonical CLI.",
            ),
        )
    if all(git_hook_installed(root, hook_name) for hook_name in HOOK_NAMES):
        return
    configured = configured_tenetora_git_hooks(root)
    if configured:
        expected = configured
    else:
        state = read_json_object(root / COMMIT_HOOK_STATE_REL)
        decision = str(state.get("decision", "pending")) if isinstance(state, dict) else "pending"
        if decision == "decline":
            return
        if decision in {"pending", "defer"}:
            code = "commit-hooks-deferred" if decision == "defer" else "commit-hooks-decision-pending"
            message = (
                "Tenetora commit hook installation was deferred and will be offered again."
                if decision == "defer"
                else "Tenetora commit hooks are not installed and the local installation decision is pending."
            )
            add_unique(
                warnings,
                issue(
                    "warning",
                    "governance_effectiveness",
                    code,
                    COMMIT_HOOK_STATE_REL,
                    message,
                    "Choose `tenetora hooks --install`, `tenetora hooks --defer`, or "
                    "`tenetora hooks --decline`; active installation remains available after declining reminders.",
                ),
            )
            return
        expected = set(HOOK_NAMES) if decision == "install" else set()

    for hook_name in sorted(expected):
        if git_hook_installed(root, hook_name):
            continue
        add_unique(
            warnings,
            issue(
                "warning",
                "governance_effectiveness",
                f"{hook_name}-hook-not-installed",
                f".git/hooks/{hook_name}",
                f"Tenetora {hook_name} configuration exists, but the local Git hook is not installed.",
                "Run `tenetora hooks --install` to install both governed hooks, or "
                "`tenetora hooks --decline` to disable local reminders without removing the active command.",
            ),
        )


def slot_has_coverage(root: Path, alternatives: tuple[str, ...]) -> bool:
    return any((root / rel).is_file() for rel in alternatives)


def latest_route_epoch(events: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return events belonging to the latest routed task, or the whole trail."""
    latest_route = -1
    for index, event in enumerate(events):
        if event.get("type") == "route":
            latest_route = index
    return events[latest_route + 1 :]


def latest_route_event(events: list[dict[str, object]]) -> dict[str, object] | None:
    for event in reversed(events):
        if event.get("type") == "route":
            return event
    return None


def audit_change_impact_lifecycle(events: list[dict[str, object]], warnings: list[dict[str, str]]) -> None:
    route = latest_route_event(events)
    contexts = route.get("recommended_contexts") if isinstance(route, dict) else None
    if not isinstance(contexts, list) or "change-impact" not in contexts:
        return
    preflights = [
        event
        for event in latest_route_epoch(events)
        if event.get("type") == "change-impact-preflight"
    ]
    if not preflights:
        lifecycle = "missing"
    else:
        latest = preflights[-1]
        lifecycle = str(latest.get("lifecycle_status") or "invalid")
        if lifecycle == "completed" and latest.get("status") == "pass":
            return
    messages = {
        "missing": "The latest routed task recommends change-impact analysis, but no preflight lifecycle event is recorded.",
        "active": "The latest routed task has an active change-impact preflight that has not been completed.",
        "failed": "The latest routed task has a failed change-impact preflight.",
        "cancelled": "The latest routed task cancelled its change-impact preflight without completed evidence.",
    }
    add_unique(
        warnings,
        issue(
            "warning",
            "governance_effectiveness",
            f"change-impact-preflight-{lifecycle}",
            ".tenetora/state/change-impact.json",
            messages.get(lifecycle, f"The latest routed task has an invalid change-impact lifecycle state: {lifecycle}."),
            "Run `tenetora impact --start ...` before editing, then `tenetora impact --complete ...` after structural rescan and verification.",
        ),
    )


def unresolved_high_risk_alignment_guard(events: list[dict[str, object]]) -> bool:
    """Detect a real high-risk guard failure without accepting weaker passes."""
    guards = [
        event
        for event in latest_route_epoch(events)
        if event.get("type") == "alignment-guard" and event.get("risk_level") == "high"
    ]
    if not guards:
        return False
    latest = guards[-1]
    return latest.get("status") != "pass" or not ALIGNMENT_PROOF_PATTERN.match(
        str(latest.get("alignment_proof", ""))
    )


def audit_governance_effectiveness(root: Path, warnings: list[dict[str, str]]) -> None:
    for context, slots in GOVERNANCE_CONTEXT_SLOTS.items():
        if any(slot_has_coverage(root, alternatives) for alternatives in slots):
            continue
        add_unique(
            warnings,
            issue(
                "warning",
                "governance_effectiveness",
                "missing-governance-context-coverage",
                ".tenetora/rules",
                f"No rule, workflow, or guardrail file covers the `{context}` task context.",
                f"Add project-specific rules or workflows for `{context}`, or run `tenetora rules --context {context}` to inspect missing slots.",
            ),
        )

    trail = load_governance_trail(root)
    if trail is None:
        add_unique(
            warnings,
            issue(
                "warning",
                "governance_effectiveness",
                "governance-trail-missing",
                GOVERNANCE_TRAIL_REL,
                "No governance trail is recorded, so audit cannot tell whether agents consumed rules, guards, or verification gates.",
                "Use `tenetora rules --context <type>` and `tenetora guard --action <action>` during agent work.",
            ),
        )
        return
    if trail.get("invalid"):
        add_unique(
            warnings,
            issue(
                "warning",
                "governance_effectiveness",
                "governance-trail-invalid",
                GOVERNANCE_TRAIL_REL,
                "The governance trail exists but is not valid JSON with an events array.",
                "Regenerate the trail by using `tenetora rules` and `tenetora guard`; remove the invalid file after review.",
            ),
        )
        return
    events = [item for item in trail.get("events", []) if isinstance(item, dict)]
    if not events:
        add_unique(
            warnings,
            issue(
                "warning",
                "governance_effectiveness",
                "governance-trail-empty",
                GOVERNANCE_TRAIL_REL,
                "The governance trail has no events, so rule and guardrail consumption cannot be measured.",
                "Use `tenetora rules --context <type>` and `tenetora guard --action <action>` during agent work.",
            ),
        )
        return
    event_types = {str(item.get("type", "")) for item in events}
    audit_change_impact_lifecycle(events, warnings)
    if unresolved_high_risk_alignment_guard(events):
        add_unique(
            warnings,
            issue(
                "warning",
                "governance_effectiveness",
                "alignment-guard-history-missing",
                GOVERNANCE_TRAIL_REL,
                "The latest routed task has an unresolved high-risk alignment guard without a valid alignment proof.",
                "Complete the alignment lifecycle and rerun `tenetora guard --action alignment --goal '<goal>' --risk-level high` before restricted execution; a low-risk pass cannot clear this warning.",
            ),
        )
    if "rules-context" not in event_types:
        add_unique(
            warnings,
            issue(
                "warning",
                "governance_effectiveness",
                "rules-context-history-missing",
                GOVERNANCE_TRAIL_REL,
                "No rules-context event is recorded; agents may be bypassing task-specific rule loading.",
                "Call `tenetora rules --context <build|test|commit|security|external-input|harness>` before task-specific work.",
            ),
        )
    if "guard-action" not in event_types:
        add_unique(
            warnings,
            issue(
                "warning",
                "governance_effectiveness",
                "guard-action-history-missing",
                GOVERNANCE_TRAIL_REL,
                "No guard-action event is recorded; action-triggered guardrails may not be used before risky actions.",
                "Call `tenetora guard --action commit|rules|external-input` at the matching decision points.",
            ),
        )
    claim_events = [item for item in events if item.get("type") == "verification-claim"]
    completion_events = [item for item in claim_events if item.get("claim_kind") == "completion"]
    if not completion_events:
        add_unique(
            warnings,
            issue(
                "warning",
                "governance_effectiveness",
                "verification-completion-history-missing",
                GOVERNANCE_TRAIL_REL,
                "No completion verification-claim event is recorded; partial checks cannot prove task completion.",
                "Run `tenetora guard --action claim --claim-kind completion --verification-command '<full-command>' --verification-status passed` after the full verification command.",
            ),
        )
    else:
        latest_claim = claim_events[-1] if claim_events else {}
        if latest_claim.get("claim_kind") in {"failed", "blocked"} or latest_claim.get("verification_status") != "passed":
            add_unique(
                warnings,
                issue(
                    "warning",
                    "governance_effectiveness",
                    "verification-latest-failed",
                    GOVERNANCE_TRAIL_REL,
                    "The latest verification event is not passed; an older completion claim cannot cover the newer failure or block.",
                    "Report the latest failure or blocker, then run a new full completion verification before claiming completion.",
                ),
            )
        elif latest_claim.get("claim_kind") != "completion":
            add_unique(
                warnings,
                issue(
                    "warning",
                    "governance_effectiveness",
                    "verification-latest-not-completion",
                    GOVERNANCE_TRAIL_REL,
                    "The latest verification event is only partial; it cannot satisfy the completion gate.",
                    "Run a new full verification and record a claim with --claim-kind completion before reporting completion.",
                ),
            )
        elif not CLAIM_PROOF_PATTERN.match(str(latest_claim.get("claim_proof", ""))):
            add_unique(
                warnings,
                issue(
                    "warning",
                    "governance_effectiveness",
                    "verification-claim-proof-missing",
                    GOVERNANCE_TRAIL_REL,
                    "The latest completion verification-claim event lacks a valid Tenetora claim proof, so completion evidence can be spoofed.",
                    "Use `tenetora guard --action claim --claim-kind completion --verification-command '<full-command>' --verification-status passed` and include the returned claim proof in the completion report.",
                ),
            )


def is_long_running_command(command: dict[str, object]) -> bool:
    name_parts = {
        part
        for part in re.split(r"[-_:./\s]+", str(command.get("name", "")).lower())
        if part
    }
    return bool(name_parts.intersection(LONG_RUNNING_SCRIPT_NAMES))


def score_category(category: str, blocking: list[dict[str, str]], warnings: list[dict[str, str]]) -> int:
    score = 100
    score -= 35 * sum(1 for item in blocking if item["category"] == category)
    score -= 10 * sum(1 for item in warnings if item["category"] == category)
    return max(0, min(100, score))


def status_from(scores: dict[str, int], blocking: list[dict[str, str]], warnings: list[dict[str, str]]) -> str:
    if blocking:
        return "fail"
    if warnings or scores["overall"] < 90:
        return "warn"
    return "pass"


def dimension_status(category: str, blocking: list[dict[str, str]], warnings: list[dict[str, str]], score: int) -> str:
    if any(item["category"] == category for item in blocking):
        return "fail"
    if any(item["category"] == category for item in warnings) or score < 90:
        return "warn"
    return "pass"


def dimension_summary(category: str, status: str) -> str:
    summaries = {
        "stability": {
            "pass": "entry map, project navigation, and instruction shape are stable enough for agent work",
            "warn": "entry map or navigation needs attention before agents can work consistently",
            "fail": "entry map or instruction structure can cause unstable agent behavior",
        },
        "reliability": {
            "pass": "evidence, provenance, project map, and verification workflow are reliable enough for agent work",
            "warn": "evidence, provenance, project map, or verification workflow needs attention",
            "fail": "missing evidence, provenance, or verification can make agent output unreliable",
        },
        "control": {
            "pass": "guardrail checks, secret controls, and approval boundaries are present",
            "warn": "guardrail wiring or control boundaries need attention",
            "fail": "missing guardrail checks or unsafe instructions can let agents act outside control boundaries",
        },
        "content_density": {
            "pass": "runtime harness documents contain enough project-specific signal for agent work",
            "warn": "runtime harness documents contain placeholder-only or low-signal content",
            "fail": "runtime harness content density is too weak for reliable agent consumption",
        },
        "governance_effectiveness": {
            "pass": "rules, action guards, and verification gates are being consumed and recorded by agent work",
            "warn": "governance mechanisms exist but consumed rule/guard/verification history is incomplete or unrecorded",
            "fail": "rules and guardrails are not measurably consumed by agent work",
        },
    }
    return summaries[category][status]


def dimension_report(
    scores: dict[str, int],
    blocking: list[dict[str, str]],
    warnings: list[dict[str, str]],
) -> dict[str, dict[str, object]]:
    report: dict[str, dict[str, object]] = {}
    for category in ENGINEERING_CATEGORIES:
        status = dimension_status(category, blocking, warnings, scores[category])
        report[category] = {
            "status": status,
            "score": scores[category],
            "issues": [
                item["code"]
                for item in [*blocking, *warnings]
                if item["category"] == category
            ],
            "summary": dimension_summary(category, status),
        }
    return report


def audit_engineering(root: Path, module: str | None = None) -> dict[str, object]:
    root = root.resolve()
    blocking: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    audit_required_files(root, blocking, warnings)
    audit_content(root, blocking, warnings)
    audit_runtime_consumption(root, warnings)
    audit_alignment_state(root, blocking, warnings)
    audit_subagent_governance(root, blocking, warnings)
    audit_entrypoints(root, blocking, warnings)
    audit_provenance(root, warnings)
    audit_content_density(root, warnings)
    audit_verification_depth(root, warnings)
    audit_evidence_derived_guardrails(root, warnings)
    audit_guardrail_recipes(root, warnings)
    audit_changes_runtime_noise(root, warnings)
    audit_project_map_quality(root, warnings)
    audit_evidence_drift(root, warnings, module=module)
    audit_governance_effectiveness(root, warnings)
    audit_hook_visibility(root, warnings)

    scores = {
        category: score_category(category, blocking, warnings)
        for category in ENGINEERING_CATEGORIES
    }
    scores["overall"] = round(sum(scores[category] for category in ENGINEERING_CATEGORIES) / len(ENGINEERING_CATEGORIES))
    recommendations = []
    for item in [*blocking, *warnings]:
        recommendation = item["recommendation"]
        if recommendation not in recommendations:
            recommendations.append(recommendation)

    report: dict[str, object] = {
        "root": str(root),
        "profile": "engineering",
        "status": status_from(scores, blocking, warnings),
        "scores": scores,
        "dimensions": dimension_report(scores, blocking, warnings),
        "blocking_issues": blocking,
        "warnings": warnings,
        "recommendations": recommendations,
        "checked_files": [relative(root, path) for path in harness_files(root)],
    }
    if module:
        module_evidence = load_module_evidence(root, module)
        report["module"] = {
            "requested": module,
            "found": module_evidence is not None,
            "source_hashes": len(module_evidence.get("source_hashes", [])) if isinstance(module_evidence, dict) else 0,
            "commands": module_evidence.get("verification_commands", []) if isinstance(module_evidence, dict) else [],
        }
    return report


def load_config_audit_module() -> ModuleType:
    script = Path(__file__).with_name("audit_ai_configs.py")
    spec = importlib.util.spec_from_file_location("audit_ai_configs", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_ai_configs"] = module
    spec.loader.exec_module(module)
    return module


def load_init_harness_module() -> ModuleType:
    script = Path(__file__).with_name("init_harness.py")
    spec = importlib.util.spec_from_file_location("init_harness_for_audit", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["init_harness_for_audit"] = module
    spec.loader.exec_module(module)
    return module


def audit_optional_guardrail_script(
    root: Path,
    warnings: list[dict[str, str]],
    script_rel: str,
    display_name: str,
) -> bool:
    script = root / script_rel
    if script.exists():
        if not script.stat().st_mode & 0o100:
            add_unique(
                warnings,
                issue(
                    "warning",
                    "control",
                    "non-executable-evidence-derived-guardrail",
                    script_rel,
                    f"The {display_name} check exists but is not executable.",
                    "Mark the guardrail script executable or regenerate the harness files.",
                ),
            )

        run_all = root / ".tenetora/guardrails/checks/run-all.sh"
        run_all_text = read_text(run_all) if run_all.exists() else ""
        if Path(script_rel).name not in run_all_text:
            add_unique(
                warnings,
                issue(
                    "warning",
                    "control",
                    "evidence-derived-guardrail-not-wired",
                    ".tenetora/guardrails/checks/run-all.sh",
                    f"The {display_name} check is not wired into run-all.sh.",
                    f"Add {Path(script_rel).name} to the generated guardrail runner or regenerate the harness files.",
                ),
            )
    return script.exists()


def audit_evidence_derived_guardrails(root: Path, warnings: list[dict[str, str]]) -> None:
    optional_checks = {
        "test_framework": (
            ".tenetora/guardrails/checks/test-framework-drift-scan.sh",
            "test framework drift",
            "test framework drift check",
            "test_framework_drift_rules",
        ),
        "dependency_version": (
            ".tenetora/guardrails/checks/dependency-version-drift-scan.sh",
            "dependency version drift",
            "dependency version drift check",
            "dependency_version_drift_rules",
        ),
    }
    existing = {
        key: audit_optional_guardrail_script(root, warnings, script_rel, display_name)
        for key, (script_rel, display_name, _missing_name, _rule_func) in optional_checks.items()
    }

    evidence_files = latest_evidence_files(root)
    if not evidence_files:
        return

    try:
        init_harness = load_init_harness_module()
    except RuntimeError:
        return

    required: set[str] = set()
    for evidence_file in evidence_files:
        try:
            payload = json.loads(read_text(evidence_file))
        except json.JSONDecodeError:
            continue
        for key, (_script_rel, _display_name, _missing_name, rule_func) in optional_checks.items():
            if getattr(init_harness, rule_func)(payload):
                required.add(key)

    for key in required:
        script_rel, display_name, missing_name, _rule_func = optional_checks[key]
        if existing[key]:
            continue
        add_unique(
            warnings,
            issue(
                "warning",
                "control",
                "missing-evidence-derived-guardrail",
                script_rel,
                f"Extraction evidence can derive a {missing_name}, but the generated check is missing.",
                f"Ask your AI to use Tenetora to update the project, then review the generated {display_name} guardrail.",
            ),
        )


def print_engineering_report(report: dict[str, object]) -> None:
    print("# Tenetora 治理工程审计" if use_chinese() else "# Harness Engineering Audit")
    print()
    print(("根目录" if use_chinese() else "Root") + f": `{report['root']}`")
    print(("状态" if use_chinese() else "Status") + f": `{report['status']}`")
    print()
    print("## 评分" if use_chinese() else "## Scores")
    scores = report["scores"]
    assert isinstance(scores, dict)
    for category in (*ENGINEERING_CATEGORIES, "overall"):
        print(f"- {category}: {scores[category]}/100")
    print()

    dimensions = report.get("dimensions", {})
    if isinstance(dimensions, dict):
        print("## 维度" if use_chinese() else "## Dimensions")
        for category in ENGINEERING_CATEGORIES:
            item = dimensions.get(category, {})
            if isinstance(item, dict):
                summary = (
                    "该维度通过当前治理检查。"
                    if use_chinese() and item.get("status") == "pass"
                    else "该维度需要关注。"
                    if use_chinese()
                    else str(item.get("summary", ""))
                )
                print(f"- {category}: `{item.get('status', 'unknown')}` - {summary}")
        print()

    blocking = report["blocking_issues"]
    assert isinstance(blocking, list)
    if blocking:
        print("## 阻断问题" if use_chinese() else "## Blocking Issues")
        for item in blocking:
            print(f"- `{item['code']}` {item['path']}: {audit_issue_text(item)}")
        print()

    warnings = report["warnings"]
    assert isinstance(warnings, list)
    if warnings:
        print("## 警告" if use_chinese() else "## Warnings")
        for item in warnings:
            print(f"- `{item['code']}` {item['path']}: {audit_issue_text(item)}")
        print()

    recommendations = report["recommendations"]
    assert isinstance(recommendations, list)
    if recommendations:
        print("## 建议" if use_chinese() else "## Recommendations")
        for recommendation in recommendations:
            print(f"- {audit_recommendation_text(recommendation)}")


def report_issues(report: dict[str, object]) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    for key, severity in (("blocking_issues", "error"), ("warnings", "warning")):
        for item in report.get(key, []):
            if isinstance(item, dict):
                issue_item = dict(item)
                issue_item["severity"] = severity
                issues.append(issue_item)
    return issues


def sarif_rule_id(item: dict[str, object]) -> str:
    category = str(item.get("category", "harness"))
    code = str(item.get("code", "issue"))
    return f"{category}.{code}"


def render_sarif(report: dict[str, object]) -> dict[str, object]:
    issues = report_issues(report)
    rules: dict[str, dict[str, object]] = {}
    results: list[dict[str, object]] = []
    for item in issues:
        rule_id = sarif_rule_id(item)
        rules.setdefault(
            rule_id,
            {
                "id": rule_id,
                "name": str(item.get("code", rule_id)),
                "shortDescription": {"text": str(item.get("message", ""))[:240]},
                "properties": {"category": str(item.get("category", "harness"))},
            },
        )
        path = str(item.get("path", ""))
        result: dict[str, object] = {
            "ruleId": rule_id,
            "level": "error" if item.get("severity") == "error" else "warning",
            "message": {"text": str(item.get("message", ""))},
        }
        if path:
            result["locations"] = [
                {
                    "physicalLocation": {
                        "artifactLocation": {
                            "uri": path,
                            "uriBaseId": "PROJECTROOT",
                        }
                    }
                }
            ]
        results.append(result)

    return {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "tenetora",
                        "semanticVersion": "0",
                        "informationUri": "https://github.com/lcgyl/Tenetora",
                        "rules": list(rules.values()),
                    }
                },
                "invocations": [
                    {
                        "executionSuccessful": report.get("status") != "fail",
                        "properties": {"root": str(report.get("root", ""))},
                    }
                ],
                "results": results,
            }
        ],
    }


def render_junit(report: dict[str, object]) -> str:
    issues = report_issues(report)
    test_count = max(1, len(issues))
    suite = ET.Element(
        "testsuite",
        {
            "name": "tenetora-audit",
            "tests": str(test_count),
            "failures": str(len(issues)),
            "errors": "0",
        },
    )
    properties = ET.SubElement(suite, "properties")
    scores = report.get("scores", {})
    if isinstance(scores, dict):
        for key, value in sorted(scores.items()):
            ET.SubElement(properties, "property", {"name": f"score.{key}", "value": str(value)})
    if not issues:
        ET.SubElement(suite, "testcase", {"classname": "tenetora", "name": "audit"})
    for item in issues:
        name = f"{sarif_rule_id(item)} {item.get('path', '')}".strip()
        testcase = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": str(item.get("category", "harness")),
                "name": name,
            },
        )
        failure = ET.SubElement(
            testcase,
            "failure",
            {
                "type": str(item.get("code", "issue")),
                "message": str(item.get("message", "")),
            },
        )
        failure.text = f"{item.get('severity', 'warning')}: {item.get('path', '')}: {item.get('message', '')}"
    return ET.tostring(suite, encoding="unicode", xml_declaration=True)


def print_report(report: dict[str, object], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif output_format == "sarif":
        print(json.dumps(render_sarif(report), ensure_ascii=False, indent=2))
    elif output_format == "junit":
        print(render_junit(report))
    else:
        print_engineering_report(report)


def load_score_history(root: Path) -> dict[str, object]:
    path = root / SCORE_HISTORY_REL
    if not path.exists():
        return {"version": 1, "runs": []}
    try:
        payload = json.loads(read_text(path))
    except json.JSONDecodeError:
        return {"version": 1, "runs": []}
    if not isinstance(payload, dict):
        return {"version": 1, "runs": []}
    runs = payload.get("runs")
    if not isinstance(runs, list):
        payload["runs"] = []
    payload["version"] = 1
    return payload


def append_score_history(root: Path, report: dict[str, object]) -> None:
    harness = root / ".tenetora"
    if not harness.is_dir():
        raise FileNotFoundError(
            harness_missing_message(root)
        )
    path = root / SCORE_HISTORY_REL
    with alignment_state.state_lock(path):
        payload = load_score_history(root)
        runs = payload.get("runs", [])
        assert isinstance(runs, list)
        runs.append(
            {
                "at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
                "status": report.get("status", "unknown"),
                "scores": report.get("scores", {}),
                "warnings": len(report.get("warnings", [])) if isinstance(report.get("warnings"), list) else 0,
                "blocking_issues": len(report.get("blocking_issues", []))
                if isinstance(report.get("blocking_issues"), list)
                else 0,
                "module": report.get("module", {}).get("requested")
                if isinstance(report.get("module"), dict)
                else None,
            }
        )
        payload["runs"] = runs[-200:]
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def print_score_trend(history: dict[str, object]) -> None:
    runs = history.get("runs", [])
    assert isinstance(runs, list)
    print("# Tenetora Audit Trend")
    print()
    if not runs:
        print("No audit score history recorded yet. Run `tenetora audit --record-history`.")
        return
    print("| At | Status | Stability | Reliability | Control | Content Density | Governance | Overall | Warnings | Blocks |")
    print("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for item in runs[-10:]:
        if not isinstance(item, dict):
            continue
        scores = item.get("scores", {})
        if not isinstance(scores, dict):
            scores = {}
        print(
            "| "
            f"{item.get('at', '')} | "
            f"`{item.get('status', 'unknown')}` | "
            f"{scores.get('stability', '')} | "
            f"{scores.get('reliability', '')} | "
            f"{scores.get('control', '')} | "
            f"{scores.get('content_density', '')} | "
            f"{scores.get('governance_effectiveness', '')} | "
            f"{scores.get('overall', '')} | "
            f"{item.get('warnings', '')} | "
            f"{item.get('blocking_issues', '')} |"
        )


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--help", action="help", help="Show this help message and exit.")
    parser.add_argument(
        "-p",
        "--path",
        default=".",
        type=existing_project_path,
        metavar="<project-dir>",
        help="Project directory to audit. Defaults to the current directory.",
    )
    parser.add_argument(
        "--profile",
        choices=("engineering", "config"),
        default="engineering",
        help="engineering audits .tenetora quality; config audits tool config risks.",
    )
    parser.add_argument("-j", "--json", action="store_true", help="Print JSON instead of Markdown")
    parser.add_argument(
        "--format",
        choices=("text", "json", "sarif", "junit"),
        default="text",
        help="Output format for engineering audit. --json is kept as a shorthand for --format json.",
    )
    parser.add_argument(
        "-s",
        "--strict",
        action="store_true",
        help="Exit non-zero when engineering audit fails or falls below --min-score.",
    )
    parser.add_argument("--min-score", type=int, default=80, help="Minimum category and overall score for --strict")
    parser.add_argument("--record-history", action="store_true", help=f"Append audit score snapshot to {SCORE_HISTORY_REL}.")
    parser.add_argument("--trend", action="store_true", help=f"Print score history from {SCORE_HISTORY_REL}.")
    parser.add_argument("--module", help="Audit evidence freshness for one module using .tenetora/state/modules/<module>/evidence.json.")
    args = parser.parse_args()

    root = args.path
    output_format = "json" if args.json else args.format
    if args.trend:
        if args.profile != "engineering":
            print("--trend is only supported for --profile engineering.", file=sys.stderr)
            return 2
        history = load_score_history(root)
        if output_format == "json":
            print(json.dumps(history, ensure_ascii=False, indent=2))
        elif output_format == "text":
            print_score_trend(history)
        else:
            print("--trend supports only text or json output.", file=sys.stderr)
            return 2
        return 0
    if args.profile == "config":
        if output_format not in {"text", "json"}:
            print("--format sarif and --format junit are only supported for --profile engineering.", file=sys.stderr)
            return 2
        module = load_config_audit_module()
        report = module.audit(root)
        if output_format == "json":
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            module.print_markdown(report)
        return 0

    report = audit_engineering(root, module=args.module)
    if args.record_history:
        try:
            append_score_history(root, report)
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    print_report(report, output_format)

    scores = report["scores"]
    assert isinstance(scores, dict)
    if args.strict and (
        report["status"] == "fail"
        or any(int(scores[category]) < args.min_score for category in (*ENGINEERING_CATEGORIES, "overall"))
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
