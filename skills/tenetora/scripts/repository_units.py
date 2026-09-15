#!/usr/bin/env python3
"""Inspect parent-repository gitlinks without treating ordinary nested paths as modules."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

from path_security import harness_missing_message, is_redirected_path  # noqa: E402
from alignment_state import state_lock  # noqa: E402
from harness_io import atomic_write_text  # noqa: E402
from worktree_fingerprint import (  # noqa: E402
    WORKTREE_FINGERPRINT_VERSION,
    git_state_fingerprint,
    normalize_scopes,
    verification_scopes_intersect,
)


GITLINK_MODE = "160000"
HEX_OBJECT = re.compile(r"^[0-9a-f]{40}$")
CLAIM_REFERENCE = re.compile(r"^ah-(?:claim|align)-[0-9a-f]{32}$")
CLAIM_PROOF = re.compile(r"^ah-claim-[0-9a-f]{32}$")
MAX_DIRTY_PATHS = 50
REGISTRY_REL = ".tenetora/state/repository-units.json"
REGISTRY_VERSION = 1


def run_git(root: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        text=text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def normalize_relative(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip().replace("\\", "/")
    if (
        not value
        or "\x00" in value
        or value.startswith("/")
        or value.startswith("~/")
        or re.match(r"^[A-Za-z]:/", value)
    ):
        return None
    parts = [part for part in value.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def _path_component_problems(
    root: Path,
    relative: str,
    *,
    include_final: bool = True,
    symlink_code: str = "repository-unit-path-symlink",
) -> list[str]:
    parts = relative.split("/")
    if not include_final:
        parts = parts[:-1]
    current = root
    for part in parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError:
            return ["repository-unit-path-unreadable"]
        if is_redirected_path(current):
            return [symlink_code]
    return []


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="surrogateescape")


def _pointer(raw: str) -> str | None:
    return raw if HEX_OBJECT.fullmatch(raw) and set(raw) != {"0"} else None


def staged_gitlink_changes(root: Path) -> list[dict[str, Any]]:
    result = run_git(
        root,
        "diff",
        "--cached",
        "--raw",
        "-z",
        "--full-index",
        "--abbrev=40",
        "--no-renames",
        "--diff-filter=ACMRDT",
        text=False,
    )
    if result.returncode != 0:
        return []
    fields = bytes(result.stdout).split(b"\0")
    changes: list[dict[str, Any]] = []
    index = 0
    while index < len(fields):
        metadata = fields[index]
        index += 1
        if not metadata:
            continue
        path_field = fields[index] if index < len(fields) else b""
        index += 1
        tokens = _decode(metadata).split()
        if len(tokens) < 5:
            continue
        old_mode, new_mode, old_oid, new_oid, status = tokens[:5]
        old_mode = old_mode.lstrip(":")
        path = normalize_relative(_decode(path_field))
        if path is None or (old_mode != GITLINK_MODE and new_mode != GITLINK_MODE):
            continue
        kind = status[0]
        changes.append(
            {
                "gitlink_path": path,
                "change": {"A": "added", "D": "deleted", "M": "updated"}.get(kind, "changed"),
                "old_pointer": _pointer(old_oid),
                "staged_pointer": _pointer(new_oid),
                "old_mode": old_mode,
                "new_mode": new_mode,
            }
        )
    return changes


def indexed_gitlinks(root: Path) -> tuple[dict[str, str], list[str]]:
    result = run_git(root, "ls-files", "--stage", "-z", text=False)
    if result.returncode != 0:
        return {}, []
    links: dict[str, str] = {}
    conflicts: set[str] = set()
    for record in bytes(result.stdout).split(b"\0"):
        if not record or b"\t" not in record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        tokens = _decode(metadata).split()
        path = normalize_relative(_decode(raw_path))
        if len(tokens) < 3 or tokens[0] != GITLINK_MODE or not path:
            continue
        stage = tokens[2]
        if stage != "0":
            conflicts.add(path)
            continue
        if _pointer(tokens[1]):
            links[path] = tokens[1]
    return links, sorted(conflicts)


def current_gitlinks(root: Path) -> dict[str, str]:
    return indexed_gitlinks(root)[0]


def _read_json(path: Path) -> dict[str, Any] | None:
    if is_redirected_path(path) or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _load_registry(root: Path) -> tuple[dict[str, Any] | None, list[str], bool]:
    """Load the explicit repository-unit registry without following redirects."""

    path = root / REGISTRY_REL
    try:
        exists = path.exists() or path.is_symlink()
    except OSError:
        return None, ["repository-unit-registry-unreadable"], True
    if not exists:
        return None, [], False
    if _path_component_problems(root, REGISTRY_REL, symlink_code="repository-unit-registry-symlink"):
        return None, ["repository-unit-registry-symlink"], True
    payload = _read_json(path)
    if payload is None or payload.get("version") != REGISTRY_VERSION or not isinstance(payload.get("units"), list):
        return None, ["repository-unit-registry-invalid"], True
    return payload, [], True


def _entry_for_path(items: object, gitlink_path: str) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    return [
        item
        for item in items
        if isinstance(item, dict) and normalize_relative(item.get("gitlink_path")) == gitlink_path
    ]


def _project_root_fingerprint(root: Path) -> str:
    return hashlib.sha256(str(root.resolve(strict=False)).encode("utf-8", errors="surrogatepass")).hexdigest()


def _registry_integrity_problems(
    governance_root: Path,
    git_root: Path,
    registry: dict[str, Any] | None,
) -> list[str]:
    if registry is None:
        return []
    indexed, _ = indexed_gitlinks(git_root)
    registered: set[str] = set()
    problems: list[str] = []
    units = registry.get("units")
    if not isinstance(units, list):
        return ["repository-unit-registry-invalid"]
    for item in units:
        if not isinstance(item, dict):
            problems.append("repository-unit-registry-entry-invalid")
            continue
        path = normalize_relative(item.get("gitlink_path"))
        if path is None:
            problems.append("repository-unit-registration-path-invalid")
            continue
        if path in registered:
            problems.append("repository-unit-registration-duplicate")
        registered.add(path)
        if item.get("kind") != "repository-unit":
            problems.append("repository-unit-kind-missing")
        if normalize_relative(item.get("repo_root")) != path:
            problems.append("repository-unit-registration-path-mismatch")
        evidence = normalize_relative(item.get("evidence"))
        if evidence is None:
            problems.append("verification-evidence-reference-missing")
        else:
            problems.extend(
                _path_component_problems(
                    governance_root,
                    evidence,
                    symlink_code="verification-evidence-path-symlink",
                )
            )
        module = item.get("module")
        if not isinstance(module, str) or not module.strip() or any(char in module for char in ("\n", "\r", "\x00")):
            problems.append("repository-unit-module-invalid")
        if path not in indexed:
            problems.append("repository-unit-registration-orphan")
    missing = set(indexed) - registered
    if missing:
        problems.append("repository-unit-registration-missing")
    return sorted(set(problems))


def _read_claim_events(child: Path) -> list[dict[str, Any]] | None:
    relative = ".tenetora/state/governance-trail.json"
    if _path_component_problems(child, relative, symlink_code="verification-claim-trail-symlink"):
        return None
    path = child / relative
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    events = payload.get("events") if isinstance(payload, dict) else None
    return [item for item in events if isinstance(item, dict)] if isinstance(events, list) else None


def _claim_proof_problems(child: Path, proof: str) -> list[str]:
    if not CLAIM_PROOF.fullmatch(proof):
        return ["verification-claim-proof-invalid"]
    events = _read_claim_events(child)
    if events is None:
        return ["verification-claim-proof-missing"]
    matching_index = None
    matching_event: dict[str, Any] | None = None
    for index, event in enumerate(events):
        if event.get("type") == "verification-claim" and event.get("claim_proof") == proof:
            matching_index = index
            matching_event = event
    if matching_event is None or matching_index is None:
        return ["verification-claim-proof-missing"]
    problems: list[str] = []
    if matching_event.get("status") != "pass" or matching_event.get("verification_status") != "passed":
        problems.append("verification-claim-not-passed")
    if matching_event.get("claim_kind") not in {"completion", "partial-verification"}:
        problems.append("verification-claim-kind-invalid")
    if matching_event.get("project_root_fingerprint") != _project_root_fingerprint(child):
        problems.append("verification-claim-project-mismatch")
    raw_scope = matching_event.get("verification_scope", [])
    if not isinstance(raw_scope, list) or any(not isinstance(item, str) for item in raw_scope):
        problems.append("verification-claim-scope-invalid")
        normalized_scope: tuple[str, ...] = ()
    else:
        try:
            normalized_scope = normalize_scopes(raw_scope)
        except ValueError:
            problems.append("verification-claim-scope-invalid")
            normalized_scope = ()
    recorded_version = matching_event.get("git_state_fingerprint_version")
    recorded_fingerprint = matching_event.get("git_state_fingerprint")
    current_fingerprint = git_state_fingerprint(child, normalized_scope)
    if recorded_version != WORKTREE_FINGERPRINT_VERSION:
        problems.append("verification-claim-fingerprint-version-invalid")
    if not isinstance(recorded_fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", recorded_fingerprint):
        problems.append("verification-claim-fingerprint-missing")
    elif current_fingerprint is None or current_fingerprint != recorded_fingerprint:
        problems.append("verification-claim-stale")
    for event in events[matching_index + 1 :]:
        if event.get("type") != "verification-claim":
            continue
        if event.get("status") == "pass" and event.get("verification_status") == "passed":
            continue
        later_scope = event.get("verification_scope", [])
        if not isinstance(later_scope, list) or any(not isinstance(item, str) for item in later_scope):
            problems.append("verification-claim-superseded")
            break
        try:
            overlaps = verification_scopes_intersect(normalized_scope, later_scope)
        except ValueError:
            overlaps = True
        if overlaps:
            problems.append("verification-claim-superseded")
            break
    return sorted(set(problems))


def repository_unit_registration_hint(gitlink_path: str, module: str | None = None) -> str:
    module_value = module or gitlink_path.rsplit("/", 1)[-1]
    return (
        "tenetora repository-units repair --path . "
        f"--gitlink-path {gitlink_path} --module {module_value} "
        '--verification-command "<passed command>" '
        "--claim-proof <child-claim-proof>"
    )


def _evidence_entry(
    governance_root: Path,
    gitlink_path: str,
    child: Path | None = None,
) -> tuple[dict[str, Any] | None, list[str], str]:
    registry, registry_problems, registry_present = _load_registry(governance_root)
    if registry_problems:
        return None, registry_problems, "invalid"
    matches = _entry_for_path(registry.get("units") if registry else [], gitlink_path)
    if matches:
        index_path_problems: list[str] = []
        index: dict[str, Any] | None = None
    elif registry_present:
        return None, ["repository-unit-registration-missing", "verification-evidence-missing"], "missing"
    else:
        index_path_problems = _path_component_problems(
            governance_root,
            ".tenetora/state/modules/index.json",
            symlink_code="verification-state-path-symlink",
        )
        if index_path_problems:
            return None, index_path_problems, "invalid"
        index = _read_json(governance_root / ".tenetora/state/modules/index.json")
    if not matches:
        if index is None:
            problems = ["module-index-missing", "verification-evidence-missing"]
            problems.extend(index_path_problems)
            return None, sorted(set(problems)), "invalid" if index_path_problems else "missing"
        modules = index.get("modules")
        if not isinstance(modules, list):
            return None, ["module-index-invalid"], "invalid"
        matches = _entry_for_path(modules, gitlink_path)
    if len(matches) != 1:
        return None, [
            "repository-unit-registration-missing" if not matches else "repository-unit-registration-ambiguous",
            "verification-evidence-missing",
        ], "missing"
    entry = matches[0]
    if entry.get("kind") != "repository-unit":
        return None, ["repository-unit-kind-missing"], "invalid"
    if normalize_relative(entry.get("repo_root")) != gitlink_path:
        return None, ["repository-unit-registration-path-mismatch"], "invalid"
    evidence_rel = normalize_relative(entry.get("evidence"))
    if evidence_rel is None:
        return None, ["verification-evidence-reference-missing"], "missing"
    evidence_path_problems = _path_component_problems(
        governance_root,
        evidence_rel,
        symlink_code="verification-evidence-path-symlink",
    )
    if evidence_path_problems:
        return None, [*evidence_path_problems, "verification-evidence-missing"], "invalid"
    evidence = _read_json(governance_root / evidence_rel)
    if evidence is None:
        return None, ["verification-evidence-missing"], "missing"
    problems: list[str] = []
    if evidence.get("kind") != "repository-unit":
        problems.append("verification-evidence-kind-invalid")
    if normalize_relative(evidence.get("repo_root")) != gitlink_path:
        problems.append("verification-evidence-root-mismatch")
    if normalize_relative(evidence.get("gitlink_path")) != gitlink_path:
        problems.append("verification-evidence-path-mismatch")
    verification = evidence.get("verification")
    if not isinstance(verification, dict) or verification.get("status") != "passed":
        problems.append("verification-not-passed")
    else:
        if not str(verification.get("command") or "").strip():
            problems.append("verification-command-missing")
        reference = str(verification.get("evidence") or "").strip()
        if not reference:
            problems.append("verification-evidence-reference-missing")
        elif not CLAIM_REFERENCE.fullmatch(reference):
            reference_rel = normalize_relative(reference)
            if (
                reference_rel is None
                or _path_component_problems(
                    governance_root,
                    reference_rel,
                    symlink_code="verification-evidence-reference-symlink",
                )
                or not (governance_root / reference_rel).is_file()
            ):
                problems.append("verification-evidence-reference-invalid")
        elif child is None or not CLAIM_PROOF.fullmatch(reference):
            problems.append("verification-claim-proof-missing")
        else:
            problems.extend(_claim_proof_problems(child, reference))
    return evidence, problems, "pass" if not problems else "invalid"


def _child_state(parent: Path, path: str) -> tuple[Path, str | None, list[str], list[str]]:
    child = parent / path
    problems: list[str] = []
    if is_redirected_path(child):
        return child, None, ["repository-unit-symlink"], []
    problems.extend(_path_component_problems(parent, path, include_final=False))
    if problems:
        return child, None, problems, []
    if not child.is_dir():
        return child, None, ["repository-unit-directory-missing"], []
    top = run_git(child, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return child, None, ["repository-unit-not-git"], []
    try:
        if Path(top.stdout.strip()).resolve() != child.resolve():
            problems.append("repository-unit-root-mismatch")
    except OSError:
        problems.append("repository-unit-root-unreadable")
    head_result = run_git(child, "rev-parse", "HEAD")
    head = head_result.stdout.strip() if head_result.returncode == 0 else None
    if not head or not HEX_OBJECT.fullmatch(head):
        problems.append("child-head-unavailable")
        head = None
    dirty_result = run_git(child, "status", "--porcelain=v1", "--untracked-files=all")
    if dirty_result.returncode != 0:
        problems.append("repository-unit-status-unavailable")
    dirty = [line for line in dirty_result.stdout.splitlines() if line.strip()][:MAX_DIRTY_PATHS]
    return child, head, problems, dirty


def _target_reachable(child: Path, target: str | None, head: str | None) -> bool | None:
    if not target or not child.is_dir() or not head:
        return None
    if run_git(child, "cat-file", "-e", f"{target}^{{commit}}").returncode != 0:
        return False
    return run_git(child, "merge-base", "--is-ancestor", target, head).returncode == 0


def _validate_change(governance_root: Path, git_root: Path, change: dict[str, Any]) -> dict[str, Any]:
    path = str(change["gitlink_path"])
    expected = change.get("staged_pointer") or change.get("old_pointer")
    problems: list[str] = []
    child, head, child_problems, dirty_paths = _child_state(git_root, path)
    evidence, evidence_problems, evidence_status = _evidence_entry(governance_root, path, child)
    problems.extend(evidence_problems)
    if change["change"] != "deleted":
        problems.extend(child_problems)
        reachable = _target_reachable(child, expected, head)
        if reachable is False:
            problems.append("target-unreachable")
        if head and expected != head:
            problems.append("child-head-mismatch")
        if dirty_paths:
            problems.append("child-worktree-dirty")
    else:
        reachable = _target_reachable(child, expected, head)
        if child.exists() and child_problems:
            problems.extend(child_problems)
        if reachable is False:
            problems.append("target-unreachable")
        if head and expected != head:
            problems.append("child-head-mismatch")
    if evidence is not None and expected and evidence.get("git_head") != expected:
        problems.append("verification-evidence-head-mismatch")
    return {
        **change,
        "worktree_head": head,
        "target_reachable": reachable,
        "dirty": bool(dirty_paths),
        "dirty_paths": dirty_paths,
        "evidence_status": evidence_status,
        "problems": sorted(set(problems)),
    }


def _status_problems(
    pointer: str,
    head: str | None,
    child_problems: list[str],
    evidence: dict[str, Any] | None,
    evidence_problems: list[str],
    reachable: bool | None,
) -> list[str]:
    problems = list(child_problems) + list(evidence_problems)
    if head and pointer != head:
        problems.append("child-head-mismatch")
    if reachable is False:
        problems.append("target-unreachable")
    if evidence is not None and evidence.get("git_head") != pointer:
        problems.append("verification-evidence-head-mismatch")
    return sorted(set(problems))


def repository_units_check(governance_root: Path, git_root: Path) -> dict[str, Any]:
    changes = staged_gitlink_changes(git_root)
    _, index_conflicts = indexed_gitlinks(git_root)
    evidence_root = governance_root if governance_root.resolve() == git_root.resolve() else git_root
    registry, registry_problems, registry_present = _load_registry(evidence_root)
    if registry_present and not registry_problems:
        registry_problems = _registry_integrity_problems(evidence_root, git_root, registry)
    if not changes and not index_conflicts and not registry_problems:
        return {
            "name": "repository-units",
            "status": "pass",
            "code": "no-staged-gitlink-changes",
            "message": "No staged gitlink changes require repository-unit validation.",
            "changes": [],
            "codes": [],
        }
    validated = [_validate_change(evidence_root, git_root, item) for item in changes]
    codes = sorted(set(registry_problems) | {problem for item in validated for problem in item["problems"]})
    if index_conflicts:
        codes = sorted(set(codes) | {"repository-unit-index-conflict"})
    passed = not codes
    return {
        "name": "repository-units",
        "status": "pass" if passed else "fail",
        "code": "repository-units-validated" if passed else "repository-unit-invalid",
        "message": "Staged gitlink changes match child repositories and verification evidence."
        if passed
        else "Staged gitlink changes require repair before commit; use repository-units inspect and repair with passed evidence.",
        "changes": validated,
        "conflicts": index_conflicts,
        "codes": codes,
        "remediation": [
            repository_unit_registration_hint(str(item.get("gitlink_path")))
            for item in validated
            if item.get("problems")
        ],
    }


def inspect_repository_units(governance_root: Path, git_root: Path) -> dict[str, Any]:
    staged = staged_gitlink_changes(git_root)
    indexed, index_conflicts = indexed_gitlinks(git_root)
    evidence_root = governance_root if governance_root.resolve() == git_root.resolve() else git_root
    registry, registry_problems, registry_present = _load_registry(evidence_root)
    if registry_present and not registry_problems:
        registry_problems = _registry_integrity_problems(evidence_root, git_root, registry)
    units: list[dict[str, Any]] = []
    for path, pointer in sorted(indexed.items()):
        child, head, child_problems, dirty_paths = _child_state(git_root, path)
        evidence, evidence_problems, evidence_status = _evidence_entry(evidence_root, path, child)
        reachable = _target_reachable(child, pointer, head)
        units.append(
            {
                "gitlink_path": path,
                "staged_pointer": pointer,
                "worktree_head": head,
                "pointer_matches_head": pointer == head if head else False,
                "target_reachable": reachable,
                "dirty": bool(dirty_paths),
                "dirty_paths": dirty_paths,
                "evidence_status": evidence_status,
                "verification_status": (
                    evidence.get("verification", {}).get("status")
                    if isinstance(evidence, dict) and isinstance(evidence.get("verification"), dict)
                    else "missing"
                ),
                "problems": _status_problems(
                    pointer,
                    head,
                    child_problems,
                    evidence,
                    evidence_problems,
                    reachable,
                ),
            }
        )
    staged_check = repository_units_check(governance_root, git_root) if staged else None
    problems = [*registry_problems, *[problem for unit in units for problem in unit["problems"]]]
    if index_conflicts:
        problems.append("repository-unit-index-conflict")
    if staged_check:
        problems.extend(staged_check["codes"])
    return {
        "version": 1,
        "status": "pass" if not problems else "attention",
        "staged_status": "conflicted" if index_conflicts else ("changed" if staged else "clean"),
        "staged_changes": staged_check["changes"] if staged_check else [],
        "units": units,
        "problems": sorted(set(problems)),
    }


def _registry_entry_path(root: Path, gitlink_path: str) -> Path:
    digest = hashlib.sha256(gitlink_path.encode("utf-8")).hexdigest()[:16]
    return root / ".tenetora" / "state" / "repository-units" / f"{digest}.json"


def _registration_failure(codes: list[str], message: str) -> dict[str, Any]:
    return {
        "version": REGISTRY_VERSION,
        "status": "fail",
        "code": "repository-unit-registration-invalid",
        "message": message,
        "codes": sorted(set(codes)),
    }


def _registration_payload(args: argparse.Namespace) -> dict[str, Any]:
    root = args.path.resolve()
    raw_path = str(args.gitlink_path or "")
    gitlink_path = normalize_relative(raw_path)
    if gitlink_path is None:
        return _registration_failure(["repository-unit-path-invalid"], "--gitlink-path must be a safe project-relative path.")
    module = str(args.module or "").strip()
    if not module or any(token in module for token in ("\n", "\r", "\x00")):
        return _registration_failure(["repository-unit-module-invalid"], "--module must be a non-empty single-line label.")
    if _path_component_problems(root, gitlink_path):
        return _registration_failure(["repository-unit-path-symlink"], "The repository-unit path contains a redirected component.")
    indexed, conflicts = indexed_gitlinks(root)
    if gitlink_path in conflicts:
        return _registration_failure(["repository-unit-index-conflict"], "The repository-unit gitlink has unresolved index stages.")
    staged = next((item for item in staged_gitlink_changes(root) if item.get("gitlink_path") == gitlink_path), None)
    pointer = str(staged.get("staged_pointer") or "") if staged else indexed.get(gitlink_path, "")
    if not _pointer(pointer):
        return _registration_failure(["repository-unit-gitlink-missing"], "The path is not an indexed or staged gitlink with a valid commit pointer.")
    child, head, child_problems, dirty_paths = _child_state(root, gitlink_path)
    if child_problems:
        return _registration_failure(child_problems, "The child repository cannot be inspected safely.")
    if head != pointer:
        return _registration_failure(["child-head-mismatch"], "The child HEAD must match the indexed or staged gitlink pointer.")
    if dirty_paths:
        return _registration_failure(["child-worktree-dirty"], "Commit or stash the child worktree before registering verification evidence.")
    requested_head = str(args.git_head or "").strip()
    if requested_head and (not _pointer(requested_head) or requested_head != head):
        return _registration_failure(["repository-unit-git-head-mismatch"], "--git-head must match the child repository HEAD.")
    command = str(args.verification_command or "").strip()
    if not command:
        return _registration_failure(["verification-command-missing"], "--verification-command is required and must be the command that actually passed.")
    reference = str(args.claim_proof or args.verification_evidence or "").strip()
    if not reference:
        return _registration_failure(
            ["verification-evidence-reference-missing"],
            "Provide --claim-proof or --verification-evidence for the passed verification.",
        )
    if args.claim_proof:
        if not CLAIM_PROOF.fullmatch(reference):
            return _registration_failure(["verification-evidence-reference-invalid"], "--claim-proof must be a valid Tenetora claim proof.")
    else:
        evidence_reference = normalize_relative(reference)
        if evidence_reference is None or _path_component_problems(
            root,
            evidence_reference,
            symlink_code="verification-evidence-reference-symlink",
        ) or not (root / evidence_reference).is_file():
            return _registration_failure(["verification-evidence-reference-invalid"], "--verification-evidence must name an existing regular project-relative file.")
    entry_path = _registry_entry_path(root, gitlink_path)
    entry_relative = entry_path.relative_to(root).as_posix()
    entry_path_problems = _path_component_problems(
        root,
        entry_relative,
        symlink_code="repository-unit-evidence-path-symlink",
    )
    if entry_path_problems:
        return _registration_failure(
            entry_path_problems,
            "The repository-unit evidence path is redirected and cannot be replaced safely.",
        )
    evidence = {
        "version": 1,
        "kind": "repository-unit",
        "module": module,
        "repo_root": gitlink_path,
        "gitlink_path": gitlink_path,
        "git_head": head,
        "verification": {
            "status": "passed",
            "command": command,
            "evidence": reference,
        },
    }
    return {
        "version": REGISTRY_VERSION,
        "status": "pass",
        "code": "repository-unit-registration-ready",
        "message": "Repository-unit registration is ready to write.",
        "gitlink_path": gitlink_path,
        "module": module,
        "git_head": head,
        "entry_path": entry_path,
        "evidence": evidence,
        "force": bool(args.force or args.operation == "repair"),
    }


def register_repository_unit(args: argparse.Namespace) -> dict[str, Any]:
    prepared = _registration_payload(args)
    if prepared.get("status") != "pass":
        return prepared
    root = args.path.resolve()
    registry_path = root / REGISTRY_REL
    entry_path = Path(str(prepared["entry_path"]))
    try:
        with state_lock(registry_path):
            registry, registry_problems, registry_present = _load_registry(root)
            if registry_problems:
                return _registration_failure(registry_problems, "The repository-unit registry is invalid or redirected.")
            current = registry or {"version": REGISTRY_VERSION, "units": []}
            if registry_present:
                integrity_problems = _registry_integrity_problems(root, root, current)
                if integrity_problems:
                    return _registration_failure(
                        integrity_problems,
                        "The existing repository-unit registry has invalid, duplicate, or orphaned entries; repair it before registering another unit.",
                    )
            entries = current.get("units") if isinstance(current.get("units"), list) else []
            path = str(prepared["gitlink_path"])
            matches = _entry_for_path(entries, path)
            if matches and not prepared["force"]:
                existing = matches[0]
                if (
                    existing.get("module") == prepared["module"]
                    and normalize_relative(existing.get("repo_root")) == path
                    and normalize_relative(existing.get("evidence")) == entry_path.relative_to(root).as_posix()
                ):
                    return {
                        **prepared,
                        "code": "repository-unit-registration-unchanged",
                        "message": "Repository-unit registration already matches the requested identity.",
                    }
                return _registration_failure(
                    ["repository-unit-registration-conflict"],
                    "A different registration already exists; use repair or --force after reviewing it.",
                )
            next_entries = [item for item in entries if not (isinstance(item, dict) and normalize_relative(item.get("gitlink_path")) == path)]
            evidence_rel = entry_path.relative_to(root).as_posix()
            next_entries.append(
                {
                    "module": prepared["module"],
                    "display": prepared["module"],
                    "slug": hashlib.sha256(path.encode("utf-8")).hexdigest()[:12],
                    "kind": "repository-unit",
                    "repo_root": path,
                    "gitlink_path": path,
                    "evidence": evidence_rel,
                }
            )
            old_entry = entry_path.read_bytes() if entry_path.is_file() else None
            old_registry = registry_path.read_bytes() if registry_path.is_file() else None
            try:
                entry_path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(entry_path, json.dumps(prepared["evidence"], ensure_ascii=False, indent=2) + "\n")
                atomic_write_text(
                    registry_path,
                    json.dumps({"version": REGISTRY_VERSION, "units": sorted(next_entries, key=lambda item: str(item.get("gitlink_path", "")))}, ensure_ascii=False, indent=2) + "\n",
                )
            except Exception:
                # Restore both state files so a failed multi-file write cannot leave an orphan evidence record.
                try:
                    if old_entry is None:
                        if entry_path.exists() or entry_path.is_symlink():
                            entry_path.unlink()
                    else:
                        entry_path.write_bytes(old_entry)
                    if old_registry is None:
                        if registry_path.exists() or registry_path.is_symlink():
                            registry_path.unlink()
                    else:
                        registry_path.write_bytes(old_registry)
                except OSError:
                    pass
                raise
    except (OSError, RuntimeError, ValueError) as exc:
        return _registration_failure(["repository-unit-registration-write-failed"], f"Unable to write repository-unit registration safely: {exc}")
    return {
        "version": REGISTRY_VERSION,
        "status": "pass",
        "code": "repository-unit-registered",
        "message": "Repository-unit registration and verification evidence were recorded.",
        "gitlink_path": prepared["gitlink_path"],
        "module": prepared["module"],
        "git_head": prepared["git_head"],
        "registry": REGISTRY_REL,
        "evidence": str(entry_path.relative_to(root)).replace("\\", "/"),
    }


def _cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tenetora repository-units", add_help=False)
    parser.add_argument("--help", action="help", help="Show help.")
    parser.add_argument("operation", nargs="?", choices=("inspect", "register", "repair"), default="inspect")
    parser.add_argument("-p", "--path", type=Path, default=Path("."))
    parser.add_argument("--gitlink-path", "--gitlink", dest="gitlink_path")
    parser.add_argument("--module")
    parser.add_argument("--git-head")
    parser.add_argument("--verification-command")
    reference = parser.add_mutually_exclusive_group()
    reference.add_argument("--claim-proof")
    reference.add_argument("--verification-evidence")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _cli_parser().parse_args(argv)
    root = args.path.expanduser().resolve()
    if not (root / ".tenetora").is_dir():
        payload = {
            "version": REGISTRY_VERSION,
            "status": "fail",
            "code": "harness-missing",
            "message": harness_missing_message(root),
        }
    elif args.operation == "inspect":
        payload = inspect_repository_units(root, root)
        payload["code"] = "repository-units-healthy" if payload.get("status") == "pass" else "repository-units-attention"
    else:
        payload = register_repository_unit(args)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"tenetora repository-units: {payload.get('status', 'unknown')}")
        print(payload.get("message", ""))
        if payload.get("codes"):
            print("codes: " + ", ".join(str(item) for item in payload["codes"]))
        if payload.get("status") != "pass" and args.operation == "inspect":
            for unit in payload.get("units", []):
                if isinstance(unit, dict) and unit.get("problems"):
                    print(f"repair: {repository_unit_registration_hint(str(unit.get('gitlink_path')))}")
    return 0 if payload.get("status") == "pass" else 1
