#!/usr/bin/env python3
"""Classify and migrate legacy Agent Harness project state into .tenetora."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
CLI_DIR = SCRIPTS_DIR.parent / "cli"
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from governance_paths import (  # noqa: E402
    CANONICAL_DIR_NAME,
    LEGACY_DIR_NAME,
    MANIFEST_NAME,
    PRODUCT_ID,
    canonical_dir,
    ensure_manifest,
    legacy_dir,
    manifest_errors,
    package_version,
    read_json_object,
    read_manifest,
    utc_now,
    write_json_atomic,
)
from harness_io import atomic_write_bytes, atomic_write_text  # noqa: E402
from tenetora.path_security import validate_unredirected_path  # noqa: E402


FAMILY_WEIGHTS = {
    "explicit_identity": 35,
    "machine_registration": 25,
    "state_schema": 25,
    "provenance": 20,
    "managed_content": 20,
    "cross_file_integrity": 15,
    "project_entrypoint": 15,
    "structure": 10,
}
STRONG_FAMILIES = {
    "explicit_identity",
    "machine_registration",
    "state_schema",
    "provenance",
    "managed_content",
}
KNOWN_TOP_LEVEL = {
    ".cache",
    ".gitignore",
    "INDEX.md",
    "README.md",
    "agents",
    "automation",
    "changes",
    "docs",
    "guardrails",
    "local",
    "manifest.json",
    "rules",
    "skills",
    "state",
    "templates",
    "tmp",
    "wiki",
    "workflows",
}
MIXED_COPY_TOP_LEVEL = KNOWN_TOP_LEVEL - {".cache", "tmp"}
TEXT_SUFFIXES = {
    "",
    ".json",
    ".md",
    ".mdc",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
ROOT_ADAPTERS = (
    "AGENTS.md",
    "CLAUDE.md",
    "CLAUDE.local.md",
    "GEMINI.md",
    "ZCODE.md",
    ".github/copilot-instructions.md",
)


@dataclass(frozen=True)
class Signal:
    family: str
    code: str
    path: str
    detail: str


@dataclass
class Classification:
    status: str
    confidence: int
    source: str
    destination: str
    evidence_families: list[str] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    foreign_signals: list[Signal] = field(default_factory=list)
    unknown_top_level: list[str] = field(default_factory=list)
    project_id: str | None = None
    source_fingerprint: str = ""
    message: str = ""

    def as_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["signals"] = [asdict(signal) for signal in self.signals]
        payload["foreign_signals"] = [asdict(signal) for signal in self.foreign_signals]
        return payload


@dataclass
class MigrationResult:
    status: str
    classification: Classification
    applied: bool = False
    backup: str | None = None
    changed_adapters: list[str] = field(default_factory=list)
    preserved_legacy: bool = False
    message: str = ""

    def as_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["classification"] = self.classification.as_json()
        return payload


def relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def unsafe_governance_entries(directory: Path) -> list[Path]:
    """Find links or unreadable entries before any governance tree is consumed."""
    if directory.is_symlink():
        return [directory]
    if not directory.is_dir():
        return []
    unsafe: list[Path] = []
    pending = [directory]
    while pending:
        current = pending.pop()
        try:
            children = list(current.iterdir())
        except OSError:
            unsafe.append(current)
            continue
        for child in children:
            if child.is_symlink():
                unsafe.append(child)
            elif child.is_dir():
                pending.append(child)
    return unsafe


def signal(signals: list[Signal], family: str, code: str, path: Path, root: Path, detail: str) -> None:
    signals.append(Signal(family, code, relative(path, root), detail))


def safe_read(path: Path, limit: int = 512_000) -> str:
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return ""
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def iter_files(directory: Path) -> Iterable[Path]:
    if not directory.is_dir():
        return []
    return (path for path in directory.rglob("*") if path.is_file() and not path.is_symlink())


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def source_fingerprint(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(iter_files(directory), key=lambda item: item.relative_to(directory).as_posix()):
        rel = path.relative_to(directory).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(hash_file(path).encode("ascii"))
        except OSError:
            digest.update(b"unreadable")
        digest.update(b"\n")
    return digest.hexdigest()


def identity_value(payload: dict[str, Any]) -> str:
    values: list[str] = []
    for key in ("product", "product_name", "owner", "provider", "tool", "name"):
        value = payload.get(key)
        if isinstance(value, str):
            values.append(value.strip().lower())
    return " ".join(values)


def inspect_explicit_identity(directory: Path, root: Path, signals: list[Signal], foreign: list[Signal]) -> None:
    for name in (MANIFEST_NAME, ".harness-manifest.json", "owner.json", ".owner.json"):
        path = directory / name
        payload = read_json_object(path)
        if payload is None:
            continue
        identity = identity_value(payload)
        if any(token in identity for token in ("tenetora", "agent harness", "agent-harness")):
            signal(signals, "explicit_identity", "recognized-owner", path, root, identity)
        elif identity:
            foreign.append(
                Signal("foreign_ownership", "foreign-owner", relative(path, root), identity)
            )


def installation_home() -> Path:
    configured = os.environ.get("TENETORA_HOME")
    return validate_unredirected_path(
        configured or (Path.home() / ".tenetora"),
        label="TENETORA_HOME" if configured else "Tenetora home",
    )


def inspect_machine_registration(root: Path, signals: list[Signal]) -> str | None:
    registry = installation_home() / "state" / "installations.json"
    payload = read_json_object(registry)
    if payload:
        projects = payload.get("projects")
        if isinstance(projects, list):
            for project in projects:
                if not isinstance(project, dict):
                    continue
                raw_path = project.get("path")
                if isinstance(raw_path, str):
                    try:
                        matches = Path(raw_path).expanduser().resolve() == root.resolve()
                    except OSError:
                        matches = False
                    if matches:
                        signal(
                            signals,
                            "machine_registration",
                            "installation-registry",
                            registry,
                            root,
                            "project is registered in the managed installation inventory",
                        )
                        project_id = project.get("project_id")
                        return project_id if isinstance(project_id, str) else None
    for tool_dir in (".agents", ".codex", ".claude", ".cursor", ".opencode", ".pi", ".zcode"):
        for skill_name in ("tenetora", "agent-harness"):
            path = root / tool_dir / "skills" / skill_name / "SKILL.md"
            text = safe_read(path, 128_000)
            if "Tenetora" in text or "Agent Harness" in text or "agent-harness" in text:
                signal(
                    signals,
                    "machine_registration",
                    "project-lifecycle-skill",
                    path,
                    root,
                    f"recognized {skill_name} lifecycle skill",
                )
                return None
    return None


def inspect_state(directory: Path, root: Path, signals: list[Signal]) -> None:
    current_path = directory / "state" / "current-evidence.json"
    current = read_json_object(current_path)
    inventory_path = directory / "state" / "rules-inventory.json"
    inventory = read_json_object(inventory_path)
    if current and isinstance(current.get("current"), dict):
        current_values = current["current"]
        if isinstance(current_values.get("evidence"), str) and isinstance(current_values.get("report"), str):
            signal(signals, "state_schema", "current-evidence-schema", current_path, root, "recognized current evidence pointer")
    if inventory:
        policy = inventory.get("policy")
        rules = inventory.get("rules")
        if isinstance(policy, dict) and isinstance(rules, list) and (
            "agent_harness_managed_only_when_exact_default" in policy
            or "tenetora_managed_only_when_exact_default" in policy
        ):
            signal(signals, "state_schema", "rules-inventory-schema", inventory_path, root, "recognized ownership inventory")
        for entry in rules if isinstance(rules, list) else []:
            if not isinstance(entry, dict) or not (
                entry.get("managed_by_tenetora") or entry.get("managed_by_agent_harness")
            ):
                continue
            raw_path = entry.get("path")
            expected_hash = entry.get("sha256")
            if not isinstance(raw_path, str) or not isinstance(expected_hash, str):
                continue
            candidate = root / raw_path
            if candidate.is_file():
                try:
                    matches = hash_file(candidate) == expected_hash
                except OSError:
                    matches = False
                if matches:
                    signal(signals, "managed_content", "managed-rule-hash", candidate, root, "managed rule hash matches inventory")
                    break
    for name in ("governance-trail.json", "loop-state.json", "delegation-state.json", "change-impact.json"):
        path = directory / "state" / name
        payload = read_json_object(path)
        if payload and isinstance(payload.get("version"), int):
            signal(signals, "state_schema", "governance-state-schema", path, root, f"recognized {name} state")
            break

    trail_path = directory / "state" / "governance-trail.json"
    trail = read_json_object(trail_path)
    events = trail.get("events") if trail else None
    if isinstance(events, list) and any(
        isinstance(event, dict)
        and isinstance(event.get("source"), str)
        and event["source"].startswith(("tenetora ", "agent-harness "))
        and isinstance(event.get("type"), str)
        and isinstance(event.get("status"), str)
        for event in events
    ):
        signal(
            signals,
            "provenance",
            "governance-trail-source",
            trail_path,
            root,
            "governance trail contains recognized Tenetora lifecycle event provenance",
        )


def inspect_provenance(directory: Path, root: Path, signals: list[Signal]) -> None:
    changes = directory / "changes"
    if not changes.is_dir():
        return
    patterns = ("*harness-bootstrap.md", "*update-report.md", "*repair-report.md", "*extraction-evidence.json")
    for pattern in patterns:
        for path in sorted(changes.glob(pattern))[:3]:
            text = safe_read(path)
            if any(marker in text for marker in ("Harness Bootstrap", "Harness Refresh", "Agent Harness", "Tenetora", '"source_hash"')):
                signal(signals, "provenance", "recognized-change-artifact", path, root, f"recognized {pattern} artifact")
                return


def inspect_cross_file_integrity(directory: Path, root: Path, signals: list[Signal]) -> None:
    pointer = read_json_object(directory / "state" / "current-evidence.json")
    if pointer and isinstance(pointer.get("current"), dict):
        current = pointer["current"]
        resolved = 0
        for key in ("evidence", "report"):
            raw = current.get(key)
            if not isinstance(raw, str):
                continue
            normalized = raw.replace(f"{LEGACY_DIR_NAME}/", "").replace(f"{CANONICAL_DIR_NAME}/", "")
            if (directory / normalized).is_file():
                resolved += 1
        if resolved == 2:
            signal(
                signals,
                "cross_file_integrity",
                "evidence-pointers-resolve",
                directory / "state" / "current-evidence.json",
                root,
                "current evidence and report pointers resolve",
            )

    impact_path = directory / "state" / "change-impact.json"
    trail_path = directory / "state" / "governance-trail.json"
    impact = read_json_object(impact_path)
    trail = read_json_object(trail_path)
    preflight_id = impact.get("preflight_id") if impact else None
    events = trail.get("events") if trail else None
    if (
        isinstance(preflight_id, str)
        and preflight_id.startswith("ah-impact-")
        and isinstance(events, list)
        and any(
            isinstance(event, dict)
            and event.get("preflight_id") == preflight_id
            and event.get("type") == "change-impact-preflight"
            and event.get("status") == "pass"
            for event in events
        )
    ):
        signal(
            signals,
            "cross_file_integrity",
            "change-impact-trail-match",
            impact_path,
            root,
            "change-impact preflight ID resolves to a passing governance-trail event",
        )


def inspect_entrypoints(root: Path, signals: list[Signal]) -> None:
    candidates = [root / name for name in ROOT_ADAPTERS]
    candidates.extend((root / ".claude" / "rules").glob("*.md") if (root / ".claude" / "rules").is_dir() else [])
    candidates.extend((root / ".cursor" / "rules").glob("*.mdc") if (root / ".cursor" / "rules").is_dir() else [])
    for path in candidates:
        text = safe_read(path, 256_000)
        if LEGACY_DIR_NAME in text and any(token in text for token in ("agent-harness", "Tenetora", "Agent Harness")):
            signal(signals, "project_entrypoint", "legacy-project-router", path, root, "entrypoint routes to the legacy governance directory")
            return


def inspect_structure(directory: Path, root: Path, signals: list[Signal]) -> list[str]:
    present = sorted(path.name for path in directory.iterdir()) if directory.is_dir() else []
    known_dirs = [name for name in ("agents", "rules", "workflows", "wiki", "guardrails", "changes", "state") if (directory / name).is_dir()]
    if len(known_dirs) >= 4:
        signal(signals, "structure", "recognized-layout", directory, root, f"recognized layout directories: {', '.join(known_dirs)}")
    return sorted(name for name in present if name not in KNOWN_TOP_LEVEL)


def confidence(signals: list[Signal]) -> tuple[int, list[str]]:
    families = sorted({entry.family for entry in signals if entry.family in FAMILY_WEIGHTS})
    score = min(100, sum(FAMILY_WEIGHTS[family] for family in families))
    return score, families


def classify_legacy(root: Path, legacy: Path, canonical: Path) -> Classification:
    unsafe = unsafe_governance_entries(legacy)
    if unsafe:
        return Classification(
            status="unsafe",
            confidence=0,
            source=str(legacy),
            destination=str(canonical),
            message=f"legacy governance directory contains an unsafe symbolic link or unreadable entry: {unsafe[0]}",
        )
    if not legacy.is_dir():
        return Classification(
            status="foreign",
            confidence=0,
            source=str(legacy),
            destination=str(canonical),
            message=".harness exists but is not a directory",
        )

    signals: list[Signal] = []
    foreign: list[Signal] = []
    inspect_explicit_identity(legacy, root, signals, foreign)
    project_id = inspect_machine_registration(root, signals)
    inspect_state(legacy, root, signals)
    inspect_provenance(legacy, root, signals)
    inspect_cross_file_integrity(legacy, root, signals)
    inspect_entrypoints(root, signals)
    unknown = inspect_structure(legacy, root, signals)
    score, families = confidence(signals)
    strong = bool(set(families) & STRONG_FAMILIES)
    owned = score >= 55 and len(families) >= 3 and strong
    if owned and (foreign or unknown):
        status = "legacy-mixed"
        message = "Tenetora legacy ownership is strong, but foreign or unknown content is also present"
    elif owned:
        status = "legacy-owned"
        message = "legacy Tenetora/Agent Harness ownership is confirmed by independent fingerprints"
    elif foreign and not (set(families) & STRONG_FAMILIES):
        status = "foreign"
        message = "explicit foreign ownership detected"
    elif signals:
        status = "legacy-ambiguous"
        message = "partial legacy Tenetora evidence is insufficient for automatic migration"
    else:
        status = "foreign"
        message = "no meaningful Tenetora ownership evidence found"
    return Classification(
        status=status,
        confidence=score,
        source=str(legacy),
        destination=str(canonical),
        evidence_families=families,
        signals=signals,
        foreign_signals=foreign,
        unknown_top_level=unknown,
        project_id=project_id,
        source_fingerprint=source_fingerprint(legacy),
        message=message,
    )


def classify_project(root: Path) -> Classification:
    raw_root = root.expanduser()
    if raw_root.is_symlink():
        return Classification(
            status="unsafe",
            confidence=0,
            source=str(raw_root),
            destination=str(raw_root / CANONICAL_DIR_NAME),
            message=f"project path is a symbolic link: {raw_root}",
        )
    try:
        raw_root = validate_unredirected_path(raw_root, label="project path")
    except RuntimeError as exc:
        return Classification(
            status="unsafe",
            confidence=0,
            source=str(raw_root),
            destination=str(raw_root / CANONICAL_DIR_NAME),
            message=str(exc),
        )
    root = raw_root
    canonical = canonical_dir(root)
    legacy = legacy_dir(root)
    unsafe_canonical = unsafe_governance_entries(canonical)
    if unsafe_canonical:
        return Classification(
            status="canonical-invalid",
            confidence=0,
            source=str(canonical),
            destination=str(canonical),
            message=f"canonical governance directory contains an unsafe symbolic link or unreadable entry: {unsafe_canonical[0]}",
        )
    if canonical.exists():
        errors = manifest_errors(canonical)
        if errors:
            return Classification(
                status="canonical-invalid",
                confidence=0,
                source=str(canonical),
                destination=str(canonical),
                message="; ".join(errors),
            )
        if not legacy.exists():
            return Classification(
                status="canonical",
                confidence=100,
                source=str(canonical),
                destination=str(canonical),
                message="canonical Tenetora project record is ready",
            )
        legacy_classification = classify_legacy(root, legacy, canonical)
        manifest = read_manifest(canonical) or {}
        migration = manifest.get("migration")
        migration = migration if isinstance(migration, dict) else {}
        expected_fingerprint = migration.get("source_fingerprint")
        preserved_status = migration.get("classification") in {"legacy-mixed", "legacy-ambiguous"}
        fingerprint_matches = (
            isinstance(expected_fingerprint, str)
            and bool(expected_fingerprint)
            and expected_fingerprint == legacy_classification.source_fingerprint
        )
        if legacy_classification.status == "foreign":
            status = "canonical-with-foreign"
            message = "canonical .tenetora coexists with an unrelated .harness directory"
        elif preserved_status and fingerprint_matches:
            status = "canonical-with-preserved-legacy"
            message = "canonical .tenetora coexists with the reviewed legacy source preserved by migration policy"
        else:
            status = "coexistence"
            message = "canonical .tenetora coexists with a Tenetora-owned or changed legacy .harness; automatic merge is disabled"
        return Classification(
            status=status,
            confidence=100,
            source=str(legacy),
            destination=str(canonical),
            evidence_families=legacy_classification.evidence_families,
            signals=legacy_classification.signals,
            foreign_signals=legacy_classification.foreign_signals,
            unknown_top_level=legacy_classification.unknown_top_level,
            project_id=manifest.get("project_id") if isinstance(manifest.get("project_id"), str) else None,
            source_fingerprint=legacy_classification.source_fingerprint,
            message=message,
        )
    if not legacy.exists():
        return Classification(
            status="absent",
            confidence=0,
            source=str(legacy),
            destination=str(canonical),
            message="no project governance directory exists",
        )
    return classify_legacy(root, legacy, canonical)


def migration_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def backup_path(root: Path, timestamp: str) -> Path:
    project_key = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:20]
    return installation_home() / "backups" / "governance-migrations" / project_key / timestamp / LEGACY_DIR_NAME


def journal_path() -> Path:
    return installation_home() / "state" / "governance-migrations.json"


def append_journal(entry: dict[str, Any]) -> None:
    path = journal_path()
    payload = read_json_object(path) or {"version": 1, "migrations": []}
    migrations = payload.get("migrations")
    if not isinstance(migrations, list):
        migrations = []
    migrations.append(entry)
    payload["migrations"] = migrations[-100:]
    payload["updated_at"] = utc_now()
    write_json_atomic(path, payload)


def replace_self_references(text: str) -> str:
    replacements = (
        ('Path.home() / ".agent-harness"', 'Path.home() / ".tenetora"'),
        ("Path.home() / '.agent-harness'", "Path.home() / '.tenetora'"),
        ("${HOME}/.agent-harness", "${HOME}/.tenetora"),
        ("$HOME/.agent-harness", "$HOME/.tenetora"),
        ("%USERPROFILE%\\.agent-harness", "%USERPROFILE%\\.tenetora"),
        ("~/.agent-harness", "~/.tenetora"),
        (f"{LEGACY_DIR_NAME}/", f"{CANONICAL_DIR_NAME}/"),
        (f"/{LEGACY_DIR_NAME}", f"/{CANONICAL_DIR_NAME}"),
        ("$ROOT/.harness", "$ROOT/.tenetora"),
        (".tenetora/skills/agent-harness", ".tenetora/skills/tenetora"),
        ("agent-harness-decision-interview", "tenetora-decision-interview"),
        ("agent-harness-prompt-guard", "tenetora-prompt-guard"),
        ("agent-harness-update", "tenetora-update"),
        ("agent-harness-audit", "tenetora-audit"),
        ("agent-harness-init", "tenetora-init"),
        ("agent-harness-align", "tenetora-align"),
        ("agent-harness-loop", "tenetora-loop"),
        ("use agent-harness-", "use tenetora-"),
        ("use agent-harness", "use tenetora"),
        (".agents/skills/agent-harness", ".agents/skills/tenetora"),
        (".claude/skills/agent-harness", ".claude/skills/tenetora"),
        (".codex/skills/agent-harness", ".codex/skills/tenetora"),
        (".cursor/skills/agent-harness", ".cursor/skills/tenetora"),
        (".opencode/skills/agent-harness", ".opencode/skills/tenetora"),
        (".pi/skills/agent-harness", ".pi/skills/tenetora"),
        (".zcode/skills/agent-harness", ".zcode/skills/tenetora"),
        ("--skill agent-harness", "--skill tenetora"),
        ("skill: agent-harness", "skill: tenetora"),
        ("# agent-harness", "# tenetora"),
        ("[agent-harness]", "[tenetora]"),
        ("agent-harness-verify", "tenetora-verify"),
        ("tenetora/skills/agent-harness/", "tenetora/skills/tenetora/"),
        ("Source: agent-harness/", "Source: tenetora/"),
        ("`agent-harness`", "`tenetora`"),
        ("managed_by_agent_harness", "managed_by_tenetora"),
        ("agent_harness_managed_only_when_exact_default", "tenetora_managed_only_when_exact_default"),
        ("project-modified-agent-harness-default", "project-modified-tenetora-default"),
        ("project-moved-or-renamed-agent-harness-default", "project-moved-or-renamed-tenetora-default"),
        ("agent-harness-default", "tenetora-default"),
        ('"owner": "agent-harness"', '"owner": "tenetora"'),
        ('for command_name in ("tenetora", "agent-harness"):', 'for command_name in ("tenetora",):'),
        ('for skill_name in ("tenetora", "agent-harness"):', 'for skill_name in ("tenetora",):'),
        ("Agent Harness", "Tenetora"),
        (LEGACY_DIR_NAME, CANONICAL_DIR_NAME),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    text = re.sub(r"(?<![.A-Za-z0-9_-])agent-harness/", "tenetora/", text)
    command_names = (
        "alignment",
        "audit-configs",
        "audit",
        "benchmark",
        "capture-rule",
        "delegation",
        "doctor",
        "guard",
        "hooks",
        "impact",
        "install",
        "installations",
        "init",
        "loop-state",
        "migrate",
        "preflight",
        "prompt-guard",
        "recap",
        "refresh",
        "repair",
        "review",
        "route",
        "rules",
        "run-all",
        "setup-ci",
        "setup",
        "skill-instructions",
        "status",
        "update",
        "upgrade",
        "validate",
        "version",
    )
    for command in command_names:
        text = text.replace(f"agent-harness {command}", f"tenetora {command}")
    return text


PRESERVED_PROVENANCE_STATE = {
    "state/alignment-session.json",
    "state/alignment-sessions/",
    "state/alignment-history/",
    "state/alignment-lifecycle.json",
    "state/benchmark-history.json",
    "state/change-impact.json",
    "state/current-alignment.json",
    "state/delegation-state.json",
    "state/governance-trail.json",
    "state/loop-state.json",
    "state/score-history.json",
}

HANDOFF_PATH_FIELD_RE = re.compile(
    r'(?P<prefix>"handoff_relative_path"\s*:\s*)(?P<value>"(?:\\.|[^"\\])*")'
)


def rewrite_alignment_handoff_locator(path: Path) -> bool:
    """Move the non-semantic handoff pointer without rewriting hash-bound state."""

    payload = read_json_object(path)
    if not payload:
        return False
    raw = payload.get("handoff_relative_path")
    legacy_prefix = f"{LEGACY_DIR_NAME}/docs/plans/"
    if not isinstance(raw, str) or not raw.startswith(legacy_prefix) or not raw.endswith(".md"):
        return False
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts:
        return False
    updated = f"{CANONICAL_DIR_NAME}/docs/plans/{raw[len(legacy_prefix):]}"
    text = path.read_text(encoding="utf-8")
    matches = list(HANDOFF_PATH_FIELD_RE.finditer(text))
    if len(matches) != 1:
        return False
    match = matches[0]
    try:
        encoded_value = json.loads(match.group("value"))
    except json.JSONDecodeError:
        return False
    if encoded_value != raw:
        return False
    replacement = match.group("prefix") + json.dumps(updated, ensure_ascii=False)
    atomic_write_text(path, text[:match.start()] + replacement + text[match.end():])
    return True


def preserves_historical_semantics(relative_path: Path) -> bool:
    relative = relative_path.as_posix()
    if relative == MANIFEST_NAME:
        return True
    for preserved in PRESERVED_PROVENANCE_STATE:
        normalized = preserved.rstrip("/")
        if relative == normalized or relative.startswith(normalized + "/"):
            return True
    if not relative_path.parts or relative_path.parts[0] != "changes":
        return False
    if len(relative_path.parts) > 1 and relative_path.parts[1] in {"archive", "backups"}:
        return True
    if relative_path.name in {"INDEX.md", "README.md"}:
        return False
    if "update-candidates" in relative_path.parts:
        return False
    return relative_path.suffix.lower() == ".md" or "migration-plan" in relative_path.name


def rewrite_tree(directory: Path) -> list[str]:
    unsafe = unsafe_governance_entries(directory)
    if unsafe:
        raise RuntimeError(f"governance tree contains an unsafe symbolic link or unreadable entry: {unsafe[0]}")
    changed: list[str] = []
    for path in iter_files(directory):
        relative_path = path.relative_to(directory)
        if relative_path.as_posix() == "state/current-alignment.json":
            if rewrite_alignment_handoff_locator(path):
                changed.append(relative_path.as_posix())
            continue
        if path == directory / MANIFEST_NAME or preserves_historical_semantics(relative_path):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        text = safe_read(path, 64_000_000)
        if not text:
            continue
        updated = replace_self_references(text)
        if updated != text:
            atomic_write_text(path, updated)
            changed.append(relative_path.as_posix())
    skills_dir = directory / "skills"
    if skills_dir.is_dir():
        for path in sorted(skills_dir.glob("agent-harness*.md")):
            target = path.with_name(path.name.replace("agent-harness", "tenetora", 1))
            if not target.exists():
                path.rename(target)
                changed.append(target.relative_to(directory).as_posix())
    return changed


def adapter_candidates(root: Path) -> list[Path]:
    paths = [root / name for name in ROOT_ADAPTERS]
    for directory, pattern in ((root / ".claude" / "rules", "*.md"), (root / ".cursor" / "rules", "*.mdc")):
        if directory.is_dir():
            paths.extend(sorted(directory.glob(pattern)))
    return [path for path in paths if path.is_file() and not path.is_symlink()]


def rewrite_adapters(
    root: Path,
    backup_root: Path,
    *,
    backups: list[tuple[Path, Path]] | None = None,
) -> tuple[list[str], list[tuple[Path, Path]]]:
    changed: list[str] = []
    backup_log = backups if backups is not None else []
    adapter_backup = backup_root.parent / "adapters"
    for path in adapter_candidates(root):
        text = safe_read(path, 512_000)
        updated = replace_self_references(text)
        if updated == text:
            continue
        rel = path.relative_to(root)
        backup = adapter_backup / rel
        atomic_write_bytes(backup, path.read_bytes(), mode=0o600)
        backup_log.append((path, backup))
        atomic_write_text(path, updated)
        changed.append(rel.as_posix())
    return changed, backup_log


def rewrite_ignore_policy(
    root: Path,
    backup_root: Path,
    *,
    preserve_legacy: bool,
    source_tracked: bool,
    backups: list[tuple[Path, Path]] | None = None,
) -> tuple[list[str], list[tuple[Path, Path]]]:
    changed: list[str] = []
    backup_log = backups if backups is not None else []
    adapter_backup = backup_root.parent / "adapters"
    for path in (root / ".gitignore", root / ".git" / "info" / "exclude"):
        text = safe_read(path, 512_000)
        if not text:
            continue
        lines = text.splitlines(keepends=True)
        canonical_present = any(line.strip() in {CANONICAL_DIR_NAME, f"{CANONICAL_DIR_NAME}/", f"/{CANONICAL_DIR_NAME}/"} for line in lines)
        updated_lines: list[str] = []
        touched = False
        for line in lines:
            stripped = line.strip()
            if stripped not in {LEGACY_DIR_NAME, f"{LEGACY_DIR_NAME}/", f"/{LEGACY_DIR_NAME}/"}:
                updated_lines.append(line)
                continue
            touched = True
            if not source_tracked or preserve_legacy:
                updated_lines.append(line)
            if preserve_legacy:
                if not source_tracked and not canonical_present:
                    newline = "\r\n" if line.endswith("\r\n") else "\n"
                    if not line.endswith(("\n", "\r")):
                        updated_lines[-1] = line + newline
                    prefix = "/" if stripped.startswith("/") else ""
                    updated_lines.append(f"{prefix}{CANONICAL_DIR_NAME}/{newline}")
                    canonical_present = True
            elif not source_tracked:
                suffix = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                prefix = "/" if stripped.startswith("/") else ""
                updated_lines[-1] = f"{prefix}{CANONICAL_DIR_NAME}/{suffix}"
        if not touched:
            continue
        updated = "".join(updated_lines)
        if updated == text:
            continue
        rel = path.relative_to(root)
        backup = adapter_backup / rel
        atomic_write_bytes(backup, path.read_bytes(), mode=0o600)
        backup_log.append((path, backup))
        atomic_write_text(path, updated)
        changed.append(rel.as_posix())
    return changed, backup_log


def source_is_tracked(root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--", LEGACY_DIR_NAME],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def restore_adapters(backups: list[tuple[Path, Path]]) -> None:
    for target, backup in reversed(backups):
        if backup.is_file():
            target = validate_unredirected_path(target, label="migration adapter target")
            backup = validate_unredirected_path(backup, label="migration adapter backup")
            atomic_write_text(target, backup.read_text(encoding="utf-8"))


def copy_source(source: Path, staging: Path, classification: Classification, allow_mixed: bool) -> None:
    unsafe = unsafe_governance_entries(source)
    if unsafe:
        raise RuntimeError(f"migration source contains an unsafe symbolic link or unreadable entry: {unsafe[0]}")
    if classification.status == "legacy-owned":
        shutil.copytree(source, staging, symlinks=True)
        return
    if classification.status not in {"legacy-mixed", "legacy-ambiguous"} or not allow_mixed:
        raise RuntimeError(f"migration requires explicit review for {classification.status}")
    staging.mkdir(parents=True)
    for child in source.iterdir():
        if child.name not in MIXED_COPY_TOP_LEVEL or child.name == MANIFEST_NAME:
            continue
        target = staging / child.name
        if child.is_symlink():
            target.symlink_to(os.readlink(child), target_is_directory=child.is_dir())
        elif child.is_dir():
            shutil.copytree(child, target, symlinks=True)
        else:
            shutil.copy2(child, target)


def validate_staging(staging: Path) -> list[str]:
    unsafe_entries = unsafe_governance_entries(staging)
    errors = [
        f"staging contains an unsafe symbolic link or unreadable entry: {entry}"
        for entry in unsafe_entries
    ]
    errors.extend(manifest_errors(staging))
    pointer_path = staging / "state" / "current-evidence.json"
    pointer = read_json_object(pointer_path)
    if pointer and isinstance(pointer.get("current"), dict):
        for key in ("evidence", "report"):
            raw = pointer["current"].get(key)
            if isinstance(raw, str) and LEGACY_DIR_NAME in raw:
                errors.append(f"current-evidence {key} still references {LEGACY_DIR_NAME}")
    if not any((staging / name).exists() for name in ("README.md", "INDEX.md", "state", "changes")):
        errors.append("staging directory does not contain a recognizable Tenetora record")
    return errors


def apply_migration(root: Path, classification: Classification, *, allow_mixed: bool = False) -> MigrationResult:
    try:
        root = validate_unredirected_path(root.expanduser(), label="project path")
    except RuntimeError as exc:
        return MigrationResult("blocked", classification, message=str(exc))
    if classification.status in {"canonical", "canonical-with-foreign", "canonical-with-preserved-legacy"}:
        changed = rewrite_tree(canonical_dir(root))
        if changed:
            return MigrationResult(
                "repaired",
                classification,
                applied=True,
                changed_adapters=changed,
                message="canonical .tenetora operational references were normalized",
            )
        return MigrationResult("current", classification, message=".tenetora is already canonical")
    if classification.status in {"unsafe", "coexistence", "canonical-invalid", "foreign", "absent"}:
        return MigrationResult("blocked", classification, message=classification.message)
    if classification.status in {"legacy-mixed", "legacy-ambiguous"} and not allow_mixed:
        return MigrationResult(
            "review-required",
            classification,
            message="review the migration plan and rerun with --allow-mixed only after confirming ownership",
        )

    source = Path(classification.source)
    destination = Path(classification.destination)
    timestamp = migration_timestamp()
    backup = backup_path(root, timestamp)
    staging = Path(tempfile.mkdtemp(prefix=f".{CANONICAL_DIR_NAME}.migrating-", dir=root))
    staging.rmdir()
    lock = root / f"{CANONICAL_DIR_NAME}.migration.lock"
    adapters: list[tuple[Path, Path]] = []
    changed_adapters: list[str] = []
    published = False
    lock_acquired = False
    tracked_source = source_is_tracked(root)
    try:
        try:
            descriptor = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise RuntimeError(f"migration lock already exists: {lock}") from exc
        lock_acquired = True
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"pid": os.getpid(), "started_at": utc_now()}) + "\n")
        if staging.exists() or destination.exists():
            raise RuntimeError("migration staging or destination already exists")
        validate_unredirected_path(backup.parent, label="governance migration backup parent")
        validate_unredirected_path(backup, label="governance migration backup")
        shutil.copytree(source, backup, symlinks=True)
        copy_source(source, staging, classification, allow_mixed)
        rewrite_tree(staging)
        ensure_manifest(
            staging,
            version=package_version(SCRIPTS_DIR),
            project_id=classification.project_id,
            migrated_from=LEGACY_DIR_NAME,
            classification=classification.status,
            evidence_families=classification.evidence_families,
            source_fingerprint=classification.source_fingerprint,
        )
        errors = validate_staging(staging)
        if errors:
            raise RuntimeError("; ".join(errors))
        os.replace(staging, destination)
        published = True
        preserved = classification.status != "legacy-owned"
        changed_adapters, _ = rewrite_adapters(root, backup, backups=adapters)
        ignore_changes, _ = rewrite_ignore_policy(
            root,
            backup,
            preserve_legacy=preserved,
            source_tracked=tracked_source,
            backups=adapters,
        )
        changed_adapters.extend(ignore_changes)
        if not preserved:
            shutil.rmtree(source)
        entry = {
            "project_id": read_manifest(destination).get("project_id") if read_manifest(destination) else "",
            "status": "completed",
            "classification": classification.status,
            "confidence": classification.confidence,
            "source_layout": LEGACY_DIR_NAME,
            "destination_layout": CANONICAL_DIR_NAME,
            "source_fingerprint": classification.source_fingerprint,
            "backup": str(backup),
            "completed_at": utc_now(),
        }
        journal_warning = ""
        try:
            append_journal(entry)
        except Exception as exc:
            journal_warning = f"; migration journal could not be updated: {exc}"
        return MigrationResult(
            "migrated",
            classification,
            applied=True,
            backup=str(backup),
            changed_adapters=changed_adapters,
            preserved_legacy=preserved,
            message="legacy project governance migrated to .tenetora" + journal_warning,
        )
    except Exception as exc:
        restore_adapters(adapters)
        if published and destination.exists() and source.exists():
            shutil.rmtree(destination)
        if staging.exists():
            shutil.rmtree(staging)
        try:
            append_journal(
                {
                    "status": "failed",
                    "classification": classification.status,
                    "source_layout": LEGACY_DIR_NAME,
                    "destination_layout": CANONICAL_DIR_NAME,
                    "source_fingerprint": classification.source_fingerprint,
                    "backup": str(backup) if backup.exists() else "",
                    "error": str(exc),
                    "failed_at": utc_now(),
                }
            )
        except Exception:
            pass
        return MigrationResult(
            "failed",
            classification,
            backup=str(backup) if backup.exists() else None,
            message=str(exc),
        )
    finally:
        if lock_acquired and lock.exists():
            lock.unlink()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="tenetora migrate",
        description="Classify and migrate legacy .harness project governance into .tenetora.",
        add_help=False,
    )
    result.add_argument("--help", action="help", help="Show this help message and exit.")
    result.add_argument("-p", "--path", type=Path, default=Path.cwd(), help="Project directory. Defaults to the current directory.")
    action = result.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="Classify without writing. This is the default.")
    action.add_argument("--plan", action="store_true", help="Print a detailed migration plan without writing.")
    action.add_argument("--apply", action="store_true", help="Apply a high-confidence migration.")
    result.add_argument("--allow-mixed", action="store_true", help="Apply a reviewed mixed or ambiguous migration while preserving the legacy source.")
    result.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return result


def migration_plan(classification: Classification) -> dict[str, Any]:
    if classification.status in {"canonical", "canonical-with-foreign", "canonical-with-preserved-legacy"}:
        action = "none"
        eligible = True
        source_disposition = "preserve"
    elif classification.status == "legacy-owned":
        action = "migrate-owned"
        eligible = True
        source_disposition = "remove-after-validated-publish"
    elif classification.status in {"legacy-mixed", "legacy-ambiguous"}:
        action = "review-required"
        eligible = False
        source_disposition = "preserve"
    else:
        action = "blocked"
        eligible = False
        source_disposition = "preserve"
    return {
        "eligible_without_override": eligible,
        "action": action,
        "source_disposition": source_disposition,
        "backup_policy": "machine-local external backup before staging",
        "publish_policy": "copy, rewrite canonical self-references, write manifest, validate, atomic publish",
        "adapter_policy": "backup and rewrite only matching Tenetora entrypoints and exact ignore rules",
        "mixed_policy": "copy recognized non-ephemeral top-level surfaces and preserve .harness",
        "rollback_policy": "restore adapters and remove unpublished or recoverable destination on failure",
    }


def render_text(
    classification: Classification,
    result: MigrationResult | None = None,
    plan: dict[str, Any] | None = None,
) -> str:
    chinese = os.environ.get("TENETORA_LANG", "").strip().lower() in {"zh", "zh-cn", "zh-hans", "chinese", "中文"}
    message = result.message if result else classification.message
    if chinese and message == "canonical Tenetora project record is ready":
        message = "Tenetora 项目规范记录已就绪"
    lines = [
        "Tenetora 治理迁移" if chinese else "Tenetora Governance Migration",
        f"{'状态' if chinese else 'Status'}: {(result.status if result else classification.status)}",
        f"{'分类' if chinese else 'Classification'}: {classification.status}",
        f"{'置信度' if chinese else 'Confidence'}: {classification.confidence}/100",
        f"{'来源' if chinese else 'Source'}: {classification.source}",
        f"{'目标' if chinese else 'Destination'}: {classification.destination}",
    ]
    if classification.evidence_families:
        lines.append(("证据族: " if chinese else "Evidence families: ") + ", ".join(classification.evidence_families))
    if classification.unknown_top_level:
        lines.append(("未知顶层条目: " if chinese else "Unknown top-level entries: ") + ", ".join(classification.unknown_top_level))
    if classification.foreign_signals:
        lines.append("外部归属信号：" if chinese else "Foreign ownership signals:")
        lines.extend(f"- {entry.path}: {entry.detail}" for entry in classification.foreign_signals)
    lines.append(f"{'消息' if chinese else 'Message'}: {message}")
    if result and result.backup:
        lines.append(f"{'备份' if chinese else 'Backup'}: {result.backup}")
    if result and result.changed_adapters:
        lines.append(("已更新适配器: " if chinese else "Updated adapters: ") + ", ".join(result.changed_adapters))
    if result and result.preserved_legacy:
        lines.append("由于来源混合或存在歧义，旧 .harness 已保留。" if chinese else "Legacy .harness was preserved because the source was mixed or ambiguous.")
    if plan:
        lines.append("计划：" if chinese else "Plan:")
        lines.extend(
            [
                f"- {'动作' if chinese else 'action'}: {plan['action']}",
                f"- {'来源' if chinese else 'source'}: {plan['source_disposition']}",
                f"- {'备份' if chinese else 'backup'}: {plan['backup_policy']}",
                f"- {'发布' if chinese else 'publish'}: {plan['publish_policy']}",
                f"- {'适配器' if chinese else 'adapters'}: {plan['adapter_policy']}",
                f"- {'回滚' if chinese else 'rollback'}: {plan['rollback_policy']}",
            ]
        )
    return "\n".join(lines)


def main() -> int:
    args = parser().parse_args()
    root = args.path.expanduser()
    classification = classify_project(root)
    result = apply_migration(root, classification, allow_mixed=args.allow_mixed) if args.apply else None
    plan = migration_plan(classification) if args.plan else None
    payload = result.as_json() if result else classification.as_json()
    if plan:
        payload = {"classification": payload, "plan": plan}
    print(
        json.dumps(payload, indent=2, ensure_ascii=False)
        if args.json
        else render_text(classification, result, plan)
    )
    if result is None:
        return 0
    return 0 if result.status in {"current", "migrated", "repaired"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
