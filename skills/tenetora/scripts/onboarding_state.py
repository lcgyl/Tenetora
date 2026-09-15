#!/usr/bin/env python3
"""Derive privacy-bounded onboarding state from existing installation facts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
CLI_DIR = SCRIPT_DIR.parent / "cli"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from installation_registry import load_registry, project_id  # noqa: E402
from migrate_harness import classify_project  # noqa: E402
from tenetora.file_lock import locked_file  # noqa: E402
from path_security import validate_unredirected_path  # noqa: E402
from tenetora.brand import machine_home  # noqa: E402


SCHEMA_VERSION = 1
MAX_STATE_BYTES = 1024 * 1024
MAX_PROJECTS = 2048
MAX_REMINDERS_PER_PROJECT = 16
VALID_STATES = {
    "unseen",
    "reminded",
    "deferred",
    "initialized",
    "migration-needed",
    "review-required",
    "declined",
    "dismissed",
}
VALID_CLASSIFICATIONS = {
    "absent",
    "canonical",
    "canonical-invalid",
    "canonical-with-foreign",
    "canonical-with-preserved-legacy",
    "coexistence",
    "foreign",
    "legacy-ambiguous",
    "legacy-mixed",
    "legacy-owned",
}
TOOL_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
PROJECT_ID_RE = re.compile(r"^[0-9a-f]{20}$")
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


class OnboardingStateError(RuntimeError):
    """Raised when onboarding state is unsafe or concurrently changed."""


def path_for(home: Path) -> Path:
    try:
        safe_home = validate_unredirected_path(home, label="Tenetora onboarding home")
    except RuntimeError as exc:
        raise OnboardingStateError(str(exc)) from exc
    return safe_home / "state" / "project-onboarding.json"


def _assert_state_location(path: Path) -> None:
    home = path.parents[1]
    parent = path.parent
    if parent.is_symlink() or path.is_symlink():
        raise OnboardingStateError("project onboarding state must not be a symbolic link")
    try:
        parent.relative_to(home)
    except ValueError as exc:
        raise OnboardingStateError("project onboarding state is outside its managed home") from exc


def _read(path: Path) -> dict[str, Any]:
    _assert_state_location(path)
    if not path.is_file():
        if path.exists():
            raise OnboardingStateError("project onboarding state is not a regular file")
        return {"version": SCHEMA_VERSION, "revision": 0, "projects": []}
    try:
        if path.stat().st_size > MAX_STATE_BYTES:
            raise OnboardingStateError("project onboarding state exceeds its bounded size limit")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OnboardingStateError("project onboarding state is unreadable") from exc
    if not isinstance(payload, dict) or payload.get("version") != SCHEMA_VERSION:
        raise OnboardingStateError("project onboarding schema is invalid")
    if type(payload.get("revision")) is not int or payload["revision"] < 0:
        raise OnboardingStateError("project onboarding revision is invalid")
    if not isinstance(payload.get("projects"), list):
        raise OnboardingStateError("project onboarding entries are invalid")
    if len(payload["projects"]) > MAX_PROJECTS:
        raise OnboardingStateError("project onboarding state exceeds its project limit")
    return payload


def _write(path: Path, payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if len(serialized.encode("utf-8")) > MAX_STATE_BYTES:
        raise OnboardingStateError("project onboarding state exceeds its bounded size limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_state_location(path)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def project_state(item: dict[str, Any]) -> str:
    governance = item.get("governance") if isinstance(item.get("governance"), dict) else None
    if governance is None:
        return "unseen"
    state = str(governance.get("state") or "")
    layout = str(governance.get("layout") or "")
    if state == "canonical" and layout == ".tenetora":
        return "initialized"
    if state == "legacy-owned" or layout == ".harness":
        return "migration-needed"
    return "review-required"


def repository_continuity_fingerprint(path: Path) -> str:
    """Return a privacy-bounded identity that survives moves and worktrees."""

    if not path.is_dir():
        return ""
    probes = (
        ("origin", ["git", "-C", str(path), "config", "--get", "remote.origin.url"]),
        ("root", ["git", "-C", str(path), "rev-list", "--max-parents=0", "--all"]),
    )
    for label, command in probes:
        try:
            result = subprocess.run(
                command,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        values = sorted(line.strip() for line in result.stdout.splitlines() if line.strip())
        if result.returncode == 0 and values:
            material = f"git-{label}\0" + "\0".join(values)
            return hashlib.sha256(material.encode("utf-8")).hexdigest()
    return ""


def repository_instance_fingerprint(path: Path) -> str:
    """Return a local opaque identity that survives an in-filesystem move."""

    try:
        marker = path / ".git"
        stat = marker.stat()
    except OSError:
        return ""
    material = f"{stat.st_dev}:{stat.st_ino}:{stat.st_mode}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _is_project_candidate(home: Path, project: Path) -> bool:
    if not project.is_dir() or project.is_symlink() or not (project / ".git").exists():
        return False
    resolved = project.resolve(strict=False)
    user_home = Path.home().resolve(strict=False)
    managed_home = home.resolve(strict=False)
    if resolved in {Path(resolved.anchor), user_home, managed_home, Path(tempfile.gettempdir()).resolve()}:
        return False
    lowered = tuple(part.lower() for part in resolved.parts)
    excluded_sequences = (
        (".codex", "plugins", "cache"),
        (".claude", "plugins", "cache"),
        (".tenetora", "releases"),
    )
    return not any(
        lowered[index : index + len(sequence)] == sequence
        for sequence in excluded_sequences
        for index in range(len(lowered) - len(sequence) + 1)
    )


def _classification_state(status: str) -> str:
    if status in {"canonical", "canonical-with-preserved-legacy"}:
        return "initialized"
    if status == "legacy-owned":
        return "migration-needed"
    if status == "absent":
        return "unseen"
    return "review-required"


def _reminders(item: dict[str, Any]) -> dict[str, dict[str, str]]:
    raw = item.get("reminders")
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, dict[str, str]] = {}
    for tool, value in sorted(raw.items()):
        if not isinstance(tool, str) or TOOL_RE.fullmatch(tool) is None or not isinstance(value, dict):
            continue
        classification = value.get("classification")
        reminded_at = value.get("reminded_at")
        if (
            isinstance(classification, str)
            and classification in VALID_CLASSIFICATIONS
            and isinstance(reminded_at, str)
            and _valid_timestamp(reminded_at)
        ):
            normalized[tool] = {"classification": classification, "reminded_at": reminded_at}
            if len(normalized) >= MAX_REMINDERS_PER_PROJECT:
                break
    return normalized


def _valid_timestamp(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _normalized_entry(item: dict[str, Any]) -> dict[str, Any] | None:
    """Keep only bounded onboarding fields before persisting historical state."""

    identity = item.get("project_id")
    if not isinstance(identity, str) or PROJECT_ID_RE.fullmatch(identity) is None:
        return None
    entry: dict[str, Any] = {"project_id": identity}
    for field in (
        "path_fingerprint",
        "continuity_fingerprint",
        "instance_fingerprint",
        "governance_fingerprint",
    ):
        value = item.get(field)
        if isinstance(value, str) and FINGERPRINT_RE.fullmatch(value):
            entry[field] = value
    state = item.get("state")
    if isinstance(state, str) and state in VALID_STATES:
        entry["state"] = state
    derived_state = item.get("derived_state")
    if isinstance(derived_state, str) and derived_state in VALID_STATES:
        entry["derived_state"] = derived_state
    classification = item.get("classification")
    if isinstance(classification, str) and classification in VALID_CLASSIFICATIONS:
        entry["classification"] = classification
    tools = item.get("tools")
    if isinstance(tools, list):
        entry["tools"] = sorted(
            {
                tool
                for tool in tools
                if isinstance(tool, str) and TOOL_RE.fullmatch(tool) is not None
            }
        )
    entry["reminders"] = _reminders(item)
    return entry


def observe_project(home: Path, project: Path, tool: str) -> dict[str, Any]:
    """Record and return the one-time onboarding decision for one host tool."""

    try:
        home = validate_unredirected_path(home, label="Tenetora onboarding home")
        project = validate_unredirected_path(project, label="onboarding project")
    except RuntimeError as exc:
        raise OnboardingStateError(str(exc)) from exc
    tool = tool.strip().lower()
    if TOOL_RE.fullmatch(tool) is None or not _is_project_candidate(home, project):
        return {"eligible": False, "should_remind": False, "state": "unseen", "classification": "ineligible"}

    classification = classify_project(project)
    classification_status = str(classification.status)
    state = _classification_state(classification_status)
    identity = project_id(project)
    continuity = repository_continuity_fingerprint(project)
    instance = repository_instance_fingerprint(project)
    path = path_for(home)
    _assert_state_location(path)
    lock = path.with_name(f".{path.name}.lock")
    with locked_file(lock):
        previous = _read(path)
        entries = [
            normalized
            for item in previous["projects"]
            if isinstance(item, dict)
            for normalized in [_normalized_entry(item)]
            if normalized is not None
        ]
        prior = next((item for item in entries if item.get("project_id") == identity), None)
        if prior is None and instance:
            matches = [
                item
                for item in entries
                if item.get("instance_fingerprint") == instance
                and (not continuity or item.get("continuity_fingerprint") == continuity)
            ]
            if len(matches) == 1:
                prior = matches[0]
                entries.remove(prior)
        prior = prior or {}
        reminders = _reminders(prior)
        prior_reminder = reminders.get(tool)
        prior_state = str(prior.get("state") or "")
        suppressed = (
            prior_state == "declined" and state == "unseen"
        ) or (
            prior_state == "dismissed" and state in {"migration-needed", "review-required"}
        )
        should_remind = not suppressed and state != "initialized" and (
            prior_reminder is None or prior_reminder.get("classification") != classification_status
        )
        if should_remind:
            reminders[tool] = {
                "classification": classification_status,
                "reminded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            }
        stored_state = prior_state if suppressed else ("reminded" if state == "unseen" and reminders else state)
        entry: dict[str, Any] = {
            "project_id": identity,
            "path_fingerprint": hashlib.sha256(str(project).encode("utf-8")).hexdigest(),
            **({"continuity_fingerprint": continuity} if continuity else {}),
            **({"instance_fingerprint": instance} if instance else {}),
            "state": stored_state,
            "derived_state": state,
            "classification": classification_status,
            "reminders": reminders,
        }
        entries = [item for item in entries if item.get("project_id") != identity]
        entries.append(entry)
        if len(entries) > MAX_PROJECTS:
            raise OnboardingStateError("project onboarding state exceeds its project limit")
        payload = {
            "version": SCHEMA_VERSION,
            "revision": int(previous["revision"]) + 1,
            "projects": sorted(entries, key=lambda item: str(item.get("project_id") or "")),
        }
        _write(path, payload)
    return {
        "eligible": True,
        "should_remind": should_remind,
        "state": stored_state,
        "classification": classification_status,
    }


def set_user_decision(home: Path, project: Path, tool: str, action: str) -> dict[str, Any]:
    """Persist an explicit reminder decision without changing project files."""

    if action not in {"declined", "dismissed"}:
        raise OnboardingStateError("unsupported onboarding decision")
    try:
        home = validate_unredirected_path(home, label="Tenetora onboarding home")
        project = validate_unredirected_path(project, label="onboarding project")
    except RuntimeError as exc:
        raise OnboardingStateError(str(exc)) from exc
    tool = tool.strip().lower()
    if TOOL_RE.fullmatch(tool) is None or not _is_project_candidate(home, project):
        raise OnboardingStateError("project is not eligible for onboarding decisions")
    classification = classify_project(project)
    derived = _classification_state(str(classification.status))
    if action == "declined" and derived != "unseen":
        raise OnboardingStateError("--decline applies only to an uninitialized project")
    if action == "dismissed" and derived not in {"migration-needed", "review-required"}:
        raise OnboardingStateError("--dismiss applies only to migration-needed or review-required governance")
    identity = project_id(project)
    continuity = repository_continuity_fingerprint(project)
    instance = repository_instance_fingerprint(project)
    path = path_for(home)
    lock = path.with_name(f".{path.name}.lock")
    with locked_file(lock):
        previous = _read(path)
        entries = [
            normalized
            for item in previous["projects"]
            if isinstance(item, dict)
            for normalized in [_normalized_entry(item)]
            if normalized is not None
        ]
        prior = next((item for item in entries if item.get("project_id") == identity), {})
        reminders = _reminders(prior)
        entry: dict[str, Any] = {
            "project_id": identity,
            "path_fingerprint": hashlib.sha256(str(project).encode("utf-8")).hexdigest(),
            **({"continuity_fingerprint": continuity} if continuity else {}),
            **({"instance_fingerprint": instance} if instance else {}),
            "state": action,
            "derived_state": derived,
            "classification": str(classification.status),
            "reminders": reminders,
        }
        entries = [item for item in entries if item.get("project_id") != identity]
        entries.append(entry)
        payload = {
            "version": SCHEMA_VERSION,
            "revision": int(previous["revision"]) + 1,
            "projects": sorted(entries, key=lambda item: str(item.get("project_id") or "")),
        }
        _write(path, payload)
    return {"status": "pass", "action": action, "state": action, "classification": derived}


def parser() -> Any:
    command = argparse.ArgumentParser(description="Record an explicit Tenetora onboarding reminder decision.")
    actions = command.add_mutually_exclusive_group(required=True)
    actions.add_argument("--decline", action="store_true", help="Stop reminders for an uninitialized project.")
    actions.add_argument("--dismiss", action="store_true", help="Stop reminders for unresolved governance data.")
    command.add_argument("--path", default=".", help="Project path.")
    command.add_argument("--tool", default="generic", help="Host tool making the decision.")
    command.add_argument("--home", default=None, help="Managed Tenetora home; defaults to the configured machine home.")
    command.add_argument("--json", action="store_true", help="Print machine-readable output.")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    action = "declined" if args.decline else "dismissed"
    try:
        result = set_user_decision(Path(args.home).expanduser() if args.home else machine_home(), args.path, args.tool, action)
    except OnboardingStateError as error:
        if args.json:
            print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        else:
            print(f"Onboarding decision failed: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"Onboarding decision: {result['action']}")
    return 0


def derive(home: Path) -> dict[str, Any]:
    try:
        home = validate_unredirected_path(home, label="Tenetora onboarding home")
    except RuntimeError as exc:
        raise OnboardingStateError(str(exc)) from exc
    registry = load_registry(home)
    path = path_for(home)
    _assert_state_location(path)
    lock = path.with_name(f".{path.name}.lock")
    with locked_file(lock):
        previous = _read(path)
        prior_entries = [
            normalized
            for item in previous.get("projects", [])
            if isinstance(item, dict)
            for normalized in [_normalized_entry(item)]
            if normalized is not None
        ]
        existing = {
            str(item.get("project_id") or ""): item
            for item in prior_entries
        }
        prior_by_continuity: dict[str, list[dict[str, Any]]] = {}
        for item in prior_entries:
            continuity = str(item.get("continuity_fingerprint") or "")
            if continuity:
                prior_by_continuity.setdefault(continuity, []).append(item)
        registry_projects: list[tuple[dict[str, Any], str]] = []
        for item in registry.get("projects", []):
            if not isinstance(item, dict):
                continue
            raw_path = str(item.get("path") or "")
            registry_projects.append((item, repository_continuity_fingerprint(Path(raw_path))))
        continuity_counts = Counter(continuity for _item, continuity in registry_projects if continuity)
        projects: list[dict[str, Any]] = []
        consumed_prior_ids: set[str] = set()
        for item, continuity in registry_projects:
            project_id = str(item.get("project_id") or "")
            raw_path = str(item.get("path") or "")
            if not project_id or not raw_path:
                continue
            derived = project_state(item)
            prior = existing.get(project_id)
            continuity_matches = prior_by_continuity.get(continuity, []) if continuity else []
            if prior is None and continuity_counts[continuity] == 1 and len(continuity_matches) == 1:
                prior = continuity_matches[0]
            prior = prior or {}
            prior_id = str(prior.get("project_id") or "")
            if prior_id:
                consumed_prior_ids.add(prior_id)
            state = str(prior.get("state") or derived)
            prior_state = str(prior.get("state") or "")
            suppressed = (
                prior_state == "declined" and derived == "unseen"
            ) or (
                prior_state == "dismissed" and derived in {"migration-needed", "review-required"}
            )
            if derived in {"initialized", "migration-needed", "review-required"} and not suppressed:
                state = derived
            if state not in VALID_STATES:
                state = derived
            surfaces = item.get("surfaces") if isinstance(item.get("surfaces"), dict) else {}
            instance = repository_instance_fingerprint(Path(raw_path))
            projects.append(
                {
                    "project_id": project_id,
                    "path_fingerprint": hashlib.sha256(raw_path.encode("utf-8")).hexdigest(),
                    **({"continuity_fingerprint": continuity} if continuity else {}),
                    **({"instance_fingerprint": instance} if instance else {}),
                    "state": state,
                    "derived_state": derived,
                    "tools": sorted(str(tool) for tool in surfaces),
                    "reminders": _reminders(prior),
                    "governance_fingerprint": hashlib.sha256(
                        json.dumps(item.get("governance"), sort_keys=True, separators=(",", ":")).encode("utf-8")
                    ).hexdigest(),
                }
            )
        registry_ids = {entry["project_id"] for entry in projects}
        for prior in prior_entries:
            prior_id = str(prior.get("project_id") or "")
            if (
                not prior_id
                or prior_id in registry_ids
                or prior_id in consumed_prior_ids
                or not _reminders(prior)
            ):
                continue
            projects.append(prior)
        if len(projects) > MAX_PROJECTS:
            raise OnboardingStateError("project onboarding state exceeds its project limit")
        payload = {
            "version": SCHEMA_VERSION,
            "revision": int(previous["revision"]) + 1,
            "projects": sorted(projects, key=lambda entry: entry["project_id"]),
        }
        _write(path, payload)
        return payload
