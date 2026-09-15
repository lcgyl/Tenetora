#!/usr/bin/env python3
"""Read-only audit for repository AI harness configuration."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from path_security import harness_missing_message, validate_existing_project_path

TOOL_COMMANDS = {
    "codex": ["codex"],
    "claude": ["claude", "claude-code"],
    "cursor": ["cursor"],
    "opencode": ["opencode"],
    "pi": ["pi"],
    "zcode": ["zcode"],
}

PROJECT_FILES = [
    "AGENTS.md",
    "AGENTS.override.md",
    "CLAUDE.md",
    "CLAUDE.local.md",
    ".mcp.json",
    "opencode.json",
    "opencode.toml",
    ".codex/config.toml",
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".claude/CLAUDE.md",
]
TOOL_PRIVATE_CONFIG_PATHS = {
    ".claude/settings.local.json",
}
SOURCE_OF_TRUTH_FILES = [
    ".tenetora/README.md",
    ".tenetora/rules/project.md",
    ".tenetora/rules/build-and-deps.md",
    ".tenetora/rules/testing.md",
    ".tenetora/rules/security.md",
    ".tenetora/workflows/task-start.md",
    ".tenetora/workflows/verification.md",
    ".tenetora/wiki/project-map.md",
]
ENTRYPOINT_FILES = [
    "AGENTS.md",
    "CLAUDE.md",
    "CLAUDE.local.md",
    ".cursor/rules/agent-harness.mdc",
    ".claude/CLAUDE.md",
]

SECRET_PATTERNS = {
    "gitlab-token": re.compile(r"glpat-[A-Za-z0-9._-]+"),
    "github-token": re.compile(r"(ghp|github_pat)_[A-Za-z0-9_]+"),
    "private-key": re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
    "token-assignment": re.compile(r"\b[A-Z0-9_]*(TOKEN|SECRET|PASSWORD)\b\s*[:=]\s*['\"]?[^'\"\s${}]+", re.I),
    "jwt": re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
}

LOCAL_PATH_PATTERN = re.compile(r"(/Users/[^\\s\"']+|/home/[^\\s\"']+|[A-Za-z]:\\\\Users\\\\[^\\s\"']+)")
BROAD_PERMISSION_PATTERN = re.compile(r"(git reset:\*|git checkout:\*|Bash\(git:\*\)|rm -rf|Bash\(rm:\*\))")


def existing_project_path(raw: str) -> Path:
    try:
        return validate_existing_project_path(raw)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def run_git(root: Path, args: list[str]) -> tuple[int, str]:
    try:
        out = subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL, text=True)
        return 0, out
    except subprocess.CalledProcessError as exc:
        return exc.returncode, exc.output or ""
    except FileNotFoundError:
        return 127, ""


def is_tracked(root: Path, rel: str) -> bool:
    code, out = run_git(root, ["ls-files", "--", rel])
    return code == 0 and bool(out.strip())


def is_ignored(root: Path, rel: str) -> bool:
    code, _ = run_git(root, ["check-ignore", "-q", rel])
    return code == 0


def detect_tools(root: Path) -> dict[str, dict[str, object]]:
    home = Path.home()
    result: dict[str, dict[str, object]] = {}
    for tool, commands in TOOL_COMMANDS.items():
        command_hits = [cmd for cmd in commands if shutil.which(cmd)]
        project_hits: list[str] = []
        if tool == "codex":
            project_hits = [p for p in ["AGENTS.md"] if (root / p).exists()]
            if (home / ".codex").exists():
                project_hits.append("~/.codex")
        elif tool == "claude":
            project_hits = [p for p in ["CLAUDE.md", "CLAUDE.local.md", ".claude"] if (root / p).exists()]
            if (home / ".claude").exists():
                project_hits.append("~/.claude")
        elif tool == "cursor":
            project_hits = [p for p in ["AGENTS.md", ".cursor"] if (root / p).exists()]
        elif tool == "opencode":
            project_hits = [p for p in ["opencode.json", ".opencode", "opencode.toml"] if (root / p).exists()]
        elif tool == "pi":
            project_hits = [p for p in [".pi"] if (root / p).exists()]
            if (home / ".pi").exists():
                project_hits.append("~/.pi")
        elif tool == "zcode":
            project_hits = [p for p in [".zcode"] if (root / p).exists()]
            if os.environ.get("ZCODE_HOME"):
                project_hits.append("$ZCODE_HOME")
            elif (home / ".zcode").exists():
                project_hits.append("~/.zcode")
            if (home / "Library" / "Application Support" / "ZCode").exists():
                project_hits.append("~/Library/Application Support/ZCode")
        result[tool] = {
            "detected": bool(command_hits or project_hits),
            "commands": command_hits,
            "signals": project_hits,
        }
    result["generic"] = {"detected": True, "commands": [], "signals": ["always"]}
    return result


def scan_file(path: Path) -> list[str]:
    findings: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return findings
    for name, pattern in SECRET_PATTERNS.items():
        if pattern.search(text):
            findings.append(name)
    if LOCAL_PATH_PATTERN.search(text):
        findings.append("local-absolute-path")
    if BROAD_PERMISSION_PATTERN.search(text):
        findings.append("broad-or-destructive-permission")
    return sorted(set(findings))


def collect_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for rel in PROJECT_FILES:
        path = root / rel
        if path.exists() and path.is_file():
            files.append(path)
    for rel_dir in [".tenetora", ".claude/rules", ".cursor/rules"]:
        directory = root / rel_dir
        if directory.exists():
            files.extend(p for p in directory.rglob("*") if p.is_file())
    return sorted(set(files))


def audit(root: Path) -> dict[str, object]:
    root = root.resolve()
    files = collect_files(root)
    file_report = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        file_report.append(
            {
                "path": rel,
                "tracked": is_tracked(root, rel),
                "ignored": is_ignored(root, rel),
                "findings": scan_file(path),
            }
        )

    code, head_mcp = run_git(root, ["grep", "-Il", "glpat-", "HEAD", "--", ".mcp.json"])
    history_findings = []
    if code == 0 and head_mcp.strip():
        history_findings.append({"path": "HEAD:.mcp.json", "finding": "gitlab-token-pattern"})

    return {
        "root": str(root),
        "harness_exists": (root / ".tenetora").exists(),
        "detected_tools": detect_tools(root),
        "files": file_report,
        "history_findings": history_findings,
    }


def thin_adapter_for(rel: str) -> str:
    if rel.endswith(".mdc"):
        return """---
