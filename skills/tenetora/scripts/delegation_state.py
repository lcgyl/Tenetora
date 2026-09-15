#!/usr/bin/env python3
"""Manage deterministic subagent dispatch evidence without spawning agents."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from governance_trail import append_event, sanitize_local_paths
from harness_io import atomic_write_text
import alignment_state
import loop_state
import review_subject
from path_security import harness_missing_message, validate_existing_project_path


STATE_REL = ".tenetora/state/delegation-state.json"
REPORT_DIR_REL = ".tenetora/.cache/subagents"
REPORT_ARCHIVE_DIR_REL = ".tenetora/.cache/subagents/archive"
DEFAULTS_DIR = Path(__file__).resolve().parents[1] / "defaults"
STATE_VERSION = 3
MAX_DISPATCHES = 50
MAX_REPORT_BYTES = 64 * 1024
MAX_ASSIGNMENT_CHARS = 2000
MAX_ROLE_CONTRACT_BYTES = 32 * 1024
MAX_REPORTS = 20
REPORT_RETENTION_DAYS = 7
FEEDBACK_OUTCOMES = ("confirmed", "not-confirmed", "unclear", "not-applicable", "not-recorded")
FOLLOW_UP_STATUSES = ("passed", "failed", "blocked", "not-run", "not-applicable", "not-recorded")
DISPATCH_ID_RE = re.compile(r"^ah-dispatch-[0-9a-f]{16}$")
OBSERVATION_ID_RE = re.compile(r"^ah-observation-[0-9a-f]{20}$")
TRANSITION_LOCK_TIMEOUT_SECONDS = 5.0
TRANSITION_LOCK_RETRY_SECONDS = 0.01
TRANSITION_LOCK_STALE_SECONDS = 30.0
SECRET_RE = re.compile(
    r"(?i)(?:glpat-[A-Za-z0-9._-]+|ghp_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|"
    r"BEGIN [A-Z ]*PRIVATE KEY|\b[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PRIVATE_KEY|ACCESS_KEY)\b\s*[:=]\s*[^\s$<{]+)"
)
RESOLUTION_MODES = ("dedicated", "builtin-role-injection", "unspecified")
ISOLATION_LEVELS = ("hard", "inherited", "prompt-only", "unknown")
HOST_TOOLS = ("codex", "claude", "zcode", "opencode", "cursor", "agents", "unknown")
RESERVED_PROMPT_MARKERS = (
    "<role-contract",
    "</role-contract",
    "<bounded-assignment",
    "</bounded-assignment",
)

ROLES: dict[str, dict[str, Any]] = {
    "code-reviewer": {
        "description": "Independent read-only correctness and regression review.",
        "spec": ".tenetora/templates/subagents/code-reviewer.spec.md",
        "mode": "read-only",
        "builtin_prompt_only": True,
    },
    "security-auditor": {
        "description": "Independent strictly read-only security or prompt-injection review.",
        "spec": ".tenetora/templates/subagents/security-auditor.spec.md",
        "mode": "strict-read-only-no-shell-network",
        "builtin_prompt_only": False,
    },
    "codebase-scout": {
        "description": "Bounded read-only architecture, flow, dependency, or impact investigation.",
        "spec": ".tenetora/templates/subagents/codebase-scout.spec.md",
        "mode": "read-only",
        "builtin_prompt_only": True,
    },
    "implementer": {
        "description": "Bounded repair inside an active authorized review cycle only.",
        "spec": ".tenetora/templates/subagents/implementer.spec.md",
        "mode": "bounded-write",
        "requires_review_cycle": True,
        "builtin_prompt_only": False,
    },
}


class DelegationError(ValueError):
    """Raised when a delegation state transition is invalid."""


def existing_project_path(raw: str) -> Path:
    try:
        return validate_existing_project_path(raw)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "revision": 0, "dispatches": [], "last_cleanup_at": None}


def state_path(root: Path) -> Path:
    return root / STATE_REL


def migrate_dispatch(item: dict[str, Any]) -> dict[str, Any]:
    migrated = dict(item)
    migrated.setdefault("resolution_mode", "unspecified")
    migrated.setdefault("host_tool", "unknown")
    migrated.setdefault("host_agent", None)
    migrated.setdefault("isolation_level", "unknown")
    migrated.setdefault("contract_status", "unknown")
    migrated.setdefault("role_contract_hash", None)
    migrated.setdefault("fallback_resolution_mode", "main-self-review" if item.get("status") == "unavailable" else None)
    migrated.setdefault("subject_fingerprint", None)
    migrated.setdefault("subject_kind", "unbound")
    migrated.setdefault("subject_git_path", None)
    migrated.setdefault("review_scope_bound", False)
    migrated.setdefault("review_scope", None)
    migrated.setdefault("review_scope_entries", None)
    migrated.setdefault("review_scope_fingerprint", None)
    migrated.setdefault("review_subject_entries", None)
    migrated.setdefault("review_subject_entries_fingerprint", None)
    migrated.setdefault("rebind_from_dispatch_id", None)
    migrated.setdefault("review_delta_paths", [])
    migrated.setdefault("association", "general")
    migrated.setdefault("observation_id", None)
    migrated.setdefault("lifecycle_status", "not-observed")
    migrated.setdefault("lifecycle_observed_at", None)
    migrated.setdefault("result_source", None)
    migrated.setdefault("finding_outcome", "not-recorded")
    migrated.setdefault("fix_regression", "not-recorded")
    migrated.setdefault("verification_status", "not-recorded")
    migrated.setdefault("verification_command_hash", None)
    migrated.setdefault("verification_command_length", 0)
    migrated.setdefault("session_id", "")
    migrated.setdefault("owner_id", "")
    migrated.setdefault("conversation_id", "")
    migrated.setdefault("tool", "unknown")
    return migrated


def load_state(root: Path) -> dict[str, Any]:
    path = state_path(root)
    if not path.is_file():
        return default_state()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DelegationError(f"delegation-state.json is unreadable: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") not in {1, 2, STATE_VERSION}:
        raise DelegationError("delegation-state.json has an unsupported schema")
    dispatches = payload.get("dispatches")
    if not isinstance(dispatches, list):
        raise DelegationError("delegation-state.json dispatches must be a list")
    revision = payload.get("revision", 0)
    if type(revision) is not int or revision < 0:
        raise DelegationError("delegation-state.json has an invalid revision")
    return {
        "version": STATE_VERSION,
        "revision": revision,
        "dispatches": [migrate_dispatch(item) for item in dispatches if isinstance(item, dict)][-MAX_DISPATCHES:],
        "last_cleanup_at": payload.get("last_cleanup_at"),
    }


def write_state(root: Path, state: dict[str, Any]) -> None:
    if not (root / ".tenetora").is_dir():
        raise DelegationError(
            harness_missing_message(root)
        )
    state["dispatches"] = list(state.get("dispatches", []))[-MAX_DISPATCHES:]
    path = state_path(root)
    expected_revision = state.get("revision", 0)
    if type(expected_revision) is not int or expected_revision < 0:
        raise DelegationError("delegation-state.json has an invalid revision")
    with alignment_state.state_lock(path):
        current_revision = 0
        if path.is_file():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise DelegationError("delegation state changed or became unreadable; retry the operation") from exc
            if not isinstance(current, dict) or type(current.get("revision", 0)) is not int:
                raise DelegationError("delegation-state.json has an invalid revision")
            current_revision = int(current.get("revision", 0))
        if current_revision != expected_revision:
            raise DelegationError("delegation state changed concurrently; reload status before retrying")
        written = dict(state)
        written["revision"] = current_revision + 1
        atomic_write_text(path, json.dumps(written, ensure_ascii=False, indent=2) + "\n")
        state.clear()
        state.update(written)


@contextmanager
def transition_lock(root: Path):
    harness_dir = root / ".tenetora"
    if not harness_dir.is_dir():
        raise DelegationError(
            harness_missing_message(root)
        )
    state_dir = harness_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / ".delegation-transition.lock"
    deadline = time.monotonic() + TRANSITION_LOCK_TIMEOUT_SECONDS
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > TRANSITION_LOCK_STALE_SECONDS:
                    lock_path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise DelegationError("timed out waiting for delegation transition lock")
            time.sleep(TRANSITION_LOCK_RETRY_SECONDS)
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


def dispatch_id() -> str:
    return f"ah-dispatch-{secrets.token_hex(8)}"


def bounded_text(value: str, label: str, maximum: int = 500) -> str:
    text = " ".join(value.split()).strip()
    if not text:
        raise DelegationError(f"{label} must not be empty")
    if len(text) > maximum:
        raise DelegationError(f"{label} exceeds {maximum} characters")
    return str(sanitize_local_paths(text))


def role_contract(root: Path, role: str) -> dict[str, str]:
    role_info = ROLES.get(role)
    if role_info is None:
        raise DelegationError(f"unknown role: {role}")
    relative = str(role_info["spec"])
    project_path = root / relative
    if project_path.is_file():
        path = project_path
        source = "project"
    else:
        default_relative = relative.removeprefix(".tenetora/")
        path = DEFAULTS_DIR / default_relative
        source = "package-default"
    if not path.is_file():
        raise DelegationError(f"role contract is missing: {relative}")
    raw = path.read_bytes()
    if len(raw) > MAX_ROLE_CONTRACT_BYTES:
        raise DelegationError(f"role contract exceeds {MAX_ROLE_CONTRACT_BYTES} bytes: {relative}")
    content = raw.decode("utf-8", errors="replace").strip()
    if SECRET_RE.search(content):
        raise DelegationError(f"role contract contains a secret-like value and was not rendered: {relative}")
    return {
        "path": relative,
        "source": source,
        "hash": hashlib.sha256(raw).hexdigest(),
        "content": content,
    }


def resolution_contract_status(isolation_level: str) -> str:
    return {
        "hard": "enforced",
        "inherited": "inherited",
        "prompt-only": "soft-boundary",
        "unknown": "unknown",
    }[isolation_level]


def validate_resolution(
    role: str,
    resolution_mode: str,
    host_tool: str,
    host_agent: str | None,
    isolation_level: str,
) -> dict[str, str | None]:
    if resolution_mode not in RESOLUTION_MODES:
        raise DelegationError(f"unknown resolution mode: {resolution_mode}")
    if host_tool not in HOST_TOOLS:
        raise DelegationError(f"unknown host tool: {host_tool}")
    if isolation_level not in ISOLATION_LEVELS:
        raise DelegationError(f"unknown isolation level: {isolation_level}")
    normalized_agent = bounded_text(host_agent, "host agent", 120) if host_agent else None
    if resolution_mode == "unspecified":
        if host_tool != "unknown" or normalized_agent is not None or isolation_level != "unknown":
            raise DelegationError(
                "resolution metadata requires --resolution-mode dedicated or builtin-role-injection"
            )
    else:
        if host_tool == "unknown":
            raise DelegationError("explicit resolution requires --host-tool")
        if normalized_agent is None:
            raise DelegationError("explicit resolution requires --host-agent")
        if isolation_level == "unknown":
            raise DelegationError("explicit resolution requires --isolation-level")
    if isolation_level == "prompt-only" and not bool(ROLES[role].get("builtin_prompt_only", False)):
        raise DelegationError(f"{role} does not allow prompt-only role enforcement")
    return {
        "resolution_mode": resolution_mode,
        "host_tool": host_tool,
        "host_agent": normalized_agent,
        "isolation_level": isolation_level,
        "contract_status": resolution_contract_status(isolation_level),
    }


def render_role_prompt(
    root: Path,
    role: str,
    assignment: str,
    host_tool: str,
    host_agent: str | None,
    isolation_level: str,
) -> dict[str, Any]:
    resolution = validate_resolution(
        role,
        "builtin-role-injection",
        host_tool,
        host_agent,
        isolation_level,
    )
    safe_assignment = bounded_text(assignment, "assignment", MAX_ASSIGNMENT_CHARS)
    if SECRET_RE.search(safe_assignment):
        raise DelegationError("assignment contains a secret-like value and was not rendered")
    if any(marker in safe_assignment.casefold() for marker in RESERVED_PROMPT_MARKERS):
        raise DelegationError("assignment contains a reserved prompt boundary marker and was not rendered")
    contract = role_contract(root, role)
    prompt = "\n".join(
        [
            f"Act as the Tenetora portable role `{role}` for one bounded assignment.",
            f"Host carrier: {host_tool}/{resolution['host_agent']}.",
            f"Isolation declaration: {isolation_level} ({resolution['contract_status']}).",
            "The host/model performs dispatch. Do not claim that Tenetora spawned or permission-isolated you.",
            "Follow the role contract below. If the declared isolation cannot satisfy it, return unavailable without acting.",
            "",
            "<role-contract>",
            contract["content"],
            "</role-contract>",
            "",
            "<bounded-assignment>",
            safe_assignment,
            "</bounded-assignment>",
            "",
            "Return only the bounded result required by the role contract.",
        ]
    )
    return {
        "version": STATE_VERSION,
        "spawns_agents": False,
        "role": role,
        **resolution,
        "role_contract_path": contract["path"],
        "role_contract_source": contract["source"],
        "role_contract_hash": contract["hash"],
        "prompt": prompt,
    }


def relative_input_path(root: Path, raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute():
        raise DelegationError("report file must be a project-relative path")
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise DelegationError("report file must stay inside the project") from exc
    if not resolved.is_file():
        raise DelegationError(f"report file does not exist: {path.as_posix()}")
    return resolved


def validate_report_text(text: str) -> str:
    raw = text.encode("utf-8")
    if len(raw) > MAX_REPORT_BYTES:
        raise DelegationError(f"report exceeds {MAX_REPORT_BYTES} bytes")
    if SECRET_RE.search(text):
        raise DelegationError("report contains a secret-like value and was not persisted")
    return str(sanitize_local_paths(text))


def report_relative_path(role: str, item_id: str) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{REPORT_DIR_REL}/{stamp}-{role}-{item_id.removeprefix('ah-dispatch-')}.md"


def persist_report(root: Path, dispatch: dict[str, Any], report_file: str | None, report_text: str | None) -> str | None:
    if report_file is None and report_text is None:
        return None
    if report_file is not None:
        source = relative_input_path(root, report_file)
        text = source.read_text(encoding="utf-8", errors="replace")
    else:
        text = report_text or ""
    safe_text = validate_report_text(text)
    rel = report_relative_path(str(dispatch["role"]), str(dispatch["dispatch_id"]))
    atomic_write_text(root / rel, safe_text.rstrip() + "\n")
    return rel


def find_dispatch(state: dict[str, Any], item_id: str) -> dict[str, Any]:
    if not DISPATCH_ID_RE.fullmatch(item_id):
        raise DelegationError("dispatch id has an invalid format")
    for item in reversed(state["dispatches"]):
        if item.get("dispatch_id") == item_id:
            return item
    raise DelegationError(f"dispatch not found: {item_id}")


def ensure_dispatch_access(dispatch: dict[str, Any], identity: dict[str, str]) -> None:
    dispatch_session = str(dispatch.get("session_id") or "")
    dispatch_owner = str(dispatch.get("owner_id") or "")
    request_session = str(identity.get("session_id") or "")
    request_owner = str(identity.get("owner_id") or "")
    if dispatch_session or dispatch_owner:
        if request_session != dispatch_session or request_owner != dispatch_owner:
            raise DelegationError("dispatch belongs to another conversation")
    elif request_session or request_owner:
        raise DelegationError("legacy unowned dispatch requires an unowned administrative request")


def visible_dispatches(state: dict[str, Any], identity: dict[str, str], *, all_sessions: bool) -> list[dict[str, Any]]:
    if all_sessions:
        return list(state["dispatches"])
    session_id = str(identity.get("session_id") or "")
    owner_id = str(identity.get("owner_id") or "")
    return [
        item
        for item in state["dispatches"]
        if (
            str(item.get("session_id") or "") == session_id
            and str(item.get("owner_id") or "") == owner_id
        )
    ]


def compact_event(dispatch: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "subagent-dispatch",
        "dispatch_id": dispatch.get("dispatch_id"),
        "role": dispatch.get("role"),
        "trigger": dispatch.get("trigger"),
        "round": dispatch.get("round", 0),
        "status": dispatch.get("status"),
        "report_path": dispatch.get("report_path"),
        "resolution_mode": dispatch.get("resolution_mode", "unspecified"),
        "host_tool": dispatch.get("host_tool", "unknown"),
        "host_agent": dispatch.get("host_agent"),
        "isolation_level": dispatch.get("isolation_level", "unknown"),
        "contract_status": dispatch.get("contract_status", "unknown"),
        "role_contract_hash": dispatch.get("role_contract_hash"),
        "fallback_resolution_mode": dispatch.get("fallback_resolution_mode"),
        "goal_hash": dispatch.get("goal_hash"),
        "started_at": dispatch.get("started_at"),
        "completed_at": dispatch.get("completed_at"),
        "subject_fingerprint": dispatch.get("subject_fingerprint"),
        "subject_kind": dispatch.get("subject_kind", "unbound"),
        "subject_git_path": dispatch.get("subject_git_path"),
        "review_scope_bound": dispatch.get("review_scope_bound", False),
        "review_scope": dispatch.get("review_scope"),
        "review_scope_fingerprint": dispatch.get("review_scope_fingerprint"),
        "review_subject_entries_fingerprint": dispatch.get("review_subject_entries_fingerprint"),
        "rebind_from_dispatch_id": dispatch.get("rebind_from_dispatch_id"),
        "review_delta_paths": dispatch.get("review_delta_paths", []),
        "association": dispatch.get("association", "general"),
        "observation_id": dispatch.get("observation_id"),
        "lifecycle_status": dispatch.get("lifecycle_status", "not-observed"),
        "lifecycle_observed_at": dispatch.get("lifecycle_observed_at"),
        "result_source": dispatch.get("result_source"),
        "finding_outcome": dispatch.get("finding_outcome", "not-recorded"),
        "fix_regression": dispatch.get("fix_regression", "not-recorded"),
        "verification_status": dispatch.get("verification_status", "not-recorded"),
        "verification_command_hash": dispatch.get("verification_command_hash"),
        "verification_command_length": dispatch.get("verification_command_length", 0),
        "session_id": dispatch.get("session_id", ""),
        "owner_id": dispatch.get("owner_id", ""),
        "conversation_id": dispatch.get("conversation_id", ""),
        "tool": dispatch.get("tool", "unknown"),
        "source": "tenetora delegation",
    }


def record_dispatch(root: Path, dispatch: dict[str, Any]) -> None:
    append_event(root, compact_event(dispatch))


def start_dispatch(
    root: Path,
    state: dict[str, Any],
    role: str,
    trigger: str,
    *,
    round_number: int = 0,
    parent_dispatch_id: str | None = None,
    resolution_mode: str = "unspecified",
    host_tool: str = "unknown",
    host_agent: str | None = None,
    isolation_level: str = "unknown",
    association: str = "general",
    subject: dict[str, Any] | None = None,
    identity: dict[str, str] | None = None,
) -> dict[str, Any]:
    if role not in ROLES:
        raise DelegationError(f"unknown role: {role}")
    if role == "implementer":
        review = loop_state.load_state(root).get("review_cycle", {})
        if not isinstance(review, dict) or review.get("status") != "needs-fix":
            raise DelegationError("implementer requires an active review cycle in needs-fix state")
        if int(review.get("fix_rounds", 0)) >= int(review.get("max_fix_rounds", 2)):
            raise DelegationError("review cycle has reached the repair-round limit")
        expected = str(review.get("active_dispatch_id") or "")
        if not parent_dispatch_id or parent_dispatch_id != expected:
            raise DelegationError("implementer must reference the active blocker dispatch")
    resolution = validate_resolution(role, resolution_mode, host_tool, host_agent, isolation_level)
    contract = role_contract(root, role)
    item_id = dispatch_id()
    owner = identity or {"session_id": "", "owner_id": "", "conversation_id": "", "tool": "unknown"}
    if bool(owner.get("session_id")) != bool(owner.get("owner_id")):
        raise DelegationError("owner-bound dispatch requires both session and owner identities")
    item = {
        "dispatch_id": item_id,
        "role": role,
        "trigger": bounded_text(trigger, "trigger", 120),
        "round": max(0, round_number),
        "status": "started",
        "report_path": None,
        "parent_dispatch_id": parent_dispatch_id,
        "goal_hash": hashlib.sha256(
            str(loop_state.load_state(root).get("current_goal", "")).encode("utf-8")
        ).hexdigest()[:16],
        "started_at": now_iso(),
        "completed_at": None,
        "reason": None,
        **resolution,
        "role_contract_hash": contract["hash"],
        "fallback_resolution_mode": None,
        "subject_fingerprint": (subject or {}).get("subject_fingerprint"),
        "subject_kind": (subject or {}).get("subject_kind", "unbound"),
        "subject_git_path": (subject or {}).get("subject_git_path"),
        "review_scope_bound": bool((subject or {}).get("review_scope_bound", False)),
        "review_scope": (subject or {}).get("review_scope"),
        "review_scope_fingerprint": (subject or {}).get("review_scope_fingerprint"),
        "review_subject_entries_fingerprint": (subject or {}).get("review_subject_entries_fingerprint"),
        "rebind_from_dispatch_id": (subject or {}).get("rebind_from_dispatch_id"),
        "review_delta_paths": list((subject or {}).get("review_delta_paths") or []),
        "association": association,
        "observation_id": None,
        "lifecycle_status": "not-observed",
        "lifecycle_observed_at": None,
        "result_source": None,
        "session_id": owner.get("session_id", ""),
        "owner_id": owner.get("owner_id", ""),
        "conversation_id": owner.get("conversation_id", ""),
        "tool": owner.get("tool", "unknown"),
    }
    state["dispatches"].append(item)
    record_dispatch(root, item)
    return item


def finish_dispatch(
    root: Path,
    dispatch: dict[str, Any],
    status: str,
    *,
    report_file: str | None = None,
    report_text: str | None = None,
    reason: str | None = None,
    result_source: str = "explicit-record",
    finding_outcome: str = "not-recorded",
    fix_regression: str = "not-recorded",
    verification_status: str = "not-recorded",
    verification_command: str | None = None,
) -> dict[str, Any]:
    if dispatch.get("status") != "started":
        raise DelegationError("only a started dispatch can be completed")
    dispatch["status"] = status
    dispatch["report_path"] = persist_report(root, dispatch, report_file, report_text)
    dispatch["completed_at"] = now_iso()
    dispatch["reason"] = bounded_text(reason, "reason") if reason else None
    dispatch["result_source"] = result_source
    if finding_outcome not in FEEDBACK_OUTCOMES:
        raise DelegationError("finding outcome is invalid")
    if fix_regression not in FOLLOW_UP_STATUSES:
        raise DelegationError("fix regression status is invalid")
    if verification_status not in FOLLOW_UP_STATUSES:
        raise DelegationError("verification status is invalid")
    if verification_command is not None:
        command = " ".join(str(verification_command).split()).strip()
        if not command or len(command) > 1000:
            raise DelegationError("verification command must contain 1-1000 characters")
        if SECRET_RE.search(command):
            raise DelegationError("verification command contains a secret-like value and was not recorded")
        dispatch["verification_command_hash"] = hashlib.sha256(command.encode("utf-8")).hexdigest()
        dispatch["verification_command_length"] = len(command)
    else:
        dispatch["verification_command_hash"] = None
        dispatch["verification_command_length"] = 0
    dispatch["finding_outcome"] = finding_outcome
    dispatch["fix_regression"] = fix_regression
    dispatch["verification_status"] = verification_status
    record_dispatch(root, dispatch)
    return dispatch


def validate_observation_id(raw: str) -> str:
    value = raw.strip()
    if not OBSERVATION_ID_RE.fullmatch(value):
        raise DelegationError("observation id has an invalid format")
    return value


def find_observed_dispatch(state: dict[str, Any], observation_id: str) -> dict[str, Any] | None:
    for item in reversed(state["dispatches"]):
        if item.get("observation_id") == observation_id:
            return item
    return None


def record_lifecycle_event(root: Path, dispatch: dict[str, Any], status: str) -> None:
    append_event(
        root,
        {
            "type": "subagent-lifecycle",
            "dispatch_id": dispatch.get("dispatch_id"),
            "role": dispatch.get("role"),
            "association": dispatch.get("association", "general"),
            "observation_id": dispatch.get("observation_id"),
            "status": status,
            "lifecycle_observed_at": dispatch.get("lifecycle_observed_at"),
            "subject_fingerprint": dispatch.get("subject_fingerprint"),
            "source": "tenetora hook observation",
        },
    )


def bind_observation(root: Path, dispatch: dict[str, Any], observation_id: str) -> dict[str, Any]:
    existing = dispatch.get("observation_id")
    if existing and existing != observation_id:
        raise DelegationError("active dispatch is already bound to another host subagent")
    if dispatch.get("lifecycle_status") == "running" and existing == observation_id:
        return dispatch
    dispatch["observation_id"] = observation_id
    dispatch["lifecycle_status"] = "running"
    dispatch["lifecycle_observed_at"] = now_iso()
    record_lifecycle_event(root, dispatch, "started")
    return dispatch


def observe_start(
    root: Path,
    state: dict[str, Any],
    role: str,
    trigger: str,
    observation_id: str,
    *,
    resolution_mode: str = "unspecified",
    host_tool: str = "unknown",
    host_agent: str | None = None,
    isolation_level: str = "unknown",
    expected_dispatch_id: str | None = None,
    identity: dict[str, str] | None = None,
) -> dict[str, Any]:
    request_identity = identity or {"session_id": "", "owner_id": "", "conversation_id": "", "tool": "unknown"}
    observation_id = validate_observation_id(observation_id)
    review = loop_state.load_state(root).get("review_cycle", {})
    dispatch: dict[str, Any] | None = None
    if isinstance(review, dict):
        active_id = str(review.get("active_dispatch_id") or "")
        status = str(review.get("status") or "idle")
        active_role = "code-reviewer" if status == "reviewing" else "implementer" if status == "fixing" else None
        if active_role is not None:
            if not active_id:
                raise DelegationError("active review/fix cycle has no dispatch to observe")
            if not expected_dispatch_id:
                raise DelegationError("observing an active review/fix cycle requires --dispatch-id")
            expected_dispatch_id = str(expected_dispatch_id)
            if expected_dispatch_id != active_id:
                raise DelegationError("observed dispatch is no longer the active review/fix dispatch")
            candidate = find_dispatch(state, expected_dispatch_id)
            ensure_dispatch_access(candidate, request_identity)
            expected_agent = bounded_text(str(host_agent), "host agent", 120) if host_agent else None
            if (
                role != active_role
                or candidate.get("role") != active_role
                or candidate.get("status") != "started"
                or candidate.get("resolution_mode") != resolution_mode
                or candidate.get("host_tool") != host_tool
                or candidate.get("host_agent") != expected_agent
                or candidate.get("isolation_level") != isolation_level
            ):
                raise DelegationError("observed carrier does not match the active review/fix dispatch contract")
            existing = find_observed_dispatch(state, observation_id)
            if existing is not None:
                ensure_dispatch_access(existing, request_identity)
                if existing is not candidate:
                    raise DelegationError("observation id is already bound to a different dispatch")
                return existing
            dispatch = candidate
        elif expected_dispatch_id:
            raise DelegationError("--dispatch-id is valid only for an active review/fix cycle")
    if dispatch is None:
        existing = find_observed_dispatch(state, observation_id)
        if existing is not None:
            ensure_dispatch_access(existing, request_identity)
            return existing
        if role == "implementer":
            raise DelegationError("observed implementer requires an active fix cycle")
        dispatch = start_dispatch(
            root,
            state,
            role,
            trigger,
            resolution_mode=resolution_mode,
            host_tool=host_tool,
            host_agent=host_agent,
            isolation_level=isolation_level,
            association="general-observed",
            identity=request_identity,
        )
    return bind_observation(root, dispatch, observation_id)


def observe_stop(
    root: Path,
    state: dict[str, Any],
    observation_id: str,
    execution_status: str,
    identity: dict[str, str] | None = None,
) -> dict[str, Any]:
    observation_id = validate_observation_id(observation_id)
    dispatch = find_observed_dispatch(state, observation_id)
    if dispatch is None:
        raise DelegationError("observed subagent dispatch was not found")
    ensure_dispatch_access(
        dispatch,
        identity or {"session_id": "", "owner_id": "", "conversation_id": "", "tool": "unknown"},
    )
    normalized = execution_status.strip().lower()
    if normalized not in {"completed", "failed", "cancelled"}:
        raise DelegationError("execution status must be completed, failed, or cancelled")
    current = str(dispatch.get("lifecycle_status") or "not-observed")
    if current in {"completed", "failed", "cancelled"}:
        return dispatch
    dispatch["lifecycle_status"] = normalized
    dispatch["lifecycle_observed_at"] = now_iso()
    record_lifecycle_event(root, dispatch, normalized)
    association = str(dispatch.get("association") or "general")
    if association == "review-cycle":
        if normalized != "completed":
            unavailable_dispatch(
                root,
                state,
                dispatch,
                f"host reviewer execution {normalized}",
                result_source="hook-lifecycle",
            )
        return dispatch
    if association == "fix-cycle":
        if normalized != "completed":
            fix_complete(
                root,
                state,
                str(dispatch["dispatch_id"]),
                "failed",
                None,
                None,
                result_source="hook-lifecycle",
                identity=identity,
            )
        return dispatch
    if normalized != "completed":
        finish_dispatch(
            root,
            dispatch,
            "failed",
            result_source="hook-lifecycle",
        )
    return dispatch


def update_review_cycle(
    root: Path,
    mutate: Any,
    *,
    delegation_state_before_loop: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = loop_state.load_state(root)
    review = state.get("review_cycle")
    if not isinstance(review, dict):
        review = loop_state.default_review_cycle()
        state["review_cycle"] = review
    mutate(state, review)
    if delegation_state_before_loop is not None:
        write_state(root, delegation_state_before_loop)
    state["updated_at"] = now_iso().replace("Z", "+00:00")
    loop_state.write_state(root, state)
    return review


def review_start(
    root: Path,
    state: dict[str, Any],
    trigger: str,
    *,
    resolution_mode: str = "unspecified",
    host_tool: str = "unknown",
    host_agent: str | None = None,
    isolation_level: str = "unknown",
    git_path: str | Path | None = None,
    subject_kind: str = "auto",
    review_scope: list[str] | None = None,
    identity: dict[str, str] | None = None,
) -> dict[str, Any]:
    holder: dict[str, Any] = {}
    try:
        subject = review_subject.capture_for_review(root, git_path, trigger, subject_kind, review_scope)
    except review_subject.ReviewSubjectError as exc:
        explicit_git_path = str(git_path or ".") != "."
        if explicit_git_path or review_scope is not None or (root / ".git").exists() or trigger.strip().lower() == "push":
            raise DelegationError(f"review subject could not be captured: {exc}") from exc
        subject = {
            "subject_fingerprint": None,
            "subject_kind": "unbound",
            "subject_git_path": None,
            "review_scope_bound": False,
            "review_scope": None,
            "review_scope_entries": None,
            "review_scope_fingerprint": None,
            "review_subject_entries": None,
            "review_subject_entries_fingerprint": None,
        }

    def mutate(loop: dict[str, Any], review: dict[str, Any]) -> None:
        if identity and identity.get("session_id") and identity.get("owner_id"):
            loop["session_id"] = identity["session_id"]
            loop["owner_id"] = identity["owner_id"]
            loop["conversation_id"] = identity.get("conversation_id", "")
            loop["tool"] = identity.get("tool", "unknown")
        active_id = str(review.get("active_dispatch_id") or "")
        if active_id:
            active_dispatch = next(
                (item for item in state["dispatches"] if item.get("dispatch_id") == active_id),
                None,
            )
            if active_dispatch is None or active_dispatch.get("status") != "started":
                review.update(
                    {
                        "active": False,
                        "status": "idle",
                        "active_dispatch_id": None,
                        "independent_review": "failed",
                        "completed_at": (
                            active_dispatch.get("completed_at")
                            if isinstance(active_dispatch, dict)
                            else now_iso()
                        ),
                    }
                )
        status = str(review.get("status", "idle"))
        if status not in {"idle", "reviewing"}:
            raise DelegationError(f"review cannot start from {status}")
        if review.get("active_dispatch_id"):
            raise DelegationError("another review dispatch is already active")
        round_number = int(review.get("round", 0)) + 1
        if round_number > int(review.get("max_review_rounds", 3)):
            raise DelegationError("review cycle has reached the review-round limit")
        item = start_dispatch(
            root,
            state,
            "code-reviewer",
            trigger,
            round_number=round_number,
            resolution_mode=resolution_mode,
            host_tool=host_tool,
            host_agent=host_agent,
            isolation_level=isolation_level,
            association="review-cycle",
            subject=subject,
            identity=identity,
        )
        review.update(
            {
                "active": True,
                "trigger": bounded_text(trigger, "trigger", 120),
                "round": round_number,
                "status": "reviewing",
                "active_dispatch_id": item["dispatch_id"],
                "independent_review": "pending",
                "resolution_mode": item["resolution_mode"],
                "isolation_level": item["isolation_level"],
                "contract_status": item["contract_status"],
                "goal_hash": item["goal_hash"],
                "started_at": item["started_at"],
                "completed_at": None,
                "subject_fingerprint": item["subject_fingerprint"],
                "subject_kind": item["subject_kind"],
                "subject_git_path": item["subject_git_path"],
                "review_scope_bound": item["review_scope_bound"],
                "review_scope": item["review_scope"],
                "review_scope_entries": subject.get("review_scope_entries"),
                "review_scope_fingerprint": item["review_scope_fingerprint"],
                "review_subject_entries": subject.get("review_subject_entries"),
                "review_subject_entries_fingerprint": item["review_subject_entries_fingerprint"],
                "rebind_from_dispatch_id": item.get("rebind_from_dispatch_id"),
                "review_delta_paths": item.get("review_delta_paths", []),
            }
        )
        holder.update(item)

    update_review_cycle(root, mutate, delegation_state_before_loop=state)
    return holder


def review_rebind(
    root: Path,
    state: dict[str, Any],
    trigger: str,
    *,
    git_path: str | Path | None = None,
    identity: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Start a new review for a changed full subject when an explicit scope is unchanged."""

    holder: dict[str, Any] = {}
    request_identity = identity or {"session_id": "", "owner_id": "", "conversation_id": "", "tool": "unknown"}
    loop = loop_state.load_state(root)
    review = loop.get("review_cycle", {})
    if not isinstance(review, dict) or review.get("status") != "passed":
        raise DelegationError("review rebind requires a passed independent review")
    if not review.get("review_scope_bound"):
        raise DelegationError("review rebind requires an explicit review scope")
    old_id = str(review.get("active_dispatch_id") or "")
    if not old_id:
        raise DelegationError("review rebind requires the latest review dispatch")
    old_dispatch = find_dispatch(state, old_id)
    ensure_dispatch_access(old_dispatch, request_identity)
    if old_dispatch.get("status") != "passed":
        raise DelegationError("review rebind requires a passed reviewer dispatch")

    stored_scope = review.get("review_scope")
    stored_scope_entries = review.get("review_scope_entries")
    stored_scope_fingerprint = review.get("review_scope_fingerprint")
    stored_subject_entries = review.get("review_subject_entries")
    stored_subject_entries_fingerprint = review.get("review_subject_entries_fingerprint")
    if (
        not isinstance(stored_scope, list)
        or not all(isinstance(item, str) for item in stored_scope)
        or not isinstance(stored_scope_entries, dict)
        or not all(isinstance(key, str) and isinstance(value, str) for key, value in stored_scope_entries.items())
        or not isinstance(stored_scope_fingerprint, str)
        or not isinstance(stored_subject_entries, dict)
        or not all(isinstance(key, str) and isinstance(value, str) for key, value in stored_subject_entries.items())
        or not isinstance(stored_subject_entries_fingerprint, str)
    ):
        raise DelegationError("review scope snapshot is missing or invalid; start a new review")
    if not secrets.compare_digest(
        stored_scope_fingerprint,
        review_subject.scope_fingerprint(stored_scope, stored_scope_entries),
    ) or not secrets.compare_digest(
        stored_subject_entries_fingerprint,
        review_subject.subject_entries_fingerprint(stored_subject_entries),
    ):
        raise DelegationError("review scope snapshot integrity check failed; start a new review")

    effective_git_path = git_path
    if str(effective_git_path or ".") == ".":
        effective_git_path = old_dispatch.get("subject_git_path") or review.get("subject_git_path") or "."
    subject_kind = "head" if review.get("subject_kind") == "git-head" else "index"
    try:
        current = review_subject.capture_subject(
            root,
            effective_git_path,
            kind=subject_kind,
            scopes=stored_scope,
        )
    except review_subject.ReviewSubjectError as exc:
        raise DelegationError(f"current review subject could not be captured: {exc}") from exc
    if current.get("subject_git_path") != review.get("subject_git_path"):
        raise DelegationError("review Git repository changed; start a new review for the current repository")
    if current.get("subject_fingerprint") == review.get("subject_fingerprint"):
        raise DelegationError("review rebind is unnecessary; the current Git subject is unchanged")
    current_scope_entries = current.get("review_scope_entries")
    if not isinstance(current_scope_entries, dict):
        raise DelegationError("current review scope snapshot is unavailable; start a new review")
    if current.get("review_scope_fingerprint") != stored_scope_fingerprint:
        changed_scope = sorted(
            path
            for path in set(stored_scope_entries) | set(current_scope_entries)
            if stored_scope_entries.get(path) != current_scope_entries.get(path)
        )
        suffix = ": " + ", ".join(changed_scope[:20]) if changed_scope else ""
        raise DelegationError(f"review scope changed; manual review is required{suffix}")
    current_subject_entries = current.get("review_subject_entries")
    if not isinstance(current_subject_entries, dict):
        raise DelegationError("current review subject delta is unavailable; start a new review")
    delta_paths = sorted(
        path
        for path in set(stored_subject_entries) | set(current_subject_entries)
        if stored_subject_entries.get(path) != current_subject_entries.get(path)
    )
    if not delta_paths:
        raise DelegationError("review subject changed without a path-level delta; start a new review")
    if len(delta_paths) > review_subject.MAX_REVIEW_DELTA_PATHS:
        raise DelegationError("review subject delta is too large for bounded rebind; start a new review")

    round_number = int(review.get("round", 0)) + 1
    if round_number > int(review.get("max_review_rounds", 3)):
        raise DelegationError("review cycle has reached the review-round limit")
    current.update(
        {
            "rebind_from_dispatch_id": old_id,
            "review_delta_paths": delta_paths,
        }
    )

    def mutate(loop_payload: dict[str, Any], review_payload: dict[str, Any]) -> None:
        item = start_dispatch(
            root,
            state,
            "code-reviewer",
            trigger,
            round_number=round_number,
            resolution_mode=str(old_dispatch.get("resolution_mode") or "unspecified"),
            host_tool=str(old_dispatch.get("host_tool") or "unknown"),
            host_agent=old_dispatch.get("host_agent"),
            isolation_level=str(old_dispatch.get("isolation_level") or "unknown"),
            association="review-cycle",
            subject=current,
            identity=request_identity,
        )
        review_payload.update(
            {
                "active": True,
                "trigger": bounded_text(trigger, "trigger", 120),
                "round": round_number,
                "status": "reviewing",
                "active_dispatch_id": item["dispatch_id"],
                "independent_review": "pending",
                "resolution_mode": item["resolution_mode"],
                "isolation_level": item["isolation_level"],
                "contract_status": item["contract_status"],
                "goal_hash": item["goal_hash"],
                "started_at": item["started_at"],
                "completed_at": None,
                "subject_fingerprint": item["subject_fingerprint"],
                "subject_kind": item["subject_kind"],
                "subject_git_path": item["subject_git_path"],
                "review_scope_bound": item["review_scope_bound"],
                "review_scope": item["review_scope"],
                "review_scope_entries": current.get("review_scope_entries"),
                "review_scope_fingerprint": item["review_scope_fingerprint"],
                "review_subject_entries": current.get("review_subject_entries"),
                "review_subject_entries_fingerprint": item["review_subject_entries_fingerprint"],
                "rebind_from_dispatch_id": old_id,
                "review_delta_paths": delta_paths,
            }
        )
        holder.update(item)

    update_review_cycle(root, mutate, delegation_state_before_loop=state)
    return holder


