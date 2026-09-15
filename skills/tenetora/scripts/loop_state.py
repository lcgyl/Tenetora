#!/usr/bin/env python3
"""Read or update bounded loop state for Tenetora."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import re
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from harness_io import atomic_write_text
import alignment_state
from path_security import harness_missing_message, validate_existing_project_path


LOOP_STATE_REL = ".tenetora/state/loop-state.json"
DELEGATION_STATE_REL = ".tenetora/state/delegation-state.json"
SUBAGENT_REPORT_DIR_REL = ".tenetora/.cache/subagents"
SUBAGENT_REPORT_ARCHIVE_REL = ".tenetora/.cache/subagents/archive"
SUBAGENT_REPORT_RETENTION_DAYS = 7
CURRENT_SCHEMA_VERSION = 3
ACTIVE_LOOP_STATUSES = {"active", "blocked"}
DEFAULT_NEXT_PROMPT = "use tenetora-loop 继续"
APPROVAL_BOUNDARIES = [
    "commits",
    "pushes",
    "destructive commands",
    "network access requiring approval",
    "secret-bearing files",
]
STOP_CONDITIONS = [
    "goal-met",
    "requires-user-approval",
    "same-blocker-repeated",
    "verification-passing-no-next-step",
    "requires-guessing-intent-or-credentials",
    "unrelated-broad-refactor",
]
ALIGNMENT_PROOF_RE = re.compile(r"^ah-align-[0-9a-f]{32}$")


def default_review_cycle() -> dict[str, object]:
    return {
        "active": False,
        "trigger": "",
        "round": 0,
        "max_review_rounds": 3,
        "fix_rounds": 0,
        "max_fix_rounds": 2,
        "status": "idle",
        "active_dispatch_id": None,
        "reports": [],
        "independent_review": "not-requested",
        "goal_hash": None,
        "started_at": None,
        "completed_at": None,
        "subject_fingerprint": None,
        "subject_kind": "unbound",
        "subject_git_path": None,
        "review_scope_bound": False,
        "review_scope": None,
        "review_scope_entries": None,
        "review_scope_fingerprint": None,
        "review_subject_entries": None,
        "review_subject_entries_fingerprint": None,
        "rebind_from_dispatch_id": None,
        "review_delta_paths": [],
        "resolution_mode": "unspecified",
        "isolation_level": "unknown",
        "contract_status": "unknown",
    }


def existing_project_path(raw: str) -> Path:
    try:
        return validate_existing_project_path(raw)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def default_state() -> dict[str, object]:
    return {
        "version": CURRENT_SCHEMA_VERSION,
        "revision": 0,
        "session_id": "",
        "owner_id": "",
        "conversation_id": "",
        "tool": "unknown",
        "status": "idle",
        "current_goal": "",
        "exit_criteria": [],
        "latest_step": "",
        "latest_verification": "",
        "next_loop_prompt": DEFAULT_NEXT_PROMPT,
        "last_blocker": "",
        "same_blocker_count": 0,
        "updated_at": None,
        "policy": {
            "auto_trigger": "user-message-or-tool-routing-only",
            "background_execution": False,
            "approval_boundaries": APPROVAL_BOUNDARIES,
            "stop_conditions": STOP_CONDITIONS,
        },
        "notes": [],
        "alignment": {
            "alignment_id": "",
            "status": "",
            "goal_fingerprint": "",
            "handoff_hash": "",
            "handoff_relative_path": None,
            "accepted_risks": [],
            "proof": "",
        },
        "review_cycle": default_review_cycle(),
        "transitions": [],
    }


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(
        prog="tenetora loop-state",
        description="Read or update .tenetora bounded loop state.",
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
    command_parser.add_argument("--write", action="store_true", help="Write loop state updates.")
    command_parser.add_argument("--session-id", help="Stable conversation/session identity for loop state.")
    command_parser.add_argument("--owner-id", help="Owner identity for loop state mutations.")
    command_parser.add_argument("--conversation-id", help="Host conversation identity.")
    command_parser.add_argument("--tool", help="Host tool identity (for diagnostics).")
    command_parser.add_argument(
        "--adopt-legacy",
        action="store_true",
        help="Explicitly adopt an unowned legacy active loop state; requires session and owner identities.",
    )
    command_parser.add_argument("--reset", action="store_true", help="Reset loop state to idle defaults. Requires --write.")
    command_parser.add_argument(
        "--status",
        choices=("idle", "active", "blocked", "done"),
        help="Loop state status to write.",
    )
    command_parser.add_argument("--goal", help="Current bounded-loop goal.")
    command_parser.add_argument(
        "--exit-criteria",
        action="append",
        default=[],
        help="Exit criterion. Can be passed multiple times.",
    )
    command_parser.add_argument("--latest-step", help="Latest executed loop step.")
    command_parser.add_argument("--latest-verification", help="Latest verification command or result.")
    command_parser.add_argument("--next-prompt", help="Prompt users or tools can use to resume the bounded loop.")
    command_parser.add_argument("--blocker", help="Current blocker. Repeating the same blocker increments same_blocker_count.")
    command_parser.add_argument("--clear-blocker", action="store_true", help="Clear blocker fields.")
    command_parser.add_argument("--note", action="append", default=[], help="Append a short note. Can be passed multiple times.")
    command_parser.add_argument(
        "--alignment-proof",
        help="Consume a passed goal-matching alignment guard proof into loop state. Requires --write.",
    )
    return command_parser


def state_path(root: Path) -> Path:
    return root / LOOP_STATE_REL


def load_state(root: Path) -> dict[str, object]:
    path = state_path(root)
    if not path.is_file():
        return default_state()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: loop-state.json unreadable ({exc}), reset to defaults", file=sys.stderr)
        return default_state()
    if not isinstance(payload, dict):
        print("warning: loop-state.json is not an object, reset to defaults", file=sys.stderr)
        return default_state()
    payload = migrate_schema(payload)
    state = default_state()
    state.update(payload)
    review = state.get("review_cycle")
    normalized_review = default_review_cycle()
    if isinstance(review, dict):
        normalized_review.update(review)
    state["review_cycle"] = normalized_review
    return state


def requested_identity(args: argparse.Namespace) -> dict[str, str]:
    session_id = str(
        getattr(args, "session_id", "")
        or os.environ.get("TENETORA_SESSION_ID", "")
        or os.environ.get("TENETORA_ALIGNMENT_SESSION_ID", "")
    ).strip()
    owner_id = str(
        getattr(args, "owner_id", "")
        or os.environ.get("TENETORA_OWNER_ID", "")
        or os.environ.get("TENETORA_ALIGNMENT_OWNER_ID", "")
    ).strip()
    conversation_id = str(
        getattr(args, "conversation_id", "") or os.environ.get("TENETORA_CONVERSATION_ID", "")
    ).strip()
    tool = str(getattr(args, "tool", "") or os.environ.get("TENETORA_TOOL", "unknown")).strip() or "unknown"
    if session_id:
        alignment_state.validate_identity(session_id, "session_id", alignment_state.SESSION_ID_RE)
    if owner_id:
        alignment_state.validate_identity(owner_id, "owner_id", alignment_state.OWNER_ID_RE)
    if conversation_id:
        alignment_state.validate_identity(conversation_id, "conversation_id", alignment_state.OWNER_ID_RE)
    alignment_state.validate_identity(tool, "tool", alignment_state.OWNER_ID_RE)
    return {
        "session_id": session_id,
        "owner_id": owner_id,
        "conversation_id": conversation_id or owner_id,
        "tool": tool,
    }


def identity_matches(state: dict[str, object], identity: dict[str, str]) -> bool:
    stored_session = str(state.get("session_id") or "")
    stored_owner = str(state.get("owner_id") or "")
    return bool(
        identity.get("session_id")
        and identity.get("owner_id")
        and stored_session == identity["session_id"]
        and stored_owner == identity["owner_id"]
    )


def safe_state_view(state: dict[str, object], args: argparse.Namespace) -> dict[str, object]:
    """Hide another conversation's loop goal instead of treating it as current state."""
    if not str(state.get("current_goal") or ""):
        return state
    identity = requested_identity(args)
    if identity_matches(state, identity):
        return state
    goal = str(state.get("current_goal") or "")
    view = default_state()
    view.update(
        {
            "status": "conflict",
            "revision": state.get("revision", 0),
            "updated_at": state.get("updated_at"),
            "session_id": state.get("session_id", ""),
            "owner_id": state.get("owner_id") or "unowned",
            "conversation_id": state.get("conversation_id", ""),
            "tool": state.get("tool", "unknown"),
            "active_state_status": state.get("status"),
            "identity_required": True,
            "goal_fingerprint": hashlib.sha256(goal.encode("utf-8")).hexdigest()[:16],
            "next_loop_prompt": "select an owner-bound loop session before continuing",
        }
    )
    return view


