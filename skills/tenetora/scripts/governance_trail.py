#!/usr/bin/env python3
"""Append-only Tenetora governance telemetry helpers."""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import os
import re
import stat
import time
import uuid
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from harness_io import atomic_write_text
from path_security import ensure_unredirected_directory, validate_unredirected_file_path


TRAIL_REL = ".tenetora/state/governance-trail.json"
JOURNAL_REL = ".tenetora/state/trail"
MAX_EVENTS = 500
AUTO_COMPACT_EVENTS = 2000
JOURNAL_SCHEMA_VERSION = 1
JOURNAL_ARCHIVE_NAME = "archive"
LOCK_TIMEOUT_SECONDS = 5.0
LOCK_RETRY_SECONDS = 0.01
STALE_LOCK_SECONDS = 30.0
LOCAL_HOME_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_.-])/(?:Users|home)/[^/\\\s]+"),
    re.compile(r"(?i)(?<![A-Za-z0-9_.-])[A-Za-z]:\\Users\\[^\\\s]+"),
)


def unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Governance evidence contains duplicate JSON fields.')
        result[key] = value
    return result


class TrailWriteError(ValueError):
    """Ordinary writes must not erase invalid or divergent evidence."""

    def __init__(self, invalid: int, conflicts: set[str], reason: str = 'invalid-or-conflicting-records'):
        self.invalid_records = invalid
        self.conflicting_records = len(conflicts)
        self.reason = reason
        super().__init__('Governance history cannot be recorded: ' + reason + '; inspect it and explicitly repair the evidence before recording new events.')

    def payload(self) -> dict[str, Any]:
        return {'version': 1, 'status': 'error', 'code': 'governance-history-not-writable',
            'message': str(self), 'invalid_records': self.invalid_records,
            'conflicting_records': self.conflicting_records, 'reason': self.reason}


def trail_path(root: Path) -> Path:
    return root / TRAIL_REL


def journal_event_path(root: Path, event: dict[str, Any]) -> Path:
    timestamp = str(event.get("timestamp") or "unknown")[:10]
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', timestamp):
        timestamp = 'unknown'
    identity = str(event.get("session_id") or event.get("conversation_id") or "anonymous")
    safe_identity = re.sub(r"[^A-Za-z0-9_.-]+", "_", identity)[:80] or "anonymous"
    return root / JOURNAL_REL / timestamp / f"{safe_identity}.jsonl"


def _append_journal_event(root: Path, event: dict[str, Any]) -> None:
    path = journal_event_path(root, event)
    ensure_unredirected_directory(path.parent, label='governance journal partition')
    validate_unredirected_file_path(path, label='governance journal record')
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def journal_events(root: Path, *, raw: bool = False) -> tuple[list[dict[str, Any]], int]:
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
                event = json.loads(line, object_pairs_hook=unique_json_object)
            except ValueError:
                invalid += 1
                continue
            if isinstance(event, dict):
                if '_journal_sequence' in event and (type(event['_journal_sequence']) is not int or event['_journal_sequence'] < 1):
                    invalid += 1
                if 'event_id' in event and event['event_id'] is not None and (not isinstance(event['event_id'], str) or not event['event_id'].strip()):
                    invalid += 1
                events.append(event if raw else sanitize_local_paths(event))
            else:
                invalid += 1
    return events, invalid


def _legacy_events(root: Path, *, raw: bool = False) -> tuple[list[dict[str, Any]], int]:
    path = trail_path(root)
    if not path.is_file():
        return [], 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_json_object)
    except (OSError, UnicodeDecodeError, ValueError):
        return [], 1
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        return [], 1
    events = [dict(item) if raw else sanitize_local_paths(item) for item in payload["events"] if isinstance(item, dict)]
    event_ids = payload.get("event_ids")
    event_sequences = payload.get('event_sequences')
    invalid = len(payload['events']) - len(events)
    for event in events:
        if '_journal_sequence' in event and (type(event['_journal_sequence']) is not int or event['_journal_sequence'] < 1):
            invalid += 1
        if 'event_id' in event and event['event_id'] is not None and (not isinstance(event['event_id'], str) or not event['event_id'].strip()):
            invalid += 1
    for name, values in (('event_ids', event_ids), ('event_sequences', event_sequences)):
        if name in payload and (not isinstance(values, list) or len(values) > len(events)):
            invalid += 1
    if isinstance(event_ids, list):
        for index, event in enumerate(events):
            if index >= len(event_ids) or event_ids[index] is None:
                continue
            identity = event_ids[index]
            if not isinstance(identity, str) or not identity or ('event_id' in event and event['event_id'] != identity):
                invalid += 1
            elif 'event_id' not in event:
                event['event_id'] = identity
    if isinstance(event_sequences, list):
        for index, event in enumerate(events):
            if index >= len(event_sequences) or event_sequences[index] is None:
                continue
            sequence = event_sequences[index]
            if type(sequence) is not int or sequence < 1 or ('_journal_sequence' in event and
                (type(event['_journal_sequence']) is not int or event['_journal_sequence'] != sequence)):
                invalid += 1
            elif '_journal_sequence' not in event:
                event['_journal_sequence'] = sequence
    # Ordering metadata is never inferred into raw records. Legacy failure
    # order is accepted by a distinct durable journal record at write time.
    return events, invalid


