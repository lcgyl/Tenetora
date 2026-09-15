"""Build strictly redacted Tenetora diagnostics for support handoff."""

from __future__ import annotations

import json
import os
import platform
import re
import tempfile
import zipfile
from pathlib import Path

from . import __version__
from .brand import PRODUCT_NAME, machine_home
from .environment import EnvironmentStatus, collect_status
from .path_security import validate_unredirected_file_path


SCHEMA_VERSION = 1
ARCHIVE_FILES = ("diagnostics.json", "README.txt")
KNOWN_TRANSACTION_STATUSES = {"running", "completed", "rolled-back", "blocked", "failed"}
SECRET_PATTERN = re.compile(
    r"(?i)(?:authorization|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"proxy[_-]?authorization|password|passwd|secret|api[_-]?key|token)\s*[:=]\s*"
    r"(?:bearer\s+)?[^\s,;&]+"
)
TOKEN_PATTERN = re.compile(r"(?i)\b(?:glpat-[a-z0-9_.-]+|ghp_[a-z0-9]{12,}|github_pat_[a-z0-9_]{12,}|sk-[a-z0-9_.-]{12,})\b")
SAFE_METADATA_PATTERN = re.compile(r"^[A-Za-z0-9_.:+-]{1,128}$")
URL_PATTERN = re.compile(r"(?i)\b(?:https?|ssh|git)://[^\s\"'<>]+")
WINDOWS_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'<>;,)}]+")
UNIX_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9])/(?!/)[^\s\"'<>;,)}]+")


class DiagnosticsError(RuntimeError):
    """Raised when a diagnostics bundle cannot be safely produced."""


def _sanitize_text(value: object) -> str:
    text = str(value)
    text = SECRET_PATTERN.sub("[REDACTED]", text)
    text = TOKEN_PATTERN.sub("[REDACTED_TOKEN]", text)
    text = URL_PATTERN.sub("[REDACTED_URL]", text)
    text = WINDOWS_PATH_PATTERN.sub("[REDACTED_PATH]", text)
    text = UNIX_PATH_PATTERN.sub("[REDACTED_PATH]", text)
    return text


def _safe_string(value: object, default: str = "unknown") -> str:
    if value is None:
        return default
    return _sanitize_text(value)


def _safe_scalar(value: object) -> object:
    if isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return _safe_string(value)
    return None


