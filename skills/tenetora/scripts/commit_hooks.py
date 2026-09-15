#!/usr/bin/env python3
"""Inspect and install Tenetora commit hooks for a project."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
CLI_DIR = SCRIPT_DIR.parent / "cli"
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

import alignment_state
from harness_io import atomic_write_text
from tenetora.commit_hook_state import (
    STATE_SCHEMA_VERSION,
    command_from_state,
    managed_cli_command,
    portable_cli_locator,
    state_has_current_locator,
    valid_state_shape,
)
from tenetora.brand import machine_home
from tenetora.path_security import (
    ensure_unredirected_directory,
    harness_missing_message,
    is_redirected_path,
    validate_existing_project_path,
    validate_unredirected_entry_path,
    validate_unredirected_file_path,
    validate_unredirected_path,
)

STATE_REL = ".tenetora/state/commit-hooks.json"
HOOK_NAMES = ("pre-commit", "commit-msg")
HOOK_MARKER = "# Tenetora managed hook"
LEGACY_HOOK_MARKER = "# Agent Harness managed hook"
ORIGINAL_HOOK_MARKER = "# Tenetora original hook: "
LEGACY_ORIGINAL_HOOK_MARKER = "# Agent Harness original hook: "
LOCAL_IGNORE_ENTRY = "state/commit-hooks.json"


class HookDecisionRequired(RuntimeError):
    """Raised when an interactive decision is required but unavailable."""


def has_managed_marker(text: str) -> bool:
    return any(line.strip() in {HOOK_MARKER, LEGACY_HOOK_MARKER} for line in text.splitlines())


def existing_project_path(raw: str) -> Path:
    try:
        return validate_existing_project_path(raw)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(
        prog="tenetora hooks",
        description="Inspect or install project-local commit governance hooks.",
        add_help=False,
    )
    command_parser.add_argument("--help", action="help", help="Show this help message and exit.")
    command_parser.add_argument("-p", "--path", default=".", type=existing_project_path, metavar="<project-dir>")
    command_parser.add_argument(
        "--action",
        choices=("status", "ensure", "install", "defer", "decline"),
        default=None,
        help="status inspect; ensure ask if needed; install/defer/decline record an explicit decision.",
    )
    action_group = command_parser.add_mutually_exclusive_group()
    action_group.add_argument("--status", dest="action_flag", action="store_const", const="status", help="Show the current local hook decision and installation status.")
    action_group.add_argument("--ensure", dest="action_flag", action="store_const", const="ensure", help="Ask for a decision when hooks are not installed and reminders are enabled.")
    action_group.add_argument("--install", dest="action_flag", action="store_const", const="install", help="Install project-local pre-commit and commit-msg hooks now.")
    action_group.add_argument("--defer", dest="action_flag", action="store_const", const="defer", help="Do not install now and ask again on a later init or update.")
    action_group.add_argument("--decline", dest="action_flag", action="store_const", const="decline", help="Do not install and stop automatic reminders on this machine.")
    command_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    command_parser.add_argument(
        "--cli-command",
        default=None,
        help="Executable used by installed hooks. Defaults to tenetora when available.",
    )
    return command_parser


def read_state(root: Path) -> dict[str, Any] | None:
    try:
        path = validate_unredirected_file_path(root / STATE_REL, label="commit hook state")
    except RuntimeError:
        return None
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def state_path(root: Path) -> Path:
    return root / STATE_REL


def state_file_needs_migration(root: Path) -> bool:
    """Detect missing, legacy, malformed, and unsafe state without accepting it."""

    try:
        path = validate_unredirected_file_path(state_path(root), label="commit hook state")
    except RuntimeError:
        return True
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    return not valid_state_shape(payload)


def backup_state_before_migration(root: Path) -> str | None:
    """Keep invalid project state outside the project before replacing it."""

    source = validate_unredirected_file_path(state_path(root), label="commit hook state")
    if not source.is_file():
        return None
    digest = hashlib.sha256(str(root.resolve(strict=False)).encode("utf-8")).hexdigest()[:16]
    backup_root = validate_unredirected_path(
        machine_home() / "backups" / "commit-hooks-state" / digest,
        label="commit hook state backup",
    )
    ensure_unredirected_directory(backup_root, label="commit hook state backup directory")
    backup = validate_unredirected_file_path(
        backup_root / "commit-hooks.json",
        label="commit hook state backup file",
    )
    if backup.exists():
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = validate_unredirected_file_path(
            backup_root / f"commit-hooks-{stamp}.json",
            label="commit hook state backup file",
        )
        suffix = 1
        while backup.exists():
            backup = validate_unredirected_file_path(
                backup_root / f"commit-hooks-{stamp}-{suffix}.json",
                label="commit hook state backup file",
            )
            suffix += 1
    shutil.copy2(source, backup)
    return str(backup)


def migration_decision(state: dict[str, Any] | None, managed_seen: bool) -> str:
    decision = state.get("decision") if isinstance(state, dict) else None
    if decision in {"install", "defer", "decline", "pending"}:
        if managed_seen:
            return "install"
        return str(decision)
    return "install" if managed_seen else "pending"


def git_hooks_dir(root: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--git-path", "hooks"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("The project is not inside a Git worktree.")
    raw = Path(result.stdout.strip())
    candidate = raw if raw.is_absolute() else root / raw
    try:
        return validate_unredirected_path(candidate, label="Git hooks directory")
    except RuntimeError as exc:
        raise RuntimeError(f"Git hooks directory is unsafe: {exc}") from exc


def is_git_worktree(root: Path) -> bool:
    try:
        git_hooks_dir(root)
    except (RuntimeError, OSError):
        return False
    return True


def hook_configured(root: Path, hook_name: str) -> bool:
    config_paths = (
        root / ".pre-commit-config.yaml",
        root / ".pre-commit-config.tenetora.yaml",
        root / ".pre-commit-config.agent-harness.yaml",
    )
    for path in config_paths:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        canonical = "tenetora guard --action commit" in text
        if hook_name == "pre-commit" and canonical and "tenetora-commit-guard" in text:
            return True
        if hook_name == "commit-msg" and canonical and "tenetora-commit-message" in text:
            return True
    return False


def hook_status(
    hooks_dir: Path,
    hook_name: str,
    state: dict[str, Any] | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    path = hooks_dir / hook_name
    try:
        safe_path = validate_unredirected_file_path(path, label="Git hook")
    except RuntimeError:
        safe_path = None
    exists = bool(safe_path and safe_path.is_file())
    text = safe_path.read_text(encoding="utf-8", errors="ignore") if exists and safe_path else ""
    managed = has_managed_marker(text)
    cli_command = command_from_state(state, text) if managed else None
    configured = bool(
        managed
        and cli_command is not None
        and hook_matches_current_template(hooks_dir, hook_name, text, cli_command, root=root)
    )
    return {
        "path": str(path),
        "exists": exists,
        "managed": managed,
        "configured": configured,
        "active": False,
    }


def status_payload(root: Path) -> dict[str, Any]:
    state = read_state(root)
    hooks: dict[str, dict[str, Any]] = {}
    git_available = is_git_worktree(root)
    if git_available:
        hooks_dir = git_hooks_dir(root)
        for hook_name in HOOK_NAMES:
            item = hook_status(hooks_dir, hook_name, state, root=root)
            if not item["managed"]:
                item["configured"] = hook_configured(root, hook_name)
            text = Path(str(item["path"])).read_text(encoding="utf-8", errors="ignore") if item["exists"] else ""
            item["active"] = bool(
                item["exists"]
                and (
                    (item["managed"] and item["configured"])
                    or (not item["managed"] and item["configured"] and "pre-commit" in text)
                )
            )
            hooks[hook_name] = item
    decision = str(state.get("decision")) if state else "pending"
    installed = all(item.get("active") for item in hooks.values()) if hooks else False
    return {
        "version": 1,
        "path": str(root),
        "git": {"available": git_available, "hooks_dir": str(git_hooks_dir(root)) if git_available else None},
        "decision": decision,
        "installed": installed,
        "state": state,
        "hooks": hooks,
        "recommendations": recommendations(decision, installed, git_available),
    }


def recommendations(decision: str, installed: bool, git_available: bool) -> list[str]:
    chinese = os.environ.get("TENETORA_LANG", "").strip().lower() in {"zh", "zh-cn", "zh-hans", "chinese", "中文"}
    if not git_available:
        return ["项目不是 Git 工作树，无法安装项目级提交 Hook。" if chinese else "The project is not a Git worktree, so project commit hooks cannot be installed."]
    if installed:
        return ["Tenetora 提交 Hook 已安装；可用 tenetora hooks --status 检查。" if chinese else "Tenetora commit hooks are installed; inspect them with tenetora hooks --status."]
    if decision == "decline":
        return ["已选择不再提醒；需要时可主动运行 tenetora hooks --install。" if chinese else "Future reminders are disabled; run tenetora hooks --install when needed."]
    if decision == "defer":
        return ["已延后；下次初始化或更新时会再次询问，也可运行 tenetora hooks --install。" if chinese else "Installation is deferred; the next init or update will ask again, or run tenetora hooks --install."]
    if decision == "install":
        return ["已记录安装选择，但本地 Hook 不完整；运行 tenetora hooks --install 修复。" if chinese else "Installation is selected, but local hooks are incomplete; repair them with tenetora hooks --install."]
    return (
        [
            "请选择：立即安装、下次再说、不需要（不再提醒）。",
            "立即安装：tenetora hooks --install",
            "下次再说：tenetora hooks --defer",
            "不需要：tenetora hooks --decline",
        ]
        if chinese
        else [
            "Choose install now, defer, or decline future reminders.",
            "Install now: tenetora hooks --install",
            "Defer: tenetora hooks --defer",
            "Decline: tenetora hooks --decline",
        ]
    )


def ensure_local_state_ignored(root: Path) -> None:
    path = validate_unredirected_file_path(
        root / ".tenetora" / ".gitignore",
        label="Tenetora project gitignore",
    )
    text = path.read_text(encoding="utf-8", errors="ignore") if path.is_file() else ""
    if any(line.strip().rstrip("/") == LOCAL_IGNORE_ENTRY for line in text.splitlines()):
        return
    if text and not text.endswith("\n"):
        text += "\n"
    atomic_write_text(path, f"{text}{LOCAL_IGNORE_ENTRY}\n")


def write_state(
    root: Path,
    decision: str,
    hooks: dict[str, Any] | None = None,
    cli_command: str | None = None,
) -> None:
    ensure_local_state_ignored(root)
    path = state_path(root)
    path = validate_unredirected_file_path(path, label="commit hook state")
    ensure_unredirected_directory(path.parent, label="commit hook state directory")
    payload = {
        "version": STATE_SCHEMA_VERSION,
        "decision": decision,
        "updated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "scope": "project",
        "install_strategy": "managed-git-hooks",
    }
    if decision == "install" or hooks is not None or cli_command is not None:
        payload["cli_locator"] = portable_cli_locator(cli_command)
    if hooks is not None:
        payload["hooks"] = {
            name: {
                "managed": bool(item.get("managed")),
                "backup_preserved": bool(item.get("backup")),
            }
            for name, item in hooks.items()
            if isinstance(item, dict)
        }
    with alignment_state.state_lock(path):
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def runner_lines(root: Path, cli_command: str | None) -> list[str]:
    command = cli_command or managed_cli_command()
    return [f"  {shlex.quote(command)} \"$@\""]


def cli_command_available(cli_command: str | None) -> bool:
    if cli_command is None:
        return True
    if "/" not in cli_command and "\\" not in cli_command:
        return shutil.which(cli_command) is not None
    return Path(cli_command).is_file()


def hook_matches_current_template(
    hooks_dir: Path,
    hook_name: str,
    text: str,
    cli_command: str | None = None,
    *,
    root: Path | None = None,
) -> bool:
    backup = original_hook_from_wrapper(text)
    project_root = root or (hooks_dir.parent.parent if hooks_dir.name == "hooks" else hooks_dir)
    return text == hook_script(project_root, hook_name, backup, cli_command)


def hook_script(root: Path, hook_name: str, backup: Path | None, cli_command: str | None) -> str:
    lines = [
        "#!/bin/sh",
        "set -u",
        HOOK_MARKER,
        "# This local hook is managed by Tenetora and is not tracked by Git.",
        f"{ORIGINAL_HOOK_MARKER}{backup}" if backup is not None else f"{ORIGINAL_HOOK_MARKER}",
        "PROJECT_ROOT=$(git rev-parse --show-toplevel)",
        "run_tenetora() {",
        *runner_lines(root, cli_command),
        "}",
    ]
    if hook_name == "pre-commit":
        lines.append(
            "run_tenetora guard --action commit --path \"$PROJECT_ROOT\" "
            "--git-path \"$PROJECT_ROOT\" --operation commit || exit $?"
        )
    else:
        lines.append(
            "run_tenetora guard --action commit --path \"$PROJECT_ROOT\" "
            "--git-path \"$PROJECT_ROOT\" --operation commit "
            "--commit-message-file \"$1\" --require-message || exit $?"
        )
    if backup is not None:
        lines.extend([f"if [ -x {shlex.quote(str(backup))} ]; then", f"  exec {shlex.quote(str(backup))} \"$@\"", "fi"])
    lines.append("exit 0")
    return "\n".join(lines) + "\n"


def original_hook_from_wrapper(text: str) -> Path | None:
    for line in text.splitlines():
        for marker in (ORIGINAL_HOOK_MARKER, LEGACY_ORIGINAL_HOOK_MARKER):
            if line.startswith(marker):
                raw = line.removeprefix(marker).strip()
                return Path(raw) if raw else None
    return None


def next_backup_path(hooks_dir: Path, hook_name: str) -> Path:
    preferred = hooks_dir / f"{hook_name}.tenetora-original"
    if not preferred.exists():
        return preferred
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = hooks_dir / f"{hook_name}.tenetora-original-{timestamp}"
    suffix = 1
    while candidate.exists():
        candidate = hooks_dir / f"{hook_name}.tenetora-original-{timestamp}-{suffix}"
        suffix += 1
    return candidate


def next_managed_wrapper_backup_path(hooks_dir: Path, hook_name: str) -> Path:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = hooks_dir / f"{hook_name}.tenetora-managed-backup-{timestamp}"
    suffix = 1
    while candidate.exists():
        candidate = hooks_dir / f"{hook_name}.tenetora-managed-backup-{timestamp}-{suffix}"
        suffix += 1
    return candidate


def install_one_hook(
    root: Path,
    hooks_dir: Path,
    hook_name: str,
    cli_command: str | None,
    *,
    backup_stale_managed: bool = False,
) -> dict[str, Any]:
    path = hooks_dir / hook_name
    backup: Path | None = None
    managed_backup: Path | None = None
    if is_redirected_path(path):
        raise RuntimeError(f"Git hook path is a symbolic link or junction: {path}")
    if path.is_file():
        text = path.read_text(encoding="utf-8", errors="ignore")
        if has_managed_marker(text):
            backup = original_hook_from_wrapper(text)
            if backup_stale_managed and not hook_matches_current_template(
                hooks_dir, hook_name, text, cli_command, root=root
            ):
                managed_backup = validate_unredirected_file_path(
                    next_managed_wrapper_backup_path(hooks_dir, hook_name),
                    label="managed Git hook backup",
                )
                ensure_unredirected_directory(
                    managed_backup.parent, label="managed Git hook backup directory"
                )
                shutil.copy2(path, managed_backup)
                managed_backup.chmod(0o600)
        else:
            backup = validate_unredirected_entry_path(
                next_backup_path(hooks_dir, hook_name), label="original Git hook backup"
            )
            path.replace(backup)
    content = hook_script(root, hook_name, backup, cli_command)
    ensure_unredirected_directory(hooks_dir, label="Git hooks directory")
    validate_unredirected_file_path(path, label="Git hook")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=hooks_dir, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.chmod(0o755)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "path": str(path),
        "managed": True,
        "backup": str(backup) if backup else None,
        "managed_backup": str(managed_backup) if managed_backup else None,
    }


@contextmanager
def hooks_operation_lock(root: Path):
    """Serialize hook-file installation and backup decisions per project."""

    path = root / ".tenetora/state/commit-hooks.install.lock"
    with alignment_state.state_lock(path):
        yield


def managed_target_sha256(root: Path) -> dict[str, str]:
    """Capture managed hook/state digests while the per-project lock is held."""

    targets = [state_path(root), root / ".tenetora" / ".gitignore"]
    if is_git_worktree(root):
        hooks_dir = git_hooks_dir(root)
        targets.extend(hooks_dir / name for name in HOOK_NAMES)
    result: dict[str, str] = {}
    for path in targets:
        key = str(path.absolute())
        try:
            safe_path = validate_unredirected_file_path(path, label="managed commit hook target")
        except RuntimeError:
            result[key] = "unsafe"
            continue
        result[key] = hashlib.sha256(safe_path.read_bytes()).hexdigest() if safe_path.is_file() else ""
    return result


def verify_expected_target_sha256(root: Path, expected: dict[str, str] | None) -> None:
    if not expected:
        return
    actual = managed_target_sha256(root)
    conflicts = [target for target, digest in expected.items() if actual.get(target, "") != digest]
    if conflicts:
        raise RuntimeError(
            "managed commit hooks changed after the upgrade snapshot; refusing to overwrite concurrent edits"
        )


def _install_hooks_unlocked(root: Path, cli_command: str | None = None) -> dict[str, Any]:
    if not (root / ".tenetora").is_dir():
        raise RuntimeError(harness_missing_message(root))
    if cli_command is not None and ("/" in cli_command or "\\" in cli_command) and not cli_command_available(cli_command):
        raise RuntimeError("explicit Tenetora CLI path is unavailable; refusing to install a dead Git hook")
    hooks_dir = git_hooks_dir(root)
    ensure_unredirected_directory(hooks_dir, label="Git hooks directory")
    installed: dict[str, Any] = {}
    for hook_name in HOOK_NAMES:
        installed[hook_name] = install_one_hook(root, hooks_dir, hook_name, cli_command)
    write_state(root, "install", installed, cli_command)
    return status_payload(root)


def install_hooks(root: Path, cli_command: str | None = None) -> dict[str, Any]:
    with hooks_operation_lock(root):
        return _install_hooks_unlocked(root, cli_command)


def _converge_managed_hooks_unlocked(
    root: Path,
    cli_command: str | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Rewrite stale managed wrappers without creating hooks the user never selected."""

    if not (root / ".tenetora").is_dir():
        return {"status": "not-applicable", "updated": [], "preserved": []}
    state = read_state(root)
    state_needs_migration = state_file_needs_migration(root)
    state_backup: str | None = None
    if not is_git_worktree(root):
        if state_needs_migration and not dry_run:
            state_backup = backup_state_before_migration(root)
            decision = migration_decision(state, managed_seen=False)
            write_state(root, decision, None, cli_command if decision == "install" else None)
            return {
                "status": "updated",
                "updated": [],
                "preserved": [],
                "state_migration": True,
                "state_backup": state_backup,
            }
        return {"status": "not-applicable", "updated": [], "preserved": []}
    hooks_dir = git_hooks_dir(root)
    updated: list[str] = []
    preserved: list[str] = []
    managed_seen = False
    for hook_name in HOOK_NAMES:
        path = hooks_dir / hook_name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if not has_managed_marker(text):
            preserved.append(hook_name)
            continue
        managed_seen = True
        if hook_matches_current_template(hooks_dir, hook_name, text, cli_command, root=root):
            continue
        updated.append(hook_name)
        if not dry_run and cli_command_available(cli_command):
            install_one_hook(
                root,
                hooks_dir,
                hook_name,
                cli_command,
                backup_stale_managed=True,
            )
    needs_hook_state_migration = bool(
        managed_seen
        and isinstance(state, dict)
        and state.get("decision") == "install"
        and not state_has_current_locator(state, cli_command)
    )
    needs_write = bool(updated or needs_hook_state_migration or state_needs_migration)
    if (updated or needs_hook_state_migration) and not dry_run and not cli_command_available(cli_command):
        return {
            "status": "deferred-cli-missing",
            "updated": updated,
            "preserved": preserved,
            "reason": f"canonical Tenetora CLI is not ready: {cli_command}",
            "state_migration": state_needs_migration or needs_hook_state_migration,
        }
    if needs_write and not dry_run:
        if state_needs_migration:
            state_backup = backup_state_before_migration(root)
        decision = (
            "install"
            if managed_seen and (updated or needs_hook_state_migration or state_needs_migration)
            else migration_decision(state, managed_seen=False)
        )
        hooks_payload = status_payload(root).get("hooks") if decision == "install" and managed_seen else None
        write_state(root, decision, hooks_payload, cli_command if decision == "install" else None)
    return {
        "status": (
            "would-update"
            if dry_run and needs_write
            else "updated"
            if needs_write
            else "current"
        ),
        "updated": updated,
        "preserved": preserved,
        "state_migration": state_needs_migration or needs_hook_state_migration,
        "state_backup": state_backup,
    }


