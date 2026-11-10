"""Conditional filesystem changes with durable isolation of replaced entries."""
from __future__ import annotations

import errno
import ctypes
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

from .path_security import (
    copy_redirected_entry,
    ensure_unredirected_directory,
    is_redirected_path,
    validate_unredirected_path,
)


class ConcurrentEntryError(RuntimeError):
    """An entry no longer has content owned by the current transaction."""


def present(path: Path) -> bool:
    return path.exists() or is_redirected_path(path)


def copy_entry(source: Path, destination: Path, *, ignored=()) -> None:
    """Copy privately, without following redirects or replacing an entry."""
    if is_redirected_path(source):
        copy_redirected_entry(source, destination, label='transaction entry')
    elif source.is_dir():
        destination.mkdir()
        for child in source.iterdir():
            if child not in ignored:
                copy_entry(child, destination / child.name, ignored=ignored)
        shutil.copystat(source, destination, follow_symlinks=False)
    else:
        with source.open('rb') as reader, destination.open('xb') as writer:
            shutil.copyfileobj(reader, writer)
        shutil.copystat(source, destination)


def _rename(source: Path, destination: Path, *, replace: bool) -> None:
    action = os.replace if replace else os.rename
    for attempt in range(5):
        try:
            action(source, destination)
            return
        except PermissionError as exc:
            if os.name != 'nt' or getattr(exc, 'winerror', None) != 5 or attempt == 4:
                raise
            time.sleep(0.05 * (2 ** attempt))


def publish_noreplace(source: Path, target: Path) -> None:
    """Publish complete data without replacing a newly appeared target."""
    validate_unredirected_path(target.parent, label='transaction publication parent')
    if os.name == 'nt':
        # Windows rename does not replace an existing destination.
        _rename(source, target, replace=False)
        return
    if is_redirected_path(source):
        copy_redirected_entry(source, target, label='transaction redirect')
        source.unlink()
    elif source.is_dir():
        _publish_directory_exclusive(source, target)
    else:
        os.link(source, target)
        source.unlink()


def _publish_directory_exclusive(source: Path, target: Path) -> None:
    """Use the kernel's exclusive rename; an empty reservation is insufficient."""
    library = ctypes.CDLL(None, use_errno=True)
    try:
        if sys.platform == 'darwin':
            action = library.renamex_np
            action.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            action.restype = ctypes.c_int
            result = action(os.fsencode(source), os.fsencode(target), 4)  # RENAME_EXCL
        elif sys.platform.startswith('linux'):
            action = library.renameat2
            action.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            action.restype = ctypes.c_int
            result = action(-100, os.fsencode(source), -100, os.fsencode(target), 1)  # AT_FDCWD, RENAME_NOREPLACE
        else:
            raise OSError(errno.ENOTSUP, 'exclusive directory publication is unavailable on this platform')
    except AttributeError as exc:
        raise OSError(errno.ENOTSUP, 'exclusive directory publication is unavailable on this runtime') from exc
    if result:
        failure = ctypes.get_errno()
        raise OSError(failure, os.strerror(failure), str(target))


def _record(path: Path, payload: dict) -> None:
    descriptor, name = tempfile.mkstemp(prefix='.record-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class IsolatedEntry:
    """Capture the actual inode before checking ownership; retain replaced data."""

    def __init__(self, target: Path, storage: Path):
        self.target = Path(os.path.abspath(target.expanduser()))
        validate_unredirected_path(self.target.parent, label='transaction target parent')
        storage = ensure_unredirected_directory(storage, label='transaction isolation storage')
        self.directory = Path(tempfile.mkdtemp(prefix='e-', dir=storage))
        self.saved = self.directory / 'entry'
        self.metadata = self.directory / 'record.json'
        self.record = {'version': 1, 'target': str(self.target), 'saved': str(self.saved), 'state': 'pending'}
        _record(self.metadata, self.record)

    def capture(self, validator):
        def move_entry():
            try:
                _rename(self.target, self.saved, replace=True)
            except FileNotFoundError:
                if present(self.target):
                    raise

        if os.name != 'nt' and self.target.is_dir() and not is_redirected_path(self.target):
            # Check the actual filesystem before detaching a live directory.
            # An older kernel/filesystem must fail without moving user data.
            probe = Path(tempfile.mkdtemp(prefix='.exclusive-', dir=self.target.parent))
            first, occupied = probe / 'first', probe / 'occupied'
            try:
                first.mkdir()
                occupied.mkdir()
                try:
                    _publish_directory_exclusive(first, occupied)
                except FileExistsError:
                    pass
                else:
                    raise OSError(errno.ENOTSUP, 'filesystem does not provide exclusive directory rename')
            finally:
                shutil.rmtree(probe)
        try:
            try:
                move_entry()
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                # A cross-mount rename may report EXDEV before looking up a
                # missing source. Absence is still validated below, and the
                # subsequent publication cannot replace a newly added entry.
                if present(self.target):
                    fallback = Path(tempfile.mkdtemp(prefix='.tenetora-recovery-', dir=self.target.parent))
                    self.directory = fallback
                    self.saved = fallback / 'entry'
                    self.record.update(saved=str(self.saved), state='target-filesystem')
                    _record(self.metadata, self.record)
                    print(f'Upgrade recovery storage: {fallback}', file=sys.stderr)
                    move_entry()
            captured = self.saved if present(self.saved) else None
            if not validator(captured):
                raise ConcurrentEntryError('filesystem entry changed concurrently; its data was preserved')
            return captured
        except BaseException:  # noqa: BLE001 -- restore namespace even for interruption
            self.put_back()
            raise

    def put_back(self) -> bool:
        if not present(self.saved):
            return not present(self.target)
        try:
            publish_noreplace(self.saved, self.target)
            return True
        except (OSError, RuntimeError):
            return False

    def install(self, replacement: Path | None) -> None:
        if replacement is None:
            if present(self.target):
                raise ConcurrentEntryError('a new filesystem entry appeared during removal; it was preserved')
            return
        try:
            publish_noreplace(replacement, self.target)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            # The active path and journal can live on different volumes.
            # Copy only our private replacement, then publish on the target FS.
            local = Path(tempfile.mkdtemp(prefix='.tenetora-publication-', dir=self.target.parent))
            self.record['publication_storage'] = str(local)
            _record(self.metadata, self.record)
            prepared = local / 'entry'
            copy_entry(replacement, prepared)
            publish_noreplace(prepared, self.target)
            local.rmdir()


def retained_entries(storage: Path) -> list[str]:
    result = []
    if not storage.is_dir():
        return result
    for record in storage.glob('*/record.json'):
        try:
            payload = json.loads(record.read_text(encoding='utf-8'))
            saved = Path(payload['saved'])
            if present(saved):
                result.append(str(saved))
        except (OSError, ValueError, KeyError, TypeError):
            # A damaged record is recovery evidence, never cleanup permission.
            result.append(str(record.parent))
    return result
