#!/usr/bin/env python3
"""Run project-level Tenetora guardrail checks."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from path_security import validate_existing_project_path

EXCLUDED_DIRS = {
    ".git",
    ".gradle",
    ".tenetora/changes/archive",
    ".idea",
    ".venv",
    ".vscode",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
    "venv",
}
# Only scan governance content that is intended to be shared with a project.
# Tenetora installations may also live below the governance root (for example
# when the root is a user's home directory); their runtime, cache, and state
# trees must not become part of the project's secret/path scan.
SHAREABLE_HARNESS_DIRS = (
    "agents",
    "automation",
    "changes",
    "docs",
    "guardrails",
    "rules",
    "skills",
    "templates",
    "wiki",
    "workflows",
)
SECRET_PATTERN = re.compile(
    r"(glpat-[A-Za-z0-9._-]+|ghp_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|BEGIN [A-Z ]*PRIVATE KEY|"
    r"\b[A-Z0-9_]*(TOKEN|SECRET|PASSWORD)\b\s*[:=]\s*['\"]?[^'\"\s${}]+)",
    re.I,
)
LOCAL_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.-])(/Users/[^\s\"']+|/home/[^\s\"']+|[A-Za-z]:\\Users\\[^\s\"']+)"
)
STALE_PATTERNS = [
    re.compile(r"<card|</span>|</p>|h[e]rness|READ[E]ME"),
    re.compile(r"(?im)^\s*(?:[-*]\s*)?(?:TODO|TBD)(?:\s*:|\s*$)"),
    re.compile(r"(?i)\b(?:TODO|TBD)\b\s*(?:here|later|placeholder|待补充|待定|补充|完善)"),
]
INLINE_VERSION_PATTERN = re.compile(r"""["'][A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+:[0-9][^"']*["']""")


def existing_project_path(raw: str) -> Path:
    try:
        return validate_existing_project_path(raw)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(
        prog="tenetora run-all",
        description="Run .tenetora guardrail checks for a project.",
        add_help=False,
    )
    command_parser.add_argument("--help", action="help", help="Show this help message and exit.")
    command_parser.add_argument(
        "-p",
        "--path",
        default=".",
        type=existing_project_path,
        metavar="<project-dir>",
        help="Project directory to check. Defaults to the current directory.",
    )
    command_parser.add_argument(
        "--runner",
        choices=("auto", "shell", "python"),
        default="auto",
        help="Guardrail runner. auto prefers generated shell checks when bash is available, otherwise uses Python.",
    )
    return command_parser


def is_excluded(root: Path, path: Path) -> bool:
    rel = path.relative_to(root).as_posix()
    parts = path.relative_to(root).parts
    if any(part in EXCLUDED_DIRS for part in parts):
        return True
    return any(rel == item or rel.startswith(item + "/") for item in EXCLUDED_DIRS)


def iter_text_files(root: Path, base: Path) -> list[Path]:
    if not base.exists():
        return []
    files: list[Path] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or is_excluded(root, path):
            continue
        try:
            sample = path.read_bytes()[:4096]
        except OSError:
            continue
        if b"\x00" in sample:
            continue
        files.append(path)
    return files


def iter_shareable_harness_files(root: Path) -> list[Path]:
    """Return only project-owned, shareable governance files."""

    harness = root / ".tenetora"
    if not harness.is_dir():
        return []
    files: list[Path] = [
        path
        for path in sorted(harness.iterdir())
        if path.is_file() and not is_excluded(root, path)
    ]
    for relative_dir in SHAREABLE_HARNESS_DIRS:
        files.extend(iter_text_files(root, harness / relative_dir))
    return files


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def print_match(path: Path, line_no: int, line: str) -> None:
    print(f"{path}:{line_no}:{line.rstrip()}")


def check_secret_scan(root: Path) -> bool:
    failed = False
    for path in iter_shareable_harness_files(root):
        if "/guardrails/checks/" in path.as_posix():
            continue
        for line_no, line in enumerate(read_text(path).splitlines(), start=1):
            if SECRET_PATTERN.search(line):
                print_match(path, line_no, line)
                failed = True
    if failed:
        print("ERROR: possible secret or credential pattern found in .tenetora", file=sys.stderr)
        print(
            "FIX: remove the value, rotate the credential if it was real, and store it outside tracked harness files",
            file=sys.stderr,
        )
        print("SEE: .tenetora/rules/security.md", file=sys.stderr)
        return False
    print("secret-scan passed")
    return True


def check_local_path_scan(root: Path) -> bool:
    failed = False
    for path in iter_shareable_harness_files(root):
        if "/guardrails/checks/" in path.as_posix():
            continue
        for line_no, line in enumerate(read_text(path).splitlines(), start=1):
            if LOCAL_PATH_PATTERN.search(line):
                print_match(path, line_no, line)
                failed = True
    if failed:
        print("ERROR: local absolute path found in .tenetora", file=sys.stderr)
        print("FIX: replace local-only paths with repository-relative paths or documented environment variables", file=sys.stderr)
        print("SEE: .tenetora/rules/project.md", file=sys.stderr)
        return False
    print("local-path-scan passed")
    return True


def check_stale_doc_scan(root: Path) -> bool:
    failed = False
    for rel in ("docs", "wiki", "rules", "workflows"):
        for path in iter_text_files(root, root / ".tenetora" / rel):
            for line_no, line in enumerate(read_text(path).splitlines(), start=1):
                if any(pattern.search(line) for pattern in STALE_PATTERNS):
                    print_match(path, line_no, line)
                    failed = True
    if failed:
        print("ERROR: stale placeholder or copied markup pattern found in .tenetora", file=sys.stderr)
        print("FIX: replace placeholders with current project guidance, or mark unknowns as Unknown:/Inference:", file=sys.stderr)
        print("SEE: .tenetora/rules/documentation.md", file=sys.stderr)
        return False
    print("stale-doc-scan passed")
    return True


def current_evidence(root: Path) -> dict[str, object]:
    pointer_path = root / ".tenetora" / "state" / "current-evidence.json"
    if pointer_path.is_file():
        try:
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            current = pointer.get("current", {}) if isinstance(pointer, dict) else {}
            evidence_rel = current.get("evidence") if isinstance(current, dict) else None
            if isinstance(evidence_rel, str):
                evidence_path = root / evidence_rel
                if evidence_path.is_file():
                    return json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    candidates = sorted((root / ".tenetora" / "changes").glob("*-extraction-evidence.json"))
    if not candidates:
        return {}
    try:
        return json.loads(candidates[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def drift_pattern(conflicts: list[str]) -> re.Pattern[str]:
    escaped = "|".join(re.escape(item) for item in conflicts)
    return re.compile(rf'(^|["/@_.-])({escaped})(["/@_.-]|$)', re.I)


def check_test_framework_drift(root: Path, evidence: dict[str, object]) -> bool:
    rules = [
        rule
        for rule in evidence.get("guardrail_rules", [])
        if isinstance(rule, dict) and rule.get("type") == "test-framework-drift"
    ]
    if not rules:
        return True
    failed = False
    for rule in rules:
        rel = str(rule.get("source", ""))
        conflicts = [str(item) for item in rule.get("conflicts", []) if str(item)]
        if not rel or not conflicts:
            continue
        path = root / rel
        if not path.is_file():
            continue
        pattern = drift_pattern(conflicts)
        for line_no, line in enumerate(read_text(path).splitlines(), start=1):
            if pattern.search(line):
                print_match(path, line_no, line)
                print(f"ERROR: test framework drift detected in {rel}", file=sys.stderr)
                print(
                    "FIX: keep tests aligned with the expected framework here, or update .tenetora evidence/rules if the project intentionally migrated frameworks",
                    file=sys.stderr,
                )
                print("SEE: .tenetora/guardrails/quality-gates.md", file=sys.stderr)
                failed = True
    if failed:
        return False
    print("test-framework-drift-scan passed")
    return True


def iter_build_gradle_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name not in {"build.gradle", "build.gradle.kts"}:
            continue
        if is_excluded(root, path):
            continue
        files.append(path)
    return files


def check_dependency_version_drift(root: Path, evidence: dict[str, object]) -> bool:
    rules = [
        rule
        for rule in evidence.get("guardrail_rules", [])
        if isinstance(rule, dict) and rule.get("type") == "dependency-version-drift"
    ]
    if not rules:
        return True
    catalog_sources = [str(rule.get("source", "")) for rule in rules if str(rule.get("source", ""))]
    if not any((root / source).is_file() for source in catalog_sources):
        return True
    failed = False
    for path in iter_build_gradle_files(root):
        for line_no, line in enumerate(read_text(path).splitlines(), start=1):
            if INLINE_VERSION_PATTERN.search(line):
                rel = path.relative_to(root).as_posix()
                print_match(path, line_no, line)
                print(f"ERROR: dependency version drift detected in {rel}", file=sys.stderr)
                print(
                    "FIX: move inline Gradle dependency versions into the version catalog or update .tenetora evidence/rules if this module intentionally opts out",
                    file=sys.stderr,
                )
                print("SEE: .tenetora/guardrails/quality-gates.md", file=sys.stderr)
                failed = True
    if failed:
        return False
    print("dependency-version-drift-scan passed")
    return True


def custom_hook_command(hook: Path) -> list[str] | None:
    suffix = hook.suffix.lower()
    if suffix in {".md", ".txt", ".json", ".yaml", ".yml"}:
        return None
    if suffix == ".py":
        return [sys.executable, str(hook)]
    if suffix == ".sh":
        bash = shutil.which("bash")
        if bash:
            return [bash, str(hook)]
        return None
    if os.access(hook, os.X_OK):
        return [str(hook)]
    return None


def run_custom_hooks(root: Path) -> int:
    custom_dir = root / ".tenetora" / "guardrails" / "custom"
    if not custom_dir.is_dir():
        return 0
    for hook in sorted(custom_dir.iterdir()):
        if not hook.is_file():
            continue
        command = custom_hook_command(hook)
        if command is None:
            continue
        rel = hook.relative_to(root).as_posix()
        print(f"[tenetora] custom hook {rel}")
        env = os.environ.copy()
        env["TENETORA_ROOT"] = str(root)
        completed = subprocess.run(command, cwd=root, env=env, check=False)
        if completed.returncode != 0:
            print(f"ERROR: custom guardrail hook failed: {rel}", file=sys.stderr)
            print("FIX: repair the hook or move it out of .tenetora/guardrails/custom", file=sys.stderr)
            print("SEE: .tenetora/guardrails/quality-gates.md", file=sys.stderr)
            return int(completed.returncode)
    return 0


def run_python_checks(root: Path) -> int:
    if not (root / ".tenetora").is_dir():
        print(
            ".tenetora/guardrails/checks/run-all.sh is missing. "
            "Ask your AI to use Tenetora to initialize or update this project's governance.",
            file=sys.stderr,
        )
        return 2
    evidence = current_evidence(root)
    checks = [
        check_secret_scan(root),
        check_local_path_scan(root),
        check_stale_doc_scan(root),
        check_test_framework_drift(root, evidence),
        check_dependency_version_drift(root, evidence),
    ]
    if not all(checks):
        return 1
    custom_result = run_custom_hooks(root)
    if custom_result != 0:
        return custom_result
    print("Tenetora guardrail checks passed")
    return 0


def run_shell_checks(root: Path) -> int:
    run_all = root / ".tenetora" / "guardrails" / "checks" / "run-all.sh"
    if not run_all.exists():
        print(
            ".tenetora/guardrails/checks/run-all.sh is missing. "
            "Ask your AI to use Tenetora to initialize or update this project's governance.",
            file=sys.stderr,
        )
        return 2
    if not run_all.is_file():
        print(".tenetora/guardrails/checks/run-all.sh is not a file.", file=sys.stderr)
        return 2
    bash = shutil.which("bash")
    if not bash:
        print("bash is not available; use --runner python for cross-platform guardrails.", file=sys.stderr)
        return 2
    completed = subprocess.run([bash, str(run_all)], cwd=root, check=False)
    return int(completed.returncode)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root: Path = args.path
    if args.runner == "python":
        return run_python_checks(root)
    if args.runner == "shell":
        return run_shell_checks(root)
    if shutil.which("bash") and (root / ".tenetora" / "guardrails" / "checks" / "run-all.sh").is_file():
        return run_shell_checks(root)
    return run_python_checks(root)


if __name__ == "__main__":
    raise SystemExit(main())
