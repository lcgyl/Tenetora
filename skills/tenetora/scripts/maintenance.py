#!/usr/bin/env python3
"""Safely inspect and remove Tenetora-managed releases and installation state."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from path_security import is_redirected_path, remove_unredirected_entry, validate_unredirected_path  # noqa: E402
from release_store import ReleaseStoreError, managed_release, read_release_manifest  # noqa: E402
from release_version import version_key  # noqa: E402


def language_is_zh() -> bool:
    return os.environ.get("TENETORA_LANG", "").lower() in {"zh", "zh-cn", "zh-hans", "中文"}


def text(en: str, zh: str) -> str:
    return zh if language_is_zh() else en


def configured_home() -> Path:
    raw = os.environ.get("TENETORA_HOME")
    if raw:
        return validate_unredirected_path(raw, label="TENETORA_HOME")
    user_home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or str(Path.home())
    return validate_unredirected_path(Path(user_home) / ".tenetora", label="Tenetora home")


def release_entries(home: Path) -> list[tuple[Path, dict[str, object]]]:
    root = home / "releases"
    if not root.exists():
        return []
    if is_redirected_path(root) or not root.is_dir():
        raise ReleaseStoreError("release storage is not a regular managed directory")
    result: list[tuple[Path, dict[str, object]]] = []
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if entry.name.startswith("."):
            continue
        if not entry.is_dir() or is_redirected_path(entry):
            raise ReleaseStoreError(f"refusing to inspect unsafe release entry: {entry.name}")
        if not managed_release(entry, home):
            raise ReleaseStoreError(f"refusing to remove unowned or invalid release: {entry.name}")
        result.append((entry, read_release_manifest(entry)))
    return result


def current_target(home: Path) -> Path | None:
    current = home / "current"
    if not (current.exists() or is_redirected_path(current)):
        return None
    try:
        target = current.resolve(strict=True)
    except OSError as exc:
        raise ReleaseStoreError("current pointer is unreadable") from exc
    return target


def cleanup_plan(home: Path) -> dict[str, object]:
    entries = release_entries(home)
    current = current_target(home)
    stable = [item for item in entries if item[1].get("channel", "stable") == "stable"]
    preview = [item for item in entries if item[1].get("channel", "stable") == "preview"]
    keep: set[Path] = set()
    if current is not None:
        keep.add(current.resolve(strict=False))
    for group in (stable, preview):
        if group:
            keep.add(max(group, key=lambda item: version_key(str(item[1]["version"])))[0].resolve(strict=False))
    remove = [path.resolve(strict=False) for path, _ in entries if path.resolve(strict=False) not in keep]
    return {
        "home": str(home),
        "current": str(current) if current else None,
        "keep": sorted(str(path) for path in keep),
        "remove": sorted(str(path) for path in remove),
    }


def apply_cleanup(plan: dict[str, object]) -> list[str]:
    removed: list[str] = []
    for raw in plan.get("remove", []):
        path = Path(str(raw))
        remove_unredirected_entry(path, label="managed release cleanup")
        removed.append(str(path))
    return removed


def uninstall_plan(home: Path, *, purge: bool) -> dict[str, object]:
    cleanup = cleanup_plan(home)
    targets: list[Path] = []
    for name in ("current", "source", "bin", "releases"):
        path = home / name
        if path.exists() or is_redirected_path(path):
            targets.append(path)
    registry = home / "state" / "installations.json"
    if registry.exists():
        targets.append(registry)
    if purge:
        allowed = {"current", "source", "bin", "releases", "state", "logs", "backups"}
        unexpected = [entry.name for entry in home.iterdir() if entry.name not in allowed]
        if unexpected:
            raise ReleaseStoreError(f"refusing purge with unrecognized home entries: {', '.join(sorted(unexpected))}")
        targets = [home]
    return {"home": str(home), "purge": purge, "remove": sorted(str(path) for path in targets), "cleanup": cleanup}


def apply_uninstall(plan: dict[str, object]) -> list[str]:
    removed: list[str] = []
    for raw in plan.get("remove", []):
        path = Path(str(raw))
        if path.exists() or is_redirected_path(path):
            remove_unredirected_entry(path, label="Tenetora uninstall")
            removed.append(str(path))
    return removed


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Inspect or remove managed Tenetora releases")
    sub = root.add_subparsers(dest="operation", required=True)
    for name in ("cleanup", "uninstall"):
        command = sub.add_parser(name)
        command.add_argument("--home", type=Path, default=None)
        command.add_argument("--apply", action="store_true", help="apply the displayed plan")
        command.add_argument("--yes", action="store_true", help="confirm destructive changes")
        command.add_argument("--purge", action="store_true", help="uninstall: remove the whole managed home")
        command.add_argument("--json", action="store_true")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        home = validate_unredirected_path(args.home or configured_home(), label="Tenetora home")
        plan = cleanup_plan(home) if args.operation == "cleanup" else uninstall_plan(home, purge=args.purge)
        if args.apply and not args.yes:
            raise ReleaseStoreError("destructive maintenance requires --yes")
        removed: list[str] = []
        if args.apply:
            removed = apply_cleanup(plan) if args.operation == "cleanup" else apply_uninstall(plan)
    except (OSError, RuntimeError, ReleaseStoreError, ValueError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False))
        else:
            print(text(f"Maintenance blocked: {exc}", f"维护操作已阻断：{exc}"), file=sys.stderr)
        return 1
    payload = {"status": "applied" if args.apply else "planned", "operation": args.operation, **plan}
    if args.apply:
        payload["removed"] = removed
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(text(f"{args.operation} plan for {home}", f"{args.operation} 计划：{home}"))
        for path in payload.get("remove", []):
            print(text(f"  {'removed' if args.apply else 'would remove'} {path}", f"  {'已删除' if args.apply else '将删除'} {path}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