def event_key(event: dict[str, Any]) -> str:
    event_id = event.get("event_id")
    if isinstance(event_id, str) and event_id:
        return f"event-id:{event_id}"
    return "event-json:" + json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def raw_event_digest(event: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(event, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')).hexdigest()


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
    # Modern events follow serialized write order, including clock rollback.
    # Anonymous legacy appends inherit their preceding view anchor and follow
    # it; older unsequenced legacy history retains timestamp order as a prefix.
    if type(sequence) is int and sequence > 0:
        return (1, sequence, 0 if event.get('event_id') else 1, str(event.get('timestamp') or '\uffff'))
    return (0, 0, 0, str(event.get('timestamp') or '\uffff'))


def _dual_read_conflicts(legacy: list[dict[str, Any]], journal: list[dict[str, Any]]) -> set[str]:
    legacy_by_id = {str(event["event_id"]): event for event in legacy if isinstance(event.get("event_id"), str) and event.get("event_id")}
    # The compatibility view retains only MAX_EVENTS. Archived/older journal
    # IDs outside that window are not deleted evidence in the view.
    active = sorted(journal, key=event_order)[-MAX_EVENTS:]
    journal_by_id = {str(event["event_id"]): event for event in active if isinstance(event.get("event_id"), str) and event.get("event_id")}
    # Only the canonical writer can publish internal order acceptances. Unlike
    # ordinary legacy data, a view-only assertion of this type has no authority.
    all_journal = {event_key(event): event for event in journal}
    internal_conflicts = {event_key(event) for event in legacy
        if event.get('type') in ('legacy-verification-order','verification-execution-start','verification-execution') and all_journal.get(event_key(event)) != event}
    # A legacy failure may arrive without order and be reconciled. It cannot
    # instead self-assign an earlier modern writer sequence in the view alone.
    internal_conflicts.update(event_key(event) for event in legacy
        if event.get('type') == 'verification-claim' and '_journal_sequence' in event
        and (event.get('status') != 'pass' or event.get('verification_status') != 'passed'
            or event.get('claim_kind') not in ('completion', 'partial-verification'))
        and all_journal.get(event_key(event)) != event)
    if not journal_by_id:
        return internal_conflicts
    if not legacy_by_id:
        return set(journal_by_id) | internal_conflicts
    conflicts = {event_id for event_id in set(legacy_by_id) & set(journal_by_id) if legacy_by_id[event_id] != journal_by_id[event_id]}
    conflicts.update(set(journal_by_id) - set(legacy_by_id))
    return conflicts | internal_conflicts


def writer_snapshot(root: Path) -> tuple[list[dict[str, Any]], int, set[str], list[dict[str, Any]]]:
    """Read raw active inputs once, retaining corruption information."""
    try:
        validate_unredirected_file_path(trail_path(root), label='governance compatibility view')
        validate_unredirected_file_path(root / JOURNAL_REL / '.path-check', label='governance journal')
        validate_unredirected_file_path(root / JOURNAL_REL / JOURNAL_ARCHIVE_NAME / '.path-check', label='governance archive')
        for path in (root / JOURNAL_REL).rglob('*.jsonl'):
            validate_unredirected_file_path(path, label='active governance journal')
    except RuntimeError as exc:
        raise TrailWriteError(0, set(), 'unsafe-history-path') from exc
    legacy, legacy_invalid = _legacy_events(root, raw=True)
    journal, journal_invalid = journal_events(root, raw=True)
    unique: dict[str, dict[str, Any]] = {}
    conflicts: set[str] = set()
    for event in [*legacy, *journal]:
        key = event_key(event)
        if key in unique and unique[key] != event:
            conflicts.add(key)
        unique[key] = event
    if trail_path(root).is_file():
        conflicts.update(_dual_read_conflicts(legacy, journal))
    # Validate before privacy normalization: rewriting a bound raw candidate
    # must never silently restore a mismatching receipt's authority.
    from completion_protocol import project_events
    raw_events = list(unique.values())
    # Ordinary writers reject conflicts below; explicit rebuild may recover
    # its view from the journal. Receipt integrity is a separate invariant.
    projected = project_events(root, raw_events)
    corrupt_phases = {'history-invalid', 'raw-candidate-invalid', 'candidate-commit-mismatch'}
    if any(event.get('completion_phase') in corrupt_phases for event in projected):
        raise TrailWriteError(legacy_invalid + journal_invalid, conflicts, 'completion-authority-invalid')
    journal_keys = {event_key(event) for event in journal}
    legacy_only = [event for event in legacy if event_key(event) not in journal_keys]
    return sanitize_local_paths(sorted(raw_events, key=event_order)), legacy_invalid + journal_invalid, conflicts, sanitize_local_paths(legacy_only)


def canonical_events(root: Path, max_events: int | None = MAX_EVENTS) -> list[dict[str, Any]]:
    """Merge with the journal winning, for writes that repair the view."""
    ordered, _, _, _ = writer_snapshot(root)
    return ordered if max_events is None else ordered[-max_events:]


def require_writable_history(root: Path) -> None:
    with trail_lock(root):
        _, invalid, conflicts, _ = writer_snapshot(root)
        if invalid or conflicts:
            raise TrailWriteError(invalid, conflicts)


def merged_events(root: Path, max_events: int | None = MAX_EVENTS, *, raw: bool = False) -> tuple[list[dict[str, Any]], int]:
    """Merge the journal with the legacy view while de-duplicating dual writes."""

    legacy, legacy_invalid = _legacy_events(root, raw=raw)
    journal, journal_invalid = journal_events(root, raw=raw)
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


def active_snapshot(events: list[dict[str, Any]], keep_events: int) -> list[dict[str, Any]]:
    kept = events[-keep_events:]
    sequenced = [event for event in events if type(event.get('_journal_sequence')) is int]
    if sequenced:
        anchor = max(sequenced, key=lambda event: event['_journal_sequence'])
        if anchor not in kept:
            kept = sorted([anchor, *kept], key=event_order)
    return kept


def compact_snapshot(root: Path, events: list[dict[str, Any]], keep_events: int) -> dict[str, Any]:
    """Publish a complete recovery snapshot before replacing active segments.

    The caller holds the writer lock and has checked every raw input. Archives
    deliberately include the kept tail, so interruption at any later step
    cannot lose it. A newly published raw segment also retains the highest
    writer sequence, even if a caller supplied an older event timestamp.
    """
    from path_security import ensure_unredirected_directory, validate_unredirected_file_path

    if len(events) <= keep_events:
        return {'archived': 0, 'kept': len(events), 'archive': None}
    kept = active_snapshot(events, keep_events)
    base = root / JOURNAL_REL
    archive_dir = base / JOURNAL_ARCHIVE_NAME
    segment_dir = base / 'segments'
    ensure_unredirected_directory(archive_dir, label='governance archive')
    ensure_unredirected_directory(segment_dir, label='governance segments')
    old_paths = [path for path in sorted(base.rglob('*.jsonl')) if JOURNAL_ARCHIVE_NAME not in path.relative_to(base).parts]
    for path in old_paths:
        validate_unredirected_file_path(path, label='active governance journal')
    identifier = uuid.uuid4().hex
    archive_path = archive_dir / f'compacted-{identifier}.jsonl.gz'
    temporary = archive_dir / f'.compacted-{identifier}.tmp'
    try:
        with gzip.open(temporary, 'wt', encoding='utf-8', newline='\n') as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + '\n')
        with temporary.open('r+b') as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, archive_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    segment = segment_dir / f'compacted-{identifier}.jsonl'
    atomic_write_text(segment, ''.join(json.dumps(event, ensure_ascii=False, sort_keys=True) + '\n' for event in kept))
    with segment.open('r+b') as handle:
        os.fsync(handle.fileno())
    # At this point both the archive and the new active segment are complete.
    # Readers tolerate identical duplicate IDs if removal is interrupted.
    for path in old_paths:
        path.unlink()
    write_json_atomic(trail_path(root), compatibility_payload(kept[-MAX_EVENTS:]))
    return {'archived': len(events) - keep_events, 'kept': len(kept),
        'archive': archive_path.relative_to(root).as_posix(), 'archive_records': len(events)}


def compact_journal(root: Path, keep_events: int = MAX_EVENTS) -> dict[str, Any]:
    """Archive validated raw history and retain a bounded active set."""

    if keep_events < 1:
        raise ValueError("keep_events must be positive")
    with trail_lock(root):
        events, invalid, conflicts, _ = writer_snapshot(root)
        if invalid or conflicts:
            raise ValueError("cannot compact a journal with invalid or conflicting records")
        return compact_snapshot(root, events, keep_events)


def active_trail_snapshot(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse each active authority once for one read, retaining every raw record."""
    legacy_events, legacy_invalid = _legacy_events(root, raw=True)
    journal, journal_invalid = journal_events(root, raw=True)
    invalid = legacy_invalid + journal_invalid
    has_journal = (root / JOURNAL_REL).is_dir()
    conflicts = _dual_read_conflicts(legacy_events, journal)
    unique = {}
    for event in legacy_events if conflicts and trail_path(root).is_file() else [*legacy_events, *journal]:
        unique[event_key(event)] = event
    events = sorted(unique.values(), key=event_order)[-MAX_EVENTS:]
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
    }, legacy_events, journal


def load_trail(root: Path, *, project: bool = True) -> dict[str, Any]:
    """Load the canonical trail with shared parsing and degradation semantics."""
    payload, _, _ = active_trail_snapshot(root)
    if project:
        from completion_protocol import project_events
        payload['events'] = sanitize_local_paths(project_events(root, payload['events'],
            history_valid=not payload['invalid'] and not payload['conflicts']))
    return payload


def iter_events(root: Path) -> Iterator[dict[str, Any]]:
    """Iterate typed events from the canonical trail reader."""

    payload = load_trail(root)
    if payload.get("invalid"):
        return
    yield from payload["events"]


def load_retained_trail(root: Path, *, project: bool = True, writer_locked: bool = False) -> dict[str, Any]:
    """Read obligations from all retained history, independent of telemetry's window.

    Active/compatibility consistency is checked first. Automatic or explicit
    compaction moves records to gzip archives without discharging obligations.
    This read-only view never rewrites or expands the bounded compatibility file.
    """
    from path_security import validate_unredirected_file_path

    def signature() -> tuple:
        validate_unredirected_file_path(trail_path(root), label='governance compatibility view')
        validate_unredirected_file_path(root / JOURNAL_REL / '.history-path-check', label='governance journal')
        archive = root / JOURNAL_REL / JOURNAL_ARCHIVE_NAME
        validate_unredirected_file_path(archive / '.archive-path-check', label='governance archive')
        if not writer_locked and trail_path(root).with_name('.governance-trail.json.lock').exists():
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
    payload, legacy, active = active_trail_snapshot(root)
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
                    event = json.loads(line, object_pairs_hook=unique_json_object)
                    if not isinstance(event, dict):
                        raise TypeError('invalid archived event')
                    remember(event)
        except (OSError, EOFError, ValueError, TypeError, RuntimeError, zlib.error):
            invalid += 1
    # Validate every raw record before any projection can collapse duplicate IDs.
    # The raw active arrays above belong to this same metadata-checked read;
    # reuse them without a second parse. The final signature still rejects a
    # write, replacement or compaction during archive traversal.
    for event in [*active, *legacy]:
        remember(event)
    try:
        if before != signature():
            invalid += 1
    except (OSError, RuntimeError):
        invalid += 1
    from completion_protocol import project_events
    ordered = sorted(unique.values(), key=event_order)
    return {**payload, 'events': sanitize_local_paths(project_events(root, ordered, history_valid=not invalid and not conflicts, complete_history=True)) if project else ordered,
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


def validate_writer_lock_path(path: Path) -> None:
    """One lstat snapshot: a released lock is absence, never an unsafe file."""
    ensure_unredirected_directory(path.parent, label='governance writer lock parent')
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise RuntimeError('governance writer lock is redirected')
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError('governance writer lock is not a regular file')


@contextmanager
def trail_lock(root: Path):
    path = trail_path(root)
    lock_path = path.with_name(f".{path.name}.lock")
    try:
        ensure_unredirected_directory(path.parent, label='governance state directory')
        validate_writer_lock_path(lock_path)
    except RuntimeError as exc:
        raise TrailWriteError(0, set(), 'unsafe-history-path') from exc
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    fd: int | None = None

    while fd is None:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                validate_writer_lock_path(lock_path)
                if _stale_lock(lock_path):
                    lock_path.unlink()
                    continue
            except FileNotFoundError:
                continue
            except RuntimeError as exc:
                raise TrailWriteError(0, set(), 'unsafe-history-path') from exc
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for governance trail lock: {lock_path}")
            time.sleep(LOCK_RETRY_SECONDS)

    try:
        os.write(fd, f"{os.getpid()}\n{uuid.uuid4().hex}\n".encode("ascii"))
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
    try:
        return _append_event_locked(root, timestamped, max_events)
    except OSError:
        # Cleanup or an error reported after atomic replacement must not turn
        # an already committed event into a false failure. Confirm both raw
        # authorities; an incomplete journal/view pair still raises.
        try:
            _, invalid, conflicts, _ = writer_snapshot(root)
            journal, _ = journal_events(root)
            legacy, _ = _legacy_events(root)
            published = not invalid and not conflicts and timestamped in journal and timestamped in legacy
        except (OSError, RuntimeError, ValueError):
            published = False
        if not published:
            raise
        return {**timestamped, '_write_warnings': [{'code': 'journal-committed-cleanup-incomplete',
            'message': 'The event is committed in the journal and view; post-write cleanup reported an error. Inspect the local writer lock before further writes.'}]}


def _append_event_locked(root: Path, timestamped: dict[str, Any], max_events: int) -> dict[str, Any]:
    with trail_lock(root):
        # Allocate under the same writer lock. Seconds-level timestamps cannot
        # preserve order across owners/files or after a bounded-view rollover.
        existing, invalid, conflicts, legacy_only = writer_snapshot(root)
        if invalid or conflicts:
            raise TrailWriteError(invalid, conflicts)
        next_sequence = max(
            (event.get('_journal_sequence', 0) for event in existing
             if type(event.get('_journal_sequence', 0)) is int), default=0,
        ) + 1
        # Reconcile failures from the view, active journal AND archives without
        # rewriting raw evidence. One immutable acceptance records exact bytes
        # and a real writer sequence before the new verification assertion.
        from claim_evidence import legacy_failure_orders, unordered_legacy_failure
        retained = load_retained_trail(root, project=False, writer_locked=True)
        if retained.get('invalid') or retained.get('conflicts'):
            raise TrailWriteError(1, set(), 'invalid-retained-history')
        orders = legacy_failure_orders(retained['events'])
        accepted = []
        for legacy in retained['events']:
            if not unordered_legacy_failure(legacy):
                continue
            # The existing legacy privacy migration may sanitize a view copy;
            # bind both exact representations seen by this writer. Archived
            # original bytes stay untouched and arbitrary later edits do not match.
            for digest in dict.fromkeys((raw_event_digest(legacy), raw_event_digest(sanitize_local_paths(legacy)))):
                if digest in orders:
                    continue
                acceptance = {'type': 'legacy-verification-order', 'event_id': uuid.uuid4().hex,
                    'timestamp': dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z'),
                    '_journal_sequence': next_sequence, 'legacy_failure_sha256': digest}
                next_sequence += 1
                orders[digest] = acceptance['_journal_sequence']
                accepted.append(acceptance)
        existing.extend(accepted)
        legacy_only.extend(accepted)
        timestamped['_journal_sequence'] = next_sequence
        keep = max(MAX_EVENTS, max_events)
        # Maintenance is a precondition for this write, never a fallible
        # post-publication action. If it fails, no event for this request was
        # appended; already-published evidence cannot be mistaken for failure.
        if len(existing) >= max(AUTO_COMPACT_EVENTS, 2 * keep):
            compact_snapshot(root, existing, keep)
            existing = active_snapshot(existing, keep)
            legacy_only = []
        unique = {event_key(event): event for event in existing}
        key = event_key(timestamped)
        if key in unique and unique[key] != timestamped:
            raise TrailWriteError(0, {key}, 'duplicate-event-identity')
        # Old clients may have recorded events only in the bounded JSON view.
        # Persist them before replacing that view; interruption merely leaves
        # identical duplicates that the shared reader already deduplicates.
        for retained in legacy_only:
            _append_journal_event(root, retained)
        _append_journal_event(root, timestamped)
        unique[key] = timestamped
        ordered = sorted(unique.values(), key=event_order)
        write_json_atomic(trail_path(root), compatibility_payload(ordered[-max_events:]))
    return timestamped
