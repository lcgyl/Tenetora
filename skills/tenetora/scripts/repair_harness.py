#!/usr/bin/env python3
"""Repair known Tenetora compatibility issues in an existing project."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import loop_state
import alignment_state
from harness_io import atomic_write_bytes, atomic_write_text

CLI_ROOT = Path(__file__).resolve().parents[1] / "cli"
if str(CLI_ROOT) not in sys.path:
    sys.path.insert(0, str(CLI_ROOT))

from tenetora.codex_legacy_recovery import assess_legacy_registration, begin_recovery  # noqa: E402
from tenetora.path_security import validate_existing_project_path, validate_unredirected_file_path  # noqa: E402


CLAUDE_FIX = "claude-entrypoint"
CLAUDE_LOCAL_FIX = "claude-local-entrypoint"
ALIGNMENT_FIX = "alignment-lifecycle"
SUBAGENT_FIX = "subagent-governance"
PLANNING_FIX = "skeleton-first-planning"
CODEX_REGISTRATION_FIX = "codex-legacy-registration"
DEFAULTS_DIR = Path(__file__).resolve().parents[1] / "defaults"
PLANNING_DEFAULTS = (
    (".tenetora/workflows/end-to-end-skeleton-first.md", "workflows/end-to-end-skeleton-first.md"),
    (".tenetora/templates/implementation-plan.md", "templates/implementation-plan.md"),
)
PLANNING_TASK_START_MARKERS = (
    "end-to-end-skeleton-first.md",
    "applicable` or `exempt",
)
ALIGNMENT_DEFAULTS = (
    (".tenetora/workflows/decision-alignment.md", "workflows/decision-alignment.md"),
    (".tenetora/skills/decision-interview.md", "skills/decision-interview.md"),
    (".tenetora/templates/alignment-handoff.md", "templates/alignment-handoff.md"),
)
ALIGNMENT_TASK_START_MARKERS = (
    "decision-alignment.md",
)
ALIGNMENT_TASK_START_COMMANDS = (
    "tenetora guard --action alignment",
)
ALIGNMENT_SESSION_IGNORE_ENTRY = "state/alignment-session.json"
ALIGNMENT_SESSION_DIRECTORY_IGNORE_ENTRY = "state/alignment-sessions/"
ALIGNMENT_HISTORY_DIRECTORY_IGNORE_ENTRY = "state/alignment-history/"
ALIGNMENT_LIFECYCLE_IGNORE_ENTRY = "state/alignment-lifecycle.json"
STATE_LOCK_IGNORE_ENTRY = "state/.*.lock"
SUBAGENT_DEFAULTS = (
    (".tenetora/rules/subagent-dispatch.md", "rules/subagent-dispatch.md"),
    (".tenetora/templates/subagents/code-reviewer.spec.md", "templates/subagents/code-reviewer.spec.md"),
    (".tenetora/templates/subagents/security-auditor.spec.md", "templates/subagents/security-auditor.spec.md"),
    (".tenetora/templates/subagents/codebase-scout.spec.md", "templates/subagents/codebase-scout.spec.md"),
    (".tenetora/templates/subagents/implementer.spec.md", "templates/subagents/implementer.spec.md"),
)
SUBAGENT_TASK_START_MARKER = "subagent-dispatch.md"
SUBAGENT_CACHE_IGNORE_ENTRY = ".cache/subagents/"
SUBAGENT_STATE_IGNORE_ENTRY = "state/delegation-state.json"
HARNESS_GITIGNORE_ENTRY = ".tenetora/"
HARNESS_LOCAL_GITIGNORE_ENTRY = ".tenetora/local/"
CLAUDE_LOCAL_OMITTED_MARKERS = (
    "Content omitted because local-only.",
    "Content omitted because the source may contain credentials or local-only configuration.",
    "Content omitted because the local source is not targeted at an ignored harness location.",
)


def existing_project_path(raw: str) -> Path:
    try:
        return validate_existing_project_path(raw)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def managed_block(name: str, body: str) -> str:
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    return "\n".join(
        [
            f"<!-- TENETORA_ENTRYPOINT_START name={name} hash={digest} -->",
            body.rstrip(),
            f"<!-- TENETORA_ENTRYPOINT_END name={name} -->",
            "",
        ]
    )


def gitignore_has_entry(root: Path, entry: str) -> bool:
    path = root / ".gitignore"
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="ignore")
    normalized = entry.strip().rstrip("/")
    return any(line.strip().rstrip("/") == normalized for line in text.splitlines())


def append_gitignore_entry(root: Path, entry: str, actions: list[str]) -> None:
    path = root / ".gitignore"
    if gitignore_has_entry(root, entry):
        actions.append(f"skip existing {path} entry {entry}")
        return
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if text and not text.endswith("\n"):
        text += "\n"
    atomic_write_text(path, f"{text}{entry}\n")
    actions.append(f"add {entry} to {path}")


def harness_ignored(root: Path) -> bool:
    return gitignore_has_entry(root, HARNESS_GITIGNORE_ENTRY)


def claude_local_target(root: Path) -> str:
    if harness_ignored(root):
        return ".tenetora/agents/claude-local.md"
    return ".tenetora/local/agents/claude-local.md"


def claude_local_targets(root: Path) -> tuple[Path, Path]:
    return (
        root / ".tenetora" / "agents" / "claude-local.md",
        root / ".tenetora" / "local" / "agents" / "claude-local.md",
    )


def claude_target() -> str:
    return ".tenetora/agents/claude.md"


def claude_entrypoint_adapter() -> str:
    body = """# Claude Entry

