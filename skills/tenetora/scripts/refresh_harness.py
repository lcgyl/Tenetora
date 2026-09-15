#!/usr/bin/env python3
"""Refresh an existing .tenetora with reviewable update artifacts."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from path_security import validate_existing_project_path


def existing_project_path(raw: str) -> Path:
    try:
        return validate_existing_project_path(raw)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def gitignore_mode(root: Path, requested: str) -> str:
    if requested != "auto":
        return requested
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        return "no"
    text = gitignore.read_text(encoding="utf-8", errors="ignore")
    return "yes" if any(line.strip().rstrip("/") == ".tenetora" for line in text.splitlines()) else "no"


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--help", action="help", help="Show this help message and exit.")
    parser.add_argument(
        "-p",
        "--path",
        default=".",
        type=existing_project_path,
        metavar="<project-dir>",
        help="Project directory to refresh. Defaults to the current directory.",
    )
    parser.add_argument(
        "--repository-scope",
        default="ask",
        metavar="{ask,parent,all,<submodule-path>}",
        help=(
            "Repositories to refresh: parent, all, or a comma-separated list of detected Git submodule paths. "
            "ask requires interactive confirmation when submodules are present."
        ),
    )
    parser.add_argument("-t", "--tools", default="auto", help="auto or comma-separated tool list")
    parser.add_argument(
        "--strategy",
        choices=("diff", "backup", "merge", "replace"),
        default="diff",
        help="Refresh strategy. Defaults to diff so existing files are not changed.",
    )
    parser.add_argument(
        "-m",
        "--mode",
        choices=("auto", "scaffold", "extract"),
        default="auto",
        help="Initialization mode passed through to init.",
    )
    parser.add_argument(
        "--migrate",
        choices=("ask", "plan", "ignore", "transfer", "merge"),
        default="plan",
        help="Migration handling passed through to init. Defaults to plan for preview refreshes.",
    )
    parser.add_argument(
        "--entrypoints",
        choices=("plan", "backup", "merge"),
        default="plan",
        help="Entrypoint handling passed through to init. Defaults to plan so entry files are not rewritten.",
    )
    parser.add_argument(
        "--defaults",
        choices=("off", "missing", "suggest"),
        default="missing",
        help="Default pack handling passed through to init. Defaults to filling missing Tenetora defaults.",
    )
    parser.add_argument(
        "--gitignore",
        choices=("auto", "yes", "no"),
        default="auto",
        help="auto preserves the current .gitignore state; yes/no are passed through explicitly.",
    )
    parser.add_argument(
        "--skip-global-migration-scan",
        action="store_true",
        help="Only scan project-level rules and skills for migration candidates. This is the default.",
    )
    parser.add_argument(
        "--include-global-migration-scan",
        action="store_true",
        help="Also scan user-level global skills for migration candidates. Use only when the user explicitly asks.",
    )
    args = parser.parse_args()

    root = args.path
    gitignore = gitignore_mode(root, args.gitignore)
    init_script = Path(__file__).with_name("init_harness.py")
    command = [
        sys.executable,
        str(init_script),
        "--path",
        str(root),
        "--repository-scope",
        args.repository_scope,
        "--tools",
        args.tools,
        "--write",
        "--gitignore",
        gitignore,
        "--migrate",
        args.migrate,
        "--entrypoints",
        args.entrypoints,
        "--defaults",
        args.defaults,
        "--mode",
        args.mode,
        "--update-strategy",
        args.strategy,
    ]
    include_global_migrations = args.include_global_migration_scan and not args.skip_global_migration_scan
    if include_global_migrations:
        command.append("--include-global-migration-scan")
    else:
        command.append("--skip-global-migration-scan")

    print("Harness Refresh")
    print(f"Path: {root}")
    global_scan = "--include-global-migration-scan" if include_global_migrations else "--skip-global-migration-scan"
    print(f"Command: tenetora init --write --gitignore {gitignore} --repository-scope {args.repository_scope} --migrate {args.migrate} --entrypoints {args.entrypoints} --defaults {args.defaults} --mode {args.mode} --update-strategy {args.strategy} {global_scan}")
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
