"""Tenetora CLI package."""

from __future__ import annotations

from importlib import metadata
from pathlib import Path


def _version_from_file() -> str | None:
    for parent in Path(__file__).resolve().parents:
        candidates = [
            parent / "VERSION",
            parent / "skills" / "tenetora" / "VERSION",
        ]
        for candidate in candidates:
            if candidate.is_file():
                value = candidate.read_text(encoding="utf-8").strip()
                if value:
                    return value
    return None


def _installed_version() -> str:
    for distribution in ("tenetora",):
        try:
            return metadata.version(distribution)
        except metadata.PackageNotFoundError:
            continue
    return "0.0.0+unknown"


__version__ = _version_from_file() or _installed_version()
