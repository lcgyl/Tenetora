#!/usr/bin/env python3
"""Manage bounded pre-execution decision alignment state."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from harness_io import atomic_write_text
from path_security import harness_missing_message, validate_existing_project_path


SESSION_REL = Path(".tenetora/state/alignment-session.json")
SESSION_DIR_REL = Path(".tenetora/state/alignment-sessions")
HISTORY_DIR_REL = Path(".tenetora/state/alignment-history")
LIFECYCLE_REL = Path(".tenetora/state/alignment-lifecycle.json")
CURRENT_REL = Path(".tenetora/state/current-alignment.json")
CURRENT_SCHEMA_VERSION = 4
LIFECYCLE_SCHEMA_VERSION = 1
MAX_BATCH_QUESTIONS = 8
DEFAULT_TTL_SECONDS = 3600
AWAITING_CONFIRMATION_TTL_SECONDS = 7 * 24 * 60 * 60
OPEN_HANDOFF_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_LIFECYCLE_RECORDS = 64
MAX_HISTORY_RECORDS = 128
HISTORY_RETENTION_SECONDS = 90 * 24 * 60 * 60
CONFIRMATION_SOURCES = {"user-message"}
EXECUTION_ATTEMPT_STATUSES = {"running", "completed", "rate-limited", "failed", "cancelled"}
ACTIVE_STATUSES = {
    "active",
    "checkpoint",
    "paused",
    "blocked",
    "awaiting-confirmation",
}


def use_chinese() -> bool:
    return os.environ.get("TENETORA_LANG", "").strip().lower() in {"zh", "zh-cn", "zh-hans", "chinese", "中文"}
TERMINAL_STATUSES = {"confirmed", "accepted-with-risks", "abandoned", "expired"}
VALID_RISK_LEVELS = {"low", "medium", "high"}
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
OWNER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{3,127}$")
MACHINE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9:/])(?:/Users/|/home/|/root/|/private/|[A-Za-z]:[\\/])"
)
SECRET_RE = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\bglpat-[A-Za-z0-9._-]{12,}|"
    r"\bghp_[A-Za-z0-9_]{20,}|"
    r"\bgithub_pat_[A-Za-z0-9_]{20,}|"
    r"\bsk-[A-Za-z0-9_-]{16,}|"
    r"\b[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD)\b\s*[:=]\s*['\"]?[A-Za-z0-9+/._=-]{16,})",
    re.IGNORECASE,
)
ALIGNMENT_LIST_FIELDS = (
    "scope",
    "non_goals",
    "decisions",
    "constraints",
    "assumptions",
    "accepted_risks",
    "approval_boundaries",
    "acceptance_criteria",
    "verification",
    "open_questions",
)


class AlignmentStateError(RuntimeError):
    """Raised when state is unavailable, invalid, or unsafe to persist."""


class AlignmentIdentityAmbiguous(AlignmentStateError):
    """Raised when a host identity maps to more than one open goal."""

    def __init__(self, message: str, candidates: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.candidates = candidates


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def utc_after(seconds: int = DEFAULT_TTL_SECONDS) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).replace(microsecond=0).isoformat()


def parse_utc(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def environment_value(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def validate_identity(value: str, field: str, pattern: re.Pattern[str]) -> str:
    cleaned = ensure_safe_text(value, field)
    if not pattern.fullmatch(cleaned):
        raise AlignmentStateError(f"{field} must use a stable opaque identifier")
    return cleaned


def existing_project_path(raw: str) -> Path:
    try:
        return validate_existing_project_path(raw)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split()).casefold()


def normalize_items(values: list[str]) -> list[str]:
    return sorted({normalized for item in values if (normalized := normalize_text(item))})


def goal_fingerprint(
    goal: str,
    scope: list[str],
    non_goals: list[str],
    acceptance_criteria: list[str],
) -> str:
    payload = {
        "goal": normalize_text(goal),
        "scope": normalize_items(scope),
        "non_goals": normalize_items(non_goals),
        "acceptance_criteria": normalize_items(acceptance_criteria),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_stale(expected_fingerprint: str, actual_fingerprint: str) -> bool:
    return not bool(expected_fingerprint) or expected_fingerprint != actual_fingerprint


def ensure_safe_text(value: str, field: str) -> str:
    cleaned = " ".join(value.split())
    if not cleaned:
        raise AlignmentStateError(f"{field} must be non-empty")
    if MACHINE_PATH_RE.search(cleaned):
        raise AlignmentStateError(f"{field} contains a machine-local absolute path; store a relative or abstract reference")
    if SECRET_RE.search(cleaned):
        raise AlignmentStateError(f"{field} appears to contain a secret or private key; do not store it in alignment state")
    return cleaned


def safe_list(values: list[str] | None, field: str) -> list[str]:
    return [ensure_safe_text(value, field) for value in (values or [])]


def validate_text_list(payload: dict[str, Any], field: str) -> None:
    values = payload.get(field)
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise AlignmentStateError(f"alignment state field {field} must be a list of strings")
    for value in values:
        ensure_safe_text(value, field)


def validate_handoff_relative_path(value: Any) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise AlignmentStateError("current alignment handoff path must be a non-empty project-relative string")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise AlignmentStateError("current alignment handoff path must be project-relative")
    if path.parts[:3] != (".tenetora", "docs", "plans") or path.suffix.lower() != ".md":
        raise AlignmentStateError("current alignment handoff path must stay under .tenetora/docs/plans")
    return path


def session_path(root: Path) -> Path:
    return root / SESSION_REL


def session_directory(root: Path) -> Path:
    return root / SESSION_DIR_REL


def isolated_session_path(root: Path, session_id: str) -> Path:
    validate_identity(session_id, "session_id", SESSION_ID_RE)
    return session_directory(root) / f"{session_id}.json"


def requested_session_id(args: argparse.Namespace, *, required: bool = False) -> str:
    value = str(getattr(args, "session_id", "") or environment_value(
        "TENETORA_ALIGNMENT_SESSION_ID", "TENETORA_SESSION_ID"
    )).strip()
    if not value:
        if required:
            raise AlignmentStateError(
                "an alignment session id is required; pass --session-id or set TENETORA_ALIGNMENT_SESSION_ID"
            )
        return ""
    return validate_identity(value, "session_id", SESSION_ID_RE)


def requested_owner_id(args: argparse.Namespace, session_id: str = "", *, required: bool = False) -> str:
    value = str(getattr(args, "owner_id", "") or environment_value(
        "TENETORA_ALIGNMENT_OWNER_ID", "TENETORA_OWNER_ID", "TENETORA_CONVERSATION_ID"
    )).strip()
    if not value and getattr(args, "conversation_id", "") and getattr(args, "start", False):
        value = str(args.conversation_id).strip()
    if not value and session_id and getattr(args, "start", False):
        value = f"owner-{uuid.uuid4().hex}"
    if not value:
        if required:
            raise AlignmentStateError(
                "an alignment owner is required; pass --owner-id or set TENETORA_ALIGNMENT_OWNER_ID"
            )
        return ""
    return validate_identity(value, "owner_id", OWNER_ID_RE)


def requested_tool(args: argparse.Namespace) -> str:
    value = str(getattr(args, "tool", "") or environment_value("TENETORA_TOOL") or "unknown").strip()
    return validate_identity(value, "tool", OWNER_ID_RE)


def requested_conversation_id(args: argparse.Namespace, owner_id: str = "") -> str:
    value = str(getattr(args, "conversation_id", "") or environment_value("TENETORA_CONVERSATION_ID")).strip()
    if not value:
        value = owner_id
    if not value:
        return ""
    return validate_identity(value, "conversation_id", OWNER_ID_RE)


def has_explicit_conversation_continuity(args: argparse.Namespace) -> bool:
    return bool(
        str(getattr(args, "conversation_continuity_id", "") or "").strip()
        or environment_value("TENETORA_CONVERSATION_CONTINUITY_ID")
    )


def requested_conversation_continuity_id(
    args: argparse.Namespace,
    owner_id: str = "",
    *,
    required: bool = False,
    allow_fallback: bool = True,
) -> str:
    value = str(
        getattr(args, "conversation_continuity_id", "")
        or environment_value("TENETORA_CONVERSATION_CONTINUITY_ID")
    ).strip()
    if not value and allow_fallback:
        value = requested_conversation_id(args, owner_id)
    if not value:
        if required:
            raise AlignmentStateError(
                "conversation continuity is unavailable; pass --conversation-continuity-id or set TENETORA_CONVERSATION_CONTINUITY_ID"
            )
        return ""
    return validate_identity(value, "conversation_continuity_id", OWNER_ID_RE)


def requested_execution_attempt_id(args: argparse.Namespace, *, required: bool = False) -> str:
    value = str(getattr(args, "execution_attempt_id", "") or "").strip()
    if not value:
        value = uuid.uuid4().hex
    return validate_identity(value, "execution_attempt_id", OWNER_ID_RE)


def requested_execution_metadata(args: argparse.Namespace, field: str, *, required: bool = False) -> str:
    env_name = f"TENETORA_{field.upper()}"
    value = str(getattr(args, field, "") or environment_value(env_name)).strip()
    if not value:
        if required:
            raise AlignmentStateError(f"{field} is required for an execution attempt")
        return ""
    return ensure_safe_text(value, field)


@contextmanager
def state_lock(path: Path):
    """Serialize read/check/write operations for one state record on all hosts."""
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        try:
            import fcntl
        except ImportError:
            import msvcrt

            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def current_path(root: Path) -> Path:
    return root / CURRENT_REL


def history_directory(root: Path) -> Path:
    return root / HISTORY_DIR_REL


def lifecycle_path(root: Path) -> Path:
    return root / LIFECYCLE_REL


LIFECYCLE_STATUSES = {"open", "completed", "archived", "expired", "abandoned"}


def empty_lifecycle() -> dict[str, Any]:
    return {"version": LIFECYCLE_SCHEMA_VERSION, "revision": 0, "records": {}}


def validate_lifecycle(payload: dict[str, Any], label: str = "alignment-lifecycle.json") -> None:
    if payload.get("version") != LIFECYCLE_SCHEMA_VERSION:
        raise AlignmentStateError(f"{label} has an unsupported schema version")
    revision = payload.get("revision", 0)
    if type(revision) is not int or revision < 0:
        raise AlignmentStateError(f"{label} has an invalid revision")
    records = payload.get("records", {})
    if not isinstance(records, dict):
        raise AlignmentStateError(f"{label} records must be an object")
    for key, record in records.items():
        if not isinstance(key, str) or not SESSION_ID_RE.fullmatch(key):
            raise AlignmentStateError(f"{label} has an invalid session id")
        if not isinstance(record, dict) or record.get("session_id") != key:
            raise AlignmentStateError(f"{label} has an invalid lifecycle record")
        if record.get("status") not in LIFECYCLE_STATUSES:
            raise AlignmentStateError(f"{label} has an invalid lifecycle status")
        for field, pattern in (
            ("owner_id", OWNER_ID_RE),
            ("conversation_id", OWNER_ID_RE),
            ("conversation_continuity_id", OWNER_ID_RE),
            ("tool", OWNER_ID_RE),
        ):
            value = record.get(field, "")
            if not isinstance(value, str) or (value and not pattern.fullmatch(value)):
                raise AlignmentStateError(f"{label} has an invalid {field}")
        goal = record.get("goal", "")
        if not isinstance(goal, str):
            raise AlignmentStateError(f"{label} has an invalid goal")
        if goal:
            ensure_safe_text(goal, "alignment lifecycle goal")
        for field in (
            "created_at",
            "confirmed_at",
            "last_activity_at",
            "completed_at",
            "archived_at",
            "expired_at",
        ):
            value = record.get(field, "")
            if value and parse_utc(value) is None:
                raise AlignmentStateError(f"{label} has an invalid {field}")
        for field in ("goal_fingerprint", "handoff_hash"):
            value = record.get(field, "")
            if value and (not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)):
                raise AlignmentStateError(f"{label} has an invalid {field}")
        claim_proof = record.get("completion_claim_proof", "")
        if claim_proof and (not isinstance(claim_proof, str) or not re.fullmatch(r"ah-claim-[0-9a-f]{32}", claim_proof)):
            raise AlignmentStateError(f"{label} has an invalid completion claim proof")


def load_lifecycle(root: Path) -> dict[str, Any]:
    payload = read_json_object(lifecycle_path(root), "alignment lifecycle")
    if payload is None:
        return empty_lifecycle()
    validate_lifecycle(payload)
    return payload


def _write_lifecycle_locked(path: Path, payload: dict[str, Any], current_revision: int) -> dict[str, Any]:
    written = dict(payload)
    written["version"] = LIFECYCLE_SCHEMA_VERSION
    written["revision"] = current_revision + 1
    validate_lifecycle(written)
    atomic_write_text(path, json.dumps(written, ensure_ascii=False, indent=2) + "\n")
    return written


def update_lifecycle(root: Path, mutate: Callable[[dict[str, dict[str, Any]]], Any]) -> Any:
    """Atomically mutate the compact lifecycle index and reject lost updates."""
    path = lifecycle_path(root)
    with state_lock(path):
        current = read_json_object(path, "alignment lifecycle") if path.is_file() else None
        payload = current if current is not None else empty_lifecycle()
        validate_lifecycle(payload)
        records = payload["records"]
        result = mutate(records)
        _write_lifecycle_locked(path, {"records": records}, int(payload.get("revision", 0)))
        return result


def lifecycle_record_from_handoff(payload: dict[str, Any], *, now: str = "") -> dict[str, Any]:
    timestamp = now or str(payload.get("confirmed_at") or utc_now())
    return {
        "session_id": str(payload.get("session_id", payload.get("alignment_id", ""))),
        "owner_id": str(payload.get("owner_id") or ""),
        "conversation_id": str(payload.get("conversation_id") or ""),
        "conversation_continuity_id": str(
            payload.get("conversation_continuity_id") or payload.get("conversation_id") or ""
        ),
        "tool": str(payload.get("tool") or "unknown"),
        "goal": str(payload.get("goal") or ""),
        "goal_fingerprint": str(payload.get("goal_fingerprint") or ""),
        "handoff_hash": str(payload.get("handoff_hash") or ""),
        "status": "open",
        "created_at": str(payload.get("created_at") or timestamp),
        "confirmed_at": timestamp,
        "last_activity_at": timestamp,
        "completed_at": "",
        "archived_at": "",
        "expired_at": "",
        "completion_claim_proof": "",
    }


def _record_activity_timestamp(record: dict[str, Any]) -> str:
    return str(
        record.get("last_activity_at")
        or record.get("confirmed_at")
        or record.get("created_at")
        or ""
    )


def _sweep_lifecycle_records(
    records: dict[str, dict[str, Any]],
    now: dt.datetime,
    protected_ids: set[str] | None = None,
) -> bool:
    changed = False
    protected_ids = protected_ids or set()
    for record in records.values():
        if record.get("status") != "open":
            continue
        last_activity = parse_utc(_record_activity_timestamp(record))
        if last_activity is None or (now - last_activity).total_seconds() > OPEN_HANDOFF_TTL_SECONDS:
            timestamp = now.replace(microsecond=0).isoformat()
            record.update({"status": "expired", "expired_at": timestamp, "last_activity_at": timestamp})
            changed = True

    terminal = [
        (session_id, record)
        for session_id, record in records.items()
        if record.get("status") in {"completed", "archived", "expired", "abandoned"}
        and session_id not in protected_ids
    ]
    if len(records) > MAX_LIFECYCLE_RECORDS and terminal:
        terminal.sort(key=lambda item: parse_utc(_record_activity_timestamp(item[1])) or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
        for session_id, _record in terminal[: max(0, len(records) - MAX_LIFECYCLE_RECORDS)]:
            records.pop(session_id, None)
            changed = True
    return changed


def sweep_lifecycle(root: Path, *, now: dt.datetime | None = None) -> dict[str, Any]:
    """Expire abandoned open handoffs and enforce a hard lifecycle-record cap."""
    path = lifecycle_path(root)
    current_time = now or dt.datetime.now(dt.timezone.utc)
    with state_lock(path):
        current = read_json_object(path, "alignment lifecycle") if path.is_file() else None
        payload = current if current is not None else empty_lifecycle()
        validate_lifecycle(payload)
        current_payload = read_json_object(current_path(root), "current-alignment.json")
        current_id = str(current_payload.get("session_id", current_payload.get("alignment_id", ""))) if current_payload else ""
        known_ids = set(payload["records"])
        changed = _sweep_lifecycle_records(payload["records"], current_time)
        if changed:
            _write_lifecycle_locked(path, {"records": payload["records"]}, int(payload.get("revision", 0)))
        if current_id in known_ids and current_id not in payload["records"]:
            current_path(root).unlink(missing_ok=True)
        return payload


def lifecycle_record(root: Path, session_id: str) -> dict[str, Any] | None:
    validate_identity(session_id, "session_id", SESSION_ID_RE)
    return load_lifecycle(root).get("records", {}).get(session_id)


def adopt_current_handoff(root: Path) -> None:
    """Migrate only the latest legacy handoff into the bounded identity index."""
    current = load_current(root)
    if current is None:
        return
    session_id = str(current.get("session_id", current.get("alignment_id", "")))
    if not lifecycle_record(root, session_id):
        register_open_handoff(root, current)


def register_open_handoff(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    session_id = validate_identity(
        str(payload.get("session_id", payload.get("alignment_id", ""))), "session_id", SESSION_ID_RE
    )
    def mutate(records: dict[str, dict[str, Any]]) -> dict[str, Any]:
        existing = records.get(session_id)
        if existing is not None and existing.get("status") in {"completed", "archived", "expired", "abandoned"}:
            raise AlignmentStateError("alignment session is already closed and cannot be reopened")
        if existing is None and sum(record.get("status") == "open" for record in records.values()) >= MAX_LIFECYCLE_RECORDS:
            raise AlignmentStateError(
                "too many open alignment goals are retained; archive or complete an existing goal before starting another"
            )
        record = lifecycle_record_from_handoff(payload)
        if existing is not None:
            record["created_at"] = existing.get("created_at", record["created_at"])
        records[session_id] = record
        return record
    return update_lifecycle(root, mutate)


def close_lifecycle(
    root: Path,
    session_id: str,
    owner_id: str,
    status: str,
    *,
    claim_proof: str = "",
) -> dict[str, Any] | None:
    if status not in {"completed", "archived", "abandoned"}:
        raise AlignmentStateError("invalid lifecycle close status")
    validate_identity(session_id, "session_id", SESSION_ID_RE)
    validate_identity(owner_id, "owner_id", OWNER_ID_RE)
    now = utc_now()
    def mutate(records: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        record = records.get(session_id)
        if record is None:
            return None
        if record.get("owner_id") != owner_id:
            raise AlignmentStateError("alignment lifecycle owner mismatch")
        if record.get("status") in {"completed", "archived", "expired", "abandoned"}:
            raise AlignmentStateError(f"alignment goal is already {record.get('status')}")
        record["status"] = status
        record["last_activity_at"] = now
        record[f"{status}_at"] = now
        if claim_proof:
            record["completion_claim_proof"] = claim_proof
        return dict(record)
    return update_lifecycle(root, mutate)


def touch_lifecycle(root: Path, session_id: str, owner_id: str) -> dict[str, Any] | None:
    """Refresh an open handoff when its owner is actively using it."""
    validate_identity(session_id, "session_id", SESSION_ID_RE)
    validate_identity(owner_id, "owner_id", OWNER_ID_RE)
    now = utc_now()

    def mutate(records: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        record = records.get(session_id)
        if record is None or record.get("status") != "open":
            return None
        if record.get("owner_id") != owner_id:
            raise AlignmentStateError("alignment lifecycle owner mismatch")
        record["last_activity_at"] = now
        return dict(record)

    return update_lifecycle(root, mutate)


def mark_completed(root: Path, session_id: str, owner_id: str, claim_proof: str) -> dict[str, Any] | None:
    if lifecycle_record(root, session_id) is None:
        handoff = load_handoff(root, session_id)
        if handoff is not None:
            register_open_handoff(root, handoff)
    return close_lifecycle(root, session_id, owner_id, "completed", claim_proof=claim_proof)


def update_lifecycle_for_terminal_session(
    root: Path,
    state: dict[str, Any],
    status: str,
) -> dict[str, Any]:
    """Record abandoned live sessions without making them automatic candidates."""
    if status not in {"abandoned", "expired"}:
        raise AlignmentStateError("invalid terminal session lifecycle status")
    session_id = validate_identity(str(state.get("session_id", "")), "session_id", SESSION_ID_RE)
    owner_id = str(state.get("owner_id") or "")
    if owner_id:
        validate_identity(owner_id, "owner_id", OWNER_ID_RE)
    now = utc_now()

    def mutate(records: dict[str, dict[str, Any]]) -> dict[str, Any]:
        record = records.get(session_id) or {
            "session_id": session_id,
            "owner_id": owner_id,
            "conversation_id": str(state.get("conversation_id") or ""),
            "conversation_continuity_id": str(
                state.get("conversation_continuity_id") or state.get("conversation_id") or ""
            ),
            "tool": str(state.get("tool") or "unknown"),
            "goal": str(state.get("goal") or ""),
            "goal_fingerprint": str(state.get("goal_fingerprint") or ""),
            "handoff_hash": "",
            "status": "open",
            "created_at": str(state.get("created_at") or now),
            "confirmed_at": "",
            "last_activity_at": now,
            "completed_at": "",
            "archived_at": "",
            "expired_at": "",
            "completion_claim_proof": "",
        }
        record.update({"status": status, "last_activity_at": now, f"{status}_at": now})
        records[session_id] = record
        return dict(record)

    return update_lifecycle(root, mutate)


def archive_handoff(root: Path, payload: dict[str, Any], owner_id: str) -> dict[str, Any]:
    session_id = str(payload.get("session_id", payload.get("alignment_id", "")))
    record = lifecycle_record(root, session_id)
    if record is None:
        register_open_handoff(root, payload)
    closed = close_lifecycle(root, session_id, owner_id, "archived")
    if closed is None:
        raise AlignmentStateError("alignment lifecycle record is unavailable")
    return closed


def prune_alignment_history(root: Path, *, now: dt.datetime | None = None) -> int:
    """Bound terminal handoff files while preserving open goals and legacy pointers."""
    directory = history_directory(root)
    if not directory.is_dir():
        return 0
    current_id = ""
    current_file = current_path(root)
    try:
        current_payload = read_json_object(current_file, "current-alignment.json")
        if current_payload:
            current_id = str(current_payload.get("session_id", current_payload.get("alignment_id", "")))
    except AlignmentStateError:
        return 0
    lifecycle = load_lifecycle(root).get("records", {})
    protected = {session_id for session_id, record in lifecycle.items() if record.get("status") == "open"}
    current_lifecycle = lifecycle.get(current_id) if current_id else None
    if current_id and current_lifecycle is None:
        # Preserve a legacy pointer until adopt_current_handoff registers it.
        protected.add(current_id)
    files = [path for path in directory.glob("*.json") if path.is_file()]
    terminal: list[tuple[dt.datetime, Path]] = []
    for path in files:
        payload = read_json_object(path, "alignment history")
        if payload is None or payload.get("status") not in TERMINAL_STATUSES:
            continue
        timestamps = [
            parsed
            for field in ("updated_at", "confirmed_at", "archived_at")
            if (parsed := parse_utc(payload.get(field))) is not None
        ]
        timestamp = max(timestamps, default=dt.datetime.fromtimestamp(0, tz=dt.timezone.utc))
        terminal.append((timestamp, path))
    terminal.sort(key=lambda item: item[0])
    cutoff = (now or dt.datetime.now(dt.timezone.utc)) - dt.timedelta(seconds=HISTORY_RETENTION_SECONDS)
    removed = 0
    remaining_count = len(files)
    for timestamp, path in terminal:
        session_id = path.stem
        if session_id in protected:
            continue
        if timestamp >= cutoff and remaining_count <= MAX_HISTORY_RECORDS:
            continue
        path.unlink(missing_ok=True)
        if session_id == current_id:
            current_file.unlink(missing_ok=True)
        remaining_count -= 1
        removed += 1
    return removed


def sweep_alignment_state(root: Path) -> None:
    sweep_lifecycle(root)
    prune_alignment_history(root)
    adopt_current_handoff(root)


def require_harness(root: Path) -> None:
    if not (root / ".tenetora").is_dir():
        raise AlignmentStateError(
            harness_missing_message(root)
        )


def read_json_object(path: Path, label: str) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        recovery = (
            "run tenetora alignment --abandon --legacy-unowned after preserving any needed summary"
            if label == "alignment-session.json"
            else (
                "run tenetora alignment --abandon --session-id <id> --owner-id <owner> after preserving any needed summary"
                if label == "alignment session"
                else "start a new alignment to replace the damaged current handoff after preserving any needed summary"
            )
        )
        raise AlignmentStateError(
            f"{label} is unreadable ({exc}). Inspect or remove the damaged file, or {recovery}."
        ) from exc
    if not isinstance(payload, dict):
        raise AlignmentStateError(f"{label} must contain a JSON object")
    return payload


def migrate_session(payload: dict[str, Any]) -> dict[str, Any]:
    version = payload.get("version", 0)
    if not isinstance(version, int):
        raise AlignmentStateError("alignment-session.json has an invalid schema version")
    migrated = dict(payload)
    if version == 0:
        if "id" in migrated and "session_id" not in migrated:
            migrated["session_id"] = migrated.pop("id")
        for field in ALIGNMENT_LIST_FIELDS:
            migrated.setdefault(field, [])
        migrated.setdefault("transitions", [])
        migrated.setdefault("batch_question_count", 0)
        migrated.setdefault("total_question_count", 0)
        migrated.setdefault("checkpoint_count", 0)
        migrated.setdefault("current_question", "")
        migrated.setdefault("blocked_reason", "")
        if not migrated.get("goal_fingerprint") and isinstance(migrated.get("goal"), str):
            migrated["goal_fingerprint"] = goal_fingerprint(
                migrated["goal"],
                list(migrated.get("scope", [])),
                list(migrated.get("non_goals", [])),
                list(migrated.get("acceptance_criteria", [])),
            )
        migrated["version"] = 1
        version = 1
    if version == 1:
        # Version 1 was a project-level singleton. Keep it readable, but mark it
        # unowned so a new conversation can never inherit it implicitly.
        migrated.setdefault("owner_id", "")
        migrated.setdefault("conversation_id", "")
        migrated.setdefault("tool", "unknown")
        migrated.setdefault("heartbeat_at", migrated.get("updated_at", ""))
        migrated.setdefault("expires_at", "")
        migrated.setdefault("revision", 0)
        migrated.setdefault("base_current_revision", 0)
        migrated["legacy_unowned"] = True
        migrated["version"] = 2
        version = 2
    if version == 2:
        migrated.setdefault("conversation_continuity_id", migrated.get("conversation_id", ""))
        migrated.setdefault("continuity_available", False)
        migrated.setdefault("execution_attempts", [])
        migrated["version"] = 3
        version = 3
    if version == 3:
        migrated.setdefault("confirmation_request", {})
        migrated["version"] = 4
        version = 4
    if version > CURRENT_SCHEMA_VERSION:
        raise AlignmentStateError(
            f"alignment-session.json schema {version} is newer than supported {CURRENT_SCHEMA_VERSION}; upgrade Tenetora"
        )
    return migrated


def session_files(root: Path) -> list[Path]:
    directory = session_directory(root)
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.json") if path.is_file())


def load_session_file(path: Path, label: str, expected_session_id: str = "") -> dict[str, Any] | None:
    payload = read_json_object(path, label)
    if payload is None:
        return None
    state = migrate_session(payload)
    validate_session(state)
    if expected_session_id and str(state.get("session_id")) != expected_session_id:
        raise AlignmentStateError("alignment session id does not match its state file")
    expires_at = parse_utc(state.get("expires_at"))
    if state.get("status") in ACTIVE_STATUSES and expires_at is not None and expires_at <= dt.datetime.now(dt.timezone.utc):
        state = dict(state)
        state.update(
            {
                "status": "expired",
                "expired_from_status": state.get("status"),
                "stale": True,
                "stale_reason": "heartbeat lease expired",
            }
        )
    return state


def running_execution_attempt_count(state: dict[str, Any]) -> int:
    attempts = state.get("execution_attempts", [])
    if not isinstance(attempts, list):
        return 0
    return sum(
        1
        for attempt in attempts
        if isinstance(attempt, dict) and attempt.get("status") == "running"
    )


def expired_session_requires_attention(state: dict[str, Any]) -> bool:
    """Keep expired sessions actionable only when work or user confirmation is still pending."""
    if state.get("status") != "expired":
        return False
    return (
        state.get("expired_from_status") == "awaiting-confirmation"
        or running_execution_attempt_count(state) > 0
    )


def session_requires_explicit_identity(state: dict[str, Any]) -> bool:
    return state.get("status") in ACTIVE_STATUSES or expired_session_requires_attention(state)


def load_session(root: Path, session_id: str = "", *, required: bool = False) -> dict[str, Any] | None:
    if session_id:
        state = load_session_file(isolated_session_path(root, session_id), "alignment session", session_id)
        if state is None and required:
            raise AlignmentStateError("No matching alignment session. Start one with tenetora alignment --start.")
        return state

    legacy = session_path(root)
    legacy_state = load_session_file(legacy, "alignment-session.json") if legacy.is_file() else None
    isolated_states = [
        state
        for path in session_files(root)
        if (state := load_session_file(path, "alignment session", path.stem)) is not None
    ]
    unresolved = [
        state
        for state in ([legacy_state] if legacy_state is not None else []) + isolated_states
        if session_requires_explicit_identity(state)
    ]
    if len(unresolved) > 1:
        raise AlignmentStateError(
            "multiple alignment sessions exist; pass --session-id for an explicit session or use --list"
        )
    if unresolved:
        return unresolved[0]
    selectable_isolated_states = [
        state
        for state in isolated_states
        if state.get("status") != "expired" or expired_session_requires_attention(state)
    ]
    if len(selectable_isolated_states) == 1:
        return selectable_isolated_states[0]
    if len(selectable_isolated_states) > 1:
        raise AlignmentStateError(
            "multiple alignment sessions exist; pass --session-id for an explicit session or use --list"
        )
    if legacy_state is not None and (
        legacy_state.get("status") != "expired" or expired_session_requires_attention(legacy_state)
    ):
        return legacy_state
    if required:
        raise AlignmentStateError("No matching alignment session. Start one with tenetora alignment --start.")
    return None


def load_all_sessions(root: Path) -> list[dict[str, Any]]:
    """Load every live state record without selecting a conversation implicitly."""

    states: list[dict[str, Any]] = []
    legacy = session_path(root)
    if legacy.is_file():
        legacy_state = load_session_file(legacy, "alignment-session.json")
        if legacy_state is not None:
            states.append(legacy_state)
    for path in session_files(root):
        state = load_session_file(path, "alignment session", path.stem)
        if state is not None:
            states.append(state)
    return states


def session_summary(
    state: dict[str, Any],
    *,
    legacy_unowned: bool = False,
    include_goal: bool = True,
) -> dict[str, Any]:
    summary = {
        "session_id": state.get("session_id", ""),
        "owner_id": state.get("owner_id") or "unowned",
        "conversation_id": state.get("conversation_id") or "",
        "conversation_continuity_id": state.get("conversation_continuity_id") or "",
        "continuity_available": bool(state.get("continuity_available", False)),
        "tool": state.get("tool", "unknown"),
        "status": state.get("status", "unknown"),
        "goal_fingerprint": state.get("goal_fingerprint", ""),
        "risk_level": state.get("risk_level", ""),
        "created_at": state.get("created_at", ""),
        "updated_at": state.get("updated_at", ""),
        "heartbeat_at": state.get("heartbeat_at", ""),
        "expires_at": state.get("expires_at", ""),
        "revision": state.get("revision", 0),
        "execution_attempt_count": len(state.get("execution_attempts", []))
        if isinstance(state.get("execution_attempts"), list)
        else 0,
        "running_execution_attempts": sum(
            1
            for attempt in state.get("execution_attempts", [])
            if isinstance(attempt, dict) and attempt.get("status") == "running"
        ),
    }
    if state.get("expired_from_status"):
        summary["expired_from_status"] = state.get("expired_from_status")
    if include_goal:
        summary["goal"] = state.get("goal", "")
    if state.get("stale"):
        summary.update({"stale": True, "stale_reason": state.get("stale_reason", "")})
    if legacy_unowned or state.get("legacy_unowned"):
        summary.update({"legacy_unowned": True, "status": "legacy-unowned"})
    return summary


def all_session_summaries(root: Path, *, include_goal: bool = True) -> list[dict[str, Any]]:
    legacy_exists = session_path(root).is_file()
    summaries = [
        session_summary(
            state,
            legacy_unowned=legacy_exists and index == 0,
            include_goal=include_goal,
        )
        for index, state in enumerate(load_all_sessions(root))
    ]
    by_id = {str(item.get("session_id")): item for item in summaries}
    for session_id, lifecycle in load_lifecycle(root).get("records", {}).items():
        if session_id in by_id:
            continue
        handoff = load_handoff(root, session_id)
        if handoff is None:
            continue
        summary = session_summary(handoff, include_goal=include_goal)
        lifecycle_status = str(lifecycle.get("status") or "")
        summary.update(
            {
                "status": lifecycle_status if lifecycle_status != "open" else "confirmed",
                "lifecycle_status": lifecycle_status,
            }
        )
        summaries.append(summary)
        by_id[session_id] = summary
    return summaries


def _identity_candidates(
    root: Path,
    field: str,
    value: str,
    *,
    include_closed: bool = False,
) -> list[dict[str, Any]]:
    """Return only live sessions and registered open handoffs.

    The history directory is an audit log, not an identity index. In particular,
    old confirmed handoffs without a lifecycle record must never become an
    automatic binding candidate merely because their conversation id matches.
    """
    if not value:
        return []
    sweep_alignment_state(root)
    candidates: dict[str, dict[str, Any]] = {}
    for state in load_all_sessions(root):
        if not session_requires_explicit_identity(state) or state.get(field) != value:
            continue
        session_id = str(state.get("session_id", ""))
        candidates[session_id] = {
            "session_id": session_id,
            "owner_id": str(state.get("owner_id") or ""),
            "conversation_id": str(state.get("conversation_id") or ""),
            "conversation_continuity_id": str(state.get("conversation_continuity_id") or ""),
            "goal": str(state.get("goal") or ""),
            "status": str(state.get("status") or ""),
        }
    allowed_lifecycle_statuses = {"open"} | ({"completed"} if include_closed else set())
    for session_id, lifecycle in load_lifecycle(root).get("records", {}).items():
        if lifecycle.get("status") not in allowed_lifecycle_statuses or lifecycle.get(field) != value:
            continue
        handoff = load_handoff(root, session_id)
        if handoff is None:
            continue
        candidates.setdefault(
            session_id,
            {
                "session_id": session_id,
                "owner_id": str(handoff.get("owner_id") or ""),
                "conversation_id": str(handoff.get("conversation_id") or ""),
                "conversation_continuity_id": str(handoff.get("conversation_continuity_id") or ""),
                "goal": str(handoff.get("goal") or ""),
                "status": "confirmed",
            },
        )
    return list(candidates.values())


def _select_identity_candidate(
    root: Path,
    field: str,
    value: str,
    label: str,
    *,
    include_closed: bool = False,
) -> str | None:
    candidates = _identity_candidates(root, field, value, include_closed=include_closed)
    if len(candidates) > 1:
        choices = "; ".join(
            f"{item['session_id']} ({item['goal'][:120]})" for item in candidates
        )
        raise AlignmentIdentityAmbiguous(
            f"multiple alignment sessions belong to this {label}; pass --session-id explicitly; candidates: {choices}",
            candidates,
        )
    return str(candidates[0]["session_id"]) if candidates else None


def session_id_for_conversation(root: Path, conversation_id: str, *, include_closed: bool = False) -> str | None:
    return _select_identity_candidate(root, "conversation_id", conversation_id, "conversation", include_closed=include_closed)


def session_id_for_conversation_continuity(
    root: Path,
    continuity_id: str,
    *,
    include_closed: bool = False,
) -> str | None:
    return _select_identity_candidate(
        root, "conversation_continuity_id", continuity_id, "conversation continuity", include_closed=include_closed
    )


def session_id_for_owner(root: Path, owner_id: str, *, include_closed: bool = False) -> str | None:
    """Find one live or registered open alignment goal owned by an explicit owner."""
    return _select_identity_candidate(root, "owner_id", owner_id, "owner", include_closed=include_closed)


def require_owned_session(
    root: Path,
    args: argparse.Namespace,
    *,
    allow_expired: bool = False,
) -> tuple[dict[str, Any], Path]:
    session_id = requested_session_id(args, required=True)
    owner_id = requested_owner_id(args, session_id, required=True)
    state = load_session(root, session_id, required=True)
    assert state is not None
    if state.get("legacy_unowned") or not state.get("owner_id"):
        raise AlignmentStateError(
            "legacy alignment session has no owner and cannot be mutated; start a new isolated session or use explicit legacy recovery"
        )
    if state.get("owner_id") != owner_id:
        raise AlignmentStateError("alignment session owner mismatch; the current owner cannot mutate this session")
    if state.get("status") == "expired" and not allow_expired:
        raise AlignmentStateError("alignment session lease expired; start a new session instead of resuming it")
    return state, isolated_session_path(root, session_id)


def refresh_lease(state: dict[str, Any]) -> None:
    now = utc_now()
    state["heartbeat_at"] = now
    ttl = AWAITING_CONFIRMATION_TTL_SECONDS if state.get("status") == "awaiting-confirmation" else DEFAULT_TTL_SECONDS
    state["expires_at"] = utc_after(ttl)
    state["updated_at"] = now


def persist_session(path: Path, state: dict[str, Any]) -> dict[str, Any]:
    written = write_json(path, state, expected_revision=int(state.get("revision", 0)))
    return written


def load_current(root: Path) -> dict[str, Any] | None:
    payload = read_json_object(current_path(root), "current-alignment.json")
    if payload is None:
        return None
    validate_handoff(payload, "current-alignment.json")
    return payload


def validate_handoff(payload: dict[str, Any], label: str = "alignment handoff") -> None:
    if payload.get("version") not in {1, 2, 3, CURRENT_SCHEMA_VERSION}:
        raise AlignmentStateError(f"{label} has an unsupported schema version")
    status = payload.get("status")
    if status not in {"confirmed", "accepted-with-risks"}:
        raise AlignmentStateError(f"{label} has an invalid terminal status")
    if status == "accepted-with-risks" and not payload.get("accepted_risks"):
        raise AlignmentStateError("accepted-with-risks alignment must contain at least one accepted risk")
    if not isinstance(payload.get("alignment_id"), str) or not SESSION_ID_RE.fullmatch(str(payload.get("alignment_id", ""))):
        raise AlignmentStateError(f"{label} has an invalid alignment_id")
    if payload.get("risk_level") not in VALID_RISK_LEVELS:
        raise AlignmentStateError(f"{label} has an invalid risk_level")
    if not isinstance(payload.get("goal"), str):
        raise AlignmentStateError(f"{label} goal must be a string")
    ensure_safe_text(payload["goal"], "goal")
    for field in ALIGNMENT_LIST_FIELDS:
        validate_text_list(payload, field)
    validate_handoff_relative_path(payload.get("handoff_relative_path"))
    if payload.get("version") == CURRENT_SCHEMA_VERSION:
        validate_confirmation_record(payload, label)
        if not isinstance(payload.get("execution_attempts", []), list):
            raise AlignmentStateError(f"{label} execution_attempts must be a list")


def load_handoff(root: Path, session_id: str) -> dict[str, Any] | None:
    """Load one terminal handoff by session id without consulting another session's latest pointer."""
    validate_identity(session_id, "session_id", SESSION_ID_RE)
    history_file = history_directory(root) / f"{session_id}.json"
    payload = read_json_object(history_file, "alignment history")
    if payload is not None:
        # Abandoned and expired sessions are audit history, not consumable
        # handoffs. They must not make a later guard fail as if a malformed
        # confirmed handoff existed.
        if payload.get("status") in {"abandoned", "expired"}:
            return None
        validate_handoff(payload, "alignment history")
        if payload.get("session_id", payload.get("alignment_id")) != session_id:
            raise AlignmentStateError("alignment history id does not match its state file")
        return payload
    current = load_current(root)
    if current is not None and current.get("session_id", current.get("alignment_id")) == session_id:
        return current
    return None


