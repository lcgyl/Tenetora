#!/usr/bin/env python3
"""Create and validate a parent-repository aggregate proof from child claims."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import stat
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from harness_io import atomic_write_text  # noqa: E402
import alignment_state  # noqa: E402
import repository_units  # noqa: E402
from path_security import is_redirected_path, validate_existing_project_path  # noqa: E402
from worktree_fingerprint import (  # noqa: E402
    WORKTREE_FINGERPRINT_VERSION,
    git_head,
    git_state_fingerprint,
    normalize_scopes,
    verification_scopes_intersect,
)


STATE_REL = ".tenetora/state/aggregate-claim.json"
STATE_VERSION = 1
CLAIM_PROOF = re.compile(r"^ah-claim-[0-9a-f]{32}$")
AGGREGATE_PROOF = re.compile(r"^ah-aggregate-[0-9a-f]{32}$")
WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")


class AggregateStateConflict(RuntimeError):
    """Raised when an aggregate state write observes a newer revision."""


def existing_project_path(raw: str) -> Path:
    try:
        return validate_existing_project_path(raw)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(
        prog="tenetora aggregate-claim",
        description="Create or validate a parent-repository aggregate verification proof.",
        add_help=False,
    )
    command_parser.add_argument("--help", action="help", help="Show this help message and exit.")
    action = command_parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--create", action="store_true", help="Create a new aggregate proof.")
    action.add_argument("--check", action="store_true", help="Validate the current aggregate proof.")
    command_parser.add_argument("-p", "--path", default=".", type=existing_project_path, metavar="<parent-repository>")
    command_parser.add_argument(
        "--child-proof",
        action="append",
        default=[],
        metavar="PATH=PROOF",
        help="Bind one indexed child repository path to an ah-claim proof; repeatable.",
    )
    command_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return command_parser


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def state_path(root: Path) -> Path:
    return root / STATE_REL


def state_parent_problem(root: Path) -> str | None:
    current = root
    for part in (".tenetora", "state"):
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError:
            return "aggregate-state-parent-unreadable"
        if is_redirected_path(current):
            return "aggregate-state-parent-symlink"
        if not stat.S_ISDIR(mode):
            return "aggregate-state-parent-not-directory"
    return None


def normalize_relative(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip().replace("\\", "/")
    if not value or value.startswith(("/", "~/")) or WINDOWS_ABSOLUTE.match(value):
        return None
    parts = [part for part in value.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def path_problem(root: Path, relative: str, *, directory: bool = False) -> str | None:
    current = root
    for part in relative.split("/"):
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return "child-path-missing"
        except OSError:
            return "child-path-unreadable"
        if is_redirected_path(current):
            return "child-path-symlink"
    try:
        mode = current.lstat().st_mode
    except OSError:
        return "child-path-unreadable"
    if directory and not stat.S_ISDIR(mode):
        return "child-path-not-directory"
    return None


def project_root_fingerprint(root: Path) -> str:
    return hashlib.sha256(str(root.resolve(strict=False)).encode("utf-8", errors="surrogatepass")).hexdigest()


def load_state(root: Path) -> dict[str, Any]:
    parent_problem = state_parent_problem(root)
    if parent_problem:
        raise ValueError(parent_problem)
    path = state_path(root)
    if is_redirected_path(path):
        raise ValueError("aggregate state path is redirected or not a file")
    if not path.exists():
        return {"version": STATE_VERSION, "revision": 0, "status": "idle"}
    if not path.is_file():
        raise ValueError("aggregate state path is redirected or not a file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"aggregate state is invalid: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        raise ValueError("aggregate state has an unsupported schema")
    revision = payload.get("revision")
    if type(revision) is not int or revision < 0:
        raise ValueError("aggregate state revision is invalid")
    return payload


def write_state(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    path = state_path(root)
    expected_revision = payload.get("revision")
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("aggregate state revision is invalid")
    parent_problem = state_parent_problem(root)
    if parent_problem:
        raise ValueError(parent_problem)
    path.parent.mkdir(parents=True, exist_ok=True)
    with alignment_state.state_lock(path):
        parent_problem = state_parent_problem(root)
        if parent_problem:
            raise ValueError(parent_problem)
        if is_redirected_path(path):
            raise ValueError("aggregate state path is redirected")
        current_revision = 0
        if path.is_file():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AggregateStateConflict("aggregate state changed or became unreadable") from exc
            if not isinstance(current, dict) or type(current.get("revision")) is not int:
                raise AggregateStateConflict("aggregate state revision is invalid")
            current_revision = int(current["revision"])
        if current_revision != expected_revision:
            raise AggregateStateConflict("aggregate state changed concurrently; reload before writing")
        written = dict(payload)
        written["revision"] = current_revision + 1
        atomic_write_text(path, json.dumps(written, ensure_ascii=False, indent=2) + "\n")
    payload.clear()
    payload.update(written)
    return payload


def parse_child_proofs(raw_values: list[str]) -> tuple[dict[str, str], list[str]]:
    result: dict[str, str] = {}
    problems: list[str] = []
    for raw in raw_values:
        if "=" not in raw:
            problems.append("child-proof-format-invalid")
            continue
        raw_path, proof = raw.split("=", 1)
        path = normalize_relative(raw_path)
        proof = proof.strip()
        if path is None:
            problems.append("child-path-invalid")
            continue
        if not CLAIM_PROOF.fullmatch(proof):
            problems.append("child-proof-invalid")
            continue
        if path in result:
            problems.append("child-proof-duplicate")
            continue
        result[path] = proof
    if not result:
        problems.append("child-proof-required")
    return result, sorted(set(problems))


def proof_event(child: Path, proof: str) -> tuple[dict[str, Any] | None, str | None]:
    relative = ".tenetora/state/governance-trail.json"
    problem = path_problem(child, relative)
    if problem:
        return None, "child-proof-trail-" + problem.removeprefix("child-path-")
    try:
        payload = json.loads((child / relative).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "child-proof-trail-invalid"
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        return None, "child-proof-trail-invalid"
    matching_index: int | None = None
    matching_event: dict[str, Any] | None = None
    for index, item in enumerate(events):
        if isinstance(item, dict) and item.get("type") == "verification-claim" and item.get("claim_proof") == proof:
            matching_index = index
            matching_event = item
    if matching_event is None or matching_index is None:
        return None, "child-proof-missing"
    if matching_event.get("status") != "pass" or matching_event.get("verification_status") != "passed":
        return None, "child-proof-not-passed"
    if matching_event.get("claim_kind") not in {"completion", "partial-verification"}:
        return None, "child-proof-kind-invalid"
    matching_scope = matching_event.get("verification_scope", [])
    if not isinstance(matching_scope, list) or any(not isinstance(item, str) for item in matching_scope):
        matching_scope = []
    for item in events[matching_index + 1 :]:
        if not isinstance(item, dict) or item.get("type") != "verification-claim":
            continue
        if item.get("status") == "pass" and item.get("verification_status") == "passed":
            continue
        later_scope = item.get("verification_scope", [])
        if not isinstance(later_scope, list) or any(not isinstance(value, str) for value in later_scope):
            return None, "child-proof-superseded"
        try:
            overlaps = verification_scopes_intersect(matching_scope, later_scope)
        except ValueError:
            overlaps = True
        if overlaps:
            return None, "child-proof-superseded"
    return matching_event, None


def proof_evidence_fingerprint(event: dict[str, Any]) -> str:
    canonical = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def proof_handoff_problem(child: Path, event: dict[str, Any]) -> str | None:
    session_id = str(event.get("session_id") or "")
    owner_id = str(event.get("owner_id") or "")
    conversation_id = str(event.get("conversation_id") or "")
    goal_fingerprint = str(event.get("goal_fingerprint") or "")
    handoff_hash = str(event.get("handoff_hash") or "")
    if not session_id or not owner_id or not conversation_id or not re.fullmatch(r"[0-9a-f]{64}", goal_fingerprint):
        return "child-proof-handoff-invalid"
    if not re.fullmatch(r"[0-9a-f]{64}", handoff_hash):
        return "child-proof-handoff-invalid"
    if event.get("confirmation_assurance") != "recorded-user-assertion":
        return "child-proof-handoff-invalid"
    try:
        handoff = alignment_state.load_handoff(child, session_id)
    except alignment_state.AlignmentStateError:
        return "child-proof-handoff-invalid"
    if not isinstance(handoff, dict):
        return "child-proof-handoff-invalid"
    if handoff.get("status") not in {"confirmed", "accepted-with-risks"}:
        return "child-proof-handoff-invalid"
    if handoff.get("owner_id") != owner_id or handoff.get("conversation_id") != conversation_id:
        return "child-proof-handoff-invalid"
    if handoff.get("goal_fingerprint") != goal_fingerprint:
        return "child-proof-handoff-invalid"
    if alignment_state.handoff_hash(handoff) != handoff_hash:
        return "child-proof-handoff-invalid"
    return None


def indexed_gitlink_snapshot(parent: Path) -> tuple[list[dict[str, Any]], list[str]]:
    indexed, conflicts = repository_units.indexed_gitlinks(parent)
    snapshot: list[dict[str, Any]] = []
    for path, pointer in sorted(indexed.items()):
        child = parent / path
        head = git_head(child)
        snapshot.append(
            {
                "path": path,
                "pointer": pointer,
                "child_head": head,
                "child_git_state_fingerprint": git_state_fingerprint(child),
            }
        )
    return snapshot, conflicts


def child_summary(parent: Path, path: str, proof: str) -> tuple[dict[str, Any], list[str]]:
    problems: list[str] = []
    path_problem_code = path_problem(parent, path, directory=True)
    if path_problem_code:
        return {"path": path, "claim_proof": proof}, [path_problem_code]
    child = parent / path
    event, proof_problem = proof_event(child, proof)
    if proof_problem:
        problems.append(proof_problem)
        event = event or {}
    raw_scope = event.get("verification_scope", []) if event else []
    try:
        if not isinstance(raw_scope, list) or any(not isinstance(item, str) for item in raw_scope):
            raise ValueError("invalid verification scope")
        verification_scope = list(normalize_scopes(raw_scope))
    except ValueError:
        verification_scope = []
        problems.append("child-proof-verification-scope-invalid")
    current_fingerprint = git_state_fingerprint(child, verification_scope)
    if event and event.get("project_root_fingerprint") != project_root_fingerprint(child):
        problems.append("child-proof-project-mismatch")
    if event:
        handoff_problem = proof_handoff_problem(child, event)
        if handoff_problem:
            problems.append(handoff_problem)
        if event.get("git_state_fingerprint_version") != WORKTREE_FINGERPRINT_VERSION:
            problems.append("child-proof-worktree-fingerprint-unsupported")
    if event and event.get("git_state_fingerprint") != current_fingerprint:
        problems.append("child-proof-worktree-changed")
    summary = {
        "path": path,
        "claim_proof": proof,
        "claim_kind": event.get("claim_kind") if event else None,
        "verification_status": event.get("verification_status") if event else None,
        "verification_command_hash": event.get("verification_command_hash") if event else None,
        "verification_command_length": event.get("verification_command_length", 0) if event else 0,
        "verification_scope": verification_scope,
        "goal_fingerprint": event.get("goal_fingerprint") if event else None,
        "proof_evidence_fingerprint": proof_evidence_fingerprint(event) if event else None,
        "git_state_fingerprint_version": event.get("git_state_fingerprint_version") if event else None,
        "git_state_fingerprint": current_fingerprint,
        "problems": sorted(set(problems)),
    }
    return summary, sorted(set(problems))


def collect_snapshot(parent: Path, proofs: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    problems: list[str] = []
    parent_head = git_head(parent)
    parent_fingerprint = git_state_fingerprint(parent)
    if parent_head is None:
        problems.append("parent-git-head-unavailable")
    if parent_fingerprint is None:
        problems.append("parent-git-state-unavailable")
    gitlinks, conflicts = indexed_gitlink_snapshot(parent)
    if conflicts:
        problems.append("parent-gitlink-index-conflict")
    indexed_paths = {item["path"] for item in gitlinks}
    proof_paths = set(proofs)
    for path in sorted(proof_paths):
        path_problem_code = path_problem(parent, path, directory=True)
        if path_problem_code:
            problems.append(path_problem_code)
    for path in sorted(indexed_paths - proof_paths):
        problems.append("child-proof-missing")
    for path in sorted(proof_paths - indexed_paths):
        problems.append("child-gitlink-missing")
    children: list[dict[str, Any]] = []
    for item in gitlinks:
        path = str(item["path"])
        proof = proofs.get(path)
        if proof is None:
            continue
        summary, child_problems = child_summary(parent, path, proof)
        item["problems"] = child_problems
        children.append(summary)
        problems.extend(child_problems)
        if item.get("child_head") is None:
            problems.append("child-head-unavailable")
        if item.get("pointer") != item.get("child_head"):
            problems.append("child-head-mismatch")
    parent_units = repository_units.inspect_repository_units(parent, parent)
    if parent_units.get("status") != "pass":
        problems.append("repository-unit-invalid")
        problems.extend(str(item) for item in parent_units.get("problems", []) if isinstance(item, str))
    return {
        "worktree_fingerprint_version": WORKTREE_FINGERPRINT_VERSION,
        "parent_head": parent_head,
        "parent_git_state_fingerprint": parent_fingerprint,
        "gitlinks": gitlinks,
        "children": sorted(children, key=lambda item: str(item["path"])),
    }, sorted(set(problems))


def aggregate_proof_for(snapshot: dict[str, Any]) -> str:
    proof_snapshot = {key: value for key, value in snapshot.items() if key != "parent_head"}
    canonical = json.dumps(proof_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"ah-aggregate-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:32]}"


def create_aggregate(parent: Path, raw_proofs: list[str]) -> dict[str, Any]:
    proofs, parse_problems = parse_child_proofs(raw_proofs)
    if parse_problems:
        return failure_payload(parse_problems)
    try:
        current = load_state(parent)
    except ValueError as exc:
        return failure_payload([state_failure_code(exc)], str(exc))
    if current.get("status") == "pass":
        return failure_payload(["aggregate-state-exists"], "An aggregate proof already exists; check it before creating another.")
    snapshot, problems = collect_snapshot(parent, proofs)
    if problems:
        return failure_payload(problems)
    payload: dict[str, Any] = {
        "version": STATE_VERSION,
        "revision": int(current.get("revision", 0)),
        "status": "pass",
        "code": "aggregate-claim-created",
        "aggregate_proof": aggregate_proof_for(snapshot),
        "created_at": now_iso(),
        **snapshot,
    }
    try:
        write_state(parent, payload)
    except AggregateStateConflict:
        return failure_payload(["aggregate-state-conflict"], "Aggregate state changed concurrently; reload before writing.")
    except ValueError as exc:
        return failure_payload([state_failure_code(exc)], "Aggregate state path is not writable safely.")
    except OSError:
        return failure_payload(["aggregate-state-write-failed"], "Aggregate state could not be written safely.")
    return payload


def check_aggregate(parent: Path) -> dict[str, Any]:
    try:
        state = load_state(parent)
    except ValueError as exc:
        return failure_payload([state_failure_code(exc)], str(exc))
    if state.get("status") != "pass":
        return failure_payload(["aggregate-state-missing"])
    if state.get("worktree_fingerprint_version") != WORKTREE_FINGERPRINT_VERSION:
        return failure_payload(["aggregate-worktree-fingerprint-unsupported"])
    if not isinstance(state.get("aggregate_proof"), str) or not AGGREGATE_PROOF.fullmatch(state["aggregate_proof"]):
        return failure_payload(["aggregate-proof-invalid"])
    stored_children = state.get("children")
    if not isinstance(stored_children, list):
        return failure_payload(["aggregate-state-invalid"])
    proofs: dict[str, str] = {}
    child_state_problems: list[str] = []
    for item in stored_children:
        if not isinstance(item, dict):
            child_state_problems.append("aggregate-children-invalid")
            continue
        path = normalize_relative(item.get("path"))
        proof = item.get("claim_proof")
        if path is None or not isinstance(proof, str) or not CLAIM_PROOF.fullmatch(proof):
            child_state_problems.append("aggregate-children-invalid")
            continue
        if path in proofs:
            child_state_problems.append("aggregate-children-invalid")
            continue
        proofs[path] = proof
    if child_state_problems:
        return failure_payload(child_state_problems)
    snapshot, problems = collect_snapshot(parent, proofs)
    if state.get("aggregate_proof") != aggregate_proof_for(snapshot):
        problems.append("aggregate-proof-mismatch")
    if snapshot.get("parent_git_state_fingerprint") != state.get("parent_git_state_fingerprint"):
        problems.append("parent-worktree-changed")
    stored_gitlinks = state.get("gitlinks")
    if stored_gitlinks != snapshot.get("gitlinks"):
        stored_by_path = {
            str(item.get("path")): item.get("pointer")
            for item in stored_gitlinks
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        } if isinstance(stored_gitlinks, list) else {}
        current_by_path = {
            str(item.get("path")): item.get("pointer")
            for item in snapshot.get("gitlinks", [])
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        }
        if set(stored_by_path) != set(current_by_path):
            problems.append("gitlink-set-changed")
        if any(stored_by_path.get(path) != current_by_path.get(path) for path in set(stored_by_path) | set(current_by_path)):
            problems.append("gitlink-pointer-changed")
        if stored_gitlinks != snapshot.get("gitlinks"):
            problems.append("gitlink-snapshot-changed")
    stored_by_path = {
        str(item.get("path")): item
        for item in stored_children
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    for current_child in snapshot.get("children", []):
        path = str(current_child.get("path"))
        stored_child = stored_by_path.get(path)
        if not stored_child:
            problems.append("child-gitlink-missing")
            continue
        if current_child.get("git_state_fingerprint") != stored_child.get("git_state_fingerprint"):
            problems.append("child-head-changed")
        if current_child.get("claim_proof") != stored_child.get("claim_proof"):
            problems.append("child-proof-changed")
        if current_child.get("proof_evidence_fingerprint") != stored_child.get("proof_evidence_fingerprint"):
            problems.append("child-proof-changed")
    if stored_children != snapshot.get("children"):
        problems.append("child-state-changed")
    status = "fail" if problems else "pass"
    return {
        "version": STATE_VERSION,
        "status": status,
        "code": "aggregate-claim-valid" if status == "pass" else "aggregate-claim-invalid",
        "aggregate_proof": state.get("aggregate_proof"),
        "revision": state.get("revision"),
        "parent_head": snapshot.get("parent_head"),
        "gitlinks": snapshot.get("gitlinks", []),
        "children": snapshot.get("children", []),
        "codes": sorted(set(problems)),
        "problems": sorted(set(problems)),
    }


def failure_payload(codes: list[str], message: str = "") -> dict[str, Any]:
    unique = sorted(set(codes))
    return {
        "version": STATE_VERSION,
        "status": "fail",
        "code": "aggregate-claim-invalid",
        "codes": unique,
        "problems": unique,
        **({"message": message} if message else {}),
    }


def state_failure_code(exc: ValueError) -> str:
    message = str(exc)
    if message in {
        "aggregate-state-parent-unreadable",
        "aggregate-state-parent-symlink",
        "aggregate-state-parent-not-directory",
    }:
        return message
    return "aggregate-state-invalid"


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    payload = create_aggregate(args.path, args.child_proof) if args.create else check_aggregate(args.path)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"tenetora aggregate-claim: {payload['status']}")
        if payload.get("codes"):
            print(f"- problems: {', '.join(payload['codes'])}")
        if payload.get("aggregate_proof"):
            print(f"- aggregate proof: {payload['aggregate_proof']}")
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