description: Tenetora shared project rules
alwaysApply: true
---

# Tenetora Entry

Read `.tenetora/README.md` first.

Project-wide rules live under `.tenetora/rules/`, task flow under `.tenetora/workflows/`, and project context under `.tenetora/wiki/`.

Do not duplicate stable rules here; update `.tenetora/` instead.
"""
    return """# Agent Entry

This repository uses `.tenetora/` as the shared AI-agent context directory.

Before starting work, read:

1. `.tenetora/README.md`
2. `.tenetora/workflows/task-start.md`
3. `.tenetora/workflows/verification.md`
4. `.tenetora/rules/project.md`
5. `.tenetora/wiki/project-map.md`

Keep this file as a thin compatibility entry. Do not duplicate stable rules here; update `.tenetora/` instead.
"""


def needs_reconcile(root: Path, rel: str) -> tuple[bool, str]:
    path = root / rel
    if not path.exists():
        return False, "missing"
    if rel in TOOL_PRIVATE_CONFIG_PATHS:
        return False, "tool-private-config-skipped"
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return True, "unreadable"
    if entrypoint_routes_to_harness(root, rel, text):
        return False, "already-routed"
    return True, "not-routed-to-harness"


def entrypoint_routes_to_harness(root: Path, rel: str, text: str) -> bool:
    required = [".tenetora/README.md", ".tenetora/rules", ".tenetora/workflows"]
    if all(marker in text for marker in required):
        return True
    if rel == "AGENTS.md" or "AGENTS.md" not in text:
        return False
    agents = root / "AGENTS.md"
    if not agents.is_file():
        return False
    try:
        agents_text = agents.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return all(marker in agents_text for marker in required)


def reconcile_plan(root: Path) -> dict[str, object]:
    source_of_truth = [rel for rel in SOURCE_OF_TRUTH_FILES if (root / rel).exists()]
    entries = []
    for rel in ENTRYPOINT_FILES:
        path = root / rel
        should_reconcile, reason = needs_reconcile(root, rel)
        candidate = thin_adapter_for(rel) if should_reconcile else ""
        entries.append(
            {
                "path": rel,
                "exists": path.exists(),
                "tracked": is_tracked(root, rel) if path.exists() else False,
                "ignored": is_ignored(root, rel) if path.exists() else False,
                "action": "write-candidate" if should_reconcile else "none",
                "reason": reason,
                "candidate": candidate,
            }
        )
    return {
        "version": 1,
        "source_of_truth": source_of_truth,
        "entries": entries,
        "summary": {
            "candidate_count": sum(1 for item in entries if item["action"] == "write-candidate"),
            "skipped_private_configs": sorted(TOOL_PRIVATE_CONFIG_PATHS),
        },
    }


def artifact_rel_for_entry(rel: str) -> str:
    parts = ["root" if part == "." else part.lstrip(".") for part in Path(rel).parts]
    return Path(*parts).as_posix()


def write_reconcile_artifacts(root: Path, plan: dict[str, object]) -> list[str]:
    harness = root / ".tenetora"
    if not harness.is_dir():
        raise FileNotFoundError(
            harness_missing_message(root)
        )
    update_id = dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    changes = harness / "changes"
    actions: list[str] = []
    json_path = changes / f"{update_id}-ai-config-reconcile-plan.json"
    md_path = changes / f"{update_id}-ai-config-reconcile-plan.md"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    actions.append(f"write {json_path}")
    md_lines = [
        f"# {update_id} AI Config Reconcile Plan",
        "",
        "Source of truth:",
        "",
    ]
    for rel in plan.get("source_of_truth", []):
        md_lines.append(f"- `{rel}`")
    md_lines.extend(["", "| Entry | Action | Reason | Candidate |", "| --- | --- | --- | --- |"])
    entries = plan.get("entries", [])
    assert isinstance(entries, list)
    candidate_root = changes / "ai-config-reconcile-candidates" / update_id
    for item in entries:
        if not isinstance(item, dict):
            continue
        candidate_rel = ""
        candidate = str(item.get("candidate", ""))
        if item.get("action") == "write-candidate" and candidate:
            candidate_rel = f".tenetora/changes/ai-config-reconcile-candidates/{update_id}/{artifact_rel_for_entry(str(item['path']))}"
            candidate_path = root / candidate_rel
            candidate_path.parent.mkdir(parents=True, exist_ok=True)
            candidate_path.write_text(candidate, encoding="utf-8")
            actions.append(f"write {candidate_path}")
        md_lines.append(
            f"| `{item.get('path')}` | `{item.get('action')}` | {item.get('reason')} | "
            f"{'`' + candidate_rel + '`' if candidate_rel else '-'} |"
        )
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    actions.append(f"write {md_path}")
    if not candidate_root.exists():
        candidate_root.mkdir(parents=True, exist_ok=True)
    return actions


def print_markdown(report: dict[str, object]) -> None:
    chinese = os.environ.get("TENETORA_LANG", "").strip().lower() in {"zh", "zh-cn", "zh-hans", "chinese", "中文"}
    print("# AI 治理配置审计" if chinese else "# AI Harness Audit")
    print()
    print(f"{'根目录' if chinese else 'Root'}: `{report['root']}`")
    harness_exists = ("是" if report["harness_exists"] else "否") if chinese else str(report["harness_exists"]).lower()
    print(f"{'治理目录存在' if chinese else 'Harness exists'}: `{harness_exists}`")
    print()
    print("## 已检测工具" if chinese else "## Detected Tools")
    tools = report["detected_tools"]
    assert isinstance(tools, dict)
    for name, info in tools.items():
        assert isinstance(info, dict)
        signals = ", ".join(info.get("signals", [])) or "-"
        commands = ", ".join(info.get("commands", [])) or "-"
        detected = "是" if info.get("detected") else "否"
        print(
            f"- `{name}`: 已检测={detected} 信号={signals} 命令={commands}"
            if chinese
            else f"- `{name}`: detected={info.get('detected')} signals={signals} commands={commands}"
        )
    print()
    print("## 文件问题" if chinese else "## File Findings")
    files = report["files"]
    assert isinstance(files, list)
    for item in files:
        findings = ", ".join(item["findings"]) if item["findings"] else "none"
        localized_findings = "无" if findings == "none" else findings
        print(
            f"- `{item['path']}` 已跟踪={'是' if item['tracked'] else '否'} 已忽略={'是' if item['ignored'] else '否'} 问题={localized_findings}"
            if chinese
            else f"- `{item['path']}` tracked={item['tracked']} ignored={item['ignored']} findings={findings}"
        )
    print()
    history = report["history_findings"]
    assert isinstance(history, list)
    if history:
        print("## 历史问题" if chinese else "## Historical Findings")
        for item in history:
            print(f"- `{item['path']}`: {item['finding']}（轮换受影响凭证）" if chinese else f"- `{item['path']}`: {item['finding']} (rotate affected credential)")
    plan = report.get("reconcile_plan")
    if isinstance(plan, dict):
        print()
        print("## 收敛计划" if chinese else "## Reconcile Plan")
        summary = plan.get("summary", {})
        candidate_count = summary.get("candidate_count", 0) if isinstance(summary, dict) else 0
        print(f"- 候选数量: `{candidate_count}`" if chinese else f"- candidate_count: `{candidate_count}`")
        entries = plan.get("entries", [])
        if isinstance(entries, list):
            for item in entries:
                if isinstance(item, dict):
                    print(f"- `{item.get('path')}` 动作={item.get('action')} 原因={item.get('reason')}" if chinese else f"- `{item.get('path')}` action={item.get('action')} reason={item.get('reason')}")
        actions = report.get("reconcile_actions", [])
        if isinstance(actions, list) and actions:
            print()
            print("## 收敛产物" if chinese else "## Reconcile Artifacts")
            for action in actions:
                print(f"- {action}")


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
    parser.add_argument("-j", "--json", action="store_true", help="Print JSON instead of Markdown")
    parser.add_argument("--reconcile", action="store_true", help="Build a reconcile plan from .tenetora source-of-truth files.")
    parser.add_argument("--write", action="store_true", help="Write reconcile plan and candidate artifacts. Requires --reconcile.")
    args = parser.parse_args()
    if args.write and not args.reconcile:
        print("--write requires --reconcile", file=sys.stderr)
        return 2
    report = audit(args.path)
    if args.reconcile:
        plan = reconcile_plan(args.path)
        report["reconcile_plan"] = plan
        if args.write:
            try:
                report["reconcile_actions"] = write_reconcile_artifacts(args.path, plan)
            except FileNotFoundError as exc:
                print(str(exc), file=sys.stderr)
                return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_markdown(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
