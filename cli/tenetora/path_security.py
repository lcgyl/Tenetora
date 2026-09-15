"""Path validation helpers for user-controlled Tenetora locations."""

from __future__ import annotations

import os
import posixpath
import shlex
import stat
import subprocess
from pathlib import Path


def _lexical_absolute_path(raw: Path | str, *, label: str) -> Path:
    # Preserve a concrete Path flavour when callers model another host in a
    # test (for example, POSIX paths with os.name temporarily set to nt).
    candidate = (raw if isinstance(raw, Path) else Path(raw)).expanduser()
    if ".." in candidate.parts:
        raise RuntimeError(f"{label} contains parent traversal: {candidate}")
    try:
        return candidate.__class__(os.path.abspath(os.fspath(candidate)))
    except OSError as exc:
        raise RuntimeError(f"cannot normalize {label}: {candidate}: {exc}") from exc


def is_redirected_path(path: Path) -> bool:
    """Detect symlinks and Windows reparse points on all supported Pythons."""

    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction and is_junction():
            return True
        if os.name == "nt":
            try:
                attributes = getattr(path.lstat(), "st_file_attributes", 0)
            except FileNotFoundError:
                return False
            return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        return False
    except OSError:
        return True


def is_allowed_system_redirect(path: Path) -> bool:
    raw_path = os.fspath(path)
    expected_raw = {"/var": "/private/var", "/tmp": "/private/tmp"}.get(raw_path)
    if expected_raw is None:
        return False
    try:
        if not path.is_symlink():
            return False
        target_raw = os.readlink(path)
        if not target_raw.startswith("/"):
            target_raw = posixpath.join(posixpath.dirname(raw_path), target_raw)
        target_raw = posixpath.normpath(target_raw)
        expected = path.__class__(expected_raw)
        return target_raw == expected_raw and expected.is_dir()
    except OSError:
        return False


