"""Small cross-platform advisory file lock used by machine-level mutations."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    from .path_security import validate_unredirected_file_path
except ImportError:  # Loaded directly by compatibility scripts and tests.
    from path_security import validate_unredirected_file_path
try:
    from .path_security import ensure_unredirected_directory
except ImportError:  # Loaded directly by compatibility scripts and tests.
    from path_security import ensure_unredirected_directory


@contextmanager
def locked_file(path: Path) -> Iterator[object]:
    path = validate_unredirected_file_path(path, label="lock file")
    ensure_unredirected_directory(path.parent, label="lock file parent")
    with path.open("a+b") as handle:
        try:
            import fcntl
        except ImportError:
            import msvcrt

            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield handle
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield handle
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
