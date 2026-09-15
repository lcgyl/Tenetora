#!/usr/bin/env python3
"""Run action-triggered Tenetora guard checks."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from governance_trail import append_event  # noqa: E402
import alignment_state  # noqa: E402
import change_impact  # noqa: E402
import execution_context  # noqa: E402
from local_env import load_local_env_paths  # noqa: E402
import repository_units  # noqa: E402
import review_subject  # noqa: E402
import verification_plan  # noqa: E402
from run_all_guardrails import (  # noqa: E402
    LOCAL_PATH_PATTERN,
    check_local_path_scan,
    check_secret_scan,
    run_python_checks,
)
from path_security import harness_missing_message, validate_existing_project_path
from worktree_fingerprint import (  # noqa: E402
    WORKTREE_ENTRIES_VERSION,
    WORKTREE_FINGERPRINT_VERSION,
    git_state_entries,
    git_state_fingerprint,
    normalize_scopes,
)


CLAIM_PROOF_PATTERN = re.compile(r"^ah-claim-[0-9a-f]{32}$")
ALIGNMENT_PROOF_PATTERN = re.compile(r"^ah-align-[0-9a-f]{32}$")
GOAL_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CLAIM_KINDS = ("completion", "partial-verification", "blocked", "failed")
CONVENTIONAL_SUBJECT_PATTERN = re.compile(
    r"^(?:feat|fix|docs|refactor|test|build|ci|chore|perf|style|revert)(?:\([^)]+\))?!?:\s+\S.{7,}$",
    re.IGNORECASE,
)
GENERIC_SUBJECT_PATTERN = re.compile(
    r"^(?:update|updates|change|changes|fix|fixed|misc|wip|提交|更新|修改|调整|修复)(?:\s+.*)?$",
    re.IGNORECASE,
)
MESSAGE_SECTIONS: dict[str, re.Pattern[str]] = {
    "background": re.compile(r"(?im)^\s*(?:#{1,3}\s*)?(?:background|context|why|背景|上下文|原因)\s*[:：]"),
    "changes": re.compile(r"(?im)^\s*(?:#{1,3}\s*)?(?:changes?|implementation|变更|修改内容|实现)\s*[:：]"),
    "verification": re.compile(r"(?im)^\s*(?:#{1,3}\s*)?(?:verification|tests?|验证|测试)\s*[:：]"),
    "risks": re.compile(
        r"(?im)^\s*(?:#{1,3}\s*)?(?:risks?(?:/compatibility)?|compatibility|风险(?:/兼容性)?|兼容性)\s*[:：]"
    ),
}
CJK_CONTENT_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
HIGH_CONFIDENCE_SECRET_PATTERN = re.compile(
    r"(?:glpat-[A-Za-z0-9._-]{16,}|ghp_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD)\b\s*[:=]\s*['\"]?[A-Za-z0-9+/._=-]{16,})",
    re.IGNORECASE,
)
SENSITIVE_KEY_SUFFIXES = {".key", ".p12", ".pfx", ".pem"}
SENSITIVE_FILE_NAMES = {
    "claude.local.md",
    "id_ed25519",
    "id_rsa",
    "settings.local.json",
}
ALLOWED_ENV_FILE_NAMES = {".env.example", ".env.sample", ".env.template"}
DEFAULT_LOCAL_ENV_DIRS = ("local", "config/local")
DEFAULT_LOCAL_ENV_NAMES = {
    "docker-compose.override.yml",
    "local.dev.env",
    "local.test.env",
}


def existing_project_path(raw: str) -> Path:
    try:
        return validate_existing_project_path(raw)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def existing_file(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.exists():
        raise argparse.ArgumentTypeError(f"File does not exist: {path}")
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"Path is not a file: {path}")
    return path.resolve()


def git_common_dir(root: Path) -> Path | None:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        return None
    raw = Path(result.stdout.strip())
    return (raw if raw.is_absolute() else root / raw).resolve(strict=False)


def git_worktree_root(root: Path) -> Path | None:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).expanduser().resolve(strict=False)


def resolve_worktree_governance_root(root: Path, git_root: Path) -> Path | None:
    """Find a governed worktree only when Git proves one shared repository."""

    if (root / ".tenetora").is_dir():
        return root
    common = git_common_dir(git_root)
    if common is None:
        return None
    result = subprocess.run(
        ["git", "-C", str(git_root), "worktree", "list", "--porcelain"],
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        candidate = Path(line.removeprefix("worktree ")).expanduser().resolve(strict=False)
        if not (candidate / ".tenetora").is_dir():
            continue
        if git_common_dir(candidate) == common:
            return candidate
    return None


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(
        prog="tenetora guard",
        description="Run guardrails tied to an action such as commit, verification claim, rule edit, or external input.",
        add_help=False,
    )
    command_parser.add_argument("--help", action="help", help="Show this help message and exit.")
    command_parser.add_argument(
        "--action",
        required=True,
        choices=("commit", "claim", "rules", "external-input", "alignment"),
        help="Action to guard.",
    )
    command_parser.add_argument(
        "-p",
        "--path",
        default=".",
        type=existing_project_path,
        metavar="<project-dir>",
        help="Project directory. Defaults to the current directory.",
    )
    command_parser.add_argument(
        "--git-path",
        default=None,
        type=existing_project_path,
        metavar="<git-dir>",
        help="Git worktree to inspect for --action commit. Defaults to --path; linked worktrees may share governance, while independent nested repositories require their own .tenetora.",
    )
    command_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    command_parser.add_argument("--text", action="append", default=[], help="Untrusted text for --action external-input.")
    command_parser.add_argument("--file", action="append", type=existing_file, default=[], help="Untrusted file for --action external-input.")
    command_parser.add_argument("--stdin", action="store_true", help="Read untrusted text from stdin for --action external-input.")
    command_parser.add_argument("--source", default="external", help="Source label for --action external-input.")
    command_parser.add_argument(
        "--operation",
        choices=("commit", "push"),
        default="commit",
        help="Git operation for --action commit. commit checks staged changes; push does not require a staged diff.",
    )
    message_group = command_parser.add_mutually_exclusive_group()
    message_group.add_argument("--commit-message", help="Commit message text to validate for --action commit.")
    message_group.add_argument(
        "--commit-message-file",
        type=existing_file,
        help="Commit message file to validate for --action commit, such as the commit-msg hook argument.",
    )
    command_parser.add_argument(
        "--require-message",
        action="store_true",
        help="Fail --action commit when no commit message is supplied. Runtime hooks use this mode.",
    )
    command_parser.add_argument("--verification-command", help="Verification command that was run before making a claim.")
    command_parser.add_argument(
        "--verification-status",
        choices=("passed", "failed", "skipped"),
        default="passed",
        help="Observed verification result for --action claim.",
    )
    command_parser.add_argument(
        "--claim-kind",
        choices=CLAIM_KINDS,
        help="Claim semantics: completion, partial-verification, blocked, or failed.",
    )
    command_parser.add_argument(
        "--expected-verification-command",
        help="Expected command for --check-proof-only; it is compared by normalized-command hash.",
    )
    command_parser.add_argument(
        "--verification-scope",
        action="append",
        default=[],
        metavar="<project-relative-path>",
        help="Project-relative path covered by the verification claim; repeatable. Omit for the whole worktree.",
    )
    command_parser.add_argument(
        "--check-proof-only",
        action="store_true",
        help="For --action claim, check whether the latest recorded verification claim has a valid claim proof without creating a new proof.",
    )
    command_parser.add_argument("--goal", help="Goal to bind for --action alignment.")
    command_parser.add_argument("--session-id", help="Conversation/session identity for owner-bound guard state.")
    command_parser.add_argument("--owner-id", help="Owner identity for owner-bound guard state.")
    command_parser.add_argument("--conversation-id", help="Host conversation identity for owner-bound guard state.")
    command_parser.add_argument(
        "--conversation-continuity-id",
        help="Stable host conversation identity shared across model execution attempts.",
    )
    command_parser.add_argument("--tool", help="Host tool identity for owner-bound guard state.")
    command_parser.add_argument("--provider", help="Model provider recorded as claim metadata.")
    command_parser.add_argument("--model", help="Model name recorded as claim metadata.")
    command_parser.add_argument("--execution-attempt-id", help="Execution attempt associated with the claim.")
    command_parser.add_argument(
        "--risk-level",
        choices=("low", "medium", "high"),
        help="Risk level for --action alignment.",
    )
    command_parser.add_argument("--scope", action="append", help="Goal scope item for alignment fingerprint matching.")
    command_parser.add_argument("--non-goal", action="append", help="Goal non-goal item for alignment fingerprint matching.")
    command_parser.add_argument(
        "--acceptance-criterion",
        action="append",
        help="Acceptance criterion for alignment fingerprint matching.",
    )
    return command_parser


def load_prompt_guard_module() -> Any:
    script = SCRIPT_DIR / "prompt_guard.py"
    module_name = "tenetora_prompt_guard_for_guard_action"
    spec = importlib.util.spec_from_file_location(module_name, script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def run_boolean_check(name: str, func: Callable[[], bool]) -> dict[str, Any]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        passed = func()
    return {
        "name": name,
        "status": "pass" if passed else "fail",
        "stdout": stdout.getvalue().strip(),
        "stderr": stderr.getvalue().strip(),
    }


def run_int_check(name: str, func: Callable[[], int]) -> dict[str, Any]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = int(func())
    status = "pass" if code == 0 else "fail"
    return {
        "name": name,
        "status": status,
        "exit_code": code,
        "stdout": stdout.getvalue().strip(),
        "stderr": stderr.getvalue().strip(),
    }


def check_result(name: str, passed: bool, code: str, message: str, **details: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": name,
        "status": "pass" if passed else "fail",
        "code": code,
        "message": message,
    }
    result.update(details)
    return result


def skipped_check(name: str, code: str, message: str) -> dict[str, Any]:
    return {"name": name, "status": "skip", "code": code, "message": message}


def run_git(root: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        text=text,
        encoding="utf-8" if text else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def git_repository_check(root: Path) -> dict[str, Any]:
    result = run_git(root, "rev-parse", "--is-inside-work-tree")
    passed = result.returncode == 0 and result.stdout.strip() == "true"
    return check_result(
        "git-repository",
        passed,
        "git-repository-present" if passed else "git-repository-missing",
        "Git repository detected." if passed else "The commit guard must run inside a Git worktree.",
    )


def git_target_scope_check(root: Path, git_root: Path) -> dict[str, Any]:
    governed = root.resolve(strict=False)
    target = git_root.resolve(strict=False)
    try:
        target.relative_to(governed)
        inside = True
    except ValueError:
        inside = False
    governed_common = git_common_dir(governed)
    target_common = git_common_dir(target)
    same_repository = governed_common is not None and governed_common == target_common
    if same_repository:
        return check_result(
            "git-target-scope",
            True,
            "git-target-governed",
            "The Git target belongs to the governed repository or one of its linked worktrees.",
        )
    if inside:
        return check_result(
            "git-target-scope",
            False,
            "git-target-independent-repository",
            "The Git target is an independent nested repository. Initialize its own .tenetora and run the guard with that repository as --path; parent governance applies only to parent gitlink and cross-repository integration operations.",
        )
    return check_result(
        "git-target-scope",
        False,
        "git-target-outside-governance",
        "The Git target is outside the governed project and belongs to a different repository.",
    )


def staged_paths(root: Path) -> list[str]:
    result = run_git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMRDT", "-z", text=False)
    if result.returncode != 0:
        return []
    return [os.fsdecode(raw) for raw in bytes(result.stdout).split(b"\0") if raw]


def git_head(root: Path) -> str | None:
    result = run_git(root, "rev-parse", "HEAD")
    value = result.stdout.strip()
    return value if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", value) else None


def git_identity(root: Path) -> str | None:
    """Return a redacted stable identity for a Git repository shared by worktrees."""

    common = git_common_dir(root)
    if common is None:
        return None
    return hashlib.sha256(str(common).encode("utf-8", errors="surrogatepass")).hexdigest()


def staged_set_fingerprint(paths: list[str]) -> str:
    normalized = "\0".join(sorted(set(paths)))
    return hashlib.sha256(normalized.encode("utf-8", errors="surrogatepass")).hexdigest()


def staged_set_drift_check(root: Path, git_root: Path, paths: list[str]) -> dict[str, Any]:
    """Warn when the same HEAD was guarded before another agent changed the shared index."""

    current_paths = sorted(set(paths))
    current_head = git_head(git_root)
    current_identity = git_identity(git_root)
    if not current_head or not current_identity:
        return check_result(
            "staged-set-drift",
            True,
            "staged-set-baseline-unavailable",
            "The previous staged-set baseline could not be resolved; commit-time hooks still revalidate the current index.",
        )
    trail_path = root / ".tenetora" / "state" / "governance-trail.json"
    try:
        payload = json.loads(trail_path.read_text(encoding="utf-8")) if trail_path.is_file() else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    events = payload.get("events") if isinstance(payload, dict) else []
    if not isinstance(events, list):
        events = []
    for event in reversed(events):
        if not isinstance(event, dict) or event.get("type") != "guard-action" or event.get("action") != "commit":
            continue
        if event.get("status") != "pass" or event.get("git_identity") != current_identity or event.get("git_head") != current_head:
            continue
        previous = event.get("staged_paths")
        if not isinstance(previous, list) or any(not isinstance(item, str) for item in previous):
            return check_result(
                "staged-set-drift",
                True,
                "staged-set-baseline-invalid",
                "The previous staged-set baseline is invalid; the current staged index will be checked again at commit time.",
            )
        previous_paths = sorted(set(previous))
        added = sorted(set(current_paths) - set(previous_paths))
        removed = sorted(set(previous_paths) - set(current_paths))
        if not added and not removed:
            return check_result(
                "staged-set-drift",
                True,
                "staged-set-unchanged",
                "The staged file set is unchanged since the previous commit guard on this HEAD.",
            )
        return check_result(
            "staged-set-drift",
            True,
            "staged-set-changed",
            "The shared Git index changed since the previous commit guard; review possible files staged by another agent.",
            added_paths=added,
            removed_paths=removed,
            previous_paths=previous_paths,
            current_paths=current_paths,
            attribution="not-provable-from-git-index",
        )
    return check_result(
        "staged-set-drift",
        True,
        "staged-set-baseline",
        "Recorded the initial staged file set for this HEAD; later commit guards can report shared-index changes.",
        current_paths=current_paths,
    )


def staged_changes_check(root: Path) -> tuple[dict[str, Any], list[str]]:
    paths = staged_paths(root)
    passed = bool(paths)
    return (
        check_result(
            "staged-changes",
            passed,
            "staged-changes-present" if passed else "no-staged-changes",
            (
                f"Staged changes detected in {len(paths)} path(s) for Git worktree {root.resolve(strict=False)}."
                if passed
                else (
                    f"No staged changes are available to commit in Git worktree {root.resolve(strict=False)}. "
                    "If this is a nested repository, stage and commit there or rerun with the governed parent "
                    "as --path and this worktree as --git-path."
                )
            ),
            paths=paths,
        ),
        paths,
    )


def staged_diff_check(root: Path) -> dict[str, Any]:
    result = run_git(root, "diff", "--cached", "--check")
    passed = result.returncode == 0
    detail = (result.stdout or result.stderr).strip()
    return check_result(
        "staged-diff-check",
        passed,
        "staged-diff-clean" if passed else "staged-diff-invalid",
        "Staged diff passed whitespace checks." if passed else "Staged diff contains whitespace errors.",
        detail=detail,
    )


def is_sensitive_staged_path(raw: str, protected_paths: set[str] | None = None) -> bool:
    path = Path(raw)
    name = path.name.lower()
    if name in ALLOWED_ENV_FILE_NAMES:
        return False
    normalized = path.as_posix().lstrip("./")
    if (
        any(normalized == directory or normalized.startswith(directory + "/") for directory in DEFAULT_LOCAL_ENV_DIRS)
        or name in DEFAULT_LOCAL_ENV_NAMES
        or name.startswith("local.")
        or ".local." in name
        or "-local." in name
    ):
        return True
    if protected_paths and any(
        normalized == protected or normalized.startswith(protected + "/")
        for protected in protected_paths
    ):
        return True
    if name == ".env" or name.startswith(".env."):
        return True
    if name in SENSITIVE_FILE_NAMES or path.suffix.lower() in SENSITIVE_KEY_SUFFIXES:
        return True
    return False


def staged_sensitive_paths_check(
    paths: list[str],
    *,
    root: Path | None = None,
    git_root: Path | None = None,
) -> dict[str, Any]:
    protected_paths = load_local_env_paths(root) if root is not None else set()
    if root is not None and git_root is not None:
        try:
            git_prefix = git_root.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()
        except ValueError:
            git_prefix = ""
        if git_prefix and git_prefix != ".":
            protected_paths = {
                path.removeprefix(git_prefix + "/") if path.startswith(git_prefix + "/") else path
                for path in protected_paths
            }
    sensitive = [path for path in paths if is_sensitive_staged_path(path, protected_paths)]
    registered = [
        path
        for path in sensitive
        if any(
            path == protected or path.startswith(protected + "/")
            for protected in protected_paths
        )
    ]
    passed = not sensitive
    return check_result(
        "staged-sensitive-paths",
        passed,
        "no-sensitive-paths" if passed else "sensitive-path-staged",
        "No sensitive local configuration or key files are staged."
        if passed
        else (
            "Sensitive local configuration or key files are staged. Unstage them with "
            "`git restore --staged <path>`; registered local environment files remain on disk "
            "and must never be deleted, emptied, or renamed to pass this check."
        ),
        paths=sensitive,
        registered_local_paths=registered,
    )


def staged_secret_scan_check(root: Path) -> dict[str, Any]:
    result = run_git(root, "diff", "--cached", "--unified=0", "--no-color", "--no-ext-diff")
    if result.returncode != 0:
        return check_result(
            "staged-secret-scan",
            False,
            "staged-diff-unavailable",
            "Unable to inspect the staged diff for credentials.",
        )
    current_path = "unknown"
    findings: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        if line.startswith("+++ b/"):
            current_path = line[6:]
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if HIGH_CONFIDENCE_SECRET_PATTERN.search(line[1:]):
            findings.append({"path": current_path, "type": "high-confidence-secret"})
    passed = not findings
    return check_result(
        "staged-secret-scan",
        passed,
        "no-staged-secrets" if passed else "staged-secret-detected",
        "No high-confidence credential values were found in added staged lines."
        if passed
        else "Possible credential values were found in staged content. Remove and rotate real credentials before committing.",
        findings=findings,
    )


def commit_message_text(args: argparse.Namespace) -> str | None:
    if args.commit_message is not None:
        return str(args.commit_message)
    if args.commit_message_file is not None:
        return args.commit_message_file.read_text(encoding="utf-8", errors="ignore")
    return None


def commit_message_check(args: argparse.Namespace) -> dict[str, Any]:
    message = commit_message_text(args)
    if message is None:
        if args.require_message:
            return check_result(
                "commit-message",
                False,
                "commit-message-missing",
                "A detailed commit message is required. Supply --commit-message or --commit-message-file.",
            )
        return skipped_check(
            "commit-message",
            "commit-message-not-supplied",
            "Commit message was not supplied to the guard. Runtime and commit-msg hooks must validate it before commit creation.",
        )

    normalized = message.strip()
    lines = normalized.splitlines()
    subject = lines[0].strip() if lines else ""
    body = "\n".join(lines[1:]).strip()
    failures: list[str] = []
    if "\\n" in normalized:
        failures.append("literal-newline-escape")
    if not subject:
        failures.append("subject-missing")
    elif len(subject) > 72:
        failures.append("subject-too-long")
    elif GENERIC_SUBJECT_PATTERN.fullmatch(subject):
        failures.append("subject-too-generic")
    elif not CONVENTIONAL_SUBJECT_PATTERN.fullmatch(subject):
        failures.append("subject-not-conventional")
    if len(body) < 120:
        failures.append("body-too-short")
    section_matches = {name: pattern.search(body) for name, pattern in MESSAGE_SECTIONS.items()}
    missing_sections = [name for name, match in section_matches.items() if match is None]
    failures.extend(f"missing-{name}-section" for name in missing_sections)
    present_matches = [match for match in section_matches.values() if match is not None]
    for name, match in section_matches.items():
        if match is None:
            continue
        following_starts = [other.start() for other in present_matches if other.start() > match.start()]
        end = min(following_starts) if following_starts else len(body)
        section_content = body[match.end() : end]
        if not CJK_CONTENT_PATTERN.search(section_content):
            failures.append(f"{name}-section-not-chinese")
    if HIGH_CONFIDENCE_SECRET_PATTERN.search(normalized):
        failures.append("message-contains-secret")
    if LOCAL_PATH_PATTERN.search(normalized):
        failures.append("message-contains-local-path")
    passed = not failures
    return check_result(
        "commit-message",
        passed,
        "commit-message-detailed" if passed else "commit-message-insufficient",
        "Commit message has a conventional subject and detailed Chinese Background, Changes, Verification, and Risks/Compatibility sections."
        if passed
        else "Commit message must provide detailed Chinese content for every required section.",
        failures=failures,
        subject=subject,
    )


def independent_review_check(root: Path, git_root: Path, operation: str) -> dict[str, Any]:
    loop_path = root / ".tenetora" / "state" / "loop-state.json"
    try:
        loop_payload = json.loads(loop_path.read_text(encoding="utf-8")) if loop_path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        loop_payload = {}
    review = loop_payload.get("review_cycle") if isinstance(loop_payload, dict) else None
    if not isinstance(review, dict):
        review = {}
    status = str(review.get("status") or "idle")
    independent = str(review.get("independent_review") or "not-requested")
    if status == "idle" or independent == "not-requested":
        return {
            "name": "independent-review",
            "status": "warn",
            "code": "independent-review-not-recorded",
            "message": "No independent review is recorded for the current work subject.",
        }
    reports = review.get("reports")
    report_path = reports[-1] if isinstance(reports, list) and reports else None
    recorded_fingerprint = review.get("subject_fingerprint")
    recorded_git_path = review.get("subject_git_path")
    if not isinstance(recorded_fingerprint, str) or not recorded_fingerprint:
        return {
            "name": "independent-review",
            "status": "warn",
            "code": "independent-review-unbound",
            "message": "The recorded review predates Git subject binding and cannot authorize current work.",
            "dispatch_id": review.get("active_dispatch_id"),
            "report_path": report_path,
        }
    try:
        current = review_subject.capture_for_guard(root, git_root, operation)
    except review_subject.ReviewSubjectError as exc:
        return {
            "name": "independent-review",
            "status": "warn",
            "code": "independent-review-subject-unavailable",
            "message": f"Current Git review subject could not be resolved: {exc}",
            "dispatch_id": review.get("active_dispatch_id"),
            "report_path": report_path,
        }
    if (
        recorded_fingerprint != current["subject_fingerprint"]
        or recorded_git_path != current["subject_git_path"]
    ):
        if review.get("review_scope_bound"):
            if recorded_git_path != current["subject_git_path"]:
                return {
                    "name": "independent-review",
                    "status": "warn",
                    "code": "independent-review-stale",
                    "message": "The reviewed Git repository path changed; a new independent review is required.",
                    "dispatch_id": review.get("active_dispatch_id"),
                    "report_path": report_path,
                    "recorded_subject": recorded_fingerprint,
                    "current_subject": current["subject_fingerprint"],
                    "recorded_git_path": recorded_git_path,
                    "current_git_path": current["subject_git_path"],
                    "review_scope": review.get("review_scope"),
                }
            stored_scope = review.get("review_scope")
            stored_scope_entries = review.get("review_scope_entries")
            stored_scope_fingerprint = review.get("review_scope_fingerprint")
            stored_subject_entries = review.get("review_subject_entries")
            stored_subject_entries_fingerprint = review.get("review_subject_entries_fingerprint")
            scoped_current: dict[str, Any] | None = None
            scope_snapshot_valid = (
                isinstance(stored_scope, list)
                and all(isinstance(item, str) for item in stored_scope)
                and isinstance(stored_scope_entries, dict)
                and all(isinstance(key, str) and isinstance(value, str) for key, value in stored_scope_entries.items())
                and isinstance(stored_scope_fingerprint, str)
                and secrets.compare_digest(
                    stored_scope_fingerprint,
                    review_subject.scope_fingerprint(stored_scope, stored_scope_entries),
                )
                and isinstance(stored_subject_entries, dict)
                and all(isinstance(key, str) and isinstance(value, str) for key, value in stored_subject_entries.items())
                and isinstance(stored_subject_entries_fingerprint, str)
                and secrets.compare_digest(
                    stored_subject_entries_fingerprint,
                    review_subject.subject_entries_fingerprint(stored_subject_entries),
                )
            )
            if scope_snapshot_valid:
                try:
                    scoped_current = review_subject.capture_for_guard(root, git_root, operation, scopes=stored_scope)
                except review_subject.ReviewSubjectError:
                    scoped_current = None
            if scoped_current is not None and scoped_current.get("review_scope_fingerprint") == stored_scope_fingerprint:
                current_subject_entries = scoped_current.get("review_subject_entries")
                if isinstance(current_subject_entries, dict):
                    delta_paths = sorted(
                        path
                        for path in set(stored_subject_entries) | set(current_subject_entries)
                        if stored_subject_entries.get(path) != current_subject_entries.get(path)
                    )
                    if len(delta_paths) > review_subject.MAX_REVIEW_DELTA_PATHS:
                        return {
                            "name": "independent-review",
                            "status": "warn",
                            "code": "independent-review-stale",
                            "message": "The Git subject delta is too large for bounded rebind; a full manual review is required.",
                            "dispatch_id": review.get("active_dispatch_id"),
                            "report_path": report_path,
                            "recorded_subject": recorded_fingerprint,
                            "current_subject": current["subject_fingerprint"],
                            "review_scope": stored_scope,
                            "scope_delta_paths": [],
                            "delta_path_count": len(delta_paths),
                            "delta_truncated": True,
                        }
                    if delta_paths and status == "passed":
                        trigger = "push" if operation == "push" else "commit"
                        subject_git_path = str(recorded_git_path or ".")
                        rebind_args = [
                            "tenetora",
                            "delegation",
                            "--review-rebind",
                            "--trigger",
                            trigger,
                            "--git-path",
                            subject_git_path,
                        ]
                        return {
                            "name": "independent-review",
                            "status": "warn",
                            "code": "independent-review-rebind-eligible",
                            "message": (
                                "The reviewed scope is unchanged, but the full Git subject has an out-of-scope delta; "
                                "explicitly rebind and review the new subject."
                            ),
                            "dispatch_id": review.get("active_dispatch_id"),
                            "report_path": report_path,
                            "recorded_subject": recorded_fingerprint,
                            "current_subject": current["subject_fingerprint"],
                            "review_scope": stored_scope,
                            "scope_delta_paths": [],
                            "review_delta_paths": delta_paths,
                            "rebind_args": rebind_args,
                            "rebind_command": shlex.join(rebind_args),
                        }
                    if delta_paths:
                        return {
                            "name": "independent-review",
                            "status": "warn",
                            "code": "independent-review-stale",
                            "message": "The reviewed scope changed; a manual independent review is required.",
                            "dispatch_id": review.get("active_dispatch_id"),
                            "report_path": report_path,
                            "recorded_subject": recorded_fingerprint,
                            "current_subject": current["subject_fingerprint"],
                            "review_scope": stored_scope,
                            "scope_delta_paths": delta_paths,
                        }
        return {
            "name": "independent-review",
            "status": "warn",
            "code": "independent-review-stale",
            "message": "The recorded independent review belongs to a different Git snapshot or repository.",
            "dispatch_id": review.get("active_dispatch_id"),
            "report_path": report_path,
            "recorded_subject": recorded_fingerprint,
            "current_subject": current["subject_fingerprint"],
        }
    if status in {"passed", "unavailable"}:
        dispatch_id = review.get("active_dispatch_id")
        delegation_path = root / ".tenetora" / "state" / "delegation-state.json"
        try:
            delegation = json.loads(delegation_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            delegation = {}
        dispatches = delegation.get("dispatches") if isinstance(delegation, dict) else None
        dispatch = next(
            (
                item
                for item in reversed(dispatches)
                if isinstance(item, dict) and item.get("dispatch_id") == dispatch_id
            ),
            None,
        ) if isinstance(dispatches, list) else None
        expected_status = "passed" if status == "passed" else "unavailable"
        evidence_valid = (
            isinstance(dispatch, dict)
            and dispatch.get("role") == "code-reviewer"
            and dispatch.get("association") == "review-cycle"
            and dispatch.get("status") == expected_status
            and dispatch.get("subject_fingerprint") == recorded_fingerprint
            and dispatch.get("subject_git_path") == recorded_git_path
        )
        if status == "passed" and isinstance(dispatch, dict):
            evidence_valid = evidence_valid and dispatch.get("result_source") == "model-reviewed-result"
        if not evidence_valid:
            return {
                "name": "independent-review",
                "status": "warn",
                "code": "independent-review-evidence-inconsistent",
                "message": "Review-cycle status is not backed by a matching semantic delegation result for the current Git subject.",
                "dispatch_id": dispatch_id,
                "report_path": report_path,
            }
    if status == "passed":
        contract_status = str(review.get("contract_status") or "unknown")
        if contract_status == "soft-boundary":
            return {
                "name": "independent-review",
                "status": "warn",
                "code": "independent-review-passed-soft-boundary",
                "message": "An independent review passed with prompt-only enforcement; it was not permission-isolated.",
                "dispatch_id": review.get("active_dispatch_id"),
                "report_path": report_path,
                "contract_status": contract_status,
            }
        return {
            "name": "independent-review",
            "status": "pass",
            "code": "independent-review-passed",
            "message": "A passed independent review is bound to the current Git subject.",
            "dispatch_id": review.get("active_dispatch_id"),
            "report_path": report_path,
        }
    if status == "unavailable":
        return {
            "name": "independent-review",
            "status": "warn",
            "code": "independent-review-unavailable",
            "message": "Independent dispatch was unavailable for the current Git subject; fallback review is marked 未经独立审查.",
            "dispatch_id": review.get("active_dispatch_id"),
            "report_path": report_path,
        }
    if status in {"reviewing", "needs-fix", "fixing", "failed"}:
        return {
            "name": "independent-review",
            "status": "warn",
            "code": f"independent-review-{status}",
            "message": f"Independent review cycle is `{status}` and has not produced a passed result.",
            "dispatch_id": review.get("active_dispatch_id"),
            "report_path": report_path,
        }
    return {
        "name": "independent-review",
        "status": "warn",
        "code": "independent-review-not-recorded",
        "message": "No passed independent review is recorded. Dispatch code-reviewer when a second perspective materially improves reliability; this reminder is non-blocking.",
    }


def latest_project_claim_events(root: Path) -> list[dict[str, Any]]:
    trail_path = root / ".tenetora" / "state" / "governance-trail.json"
    if not trail_path.is_file():
        return []
    try:
        payload = json.loads(trail_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        return []
    project_fingerprint = project_root_fingerprint(root)
    return [
        event
        for event in reversed(events)
        if isinstance(event, dict)
        and event.get("type") == "verification-claim"
        and event.get("project_root_fingerprint") == project_fingerprint
    ]


def event_verification_scopes(event: dict[str, Any]) -> tuple[str, ...] | None:
    raw = event.get("verification_scope", [])
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        return None
    try:
        return normalize_scopes(raw)
    except ValueError:
        return None


def scope_covers_path(scopes: tuple[str, ...], relative: str) -> bool:
    if not scopes:
        return True
    return any(relative == scope or relative.startswith(scope + "/") for scope in scopes)


def claim_worktree_freshness(root: Path, event: dict[str, Any]) -> dict[str, Any]:
    """Compare a claim's content evidence with the current worktree."""

    scopes = event_verification_scopes(event)
    if scopes is None:
        return {
            "status": "fail",
            "code": "claim-verification-scope-invalid",
            "message": "The claim contains an invalid verification scope.",
        }
    current = git_state_fingerprint(root, scopes)
    recorded = event.get("git_state_fingerprint")
    recorded_version = event.get("git_state_fingerprint_version")
    if current is None and recorded is None:
        return {
            "status": "unavailable",
            "code": "claim-worktree-fingerprint-unavailable",
            "message": "A Git content fingerprint is unavailable for this project.",
            "current_fingerprint": None,
            "recorded_fingerprint": None,
        }
    if current is None:
        return {
            "status": "fail",
            "code": "claim-worktree-fingerprint-unavailable",
            "message": "The current Git content fingerprint could not be read.",
            "current_fingerprint": None,
            "recorded_fingerprint": recorded,
        }
    if recorded_version != WORKTREE_FINGERPRINT_VERSION:
        return {
            "status": "fail",
            "code": "claim-worktree-fingerprint-unsupported",
            "message": "The claim uses an older or missing worktree fingerprint version.",
            "recorded_fingerprint_version": recorded_version,
            "current_fingerprint_version": WORKTREE_FINGERPRINT_VERSION,
        }
    if not isinstance(recorded, str) or not re.fullmatch(r"[0-9a-f]{64}", recorded):
        return {
            "status": "fail",
            "code": "claim-worktree-fingerprint-missing",
            "message": "The claim does not contain a valid worktree content fingerprint.",
            "current_fingerprint": current,
            "recorded_fingerprint": recorded,
        }
    if recorded != current:
        return {
            "status": "fail",
            "code": "claim-worktree-changed",
            "message": "Verified file paths, types, permissions, or contents changed after verification.",
            "current_fingerprint": current,
            "recorded_fingerprint": recorded,
        }
    return {
        "status": "pass",
        "code": "claim-worktree-fresh",
        "message": "The claim's verified worktree content is unchanged.",
        "current_fingerprint": current,
        "recorded_fingerprint": recorded,
        "verification_scope": list(scopes),
    }