def _safe_count(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_metadata(value: object) -> str:
    sanitized = _safe_string(value, "")
    return sanitized if SAFE_METADATA_PATTERN.fullmatch(sanitized) else "[REDACTED]"


def _compact_sources(sources: object) -> dict[str, object]:
    if not isinstance(sources, dict):
        return {}
    compact: dict[str, object] = {}
    for name in ("skill", "plugin", "runtime"):
        item = sources.get(name)
        if not isinstance(item, dict):
            continue
        selected = {
            key: _safe_scalar(item[key])
            for key in ("status", "state", "basis", "configured_status")
            if isinstance(item.get(key), (str, int, bool))
        }
        effective = item.get("effective")
        if isinstance(effective, dict):
            selected["effective_state"] = _safe_string(effective.get("state"), "unknown")
            selected["effective_version"] = _safe_string(effective.get("version"), "unknown")
        compact[name] = selected
    return compact


def _compact_tool(tool: object) -> dict[str, object]:
    skills = getattr(tool, "skills", None) or []
    safe_skills = []
    for skill in skills:
        safe_skills.append(
            {
                "name": _safe_string(getattr(skill, "name", None)),
                "state": _safe_string(getattr(skill, "state", None)),
                "mode": _safe_string(getattr(skill, "mode", None)),
                "version": _safe_string(getattr(skill, "version", None)),
            }
        )
    command = getattr(tool, "command", None)
    command_name = command if isinstance(command, str) and "/" not in command and "\\" not in command else None
    return {
        "tool": _safe_string(getattr(tool, "tool", None)),
        "scope": _safe_string(getattr(tool, "scope", None)),
        "state": _safe_string(getattr(tool, "state", None)),
        "mode": _safe_string(getattr(tool, "mode", None)),
        "command": command_name,
        "command_available": bool(getattr(tool, "command_available", False)),
        "plugin_state": _safe_string(getattr(tool, "plugin_state", None), "not-applicable"),
        "hooks_state": _safe_string(getattr(tool, "hooks_state", None), "not-applicable"),
        "activation_state": _safe_string(getattr(tool, "activation_state", None), "not-applicable"),
        "registration_state": _safe_string(getattr(tool, "registration_state", None), "not-applicable"),
        "runtime_state": _safe_string(getattr(tool, "runtime_state", None), "not-applicable"),
        "hook_mode": _safe_string(getattr(tool, "hook_mode", None), "not-applicable"),
        "degraded_reason": _safe_string(getattr(tool, "degraded_reason", None), ""),
        "skills": safe_skills,
        "sources": _compact_sources(getattr(tool, "effective_sources", None)),
        "runtime_observation": _compact_sources({"runtime": getattr(tool, "runtime_observation", None)}),
        "version_alignment": {
            key: _safe_string(value)
            for key, value in (getattr(tool, "version_alignment", None) or {}).items()
            if key in {"status", "expected", "observed", "basis"}
        },
    }


def _compact_finding(finding: object) -> dict[str, object]:
    if not isinstance(finding, dict):
        return {"summary": "invalid finding"}
    remediation = finding.get("remediation")
    safe_remediation = {}
    if isinstance(remediation, dict):
        safe_remediation = {
            "command": _safe_string(remediation.get("command"), ""),
            "description": _safe_string(remediation.get("description"), ""),
            "safe_to_auto_apply": bool(remediation.get("safe_to_auto_apply")),
            "requires_confirmation": bool(remediation.get("requires_confirmation")),
        }
    evidence = finding.get("evidence")
    return {
        key: _safe_string(finding.get(key))
        for key in ("finding_id", "code", "severity", "tool", "scope")
        if finding.get(key) is not None
    } | {
        "affects_health": bool(finding.get("affects_health")),
        "summary": _safe_string(finding.get("summary"), ""),
        "evidence": [_safe_string(item) for item in evidence] if isinstance(evidence, list) else [],
        "remediation": safe_remediation,
    }


def _compact_activity(activity: object) -> dict[str, object]:
    if not isinstance(activity, dict):
        return {"status": "invalid", "counts": {}, "recent": []}
    counts = activity.get("counts") if isinstance(activity.get("counts"), dict) else {}
    recent = activity.get("recent") if isinstance(activity.get("recent"), list) else []
    return {
        "status": _safe_string(activity.get("status")),
        "events_considered": _safe_count(activity.get("events_considered")),
        "event_limit": _safe_count(activity.get("event_limit")),
        "counts": {
            _safe_string(key): int(value)
            for key, value in counts.items()
            if isinstance(value, int) and not isinstance(value, bool)
        },
        "recent": [
            {
                key: _safe_string(item.get(key))
                for key in ("type", "action", "status", "timestamp")
                if isinstance(item, dict) and isinstance(item.get(key), str)
            }
            for item in recent[-5:]
            if isinstance(item, dict)
        ],
    }


def _read_upgrade_state() -> dict[str, object]:
    try:
        state_path = machine_home() / "state" / "upgrade-transaction.json"
        state_path = validate_unredirected_file_path(state_path, label="upgrade transaction")
    except (OSError, RuntimeError):
        return {"status": "invalid", "reason": "unsafe-or-unavailable"}
    if not state_path.exists():
        return {"status": "not-observed"}
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"status": "invalid", "reason": "unreadable"}
    if not isinstance(payload, dict):
        return {"status": "invalid", "reason": "invalid-shape"}
    status = payload.get("status")
    if not isinstance(status, str) or status not in KNOWN_TRANSACTION_STATUSES:
        return {"status": "invalid", "reason": "unknown-status"}
    result: dict[str, object] = {"status": status}
    for key in ("stage", "source_version", "target_version", "error_code"):
        if isinstance(payload.get(key), (str, int, bool)):
            result[key] = _safe_metadata(payload[key])
    result["recoverable"] = status in {"running", "rolled-back", "blocked", "failed"}
    return result


def _compact_project(status: EnvironmentStatus) -> dict[str, object]:
    project = status.project if isinstance(status.project, dict) else {}
    harness = project.get("harness") if isinstance(project.get("harness"), dict) else {}
    units = project.get("repository_units") if isinstance(project.get("repository_units"), dict) else {}
    alignment = harness.get("alignment") if isinstance(harness.get("alignment"), dict) else {}
    delegation = harness.get("delegation") if isinstance(harness.get("delegation"), dict) else {}
    return {
        "initialized": bool(harness.get("exists")),
        "harness_ignored": bool(harness.get("ignored")),
        "inspection_scope": _safe_string(project.get("inspection_scope")),
        "tool_selection": _safe_string(project.get("tool_selection")),
        "alignment_status": _safe_string(alignment.get("status")),
        "alignment_stale": bool(alignment.get("stale")),
        "delegation_infrastructure": _safe_string(delegation.get("infrastructure")),
        "repository_units": {
            "status": _safe_string(units.get("status")),
            "staged_status": _safe_string(units.get("staged_status")),
            "unit_count": len(units.get("units", [])) if isinstance(units.get("units"), list) else 0,
            "problem_count": len(units.get("problems", [])) if isinstance(units.get("problems"), list) else 0,
        },
        "activity_status": _safe_string(status.activity.get("status") if isinstance(status.activity, dict) else None),
    }


