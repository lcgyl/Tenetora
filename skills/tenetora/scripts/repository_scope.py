#!/usr/bin/env python3
"""Resolve explicit parent/submodule repository scope for lifecycle writes."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from path_security import is_redirected_path


class RepositoryScopeRequired(Exception):
    """Raised when a lifecycle write needs an explicit repository selection."""


@dataclass(frozen=True)
class RepositoryTarget:
    """One independently governed Git repository selected for a lifecycle run."""

    relative: str
    path: Path
    kind: str


def _safe_relative(value: str) -> str:
    candidate = value.strip().replace("\\", "/")
    if not candidate or candidate.startswith("/") or candidate.startswith("~/"):
        return ""
    parts = tuple(part for part in candidate.split("/") if part)
    if any(part in {".", ".."} for part in parts):
        return ""
    return "/".join(parts)


def _checked_out_directory(root: Path, relative: str) -> bool:
    current = root
    for part in Path(relative).parts:
        current /= part
        try:
            if is_redirected_path(current):
                return False
        except OSError:
            return False
    return current.is_dir()


def git_submodule_paths(root: Path) -> list[str]:
    """Discover declared and checked-out submodules without reading their files."""

    paths: list[str] = []
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "submodule", "status", "--recursive"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        result = None
    if result is not None and result.returncode == 0:
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) < 2:
                continue
            candidate = _safe_relative(parts[1])
            if candidate and _checked_out_directory(root, candidate):
                paths.append(candidate)

    gitmodules = root / ".gitmodules"
    if gitmodules.is_file():
        try:
            declared = subprocess.run(
                [
                    "git",
                    "config",
                    "-f",
                    str(gitmodules),
                    "--get-regexp",
                    r"^submodule\..*\.path$",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            declared = None
        if declared is not None and declared.returncode == 0:
            for line in declared.stdout.splitlines():
                _, separator, value = line.partition(" ")
                candidate = _safe_relative(value if separator else "")
                if candidate and _checked_out_directory(root, candidate):
                    paths.append(candidate)

    return sorted(set(paths), key=lambda value: (value.count("/"), value))


def repository_targets(root: Path) -> list[RepositoryTarget]:
    """Return the parent target followed by independent submodule targets."""

    return [
        RepositoryTarget(".", root, "parent"),
        *[
            RepositoryTarget(relative, root / relative, "submodule")
            for relative in git_submodule_paths(root)
        ],
    ]


def _normalize_requested(raw: str) -> str:
    value = raw.strip()
    if value in {"", "ask"}:
        return "ask"
    if value in {"parent", "root", "."}:
        return "parent"
    if value == "all":
        return "all"
    values = [_safe_relative(item) for item in value.split(",")]
    if not values or any(not item for item in values):
        raise RepositoryScopeRequired(
            "Invalid --repository-scope. Use parent, all, or a comma-separated list of detected submodule paths."
        )
    return ",".join(dict.fromkeys(values))


def scope_help(targets: list[RepositoryTarget]) -> str:
    children = [target.relative for target in targets if target.kind == "submodule"]
    lines = [
        "Independent Git repositories were detected:",
        "  parent: .",
        *[f"  submodule: {child}" for child in children],
        "Choose the repositories for this lifecycle operation: parent, all, or comma-separated submodule paths.",
    ]
    return "\n".join(lines)


def resolve_repository_scope(
    root: Path,
    requested: str,
    *,
    write: bool,
    stdin_is_tty: bool | None = None,
    input_fn: Callable[[str], str] = input,
) -> list[RepositoryTarget]:
    """Resolve a user-owned repository selection before any lifecycle writes."""

    targets = repository_targets(root)
    normalized = _normalize_requested(requested)
    if len(targets) == 1:
        if normalized in {"ask", "parent", "all"}:
            return targets
        raise RepositoryScopeRequired(
            f"Unknown --repository-scope path(s): {normalized}. No independent Git submodules were detected."
        )

    if normalized == "ask":
        if stdin_is_tty is None:
            import sys

            stdin_is_tty = sys.stdin.isatty()
        if not stdin_is_tty:
            raise RepositoryScopeRequired(
                scope_help(targets)
                + "\nNon-interactive lifecycle writes require --repository-scope parent|all|<submodule-path>."
            )
        print(scope_help(targets))
        while True:
            answer = input_fn("Select scope [1] parent [2] all [3] specific submodule: ").strip().lower()
            if answer in {"1", "parent", "root", "."}:
                normalized = "parent"
                break
            if answer in {"2", "all"}:
                normalized = "all"
                break
            if answer in {"3", "submodule", "specific"}:
                selected = input_fn("Enter comma-separated submodule paths: ").strip()
                normalized = _normalize_requested(selected)
                if normalized not in {"ask", "parent", "all"}:
                    break
            print("Please choose parent, all, or detected submodule paths.")

    if normalized == "parent":
        return [targets[0]]
    if normalized == "all":
        return targets

    selected = set(normalized.split(","))
    known = {target.relative for target in targets if target.kind == "submodule"}
    unknown = sorted(selected - known)
    if unknown:
        raise RepositoryScopeRequired(
            f"Unknown --repository-scope path(s): {', '.join(unknown)}.\n{scope_help(targets)}"
        )
    return [target for target in targets if target.relative in selected]


__all__ = [
    "RepositoryScopeRequired",
    "RepositoryTarget",
    "git_submodule_paths",
    "repository_targets",
    "resolve_repository_scope",
    "scope_help",
]