def migrate_schema(payload: dict[str, object]) -> dict[str, object]:
    """Migrate older loop-state payloads without losing useful state."""
    version = payload.get("version", 1)
    if not isinstance(version, int):
        print("warning: loop-state.json has invalid version, reset version metadata", file=sys.stderr)
        version = 1
    migrated = dict(payload)
    if version <= 1:
        transitions = migrated.get("transitions")
        if not isinstance(transitions, list):
            migrated["transitions"] = []
        migrated["version"] = 2
        version = 2
    if version == 2:
        migrated["review_cycle"] = default_review_cycle()
        migrated["version"] = 3
        version = 3
    migrated.setdefault("revision", 0)
    migrated.setdefault("session_id", "")
    migrated.setdefault("owner_id", "")
    migrated.setdefault("conversation_id", "")
    migrated.setdefault("tool", "unknown")
    if version > CURRENT_SCHEMA_VERSION:
        print(
            f"warning: loop-state.json version {version} is newer than supported {CURRENT_SCHEMA_VERSION}; "
            "loading known fields only",
            file=sys.stderr,
        )
        migrated["version"] = CURRENT_SCHEMA_VERSION
    return migrated


def write_state(root: Path, state: dict[str, object]) -> None:
    harness = root / ".tenetora"
    if not harness.is_dir():
        raise FileNotFoundError(
            harness_missing_message(root)
        )
    path = state_path(root)
    expected_revision = state.get("revision", 0)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("loop state has an invalid revision")
    with alignment_state.state_lock(path):
        current_revision = 0
        if path.is_file():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("loop state changed or became unreadable; retry from status") from exc
            if not isinstance(current, dict) or type(current.get("revision", 0)) is not int:
                raise ValueError("loop state has an invalid revision")
            current_revision = int(current.get("revision", 0))
        if current_revision != expected_revision:
            raise ValueError("loop state changed concurrently; reload status before retrying")
        written = dict(state)
        written["revision"] = current_revision + 1
        atomic_write_text(path, json.dumps(written, ensure_ascii=False, indent=2) + "\n")
        state.clear()
        state.update(written)