def converge_managed_hooks(
    root: Path,
    cli_command: str | None = None,
    *,
    dry_run: bool = False,
    expected_sha256: dict[str, str] | None = None,
) -> dict[str, Any]:
    with hooks_operation_lock(root):
        verify_expected_target_sha256(root, expected_sha256)
        result = _converge_managed_hooks_unlocked(root, cli_command, dry_run=dry_run)
        result["target_sha256"] = managed_target_sha256(root)
        return result


def prompt_decision() -> str:
    chinese = os.environ.get("TENETORA_LANG", "").strip().lower() in {"zh", "zh-cn", "zh-hans", "chinese", "中文"}
    if chinese:
        print("检测到项目尚未决定是否安装 Tenetora 提交 Hook。")
        print("1. 立即安装：安装 pre-commit 和 commit-msg 本地 Hook。")
        print("2. 下次再说：本次不安装，下次初始化或更新时再次询问。")
        print("3. 不需要：不安装且不再提醒；以后仍可主动执行 tenetora hooks --install。")
    else:
        print("The project has not decided whether to install Tenetora commit hooks.")
        print("1. Install now: install local pre-commit and commit-msg hooks.")
        print("2. Defer: ask again during the next init or update.")
        print("3. Decline: do not install or remind again; tenetora hooks --install remains available.")
    while True:
        try:
            answer = input("请选择 [1/2/3]: " if chinese else "Choose [1/2/3]: ").strip().lower()
        except EOFError:
            return ""
        decision = {"1": "install", "2": "defer", "3": "decline"}.get(answer, "")
        if decision:
            return decision
        print("无效选择，请输入 1、2 或 3。" if chinese else "Invalid choice; enter 1, 2, or 3.")


