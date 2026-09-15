"""Canonical Tenetora identity and bounded legacy-migration helpers."""

from __future__ import annotations

import os
import json
import hashlib
import shutil
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from typing import Any

from .file_lock import locked_file
from .path_security import (
    copy_redirected_entry,
    create_redirected_entry,
    ensure_unredirected_directory,
    is_redirected_path,
    remove_unredirected_entry,
    validate_unredirected_entry_path,
    validate_unredirected_file_path,
    validate_unredirected_path,
)


PRODUCT_NAME = "Tenetora"
LEGACY_PRODUCT_NAME = "Agent Harness"
COMMAND_NAME = "tenetora"
LEGACY_COMMAND_NAME = "agent-harness"
REPOSITORY_URL = "https://github.com/lcgyl/Tenetora"
LEGACY_REPOSITORY_URL = "https://github.com/lcgyl/Tenetora"
CANONICAL_SKILL_PREFIX = "tenetora"
LEGACY_SKILL_PREFIX = "agent-harness"
CANONICAL_HOME_NAME = ".tenetora"
LEGACY_HOME_NAME = ".agent-harness"
MACHINE_HOME_MIGRATION_VERSION = 1
PREFERENCES_VERSION = 1


def user_home() -> Path:
    configured = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    return Path(configured).expanduser() if configured else Path.home()


def default_machine_home() -> Path:
    return user_home() / CANONICAL_HOME_NAME


def default_legacy_machine_home() -> Path:
    return user_home() / LEGACY_HOME_NAME


def validate_managed_home_path(raw: Path | str, *, label: str = "Tenetora home") -> Path:
    """Return a lexical absolute home only when every existing component is safe."""

    return validate_unredirected_path(raw, label=label)


def machine_home() -> Path:
    """Return the canonical active machine home.

    Legacy homes are migration inputs discovered from disk. They are not an
    alternative runtime configuration surface.
    """

    canonical = os.environ.get("TENETORA_HOME")
    if canonical:
        return validate_managed_home_path(canonical, label="TENETORA_HOME")
    return validate_managed_home_path(default_machine_home(), label="Tenetora home")


def language_preferences_path() -> Path:
    return machine_home() / "state" / "preferences.json"


def read_language_preference() -> str | None:
    try:
        payload = json.loads(language_preferences_path().read_text(encoding="utf-8"))
    except (OSError, RuntimeError, json.JSONDecodeError):
        return None
    language = payload.get("language") if isinstance(payload, dict) else None
    return str(language) if language in {"en", "zh"} else None


def write_language_preference(language: str) -> None:
    if language not in {"en", "zh"}:
        return
    try:
        path = validate_unredirected_file_path(language_preferences_path(), label="language preferences")
        ensure_unredirected_directory(path.parent, label="language preferences directory")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".preferences.", suffix=".tmp", dir=str(path.parent))
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"version": PREFERENCES_VERSION, "language": language}, handle, sort_keys=True)
                handle.write("\n")
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass
    except OSError:
        return


def _read_text(path: Path, limit: int = 131072) -> str:
    try:
        path = validate_unredirected_file_path(path, label="managed metadata")
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def _valid_registry(path: Path) -> bool:
    try:
        path = validate_unredirected_file_path(path, label="installation registry")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, RuntimeError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("version") == 1
        and isinstance(payload.get("projects"), list)
    )


