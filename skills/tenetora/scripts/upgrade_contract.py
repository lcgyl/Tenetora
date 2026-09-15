#!/usr/bin/env python3
"""Versioned release and historical-upgrade contract for Tenetora."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any


BRIDGE_PROTOCOL = 1
INSTALLER_PROTOCOL = 3
MINIMUM_BRIDGE_VERSION = "0.2.15"
RELEASE_FORMAT = "tenetora-release-zip-v1"
UPGRADE_RESULTS = {"READY", "FULL", "PENDING_TRUST", "PARTIAL", "BLOCKED", "DRY_RUN"}
REQUIRED_MANIFEST_FIELDS = {
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
}
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class UpgradeContractError(RuntimeError):
    """Raised when a release or bridge contract is incomplete or unsafe."""


def version_key(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value.strip()):
        raise UpgradeContractError("release version must use MAJOR.MINOR.PATCH")
    major, minor, patch = value.strip().split(".")
    return int(major), int(minor), int(patch)


def normalize_manifest(payload: object, *, require_artifact: bool) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise UpgradeContractError("release manifest must be a JSON object")
    missing = sorted(REQUIRED_MANIFEST_FIELDS - set(payload))
    if missing:
        raise UpgradeContractError(f"release manifest is missing required fields: {', '.join(missing)}")
    if payload.get("name") != "tenetora" or payload.get("format") != RELEASE_FORMAT:
        raise UpgradeContractError("release manifest identity or format is invalid")
    if not isinstance(payload.get("commit"), str) or not str(payload["commit"]).strip():
        raise UpgradeContractError("release commit identity is invalid")
    version_key(str(payload.get("version") or ""))
    version_key(str(payload.get("minimum_bridge_version") or ""))
    if type(payload.get("installer_protocol")) is not int or int(payload["installer_protocol"]) < INSTALLER_PROTOCOL:
        raise UpgradeContractError("release installer protocol is older than this upgrader supports")
    if payload.get("bridge_protocol") != BRIDGE_PROTOCOL:
        raise UpgradeContractError("release bridge protocol is incompatible")
    if payload.get("zip_root") != "tenetora":
        raise UpgradeContractError("release zip root is invalid")
    entrypoint = payload.get("upgrade_entrypoint")
    if entrypoint != "skills/tenetora/scripts/upgrade_skill.py":
        raise UpgradeContractError("release upgrade entrypoint is invalid")
    files = payload.get("files")
    if not isinstance(files, list) or not files or not all(isinstance(item, str) and item for item in files):
        raise UpgradeContractError("release file inventory is invalid")
    if len(files) != len(set(files)):
        raise UpgradeContractError("release file inventory contains duplicates")
    if files != sorted(files):
        raise UpgradeContractError("release file inventory must be sorted")
    for item in files:
        path = PurePosixPath(item)
        if path.is_absolute() or ".." in path.parts or "." in path.parts or "\\" in item:
            raise UpgradeContractError("release file inventory contains an unsafe path")
    file_sha256 = payload.get("file_sha256")
    if not isinstance(file_sha256, dict) or sorted(file_sha256) != files:
        raise UpgradeContractError("release file SHA256 inventory does not match files")
    if not all(
        isinstance(key, str) and isinstance(value, str) and SHA256_RE.fullmatch(value)
        for key, value in file_sha256.items()
    ):
        raise UpgradeContractError("release file SHA256 inventory is invalid")
    state_schemas = payload.get("state_schemas")
    if not isinstance(state_schemas, dict) or not state_schemas:
        raise UpgradeContractError("release state schema contract is invalid")
    for key, value in state_schemas.items():
        if not isinstance(key, str) or not key or type(value) is not int or value < 1:
            raise UpgradeContractError("release state schema contract is invalid")
    if require_artifact:
        artifact = payload.get("artifact")
        digest = payload.get("sha256")
        if artifact not in {f"tenetora-{payload['version']}.zip", "tenetora-latest.zip"}:
            raise UpgradeContractError("release artifact name is invalid")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise UpgradeContractError("release SHA256 is invalid")
    return dict(payload)


def ensure_source_supported(source_version: str, manifest: dict[str, Any]) -> None:
    minimum = version_key(str(manifest["minimum_bridge_version"]))
    if version_key(source_version) < minimum:
        raise UpgradeContractError(
            f"installed Tenetora {source_version} is older than the direct bridge minimum {manifest['minimum_bridge_version']}"
        )


def release_contract_fields() -> dict[str, Any]:
    return {
        "bridge_protocol": BRIDGE_PROTOCOL,
        "minimum_bridge_version": MINIMUM_BRIDGE_VERSION,
        "upgrade_entrypoint": "skills/tenetora/scripts/upgrade_skill.py",
        "state_schemas": {
            "alignment": 4,
            "installation_registry": 1,
            "upgrade_transaction": 1,
            "update_hint": 1,
        },
    }
