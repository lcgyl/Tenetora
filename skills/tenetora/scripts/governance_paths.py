#!/usr/bin/env python3
"""Canonical Tenetora project-directory and manifest contract."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


CLI_DIR = Path(__file__).resolve().parents[1] / "cli"
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from tenetora.path_security import validate_unredirected_file_path  # noqa: E402


CANONICAL_DIR_NAME = ".tenetora"
LEGACY_DIR_NAME = ".harness"
MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA_VERSION = 1
LAYOUT_VERSION = 1
PRODUCT_ID = "tenetora"
PRODUCT_NAME = "Tenetora"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_dir(root: Path) -> Path:
    return root.resolve() / CANONICAL_DIR_NAME


def legacy_dir(root: Path) -> Path:
    return root.resolve() / LEGACY_DIR_NAME


def manifest_path(root_or_directory: Path) -> Path:
    path = root_or_directory.resolve()
    directory = path if path.name == CANONICAL_DIR_NAME else path / CANONICAL_DIR_NAME
    return directory / MANIFEST_NAME


def package_version(scripts_dir: Path) -> str:
    version_file = scripts_dir.resolve().parent / "VERSION"
    if version_file.is_file():
        value = version_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    return "unknown"


def read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def read_manifest(directory: Path) -> dict[str, Any] | None:
    return read_json_object(directory / MANIFEST_NAME)


def manifest_errors(directory: Path) -> list[str]:
    payload = read_manifest(directory)
    if payload is None:
        return [f"missing or invalid {MANIFEST_NAME}"]
    errors: list[str] = []
    if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        errors.append("unsupported manifest schema_version")
    if payload.get("product") != PRODUCT_ID:
        errors.append("manifest product is not tenetora")
    if payload.get("layout") != CANONICAL_DIR_NAME:
        errors.append("manifest layout is not .tenetora")
    if payload.get("layout_version") != LAYOUT_VERSION:
        errors.append("unsupported layout_version")
    project_id = payload.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        errors.append("manifest project_id is missing")
    ownership = payload.get("ownership")
    if not isinstance(ownership, dict) or ownership.get("ecosystem") != PRODUCT_ID:
        errors.append("manifest ownership ecosystem is not tenetora")
    return errors


def build_manifest(
    *,
    version: str,
    project_id: str | None = None,
    created_at: str | None = None,
    migrated_from: str | None = None,
    classification: str | None = None,
    evidence_families: Iterable[str] = (),
    source_fingerprint: str | None = None,
) -> dict[str, Any]:
    now = utc_now()
    payload: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "product": PRODUCT_ID,
        "product_name": PRODUCT_NAME,
        "layout": CANONICAL_DIR_NAME,
        "layout_version": LAYOUT_VERSION,
        "project_id": project_id or f"tn-{uuid.uuid4().hex[:20]}",
        "created_at": created_at or now,
        "updated_at": now,
        "created_by_version": version,
        "ownership": {
            "ecosystem": PRODUCT_ID,
            "project_content_policy": "preserve-project-owned",
            "managed_content_policy": "replace-only-while-exact-default",
        },
    }
    if migrated_from:
        payload["migration"] = {
            "source_layout": migrated_from,
            "classification": classification or "legacy-owned",
            "evidence_families": sorted(set(evidence_families)),
            "source_fingerprint": source_fingerprint or "",
            "migrated_at": now,
        }
    return payload


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path = validate_unredirected_file_path(path, label="governance JSON file")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def ensure_manifest(
    directory: Path,
    *,
    version: str,
    project_id: str | None = None,
    migrated_from: str | None = None,
    classification: str | None = None,
    evidence_families: Iterable[str] = (),
    source_fingerprint: str | None = None,
) -> dict[str, Any]:
    existing = read_manifest(directory)
    if existing and not manifest_errors(directory):
        existing = dict(existing)
        existing["updated_at"] = utc_now()
        existing["created_by_version"] = version
        write_json_atomic(directory / MANIFEST_NAME, existing)
        return existing
    payload = build_manifest(
        version=version,
        project_id=project_id,
        migrated_from=migrated_from,
        classification=classification,
        evidence_families=evidence_families,
        source_fingerprint=source_fingerprint,
    )
    write_json_atomic(directory / MANIFEST_NAME, payload)
    return payload