def inspect_machine_home(path: Path) -> dict[str, Any]:
    """Return independent ownership fingerprints for a machine home."""

    root = path.expanduser()
    if not root.exists() and not is_redirected_path(root):
        return {"path": str(root), "state": "missing", "managed": False, "fingerprints": []}
    if is_redirected_path(root) or not root.is_dir():
        return {"path": str(root), "state": "unsafe", "managed": False, "fingerprints": []}
    if os.name != "nt" and hasattr(os, "getuid"):
        try:
            info = root.stat()
        except OSError:
            return {"path": str(root), "state": "unsafe", "managed": False, "fingerprints": []}
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            return {"path": str(root), "state": "unsafe", "managed": False, "fingerprints": []}

    # Inspection must not follow an attacker-controlled child and then use its
    # contents as ownership evidence. `current` is the only supported pointer;
    # all metadata directories and inspected files must be real entries.
    for relative in (
        Path("bin"),
        Path("runtime"),
        Path("state"),
        Path("releases"),
        Path("plugin-sources"),
        Path("source"),
    ):
        candidate = root / relative
        if is_redirected_path(candidate):
            return {"path": str(root), "state": "unsafe", "managed": False, "fingerprints": []}
    pointer = root / "current"
    if is_redirected_path(pointer):
        try:
            pointer.resolve(strict=False).relative_to(root.resolve(strict=False))
        except (OSError, ValueError):
            return {"path": str(root), "state": "unsafe", "managed": False, "fingerprints": []}
    for relative in (
        Path("bin/tenetora"),
        Path("bin/agent-harness"),
        Path("bin/tenetora.cmd"),
        Path("bin/agent-harness.cmd"),
        Path("runtime/VERSION"),
        Path("runtime/hooks/tenetora_hook.py"),
        Path("runtime/hooks/agent_harness_hook.py"),
        Path("runtime/cli/tenetora/cli.py"),
        Path("runtime/cli/agent_harness/cli.py"),
        Path("state/installations.json"),
    ):
        if is_redirected_path(root / relative):
            return {"path": str(root), "state": "unsafe", "managed": False, "fingerprints": []}

    fingerprints: list[str] = []
    shim_text = "\n".join(
        _read_text(root / "bin" / name)
        for name in ("tenetora", "agent-harness", "tenetora.cmd", "agent-harness.cmd")
    )
    if ("tenetora.cli" in shim_text or "agent_harness.cli" in shim_text) and (
        "TENETORA_INVOKED_AS" in shim_text
        or "AGENT_HARNESS_INVOKED_AS" in shim_text
        or "AGENT_HARNESS_HOME" in shim_text
    ):
        fingerprints.append("managed-cli-shim")

    runtime = root / "runtime"
    canonical_runtime = (
        (runtime / "hooks" / "tenetora_hook.py").is_file()
        and (runtime / "cli" / "tenetora" / "cli.py").is_file()
    )
    legacy_runtime = (
        (runtime / "hooks" / "agent_harness_hook.py").is_file()
        and (runtime / "cli" / "agent_harness" / "cli.py").is_file()
    )
    if (runtime / "VERSION").is_file() and (canonical_runtime or legacy_runtime):
        fingerprints.append("managed-runtime")

    package_skill = any(
        (root / "current" / "skills" / name / "SKILL.md").is_file()
        for name in ("tenetora", "agent-harness")
    )
    if not package_skill:
        releases = root / "releases"
        if releases.is_dir():
            for release in sorted(releases.iterdir(), reverse=True)[:32]:
                if any((release / "skills" / name / "SKILL.md").is_file() for name in ("tenetora", "agent-harness")):
                    package_skill = True
                    break
    if package_skill:
        fingerprints.append("managed-release")

    if _valid_registry(root / "state" / "installations.json"):
        fingerprints.append("installation-registry")

    plugin_sources = root / "plugin-sources"
    if plugin_sources.is_dir() and any(
        (entry / "skills" / "tenetora" / "SKILL.md").is_file()
        or (entry / "skills" / "agent-harness" / "SKILL.md").is_file()
        for entry in list(plugin_sources.iterdir())[:32]
        if entry.is_dir()
    ):
        fingerprints.append("managed-plugin-source")

    source = root / "source"
    if any(
        (source / repository / "skills" / skill / "SKILL.md").is_file()
        for repository in ("tenetora", "agent-harness")
        for skill in ("tenetora", "agent-harness")
    ):
        fingerprints.append("managed-source")

    return {
        "path": str(root),
        "state": "managed" if len(fingerprints) >= 2 else "unproven",
        "managed": len(fingerprints) >= 2,
        "fingerprints": fingerprints,
    }


def _copy_entry(source: Path, destination: Path) -> None:
    ensure_unredirected_directory(destination.parent, label="managed metadata backup directory")
    if is_redirected_path(destination):
        raise RuntimeError(f"managed metadata backup destination is a symbolic link or junction: {destination}")
    if is_redirected_path(source):
        copy_redirected_entry(source, destination, label="managed metadata backup entry")
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    elif source.is_file():
        shutil.copy2(source, destination)


