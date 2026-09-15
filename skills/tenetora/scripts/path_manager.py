#!/usr/bin/env python3
"""Idempotently manage the user PATH entry for the stable Tenetora launcher."""

from __future__ import annotations

import os
import hashlib
import shlex
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
CLI_DIR = SCRIPT_DIR.parent / "cli"
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from tenetora.path_security import validate_unredirected_file_path  # noqa: E402


START_MARKER = "# >>> tenetora managed PATH >>>"
END_MARKER = "# <<< tenetora managed PATH <<<"


class PathManagerError(RuntimeError):
    """Raised when PATH ownership cannot be proven or updated safely."""


def profile_kind(shell: str) -> str:
    name = Path(shell or "").name.lower()
    if name == "fish":
        return "fish"
    if name in {"bash", "sh"}:
        return "bash"
    return "zsh"


def profile_path(home: Path, kind: str) -> Path:
    if kind == "fish":
        return home / ".config" / "fish" / "config.fish"
    if kind == "bash":
        return home / ".bashrc"
    return home / ".zshrc"


def managed_block(bin_dir: Path, kind: str) -> str:
    quoted = shlex.quote(str(bin_dir.expanduser().absolute()))
    command = f"set -gx PATH {quoted} $PATH" if kind == "fish" else f"export PATH={quoted}:\"$PATH\""
    return f"{START_MARKER}\n{command}\n{END_MARKER}"


def replace_managed_block(content: str, block: str) -> tuple[str, str]:
    start_count = content.count(START_MARKER)
    end_count = content.count(END_MARKER)
    if start_count != end_count or start_count > 1:
        raise PathManagerError("shell profile contains duplicate or damaged Tenetora PATH markers")
    start = content.find(START_MARKER)
    end = content.find(END_MARKER)
    if (start < 0) != (end < 0) or (start >= 0 and end < start):
        raise PathManagerError("shell profile contains a damaged Tenetora PATH marker")
    if start >= 0:
        end += len(END_MARKER)
        updated = content[:start].rstrip() + "\n\n" + block + content[end:]
        return updated.rstrip() + "\n", "updated"
    prefix = content.rstrip()
    updated = (prefix + "\n\n" if prefix else "") + block + "\n"
    return updated, "added"


def _assert_profile_safe(path: Path) -> None:
    try:
        path = validate_unredirected_file_path(path, label="shell profile")
    except RuntimeError as error:
        raise PathManagerError(str(error)) from error
    if path.exists() and os.name != "nt" and hasattr(os, "getuid"):
        info = path.stat()
        if info.st_uid != os.getuid():
            raise PathManagerError("shell profile is not owned by the current user")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PathManagerError("shell profile is writable by group or others")


def configure_posix_path(home: Path, bin_dir: Path, shell: str) -> dict[str, Any]:
    kind = profile_kind(shell)
    path = profile_path(home.expanduser().absolute(), kind)
    _assert_profile_safe(path)
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    updated, action = replace_managed_block(current, managed_block(bin_dir, kind))
    if updated == current:
        return {"status": "current", "profile": str(path), "shell": kind, "changed": False}
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"status": action, "profile": str(path), "shell": kind, "changed": True}


def configure_windows_path(bin_dir: Path) -> dict[str, Any]:
    if os.name != "nt":
        raise PathManagerError("Windows PATH management is only available on Windows")
    try:
        import winreg
    except ImportError as exc:
        raise PathManagerError("Windows registry support is unavailable") from exc
    value = str(bin_dir.expanduser().absolute())
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
        try:
            current, value_type = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current, value_type = "", winreg.REG_EXPAND_SZ
        entries = [item for item in str(current).split(";") if item]
        normalized = {os.path.normcase(os.path.normpath(item)) for item in entries}
        if os.path.normcase(os.path.normpath(value)) in normalized:
            return {"status": "current", "profile": "HKCU\\Environment\\Path", "shell": "windows", "changed": False}
        winreg.SetValueEx(key, "Path", 0, value_type, ";".join([value, *entries]))
    broadcast = False
    try:
        import ctypes

        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF,
            0x001A,
            0,
            "Environment",
            0x0002,
            5000,
            ctypes.byref(result),
        )
        broadcast = True
    except (AttributeError, ImportError, OSError):
        pass
    return {"status": "added", "profile": "HKCU\\Environment\\Path", "shell": "windows", "changed": True, "broadcast": broadcast}


def configure_user_path(bin_dir: Path, *, home: Path | None = None, shell: str | None = None) -> dict[str, Any]:
    if os.name == "nt":
        return configure_windows_path(bin_dir)
    return configure_posix_path(
        (home or Path.home()).expanduser().absolute(),
        bin_dir,
        shell or os.environ.get("SHELL", ""),
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def snapshot_user_path(*, home: Path | None = None, shell: str | None = None) -> dict[str, Any]:
    if os.name == "nt":
        try:
            import winreg
        except ImportError as exc:
            raise PathManagerError("Windows registry support is unavailable") from exc
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            try:
                current, value_type = winreg.QueryValueEx(key, "Path")
                exists = True
            except FileNotFoundError:
                current, value_type, exists = "", winreg.REG_EXPAND_SZ, False
        return {"kind": "windows", "exists": exists, "content": str(current), "value_type": value_type, "before_sha256": _digest(str(current))}
    selected_home = (home or Path.home()).expanduser().absolute()
    kind = profile_kind(shell or os.environ.get("SHELL", ""))
    path = profile_path(selected_home, kind)
    _assert_profile_safe(path)
    content = path.read_text(encoding="utf-8") if path.is_file() else ""
    mode = stat.S_IMODE(path.stat().st_mode) if path.is_file() else 0o600
    return {"kind": kind, "path": str(path), "exists": path.is_file(), "content": content, "mode": mode, "before_sha256": _digest(content)}


def checkpoint_user_path(snapshot: dict[str, Any]) -> dict[str, Any]:
    updated = dict(snapshot)
    if snapshot.get("kind") == "windows":
        current = snapshot_user_path()
    else:
        path = Path(str(snapshot["path"]))
        current_content = path.read_text(encoding="utf-8") if path.is_file() else ""
        current = {"before_sha256": _digest(current_content)}
    updated["after_sha256"] = current["before_sha256"]
    return updated


def restore_user_path(snapshot: dict[str, Any]) -> bool:
    after = str(snapshot.get("after_sha256") or "")
    if not after:
        return True
    if snapshot.get("kind") == "windows":
        current = snapshot_user_path()
        if current["before_sha256"] == snapshot.get("before_sha256"):
            return True
        if current["before_sha256"] != after:
            return False
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            if snapshot.get("exists"):
                winreg.SetValueEx(key, "Path", 0, int(snapshot["value_type"]), str(snapshot["content"]))
            else:
                try:
                    winreg.DeleteValue(key, "Path")
                except FileNotFoundError:
                    pass
        return True
    path = Path(str(snapshot["path"]))
    _assert_profile_safe(path)
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    if _digest(current) == snapshot.get("before_sha256"):
        return True
    if _digest(current) != after:
        return False
    if snapshot.get("exists"):
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(str(snapshot.get("content") or ""))
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(int(snapshot.get("mode") or 0o600))
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        path.unlink(missing_ok=True)
    return True
