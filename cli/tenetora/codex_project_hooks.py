"""Manage Codex project-level Tenetora hook fallback configuration."""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

try:
    from .file_lock import locked_file
    from .path_security import (
        ensure_unredirected_directory,
        validate_existing_project_path,
        validate_unredirected_file_path,
        validate_unredirected_path,
    )
except ImportError:  # Loaded by installer/init through spec_from_file_location.
    import importlib.util

    _file_lock_path = Path(__file__).with_name("file_lock.py")
    _file_lock_spec = importlib.util.spec_from_file_location(
        "tenetora_dynamic_file_lock",
        _file_lock_path,
    )
    if _file_lock_spec is None or _file_lock_spec.loader is None:
        raise ImportError("cannot load Tenetora file lock helper")
    _file_lock_module = importlib.util.module_from_spec(_file_lock_spec)
    _file_lock_spec.loader.exec_module(_file_lock_module)
    locked_file = _file_lock_module.locked_file
    _path_security_path = Path(__file__).with_name("path_security.py")
    _path_security_spec = importlib.util.spec_from_file_location(
        "tenetora_dynamic_path_security",
        _path_security_path,
    )
    if _path_security_spec is None or _path_security_spec.loader is None:
        raise ImportError("cannot load Tenetora path security helper")
    _path_security_module = importlib.util.module_from_spec(_path_security_spec)
    _path_security_spec.loader.exec_module(_path_security_module)
    ensure_unredirected_directory = _path_security_module.ensure_unredirected_directory
    validate_existing_project_path = _path_security_module.validate_existing_project_path
    validate_unredirected_file_path = _path_security_module.validate_unredirected_file_path
    validate_unredirected_path = _path_security_module.validate_unredirected_path


HOOK_NAMES = (
    "session-start",
    "user-prompt-submit",
    "pre-tool-commit",
    "subagent-start",
    "subagent-stop",
    "stop",
)
POSIX_RUNNER = "$HOME/.tenetora/runtime/hooks/run-hook"
WINDOWS_RUNNER = "%USERPROFILE%\\.tenetora\\runtime\\hooks\\run-hook.cmd"
LEGACY_POSIX_RUNNER = "$HOME/.agent-harness/runtime/hooks/run-hook"
LEGACY_WINDOWS_RUNNER = "%USERPROFILE%\\.agent-harness\\runtime\\hooks\\run-hook.cmd"


@dataclass(frozen=True)
class ProjectHookResult:
    state: str
    detail: str
    hooks_path: Path
    backup_path: Path | None = None
    ownership_path: Path | None = None


def materialized_project_hooks(template: dict[str, object]) -> dict[str, object]:
    payload = deepcopy(template)
    payload["description"] = "Tenetora project fallback hooks for Codex"
    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        raise ValueError("Tenetora Codex hook template has no hooks object")
    for event_name, groups in hooks.items():
        if not isinstance(groups, list):
            raise ValueError(f"Tenetora Codex hook event {event_name} is not a list")
        for group in groups:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(entries, list):
                raise ValueError(f"Tenetora Codex hook event {event_name} has no hook list")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError(f"Tenetora Codex hook event {event_name} has an invalid hook")
                command = entry.get("command")
                windows = entry.get("commandWindows")
                if not isinstance(command, str) or "${PLUGIN_ROOT}/hooks/run-hook" not in command:
                    raise ValueError(f"Tenetora Codex hook event {event_name} has an invalid Unix command")
                if not isinstance(windows, str) or "%PLUGIN_ROOT%\\hooks\\run-hook.cmd" not in windows:
                    raise ValueError(f"Tenetora Codex hook event {event_name} has an invalid Windows command")
                entry["command"] = "TENETORA_HOOK_PLATFORM=codex " + command.replace(
                    "${PLUGIN_ROOT}/hooks/run-hook", POSIX_RUNNER
                )
                entry["commandWindows"] = 'set "TENETORA_HOOK_PLATFORM=codex" && ' + windows.replace(
                    "%PLUGIN_ROOT%\\hooks\\run-hook.cmd", WINDOWS_RUNNER
                )
    return payload


