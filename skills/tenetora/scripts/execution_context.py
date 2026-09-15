#!/usr/bin/env python3
"""Capture and resolve host execution identity without putting it in project state."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = 1
MAX_HISTORY = 8
CONTEXT_TTL_SECONDS = 7 * 24 * 60 * 60
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+/-]{3,127}$")
METADATA_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+/-]{0,127}$")
PLATFORM_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "session": ("session_id", "sessionId"),
    "conversation": ("conversation_id", "conversationId"),
    "continuity": ("conversation_continuity_id", "conversationContinuityId"),
    "owner": ("owner_id", "ownerId"),
    "provider": ("provider", "model_provider", "modelProvider"),
    "model": ("model", "model_name", "modelName"),
    "attempt": ("execution_attempt_id", "executionAttemptId", "attempt_id", "attemptId"),
}
ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "session": (
        "TENETORA_HOST_SESSION_ID",
        "CLAUDE_SESSION_ID",
        "CODEX_SESSION_ID",
        "CURSOR_SESSION_ID",
        "OPENCODE_SESSION_ID",
        "ZCODE_SESSION_ID",
    ),
    "conversation": (
        "TENETORA_HOST_CONVERSATION_ID",
        "CLAUDE_CONVERSATION_ID",
        "CODEX_CONVERSATION_ID",
        "CURSOR_CONVERSATION_ID",
        "OPENCODE_CONVERSATION_ID",
        "ZCODE_CONVERSATION_ID",
    ),
    "continuity": (
        "TENETORA_HOST_CONVERSATION_CONTINUITY_ID",
        "CLAUDE_CONVERSATION_CONTINUITY_ID",
        "CODEX_CONVERSATION_CONTINUITY_ID",
        "CURSOR_CONVERSATION_CONTINUITY_ID",
        "OPENCODE_CONVERSATION_CONTINUITY_ID",
        "ZCODE_CONVERSATION_CONTINUITY_ID",
    ),
    "owner": ("TENETORA_HOST_OWNER_ID", "CLAUDE_OWNER_ID", "CODEX_OWNER_ID", "CURSOR_OWNER_ID", "OPENCODE_OWNER_ID", "ZCODE_OWNER_ID"),
    "provider": ("TENETORA_HOST_PROVIDER",),
    "model": ("TENETORA_HOST_MODEL",),
    "attempt": ("TENETORA_HOST_EXECUTION_ATTEMPT_ID",),
}
KNOWN_NESTED_CONTEXTS = ("session", "conversation", "execution", "metadata", "context", "host", "identity")
CONTEXT_FIELDS = (
    "session_id",
    "owner_id",
    "conversation_id",
    "conversation_continuity_id",
    "provider",
    "model",
    "execution_attempt_id",
    "tool",
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def machine_home(environ: Mapping[str, str] | None = None) -> Path:
    values = environ if environ is not None else os.environ
    return Path(values.get("TENETORA_HOME", "") or (Path.home() / ".tenetora")).expanduser()


def _containers(event: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    containers: list[Mapping[str, Any]] = [event]
    for key in KNOWN_NESTED_CONTEXTS:
        value = event.get(key)
        if isinstance(value, Mapping):
            containers.append(value)
    return containers


def _value(
    event: Mapping[str, Any],
    field: str,
    environ: Mapping[str, str],
) -> tuple[str, str]:
    aliases = FIELD_ALIASES[field]
    for container in _containers(event):
        for key in aliases:
            value = container.get(key)
            if value is not None and str(value).strip():
                return str(value).strip(), "event"
    for key in ENV_ALIASES[field]:
        value = environ.get(key, "")
        if value and value.strip():
            return value.strip(), "environment"
    return "", ""


def _safe(value: str, pattern: re.Pattern[str]) -> str:
    return value if pattern.fullmatch(value) else ""


def extract(
    event: Mapping[str, Any] | None,
    platform: str,
    *,
    environ: Mapping[str, str] | None = None,
    captured_at: str | None = None,
) -> dict[str, Any]:
    """Normalize host identity; no value is derived from pid, randomness, or username."""

    payload = event if isinstance(event, Mapping) else {}
    values = environ if environ is not None else os.environ
    raw: dict[str, str] = {}
    sources: set[str] = set()
    for field in FIELD_ALIASES:
        value, source = _value(payload, field, values)
        raw[field] = value
        if source:
            sources.add(source)

    explicit_session = _safe(raw["session"], SESSION_ID_RE)
    explicit_conversation = _safe(raw["conversation"], IDENTITY_RE)
    explicit_continuity = _safe(raw["continuity"], IDENTITY_RE)
    session_id = explicit_session or explicit_conversation
    conversation_id = explicit_conversation or session_id
    owner_id = _safe(raw["owner"], IDENTITY_RE) or conversation_id or session_id
    continuity_id = explicit_continuity or explicit_conversation
    provider = _safe(raw["provider"], METADATA_RE)
    model = _safe(raw["model"], METADATA_RE)
    attempt = _safe(raw["attempt"], IDENTITY_RE)
    normalized_platform = _safe(str(platform or "unknown").strip().lower(), PLATFORM_RE) or "unknown"
    return {
        "session_id": session_id,
        "owner_id": owner_id,
        "conversation_id": conversation_id,
        "conversation_continuity_id": continuity_id,
        "provider": provider,
        "model": model,
        "execution_attempt_id": attempt,
        "tool": normalized_platform,
        "host_session_id": explicit_session,
        "continuity_explicit": bool(explicit_continuity or explicit_conversation),
        "identity_present": bool(session_id and owner_id),
        "source": "+".join(sorted(sources)) or "none",
        "captured_at": captured_at or utc_now(),
    }


def context_identity_key(context: Mapping[str, Any]) -> str:
    stable = (
        str(context.get("conversation_continuity_id") or "")
        or str(context.get("conversation_id") or "")
        or str(context.get("session_id") or "")
    )
    owner = str(context.get("owner_id") or "")
    tool = str(context.get("tool") or "unknown")
    return f"{stable}\0{owner}\0{tool}"


def project_hash(root: Path) -> str:
    return hashlib.sha256(str(root.resolve(strict=False)).encode("utf-8", errors="surrogatepass")).hexdigest()


def _platform_name(platform: str) -> str:
    return _safe(str(platform or "unknown").strip().lower(), PLATFORM_RE) or "unknown"


def context_directory(environ: Mapping[str, str] | None = None) -> Path:
    return machine_home(environ) / "state" / "execution-contexts"


def context_path(root: Path, platform: str, environ: Mapping[str, str] | None = None) -> Path:
    return context_directory(environ) / f"{project_hash(root)}-{_platform_name(platform)}.json"


def _read_record(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != SCHEMA_VERSION:
        return {}
    return payload


def _write_record(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def _lock(path: Path):
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            unlock = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except ImportError:
            import msvcrt

            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            unlock = lambda: (handle.seek(0), msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1))
        yield
    finally:
        try:
            unlock()
        except (UnboundLocalError, OSError):
            pass
        handle.close()


def record(root: Path, platform: str, event: Mapping[str, Any]) -> dict[str, Any] | None:
    context = extract(event, platform)
    if not context["identity_present"]:
        return None
    path = context_path(root, platform)
    try:
        with _lock(path):
            previous = _read_record(path)
            current = previous.get("current") if isinstance(previous.get("current"), dict) else None
            history = previous.get("history") if isinstance(previous.get("history"), list) else []
            if current and context_identity_key(current) != context_identity_key(context):
                history.append(current)
            elif current:
                history = [
                    item
                    for item in history
                    if isinstance(item, dict) and context_identity_key(item) != context_identity_key(current)
                ]
            history = [item for item in history if isinstance(item, dict)][-MAX_HISTORY:]
            payload = {
                "version": SCHEMA_VERSION,
                "project_hash": project_hash(root),
                "platform": _platform_name(platform),
                "current": context,
                "history": history,
            }
            _write_record(path, payload)
    except OSError:
        return None
    return context


def _parse_time(value: Any) -> dt.datetime | None:
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


def _is_recent(context: Mapping[str, Any], now: dt.datetime) -> bool:
    observed = _parse_time(context.get("captured_at"))
    if observed is None:
        return False
    age = (now - observed).total_seconds()
    return 0 <= age <= CONTEXT_TTL_SECONDS


def _cached_candidates(root: Path, platform: str | None, environ: Mapping[str, str]) -> list[dict[str, Any]]:
    directory = context_directory(environ)
    if not directory.is_dir():
        return []
    expected_project = project_hash(root)
    expected_platform = _platform_name(platform) if platform else None
    candidates: list[dict[str, Any]] = []
    now = dt.datetime.now(dt.timezone.utc)
    for path in directory.glob(f"{expected_project}-*.json"):
        payload = _read_record(path)
        if payload.get("project_hash") != expected_project:
            continue
        if expected_platform and payload.get("platform") != expected_platform:
            continue
        history = payload.get("history")
        values = [payload.get("current"), *(history if isinstance(history, list) else [])]
        candidates.extend(
            item
            for item in values
            if isinstance(item, dict) and item.get("identity_present") is True and _is_recent(item, now)
        )
    grouped: dict[str, dict[str, Any]] = {}
    for item in candidates:
        key = context_identity_key(item)
        previous = grouped.get(key)
        if previous is None or str(item.get("captured_at", "")) > str(previous.get("captured_at", "")):
            grouped[key] = item
    return sorted(grouped.values(), key=lambda item: str(item.get("captured_at", "")), reverse=True)


def from_environment(
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    values = environ if environ is not None else os.environ
    chosen_platform = platform or values.get("TENETORA_HOOK_PLATFORM") or "unknown"
    context = extract({}, chosen_platform, environ=values)
    return context if context["identity_present"] else None


def resolve(
    root: Path,
    *,
    platform: str | None = None,
    event: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    values = environ if environ is not None else os.environ
    if event is not None:
        event_context = extract(event, platform or values.get("TENETORA_HOOK_PLATFORM") or "unknown", environ=values)
        if event_context["identity_present"]:
            return {"status": "event", "context": event_context, "candidate_count": 1}
    environment_context = from_environment(platform, values)
    if environment_context is not None:
        return {"status": "environment", "context": environment_context, "candidate_count": 1}
    candidates = _cached_candidates(root, platform, values)
    if not candidates:
        return {
            "status": "missing",
            "context": None,
            "candidate_count": 0,
            "reason": "no recent host execution identity was recorded for this project",
        }
    if len(candidates) > 1:
        return {
            "status": "ambiguous",
            "context": None,
            "candidate_count": len(candidates),
            "reason": "multiple recent host execution identities belong to this project",
        }
    return {"status": "cached", "context": candidates[0], "candidate_count": 1}


def context_fields(context: Mapping[str, Any]) -> dict[str, str]:
    return {field: str(context.get(field) or "") for field in CONTEXT_FIELDS}