@AGENTS.md

This repository uses `.tenetora/` as the shared AI-agent context directory.

Claude-specific notes live in `.tenetora/agents/claude.md`.

Keep this file as a thin compatibility entry. Do not duplicate stable rules here; update `.tenetora/rules/`, `.tenetora/workflows/`, or `.tenetora/wiki/` instead.
"""
    return managed_block("claude", body)


def claude_local_entrypoint_adapter(target: str) -> str:
    body = """# Claude Local Entry

@AGENTS.md

This file is a local-only Claude Code entrypoint.

Read `.tenetora/README.md` and `{target}` for local-only Claude preferences that are preserved through Tenetora governance.

Keep this file as a thin compatibility entry. Do not duplicate stable project rules here; update `.tenetora/rules/`, `.tenetora/workflows/`, or `.tenetora/wiki/` instead.

When `.tenetora/` is tracked, local-only preferences must live under ignored `.tenetora/local/` instead of shared harness files.
""".format(target=target)
    return managed_block("claude-local", body)


def is_managed_claude(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="ignore")
    return any(
        marker in text
        for marker in (
            "TENETORA_ENTRYPOINT_START name=claude",
        )
    )


def is_managed_claude_local(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="ignore")
    return any(
        marker in text
        for marker in (
            "TENETORA_ENTRYPOINT_START name=claude-local",
        )
    )


def is_legacy_managed_claude(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="ignore")
    return bool(re.search(r"AGENT_HARNESS_ENTRYPOINT_START name=claude(?:\s|>)", text))


def is_legacy_managed_claude_local(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="ignore")
    return "AGENT_HARNESS_ENTRYPOINT_START name=claude-local" in text


def has_unmanaged_claude_content(path: Path) -> bool:
    if not path.exists() or is_managed_claude(path) or is_legacy_managed_claude(path):
        return False
    text = path.read_text(encoding="utf-8", errors="ignore")
    return bool(text.strip())


def has_unmanaged_claude_local_content(path: Path) -> bool:
    if not path.exists() or is_managed_claude_local(path) or is_legacy_managed_claude_local(path):
        return False
    text = path.read_text(encoding="utf-8", errors="ignore")
    return bool(text.strip())


def migrated_claude_local_exists(root: Path) -> bool:
    for path in claude_local_targets(root):
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "Migrated from `CLAUDE.local.md`" in text or "local-only Claude" in text:
            return True
    return False


def has_omitted_claude_local_content(text: str) -> bool:
    return "Migrated from `CLAUDE.local.md`" in text and any(marker in text for marker in CLAUDE_LOCAL_OMITTED_MARKERS)


def referenced_claude_local_paths(root: Path, source: Path) -> list[Path]:
    if not source.exists():
        return []
    text = source.read_text(encoding="utf-8", errors="ignore")
    paths: list[Path] = []
    for path in claude_local_targets(root):
        if relpath(root, path) in text:
            paths.append(path)
    return paths


def relevant_omitted_claude_local_paths(root: Path, source: Path, target: str) -> list[Path]:
    relevant = {root / target}
    relevant.update(referenced_claude_local_paths(root, source))
    paths: list[Path] = []
    for path in sorted(relevant):
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if has_omitted_claude_local_content(text):
            paths.append(path)
    return paths


def strip_omitted_claude_local_markers(text: str) -> str:
    lines = [
        line
        for line in text.splitlines()
        if not any(marker in line for marker in CLAUDE_LOCAL_OMITTED_MARKERS)
    ]
    return ("\n".join(lines).rstrip() + "\n") if lines else ""


def stale_shared_claude_local_pointer(target: str) -> str:
    return (
        "# Claude Local\n\n"
        "Local-only Claude Code preferences are intentionally kept out of shared harness files.\n\n"
        f"Use `{target}` on this machine when local Claude preferences are present.\n"
    )


def latest_claude_local_backup(root: Path) -> Path | None:
    base = root / ".tenetora" / "changes" / "backups"
    if not base.exists():
        return None
    candidates = sorted(base.glob("*/entrypoints/CLAUDE.local.md"))
    return candidates[-1] if candidates else None


def relpath(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def managed_claude_local_points_to_target(source: Path, target: str) -> bool:
    if not source.exists():
        return False
    text = source.read_text(encoding="utf-8", errors="ignore")
    return target in text


def claude_local_content_source(root: Path, source: Path) -> Path | None:
    if source.exists() and not is_managed_claude_local(source) and not is_legacy_managed_claude_local(source):
        return source
    return latest_claude_local_backup(root)


def claude_local_migration_block(source: Path, source_note: str = "- Source file left unchanged.") -> str:
    text = source.read_text(encoding="utf-8", errors="ignore")
    return (
        "\n\n## Migrated from `CLAUDE.local.md`\n\n"
        "- Scope: `project`\n"
        "- Kind: `local-agent`\n"
        f"{source_note}\n\n"
        f"{text.strip()}\n"
    )


def claude_migration_block(source: Path, source_note: str = "- Source file left unchanged.") -> str:
    text = source.read_text(encoding="utf-8", errors="ignore")
    return (
        "\n\n## Migrated from `CLAUDE.md`\n\n"
        "- Scope: `project`\n"
        "- Kind: `agent`\n"
        f"{source_note}\n\n"
        f"{text.strip()}\n"
    )


def missing_alignment_task_start_markers(root: Path) -> list[str]:
    path = root / ".tenetora" / "workflows" / "task-start.md"
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="ignore")
    missing = [marker for marker in ALIGNMENT_TASK_START_MARKERS if marker not in text]
    if not any(command in text for command in ALIGNMENT_TASK_START_COMMANDS):
        missing.append("tenetora guard --action alignment")
    return missing


def append_alignment_task_start_gate(root: Path, actions: list[str]) -> bool:
    path = root / ".tenetora" / "workflows" / "task-start.md"
    current = path.read_text(encoding="utf-8")
    legacy_command = "agent-harness guard --action alignment"
    canonical_command = "tenetora guard --action alignment"
    replaced_legacy = canonical_command not in current and legacy_command in current
    if replaced_legacy:
        current = current.replace(legacy_command, canonical_command)
        atomic_write_text(path, current)
        actions.append("replace retired agent-harness alignment command with tenetora")
    missing = missing_alignment_task_start_markers(root)
    if not missing:
        return replaced_legacy

    lines = ["## Decision Alignment Gate", ""]
    if "decision-alignment.md" in missing:
        lines.append(
            "- For high-risk or materially ambiguous work, follow `.tenetora/workflows/decision-alignment.md` before restricted planning or execution."
        )
    if "tenetora guard --action alignment" in missing:
        lines.append(
            "- Enforce the current goal with `tenetora guard --action alignment --goal \"<goal>\" --risk-level <low|medium|high>`."
        )

    current = path.read_text(encoding="utf-8")
    separator = "\n\n" if current and not current.endswith("\n\n") else ""
    atomic_write_text(path, current + separator + "\n".join(lines) + "\n")
    actions.append("append missing decision alignment gate to .tenetora/workflows/task-start.md")
    return True


def harness_internal_gitignore_has_entry(root: Path, entry: str) -> bool:
    path = root / ".tenetora" / ".gitignore"
    if not path.is_file():
        return False
    normalized = entry.strip().rstrip("/")
    return any(line.strip().rstrip("/") == normalized for line in path.read_text(encoding="utf-8").splitlines())


def append_harness_internal_gitignore_entry(root: Path, entry: str, actions: list[str]) -> bool:
    path = root / ".tenetora" / ".gitignore"
    lock_target = root / ".tenetora" / "state" / "runtime-ignore-state"
    with alignment_state.state_lock(lock_target):
        if harness_internal_gitignore_has_entry(root, entry):
            actions.append(f"skip existing .tenetora/.gitignore entry {entry}")
            return False
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise RuntimeError(f".tenetora/.gitignore must be a regular file: {path}")
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if text and not text.endswith("\n"):
            text += "\n"
        atomic_write_text(path, f"{text}{entry}\n")
    actions.append(f"add {entry} to .tenetora/.gitignore")
    return True


def append_alignment_repair(root: Path, repairs: list[dict[str, str]]) -> None:
    runtime_entries = (
        root / ".tenetora" / "README.md",
        root / ".tenetora" / "INDEX.md",
        root / ".tenetora" / "workflows" / "task-start.md",
    )
    if not all(path.is_file() for path in runtime_entries):
        return
    missing_alignment = [target for target, _ in ALIGNMENT_DEFAULTS if not (root / target).is_file()]
    missing_markers = missing_alignment_task_start_markers(root)
    missing_session_ignores = [
        entry
        for entry in (
            ALIGNMENT_SESSION_IGNORE_ENTRY,
            ALIGNMENT_SESSION_DIRECTORY_IGNORE_ENTRY,
            ALIGNMENT_HISTORY_DIRECTORY_IGNORE_ENTRY,
            ALIGNMENT_LIFECYCLE_IGNORE_ENTRY,
            STATE_LOCK_IGNORE_ENTRY,
        )
        if not harness_internal_gitignore_has_entry(root, entry)
    ]
    if missing_alignment or missing_markers or missing_session_ignores:
        targets = list(missing_alignment)
        if missing_markers:
            targets.append(".tenetora/workflows/task-start.md (missing decision alignment gate)")
        if missing_session_ignores:
            targets.append(".tenetora/.gitignore (missing alignment session exclusion)")
        repairs.append(
            {
                "code": ALIGNMENT_FIX,
                "source": "Tenetora alignment defaults",
                "target": ", ".join(targets),
                "reason": "This project predates the decision-alignment lifecycle. Missing canonical files are added, and a bounded gate is appended to task-start without replacing existing project-owned content.",
            }
        )


def missing_planning_task_start_markers(root: Path) -> list[str]:
    path = root / ".tenetora" / "workflows" / "task-start.md"
    if not path.is_file():
        return list(PLANNING_TASK_START_MARKERS)
    text = path.read_text(encoding="utf-8", errors="ignore")
    return [marker for marker in PLANNING_TASK_START_MARKERS if marker not in text]


def append_planning_task_start_gate(root: Path, actions: list[str]) -> bool:
    missing = missing_planning_task_start_markers(root)
    if not missing:
        return False
    path = root / ".tenetora" / "workflows" / "task-start.md"
    if not path.is_file():
        return False
    current = path.read_text(encoding="utf-8")
    separator = ""
    if current and not current.endswith("\n\n"):
        separator = "\n" if current.endswith("\n") else "\n\n"
    block = (
        "## End-to-End Skeleton Planning Gate\n\n"
        "- For implementation planning, load `tenetora rules --context planning` and "
        "`.tenetora/workflows/end-to-end-skeleton-first.md`.\n"
        "- Every implementation plan records the workflow as `applicable` or `exempt` with a reason. "
        "Applicable plans define participating boundaries, the minimum safe skeleton, its verification gate, "
        "and deferred refinement before deep component work.\n"
    )
    atomic_write_text(path, current + separator + block)
    actions.append("append missing end-to-end skeleton planning gate to .tenetora/workflows/task-start.md")
    return True


def append_planning_repair(root: Path, repairs: list[dict[str, str]]) -> None:
    runtime_entries = (
        root / ".tenetora" / "README.md",
        root / ".tenetora" / "INDEX.md",
        root / ".tenetora" / "workflows" / "task-start.md",
    )
    if not all(path.is_file() for path in runtime_entries):
        return
    missing_defaults = [target for target, _ in PLANNING_DEFAULTS if not (root / target).is_file()]
    missing_markers = missing_planning_task_start_markers(root)
    if not (missing_defaults or missing_markers):
        return
    targets = list(missing_defaults)
    if missing_markers:
        targets.append(".tenetora/workflows/task-start.md (missing end-to-end skeleton planning gate)")
    repairs.append(
        {
            "code": PLANNING_FIX,
            "source": "Tenetora end-to-end skeleton planning defaults",
            "target": ", ".join(targets),
            "reason": "This project predates the skeleton-first planning workflow. Missing canonical files and a minimal task-start consumption gate are added without replacing project-owned content.",
        }
    )


def task_start_has_subagent_gate(root: Path) -> bool:
    path = root / ".tenetora" / "workflows" / "task-start.md"
    return path.is_file() and SUBAGENT_TASK_START_MARKER in path.read_text(encoding="utf-8", errors="ignore")


def append_subagent_task_start_gate(root: Path, actions: list[str]) -> bool:
    if task_start_has_subagent_gate(root):
        return False
    path = root / ".tenetora" / "workflows" / "task-start.md"
    if not path.is_file():
        return False
    current = path.read_text(encoding="utf-8")
    separator = "\n\n" if current and not current.endswith("\n\n") else ""
    block = (
        "## Subagent Dispatch Gate\n\n"
        "- Read `.tenetora/rules/subagent-dispatch.md` when independent review, isolated security analysis, "
        "bounded codebase investigation, or parallel read-only work would materially improve reliability.\n"
        "- The host platform performs dispatch. Tenetora hooks and `tenetora delegation` only remind, "
        "observe, record, and enforce bounded review-cycle state.\n"
    )
    atomic_write_text(path, current + separator + block)
    actions.append("append missing subagent dispatch gate to .tenetora/workflows/task-start.md")
    return True


def subagent_cache_is_ignored(root: Path) -> bool:
    return (
        harness_internal_gitignore_has_entry(root, ".cache/")
        or harness_internal_gitignore_has_entry(root, SUBAGENT_CACHE_IGNORE_ENTRY)
    )


def loop_review_cycle_needs_repair(root: Path) -> bool:
    path = root / ".tenetora" / "state" / "loop-state.json"
    if not path.is_file():
        return True
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return True
    if not isinstance(payload, dict):
        return True
    review = payload.get("review_cycle")
    return (
        payload.get("version") != loop_state.CURRENT_SCHEMA_VERSION
        or not isinstance(review, dict)
        or review.get("max_review_rounds") != 3
        or review.get("max_fix_rounds") != 2
    )


def repair_loop_review_cycle(root: Path, actions: list[str]) -> bool:
    path = root / ".tenetora" / "state" / "loop-state.json"
    if not path.is_file():
        loop_state.write_state(root, loop_state.default_state())
        actions.append("write missing .tenetora/state/loop-state.json with bounded review cycle")
        return True
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        actions.append("skip corrupt .tenetora/state/loop-state.json; manual recovery is required")
        return False
    if not isinstance(payload, dict):
        actions.append("skip non-object .tenetora/state/loop-state.json; manual recovery is required")
        return False
    migrated = loop_state.migrate_schema(payload)
    review = migrated.get("review_cycle")
    if not isinstance(review, dict):
        migrated["review_cycle"] = loop_state.default_review_cycle()
    elif review.get("max_review_rounds") != 3 or review.get("max_fix_rounds") != 2:
        actions.append("skip custom or invalid review-cycle limits; manual recovery is required")
        return False
    if migrated == payload:
        return False
    loop_state.write_state(root, migrated)
    actions.append("migrate .tenetora/state/loop-state.json to schema v3 with bounded review cycle")
    return True


def append_subagent_repair(root: Path, repairs: list[dict[str, str]]) -> None:
    runtime_entries = (
        root / ".tenetora" / "README.md",
        root / ".tenetora" / "INDEX.md",
        root / ".tenetora" / "workflows" / "task-start.md",
    )
    if not all(path.is_file() for path in runtime_entries):
        return
    missing_defaults = [target for target, _ in SUBAGENT_DEFAULTS if not (root / target).is_file()]
    missing_gate = not task_start_has_subagent_gate(root)
    missing_cache_ignore = not subagent_cache_is_ignored(root)
    missing_state_ignore = not harness_internal_gitignore_has_entry(root, SUBAGENT_STATE_IGNORE_ENTRY)
    loop_needs_repair = loop_review_cycle_needs_repair(root)
    if not (missing_defaults or missing_gate or missing_cache_ignore or missing_state_ignore or loop_needs_repair):
        return
    targets = list(missing_defaults)
    if missing_gate:
        targets.append(".tenetora/workflows/task-start.md (missing subagent dispatch gate)")
    if missing_cache_ignore:
        targets.append(".tenetora/.gitignore (missing subagent report cache exclusion)")
    if missing_state_ignore:
        targets.append(".tenetora/.gitignore (missing delegation runtime state exclusion)")
    if loop_needs_repair:
        targets.append(".tenetora/state/loop-state.json (missing bounded review cycle)")
    repairs.append(
        {
            "code": SUBAGENT_FIX,
            "source": "Tenetora subagent governance defaults",
            "target": ", ".join(targets),
            "reason": "This project predates portable subagent dispatch governance. Missing-only repair adds contracts, ignored runtime state, and bounded review-cycle metadata without replacing project-owned content.",
        }
    )


def append_lifecycle_repairs(root: Path, repairs: list[dict[str, str]]) -> None:
    append_planning_repair(root, repairs)
    append_alignment_repair(root, repairs)
    append_subagent_repair(root, repairs)


def detect_repairs(root: Path) -> list[dict[str, str]]:
    repairs: list[dict[str, str]] = []
    claude = root / "CLAUDE.md"
    if is_legacy_managed_claude(claude):
        repairs.append(
            {
                "code": CLAUDE_FIX,
                "source": "CLAUDE.md",
                "target": claude_target(),
                "reason": "CLAUDE.md is a retired Agent Harness thin adapter and must be rewritten to the canonical Tenetora marker without re-migrating adapter text.",
            }
        )
    elif has_unmanaged_claude_content(claude):
        repairs.append(
            {
                "code": CLAUDE_FIX,
                "source": "CLAUDE.md",
                "target": claude_target(),
                "reason": "CLAUDE.md is not governed by .tenetora and should be backed up, migrated, and rewritten as a thin shared adapter.",
            }
        )
    claude_local = root / "CLAUDE.local.md"
    if not claude_local.exists():
        append_lifecycle_repairs(root, repairs)
        return repairs
    target = claude_local_target(root)
    managed = is_managed_claude_local(claude_local)
    legacy_managed = is_legacy_managed_claude_local(claude_local)
    if legacy_managed:
        repairs.append(
            {
                "code": CLAUDE_LOCAL_FIX,
                "source": "CLAUDE.local.md",
                "target": target,
                "reason": "CLAUDE.local.md is a retired Agent Harness thin adapter and must be rewritten to the canonical Tenetora marker without re-migrating adapter text.",
            }
        )
        append_lifecycle_repairs(root, repairs)
        return repairs
    if not managed and has_unmanaged_claude_local_content(claude_local):
        if migrated_claude_local_exists(root):
            reason = "CLAUDE.local.md is not governed as a thin local adapter after migration."
        else:
            reason = "CLAUDE.local.md is not governed by .tenetora and should be backed up, migrated, and rewritten as a thin local adapter."
        repairs.append(
            {
                "code": CLAUDE_LOCAL_FIX,
                "source": "CLAUDE.local.md",
                "target": target,
                "reason": reason,
            }
        )
        append_lifecycle_repairs(root, repairs)
        return repairs
    omitted_paths = relevant_omitted_claude_local_paths(root, claude_local, target)
    points_to_target = managed_claude_local_points_to_target(claude_local, target)
    if managed and (omitted_paths or not points_to_target):
        backup = latest_claude_local_backup(root)
        reason_parts: list[str] = []
        if omitted_paths:
            reason_parts.append("migrated Claude local content is still omitted")
        if not points_to_target:
            reason_parts.append("CLAUDE.local.md points at the wrong harness target")
        repair = {
            "code": CLAUDE_LOCAL_FIX,
            "source": "CLAUDE.local.md",
            "target": target,
            "reason": "; ".join(reason_parts) + ".",
        }
        if backup is not None:
            repair["backup_source"] = relpath(root, backup)
        else:
            repair["recoverable"] = "false"
        repairs.append(repair)
    append_lifecycle_repairs(root, repairs)
    return repairs


def backup_rel(update_id: str, rel: str) -> str:
    return (Path(".tenetora/changes/backups") / update_id / "entrypoints" / rel).as_posix()


def write_repair_report(root: Path, update_id: str, applied: list[dict[str, str]]) -> str:
    rel = f".tenetora/changes/{update_id}-repair-report.md"
    path = root / rel
    rows = ["| Code | Source | Action |", "| --- | --- | --- |"]
    for item in applied:
        rows.append(f"| `{item['code']}` | `{item['source']}` | `{item['action']}` |")
    text = f"# {update_id} Harness Repair Report\n\n" + "\n".join(rows) + "\n"
    atomic_write_text(path, text)
    return rel


def apply_repairs(root: Path, repairs: list[dict[str, str]], fix: str) -> tuple[list[dict[str, str]], list[str]]:
    selected = [item for item in repairs if fix == "all" or item["code"] == fix]
    applied: list[dict[str, str]] = []
    actions: list[str] = []
    if not selected:
        return applied, actions
    update_id = dt.datetime.now().strftime("%Y-%m-%d-%H%M%S-%f")
    for item in selected:
        if item["code"] == PLANNING_FIX:
            written: list[str] = []
            for target_rel, source_rel in PLANNING_DEFAULTS:
                target = root / target_rel
                if target.exists():
                    actions.append(f"skip existing {target_rel}")
                    continue
                source = DEFAULTS_DIR / source_rel
                atomic_write_text(target, source.read_text(encoding="utf-8"))
                written.append(target_rel)
                actions.append(f"write missing skeleton-first planning file {target_rel}")
            gate_updated = append_planning_task_start_gate(root, actions)
            if written or gate_updated:
                applied.append(
                    {
                        **item,
                        "action": "wrote only missing planning workflow/template files and appended the task-start gate; existing project-owned content was preserved",
                    }
                )
            continue
        if item["code"] == SUBAGENT_FIX:
            written: list[str] = []
            for target_rel, source_rel in SUBAGENT_DEFAULTS:
                target = root / target_rel
                if target.exists():
                    actions.append(f"skip existing {target_rel}")
                    continue
                source = DEFAULTS_DIR / source_rel
                atomic_write_text(target, source.read_text(encoding="utf-8"))
                written.append(target_rel)
                actions.append(f"write missing subagent governance file {target_rel}")
            gate_updated = append_subagent_task_start_gate(root, actions)
            cache_ignore_updated = False
            if not subagent_cache_is_ignored(root):
                cache_ignore_updated = append_harness_internal_gitignore_entry(
                    root, SUBAGENT_CACHE_IGNORE_ENTRY, actions
                )
            state_ignore_updated = append_harness_internal_gitignore_entry(
                root, SUBAGENT_STATE_IGNORE_ENTRY, actions
            )
            loop_updated = repair_loop_review_cycle(root, actions)
            if written or gate_updated or cache_ignore_updated or state_ignore_updated or loop_updated:
                applied.append(
                    {
                        **item,
                        "action": "wrote only missing role/rule files, appended the task-start gate, ignored volatile reports/state, and migrated bounded loop metadata; existing project-owned content was preserved",
                    }
                )
            continue
        if item["code"] == ALIGNMENT_FIX:
            written: list[str] = []
            for target_rel, source_rel in ALIGNMENT_DEFAULTS:
                target = root / target_rel
                if target.exists():
                    actions.append(f"skip existing {target_rel}")
                    continue
                source = DEFAULTS_DIR / source_rel
                atomic_write_text(target, source.read_text(encoding="utf-8"))
                written.append(target_rel)
                actions.append(f"write missing alignment lifecycle file {target_rel}")
            task_start_updated = append_alignment_task_start_gate(root, actions)
            session_ignore_updates = [
                append_harness_internal_gitignore_entry(root, entry, actions)
                for entry in (
                    ALIGNMENT_SESSION_IGNORE_ENTRY,
                    ALIGNMENT_SESSION_DIRECTORY_IGNORE_ENTRY,
                    ALIGNMENT_HISTORY_DIRECTORY_IGNORE_ENTRY,
                    ALIGNMENT_LIFECYCLE_IGNORE_ENTRY,
                    STATE_LOCK_IGNORE_ENTRY,
                )
            ]
            if written or task_start_updated or any(session_ignore_updates):
                applied.append(
                    {
                        **item,
                        "action": "wrote missing alignment lifecycle files, appended the missing task-start gate, and excluded temporary session state; existing project-owned content was preserved",
                    }
                )
            continue
        if item["code"] == CLAUDE_FIX:
            source = root / "CLAUDE.md"
            target_rel = claude_target()
            target = root / target_rel
            rel = backup_rel(update_id, "CLAUDE.md")
            backup = root / rel
            atomic_write_bytes(backup, source.read_bytes(), mode=0o600)
            validate_unredirected_file_path(target, label="Claude migration target")
            current = target.read_text(encoding="utf-8") if target.exists() else "# Claude Agent Entry\n\nRead `.tenetora/README.md` first.\n"
            legacy_adapter = "retired Agent Harness thin adapter" in item.get("reason", "")
            block = "" if legacy_adapter else claude_migration_block(source)
            if block and ("Migrated from `CLAUDE.md`" not in current or source.read_text(encoding="utf-8", errors="ignore").strip() not in current):
                if current and not current.endswith("\n"):
                    current += "\n"
                atomic_write_text(target, current + block)
                actions.append(f"merge {item['source']} into {target_rel}")
            atomic_write_text(source, claude_entrypoint_adapter())
            applied.append({**item, "target": target_rel, "backup": rel, "action": "backed up, migrated content, and rewrote thin adapter"})
            actions.append(f"backup {item['source']} to {rel}")
            actions.append(f"rewrite {item['source']} as thin adapter")
            continue
        if item["code"] != CLAUDE_LOCAL_FIX:
            continue
        source = root / "CLAUDE.local.md"
        target_rel = claude_local_target(root)
        target = root / target_rel
        legacy_adapter = "retired Agent Harness thin adapter" in item.get("reason", "")
        content_source = None if legacy_adapter else claude_local_content_source(root, source)
        needs_content_restore = "migrated Claude local content is still omitted" in item.get("reason", "")
        if needs_content_restore and content_source is None:
            actions.append("skip CLAUDE.local.md content restore because no original backup was found")
            continue
        rel = backup_rel(update_id, "CLAUDE.local.md")
        backup = root / rel
        atomic_write_bytes(backup, source.read_bytes(), mode=0o600)
        if target_rel.startswith(".tenetora/local/"):
            append_gitignore_entry(root, HARNESS_LOCAL_GITIGNORE_ENTRY, actions)
        validate_unredirected_file_path(target, label="Claude local migration target")
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        had_omitted_content = has_omitted_claude_local_content(current)
        if had_omitted_content:
            current = strip_omitted_claude_local_markers(current)
        if content_source is not None:
            source_note = "- Source file left unchanged."
            if content_source != source:
                source_note = f"- Restored from backup: `{relpath(root, content_source)}`"
            block = claude_local_migration_block(content_source, source_note)
        elif not legacy_adapter:
            block = ""
            actions.append("skip content restore for CLAUDE.local.md because no original backup was found")
        if block and ("Migrated from `CLAUDE.local.md`" not in current or had_omitted_content):
            if current and not current.endswith("\n"):
                current += "\n"
            atomic_write_text(target, current + block)
            actions.append(f"merge {item['source']} into {target_rel}")
        if block and target_rel.startswith(".tenetora/local/"):
            shared = root / ".tenetora" / "agents" / "claude-local.md"
            if shared.exists() and has_omitted_claude_local_content(shared.read_text(encoding="utf-8", errors="ignore")):
                atomic_write_text(shared, stale_shared_claude_local_pointer(target_rel))
                actions.append("replace stale .tenetora/agents/claude-local.md omitted placeholder with local pointer")
        atomic_write_text(source, claude_local_entrypoint_adapter(target_rel))
        applied.append({**item, "target": target_rel, "backup": rel, "action": "backed up, migrated content, and rewrote thin adapter"})
        actions.append(f"backup {item['source']} to {rel}")
        actions.append(f"rewrite {item['source']} as thin adapter")
    if applied:
        report = write_repair_report(root, update_id, applied)
        actions.append(f"write {report}")
    return applied, actions


def codex_registration_repair() -> tuple[dict[str, object], dict[str, str] | None]:
    assessment = assess_legacy_registration()
    repair = None
    if assessment.status in {"recoverable", "blocked"}:
        repair = {
            "code": CODEX_REGISTRATION_FIX,
            "source": assessment.config_path,
            "target": "tenetora@tenetora-local",
            "reason": assessment.reason or "legacy Agent Harness Codex registration requires canonical convergence",
            "recoverable": "true" if assessment.recoverable else "false",
        }
    return assessment.payload(), repair


def apply_codex_registration_repair(root: Path) -> tuple[dict[str, object], list[str]]:
    assessment = assess_legacy_registration()
    if not assessment.recoverable:
        raise RuntimeError(assessment.reason or "Codex legacy registration is not recoverable")
    package_root = Path(__file__).resolve().parents[3]
    installer = package_root / "scripts" / "install-skill.py"
    if not installer.is_file():
        raise RuntimeError(f"Tenetora package installer is missing: {installer}")
    transaction = begin_recovery(assessment)
    command = [
        sys.executable,
        str(installer),
        "--tools",
        "codex",
        "--global",
        "--force",
        "--path",
        str(root),
        "--package-root",
        str(package_root),
        "--json",
    ]
    env = os.environ.copy()
    env.setdefault("TENETORA_HOME", str(Path.home() / ".tenetora"))
    result = subprocess.run(
        command,
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        transaction.rollback()
        detail = " ".join((result.stderr or result.stdout).split())
        raise RuntimeError(f"canonical Codex plugin installation failed; original config restored: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        transaction.rollback()
        raise RuntimeError("canonical Codex installer returned invalid JSON; original config restored") from exc
    if not isinstance(payload, dict) or payload.get("result") not in {"FULL", "PENDING_TRUST"}:
        transaction.rollback()
        raise RuntimeError("canonical Codex plugin verification failed; original config restored")
    transaction.commit()
    actions = [
        f"backed up Codex config to {transaction.backup_path}",
        f"removed {len(assessment.sections)} proven legacy marketplace/plugin/trust sections",
        "installed and verified tenetora@tenetora-local; legacy Hook trust hashes were not copied",
    ]
    if transaction.archived_caches:
        actions.append(f"archived {len(transaction.archived_caches)} proven legacy Codex plugin cache(s)")
    return transaction.payload(), actions


def render_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tenetora repair",
        description="Detect and repair known compatibility issues left by older Tenetora versions.",
        add_help=False,
    )
    parser.add_argument("--help", action="help", help="Show this help message and exit.")
    parser.add_argument("-p", "--path", default=".", type=existing_project_path, metavar="<project-dir>", help="Project directory to repair. Defaults to current directory.")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", dest="action", action="store_const", const="check", help="Detect repairs without writing")
    action.add_argument("--apply", dest="action", action="store_const", const="apply", help="Apply selected repairs")
    parser.set_defaults(action="check")
    parser.add_argument(
        "--fix",
        default="all",
        choices=("all", CLAUDE_FIX, CLAUDE_LOCAL_FIX, PLANNING_FIX, ALIGNMENT_FIX, SUBAGENT_FIX, CODEX_REGISTRATION_FIX),
        help="Repair code to apply. Defaults to all.",
    )
    parser.add_argument("-j", "--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args(argv)

    repairs = detect_repairs(args.path)
    codex_assessment: dict[str, object] = {
        "status": "not-requested",
        "reason": "Select --fix codex-legacy-registration to inspect machine-level Codex registration state.",
    }
    codex_repair = None
    if args.fix == CODEX_REGISTRATION_FIX:
        codex_assessment, codex_repair = codex_registration_repair()
        if codex_repair is not None:
            repairs.append(codex_repair)
    applied: list[dict[str, str]] = []
    actions: list[str] = []
    if args.action == "apply":
        project_repairs = [item for item in repairs if item["code"] != CODEX_REGISTRATION_FIX]
        applied, actions = apply_repairs(args.path, project_repairs, args.fix)
        if args.fix == CODEX_REGISTRATION_FIX and codex_repair is not None:
            if codex_assessment.get("status") == "recoverable":
                transaction, codex_actions = apply_codex_registration_repair(args.path)
                applied.append({**codex_repair, "action": "recovered canonical Codex registration"})
                actions.extend(codex_actions)
                codex_assessment = transaction
            else:
                actions.append(f"skip {CODEX_REGISTRATION_FIX}: {codex_assessment.get('reason')}")
    selected_repairs = [item for item in repairs if args.fix == "all" or item["code"] == args.fix]

    status = "pass"
    if args.action == "check" and repairs:
        status = "needs-repair"
    elif args.action == "apply" and applied:
        status = "repaired"
    elif args.action == "apply" and selected_repairs:
        status = "skipped"

    payload = {
        "status": status,
        "action": args.action,
        "repairs": repairs,
        "applied": applied,
        "actions": actions,
        "codex_registration": codex_assessment,
    }
    if args.json:
        print(render_json(payload))
    else:
        chinese = os.environ.get("TENETORA_LANG", "").strip().lower() in {"zh", "zh-cn", "zh-hans", "chinese", "中文"}
        print("Tenetora 修复" if chinese else "Tenetora Repair")
        print(f"{'状态' if chinese else 'Status'}: {status}")
        for action_text in actions:
            print(action_text)
        if args.action == "check" and repairs:
            print("可用修复：" if chinese else "Repairs available:")
            for item in repairs:
                print(f"- {item['code']}: {item['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