def validate_session(state: dict[str, Any]) -> None:
    if state.get("status") not in ACTIVE_STATUSES | TERMINAL_STATUSES:
        raise AlignmentStateError(f"alignment-session.json has invalid status: {state.get('status')}")
    if state.get("risk_level") not in VALID_RISK_LEVELS:
        raise AlignmentStateError("alignment-session.json has invalid risk_level")
    if not isinstance(state.get("session_id"), str) or not SESSION_ID_RE.fullmatch(str(state.get("session_id", ""))):
        raise AlignmentStateError("alignment-session.json has invalid session_id")
    owner_id = state.get("owner_id", "")
    if not isinstance(owner_id, str) or (owner_id and not OWNER_ID_RE.fullmatch(owner_id)):
        raise AlignmentStateError("alignment-session.json has invalid owner_id")
    conversation_id = state.get("conversation_id", "")
    if not isinstance(conversation_id, str) or (conversation_id and not OWNER_ID_RE.fullmatch(conversation_id)):
        raise AlignmentStateError("alignment-session.json has invalid conversation_id")
    tool = state.get("tool", "unknown")
    if not isinstance(tool, str) or not OWNER_ID_RE.fullmatch(tool):
        raise AlignmentStateError("alignment-session.json has invalid tool")
    continuity_id = state.get("conversation_continuity_id", conversation_id)
    if not isinstance(continuity_id, str) or (continuity_id and not OWNER_ID_RE.fullmatch(continuity_id)):
        raise AlignmentStateError("alignment-session.json has invalid conversation_continuity_id")
    if not isinstance(state.get("continuity_available", False), bool):
        raise AlignmentStateError("alignment-session.json has invalid continuity_available")
    attempts = state.get("execution_attempts", [])
    if not isinstance(attempts, list):
        raise AlignmentStateError("alignment-session.json execution_attempts must be a list")
    seen_attempts: set[str] = set()
    for attempt in attempts:
        if not isinstance(attempt, dict):
            raise AlignmentStateError("alignment execution attempt must be an object")
        attempt_id = attempt.get("execution_attempt_id")
        if not isinstance(attempt_id, str) or not OWNER_ID_RE.fullmatch(attempt_id) or attempt_id in seen_attempts:
            raise AlignmentStateError("alignment execution attempt has an invalid or duplicate id")
        seen_attempts.add(attempt_id)
        if attempt.get("alignment_session_id") != state.get("session_id"):
            raise AlignmentStateError("alignment execution attempt session mismatch")
        for field in ("owner_id", "conversation_continuity_id", "tool", "provider", "model"):
            value = attempt.get(field, "")
            if not isinstance(value, str) or (field != "conversation_continuity_id" and not value):
                raise AlignmentStateError(f"alignment execution attempt has invalid {field}")
            if value and field in {"owner_id", "conversation_continuity_id", "tool", "provider", "model"}:
                ensure_safe_text(value, f"execution attempt {field}")
        if attempt.get("status") not in EXECUTION_ATTEMPT_STATUSES:
            raise AlignmentStateError("alignment execution attempt has invalid status")
        for field in ("base_revision", "revision"):
            value = attempt.get(field, 0)
            if type(value) is not int or value < 0:
                raise AlignmentStateError(f"alignment execution attempt has invalid {field}")
        for field in ("created_at", "updated_at", "ended_at"):
            if attempt.get(field) and parse_utc(attempt.get(field)) is None:
                raise AlignmentStateError(f"alignment execution attempt has invalid {field}")
        if attempt.get("reason"):
            ensure_safe_text(str(attempt["reason"]), "execution attempt reason")
    revision = state.get("revision", 0)
    if not isinstance(revision, int) or revision < 0:
        raise AlignmentStateError("alignment-session.json has invalid revision")
    count = state.get("batch_question_count")
    if not isinstance(count, int) or not 0 <= count <= MAX_BATCH_QUESTIONS:
        raise AlignmentStateError("alignment-session.json has invalid batch_question_count")
    for field in ("total_question_count", "checkpoint_count"):
        value = state.get(field)
        if not isinstance(value, int) or value < 0:
            raise AlignmentStateError(f"alignment-session.json has invalid {field}")
    if not isinstance(state.get("goal"), str):
        raise AlignmentStateError("alignment-session.json goal must be a string")
    ensure_safe_text(state["goal"], "goal")
    for field in ALIGNMENT_LIST_FIELDS:
        validate_text_list(state, field)
    for field in ("current_question", "blocked_reason"):
        value = state.get(field, "")
        if not isinstance(value, str):
            raise AlignmentStateError(f"alignment state field {field} must be a string")
        if value.strip():
            ensure_safe_text(value, field)
    for field in ("created_at", "updated_at", "heartbeat_at"):
        if state.get(field) and parse_utc(state.get(field)) is None:
            raise AlignmentStateError(f"alignment-session.json has invalid {field}")
    if state.get("expires_at") and parse_utc(state.get("expires_at")) is None:
        raise AlignmentStateError("alignment-session.json has invalid expires_at")
    confirmation_request = state.get("confirmation_request", {})
    if not isinstance(confirmation_request, dict):
        raise AlignmentStateError("alignment-session.json has invalid confirmation_request")
    if confirmation_request:
        if confirmation_request.get("goal_fingerprint") != state.get("goal_fingerprint"):
            raise AlignmentStateError("alignment confirmation request goal fingerprint mismatch")
        if type(confirmation_request.get("revision")) is not int or confirmation_request["revision"] < 0:
            raise AlignmentStateError("alignment confirmation request has invalid revision")
        for field in ("requested_at", "expires_at"):
            if parse_utc(confirmation_request.get(field)) is None:
                raise AlignmentStateError(f"alignment confirmation request has invalid {field}")
    base_revision = state.get("base_current_revision", 0)
    if type(base_revision) is not int or base_revision < 0:
        raise AlignmentStateError("alignment-session.json has invalid base_current_revision")


