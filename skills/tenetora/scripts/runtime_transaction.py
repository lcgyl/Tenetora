#!/usr/bin/env python3
"""Snapshot and conditionally restore the managed CLI runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar


SCRIPT_DIR = Path(__file__).resolve().parent
CLI_DIR = SCRIPT_DIR.parent / "cli"
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from tenetora.file_lock import locked_file  # noqa: E402
from tenetora.path_security import (  # noqa: E402
    ensure_unredirected_directory,
    copy_redirected_entry,
    is_redirected_path,
    remove_unredirected_entry,
    validate_unredirected_file_path,
    validate_unredirected_path,
)


T = TypeVar("T")
JOURNAL_VERSION = 1


def command_suffix() -> str:
    return ".cmd" if os.name == "nt" else ""


def managed_targets(bin_dir: Path) -> tuple[Path, ...]:
    root = validate_unredirected_path(bin_dir, label="managed runtime bin directory")
    suffix = command_suffix()
    return (
        root / f"tenetora{suffix}",
        root / f"agent-harness{suffix}",
        root.parent / "runtime",
        root.parent / "current",
        root.parent / "source" / "tenetora",
        root.parent / "source" / "agent-harness",
    )


def target_names(bin_dir: Path) -> dict[str, Path]:
    bin_root = validate_unredirected_path(bin_dir, label="managed runtime bin directory")
    root = bin_root.parent
    return {
        "tenetora": bin_root / f"tenetora{command_suffix()}",
        "agent-harness": bin_root / f"agent-harness{command_suffix()}",
        "runtime": root / "runtime",
        "current": root / "current",
        "source/tenetora": root / "source" / "tenetora",
        "source/agent-harness": root / "source" / "agent-harness",
    }


def _target_name(bin_dir: Path, target: Path) -> str | None:
    for name, candidate in target_names(bin_dir).items():
        if candidate == target:
            return name
    return None


def _points_to(target: Path, destination: Path) -> bool:
    if not (target.exists() or is_redirected_path(target)):
        return False
    try:
        return target.resolve(strict=False) == destination.expanduser().absolute().resolve(strict=False)
    except OSError:
        return False


def _redirect_target(path: Path, *, strict: bool = False) -> str:
    try:
        return os.fsdecode(os.readlink(path))
    except OSError:
        try:
            return str(path.resolve(strict=strict))
        except OSError as exc:
            raise RuntimeError(f"cannot resolve redirected runtime entry: {path}") from exc


def entry_digest(path: Path) -> str:
    if is_redirected_path(path):
        try:
            target = _redirect_target(path)
        except RuntimeError:
            target = str(path)
        return hashlib.sha256(("link\0" + target).encode("utf-8")).hexdigest()
    if path.is_file():
        return hashlib.sha256(b"file\0" + path.read_bytes()).hexdigest()
    if not path.exists():
        return ""
    digest = hashlib.sha256(b"directory\0")
    for item in sorted(path.rglob("*"), key=lambda candidate: candidate.as_posix()):
        relative = item.relative_to(path).as_posix().encode("utf-8")
        if is_redirected_path(item):
            try:
                target = _redirect_target(item)
            except RuntimeError:
                target = str(item)
            digest.update(b"link\0" + relative + b"\0" + target.encode("utf-8") + b"\0")
        elif item.is_file():
            digest.update(b"file\0" + relative + b"\0" + item.read_bytes() + b"\0")
        elif item.is_dir():
            digest.update(b"directory\0" + relative + b"\0")
    return digest.hexdigest()


def _copy_entry(source: Path, destination: Path) -> None:
    ensure_unredirected_directory(destination.parent, label="runtime rollback destination directory")
    if is_redirected_path(destination):
        raise RuntimeError(f"runtime rollback destination is a symbolic link or junction: {destination}")
    if is_redirected_path(source):
        copy_redirected_entry(source, destination, label="runtime rollback entry")
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination)


def _remove_entry(path: Path) -> None:
    remove_unredirected_entry(path, label="managed runtime entry")


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    path = validate_unredirected_file_path(path, label="runtime rollback manifest")
    ensure_unredirected_directory(path.parent, label="runtime rollback journal directory")
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_manifest(journal: Path) -> dict[str, object]:
    journal = validate_unredirected_path(journal, label="runtime rollback journal")
    path = validate_unredirected_file_path(journal / "manifest.json", label="runtime rollback manifest")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"managed runtime rollback journal is unreadable: {path}") from exc
    if not isinstance(payload, dict) or payload.get("version") != JOURNAL_VERSION:
        raise RuntimeError(f"managed runtime rollback journal is invalid: {path}")
    entries = payload.get("files")
    if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
        raise RuntimeError(f"managed runtime rollback journal has invalid entries: {path}")
    return payload


def _target_set(entries: list[dict[str, object]]) -> set[str]:
    return {str(item.get("target") or "") for item in entries}


def _validate_manifest_targets(entries: list[dict[str, object]], bin_dir: Path) -> tuple[Path, ...]:
    expected = managed_targets(bin_dir)
    actual = [str(item.get("target") or "") for item in entries]
    if len(entries) != len(expected) or actual != [str(path) for path in expected]:
        raise RuntimeError("managed runtime rollback journal has an unexpected target set")
    for target in expected:
        validate_unredirected_path(target.parent, label="managed runtime target parent")
    return expected


def _backup_path_is_within_journal(backup: Path, journal: Path) -> bool:
    """Validate backup storage without allowing a redirected parent."""

    try:
        validate_unredirected_path(backup.parent, label="runtime rollback backup parent")
        backup.parent.resolve(strict=False).relative_to(journal.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return False
    return True


POINTER_TARGET_NAMES = {"current", "source/tenetora", "source/agent-harness"}


def _within_root(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, ValueError):
        return False
    return True


def _allowed_pointer_target(target: Path, name: str, root: Path) -> bool:
    """Accept only canonical releases or the known legacy harness release root."""

    if name not in POINTER_TARGET_NAMES or not is_redirected_path(target):
        return False
    try:
        resolved = target.resolve(strict=True)
        allowed_roots = [root / "releases"]
        if name == "source/agent-harness":
            allowed_roots.append(root.parent / ".agent-harness" / "releases")
        for allowed_root in allowed_roots:
            validate_unredirected_path(allowed_root, label="managed pointer release root")
            if _within_root(resolved, allowed_root) and resolved.is_dir():
                return True
    except (OSError, RuntimeError):
        return False
    return False


def _directory_contains_redirect(path: Path) -> bool:
    try:
        entries = list(path.rglob("*"))
    except OSError:
        return True
    return any(is_redirected_path(entry) for entry in entries)


def _managed_target_is_safe(target: Path, name: str, root: Path) -> bool:
    try:
        validate_unredirected_path(target.parent, label="managed runtime target parent")
    except RuntimeError:
        return False
    if is_redirected_path(target):
        return (
            _allowed_pointer_target(target, name, root)
        )
    if not (target.exists() or target.is_symlink()):
        return True
    if not (target.is_file() or target.is_dir()):
        return False
    return not target.is_dir() or not _directory_contains_redirect(target)


def _backup_is_safe(backup: Path, name: str, root: Path, journal: Path) -> bool:
    if not _backup_path_is_within_journal(backup, journal):
        return False
    if is_redirected_path(backup):
        return (
            _allowed_pointer_target(backup, name, root)
        )
    if backup.is_file():
        return True
    if backup.is_dir():
        return not _directory_contains_redirect(backup)
    return False


@contextmanager
def runtime_operation_lock(bin_dir: Path) -> Iterator[None]:
    root = validate_unredirected_path(bin_dir, label="managed runtime bin directory").parent
    lock_path = root / ".managed-runtime.lock"
    primary_stack = ExitStack()
    try:
        primary_stack.enter_context(locked_file(lock_path))
    except OSError:
        primary_stack.close()
        fallback = Path(tempfile.gettempdir()) / (
            "tenetora-runtime-" + hashlib.sha256(str(root).encode("utf-8")).hexdigest() + ".lock"
        )
        with locked_file(fallback):
            yield
    else:
        try:
            yield
        finally:
            primary_stack.close()


def snapshot_runtime(bin_dir: Path, journal: Path) -> dict[str, object]:
    # Validate all caller-controlled paths before creating any rollback state.
    validate_unredirected_path(bin_dir, label="managed runtime bin directory")
    validate_unredirected_path(journal, label="runtime rollback journal")
    journal = ensure_unredirected_directory(journal, label="runtime rollback journal")
    manifest = validate_unredirected_file_path(journal / "manifest.json", label="runtime rollback manifest")
    if manifest.exists():
        raise RuntimeError(f"managed runtime rollback journal already exists: {manifest}")
    backup_root = ensure_unredirected_directory(journal / "files", label="runtime rollback backup directory")
    entries: list[dict[str, object]] = []
    with runtime_operation_lock(bin_dir):
        root = validate_unredirected_path(bin_dir, label="managed runtime bin directory").parent
        for index, target in enumerate(managed_targets(bin_dir)):
            name = _target_name(bin_dir, target) or str(target)
            if not _managed_target_is_safe(target, name, root):
                raise RuntimeError(f"managed runtime target is a symbolic link, junction, or unsafe tree: {target}")
            present = target.exists() or is_redirected_path(target)
            backup = backup_root / f"{index:02d}"
            if present:
                _copy_entry(target, backup)
            before = entry_digest(target)
            entries.append(
                {
                    "target": str(target),
                    "backup": str(backup) if present else None,
                    "before_exists": present,
                    "before_sha256": before,
                    "after_sha256": before,
                }
            )
        _write_manifest(
            manifest,
            {"version": JOURNAL_VERSION, "status": "snapshotted", "files": entries},
        )
    return {"status": "snapshotted", "file_count": len(entries)}


def run_runtime_update(bin_dir: Path, journal: Path | None, action: Callable[[], T]) -> T:
    if journal is not None:
        journal = validate_unredirected_path(journal, label="runtime rollback journal")
    with runtime_operation_lock(bin_dir):
        payload: dict[str, object] | None = None
        entries: list[dict[str, object]] = []
        if journal is not None:
            payload = _read_manifest(journal)
            entries = [item for item in payload["files"] if isinstance(item, dict)]
            _validate_manifest_targets(entries, bin_dir)
            conflicts = [
                str(item.get("target") or "")
                for item in entries
                if entry_digest(Path(str(item.get("target") or ""))) != str(item.get("after_sha256") or "")
            ]
            if conflicts:
                raise RuntimeError("managed runtime changed after the latest transaction checkpoint; refusing to overwrite it")
        try:
            return action()
        finally:
            if journal is not None and payload is not None:
                for item in entries:
                    item["after_sha256"] = entry_digest(Path(str(item.get("target") or "")))
                _write_manifest(
                    journal / "manifest.json",
                    {"version": JOURNAL_VERSION, "status": "converged", "files": entries},
                )


def restore_runtime(bin_dir: Path, journal: Path) -> dict[str, object]:
    journal = validate_unredirected_path(journal, label="runtime rollback journal")
    restored: list[str] = []
    conflicts: list[str] = []
    with runtime_operation_lock(bin_dir):
        payload = _read_manifest(journal)
        entries = [item for item in payload["files"] if isinstance(item, dict)]
        try:
            _validate_manifest_targets(entries, bin_dir)
        except RuntimeError:
            return {"status": "blocked", "restored": [], "conflicts": ["target-set"]}
        root = validate_unredirected_path(bin_dir, label="managed runtime bin directory").parent
        pending: list[tuple[Path, bool, Path | None]] = []
        for item in entries:
            target = Path(str(item.get("target") or ""))
            before = str(item.get("before_sha256") or "")
            after = str(item.get("after_sha256") or "")
            current = entry_digest(target)
            if current == before:
                continue
            if current != after:
                conflicts.append(str(target))
                continue
            present = bool(item.get("before_exists"))
            backup_value = item.get("backup")
            backup = Path(str(backup_value)) if isinstance(backup_value, str) else None
            if present:
                if backup is None or not (backup.exists() or is_redirected_path(backup)):
                    conflicts.append(str(target))
                    continue
                name = _target_name(bin_dir, target) or str(target)
                if not _backup_is_safe(backup, name, root, journal):
                    conflicts.append(str(target))
                    continue
            pending.append((target, present, backup))
        if conflicts:
            return {"status": "blocked", "restored": [], "conflicts": conflicts}
        if not pending:
            return {"status": "restored", "restored": [], "conflicts": []}

        # Keep an ephemeral copy of the post-update state so a filesystem
        # error cannot leave earlier targets restored while later targets are
        # still at the managed update.
        restore_snapshot_root = Path(
            tempfile.mkdtemp(prefix=".restore-", dir=journal)
        )
        restore_snapshots: list[tuple[Path, bool, Path | None]] = []
        try:
            try:
                for index, (target, _present, _backup) in enumerate(pending):
                    current_present = target.exists() or is_redirected_path(target)
                    snapshot = restore_snapshot_root / f"{index:02d}"
                    if current_present:
                        _copy_entry(target, snapshot)
                    restore_snapshots.append((target, current_present, snapshot))
            except Exception:
                return {
                    "status": "blocked",
                    "restored": [],
                    "conflicts": ["restore-snapshot"],
                }

            try:
                for target, present, backup in pending:
                    _remove_entry(target)
                    if present and backup is not None:
                        _copy_entry(backup, target)
                    restored.append(str(target))
            except Exception:
                try:
                    for target, current_present, snapshot in reversed(restore_snapshots):
                        _remove_entry(target)
                        if current_present:
                            _copy_entry(snapshot, target)
                except Exception:
                    return {
                        "status": "blocked",
                        "restored": [],
                        "conflicts": ["restore-incomplete"],
                    }
                return {
                    "status": "blocked",
                    "restored": [],
                    "conflicts": ["restore-apply"],
                }
        finally:
            shutil.rmtree(restore_snapshot_root, ignore_errors=True)
    return {"status": "restored", "restored": restored, "conflicts": []}


def checkpoint_runtime(
    bin_dir: Path,
    journal: Path,
    *,
    expected_links: dict[str, Path] | None = None,
    expected_absent: set[str] | None = None,
) -> dict[str, object]:
    """Record a post-activation state without blessing unrelated edits."""

    journal = validate_unredirected_path(journal, label="runtime rollback journal")
    with runtime_operation_lock(bin_dir):
        payload = _read_manifest(journal)
        entries = [item for item in payload["files"] if isinstance(item, dict)]
        try:
            _validate_manifest_targets(entries, bin_dir)
        except RuntimeError:
            return {"status": "blocked", "conflicts": ["target-set"]}
        expected_links = expected_links or {}
        expected_absent = expected_absent or set()
        conflicts: list[str] = []
        root = validate_unredirected_path(bin_dir, label="managed runtime bin directory").parent
        for name, destination in expected_links.items():
            if name not in target_names(bin_dir):
                conflicts.append(name)
                continue
            try:
                expected_path = validate_unredirected_path(destination, label="expected managed runtime link")
            except RuntimeError:
                conflicts.append(name)
                continue
            if not expected_path.exists() or not expected_path.is_dir():
                conflicts.append(name)
        for item in entries:
            target = Path(str(item.get("target") or ""))
            current = entry_digest(target)
            previous = str(item.get("after_sha256") or "")
            if current != previous:
                name = _target_name(bin_dir, target)
                accepted = False
                if name in expected_links and _points_to(target, expected_links[name]):
                    accepted = True
                if name in expected_absent and current == "":
                    accepted = True
                if not accepted:
                    conflicts.append(name or str(target))
                    continue
            item["after_sha256"] = current
        if conflicts:
            return {"status": "blocked", "conflicts": conflicts}
        _write_manifest(
            journal / "manifest.json",
            {"version": JOURNAL_VERSION, "status": "checkpointed", "files": entries},
        )
    return {
        "status": "checkpointed",
        "file_count": len(entries),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--help", action="help")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--snapshot", action="store_true")
    actions.add_argument("--restore", action="store_true")
    actions.add_argument("--checkpoint", action="store_true")
    parser.add_argument("--bin-dir", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--expect-link", action="append", default=[], metavar="TARGET=PATH")
    parser.add_argument("--expect-absent", action="append", default=[], metavar="TARGET")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.snapshot:
        result = snapshot_runtime(args.bin_dir, args.journal)
    elif args.restore:
        result = restore_runtime(args.bin_dir, args.journal)
    else:
        expected_links: dict[str, Path] = {}
        for raw in args.expect_link:
            name, separator, value = str(raw).partition("=")
            if not separator or name not in target_names(args.bin_dir) or not value:
                parser.error("--expect-link must use a managed target name and path")
            expected_links[name] = Path(value)
        expected_absent = set(args.expect_absent)
        unknown = expected_absent - set(target_names(args.bin_dir))
        if unknown:
            parser.error("--expect-absent must use a managed target name")
        result = checkpoint_runtime(
            args.bin_dir,
            args.journal,
            expected_links=expected_links,
            expected_absent=expected_absent,
        )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Tenetora managed runtime: {result['status']}")
    return 1 if result["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