def append_review_report(review: dict[str, Any], dispatch: dict[str, Any]) -> None:
    reports = review.get("reports")
    if not isinstance(reports, list):
        reports = []
    if dispatch.get("report_path"):
        reports.append(dispatch["report_path"])
    review["reports"] = reports[-MAX_REPORTS:]


def review_result(
    root: Path,
    state: dict[str, Any],
    item_id: str,
    result: str,
    blocker: str | None,
    report_file: str | None,
    report_text: str | None,
    finding_outcome: str = "not-recorded",
    fix_regression: str = "not-recorded",
    verification_status: str = "not-recorded",
    verification_command: str | None = None,
    identity: dict[str, str] | None = None,
) -> dict[str, Any]:
    dispatch = find_dispatch(state, item_id)
    ensure_dispatch_access(
        dispatch,
        identity or {"session_id": "", "owner_id": "", "conversation_id": "", "tool": "unknown"},
    )
    if dispatch.get("role") != "code-reviewer":
        raise DelegationError("review result requires a code-reviewer dispatch")
    if result not in {"passed", "blocker"}:
        raise DelegationError("review result must be passed or blocker")
    current_review = loop_state.load_state(root).get("review_cycle", {})
    if (
        not isinstance(current_review, dict)
        or current_review.get("active_dispatch_id") != item_id
        or current_review.get("status") != "reviewing"
    ):
        raise DelegationError("dispatch is not the active review")
    if result == "blocker" and not blocker:
        raise DelegationError("blocker result requires --blocker")
    finish_dispatch(
        root,
        dispatch,
        result,
        report_file=report_file,
        report_text=report_text,
        result_source="model-reviewed-result",
        finding_outcome=finding_outcome,
        fix_regression=fix_regression,
        verification_status=verification_status,
        verification_command=verification_command,
    )
    write_state(root, state)

    def mutate(loop: dict[str, Any], review: dict[str, Any]) -> None:
        append_review_report(review, dispatch)
        review["active_dispatch_id"] = item_id
        if result == "passed":
            review.update(
                {
                    "active": False,
                    "status": "passed",
                    "independent_review": "passed",
                    "completed_at": dispatch.get("completed_at"),
                }
            )
            loop["last_blocker"] = ""
            loop["same_blocker_count"] = 0
            return
        safe_blocker = bounded_text(str(blocker), "blocker")
        previous = str(loop.get("last_blocker", ""))
        loop["last_blocker"] = safe_blocker
        loop["same_blocker_count"] = int(loop.get("same_blocker_count", 0)) + 1 if previous == safe_blocker else 1
        limit_hit = int(review.get("round", 0)) >= int(review.get("max_review_rounds", 3))
        repeated = int(loop.get("same_blocker_count", 0)) >= 2
        review.update(
            {
                "active": not (limit_hit or repeated),
                "status": "failed" if limit_hit or repeated else "needs-fix",
                "independent_review": "failed",
                "completed_at": dispatch.get("completed_at"),
            }
        )

    update_review_cycle(root, mutate)
    return dispatch