def write_json(path: Path, payload: dict[str, Any], *, expected_revision: int | None = None) -> dict[str, Any]:
    """Write with a per-record lock and revision check to reject lost updates."""
    with state_lock(path):
        current = read_json_object(path, "alignment state") if path.is_file() else None
        current_revision = 0
        if current is not None:
            raw_revision = current.get("revision", 0)
            if type(raw_revision) is not int or raw_revision < 0:
                raise AlignmentStateError("alignment state has an invalid revision")
            current_revision = raw_revision
        if expected_revision is not None and current_revision != expected_revision:
            raise AlignmentStateError(
                "alignment state changed concurrently; reload the matching session before writing"
            )
        written = dict(payload)
        written["revision"] = current_revision + 1
        atomic_write_text(path, json.dumps(written, ensure_ascii=False, indent=2) + "\n")
        return written


def append_unique(state: dict[str, Any], field: str, values: list[str]) -> None:
    existing = state.get(field, [])
    if not isinstance(existing, list):
        existing = []
    for value in values:
        if value not in existing:
            existing.append(value)
    state[field] = existing


def apply_structured_updates(state: dict[str, Any], args: argparse.Namespace) -> None:
    mapping = {
        "scope": "scope",
        "non_goal": "non_goals",
        "constraint": "constraints",
        "assumption": "assumptions",
        "approval_boundary": "approval_boundaries",
        "acceptance_criterion": "acceptance_criteria",
        "verification": "verification",
        "open_question": "open_questions",
    }
    for arg_name, field in mapping.items():
        values = safe_list(getattr(args, arg_name), field)
        append_unique(state, field, values)
    state["goal_fingerprint"] = goal_fingerprint(
        str(state.get("goal", "")),
        list(state.get("scope", [])),
        list(state.get("non_goals", [])),
        list(state.get("acceptance_criteria", [])),
    )