def project_hook_is_managed(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    command = entry.get("command")
    windows = entry.get("commandWindows")
    if not isinstance(command, str) or not isinstance(windows, str):
        return False
    runner_pair = any(
        posix in command and windows_runner in windows
        for posix, windows_runner in (
            (POSIX_RUNNER, WINDOWS_RUNNER),
            (LEGACY_POSIX_RUNNER, LEGACY_WINDOWS_RUNNER),
        )
    )
    return runner_pair and any(mode in command for mode in HOOK_NAMES)


def _validated_hooks(payload: dict[str, object], label: str) -> dict[str, object]:
    hooks = payload.get("hooks")
    if hooks is None:
        return {}
    if not isinstance(hooks, dict):
        raise ValueError(f"{label} has a non-object hooks field")
    return hooks


def _without_managed_groups(groups: object, event_name: str) -> list[object]:
    if not isinstance(groups, list):
        raise ValueError(f"Codex hooks event {event_name} is not a list")
    preserved: list[object] = []
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError(f"Codex hooks event {event_name} contains a non-object group")
        entries = group.get("hooks")
        if not isinstance(entries, list):
            raise ValueError(f"Codex hooks event {event_name} group has no hook list")
        remaining = [deepcopy(entry) for entry in entries if not project_hook_is_managed(entry)]
        if remaining:
            copied = deepcopy(group)
            copied["hooks"] = remaining
            preserved.append(copied)
    return preserved


def merged_project_hooks(
    existing: dict[str, object],
    desired: dict[str, object],
) -> dict[str, object]:
    merged = deepcopy(existing)
    existing_hooks = _validated_hooks(existing, "Existing Codex hooks.json")
    desired_hooks = _validated_hooks(desired, "Tenetora Codex project hooks")
    hooks: dict[str, object] = {}
    for event_name, groups in existing_hooks.items():
        hooks[event_name] = _without_managed_groups(groups, event_name)
    for event_name, groups in desired_hooks.items():
        current = hooks.get(event_name, [])
        if not isinstance(current, list) or not isinstance(groups, list):
            raise ValueError(f"Codex hooks event {event_name} is not a list")
        hooks[event_name] = current + deepcopy(groups)
    if "description" not in merged and isinstance(desired.get("description"), str):
        merged["description"] = desired["description"]
    merged["hooks"] = hooks
    return merged


def _managed_snapshot(payload: dict[str, object]) -> list[dict[str, object]]:
    snapshot: list[dict[str, object]] = []
    hooks = _validated_hooks(payload, "Codex hooks.json")
    for event_name in sorted(hooks):
        groups = hooks[event_name]
        if not isinstance(groups, list):
            raise ValueError(f"Codex hooks event {event_name} is not a list")
        for group in groups:
            if not isinstance(group, dict):
                raise ValueError(f"Codex hooks event {event_name} contains a non-object group")
            entries = group.get("hooks")
            if not isinstance(entries, list):
                raise ValueError(f"Codex hooks event {event_name} group has no hook list")
            managed = [deepcopy(entry) for entry in entries if project_hook_is_managed(entry)]
            if managed:
                snapshot.append(
                    {
                        "event": event_name,
                        "matcher": deepcopy(group.get("matcher")),
                        "hooks": managed,
                    }
                )
    return snapshot


def managed_hook_fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        _managed_snapshot(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _project_id(project_root: Path) -> str:
    return hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()[:16]


def _harness_home(raw: Path | None) -> Path:
    if raw is not None:
        return validate_unredirected_path(raw, label="Tenetora home")
    canonical = os.environ.get("TENETORA_HOME")
    if canonical:
        return validate_unredirected_path(canonical, label="TENETORA_HOME")
    return validate_unredirected_path(Path.home() / ".tenetora", label="Tenetora home")


def project_state_path(project_root: Path, harness_home: Path | None = None) -> Path:
    root = validate_existing_project_path(project_root)
    return validate_unredirected_file_path(
        _harness_home(harness_home) / "state" / "projects" / _project_id(root) / "codex-hooks.json",
        label="Codex project hook state",
    )


@contextmanager
def project_hooks_lock(project_root: Path, harness_home: Path | None = None):
    """Serialize Codex fallback read/merge/write operations for one project."""

    path = project_state_path(project_root, harness_home)
    lock_path = path.with_name(".codex-hooks.lock")
    ensure_unredirected_directory(lock_path.parent, label="Codex project hook lock directory")
    with locked_file(lock_path):
        yield


def get_project_preference(project_root: Path, *, harness_home: Path | None = None) -> str | None:
    path = project_state_path(project_root, harness_home)
    if not path.is_file():
        return None
    try:
        payload = _read_json(path, "Codex project hook state")
    except (OSError, ValueError):
        return None
    preference = payload.get("preference")
    return str(preference) if preference in {"ask", "skills-only", "declined"} else None


def _set_project_preference_unlocked(
    project_root: Path,
    preference: str,
    *,
    harness_home: Path | None = None,
) -> Path:
    if preference not in {"ask", "skills-only", "declined"}:
        raise ValueError(f"Unsupported Codex project hook preference: {preference}")
    root = validate_existing_project_path(project_root)
    path = project_state_path(root, harness_home)
    payload = _read_json(path, "Codex project hook state") if path.is_file() else {
        "version": 1,
        "project_root": str(root),
    }
    payload["preference"] = preference
    _write_json_atomic(path, payload, 0o600)
    return path


def set_project_preference(
    project_root: Path,
    preference: str,
    *,
    harness_home: Path | None = None,
) -> Path:
    with project_hooks_lock(project_root, harness_home):
        return _set_project_preference_unlocked(project_root, preference, harness_home=harness_home)


def _read_json(path: Path, label: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} contains invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return payload


def _write_json_atomic(path: Path, payload: dict[str, object], mode: int | None = None) -> None:
    path = validate_unredirected_file_path(path, label="Codex project hook JSON")
    ensure_unredirected_directory(path.parent, label="Codex project hook JSON directory")
    target_mode = mode if mode is not None else (path.stat().st_mode & 0o777 if path.is_file() else 0o600)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        temporary.chmod(target_mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def project_fallback_owned_and_current(
    project_root: Path,
    *,
    harness_home: Path | None = None,
) -> bool:
    root = validate_existing_project_path(project_root)
    hooks_path = root / ".codex" / "hooks.json"
    ownership_path = project_state_path(root, harness_home)
    try:
        hooks_path = validate_unredirected_file_path(hooks_path, label="Codex hooks.json")
    except RuntimeError:
        return False
    if not hooks_path.is_file() or not ownership_path.is_file():
        return False
    try:
        ownership = _read_json(ownership_path, "Codex project fallback ownership")
        expected = ownership.get("managed_fingerprint")
        current = _read_json(hooks_path, "Codex hooks.json")
        managed = _managed_snapshot(current)
    except (OSError, ValueError):
        return False
    return bool(
        isinstance(expected, str)
        and expected
        and managed
        and managed_hook_fingerprint(current) == expected
    )


def _git_metadata(project_root: Path, hooks_path: Path) -> tuple[Path, Path, str] | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--show-toplevel", "--git-dir"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or len(lines) < 2:
        return None
    repository_root = Path(lines[0]).expanduser().resolve()
    git_directory = Path(lines[1]).expanduser()
    exclude_path = git_directory / "info" / "exclude"
    if not exclude_path.is_absolute():
        exclude_path = project_root / exclude_path
    try:
        relative = hooks_path.absolute().relative_to(project_root.absolute())
        relative = (project_root.resolve().relative_to(repository_root) / relative).as_posix()
    except ValueError:
        return None
    return repository_root, exclude_path, relative


def _is_tracked(project_root: Path, hooks_path: Path) -> bool:
    metadata = _git_metadata(project_root, hooks_path)
    if metadata is None:
        return False
    repository_root, _, relative = metadata
    result = subprocess.run(
        ["git", "-C", str(repository_root), "ls-files", "--error-unmatch", "--", relative],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _ensure_local_exclude(project_root: Path, hooks_path: Path) -> None:
    metadata = _git_metadata(project_root, hooks_path)
    if metadata is None:
        return
    _, exclude_path, relative = metadata
    exclude_path = validate_unredirected_file_path(exclude_path, label="Git info exclude")
    pattern = f"/{relative}"
    existing = exclude_path.read_text(encoding="utf-8").splitlines() if exclude_path.is_file() else []
    if pattern in {line.strip() for line in existing}:
        return
    ensure_unredirected_directory(exclude_path.parent, label="Codex Git exclude directory")
    content = "\n".join(existing)
    if content and not content.endswith("\n"):
        content += "\n"
    content += pattern + "\n"
    mode = exclude_path.stat().st_mode & 0o777 if exclude_path.is_file() else 0o644
    fd, temporary_name = tempfile.mkstemp(prefix=f".{exclude_path.name}.", dir=exclude_path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        temporary.chmod(mode)
        os.replace(temporary, exclude_path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _backup_hooks(project_root: Path, hooks_path: Path, harness_home: Path) -> Path | None:
    hooks_path = validate_unredirected_file_path(hooks_path, label="Codex hooks.json")
    if not hooks_path.is_file():
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = validate_unredirected_file_path(
        harness_home / "backups" / "codex-project-hooks" / _project_id(project_root) / f"{timestamp}-hooks.json",
        label="Codex project hook backup",
    )
    ensure_unredirected_directory(backup.parent, label="Codex project hook backup directory")
    shutil.copy2(hooks_path, backup)
    backup.chmod(0o600)
    return backup


def _install_project_fallback_unlocked(
    project_root: Path,
    template_path: Path,
    *,
    harness_home: Path | None = None,
    allow_tracked: bool = False,
    dry_run: bool = False,
) -> ProjectHookResult:
    try:
        root = validate_existing_project_path(project_root)
        home = _harness_home(harness_home)
    except RuntimeError as exc:
        return ProjectHookResult("conflict", str(exc), Path(project_root))
    try:
        hooks_path = validate_unredirected_file_path(
            root / ".codex" / "hooks.json",
            label="Codex hooks.json",
        )
    except RuntimeError as exc:
        return ProjectHookResult("conflict", str(exc), root / ".codex" / "hooks.json")
    ownership_path = project_state_path(root, home)
    if hooks_path.exists() and not hooks_path.is_file():
        return ProjectHookResult("conflict", "Codex hooks path is not a file", hooks_path)
    tracked = _is_tracked(root, hooks_path)
    if tracked and not allow_tracked:
        return ProjectHookResult(
            "tracked-config",
            "Codex hooks.json is tracked; explicit authorization is required",
            hooks_path,
        )
    originally_existed = hooks_path.is_file()
    try:
        existing = _read_json(hooks_path, "Codex hooks.json") if originally_existed else {}
        template = _read_json(template_path, "Tenetora Codex hook template")
        merged = merged_project_hooks(existing, materialized_project_hooks(template))
    except (OSError, ValueError) as exc:
        return ProjectHookResult("conflict", str(exc), hooks_path)
    fingerprint = managed_hook_fingerprint(merged)
    if dry_run:
        return ProjectHookResult("planned", "would install Codex project hook fallback", hooks_path)

    backup = _backup_hooks(root, hooks_path, home) if merged != existing else None
    if merged != existing:
        mode = hooks_path.stat().st_mode & 0o777 if hooks_path.is_file() else 0o600
        _write_json_atomic(hooks_path, merged, mode)
    if not tracked:
        _ensure_local_exclude(root, hooks_path)
    ownership = {
        "version": 1,
        "project_root": str(root),
        "hooks_path": str(hooks_path),
        "originally_existed": originally_existed,
        "managed_fingerprint": fingerprint,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }
    if ownership_path.is_file():
        try:
            previous_state = _read_json(ownership_path, "Codex project hook state")
        except (OSError, ValueError):
            previous_state = {}
        if previous_state.get("preference") in {"ask", "skills-only", "declined"}:
            ownership["preference"] = previous_state["preference"]
    _write_json_atomic(ownership_path, ownership, 0o600)
    return ProjectHookResult(
        "installed" if merged != existing else "current",
        "Codex project hook fallback installed" if merged != existing else "Codex project hook fallback is current",
        hooks_path,
        backup,
        ownership_path,
    )


def _remove_managed_hooks(payload: dict[str, object]) -> dict[str, object]:
    cleaned = deepcopy(payload)
    hooks = _validated_hooks(payload, "Codex hooks.json")
    remaining: dict[str, object] = {}
    for event_name, groups in hooks.items():
        preserved = _without_managed_groups(groups, event_name)
        if preserved:
            remaining[event_name] = preserved
    cleaned["hooks"] = remaining
    return cleaned


def _has_user_content(payload: dict[str, object]) -> bool:
    hooks = payload.get("hooks")
    if isinstance(hooks, dict) and hooks:
        return True
    for key, value in payload.items():
        if key == "hooks":
            continue
        if key == "description" and value == "Tenetora project fallback hooks for Codex":
            continue
        return True
    return False


def install_project_fallback(
    project_root: Path,
    template_path: Path,
    *,
    harness_home: Path | None = None,
    allow_tracked: bool = False,
    dry_run: bool = False,
) -> ProjectHookResult:
    try:
        root = validate_existing_project_path(project_root)
    except RuntimeError as exc:
        return ProjectHookResult("conflict", str(exc), Path(project_root))
    if dry_run:
        return _install_project_fallback_unlocked(
            root,
            template_path,
            harness_home=harness_home,
            allow_tracked=allow_tracked,
            dry_run=True,
        )
    with project_hooks_lock(project_root, harness_home):
        return _install_project_fallback_unlocked(
            root,
            template_path,
            harness_home=harness_home,
            allow_tracked=allow_tracked,
            dry_run=dry_run,
        )


def _remove_project_fallback_unlocked(
    project_root: Path,
    *,
    harness_home: Path | None = None,
    dry_run: bool = False,
) -> ProjectHookResult:
    try:
        root = validate_existing_project_path(project_root)
        home = _harness_home(harness_home)
    except RuntimeError as exc:
        return ProjectHookResult("conflict", str(exc), Path(project_root))
    try:
        hooks_path = validate_unredirected_file_path(
            root / ".codex" / "hooks.json",
            label="Codex hooks.json",
        )
    except RuntimeError as exc:
        return ProjectHookResult("conflict", str(exc), root / ".codex" / "hooks.json")
    ownership_path = project_state_path(root, home)
    if not ownership_path.is_file():
        return ProjectHookResult("missing", "No Tenetora Codex project fallback ownership record", hooks_path)
    try:
        ownership = _read_json(ownership_path, "Codex project fallback ownership")
        if not ownership.get("managed_fingerprint"):
            return ProjectHookResult(
                "missing",
                "No Tenetora Codex project fallback ownership record",
                hooks_path,
                ownership_path=ownership_path,
            )
        current = _read_json(hooks_path, "Codex hooks.json")
        expected = str(ownership.get("managed_fingerprint", ""))
        actual = managed_hook_fingerprint(current)
    except (OSError, ValueError) as exc:
        return ProjectHookResult("ownership-conflict", str(exc), hooks_path, ownership_path=ownership_path)
    if not expected or actual != expected:
        return ProjectHookResult(
            "ownership-conflict",
            "Managed Codex project hooks changed after installation; refusing automatic removal",
            hooks_path,
            ownership_path=ownership_path,
        )
    if dry_run:
        return ProjectHookResult("planned", "would remove Codex project hook fallback", hooks_path)

    backup = _backup_hooks(root, hooks_path, home)
    originally_existed = bool(ownership.get("originally_existed"))
    cleaned = _remove_managed_hooks(current)
    if not originally_existed and not _has_user_content(cleaned):
        hooks_path.unlink()
    else:
        mode = hooks_path.stat().st_mode & 0o777
        _write_json_atomic(hooks_path, cleaned, mode)
    preference = ownership.get("preference")
    if preference in {"ask", "skills-only", "declined"}:
        _write_json_atomic(
            ownership_path,
            {
                "version": 1,
                "project_root": str(root),
                "preference": preference,
            },
            0o600,
        )
    else:
        ownership_path.unlink()
    return ProjectHookResult(
        "removed",
        "Codex project hook fallback removed",
        hooks_path,
        backup,
        ownership_path,
    )


def remove_project_fallback(
    project_root: Path,
    *,
    harness_home: Path | None = None,
    dry_run: bool = False,
) -> ProjectHookResult:
    try:
        root = validate_existing_project_path(project_root)
    except RuntimeError as exc:
        return ProjectHookResult("conflict", str(exc), Path(project_root))
    with project_hooks_lock(project_root, harness_home):
        return _remove_project_fallback_unlocked(
            root,
            harness_home=harness_home,
            dry_run=dry_run,
        )