def fix_start(
    root: Path,
    state: dict[str, Any],
    parent_dispatch_id: str,
    *,
    resolution_mode: str = "unspecified",
    host_tool: str = "unknown",
    host_agent: str | None = None,
    isolation_level: str = "unknown",
    identity: dict[str, str] | None = None,
) -> dict[str, Any]:
    holder: dict[str, Any] = {}
    request_identity = identity or {"session_id": "", "owner_id": "", "conversation_id": "", "tool": "unknown"}
    ensure_dispatch_access(find_dispatch(state, parent_dispatch_id), request_identity)

    def mutate(loop: dict[str, Any], review: dict[str, Any]) -> None:
        if identity and identity.get("session_id") and identity.get("owner_id"):
            loop["session_id"] = identity["session_id"]
            loop["owner_id"] = identity["owner_id"]
            loop["conversation_id"] = identity.get("conversation_id", "")
            loop["tool"] = identity.get("tool", "unknown")
        if review.get("status") != "needs-fix":
            raise DelegationError("fix can start only when review_cycle.status is needs-fix")
        item = start_dispatch(
            root,
            state,
            "implementer",
            "review-fix",
            round_number=int(review.get("round", 0)),
            parent_dispatch_id=parent_dispatch_id,
            resolution_mode=resolution_mode,
            host_tool=host_tool,
            host_agent=host_agent,
            isolation_level=isolation_level,
            association="fix-cycle",
            subject={
                "subject_fingerprint": review.get("subject_fingerprint"),
                "subject_kind": review.get("subject_kind", "unbound"),
                "subject_git_path": review.get("subject_git_path"),
            },
            identity=identity,
        )
        review["fix_rounds"] = int(review.get("fix_rounds", 0)) + 1
        review["status"] = "fixing"
        review["active_dispatch_id"] = item["dispatch_id"]
        holder.update(item)

    update_review_cycle(root, mutate, delegation_state_before_loop=state)
    return holder


