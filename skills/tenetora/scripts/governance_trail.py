#!/usr/bin/env python3
"""Append-only Tenetora governance telemetry helpers."""

from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import re
import time
import uuid
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from harness_io import atomic_write_text


TRAIL_REL = ".tenetora/state/governance-trail.json"
JOURNAL_REL = ".tenetora/state/trail"
MAX_EVENTS = 500
JOURNAL_SCHEMA_VERSION = 1
JOURNAL_ARCHIVE_NAME = "archive"
LOCK_TIMEOUT_SECONDS = 5.0
LOCK_RETRY_SECONDS = 0.01
STALE_LOCK_SECONDS = 30.0
LOCAL_HOME_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_.-])/(?:Users|home)/[^/\\\s]+"),
    re.compile(r"(?i)(?<![A-Za-z0-9_.-])[A-Za-z]:\\Users\\[^\\\s]+"),
)


def trail_path(root: Path) -> Path:
    return root / TRAIL_REL


def journal_event_path(root: Path, event: dict[str, Any]) -> Path:
    timestamp = str(event.get("timestamp") or "unknown")[:10]
    identity = str(event.get("session_id") or event.get("conversation_id") or "anonymous")
    safe_identity = re.sub(r"[^A-Za-z0-9_.-]+", "_", identity)[:80] or "anonymous"
    return root / JOURNAL_REL / timestamp / f"{safe_identity}.jsonl"


def _append_journal_event(root: Path, event: dict[str, Any]) -> None:
    path = journal_event_path(root, event)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def journal_events(root: Path) -> tuple[list[dict[str, Any]], int]:
    """Read valid JSONL events and count malformed journal records."""

    base = root / JOURNAL_REL
    events: list[dict[str, Any]] = []
    invalid = 0
    if not base.is_dir():
        return events, invalid
    for path in sorted(base.rglob("*.jsonl")):
        if JOURNAL_ARCHIVE_NAME in path.relative_to(base).parts:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            invalid += 1
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                invalid += 1
                continue
            if isinstance(event, dict):
                events.append(sanitize_local_paths(event))
            else:
                invalid += 1
    return events, invalid


def _legacy_events(root: Path) -> tuple[list[dict[str, Any]], int]:
    path = trail_path(root)
    if not path.is_file():
        return [], 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return [], 1
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        return [], 1
    events = [sanitize_local_paths(item) for item in payload["events"] if isinstance(item, dict)]
    event_ids = payload.get("event_ids")
    event_sequences = payload.get('event_sequences')
    if isinstance(event_ids, list):
        for index, event in enumerate(events):
            if index < len(event_ids) and isinstance(event_ids[index], str) and event_ids[index] and "event_id" not in event:
                event["event_id"] = event_ids[index]
    if isinstance(event_sequences, list):
        for index, event in enumerate(events):
            if index < len(event_sequences) and type(event_sequences[index]) is int:
                event['_journal_sequence'] = event_sequences[index]
    # A legacy client may append an unsequenced record to the compatibility
    # view. Preserve its position after existing same-timestamp records; it
    # must not sort before a proof simply because that proof has a sequence.
    previous_sequences: dict[str, int] = {}
    for event in events:
        timestamp = str(event.get('timestamp') or '\uffff')
        sequence = event.get('_journal_sequence')
        if type(sequence) is int:
            previous_sequences[timestamp] = sequence
        elif timestamp in previous_sequences:
            event['_journal_sequence'] = previous_sequences[timestamp]
    return events, len(payload["events"]) - len(events)


