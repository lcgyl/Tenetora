"""Preview and safely materialize clean-git-ref checkpoints in a new worktree."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from checkpoint_state import (  # noqa: E402
    CheckpointError,
    CHECKPOINTS_REL,
    checkpoint_path,
    create_checkpoint,
    display_record,
    IDENTITY_RE,
    git,
    git_repository,
    git_state_fingerprint,
    inspect_checkpoint,
    parse_iso,
    project_fingerprint,
    read_record,
    ref_name,
    utc_now,
    validate_value,
)
from governance_trail import append_event  # noqa: E402
from harness_io import atomic_write_text  # noqa: E402
from path_security import validate_unredirected_path  # noqa: E402


PLANS_REL = Path(".tenetora/state/rollback-plans")
PLAN_RE = re.compile(r"rb-[A-Za-z0-9][A-Za-z0-9._:-]{3,127}")


def plan_directory(root: Path) -> Path:
    return root / PLANS_REL


def plan_digest(payload: dict[str, object]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "plan_digest"}
    return hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def read_plan(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CheckpointError("rollback plan is corrupt") from error
    if not isinstance(payload, dict) or payload.get("plan_digest") != plan_digest(payload):
        raise CheckpointError("rollback plan integrity check failed")
    if payload.get("plan_id") != path.stem:
        raise CheckpointError("rollback plan id is invalid")
    return payload


def diff_counts(root: Path, base: str) -> dict[str, int | bool]:
    committed = git(root, "diff", "--name-status", f"{base}..HEAD")
    staged = git(root, "diff", "--cached", "--name-status")
    unstaged = git(root, "diff", "--name-status")
    untracked = git(root, "status", "--porcelain=v1", "--untracked-files=all")
    return {
        "committed_paths": len([line for line in committed.splitlines() if line.strip()]),
        "staged_paths": len([line for line in staged.splitlines() if line.strip()]),
        "unstaged_paths": len([line for line in unstaged.splitlines() if line.strip()]),
        "untracked_present": bool(untracked.strip()),
    }


def write_plan(root: Path, payload: dict[str, object]) -> None:
    directory = plan_directory(root)
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_text(directory / f"{payload['plan_id']}.json", json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def create_plan(args: argparse.Namespace) -> dict[str, object]:
    try:
        root = validate_unredirected_path(args.path, label="rollback project")
    except RuntimeError as exc:
        raise CheckpointError(str(exc)) from exc
    git_repository(root)
    checkpoint = inspect_checkpoint(
        argparse.Namespace(path=str(root), id=args.to, owner_id=args.owner_id or "", goal_fingerprint=args.goal_fingerprint or "")
    )
    if checkpoint.get("integrity") != "valid":
        raise CheckpointError("rollback target checkpoint is not intact")
    current_head = git(root, "rev-parse", "HEAD")
    current_state = git_state_fingerprint(root)
    counts = diff_counts(root, str(checkpoint["git_head"]))
    plan_id = f"rb-{uuid.uuid4().hex}"
    owner_id = str(args.owner_id or "").strip()
    if not owner_id:
        raise CheckpointError("rollback plan requires explicit owner id")
    validate_value(owner_id, "owner id", IDENTITY_RE)
    payload: dict[str, object] = {
        "schema_version": 1,
        "plan_id": plan_id,
        "checkpoint_id": args.to,
        "project_root_fingerprint": project_fingerprint(root),
        "owner_id": owner_id,
        "session_id": str(args.session_id or "").strip(),
        "conversation_continuity_id": str(args.conversation_continuity_id or "").strip(),
        "checkpoint_git_head": checkpoint["git_head"],
        "planned_current_git_head": current_head,
        "planned_current_git_state_fingerprint": current_state,
        "change_counts": counts,
        "risk": "clean-git-ref restores tracked Git content only; current dirty or untracked content is not restored",
        "status": "planned",
        "created_at": utc_now().isoformat(),
    }
    payload["plan_digest"] = plan_digest(payload)
    write_plan(root, payload)
    append_event(
        root,
        {
            "type": "rollback",
            "action": "plan",
            "status": "pass",
            "plan_id": plan_id,
            "checkpoint_id": args.to,
            "project_root_fingerprint": payload["project_root_fingerprint"],
            "owner_id": owner_id,
            "change_counts": counts,
        },
    )
    return {**payload, "checkpoint": checkpoint}


def apply_plan(args: argparse.Namespace) -> dict[str, object]:
    try:
        root = validate_unredirected_path(args.path, label="rollback project")
    except RuntimeError as exc:
        raise CheckpointError(str(exc)) from exc
    git_repository(root)
    if not args.confirm:
        raise CheckpointError("rollback apply requires --confirm; default recovery target is a new worktree")
    if args.in_place:
        raise CheckpointError("in-place rollback is disabled; use the default new-worktree recovery")
    if not args.plan_id:
        raise CheckpointError("rollback apply requires --plan-id")
    plan_path = plan_directory(root) / f"{args.plan_id}.json"
    payload = read_plan(plan_path)
    if payload.get("status") != "planned":
        raise CheckpointError("rollback plan is no longer pending")
    if str(payload.get("owner_id")) != str(args.owner_id or ""):
        raise CheckpointError("rollback plan belongs to another owner")
    if project_fingerprint(root) != payload.get("project_root_fingerprint"):
        raise CheckpointError("rollback plan belongs to another project")
    if git(root, "rev-parse", "HEAD") != payload.get("planned_current_git_head"):
        raise CheckpointError("rollback plan is stale because Git HEAD changed")
    if git_state_fingerprint(root) != payload.get("planned_current_git_state_fingerprint"):
        raise CheckpointError("rollback plan is stale because the worktree changed")
    status = git(root, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise CheckpointError("rollback apply requires a clean current worktree so recovery can be created")

    checkpoint = read_record(checkpoint_path(root, str(payload["checkpoint_id"])))
    if args.worktree_path:
        try:
            worktree = validate_unredirected_path(args.worktree_path, label="recovery worktree")
        except RuntimeError as exc:
            raise CheckpointError(str(exc)) from exc
    else:
        worktree = root.parent / f"{root.name}.tenetora-recovery-{payload['checkpoint_id']}"
    try:
        worktree.relative_to(root)
    except ValueError:
        pass
    else:
        raise CheckpointError("recovery worktree must not be inside the current project")
    if worktree.exists():
        raise CheckpointError("recovery worktree path already exists")

    recovery_args = argparse.Namespace(
        path=str(root),
        owner_id=payload.get("owner_id", ""),
        session_id=payload.get("session_id", ""),
        conversation_continuity_id=payload.get("conversation_continuity_id", ""),
        goal_fingerprint="",
        execution_attempt_id="",
        provider="",
        model="",
        tool="",
        ttl_days=30,
    )
    recovery = create_checkpoint(recovery_args)
    result = subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "--detach", str(worktree), ref_name(str(checkpoint["checkpoint_id"]))],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise CheckpointError("recovery worktree could not be created")
    payload["status"] = "applied"
    payload["recovery_checkpoint_id"] = recovery["checkpoint_id"]
    payload["recovery_worktree_fingerprint"] = hashlib.sha256(str(worktree).encode("utf-8")).hexdigest()
    payload["applied_at"] = utc_now().isoformat()
    payload["plan_digest"] = plan_digest(payload)
    atomic_write_text(plan_path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    append_event(
        root,
        {
            "type": "rollback",
            "action": "apply",
            "status": "pass",
            "plan_id": payload["plan_id"],
            "checkpoint_id": payload["checkpoint_id"],
            "recovery_checkpoint_id": recovery["checkpoint_id"],
            "recovery_worktree_fingerprint": payload["recovery_worktree_fingerprint"],
            "owner_id": payload["owner_id"],
        },
    )
    return {
        "plan_id": payload["plan_id"],
        "status": payload["status"],
        "checkpoint_id": payload["checkpoint_id"],
        "recovery_checkpoint_id": recovery["checkpoint_id"],
        "recovery_worktree": str(worktree),
        "recovery_worktree_fingerprint": payload["recovery_worktree_fingerprint"],
    }


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Preview or safely materialize a checkpoint rollback.")
    actions = command.add_mutually_exclusive_group(required=True)
    actions.add_argument("--plan", action="store_true")
    actions.add_argument("--apply", action="store_true")
    command.add_argument("--to", help="Checkpoint id for --plan.")
    command.add_argument("--plan-id", help="Rollback plan id for --apply.")
    command.add_argument("--path", default=".")
    command.add_argument("--owner-id")
    command.add_argument("--session-id")
    command.add_argument("--conversation-continuity-id")
    command.add_argument("--goal-fingerprint")
    command.add_argument("--worktree-path")
    command.add_argument("--confirm", action="store_true")
    command.add_argument("--in-place", action="store_true")
    command.add_argument("--json", action="store_true")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.plan:
            if not args.to:
                raise CheckpointError("rollback plan requires --to")
            result = create_plan(args)
        else:
            result = apply_plan(args)
    except CheckpointError as error:
        if args.json:
            print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        else:
            print(f"Rollback error: {error}")
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        for key, value in result.items():
            if key != "checkpoint":
                print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
