"""Bridge from the package CLI to the repository installer script."""

from __future__ import annotations

import argparse
import runpy
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install-skill.py"


def scope_flag(args: argparse.Namespace) -> str | None:
    if getattr(args, "scope_flag", None):
        return args.scope_flag
    return getattr(args, "scope", None)


def run_install_script(action: str, args: argparse.Namespace) -> int:
    forwarded = [
        "--tools",
        args.tools,
        "--mode",
        args.mode,
        "--path",
        str(args.project_path),
        "--compact",
        "--progress",
        getattr(args, "progress", None) or "auto",
    ]
    selected_scope = scope_flag(args)
    if action == "update" and selected_scope is None:
        forwarded.append("--all-existing")
    elif selected_scope in {None, "global"}:
        forwarded.append("-g")
    elif selected_scope == "project":
        forwarded.append("-i")
    elif selected_scope == "both":
        forwarded.append("-b")
    else:
        forwarded.extend(["--scope", selected_scope])

    if getattr(args, "force", False):
        forwarded.append("--force")
    if getattr(args, "dry_run", False):
        forwarded.append("--dry-run")
    if getattr(args, "json", False):
        forwarded.append("--json")
    if getattr(args, "allow_skills_only", False):
        forwarded.append("--allow-skills-only")
    if getattr(args, "require_full", False):
        forwarded.append("--require-full")
    if getattr(args, "prune_shadowed", False):
        forwarded.append("--prune-shadowed")
    if getattr(args, "no_prune_shadowed", False):
        forwarded.append("--no-prune-shadowed")
    if getattr(args, "verbose", False):
        forwarded.append("--verbose")
    codex_hooks = getattr(args, "codex_hooks", "auto")
    forwarded.extend(["--codex-hooks", codex_hooks])
    if getattr(args, "allow_tracked_codex_hooks", False):
        forwarded.append("--allow-tracked-codex-hooks")
    if action == "update":
        forwarded.append("--update")
    elif action == "preflight":
        forwarded.append("--preflight")

    old_argv = sys.argv[:]
    try:
        sys.argv = [str(INSTALL_SCRIPT), *forwarded]
        runpy.run_path(str(INSTALL_SCRIPT), run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        sys.argv = old_argv
    return 0
