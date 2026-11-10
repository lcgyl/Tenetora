"""Plan and restore only exact managed Git hooks bound to one machine home."""

from __future__ import annotations

import hashlib
import json
import re
import stat
import os
from contextlib import ExitStack
from pathlib import Path

import commit_hooks
from harness_io import atomic_write_bytes
from path_security import validate_unredirected_file_path
from tenetora.commit_hook_state import extract_managed_hook_command


def fingerprint(path: Path) -> str:
    validate_unredirected_file_path(path, label="maintenance hook target")
    if not path.exists():
        return ""
    if not path.is_file():
        raise ValueError("Maintenance hook target is not a regular file.")
    data = path.read_bytes()
    mode = stat.S_IMODE(path.stat().st_mode)
    return hashlib.sha256(str(mode).encode() + b"\0" + data).hexdigest()


def plan(projects: list[Path], home: Path, installer=None) -> dict:
    records = []
    seen = set()
    matched = set()
    projects_seen = set()
    states = []
    preserved = []
    expected = {
        (home / "bin" / name).resolve(strict=False)
        for name in ("tenetora", "tenetora.cmd", "agent-harness", "agent-harness.cmd")
    }
    for root in projects:
        root = root.resolve()
        if not root.is_dir():
            continue
        identity = (root.stat().st_dev, root.stat().st_ino)
        if identity in projects_seen:
            continue
        projects_seen.add(identity)
        if not commit_hooks.is_git_worktree(root):
            continue
        directory = commit_hooks.git_hooks_dir(root)
        shared = directory in seen
        seen.add(directory)
        removed = directory in matched
        for name in () if shared else commit_hooks.HOOK_NAMES:
            path = validate_unredirected_file_path(
                directory / name, label="managed Git hook"
            )
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            if not commit_hooks.has_managed_marker(content):
                continue
            command = extract_managed_hook_command(content)
            if (
                command is None
                or not Path(command).is_absolute()
                or Path(command).resolve(strict=False) not in expected
            ):
                continue
            if not commit_hooks.hook_matches_current_template(
                directory, name, content, command, root=root
            ):
                raise ValueError(
                    "Modified managed Git hook must be reviewed before uninstall: "
                    + name
                )
            backup = commit_hooks.original_hook_from_wrapper(content)
            if backup is not None:
                if backup.parent.resolve() != directory.resolve() or not re.fullmatch(
                    re.escape(name)
                    + r"\.(?:tenetora|agent-harness)-original(?:-[0-9TZ-]+)?",
                    backup.name,
                ):
                    raise ValueError(
                        "Original Git hook backup is outside the owned backup boundary."
                    )
                if not fingerprint(backup):
                    raise ValueError("Original Git hook backup is missing.")
            records.append(
                {
                    "project": str(root),
                    "path": str(path),
                    "before": fingerprint(path),
                    "backup": str(backup) if backup else None,
                    "backup_fingerprint": fingerprint(backup) if backup else None,
                }
            )
            removed = True
            matched.add(directory)
        state = commit_hooks.state_path(root)
        if removed and state.is_file():
            value = commit_hooks.read_state(root)
            if isinstance(value, dict) and value.get("version") == 2:
                states.append(
                    {
                        "project": str(root),
                        "path": str(state),
                        "before": fingerprint(state),
                    }
                )
            else:
                preserved.append(
                    {"path": str(state), "reason": "unknown hook policy preserved"}
                )
    from maintenance_profiles import plan as profile_plan

    profiles = profile_plan(home, fingerprint)
    from maintenance_integrations import plan as integration_plan

    integrations = (
        integration_plan(home, projects, installer, fingerprint)
        if installer is not None
        else {"actions": [], "preserved": []}
    )
    return {
        "hooks": records,
        "states": states,
        "profiles": profiles["actions"],
        "integrations": integrations["actions"],
        "preserved": preserved + profiles["preserved"] + integrations["preserved"],
    }


def validate(snapshot: dict) -> None:
    for item in [
        *snapshot["hooks"],
        *snapshot["states"],
        *snapshot.get("profiles", []),
    ]:
        if fingerprint(Path(item["path"])) != item["before"]:
            raise ValueError("Git hook or policy changed after uninstall planning.")
    for item in snapshot["hooks"]:
        if (
            item["backup"]
            and fingerprint(Path(item["backup"])) != item["backup_fingerprint"]
        ):
            raise ValueError(
                "Original Git hook backup changed after uninstall planning."
            )
    from maintenance_integrations import link_fingerprint

    for item in snapshot.get("integrations", []):
        reader = link_fingerprint if item["kind"] == "skill-link" else fingerprint
        if reader(Path(item["path"])) != item["before"]:
            raise ValueError(
                "A host integration changed after uninstall planning; it was preserved."
            )