def _backup_mutable_metadata(root: Path, report: dict[str, Any], stamp: str) -> Path:
    backup = root / "backups" / "legacy-machine-home" / stamp / "metadata-before-rewrite"
    ensure_unredirected_directory(backup, label="managed metadata backup directory")
    for relative in (
        Path("bin"),
        Path("runtime"),
        Path("state"),
        Path("current"),
    ):
        source = root / relative
        if source.exists() or is_redirected_path(source):
            _copy_entry(source, backup / relative)
    _write_managed_text(
        backup / "ownership.json",
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        label="metadata ownership report",
    )
    return backup.parent


def _write_managed_text(path: Path, content: str, *, label: str) -> None:
    path = validate_unredirected_file_path(path, label=label)
    ensure_unredirected_directory(path.parent, label=f"{label} directory")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _rewrite_managed_symlinks(root: Path, legacy: Path, canonical: Path) -> int:
    rewritten = 0
    for current_root, directory_names, file_names in os.walk(root, followlinks=False):
        base = Path(current_root)
        try:
            relative_base = base.relative_to(root)
        except ValueError:
            continue
        if relative_base.parts[:2] == ("backups", "legacy-machine-home"):
            directory_names[:] = []
            continue
        for name in [*directory_names, *file_names]:
            candidate = base / name
            if not is_redirected_path(candidate):
                continue
            if name in directory_names:
                directory_names.remove(name)
            try:
                target = Path(os.readlink(candidate))
            except OSError:
                try:
                    target = candidate.resolve(strict=False)
                except OSError:
                    continue
            if not target.is_absolute():
                continue
            try:
                relative = target.resolve(strict=False).relative_to(legacy.resolve(strict=False))
            except ValueError:
                continue
            replacement = canonical / relative
            temporary = candidate.with_name(f".{candidate.name}.{uuid.uuid4().hex}.tmp")
            target_is_directory = candidate.is_dir() or replacement.is_dir()
            is_windows_non_symlink_redirect = os.name == "nt" and not candidate.is_symlink()
            if is_windows_non_symlink_redirect and not target_is_directory:
                raise RuntimeError(f"unsupported Windows reparse migration pointer: {candidate}")
            prefer_junction = is_windows_non_symlink_redirect and target_is_directory
            try:
                create_redirected_entry(
                    temporary,
                    replacement,
                    target_is_directory=target_is_directory,
                    prefer_junction=prefer_junction,
                    label="managed migration pointer",
                )
                remove_unredirected_entry(candidate, label="managed migration pointer")
                os.replace(temporary, candidate)
            finally:
                if temporary.exists() or is_redirected_path(temporary):
                    remove_unredirected_entry(temporary, label="temporary managed migration pointer")
            rewritten += 1
    return rewritten


