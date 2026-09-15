"""Minimum-runtime checks for Python interpreters used by Tenetora."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Sequence


MINIMUM_PYTHON = (3, 9)
INTERPRETER_PROBE_TIMEOUT_SECONDS = 5
_VERSION_PROBE = (
    "import sys; "
    f"raise SystemExit(0 if sys.version_info >= {MINIMUM_PYTHON!r} else 1)"
)


def resolve_hook_interpreter(raw: object) -> str | None:
    """Resolve a hook command name or path to an executable file."""
    if not isinstance(raw, str) or not raw.strip():
        return None

    command = raw.strip()
    resolved = shutil.which(command)
    if resolved is None:
        candidate = Path(command).expanduser()
        if not candidate.is_file():
            return None
        resolved = str(candidate)

    path = Path(resolved)
    if not path.is_file() or not os.access(path, os.X_OK):
        return None
    return str(path)


def python_version_meets_minimum(version_info: Sequence[int]) -> bool:
    """Return whether a Python version satisfies the package runtime floor."""
    return tuple(version_info[:2]) >= MINIMUM_PYTHON


def interpreter_meets_minimum(raw: object) -> bool:
    """Return whether a recorded interpreter satisfies the runtime floor."""
    resolved = resolve_hook_interpreter(raw)
    if resolved is None:
        return False
    try:
        result = subprocess.run(
            [resolved, "-c", _VERSION_PROBE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=INTERPRETER_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0
