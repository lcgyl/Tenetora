#!/usr/bin/env python3
"""Build the compact current state view from existing governance sources."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from governance_trail import iter_events, load_trail, write_json_atomic

STATE_REL = Path(".tenetora/state/current.json")


def read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else fallback
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return fallback


def build_state(root: Path) -> dict[str, Any]:
    sources: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    fingerprints: dict[str, str | None] = {}
    for name in ("loop-state", "current-alignment", "current-evidence", "features"):
        relative = f".tenetora/state/{name}.json"
        path = root / relative
        try:
            raw = path.read_bytes() if path.is_file() else None
            payload = json.loads(raw) if raw is not None else None
            fingerprints[relative] = hashlib.sha256(raw).hexdigest() if raw is not None else None
        except (OSError, ValueError, UnicodeDecodeError):
            payload = None
            fingerprints[relative] = None
        if path.exists() and not isinstance(payload, dict):
            problems.append(f"{name}-invalid")
        sources[name] = payload if isinstance(payload, dict) else {}
    loop = sources["loop-state"]
    alignment = sources["current-alignment"]
    evidence_pointer = sources["current-evidence"]
    features = sources["features"].get("features", [])
    if not isinstance(features, list) or any(not isinstance(item, dict) for item in features):
        problems.append("features-invalid")
        features = []
    goal = alignment.get("goal") or loop.get("current_goal")
    loop_matches = not alignment.get("goal") or loop.get("current_goal") == alignment.get("goal")
    if loop.get("status") in {"active", "blocked"} and not loop_matches:
        problems.append("active-goal-conflict")
    trail = load_trail(root)
    if trail.get("invalid") or trail.get("conflicts"):
        problems.append("trail-invalid-or-divergent")
    latest_claim = next(
        (
            event
            for event in reversed(list(iter_events(root)))
            if event.get("type") == "verification-claim"
            and (not alignment.get("goal_fingerprint") or event.get("goal_fingerprint") == alignment.get("goal_fingerprint"))
            and (not alignment.get("owner_id") or event.get("owner_id") == alignment.get("owner_id"))
        ),
        None,
    )
    loop_status = str(loop.get("status") or "") if loop_matches else ""
    phase = "planning"
    if latest_claim:
        phase = "verifying"
    if loop_status == "active":
        phase = "implementing"
    if loop_status == "done" or alignment.get("lifecycle_status") == "completed":
        phase = "done"
    if loop_status == "blocked" or problems or (latest_claim and latest_claim.get("verification_status") != "passed"):
        phase = "blocked"
    current = evidence_pointer.get("current", {}) if isinstance(evidence_pointer, dict) else {}
    if current and not isinstance(current, dict):
        problems.append("current-evidence-invalid")
    if isinstance(current, dict):
        for key in ("evidence", "report"):
            value = current.get(key)
            if value and (not isinstance(value, str) or Path(value).is_absolute() or ".." in Path(value).parts or not (root / value).is_file()):
                problems.append(f"{key}-pointer-missing-or-unsafe")
    if problems:
        phase = 'blocked'
    return {
        "version": 2,
        "authority": "derived-view; source state and journal remain authoritative",
        "goal": goal or None,
        "goal_fingerprint": alignment.get("goal_fingerprint"),
        "phase": phase,
        "verification": {
            "status": latest_claim.get("verification_status") if latest_claim else "none",
            "claim_kind": latest_claim.get("claim_kind") if latest_claim else None,
            "claim_proof": latest_claim.get("claim_proof") if latest_claim else None,
            "command_hash": latest_claim.get("verification_command_hash") if latest_claim else None,
        },
        "trail": {
            "source": trail.get("source", "legacy"),
            "events": len(trail.get("events", [])),
            "invalid": bool(trail.get("invalid")),
        },
        "evidence": current.get("evidence") if isinstance(current, dict) else None,
        "features": [{"id": item.get("id"), "status": item.get("status")} for item in features],
        "source_fingerprints": fingerprints,
        "source_problems": sorted(set(problems)),
        "next_step": (loop.get("next_loop_prompt") or None) if loop_matches else None,
        "updated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


def _comparable(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove volatile fields before comparing the derived view."""

    return {key: value for key, value in payload.items() if key != "updated_at"}


def consistency(root: Path) -> dict[str, Any]:
    """Check whether current.json matches the current source state."""

    path = root / STATE_REL
    expected = build_state(root)
    if expected["source_problems"]:
        return {"status": "invalid", "code": "current-state-source-conflict", "message": "Source state is invalid or inconsistent; rebuilding cannot resolve it.", "problems": expected["source_problems"]}
    if not path.is_file():
        return {
            "status": "missing",
            "code": "current-state-missing",
            "message": "Derived current state has not been rebuilt.",
            "path": STATE_REL.as_posix(),
        }
    try:
        observed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {
            "status": "invalid",
            "code": "current-state-invalid",
            "message": "Derived current state is not valid JSON.",
            "path": STATE_REL.as_posix(),
        }
    if not isinstance(observed, dict):
        return {
            "status": "invalid",
            "code": "current-state-invalid",
            "message": "Derived current state must be a JSON object.",
            "path": STATE_REL.as_posix(),
        }
    if _comparable(observed) == _comparable(expected):
        return {"status": "pass", "code": "current-state-consistent", "message": "Derived current state matches its sources."}
    changed = sorted(
        key
        for key in set(observed) | set(expected)
        if key != "updated_at" and observed.get(key) != expected.get(key)
    )
    return {
        "status": "stale",
        "code": "current-state-stale",
        "message": "Derived current state is stale; run `tenetora state rebuild --path .`.",
        "changed_fields": changed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read or rebuild the compact current governance state.")
    parser.add_argument("action", choices=("status", "check", "rebuild"))
    parser.add_argument("--path", default=".", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    root = args.path.expanduser().resolve()
    payload = build_state(root)
    check = consistency(root)
    payload["consistency"] = check
    if args.action == "check":
        if args.json:
            print(json.dumps(check, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(check, ensure_ascii=False))
        return 0 if check["status"] == "pass" else 1
    if args.action == "rebuild":
        if payload["source_problems"]:
            print(json.dumps(check, ensure_ascii=False, indent=2))
            return 1
        write_json_atomic(root / STATE_REL, build_state(root))
        payload["consistency"] = consistency(root)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
