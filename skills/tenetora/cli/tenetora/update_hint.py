"""Privacy-bounded cache for release update hints."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

try:
    from .file_lock import locked_file
    from .path_security import (
        ensure_unredirected_directory,
        validate_unredirected_file_path,
        validate_unredirected_path,
    )
except ImportError:  # Loaded directly by compatibility scripts and tests.
    from file_lock import locked_file
    from path_security import (
        ensure_unredirected_directory,
        validate_unredirected_file_path,
        validate_unredirected_path,
    )


CACHE_TTL_SECONDS = 24 * 60 * 60
SCHEMA_VERSION = 1
CACHE_NAME = "update-hint.json"
LOCK_NAME = ".update-hint.lock"
MAX_CACHE_BYTES = 32 * 1024
NETWORK_FAILURE = "network-failed"
CHANNEL = "stable"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
COMMIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CACHE_KEYS = frozenset(
    {
        "schema_version",
        "channel",
        "current_version",
        "latest_version",
        "latest_commit",
        "artifact_sha256",
        "checked_at",
        "last_attempt_at",
        "last_error_code",
    }
)


class UpdateHintError(RuntimeError):
    """Raised when the update hint cache cannot be safely read or written."""


def _version_key(value: object) -> tuple[int, int, int]:
    if not isinstance(value, str) or VERSION_RE.fullmatch(value.strip()) is None:
        raise UpdateHintError("update hint contains an invalid version")
    major, minor, patch = value.strip().split(".")
    return int(major), int(minor), int(patch)


def _timestamp(value: object, *, allow_empty: bool = False) -> str:
    if value == "" and allow_empty:
        return ""
    if not isinstance(value, str):
        raise UpdateHintError("update hint contains an invalid timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UpdateHintError("update hint contains an invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise UpdateHintError("update hint timestamp must include a timezone")
    return value


def _iso_now(now: float | int | None = None) -> str:
    instant = time.time() if now is None else float(now)
    return dt.datetime.fromtimestamp(instant, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _epoch(value: str) -> float:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _safe_home(home: Path | str) -> Path:
    try:
        return validate_unredirected_path(Path(home).expanduser(), label="Tenetora home")
    except (OSError, RuntimeError) as exc:
        raise UpdateHintError("update hint cache path is unsafe") from exc


def _cache_path(home: Path | str, *, create: bool) -> tuple[Path, Path]:
    root = _safe_home(home)
    state = root / "state"
    try:
        if create:
            ensure_unredirected_directory(state, label="update hint state directory")
        else:
            validate_unredirected_path(state, label="update hint state directory")
        cache = validate_unredirected_file_path(state / CACHE_NAME, label="update hint cache")
        lock = validate_unredirected_file_path(state / LOCK_NAME, label="update hint lock")
    except (OSError, RuntimeError) as exc:
        raise UpdateHintError("update hint cache path is unsafe") from exc
    return cache, lock


def _validate_payload(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != CACHE_KEYS:
        raise UpdateHintError("update hint cache schema is invalid")
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("channel") != CHANNEL:
        raise UpdateHintError("update hint cache schema is invalid")
    _version_key(payload.get("current_version"))
    _version_key(payload.get("latest_version"))
    commit = payload.get("latest_commit")
    if not isinstance(commit, str) or (commit and COMMIT_RE.fullmatch(commit) is None):
        raise UpdateHintError("update hint cache commit is invalid")
    digest = payload.get("artifact_sha256")
    if not isinstance(digest, str) or (digest and SHA256_RE.fullmatch(digest) is None):
        raise UpdateHintError("update hint cache digest is invalid")
    _timestamp(payload.get("checked_at"), allow_empty=True)
    _timestamp(payload.get("last_attempt_at"))
    if payload.get("last_error_code") not in {"", NETWORK_FAILURE}:
        raise UpdateHintError("update hint cache error status is invalid")
    return dict(payload)


def _read_payload(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        if path.stat().st_size > MAX_CACHE_BYTES:
            raise UpdateHintError("update hint cache exceeds the bounded size limit")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except UpdateHintError:
        raise
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise UpdateHintError("update hint cache is unreadable") from exc
    return _validate_payload(payload)


def _result(payload: dict[str, Any] | None, current_version: str, *, now: float | int | None = None) -> dict[str, Any]:
    _version_key(current_version)
    if payload is None:
        return {
            "status": "missing",
            "current_version": current_version,
            "update_available": False,
        }
    latest = str(payload["latest_version"])
    checked_at = str(payload["checked_at"])
    if payload["last_error_code"] == NETWORK_FAILURE or not checked_at:
        status = NETWORK_FAILURE
        age = None
    else:
        current_time = time.time() if now is None else float(now)
        age = max(0.0, current_time - _epoch(checked_at))
        status = "fresh" if age <= CACHE_TTL_SECONDS else "stale"
    result = dict(payload)
    result.update(
        {
            "status": status,
            "current_version": current_version,
            "update_available": _version_key(latest) > _version_key(current_version),
        }
    )
    if age is not None:
        result["age_seconds"] = int(age)
    return result


def read_hint(home: Path | str, *, current_version: str, now: float | int | None = None) -> dict[str, Any]:
    """Read the cache without creating a machine-home directory."""

    try:
        cache, _lock = _cache_path(home, create=False)
        payload = _read_payload(cache)
    except UpdateHintError:
        return {"status": "invalid", "current_version": current_version, "update_available": False}
    try:
        return _result(payload, current_version, now=now)
    except UpdateHintError:
        return {"status": "invalid", "current_version": current_version, "update_available": False}


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if len(serialized.encode("utf-8")) > MAX_CACHE_BYTES:
        raise UpdateHintError("update hint cache exceeds the bounded size limit")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{CACHE_NAME}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        if path.is_symlink():
            raise UpdateHintError("update hint cache is a symbolic link")
        os.replace(temporary, path)
    except UpdateHintError:
        raise
    except OSError as exc:
        raise UpdateHintError("update hint cache write failed") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _write(home: Path | str, payload: dict[str, Any]) -> dict[str, Any]:
    cache, lock = _cache_path(home, create=True)
    with locked_file(lock):
        _atomic_write(cache, _validate_payload(payload))
    return _result(payload, str(payload["current_version"]))


def record_success(
    home: Path | str,
    *,
    current_version: str,
    manifest: dict[str, object],
    now: float | int | None = None,
) -> dict[str, Any]:
    """Persist only authenticated release metadata after a successful check."""

    _version_key(current_version)
    if not isinstance(manifest, dict):
        raise UpdateHintError("release manifest is invalid")
    latest_version = manifest.get("version")
    commit = manifest.get("commit")
    digest = manifest.get("sha256")
    _version_key(latest_version)
    if not isinstance(commit, str) or COMMIT_RE.fullmatch(commit) is None:
        raise UpdateHintError("release manifest commit is invalid")
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest.lower()) is None:
        raise UpdateHintError("release manifest digest is invalid")
    timestamp = _iso_now(now)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "channel": CHANNEL,
        "current_version": current_version,
        "latest_version": latest_version,
        "latest_commit": commit,
        "artifact_sha256": digest.lower(),
        "checked_at": timestamp,
        "last_attempt_at": timestamp,
        "last_error_code": "",
    }
    return _write(home, payload)


def record_failure(
    home: Path | str,
    *,
    current_version: str,
    now: float | int | None = None,
) -> dict[str, Any]:
    """Record a bounded network failure without replacing the last good hint."""

    _version_key(current_version)
    cache, lock = _cache_path(home, create=True)
    timestamp = _iso_now(now)
    with locked_file(lock):
        try:
            existing = _read_payload(cache)
        except UpdateHintError:
            raise
        if existing is None:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "channel": CHANNEL,
                "current_version": current_version,
                "latest_version": current_version,
                "latest_commit": "",
                "artifact_sha256": "",
                "checked_at": "",
                "last_attempt_at": timestamp,
                "last_error_code": NETWORK_FAILURE,
            }
        else:
            payload = dict(existing)
            payload["current_version"] = current_version
            payload["last_attempt_at"] = timestamp
            payload["last_error_code"] = NETWORK_FAILURE
        _atomic_write(cache, _validate_payload(payload))
    return _result(payload, current_version, now=now)