def fix_complete(
    root: Path,
    state: dict[str, Any],
    item_id: str,
    result: str,
    report_file: str | None,
    report_text: str | None,
    *,
    result_source: str = "explicit-record",
    finding_outcome: str = "not-recorded",
    fix_regression: str = "not-recorded",
    verification_status: str = "not-recorded",
    verification_command: str | None = None,
    identity: dict[str, str] | None = None,
) -> dict[str, Any]:
    dispatch = find_dispatch(state, item_id)
    ensure_dispatch_access(
        dispatch,
        identity or {"session_id": "", "owner_id": "", "conversation_id": "", "tool": "unknown"},
    )
    if dispatch.get("role") != "implementer":
        raise DelegationError("fix completion requires an implementer dispatch")
    if result not in {"passed", "failed"}:
        raise DelegationError("fix result must be passed or failed")
    current_review = loop_state.load_state(root).get("review_cycle", {})
    if (
        not isinstance(current_review, dict)
        or current_review.get("active_dispatch_id") != item_id
        or current_review.get("status") != "fixing"
    ):
        raise DelegationError("dispatch is not the active fix")
    finish_dispatch(
        root,
        dispatch,
        result,
        report_file=report_file,
        report_text=report_text,
        result_source=result_source,
        finding_outcome=finding_outcome,
        fix_regression=fix_regression,
        verification_status=verification_status,
        verification_command=verification_command,
    )
    write_state(root, state)

    def mutate(loop: dict[str, Any], review: dict[str, Any]) -> None:
        append_review_report(review, dispatch)
        review["active_dispatch_id"] = None
        review["status"] = "reviewing" if result == "passed" else "failed"
        review["active"] = result == "passed"

    update_review_cycle(root, mutate)
    return dispatch