def transition(state: dict[str, Any], target: str, allowed_from: set[str]) -> None:
    source = str(state.get("status", ""))
    if source not in allowed_from:
        raise AlignmentStateError(f"invalid alignment transition: {source} -> {target}")
    now = utc_now()
    transitions = state.get("transitions", [])
    if not isinstance(transitions, list):
        transitions = []
    transitions.append({"at": now, "from": source, "to": target})
    state["transitions"] = transitions[-100:]
    state["status"] = target
    state["updated_at"] = now


def new_session(
    args: argparse.Namespace,
    session_id: str,
    owner_id: str,
    *,
    base_current_revision: int = 0,
) -> dict[str, Any]:
    goal = ensure_safe_text(args.goal or "", "goal")
    if args.risk_level not in VALID_RISK_LEVELS:
        raise AlignmentStateError("--risk-level is required when starting alignment")
    now = utc_now()
    state: dict[str, Any] = {
        "version": CURRENT_SCHEMA_VERSION,
        "session_id": session_id,
        "owner_id": owner_id,
        "conversation_id": requested_conversation_id(args, owner_id),
        "conversation_continuity_id": requested_conversation_continuity_id(args, owner_id),
        "continuity_available": has_explicit_conversation_continuity(args),
        "tool": requested_tool(args),
        "status": "active",
        "goal": goal,
        "risk_level": args.risk_level,
        "goal_fingerprint": "",
        "scope": [],
        "non_goals": [],
        "decisions": [],
        "constraints": [],
        "assumptions": [],
        "accepted_risks": [],
        "approval_boundaries": [],
        "acceptance_criteria": [],
        "verification": [],
        "open_questions": [],
        "current_question": "",
        "batch_question_count": 0,
        "total_question_count": 0,
        "checkpoint_count": 0,
        "blocked_reason": "",
        "created_at": now,
        "updated_at": now,
        "heartbeat_at": now,
        "expires_at": utc_after(),
        "revision": 0,
        "base_current_revision": base_current_revision,
        "legacy_unowned": False,
        "transitions": [{"at": now, "from": "idle", "to": "active"}],
        "execution_attempts": [],
        "confirmation_request": {},
    }
    apply_structured_updates(state, args)
    return state