def latest_alignment_guard(root: Path, proof: str) -> dict[str, object] | None:
    path = root / ".tenetora" / "state" / "governance-trail.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    events = payload.get("events")
    if not isinstance(events, list):
        return None
    for event in reversed(events):
        if (
            isinstance(event, dict)
            and event.get("type") == "alignment-guard"
            and event.get("status") == "pass"
            and event.get("alignment_proof") == proof
        ):
            return event
    return None


def consume_alignment(root: Path, state: dict[str, object], proof: str) -> None:
    if not ALIGNMENT_PROOF_RE.fullmatch(proof):
        raise ValueError("alignment proof has an invalid format")
    event = latest_alignment_guard(root, proof)
    if event is None:
        raise ValueError("alignment proof is not present in a passed governance event")
    alignment_id = str(event.get("alignment_id", ""))
    if not alignment_id:
        raise ValueError("alignment proof event does not identify its alignment session")
    try:
        current = alignment_state.load_handoff(root, alignment_id)
    except alignment_state.AlignmentStateError as exc:
        raise ValueError(f"alignment proof cannot be consumed: {exc}") from exc
    if current is None:
        raise ValueError("alignment proof does not have a matching terminal handoff")
    state_session = str(state.get("session_id") or "")
    state_owner = str(state.get("owner_id") or "")
    event_session = str(event.get("session_id") or "")
    event_owner = str(event.get("owner_id") or "")
    if not state_session or not state_owner:
        raise ValueError("alignment proof consumption requires an owner-bound loop session")
    if event_session != state_session or event_owner != state_owner:
        raise ValueError("alignment proof belongs to another loop session")
    if (current.get("session_id") or current.get("alignment_id")) != state_session:
        raise ValueError("alignment proof belongs to another alignment session")
    if current.get("owner_id") != state_owner:
        raise ValueError("alignment proof belongs to another loop owner")
    if state.get("conversation_id") and current.get("conversation_id") != state.get("conversation_id"):
        raise ValueError("alignment proof belongs to another conversation")
    fingerprint = str(current.get("goal_fingerprint", ""))
    handoff_hash = str(current.get("handoff_hash", ""))
    if event.get("goal_fingerprint") != fingerprint or event.get("handoff_hash") != handoff_hash:
        raise ValueError("alignment proof is stale and does not match the current handoff")
    goal = str(state.get("current_goal", ""))
    current_acceptance = list(current.get("acceptance_criteria", []))
    loop_acceptance = state.get("exit_criteria", [])
    if not isinstance(loop_acceptance, list):
        loop_acceptance = []
    if not loop_acceptance:
        loop_acceptance = current_acceptance
        state["exit_criteria"] = current_acceptance
    actual = alignment_state.goal_fingerprint(
        goal,
        list(current.get("scope", [])),
        list(current.get("non_goals", [])),
        [str(item) for item in loop_acceptance],
    )
    if alignment_state.is_stale(fingerprint, actual):
        raise ValueError("alignment proof does not match the current loop goal")
    state["alignment"] = {
        "alignment_id": current.get("alignment_id", ""),
        "status": current.get("status", ""),
        "goal_fingerprint": fingerprint,
        "handoff_hash": handoff_hash,
        "handoff_relative_path": current.get("handoff_relative_path"),
        "accepted_risks": current.get("accepted_risks", []),
        "proof": proof,
    }


