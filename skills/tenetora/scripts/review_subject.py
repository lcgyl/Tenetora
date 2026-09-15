#!/usr/bin/env python3
"""Compute Git-bound independent-review subjects without mutating the worktree."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Sequence


REVIEW_SCOPE_VERSION = 1
MAX_REVIEW_SUBJECT_ENTRIES = 20000
MAX_REVIEW_DELTA_PATHS = 2000


class ReviewSubjectError(ValueError):
    """Raised when a review subject cannot be resolved safely."""


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_git_root(project_root: Path, raw_git_path: str | Path | None) -> tuple[Path, str]:
    project = project_root.resolve()
    raw = Path(raw_git_path or ".").expanduser()
    candidate = raw.resolve() if raw.is_absolute() else (project / raw).resolve()
    if not _inside(project, candidate):
        raise ReviewSubjectError("review git path must stay inside the governed project")
    result = subprocess.run(
        ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise ReviewSubjectError("review git path is not inside a Git worktree")
    git_root = Path(result.stdout.decode("utf-8", errors="surrogateescape").strip()).resolve()
    if not _inside(project, git_root):
        raise ReviewSubjectError("review Git repository must stay inside the governed project")
    relative = git_root.relative_to(project).as_posix() or "."
    return git_root, relative


def _git_bytes(git_root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(git_root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReviewSubjectError(detail or f"git {' '.join(args)} failed")
    return result.stdout


def _git_optional_bytes(git_root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(git_root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return result.stdout if result.returncode == 0 else b""


def _canonical_index(raw: bytes) -> bytes:
    records: list[bytes] = []
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        try:
            header, path = entry.split(b"\t", 1)
            mode, object_id, stage = header.split(b" ", 2)
        except ValueError as exc:
            raise ReviewSubjectError("git index output has an unsupported shape") from exc
        suffix = b"" if stage == b"0" else b" stage=" + stage
        records.append(mode + b" " + object_id + suffix + b"\t" + path + b"\0")
    return b"".join(records)


def _canonical_head(raw: bytes) -> bytes:
    records: list[bytes] = []
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        try:
            header, path = entry.split(b"\t", 1)
            mode, _kind, object_id = header.split(b" ", 2)
        except ValueError as exc:
            raise ReviewSubjectError("git tree output has an unsupported shape") from exc
        records.append(mode + b" " + object_id + b"\t" + path + b"\0")
    return b"".join(records)


def normalize_review_scope(scopes: Sequence[str] | None) -> tuple[str, ...]:
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
            raise ReviewSubjectError("review scope must stay inside the Git worktree")
        parts = [part for part in value.split("/") if part not in {"", "."}]
        if parts:
            normalized.add("/".join(parts))
    return tuple(sorted(normalized))


def _scope_matches(path: str, scopes: tuple[str, ...]) -> bool:
    if not scopes:
        return True
    return any(
        path == scope or path.startswith(scope + "/")
        for scope in scopes
    )


def _decode_path(raw: bytes) -> str:
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReviewSubjectError("Git review subject contains a non-UTF-8 path") from exc
    if not value or value.startswith("/") or "\\" in value or any(part == ".." for part in Path(value).parts):
        raise ReviewSubjectError("Git review subject contains an unsafe path")
    return value


def _index_scope_entries(raw: bytes, scopes: tuple[str, ...]) -> dict[str, str]:
    grouped: dict[str, list[bytes]] = {}
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        try:
            header, path = entry.split(b"\t", 1)
            mode, object_id, stage = header.split(b" ", 2)
        except ValueError as exc:
            raise ReviewSubjectError("git index output has an unsupported shape") from exc
        decoded = _decode_path(path)
        if _scope_matches(decoded, scopes):
            suffix = b"" if stage == b"0" else b" stage=" + stage
            grouped.setdefault(decoded, []).append(mode + b" " + object_id + suffix + b"\t" + path + b"\0")
    return {
        path: hashlib.sha256(b"".join(sorted(values))).hexdigest()
        for path, values in sorted(grouped.items())
    }


def _head_scope_entries(raw: bytes, scopes: tuple[str, ...]) -> dict[str, str]:
    entries: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        try:
            header, path = entry.split(b"\t", 1)
            mode, _kind, object_id = header.split(b" ", 2)
        except ValueError as exc:
            raise ReviewSubjectError("git tree output has an unsupported shape") from exc
        decoded = _decode_path(path)
        if _scope_matches(decoded, scopes):
            canonical = mode + b" " + object_id + b"\t" + path + b"\0"
            entries[decoded] = hashlib.sha256(canonical).hexdigest()
    return dict(sorted(entries.items()))


def scope_fingerprint(scopes: Sequence[str], entries: dict[str, str]) -> str:
    canonical = json.dumps(
        {
            "version": REVIEW_SCOPE_VERSION,
            "scope": list(scopes),
            "entries": entries,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def subject_entries_fingerprint(entries: dict[str, str]) -> str:
    canonical = json.dumps(
        {"version": REVIEW_SCOPE_VERSION, "entries": entries},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _index_parent_anchor(git_root: Path) -> bytes:
    parents: list[bytes] = []
    head = _git_optional_bytes(git_root, "rev-parse", "--verify", "HEAD").strip()
    if head:
        parents.append(head)
    merge_head_path = _git_optional_bytes(git_root, "rev-parse", "--git-path", "MERGE_HEAD").strip()
    if merge_head_path:
        path = Path(merge_head_path.decode("utf-8", errors="surrogateescape"))
        if not path.is_absolute():
            path = git_root / path
        try:
            parents.extend(line for line in path.read_bytes().splitlines() if line)
        except OSError:
            pass
    return b" ".join(parents)


def _head_parent_anchor(git_root: Path) -> bytes:
    return _git_bytes(git_root, "show", "-s", "--format=%P", "HEAD").strip()


def capture_subject(
    project_root: Path,
    git_path: str | Path | None = None,
    *,
    kind: str = "index",
    scopes: Sequence[str] | None = None,
) -> dict[str, Any]:
    if kind not in {"index", "head"}:
        raise ReviewSubjectError(f"unsupported review subject kind: {kind}")
    git_root, relative = resolve_git_root(project_root, git_path)
    if kind == "index":
        raw = _git_bytes(git_root, "ls-files", "--stage", "-z")
        canonical = _canonical_index(raw)
        parents = _index_parent_anchor(git_root)
        subject_kind = "git-index"
    else:
        raw = _git_bytes(git_root, "ls-tree", "-r", "-z", "--full-tree", "HEAD")
        canonical = _canonical_head(raw)
        parents = _head_parent_anchor(git_root)
        subject_kind = "git-head"
    digest = hashlib.sha256(
        b"review-subject-v2\0"
        + relative.encode("utf-8", errors="surrogatepass")
        + b"\0parents\0"
        + parents
        + b"\0tree\0"
        + canonical
    ).hexdigest()
    result = {
        "subject_fingerprint": digest,
        "subject_kind": subject_kind,
        "subject_git_path": relative,
    }
    if scopes is not None:
        normalized_scope = normalize_review_scope(scopes)
        subject_entries = (
            _index_scope_entries(raw, ())
            if kind == "index"
            else _head_scope_entries(raw, ())
        )
        if len(subject_entries) > MAX_REVIEW_SUBJECT_ENTRIES:
            raise ReviewSubjectError("review subject contains too many paths for scoped delta tracking")
        entries = (
            _index_scope_entries(raw, normalized_scope)
            if kind == "index"
            else _head_scope_entries(raw, normalized_scope)
        )
        result.update(
            {
                "review_scope_bound": True,
                "review_scope": list(normalized_scope),
                "review_scope_entries": entries,
                "review_scope_fingerprint": scope_fingerprint(normalized_scope, entries),
                "review_subject_entries": subject_entries,
                "review_subject_entries_fingerprint": subject_entries_fingerprint(subject_entries),
            }
        )
    else:
        result.update(
            {
                "review_scope_bound": False,
                "review_scope": None,
                "review_scope_entries": None,
                "review_scope_fingerprint": None,
                "review_subject_entries": None,
                "review_subject_entries_fingerprint": None,
            }
        )
    return result


def capture_for_review(
    project_root: Path,
    git_path: str | Path | None,
    trigger: str,
    requested_kind: str = "auto",
    scopes: Sequence[str] | None = None,
) -> dict[str, Any]:
    if requested_kind not in {"auto", "index", "head"}:
        raise ReviewSubjectError(f"unsupported review subject selection: {requested_kind}")
    kind = requested_kind
    if kind == "auto":
        kind = "head" if trigger.strip().lower() == "push" else "index"
    return capture_subject(project_root, git_path, kind=kind, scopes=scopes)


def capture_for_guard(
    project_root: Path,
    git_path: Path,
    operation: str,
    scopes: Sequence[str] | None = None,
) -> dict[str, Any]:
    return capture_subject(
        project_root,
        git_path,
        kind="head" if operation == "push" else "index",
        scopes=scopes,
    )
