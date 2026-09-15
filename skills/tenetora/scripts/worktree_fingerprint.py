#!/usr/bin/env python3
"""Compute a stable fingerprint for the verified contents of a Git worktree."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any, Sequence


WORKTREE_FINGERPRINT_VERSION = 3
WORKTREE_ENTRIES_VERSION = 1
VOLATILE_WORKTREE_PATHS = frozenset(
    {
        ".tenetora/state/aggregate-claim.json",
        ".tenetora/state/governance-trail.json",
        ".tenetora/state/alignment-lifecycle.json",
    }
)


def _is_volatile_path(relative: str) -> bool:
    if relative in VOLATILE_WORKTREE_PATHS:
        return True
    path = Path(relative)
    return (
        len(path.parts) >= 3
        and path.parts[:2] == (".tenetora", "state")
        and path.name.startswith(".")
        and path.name.endswith(".lock")
    )


def run_git(root: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        text=text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def git_head(root: Path) -> str | None:
    result = run_git(root, "rev-parse", "HEAD")
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value if re.fullmatch(r"[0-9a-f]{40}", value) else None


def _tracked_and_untracked_paths(root: Path) -> list[str] | None:
    result = run_git(
        root,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
        text=False,
    )
    if result.returncode != 0:
        return None
    paths: list[str] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative = os.fsdecode(raw)
        candidate = Path(relative)
        if _is_volatile_path(relative):
            continue
        if candidate.is_absolute() or candidate.drive or ".." in candidate.parts:
            return None
        paths.append(relative)
    return sorted(set(paths))


def normalize_scopes(scopes: Sequence[str] | None) -> tuple[str, ...]:
    """Normalize project-relative verification scopes and reject traversal."""

    normalized: set[str] = set()
    for raw in scopes or ():
        value = str(raw).strip().replace("\\", "/")
        if not value or value == ".":
            continue
        path = Path(value)
        if (
            path.is_absolute()
            or value.startswith("/")
            or value.startswith("~/")
            or re.match(r"^[A-Za-z]:", value)
            or any(part == ".." for part in path.parts)
        ):
            raise ValueError(f"verification scope must stay inside the project: {raw}")
        parts = [part for part in value.split("/") if part not in {"", "."}]
        if not parts:
            continue
        normalized.add("/".join(parts))
    return tuple(sorted(normalized))


def verification_scopes_intersect(
    left: Sequence[str] | None,
    right: Sequence[str] | None,
) -> bool:
    """Return whether two verification scopes can cover common project content."""

    left_scopes = normalize_scopes(left)
    right_scopes = normalize_scopes(right)
    if not left_scopes or not right_scopes:
        return True
    return any(
        left_scope == right_scope
        or left_scope.startswith(right_scope + "/")
        or right_scope.startswith(left_scope + "/")
        for left_scope in left_scopes
        for right_scope in right_scopes
    )


def _matches_scope(relative: str, scopes: tuple[str, ...]) -> bool:
    if not scopes:
        return True
    return any(
        relative == scope
        or relative.startswith(scope + "/")
        or scope.startswith(relative + "/")
        for scope in scopes
    )


def _file_content_digest(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        before = path.lstat()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.lstat()
    except (OSError, ValueError):
        return None
    if (
        before.st_mode != after.st_mode
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ino != after.st_ino
    ):
        return None
    return digest.hexdigest()


def _safe_lstat(root: Path, relative: str) -> tuple[os.stat_result | None, bool]:
    current = root
    parts = Path(relative).parts
    for index, part in enumerate(parts):
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return None, True
        except OSError:
            return None, False
        if index < len(parts) - 1 and stat.S_ISLNK(metadata.st_mode):
            return None, False
    return metadata, True


def _worktree_entry(root: Path, relative: str) -> str | None:
    path = root / relative
    metadata, safe = _safe_lstat(root, relative)
    if not safe:
        return None
    if metadata is None:
        return f"{relative}\0missing"
    mode = stat.S_IMODE(metadata.st_mode)
    if stat.S_ISLNK(metadata.st_mode):
        try:
            target = os.fsencode(os.readlink(path))
            after = path.lstat()
        except OSError:
            return None
        if (
            metadata.st_mode != after.st_mode
            or metadata.st_mtime_ns != after.st_mtime_ns
            or metadata.st_ino != after.st_ino
        ):
            return None
        digest = hashlib.sha256(target).hexdigest()
        return f"{relative}\0symlink\0{mode:o}\0{digest}"
    if stat.S_ISREG(metadata.st_mode):
        digest = _file_content_digest(path)
        if digest is None:
            return None
        return f"{relative}\0file\0{mode:o}\0{digest}"
    if stat.S_ISDIR(metadata.st_mode):
        nested = git_state_fingerprint(path)
        if nested is None:
            return None
        return f"{relative}\0repository\0{mode:o}\0{nested}"
    return None


def git_state_fingerprint(root: Path, scopes: Sequence[str] | None = None) -> str | None:
    """Return a content fingerprint independent of Git index state and HEAD."""

    try:
        normalized_scopes = normalize_scopes(scopes)
    except ValueError:
        return None

    entries = git_state_entries(root, normalized_scopes)
    if entries is None:
        return None
    scope_tag = "\0".join(normalized_scopes) if normalized_scopes else "<all>"
    state = "\0".join([str(WORKTREE_FINGERPRINT_VERSION), "scope", scope_tag, *entries.values()])
    return hashlib.sha256(state.encode("utf-8", errors="surrogatepass")).hexdigest()


def git_state_entries(root: Path, scopes: Sequence[str] | None = None) -> dict[str, str] | None:
    """Return stable per-path entries for explaining a later fingerprint change."""

    try:
        normalized_scopes = normalize_scopes(scopes)
    except ValueError:
        return None
    if git_head(root) is None:
        return None
    for _ in range(2):
        paths = _tracked_and_untracked_paths(root)
        if paths is None:
            return None
        entries: dict[str, str] = {}
        selected_paths = [relative for relative in paths if _matches_scope(relative, normalized_scopes)]
        for relative in selected_paths:
            entry = _worktree_entry(root, relative)
            if entry is None:
                return None
            entries[relative] = entry
        if paths == _tracked_and_untracked_paths(root):
            return dict(sorted(entries.items()))
    return None