def confirmation_record(state: dict[str, Any], args: argparse.Namespace, confirmed_at: str) -> dict[str, Any]:
    source = str(getattr(args, "confirmation_source", "") or "").strip()
    actor_id = str(getattr(args, "confirmation_actor_id", "") or "").strip()
    event_id = str(getattr(args, "confirmation_event_id", "") or "").strip()
    reason = str(getattr(args, "confirmation_reason", "") or "").strip()
    request = state.get("confirmation_request") if isinstance(state.get("confirmation_request"), dict) else {}
    if source not in CONFIRMATION_SOURCES:
        raise AlignmentStateError(
            "confirmation requires --confirmation-source user-message with matching actor and event ids"
        )
    if not request:
        raise AlignmentStateError("audited confirmation requires a current confirmation request")
    request_expiry = parse_utc(request.get("expires_at"))
    if request_expiry is None or request_expiry <= dt.datetime.now(dt.timezone.utc):
        raise AlignmentStateError("the confirmation request expired; resume and request confirmation again")
    if request.get("goal_fingerprint") != state.get("goal_fingerprint"):
        raise AlignmentStateError("confirmation request goal fingerprint mismatch")
    if request.get("revision") != state.get("revision"):
        raise AlignmentStateError("confirmation request revision mismatch; reload the current summary")
    actor_id = validate_identity(actor_id, "confirmation_actor_id", OWNER_ID_RE)
    event_id = validate_identity(event_id, "confirmation_event_id", OWNER_ID_RE)
    allowed_actors = {
        str(state.get("owner_id") or ""),
        str(state.get("conversation_id") or ""),
        str(state.get("conversation_continuity_id") or ""),
    }
    if actor_id not in allowed_actors:
        raise AlignmentStateError("confirmation actor does not match the alignment owner or conversation continuity")
    if reason:
        reason = ensure_safe_text(reason, "confirmation reason")
    assurance = "recorded-user-assertion"
    return {
        "source": source,
        "assurance": assurance,
        "actor_id": actor_id,
        "event_id": event_id,
        "reason": reason,
        "authorized_at": confirmed_at,
        "request_revision": int(request.get("revision", state.get("revision", 0))),
        "goal_fingerprint": str(state.get("goal_fingerprint") or ""),
        "conversation_continuity_id": str(
            state.get("conversation_continuity_id") or state.get("conversation_id") or ""
        ),
    }


def validate_confirmation_record(payload: dict[str, Any], label: str) -> None:
    record = payload.get("confirmation")
    if not isinstance(record, dict):
        raise AlignmentStateError(f"{label} has no confirmation authorization record")
    source = record.get("source")
    assurance = record.get("assurance")
    allowed = {
        "user-message": "recorded-user-assertion",
    }
    if source not in allowed or assurance != allowed[source]:
        raise AlignmentStateError(f"{label} has invalid confirmation assurance")
    for field in ("actor_id", "event_id"):
        value = record.get(field, "")
        if not isinstance(value, str) or (value and not OWNER_ID_RE.fullmatch(value)):
            raise AlignmentStateError(f"{label} has invalid confirmation {field}")
    if not record.get("actor_id") or not record.get("event_id"):
        raise AlignmentStateError(f"{label} has incomplete confirmation authorization metadata")
    if parse_utc(record.get("authorized_at")) is None:
        raise AlignmentStateError(f"{label} has invalid confirmation timestamp")
    if type(record.get("request_revision")) is not int or record["request_revision"] < 0:
        raise AlignmentStateError(f"{label} has invalid confirmation revision")
    if record.get("goal_fingerprint") != payload.get("goal_fingerprint"):
        raise AlignmentStateError(f"{label} confirmation goal fingerprint mismatch")
    expected_continuity = payload.get("conversation_continuity_id", payload.get("conversation_id", ""))
    if record.get("conversation_continuity_id") != expected_continuity:
        raise AlignmentStateError(f"{label} confirmation conversation continuity mismatch")
    reason = record.get("reason", "")
    if not isinstance(reason, str):
        raise AlignmentStateError(f"{label} has invalid confirmation reason")
    if reason:
        ensure_safe_text(reason, "confirmation reason")