def apply_updates(root: Path, state: dict[str, object], args: argparse.Namespace) -> dict[str, object]:
    identity = requested_identity(args)
    previous_status = str(state.get("status", "idle"))
    previous_goal = str(state.get("current_goal", ""))
    if (state.get("session_id") or state.get("owner_id")) and not identity_matches(state, identity):
        raise ValueError(
            "loop state belongs to another or unknown conversation; pass matching --session-id/--owner-id"
        )
    if previous_status in ACTIVE_LOOP_STATUSES and previous_goal:
        if not identity_matches(state, identity):
            if not args.adopt_legacy or state.get("owner_id") or not identity["session_id"] or not identity["owner_id"]:
                raise ValueError(
                    "active loop state belongs to another or unknown conversation; pass matching --session-id/--owner-id, "
                    "or use --adopt-legacy with explicit identities for an unowned legacy state"
                )
            state["session_id"] = identity["session_id"]
            state["owner_id"] = identity["owner_id"]
            state["conversation_id"] = identity["conversation_id"]
            state["tool"] = identity["tool"]
    requested_status = args.status or previous_status
    if requested_status in ACTIVE_LOOP_STATUSES and (not identity["session_id"] or not identity["owner_id"]):
        raise ValueError("active loop state requires --session-id and --owner-id (or TENETORA_SESSION_ID/TENETORA_OWNER_ID)")
    if requested_status in ACTIVE_LOOP_STATUSES and not state.get("owner_id"):
        state["session_id"] = identity["session_id"]
        state["owner_id"] = identity["owner_id"]
        state["conversation_id"] = identity["conversation_id"]
        state["tool"] = identity["tool"]
    previous_exit_criteria = state.get("exit_criteria", [])
    if not isinstance(previous_exit_criteria, list):
        previous_exit_criteria = []
    if args.reset:
        expected_revision = state.get("revision", 0)
        state = default_state()
        state["revision"] = expected_revision
    if args.status:
        state["status"] = args.status
    if args.goal is not None:
        state["current_goal"] = args.goal
    if args.exit_criteria:
        state["exit_criteria"] = args.exit_criteria
    if args.latest_step is not None:
        state["latest_step"] = args.latest_step
    if args.latest_verification is not None:
        state["latest_verification"] = args.latest_verification
    if args.next_prompt is not None:
        state["next_loop_prompt"] = args.next_prompt
    if args.clear_blocker:
        state["last_blocker"] = ""
        state["same_blocker_count"] = 0
    if args.blocker is not None:
        previous = str(state.get("last_blocker", ""))
        state["last_blocker"] = args.blocker
        state["same_blocker_count"] = int(state.get("same_blocker_count", 0)) + 1 if previous == args.blocker else 1
    if args.note:
        notes = state.get("notes", [])
        if not isinstance(notes, list):
            notes = []
        notes.extend(args.note)
        state["notes"] = notes
    current_goal = str(state.get("current_goal", ""))
    current_exit_criteria = state.get("exit_criteria", [])
    if not isinstance(current_exit_criteria, list):
        current_exit_criteria = []
    context_changed = (
        alignment_state.normalize_text(previous_goal) != alignment_state.normalize_text(current_goal)
        or alignment_state.normalize_items([str(item) for item in previous_exit_criteria])
        != alignment_state.normalize_items([str(item) for item in current_exit_criteria])
    )
    if context_changed and not args.alignment_proof:
        state["alignment"] = default_state()["alignment"]
    current_status = str(state.get("status", "idle"))
    if context_changed or current_status in {"idle", "done"}:
        archive_goal_reports(root, state, previous_goal)
        state["review_cycle"] = default_review_cycle()
    if args.alignment_proof:
        consume_alignment(root, state, args.alignment_proof)
    updated_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    state["updated_at"] = updated_at
    current_status = str(state.get("status", "idle"))
    if current_status != previous_status:
        transitions = state.get("transitions", [])
        if not isinstance(transitions, list):
            transitions = []
        transitions.append(
            {
                "at": updated_at,
                "from": previous_status,
                "to": current_status,
                "goal": str(state.get("current_goal", "")),
                "latest_step": str(state.get("latest_step", "")),
                "latest_verification": str(state.get("latest_verification", "")),
                "blocker": str(state.get("last_blocker", "")),
            }
        )
        state["transitions"] = transitions[-100:]
    return state