def project_relative_staged_paths(root: Path, git_root: Path, paths: list[str]) -> list[str]:
    try:
        prefix = git_root.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return []
    return sorted(f"{prefix}/{path}" if prefix != "." else path for path in paths)


def claim_event_problem(root: Path, event: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    if event.get("status") != "pass" or event.get("verification_status") != "passed":
        return "evidence-stale", {
            "message": "The latest covering verification claim did not pass.",
            "verification_status": event.get("verification_status"),
        }
    claim_proof = str(event.get("claim_proof") or "")
    if not CLAIM_PROOF_PATTERN.fullmatch(claim_proof):
        return "evidence-proof-missing", {"message": "A covering passed claim has no valid claim proof."}
    comparison = claim_worktree_freshness(root, event)
    if comparison["status"] != "pass":
        code = "evidence-unavailable" if comparison["status"] == "unavailable" else "evidence-stale"
        return code, {
            "message": comparison["message"],
            **{key: value for key, value in comparison.items() if key not in {"status", "code", "message"}},
        }
    return None, {
        "claim_proof": claim_proof,
        "claim_kind": event.get("claim_kind"),
        "verification_scope": list(event_verification_scopes(event) or ()),
        "verification_command_hash": event.get("verification_command_hash"),
    }


def evidence_freshness_check(
    root: Path,
    git_root: Path,
    staged: list[str] | None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    events = latest_project_claim_events(root)
    if not events:
        return {
            "name": "evidence-freshness",
            "status": "warn",
            "code": "evidence-claim-missing",
            "message": "No verification claim is recorded for the current project.",
        }
    staged_paths_for_project = project_relative_staged_paths(root, git_root, staged or [])
    if staged is not None and staged and not staged_paths_for_project:
        return {
            "name": "evidence-freshness",
            "status": "warn",
            "code": "evidence-scope-unavailable",
            "message": "The Git worktree is outside the governed project, so verification scope coverage cannot be proven.",
        }
    selected: list[dict[str, Any]] = []
    uncovered: list[str] = []
    if staged_paths_for_project:
        for path in staged_paths_for_project:
            event = next(
                (
                    item
                    for item in events
                    if (scopes := event_verification_scopes(item)) is not None and scope_covers_path(scopes, path)
                ),
                None,
            )
            if event is None:
                uncovered.append(path)
            elif event not in selected:
                selected.append(event)
    else:
        selected.append(events[0])
    if uncovered:
        return {
            "name": "evidence-freshness",
            "status": "warn",
            "code": "evidence-scope-uncovered",
            "message": "Some staged paths are not covered by any verification claim.",
            "uncovered_paths": uncovered,
        }
    evidence: list[dict[str, Any]] = []
    for event in selected:
        problem, details = claim_event_problem(root, event)
        if problem is not None:
            if (
                plan is not None
                and plan.get("decision") == "targeted"
                and not plan.get("verification_impact_paths")
                and plan.get("claim_reuse", {}).get("eligible")
            ):
                return {
                    "name": "evidence-freshness",
                    "status": "warn",
                    "code": "evidence-targeted-check-required",
                    "message": (
                        "Implementation evidence remains reusable; only the listed targeted checks are required, "
                        "not a full verification rerun."
                    ),
                    "claim_proofs": plan.get("claim_reuse", {}).get("claim_proofs")
                    or [plan.get("claim_reuse", {}).get("claim_proof")],
                    "required_checks": plan.get("required_checks", []),
                }
            return {
                "name": "evidence-freshness",
                "status": "warn",
                "code": problem,
                **details,
            }
        evidence.append(details)
    return {
        "name": "evidence-freshness",
        "status": "pass",
        "code": "evidence-fresh",
        "message": "Every staged path is covered by a fresh verification claim.",
        "claim_proofs": [item["claim_proof"] for item in evidence],
        "evidence": evidence,
        "git_state_fingerprint_version": WORKTREE_FINGERPRINT_VERSION,
    }


def guard_commit(args: argparse.Namespace) -> dict[str, Any]:
    root: Path = args.path
    requested_git_root: Path = args.git_path or root
    git_root = git_worktree_root(requested_git_root) or requested_git_root
    checks = [
        run_boolean_check("secret-scan", lambda: check_secret_scan(root)),
        run_boolean_check("local-path-scan", lambda: check_local_path_scan(root)),
        git_repository_check(git_root),
    ]
    target_allowed = checks[-1]["status"] == "pass"
    if target_allowed:
        target_check = git_target_scope_check(root, git_root)
        checks.append(target_check)
        target_allowed = target_check["status"] == "pass"
    paths: list[str] | None = None
    if target_allowed and args.operation == "commit":
        staged_check, paths = staged_changes_check(git_root)
        checks.extend(
            [
                staged_check,
                staged_diff_check(git_root),
                staged_set_drift_check(root, git_root, paths),
                staged_sensitive_paths_check(paths, root=root, git_root=git_root),
                staged_secret_scan_check(git_root),
                repository_units.repository_units_check(root, git_root),
                change_impact.commit_preflight_check(root, git_root, paths),
                commit_message_check(args),
            ]
        )
    if target_allowed:
        plan_paths = project_relative_staged_paths(root, git_root, paths) if paths is not None else None
        plan = (
            verification_plan.build_staged_plan(root, git_root, plan_paths)
            if plan_paths is not None
            else verification_plan.build_plan(
                root,
                git_root,
                # Push authorizes the already-created HEAD object. Uncommitted
                # worktree changes belong to a later commit and must not force
                # a new verification plan for this push.
                changed_paths_override=[] if args.operation == "push" else None,
            )
        )
        plan_status = "pass" if plan.get("status") == "pass" and plan.get("decision") == "reuse" else "warn"
        if plan.get("status") == "error":
            plan_status = "fail"
        checks.append(
            {
                "name": "verification-plan",
                "status": plan_status,
                "code": plan.get("code", "verification-plan-error"),
                "message": plan.get("message", "Unable to classify verification freshness."),
                "decision": plan.get("decision"),
                "changed_paths": plan.get("changed_paths", []),
                "verification_impact_paths": plan.get("verification_impact_paths", []),
                "claim_reuse": plan.get("claim_reuse", {}),
                "required_checks": plan.get("required_checks", []),
            }
        )
        checks.append(evidence_freshness_check(root, git_root, paths, plan))
    checks.append(independent_review_check(root, git_root, args.operation))
    status = "fail" if any(item["status"] == "fail" for item in checks) else "pass"
    payload = {
        "version": 1,
        "action": "commit",
        "operation": args.operation,
        "governance_path": str(root),
        "git_path": str(git_root.resolve(strict=False)),
        "status": status,
        "checks": checks,
    }
    if args.operation == "commit" and paths:
        payload.update(
            {
                "staged_paths": sorted(set(paths)),
                "staged_set_fingerprint": staged_set_fingerprint(paths),
                "git_head": git_head(git_root),
                "git_identity": git_identity(git_root),
            }
        )
    return payload


def guard_rules(root: Path) -> dict[str, Any]:
    checks = [run_int_check("run-all", lambda: run_python_checks(root))]
    status = "pass" if checks[0]["status"] == "pass" else "fail"
    return {"version": 1, "action": "rules", "status": status, "checks": checks}


def _arg_or_environment(args: argparse.Namespace, attribute: str, *names: str) -> str:
    value = str(getattr(args, attribute, "") or "").strip()
    if value:
        return value
    for name in names:
        value = str(os.environ.get(name, "") or "").strip()
        if value:
            return value
    return ""


def explicit_guard_identity(args: argparse.Namespace) -> dict[str, str]:
    """Read only alignment identity supplied directly to the guard invocation."""

    return {
        "session_id": _arg_or_environment(
            args, "session_id", "TENETORA_ALIGNMENT_SESSION_ID", "TENETORA_SESSION_ID"
        ),
        "owner_id": _arg_or_environment(
            args, "owner_id", "TENETORA_ALIGNMENT_OWNER_ID", "TENETORA_OWNER_ID"
        ),
        "conversation_id": _arg_or_environment(args, "conversation_id", "TENETORA_CONVERSATION_ID"),
        "conversation_continuity_id": _arg_or_environment(
            args, "conversation_continuity_id", "TENETORA_CONVERSATION_CONTINUITY_ID"
        ),
        "tool": _arg_or_environment(args, "tool", "TENETORA_TOOL"),
    }


def alignment_session_for_identity(
    root: Path,
    *,
    continuity_id: str = "",
    conversation_id: str = "",
    owner_id: str = "",
    include_closed: bool = False,
) -> tuple[str | None, str | None, str | None]:
    """Resolve one alignment id only when every available identity agrees."""

    lookups = (
        (
            "conversation continuity",
            continuity_id,
            lambda path, value: alignment_state.session_id_for_conversation_continuity(
                path, value, include_closed=include_closed
            ),
        ),
        (
            "conversation",
            conversation_id,
            lambda path, value: alignment_state.session_id_for_conversation(
                path, value, include_closed=include_closed
            ),
        ),
        (
            "owner",
            owner_id,
            lambda path, value: alignment_state.session_id_for_owner(path, value, include_closed=include_closed),
        ),
    )
    matches: list[tuple[str, str]] = []
    for label, value, lookup in lookups:
        if not value:
            continue
        try:
            session_id = lookup(root, value)
        except alignment_state.AlignmentIdentityAmbiguous as exc:
            return None, label, exc
        except alignment_state.AlignmentStateError as exc:
            return None, label, str(exc)
        if session_id:
            matches.append((session_id, label))
    unique = list(dict.fromkeys(session_id for session_id, _label in matches))
    if len(unique) > 1:
        candidates = [{"session_id": session_id} for session_id in unique]
        labels = ", ".join(label for _session_id, label in matches)
        return None, "host identity", alignment_state.AlignmentIdentityAmbiguous(
            f"available host identity fields disagree about the alignment goal ({labels}); pass --session-id explicitly",
            candidates,
        )
    if unique:
        return unique[0], next(label for session_id, label in matches if session_id == unique[0]), None
    return None, None, None


def prepare_guard_identity(args: argparse.Namespace) -> dict[str, Any]:
    """Bind a guard request to a cached host context without inventing an alignment id."""

    explicit = explicit_guard_identity(args)
    if explicit["session_id"]:
        resolution = {"status": "explicit", "context": explicit, "candidate_count": 1}
        setattr(args, "_execution_identity_resolution", resolution)
        return resolution

    context: dict[str, Any] | None = None
    source = "explicit"
    if any(explicit[field] for field in ("owner_id", "conversation_id", "conversation_continuity_id")):
        context = dict(explicit)
        context["session_id"] = ""
        context["identity_present"] = bool(
            explicit["owner_id"] or explicit["conversation_id"] or explicit["conversation_continuity_id"]
        )
    else:
        platform = str(explicit["tool"] or os.environ.get("TENETORA_HOOK_PLATFORM", "") or "").strip()
        resolution = execution_context.resolve(args.path, platform=platform or None)
        source = str(resolution.get("status") or "missing")
        candidate = resolution.get("context") if isinstance(resolution, dict) else None
        if isinstance(candidate, dict):
            context = dict(candidate)
        else:
            resolution = dict(resolution)
            resolution["source"] = source
            setattr(args, "_execution_identity_resolution", resolution)
            return resolution

    assert context is not None
    continuity_id = str(context.get("conversation_continuity_id") or explicit["conversation_continuity_id"] or "").strip()
    conversation_id = str(context.get("conversation_id") or explicit["conversation_id"] or "").strip()
    owner_id = str(context.get("owner_id") or explicit["owner_id"] or "").strip()
    session_id, matched_by, error = alignment_session_for_identity(
        args.path,
        continuity_id=continuity_id,
        conversation_id=conversation_id,
        owner_id=owner_id,
        include_closed=bool(getattr(args, "check_proof_only", False)),
    )
    if error:
        candidates = getattr(error, "candidates", None)
        resolution = {
            "status": "ambiguous",
            "context": context,
            "candidate_count": len(candidates) if isinstance(candidates, list) else 2,
            "matched_by": matched_by,
            "reason": str(error),
            "source": source,
        }
        if isinstance(candidates, list):
            resolution["candidates"] = candidates
        setattr(args, "_execution_identity_resolution", resolution)
        return resolution
    if not session_id:
        resolution = {
            "status": "alignment-missing",
            "context": context,
            "candidate_count": 0,
            "reason": "host execution identity was found, but no matching alignment session exists",
            "source": source,
        }
        setattr(args, "_execution_identity_resolution", resolution)
        return resolution

    args.session_id = session_id
    if not explicit["owner_id"] and owner_id:
        args.owner_id = owner_id
    if not explicit["conversation_id"] and conversation_id:
        args.conversation_id = conversation_id
    if not explicit["conversation_continuity_id"] and continuity_id:
        args.conversation_continuity_id = continuity_id
    if not explicit["tool"] and context.get("tool") and context.get("tool") != "unknown":
        args.tool = str(context["tool"])
    for field in ("provider", "model"):
        if not getattr(args, field, "") and context.get(field):
            setattr(args, field, str(context[field]))
    resolution = {
        "status": "mapped",
        "context": context,
        "candidate_count": 1,
        "alignment_session_id": session_id,
        "matched_by": matched_by,
        "source": source,
    }
    setattr(args, "_execution_identity_resolution", resolution)
    return resolution


def identity_resolution_failure(args: argparse.Namespace, *, proof_lookup: bool = False) -> dict[str, Any] | None:
    resolution = getattr(args, "_execution_identity_resolution", {})
    status = str(resolution.get("status") or "") if isinstance(resolution, dict) else ""
    if status not in {"ambiguous", "alignment-missing"}:
        return None
    context = resolution.get("context") if isinstance(resolution, dict) else None
    host_session = str(context.get("session_id") or "") if isinstance(context, dict) else ""
    reason = str(resolution.get("reason") or "") if isinstance(resolution, dict) else ""
    if status == "ambiguous":
        code = "claim-execution-identity-ambiguous"
        message = "Multiple alignment sessions match the current host execution identity; automatic binding is refused."
    else:
        code = "claim-alignment-session-not-found"
        message = "The host execution identity was found, but no matching Tenetora alignment session exists."
    details = {
        "execution_identity_status": status,
        "host_session_id": host_session,
        "candidate_count": resolution.get("candidate_count", 0) if isinstance(resolution, dict) else 0,
        "reason": reason,
        "recommendation": "Pass the actual alignment --session-id and --owner-id explicitly, or start/resume the matching isolated alignment session.",
    }
    if isinstance(resolution, dict) and isinstance(resolution.get("candidates"), list):
        details["candidates"] = resolution["candidates"]
    if proof_lookup:
        return {
            "version": 1,
            "action": "claim",
            "mode": "check-proof-only",
            "status": "fail",
            "checks": [{"name": "claim-identity", "status": "fail", "code": code, "message": message, **details}],
        }
    return claim_failure(code, message, **details)


def claim_session_binding(
    args: argparse.Namespace,
    session_id: str,
    owner_id: str,
    *,
    allow_closed: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not session_id:
        try:
            active = [
                item
                for item in alignment_state.load_all_sessions(args.path)
                if alignment_state.session_requires_explicit_identity(item)
            ]
        except alignment_state.AlignmentStateError as exc:
            return None, {
                "name": "claim-identity",
                "status": "fail",
                "code": "claim-alignment-binding-invalid",
                "message": f"Verification claim cannot inspect alignment sessions: {exc}",
            }
        if active:
            return None, {
                "name": "claim-identity",
                "status": "fail",
                "code": "claim-owner-required",
                "message": "An active alignment session exists; a verification claim must name its session and owner.",
                "recommendation": "Pass --session-id, --owner-id, --conversation-id, and --tool for the current alignment session.",
            }
        requested_continuity = alignment_state.requested_conversation_continuity_id(args, owner_id)
        return {"goal_fingerprint": "", "conversation_id": "", "conversation_continuity_id": requested_continuity, "tool": ""}, None
    try:
        session = alignment_state.load_session(args.path, session_id)
        handoff = alignment_state.load_handoff(args.path, session_id)
        binding = handoff or session
        binding_kind = "handoff" if handoff is not None else "session"
        if binding is None:
            requested_conversation = alignment_state.requested_conversation_id(args, owner_id)
            requested_tool = alignment_state.requested_tool(args)
            related = [
                item
                for item in alignment_state.load_all_sessions(args.path)
                if item.get("conversation_id") == requested_conversation
                or item.get("owner_id") == owner_id
            ]
            if related:
                raise alignment_state.AlignmentStateError(
                    "an alignment session exists for this owner or conversation under a different session id"
                )
            return {
                "goal_fingerprint": "",
                "conversation_id": requested_conversation,
                "tool": requested_tool,
            }, None
        lifecycle = alignment_state.lifecycle_record(args.path, session_id)
        if (
            str(getattr(args, "claim_kind", "") or "") == "completion"
            and not allow_closed
            and lifecycle is not None
            and lifecycle.get("status") in {"completed", "archived", "expired", "abandoned"}
        ):
            raise alignment_state.AlignmentStateError(
                f"the alignment goal is {lifecycle.get('status')} and cannot receive another completion claim"
            )
        if binding.get("status") in {"abandoned", "expired"}:
            raise alignment_state.AlignmentStateError("the alignment session is no longer claimable")
        if handoff is not None:
            recorded_handoff_hash = str(handoff.get("handoff_hash") or "")
            actual_handoff_hash = alignment_state.handoff_hash(handoff)
            if not recorded_handoff_hash or recorded_handoff_hash != actual_handoff_hash:
                raise alignment_state.AlignmentStateError("alignment handoff hash is invalid")
        if str(binding.get("owner_id") or "") != owner_id:
            raise alignment_state.AlignmentStateError("alignment session owner mismatch")
        requested_conversation = alignment_state.requested_conversation_id(args)
        bound_conversation = str(binding.get("conversation_id") or "")
        if requested_conversation and requested_conversation != bound_conversation:
            raise alignment_state.AlignmentStateError("alignment session conversation mismatch")
        requested_tool = alignment_state.requested_tool(args)
        bound_tool = str(binding.get("tool") or "unknown")
        if requested_tool != "unknown" and requested_tool != bound_tool:
            raise alignment_state.AlignmentStateError("alignment session tool mismatch")
        bound_continuity = str(
            binding.get("conversation_continuity_id") or binding.get("conversation_id") or ""
        )
        requested_continuity = alignment_state.requested_conversation_continuity_id(args)
        if requested_continuity and bound_continuity and requested_continuity != bound_continuity:
            raise alignment_state.AlignmentStateError("conversation continuity mismatch")
        fingerprint = str(binding.get("goal_fingerprint") or "")
        if not GOAL_FINGERPRINT_PATTERN.fullmatch(fingerprint):
            raise alignment_state.AlignmentStateError("alignment session has no valid goal fingerprint")
        if not allow_closed and lifecycle is not None and lifecycle.get("status") == "open":
            alignment_state.touch_lifecycle(args.path, session_id, owner_id)
    except alignment_state.AlignmentStateError as exc:
        return None, {
            "name": "claim-identity",
            "status": "fail",
            "code": "claim-alignment-binding-invalid",
            "message": f"Verification claim cannot bind to the requested alignment session: {exc}",
        }
    return {
        "goal_fingerprint": fingerprint,
        "conversation_id": bound_conversation,
        "conversation_continuity_id": bound_continuity,
        "tool": bound_tool,
        "continuity_available": bool(binding.get("continuity_available", False)),
        "binding_kind": binding_kind,
        "alignment_status": str(binding.get("status") or ""),
        "risk_level": str(binding.get("risk_level") or ""),
        "handoff_hash": str(binding.get("handoff_hash") or "") if binding_kind == "handoff" else "",
        "confirmation_assurance": str(
            binding.get("confirmation", {}).get("assurance")
            if isinstance(binding.get("confirmation"), dict)
            else "legacy-unattested"
        ) or "legacy-unattested",
        "confirmation_event_id": str(
            binding.get("confirmation", {}).get("event_id")
            if isinstance(binding.get("confirmation"), dict)
            else ""
        ),
    }, None


def normalize_verification_command(command: str) -> str:
    return " ".join(str(command).split()).strip()


def verification_command_hash(command: str) -> str:
    return hashlib.sha256(normalize_verification_command(command).encode("utf-8")).hexdigest()


def project_root_fingerprint(root: Path) -> str:
    return hashlib.sha256(str(root.resolve(strict=False)).encode("utf-8", errors="surrogatepass")).hexdigest()


def default_claim_kind(verification_status: str) -> str:
    if verification_status == "passed":
        return "partial-verification"
    if verification_status == "skipped":
        return "blocked"
    return "failed"


def claim_failure(code: str, message: str, **details: Any) -> dict[str, Any]:
    check = {
        "name": "verification-claim",
        "status": "fail",
        "code": code,
        "message": message,
    }
    check.update(details)
    return {"version": 1, "action": "claim", "status": "fail", "checks": [check]}


def guard_claim(args: argparse.Namespace) -> dict[str, Any]:
    try:
        # Explicit session claims also need the same expiry policy as implicit identity lookup.
        alignment_state.sweep_alignment_state(args.path)
    except alignment_state.AlignmentStateError as exc:
        return claim_failure("claim-alignment-state-invalid", f"Cannot maintain alignment lifecycle: {exc}")
    prepare_guard_identity(args)
    if args.check_proof_only:
        return guard_claim_proof_check(args)
    try:
        verification_scope = list(normalize_scopes(args.verification_scope))
    except ValueError as exc:
        return claim_failure("claim-verification-scope-invalid", str(exc))
    session_id = alignment_state.requested_session_id(args)
    owner_id = alignment_state.requested_owner_id(args, session_id)
    conversation_id = alignment_state.requested_conversation_id(args)
    if bool(session_id) != bool(owner_id):
        return claim_failure(
            "claim-identity-incomplete",
            "Claim identity requires both --session-id and --owner-id, or neither.",
        )
    if not args.verification_command:
        return claim_failure(
            "verification-evidence-missing",
            "A verification claim needs the command that was actually run.",
            recommendation="Pass --verification-command '<command>' and --verification-status passed after the command completes.",
        )

    claim_kind = args.claim_kind or default_claim_kind(args.verification_status)
    if claim_kind == "completion":
        resolution_failure = identity_resolution_failure(args)
        if resolution_failure is not None:
            return resolution_failure
    if claim_kind == "completion" and verification_scope:
        return claim_failure(
            "completion-scope-not-allowed",
            "A completion claim must verify the whole project; use partial-verification for a scoped check.",
        )
    if claim_kind == "completion" and args.verification_status != "passed":
        return claim_failure(
            "completion-verification-not-passed",
            "A completion claim requires verification-status=passed; record blocked or failed work with its matching claim kind.",
        )
    if claim_kind == "partial-verification" and args.verification_status != "passed":
        return claim_failure(
            "claim-kind-status-mismatch",
            "A partial-verification claim must describe a passed scoped check; record a failed or blocked check with its matching claim kind.",
        )
    if claim_kind in {"blocked", "failed"} and args.verification_status == "passed":
        return claim_failure(
            "claim-kind-status-mismatch",
            "A blocked or failed claim cannot carry a passed verification status.",
        )

    binding, binding_error = claim_session_binding(
        args,
        session_id,
        owner_id,
        allow_closed=claim_kind != "completion",
    )
    if binding_error is not None:
        return {
            "version": 1,
            "action": "claim",
            "status": "fail",
            "checks": [binding_error],
            "session_id": session_id,
            "owner_id": owner_id,
        }
    assert binding is not None
    if claim_kind == "completion" and not session_id:
        return claim_failure(
            "claim-owner-required",
            "A completion claim requires an explicit session and owner binding; an unscoped claim is only partial verification.",
        )
    if claim_kind == "completion" and not GOAL_FINGERPRINT_PATTERN.fullmatch(binding["goal_fingerprint"]):
        return claim_failure(
            "claim-goal-required",
            "A completion claim requires a current alignment session with a valid goal fingerprint; use partial-verification when no goal is aligned.",
        )
    execution_attempt: dict[str, Any] | None = None
    execution_attempt_id = str(args.execution_attempt_id or "").strip()
    if execution_attempt_id:
        if not session_id:
            return claim_failure(
                "claim-attempt-binding-invalid",
                "An execution attempt claim requires an explicit alignment session and owner.",
            )
        if not alignment_state.has_explicit_conversation_continuity(args):
            return claim_failure(
                "claim-continuity-required",
                "An execution attempt claim requires the stable conversation continuity id that owns the attempt.",
            )
        try:
            session = alignment_state.load_session(args.path, session_id)
            handoff = alignment_state.load_handoff(args.path, session_id)
            attempt_source = handoff or session
            attempts = attempt_source.get("execution_attempts", []) if attempt_source else []
            execution_attempt = next(
                (
                    item
                    for item in attempts
                    if isinstance(item, dict) and item.get("execution_attempt_id") == execution_attempt_id
                ),
                None,
            )
        except alignment_state.AlignmentStateError as exc:
            return claim_failure("claim-attempt-binding-invalid", f"Cannot inspect execution attempt: {exc}")
        if execution_attempt is None:
            return claim_failure("claim-attempt-binding-invalid", "The execution attempt is not part of the alignment session.")
        if execution_attempt.get("conversation_continuity_id") != binding.get("conversation_continuity_id"):
            return claim_failure("claim-attempt-binding-invalid", "The execution attempt belongs to a different conversation continuity.")
        if claim_kind == "completion" and execution_attempt.get("status") != "completed":
            return claim_failure(
                "claim-attempt-not-completed",
                "A completion claim must reference an execution attempt that finished successfully.",
            )
    if claim_kind == "completion" and session_id and binding.get("binding_kind") != "handoff":
        return claim_failure(
            "claim-confirmed-handoff-required",
            "A completion claim requires a confirmed or risk-accepted alignment handoff; an active alignment session is not completion authorization.",
        )
    if claim_kind == "completion" and session_id and binding.get("confirmation_assurance") not in {
        "recorded-user-assertion",
    }:
        return claim_failure(
            "claim-confirmation-assurance-required",
            "A completion claim requires an audited confirmation authorization record; legacy or unattested CLI confirmation is insufficient.",
        )
    state_fingerprint = git_state_fingerprint(args.path, verification_scope)
    state_entries = git_state_entries(args.path, verification_scope) if state_fingerprint is not None else None
    if claim_kind == "completion" and state_fingerprint is None:
        return claim_failure(
            "claim-git-state-unavailable",
            "A completion claim requires a readable Git HEAD and worktree fingerprint.",
        )

    conversation_id = binding["conversation_id"] or conversation_id
    conversation_continuity_id = binding.get("conversation_continuity_id") or conversation_id
    goal_fingerprint = binding["goal_fingerprint"]
    tool = binding["tool"]
    try:
        provider = alignment_state.requested_execution_metadata(args, "provider") if args.provider else ""
        model = alignment_state.requested_execution_metadata(args, "model") if args.model else ""
    except alignment_state.AlignmentStateError as exc:
        return claim_failure("claim-execution-metadata-invalid", str(exc))
    if execution_attempt:
        provider = provider or str(execution_attempt.get("provider") or "")
        model = model or str(execution_attempt.get("model") or "")
        tool = str(execution_attempt.get("tool") or tool)
    command_hash = verification_command_hash(args.verification_command)
    claim_proof = (
        f"ah-claim-{secrets.token_hex(16)}"
        if args.verification_status == "passed" and claim_kind in {"completion", "partial-verification"}
        else None
    )
    status = "pass" if args.verification_status == "passed" else "fail"
    check = {
        "name": "verification-claim",
        "status": status,
        "claim_kind": claim_kind,
        "verification_status": args.verification_status,
        "verification_command_hash": command_hash,
        "verification_command_length": len(normalize_verification_command(args.verification_command)),
        "verification_scope": verification_scope,
        "claim_proof": claim_proof,
        "session_id": session_id,
        "owner_id": owner_id,
        "conversation_id": conversation_id,
        "conversation_continuity_id": conversation_continuity_id,
        "tool": tool,
        "provider": provider,
        "model": model,
        "execution_attempt_id": execution_attempt_id,
        "goal_fingerprint": goal_fingerprint,
        "handoff_hash": binding.get("handoff_hash", ""),
        "confirmation_assurance": binding.get("confirmation_assurance", ""),
        "confirmation_event_id": binding.get("confirmation_event_id", ""),
        "project_root_fingerprint": project_root_fingerprint(args.path),
        "git_state_fingerprint_version": WORKTREE_FINGERPRINT_VERSION if state_fingerprint is not None else None,
        "git_state_fingerprint": state_fingerprint,
        "git_state_entries_version": WORKTREE_ENTRIES_VERSION if state_entries is not None else None,
        "git_state_entries": state_entries,
    }
    attribution = "owner-bound" if session_id and owner_id else "unattributed"
    attribution_warning = None
    if claim_kind == "partial-verification" and attribution == "unattributed":
        attribution_warning = {
            "name": "claim-attribution",
            "status": "warn",
            "code": "claim-attribution-unbound",
            "message": "This partial verification claim has no session or owner binding and cannot be attributed to one agent.",
            "recommendation": "Use an owner-bound alignment session and pass --session-id, --owner-id, --conversation-id, and --tool in multi-agent work.",
            "attribution": attribution,
        }
        check["attribution"] = attribution
        check["attribution_warning"] = attribution_warning["code"]
    return {
        "version": 1,
        "action": "claim",
        "status": status,
        "checks": [check, attribution_warning] if attribution_warning else [check],
        "claim_kind": claim_kind,
        "verification_status": args.verification_status,
        "verification_command_hash": command_hash,
        "verification_command_length": len(normalize_verification_command(args.verification_command)),
        "verification_scope": verification_scope,
        "claim_proof": claim_proof,
        "session_id": session_id,
        "owner_id": owner_id,
        "conversation_id": conversation_id,
        "conversation_continuity_id": conversation_continuity_id,
        "tool": tool,
        "provider": provider,
        "model": model,
        "execution_attempt_id": execution_attempt_id,
        "goal_fingerprint": goal_fingerprint,
        "handoff_hash": binding.get("handoff_hash", ""),
        "confirmation_assurance": binding.get("confirmation_assurance", ""),
        "confirmation_event_id": binding.get("confirmation_event_id", ""),
        "project_root_fingerprint": project_root_fingerprint(args.path),
        "git_state_fingerprint_version": WORKTREE_FINGERPRINT_VERSION if state_fingerprint is not None else None,
        "git_state_fingerprint": state_fingerprint,
        "git_state_entries_version": WORKTREE_ENTRIES_VERSION if state_entries is not None else None,
        "git_state_entries": state_entries,
        "attribution": attribution,
        "attribution_warning": attribution_warning["code"] if attribution_warning else None,
        "verification_timestamp": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


def latest_claim_event(
    root: Path,
    session_id: str,
    owner_id: str,
    goal_fingerprint: str = "",
    conversation_id: str = "",
    tool: str = "",
    conversation_continuity_id: str = "",
    execution_attempt_id: str = "",
    handoff_hash: str = "",
    verification_scope: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    trail_path = root / ".tenetora" / "state" / "governance-trail.json"
    if not trail_path.is_file():
        return None
    try:
        payload = json.loads(trail_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    events = payload.get("events")
    if not isinstance(events, list):
        return None
    for event in reversed(events):
        if not isinstance(event, dict) or event.get("type") != "verification-claim":
            continue
        event_scope = event_verification_scopes(event)
        if event_scope is None or event_scope != verification_scope:
            continue
        event_session = str(event.get("session_id") or "")
        event_owner = str(event.get("owner_id") or "")
        if session_id:
            if event_session != session_id:
                continue
            if event_owner != owner_id:
                continue
            if str(event.get("goal_fingerprint") or "") != goal_fingerprint:
                continue
            if conversation_id and str(event.get("conversation_id") or "") != conversation_id:
                continue
            if tool and tool != "unknown" and str(event.get("tool") or "") != tool:
                continue
            if conversation_continuity_id and event.get("conversation_continuity_id") and str(event.get("conversation_continuity_id")) != conversation_continuity_id:
                continue
            if execution_attempt_id and str(event.get("execution_attempt_id") or "") != execution_attempt_id:
                continue
            if handoff_hash and str(event.get("handoff_hash") or "") != handoff_hash:
                continue
        elif event_session or event_owner:
            continue
        return event
    return None


def guard_claim_proof_check(args: argparse.Namespace) -> dict[str, Any]:
    try:
        verification_scope = normalize_scopes(args.verification_scope)
    except ValueError as exc:
        return claim_failure("claim-verification-scope-invalid", str(exc))
    if args.claim_kind == "completion" and verification_scope:
        return claim_failure(
            "completion-scope-not-allowed",
            "A completion proof must cover the whole project; scoped evidence can only be checked as partial-verification.",
        )
    session_id = alignment_state.requested_session_id(args)
    owner_id = alignment_state.requested_owner_id(args, session_id)
    if bool(session_id) != bool(owner_id):
        check = {
            "name": "claim-proof",
            "status": "fail",
            "code": "claim-identity-incomplete",
            "message": "Claim proof lookup requires both --session-id and --owner-id, or neither.",
        }
        return {
            "version": 1,
            "action": "claim",
            "mode": "check-proof-only",
            "status": "fail",
            "checks": [check],
        }
    expected_command = args.expected_verification_command or args.verification_command
    if args.claim_kind == "completion" and not expected_command:
        return {
            "version": 1,
            "action": "claim",
            "mode": "check-proof-only",
            "status": "fail",
            "checks": [
                {
                    "name": "claim-proof",
                    "status": "fail",
                    "code": "claim-verification-command-required",
                    "message": "Completion proof lookup requires the exact verification command that must be covered.",
                    "recommendation": "Pass --expected-verification-command '<command>' or set TENETORA_COMPLETION_VERIFICATION_COMMAND in the completion workflow.",
                }
            ],
        }
    if args.claim_kind == "completion":
        resolution_failure = identity_resolution_failure(args, proof_lookup=True)
        if resolution_failure is not None:
            return resolution_failure
    binding, binding_error = claim_session_binding(args, session_id, owner_id, allow_closed=True)
    if binding_error is not None:
        return {
            "version": 1,
            "action": "claim",
            "mode": "check-proof-only",
            "status": "fail",
            "checks": [binding_error],
        }
    assert binding is not None
    preliminary_event = latest_claim_event(
        args.path,
        session_id,
        owner_id,
        binding["goal_fingerprint"],
        binding["conversation_id"],
        binding["tool"],
        binding.get("conversation_continuity_id", ""),
        str(args.execution_attempt_id or ""),
        str(binding.get("handoff_hash") or ""),
        verification_scope,
    )
    preliminary_kind = str(preliminary_event.get("claim_kind") or "") if preliminary_event else ""
    if preliminary_event and (
        preliminary_event.get("status") != "pass"
        or preliminary_event.get("verification_status") != "passed"
    ):
        return {
            "version": 1,
            "action": "claim",
            "mode": "check-proof-only",
            "status": "fail",
            "checks": [
                {
                    "name": "claim-proof",
                    "status": "fail",
                    "code": "claim-verification-failed",
                    "message": "The latest verification event failed or was skipped; an older passed claim cannot cover it.",
                    "verification_status": str(preliminary_event.get("verification_status") or "unknown"),
                }
            ],
        }
    if args.claim_kind == "completion" and preliminary_event and preliminary_kind != "completion":
        code = "claim-partial-only" if preliminary_kind == "partial-verification" else "claim-kind-mismatch"
        return {
            "version": 1,
            "action": "claim",
            "mode": "check-proof-only",
            "status": "fail",
            "checks": [
                {
                    "name": "claim-proof",
                    "status": "fail",
                    "code": code,
                    "message": "The latest verification event is not a completion claim and cannot satisfy the completion gate.",
                    "claim_kind": preliminary_kind or "legacy",
                    "expected_claim_kind": "completion",
                }
            ],
        }
    if args.claim_kind == "completion" and not GOAL_FINGERPRINT_PATTERN.fullmatch(binding["goal_fingerprint"]):
        return {
            "version": 1,
            "action": "claim",
            "mode": "check-proof-only",
            "status": "fail",
            "checks": [
                {
                    "name": "claim-proof",
                    "status": "fail",
                    "code": "claim-goal-required",
                    "message": "Completion proof lookup requires a current alignment session with a valid goal fingerprint.",
                }
            ],
        }
    if args.claim_kind == "completion" and session_id and binding.get("binding_kind") != "handoff":
        return {
            "version": 1,
            "action": "claim",
            "mode": "check-proof-only",
            "status": "fail",
            "checks": [
                {
                    "name": "claim-proof",
                    "status": "fail",
                    "code": "claim-confirmed-handoff-required",
                    "message": "Completion proof lookup requires a confirmed or risk-accepted alignment handoff.",
                }
            ],
        }
    if args.claim_kind == "completion" and session_id and binding.get("confirmation_assurance") not in {
        "recorded-user-assertion",
    }:
        return {
            "version": 1,
            "action": "claim",
            "mode": "check-proof-only",
            "status": "fail",
            "checks": [
                {
                    "name": "claim-proof",
                    "status": "fail",
                    "code": "claim-confirmation-assurance-required",
                    "message": "Completion proof lookup requires an audited confirmation authorization record.",
                }
            ],
        }
    event = latest_claim_event(
        args.path,
        session_id,
        owner_id,
        binding["goal_fingerprint"],
        binding["conversation_id"],
        binding["tool"],
        binding.get("conversation_continuity_id", ""),
        str(args.execution_attempt_id or ""),
        str(binding.get("handoff_hash") or ""),
        verification_scope,
    )
    claim_proof = str(event.get("claim_proof", "")) if event else ""
    verification_status = str(event.get("verification_status", "")) if event else ""
    event_kind = str(event.get("claim_kind") or "") if event else ""
    # Explicit completion workflows pass --claim-kind completion. Keep an
    # unqualified lookup compatible with legacy scoped-claim consumers.
    expected_kind = args.claim_kind or (event_kind if event_kind else "completion")
    if event and (event.get("status") != "pass" or verification_status != "passed"):
        return {
            "version": 1,
            "action": "claim",
            "mode": "check-proof-only",
            "status": "fail",
            "checks": [
                {
                    "name": "claim-proof",
                    "status": "fail",
                    "code": "claim-verification-failed",
                    "message": "The latest verification event failed or was skipped; an older passed claim cannot cover it.",
                    "verification_status": verification_status or "unknown",
                }
            ],
        }
    if event and event_kind != expected_kind:
        code = "claim-partial-only" if event_kind == "partial-verification" else "claim-kind-mismatch"
        return {
            "version": 1,
            "action": "claim",
            "mode": "check-proof-only",
            "status": "fail",
            "checks": [
                {
                    "name": "claim-proof",
                    "status": "fail",
                    "code": code,
                    "message": "The latest verification event is not a completion claim and cannot satisfy the completion gate.",
                    "claim_kind": event_kind or "legacy",
                    "expected_claim_kind": expected_kind,
                }
            ],
        }
    if args.claim_kind == "completion" and not GOAL_FINGERPRINT_PATTERN.fullmatch(binding["goal_fingerprint"]):
        return {
            "version": 1,
            "action": "claim",
            "mode": "check-proof-only",
            "status": "fail",
            "checks": [
                {
                    "name": "claim-proof",
                    "status": "fail",
                    "code": "claim-goal-required",
                    "message": "Completion proof lookup requires a current alignment session with a valid goal fingerprint.",
                }
            ],
        }
    expected_hash = verification_command_hash(expected_command) if expected_command else None
    if event and expected_hash and event.get("verification_command_hash") != expected_hash:
        return {
            "version": 1,
            "action": "claim",
            "mode": "check-proof-only",
            "status": "fail",
            "checks": [
                {
                    "name": "claim-proof",
                    "status": "fail",
                    "code": "claim-verification-command-mismatch",
                    "message": "The latest claim does not cover the requested verification command.",
                    "expected_command_hash": expected_hash,
                    "recorded_command_hash": event.get("verification_command_hash"),
                }
            ],
        }
    if event and event.get("project_root_fingerprint") != project_root_fingerprint(args.path):
        return {
            "version": 1,
            "action": "claim",
            "mode": "check-proof-only",
            "status": "fail",
            "checks": [
                {
                    "name": "claim-proof",
                    "status": "fail",
                    "code": "claim-project-mismatch",
                    "message": "The latest claim belongs to a different project root.",
                }
            ],
        }
    freshness = claim_worktree_freshness(args.path, event) if event else None
    if freshness and freshness["status"] == "fail":
        return {
            "version": 1,
            "action": "claim",
            "mode": "check-proof-only",
            "status": "fail",
            "checks": [
                {
                    "name": "claim-proof",
                    "status": "fail",
                    "code": freshness["code"],
                    "message": freshness["message"],
                    **{key: value for key, value in freshness.items() if key not in {"status", "code", "message"}},
                }
            ],
        }
    if event and event.get("status") == "pass" and verification_status == "passed" and CLAIM_PROOF_PATTERN.match(claim_proof):
        check = {
            "name": "claim-proof",
            "status": "pass",
            "code": "claim-proof-present",
            "message": "Latest verification claim has a valid Tenetora claim proof.",
            "claim_proof": claim_proof,
            "claim_kind": event_kind,
            "verification_command_hash": event.get("verification_command_hash"),
            "verification_scope": list(verification_scope),
        }
        return {
            "version": 1,
            "action": "claim",
            "mode": "check-proof-only",
            "status": "pass",
            "checks": [check],
            "claim_proof": claim_proof,
            "claim_kind": event_kind,
            "verification_command_hash": event.get("verification_command_hash"),
            "verification_scope": list(verification_scope),
            "verification_status": verification_status,
            "session_id": session_id,
            "owner_id": owner_id,
            "conversation_continuity_id": binding.get("conversation_continuity_id", ""),
            "execution_attempt_id": str(args.execution_attempt_id or ""),
            "goal_fingerprint": binding["goal_fingerprint"],
            "handoff_hash": binding.get("handoff_hash", ""),
            "confirmation_assurance": binding.get("confirmation_assurance", ""),
            "confirmation_event_id": binding.get("confirmation_event_id", ""),
        }
    check = {
        "name": "claim-proof",
        "status": "fail",
        "code": "claim-proof-missing",
        "message": (
            "No latest completion verification claim with a valid Tenetora claim proof was found. "
            "Claim proofs are bound to the current session, owner, conversation, tool, and goal; "
            "a proof from another conversation cannot be reused."
        ),
        "recommendation": (
            "For a continued task in a new conversation, first inspect the owner-bound session with "
            "`tenetora alignment --status --session-id <session-id> --owner-id <owner-id> --json`; "
            "then rerun the verification and claim with the matching `--session-id`, `--owner-id`, "
            "`--conversation-id`, and `--tool`. Do not adopt another conversation's goal silently."
        ),
    }
    return {
        "version": 1,
        "action": "claim",
        "mode": "check-proof-only",
        "status": "fail",
        "checks": [check],
    }


def alignment_check(status: str, code: str, message: str, **details: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "decision-alignment",
        "status": status,
        "code": code,
        "message": message,
    }
    payload.update(details)
    return payload


def guard_alignment(args: argparse.Namespace) -> dict[str, Any]:
    risk_level = args.risk_level
    goal = args.goal
    if not risk_level or not goal or not str(goal).strip():
        check = alignment_check(
            "fail",
            "alignment-input-missing",
            "Alignment guard requires both --goal and --risk-level.",
            recommendation="Pass the current goal and its low, medium, or high risk level.",
        )
        return {"version": 1, "action": "alignment", "status": "fail", "checks": [check]}

    root: Path = args.path
    prepare_guard_identity(args)
    closed_lifecycle: dict[str, Any] | None = None
    try:
        alignment_state.sweep_alignment_state(root)
        session_id = alignment_state.requested_session_id(args)
        owner_id = alignment_state.requested_owner_id(args, session_id)
        conversation_id = alignment_state.requested_conversation_id(args)
        if not session_id and conversation_id:
            session_id = alignment_state.session_id_for_conversation(root, conversation_id) or ""
        session = alignment_state.load_session(root, session_id) if session_id else alignment_state.load_session(root)
        current = alignment_state.load_handoff(root, session_id) if session_id else alignment_state.load_current(root)
        if current is not None:
            lifecycle = alignment_state.lifecycle_record(
                root, str(current.get("session_id", current.get("alignment_id", "")))
            )
            if lifecycle is not None and lifecycle.get("status") != "open":
                closed_lifecycle = lifecycle
                current = None
        safe_goal = alignment_state.ensure_safe_text(str(goal), "goal")
    except alignment_state.AlignmentStateError as exc:
        check = alignment_check("fail", "alignment-state-invalid", str(exc))
        return {"version": 1, "action": "alignment", "risk_level": risk_level, "status": "fail", "checks": [check]}

    if session is not None and session_id:
        if not owner_id:
            check = alignment_check(
                "fail",
                "alignment-owner-required",
                "An explicit alignment session requires its owner identity.",
                session_id=session_id,
            )
            return {"version": 1, "action": "alignment", "risk_level": risk_level, "status": "fail", "checks": [check]}
        if session.get("owner_id") != owner_id:
            check = alignment_check(
                "fail",
                "alignment-owner-mismatch",
                "The requested owner cannot use or mutate this alignment session.",
                session_id=session_id,
            )
            return {"version": 1, "action": "alignment", "risk_level": risk_level, "status": "fail", "checks": [check]}
        if conversation_id and session.get("conversation_id") != conversation_id:
            check = alignment_check(
                "fail",
                "alignment-conversation-mismatch",
                "The requested conversation does not own this alignment session.",
                session_id=session_id,
            )
            return {"version": 1, "action": "alignment", "risk_level": risk_level, "status": "fail", "checks": [check]}

    if current is not None and session_id:
        if not owner_id or current.get("owner_id") != owner_id:
            check = alignment_check(
                "fail",
                "alignment-handoff-owner-mismatch",
                "The requested owner cannot consume this alignment handoff.",
                session_id=session_id,
            )
            return {"version": 1, "action": "alignment", "risk_level": risk_level, "status": "fail", "checks": [check]}
        if conversation_id and current.get("conversation_id") != conversation_id:
            check = alignment_check(
                "fail",
                "alignment-handoff-conversation-mismatch",
                "The requested conversation cannot consume this alignment handoff.",
                session_id=session_id,
            )
            return {"version": 1, "action": "alignment", "risk_level": risk_level, "status": "fail", "checks": [check]}

    if current is not None and not session_id:
        check = alignment_check(
            "fail",
            "alignment-owner-required",
            "A confirmed alignment handoff is owner-bound; select its session before consuming it.",
            recommendation="Pass --session-id and --owner-id for the current conversation, or start a new isolated alignment session.",
        )
        return {"version": 1, "action": "alignment", "risk_level": risk_level, "status": "fail", "checks": [check]}

    if session is not None and session.get("status") == "expired":
        check = alignment_check(
            "fail",
            "alignment-session-expired",
            "The alignment session lease has expired and cannot be resumed automatically.",
            session_id=session.get("session_id"),
            session_status="expired",
            recommendation="Start a new alignment session for the current goal.",
        )
        return {"version": 1, "action": "alignment", "risk_level": risk_level, "status": "fail", "checks": [check]}

    if session is not None:
        check = alignment_check(
            "fail",
            "alignment-session-active",
            f"Alignment session is `{session.get('status')}` and must be resolved before restricted execution.",
            session_id=session.get("session_id"),
            session_status=session.get("status"),
            recommendation="Resume, confirm, accept an explicit risk, or abandon the active alignment session.",
        )
        return {"version": 1, "action": "alignment", "risk_level": risk_level, "status": "fail", "checks": [check]}

    if current is None:
        if closed_lifecycle is not None:
            check_status = "fail" if risk_level == "high" else "warn"
            overall = "fail" if risk_level == "high" else "pass"
            check = alignment_check(
                check_status,
                "alignment-goal-closed",
                f"The alignment goal is already {closed_lifecycle.get('status')} and cannot authorize new work.",
                session_id=closed_lifecycle.get("session_id"),
                lifecycle_status=closed_lifecycle.get("status"),
                recommendation="Start a new isolated alignment session for the new work, or explicitly select another open goal.",
            )
            return {"version": 1, "action": "alignment", "risk_level": risk_level, "status": overall, "checks": [check]}
        if risk_level == "low":
            check = alignment_check("pass", "alignment-not-required", "Low-risk work does not require a prior alignment handoff.")
            return {"version": 1, "action": "alignment", "risk_level": risk_level, "status": "pass", "checks": [check]}
        if risk_level == "medium":
            check = alignment_check(
                "warn",
                "alignment-recommended",
                "No current alignment handoff exists; alignment is recommended but not blocking for medium-risk work.",
                recommendation="Ask the user to invoke `tenetora-align` when material decisions remain.",
            )
            return {"version": 1, "action": "alignment", "risk_level": risk_level, "status": "pass", "checks": [check]}
        check = alignment_check(
            "fail",
            "alignment-required",
            "High-risk work requires a confirmed, goal-matching alignment handoff.",
            recommendation="Ask the user to invoke `tenetora-align` before planning or execution.",
        )
        return {"version": 1, "action": "alignment", "risk_level": risk_level, "status": "fail", "checks": [check]}

    scope = args.scope if args.scope is not None else list(current.get("scope", []))
    non_goals = args.non_goal if args.non_goal is not None else list(current.get("non_goals", []))
    acceptance = (
        args.acceptance_criterion
        if args.acceptance_criterion is not None
        else list(current.get("acceptance_criteria", []))
    )
    actual_fingerprint = alignment_state.goal_fingerprint(safe_goal, scope, non_goals, acceptance)
    expected_fingerprint = str(current.get("goal_fingerprint", ""))
    expected_handoff_hash = str(current.get("handoff_hash", ""))
    actual_handoff_hash = alignment_state.handoff_hash(current)
    handoff_path = current.get("handoff_relative_path")
    handoff_missing = bool(handoff_path) and not (root / str(handoff_path)).is_file()
    stale = alignment_state.is_stale(expected_fingerprint, actual_fingerprint)
    invalid_handoff = not expected_handoff_hash or expected_handoff_hash != actual_handoff_hash or handoff_missing

    if stale or invalid_handoff:
        code = "alignment-stale" if stale else "alignment-handoff-invalid"
        message = (
            "Current alignment does not match the requested goal, scope, non-goals, or acceptance criteria."
            if stale
            else "Current alignment handoff hash or relative document pointer is invalid."
        )
        if risk_level == "high":
            check_status, overall = "fail", "fail"
        else:
            check_status, overall = "warn", "pass"
        check = alignment_check(
            check_status,
            code,
            message,
            expected_fingerprint=expected_fingerprint,
            actual_fingerprint=actual_fingerprint,
            recommendation="Run `tenetora-align` again for the current goal before restricted work.",
        )
        return {"version": 1, "action": "alignment", "risk_level": risk_level, "status": overall, "checks": [check]}

    confirmation = current.get("confirmation") if isinstance(current.get("confirmation"), dict) else {}
    confirmation_assurance = str(confirmation.get("assurance") or "legacy-unattested")
    if confirmation_assurance != "recorded-user-assertion":
        check_status = "fail" if risk_level == "high" else "warn"
        overall = "fail" if risk_level == "high" else "pass"
        check = alignment_check(
            check_status,
            "alignment-confirmation-unattested",
            "Current alignment handoff has no audited user-message confirmation record.",
            alignment_id=current.get("alignment_id"),
            alignment_status=current.get("status"),
            confirmation_assurance=confirmation_assurance,
            recommendation="Resume or start an owner-bound alignment, request confirmation again, and record the matching user-message event before restricted high-risk work.",
        )
        return {
            "version": 1,
            "action": "alignment",
            "risk_level": risk_level,
            "status": overall,
            "checks": [check],
        }

    proof = f"ah-align-{secrets.token_hex(16)}"
    check = alignment_check(
        "pass",
        "alignment-confirmed",
        "Current alignment is confirmed and matches the requested goal fingerprint.",
        alignment_id=current.get("alignment_id"),
        alignment_status=current.get("status"),
        goal_fingerprint=expected_fingerprint,
        handoff_hash=expected_handoff_hash,
        confirmation_assurance=confirmation_assurance,
    )
    return {
        "version": 1,
        "action": "alignment",
        "risk_level": risk_level,
        "status": "pass",
        "checks": [check],
        "alignment_id": current.get("alignment_id"),
        "alignment_status": current.get("status"),
        "goal_fingerprint": expected_fingerprint,
        "handoff_hash": expected_handoff_hash,
        "confirmation_assurance": confirmation_assurance,
        "confirmation_event_id": confirmation.get("event_id", ""),
        "handoff_relative_path": handoff_path,
        "accepted_risks": current.get("accepted_risks", []),
        "session_id": current.get("session_id", current.get("alignment_id")),
        "owner_id": current.get("owner_id", ""),
        "conversation_id": current.get("conversation_id", ""),
        "alignment_proof": proof,
        "authorization_scope": ["decision-alignment-only"],
        "does_not_authorize": ["implementation", "commit", "push", "deployment", "release"],
    }


def external_inputs(args: argparse.Namespace, prompt_guard: Any) -> list[Any]:
    inputs: list[Any] = []
    for index, text in enumerate(args.text, start=1):
        label = args.source if len(args.text) == 1 else f"{args.source}:text-{index}"
        inputs.append(prompt_guard.PromptInput(label=label, text=text))
    for path in args.file:
        inputs.append(prompt_guard.PromptInput(label=args.source, text=path.read_text(encoding="utf-8", errors="ignore"), path=str(path)))
    if args.stdin:
        inputs.append(prompt_guard.PromptInput(label=args.source, text=sys.stdin.read()))
    return inputs


def guard_external_input(args: argparse.Namespace) -> dict[str, Any]:
    prompt_guard = load_prompt_guard_module()
    inputs = external_inputs(args, prompt_guard)
    if not inputs:
        return {
            "version": 1,
            "action": "external-input",
            "status": "fail",
            "checks": [
                {
                    "name": "prompt-guard",
                    "status": "fail",
                    "code": "external-input-missing",
                    "message": "No untrusted input was supplied.",
                    "recommendation": "Pass --text, --file, or --stdin for external input.",
                }
            ],
            "findings": [],
        }
    findings: list[dict[str, Any]] = []
    for item in inputs:
        findings.extend(prompt_guard.scan_input(item))
    high_risk = any(str(item.get("severity")) == "high" for item in findings if isinstance(item, dict))
    return {
        "version": 1,
        "action": "external-input",
        "status": "fail" if findings else "pass",
        "checks": [{"name": "prompt-guard", "status": "fail" if findings else "pass"}],
        "findings": findings,
        "semantic_review": {
            "recommendation": "recommended" if high_risk else "not-needed",
            "role": "security-auditor" if high_risk else None,
            "precondition": "mechanical-prompt-guard-completed",
            "input_policy": "bounded-redacted-findings-only",
            "permissions": {"read": True, "write": False, "shell": False, "network": False},
            "resolution_order": ["dedicated", "builtin-role-injection", "main-self-review"],
            "builtin_isolation_allowed": ["hard", "inherited"],
            "prompt_only_allowed": False,
            "spawns_agent": False,
        },
    }


def record(root: Path, payload: dict[str, Any]) -> None:
    action = str(payload.get("action", "unknown"))
    status = str(payload.get("status", "unknown"))
    if action == "claim" and (payload.get("mode") == "check-proof-only" or not payload.get("claim_kind")):
        return
    if action == "alignment":
        event = {
            "type": "alignment-guard",
            "action": "alignment",
            "status": status,
            "risk_level": payload.get("risk_level"),
            "alignment_id": payload.get("alignment_id"),
            "session_id": payload.get("session_id"),
            "owner_id": payload.get("owner_id"),
            "conversation_id": payload.get("conversation_id"),
            "goal_fingerprint": payload.get("goal_fingerprint"),
            "handoff_hash": payload.get("handoff_hash"),
            "alignment_proof": payload.get("alignment_proof"),
            "source": "tenetora guard",
        }
    elif action == "claim":
        event: dict[str, Any] = {
            "type": "verification-claim",
            "action": "claim",
            "status": status,
            "claim_kind": payload.get("claim_kind"),
            "verification_status": payload.get("verification_status"),
            "verification_command_hash": payload.get("verification_command_hash"),
            "verification_command_length": payload.get("verification_command_length", 0),
            "claim_proof": payload.get("claim_proof"),
            "session_id": payload.get("session_id"),
            "owner_id": payload.get("owner_id"),
            "conversation_id": payload.get("conversation_id"),
            "conversation_continuity_id": payload.get("conversation_continuity_id"),
            "tool": payload.get("tool"),
            "provider": payload.get("provider"),
            "model": payload.get("model"),
            "execution_attempt_id": payload.get("execution_attempt_id"),
            "goal_fingerprint": payload.get("goal_fingerprint"),
            "handoff_hash": payload.get("handoff_hash"),
            "confirmation_assurance": payload.get("confirmation_assurance"),
            "confirmation_event_id": payload.get("confirmation_event_id"),
            "attribution": payload.get("attribution"),
            "attribution_warning": payload.get("attribution_warning"),
            "project_root_fingerprint": payload.get("project_root_fingerprint"),
            "verification_scope": payload.get("verification_scope", []),
            "git_state_fingerprint_version": payload.get("git_state_fingerprint_version"),
            "git_state_fingerprint": payload.get("git_state_fingerprint"),
            "git_state_entries_version": payload.get("git_state_entries_version"),
            "git_state_entries": payload.get("git_state_entries"),
            "verification_timestamp": payload.get("verification_timestamp"),
            "source": "tenetora guard",
        }
    elif action == "external-input":
        event = {
            "type": "prompt-guard",
            "action": "external-input",
            "status": status,
            "finding_count": len(payload.get("findings", [])) if isinstance(payload.get("findings"), list) else 0,
            "finding_types": sorted({str(item.get("type")) for item in payload.get("findings", []) if isinstance(item, dict)}),
            "source": "tenetora guard",
        }
    else:
        checks = payload.get("checks", [])
        event = {
            "type": "guard-action",
            "action": action,
            "status": status,
            "operation": payload.get("operation"),
            "checks": [str(item.get("name")) for item in checks if isinstance(item, dict)],
            "source": "tenetora guard",
        }
        if action == "commit":
            event.update(
                {
                    "staged_paths": payload.get("staged_paths", []),
                    "staged_set_fingerprint": payload.get("staged_set_fingerprint"),
                    "git_head": payload.get("git_head"),
                    "git_identity": payload.get("git_identity"),
                }
            )
    append_event(root, event)


def print_text(payload: dict[str, Any]) -> None:
    chinese = os.environ.get("TENETORA_LANG", "").strip().lower() in {"zh", "zh-cn", "zh-hans", "chinese", "中文"}
    print(
        f"tenetora guard {payload['action']}：{payload['status']}"
        if chinese
        else f"tenetora guard {payload['action']}: {payload['status']}"
    )
    for check in payload.get("checks", []):
        if isinstance(check, dict):
            print(f"- {check.get('name')}: {check.get('status')}")
            if check.get("message"):
                print(f"  {check['message']}")
    findings = payload.get("findings", [])
    if findings:
        print("问题：" if chinese else "Findings:")
        for item in findings:
            if isinstance(item, dict):
                print(f"- {item.get('severity')} {item.get('type')}: {item.get('message')}")
    if payload.get("claim_proof"):
        print(f"claim proof: {payload['claim_proof']}")
    if payload.get("alignment_proof"):
        print(f"alignment proof: {payload['alignment_proof']}")
        print(
            "对齐凭证只授权决策对齐，不授权实现、提交、推送、部署或发布。"
            if chinese
            else "alignment proof authorizes decision alignment only; it does not authorize implementation, commit, push, deployment, or release"
        )


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root: Path = args.path
    if args.action == "commit" and not (root / ".tenetora").is_dir():
        resolved = resolve_worktree_governance_root(root, args.git_path or root)
        if resolved is not None:
            root = resolved
            args.path = resolved
    if not (root / ".tenetora").is_dir():
        payload = {
            "version": 1,
            "action": args.action,
            "status": "error",
            "message": harness_missing_message(root),
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(payload["message"], file=sys.stderr)
        return 2
    if args.action == "commit":
        payload = guard_commit(args)
    elif args.action == "rules":
        payload = guard_rules(root)
    elif args.action == "claim":
        payload = guard_claim(args)
    elif args.action == "alignment":
        payload = guard_alignment(args)
    else:
        payload = guard_external_input(args)
    if (
        args.action == "claim"
        and payload.get("mode") != "check-proof-only"
        and payload.get("status") == "pass"
        and payload.get("claim_kind") == "completion"
        and payload.get("claim_proof")
        and payload.get("session_id")
        and payload.get("owner_id")
    ):
        try:
            closed = alignment_state.mark_completed(
                root,
                str(payload["session_id"]),
                str(payload["owner_id"]),
                str(payload["claim_proof"]),
            )
            if closed is None:
                raise alignment_state.AlignmentStateError("no open lifecycle record exists for this handoff")
            payload["alignment_lifecycle_status"] = "completed"
        except alignment_state.AlignmentStateError as exc:
            payload = claim_failure(
                "claim-alignment-close-failed",
                f"Verification passed, but the alignment goal could not be closed: {exc}",
                recommendation="Keep the verification evidence and explicitly inspect the alignment lifecycle before retrying.",
            )
    record(root, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_text(payload)
    return 0 if payload.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
