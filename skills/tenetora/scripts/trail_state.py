#!/usr/bin/env python3
"""Inspect or rebuild the append-only governance trail compatibility view."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from governance_trail import AUTO_COMPACT_EVENTS, JOURNAL_REL, MAX_EVENTS, canonical_events, compact_journal, compatibility_payload, load_trail, trail_lock, trail_path, write_json_atomic  # noqa: E402


def journal_events(root: Path) -> tuple[list[dict[str, Any]], int]:
    # Keep this helper for callers that imported the old module-local name;
    # governance_trail is now the single reader used by all consumers.
    from governance_trail import journal_events as read_journal_events

    return read_journal_events(root)


def merged_events(root: Path) -> tuple[list[dict[str, Any]], int]:
    payload = load_trail(root)
    return list(payload.get("events", [])), int(payload.get("ignored_events", 0))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect or rebuild the governance trail view.")
    parser.add_argument("action", choices=("status", "rebuild", "compact"))
    parser.add_argument("--path", default=".", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--keep-events", type=int, default=MAX_EVENTS)
    args = parser.parse_args(argv)
    root = args.path.expanduser().resolve()
    original = load_trail(root)
    events, invalid = list(original.get('events', [])), int(original.get('ignored_events', 0))
    compact = None
    if args.action == "compact":
        try:
            compact = compact_journal(root, args.keep_events)
        except (OSError, ValueError) as exc:
            payload = {"version": 1, "status": "blocked", "error": str(exc)}
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"trail: blocked — {exc}")
            return 1
        events, invalid = merged_events(root)
        payload_status = "ready" if not invalid else "ready-with-invalid-events"
    payload = {
        "version": 1,
        "status": "ready" if not invalid else "ready-with-invalid-events",
        "events": len(events),
        "invalid_events": invalid,
        "conflicting_events": len(original.get('conflicts', [])),
        "journal": str((root / JOURNAL_REL).relative_to(root)),
        "view": str(trail_path(root).relative_to(root)),
    }
    if args.action == "rebuild":
        with trail_lock(root):
            events = canonical_events(root)
            invalid = int(load_trail(root).get("journal_invalid", 0))
            write_json_atomic(trail_path(root), compatibility_payload(events))
        repaired = load_trail(root)
        payload["events"] = len(repaired.get("events", []))
        payload["invalid_events"] = int(repaired.get("ignored_events", invalid))
        payload['conflicting_events'] = len(repaired.get('conflicts', []))
        invalid = payload["invalid_events"]
        payload["status"] = "ready" if not invalid else "ready-with-invalid-events"
        payload["rebuilt"] = True
    if compact is not None:
        payload["compacted"] = compact
        payload["status"] = payload_status
    if args.action == 'status' and original.get('conflicts'):
        payload['status'] = 'blocked'
    payload["source"] = "journal" if (root / JOURNAL_REL).is_dir() else "legacy"
    payload["legacy_view"] = "governance-trail.json"
    payload["retention"] = {
        "journal": "append-only segments with lossless automatic or explicit compaction",
        "active_event_threshold": AUTO_COMPACT_EVENTS,
        "archive": "complete snapshots; historical obligations remain retained",
        "compatibility_view_max_events": MAX_EVENTS,
    }
    payload["legacy_retirement"] = {
        "status": "deferred",
        "conditions": [
            "all supported readers use governance_trail.iter_events",
            "a release has completed dual-read verification",
            "an explicit migration decision approves removing the compatibility view",
        ],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"trail: {payload['status']} events={len(events)} invalid={invalid}")
    return 0 if not invalid and payload['status'] != 'blocked' else 1


if __name__ == "__main__":
    raise SystemExit(main())