def unavailable_dispatch(
    root: Path,
    state: dict[str, Any],
    dispatch: dict[str, Any],
    reason: str,
    *,
    result_source: str = "explicit-record",
) -> dict[str, Any]:
    dispatch["fallback_resolution_mode"] = "main-self-review"
    finish_dispatch(root, dispatch, "unavailable", reason=reason, result_source=result_source)
    write_state(root, state)

    def mutate(loop: dict[str, Any], review: dict[str, Any]) -> None:
        if review.get("active_dispatch_id") != dispatch.get("dispatch_id"):
            return
        review.update(
            {
                "active": False,
                "status": "unavailable",
                "active_dispatch_id": dispatch.get("dispatch_id"),
                "independent_review": "unavailable",
                "completed_at": dispatch.get("completed_at"),
            }
        )

    update_review_cycle(root, mutate)
    return dispatch


def parse_timestamp(raw: Any) -> dt.datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def cleanup(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc)
    report_dir = root / REPORT_DIR_REL
    archive_dir = root / REPORT_ARCHIVE_DIR_REL
    active = sorted(
        (
            (path.stat().st_mtime, path)
            for path in report_dir.glob("*.md")
            if not path.is_symlink() and path.is_file()
        )
        if report_dir.is_dir()
        else [],
        key=lambda item: item[0],
    )
    archived: list[str] = []
    moved_paths: dict[str, str] = {}
    while len(active) > MAX_REPORTS:
        _, path = active.pop(0)
        archive_dir.mkdir(parents=True, exist_ok=True)
        old_rel = path.relative_to(root).as_posix()
        destination = archive_dir / path.name
        path.replace(destination)
        destination.touch()
        new_rel = destination.relative_to(root).as_posix()
        archived.append(new_rel)
        moved_paths[old_rel] = new_rel
    if moved_paths:
        for item in state["dispatches"]:
            if item.get("report_path") in moved_paths:
                item["report_path"] = moved_paths[str(item["report_path"])]

    cutoff = (now - dt.timedelta(days=REPORT_RETENTION_DAYS)).timestamp()
    deleted: list[str] = []
    archived_files = (
        [path for path in archive_dir.glob("*.md") if not path.is_symlink() and path.is_file()]
        if archive_dir.is_dir()
        else []
    )
    for path in archived_files:
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            deleted.append(path.relative_to(root).as_posix())
    state["last_cleanup_at"] = now_iso()
    archive_remaining = (
        len([path for path in archive_dir.glob("*.md") if not path.is_symlink() and path.is_file()])
        if archive_dir.is_dir()
        else 0
    )
    return {
        "archived": archived,
        "deleted": deleted,
        "remaining": len(active),
        "archive_remaining": archive_remaining,
        "limit": MAX_REPORTS,
        "retention_days_after_archive": REPORT_RETENTION_DAYS,
    }


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="tenetora delegation",
        description="Record portable subagent dispatch governance. This command never spawns an agent.",
        add_help=False,
    )
    command.add_argument("--help", action="help", help="Show this help message and exit.")
    command.add_argument("-p", "--path", default=".", type=existing_project_path, metavar="<project-dir>")
    actions = command.add_mutually_exclusive_group()
    actions.add_argument("--list-roles", action="store_true", help="List portable role contracts.")
    actions.add_argument("--status", action="store_true", help="Show delegation and review-cycle state.")
    actions.add_argument(
        "--render-prompt",
        action="store_true",
        help="Render a project role contract for a host built-in agent without spawning it.",
    )
    actions.add_argument("--start", action="store_true", help="Record a platform dispatch attempt.")
    actions.add_argument("--complete", action="store_true", help="Complete a started general dispatch.")
    actions.add_argument("--unavailable", action="store_true", help="Record dispatch fallback to main-agent self-review.")
    actions.add_argument("--review-start", action="store_true", help="Start a bounded code review round.")
    actions.add_argument("--review-rebind", action="store_true", help="Start a new review after an out-of-scope subject delta.")
    actions.add_argument("--review-result", action="store_true", help="Record the active code review result.")
    actions.add_argument("--fix-start", action="store_true", help="Start an authorized bounded repair round.")
    actions.add_argument("--fix-complete", action="store_true", help="Complete the active bounded repair round.")
    actions.add_argument("--observe-start", action="store_true", help="Associate a host SubagentStart event.")
    actions.add_argument("--observe-stop", action="store_true", help="Record a host SubagentStop event.")
    actions.add_argument("--cleanup", action="store_true", help="Apply report retention and count limits.")
    command.add_argument("--role", choices=sorted(ROLES), help="Portable role contract.")
    command.add_argument("--trigger", help="Dispatch trigger, such as commit, security, or architecture-investigation.")
    command.add_argument("--assignment", help="Bounded assignment used only with --render-prompt.")
    command.add_argument(
        "--resolution-mode",
        choices=RESOLUTION_MODES,
        default="unspecified",
        help="How the host resolves the role. Defaults to unspecified for legacy callers.",
    )
    command.add_argument(
        "--host-tool",
        choices=HOST_TOOLS,
        default="unknown",
        help="Host AI tool performing dispatch.",
    )
    command.add_argument("--host-agent", help="Platform agent name selected by the host.")
    command.add_argument("--session-id", help="Owner-bound loop session identity when reviewing an active loop.")
    command.add_argument("--owner-id", help="Owner identity when reviewing an active loop.")
    command.add_argument("--conversation-id", help="Conversation identity for active-loop ownership checks.")
    command.add_argument("--tool", help="Host tool identity for active-loop ownership checks.")
    command.add_argument("--all-sessions", action="store_true", help="With --status, explicitly list dispatches from every owner.")
    command.add_argument(
        "--isolation-level",
        choices=ISOLATION_LEVELS,
        default="unknown",
        help="Declared role-boundary enforcement: hard, inherited, prompt-only, or unknown.",
    )
    command.add_argument(
        "--dispatch-id",
        help="Dispatch identifier returned by a start action; required when observing an active review/fix cycle.",
    )
    command.add_argument("--observation-id", help="Anonymous host lifecycle observation identifier.")
    command.add_argument(
        "--execution-status",
        choices=("completed", "failed", "cancelled"),
        help="Observed host subagent execution status.",
    )
    command.add_argument(
        "--git-path",
        default=".",
        help="Project-relative Git repository bound to a review cycle.",
    )
    command.add_argument(
        "--subject-kind",
        choices=("auto", "index", "head"),
        default="auto",
        help="Git snapshot used by --review-start. Auto uses index except for push.",
    )
    command.add_argument(
        "--review-scope",
        action="append",
        default=None,
        help="Selected Git-worktree-relative path scope reviewed by --review-start. Repeat for multiple paths.",
    )
    command.add_argument("--result", choices=("passed", "blocker", "failed"), help="Review, fix, or general result.")
    command.add_argument("--blocker", help="Stable blocker summary used for repeated-blocker detection.")
    command.add_argument(
        "--finding-outcome",
        choices=FEEDBACK_OUTCOMES,
        default="not-recorded",
        help="Review feedback: whether the reported finding was confirmed.",
    )
    command.add_argument(
        "--fix-regression",
        choices=FOLLOW_UP_STATUSES,
        default="not-recorded",
        help="Review feedback: whether the repair regressed in follow-up checks.",
    )
    command.add_argument(
        "--verification-status",
        choices=FOLLOW_UP_STATUSES,
        default="not-recorded",
        help="Follow-up verification status.",
    )
    command.add_argument(
        "--verification-command",
        help="Optional verification command; only a hash and length are persisted in governance metadata.",
    )
    command.add_argument("--reason", help="Bounded reason for an unavailable dispatch.")
    report = command.add_mutually_exclusive_group()
    report.add_argument("--report-file", help="Project-relative report source copied into the ignored cache.")
    report.add_argument("--report-text", help="Bounded report text copied into the ignored cache.")
    command.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return command