def semantic_handoff(
    state: dict[str, Any],
    status: str,
    confirmed_at: str,
    confirmation: dict[str, Any],
) -> dict[str, Any]:
    return {
        "version": CURRENT_SCHEMA_VERSION,
        "alignment_id": state["session_id"],
        "session_id": state["session_id"],
        "owner_id": state.get("owner_id", ""),
        "conversation_id": state.get("conversation_id", ""),
        "conversation_continuity_id": state.get("conversation_continuity_id", state.get("conversation_id", "")),
        "tool": state.get("tool", "unknown"),
        "status": status,
        "goal": state["goal"],
        "risk_level": state["risk_level"],
        "goal_fingerprint": state["goal_fingerprint"],
        "scope": state["scope"],
        "non_goals": state["non_goals"],
        "decisions": state["decisions"],
        "constraints": state["constraints"],
        "assumptions": state["assumptions"],
        "accepted_risks": state["accepted_risks"],
        "approval_boundaries": state["approval_boundaries"],
        "acceptance_criteria": state["acceptance_criteria"],
        "verification": state["verification"],
        "open_questions": state["open_questions"],
        "execution_attempts": state.get("execution_attempts", []),
        "confirmed_at": confirmed_at,
        "confirmation": confirmation,
    }


def handoff_hash(payload: dict[str, Any]) -> str:
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {"handoff_hash", "handoff_relative_path", "revision"}
    }
    canonical = json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def current_handoff_revision(root: Path) -> int:
    path = current_path(root)
    with state_lock(path):
        current = read_json_object(path, "current-alignment.json")
        if current is None:
            return 0
        validate_handoff(current, "current-alignment.json")
        revision = current.get("revision", 0)
        if type(revision) is not int or revision < 0:
            raise AlignmentStateError("current-alignment.json has an invalid revision")
        return revision


def publish_current_handoff(
    root: Path,
    payload: dict[str, Any],
    *,
    expected_revision: int,
) -> tuple[bool, dict[str, Any]]:
    """Publish the latest pointer only when it still matches the session baseline."""

    path = current_path(root)
    with state_lock(path):
        current = read_json_object(path, "current-alignment.json")
        current_revision = 0
        if current is not None:
            validate_handoff(current, "current-alignment.json")
            raw_revision = current.get("revision", 0)
            if type(raw_revision) is not int or raw_revision < 0:
                raise AlignmentStateError("current-alignment.json has an invalid revision")
            current_revision = raw_revision
        if current_revision != expected_revision:
            return False, dict(payload)
        written = dict(payload)
        written["revision"] = current_revision + 1
        atomic_write_text(path, json.dumps(written, ensure_ascii=False, indent=2) + "\n")
        return True, written


def finalize(
    root: Path,
    session_file: Path,
    state: dict[str, Any],
    status: str,
    args: argparse.Namespace,
    accepted_risk: str | None = None,
) -> dict[str, Any]:
    if accepted_risk is not None:
        risk = ensure_safe_text(accepted_risk, "accepted risk")
        append_unique(state, "accepted_risks", [risk])
    if status == "accepted-with-risks" and not state.get("accepted_risks"):
        raise AlignmentStateError("accepted-with-risks requires at least one non-empty accepted risk")
    transition(state, status, {"awaiting-confirmation"})
    refresh_lease(state)
    confirmed_at = utc_now()
    payload = semantic_handoff(state, status, confirmed_at, confirmation_record(state, args, confirmed_at))
    payload["handoff_hash"] = handoff_hash(payload)
    payload["handoff_relative_path"] = None
    history_file = history_directory(root) / f"{state['session_id']}.json"
    expected_revision = state.get("revision", 0)
    with state_lock(session_file):
        current = read_json_object(session_file, "alignment session")
        if current is None:
            raise AlignmentStateError("alignment session disappeared before finalization")
        current_state = migrate_session(current)
        validate_session(current_state)
        if current_state.get("revision", 0) != expected_revision:
            raise AlignmentStateError(
                "alignment state changed concurrently; reload the matching session before finalization"
            )
        history_payload = write_json(history_file, payload, expected_revision=0)
        register_open_handoff(root, history_payload)
        pointer_updated, _current_payload = publish_current_handoff(
            root,
            payload,
            expected_revision=int(state.get("base_current_revision", 0)),
        )
        session_file.unlink(missing_ok=True)
    prune_alignment_history(root)
    result = dict(history_payload)
    result["current_pointer_updated"] = pointer_updated
    return result


