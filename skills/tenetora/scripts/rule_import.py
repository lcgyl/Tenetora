#!/usr/bin/env python3
"""Assess project rule imports before they can enter the shared harness."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from path_security import is_redirected_path, validate_unredirected_file_path
from prompt_guard import PromptInput, scan_input


MAX_RULE_IMPORT_BYTES = 512 * 1024
LOCAL_PATH_RE = re.compile(
    r"(?i)(?:^|[\s`'\"(])(?:"
    r"~[/\\]|"
    r"\$(?:HOME|USERPROFILE)[/\\]|"
    r"%(?:USERPROFILE|HOMEDRIVE|HOMEPATH)%[/\\]|"
    r"/(?:Users|home|private/tmp|tmp)/[^\s`'\")]+|"
    r"[A-Z]:[\\/]Users[\\/][^\s`'\")]+|"
    r"\\\\(?:Users|home)\\[^\s`'\")]+)"
)
SECRET_VALUE_RE = re.compile(
    r"(?i)(?:"
    r"glpat-[A-Za-z0-9._-]+|ghp_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|"
    r"(?:aws_access_key_id|aws_secret_access_key|token|secret|password|private_key)\s*[:=]\s*[^\s'\"`]+"
    r")"
)


@dataclass(frozen=True)
class RuleImportAssessment:
    status: str
    source_hash: str = ""
    source_bytes: int = 0
    ownership: dict[str, str] = field(default_factory=dict)
    findings: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _finding(finding_type: str, severity: str, message: str, *, line: int = 1) -> dict[str, Any]:
    return {
        "type": finding_type,
        "severity": severity,
        "line": line,
        "message": message,
        "recommendation": "保留来源文件，进入人工审查后再决定是否导入。",
        "snippet": "<redacted-rule-content>",
    }


def _line_number(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _content_findings(text: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    for item in scan_input(PromptInput(label="cursorrules-import", text=text)):
        key = (str(item.get("type")), int(item.get("line", 1)))
        if key in seen:
            continue
        seen.add(key)
        item = dict(item)
        item.pop("source", None)
        item.pop("path", None)
        item["snippet"] = "<redacted-rule-content>"
        findings.append(item)

    custom_patterns = (
        (
            "local-path",
            "high",
            "Rule content contains a concrete local-machine path.",
            LOCAL_PATH_RE,
        ),
        (
            "secret-content",
            "high",
            "Rule content contains a credential or secret-looking value.",
            SECRET_VALUE_RE,
        ),
    )
    for finding_type, severity, message, pattern in custom_patterns:
        for match in pattern.finditer(text):
            line = _line_number(text, match.start())
            key = (finding_type, line)
            if key in seen:
                continue
            seen.add(key)
            findings.append(_finding(finding_type, severity, message, line=line))
    return findings


def _safe_relative(path: Path, root: Path) -> str | None:
    candidate = Path(os.path.abspath(os.fspath(path)))
    base = Path(os.path.abspath(os.fspath(root)))
    try:
        relative = candidate.relative_to(base)
    except ValueError:
        return None
    if not relative.parts or ".." in relative.parts:
        return None
    return relative.as_posix()


def _target_conflicts(root: Path, target_rel: str) -> list[dict[str, Any]]:
    target_path = Path(target_rel)
    if target_path.is_absolute() or ".." in target_path.parts:
        return [_finding("target-path-traversal", "high", "Import target must remain inside the project root.")]
    target = root / target_rel
    try:
        validate_unredirected_file_path(target, label="rule import target")
    except RuntimeError as exc:
        return [_finding("unsafe-target", "high", str(exc))]
    if not (target.exists() or is_redirected_path(target)):
        return []
    if is_redirected_path(target):
        return [_finding("unsafe-target", "high", "Import target is a symbolic link or junction.")]
    if not target.is_file():
        return [_finding("unsafe-target", "high", "Import target is not a regular file.")]
    return [
        {
            "type": "target-exists",
            "severity": "medium",
            "resolution": "review-before-merge",
            "message": "Import target already exists and must be reviewed before merging source content.",
        }
    ]


def assess_rule_import(path: Path, root: Path, target_rel: str) -> RuleImportAssessment:
    """Return a fail-closed, portable assessment for one project rule source."""

    findings: list[dict[str, Any]] = []
    relative = _safe_relative(path, root)
    ownership = {
        "status": "project-owned" if relative else "unknown",
        "basis": "project-root-entrypoint" if relative else "outside-project-root",
        "confidence": "inferred" if relative else "none",
    }
    if relative is None:
        findings.append(_finding("source-outside-project", "high", "Rule source is outside the project root."))
        return RuleImportAssessment("blocked", ownership=ownership, findings=findings)
    if ".." in path.parts:
        findings.append(_finding("source-path-traversal", "high", "Rule source contains parent traversal."))
        return RuleImportAssessment("blocked", ownership=ownership, findings=findings)
    try:
        validate_unredirected_file_path(path, label="rule import source")
    except RuntimeError as exc:
        findings.append(_finding("unsafe-source", "high", str(exc)))
        return RuleImportAssessment("blocked", ownership=ownership, findings=findings)
    if is_redirected_path(path):
        findings.append(_finding("unsafe-source", "high", "Rule source is a symbolic link or junction."))
        return RuleImportAssessment("blocked", ownership=ownership, findings=findings)
    try:
        source_bytes = path.stat().st_size
    except OSError as exc:
        findings.append(_finding("unreadable-source", "high", f"Cannot inspect rule source: {exc}"))
        return RuleImportAssessment("blocked", ownership=ownership, findings=findings)
    if source_bytes > MAX_RULE_IMPORT_BYTES:
        findings.append(_finding("source-too-large", "high", "Rule source exceeds the bounded import size."))
        return RuleImportAssessment("blocked", source_bytes=source_bytes, ownership=ownership, findings=findings)
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        findings.append(_finding("invalid-encoding", "high", "Rule source is not valid UTF-8 text."))
        return RuleImportAssessment("blocked", source_bytes=source_bytes, ownership=ownership, findings=findings)
    except OSError as exc:
        findings.append(_finding("unreadable-source", "high", f"Cannot read rule source: {exc}"))
        return RuleImportAssessment("blocked", source_bytes=source_bytes, ownership=ownership, findings=findings)

    findings.extend(_content_findings(text))
    conflicts = _target_conflicts(root, target_rel)
    source_hash = hashlib.sha256(raw).hexdigest()
    status = "blocked" if any(item.get("severity") == "high" for item in findings + conflicts) else "review" if conflicts else "pass"
    return RuleImportAssessment(
        status,
        source_hash=source_hash,
        source_bytes=source_bytes,
        ownership=ownership,
        findings=findings,
        conflicts=conflicts,
    )


def rule_import_is_blocked(assessment: RuleImportAssessment) -> bool:
    return assessment.status == "blocked"


__all__ = ["MAX_RULE_IMPORT_BYTES", "RuleImportAssessment", "assess_rule_import", "rule_import_is_blocked"]
