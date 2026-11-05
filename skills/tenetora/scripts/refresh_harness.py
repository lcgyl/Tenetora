#!/usr/bin/env python3
"""Refresh an existing .tenetora with reviewable update artifacts."""

from __future__ import annotations

import argparse
import difflib
import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from path_security import (
    validate_existing_project_path,
    validate_unredirected_file_path,
)


def defaults_preview(root: Path, requested_profile: str | None) -> dict[str, object]:
    """Render shipped candidates in memory; never call the writing init path."""
    from governance_paths import (
        default_pack_version,
        pending_defaults_review,
        read_json_object,
    )
    from init_harness import (
        DEFAULTS_DIR,
        MINIMAL_DEFAULT_TARGETS,
        classify_rule_origin,
        default_rule_payloads,
        load_default_items,
        resolve_profile,
    )

    if not (root / '.tenetora').is_dir():
        raise ValueError('Project governance is missing; preview requires an existing .tenetora.')
    manifest_path = validate_unredirected_file_path(root / '.tenetora/manifest.json', label='governance manifest')
    manifest = read_json_object(manifest_path)
    if manifest is None and manifest_path.is_file():
        raise ValueError('Existing governance manifest is unreadable or is not a valid JSON object.')
    manifest = manifest or {}
    version = default_pack_version(SCRIPT_DIR)
    profile = resolve_profile(root, requested_profile)
    defaults = default_rule_payloads()
    items = []
    for item in load_default_items():
        relative = item['target']
        if profile == 'minimal' and relative not in MINIMAL_DEFAULT_TARGETS:
            continue
        if '..' in Path(relative).parts or Path(relative).is_absolute():
            raise ValueError('Invalid default target path.')
        path = validate_unredirected_file_path(root / relative, label='default preview target')
        candidate = (DEFAULTS_DIR / item['source']).read_text(encoding='utf-8')
        current = path.read_text(encoding='utf-8') if path.is_file() else ''
        origin = classify_rule_origin(relative, current, defaults)
        different = not path.is_file() or current != candidate
        items.append({'path': relative, 'status': 'missing' if not path.is_file() else 'changed' if different else 'unchanged',
            'owner': origin['owner'], 'origin': origin['origin'],
            'conflict_status': 'needs-semantic-review' if path.is_file() and different else 'none',
            'diff': ''.join(difflib.unified_diff(current.splitlines(keepends=True), candidate.splitlines(keepends=True),
                fromfile=relative, tofile=relative + ' (shipped candidate)')) if different else ''})
    proposed = {item['path'] for item in items}
    preserved = []
    for path in sorted((root / '.tenetora/rules').rglob('*.md')):
        validate_unredirected_file_path(path, label='project rule')
        if path.relative_to(root).as_posix() not in proposed:
            preserved.append(path.relative_to(root).as_posix())
    return {'status': 'pass', 'read_only': True, 'applied': False,
        'project_defaults_version': manifest.get('defaults_version'), 'runtime_defaults_version': version,
        'review_status': 'audit-pending' if pending_defaults_review(manifest, version) else 'not-pending',
        'profile': profile, 'items': items, 'preserved_project_rules': preserved,
        'assurance': 'Text differences and ownership hints; conflicts require semantic review. No approval or update is recorded.'}