def archive_abandoned(root: Path, session_file: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Archive an owned abandoned session without allowing a stale reader to delete newer state."""
    history_file = history_directory(root) / f"{state['session_id']}.json"
    expected_revision = state.get("revision", 0)
    record = dict(state)
    record.update({"record_type": "terminal-session", "archived_at": utc_now()})
    with state_lock(session_file):
        current = read_json_object(session_file, "alignment session")
        if current is None:
            raise AlignmentStateError("alignment session disappeared before abandonment")
        current_state = migrate_session(current)
        validate_session(current_state)
        if current_state.get("revision", 0) != expected_revision:
            raise AlignmentStateError(
                "alignment state changed concurrently; reload the matching session before abandonment"
            )
        write_json(history_file, record)
        update_lifecycle_for_terminal_session(root, record, "abandoned")
        session_file.unlink(missing_ok=True)
    prune_alignment_history(root)
    return record


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", normalize_text(value)).strip("-")
    return (slug[:64].strip("-") or "decision") + "-alignment"


def markdown_list(values: list[str]) -> str:
    return "\n".join(f"- {value}" for value in values) if values else ("- 无" if use_chinese() else "- None")


def render_handoff_markdown(payload: dict[str, Any]) -> str:
    headings = (
        ("范围", "非目标", "决策", "约束", "假设", "已接受风险", "审批边界", "验收标准", "验证", "开放问题")
        if use_chinese()
        else ("Scope", "Non-Goals", "Decisions", "Constraints", "Assumptions", "Accepted Risks", "Approval Boundaries", "Acceptance Criteria", "Verification", "Open Questions")
    )
    sections = list(zip(headings, [payload.get(key, []) for key in ("scope", "non_goals", "decisions", "constraints", "assumptions", "accepted_risks", "approval_boundaries", "acceptance_criteria", "verification", "open_questions")]))
    confirmation = payload.get("confirmation") if isinstance(payload.get("confirmation"), dict) else {}
    lines = [
        "# 决策对齐交接" if use_chinese() else "# Decision Alignment Handoff",
        "",
        f"- {'对齐 ID' if use_chinese() else 'Alignment ID'}: `{payload['alignment_id']}`",
        f"- {'状态' if use_chinese() else 'Status'}: `{payload['status']}`",
        f"- {'风险等级' if use_chinese() else 'Risk level'}: `{payload['risk_level']}`",
        f"- {'目标指纹' if use_chinese() else 'Goal fingerprint'}: `{payload['goal_fingerprint']}`",
        f"- {'交接哈希' if use_chinese() else 'Handoff hash'}: `{payload['handoff_hash']}`",
        f"- {'确认保证级别' if use_chinese() else 'Confirmation assurance'}: `{confirmation.get('assurance', 'legacy-unattested')}`",
        f"- {'确认来源' if use_chinese() else 'Confirmation source'}: `{confirmation.get('source', 'legacy')}`",
        f"- {'确认事件' if use_chinese() else 'Confirmation event'}: `{confirmation.get('event_id', '') or ('无' if use_chinese() else 'None')}`",
        "",
        "## 目标" if use_chinese() else "## Goal",
        "",
        str(payload["goal"]),
    ]
    for heading, values in sections:
        lines.extend(["", f"## {heading}", "", markdown_list(list(values))])
    lines.extend(
        [
            "",
            "## 授权边界" if use_chinese() else "## Authorization Boundary",
            "",
            (
                "此交接仅记录决策对齐，不授权实现、提交、推送、部署或发布。"
                if use_chinese()
                else "This handoff records decision alignment only. It does not authorize implementation, commit, push, deployment, or release."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def write_handoff_document(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    date = dt.datetime.now(dt.timezone.utc).date().isoformat()
    alignment_suffix = hashlib.sha256(str(payload["alignment_id"]).encode("utf-8")).hexdigest()[:10]
    relative = Path(".tenetora/docs/plans") / f"{date}-{slugify(str(payload['goal']))}-{alignment_suffix}.md"
    if relative.is_absolute():
        raise AlignmentStateError("handoff document path must be project-relative")
    atomic_write_text(root / relative, render_handoff_markdown(payload))
    updated = dict(payload)
    updated["handoff_relative_path"] = relative.as_posix()
    session_id = str(updated.get("session_id", updated.get("alignment_id", "")))
    history_file = history_directory(root) / f"{session_id}.json"
    expected_history_revision = int(payload.get("revision", 0)) if history_file.is_file() else 0
    history_payload = write_json(history_file, updated, expected_revision=expected_history_revision)

    # current-alignment.json is only a latest pointer. Never let an older
    # session's document generation replace a newer session's pointer.
    latest_path = current_path(root)
    with state_lock(latest_path):
        latest = read_json_object(latest_path, "current-alignment.json")
        if latest is not None:
            validate_handoff(latest, "current-alignment.json")
        if latest is not None and latest.get("session_id", latest.get("alignment_id")) == session_id:
            latest_revision = latest.get("revision", 0)
            if type(latest_revision) is not int or latest_revision < 0:
                raise AlignmentStateError("current-alignment.json has an invalid revision")
            current_payload = dict(history_payload)
            current_payload["revision"] = latest_revision + 1
            atomic_write_text(latest_path, json.dumps(current_payload, ensure_ascii=False, indent=2) + "\n")
            return current_payload
    return history_payload


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(
        prog="tenetora alignment",
        description="Manage bounded pre-execution decision alignment state.",
        add_help=False,
    )
    command_parser.add_argument("--help", action="help", help="Show this help message and exit.")
    command_parser.add_argument(
        "-p",
        "--path",
        default=".",
        type=existing_project_path,
        metavar="<project-dir>",
        help="Project directory. Defaults to the current directory.",
    )
    command_parser.add_argument("--json", action="store_true", help="Print JSON instead of Markdown.")
    action = command_parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--status", action="store_true", help="Show one explicit session, or an unscoped conflict summary.")
    action.add_argument("--list", action="store_true", help="List all isolated and legacy alignment sessions.")
    action.add_argument("--start", action="store_true", help="Start a new bounded alignment session.")
    action.add_argument("--record-decision", metavar="<summary>", help="Record one answered decision and advance the question budget.")
    action.add_argument("--checkpoint", action="store_true", help="Move an active session to checkpoint.")
    action.add_argument("--pause", action="store_true", help="Pause an active, checkpoint, or awaiting-confirmation session.")
    action.add_argument("--resume", action="store_true", help="Resume a checkpoint, paused, blocked, or awaiting-confirmation session.")
    action.add_argument("--block", metavar="<reason>", help="Block the session with a concise reason.")
    action.add_argument("--await-confirmation", action="store_true", help="Mark the converged summary ready for user confirmation.")
    action.add_argument("--confirm", action="store_true", help="Confirm the alignment and create the current handoff pointer.")
    action.add_argument("--accept-risk", metavar="<risk>", help="Confirm while explicitly accepting a non-empty risk.")
    action.add_argument("--abandon", action="store_true", help="Abandon and clean the temporary session.")
    action.add_argument("--archive", action="store_true", help="Archive a confirmed goal so it is no longer automatically matched.")
    action.add_argument("--handoff", action="store_true", help="Render the latest confirmed or risk-accepted handoff.")
    action.add_argument("--heartbeat", action="store_true", help="Renew the lease for an owned active session.")
    command_parser.add_argument("--session-id", help="Explicit alignment session id; required for mutations.")
    command_parser.add_argument("--owner-id", help="Explicit alignment owner or conversation id.")
    command_parser.add_argument("--conversation-id", help="Conversation id recorded with a new session.")
    command_parser.add_argument(
        "--conversation-continuity-id",
        help="Stable host conversation id shared by model execution attempts.",
    )
    command_parser.add_argument("--tool", help="Calling tool recorded with a new session.")
    command_parser.add_argument("--provider", help="Model provider recorded with an execution attempt.")
    command_parser.add_argument("--model", help="Model name recorded with an execution attempt.")
    command_parser.add_argument("--execution-attempt-id", help="Opaque execution attempt id.")
    command_parser.add_argument("--attempt-status", choices=sorted(EXECUTION_ATTEMPT_STATUSES - {"running"}))
    command_parser.add_argument("--reason", help="Safe diagnostic reason for ending an execution attempt.")
    command_parser.add_argument(
        "--confirmation-source",
        choices=sorted(CONFIRMATION_SOURCES),
        help="Audited authorization source for --confirm or --accept-risk.",
    )
    command_parser.add_argument("--confirmation-actor-id", help="Owner or conversation continuity that authorized confirmation.")
    command_parser.add_argument("--confirmation-event-id", help="Opaque host event id for the explicit confirmation.")
    command_parser.add_argument("--confirmation-reason", help="Optional bounded context for the audited confirmation event.")
    command_parser.add_argument(
        "--legacy-unowned",
        action="store_true",
        help="Explicitly recover a damaged legacy singleton; never treats it as a current owned session.",
    )
    command_parser.add_argument("--goal", help="Alignment goal; required with --start.")
    command_parser.add_argument("--risk-level", choices=sorted(VALID_RISK_LEVELS), help="Task risk level; required with --start.")
    command_parser.add_argument("--scope", action="append", help="In-scope item. Can be repeated.")
    command_parser.add_argument("--non-goal", action="append", help="Explicit non-goal. Can be repeated.")
    command_parser.add_argument("--constraint", action="append", help="Confirmed constraint. Can be repeated.")
    command_parser.add_argument("--assumption", action="append", help="Visible assumption. Can be repeated.")
    command_parser.add_argument("--approval-boundary", action="append", help="User-owned approval boundary. Can be repeated.")
    command_parser.add_argument("--acceptance-criterion", action="append", help="Acceptance criterion. Can be repeated.")
    command_parser.add_argument("--verification", action="append", help="Verification expectation. Can be repeated.")
    command_parser.add_argument("--open-question", action="append", help="Unresolved decision summary. Can be repeated.")
    command_parser.add_argument("--write-document", action="store_true", help="With --handoff, write an optional Markdown handoff document.")
    action.add_argument(
        "--execution-start",
        action="store_true",
        help="Start one model execution attempt inside the owned alignment session.",
    )
    action.add_argument(
        "--execution-finish",
        action="store_true",
        help="Finish one model execution attempt without changing alignment ownership or goal.",
    )
    action.add_argument(
        "--execution-list",
        action="store_true",
        help="List execution attempts for the explicitly owned alignment session.",
    )
    return command_parser


def print_markdown(payload: dict[str, Any]) -> None:
    if "alignment_id" in payload:
        print(render_handoff_markdown(payload), end="")
        if payload.get("handoff_relative_path"):
            print(f"\n{'文档' if use_chinese() else 'Document'}: `{payload['handoff_relative_path']}`")
        return
    print("# Tenetora 对齐状态" if use_chinese() else "# Tenetora Alignment State")
    print()
    print(f"- {'状态' if use_chinese() else 'status'}: `{payload.get('status', 'idle')}`")
    print(f"- {'目标' if use_chinese() else 'goal'}: {payload.get('goal') or ('无' if use_chinese() else 'None')}")
    print(f"- {'风险等级' if use_chinese() else 'risk_level'}: `{payload.get('risk_level') or ('无' if use_chinese() else 'None')}`")
    print(f"- {'本轮问题数' if use_chinese() else 'batch_questions'}: {payload.get('batch_question_count', 0)} / {MAX_BATCH_QUESTIONS}")
    print(f"- {'指纹' if use_chinese() else 'fingerprint'}: `{payload.get('goal_fingerprint') or ('无' if use_chinese() else 'None')}`")
    if payload.get("reason"):
        print(f"- {'说明' if use_chinese() else 'reason'}: {payload['reason']}")
    if payload.get("historical_expired_sessions"):
        label = "历史过期会话（不阻断当前任务）" if use_chinese() else "historical expired sessions (non-blocking)"
        print(f"- {label}: {len(payload['historical_expired_sessions'])}")
    if payload.get("expired_sessions"):
        label = "仍需处理的过期会话" if use_chinese() else "actionable expired sessions"
        print(f"- {label}: {len(payload['expired_sessions'])}")
    if payload.get("active_sessions"):
        print()
        print("需要显式会话身份；活动会话如下：" if use_chinese() else "Explicit session identity is required; active sessions:")
        for session in payload["active_sessions"]:
            print(
                f"- `{session.get('session_id')}` owner `{session.get('owner_id')}` "
                f"status `{session.get('status')}` goal: {session.get('goal') or 'None'}"
            )


def output(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_markdown(payload)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root: Path = args.path
    try:
        require_harness(root)
        sweep_alignment_state(root)
        if args.start:
            session_id = requested_session_id(args) or f"align-{uuid.uuid4().hex}"
            owner_id = requested_owner_id(args, session_id, required=True)
            session_file = isolated_session_path(root, session_id)
            history_file = history_directory(root) / f"{session_id}.json"
            if session_file.exists() or history_file.exists():
                raise AlignmentStateError("that alignment session id already exists; choose a new session id")
            session_directory(root).mkdir(parents=True, exist_ok=True)
            state = new_session(
                args,
                session_id,
                owner_id,
                base_current_revision=current_handoff_revision(root),
            )
            written = write_json(session_file, state, expected_revision=0)
            output(written, args.json)
            return 0

        if args.execution_start or args.execution_finish or args.execution_list:
            state, session_file = require_owned_session(root, args)
            if not state.get("continuity_available"):
                raise AlignmentStateError(
                    "continuity-unavailable: this alignment was created without a stable host conversation id; execution attempts require an explicit continuity id"
                )
            continuity_id = requested_conversation_continuity_id(args, required=True, allow_fallback=False)
            if continuity_id != str(state.get("conversation_continuity_id") or ""):
                raise AlignmentStateError("conversation continuity does not match the alignment session")

            if args.execution_list:
                output(
                    {
                        "version": CURRENT_SCHEMA_VERSION,
                        "status": state.get("status"),
                        "session_id": state.get("session_id"),
                        "owner_id": state.get("owner_id"),
                        "conversation_continuity_id": continuity_id,
                        "revision": state.get("revision", 0),
                        "execution_attempts": state.get("execution_attempts", []),
                    },
                    args.json,
                )
                return 0

            attempts = state.setdefault("execution_attempts", [])
            if not isinstance(attempts, list):
                raise AlignmentStateError("alignment session execution_attempts is invalid")
            if args.execution_start:
                if state.get("status") != "active":
                    raise AlignmentStateError("execution-start requires an active alignment session")
                attempt_id = requested_execution_attempt_id(args, required=False)
                if any(isinstance(item, dict) and item.get("execution_attempt_id") == attempt_id for item in attempts):
                    raise AlignmentStateError("that execution attempt id already exists; choose a new id")
                now = utc_now()
                tool = requested_tool(args)
                if tool == "unknown":
                    tool = str(state.get("tool") or "unknown")
                attempt = {
                    "execution_attempt_id": attempt_id,
                    "alignment_session_id": state["session_id"],
                    "owner_id": state["owner_id"],
                    "conversation_continuity_id": continuity_id,
                    "tool": tool,
                    "provider": requested_execution_metadata(args, "provider", required=True),
                    "model": requested_execution_metadata(args, "model", required=True),
                    "status": "running",
                    "base_revision": int(state.get("revision", 0)),
                    "revision": 0,
                    "created_at": now,
                    "updated_at": now,
                    "ended_at": "",
                    "reason": "",
                }
                attempts.append(attempt)
                refresh_lease(state)
                output(persist_session(session_file, state), args.json)
                return 0

            attempt_id = requested_execution_attempt_id(args, required=True)
            attempt = next(
                (
                    item
                    for item in attempts
                    if isinstance(item, dict) and item.get("execution_attempt_id") == attempt_id
                ),
                None,
            )
            if attempt is None:
                raise AlignmentStateError("No matching execution attempt exists for this alignment session")
            if attempt.get("status") != "running":
                raise AlignmentStateError("execution attempt has already ended and cannot be written again")
            if int(state.get("revision", 0)) != int(attempt.get("base_revision", -1)) + 1:
                raise AlignmentStateError(
                    "execution revision conflict; reload the latest alignment state before finishing this attempt"
                )
            if args.attempt_status is None:
                raise AlignmentStateError("--attempt-status is required with --execution-finish")
            if args.attempt_status != "completed" and not args.reason:
                raise AlignmentStateError(
                    "--reason is required when an execution attempt finishes as rate-limited, failed, or cancelled"
                )
            reason = ensure_safe_text(args.reason, "execution attempt reason") if args.reason else ""
            now = utc_now()
            attempt.update({"status": args.attempt_status, "updated_at": now, "ended_at": now, "reason": reason})
            refresh_lease(state)
            output(persist_session(session_file, state), args.json)
            return 0

        if args.list:
            summaries = all_session_summaries(root)
            current = load_current(root)
            current_summary = None
            if current is not None:
                current_confirmation = (
                    current.get("confirmation") if isinstance(current.get("confirmation"), dict) else {}
                )
                current_session_id = current.get("session_id", current.get("alignment_id", ""))
                current_lifecycle = lifecycle_record(root, str(current_session_id))
                current_lifecycle_status = (
                    str(current_lifecycle.get("status")) if current_lifecycle is not None else ""
                )
                current_summary = {
                    "alignment_id": current.get("alignment_id", ""),
                    "session_id": current_session_id,
                    "owner_id": current.get("owner_id", "") or "unowned",
                    "conversation_id": current.get("conversation_id", ""),
                    "tool": current.get("tool", "unknown"),
                    "status": current_lifecycle_status or current.get("status", ""),
                    "lifecycle_status": current_lifecycle_status or "legacy",
                    "goal": current.get("goal", ""),
                    "goal_fingerprint": current.get("goal_fingerprint", ""),
                    "confirmed_at": current.get("confirmed_at", ""),
                    "confirmation_assurance": current_confirmation.get("assurance", "legacy-unattested"),
                    "confirmation_source": current_confirmation.get("source", "legacy"),
                }
            output(
                {
                    "version": CURRENT_SCHEMA_VERSION,
                    "status": "list",
                    "sessions": summaries,
                    "current_alignment": current_summary,
                },
                args.json,
            )
            return 0

        if args.status:
            session_id = requested_session_id(args)
            conversation_id = requested_conversation_id(args)
            continuity_id = requested_conversation_continuity_id(args, allow_fallback=False)
            if not session_id and continuity_id:
                session_id = session_id_for_conversation_continuity(root, continuity_id) or ""
            if not session_id and conversation_id:
                session_id = session_id_for_conversation(root, conversation_id) or ""
            if session_id:
                session = load_session(root, session_id)
                if session is None:
                    session = load_handoff(root, session_id)
                if session is None:
                    raise AlignmentStateError("No matching alignment session or handoff. Start one with tenetora alignment --start.")
                owner_id = requested_owner_id(args, session_id)
                safe_metadata = {
                    "version": CURRENT_SCHEMA_VERSION,
                    "session_id": session_id,
                    "owner_id": session.get("owner_id") or "unowned",
                    "conversation_id": session.get("conversation_id", ""),
                    "tool": session.get("tool", "unknown"),
                    "updated_at": session.get("updated_at", ""),
                    "expires_at": session.get("expires_at", ""),
                }
                if session.get("legacy_unowned") or not session.get("owner_id"):
                    payload = {
                        **safe_metadata,
                        "status": "legacy-unowned",
                        "reason": "this historical singleton has no owner and cannot be resumed implicitly",
                    }
                elif not owner_id:
                    payload = {
                        **safe_metadata,
                        "status": "owner-required",
                        "reason": "the session owner must be supplied before its goal can be read",
                    }
                elif owner_id != session.get("owner_id"):
                    payload = {
                        **safe_metadata,
                        "status": "owner-mismatch",
                        "reason": "the requested owner does not own this session",
                    }
                elif conversation_id and session.get("conversation_id") != conversation_id:
                    payload = {
                        **safe_metadata,
                        "status": "conversation-mismatch",
                        "reason": "the requested conversation does not own this session",
                    }
                else:
                    payload = session
                    lifecycle = lifecycle_record(root, session_id)
                    if lifecycle is not None:
                        payload = dict(payload)
                        payload["lifecycle_status"] = lifecycle.get("status")
            else:
                summaries = all_session_summaries(root, include_goal=False)
                active = [
                    item for item in summaries
                    if item.get("status") in ACTIVE_STATUSES or item.get("status") == "legacy-unowned"
                ]
                open_goals = [item for item in summaries if item.get("lifecycle_status") == "open"]
                expired = [item for item in summaries if item.get("status") == "expired"]
                actionable_expired = [
                    item
                    for item in expired
                    if item.get("expired_from_status") == "awaiting-confirmation"
                    or int(item.get("running_execution_attempts", 0) or 0) > 0
                ]
                historical_expired = [item for item in expired if item not in actionable_expired]
                current = load_current(root)
                if active:
                    payload = {
                        "version": CURRENT_SCHEMA_VERSION,
                        "status": "conflict",
                        "reason": "active alignment sessions require explicit session identity",
                        "active_sessions": active,
                    }
                    if actionable_expired:
                        payload["expired_sessions"] = actionable_expired
                    if historical_expired:
                        payload["historical_expired_sessions"] = historical_expired
                elif len(open_goals) > 1:
                    payload = {
                        "version": CURRENT_SCHEMA_VERSION,
                        "status": "conflict",
                        "reason": "multiple open alignment goals require explicit session identity",
                        "open_goals": open_goals,
                    }
                elif actionable_expired:
                    payload = {
                        "version": CURRENT_SCHEMA_VERSION,
                        "status": "stale",
                        "stale": True,
                        "reason": "one or more expired alignment sessions still have pending work",
                        "expired_sessions": actionable_expired,
                    }
                    if historical_expired:
                        payload["historical_expired_sessions"] = historical_expired
                elif current is not None:
                    current_confirmation = (
                        current.get("confirmation") if isinstance(current.get("confirmation"), dict) else {}
                    )
                    current_session_id = current.get("session_id", current.get("alignment_id", ""))
                    current_lifecycle = lifecycle_record(root, str(current_session_id))
                    current_lifecycle_status = (
                        str(current_lifecycle.get("status")) if current_lifecycle is not None else ""
                    )
                    payload = {
                        "version": CURRENT_SCHEMA_VERSION,
                        "status": "historical",
                        "reason": (
                            f"the current handoff lifecycle is {current_lifecycle_status}; it is not an active session"
                            if current_lifecycle_status
                            else "a confirmed handoff exists; it is not an active session"
                        ),
                        "current_alignment": {
                            "alignment_id": current.get("alignment_id", ""),
                            "session_id": current_session_id,
                            "owner_id": current.get("owner_id", "") or "unowned",
                            "conversation_id": current.get("conversation_id", ""),
                            "tool": current.get("tool", "unknown"),
                            "status": current_lifecycle_status or current.get("status", ""),
                            "lifecycle_status": current_lifecycle_status or "legacy",
                            "goal_fingerprint": current.get("goal_fingerprint", ""),
                            "confirmed_at": current.get("confirmed_at", ""),
                            "confirmation_assurance": current_confirmation.get("assurance", "legacy-unattested"),
                            "confirmation_source": current_confirmation.get("source", "legacy"),
                        },
                    }
                    if historical_expired:
                        payload["historical_expired_sessions"] = historical_expired
                elif historical_expired:
                    payload = {
                        "version": CURRENT_SCHEMA_VERSION,
                        "status": "idle",
                        "reason": "no active alignment session; expired sessions have no pending work",
                        "historical_expired_sessions": historical_expired,
                    }
                else:
                    payload = {"version": CURRENT_SCHEMA_VERSION, "status": "idle"}
            output(payload, args.json)
            return 0

        if args.handoff:
            session_id = requested_session_id(args, required=True)
            owner_id = requested_owner_id(args, session_id, required=True)
            payload = load_handoff(root, session_id)
            if payload is None:
                raise AlignmentStateError("No confirmed alignment handoff is available for this session.")
            if payload.get("owner_id") != owner_id:
                raise AlignmentStateError("alignment handoff owner mismatch")
            if args.write_document:
                require_harness(root)
                payload = write_handoff_document(root, payload)
            output(payload, args.json)
            return 0

        if args.archive:
            session_id = requested_session_id(args, required=True)
            owner_id = requested_owner_id(args, session_id, required=True)
            payload = load_handoff(root, session_id)
            if payload is None:
                raise AlignmentStateError("No confirmed alignment handoff is available for this session.")
            if payload.get("owner_id") != owner_id:
                raise AlignmentStateError("alignment handoff owner mismatch")
            archived = archive_handoff(root, payload, owner_id)
            output(archived, args.json)
            return 0

        if args.abandon:
            if args.legacy_unowned:
                path = session_path(root)
                if not path.is_file():
                    raise AlignmentStateError("No legacy alignment-session.json exists to recover")
                try:
                    state = load_session(root, required=True)
                    assert state is not None
                    transition(state, "abandoned", ACTIVE_STATUSES | {"expired"})
                    payload = state
                except AlignmentStateError as exc:
                    payload = {
                        "version": CURRENT_SCHEMA_VERSION,
                        "status": "abandoned",
                        "recovered_from_invalid_session": True,
                        "recovery_reason": str(exc),
                    }
                path.unlink(missing_ok=True)
            else:
                state, path = require_owned_session(root, args, allow_expired=True)
                transition(state, "abandoned", ACTIVE_STATUSES | {"expired"})
                refresh_lease(state)
                payload = archive_abandoned(root, path, state)
            output(payload, args.json)
            return 0

        if args.heartbeat:
            state, path = require_owned_session(root, args)
            if state["status"] not in ACTIVE_STATUSES:
                raise AlignmentStateError("heartbeat requires an active alignment session")
            if state["status"] == "awaiting-confirmation":
                request = state.get("confirmation_request")
                if not isinstance(request, dict) or not request:
                    raise AlignmentStateError("awaiting-confirmation has no valid confirmation request")
                request["revision"] = int(state.get("revision", 0)) + 1
                request["expires_at"] = utc_after(AWAITING_CONFIRMATION_TTL_SECONDS)
            refresh_lease(state)
            output(persist_session(path, state), args.json)
            return 0

        state, session_file = require_owned_session(root, args, allow_expired=bool(args.resume))
        if args.record_decision is not None:
            if state["status"] != "active":
                raise AlignmentStateError("recording a decision requires active status; resume the session first")
            decision = ensure_safe_text(args.record_decision, "decision")
            append_unique(state, "decisions", [decision])
            apply_structured_updates(state, args)
            state["current_question"] = ""
            state["batch_question_count"] = int(state["batch_question_count"]) + 1
            state["total_question_count"] = int(state["total_question_count"]) + 1
            state["updated_at"] = utc_now()
            if state["batch_question_count"] >= MAX_BATCH_QUESTIONS:
                state["checkpoint_count"] = int(state["checkpoint_count"]) + 1
                transition(state, "checkpoint", {"active"})
            refresh_lease(state)
            output(persist_session(session_file, state), args.json)
            return 0
        if args.checkpoint:
            state["checkpoint_count"] = int(state["checkpoint_count"]) + 1
            transition(state, "checkpoint", {"active"})
        elif args.pause:
            transition(state, "paused", {"active", "checkpoint", "awaiting-confirmation"})
        elif args.resume:
            source = str(state["status"])
            if source == "expired" and state.get("expired_from_status") != "awaiting-confirmation":
                raise AlignmentStateError(
                    "alignment session lease expired; only an expired awaiting-confirmation session can be resumed explicitly"
                )
            transition(state, "active", {"checkpoint", "paused", "blocked", "awaiting-confirmation", "expired"})
            if source == "checkpoint":
                state["batch_question_count"] = 0
            state["blocked_reason"] = ""
            state["confirmation_request"] = {}
            state.pop("stale", None)
            state.pop("stale_reason", None)
            state.pop("expired_from_status", None)
        elif args.block is not None:
            state["blocked_reason"] = ensure_safe_text(args.block, "block reason")
            transition(state, "blocked", {"active", "checkpoint", "paused", "awaiting-confirmation"})
        elif args.await_confirmation:
            apply_structured_updates(state, args)
            transition(state, "awaiting-confirmation", {"active", "checkpoint"})
            now = utc_now()
            state["confirmation_request"] = {
                "requested_at": now,
                "expires_at": utc_after(AWAITING_CONFIRMATION_TTL_SECONDS),
                "revision": int(state.get("revision", 0)) + 1,
                "goal_fingerprint": state["goal_fingerprint"],
            }
        elif args.confirm:
            payload = finalize(root, session_file, state, "confirmed", args)
            output(payload, args.json)
            return 0
        elif args.accept_risk is not None:
            payload = finalize(root, session_file, state, "accepted-with-risks", args, args.accept_risk)
            output(payload, args.json)
            return 0
        else:
            raise AlignmentStateError("No alignment action selected")

        apply_structured_updates(state, args)
        refresh_lease(state)
        output(persist_session(session_file, state), args.json)
        return 0
    except AlignmentStateError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