def _merge_registry(source: Path, destination: Path) -> None:
    destination = validate_unredirected_file_path(destination, label="installation registry")
    try:
        old = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not destination.is_file():
        _copy_entry(source, destination)
        return
    try:
        current = json.loads(destination.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    old_projects = old.get("projects") if isinstance(old, dict) else None
    current_projects = current.get("projects") if isinstance(current, dict) else None
    if not isinstance(old_projects, list) or not isinstance(current_projects, list):
        return
    by_path = {
        str(item.get("path")): item
        for item in current_projects
        if isinstance(item, dict) and item.get("path")
    }
    for item in old_projects:
        if isinstance(item, dict) and item.get("path") and str(item["path"]) not in by_path:
            current_projects.append(item)
    old_global = old.get("global") if isinstance(old, dict) else None
    current_global = current.get("global") if isinstance(current, dict) else None
    old_global = old_global if isinstance(old_global, dict) else {}
    current_global = current_global if isinstance(current_global, dict) else {}
    old_surfaces = old_global.get("surfaces") if isinstance(old_global.get("surfaces"), dict) else {}
    current_surfaces = (
        current_global.get("surfaces") if isinstance(current_global.get("surfaces"), dict) else {}
    )
    old_ignored = set(old_global.get("ignored_tools") or [])
    current_ignored = set(current_global.get("ignored_tools") or [])
    merged_surfaces = dict(current_surfaces)
    ignored = set(current_ignored)
    canonical_decisions = set(current_surfaces) | current_ignored
    for tool, scopes in old_surfaces.items():
        if tool not in canonical_decisions and tool not in old_ignored:
            merged_surfaces[tool] = scopes
    for tool in old_ignored:
        if tool not in canonical_decisions:
            ignored.add(tool)
    current["projects"] = current_projects
    current["global"] = {
        "surfaces": dict(sorted(merged_surfaces.items())),
        "ignored_tools": sorted(ignored),
        "last_seen_at": max(
            str(old_global.get("last_seen_at") or ""),
            str(current_global.get("last_seen_at") or ""),
        ),
    }
    _write_managed_text(
        destination,
        json.dumps(current, ensure_ascii=False, indent=2) + "\n",
        label="installation registry",
    )


def _merge_legacy_backup(backup_home: Path, canonical: Path) -> list[str]:
    canonical = validate_unredirected_path(canonical, label="Tenetora canonical home")
    merged: list[str] = []
    for entry_name in ("bin", "runtime", "current", "source"):
        source = backup_home / entry_name
        destination = canonical / entry_name
        if (source.exists() or is_redirected_path(source)) and not (
            destination.exists() or is_redirected_path(destination)
        ):
            _copy_entry(source, destination)
            merged.append(entry_name)
    for directory in ("releases", "plugin-sources", "logs", "uninstall-backups"):
        source_root = backup_home / directory
        if is_redirected_path(source_root):
            raise RuntimeError(f"legacy backup directory is a symbolic link or junction: {source_root}")
        if not source_root.is_dir():
            continue
        destination_root = canonical / directory
        ensure_unredirected_directory(destination_root, label="canonical migration directory")
        for entry in source_root.iterdir():
            destination = destination_root / entry.name
            if destination.exists() or is_redirected_path(destination):
                continue
            _copy_entry(entry, destination)
            merged.append(str(Path(directory) / entry.name))

    source_state = backup_home / "state"
    destination_state = canonical / "state"
    if is_redirected_path(source_state):
        raise RuntimeError(f"legacy backup state directory is a symbolic link or junction: {source_state}")
    if source_state.is_dir():
        ensure_unredirected_directory(destination_state, label="canonical state directory")
        _merge_registry(source_state / "installations.json", destination_state / "installations.json")
        for entry in source_state.iterdir():
            if entry.name == "installations.json":
                continue
            destination = destination_state / entry.name
            if destination.exists() or is_redirected_path(destination):
                continue
            _copy_entry(entry, destination)
            merged.append(str(Path("state") / entry.name))
    return merged


def _managed_source_checkout(path: Path) -> bool:
    skill = any(
        (path / "skills" / name / "SKILL.md").is_file()
        for name in ("tenetora", "agent-harness")
    )
    package = (path / "install.sh").is_file() and (
        (path / "pyproject.toml").is_file()
        or (path / "tenetora.plugin.json").is_file()
        or (path / "agent-harness.plugin.json").is_file()
    )
    return skill and package


def _normalize_legacy_source_entry(root: Path, stamp: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Retire the old active source entry without touching ambiguous content."""

    legacy_source = root / "source" / "agent-harness"
    result: dict[str, Any] = {"status": "not-needed", "path": str(legacy_source), "backup": None}
    if not legacy_source.exists() and not is_redirected_path(legacy_source):
        return result
    if is_redirected_path(legacy_source):
        try:
            target = legacy_source.resolve(strict=False)
            target.relative_to(root.resolve(strict=False))
        except (OSError, ValueError):
            result["status"] = "foreign-preserved"
            return result
        if dry_run:
            result["status"] = "would-remove-managed-symlink"
            return result
        remove_unredirected_entry(legacy_source, label="managed legacy source pointer")
        result["status"] = "removed-managed-symlink"
        return result
    if legacy_source.is_dir():
        try:
            empty = next(legacy_source.iterdir(), None) is None
        except OSError:
            empty = False
        if empty:
            if dry_run:
                result["status"] = "would-remove-empty-directory"
                return result
            legacy_source.rmdir()
            result["status"] = "removed-empty-directory"
            return result
        if _managed_source_checkout(legacy_source):
            backup = root / "backups" / "legacy-machine-home" / stamp / "legacy-source" / "agent-harness"
            if dry_run:
                result["status"] = "would-archive-managed-source"
                result["backup"] = str(backup)
                return result
            backup.parent.mkdir(parents=True, exist_ok=True)
            ensure_unredirected_directory(backup.parent, label="legacy source backup directory")
            legacy_source.replace(backup)
            result["status"] = "archived-managed-source"
            result["backup"] = str(backup)
            return result
    result["status"] = "foreign-preserved"
    return result


def _recorded_legacy_migration(canonical: Path, legacy: Path) -> bool:
    marker = canonical / "state" / "machine-home-migration.json"
    try:
        marker = validate_unredirected_file_path(marker, label="machine-home migration marker")
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, RuntimeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    recorded = payload.get("legacy_home")
    if not isinstance(recorded, str) or not recorded:
        return False
    try:
        return Path(recorded).expanduser().resolve(strict=False) == legacy.resolve(strict=False)
    except OSError:
        return False


def _legacy_residual_is_managed(canonical: Path, legacy: Path) -> bool:
    if not _recorded_legacy_migration(canonical, legacy):
        return False
    allowed_files = {
        Path("bin/agent-harness"),
        Path("bin/agent-harness.cmd"),
        Path("bin/tenetora"),
        Path("bin/tenetora.cmd"),
    }
    try:
        for current_root, directory_names, file_names in os.walk(legacy, followlinks=False):
            base = Path(current_root)
            for name in list(directory_names):
                entry = base / name
                relative = entry.relative_to(legacy)
                if is_redirected_path(entry):
                    directory_names.remove(name)
                    return False
                if not entry.is_dir():
                    return False
            for name in file_names:
                entry = base / name
                relative = entry.relative_to(legacy)
                if is_redirected_path(entry):
                    if relative not in allowed_files:
                        return False
                    continue
                if relative not in allowed_files:
                    return False
                text = _read_text(entry, limit=16384)
                if "tenetora.cli" not in text and "TENETORA_INVOKED_AS" not in text:
                    return False
    except OSError:
        return False
    return True


def _archive_legacy_residual(
    canonical: Path,
    legacy: Path,
    stamp: str,
    *,
    dry_run: bool,
) -> dict[str, Any] | None:
    if not legacy.is_dir() or is_redirected_path(legacy) or not _legacy_residual_is_managed(canonical, legacy):
        return None
    backup = canonical / "backups" / "legacy-machine-home" / stamp / "residual-home"
    if dry_run:
        return {"status": "would-archive-managed-residual", "backup": str(backup)}
    ensure_unredirected_directory(backup.parent, label="legacy residual backup directory")
    legacy.replace(backup)
    return {"status": "archived-managed-residual", "backup": str(backup)}


def _validate_legacy_source_path(raw: Path | str) -> Path:
    """Validate a legacy source without requiring a directory that may be gone."""

    candidate = validate_unredirected_entry_path(raw, label="legacy Agent Harness home")
    if candidate.exists() or is_redirected_path(candidate):
        return validate_managed_home_path(candidate, label="legacy Agent Harness home")
    return candidate


def _migrate_legacy_machine_home(
    *,
    canonical: Path | None = None,
    legacy: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Migrate a proven legacy machine home without touching foreign data."""

    destination = validate_managed_home_path(canonical or machine_home(), label="Tenetora canonical home")
    source = _validate_legacy_source_path(legacy or default_legacy_machine_home())
    result: dict[str, Any] = {
        "version": MACHINE_HOME_MIGRATION_VERSION,
        "canonical_home": str(destination),
        "legacy_home": str(source),
        "status": "not-needed",
        "backup": None,
        "rewritten_symlinks": 0,
        "merged": [],
        "legacy_source": None,
    }
    if destination == source:
        return result

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if not source.exists():
        if destination.is_dir() and inspect_machine_home(destination)["managed"]:
            normalized = _normalize_legacy_source_entry(destination, stamp, dry_run=dry_run)
            result["legacy_source"] = normalized
            if normalized["status"].startswith("would-"):
                result["status"] = "would-normalize"
            elif normalized["status"] not in {"not-needed", "foreign-preserved"}:
                result["status"] = "normalized"
                state = ensure_unredirected_directory(destination / "state", label="canonical state directory")
                _write_managed_text(
                    state / "machine-home-migration.json",
                    json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                    label="machine-home migration marker",
                )
        return result

    legacy_report = inspect_machine_home(source)
    result["legacy_fingerprints"] = legacy_report["fingerprints"]
    if not legacy_report["managed"]:
        residual = _archive_legacy_residual(
            destination,
            source,
            stamp,
            dry_run=dry_run,
        )
        if residual is not None:
            result["status"] = str(residual["status"])
            result["backup"] = residual["backup"]
            return result
        result["status"] = "foreign-preserved"
        return result

    canonical_report = inspect_machine_home(destination)
    if destination.exists():
        if is_redirected_path(destination) or not destination.is_dir():
            result["status"] = "blocked-canonical-conflict"
            return result
        if any(destination.iterdir()) and not canonical_report["managed"]:
            result["status"] = "blocked-canonical-conflict"
            return result
    if dry_run:
        result["status"] = "would-merge" if destination.exists() else "would-migrate"
        return result

    if not destination.exists():
        source.replace(destination)
        try:
            backup = _backup_mutable_metadata(destination, legacy_report, stamp)
            result["backup"] = str(backup)
            result["rewritten_symlinks"] = _rewrite_managed_symlinks(destination, source, destination)
            result["status"] = "migrated"
        except Exception:
            if destination.exists() and not source.exists():
                destination.replace(source)
            raise
    else:
        backup = destination / "backups" / "legacy-machine-home" / stamp
        ensure_unredirected_directory(backup, label="legacy home backup directory")
        backup_home = backup / "legacy-home"
        source.replace(backup_home)
        try:
            result["backup"] = str(backup)
            result["merged"] = _merge_legacy_backup(backup_home, destination)
            result["rewritten_symlinks"] = _rewrite_managed_symlinks(destination, source, destination)
            result["status"] = "merged"
        except Exception:
            if backup_home.exists() and not source.exists():
                backup_home.replace(source)
            raise

    result["legacy_source"] = _normalize_legacy_source_entry(destination, stamp, dry_run=dry_run)
    state = destination / "state"
    state = ensure_unredirected_directory(state, label="canonical state directory")
    marker = state / "machine-home-migration.json"
    _write_managed_text(
        marker,
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        label="machine-home migration marker",
    )
    return result


@contextmanager
def _machine_home_migration_lock(canonical: Path):
    """Serialize migrations targeting the same canonical machine home."""

    lock_name = ".tenetora-legacy-migration-" + hashlib.sha256(str(canonical).encode("utf-8")).hexdigest() + ".lock"
    preferred_path = canonical.parent / f".{canonical.name}.legacy-migration.lock"
    try:
        lock_context = locked_file(preferred_path)
        lock_context.__enter__()
    except OSError:
        lock_context = locked_file(Path(tempfile.gettempdir()) / lock_name)
        lock_context.__enter__()
    try:
        yield
    finally:
        lock_context.__exit__(*sys.exc_info())


def migrate_legacy_machine_home(
    *,
    canonical: Path | None = None,
    legacy: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Migrate a proven legacy machine home under a per-destination lock."""

    destination = validate_managed_home_path(canonical or machine_home(), label="Tenetora canonical home")
    with _machine_home_migration_lock(destination):
        return _migrate_legacy_machine_home(
            canonical=destination,
            legacy=legacy or default_legacy_machine_home(),
            dry_run=dry_run,
        )


def normalize_environment() -> None:
    """Reserve one normalization point for the canonical environment protocol."""


def invoked_command() -> str:
    raw = os.environ.get("TENETORA_INVOKED_AS", "").strip()
    if not raw:
        raw = sys.argv[0] if sys.argv else ""
    name = Path(raw).name
    if name.endswith(".cmd"):
        name = name[:-4]
    if name == COMMAND_NAME:
        return name
    return COMMAND_NAME
