"""Explicit, one-time handoff transfer requests for alignment sessions."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import sys
import uuid
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from alignment_state import (  # noqa: E402
    ACTIVE_STATUSES,
    AlignmentStateError,
    OWNER_ID_RE,
    current_handoff_revision,
    isolated_session_path,
    load_all_sessions,
    load_handoff,
    new_session,
    state_lock,
    utc_now,
    validate_identity,
    write_json,
)
from governance_trail import append_event  # noqa: E402
from harness_io import atomic_write_text  # noqa: E402
from path_security import validate_existing_project_path  # noqa: E402


REQUESTS_REL = Path(".tenetora/state/handoff-transfers")
REQUEST_SCHEMA_VERSION = 1


class HandoffTransferError(RuntimeError):
    pass


def request_directory(root: Path) -> Path:
    return root / REQUESTS_REL


def project_root(raw: Path | str) -> Path:
    try:
        return validate_existing_project_path(raw, label="handoff project")
    except RuntimeError as error:
        raise HandoffTransferError(str(error)) from error


def request_digest(payload: dict[str, object]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "request_digest"}
    return hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def read_request(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HandoffTransferError("handoff transfer request is corrupt") from error
    if not isinstance(payload, dict) or payload.get("request_digest") != request_digest(payload):
        raise HandoffTransferError("handoff transfer request integrity check failed")
    if payload.get("schema_version") != REQUEST_SCHEMA_VERSION or payload.get("request_id") != path.stem:
        raise HandoffTransferError("handoff transfer request schema is unsupported")
    return payload


def parse_time(value: object) -> dt.datetime:
    if not isinstance(value, str):
        raise HandoffTransferError("handoff transfer request timestamp is invalid")
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except ValueError as error:
        raise HandoffTransferError("handoff transfer request timestamp is invalid") from error


def identity(value: str, name: str) -> str:
    try:
        return validate_identity(value.strip(), name, OWNER_ID_RE)
    except AlignmentStateError as error:
        raise HandoffTransferError(str(error)) from error


def summary(payload: dict[str, object]) -> dict[str, object]:
    expires = parse_time(payload["expires_at"])
    status = str(payload.get("status", "pending"))
    if status == "pending" and expires <= dt.datetime.now(dt.timezone.utc):
        status = "expired"
    return {
        "request_id": payload["request_id"],
        "status": status,
        "source_session_id": payload["source_session_id"],
        "source_owner_id": payload["source_owner_id"],
        "target_owner_id": payload["target_owner_id"],
        "target_conversation_continuity_id": payload["target_conversation_continuity_id"],
        "goal_fingerprint": payload["goal_fingerprint"],
        "created_at": payload["created_at"],
        "expires_at": payload["expires_at"],
        "accepted_session_id": payload.get("accepted_session_id", ""),
    }


def request_transfer(args: argparse.Namespace) -> dict[str, object]:
    root = project_root(args.path)
    source_id = identity(str(args.id or ""), "source session id")
    source_owner = identity(str(args.source_owner_id or ""), "source owner id")
    target_owner = identity(str(args.target_owner_id or ""), "target owner id")
    continuity = identity(str(args.conversation_continuity_id or ""), "target conversation continuity id")
    goal = str(args.goal_fingerprint or "").strip()
    if len(goal) != 64 or any(char not in "0123456789abcdef" for char in goal):
        raise HandoffTransferError("goal fingerprint is invalid")
    handoff = load_handoff(root, source_id)
    if handoff is None:
        raise HandoffTransferError("no confirmed handoff exists for the source session")
    if str(handoff.get("owner_id")) != source_owner:
        raise HandoffTransferError("source owner does not own this handoff")
    if str(handoff.get("goal_fingerprint")) != goal:
        raise HandoffTransferError("goal fingerprint does not match the handoff")
    try:
        ttl = int(args.ttl_days)
    except (TypeError, ValueError) as error:
        raise HandoffTransferError("ttl days is invalid") from error
    if ttl < 1 or ttl > 30:
        raise HandoffTransferError("ttl days must be between 1 and 30")
    request_id = f"transfer-{uuid.uuid4().hex}"
    # A fixed alphanumeric prefix keeps the token safe as a separate argparse
    # value even when the random URL-safe payload begins with "-".
    handoff_value = f"th_{secrets.token_urlsafe(24)}"
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    payload: dict[str, object] = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "request_id": request_id,
        "source_session_id": source_id,
        "source_owner_id": source_owner,
        "target_owner_id": target_owner,
        "target_conversation_continuity_id": continuity,
        "goal_fingerprint": goal,
        "source_handoff_revision": handoff.get("revision", 0),
        "token_digest": hashlib.sha256(handoff_value.encode("utf-8")).hexdigest(),
        "status": "pending",
        "created_at": now.isoformat(),
        "expires_at": (now + dt.timedelta(days=ttl)).isoformat(),
        "accepted_session_id": "",
    }
    payload["request_digest"] = request_digest(payload)
    directory = request_directory(root)
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_text(directory / f"{request_id}.json", json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    append_event(
        root,
        {
            "type": "handoff-transfer",
            "action": "request",
            "status": "pass",
            "request_id": request_id,
            "source_session_id": source_id,
            "source_owner_id": source_owner,
            "target_owner_id": target_owner,
            "target_conversation_continuity_id": continuity,
            "goal_fingerprint": goal,
        },
    )
    result = summary(payload)
    result["transfer_token"] = handoff_value
    return result


def accept_transfer(args: argparse.Namespace) -> dict[str, object]:
    root = project_root(args.path)
    request_id = identity(str(args.request_id or ""), "transfer request id")
    target_owner = identity(str(args.target_owner_id or ""), "target owner id")
    continuity = identity(str(args.conversation_continuity_id or ""), "target conversation continuity id")
    goal = str(args.goal_fingerprint or "").strip()
    token = str(args.token or "")
    if not token or len(token) > 256:
        raise HandoffTransferError("transfer token is invalid")
    path = request_directory(root) / f"{request_id}.json"
    with state_lock(path):
        payload = read_request(path)
        if payload.get("status") != "pending":
            raise HandoffTransferError("handoff transfer request is not pending")
        if parse_time(payload["expires_at"]) <= dt.datetime.now(dt.timezone.utc):
            raise HandoffTransferError("handoff transfer request has expired")
        if str(payload["target_owner_id"]) != target_owner or str(payload["target_conversation_continuity_id"]) != continuity:
            raise HandoffTransferError("handoff transfer target identity mismatch")
        if goal and str(payload["goal_fingerprint"]) != goal:
            raise HandoffTransferError("handoff transfer goal fingerprint mismatch")
        expected = str(payload["token_digest"])
        actual = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(expected, actual):
            raise HandoffTransferError("handoff transfer token is invalid")
        handoff = load_handoff(root, str(payload["source_session_id"]))
        if handoff is None or str(handoff.get("goal_fingerprint")) != str(payload["goal_fingerprint"]):
            raise HandoffTransferError("source handoff is no longer valid")
        for state in load_all_sessions(root):
            if (
                state.get("status") in ACTIVE_STATUSES
                and state.get("owner_id") == target_owner
                and state.get("conversation_continuity_id") == continuity
            ):
                raise HandoffTransferError("target conversation already has an active alignment session")
        args_for_session = argparse.Namespace(
            goal=handoff["goal"],
            risk_level=handoff["risk_level"],
            conversation_id=continuity,
            conversation_continuity_id=continuity,
            tool=handoff.get("tool", "unknown"),
            scope=None,
            non_goal=None,
            constraint=None,
            assumption=None,
            approval_boundary=None,
            acceptance_criterion=None,
            verification=None,
            open_question=None,
        )
        session_id = f"align-{uuid.uuid4().hex}"
        state = new_session(args_for_session, session_id, target_owner, base_current_revision=current_handoff_revision(root))
        for field in ("scope", "non_goals", "decisions", "constraints", "assumptions", "accepted_risks", "approval_boundaries", "acceptance_criteria", "verification", "open_questions"):
            state[field] = list(handoff.get(field, []))
        state["goal_fingerprint"] = str(payload["goal_fingerprint"])
        state["status"] = "awaiting-confirmation"
        state["transitions"].append({"at": utc_now(), "from": "active", "to": "awaiting-confirmation", "reason": "handoff-transfer"})
        state["transfer_request_id"] = request_id
        write_json(isolated_session_path(root, session_id), state, expected_revision=0)
        payload["status"] = "accepted"
        payload["accepted_session_id"] = session_id
        payload["accepted_at"] = utc_now()
        payload["request_digest"] = request_digest(payload)
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    append_event(
        root,
        {
            "type": "handoff-transfer",
            "action": "accept",
            "status": "pass",
            "request_id": request_id,
            "source_session_id": payload["source_session_id"],
            "target_owner_id": target_owner,
            "target_conversation_continuity_id": continuity,
            "accepted_session_id": session_id,
            "goal_fingerprint": payload["goal_fingerprint"],
        },
    )
    return {**summary(payload), "accepted_session_id": session_id}


def list_transfers(args: argparse.Namespace) -> list[dict[str, object]]:
    root = project_root(args.path)
    directory = request_directory(root)
    if not directory.is_dir():
        return []
    rows: list[dict[str, object]] = []
    for path in sorted(directory.glob("transfer-*.json")):
        try:
            rows.append(summary(read_request(path)))
        except HandoffTransferError:
            rows.append({"request_id": path.stem, "status": "corrupt"})
    return sorted(rows, key=lambda row: str(row.get("created_at", "")), reverse=True)


def inspect_transfer(args: argparse.Namespace) -> dict[str, object]:
    root = project_root(args.path)
    request_id = identity(str(args.request_id or args.id or ""), "transfer request id")
    payload = read_request(request_directory(root) / f"{request_id}.json")
    return summary(payload)


def revoke_transfer(args: argparse.Namespace) -> dict[str, object]:
    root = project_root(args.path)
    request_id = identity(str(args.request_id or ""), "transfer request id")
    owner = identity(str(args.target_owner_id or args.source_owner_id or ""), "owner id")
    path = request_directory(root) / f"{request_id}.json"
    with state_lock(path):
        payload = read_request(path)
        if owner not in {str(payload["source_owner_id"]), str(payload["target_owner_id"])}:
            raise HandoffTransferError("owner is not authorized to revoke this transfer")
        if payload.get("status") != "pending":
            raise HandoffTransferError("only pending transfer requests can be revoked")
        payload["status"] = "revoked"
        payload["revoked_at"] = utc_now()
        payload["request_digest"] = request_digest(payload)
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    append_event(root, {"type": "handoff-transfer", "action": "revoke", "status": "pass", "request_id": request_id, "owner_id": owner})
    return summary(payload)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Manage explicit one-time alignment handoff transfers.")
    actions = command.add_mutually_exclusive_group(required=True)
    actions.add_argument("--list", action="store_true")
    actions.add_argument("--inspect", action="store_true")
    actions.add_argument("--request", action="store_true")
    actions.add_argument("--accept", action="store_true")
    actions.add_argument("--revoke", action="store_true")
    command.add_argument("--path", default=".")
    command.add_argument("--id")
    command.add_argument("--request-id", dest="request_id")
    command.add_argument("--token")
    command.add_argument("--source-owner-id")
    command.add_argument("--target-owner-id")
    command.add_argument("--conversation-continuity-id")
    command.add_argument("--goal-fingerprint")
    command.add_argument("--ttl-days", type=int, default=7)
    command.add_argument("--json", action="store_true")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.list:
            result = list_transfers(args)
        elif args.inspect:
            args.request_id = args.request_id or args.id
            result = inspect_transfer(args)
        elif args.request:
            result = request_transfer(args)
        elif args.accept:
            result = accept_transfer(args)
        else:
            result = revoke_transfer(args)
    except (HandoffTransferError, AlignmentStateError) as error:
        if args.json:
            print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        else:
            print(f"Handoff transfer error: {error}")
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        if isinstance(result, list):
            for row in result:
                print(f"{row.get('request_id')} status={row.get('status')}")
        else:
            for key, value in result.items():
                print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
