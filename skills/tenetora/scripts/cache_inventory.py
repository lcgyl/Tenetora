"""Read-only project cache inventory; no cache deletion is performed here."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import stat
import time
from pathlib import Path

from path_security import (
    is_redirected_path,
    validate_existing_project_path,
    validate_unredirected_path,
    validate_unredirected_file_path,
)
from worktree_fingerprint import git_repository_env
from governance_trail import load_retained_trail

MAX_ITEMS = 100000
MAX_SECONDS = 10
MAX_STATE_BYTES = 4 * 1024 * 1024
STATE_FILES = (
    "loop-state.json",
    "current-evidence.json",
    "current-alignment.json",
    "aggregate-claim.json",
    "features.json",
    "delegation-state.json",
)


def sizes(path: Path, deadline: float) -> dict:
    pending = [path]
    logical = allocated = files = links = 0
    seen = 0
    complete = True
    while pending:
        if seen >= MAX_ITEMS or time.monotonic() > deadline:
            complete = False
            break
        current = pending.pop()
        seen += 1
        try:
            metadata = current.lstat()
        except OSError:
            complete = False
            continue
        logical += metadata.st_size
        allocated += getattr(metadata, "st_blocks", 0) * 512
        if is_redirected_path(current):
            links += 1
            continue
        if stat.S_ISDIR(metadata.st_mode):
            try:
                with os.scandir(current) as entries:
                    for item in entries:
                        if len(pending) + seen >= MAX_ITEMS:
                            complete = False
                            break
                        pending.append(Path(item.path))
            except OSError:
                complete = False
        elif stat.S_ISREG(metadata.st_mode):
            files += 1
        else:
            complete = False
    return {
        "logical_bytes": logical,
        "allocated_bytes": allocated if hasattr(os.stat_result, "st_blocks") else None,
        "regular_files": files,
        "redirected_entries": links,
        "complete": complete,
        "visited_entries": seen,
    }


def references(root: Path) -> tuple[set[str], bool]:
    found = set()
    complete = True

    def walk(value):
        if isinstance(value, str):
            prefix = ".tenetora/.cache/"
            candidate = value.replace("\\", "/")
            if candidate.startswith(str(root).replace("\\", "/") + "/"):
                candidate = candidate[len(str(root)) + 1 :]
            if candidate.startswith(prefix) and ".." not in Path(candidate).parts:
                found.add(candidate)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for name in STATE_FILES:
        path = root / ".tenetora/state" / name
        if not path.exists():
            continue
        try:
            validate_unredirected_file_path(path, label="cache reference state")
            if (
                is_redirected_path(path)
                or not path.is_file()
                or path.stat().st_size > MAX_STATE_BYTES
            ):
                raise ValueError("unsafe state")
            walk(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, RuntimeError, ValueError, RecursionError):
            complete = False
    try:
        history = load_retained_trail(root, project=False)
        if history.get("invalid") or history.get("conflicts"):
            complete = False
        else:
            walk(history.get("events", []))
    except (OSError, ValueError, RuntimeError):
        complete = False
    machine = Path(os.environ.get("TENETORA_HOME") or (Path.home() / ".tenetora"))
    for relative in ("current", "source/tenetora", "runtime/python"):
        path = machine / relative
        try:
            if path.is_symlink():
                walk(str(path.resolve(strict=False)))
            elif relative == "runtime/python":
                validate_unredirected_file_path(path, label="runtime Python reference")
                if path.is_file() and path.stat().st_size <= 4096:
                    walk(path.read_text().strip())
        except (OSError, RuntimeError):
            complete = False
    return found, complete


def git(root: Path, *args, text=True):
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            env=git_repository_env(),
            capture_output=True,
            text=text,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(
            [], 1, "" if text else b"", "" if text else b""
        )


def worktrees(root: Path) -> tuple[list[dict], bool]:
    result = git(root, "worktree", "list", "--porcelain", "-z", text=False)
    if result.returncode:
        return [], False
    entries = []
    current = None
    for item in result.stdout.split(b"\0"):
        if item.startswith(b"worktree "):
            current = {"path": Path(os.fsdecode(item[9:])), "prunable": False}
            entries.append(current)
        elif current is not None and item.startswith(b"prunable"):
            current["prunable"] = True
    return entries, True


def inventory(raw: Path) -> dict:
    root = validate_existing_project_path(raw).resolve()
    directory = validate_unredirected_path(
        root / ".tenetora/.cache", label="project cache"
    )
    if directory.exists() and not directory.is_dir():
        raise ValueError("Project cache is not a directory.")
    refs, refs_complete = references(root)
    trees, trees_complete = worktrees(root)
    rows = []
    candidates = []
    deadline = time.monotonic() + MAX_SECONDS
    for path in sorted(directory.iterdir()) if directory.exists() else []:
        relative = path.relative_to(root).as_posix()
        reasons = []
        measurement = sizes(path, deadline)
        if is_redirected_path(path):
            reasons.append("redirected-entry; target was not followed")
        related = [
            item
            for item in trees
            if item["path"] == path or path in item["path"].parents
        ]
        for item in related:
            if item["prunable"]:
                reasons.append(
                    "prunable-worktree-registration; explicit Git review required"
                )
                continue
            state = git(
                item["path"], "status", "--porcelain=v1", "--untracked-files=all"
            )
            reasons.append(
                "git-worktree-dirty"
                if state.returncode or state.stdout.strip()
                else "git-worktree; preserve until explicit lifecycle review"
            )
        referenced = any(
            value == relative
            or value.startswith(relative + "/")
            or relative.startswith(value + "/")
            for value in refs
        )
        if referenced:
            reasons.append(
                "referenced-by-governance-state; validity is not inferred from a label"
            )
        if not refs_complete or not trees_complete:
            reasons.append("reference-inventory-incomplete")
        generator = (
            "delegation"
            if path.name == "subagents"
            else "hook-timing"
            if path.name == "hook-timing"
            else "unknown"
        )
        if path.name == "subagents" and path.is_dir() and not is_redirected_path(path):
            archive = path / "archive"
            cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - 7 * 86400
            if archive.is_dir() and not is_redirected_path(archive):
                for report in sorted(archive.glob("*.md")):
                    rel = report.relative_to(root).as_posix()
                    if (
                        re.fullmatch(
                            r"\d{8}T\d{6}Z-(?:code-reviewer|codebase-scout|security-auditor|implementer)-[0-9a-f]{16}\.md",
                            report.name,
                        )
                        and not is_redirected_path(report)
                        and report.is_file()
                        and report.stat().st_mtime < cutoff
                        and not any(
                            value == rel
                            or value.startswith(rel + "/")
                            or rel.startswith(value + "/")
                            for value in refs
                        )
                        and refs_complete
                        and trees_complete
                        and not related
                    ):
                        candidates.append(
                            {
                                "path": rel,
                                "reason": "delegation archive exceeded existing seven-day retention",
                                "bytes": report.stat().st_size,
                                "review_required": True,
                            }
                        )
        if not reasons:
            reasons.append("ownership-or-terminal-lifecycle-not-proven; preserve")
        rows.append(
            {
                "path": relative,
                "generator": generator,
                "disposition": "preserve",
                "keep_reasons": reasons,
                "referenced": referenced,
                "worktrees": len(related),
                **measurement,
            }
        )
    return {
        "version": 1,
        "status": "planned",
        "operation": "cleanup",
        "scope": "project-cache",
        "read_only": True,
        "entries": rows,
        "candidates": candidates,
        "remove": [],
        "reference_inventory_complete": refs_complete and trees_complete,
        "logical_bytes": sum(row["logical_bytes"] for row in rows),
        "limitations": [
            "sizes are bounded observations; incomplete totals are lower bounds",
            "candidates require human review; this command never deletes cache entries",
            "unknown, referenced, redirected and Git worktree resources are preserved",
        ],
    }
