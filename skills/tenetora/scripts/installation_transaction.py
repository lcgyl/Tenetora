"""Authenticated target inventory and conditional rollback for installer writes.

The worker holds the immutable inventory digest outside the mutable journal.
Installer children can checkpoint only those targets, immediately around their
own mutations. Recovery never adopts a later filesystem observation as a write.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

CLI_DIR = Path(__file__).resolve().parents[1] / 'cli'
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))
from tenetora.file_lock import locked_file  # noqa: E402
from tenetora.file_transaction import (  # noqa: E402
    IsolatedEntry, ConcurrentEntryError, copy_entry, present, retained_entries,
)
from tenetora.path_security import (  # noqa: E402
    is_redirected_path,
    remove_unredirected_entry,
    validate_unredirected_file_path,
    validate_unredirected_path,
    ensure_unredirected_directory as original_ensure_directory,
)

JOURNAL_ENV = 'TENETORA_SURFACE_JOURNAL'
DIGEST_ENV = 'TENETORA_SURFACE_JOURNAL_SHA256'
IS_WINDOWS = os.name == 'nt'
MISSING = hashlib.sha256(b'missing').hexdigest()


def _path(path: Path) -> Path:
    path = Path(os.path.abspath(path.expanduser()))
    validate_unredirected_path(path.parent, label='installation transaction target parent')
    return path


def _redirect(path: Path) -> str:
    try:
        return os.fsdecode(os.readlink(path))
    except OSError:
        return str(path.resolve(strict=True))


def _portable_mode(path: Path) -> int:
    mode = stat.S_IMODE(path.lstat().st_mode)
    if IS_WINDOWS:
        # Windows derives execute bits from the file extension, so a copy under
        # a journal path without .cmd/.exe can read a different mode even when
        # the content is identical. Only the writable attribute is portable.
        mode = 0o200 if mode & 0o200 else 0
    return mode


def fingerprint(path: Path, replacement=None, ignored=()) -> str:
    """Hash content, entry type and permissions, optionally applying one known write."""
    changes = replacement if isinstance(replacement, list) else ([replacement] if replacement is not None else [])
    for target, source in changes:
        if path == target:
            return fingerprint(source) if source is not None else hashlib.sha256(b'missing').hexdigest()
    if not path.exists() and not is_redirected_path(path):
        return hashlib.sha256(b'missing').hexdigest()
    digest = hashlib.sha256(str(_portable_mode(path)).encode() + b'\0')
    if is_redirected_path(path):
        digest.update(b'link\0' + _redirect(path).encode('utf-8'))
    elif path.is_file():
        digest.update(b'file\0')
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
    elif path.is_dir():
        digest.update(b'directory\0')
        children = {child.name: child for child in path.iterdir() if child not in ignored}
        for target, source in changes:
            if target.parent == path:
                if source is None:
                    children.pop(target.name, None)
                else:
                    children[target.name] = target
        for name, child in sorted(children.items()):
            digest.update(name.encode('utf-8') + b'\0' + fingerprint(child, replacement, ignored).encode() + b'\0')
    else:
        raise RuntimeError('installation transaction encountered a special file')
    return digest.hexdigest()


def _copy(source: Path, destination: Path, *, ignored=()) -> None:
    copy_entry(source, destination, ignored=ignored)


def _write(path: Path, payload) -> None:
    validate_unredirected_file_path(path, label='installation transaction metadata')
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
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


def _digest(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def snapshot(journal: Path, targets: list[Path]) -> str:
    journal = validate_unredirected_path(journal, label='installation transaction journal')
    journal.mkdir(parents=True, mode=0o700, exist_ok=False)
    (journal / 'files').mkdir(mode=0o700)
    unique = sorted({_path(path) for path in targets}, key=lambda item: (len(item.parts), str(item)))
    # A declared directory already owns its descendants; never snapshot or
    # restore an entry twice in different orders.
    roots = []
    for path in unique:
        if not any(root in path.parents for root in roots):
            roots.append(path)
    entries = []
    for index, path in enumerate(roots):
        before = fingerprint(path)
        present = path.exists() or is_redirected_path(path)
        if present:
            _copy(path, journal / 'files' / str(index))
            if fingerprint(journal / 'files' / str(index)) != before:
                raise RuntimeError(f'installation target changed while being snapshotted: {path}')
        entries.append({'path': str(path), 'before': before, 'present': present})
    inventory = {'version': 1, 'entries': entries}
    _write(journal / 'inventory.json', inventory)
    _write(journal / 'checkpoints.json', {str(i): [entry['before']] for i, entry in enumerate(entries)})
    return _digest(inventory)


class Journal:
    def __init__(self, root: Path, expected_digest: str):
        self.root = validate_unredirected_path(root, label='installation transaction journal')
        validate_unredirected_file_path(self.root / 'inventory.json', label='installation transaction inventory')
        self.inventory = json.loads((self.root / 'inventory.json').read_text(encoding='utf-8'))
        if not expected_digest or _digest(self.inventory) != expected_digest or self.inventory.get('version') != 1:
            raise RuntimeError('installation transaction inventory changed')
        self.entries = self.inventory['entries']
        self.active = set()
        self.private = set()

    def _checkpoints(self):
        path = validate_unredirected_file_path(self.root / 'checkpoints.json', label='installation transaction checkpoints')
        data = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(data, dict) or set(data) != {str(i) for i in range(len(self.entries))}
                or any(not isinstance(value, list) or not value for value in data.values())):
            raise RuntimeError('installation transaction checkpoints are invalid')
        return data

    def _index(self, path):
        if any(root == path or root in path.parents for root in self.private):
            return None
        for index, entry in enumerate(self.entries):
            root = Path(entry['path'])
            if path == root or root in path.parents:
                return index
        return None

    @contextmanager
    def operation(self, targets):
        indices = set()
        for target in targets:
            index = self._index(_path(target))
            if index is None:
                raise RuntimeError('installer write target was not included in the upgrade snapshot')
            indices.add(index)
        owned = indices - self.active
        with locked_file(self.root / '.lock'):
            checkpoints = self._checkpoints()
            for index in owned:
                if fingerprint(_path(Path(self.entries[index]['path'])), ignored=self.private) not in checkpoints[str(index)]:
                    raise RuntimeError('installation target changed after the latest transaction checkpoint')
        self.active.update(owned)
        try:
            yield
        finally:
            # An observation after a broad operation cannot prove who wrote a
            # file. Only prepare() at an actual mutation records owned states.
            self.active.difference_update(owned)

    def _remember(self, index, *values):
        checkpoints = self._checkpoints()
        for value in values:
            if value not in checkpoints[str(index)]:
                checkpoints[str(index)].append(value)
        _write(self.root / 'checkpoints.json', checkpoints)

    def _ignored_in(self, root, captured, staged=None):
        paths = self.private | ({Path(staged)} if staged is not None else set())
        result = set()
        if captured is not None:
            for path in paths:
                try:
                    result.add(captured / path.relative_to(root))
                except ValueError:
                    pass
        return result

    def _change(self, target, staged):
        target = _path(Path(target))
        index = self._index(target)
        if index is None:
            if staged is None:
                remove_unredirected_entry(target, label='private installation entry')
            else:
                os.replace(staged, target)
            return
        with locked_file(self.root / '.lock'):
            root = _path(Path(self.entries[index]['path']))
            allowed = self._checkpoints()[str(index)]
            token = IsolatedEntry(root, self.root / 'after-images')
            ignored = set()
            captured_fingerprint = None

            def owns(captured):
                nonlocal ignored, captured_fingerprint
                ignored = self._ignored_in(root, captured, staged)
                captured_fingerprint = fingerprint(captured, ignored=ignored) if captured is not None else MISSING
                return captured_fingerprint in allowed

            captured = token.capture(owns)
            try:
                source = Path(staged) if staged is not None else None
                if source is not None and captured is not None:
                    try:
                        source = captured / source.relative_to(root)
                    except ValueError:
                        pass
                if target == root:
                    replacement = source
                else:
                    replacement = token.directory / 'working'
                    if captured is None:
                        replacement.mkdir()
                    else:
                        _copy(captured, replacement, ignored=ignored)
                    changed = replacement / target.relative_to(root)
                    remove_unredirected_entry(changed, label='private transaction replacement')
                    if source is not None:
                        changed.parent.mkdir(parents=True, exist_ok=True)
                        _copy(source, changed)
                expected = fingerprint(replacement) if replacement is not None else MISSING
                if captured is not None and fingerprint(captured, ignored=ignored) != captured_fingerprint:
                    raise ConcurrentEntryError('isolated entry changed while preparing the replacement; its data was preserved')
                # Both the detached namespace and the explicit replacement are
                # our states. No observation of an unvalidated active path is
                # ever added to the receipt.
                self._remember(index, MISSING, expected)
                token.install(replacement)
                if fingerprint(root) != expected:
                    raise ConcurrentEntryError('filesystem entry changed after publication; its data was preserved')
            except BaseException:  # noqa: BLE001 -- preserve captured data on any aborted mutation
                if not present(root):
                    token.put_back()
                raise

    def publish(self, staged, target):
        self._change(target, Path(staged))

    def remove(self, target):
        self._change(target, None)

    def mkdir(self, target, staged):
        self._change(target, staged)

    def move(self, source, destination):
        source, destination = _path(Path(source)), _path(Path(destination))
        index = self._index(source)
        if index is None:
            temporary = Path(tempfile.mkdtemp(prefix='move-', dir=self.root))
            staged = temporary / 'value'
            _copy(source, staged)
        else:
            with locked_file(self.root / '.lock'):
                root = _path(Path(self.entries[index]['path']))
                allowed = self._checkpoints()[str(index)]
                token = IsolatedEntry(root, self.root / 'after-images')
                captured = token.capture(lambda entry: (fingerprint(entry) if entry is not None else MISSING) in allowed)
                if captured is None:
                    raise FileNotFoundError(str(source))
                value = fingerprint(captured)
                temporary = Path(tempfile.mkdtemp(prefix='move-', dir=self.root))
                staged = temporary / 'value'
                try:
                    _copy(captured / source.relative_to(root), staged)
                    if fingerprint(captured) != value:
                        raise ConcurrentEntryError('move source changed concurrently; its data was preserved')
                finally:
                    if not token.put_back():
                        raise ConcurrentEntryError('move source could not be returned without overwriting another entry')
        self.publish(staged, destination)
        self.remove(source)

    def accept_verified(self, target, verifier):
        """Attribute an external producer's change only after semantic proof.

        Unknown formats or changes outside the producer's owned fields remain
        unclaimed, so recovery preserves them and reports a conflict.
        """
        path = _path(Path(target))
        index = self._index(path)
        if index is None or path != Path(self.entries[index]['path']):
            return False
        entry = self.entries[index]
        backup = self.root / 'files' / str(index)
        if is_redirected_path(path) or is_redirected_path(backup):
            return False
        if entry['present'] and fingerprint(backup) != entry['before']:
            return False
        expected = fingerprint(path)
        if expected == entry['before']:
            return True
        if not verifier(backup if entry['present'] else None, path):
            return False
        if entry['present'] and path.exists() and backup.exists():
            if _portable_mode(path) != _portable_mode(backup):
                return False
        with locked_file(self.root / '.lock'):
            if fingerprint(path) != expected:
                return False
            checkpoints = self._checkpoints()
            if expected not in checkpoints[str(index)]:
                checkpoints[str(index)].append(expected)
                _write(self.root / 'checkpoints.json', checkpoints)
        return True

    def restore(self):
        restored, conflicts = [], []
        with locked_file(self.root / '.lock'):
            checkpoints = self._checkpoints()
            for index, entry in reversed(list(enumerate(self.entries))):
                token = None
                try:
                    path = _path(Path(entry['path']))
                    if fingerprint(path) == entry['before']:
                        continue
                    backup = self.root / 'files' / str(index)
                    validate_unredirected_path(backup.parent, label='installation backup parent')
                    if entry['present'] and fingerprint(backup) != entry['before']:
                        conflicts.append(entry['path'])
                        continue
                    token = IsolatedEntry(path, self.root / 'after-images')
                    staged = token.directory / 'replacement'
                    if entry['present']:
                        _copy(backup, staged)
                        if fingerprint(staged) != entry['before']:
                            raise ConcurrentEntryError('rollback backup changed while being copied')
                    verified = []
                    def owns(current, _verified=verified, _index=index):
                        value = fingerprint(current) if current is not None else MISSING
                        _verified.append(value)
                        return value in checkpoints[str(_index)]
                    captured = token.capture(owns)
                    captured_value = verified[-1]
                    if captured is not None and fingerprint(captured) != captured_value:
                        raise ConcurrentEntryError('isolated rollback entry changed concurrently')
                    token.install(staged if entry['present'] else None)
                    if fingerprint(path) != entry['before']:
                        raise ConcurrentEntryError('installation rollback verification failed')
                    restored.append(entry['path'])
                except (OSError, RuntimeError):
                    if token is not None and not present(Path(entry['path'])):
                        token.put_back()
                    conflicts.append(entry['path'])
        return {'status': 'blocked' if conflicts else 'restored', 'restored': restored, 'conflicts': conflicts,
                'retained': retained_entries(self.root / 'after-images')}


_cached = None


def environment_journal():
    global _cached
    path, digest = os.environ.get(JOURNAL_ENV), os.environ.get(DIGEST_ENV)
    if not path and not digest:
        return None
    if not path or not digest:
        raise RuntimeError('installation transaction environment is incomplete')
    if _cached is None or _cached[0] != (path, digest):
        _cached = ((path, digest), Journal(Path(path), digest))
    return _cached[1]


@contextmanager
def operation(targets):
    journal = environment_journal()
    if journal is None:
        yield
    else:
        with journal.operation(targets):
            yield


def publish(staged: Path, target: Path) -> None:
    journal = environment_journal()
    if journal is not None:
        journal.publish(staged, target)
    else:
        os.replace(staged, target)


def remove(target: Path) -> None:
    journal = environment_journal()
    if journal is not None:
        journal.remove(target)
    else:
        remove_unredirected_entry(target, label='managed installation entry')


def backup_and_remove(source: Path, destination: Path) -> None:
    journal = environment_journal()
    if journal is not None:
        journal.move(source, destination)
    else:
        shutil.move(str(source), str(destination))


@contextmanager
def private_staging(path):
    journal = environment_journal()
    root = Path(path)
    if journal is not None:
        journal.private.add(root)
    try:
        yield
    finally:
        if journal is not None:
            journal.private.discard(root)


def ensure_directory(path, *, label):
    journal = environment_journal()
    if journal is None:
        return original_ensure_directory(path, label=label)
    path = Path(path)
    if present(path):
        return original_ensure_directory(path, label=label)
    if not path.parent.exists():
        ensure_directory(path.parent, label=label)
    if journal._index(_path(path)) is None:
        return original_ensure_directory(path, label=label)
    staged = Path(tempfile.mkdtemp(prefix='.tenetora-directory-', dir=path.parent))
    staged.rmdir()
    try:
        with private_staging(staged):
            original_ensure_directory(staged, label=label)
            journal.mkdir(path, staged)
        return path
    finally:
        if staged.exists():
            staged.rmdir()


def journaled(resolver):
    """Wrap a real installer operation; preflight/dry-run remains read-only."""
    def decorate(function):
        signature = inspect.signature(function)
        @wraps(function)
        def call(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            journal = environment_journal()
            if journal is None or bound.arguments.get('dry_run', False):
                return function(*args, **kwargs)
            with journal.operation(resolver(bound.arguments)):
                return function(*args, **kwargs)
        return call
    return decorate