def require(value: Any, message: str) -> Any:
    if value is None or value == "":
        raise DelegationError(message)
    return value


def loop_for_request(root: Path, args: argparse.Namespace, *, safe: bool) -> dict[str, object]:
    raw = loop_state.load_state(root)
    if safe:
        return loop_state.safe_state_view(raw, args)
    if str(raw.get("status") or "idle") in loop_state.ACTIVE_LOOP_STATUSES and str(raw.get("current_goal") or ""):
        identity = loop_state.requested_identity(args)
        if not loop_state.identity_matches(raw, identity):
            raise DelegationError(
                "active loop state belongs to another or unknown conversation; pass matching --session-id/--owner-id"
            )
    return raw


def execute(args: argparse.Namespace) -> dict[str, Any]:
    root: Path = args.path
    identity = loop_state.requested_identity(args)
    if args.list_roles:
        return {
            "version": STATE_VERSION,
            "roles": ROLES,
            "resolution_order": ["dedicated", "builtin-role-injection", "main-self-review"],
            "isolation_levels": list(ISOLATION_LEVELS),
            "spawns_agents": False,
        }
    if args.render_prompt:
        return render_role_prompt(
            root,
            str(require(args.role, "--render-prompt requires --role")),
            str(require(args.assignment, "--render-prompt requires --assignment")),
            args.host_tool,
            args.host_agent,
            args.isolation_level,
        )
    mutating_action = any(
        (
            args.start,
            args.complete,
            args.unavailable,
            args.review_start,
            args.review_rebind,
            args.review_result,
            args.fix_start,
            args.fix_complete,
            args.observe_start,
            args.observe_stop,
            args.cleanup,
        )
    )
    if args.status or not mutating_action:
        state = load_state(root)
        loop = loop_for_request(root, args, safe=True)
        dispatches = visible_dispatches(state, identity, all_sessions=bool(args.all_sessions))
        return {
            "version": STATE_VERSION,
            "spawns_agents": False,
            "dispatch": None,
            "dispatches": dispatches,
            "review_cycle": loop.get("review_cycle", loop_state.default_review_cycle()),
            "cleanup": None,
            "fallback_label": "未经独立审查",
        }
    with transition_lock(root):
        state = load_state(root)
        loop_for_request(root, args, safe=False)
        cleanup_result: dict[str, Any] | None = None
        item: dict[str, Any] | None = None
        if args.start:
            role = str(require(args.role, "--start requires --role"))
            if role == "implementer":
                raise DelegationError("use --fix-start for implementer so review authorization can be enforced")
            item = start_dispatch(
                root,
                state,
                role,
                str(require(args.trigger, "--start requires --trigger")),
                resolution_mode=args.resolution_mode,
                host_tool=args.host_tool,
                host_agent=args.host_agent,
                isolation_level=args.isolation_level,
                identity=identity,
            )
        elif args.complete:
            item = find_dispatch(state, str(require(args.dispatch_id, "--complete requires --dispatch-id")))
            ensure_dispatch_access(item, identity)
            result = str(require(args.result, "--complete requires --result"))
            if result == "blocker":
                raise DelegationError("use --review-result for blocker outcomes")
            finish_dispatch(
                root,
                item,
                result,
                report_file=args.report_file,
                report_text=args.report_text,
                finding_outcome=args.finding_outcome,
                fix_regression=args.fix_regression,
                verification_status=args.verification_status,
                verification_command=args.verification_command,
            )
        elif args.unavailable:
            item = find_dispatch(state, str(require(args.dispatch_id, "--unavailable requires --dispatch-id")))
            ensure_dispatch_access(item, identity)
            unavailable_dispatch(
                root,
                state,
                item,
                str(require(args.reason, "--unavailable requires --reason")),
            )
        elif args.review_start:
            item = review_start(
                root,
                state,
                args.trigger or "commit",
                resolution_mode=args.resolution_mode,
                host_tool=args.host_tool,
                host_agent=args.host_agent,
                isolation_level=args.isolation_level,
                git_path=args.git_path,
                subject_kind=args.subject_kind,
                review_scope=args.review_scope,
                identity=identity,
            )
        elif args.review_rebind:
            item = review_rebind(
                root,
                state,
                args.trigger or "commit",
                git_path=args.git_path,
                identity=identity,
            )
        elif args.review_result:
            item = review_result(
                root,
                state,
                str(require(args.dispatch_id, "--review-result requires --dispatch-id")),
                str(require(args.result, "--review-result requires --result")),
                args.blocker,
                args.report_file,
                args.report_text,
                args.finding_outcome,
                args.fix_regression,
                args.verification_status,
                args.verification_command,
                identity=identity,
            )
        elif args.fix_start:
            item = fix_start(
                root,
                state,
                str(require(args.dispatch_id, "--fix-start requires blocker --dispatch-id")),
                resolution_mode=args.resolution_mode,
                host_tool=args.host_tool,
                host_agent=args.host_agent,
                isolation_level=args.isolation_level,
                identity=identity,
            )
        elif args.fix_complete:
            item = fix_complete(
                root,
                state,
                str(require(args.dispatch_id, "--fix-complete requires --dispatch-id")),
                str(require(args.result, "--fix-complete requires --result")),
                args.report_file,
                args.report_text,
                finding_outcome=args.finding_outcome,
                fix_regression=args.fix_regression,
                verification_status=args.verification_status,
                verification_command=args.verification_command,
                identity=identity,
            )
        elif args.observe_start:
            item = observe_start(
                root,
                state,
                str(require(args.role, "--observe-start requires --role")),
                args.trigger or f"{args.host_tool}-subagent-observed",
                str(require(args.observation_id, "--observe-start requires --observation-id")),
                resolution_mode=args.resolution_mode,
                host_tool=args.host_tool,
                host_agent=args.host_agent,
                isolation_level=args.isolation_level,
                expected_dispatch_id=args.dispatch_id,
                identity=identity,
            )
        elif args.observe_stop:
            item = observe_stop(
                root,
                state,
                str(require(args.observation_id, "--observe-stop requires --observation-id")),
                str(require(args.execution_status, "--observe-stop requires --execution-status")),
                identity=identity,
            )
        elif args.cleanup:
            cleanup_result = cleanup(root, state)
        if item is not None or cleanup_result is not None:
            cleanup_result = cleanup(root, state) if cleanup_result is None else cleanup_result
            write_state(root, state)
        loop = loop_for_request(root, args, safe=False)
        return {
            "version": STATE_VERSION,
            "spawns_agents": False,
            "dispatch": item,
            "dispatches": visible_dispatches(state, identity, all_sessions=bool(args.all_sessions)),
            "review_cycle": loop.get("review_cycle", loop_state.default_review_cycle()),
            "cleanup": cleanup_result,
            "fallback_label": "未经独立审查",
        }


