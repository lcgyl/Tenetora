#!/usr/bin/env python3
"""Record bounded change-impact preflight evidence and inspect staged contracts."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from governance_trail import append_event, sanitize_local_paths  # noqa: E402
import alignment_state  # noqa: E402
from harness_io import atomic_write_text  # noqa: E402
from path_security import harness_missing_message, validate_existing_project_path  # noqa: E402
from worktree_fingerprint import WORKTREE_ENTRIES_VERSION, git_state_entries  # noqa: E402


STATE_REL = ".tenetora/state/change-impact.json"
STATE_VERSION = 1
MAX_PATHS = 200
MAX_TEXT = 1000
MAX_SCOPE_SNAPSHOT_ENTRIES = 1000
MAX_REFLOG_TEXT = 300
KINDS = (
    "constructor",
    "public-api",
    "interface",
    "schema",
    "cli",
    "hook",
    "manifest",
    "runtime",
    "cross-module",
    "rename",
    "behavior",
    "other",
)
HIGH_CONFIDENCE_SECRET = re.compile(
    r"(?:glpat-[A-Za-z0-9._-]{16,}|ghp_[A-Za-z0-9_]{20,}|"
    r"\b(?:TOKEN|SECRET|PASSWORD)\b\s*[:=]\s*['\"]?\S{12,})",
    re.IGNORECASE,
)
WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
CONTRACT_PATH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("hook-contract", re.compile(r"(^|/)(?:hooks?|hook-adapters?)(/|$)|(?:^|/)hooks?[^/]*\.(?:json|ya?ml)$", re.I)),
    ("plugin-manifest", re.compile(r"(?:^|/)(?:plugin|marketplace|agent-harness\.plugin)[^/]*\.json$|(?:^|/)\.\w+-plugin/", re.I)),
    ("schema-contract", re.compile(r"(?:^|/)(?:schemas?|migrations?)(/|$)|(?:openapi|asyncapi|schema)[^/]*\.(?:json|ya?ml|sql|proto)$", re.I)),
    ("cli-contract", re.compile(r"(?:^|/)(?:cli|commands?)(?:/|\.|_)|(?:^|/)install\.sh$", re.I)),
    ("runtime-contract", re.compile(r"(?:^|/)(?:runtime|launchers?)(?:/|\.|_)|(?:^|/)run-hook(?:\.cmd)?$", re.I)),
)
PYTHON_PUBLIC_SIGNATURE = re.compile(r"^(?:async\s+)?def\s+(?!_)[A-Za-z][A-Za-z0-9_]*\s*\(")
PYTHON_CONSTRUCTOR_SIGNATURE = re.compile(r"^def\s+__init__\s*\(")
JVM_PUBLIC_SIGNATURE = re.compile(
    r"^(?:public|protected)\s+(?:(?:static|final|abstract|synchronized|default)\s+)*"
    r"(?:class|interface|record|enum|[A-Za-z_$][\w$<>,.?\[\] ]*)\s+[A-Za-z_$][\w$]*\s*(?:\(|\{|$)"
)
JVM_CONSTRUCTOR_SIGNATURE = re.compile(r"^(?:public|protected)\s+[A-Z][\w$]*\s*\(")
GENERAL_CONSTRUCTOR_SIGNATURE = re.compile(r"^(?:(?:public|protected|private)\s+)?constructor\s*\(")
EXPORTED_SIGNATURE = re.compile(
    r"^export\s+(?:default\s+)?(?:declare\s+)?(?:async\s+)?"
    r"(?:function|class|interface|type|enum|const|let|var)\b"
)
CLI_OPTION_SIGNATURE = re.compile(r"(?:add_argument|add_option|\.option)\s*\(\s*['\"]--[A-Za-z0-9-]+")


def existing_project_path(raw: str) -> Path:
    try:
        return validate_existing_project_path(raw)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(
        prog="tenetora impact",
        description="Record and verify a bounded change-impact preflight for shared contract changes.",
        add_help=False,
    )
    command_parser.add_argument("--help", action="help", help="Show this help message and exit.")
    action = command_parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--status", action="store_true", help="Show the current change-impact state.")
    action.add_argument("--start", action="store_true", help="Record an impact inventory before editing.")
    action.add_argument("--complete", action="store_true", help="Complete the active preflight with rescan and verification evidence.")
    action.add_argument("--cancel", action="store_true", help="Cancel the active or failed preflight with a reason.")
    command_parser.add_argument("-p", "--path", default=".", type=existing_project_path, metavar="<project-dir>")
    command_parser.add_argument("--git-path", type=existing_project_path, metavar="<git-dir>")
    command_parser.add_argument("--kind", choices=KINDS, help="Shared contract category for --start.")
    command_parser.add_argument("--summary", help="Bounded change summary for --start.")
    command_parser.add_argument("--impact-tool", action="append", default=[], help="Structural or fallback impact tool used before editing; repeatable.")
    command_parser.add_argument("--symbol", action="append", default=[], help="Affected symbol or contract; repeatable.")
    command_parser.add_argument("--scope", action="append", default=[], help="Affected module or compatibility scope; repeatable.")
    command_parser.add_argument("--affected-file", action="append", default=[], help="Expected project-relative affected path; repeatable.")
    command_parser.add_argument("--reference-count", type=int, default=0, help="Observed reference count from the impact inventory.")
    command_parser.add_argument("--baseline-command", help="Baseline command run before editing.")
    command_parser.add_argument("--baseline-status", choices=("passed", "failed", "skipped"), help="Observed baseline result.")
    command_parser.add_argument("--baseline-reason", help="Required when the baseline was skipped.")
    command_parser.add_argument("--rescan-command", help="Impact query repeated after editing.")
    command_parser.add_argument("--rescan-status", choices=("passed", "failed"), help="Observed rescan result.")
    command_parser.add_argument("--verification-command", help="Compile, typecheck, or test command run after editing.")
    command_parser.add_argument("--verification-status", choices=("passed", "failed"), help="Observed verification result.")
    command_parser.add_argument("--reason", help="Cancellation reason for --cancel.")
    command_parser.add_argument("--replace", action="store_true", help="Replace an active preflight when starting a newly authorized task.")
    command_parser.add_argument(
        "--expected-preflight-id",
        help="Required with --replace when replacing a valid active preflight; binds authorization to that exact preflight.",
    )
    command_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return command_parser


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def state_path(root: Path) -> Path:
    return root / STATE_REL


def idle_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "status": "pass", "lifecycle_status": "idle", "revision": 0}


def load_state(root: Path) -> dict[str, Any]:
    path = state_path(root)
    if not path.is_file():
        return idle_state()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "version": STATE_VERSION,
            "status": "error",
            "lifecycle_status": "invalid",
            "revision": 0,
            "message": f"Invalid change-impact state: {exc}",
        }
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        return {
            "version": STATE_VERSION,
            "status": "error",
            "lifecycle_status": "invalid",
            "revision": 0,
            "message": "Unsupported change-impact state schema.",
        }
    result = dict(payload)
    result["status"] = "pass"
    revision = result.get("revision", 0)
    if type(revision) is not int or revision < 0:
        return {
            "version": STATE_VERSION,
            "status": "error",
            "lifecycle_status": "invalid",
            "revision": 0,
            "message": "Invalid change-impact state revision.",
        }
    return result


def write_state(root: Path, payload: dict[str, Any], *, allow_invalid: bool = False) -> None:
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    expected_revision = payload.get("revision", 0)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("change-impact state has an invalid revision")
    with alignment_state.state_lock(path):
        current_revision = 0
        if path.is_file():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                if not allow_invalid:
                    raise ValueError("change-impact state changed or became unreadable; retry from status") from exc
                current = {}
            if not isinstance(current, dict) or type(current.get("revision", 0)) is not int:
                if allow_invalid:
                    current = {}
                else:
                    raise ValueError("change-impact state has an invalid revision")
            if not isinstance(current, dict) or type(current.get("revision", 0)) is not int:
                raise ValueError("change-impact state has an invalid revision")
            current_revision = int(current.get("revision", 0))
        if current_revision != expected_revision:
            raise ValueError("change-impact state changed concurrently; reload status before retrying")
        written = dict(payload)
        written["revision"] = current_revision + 1
        atomic_write_text(path, json.dumps(written, ensure_ascii=False, indent=2) + "\n")
        payload.clear()
        payload.update(written)


def run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def git_head(root: Path) -> str:
    result = run_git(root, "rev-parse", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else "unavailable"


def _diagnostic_text(value: str) -> str:
    text = str(sanitize_local_paths(value)).strip()
    if HIGH_CONFIDENCE_SECRET.search(text):
        return "[redacted]"
    return text[:MAX_REFLOG_TEXT]


def head_transition(root: Path, recorded_head: object) -> dict[str, Any]:
    current_head = git_head(root)
    reflog = run_git(root, "reflog", "-1", "--format=%H%x09%gD%x09%gs")
    reflog_payload: dict[str, str] = {"status": "unavailable"}
    if reflog.returncode == 0 and reflog.stdout.strip():
        parts = reflog.stdout.strip().split("\t", 2)
        reflog_payload = {
            "status": "available",
            "head": _diagnostic_text(parts[0]),
            "selector": _diagnostic_text(parts[1] if len(parts) > 1 else ""),
            "summary": _diagnostic_text(parts[2] if len(parts) > 2 else ""),
        }
    recorded = str(recorded_head or "unavailable")
    return {
        "recorded_head": recorded,
        "current_head": current_head,
        "changed": recorded != current_head,
        "reflog": reflog_payload,
    }


def _scope_snapshot_fingerprint(paths: list[str], entries: dict[str, str]) -> str:
    canonical = json.dumps(
        {
            "version": WORKTREE_ENTRIES_VERSION,
            "paths": paths,
            "entries": entries,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8", errors="surrogatepass")).hexdigest()


def affected_scope_snapshot(root: Path, paths: list[str]) -> dict[str, Any] | None:
    normalized = sorted(set(paths))[:MAX_PATHS]
    entries = {} if not normalized else git_state_entries(root, normalized)
    if entries is None or len(entries) > MAX_SCOPE_SNAPSHOT_ENTRIES:
        return None
    return {
        "version": WORKTREE_ENTRIES_VERSION,
        "paths": normalized,
        "entries": entries,
        "fingerprint": _scope_snapshot_fingerprint(normalized, entries),
    }


def scope_snapshot_delta(recorded: dict[str, str], current: dict[str, str]) -> list[str]:
    return sorted(
        path
        for path in set(recorded) | set(current)
        if recorded.get(path) != current.get(path)
    )[:MAX_PATHS]


def repo_key(project_root: Path, git_root: Path) -> str:
    try:
        rel = git_root.resolve().relative_to(project_root.resolve()).as_posix()
        return rel or "."
    except ValueError:
        digest = hashlib.sha256(str(git_root.resolve()).encode("utf-8")).hexdigest()[:16]
        return f"external:{digest}"


def git_changed_paths(root: Path) -> list[str]:
    paths: set[str] = set()
    for args in (
        ("diff", "--name-only", "--diff-filter=ACMRDT"),
        ("diff", "--cached", "--name-only", "--diff-filter=ACMRDT"),
        ("ls-files", "--others", "--exclude-standard"),
    ):
        result = run_git(root, *args)
        if result.returncode == 0:
            paths.update(line.strip() for line in result.stdout.splitlines() if line.strip())
    return sorted(paths)[:MAX_PATHS]


def safe_text(value: str | None, field: str, required: bool = False) -> str:
    text = str(sanitize_local_paths(value or "")).strip()
    if required and not text:
        raise ValueError(f"{field} is required")
    if HIGH_CONFIDENCE_SECRET.search(text):
        raise ValueError(f"{field} may contain a credential; do not store secrets in change-impact state")
    return text[:MAX_TEXT]


def normalize_relative_path(raw: str) -> str:
    value = raw.strip().replace("\\", "/")
    if not value or value.startswith(("/", "~/")) or WINDOWS_ABSOLUTE.match(value):
        raise ValueError(f"affected file must be project-relative: {raw}")
    parts = [part for part in value.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"affected file must stay inside the Git worktree: {raw}")
    return "/".join(parts)


def bounded_strings(values: list[str], field: str) -> list[str]:
    result: list[str] = []
    for value in values:
        item = safe_text(value, field, required=True)
        if item not in result:
            result.append(item)
        if len(result) >= MAX_PATHS:
            break
    return result


def bounded_paths(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        item = normalize_relative_path(value)
        if item not in result:
            result.append(item)
        if len(result) >= MAX_PATHS:
            break
    return result


def error_payload(message: str, lifecycle_status: str = "error") -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "status": "error",
        "lifecycle_status": lifecycle_status,
        "message": message,
    }


def record_event(root: Path, action: str, payload: dict[str, Any]) -> None:
    append_event(
        root,
        {
            "type": "change-impact-preflight",
            "action": action,
            "status": "pass" if payload.get("lifecycle_status") in {"active", "completed", "cancelled"} else "fail",
            "lifecycle_status": payload.get("lifecycle_status"),
            "preflight_id": payload.get("preflight_id"),
            "kind": payload.get("kind"),
            "repo": payload.get("repo"),
            "reference_count": payload.get("reference_count", 0),
            "affected_file_count": len(payload.get("affected_files", [])),
            "observed_path_count": len(payload.get("observed_paths", [])),
            "first_verification_pass": payload.get("first_verification_pass"),
            "source": "tenetora impact",
        },
    )


def start_preflight(root: Path, git_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    current = load_state(root)
    if current.get("status") == "error" and not args.replace:
        return current
    current_lifecycle = current.get("lifecycle_status")
    if args.expected_preflight_id and not args.replace:
        return error_payload("--expected-preflight-id requires --replace")
    if current_lifecycle == "active":
        if not args.replace:
            return error_payload("An active change-impact preflight already exists; complete, cancel, or explicitly replace it.", "active")
        current_preflight_id = current.get("preflight_id")
        expected_preflight_id = args.expected_preflight_id
        if not isinstance(current_preflight_id, str) or not expected_preflight_id:
            return error_payload(
                "Replacing an active change-impact preflight requires --expected-preflight-id from the latest status.",
                "conflict",
            )
        if not secrets.compare_digest(current_preflight_id, expected_preflight_id):
            return error_payload(
                "The active change-impact preflight changed; reload status before replacing it.",
                "conflict",
            )
    if args.kind is None:
        return error_payload("--kind is required with --start")
    if args.baseline_status is None:
        return error_payload("--baseline-status is required with --start")
    if not args.impact_tool:
        return error_payload("At least one --impact-tool is required with --start")
    if args.reference_count < 0:
        return error_payload("--reference-count must be zero or greater")
    if args.baseline_status in {"passed", "failed"} and not args.baseline_command:
        return error_payload("--baseline-command is required when the baseline ran")
    if args.baseline_status == "skipped" and not args.baseline_reason:
        return error_payload("--baseline-reason is required when the baseline is skipped")
    try:
        summary = safe_text(args.summary, "summary", required=True)
        impact_tools = bounded_strings(args.impact_tool, "impact tool")
        symbols = bounded_strings(args.symbol, "symbol")
        scopes = bounded_strings(args.scope, "scope")
        affected_files = bounded_paths(args.affected_file)
        baseline_command = safe_text(args.baseline_command, "baseline command")
        baseline_reason = safe_text(args.baseline_reason, "baseline reason")
    except ValueError as exc:
        return error_payload(str(exc))
    if not (symbols or scopes or affected_files):
        return error_payload("Record at least one --symbol, --scope, or --affected-file before editing")
    payload: dict[str, Any] = {
        "version": STATE_VERSION,
        "revision": int(current.get("revision", 0)),
        "preflight_id": f"ah-impact-{secrets.token_hex(16)}",
        "lifecycle_status": "active",
        "kind": args.kind,
        "summary": summary,
        "impact_tools": impact_tools,
        "symbols": symbols,
        "scopes": scopes,
        "affected_files": affected_files,
        "reference_count": args.reference_count,
        "baseline_command": baseline_command,
        "baseline_status": args.baseline_status,
        "baseline_reason": baseline_reason,
        "baseline_dirty_paths": git_changed_paths(git_root),
        "observed_paths": [],
        "repo": repo_key(root, git_root),
        "base_head": git_head(git_root),
        "started_at": now_iso(),
    }
    write_state(root, payload, allow_invalid=current.get("status") == "error" and args.replace)
    record_event(root, "start", payload)
    return {"status": "pass", **payload}


def complete_preflight(root: Path, git_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    current = load_state(root)
    if current.get("status") == "error":
        return current
    if current.get("lifecycle_status") not in {"active", "failed"}:
        return error_payload("No active or failed change-impact preflight is available to complete.", str(current.get("lifecycle_status")))
    if args.rescan_status is None or not args.rescan_command:
        return error_payload("--rescan-command and --rescan-status are required with --complete")
    if args.verification_status is None or not args.verification_command:
        return error_payload("--verification-command and --verification-status are required with --complete")
    expected_repo = repo_key(root, git_root)
    if current.get("repo") != expected_repo:
        return error_payload("The active preflight belongs to a different Git worktree.", "stale")
    transition = head_transition(git_root, current.get("base_head"))
    if transition["changed"]:
        return {
            **error_payload("Git HEAD changed after the preflight started; start a new impact analysis.", "stale"),
            "head_transition": transition,
        }
    try:
        rescan_command = safe_text(args.rescan_command, "rescan command", required=True)
        verification_command = safe_text(args.verification_command, "verification command", required=True)
    except ValueError as exc:
        return error_payload(str(exc))
    baseline_dirty = set(str(item) for item in current.get("baseline_dirty_paths", []) if isinstance(item, str))
    observed = sorted(set(git_changed_paths(git_root)) - baseline_dirty)[:MAX_PATHS]
    affected = sorted(
        set(str(item) for item in current.get("affected_files", []) if isinstance(item, str)) | set(observed)
    )[:MAX_PATHS]
    completed = args.rescan_status == "passed" and args.verification_status == "passed"
    snapshot = affected_scope_snapshot(git_root, affected)
    if completed and snapshot is None:
        return error_payload(
            "The affected-path content snapshot could not be recorded safely; retry the impact analysis.",
            "stale",
        )
    payload = {
        key: value
        for key, value in current.items()
        if key not in {"status", "message"}
    }
    payload.update(
        {
            "lifecycle_status": "completed" if completed else "failed",
            "affected_files": affected,
            "observed_paths": observed,
            "rescan_command": rescan_command,
            "rescan_status": args.rescan_status,
            "verification_command": verification_command,
            "verification_status": args.verification_status,
            "first_verification_pass": completed,
            "affected_scope_snapshot": snapshot if completed else None,
            "completed_at": now_iso() if completed else None,
            "last_attempt_at": now_iso(),
        }
    )
    write_state(root, payload)
    record_event(root, "complete", payload)
    return {"status": "pass" if completed else "fail", **payload}


def cancel_preflight(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    current = load_state(root)
    if current.get("status") == "error":
        return current
    if current.get("lifecycle_status") not in {"active", "failed"}:
        return error_payload("No active or failed change-impact preflight is available to cancel.", str(current.get("lifecycle_status")))
    try:
        reason = safe_text(args.reason, "reason", required=True)
    except ValueError as exc:
        return error_payload(str(exc))
    payload = {key: value for key, value in current.items() if key not in {"status", "message"}}
    payload.update({"lifecycle_status": "cancelled", "cancel_reason": reason, "cancelled_at": now_iso()})
    write_state(root, payload)
    record_event(root, "cancel", payload)
    return {"status": "pass", **payload}


def path_contract_reasons(path: str) -> list[str]:
    return [reason for reason, pattern in CONTRACT_PATH_PATTERNS if pattern.search(path)]


def signature_reason(line: str) -> str | None:
    stripped = line.strip()
    if (
        PYTHON_CONSTRUCTOR_SIGNATURE.match(stripped)
        or JVM_CONSTRUCTOR_SIGNATURE.match(stripped)
        or GENERAL_CONSTRUCTOR_SIGNATURE.match(stripped)
    ):
        return "constructor-signature"
    if PYTHON_PUBLIC_SIGNATURE.match(stripped):
        return "public-signature"
    if JVM_PUBLIC_SIGNATURE.match(stripped):
        return "public-signature"
    if EXPORTED_SIGNATURE.match(stripped):
        return "exported-symbol"
    if CLI_OPTION_SIGNATURE.search(stripped):
        return "cli-contract"
    if stripped.startswith("#!") and ("sh" in stripped or "python" in stripped):
        return "runtime-launcher"
    return None


def detect_staged_contract_change(git_root: Path, staged: list[str] | None = None) -> dict[str, Any]:
    paths = staged if staged is not None else []
    if staged is None:
        result = run_git(git_root, "diff", "--cached", "--name-only", "--diff-filter=ACMRDT")
        if result.returncode == 0:
            paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    reasons_by_path: dict[str, set[str]] = {}
    for path in paths:
        reasons = path_contract_reasons(path)
        if reasons:
            reasons_by_path.setdefault(path, set()).update(reasons)

    patch = run_git(git_root, "diff", "--cached", "--no-ext-diff", "--unified=0", "--no-color")
    current_path = ""
    if patch.returncode == 0:
        for line in patch.stdout.splitlines():
            if line.startswith("--- a/"):
                current_path = line[6:]
                continue
            if line.startswith("+++ b/"):
                current_path = line[6:]
                continue
            if not current_path or not line.startswith(("+", "-")) or line.startswith(("+++", "---")):
                continue
            reason = signature_reason(line[1:])
            if reason:
                reasons_by_path.setdefault(current_path, set()).add(reason)

    candidate_paths = sorted(reasons_by_path)
    reasons = sorted({reason for values in reasons_by_path.values() for reason in values})
    return {
        "status": "suspected" if candidate_paths else "none",
        "confidence": "heuristic",
        "candidate_paths": candidate_paths,
        "reasons": reasons,
        "reasons_by_path": {path: sorted(values) for path, values in sorted(reasons_by_path.items())},
    }


def commit_preflight_check(project_root: Path, git_root: Path, staged: list[str]) -> dict[str, Any]:
    detected = detect_staged_contract_change(git_root, staged)
    base = {
        "name": "change-impact-preflight",
        "confidence": "heuristic",
        "candidate_paths": detected["candidate_paths"],
        "reasons": detected["reasons"],
    }
    if detected["status"] == "none":
        return {
            **base,
            "status": "skip",
            "code": "no-shared-contract-change-detected",
            "message": "No high-signal shared contract change was detected in the staged diff.",
        }
    state = load_state(project_root)
    lifecycle = str(state.get("lifecycle_status") or "idle")
    if lifecycle == "idle":
        return {
            **base,
            "status": "warn",
            "code": "change-impact-preflight-missing",
            "message": "The staged diff may change a shared contract, but no change-impact preflight is recorded.",
        }
    if state.get("status") == "error":
        return {
            **base,
            "status": "warn",
            "code": "change-impact-preflight-invalid",
            "message": "The staged diff may change a shared contract, but the change-impact state is invalid.",
        }
    if lifecycle != "completed":
        return {
            **base,
            "status": "warn",
            "code": "change-impact-preflight-incomplete",
            "message": f"The staged diff may change a shared contract, but the preflight is {lifecycle}.",
        }
    if state.get("repo") != repo_key(project_root, git_root):
        return {
            **base,
            "status": "warn",
            "code": "change-impact-preflight-repo-mismatch",
            "message": "The completed preflight belongs to a different Git worktree.",
        }
    covered = {
        str(item)
        for key in ("affected_files", "observed_paths")
        for item in state.get(key, [])
        if isinstance(item, str)
    }
    missing = sorted(set(detected["candidate_paths"]) - covered)
    if missing:
        return {
            **base,
            "status": "warn",
            "code": "change-impact-preflight-scope-mismatch",
            "message": "The completed preflight does not cover every suspected shared-contract path.",
            "uncovered_paths": missing,
        }
    transition = head_transition(git_root, state.get("base_head"))
    recorded_snapshot = state.get("affected_scope_snapshot")
    if isinstance(recorded_snapshot, dict):
        version = recorded_snapshot.get("version")
        paths = recorded_snapshot.get("paths")
        entries = recorded_snapshot.get("entries")
        fingerprint = recorded_snapshot.get("fingerprint")
        snapshot_valid = (
            version == WORKTREE_ENTRIES_VERSION
            and isinstance(paths, list)
            and all(isinstance(path, str) for path in paths)
            and isinstance(entries, dict)
            and all(isinstance(path, str) and isinstance(value, str) for path, value in entries.items())
            and isinstance(fingerprint, str)
            and secrets.compare_digest(
                fingerprint,
                _scope_snapshot_fingerprint(paths, entries),
            )
        )
        current_snapshot = affected_scope_snapshot(git_root, paths) if snapshot_valid else None
        if current_snapshot is None:
            return {
                **base,
                "status": "warn",
                "code": "change-impact-preflight-stale",
                "message": "The completed preflight has an invalid or unreadable affected-path snapshot.",
                "head_transition": transition,
            }
        current_entries = current_snapshot["entries"]
        if not secrets.compare_digest(fingerprint, current_snapshot["fingerprint"]):
            return {
                **base,
                "status": "warn",
                "code": "change-impact-preflight-stale",
                "message": "Affected paths changed after the completed preflight; rerun impact analysis.",
                "scope_delta_paths": scope_snapshot_delta(entries, current_entries),
                "recorded_scope_fingerprint": fingerprint,
                "current_scope_fingerprint": current_snapshot["fingerprint"],
                "head_transition": transition,
            }
    elif transition["changed"]:
        return {
            **base,
            "status": "warn",
            "code": "change-impact-preflight-stale",
            "message": (
                "Git HEAD changed after a legacy completed preflight without an affected-path snapshot; "
                "rerun impact analysis."
            ),
            "head_transition": transition,
        }
    return {
        **base,
        "status": "pass",
        "code": "change-impact-preflight-matched",
        "message": "The staged shared-contract candidates are covered by a completed change-impact preflight.",
        "preflight_id": state.get("preflight_id"),
        "scope_fingerprint": (
            recorded_snapshot.get("fingerprint") if isinstance(recorded_snapshot, dict) else None
        ),
        "head_transition": transition,
    }


def print_text(payload: dict[str, Any]) -> None:
    chinese = os.environ.get("TENETORA_LANG", "").strip().lower() in {"zh", "zh-cn", "zh-hans", "chinese", "中文"}
    print(f"tenetora impact：{payload.get('status', 'unknown')}" if chinese else f"tenetora impact: {payload.get('status', 'unknown')}")
    print(f"- {'生命周期' if chinese else 'lifecycle'}: {payload.get('lifecycle_status', 'unknown')}")
    if payload.get("preflight_id"):
        print(f"- {'预检' if chinese else 'preflight'}: {payload['preflight_id']}")
    if payload.get("kind"):
        print(f"- {'类型' if chinese else 'kind'}: {payload['kind']}")
    if payload.get("message"):
        print(f"- {'消息' if chinese else 'message'}: {payload['message']}")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root: Path = args.path
    if not (root / ".tenetora").is_dir():
        payload = error_payload(
            harness_missing_message(root)
        )
        exit_code = 2
    else:
        git_root: Path = args.git_path or root
        try:
            if args.status:
                payload = load_state(root)
            elif args.start:
                payload = start_preflight(root, git_root, args)
            elif args.complete:
                payload = complete_preflight(root, git_root, args)
            else:
                payload = cancel_preflight(root, args)
        except ValueError as exc:
            payload = error_payload(str(exc), "conflict")
        if payload.get("status") == "pass":
            exit_code = 0
        elif payload.get("status") == "fail":
            exit_code = 1
        else:
            exit_code = 2
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_text(payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
