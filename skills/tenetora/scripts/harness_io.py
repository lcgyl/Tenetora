"""Shared filesystem helpers for Tenetora scripts."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from path_security import ensure_unredirected_directory, validate_unredirected_file_path  # noqa: E402


def _safe_path(path: Path, label: str) -> Path:
    try:
        return validate_unredirected_file_path(path, label=label)
    except RuntimeError:
        raise


def atomic_write_bytes(path: Path, content: bytes, mode: int | None = None) -> None:
    """Replace a file without following a redirected parent or target."""
    path = _safe_path(path, "atomic file")
    ensure_unredirected_directory(path.parent, label="atomic file parent")
    try:
        existing_mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        existing_mode = mode
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if existing_mode is not None:
            os.chmod(tmp_name, existing_mode)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write text without leaving a partially-written target on interruption."""
    atomic_write_bytes(path, text.encode(encoding), mode=None)
