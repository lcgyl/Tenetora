#!/usr/bin/env python3
"""Inspect and maintain the machine-local Tenetora installation registry."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

from installation_registry import (
    DEFAULT_DISCOVERY_CANDIDATES,
    DEFAULT_DISCOVERY_DEPTH,
    AUTO_DISCOVERY_CANDIDATES,
    AUTO_DISCOVERY_DEPTH,
    RegistryError,
    SUPPORTED_TOOLS,
    detect_project_surfaces,
    discover_project_candidates,
    auto_discovery_roots,
    governance_registration_for_classification,
    effective_global_surfaces,
    forget_global_surfaces,
    ignored_global_tools,
    legacy_global_surface_evidence,
    load_registry,
    prune_projects,
    registered_global_surfaces,
    registry_path,
    upsert_project,
    registry_project_expired,
)


def migration_module() -> object:
    candidates = (
        SCRIPT_DIRECTORY / "migrate_harness.py",
        SCRIPT_DIRECTORY.parent / "skills" / "tenetora" / "scripts" / "migrate_harness.py",
    )
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        raise RegistryError("Tenetora governance migration classifier is unavailable")
    spec = importlib.util.spec_from_file_location("tenetora_installations_migration", path)
    if spec is None or spec.loader is None:
        raise RegistryError(f"cannot load governance migration classifier: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def governance_registration(project: Path, module: object) -> dict[str, object] | None:
    classification = module.classify_project(project)
    status = str(classification.status)
    return governance_registration_for_classification(
        status,
        canonical_present=(project / ".tenetora").is_dir() and not (project / ".tenetora").is_symlink(),
        source_fingerprint=getattr(classification, "source_fingerprint", None),
    )


def safe_governance_directory(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="tenetora installations")
    command.add_argument("--json", action="store_true", help="Print a machine-readable result.")
    subcommands = command.add_subparsers(dest="operation", required=True)

    list_command = subcommands.add_parser("list", help="List registered project installations.")
    list_command.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Print a machine-readable result.")

    discover = subcommands.add_parser("discover", help="Discover existing project installations below bounded roots.")
    discover.add_argument("--root", action="append", default=[], type=Path, help="Root to scan; repeat as needed.")
    discover.add_argument("--auto", action="store_true", help="Use bounded machine and user project roots automatically.")
    discover.add_argument("--max-depth", type=int, default=DEFAULT_DISCOVERY_DEPTH)
    discover.add_argument("--max-candidates", type=int, default=DEFAULT_DISCOVERY_CANDIDATES)
    discover.add_argument("--dry-run", action="store_true", help="Report discoveries without updating the registry.")
    discover.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Print a machine-readable result.")

    prune = subcommands.add_parser("prune", help="Remove stale or selected registry entries without deleting project files.")
    prune.add_argument("--path", action="append", type=Path, default=[], help="Registered project path to remove.")
    prune.add_argument("--stale", action="store_true", help="Remove entries whose project or Tenetora surfaces are gone.")
    prune.add_argument("--expired", action="store_true", help="Remove stale entries unseen for the bounded hygiene window.")
    prune.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Print a machine-readable result.")

    forget_global = subcommands.add_parser(
        "forget-global",
        help="Stop restoring selected global tools during scope-less upgrades.",
    )
    forget_global.add_argument(
        "--tools",
        required=True,
        help="Comma-separated tools or all.",
    )
    forget_global.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Print a machine-readable result.")
    return command


def list_payload() -> dict[str, object]:
    payload = load_registry()
    registered = registered_global_surfaces()
    ignored = ignored_global_tools()
    legacy = {
        tool: scopes
        for tool, scopes in legacy_global_surface_evidence().items()
        if tool not in ignored and tool not in registered
    }
    projects: list[dict[str, object]] = []
    for item in payload["projects"]:
        project = Path(str(item["path"]))
        current = detect_project_surfaces(project) if project.is_dir() and not project.is_symlink() else {}
        governance = item.get("governance") if isinstance(item.get("governance"), dict) else None
        governance_exists = bool(
            governance
            and project.is_dir()
            and not project.is_symlink()
            and safe_governance_directory(project / str(governance.get("layout")))
        )
        entry = dict(item)
        entry["exists"] = project.is_dir() and not project.is_symlink()
        entry["current_surfaces"] = current
        entry["governance_exists"] = governance_exists
        entry["stale"] = not bool(current) and not governance_exists
        entry["expired"] = bool(entry["stale"] and registry_project_expired(item))
        projects.append(entry)
    return {
        "version": payload["version"],
        "registry": str(registry_path()),
        "global": {
            "registered_surfaces": registered,
            "recovered_legacy_surfaces": legacy,
            "effective_surfaces": effective_global_surfaces(),
            "ignored_tools": sorted(ignored),
        },
        "project_count": len(projects),
        "projects": projects,
    }


def selected_tools(raw: str) -> list[str]:
    requested = [part.strip() for part in raw.split(",") if part.strip()]
    if "all" in requested:
        return list(SUPPORTED_TOOLS)
    invalid = sorted(set(requested) - set(SUPPORTED_TOOLS))
    if not requested or invalid:
        raise RegistryError(f"invalid global tools: {', '.join(invalid) if invalid else raw}")
    return sorted(set(requested))


def forget_global_payload(args: argparse.Namespace) -> dict[str, object]:
    tools = selected_tools(args.tools)
    forget_global_surfaces(tools)
    return {
        "registry": str(registry_path()),
        "forgotten_count": len(tools),
        "tools": tools,
        "ignored_tools": sorted(ignored_global_tools()),
    }


def discover_payload(args: argparse.Namespace) -> dict[str, object]:
    roots = list(args.root)
    if args.auto:
        roots.extend(auto_discovery_roots(include_home=True))
    if not roots:
        raise RegistryError("discover requires --root or --auto")
    roots = sorted({path.expanduser() for path in roots})
    candidates = discover_project_candidates(
        roots,
        max_depth=AUTO_DISCOVERY_DEPTH if args.auto else args.max_depth,
        max_candidates=AUTO_DISCOVERY_CANDIDATES if args.auto else args.max_candidates,
    )
    classifier = migration_module()
    discoveries: list[dict[str, object]] = []
    ignored_foreign = 0
    for project in candidates:
        surfaces = detect_project_surfaces(project)
        governance = governance_registration(project, classifier)
        if not surfaces and governance is None:
            ignored_foreign += 1
            continue
        if not args.dry_run:
            upsert_project(project, surfaces, governance=governance)
        discoveries.append({"path": str(project), "surfaces": surfaces, "governance": governance})
    return {
        "registry": str(registry_path()),
        "dry_run": bool(args.dry_run),
        "mode": "auto" if args.auto else "explicit",
        "roots": [str(root) for root in roots],
        "discovered_count": len(discoveries),
        "governance_only_count": sum(
            not item["surfaces"] and isinstance(item["governance"], dict)
            for item in discoveries
        ),
        "needs_review_count": sum(
            isinstance(item["governance"], dict) and item["governance"].get("state") == "needs-review"
            for item in discoveries
        ),
        "ignored_foreign_count": ignored_foreign,
        "projects": discoveries,
    }


def prune_payload(args: argparse.Namespace) -> dict[str, object]:
    selected = list(args.path)
    registry = load_registry()
    if args.stale or args.expired:
        for item in registry["projects"]:
            project = Path(str(item["path"]))
            governance = item.get("governance") if isinstance(item.get("governance"), dict) else None
            governance_exists = bool(governance and safe_governance_directory(project / str(governance.get("layout"))))
            stale = (
                not project.is_dir()
                or project.is_symlink()
                or not detect_project_surfaces(project) and not governance_exists
            )
            if (args.stale and stale) or (args.expired and stale and registry_project_expired(item)):
                selected.append(project)
    for path in selected:
        expanded = path.expanduser()
        if expanded.is_symlink():
            raise RegistryError(f"project prune path is a symbolic link: {expanded}")
    unique = sorted({path.expanduser().resolve(strict=False) for path in selected})
    if not unique:
        raise RegistryError("select --path or --stale before pruning the installation registry")
    registered = {str(item["path"]) for item in registry["projects"]}
    matched = [path for path in unique if str(path) in registered]
    if matched:
        prune_projects(matched)
    return {
        "registry": str(registry_path()),
        "requested_count": len(unique),
        "pruned_count": len(matched),
        "paths": [str(path) for path in matched],
    }


def render(payload: dict[str, object], operation: str) -> str:
    chinese = os.environ.get("TENETORA_LANG", "").strip().lower() in {"zh", "zh-cn", "zh-hans", "chinese", "中文"}
    lines = [
        f"Tenetora 安装登记：{operation}" if chinese else f"Tenetora installations: {operation}",
        f"{'登记文件' if chinese else 'Registry'}: {payload.get('registry')}",
    ]
    global_state = payload.get("global")
    if isinstance(global_state, dict):
        effective = global_state.get("effective_surfaces") or {}
        ignored = global_state.get("ignored_tools") or []
        lines.append(f"- global [{'受管' if chinese else 'managed'}] {','.join(sorted(effective)) or ('无' if chinese else 'none')}")
        if ignored:
            lines.append(f"- global [{'已忽略' if chinese else 'ignored'}] {','.join(ignored)}")
    projects = payload.get("projects")
    if isinstance(projects, list):
        for item in projects:
            if not isinstance(item, dict):
                continue
            status = (
                "过期" if item.get("expired") else "过期" if item.get("stale") else "活动"
            ) if chinese else (
                "expired" if item.get("expired") else "stale" if item.get("stale") else "active"
            )
            surfaces = item.get("current_surfaces") or item.get("surfaces") or {}
            lines.append(f"- {item.get('path')} [{status}] {','.join(sorted(surfaces))}")
    count_key = (
        "project_count"
        if operation == "list"
        else "discovered_count"
        if operation == "discover"
        else "forgotten_count"
        if operation == "forget-global"
        else "pruned_count"
    )
    lines.append(f"{'数量' if chinese else 'Count'}: {payload.get(count_key, 0)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.operation == "list":
            payload = list_payload()
        elif args.operation == "discover":
            payload = discover_payload(args)
        elif args.operation == "prune":
            payload = prune_payload(args)
        else:
            payload = forget_global_payload(args)
    except (OSError, RegistryError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else render(payload, args.operation))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