def validate_unredirected_path(raw: Path | str, *, label: str) -> Path:
    """Return a lexical absolute path after checking every existing component."""

    candidate = _lexical_absolute_path(raw, label=label)
    if candidate == candidate.__class__(candidate.anchor):
        raise RuntimeError(f"{label} cannot be the filesystem root: {candidate}")
    current = candidate.__class__(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        redirected = is_redirected_path(current)
        exists = current.exists() or redirected
        if redirected and not is_allowed_system_redirect(current):
            raise RuntimeError(f"{label} contains a symbolic link or junction: {current}")
        if exists and not current.is_dir():
            raise RuntimeError(f"{label} is not a regular directory: {current}")
    return candidate


def validate_existing_project_path(raw: Path | str, *, label: str = "project path") -> Path:
    candidate = validate_unredirected_path(raw, label=label)
    if not candidate.exists():
        raise RuntimeError(f"Path does not exist: {candidate}")
    if not candidate.is_dir():
        raise RuntimeError(f"Path is not a directory: {candidate}")
    return candidate


def nearest_governed_parent(raw: Path | str) -> Path | None:
    """Find a governed ancestor without implying that its policy owns the child."""

    candidate = Path(raw).expanduser().resolve(strict=False)
    current = candidate if candidate.is_dir() else candidate.parent
    machine_home = Path(os.environ.get("TENETORA_HOME") or (Path.home() / ".tenetora")).expanduser().resolve(strict=False)
    for parent in current.parents:
        harness = parent / ".tenetora"
        if harness.is_dir() and harness.resolve(strict=False) != machine_home:
            return parent
    return None


def harness_missing_message(raw: Path | str) -> str:
    """Explain which project is missing governance and how nested repos can recover."""

    project = Path(raw).expanduser().resolve(strict=False)
    parent = nearest_governed_parent(project)
    if parent is not None:
        return (
            f".tenetora is missing for project {project}. A governed parent was found at {parent}; "
            "an independent nested repository does not inherit that parent's rules. "
            f"Initialize this repository explicitly with --path {shlex.quote(str(project))}; "
            "keep parent gitlink and cross-repository integration checks at the parent governance root."
        )
    if os.environ.get("TENETORA_LANG", "").strip().lower() in {"zh", "zh-cn", "zh-hans", "chinese", "中文"}:
        return (
            f".tenetora 缺失（项目：{project}）。请在 AI 对话中输入“使用 Tenetora 初始化当前项目”，"
            "随后让 Tenetora 以该项目路径完成初始化。"
        )
    return (
        f".tenetora is missing for project {project}. In your AI conversation, ask: "
        "Use Tenetora to initialize this project at the displayed path."
    )


def validate_unredirected_file_path(raw: Path | str, *, label: str = "file path") -> Path:
    """Return a file path whose parent chain and final entry cannot redirect."""

    candidate = _lexical_absolute_path(raw, label=label)
    if candidate == candidate.__class__(candidate.anchor):
        raise RuntimeError(f"{label} cannot be the filesystem root: {candidate}")
    validate_unredirected_path(candidate.parent, label=f"{label} parent")
    if is_redirected_path(candidate):
        raise RuntimeError(f"{label} is a symbolic link or junction: {candidate}")
    if candidate.exists() and not candidate.is_file():
        raise RuntimeError(f"{label} is not a regular file: {candidate}")
    return candidate


def validate_unredirected_replace_target(raw: Path | str, *, label: str = "replace target") -> Path:
    """Validate a destination that will be atomically replaced.

    The final entry may be a symlink or Windows reparse point because
    ``os.replace`` replaces that directory entry without following it. Its
    parent chain must still be an ordinary, non-redirected directory, and
    ordinary existing entries must be regular files.
    """

    candidate = _lexical_absolute_path(raw, label=label)
    if candidate == candidate.__class__(candidate.anchor):
        raise RuntimeError(f"{label} cannot be the filesystem root: {candidate}")
    validate_unredirected_path(candidate.parent, label=f"{label} parent")
    if is_redirected_path(candidate):
        return candidate
    if candidate.exists() and not candidate.is_file():
        raise RuntimeError(f"{label} is not a regular file or replaceable redirect: {candidate}")
    return candidate


def validate_unredirected_entry_path(raw: Path | str, *, label: str = "path") -> Path:
    """Validate an entry and its parents without requiring a file or directory."""

    candidate = _lexical_absolute_path(raw, label=label)
    if candidate == candidate.__class__(candidate.anchor):
        raise RuntimeError(f"{label} cannot be the filesystem root: {candidate}")
    validate_unredirected_path(candidate.parent, label=f"{label} parent")
    if is_redirected_path(candidate):
        raise RuntimeError(f"{label} is a symbolic link or junction: {candidate}")
    return candidate


def ensure_unredirected_directory(raw: Path | str, *, label: str) -> Path:
    """Create a directory one component at a time without following redirects."""

    candidate = _lexical_absolute_path(raw, label=label)
    if candidate == candidate.__class__(candidate.anchor):
        raise RuntimeError(f"{label} cannot be the filesystem root: {candidate}")
    current = candidate.__class__(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        redirected = is_redirected_path(current)
        exists = current.exists() or redirected
        if redirected and not is_allowed_system_redirect(current):
            raise RuntimeError(f"{label} contains a symbolic link or junction: {current}")
        if exists:
            if not current.is_dir():
                raise RuntimeError(f"{label} is not a regular directory: {current}")
            continue
        try:
            current.mkdir()
        except FileExistsError:
            pass
        except OSError as exc:
            raise RuntimeError(f"cannot create {label}: {current}: {exc}") from exc
        redirected = is_redirected_path(current)
        if redirected and not is_allowed_system_redirect(current):
            raise RuntimeError(f"{label} contains a symbolic link or junction: {current}")
        if not current.is_dir():
            raise RuntimeError(f"{label} is not a regular directory: {current}")
    return candidate


def _remove_tree_without_following(path: Path, *, label: str) -> None:
    try:
        entries = list(path.iterdir())
    except OSError as exc:
        raise RuntimeError(f"cannot inspect {label}: {path}: {exc}") from exc
    for entry in entries:
        if is_redirected_path(entry):
            try:
                if entry.is_symlink():
                    entry.unlink()
                elif getattr(entry, "is_junction", None) and entry.is_junction():
                    entry.rmdir()
                elif os.name == "nt" and entry.is_dir():
                    # Other Windows reparse points that present as directories
                    # must be removed as links, never traversed.
                    entry.rmdir()
                else:
                    entry.unlink()
            except OSError as exc:
                raise RuntimeError(f"cannot remove redirected {label} entry: {entry}: {exc}") from exc
        elif entry.is_dir():
            _remove_tree_without_following(entry, label=label)
            try:
                entry.rmdir()
            except OSError as exc:
                raise RuntimeError(f"cannot remove {label} directory: {entry}: {exc}") from exc
        elif entry.is_file():
            try:
                entry.unlink()
            except OSError as exc:
                raise RuntimeError(f"cannot remove {label} file: {entry}: {exc}") from exc
        else:
            raise RuntimeError(f"{label} contains a non-regular entry: {entry}")


def remove_unredirected_entry(raw: Path | str, *, label: str) -> None:
    """Remove an entry without recursively traversing a symlink or reparse point."""

    candidate = _lexical_absolute_path(raw, label=label)
    if candidate == candidate.__class__(candidate.anchor):
        raise RuntimeError(f"{label} cannot be the filesystem root: {candidate}")
    validate_unredirected_path(candidate.parent, label=f"{label} parent")
    redirected = is_redirected_path(candidate)
    if not (candidate.exists() or redirected):
        return
    if redirected:
        try:
            if candidate.is_symlink():
                candidate.unlink()
            elif getattr(candidate, "is_junction", None) and candidate.is_junction():
                candidate.rmdir()
            elif os.name == "nt" and candidate.is_dir():
                candidate.rmdir()
            else:
                candidate.unlink()
        except OSError as exc:
            raise RuntimeError(f"cannot remove redirected {label}: {candidate}: {exc}") from exc
        return
    if candidate.is_file():
        candidate.unlink()
        return
    if not candidate.is_dir():
        raise RuntimeError(f"{label} is not a regular file or directory: {candidate}")
    _remove_tree_without_following(candidate, label=label)
    candidate.rmdir()


def create_redirected_entry(
    destination: Path,
    target: Path | str,
    *,
    target_is_directory: bool,
    prefer_junction: bool = False,
    label: str = "redirected entry",
) -> None:
    """Create a symlink or Windows junction without following the target."""

    ensure_unredirected_directory(destination.parent, label=f"{label} parent")
    if destination.exists() or is_redirected_path(destination):
        raise RuntimeError(f"{label} destination already exists: {destination}")
    target_text = os.fsdecode(os.fspath(target))

    if prefer_junction:
        if os.name != "nt" or not target_is_directory:
            raise RuntimeError(f"invalid junction request for {label}: {destination}")
        try:
            subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(destination), target_text],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(f"cannot create {label} junction: {destination}") from exc
        return

    try:
        destination.symlink_to(target_text, target_is_directory=target_is_directory)
    except OSError as exc:
        raise RuntimeError(f"cannot create {label} symbolic link: {destination}") from exc


def copy_redirected_entry(source: Path, destination: Path, *, label: str = "redirected entry") -> None:
    """Copy an already-validated redirect without materializing its target tree."""

    prefer_junction = os.name == "nt" and not source.is_symlink()
    target_is_directory = source.is_dir() or prefer_junction
    target = None
    try:
        target = os.fsdecode(os.readlink(source))
    except OSError:
        pass
    if prefer_junction:
        try:
            target = str(source.resolve(strict=True))
        except OSError:
            if target is None:
                raise RuntimeError(f"cannot resolve {label}: {source}")
    elif target is None:
        try:
            target = str(source.resolve(strict=True))
        except OSError as exc:
            raise RuntimeError(f"cannot resolve {label}: {source}") from exc

    if os.name == "nt" and not source.is_symlink() and not prefer_junction:
        raise RuntimeError(f"unsupported Windows reparse {label}: {source}")
    create_redirected_entry(
        destination,
        target,
        target_is_directory=target_is_directory,
        prefer_junction=prefer_junction,
        label=label,
    )