def resolve_decision(root: Path, requested: str, interactive: bool) -> str:
    state = read_state(root)
    if requested == "ask":
        current = str(state.get("decision")) if state else "pending"
        if state and state.get("decision") == "decline":
            return "decline"
        if state and state.get("decision") == "install":
            return "install"
        if not interactive:
            return current if current in {"defer", "pending"} else "pending"
        decision = prompt_decision()
        if not decision:
            return "pending"
        return decision
    return requested


def ensure_decision(root: Path, requested: str, write: bool, interactive: bool, cli_command: str | None = None) -> dict[str, Any]:
    current_status = status_payload(root)
    if requested == "ask" and current_status["installed"]:
        if write:
            converge_managed_hooks(root, cli_command=cli_command, dry_run=False)
            write_state(root, "install", current_status["hooks"], cli_command)
        return status_payload(root)
    decision = resolve_decision(root, requested, interactive)
    if not write:
        return {"decision": decision, "status": "would-apply"}
    if decision == "install":
        return install_hooks(root, cli_command)
    write_state(root, decision)
    return status_payload(root)


def render_text(payload: dict[str, Any]) -> None:
    chinese = os.environ.get("TENETORA_LANG", "").strip().lower() in {"zh", "zh-cn", "zh-hans", "chinese", "中文"}
    print("Tenetora 提交 Hook" if chinese else "Tenetora Commit Hooks")
    print(f"{'项目' if chinese else 'Project'}: {payload.get('path')}")
    print(f"{'决策' if chinese else 'Decision'}: {payload.get('decision')}")
    print(
        f"已安装: {'是' if payload.get('installed') else '否'}"
        if chinese
        else f"Installed: {'yes' if payload.get('installed') else 'no'}"
    )
    for name, item in payload.get("hooks", {}).items():
        print(
            f"- {name}: "
            + (
                f"{'已激活' if item.get('active') else '缺失'}（受管={'是' if item.get('managed') else '否'}，已配置={'是' if item.get('configured') else '否'}）"
                if chinese
                else f"{'active' if item.get('active') else 'missing'} (managed={'yes' if item.get('managed') else 'no'}, configured={'yes' if item.get('configured') else 'no'})"
            )
        )
    for recommendation in payload.get("recommendations", []):
        print(f"{'下一步' if chinese else 'Next'}: {recommendation}")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = args.path
    action = args.action_flag or args.action or "status"
    try:
        if action == "status":
            payload = status_payload(root)
        elif action in {"ensure", "install", "defer", "decline"}:
            requested = "ask" if action == "ensure" else action
            payload = ensure_decision(root, requested, write=True, interactive=sys.stdin.isatty(), cli_command=args.cli_command)
        else:
            raise RuntimeError(f"Unsupported hook action: {action}")
    except (HookDecisionRequired, RuntimeError, OSError) as exc:
        payload = {"version": 1, "path": str(root), "status": "error", "message": str(exc)}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(payload["message"], file=sys.stderr)
        return 2
    payload.setdefault("status", "pass")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        render_text(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
