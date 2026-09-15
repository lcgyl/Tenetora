"""Bounded recovery for a broken legacy Codex plugin registration."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any

from .brand import default_legacy_machine_home, machine_home
from .file_lock import locked_file
from .path_security import (
    ensure_unredirected_directory,
    is_redirected_path,
    validate_unredirected_entry_path,
    validate_unredirected_file_path,
    validate_unredirected_path,
)


LEGACY_MARKETPLACE = "agent-harness-local"
LEGACY_PLUGIN_ID = "agent-harness@agent-harness-local"
LEGACY_HOOK_PREFIX = LEGACY_PLUGIN_ID + ":hooks/hooks.json:"
CANONICAL_MARKETPLACE = "tenetora-local"
CANONICAL_PLUGIN_ID = "tenetora@tenetora-local"
CANONICAL_HOOK_PREFIX = CANONICAL_PLUGIN_ID + ":hooks/hooks.json:"
TRUSTED_HOOK_NAMES = {
    "pre_tool_use",
    "session_start",
    "stop",
    "subagent_start",
    "subagent_stop",
    "user_prompt_submit",
}
HOOK_INSTANCE_INDEX_RE = re.compile(r"^[0-9]+$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
SOURCE_RE = re.compile(r"(?m)^[ \t]*source[ \t]*=[ \t]*(?P<value>\"(?:\\.|[^\"\\])*\"|'[^']*')[ \t]*(?:#.*)?$")
SOURCE_TYPE_RE = re.compile(
    r"(?m)^[ \t]*source_type[ \t]*=[ \t]*(?P<value>\"(?:\\.|[^\"\\])*\"|'[^']*')[ \t]*(?:#.*)?$"
)
CODEX_OFFICIAL_MARKETPLACES = {
    "openai-curated": "marketplace.json",
    "openai-api-curated": "api_marketplace.json",
}
CODEX_MALFORMED_LOCAL_SOURCE_PREFIX = r"\\?\/"


@dataclass(frozen=True)
class TomlSection:
    key: tuple[str, ...]
    start: int
    end: int
    text: str
    array: bool = False


@dataclass
class RecoveryAssessment:
    status: str
    config_path: str
    reason: str | None = None
    marketplace_source: str | None = None
    sections: list[str] = field(default_factory=list)
    section_keys: list[list[str]] = field(default_factory=list)
    ownership_evidence: list[str] = field(default_factory=list)
    legacy_caches: list[str] = field(default_factory=list)
    backup_root: str | None = None
    config_sha256: str | None = None

    @property
    def recoverable(self) -> bool:
        return self.status == "recoverable"

    def payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RecoveryTransaction:
    assessment: RecoveryAssessment
    original: bytes
    backup_path: Path
    active: bool = True
    committed: bool = False
    archived_caches: list[str] = field(default_factory=list)
    cache_moves: list[tuple[str, str]] = field(default_factory=list)
    rollback_conflicts: list[str] = field(default_factory=list)
    lock_context: Any = field(default=None, repr=False)

    def _release_lock(self) -> None:
        if self.lock_context is None:
            return
        context = self.lock_context
        self.lock_context = None
        context.__exit__(None, None, None)

    def rollback(self) -> None:
        if not self.active:
            return
        try:
            config = Path(self.assessment.config_path)
            try:
                current = config.read_bytes()
                original_unmanaged = _unmanaged_config_signature(self.original)
                current_unmanaged = _unmanaged_config_signature(current)
                if current_unmanaged != original_unmanaged:
                    self.rollback_conflicts.append(
                        "Codex config.toml changed outside Tenetora-managed sections during recovery"
                    )
                else:
                    _atomic_write(config, self.original)
            except (OSError, UnicodeError, ValueError) as exc:
                self.rollback_conflicts.append(f"Codex config.toml could not be safely restored: {exc}")
            for original, archived in reversed(self.cache_moves):
                try:
                    source = validate_unredirected_entry_path(
                        Path(archived), label="Codex archived cache"
                    )
                    destination = validate_unredirected_entry_path(
                        Path(original), label="Codex cache restore target"
                    )
                except RuntimeError as exc:
                    self.rollback_conflicts.append(str(exc))
                    continue
                if (
                    not (source.exists() or is_redirected_path(source))
                    or destination.exists()
                    or is_redirected_path(destination)
                ):
                    continue
                ensure_unredirected_directory(
                    destination.parent, label="Codex cache restore directory"
                )
                source.replace(destination)
            self.active = False
        finally:
            self._release_lock()
        if self.rollback_conflicts:
            raise RuntimeError(
                "Codex recovery rollback stopped to preserve concurrent configuration changes: "
                + "; ".join(self.rollback_conflicts)
            )

    def commit(self) -> None:
        if not self.active:
            return
        try:
            self.committed = True
            self.active = False
        finally:
            self._release_lock()

    def payload(self) -> dict[str, Any]:
        return {
            "status": "committed" if self.committed else "active" if self.active else "rolled-back",
            "backup": str(self.backup_path),
            "archived_caches": list(self.archived_caches),
            "rollback_conflicts": list(self.rollback_conflicts),
            "assessment": self.assessment.payload(),
        }


def codex_home() -> Path:
    raw = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    return validate_unredirected_path(raw, label="CODEX_HOME")


def config_path() -> Path:
    return validate_unredirected_file_path(codex_home() / "config.toml", label="Codex config.toml")


def _config_display_path(path: Path | None = None) -> Path:
    raw = path if path is not None else Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    return Path(os.path.abspath(os.fspath(Path(raw).expanduser())))


def _safe_config_path(path: Path | None = None) -> Path:
    return validate_unredirected_file_path(
        path if path is not None else codex_home() / "config.toml",
        label="Codex config.toml",
    )


def _parse_key(raw: str) -> tuple[str, ...] | None:
    parts: list[str] = []
    index = 0
    length = len(raw)
    while index < length:
        while index < length and raw[index].isspace():
            index += 1
        if index >= length:
            break
        if raw[index] in {'"', "'"}:
            quote = raw[index]
            index += 1
            value: list[str] = []
            while index < length:
                char = raw[index]
                if quote == '"' and char == "\\" and index + 1 < length:
                    value.append(raw[index + 1])
                    index += 2
                    continue
                if char == quote:
                    index += 1
                    break
                value.append(char)
                index += 1
            else:
                return None
            part = "".join(value)
        else:
            start = index
            while index < length and raw[index] not in ". \t":
                index += 1
            part = raw[start:index]
        if not part:
            return None
        parts.append(part)
        while index < length and raw[index].isspace():
            index += 1
        if index >= length:
            break
        if raw[index] != ".":
            return None
        index += 1
    return tuple(parts) if parts else None


def _table_header(line: str) -> tuple[str, bool] | None:
    stripped = line.lstrip(" \t")
    if not stripped.startswith("["):
        return None
    array = stripped.startswith("[[")
    opening = 2 if array else 1
    closing = "]]" if array else "]"
    quote: str | None = None
    escaped = False
    index = opening
    while index < len(stripped):
        char = stripped[index]
        if quote is not None:
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in {'"', "'"}:
            quote = char
            index += 1
            continue
        if stripped.startswith(closing, index):
            remainder = stripped[index + len(closing):].strip()
            if remainder and not remainder.startswith("#"):
                return None
            return stripped[opening:index], array
        index += 1
    return None


def _advance_multiline_state(line: str, state: str | None) -> str | None:
    index = 0
    quote: str | None = None
    escaped = False
    while index < len(line):
        if state is not None:
            found = line.find(state, index)
            if found < 0:
                return state
            state = None
            index = found + 3
            continue
        char = line[index]
        if quote is not None:
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char == "#":
            return None
        if line.startswith('\"\"\"', index) or line.startswith("'''", index):
            state = line[index:index + 3]
            index += 3
            continue
        if char in {'"', "'"}:
            quote = char
        index += 1
    return state


def parse_sections(text: str) -> list[TomlSection]:
    matches: list[tuple[int, tuple[str, ...], bool]] = []
    offset = 0
    multiline_state: str | None = None
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        header = _table_header(line) if multiline_state is None else None
        if header is not None:
            raw_key, array = header
            key = _parse_key(raw_key)
            if key is not None:
                matches.append((offset, key, array))
        else:
            multiline_state = _advance_multiline_state(line, multiline_state)
        offset += len(raw_line)
    sections: list[TomlSection] = []
    for index, (start, key, array) in enumerate(matches):
        end = matches[index + 1][0] if index + 1 < len(matches) else len(text)
        sections.append(TomlSection(key, start, end, text[start:end], array=array))
    return sections


def _decode_toml_string(raw: str) -> str | None:
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1]
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, str) else None


def _marketplace_source(section: TomlSection) -> str | None:
    match = SOURCE_RE.search(section.text)
    return _decode_toml_string(match.group("value")) if match else None


def _section_string(section: TomlSection, pattern: re.Pattern[str]) -> str | None:
    match = pattern.search(section.text)
    return _decode_toml_string(match.group("value")) if match else None


@dataclass(frozen=True)
class CodexMarketplaceRepairAssessment:
    status: str
    config_path: str
    reason: str | None = None
    marketplace_ids: tuple[str, ...] = ()
    config_sha256: str | None = None
    replacement_sources: tuple[tuple[str, str], ...] = ()

    @property
    def repairable(self) -> bool:
        return self.status == "repairable"

    def payload(self) -> dict[str, Any]:
        data = asdict(self)
        data["marketplace_ids"] = list(self.marketplace_ids)
        data["replacement_sources"] = [list(item) for item in self.replacement_sources]
        return data


@dataclass
class CodexMarketplaceRepairTransaction:
    assessment: CodexMarketplaceRepairAssessment
    original: bytes
    backup_path: Path
    active: bool = True
    committed: bool = False
    rollback_conflicts: list[str] = field(default_factory=list)
    lock_context: Any = field(default=None, repr=False)

    def _release_lock(self) -> None:
        if self.lock_context is None:
            return
        context = self.lock_context
        self.lock_context = None
        context.__exit__(None, None, None)

    def rollback(self) -> None:
        if not self.active:
            return
        try:
            config = Path(self.assessment.config_path)
            try:
                current = config.read_bytes()
                if _unmanaged_config_signature(current) != _unmanaged_config_signature(self.original):
                    self.rollback_conflicts.append(
                        "Codex config.toml changed outside Tenetora-managed marketplace sections during repair"
                    )
                else:
                    _atomic_write(config, self.original)
            except (OSError, UnicodeError, ValueError) as exc:
                self.rollback_conflicts.append(f"Codex config.toml could not be safely restored: {exc}")
            self.active = False
        finally:
            self._release_lock()
        if self.rollback_conflicts:
            raise RuntimeError(
                "Codex marketplace repair rollback stopped to preserve concurrent configuration changes: "
                + "; ".join(self.rollback_conflicts)
            )

    def commit(self) -> None:
        if not self.active:
            return
        self.committed = True
        self.active = False
        self._release_lock()

    def payload(self) -> dict[str, Any]:
        return {
            "status": "committed" if self.committed else "active" if self.active else "rolled-back",
            "backup": str(self.backup_path),
            "rollback_conflicts": list(self.rollback_conflicts),
            "assessment": self.assessment.payload(),
        }


def _malformed_codex_source_candidate(raw: str) -> Path | None:
    if not raw.startswith(CODEX_MALFORMED_LOCAL_SOURCE_PREFIX):
        return None
    try:
        return validate_unredirected_path(
            Path("/" + raw[len(CODEX_MALFORMED_LOCAL_SOURCE_PREFIX):]),
            label="Codex marketplace snapshot",
        )
    except RuntimeError:
        return None


def _official_snapshot_is_valid(source: Path, marketplace_id: str) -> bool:
    snapshot_name = CODEX_OFFICIAL_MARKETPLACES[marketplace_id]
    snapshot = source / ".agents" / "plugins" / snapshot_name
    try:
        payload = json.loads(validate_unredirected_file_path(snapshot, label="Codex marketplace snapshot").read_text(encoding="utf-8"))
    except (OSError, RuntimeError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("name") == marketplace_id
        and isinstance(payload.get("plugins"), list)
    )


def assess_codex_marketplace_path_repair(path: Path | None = None) -> CodexMarketplaceRepairAssessment:
    """Find only the known malformed Codex official marketplace source encoding."""

    try:
        config = _safe_config_path(path)
    except RuntimeError as error:
        return CodexMarketplaceRepairAssessment("blocked", str(_config_display_path(path)), str(error))
    if not config.exists():
        return CodexMarketplaceRepairAssessment("not-needed", str(config), "Codex config.toml is missing")
    if config.is_symlink() or not config.is_file():
        return CodexMarketplaceRepairAssessment("blocked", str(config), "Codex config.toml is not a regular file")
    try:
        original = config.read_bytes()
        text = original.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        return CodexMarketplaceRepairAssessment("blocked", str(config), f"Codex config.toml is unreadable: {exc}")

    sections = parse_sections(text)
    targets: dict[str, TomlSection] = {}
    for section in sections:
        if len(section.key) != 2 or section.key[0] != "marketplaces":
            continue
        marketplace_id = section.key[1]
        if marketplace_id not in CODEX_OFFICIAL_MARKETPLACES:
            continue
        if marketplace_id in targets or section.array:
            return CodexMarketplaceRepairAssessment(
                "blocked",
                str(config),
                "Codex official marketplace section is duplicated or uses an array table",
            )
        targets[marketplace_id] = section
    if not targets:
        return CodexMarketplaceRepairAssessment("not-needed", str(config), "No malformed official marketplace source")

    expected = codex_home() / ".tmp" / "plugins"
    replacements: list[tuple[str, str]] = []
    for marketplace_id, section in targets.items():
        if _section_string(section, SOURCE_TYPE_RE) != "local":
            continue
        raw = _marketplace_source(section)
        candidate = _malformed_codex_source_candidate(raw or "")
        if candidate is None:
            continue
        try:
            expected = validate_unredirected_path(expected, label="Codex marketplace snapshot")
        except RuntimeError as error:
            return CodexMarketplaceRepairAssessment("blocked", str(config), str(error), marketplace_ids=(marketplace_id,))
        if candidate != expected:
            return CodexMarketplaceRepairAssessment(
                "blocked",
                str(config),
                "Codex official marketplace source is malformed but does not resolve to the expected Codex snapshot directory",
                marketplace_ids=(marketplace_id,),
            )
        if not expected.is_dir() or not _official_snapshot_is_valid(expected, marketplace_id):
            return CodexMarketplaceRepairAssessment(
                "blocked",
                str(config),
                "Codex official marketplace source is malformed and its matching snapshot evidence is unavailable",
                marketplace_ids=(marketplace_id,),
            )
        replacements.append((marketplace_id, str(expected)))

    if not replacements:
        return CodexMarketplaceRepairAssessment("not-needed", str(config), "No repairable malformed official source")
    return CodexMarketplaceRepairAssessment(
        "repairable",
        str(config),
        marketplace_ids=tuple(sorted(item[0] for item in replacements)),
        config_sha256=hashlib.sha256(original).hexdigest(),
        replacement_sources=tuple(sorted(replacements)),
    )


def _replace_marketplace_sources(text: str, assessment: CodexMarketplaceRepairAssessment) -> str:
    replacements = dict(assessment.replacement_sources)
    sections = parse_sections(text)
    for section in sorted(sections, key=lambda item: item.start, reverse=True):
        marketplace_id = section.key[1] if len(section.key) == 2 and section.key[0] == "marketplaces" else None
        replacement = replacements.get(marketplace_id or "")
        if replacement is None:
            continue
        match = SOURCE_RE.search(section.text)
        if match is None:
            raise RuntimeError("Codex official marketplace source became unreadable during repair")
        raw = match.group("value")
        encoded = json.dumps(replacement) if raw.startswith('"') else "'" + replacement + "'"
        start = section.start + match.start("value")
        end = section.start + match.end("value")
        text = text[:start] + encoded + text[end:]
    return text


def begin_codex_marketplace_path_repair(
    assessment: CodexMarketplaceRepairAssessment,
) -> CodexMarketplaceRepairTransaction:
    if not assessment.repairable:
        raise RuntimeError(assessment.reason or "Codex marketplace path repair is not available")
    config = validate_unredirected_file_path(assessment.config_path, label="Codex config.toml")
    lock_context = _codex_recovery_lock(config)
    lock_context.__enter__()
    transaction: CodexMarketplaceRepairTransaction | None = None
    try:
        original = config.read_bytes()
        if not assessment.config_sha256 or hashlib.sha256(original).hexdigest() != assessment.config_sha256:
            raise RuntimeError("Codex config.toml changed after marketplace repair assessment; rerun the upgrade")
        text = original.decode("utf-8")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-%fZ")
        backup_dir = validate_unredirected_path(
            machine_home() / "backups" / "codex-marketplace" / stamp,
            label="Codex marketplace backup",
        )
        ensure_unredirected_directory(backup_dir, label="Codex marketplace backup")
        if os.name != "nt":
            backup_dir.chmod(0o700)
        backup = validate_unredirected_file_path(
            backup_dir / "config.toml", label="Codex marketplace backup file"
        )
        _atomic_write(backup, original)
        if os.name != "nt":
            backup.chmod(0o600)
        _atomic_write(config, _replace_marketplace_sources(text, assessment).encode("utf-8"))
        transaction = CodexMarketplaceRepairTransaction(assessment, original, backup, lock_context=lock_context)
        return transaction
    except Exception:
        if transaction is None or transaction.lock_context is not None:
            lock_context.__exit__(*sys.exc_info())
        raise


def _managed_legacy_payload(path: Path) -> bool:
    try:
        path = validate_unredirected_path(path, label="legacy plugin payload")
    except RuntimeError:
        return False
    manifest = path / ".codex-plugin" / "plugin.json"
    try:
        payload = json.loads(
            validate_unredirected_file_path(
                manifest, label="legacy plugin manifest"
            ).read_text(encoding="utf-8")
        )
    except (OSError, RuntimeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or payload.get("name") != "agent-harness":
        return False
    markers = (
        path / "hooks" / "run-hook",
        path / "hooks" / "agent_harness_hook.py",
        path / "skills" / "agent-harness" / "SKILL.md",
        path / "cli" / "tenetora" / "cli.py",
    )
    return sum(_safe_marker(marker) for marker in markers) >= 2


def _safe_marker(path: Path) -> bool:
    try:
        return validate_unredirected_file_path(path, label="legacy plugin marker").is_file()
    except RuntimeError:
        return False


def _legacy_release_source(raw: str) -> Path | None:
    try:
        source = validate_unredirected_path(raw, label="legacy marketplace source")
        release_root = validate_unredirected_path(
            default_legacy_machine_home() / "releases",
            label="legacy release home",
        )
    except RuntimeError:
        return None
    try:
        relative = source.relative_to(release_root)
    except ValueError:
        return None
    return source if len(relative.parts) == 1 and SEMVER_RE.fullmatch(relative.name) else None


def _managed_canonical_legacy_release(source: Path) -> bool:
    """Prove that a missing legacy release was preserved by machine-home migration."""

    try:
        canonical_release = validate_unredirected_path(
            machine_home() / "releases" / source.name,
            label="canonical legacy release",
        )
        if not canonical_release.is_dir() or is_redirected_path(canonical_release):
            return False
        manifest = validate_unredirected_file_path(
            canonical_release / ".codex-plugin" / "plugin.json",
            label="canonical legacy release manifest",
        )
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, RuntimeError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("name") == "agent-harness"
        and payload.get("version") == source.name
        and _managed_legacy_payload(canonical_release)
    )


def _migration_evidence(source: Path) -> list[str]:
    try:
        marker = validate_unredirected_file_path(
            machine_home() / "state" / "machine-home-migration.json",
            label="machine-home migration evidence",
        )
        expected_legacy = validate_unredirected_path(
            default_legacy_machine_home(),
            label="legacy Agent Harness home",
        )
        source = validate_unredirected_path(source, label="legacy marketplace source")
    except RuntimeError:
        return []
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    try:
        recorded_legacy = validate_unredirected_path(
            str(payload.get("legacy_home", "")),
            label="recorded legacy Agent Harness home",
        )
    except RuntimeError:
        return []
    if recorded_legacy != expected_legacy or source.parent != expected_legacy / "releases":
        return []

    evidence: list[str] = []
    fingerprints = payload.get("legacy_fingerprints")
    if isinstance(fingerprints, list) and len({str(item) for item in fingerprints}) >= 2:
        evidence.append("managed-machine-home-migration")

    # Early 0.3.x migrations could normalize an already-missing legacy home
    # without copying its fingerprints into the marker. The release itself is
    # still usable as evidence when the marker binds both home paths and state.
    try:
        recorded_canonical = validate_unredirected_path(
            str(payload.get("canonical_home", "")),
            label="recorded canonical Tenetora home",
        )
    except RuntimeError:
        recorded_canonical = None
    if (
        payload.get("status") in {"migrated", "merged", "normalized"}
        and recorded_canonical == machine_home()
        and not source.exists()
        and not source.is_symlink()
        and _managed_canonical_legacy_release(source)
    ):
        evidence.append("managed-canonical-legacy-release")
    return evidence


def _legacy_cache_candidates(version: str) -> list[Path]:
    try:
        base = validate_unredirected_path(
            codex_home() / "plugins" / "cache" / LEGACY_MARKETPLACE / "agent-harness",
            label="legacy Codex plugin cache",
        )
    except RuntimeError:
        return []
    candidates = [base / version]
    if base.is_dir():
        candidates.extend(path for path in base.iterdir() if path.is_dir() and path.name != version)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        try:
            path = validate_unredirected_path(path, label="legacy Codex plugin cache")
        except RuntimeError:
            continue
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def assess_legacy_registration(path: Path | None = None) -> RecoveryAssessment:
    try:
        config = _safe_config_path(path)
    except RuntimeError as error:
        return RecoveryAssessment("blocked", str(_config_display_path(path)), str(error))
    if not config.exists():
        return RecoveryAssessment("not-needed", str(config), "Codex config.toml is missing")
    if config.is_symlink() or not config.is_file():
        return RecoveryAssessment("blocked", str(config), "Codex config.toml is not a regular file")
    try:
        original = config.read_bytes()
        text = original.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        return RecoveryAssessment("blocked", str(config), f"Codex config.toml is unreadable: {exc}")
    sections = parse_sections(text)
    digest = hashlib.sha256(original).hexdigest()
    marketplace_sections = [
        item for item in sections if item.key == ("marketplaces", LEGACY_MARKETPLACE)
    ]
    if len(marketplace_sections) > 1 or any(item.array for item in marketplace_sections):
        return RecoveryAssessment("blocked", str(config), "Legacy marketplace section is duplicated or uses an array table")
    marketplace = marketplace_sections[0] if marketplace_sections else None
    if marketplace is None:
        return RecoveryAssessment("not-needed", str(config), "No legacy Agent Harness marketplace registration")
    source_raw = _marketplace_source(marketplace)
    if not source_raw:
        return RecoveryAssessment("blocked", str(config), "Legacy marketplace source is missing or unsupported")
    source = _legacy_release_source(source_raw)
    if source is None:
        return RecoveryAssessment("blocked", str(config), "Legacy marketplace source is outside the default managed release home", marketplace_source=source_raw)

    evidence: list[str] = []
    caches: list[str] = []
    if _managed_legacy_payload(source):
        evidence.append("managed-legacy-marketplace-source")
    for cache in _legacy_cache_candidates(source.name):
        if _managed_legacy_payload(cache):
            evidence.append("managed-legacy-plugin-cache")
            caches.append(str(cache))
    if not source.exists():
        evidence.extend(_migration_evidence(source))
    managed_evidence = {
        "managed-legacy-marketplace-source",
        "managed-legacy-plugin-cache",
        "managed-machine-home-migration",
        "managed-canonical-legacy-release",
    }
    if not set(evidence) & managed_evidence:
        return RecoveryAssessment(
            "blocked",
            str(config),
            "Legacy marketplace ownership could not be proven from managed payload or migration evidence",
            marketplace_source=source_raw,
            ownership_evidence=sorted(set(evidence)),
            legacy_caches=caches,
        )

    selected: list[TomlSection] = [marketplace]
    plugin_sections = [item for item in sections if item.key == ("plugins", LEGACY_PLUGIN_ID)]
    if len(plugin_sections) > 1 or any(item.array for item in plugin_sections):
        return RecoveryAssessment(
            "blocked",
            str(config),
            "Legacy plugin section is duplicated or uses an array table",
            marketplace_source=source_raw,
            ownership_evidence=sorted(set(evidence)),
            legacy_caches=caches,
        )
    plugin = plugin_sections[0] if plugin_sections else None
    if plugin is not None:
        selected.append(plugin)
    for item in sections:
        if len(item.key) != 3 or item.key[:2] != ("hooks", "state"):
            continue
        hook_id = item.key[2]
        if not hook_id.startswith(LEGACY_HOOK_PREFIX):
            continue
        if item.array or sum(candidate.key == item.key for candidate in sections) > 1:
            return RecoveryAssessment(
                "blocked",
                str(config),
                "Legacy Hook trust section is duplicated or uses an array table",
                marketplace_source=source_raw,
                ownership_evidence=sorted(set(evidence)),
                legacy_caches=caches,
            )
        hook_name = hook_id[len(LEGACY_HOOK_PREFIX):]
        if _managed_hook_event(hook_name) is None:
            return RecoveryAssessment(
                "blocked",
                str(config),
                f"Unexpected legacy Hook trust key requires review: {hook_name}",
                marketplace_source=source_raw,
                ownership_evidence=sorted(set(evidence)),
                legacy_caches=caches,
            )
        selected.append(item)
    backup_root = machine_home() / "backups" / "codex-registration"
    return RecoveryAssessment(
        "recoverable",
        str(config),
        marketplace_source=source_raw,
        sections=[".".join(item.key) for item in selected],
        section_keys=[list(item.key) for item in selected],
        ownership_evidence=sorted(set(evidence)),
        legacy_caches=caches,
        backup_root=str(backup_root),
        config_sha256=digest,
    )


def _remove_managed_sections(text: str, assessment: RecoveryAssessment) -> str:
    wanted = {tuple(item) for item in assessment.section_keys}
    selected = [item for item in parse_sections(text) if item.key in wanted]
    for item in sorted(selected, key=lambda candidate: candidate.start, reverse=True):
        text = text[:item.start] + text[item.end:]
    return text


def _product_section(section: TomlSection) -> bool:
    if section.key in {
        ("marketplaces", LEGACY_MARKETPLACE),
        ("marketplaces", CANONICAL_MARKETPLACE),
        *(("marketplaces", item) for item in CODEX_OFFICIAL_MARKETPLACES),
        ("plugins", LEGACY_PLUGIN_ID),
        ("plugins", CANONICAL_PLUGIN_ID),
    }:
        return True
    if len(section.key) != 3 or section.key[:2] != ("hooks", "state"):
        return False
    hook_id = section.key[2]
    return any(
        hook_id.startswith(prefix)
        and _managed_hook_event(hook_id[len(prefix):]) is not None
        for prefix in (LEGACY_HOOK_PREFIX, CANONICAL_HOOK_PREFIX)
    )


def _managed_hook_event(hook_name: str) -> str | None:
    """Return a known Hook event, accepting only Codex numeric instance indexes."""
    parts = hook_name.split(":")
    if not parts or parts[0] not in TRUSTED_HOOK_NAMES:
        return None
    suffix = parts[1:]
    if len(suffix) > 2 or any(HOOK_INSTANCE_INDEX_RE.fullmatch(part) is None for part in suffix):
        return None
    return parts[0]


def _normalize_section_boundary(text: str) -> str:
    if not text.strip():
        return ""
    return re.sub(r"(?:\r?\n[ \t]*)+\Z", "\n", text)


def _unmanaged_config_signature(content: bytes) -> tuple[str, tuple[tuple[tuple[str, ...], bool, str], ...]]:
    text = content.decode("utf-8")
    sections = parse_sections(text)
    preamble_end = sections[0].start if sections else len(text)
    preamble = _normalize_section_boundary(text[:preamble_end])
    unmanaged = tuple(
        (section.key, section.array, _normalize_section_boundary(section.text))
        for section in sections
        if not _product_section(section)
    )
    return preamble, unmanaged


def _atomic_write(path: Path, content: bytes) -> None:
    path = validate_unredirected_file_path(path, label="Codex config.toml")
    ensure_unredirected_directory(path.parent, label="Codex config directory")
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _codex_recovery_lock(config: Path):
    """Serialize recovery transactions targeting one Codex config."""

    config = validate_unredirected_file_path(config, label="Codex config.toml")
    lock_path = config.with_name(f".{config.name}.tenetora-recovery.lock")
    ensure_unredirected_directory(lock_path.parent, label="Codex recovery lock directory")
    with locked_file(lock_path):
        yield


def begin_recovery(assessment: RecoveryAssessment) -> RecoveryTransaction:
    if not assessment.recoverable:
        raise RuntimeError(assessment.reason or "Legacy Codex registration is not recoverable")
    config = validate_unredirected_file_path(assessment.config_path, label="Codex config.toml")
    lock_context = _codex_recovery_lock(config)
    lock_context.__enter__()
    transaction: RecoveryTransaction | None = None
    try:
        original = config.read_bytes()
        current_sha256 = hashlib.sha256(original).hexdigest()
        if not assessment.config_sha256 or current_sha256 != assessment.config_sha256:
            raise RuntimeError("Codex config.toml changed after recovery assessment; rerun the check")
        try:
            text = original.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError("Codex config.toml is not UTF-8") from exc
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-%fZ")
        backup_dir = validate_unredirected_path(
            Path(assessment.backup_root or machine_home() / "backups" / "codex-registration") / stamp,
            label="Codex recovery backup",
        )
        ensure_unredirected_directory(backup_dir, label="Codex recovery backup")
        if os.name != "nt":
            backup_dir.chmod(0o700)
        backup = validate_unredirected_file_path(
            backup_dir / "config.toml", label="Codex recovery backup file"
        )
        _atomic_write(backup, original)
        if os.name != "nt":
            backup.chmod(0o600)
        updated = _remove_managed_sections(text, assessment).encode("utf-8")
        _atomic_write(config, updated)
        transaction = RecoveryTransaction(assessment, original, backup, lock_context=lock_context)
        archive_root = backup_dir / "legacy-cache"
        try:
            for raw in assessment.legacy_caches:
                cache = Path(raw)
                if not _managed_legacy_payload(cache):
                    continue
                ensure_unredirected_directory(archive_root, label="Codex legacy cache archive")
                target = archive_root / cache.name
                suffix = 1
                while target.exists():
                    target = archive_root / f"{cache.name}-{suffix}"
                    suffix += 1
                target = validate_unredirected_entry_path(
                    target, label="Codex legacy cache archive entry"
                )
                cache.replace(target)
                transaction.archived_caches.append(str(target))
                transaction.cache_moves.append((str(cache), str(target)))
        except Exception:
            transaction.rollback()
            raise
        return transaction
    except Exception:
        if transaction is None or transaction.lock_context is not None:
            lock_context.__exit__(*sys.exc_info())
        raise
