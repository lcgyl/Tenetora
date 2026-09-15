#!/usr/bin/env python3
"""Create reviewable CI integration files for Tenetora."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from path_security import validate_existing_project_path

def existing_project_path(raw: str) -> Path:
    try:
        return validate_existing_project_path(raw)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(
        prog="tenetora setup-ci",
        description="Generate reviewable CI files that run Tenetora validation and guardrails.",
        add_help=False,
    )
    command_parser.add_argument("--help", action="help", help="Show this help message and exit.")
    command_parser.add_argument(
        "-p",
        "--path",
        default=".",
        type=existing_project_path,
        metavar="<project-dir>",
        help="Project directory. Defaults to the current directory.",
    )
    command_parser.add_argument(
        "--provider",
        choices=("github", "gitlab", "pre-commit", "all"),
        default="all",
        help="CI provider or local hook template to generate.",
    )
    command_parser.add_argument("--write", action="store_true", help="Write files. Without this flag, only preview actions.")
    command_parser.add_argument("--force", action="store_true", help="Overwrite files that already exist.")
    return command_parser


def github_actions_template() -> str:
    return """name: Tenetora

on:
  pull_request:
  push:

jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install Tenetora
        run: curl -fsSL https://raw.githubusercontent.com/lcgyl/Tenetora/main/install.sh | bash -s -- --lang en
      - name: Validate .tenetora
        run: |
          export PATH="$HOME/.tenetora/bin:$PATH"
          tenetora validate
          tenetora audit --strict --format sarif > tenetora-audit.sarif
          tenetora run-all --runner python
      - name: Verify completion claim
        if: always()
        run: |
          export PATH="$HOME/.tenetora/bin:$PATH"
          tenetora guard --action claim --claim-kind completion --check-proof-only \
            --expected-verification-command "tenetora validate && tenetora audit --strict --format sarif && tenetora run-all --runner python" \
            --json > tenetora-completion-claim.json
      - name: Upload SARIF
        uses: github/codeql-action/upload-sarif@v3
        if: always()
        with:
          sarif_file: tenetora-audit.sarif
      - name: Upload Tenetora evidence
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: tenetora-evidence
          path: |
            tenetora-audit.sarif
            tenetora-completion-claim.json
"""


def gitlab_ci_template() -> str:
    return """stages:
  - verify

tenetora_audit:
  stage: verify
  image: python:3.12
  script:
    - curl -fsSL https://raw.githubusercontent.com/lcgyl/Tenetora/main/install.sh | bash -s -- --lang en
    - export PATH="$HOME/.tenetora/bin:$PATH"
    - tenetora validate
    - tenetora audit --strict --format junit > tenetora-audit.xml
    - tenetora run-all --runner python
    - tenetora guard --action claim --claim-kind completion --check-proof-only --expected-verification-command "tenetora validate && tenetora audit --strict --format junit && tenetora run-all --runner python" --json > tenetora-completion-claim.json
  artifacts:
    when: always
    reports:
      junit: tenetora-audit.xml
    paths:
      - tenetora-audit.xml
      - tenetora-completion-claim.json
"""


def pre_commit_template() -> str:
    return """repos:
  - repo: local
    hooks:
      - id: tenetora-commit-guard
        name: Tenetora commit guard
        entry: tenetora guard --action commit
        language: system
        pass_filenames: false
        stages: [pre-commit]
      - id: tenetora-commit-message
        name: Tenetora detailed commit message guard
        entry: tenetora guard --action commit --operation commit --commit-message-file
        language: system
        stages: [commit-msg]
"""


def targets(root: Path, provider: str) -> list[tuple[str, Path, str]]:
    selected = ["github", "gitlab", "pre-commit"] if provider == "all" else [provider]
    items: list[tuple[str, Path, str]] = []
    for item in selected:
        if item == "github":
            canonical = root / ".github" / "workflows" / "tenetora.yml"
            legacy = root / ".github" / "workflows" / "agent-harness.yml"
            items.append((item, legacy if legacy.exists() and not canonical.exists() else canonical, github_actions_template()))
        elif item == "gitlab":
            canonical = root / ".gitlab-ci-tenetora.yml"
            legacy = root / ".gitlab-ci-agent-harness.yml"
            items.append((item, legacy if legacy.exists() and not canonical.exists() else canonical, gitlab_ci_template()))
        elif item == "pre-commit":
            default_path = root / ".pre-commit-config.yaml"
            canonical = root / ".pre-commit-config.tenetora.yaml"
            legacy = root / ".pre-commit-config.agent-harness.yaml"
            path = default_path if not default_path.exists() else legacy if legacy.exists() and not canonical.exists() else canonical
            items.append((item, path, pre_commit_template()))
    return items


def write_target(path: Path, text: str, write: bool, force: bool) -> str:
    if path.exists() and not force:
        return f"skip existing {path} (use --force to overwrite)"
    if not write:
        prefix = "would overwrite" if path.exists() else "would write"
        return f"{prefix} {path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return f"wrote {path}"


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    print("# Tenetora CI Setup")
    print()
    print(f"Root: {args.path}")
    print(f"Provider: {args.provider}")
    print(f"Mode: {'write' if args.write else 'dry-run'}")
    print()
    for provider, path, text in targets(args.path, args.provider):
        print(f"- {provider}: {write_target(path, text, args.write, args.force)}")
    print()
    if args.provider in {"pre-commit", "all"}:
        print(
            "Pre-commit visibility: after reviewing the config, run "
            "`pre-commit install --hook-type pre-commit --hook-type commit-msg` so staged content and detailed commit messages are enforced."
        )
        print()
    print("Next: review generated files, then run tenetora validate && tenetora audit --strict && tenetora run-all.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
