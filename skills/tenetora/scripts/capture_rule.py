#!/usr/bin/env python3
"""Capture a confirmed chat rule into .tenetora."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path, PureWindowsPath

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from path_security import validate_existing_project_path, validate_unredirected_file_path, ensure_unredirected_directory
from harness_io import atomic_write_text
from alignment_state import state_lock

SEARCH_DIRS = [
    ".tenetora/rules",
    ".tenetora/workflows",
    ".tenetora/agents",
    ".cursor/rules",
    ".claude/rules",
]
SEARCH_FILES = [
    "AGENTS.md",
    "AGENTS.override.md",
    "CLAUDE.md",
    "CLAUDE.local.md",
]


def existing_project_path(raw: str) -> Path:
    try:
        return validate_existing_project_path(raw)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def clean_rule_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def markdown_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for rel in SEARCH_FILES:
        path = root / rel
        if path.exists() and path.is_file():
            files.append(path)
    for rel in SEARCH_DIRS:
        directory = root / rel
        if directory.exists() and directory.is_dir():
            files.extend(path for path in directory.rglob("*") if path.is_file() and path.suffix in {"", ".md", ".mdc"})
    return sorted(set(files))


def relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def find_existing_coverage(root: Path, rule_text: str) -> str | None:
    needle = normalize(rule_text)
    if not needle:
        return None
    for path in markdown_files(root):
        try:
            haystack = normalize(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        if needle and needle in haystack:
            return relative(root, path)
    return None


def choose_destination(rule_text: str, explicit: str | None = None) -> str:
    if explicit is not None:
        return explicit
    text = normalize(rule_text)
    if re.search(r"\b(commit|push|branch|merge|rebase|git)\b", text):
        return ".tenetora/rules/git.md"
    if re.search(r"\b(secret|token|password|credential|private key|api key)\b", text):
        return ".tenetora/rules/security.md"
    if re.search(r"\b(test|tests|verify|verification|coverage|ci|lint)\b", text):
        return ".tenetora/rules/testing.md"
    if re.search(r"\b(doc|docs|readme|changelog|comment)\b", text):
        return ".tenetora/rules/documentation.md"
    if re.search(r"\b(build|dependency|dependencies|gradle|maven|npm|pnpm|yarn|pip)\b", text):
        return ".tenetora/rules/build-and-deps.md"
    return ".tenetora/rules/project.md"


def title_from_destination(destination: str) -> str:
    stem = Path(destination).stem.replace("-", " ").title()
    return f"# {stem} Rules\n"


def validate_destination(root: Path, destination: str) -> str:
    if not destination or any(ord(char) < 32 for char in destination) or PureWindowsPath(destination).drive:
        raise ValueError('Rule destination must be a project-relative governance path.')
    normalized = destination.replace('\\', '/')
    path = Path(normalized)
    if (path.is_absolute() or '..' in path.parts or len(path.parts) < 3
        or path.parts[:2] not in {('.tenetora', 'rules'), ('.tenetora', 'workflows'), ('.tenetora', 'agents')}
        or any(part.startswith('.') for part in path.parts[2:])
        or path.suffix.lower() not in {'', '.md', '.mdc'}):
        raise ValueError('Rule destination must stay inside .tenetora/rules, workflows or agents.')
    validate_unredirected_file_path(root / path, label='rule destination')
    return path.as_posix()


def validate_source(source: str) -> str:
    if not source.strip() or len(source) > 200 or not source.isprintable():
        raise ValueError('Rule source must be a non-empty single line of at most 200 characters.')
    return source.strip()


def append_rule(root: Path, destination: str, rule_text: str, source: str) -> bool:
    destination = validate_destination(root, destination)
    source = validate_source(source)
    lock_directory = root / '.tenetora/.cache/rule-capture-locks'
    ensure_unredirected_directory(lock_directory, label='rule capture locks')
    # Conservative normalization may serialize distinct POSIX files, but never
    # lets case/Unicode aliases of the same Windows/macOS target use two locks.
    lock_key = unicodedata.normalize('NFKC', destination).casefold()
    lock = lock_directory / (hashlib.sha256(lock_key.encode('utf-8')).hexdigest() + '.json')
    validate_unredirected_file_path(lock.with_name('.' + lock.name + '.lock'), label='rule capture lock')
    with state_lock(lock):
        target = root / validate_destination(root, destination)
        current = target.read_text(encoding='utf-8') if target.exists() else title_from_destination(destination)
        if normalize(rule_text) and normalize(rule_text) in normalize(current):
            return False
        if current and not current.endswith('\n'):
            current += '\n'
        if '## Captured Rules' not in current:
            current += '\n## Captured Rules\n'
        block = f'\n- {clean_rule_text(rule_text)}\n  Source: {source}\n  Captured: {dt.date.today().isoformat()}\n'
        atomic_write_text(target, current + block)
    return True


def result_payload(status: str, rule_text: str, target: str | None, matched_path: str | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": status,
        "rule": clean_rule_text(rule_text),
    }
    if target is not None:
        payload["target"] = target
    if matched_path is not None:
        payload["matched_path"] = matched_path
    return payload


def print_markdown(payload: dict[str, object]) -> None:
    print("# Rule Capture")
    print()
    print(f"Status: `{payload['status']}`")
    if "matched_path" in payload:
        print(f"Matched: `{payload['matched_path']}`")
    if "target" in payload:
        print(f"Target: `{payload['target']}`")
    print(f"Rule: {payload['rule']}")
    if payload.get('message'):
        print(f"ERROR: {payload['message']}")


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--help", action="help", help="Show this help message and exit.")
    parser.add_argument(
        "-p",
        "--path",
        default=".",
        type=existing_project_path,
        metavar="<project-dir>",
        help="Project directory to update. Defaults to the current directory.",
    )
    parser.add_argument("--text", required=True, help="Candidate rule text confirmed or proposed from the chat.")
    parser.add_argument("--destination", help="Explicit .tenetora destination file. Defaults to keyword-based rules routing.")
    parser.add_argument("--source", default="chat", help="Source label to write with the captured rule.")
    parser.add_argument("--apply", action="store_true", help="Write the rule. Omit to only propose the destination.")
    parser.add_argument("-j", "--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args()

    rule_text = clean_rule_text(args.text)
    if not rule_text:
        parser.error("--text must contain a non-empty rule")

    try:
        destination = validate_destination(args.path, choose_destination(rule_text, args.destination))
        source = validate_source(args.source)
        matched_path = find_existing_coverage(args.path, rule_text)
        if matched_path:
            payload = result_payload('covered', rule_text, None, matched_path)
        elif args.apply:
            written = append_rule(args.path, destination, rule_text, source)
            payload = result_payload('written' if written else 'covered', rule_text, destination, None)
        else:
            payload = result_payload('new', rule_text, destination, None)
    except (OSError, RuntimeError, ValueError) as exc:
        payload = {**result_payload('fail', rule_text, None, None), 'code': 'capture-rule-rejected', 'message': str(exc)}

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_markdown(payload)
    return 1 if payload['status'] == 'fail' else 0


if __name__ == "__main__":
    raise SystemExit(main())