def print_text(payload: dict[str, Any]) -> None:
    chinese = os.environ.get("TENETORA_LANG", "").strip().lower() in {"zh", "zh-cn", "zh-hans", "chinese", "中文"}
    if "prompt" in payload:
        print(payload["prompt"])
        return
    if "roles" in payload:
        print("Tenetora 可移植子代理角色" if chinese else "Tenetora portable subagent roles")
        for name, role in payload["roles"].items():
            print(f"- {name}: {role['description']} ({role['mode']})")
        print("CLI 只记录治理状态，绝不会启动 agent。" if chinese else "The CLI records governance state and never spawns agents.")
        return
    print("Tenetora 子代理调度" if chinese else "Tenetora Delegation")
    item = payload.get("dispatch")
    if isinstance(item, dict):
        print(f"- 调度 ID: {item.get('dispatch_id')}" if chinese else f"- dispatch_id: {item.get('dispatch_id')}")
        print(f"- 角色: {item.get('role')}" if chinese else f"- role: {item.get('role')}")
        print(f"- 状态: {item.get('status')}" if chinese else f"- status: {item.get('status')}")
        print(f"- 解析方式: {item.get('resolution_mode', 'unspecified')}" if chinese else f"- resolution: {item.get('resolution_mode', 'unspecified')}")
        print(f"- 载体: {item.get('host_tool', 'unknown')}/{item.get('host_agent') or '-'}" if chinese else f"- carrier: {item.get('host_tool', 'unknown')}/{item.get('host_agent') or '-'}")
        print(f"- 隔离: {item.get('isolation_level', 'unknown')} ({item.get('contract_status', 'unknown')})" if chinese else f"- isolation: {item.get('isolation_level', 'unknown')} ({item.get('contract_status', 'unknown')})")
        if item.get("report_path"):
            print(f"- 报告: {item.get('report_path')}" if chinese else f"- report: {item.get('report_path')}")
    review = payload.get("review_cycle", {})
    if isinstance(review, dict):
        print(
            f"- 审查循环: {review.get('status', 'idle')}（审查 {review.get('round', 0)}/3，修复 {review.get('fix_rounds', 0)}/2）"
            if chinese
            else f"- review_cycle: {review.get('status', 'idle')} (review {review.get('round', 0)}/3, fix {review.get('fix_rounds', 0)}/2)"
        )
    print("- 调度执行: 由平台/模型负责；CLI 不会启动 agent" if chinese else "- dispatch_execution: platform/model responsibility; CLI does not spawn agents")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        payload = execute(args)
    except (DelegationError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_text(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
