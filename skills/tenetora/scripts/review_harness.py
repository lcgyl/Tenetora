#!/usr/bin/env python3
"""Review latest .tenetora update artifacts and quality state."""

from __future__ import annotations

import argparse
import json
import os
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


def latest(root: Path, pattern: str) -> Path | None:
    files = sorted((root / ".tenetora" / "changes").glob(pattern), key=lambda path: path.stat().st_mtime)
    return files[-1] if files else None


def rel(root: Path, path: Path | None) -> str:
    return path.relative_to(root).as_posix() if path else "None"


def run_script(script: str, root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    path = Path(__file__).with_name(script)
    return subprocess.run(
        [sys.executable, str(path), "--path", str(root), *extra],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
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
        help="Project directory to review. Defaults to the current directory.",
    )
    parser.add_argument(
        "--repository-scope",
        default="ask",
        metavar="{ask,parent,all,<submodule-path>}",
        help=(
            "Repositories to refresh/review: parent, all, or a comma-separated list of detected Git submodule paths. "
            "ask requires interactive confirmation when submodules are present."
        ),
    )
    parser.add_argument("--apply", action="store_true", help="Apply a refresh strategy before reviewing.")
    parser.add_argument(
        "--strategy",
        choices=("diff", "backup", "merge", "replace"),
        default="merge",
        help="Refresh strategy used with --apply. Defaults to merge so existing harness content is preserved.",
    )
    parser.add_argument("-t", "--tools", default="auto", help="Tools passed through to refresh when --apply is used.")
    parser.add_argument(
        "-m",
        "--mode",
        choices=("auto", "scaffold", "extract"),
        default="auto",
        help="Initialization mode passed through to refresh when --apply is used.",
    )
    parser.add_argument(
        "--migrate",
        choices=("ask", "plan", "ignore", "transfer", "merge"),
        default="ask",
        help="Migration handling passed through to refresh when --apply is used.",
    )
    parser.add_argument(
        "--entrypoints",
        choices=("plan", "backup", "merge"),
        default="plan",
        help="Entrypoint handling passed through to refresh when --apply is used.",
    )
    parser.add_argument(
        "--gitignore",
        choices=("auto", "yes", "no"),
        default="auto",
        help="Gitignore handling passed through to refresh when --apply is used.",
    )
    parser.add_argument(
        "--defaults",
        choices=("off", "missing", "suggest"),
        default="missing",
        help="Default pack handling passed through to refresh when --apply is used.",
    )
    parser.add_argument(
        "--skip-global-migration-scan",
        action="store_true",
        help="Only scan project-level rules and skills when --apply is used. This is the default.",
    )
    parser.add_argument(
        "--include-global-migration-scan",
        action="store_true",
        help="Also scan user-level global skills when --apply is used. Use only when the user explicitly asks.",
    )
    args = parser.parse_args()
    root = args.path

    chinese = os.environ.get("TENETORA_LANG", "").strip().lower() in {"zh", "zh-cn", "zh-hans", "chinese", "中文"}
    print("治理目录审查" if chinese else "Harness Review")
    print(f"{'路径' if chinese else 'Path'}: {root}")
    if args.apply:
        refresh_args = [
            "--tools",
            args.tools,
            "--repository-scope",
            args.repository_scope,
            "--strategy",
            args.strategy,
            "--mode",
            args.mode,
            "--migrate",
            args.migrate,
            "--entrypoints",
            args.entrypoints,
            "--gitignore",
            args.gitignore,
            "--defaults",
            args.defaults,
        ]
        include_global_migrations = args.include_global_migration_scan and not args.skip_global_migration_scan
        if include_global_migrations:
            refresh_args.append("--include-global-migration-scan")
        else:
            refresh_args.append("--skip-global-migration-scan")
        print(f"{'应用策略' if chinese else 'Apply'}: {args.strategy}")
        print()
        refresh = run_script("refresh_harness.py", root, *refresh_args)
        if refresh.stdout:
            print(refresh.stdout, end="")
        if refresh.stderr:
            print(refresh.stderr, end="", file=sys.stderr)
        if refresh.returncode != 0:
            return refresh.returncode
        print()

    update_report = latest(root, "*-update-report.md")
    update_diff = latest(root, "*-update-diff.patch")
    changes_index = root / ".tenetora" / "changes" / "INDEX.md"
    validation = run_script("validate_harness.py", root)
    audit = run_script("audit_harness_quality.py", root, "--profile", "engineering", "--json")
    audit_status = "unknown"
    scores = ""
    if audit.stdout:
        try:
            payload = json.loads(audit.stdout)
            audit_status = str(payload.get("status", "unknown"))
            score_payload = payload.get("scores", {})
            if isinstance(score_payload, dict):
                scores = ", ".join(f"{key} {value}" for key, value in score_payload.items())
        except json.JSONDecodeError:
            audit_status = "invalid-json"

    print(f"{'变更索引' if chinese else 'Changes index'}: {rel(root, changes_index if changes_index.exists() else None)}")
    if changes_index.exists():
        print()
        print(changes_index.read_text(encoding="utf-8").rstrip())
        print()
    print(f"{'最新更新报告' if chinese else 'Latest update report'}: {rel(root, update_report)}")
    print(f"{'最新更新差异' if chinese else 'Latest update diff'}: {rel(root, update_diff)}")
    print(f"{'验证' if chinese else 'Validation'}: {'pass' if validation.returncode == 0 else 'fail'}")
    print(f"{'审计' if chinese else 'Audit'}: {audit_status}")
    if scores:
        print(f"{'评分' if chinese else 'Scores'}: {scores}")
    if validation.returncode != 0 and validation.stdout:
        print()
        print(validation.stdout, end="")
    if validation.stderr:
        print(validation.stderr, end="", file=sys.stderr)
    if audit.stderr:
        print(audit.stderr, end="", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