def existing_project_path(raw: str) -> Path:
    try:
        return validate_existing_project_path(raw)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def gitignore_mode(root: Path, requested: str) -> str:
    if requested != "auto":
        return requested
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        return "no"
    text = gitignore.read_text(encoding="utf-8", errors="ignore")
    return "yes" if any(line.strip().rstrip("/") == ".tenetora" for line in text.splitlines()) else "no"


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--help", action="help", help="Show this help message and exit.")
    parser.add_argument('--preview', action='store_true', help='Print a fully read-only shipped-default diff; creates no candidates or reports.')
    parser.add_argument('--json', action='store_true', help='Structured output for --preview only.')
    parser.add_argument(
        "-p",
        "--path",
        default=".",
        type=existing_project_path,
        metavar="<project-dir>",
        help="Project directory to refresh. Defaults to the current directory.",
    )
    parser.add_argument(
        "--repository-scope",
        default="ask",
        metavar="{ask,parent,all,<submodule-path>}",
        help=(
            "Repositories to refresh: parent, all, or a comma-separated list of detected Git submodule paths. "
            "ask requires interactive confirmation when submodules are present."
        ),
    )
    parser.add_argument("-t", "--tools", default="auto", help="auto or comma-separated tool list")
    parser.add_argument('--profile', choices=('full', 'minimal'), default=None,
                        help='Preserve the saved profile by default; explicitly select full/minimal without deleting user files.')
    parser.add_argument(
        "--strategy",
        choices=("diff", "backup", "merge", "replace"),
        default="diff",
        help="Refresh strategy. Defaults to diff so existing files are not changed.",
    )
    parser.add_argument(
        "-m",
        "--mode",
        choices=("auto", "scaffold", "extract"),
        default="auto",
        help="Initialization mode passed through to init.",
    )
    parser.add_argument(
        "--migrate",
        choices=("ask", "plan", "ignore", "transfer", "merge"),
        default="plan",
        help="Migration handling passed through to init. Defaults to plan for preview refreshes.",
    )
    parser.add_argument(
        "--entrypoints",
        choices=("plan", "backup", "merge"),
        default="plan",
        help="Entrypoint handling passed through to init. Defaults to plan so entry files are not rewritten.",
    )
    parser.add_argument(
        "--defaults",
        choices=("off", "missing", "suggest"),
        default="missing",
        help="Default pack handling passed through to init. Defaults to filling missing Tenetora defaults.",
    )
    parser.add_argument(
        "--gitignore",
        choices=("auto", "yes", "no"),
        default="auto",
        help="auto preserves the current .gitignore state; yes/no are passed through explicitly.",
    )
    parser.add_argument(
        "--skip-global-migration-scan",
        action="store_true",
        help="Only scan project-level rules and skills for migration candidates. This is the default.",
    )
    parser.add_argument(
        "--include-global-migration-scan",
        action="store_true",
        help="Also scan user-level global skills for migration candidates. Use only when the user explicitly asks.",
    )
    args = parser.parse_args()

    root = args.path
    if args.json and not args.preview:
        parser.error('--json requires --preview')
    if args.preview:
        try:
            payload = defaults_preview(root, args.profile)
        except (OSError, ValueError, RuntimeError) as exc:
            payload = {'status': 'fail', 'code': 'defaults-preview-invalid', 'message': str(exc)}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        elif payload['status'] == 'pass':
            print(f"Defaults preview (read-only): {payload['project_defaults_version']} -> {payload['runtime_defaults_version']}; profile={payload['profile']}; review={payload['review_status']}")
            for item in payload['items']:
                print(f"{item['path']}: {item['status']}; owner={item['owner']}; {item['conflict_status']}")
                if item['diff']:
                    print(item['diff'], end='' if item['diff'].endswith('\n') else '\n')
            print(payload['assurance'])
        else:
            print(payload['message'], file=sys.stderr)
        return 0 if payload['status'] == 'pass' else 1
    gitignore = gitignore_mode(root, args.gitignore)
    init_script = Path(__file__).with_name("init_harness.py")
    command = [
        sys.executable,
        str(init_script),
        "--path",
        str(root),
        "--repository-scope",
        args.repository_scope,
        "--tools",
        args.tools,
        "--write",
        "--gitignore",
        gitignore,
        "--migrate",
        args.migrate,
        "--entrypoints",
        args.entrypoints,
        "--defaults",
        args.defaults,
        "--mode",
        args.mode,
        "--update-strategy",
        args.strategy,
    ]
    if args.profile:
        command.extend(['--profile', args.profile])
    include_global_migrations = args.include_global_migration_scan and not args.skip_global_migration_scan
    if include_global_migrations:
        command.append("--include-global-migration-scan")
    else:
        command.append("--skip-global-migration-scan")

    print("Harness Refresh")
    print(f"Path: {root}")
    global_scan = "--include-global-migration-scan" if include_global_migrations else "--skip-global-migration-scan"
    print(f"Command: tenetora init --write --gitignore {gitignore} --repository-scope {args.repository_scope} --migrate {args.migrate} --entrypoints {args.entrypoints} --defaults {args.defaults} --mode {args.mode} --update-strategy {args.strategy} {global_scan}")
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
