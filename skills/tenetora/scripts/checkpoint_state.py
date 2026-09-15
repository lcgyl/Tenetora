"""Create and inspect conservative project checkpoints backed by Git objects."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from harness_io import atomic_write_text
from governance_trail import append_event
from path_security import validate_existing_project_path


CHECKPOINTS_REL = Path(".tenetora/state/checkpoints")
CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINT_REF_PREFIX = "refs/tenetora/checkpoints/"
IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{3,127}")
FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
SAFE_TEXT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+/-]{0,127}")


class CheckpointError(RuntimeError):
    pass


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(value: dt.datetime) -> str:
    return value.isoformat()


def parse_iso(value: object) -> dt.datetime:
    if not isinstance(value, str) or not value.strip():
        raise CheckpointError("checkpoint timestamp is invalid")
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError as error:
        raise CheckpointError("checkpoint timestamp is invalid") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def validate_value(value: str, name: str, pattern: re.Pattern[str]) -> str:
    if not pattern.fullmatch(value):
        raise CheckpointError(f"{name} is invalid")
    return value


def project_fingerprint(root: Path) -> str:
    try:
        root = validate_existing_project_path(root, label="checkpoint project")
    except RuntimeError as exc:
        raise CheckpointError(str(exc)) from exc
    return hashlib.sha256(str(root).encode("utf-8", errors="surrogatepass")).hexdigest()


def git(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        raise CheckpointError("Git repository query failed")
    return result.stdout.strip()


def git_repository(root: Path) -> dict[str, str]:
    try:
        root = validate_existing_project_path(root, label="checkpoint project")
    except RuntimeError as exc:
        raise CheckpointError(str(exc)) from exc
    if not (root / ".tenetora").is_dir():
        raise CheckpointError("Tenetora harness is missing; initialize the project first")
    try:
        if git(root, "rev-parse", "--is-inside-work-tree") != "true":
            raise CheckpointError("checkpoint provider clean-git-ref requires a Git worktree")
        top = git(root, "rev-parse", "--show-toplevel")
        head = git(root, "rev-parse", "HEAD")
        common = git(root, "rev-parse", "--git-common-dir")
    except CheckpointError as error:
        if str(error) == "Git repository query failed":
            raise CheckpointError("checkpoint provider clean-git-ref requires a Git worktree") from error
        raise
    except Exception as error:
        raise CheckpointError("checkpoint provider clean-git-ref requires a Git worktree") from error
    if not re.fullmatch(r"[0-9a-f]{40,64}", head):
        raise CheckpointError("Git HEAD is unavailable")
    return {
        "git_head": head,
        "git_top_fingerprint": hashlib.sha256(top.encode("utf-8", errors="surrogatepass")).hexdigest(),
        "git_common_fingerprint": hashlib.sha256(common.encode("utf-8", errors="surrogatepass")).hexdigest(),
        "git_worktree_fingerprint": hashlib.sha256(str(root.resolve()).encode("utf-8", errors="surrogatepass")).hexdigest(),
        "git_branch": git(root, "branch", "--show-current", check=False),
    }


def git_state_fingerprint(root: Path) -> str:
    status = git(root, "status", "--porcelain=v1", "--untracked-files=all")
    return hashlib.sha256(status.encode("utf-8", errors="surrogatepass")).hexdigest()


def ensure_clean(root: Path) -> str:
    status = git(root, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise CheckpointError("clean-git-ref requires a clean Git worktree; dirty snapshots are not supported yet")
    return hashlib.sha256(status.encode("utf-8")).hexdigest()


def checkpoint_directory(root: Path) -> Path:
    try:
        root = validate_existing_project_path(root, label="checkpoint project")
    except RuntimeError as exc:
        raise CheckpointError(str(exc)) from exc
    return root / CHECKPOINTS_REL


@contextmanager
def directory_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / ".create.lock"
    handle = lock_path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def canonical_record(record: dict[str, object]) -> str:
    unsigned = {key: value for key, value in record.items() if key != "record_digest"}
    return json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def record_digest(record: dict[str, object]) -> str:
    return hashlib.sha256(canonical_record(record).encode("utf-8")).hexdigest()


def checkpoint_path(root: Path, checkpoint_id: str) -> Path:
    validate_value(checkpoint_id, "checkpoint id", IDENTITY_RE)
    return checkpoint_directory(root) / f"{checkpoint_id}.json"


def read_record(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CheckpointError("checkpoint record is corrupt") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        raise CheckpointError("checkpoint record schema is unsupported")
    if payload.get("record_digest") != record_digest(payload):
        raise CheckpointError("checkpoint record integrity check failed")
    if not isinstance(payload.get("checkpoint_id"), str) or payload["checkpoint_id"] != path.stem:
        raise CheckpointError("checkpoint record id is invalid")
    if not isinstance(payload.get("project_root_fingerprint"), str) or not FINGERPRINT_RE.fullmatch(
        payload["project_root_fingerprint"]
    ):
        raise CheckpointError("checkpoint project fingerprint is invalid")
    if not isinstance(payload.get("git_head"), str) or not re.fullmatch(r"[0-9a-f]{40,64}", payload["git_head"]):
        raise CheckpointError("checkpoint Git ref is invalid")
    parse_iso(payload.get("created_at"))
    expires = parse_iso(payload.get("expires_at"))
    if expires <= parse_iso(payload.get("created_at")):
        raise CheckpointError("checkpoint expiry is invalid")
    if payload.get("scope") != "clean-git-ref" or payload.get("snapshot_provider") != "git-object":
        raise CheckpointError("checkpoint provider is unsupported")
    return payload


def ref_name(checkpoint_id: str) -> str:
    validate_value(checkpoint_id, "checkpoint id", IDENTITY_RE)
    return f"{CHECKPOINT_REF_PREFIX}{checkpoint_id}"


def ref_matches(root: Path, record: dict[str, object]) -> bool:
    return git(root, "rev-parse", "--verify", ref_name(str(record["checkpoint_id"])), check=False) == record["git_head"]


def display_record(root: Path, record: dict[str, object], *, integrity: str = "valid") -> dict[str, object]:
    now = utc_now()
    expires = parse_iso(record["expires_at"])
    result = {
        "checkpoint_id": record["checkpoint_id"],
        "status": "expired" if expires <= now else record.get("status", "available"),
        "integrity": integrity,
        "scope": record["scope"],
        "snapshot_provider": record["snapshot_provider"],
        "git_head": record["git_head"],
        "git_branch": record.get("git_branch", ""),
        "project_root_fingerprint": record["project_root_fingerprint"],
        "git_state_fingerprint": record["git_state_fingerprint"],
        "created_at": record["created_at"],
        "expires_at": record["expires_at"],
        "owner_id": record.get("owner_id", ""),
        "session_id": record.get("session_id", ""),
        "conversation_continuity_id": record.get("conversation_continuity_id", ""),
        "goal_fingerprint": record.get("goal_fingerprint", ""),
        "execution_attempt_id": record.get("execution_attempt_id", ""),
        "provider": record.get("provider", ""),
        "model": record.get("model", ""),
    }
    return result


def create_checkpoint(args: argparse.Namespace) -> dict[str, object]:
    try:
        root = validate_existing_project_path(args.path, label="checkpoint project")
    except RuntimeError as exc:
        raise CheckpointError(str(exc)) from exc
    repository = git_repository(root)
    state_fingerprint = ensure_clean(root)
    identity_fields = {
        "owner_id": str(args.owner_id or "").strip(),
        "session_id": str(args.session_id or "").strip(),
        "conversation_continuity_id": str(args.conversation_continuity_id or "").strip(),
        "goal_fingerprint": str(args.goal_fingerprint or "").strip(),
        "execution_attempt_id": str(args.execution_attempt_id or "").strip(),
        "provider": str(args.provider or "").strip(),
        "model": str(args.model or "").strip(),
        "tool": str(args.tool or "").strip(),
    }
    for field in ("owner_id", "session_id", "conversation_continuity_id", "execution_attempt_id"):
        if identity_fields[field]:
            validate_value(identity_fields[field], field, IDENTITY_RE)
    if identity_fields["goal_fingerprint"]:
        validate_value(identity_fields["goal_fingerprint"], "goal fingerprint", FINGERPRINT_RE)
    for field in ("provider", "model", "tool"):
        if identity_fields[field]:
            validate_value(identity_fields[field], field, SAFE_TEXT_RE)
    try:
        ttl_days = int(args.ttl_days)
    except (TypeError, ValueError) as error:
        raise CheckpointError("ttl days is invalid") from error
    if ttl_days < 1 or ttl_days > 365:
        raise CheckpointError("ttl days must be between 1 and 365")
    checkpoint_id = f"cp-{uuid.uuid4().hex}"
    created = utc_now()
    record: dict[str, object] = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "checkpoint_id": checkpoint_id,
        "project_root_fingerprint": project_fingerprint(root),
        **identity_fields,
        **repository,
        "git_state_fingerprint": state_fingerprint,
        "scope": "clean-git-ref",
        "snapshot_provider": "git-object",
        "content_digest": hashlib.sha256(repository["git_head"].encode("ascii")).hexdigest(),
        "created_at": iso(created),
        "expires_at": iso(created + dt.timedelta(days=ttl_days)),
        "status": "available",
    }
    record["record_digest"] = record_digest(record)
    directory = checkpoint_directory(root)
    with directory_lock(directory):
        path = checkpoint_path(root, checkpoint_id)
        if path.exists():
            raise CheckpointError("checkpoint id collision")
        ref = ref_name(checkpoint_id)
        result = subprocess.run(
            ["git", "-C", str(root), "update-ref", ref, repository["git_head"]],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise CheckpointError("checkpoint Git ref could not be created")
        try:
            atomic_write_text(path, json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        except Exception:
            subprocess.run(["git", "-C", str(root), "update-ref", "-d", ref], check=False)
            raise CheckpointError("checkpoint metadata could not be written")
    append_event(
        root,
        {
            "type": "checkpoint",
            "action": "create",
            "status": "pass",
            "checkpoint_id": checkpoint_id,
            "scope": "clean-git-ref",
            "snapshot_provider": "git-object",
            "project_root_fingerprint": record["project_root_fingerprint"],
            "git_state_fingerprint": record["git_state_fingerprint"],
            "owner_id": record["owner_id"],
            "session_id": record["session_id"],
            "conversation_continuity_id": record["conversation_continuity_id"],
            "goal_fingerprint": record["goal_fingerprint"],
        },
    )
    return display_record(root, record)


def list_checkpoints(args: argparse.Namespace) -> list[dict[str, object]]:
    try:
        root = validate_existing_project_path(args.path, label="checkpoint project")
    except RuntimeError as exc:
        raise CheckpointError(str(exc)) from exc
    git_repository(root)
    directory = checkpoint_directory(root)
    if not directory.is_dir():
        return []
    rows: list[dict[str, object]] = []
    for path in sorted(directory.glob("cp-*.json")):
        try:
            record = read_record(path)
            integrity = "valid" if ref_matches(root, record) else "ref-missing"
            rows.append(display_record(root, record, integrity=integrity))
        except CheckpointError:
            rows.append({"checkpoint_id": path.stem, "status": "corrupt", "integrity": "invalid"})
    rows.sort(key=lambda row: str(row.get("created_at", "")), reverse=True)
    return rows


def inspect_checkpoint(args: argparse.Namespace) -> dict[str, object]:
    try:
        root = validate_existing_project_path(args.path, label="checkpoint project")
    except RuntimeError as exc:
        raise CheckpointError(str(exc)) from exc
    git_repository(root)
    record = read_record(checkpoint_path(root, args.id))
    expected_root = project_fingerprint(root)
    if record["project_root_fingerprint"] != expected_root:
        raise CheckpointError("checkpoint belongs to another project")
    requested_owner = str(args.owner_id or "").strip()
    recorded_owner = str(record.get("owner_id") or "")
    if requested_owner and recorded_owner and requested_owner != recorded_owner:
        raise CheckpointError("checkpoint belongs to another owner")
    integrity = "valid" if ref_matches(root, record) else "ref-missing"
    result = display_record(root, record, integrity=integrity)
    current_head = git(root, "rev-parse", "HEAD")
    current_state = git_state_fingerprint(root)
    result["current_git_head"] = current_head
    result["current_git_state_fingerprint"] = current_state
    result["baseline_match"] = current_head == record["git_head"] and current_state == record["git_state_fingerprint"]
    requested_goal = str(args.goal_fingerprint or "").strip()
    if requested_goal:
        validate_value(requested_goal, "goal fingerprint", FINGERPRINT_RE)
        result["goal_match"] = requested_goal == record.get("goal_fingerprint", "")
    return result


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Manage conservative clean-git-ref project checkpoints.")
    actions = command.add_mutually_exclusive_group(required=True)
    actions.add_argument("--create", action="store_true", help="Create a checkpoint from a clean Git worktree.")
    actions.add_argument("--list", action="store_true", help="List checkpoint metadata without modifying the worktree.")
    actions.add_argument("--inspect", action="store_true", help="Inspect one checkpoint by id.")
    command.add_argument("--path", default=".", help="Project path.")
    command.add_argument("--id", help="Checkpoint id for --inspect.")
    command.add_argument("--json", action="store_true", help="Print machine-readable output.")
    command.add_argument("--owner-id")
    command.add_argument("--session-id")
    command.add_argument("--conversation-continuity-id")
    command.add_argument("--goal-fingerprint")
    command.add_argument("--execution-attempt-id")
    command.add_argument("--provider")
    command.add_argument("--model")
    command.add_argument("--tool")
    command.add_argument("--ttl-days", type=int, default=30)
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.inspect and not args.id:
            raise CheckpointError("--inspect requires --id")
        if args.create:
            result: object = create_checkpoint(args)
        elif args.inspect:
            result = inspect_checkpoint(args)
        else:
            result = list_checkpoints(args)
    except CheckpointError as error:
        if args.json:
            print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        else:
            print(f"Checkpoint error: {error}")
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    elif isinstance(result, list):
        if not result:
            print("No checkpoints.")
        for row in result:
            print(
                f"{row.get('checkpoint_id', '<unknown>')} "
                f"status={row.get('status', 'unknown')} integrity={row.get('integrity', 'unknown')} "
                f"created={row.get('created_at', 'unknown')} head={row.get('git_head', 'unknown')}"
            )
    else:
        for key, value in result.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