def archive_report(root: Path, raw: str) -> str | None:
    active_dir = (root / SUBAGENT_REPORT_DIR_REL).resolve()
    unresolved = root / raw
    if unresolved.is_symlink():
        return None
    path = unresolved.resolve()
    try:
        path.relative_to(active_dir)
    except ValueError:
        return None
    if path.parent != active_dir or not path.is_file():
        return None
    archive_dir = root / SUBAGENT_REPORT_ARCHIVE_REL
    archive_dir.mkdir(parents=True, exist_ok=True)
    destination = archive_dir / path.name
    path.replace(destination)
    destination.touch()
    return destination.relative_to(root).as_posix()


def archive_goal_reports(root: Path, state: dict[str, object], previous_goal: str) -> None:
    review = state.get("review_cycle", {})
    report_paths = {
        raw
        for raw in (review.get("reports", []) if isinstance(review, dict) else [])
        if isinstance(raw, str)
    }
    delegation_path = root / DELEGATION_STATE_REL
    archived: dict[str, str] = {}
    with alignment_state.state_lock(delegation_path):
        delegation: dict[str, object] | None = None
        if delegation_path.is_file():
            try:
                loaded = json.loads(delegation_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = None
            if isinstance(loaded, dict) and isinstance(loaded.get("dispatches"), list):
                delegation = loaded
                expected_goal_hash = hashlib.sha256(previous_goal.encode("utf-8")).hexdigest()[:16]
                for item in loaded["dispatches"]:
                    if not isinstance(item, dict) or item.get("goal_hash") != expected_goal_hash:
                        continue
                    raw = item.get("report_path")
                    if isinstance(raw, str):
                        report_paths.add(raw)

        for raw in sorted(report_paths):
            target = archive_report(root, raw)
            if target:
                archived[raw] = target

        if delegation is not None and archived:
            for item in delegation.get("dispatches", []):
                if isinstance(item, dict) and item.get("report_path") in archived:
                    item["report_path"] = archived[str(item["report_path"])]
            current_revision = delegation.get("revision", 0)
            if type(current_revision) is int and current_revision >= 0:
                delegation["revision"] = current_revision + 1
                atomic_write_text(delegation_path, json.dumps(delegation, ensure_ascii=False, indent=2) + "\n")
    prune_report_archive(root)


def prune_report_archive(root: Path) -> None:
    archive_dir = root / SUBAGENT_REPORT_ARCHIVE_REL
    if not archive_dir.is_dir():
        return
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=SUBAGENT_REPORT_RETENTION_DAYS)).timestamp()
    for path in archive_dir.glob("*.md"):
        if path.is_symlink() or not path.is_file():
            continue
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)


