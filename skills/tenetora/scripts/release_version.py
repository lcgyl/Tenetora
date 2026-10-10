#!/usr/bin/env python3
"""Stable and preview release-version identities."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Mapping, Tuple


STABLE_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
PREVIEW_SEQUENCE_RE = re.compile(r"^\d{3}$")
PREVIEW_RELEASE_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.(\d{3})-preview$")
ADAPTER_PREVIEW_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)-preview\.(\d+)$")
CHANNELS = {"stable", "preview"}


class ReleaseVersionError(ValueError):
    """Raised when release-version metadata is malformed or inconsistent."""


@dataclass(frozen=True)
class ReleaseIdentity:
    channel: str
    stable_version: str
    release_id: str
    adapter_version: str
    preview_sequence: str | None = None

    @property
    def artifact_name(self) -> str:
        return f"tenetora-{self.release_id}.zip"

    @property
    def alias_name(self) -> str:
        return "tenetora-preview.zip" if self.channel == "preview" else "tenetora-latest.zip"


def parse_stable_version(value: object) -> Tuple[int, int, int]:
    text = str(value or "").strip()
    match = STABLE_VERSION_RE.fullmatch(text)
    if match is None:
        raise ReleaseVersionError("stable version must use MAJOR.MINOR.PATCH")
    return tuple(int(part) for part in match.groups())


def preview_identity(stable_version: str, sequence: str) -> ReleaseIdentity:
    parse_stable_version(stable_version)
    sequence = str(sequence or "").strip()
    if PREVIEW_SEQUENCE_RE.fullmatch(sequence) is None or sequence == "000":
        raise ReleaseVersionError("preview sequence must use three digits from 001 to 999")
    numeric_sequence = int(sequence)
    return ReleaseIdentity(
        channel="preview",
        stable_version=stable_version,
        release_id=f"{stable_version}.{sequence}-preview",
        adapter_version=f"{stable_version}-preview.{numeric_sequence}",
        preview_sequence=sequence,
    )


def source_identity(root: Path, channel: str) -> ReleaseIdentity:
    channel = str(channel or "").strip().lower()
    if channel not in CHANNELS:
        raise ReleaseVersionError("release channel must be stable or preview")
    stable_version = (root / "skills" / "tenetora" / "VERSION").read_text(encoding="utf-8").strip()
    parse_stable_version(stable_version)
    if channel == "stable":
        return ReleaseIdentity(
            channel="stable",
            stable_version=stable_version,
            release_id=stable_version,
            adapter_version=stable_version,
        )
    preview_file = root / "PREVIEW_VERSION"
    if not preview_file.is_file():
        raise ReleaseVersionError("preview packaging requires root PREVIEW_VERSION")
    return preview_identity(stable_version, preview_file.read_text(encoding="utf-8").strip())


def manifest_identity(payload: Mapping[str, object]) -> ReleaseIdentity:
    release_id = str(payload.get("version") or "").strip()
    channel = str(payload.get("channel") or "stable").strip().lower()
    if channel == "stable":
        parse_stable_version(release_id)
        for field in ("stable_version", "preview_sequence", "adapter_version"):
            value = payload.get(field)
            if value not in (None, "", release_id):
                raise ReleaseVersionError(f"stable release manifest has invalid {field}")
        return ReleaseIdentity("stable", release_id, release_id, release_id)
    if channel != "preview":
        raise ReleaseVersionError("release channel must be stable or preview")
    stable_version = str(payload.get("stable_version") or "").strip()
    sequence = str(payload.get("preview_sequence") or "").strip()
    expected = preview_identity(stable_version, sequence)
    if release_id != expected.release_id:
        raise ReleaseVersionError("preview release id does not match stable version and sequence")
    if str(payload.get("adapter_version") or "").strip() != expected.adapter_version:
        raise ReleaseVersionError("preview adapter version does not match release identity")
    return expected


def version_key(value: str) -> Tuple[int, int, int, int, int]:
    text = str(value or "").strip()
    stable = STABLE_VERSION_RE.fullmatch(text)
    if stable is not None:
        major, minor, patch = (int(part) for part in stable.groups())
        return major, minor, patch, 0, 0
    preview = PREVIEW_RELEASE_RE.fullmatch(text)
    if preview is None or preview.group(4) == "000":
        raise ReleaseVersionError(
            "release version must use MAJOR.MINOR.PATCH or MAJOR.MINOR.PATCH.NNN-preview"
        )
    major, minor, patch, sequence = (int(part) for part in preview.groups())
    return major, minor, patch, 1, sequence