def _compact_health(status: EnvironmentStatus) -> dict[str, object]:
    health = status.health if isinstance(status.health, dict) else {}
    return {
        "status": _safe_string(health.get("status")),
        "summary": _safe_string(health.get("summary"), ""),
        "relevant_platforms": [_safe_string(item) for item in health.get("relevant_platforms", []) if isinstance(item, str)],
        "active_platforms": [_safe_string(item) for item in health.get("active_platforms", []) if isinstance(item, str)],
        "actionable_findings": _safe_count(health.get("actionable_findings")),
        "dormant_findings": _safe_count(health.get("dormant_findings")),
        "acknowledged_findings": _safe_count(health.get("acknowledged_findings")),
        "next_actions": [_safe_string(item) for item in health.get("next_actions", []) if isinstance(item, str)],
    }


def _fallback_payload(error_code: str) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "bundle_type": "tenetora-diagnostics",
        "status": "blocked",
        "error_code": error_code,
        "cli": {"product": PRODUCT_NAME, "version": __version__},
        "project": {"initialized": False, "activity_status": "not-observed"},
        "tools": [],
        "health": {"status": "BLOCKED", "actionable_findings": 1},
        "findings": [],
        "activity": {"status": "not-observed", "counts": {}, "recent": []},
        "upgrade": _read_upgrade_state(),
        "redaction": {"mode": "strict", "omitted": ["credentials", "local paths", "URLs", "project content", "raw state"]},
    }


def build_diagnostics(root: Path, tools: list[str], scope: str) -> dict[str, object]:
    try:
        status = collect_status(root=root, tools=tools, scope=scope, include_diagnostics=True, tool_selection="explicit")
    except Exception:
        return _fallback_payload("status-collection-failed")
    health = _compact_health(status)
    activity = _compact_activity(status.activity)
    upgrade = _read_upgrade_state()
    diagnostic_status = "blocked" if health["status"] == "BLOCKED" else "attention" if upgrade["status"] == "invalid" or activity["status"] == "invalid" else "ok"
    return {
        "schema_version": SCHEMA_VERSION,
        "bundle_type": "tenetora-diagnostics",
        "status": diagnostic_status,
        "cli": {
            "product": PRODUCT_NAME,
            "version": __version__,
            "python": platform.python_version(),
            "platform": platform.system().lower(),
            "machine": platform.machine().lower(),
        },
        "project": _compact_project(status),
        "tools": [_compact_tool(tool) for tool in status.tools],
        "health": health,
        "findings": [_compact_finding(item) for item in status.findings],
        "activity": activity,
        "upgrade": upgrade,
        "redaction": {
            "mode": "strict",
            "omitted": ["credentials", "local paths", "URLs", "project content", "raw governance events", "unrecognized state fields"],
        },
    }


def _archive_readme() -> str:
    return (
        "Tenetora redacted diagnostics bundle\n"
        "\n"
        "This archive contains only diagnostics.json and this explanation.\n"
        "Credentials, local paths, URLs, project content, and raw state are omitted.\n"
        "Review the JSON before sharing it with a third party.\n"
    )


def write_bundle(payload: dict[str, object], output: Path, *, force: bool = False) -> None:
    try:
        destination = validate_unredirected_file_path(output, label="diagnostics output")
    except (OSError, RuntimeError) as error:
        raise DiagnosticsError("diagnostics output path is unsafe") from error
    if not destination.parent.is_dir():
        raise DiagnosticsError("diagnostics output directory does not exist")
    if destination.exists() and not force:
        raise DiagnosticsError("diagnostics output already exists; use --force to replace it")
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".tenetora-diagnostics-",
            suffix=".tmp",
            dir=str(destination.parent),
        )
    except OSError as error:
        raise DiagnosticsError("unable to create diagnostics bundle") from error
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("diagnostics.json", json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            archive.writestr("README.txt", _archive_readme())
        os.replace(temporary, destination)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        raise DiagnosticsError("unable to write diagnostics bundle") from error
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def render_text(payload: dict[str, object], *, output_written: bool = False) -> str:
    status = payload.get("status", "unknown")
    health = payload.get("health") if isinstance(payload.get("health"), dict) else {}
    lines = [f"Tenetora diagnostics: {status}", f"Health: {health.get('status', 'unknown')}"]
    if output_written:
        lines.append("Bundle: written")
    lines.append("Next: review diagnostics.json before sharing")
    return "\n".join(lines)