def print_markdown(state: dict[str, object]) -> None:
    chinese = os.environ.get("TENETORA_LANG", "").strip().lower() in {"zh", "zh-cn", "zh-hans", "chinese", "中文"}
    empty = "无" if chinese else "None"
    next_prompt = state.get("next_loop_prompt") or DEFAULT_NEXT_PROMPT
    if chinese and next_prompt == "select an owner-bound loop session before continuing":
        next_prompt = "继续前先选择 owner 绑定的循环会话"
    print("# Tenetora 循环状态" if chinese else "# Tenetora Loop State")
    print()
    print(f"- {'状态' if chinese else 'status'}: `{state.get('status', 'idle')}`")
    print(f"- {'目标' if chinese else 'goal'}: {state.get('current_goal') or empty}")
    print(f"- {'下一提示' if chinese else 'next_prompt'}: `{next_prompt}`")
    print(f"- {'最近步骤' if chinese else 'latest_step'}: {state.get('latest_step') or empty}")
    print(f"- {'最近验证' if chinese else 'latest_verification'}: {state.get('latest_verification') or empty}")
    blocker = state.get("last_blocker") or empty
    print(f"- {'阻塞项' if chinese else 'blocker'}: {blocker}")
    print(f"- {'同一阻塞次数' if chinese else 'same_blocker_count'}: {state.get('same_blocker_count', 0)}")
    alignment = state.get("alignment", {})
    if isinstance(alignment, dict) and alignment.get("alignment_id"):
        print(f"- {'对齐' if chinese else 'alignment'}: `{alignment.get('alignment_id')}` ({alignment.get('status')})")
    review = state.get("review_cycle", {})
    if isinstance(review, dict):
        print(
            (
                f"- 审查循环: `{review.get('status', 'idle')}`（审查 {review.get('round', 0)}/{review.get('max_review_rounds', 3)}，修复 {review.get('fix_rounds', 0)}/{review.get('max_fix_rounds', 2)}）"
                if chinese
                else f"- review_cycle: `{review.get('status', 'idle')}` (review {review.get('round', 0)}/{review.get('max_review_rounds', 3)}, fix {review.get('fix_rounds', 0)}/{review.get('max_fix_rounds', 2)})"
            )
        )
    criteria = state.get("exit_criteria", [])
    if isinstance(criteria, list) and criteria:
        print()
        print("## 退出标准" if chinese else "## Exit Criteria")
        for item in criteria:
            print(f"- {item}")
    print()
    print("自动触发策略：仅用户消息或工具路由；禁止后台执行。" if chinese else "Auto-trigger policy: user-message-or-tool-routing-only; no background execution.")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root: Path = args.path
    if args.reset and not args.write:
        print("--reset requires --write", file=sys.stderr)
        return 2
    if args.alignment_proof and not args.write:
        print("--alignment-proof requires --write", file=sys.stderr)
        return 2
    if args.write:
        if not (root / ".tenetora").is_dir():
            print(
                harness_missing_message(root),
                file=sys.stderr,
            )
            return 2
        try:
            state = load_state(root)
            state = apply_updates(root, state, args)
            write_state(root, state)
        except (FileNotFoundError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    else:
        try:
            state = safe_state_view(load_state(root), args)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
    else:
        print_markdown(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
