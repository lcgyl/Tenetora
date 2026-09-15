#!/usr/bin/env python3
"""Stage and atomically activate immutable Tenetora release directories."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from upgrade_contract import UpgradeContractError, normalize_manifest
from path_security import (
    is_redirected_path,
    remove_unredirected_entry,
    validate_existing_project_path,
    validate_unredirected_path,
)


RELEASE_CONTRACT_FIELDS = (
    "name",
    "version",
    "commit",
    "format",
    "installer_protocol",
    "bridge_protocol",
    "minimum_bridge_version",
    "upgrade_entrypoint",
    "state_schemas",
    "zip_root",
    "files",
    "file_sha256",
)


class ReleaseStoreError(RuntimeError):
    """Raised when a release or managed pointer is unsafe."""


def read_release_manifest(root: Path) -> dict[str, Any]:
    try:
        payload = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        return normalize_manifest(payload, require_artifact=False)
    except (OSError, json.JSONDecodeError, UpgradeContractError) as exc:
        raise ReleaseStoreError(f"managed release manifest is invalid: {exc}") from exc


def _inside_releases(path: Path, home: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to((home / "releases").resolve(strict=False))
    except ValueError:
        return False
    return True


def _managed_release_identity_matches(
    path: Path,
    home: Path,
    manifest: dict[str, Any],
) -> bool:
    """Require a release to be the versioned child named by its manifest."""

    if path.name != manifest.get("version"):
        return False
    try:
        releases = validate_existing_project_path(
            home / "releases",
            label="Tenetora release storage",
        )
        return path.parent.resolve(strict=True) == releases.resolve(strict=True)
    except (OSError, RuntimeError):
        return False


def _manifest_contract_matches(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(left.get(field) == right.get(field) for field in RELEASE_CONTRACT_FIELDS)


def managed_release(path: Path, home: Path) -> bool:
    try:
        home = validate_unredirected_path(home, label="Tenetora release home")
        path = validate_existing_project_path(path, label="managed release")
    except RuntimeError:
        return False
    if not _inside_releases(path, home):
        return False
    try:
        manifest = read_release_manifest(path)
    except ReleaseStoreError:
        return False
    return (
        _managed_release_identity_matches(path, home, manifest)
        and release_content_matches(path, manifest)
    )


def prune_python_bytecode(root: Path) -> None:
    """Remove only interpreter caches that older managed shims could create."""

    cache_directories = sorted(
        (path for path in root.rglob("__pycache__")),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for cache in cache_directories:
        if is_redirected_path(cache) or not cache.is_dir():
            raise ReleaseStoreError("managed release contains an unsafe Python cache entry")
        for entry in cache.rglob("*"):
            if is_redirected_path(entry):
                raise ReleaseStoreError("managed release contains an unsafe Python cache entry")
            if entry.is_dir():
                continue
            if not entry.is_file() or entry.suffix not in {".pyc", ".pyo"}:
                raise ReleaseStoreError("managed release contains an unexpected Python cache entry")
    for cache in cache_directories:
        if cache.exists():
            shutil.rmtree(cache)


def release_content_matches(root: Path, manifest: dict[str, Any]) -> bool:
    expected = manifest.get("file_sha256")
    if not isinstance(expected, dict):
        return False
    try:
        entries = list(root.rglob("*"))
        if any(is_redirected_path(path) or (not path.is_file() and not path.is_dir()) for path in entries):
            return False
        actual_files = sorted(
            path.relative_to(root).as_posix()
            for path in entries
            if path.is_file() and path.relative_to(root).as_posix() != "manifest.json"
        )
        if actual_files != manifest.get("files"):
            return False
        actual = {
            relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
            for relative in actual_files
        }
    except (OSError, ValueError):
        return False
    return actual == expected


def legacy_release(path: Path, home: Path) -> bool:
    """Recognize historical release roots only for replacing an existing pointer."""
    try:
        home = validate_unredirected_path(home, label="Tenetora release home")
        path = validate_existing_project_path(path, label="legacy release")
    except RuntimeError:
        return False
    roots = (home / "releases", home.parent / ".agent-harness" / "releases")
    resolved = path.resolve(strict=False)
    for root in roots:
        try:
            resolved.relative_to(root.resolve(strict=False))
        except ValueError:
            continue
        return resolved != root.resolve(strict=False)
    return False


def stage_release(source_root: Path, home: Path, manifest: dict[str, Any]) -> tuple[Path, bool]:
    requested_source = source_root.expanduser()
    if is_redirected_path(requested_source):
        raise ReleaseStoreError("release staging source must not be a symbolic link")
    try:
        source = validate_existing_project_path(source_root, label="release staging source")
        home = validate_unredirected_path(home, label="Tenetora release home")
    except RuntimeError as exc:
        raise ReleaseStoreError(str(exc)) from exc
    expected = normalize_manifest(manifest, require_artifact=bool(manifest.get("artifact")))
    source_manifest = read_release_manifest(source)
    if not release_content_matches(source, source_manifest):
        raise ReleaseStoreError("acquired release contents do not match its manifest")
    if not _manifest_contract_matches(source_manifest, expected):
        raise ReleaseStoreError("acquired release does not match the selected manifest")
    try:
        releases = validate_unredirected_path(
            home / "releases",
            label="Tenetora release storage",
        )
    except RuntimeError as exc:
        raise ReleaseStoreError(str(exc)) from exc
    releases.mkdir(parents=True, exist_ok=True)
    destination = releases / str(expected["version"])
    if destination.exists() or is_redirected_path(destination):
        if is_redirected_path(destination) or not destination.is_dir() or not _inside_releases(destination, home):
            raise ReleaseStoreError("existing versioned release is not a valid managed release")
        current = read_release_manifest(destination)
        if not _managed_release_identity_matches(destination, home, current):
            raise ReleaseStoreError("existing versioned release is not a valid managed release")
        if not _manifest_contract_matches(current, expected):
            raise ReleaseStoreError("versioned release is immutable and already contains different content")
        prune_python_bytecode(destination)
        if not managed_release(destination, home):
            raise ReleaseStoreError("existing versioned release is not a valid managed release")
        if not release_content_matches(destination, expected):
            raise ReleaseStoreError("versioned release contents do not match its manifest")
        return destination, False
    staging = Path(tempfile.mkdtemp(prefix=f".{expected['version']}.", dir=releases))
    staging.rmdir()
    try:
        shutil.copytree(source, staging, symlinks=False)
        staged_manifest = read_release_manifest(staging)
        if not _manifest_contract_matches(staged_manifest, expected) or not release_content_matches(
            staging, staged_manifest
        ):
            raise ReleaseStoreError("staged release contents do not match its manifest")
        os.replace(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination, True


def _assert_pointer_replaceable(path: Path, home: Path) -> None:
    try:
        home = validate_unredirected_path(home, label="Tenetora release home")
        validate_unredirected_path(path.parent, label="managed pointer parent")
    except RuntimeError as exc:
        raise ReleaseStoreError(str(exc)) from exc
    if not (path.exists() or is_redirected_path(path)):
        return
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ReleaseStoreError(f"managed pointer is unreadable: {path.name}") from exc
    if not managed_release(resolved, home) and not legacy_release(resolved, home):
        raise ReleaseStoreError(f"refusing to replace unowned pointer: {path.name}")


def _remove(path: Path) -> None:
    try:
        remove_unredirected_entry(path, label="managed release cleanup")
    except RuntimeError as exc:
        raise ReleaseStoreError(str(exc)) from exc


def _new_pointer(link: Path, target: Path) -> Path:
    temporary = link.with_name(f".{link.name}.{uuid.uuid4().hex}.tmp")
    if os.name == "nt":
        # cmd.exe writes its localized junction confirmation to stdout in the
        # active code page (GBK on zh-CN). Decode with replacement so the
        # reader thread survives; the text is diagnostic only, control flow
        # uses the exit code.
        result = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(temporary), str(target)],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            suffix = f": {detail[0]}" if detail else ""
            raise ReleaseStoreError(f"cannot create the managed Windows release junction{suffix}")
        if not temporary.exists():
            # mklink /J can exit 0 while creating nothing (e.g. a dangling
            # target); surface it instead of handing a ghost path to os.replace.
            raise ReleaseStoreError("managed Windows release junction was not created")
    else:
        temporary.symlink_to(target, target_is_directory=True)
    return temporary


def _replace_windows_pointer(link: Path, temporary: Path) -> None:
    """Replace a junction while preserving the prior pointer on failure."""

    backup: Path | None = None
    if link.exists() or is_redirected_path(link):
        backup = link.with_name(f".{link.name}.{uuid.uuid4().hex}.backup")
        os.replace(link, backup)
    try:
        os.replace(temporary, link)
    except OSError as exc:
        if backup is not None and (backup.exists() or is_redirected_path(backup)):
            try:
                os.replace(backup, link)
            except OSError as restore_exc:
                raise ReleaseStoreError(
                    f"managed pointer replacement failed and its backup could not be restored: {backup.name}"
                ) from restore_exc
        raise ReleaseStoreError("managed pointer replacement failed; the prior pointer was restored") from exc
    if backup is not None:
        _remove(backup)


def replace_pointer(link: Path, target: Path, home: Path) -> None:
    try:
        validate_unredirected_path(link.parent, label="managed pointer parent")
        target = validate_existing_project_path(target, label="managed release target")
    except RuntimeError as exc:
        raise ReleaseStoreError(str(exc)) from exc
    _assert_pointer_replaceable(link, home)
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = _new_pointer(link, target)
    try:
        if os.name == "nt":
            _replace_windows_pointer(link, temporary)
        else:
            # Replacing one symlink with another is atomic on POSIX; removing the
            # old link first would create a visible gap for concurrent launchers.
            os.replace(temporary, link)
    finally:
        _remove(temporary)


def _pointer_target(path: Path) -> Path | None:
    if not (path.exists() or is_redirected_path(path)):
        return None
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise ReleaseStoreError(f"managed pointer is unreadable: {path.name}") from exc


def _receipt_target(value: object, *, label: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ReleaseStoreError(f"release activation receipt has an invalid {label} target")
    return Path(value)


def _valid_previous_release(target: Path | None, home: Path, *, label: str) -> None:
    if target is None:
        return
    if not (managed_release(target, home) or legacy_release(target, home)):
        raise ReleaseStoreError(f"release activation receipt has an invalid {label} target")


def restore_release_activation(home: Path, receipt: dict[str, Any]) -> None:
    """Compensate a completed activation when a later upgrade step fails."""

    try:
        home = validate_unredirected_path(home, label="Tenetora release home")
    except RuntimeError as exc:
        raise ReleaseStoreError(str(exc)) from exc
    if not isinstance(receipt, dict):
        raise ReleaseStoreError("release activation receipt is invalid")
    try:
        expected = validate_existing_project_path(
            Path(str(receipt.get("release") or "")),
            label="activated release",
        ).resolve(strict=True)
    except RuntimeError as exc:
        raise ReleaseStoreError(str(exc)) from exc
    if not _inside_releases(expected, home) or not managed_release(expected, home):
        raise ReleaseStoreError("release activation receipt points to an invalid release")
    previous = receipt.get("previous")
    if not isinstance(previous, dict):
        raise ReleaseStoreError("release activation receipt has no previous pointers")

    pointers = {
        "current": home / "current",
        "source": home / "source" / "tenetora",
        "legacy": home / "source" / "agent-harness",
    }
    prior = {
        name: _receipt_target(previous.get(name), label=name)
        for name in pointers
    }
    for name, target in prior.items():
        _valid_previous_release(target, home, label=name)

    actual = {name: _pointer_target(path) for name, path in pointers.items()}
    for name in ("current", "source"):
        if actual[name] not in {expected, prior[name]}:
            raise ReleaseStoreError(f"managed pointer changed concurrently: {pointers[name].name}")
    if actual["legacy"] not in {None, prior["legacy"]}:
        raise ReleaseStoreError(f"managed pointer changed concurrently: {pointers['legacy'].name}")

    changed: list[tuple[str, Path | None]] = []
    try:
        for name in ("current", "source"):
            if actual[name] == prior[name]:
                continue
            if prior[name] is None:
                _remove(pointers[name])
            else:
                replace_pointer(pointers[name], prior[name], home)
            changed.append((name, actual[name]))
        if actual["legacy"] != prior["legacy"]:
            if prior["legacy"] is not None:
                replace_pointer(pointers["legacy"], prior["legacy"], home)
            changed.append(("legacy", actual["legacy"]))
    except Exception as exc:
        try:
            for name, original in reversed(changed):
                current = _pointer_target(pointers[name])
                if current == original:
                    continue
                if original is None:
                    _remove(pointers[name])
                else:
                    replace_pointer(pointers[name], original, home)
        except Exception as rollback_exc:
            raise ReleaseStoreError("release pointer restoration was not complete") from rollback_exc
        if isinstance(exc, ReleaseStoreError):
            raise
        raise ReleaseStoreError("release pointer restoration failed") from exc


def _restore_pointer_after_activation_failure(
    link: Path,
    previous: Path | None,
    expected: Path,
    home: Path,
) -> None:
    """Restore one pointer only when this activation still owns its new value."""

    actual = _pointer_target(link)
    if actual == previous:
        return
    if actual != expected.resolve(strict=True):
        raise ReleaseStoreError(f"managed pointer changed concurrently: {link.name}")
    if previous is None:
        _remove(link)
    else:
        replace_pointer(link, previous, home)


def activate_release(home: Path, release: Path) -> dict[str, Any]:
    requested_release = release.expanduser()
    if is_redirected_path(requested_release):
        raise ReleaseStoreError("release activation requires a valid immutable managed release")
    try:
        home = validate_unredirected_path(home, label="Tenetora release home")
        release = validate_existing_project_path(requested_release, label="managed release")
    except RuntimeError as exc:
        raise ReleaseStoreError(str(exc)) from exc
    if not _inside_releases(release, home):
        raise ReleaseStoreError("release activation requires a valid immutable managed release")
    read_release_manifest(release)
    prune_python_bytecode(release)
    if not managed_release(release, home):
        raise ReleaseStoreError("release activation requires a valid immutable managed release")
    current = home / "current"
    source = home / "source" / "tenetora"
    legacy = home / "source" / "agent-harness"
    # Validate every replaceable pointer before mutating any of them. The two
    # canonical pointers are updated independently, so activation also needs a
    # compensating rollback if the second filesystem operation fails.
    _assert_pointer_replaceable(current, home)
    _assert_pointer_replaceable(source, home)
    if legacy.exists() or is_redirected_path(legacy):
        _assert_pointer_replaceable(legacy, home)
    previous_current = _pointer_target(current)
    previous_source = _pointer_target(source)
    previous_legacy = _pointer_target(legacy) if legacy.exists() or is_redirected_path(legacy) else None
    previous = {
        "current": str(previous_current) if previous_current is not None else None,
        "source": str(previous_source) if previous_source is not None else None,
        "legacy": str(previous_legacy) if previous_legacy is not None else None,
    }
    try:
        replace_pointer(current, release, home)
        replace_pointer(source, release, home)
        if legacy.exists() or is_redirected_path(legacy):
            _remove(legacy)
    except Exception as exc:
        try:
            _restore_pointer_after_activation_failure(current, previous_current, release, home)
            _restore_pointer_after_activation_failure(source, previous_source, release, home)
            actual_legacy = _pointer_target(legacy)
            if actual_legacy != previous_legacy:
                if actual_legacy is not None:
                    if previous_legacy is None:
                        _remove(legacy)
                    else:
                        raise ReleaseStoreError(f"managed pointer changed concurrently: {legacy.name}")
                elif previous_legacy is not None:
                    replace_pointer(legacy, previous_legacy, home)
        except Exception as rollback_exc:
            raise ReleaseStoreError(
                "release activation failed and pointer rollback was not complete"
            ) from rollback_exc
        raise ReleaseStoreError("release activation failed; prior pointers were restored") from exc
    return {
        "current": str(current),
        "source": str(source),
        "release": str(release),
        "previous": previous,
    }