def apply(snapshot: dict) -> dict:
    written = []
    undo = []
    roots = sorted(
        {Path(item["project"]) for item in [*snapshot["hooks"], *snapshot["states"]]}
    )
    with ExitStack() as locks:
        for root in roots:
            locks.enter_context(commit_hooks.hooks_operation_lock(root))
        validate(snapshot)
        try:
            for item in snapshot["hooks"]:
                path = Path(item["path"])
                before = path.read_bytes()
                mode = stat.S_IMODE(path.stat().st_mode)
                if fingerprint(path) != item["before"]:
                    raise ValueError("Git hook changed before restoration.")
                if item["backup"]:
                    original = Path(item["backup"])
                    data = original.read_bytes()
                    original_mode = stat.S_IMODE(original.stat().st_mode)
                    if fingerprint(original) != item["backup_fingerprint"]:
                        raise ValueError(
                            "Original Git hook backup changed before restoration."
                        )
                    after = hashlib.sha256(
                        str(original_mode).encode() + b"\0" + data
                    ).hexdigest()
                    transition = hashlib.sha256(
                        str(mode).encode() + b"\0" + data
                    ).hexdigest()
                    undo.append(
                        (path, before, mode, item["before"], {after, transition})
                    )
                    atomic_write_bytes(path, data, mode=original_mode)
                    path.chmod(original_mode)
                else:
                    undo.append((path, before, mode, item["before"], {""}))
                    path.unlink()
                written.append(str(path))
            for item in snapshot.get("profiles", []):
                path = Path(item["path"])
                before = path.read_bytes()
                mode = stat.S_IMODE(path.stat().st_mode)
                if fingerprint(path) != item["before"]:
                    raise ValueError("Shell profile changed after uninstall planning.")
                block = item["block"].encode("utf-8")
                if before.count(block) != 1:
                    raise ValueError("Managed PATH block no longer matches.")
                data = before.replace(block, b"", 1)
                after = hashlib.sha256(str(mode).encode() + b"\0" + data).hexdigest()
                undo.append((path, before, mode, item["before"], {after}))
                atomic_write_bytes(path, data, mode=mode)
            from maintenance_integrations import link_fingerprint, transformed

            for item in snapshot.get("integrations", []):
                path = Path(item["path"])
                if item["kind"] == "skill-link":
                    if link_fingerprint(path) != item["before"]:
                        raise ValueError("Skill link changed before removal.")
                    undo.append(
                        (
                            path,
                            os.readlink(path),
                            stat.S_IMODE(path.lstat().st_mode),
                            item["before"],
                            {""},
                        )
                    )
                    path.unlink()
                    continue
                if fingerprint(path) != item["before"]:
                    raise ValueError("Host integration changed before removal.")
                before = path.read_bytes()
                mode = stat.S_IMODE(path.stat().st_mode)
                data = transformed(item, before)
                actual = hashlib.sha256(data).hexdigest() if data is not None else None
                if actual != item["after_sha256"]:
                    raise ValueError(
                        "Host integration removal no longer matches its plan."
                    )
                after = (
                    hashlib.sha256(str(mode).encode() + b"\0" + data).hexdigest()
                    if data is not None
                    else ""
                )
                undo.append((path, before, mode, item["before"], {after}))
                if data is None:
                    path.unlink()
                else:
                    atomic_write_bytes(path, data, mode=mode)
            for item in snapshot["states"]:
                path = Path(item["path"])
                before = path.read_bytes()
                mode = stat.S_IMODE(path.stat().st_mode)
                if fingerprint(path) != item["before"]:
                    raise ValueError("Git hook policy changed before restoration.")
                value = json.loads(before)
                value.update(decision="defer", hooks={})
                value.pop("cli_locator", None)
                data = (
                    json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2)
                    + "\n"
                ).encode()
                after = hashlib.sha256(str(mode).encode() + b"\0" + data).hexdigest()
                undo.append((path, before, mode, item["before"], {after}))
                atomic_write_bytes(path, data, mode=mode)
            for path, data, _, _, expected in undo:
                current = (
                    link_fingerprint(path)
                    if isinstance(data, str)
                    else fingerprint(path)
                )
                if current not in expected:
                    raise ValueError(
                        "A restored host file was concurrently modified; it was preserved."
                    )
        except (OSError, RuntimeError, ValueError):
            conflicts = []
            for path, data, mode, original, expected in reversed(undo):
                try:
                    current = (
                        link_fingerprint(path)
                        if isinstance(data, str)
                        else fingerprint(path)
                    )
                    if current == original:
                        continue
                    if current not in expected:
                        conflicts.append(str(path))
                        continue
                    if isinstance(data, str):
                        path.symlink_to(data, target_is_directory=True)
                    else:
                        atomic_write_bytes(path, data, mode=mode)
                        path.chmod(mode)
                except (OSError, RuntimeError, ValueError):
                    conflicts.append(str(path))
            if conflicts:
                raise ValueError(
                    "Host file restoration preserved concurrent changes; review: "
                    + ", ".join(conflicts)
                )
            raise
    return {
        "restored_hooks": written,
        "original_backups_preserved": True,
        "profile_blocks_removed": [
            item["path"] for item in snapshot.get("profiles", [])
        ],
        "host_files_detached": [
            item["path"] for item in snapshot.get("integrations", [])
        ],
    }
