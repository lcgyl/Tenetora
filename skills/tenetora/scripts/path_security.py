"""Script-facing import for the canonical Tenetora path validator."""

from __future__ import annotations

import sys
from pathlib import Path


CLI_DIR = Path(__file__).resolve().parents[1] / "cli"
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from tenetora.path_security import (  # noqa: E402
    copy_redirected_entry,
    create_redirected_entry,
    ensure_unredirected_directory,
    harness_missing_message,
    is_allowed_system_redirect,
    is_redirected_path,
    nearest_governed_parent,
    remove_unredirected_entry,
    validate_existing_project_path,
    validate_unredirected_file_path,
    validate_unredirected_replace_target,
    validate_unredirected_path,
)

__all__ = [
    "copy_redirected_entry",
    "create_redirected_entry",
    "ensure_unredirected_directory",
    "harness_missing_message",
    "is_allowed_system_redirect",
    "is_redirected_path",
    "nearest_governed_parent",
    "remove_unredirected_entry",
    "validate_existing_project_path",
    "validate_unredirected_file_path",
    "validate_unredirected_replace_target",
    "validate_unredirected_path",
]