def event_key(event: dict[str, Any]) -> str:
    event_id = event.get("event_id")
    if isinstance(event_id, str) and event_id:
        return f"event-id:{event_id}"
    return "event-json:" + json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compatibility_payload(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep the legacy event object shape while retaining internal IDs by index."""

    visible: list[dict[str, Any]] = []
    event_ids: list[str | None] = []
    sequences: list[int | None] = []
    for event in events:
        item = dict(event)
        event_id = item.pop("event_id", None)
        sequence = item.pop('_journal_sequence', None)
        visible.append(item)
        event_ids.append(event_id if isinstance(event_id, str) and event_id else None)
        sequences.append(sequence if type(sequence) is int else None)
    payload: dict[str, Any] = {"version": JOURNAL_SCHEMA_VERSION, "events": visible}
    if any(event_ids):
        payload["event_ids"] = event_ids
    if any(sequence is not None for sequence in sequences):
        payload['event_sequences'] = sequences
    return payload


def event_order(event: dict[str, Any]) -> tuple:
    sequence = event.get('_journal_sequence')
    # Preserve legacy timestamp semantics (including undated failure records).
    # The writer sequence disambiguates equal timestamps across owner files.
    return (str(event.get('timestamp') or '\uffff'), sequence if type(sequence) is int else 0)


def _dual_read_conflicts(legacy: list[dict[str, Any]], journal: list[dict[str, Any]]) -> set[str]:
    legacy_by_id = {str(event["event_id"]): event for event in legacy if isinstance(event.get("event_id"), str) and event.get("event_id")}
    # The compatibility view retains only MAX_EVENTS. Archived/older journal
    # IDs outside that window are not deleted evidence in the view.
    active = sorted(journal, key=event_order)[-MAX_EVENTS:]
    journal_by_id = {str(event["event_id"]): event for event in active if isinstance(event.get("event_id"), str) and event.get("event_id")}
    if not journal_by_id:
        return set()
    if not legacy_by_id:
        return set(journal_by_id)
    conflicts = {event_id for event_id in set(legacy_by_id) & set(journal_by_id) if legacy_by_id[event_id] != journal_by_id[event_id]}
    conflicts.update(set(journal_by_id) - set(legacy_by_id))
    return conflicts


def canonical_events(root: Path, max_events: int | None = MAX_EVENTS) -> list[dict[str, Any]]:
    """Merge with the journal winning, for writes that repair the view."""

    legacy, _ = _legacy_events(root)
    journal, _ = journal_events(root)
    unique: dict[str, dict[str, Any]] = {}
    for event in [*legacy, *journal]:
        unique[event_key(event)] = event
    ordered = sorted(unique.values(), key=event_order)
    return ordered if max_events is None else ordered[-max_events:]


def merged_events(root: Path, max_events: int | None = MAX_EVENTS) -> tuple[list[dict[str, Any]], int]:
    """Merge the journal with the legacy view while de-duplicating dual writes."""

    legacy, legacy_invalid = _legacy_events(root)
    journal, journal_invalid = journal_events(root)
    unique: dict[str, dict[str, Any]] = {}
    conflicts = _dual_read_conflicts(legacy, journal) if trail_path(root).is_file() else set()
    if conflicts:
        # Preserve the compatibility view for claim/audit diagnostics. The
        # shared iterator refuses this divergent view, while aggregate readers
        # can report the specific changed or missing proof instead of hiding it
        # behind a generic trail-invalid error.
        selected = legacy
    else:
        selected = [*legacy, *journal]
    for event in selected:
        unique[event_key(event)] = event
    ordered = sorted(unique.values(), key=event_order)
    if max_events is not None:
        ordered = ordered[-max_events:]
    return ordered, legacy_invalid + journal_invalid


def compact_journal(root: Path, keep_events: int = MAX_EVENTS) -> dict[str, Any]:
    """Archive old journal records and retain a bounded active JSONL set."""

    if keep_events < 1:
        raise ValueError("keep_events must be positive")
    with trail_lock(root):
        events, invalid = merged_events(root, max_events=None)
        events = canonical_events(root, max_events=None)
        if invalid:
            raise ValueError("cannot compact a journal with invalid records")
        if len(events) <= keep_events:
            return {"archived": 0, "kept": len(events), "archive": None}
        archived = events[:-keep_events]
        kept = events[-keep_events:]
        archive_dir = root / JOURNAL_REL / JOURNAL_ARCHIVE_NAME
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / f"compacted-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex}.jsonl.gz"
        with gzip.open(archive_path, "wt", encoding="utf-8", newline="\n") as handle:
            for event in archived:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        base = root / JOURNAL_REL
        for path in sorted(base.rglob("*.jsonl")):
            if JOURNAL_ARCHIVE_NAME not in path.relative_to(base).parts:
                path.unlink()
        for event in kept:
            _append_journal_event(root, event)
        write_json_atomic(trail_path(root), compatibility_payload(kept[-MAX_EVENTS:]))
        return {"archived": len(archived), "kept": len(kept), "archive": archive_path.relative_to(root).as_posix()}


def load_trail(root: Path) -> dict[str, Any]:
    """Load the canonical trail with shared parsing and degradation semantics."""
    events, invalid = merged_events(root)
    legacy_events, _ = _legacy_events(root)
    journal, journal_invalid = journal_events(root)
    has_journal = (root / JOURNAL_REL).is_dir()
    conflicts = _dual_read_conflicts(legacy_events, journal)
    return {
        "version": JOURNAL_SCHEMA_VERSION,
        "events": events,
        "invalid": bool(invalid),
        "ignored_events": invalid,
        "source": "journal" if has_journal else "legacy",
        "journal_events": len(journal),
        "legacy_events": len(legacy_events),
        "journal_invalid": journal_invalid,
        "conflicts": sorted(conflicts),
    }


def iter_events(root: Path) -> Iterator[dict[str, Any]]:
    """Iterate typed events from the canonical trail reader."""

    payload = load_trail(root)
    if payload.get("invalid"):
        return
    yield from payload["events"]


def load_retained_trail(root: Path) -> dict[str, Any]:
    """Read obligations from all retained history, independent of telemetry's window.

    Active/compatibility consistency is checked first. Explicit compaction moves
    records to gzip archives; it does not discharge obligations in those records.
    This read-only view never rewrites or expands the bounded compatibility file.
    """
    from path_security import validate_unredirected_file_path

    def signature() -> tuple:
        validate_unredirected_file_path(trail_path(root), label='governance compatibility view')
        validate_unredirected_file_path(root / JOURNAL_REL / '.history-path-check', label='governance journal')
        archive = root / JOURNAL_REL / JOURNAL_ARCHIVE_NAME
        validate_unredirected_file_path(archive / '.archive-path-check', label='governance archive')
        if trail_path(root).with_name('.governance-trail.json.lock').exists():
            raise RuntimeError('governance history is changing')
        paths = [trail_path(root), *(root / JOURNAL_REL).rglob('*.jsonl'), *archive.glob('*.jsonl.gz')]
        result = []
        for path in sorted(set(paths)):
            validate_unredirected_file_path(path, label='retained governance event file')
            if path.exists():
                stat = path.stat()
                result.append((path.relative_to(root).as_posix(), stat.st_size, stat.st_mtime_ns, stat.st_ino))
        return tuple(result)

    try:
        before = signature()
    except (OSError, RuntimeError):
        return {'events': [], 'invalid': True, 'conflicts': []}
    payload = load_trail(root)
    if payload.get('invalid') or payload.get('conflicts'):
        return payload
    unique: dict[str, dict[str, Any]] = {}
    invalid = 0
    conflicts: set[str] = set()
    def remember(event: dict[str, Any]) -> None:
        key = event_key(event)
        if key in unique and unique[key] != event:
            conflicts.add(key)
        unique[key] = event

    archive_dir = root / JOURNAL_REL / JOURNAL_ARCHIVE_NAME
    try:
        # Check the parent chain even when a redirected archive is empty.
        validate_unredirected_file_path(archive_dir / '.archive-path-check', label='retained governance archive')
    except RuntimeError:
        return {**payload, 'invalid': True, 'archive_invalid': 1}
    for path in sorted(archive_dir.glob('*.jsonl.gz')):
        try:
            validate_unredirected_file_path(path, label='retained governance archive')
            with gzip.open(path, 'rt', encoding='utf-8') as handle:
                for line in handle:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise TypeError('invalid archived event')
                    event = sanitize_local_paths(event)
                    remember(event)
        except (OSError, EOFError, ValueError, TypeError, RuntimeError, zlib.error):
            invalid += 1
    # Validate every raw record before any projection can collapse duplicate IDs.
    active, active_invalid = journal_events(root)
    legacy, legacy_invalid = _legacy_events(root)
    invalid += active_invalid + legacy_invalid
    for event in [*active, *legacy]:
        remember(event)
    try:
        if before != signature():
            invalid += 1
    except (OSError, RuntimeError):
        invalid += 1
    return {**payload, 'events': sorted(unique.values(), key=event_order),
            'invalid': bool(invalid), 'archive_invalid': invalid, 'conflicts': sorted(conflicts)}


def latest_event(
    root: Path,
    predicate: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any] | None:
    """Return the newest event matching ``predicate``."""

    payload = load_trail(root)
    if payload.get("invalid"):
        return None
    for event in reversed(payload["events"]):
        if predicate is None or predicate(event):
            return event
    return None


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
        "event_id": uuid.uuid4().hex,
        **event,
    })
    with trail_lock(root):
        # Allocate under the same writer lock. Seconds-level timestamps cannot
        # preserve order across owners/files or after a bounded-view rollover.
        existing = canonical_events(root, max_events=None)
        timestamped['_journal_sequence'] = max(
            (event.get('_journal_sequence', 0) for event in existing
             if type(event.get('_journal_sequence', 0)) is int), default=0,
        ) + 1
        _append_journal_event(root, timestamped)
        events = canonical_events(root, max_events=max_events)
        write_json_atomic(trail_path(root), compatibility_payload(events))
    return timestamped
