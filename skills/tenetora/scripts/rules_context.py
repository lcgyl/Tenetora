#!/usr/bin/env python3
"""Print the smallest useful .tenetora rule set for a task context."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from governance_trail import append_event  # noqa: E402
from path_security import harness_missing_message, validate_existing_project_path  # noqa: E402


def use_chinese() -> bool:
    return os.environ.get("TENETORA_LANG", "").strip().lower() in {"zh", "zh-cn", "zh-hans", "chinese", "中文"}


CONTEXTS: dict[str, list[tuple[str, ...]]] = {
    "default": [
        (".tenetora/README.md",),
        (".tenetora/rules/agent-control.md", ".tenetora/rules/project.md"),
        (".tenetora/rules/subagent-dispatch.md",),
        (".tenetora/workflows/task-start.md",),
        (".tenetora/workflows/verification.md",),
    ],
    "build": [
        (".tenetora/rules/build-and-deps.md", ".tenetora/rules/project.md"),
        (".tenetora/rules/dependency-change.md", ".tenetora/rules/project-meta.md"),
        (".tenetora/rules/subagent-dispatch.md",),
        (".tenetora/workflows/verification.md",),
    ],
    "test": [
        (".tenetora/rules/testing.md", ".tenetora/rules/verification-claims.md"),
        (".tenetora/rules/subagent-dispatch.md",),
        (".tenetora/workflows/verification.md",),
        (".tenetora/guardrails/quality-gates.md",),
        (".tenetora/guardrails/checks/test-framework-drift-scan.sh",),
    ],
    "change-impact": [
        (".tenetora/workflows/change-impact-preflight.md",),
        (".tenetora/rules/subagent-dispatch.md",),
        (".tenetora/workflows/verification.md",),
    ],
    "planning": [
        (".tenetora/workflows/end-to-end-skeleton-first.md",),
        (".tenetora/templates/implementation-plan.md",),
        (".tenetora/rules/subagent-dispatch.md",),
        (".tenetora/workflows/verification.md",),
    ],
    "commit": [
        (".tenetora/rules/git-safety.md",),
        (".tenetora/rules/security-boundary.md", ".tenetora/rules/security.md"),
        (".tenetora/rules/subagent-dispatch.md",),
        (".tenetora/workflows/verification.md",),
        (".tenetora/guardrails/checks/secret-scan.sh",),
        (".tenetora/guardrails/checks/local-path-scan.sh",),
    ],
    "security": [
        (".tenetora/rules/security-boundary.md", ".tenetora/rules/security.md"),
        (".tenetora/rules/prompt-injection.md",),
        (".tenetora/rules/subagent-dispatch.md",),
        (".tenetora/guardrails/checks/secret-scan.sh",),
        (".tenetora/guardrails/checks/local-path-scan.sh",),
    ],
    "external-input": [
        (".tenetora/rules/prompt-injection.md",),
        (".tenetora/rules/subagent-dispatch.md",),
        (".tenetora/workflows/untrusted-input.md",),
        (".tenetora/skills/prompt-guard.md",),
    ],
    "harness": [
        (".tenetora/rules/harness-governance.md", ".tenetora/rules/project.md"),
        (".tenetora/rules/rule-capture.md",),
        (".tenetora/rules/subagent-dispatch.md",),
        (".tenetora/workflows/harness-maintenance.md", ".tenetora/workflows/task-start.md"),
        (".tenetora/README.md",),
    ],
    "docs": [
        (".tenetora/docs/conventions/README.md",),
        (".tenetora/wiki/project-map.md",),
        (".tenetora/rules/subagent-dispatch.md",),
        (".tenetora/workflows/verification.md",),
    ],
}

OPTIONAL_CONTEXT_FILES: dict[str, tuple[str, ...]] = {
    "planning": (
        ".tenetora/docs/architecture/overview.md",
        ".tenetora/docs/architecture/boundaries.md",
        ".tenetora/wiki/project-map.md",
    ),
    "change-impact": (
        ".tenetora/rules/change-impact.md",
        ".tenetora/docs/architecture/boundaries.md",
        ".tenetora/wiki/project-map.md",
    ),
    "commit": (
        ".tenetora/rules/git.md",
        ".tenetora/rules/git-and-branch.md",
    ),
}


def existing_project_path(raw: str) -> Path:
    try:
        return validate_existing_project_path(raw)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(
        prog="tenetora rules",
        description="Print the relevant .tenetora rules/workflows for one task context.",
        add_help=False,
    )
    command_parser.add_argument("--help", action="help", help="Show this help message and exit.")
    command_parser.add_argument("--context", required=True, help=f"Task context: {', '.join(sorted(CONTEXTS))}.")
    command_parser.add_argument(
        "-p",
        "--path",
        default=".",
        type=existing_project_path,
        metavar="<project-dir>",
        help="Project directory. Defaults to the current directory.",
    )
    command_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    command_parser.add_argument("--no-record", action="store_true", help="Do not append a governance-trail event.")
    return command_parser


def resolve_ref(root: Path, alternatives: tuple[str, ...]) -> str:
    for rel in alternatives:
        if (root / rel).exists():
            return rel
    return alternatives[0]


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def context_payload(root: Path, context: str) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for alternatives in CONTEXTS[context]:
        rel = resolve_ref(root, alternatives)
        path = root / rel
        item: dict[str, Any] = {
            "path": rel,
            "exists": path.is_file(),
            "alternatives": list(alternatives),
        }
        if path.is_file():
            text = read_text(path)
            item["bytes"] = len(text.encode("utf-8"))
            item["content"] = text
        files.append(item)
    existing_paths = {str(item["path"]) for item in files}
    for rel in OPTIONAL_CONTEXT_FILES.get(context, ()):
        path = root / rel
        if rel in existing_paths or not path.is_file():
            continue
        text = read_text(path)
        files.append(
            {
                "path": rel,
                "exists": True,
                "optional": True,
                "alternatives": [rel],
                "bytes": len(text.encode("utf-8")),
                "content": text,
            }
        )
    return {
        "version": 1,
        "status": "pass",
        "context": context,
        "files": files,
        "missing": [item["path"] for item in files if not item["exists"]],
    }


def context_payloads(root: Path, contexts: list[str]) -> dict[str, Any]:
    if len(contexts) == 1:
        return context_payload(root, contexts[0])
    files_by_path: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for context in contexts:
        payload = context_payload(root, context)
        for item in payload["files"]:
            existing = files_by_path.setdefault(str(item["path"]), dict(item))
            context_list = existing.setdefault("contexts", [])
            if isinstance(context_list, list):
                context_list.append(context)
        missing.extend(str(item) for item in payload["missing"])
    return {
        "version": 1,
        "status": "pass",
        "context": ",".join(contexts),
        "contexts": contexts,
        "files": list(files_by_path.values()),
        "missing": sorted(set(missing)),
    }


def print_text(payload: dict[str, Any]) -> None:
    print(f"# Tenetora 规则：{payload['context']}" if use_chinese() else f"# Tenetora Rules: {payload['context']}")
    print()
    for item in payload["files"]:
        marker = ("已找到" if item["exists"] else "缺失") if use_chinese() else ("found" if item["exists"] else "missing")
        print(f"## {item['path']} ({marker})")
        print()
        if item["exists"]:
            print(str(item.get("content", "")).rstrip())
            print()
        else:
            print("此规则位置没有对应的项目文件。" if use_chinese() else "No project file found for this rule slot.")
            print()
    if payload["missing"]:
        print("缺失的规则位置：" if use_chinese() else "Missing rule slots:")
        for rel in payload["missing"]:
            print(f"- {rel}")


def error_payload(message: str) -> dict[str, Any]:
    return {
        "version": 1,
        "status": "error",
        "message": message,
        "available_contexts": sorted(CONTEXTS),
    }


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    contexts = [item.strip().lower() for item in str(args.context).split(",") if item.strip()]
    unknown = [context for context in contexts if context not in CONTEXTS]
    if not contexts or unknown:
        payload = error_payload(f"Unknown context: {', '.join(unknown) if unknown else args.context}")
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(payload["message"], file=sys.stderr)
            print("Available contexts: " + ", ".join(payload["available_contexts"]), file=sys.stderr)
        return 2
    root: Path = args.path
    if not (root / ".tenetora").is_dir():
        payload = error_payload(
            harness_missing_message(root)
        )
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(payload["message"], file=sys.stderr)
        return 2
    payload = context_payloads(root, contexts)
    if not args.no_record:
        append_event(
            root,
            {
                "type": "rules-context",
                "context": payload["context"],
                "contexts": contexts,
                "status": payload["status"],
                "files": [item["path"] for item in payload["files"] if item["exists"]],
                "missing": payload["missing"],
                "source": "tenetora rules",
            },
        )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_text(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
