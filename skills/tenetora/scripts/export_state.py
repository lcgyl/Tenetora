#!/usr/bin/env python3
"""Export a reviewable compatibility entrypoint from the canonical harness."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import uuid
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from governance_trail import append_event  # noqa: E402
from harness_io import atomic_write_text  # noqa: E402
from init_harness import agents_entrypoint_adapter  # noqa: E402
from path_security import harness_missing_message, validate_existing_project_path  # noqa: E402


START_RE = re.compile(r"(?m)^<!-- TENETORA_ENTRYPOINT_START name=agents hash=[0-9a-f]{12} -->\n")
END_RE = re.compile(r"(?m)^<!-- TENETORA_ENTRYPOINT_END name=agents -->\n?")
ROOT_ENTRY = Path("AGENTS.md")


class ExportError(RuntimeError):
    pass


def run_id() -> str:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    return "export-" + now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


def managed_region(text: str) -> tuple[int, int] | None:
    start = START_RE.search(text)
    end = END_RE.search(text, start.end() if start else 0)
    if not start or not end or end.start() < start.end():
        return None
    return start.start(), end.end()


def expected_text() -> str:
    return agents_entrypoint_adapter().rstrip() + "\n"


def replace_region(text: str, expected: str) -> str:
    region = managed_region(text)
    if region is None:
        raise ExportError("AGENTS.md has no Tenetora-managed agents block")
    start, end = region
    return text[:start] + expected + text[end:]


def relative_backup(root: Path, source: Path, identifier: str) -> Path:
    return root / ".tenetora" / "changes" / "backups" / identifier / "entrypoints" / source.name


def project_root(raw: Path | str) -> Path:
    try:
        return validate_existing_project_path(raw, label="export project")
    except RuntimeError as error:
        raise ExportError(str(error)) from error


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Check or export a Tenetora compatibility entrypoint.")
    command.add_argument("format", choices=("agents-md",))
    command.add_argument("--path", default=".")
    command.add_argument("--check", action="store_true")
    command.add_argument("--write", action="store_true")
    command.add_argument("--adopt", action="store_true", help="Adopt an unowned existing file after backing it up.")
    command.add_argument("--json", action="store_true")
    return command


def export_agents(args: argparse.Namespace) -> dict[str, object]:
    root = project_root(args.path)
    if not (root / ".tenetora").is_dir():
        raise ExportError(harness_missing_message(root))
    target = root / ROOT_ENTRY
    expected = expected_text()
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    region = managed_region(existing) if target.exists() else None
    if region is not None:
        candidate = replace_region(existing, expected)
        owned = True
    elif target.exists():
        candidate = existing.rstrip() + "\n\n" + expected
        owned = False
    else:
        candidate = expected
        owned = True
    changed = candidate != existing
    result: dict[str, object] = {
        "format": "agents-md",
        "path": ROOT_ENTRY.as_posix(),
        "managed": owned,
        "changed": changed,
        "mode": "check" if args.check else "write" if args.write else "preview",
    }
    if args.check:
        if not target.exists():
            result["status"] = "missing"
            raise ExportError("AGENTS.md is missing; run with --write to create it")
        if region is None:
            result["status"] = "unmanaged"
            raise ExportError("AGENTS.md exists but is not Tenetora-managed; review it or use --write --adopt")
        if changed:
            result["status"] = "drift"
            raise ExportError("AGENTS.md managed block has drifted")
        result["status"] = "clean"
        return result
    if args.write:
        if target.exists() and region is None and not args.adopt:
            raise ExportError("AGENTS.md is unowned; --write requires explicit --adopt")
        adopted = target.exists() and region is None
        identifier = run_id()
        if target.exists() and changed:
            backup = relative_backup(root, target, identifier)
            backup.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(backup, existing)
            result["backup"] = backup.relative_to(root).as_posix()
        atomic_write_text(target, candidate)
        append_event(
            root,
            {
                "type": "compatibility-export",
                "action": "write",
                "status": "pass",
                "format": "agents-md",
                "managed_block": True,
                "adopted": adopted,
            },
        )
        result["status"] = "written"
        return result
    result["status"] = "would-write" if changed else "clean"
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.check and not args.write:
        args.check = True
    try:
        result = export_agents(args)
    except ExportError as error:
        payload = {"status": "error", "error": str(error)}
        print(json.dumps(payload, ensure_ascii=False) if args.json else f"Export error: {error}")
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True) if args.json else "\n".join(f"{key}: {value}" for key, value in result.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
