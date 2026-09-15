"""Expiring, evidence-bound acknowledgement state for health findings."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable

from .file_lock import locked_file


STATE_REL = ".tenetora/state/finding-acknowledgements.json"
STATE_VERSION = 1
DEFAULT_EXPIRY_DAYS = 30
MAX_EXPIRY_DAYS = 90
MAX_ACKNOWLEDGEMENTS = 500
MAX_REASON_LENGTH = 240
_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_FINDING_ID_RE = re.compile(r"^tnf-[A-Za-z0-9_.-]+-[A-Za-z0-9_.-]+-[A-Za-z0-9_.-]+-[0-9a-f]{12}$")
_LOCAL_PATH_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_.-])/(?:Users|home)/[^/\\\s]+"),
    re.compile(r"(?i)(?<![A-Za-z0-9_.-])[A-Za-z]:\\Users\\[^\\\s]+"),
)
_CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(?:glpat-|ghp_|github_pat_|sk-)[A-Za-z0-9_-]{8,}"),
)


class AcknowledgementStateError(RuntimeError):
    """Raised when acknowledgement state cannot be safely consumed or changed."""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _timestamp(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def _identity_component(value: object, fallback: str) -> str:
    text = str(value or fallback)
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", text):
        return text
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()[:12]


def finding_fingerprint(finding: dict[str, object]) -> str:
    evidence = finding.get("evidence")
    normalized_evidence = [str(item) for item in evidence] if isinstance(evidence, list) else []
    payload = {
        "version": 1,
        "code": str(finding.get("code") or "unknown"),
        "severity": str(finding.get("severity") or "info"),
        "tool": str(finding.get("tool") or "all"),
        "scope": str(finding.get("scope") or "all"),
        "evidence": normalized_evidence,
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def finding_id(finding: dict[str, object]) -> str:
    fingerprint = finding_fingerprint(finding)
    return "tnf-{}-{}-{}-{}".format(
        _identity_component(finding.get("code"), "unknown"),
        _identity_component(finding.get("tool"), "all"),
        _identity_component(finding.get("scope"), "all"),
        fingerprint[:12],
    )


def attach_finding_identity(
    finding: dict[str, object],
    *,
    acknowledgeable: bool | None = None,
) -> dict[str, object]:
    identified = dict(finding)
    identified["fingerprint"] = finding_fingerprint(identified)
    identified["finding_id"] = finding_id(identified)
    identified["acknowledgeable"] = (
        str(identified.get("severity")) == "attention" and identified.get("affects_health") is True
        if acknowledgeable is None
        else acknowledgeable
    )
    identified["acknowledged"] = False
    identified["acknowledgement"] = None
    return identified


def normalize_reason(raw: str) -> str:
    reason = raw.strip()
    if not reason:
        raise AcknowledgementStateError("acknowledgement reason is required")
    if len(reason) > MAX_REASON_LENGTH:
        raise AcknowledgementStateError(
            f"acknowledgement reason exceeds {MAX_REASON_LENGTH} characters"
        )
    if any(character in reason for character in ("\n", "\r", "\x00")):
        raise AcknowledgementStateError("acknowledgement reason must be one line")
    if any(pattern.search(reason) for pattern in (*_LOCAL_PATH_PATTERNS, *_CREDENTIAL_PATTERNS)):
        raise AcknowledgementStateError(
            "acknowledgement reason must not contain credentials or machine-local paths"
        )
    return reason


def normalize_actor(raw: str | None) -> str:
    candidate = str(raw or os.environ.get("TENETORA_OWNER_ID") or "local-user").strip()
    if not _IDENTIFIER_RE.fullmatch(candidate):
        raise AcknowledgementStateError(
            "acknowledgement owner must be a portable identifier using letters, digits, . _ : @ or -"
        )
    return candidate


def state_path(root: Path) -> Path:
    return root / STATE_REL


def _empty_state() -> dict[str, object]:
    return {"version": STATE_VERSION, "revision": 0, "acknowledgements": []}


def _valid_record(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    required = {
        "finding_id",
        "fingerprint",
        "code",
        "tool",
        "scope",
        "reason",
        "owner_id",
        "created_at",
        "expires_at",
    }
    if set(record) != required:
        return False
    if not isinstance(record.get("finding_id"), str) or not _FINDING_ID_RE.fullmatch(record["finding_id"]):
        return False
    if not isinstance(record.get("fingerprint"), str) or not _HEX_64_RE.fullmatch(record["fingerprint"]):
        return False
    if not all(isinstance(record.get(key), str) for key in ("code", "tool", "scope")):
        return False
    try:
        normalize_reason(str(record["reason"]))
        normalize_actor(str(record["owner_id"]))
    except AcknowledgementStateError:
        return False
    created = _parse_timestamp(record.get("created_at"))
    expires = _parse_timestamp(record.get("expires_at"))
    return bool(
        created
        and expires
        and created < expires
        and expires - created <= dt.timedelta(days=MAX_EXPIRY_DAYS)
    )


def _read_state(path: Path) -> tuple[str, dict[str, object]]:
    if not path.is_file():
        return "not-configured", _empty_state()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "invalid", _empty_state()
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        return "invalid", _empty_state()
    revision = payload.get("revision")
    records = payload.get("acknowledgements")
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 0
        or not isinstance(records, list)
        or len(records) > MAX_ACKNOWLEDGEMENTS
        or not all(_valid_record(record) for record in records)
    ):
        return "invalid", _empty_state()
    ids = [record["finding_id"] for record in records]
    if len(ids) != len(set(ids)):
        return "invalid", _empty_state()
    return "ready", payload


def load_state(root: Path) -> tuple[str, dict[str, object]]:
    return _read_state(state_path(root))


def _write_state(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _mutate_state(
    root: Path,
    mutate: Callable[[list[dict[str, object]]], tuple[list[dict[str, object]], dict[str, object]]],
    audit: Callable[[dict[str, object]], None],
    now: dt.datetime,
) -> dict[str, object]:
    path = state_path(root)
    lock_path = path.with_name(f".{path.name}.lock")
    with locked_file(lock_path):
        state_status, previous = _read_state(path)
        if state_status == "invalid":
            raise AcknowledgementStateError(
                "finding acknowledgement state is invalid; inspect it before changing health state"
            )
        records = [
            dict(record)
            for record in previous["acknowledgements"]
            if _parse_timestamp(record.get("expires_at")) > now
        ]
        updated_records, event = mutate(records)
        if len(updated_records) > MAX_ACKNOWLEDGEMENTS:
            raise AcknowledgementStateError(
                f"finding acknowledgement state exceeds {MAX_ACKNOWLEDGEMENTS} records"
            )
        updated = {
            "version": STATE_VERSION,
            "revision": int(previous["revision"]) + 1,
            "acknowledgements": updated_records,
        }
        existed = path.is_file()
        _write_state(path, updated)
        try:
            audit(event)
        except Exception:
            if existed:
                _write_state(path, previous)
            else:
                path.unlink(missing_ok=True)
            raise
    return event


def acknowledge_finding(
    root: Path,
    finding: dict[str, object],
    *,
    reason: str,
    owner_id: str | None,
    expires_days: int = DEFAULT_EXPIRY_DAYS,
    audit: Callable[[dict[str, object]], None],
    now: dt.datetime | None = None,
) -> dict[str, object]:
    current = (now or utc_now()).astimezone(dt.timezone.utc)
    if isinstance(expires_days, bool) or not 1 <= expires_days <= MAX_EXPIRY_DAYS:
        raise AcknowledgementStateError(
            f"acknowledgement expiry must be between 1 and {MAX_EXPIRY_DAYS} days"
        )
    identified = attach_finding_identity(finding)
    if not identified.get("acknowledgeable"):
        raise AcknowledgementStateError(
            "only actionable attention findings can be acknowledged; blocking findings must be resolved"
        )
    normalized_reason = normalize_reason(reason)
    actor = normalize_actor(owner_id)
    created_at = _timestamp(current)
    expires_at = _timestamp(current + dt.timedelta(days=expires_days))
    record = {
        "finding_id": identified["finding_id"],
        "fingerprint": identified["fingerprint"],
        "code": str(identified.get("code") or "unknown"),
        "tool": str(identified.get("tool") or "all"),
        "scope": str(identified.get("scope") or "all"),
        "reason": normalized_reason,
        "owner_id": actor,
        "created_at": created_at,
        "expires_at": expires_at,
    }

    def mutate(records: list[dict[str, object]]) -> tuple[list[dict[str, object]], dict[str, object]]:
        previous_record = next(
            (item for item in records if item["finding_id"] == record["finding_id"]),
            None,
        )
        if previous_record is not None and previous_record.get("owner_id") != actor:
            raise AcknowledgementStateError(
                "this finding acknowledgement is owned by another actor"
            )
        retained = [item for item in records if item["finding_id"] != record["finding_id"]]
        retained.append(record)
        retained.sort(key=lambda item: str(item["finding_id"]))
        return retained, {
            "type": "finding-acknowledgement",
            "action": "acknowledge",
            "status": "pass",
            **record,
        }

    return _mutate_state(root, mutate, audit, current)


def unacknowledge_finding(
    root: Path,
    selected_finding_id: str,
    *,
    owner_id: str | None,
    audit: Callable[[dict[str, object]], None],
    now: dt.datetime | None = None,
) -> dict[str, object]:
    if not _FINDING_ID_RE.fullmatch(selected_finding_id):
        raise AcknowledgementStateError("finding acknowledgement id is invalid")
    current = (now or utc_now()).astimezone(dt.timezone.utc)
    actor = normalize_actor(owner_id)

    def mutate(records: list[dict[str, object]]) -> tuple[list[dict[str, object]], dict[str, object]]:
        matched = next(
            (item for item in records if item["finding_id"] == selected_finding_id),
            None,
        )
        if matched is None:
            raise AcknowledgementStateError("no active acknowledgement matches this finding id")
        if matched.get("owner_id") != actor:
            raise AcknowledgementStateError(
                "this finding acknowledgement is owned by another actor"
            )
        retained = [item for item in records if item["finding_id"] != selected_finding_id]
        return retained, {
            "type": "finding-acknowledgement",
            "action": "unacknowledge",
            "status": "pass",
            "finding_id": selected_finding_id,
            "fingerprint": matched["fingerprint"],
            "code": matched["code"],
            "tool": matched["tool"],
            "scope": matched["scope"],
            "owner_id": actor,
        }

    return _mutate_state(root, mutate, audit, current)


def apply_acknowledgements(
    root: Path,
    findings: list[dict[str, object]],
    *,
    now: dt.datetime | None = None,
) -> dict[str, object]:
    current = (now or utc_now()).astimezone(dt.timezone.utc)
    state_status, payload = load_state(root)
    # Recompute identity from the current evidence; callers must not be able to
    # carry a previous finding_id across an evidence change.
    identified = [attach_finding_identity(finding) for finding in findings]
    findings[:] = identified
    if state_status == "invalid":
        return {
            "status": "invalid",
            "stored": 0,
            "active": 0,
            "applied": 0,
            "expired": 0,
            "stale": 0,
            "state_file": STATE_REL,
        }
    records = [dict(record) for record in payload["acknowledgements"]]
    active_records = [
        record
        for record in records
        if (_parse_timestamp(record.get("expires_at")) or current) > current
    ]
    expired = len(records) - len(active_records)
    by_id = {str(record["finding_id"]): record for record in active_records}
    applied = 0
    matched_ids: set[str] = set()
    for finding in findings:
        selected = by_id.get(str(finding.get("finding_id") or ""))
        if selected is None or not finding.get("acknowledgeable"):
            continue
        if selected.get("fingerprint") != finding.get("fingerprint"):
            continue
        finding["acknowledged"] = True
        finding["acknowledgement"] = {
            key: selected[key]
            for key in ("reason", "owner_id", "created_at", "expires_at")
        }
        matched_ids.add(str(selected["finding_id"]))
        applied += 1
    return {
        "status": state_status,
        "stored": len(records),
        "active": len(active_records),
        "applied": applied,
        "expired": expired,
        "stale": len(active_records) - len(matched_ids),
        "state_file": STATE_REL,
    }
