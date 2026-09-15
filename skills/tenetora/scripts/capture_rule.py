#!/usr/bin/env python3
"""Capture a confirmed chat rule into .tenetora."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from path_security import validate_existing_project_path

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
    if explicit:
        return explicit.lstrip("/")
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


def append_rule(root: Path, destination: str, rule_text: str, source: str) -> None:
    target = root / destination
    target.parent.mkdir(parents=True, exist_ok=True)
    current = target.read_text(encoding="utf-8") if target.exists() else title_from_destination(destination)
    if current and not current.endswith("\n"):
        current += "\n"
    if "## Captured Rules" not in current:
        current += "\n## Captured Rules\n"
    today = dt.date.today().isoformat()
    block = f"\n- {clean_rule_text(rule_text)}\n  Source: {source}\n  Captured: {today}\n"
    target.write_text(current + block, encoding="utf-8")


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

    matched_path = find_existing_coverage(args.path, rule_text)
    if matched_path:
        payload = result_payload("covered", rule_text, None, matched_path)
    else:
        destination = choose_destination(rule_text, args.destination)
        if args.apply:
            append_rule(args.path, destination, rule_text, args.source)
            payload = result_payload("written", rule_text, destination, None)
        else:
            payload = result_payload("new", rule_text, destination, None)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_markdown(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
