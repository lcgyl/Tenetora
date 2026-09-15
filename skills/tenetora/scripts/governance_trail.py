#!/usr/bin/env python3
"""Append-only Tenetora governance telemetry helpers."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from harness_io import atomic_write_text


TRAIL_REL = ".tenetora/state/governance-trail.json"
MAX_EVENTS = 500
LOCK_TIMEOUT_SECONDS = 5.0
LOCK_RETRY_SECONDS = 0.01
STALE_LOCK_SECONDS = 30.0
LOCAL_HOME_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_.-])/(?:Users|home)/[^/\\\s]+"),
    re.compile(r"(?i)(?<![A-Za-z0-9_.-])[A-Za-z]:\\Users\\[^\\\s]+"),
)


def trail_path(root: Path) -> Path:
    return root / TRAIL_REL


def load_trail(root: Path) -> dict[str, Any]:
    path = trail_path(root)
    if not path.is_file():
        return {"version": 1, "events": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "events": []}
    if not isinstance(payload, dict):
        return {"version": 1, "events": []}
    events = payload.get("events")
    if not isinstance(events, list):
        events = []
    return {"version": 1, "events": [item for item in events if isinstance(item, dict)]}


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def sanitize_local_paths(value: Any) -> Any:
    if isinstance(value, str):
        sanitized = value
        for pattern in LOCAL_HOME_PATTERNS:
            sanitized = pattern.sub("<home>", sanitized)
        return sanitized
    if isinstance(value, dict):
        return {key: sanitize_local_paths(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_local_paths(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_local_paths(item) for item in value]
    return value


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _stale_lock(path: Path) -> bool:
    try:
        pid = int(path.read_text(encoding="ascii").splitlines()[0].strip())
    except (OSError, ValueError, IndexError):
        try:
            return time.time() - path.stat().st_mtime > STALE_LOCK_SECONDS
        except OSError:
            return False
    try:
        old_enough = time.time() - path.stat().st_mtime > STALE_LOCK_SECONDS
    except OSError:
        return False
    return old_enough and not _pid_alive(pid)


@contextmanager
def trail_lock(root: Path):
    path = trail_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    fd: int | None = None

    while fd is None:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                if _stale_lock(lock_path):
                    lock_path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for governance trail lock: {lock_path}")
            time.sleep(LOCK_RETRY_SECONDS)

    try:
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        os.close(fd)
        fd = None
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def append_event(root: Path, event: dict[str, Any], max_events: int = MAX_EVENTS) -> dict[str, Any]:
    timestamped = sanitize_local_paths({
        "timestamp": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        **event,
    })
    with trail_lock(root):
        payload = load_trail(root)
        existing = [sanitize_local_paths(item) for item in payload["events"]]
        events = [*existing, timestamped][-max_events:]
        payload = {"version": 1, "events": events}
        write_json_atomic(trail_path(root), payload)
    return timestamped
