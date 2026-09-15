#!/usr/bin/env python3
"""Machine-local Tenetora project installation registry."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
from typing import Callable, Iterator


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
PACKAGE_ROOT = SCRIPT_DIRECTORY.parent
CLI_ROOT = PACKAGE_ROOT / "cli"
if str(CLI_ROOT) not in sys.path:
    sys.path.insert(0, str(CLI_ROOT))

from tenetora.brand import (  # noqa: E402
    default_legacy_machine_home,
    default_machine_home,
    machine_home,
    user_home,
    validate_managed_home_path,
)
from tenetora.path_security import is_redirected_path, validate_unredirected_file_path  # noqa: E402
from tenetora.platform_contracts import supported_platforms  # noqa: E402


REGISTRY_VERSION = 1
MAX_REGISTRY_BYTES = 1024 * 1024
MAX_PROJECTS = 2048
REGISTRY_STALE_DAYS = 30
DEFAULT_DISCOVERY_DEPTH = 6
MAX_DISCOVERY_DEPTH = 12
DEFAULT_DISCOVERY_CANDIDATES = 5000
MAX_DISCOVERY_CANDIDATES = 50000
AUTO_DISCOVERY_DEPTH = 4
AUTO_DISCOVERY_CANDIDATES = 5000
MAX_AUTO_DISCOVERY_ROOTS = 256
LOCK_STALE_SECONDS = 120
LOCK_WAIT_SECONDS = 2.0
MAX_LEGACY_LOG_FILES = 256
MAX_LEGACY_LOG_BYTES = 2 * 1024 * 1024
SUPPORTED_TOOLS = supported_platforms()
SUPPORTED_TOOL_PATTERN = "|".join(re.escape(tool) for tool in SUPPORTED_TOOLS)
LEGACY_GLOBAL_SURFACE_RE = re.compile(
    r"(?:^|[\s\"'])(?:INSTALLED|UPDATED|CURRENT|已安装|已更新|已是最新) global:"
    rf"({SUPPORTED_TOOL_PATTERN}):"
)
LEGACY_DRY_RUN_EVIDENCE_RE = re.compile(
    r"(?:\bwould\s+(?:install|update)\b|"
    r"\bDRY_RUN\b|\bdry run completed\b|"
    r"将[^\n]{0,512}?(?:安装|更新)|演练完成)",
    re.IGNORECASE,
)
SKIP_DIRECTORY_NAMES = {
    ".agent-harness",
    ".cache",
    ".cargo",
    ".config",
    ".tenetora",
    ".git",
    ".gradle",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    ".local",
    ".npm",
    ".ssh",
    ".Trash",
    "Applications",
    "Library",
    "Movies",
    "Music",
    "Pictures",
    "Public",
    "build",
    "dist",
    "node_modules",
    "target",
    "venv",
}
AUTO_DISCOVERY_DIRECTORY_NAMES = (
    "Projects",
    "projects",
    "workspace",
    "workspaces",
    "src",
    "code",
    "dev",
    "repos",
    "repositories",
    "work",
)
PROJECT_SURFACE_MARKERS = (
    Path(".agents/skills/tenetora/SKILL.md"),
    Path(".agents/skills/agent-harness/SKILL.md"),
    Path(".codex/skills/tenetora/SKILL.md"),
    Path(".codex/skills/agent-harness/SKILL.md"),
    Path(".claude/skills/tenetora/SKILL.md"),
    Path(".claude/skills/agent-harness/SKILL.md"),
    Path(".cursor/skills/tenetora/SKILL.md"),
    Path(".cursor/skills/agent-harness/SKILL.md"),
    Path(".opencode/skills/tenetora/SKILL.md"),
    Path(".opencode/skills/agent-harness/SKILL.md"),
    Path(".pi/skills/tenetora/SKILL.md"),
    Path(".pi/skills/agent-harness/SKILL.md"),
    Path(".zcode/skills/tenetora/SKILL.md"),
    Path(".zcode/skills/agent-harness/SKILL.md"),
    Path(".codex/hooks.json"),
    Path(".cursor/hooks.json"),
    Path(".opencode/plugins/tenetora.js"),
    Path(".opencode/plugins/agent-harness.js"),
    Path(".pi/extensions/tenetora.ts"),
)
PROJECT_SKILL_DIRECTORIES = {
    "agents": Path(".agents/skills"),
    "codex": Path(".codex/skills"),
    "claude": Path(".claude/skills"),
    "cursor": Path(".cursor/skills"),
    "opencode": Path(".opencode/skills"),
    "pi": Path(".pi/skills"),
    "zcode": Path(".zcode/skills"),
}
GOVERNANCE_STATES = {"canonical", "legacy-owned", "needs-review"}
GOVERNANCE_LAYOUTS = {".tenetora", ".harness"}
CANONICAL_GOVERNANCE_CLASSIFICATIONS = {
    "canonical",
    "canonical-with-foreign",
    "canonical-with-preserved-legacy",
}
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


class RegistryError(RuntimeError):
    """Raised when machine-local installation registry data is unsafe."""


def assert_secure_owner_mode(path: Path, *, label: str) -> None:
    if os.name == "nt" or not hasattr(os, "getuid"):
        return
    try:
        info = path.lstat()
    except OSError as exc:
        raise RegistryError(f"cannot inspect {label}: {path}: {exc}") from exc
    if info.st_uid != os.getuid():
        raise RegistryError(f"{label} is not owned by the current user: {path}")
    if info.st_mode & 0o022:
        raise RegistryError(f"{label} is writable by group or others: {path}")


def secure_chmod(path: Path, mode: int, *, label: str) -> None:
    try:
        path.chmod(mode)
    except OSError as exc:
        if os.name != "nt":
            raise RegistryError(f"cannot secure {label}: {path}: {exc}") from exc
    assert_secure_owner_mode(path, label=label)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def harness_home() -> Path:
    # The registry-specific override keeps tests and administrative tooling from
    # changing the runtime-home contract used by hooks and CLI shims.
    configured = os.environ.get("TENETORA_REGISTRY_HOME")
    if configured:
        try:
            return validate_managed_home_path(configured, label="TENETORA_REGISTRY_HOME")
        except RuntimeError as exc:
            raise RegistryError(str(exc)) from exc
    return machine_home()


def state_directory(home: Path | None = None, *, create: bool = False) -> Path:
    try:
        managed_home = validate_managed_home_path(
            home if home is not None else harness_home(),
            label="Tenetora registry home",
        )
    except RuntimeError as exc:
        raise RegistryError(str(exc)) from exc
    path = managed_home / "state"
    if (path.exists() or is_redirected_path(path)) and (is_redirected_path(path) or not path.is_dir()):
        raise RegistryError(f"installation registry state directory is unsafe: {path}")
    if create:
        path.mkdir(parents=True, exist_ok=True)
        secure_chmod(path, 0o700, label="installation registry state directory")
    elif path.exists():
        assert_secure_owner_mode(path, label="installation registry state directory")
    return path


def registry_path(home: Path | None = None) -> Path:
    return state_directory(home) / "installations.json"


def lock_path(home: Path | None = None) -> Path:
    return state_directory(home) / "installations.lock"


def empty_registry() -> dict[str, object]:
    now = utc_now()
    return {
        "version": REGISTRY_VERSION,
        "updated_at": now,
        "global": {
            "surfaces": {},
            "ignored_tools": [],
            "last_seen_at": now,
        },
        "projects": [],
    }


def project_id(path: Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:20]


def canonical_project_path(raw: str | Path, *, home: Path | None = None) -> Path:
    try:
        path = validate_managed_home_path(raw, label="project registry path")
        user_home = validate_managed_home_path(Path.home(), label="user home")
        managed_home = validate_managed_home_path(
            home if home is not None else harness_home(),
            label="Tenetora registry home",
        )
        path = path.resolve(strict=False)
        user_home = user_home.resolve(strict=False)
        managed_home = managed_home.resolve(strict=False)
    except RuntimeError as exc:
        raise RegistryError(str(exc)) from exc
    if not path.is_absolute() or path == Path(path.anchor) or path in {user_home, managed_home}:
        raise RegistryError(f"unsafe project registry path: {path}")
    try:
        managed_home.relative_to(path)
    except ValueError:
        pass
    else:
        raise RegistryError(f"project registry path contains Tenetora home: {path}")
    return path


def normalize_surfaces(raw: object, *, scope: str = "project") -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        raise RegistryError("installation surfaces must be an object")
    normalized: dict[str, list[str]] = {}
    for tool, scopes in raw.items():
        if tool not in SUPPORTED_TOOLS:
            raise RegistryError(f"unsupported installation surface tool: {tool}")
        if not isinstance(scopes, list) or not scopes:
            raise RegistryError(f"installation surface scopes must be a non-empty list: {tool}")
        if any(item != scope for item in scopes):
            raise RegistryError(f"installation registry may only contain {scope} scope: {tool}")
        normalized[tool] = [scope]
    return dict(sorted(normalized.items()))


def normalize_ignored_tools(raw: object) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RegistryError("global ignored_tools must be an array")
    if any(tool not in SUPPORTED_TOOLS for tool in raw):
        raise RegistryError("global ignored_tools contains an unsupported tool")
    return sorted(set(raw))


def normalize_governance(raw: object) -> dict[str, object] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RegistryError("project governance registration must be an object")
    state = str(raw.get("state") or "")
    if state not in GOVERNANCE_STATES:
        raise RegistryError(f"unsupported project governance state: {state or '<empty>'}")
    layout = str(raw.get("layout") or "")
    if layout not in GOVERNANCE_LAYOUTS:
        raise RegistryError(f"unsupported project governance layout: {layout or '<empty>'}")
    classification = str(raw.get("classification") or state)
    fingerprint = raw.get("source_fingerprint")
    if fingerprint is not None and (
        not isinstance(fingerprint, str) or not FINGERPRINT_RE.fullmatch(fingerprint)
    ):
        raise RegistryError("project governance source_fingerprint must be 64 lowercase hex characters")
    normalized: dict[str, object] = {
        "state": state,
        "layout": layout,
        "classification": classification,
    }
    if fingerprint:
        normalized["source_fingerprint"] = fingerprint
    return normalized


def governance_registration_for_classification(
    status: str,
    *,
    canonical_present: bool,
    source_fingerprint: object = None,
) -> dict[str, object] | None:
    if status in {"absent", "foreign"}:
        return None
    if status in CANONICAL_GOVERNANCE_CLASSIFICATIONS:
        state, layout = "canonical", ".tenetora"
    elif status == "legacy-owned":
        state, layout = "legacy-owned", ".harness"
    else:
        state = "needs-review"
        layout = ".tenetora" if canonical_present else ".harness"
    registration: dict[str, object] = {
        "state": state,
        "layout": layout,
        "classification": status,
    }
    if isinstance(source_fingerprint, str) and source_fingerprint:
        registration["source_fingerprint"] = source_fingerprint
    return normalize_governance(registration)


def validate_registry(payload: object, *, home: Path | None = None) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise RegistryError("installation registry must contain a JSON object")
    if payload.get("version") != REGISTRY_VERSION:
        raise RegistryError(f"unsupported installation registry version: {payload.get('version')}")
    projects = payload.get("projects")
    if not isinstance(projects, list):
        raise RegistryError("installation registry projects must be a list")
    if len(projects) > MAX_PROJECTS:
        raise RegistryError(f"installation registry exceeds {MAX_PROJECTS} projects")
    raw_global = payload.get("global", {})
    if not isinstance(raw_global, dict):
        raise RegistryError("installation registry global state must be an object")
    normalized_global = {
        "surfaces": normalize_surfaces(raw_global.get("surfaces", {}), scope="global"),
        "ignored_tools": normalize_ignored_tools(raw_global.get("ignored_tools", [])),
        "last_seen_at": str(raw_global.get("last_seen_at") or payload.get("updated_at") or utc_now()),
    }
    overlap = set(normalized_global["surfaces"]) & set(normalized_global["ignored_tools"])
    if overlap:
        raise RegistryError(f"global tools cannot be both registered and ignored: {', '.join(sorted(overlap))}")
    normalized_projects: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in projects:
        if not isinstance(item, dict):
            raise RegistryError("installation registry project entry must be an object")
        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise RegistryError("installation registry project path must be a non-empty string")
        path = canonical_project_path(raw_path, home=home)
        key = str(path)
        if key in seen:
            raise RegistryError(f"duplicate installation registry project: {path}")
        seen.add(key)
        governance = normalize_governance(item.get("governance"))
        surfaces = normalize_surfaces(item.get("surfaces", {}))
        if not surfaces and governance is None:
            raise RegistryError("installation registry project must contain surfaces or governance metadata")
        normalized_projects.append(
            {
                "path": key,
                "project_id": project_id(path),
                "surfaces": surfaces,
                **({"governance": governance} if governance is not None else {}),
                "last_seen_at": str(item.get("last_seen_at") or payload.get("updated_at") or utc_now()),
            }
        )
    normalized_projects.sort(key=lambda item: str(item["path"]))
    return {
        "version": REGISTRY_VERSION,
        "updated_at": str(payload.get("updated_at") or utc_now()),
        "global": normalized_global,
        "projects": normalized_projects,
    }


def load_registry(home: Path | None = None) -> dict[str, object]:
    path = registry_path(home)
    redirected = is_redirected_path(path)
    if not path.exists() and not redirected:
        return empty_registry()
    if redirected or not path.is_file():
        raise RegistryError(f"installation registry is not a regular file: {path}")
    assert_secure_owner_mode(path, label="installation registry")
    if path.stat().st_size > MAX_REGISTRY_BYTES:
        raise RegistryError(f"installation registry exceeds {MAX_REGISTRY_BYTES} bytes: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"invalid installation registry: {path}: {exc}") from exc
    return validate_registry(payload, home=home)


def _filesystem_identity(path: Path) -> tuple[object, ...]:
    try:
        info = path.stat()
    except OSError:
        return ("path", str(path))
    return ("inode", info.st_dev, info.st_ino)


@contextmanager
def registry_lock(home: Path | None = None) -> Iterator[None]:
    path = state_directory(home, create=True) / "installations.lock"
    if is_redirected_path(path):
        raise RegistryError(f"installation registry lock is unsafe: {path}")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(path), flags, 0o600)
    except OSError as exc:
        raise RegistryError(f"installation registry lock is unsafe: {path}") from exc
    handle = os.fdopen(fd, "r+b")
    if is_redirected_path(path):
        handle.close()
        raise RegistryError(f"installation registry lock is unsafe: {path}")
    info = os.fstat(handle.fileno())
    if not stat.S_ISREG(info.st_mode):
        handle.close()
        raise RegistryError(f"installation registry lock is unsafe: {path}")
    if os.name != "nt" and hasattr(os, "getuid"):
        if info.st_uid != os.getuid():
            handle.close()
            raise RegistryError(f"installation registry lock is not owned by the current user: {path}")
        if info.st_mode & 0o022:
            handle.close()
            raise RegistryError(f"installation registry lock is writable by group or others: {path}")
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    locked = False
    try:
        try:
            import fcntl
        except ImportError:
            import msvcrt

            if info.st_size == 0:
                handle.write(b"0")
                handle.flush()
            while not locked:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RegistryError(f"installation registry is locked by another process: {path}")
                    time.sleep(0.02)
        else:
            while not locked:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RegistryError(f"installation registry is locked by another process: {path}")
                    time.sleep(0.02)
        handle.seek(0)
        handle.truncate()
        handle.write((json.dumps({"pid": os.getpid(), "created": time.time()}) + "\n").encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
        yield
    finally:
        if locked:
            try:
                import fcntl
            except ImportError:
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def write_registry(payload: object, *, home: Path | None = None) -> Path:
    normalized = validate_registry(payload, home=home)
    normalized["updated_at"] = utc_now()
    path = state_directory(home, create=True) / "installations.json"
    if is_redirected_path(path) or path.exists() and not path.is_file():
        raise RegistryError(f"installation registry is not a regular file: {path}")
    serialized = json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if len(serialized.encode("utf-8")) > MAX_REGISTRY_BYTES:
        raise RegistryError(f"installation registry exceeds {MAX_REGISTRY_BYTES} bytes")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        secure_chmod(temporary, 0o600, label="temporary installation registry")
        os.replace(temporary, path)
        secure_chmod(path, 0o600, label="installation registry")
    finally:
        temporary.unlink(missing_ok=True)
    return path


def registered_projects(home: Path | None = None) -> list[Path]:
    registry = load_registry(home)
    return [Path(str(item["path"])) for item in registry["projects"]]


def registered_project_entries(home: Path | None = None) -> list[dict[str, object]]:
    registry = load_registry(home)
    return [dict(item) for item in registry["projects"]]


def auto_discovery_roots(
    current_project: Path | None = None,
    *,
    home: Path | None = None,
    include_home: bool = False,
) -> list[Path]:
    """Return bounded roots suitable for upgrade-time project discovery."""

    candidates: list[Path] = []
    configured = os.environ.get("TENETORA_DISCOVERY_ROOTS", "")
    for raw in configured.split(os.pathsep):
        if not raw.strip():
            continue
        try:
            candidates.append(
                validate_managed_home_path(
                    raw,
                    label="TENETORA_DISCOVERY_ROOTS",
                ).resolve(strict=False)
            )
        except RuntimeError as exc:
            raise RegistryError(str(exc)) from exc
    if current_project is not None:
        current_raw = Path(current_project).expanduser()
        if is_redirected_path(current_raw):
            raise RegistryError(f"current project must not be a directory symlink: {current_raw}")
        try:
            current = validate_managed_home_path(
                current_raw,
                label="current project",
            ).resolve(strict=False)
        except RuntimeError as exc:
            raise RegistryError(str(exc)) from exc
        if looks_like_tenetora_project(current):
            candidates.append(current.parent)
    try:
        candidates.extend(
            Path(str(item["path"])).expanduser().resolve(strict=False).parent
            for item in registered_project_entries(home)
        )
    except RegistryError:
        pass
    try:
        user_home = validate_managed_home_path(Path.home(), label="user home").resolve(strict=False)
    except RuntimeError as exc:
        raise RegistryError(str(exc)) from exc
    if include_home:
        try:
            children = sorted(user_home.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            children = []
        visible_directories: list[Path] = []
        for child in children:
            if child.name.startswith(".") or child.name in SKIP_DIRECTORY_NAMES or is_redirected_path(child):
                continue
            try:
                if child.is_dir():
                    visible_directories.append(child)
            except OSError:
                continue
        if len(visible_directories) > MAX_AUTO_DISCOVERY_ROOTS:
            raise RegistryError(
                f"automatic discovery found more than {MAX_AUTO_DISCOVERY_ROOTS} user-home roots; "
                "set TENETORA_DISCOVERY_ROOTS to bounded project roots"
            )
        candidates.extend(visible_directories)
    candidates.extend(user_home / name for name in AUTO_DISCOVERY_DIRECTORY_NAMES)

    roots: list[Path] = []
    seen: set[tuple[object, ...]] = set()
    for raw in candidates:
        raw_path = Path(raw).expanduser()
        if is_redirected_path(raw_path):
            continue
        try:
            root = validate_managed_home_path(
                raw_path,
                label="automatic discovery root",
            ).resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        identity = _filesystem_identity(root)
        if (
            identity in seen
            or is_reserved_project_root(root, home=home)
            or not root.is_dir()
            or is_redirected_path(root)
        ):
            continue
        seen.add(identity)
        roots.append(root)
    return sorted(roots)


def registered_global_surfaces(home: Path | None = None) -> dict[str, list[str]]:
    registry = load_registry(home)
    global_state = registry["global"]
    return dict(global_state["surfaces"])


def ignored_global_tools(home: Path | None = None) -> set[str]:
    registry = load_registry(home)
    global_state = registry["global"]
    return set(global_state["ignored_tools"])


def legacy_global_surface_evidence(home: Path | None = None) -> dict[str, list[str]]:
    """Recover proven global install intent from bounded Tenetora-owned logs."""
    managed_home = (home or harness_home()).resolve(strict=False)
    log_root = managed_home / "logs"
    if not log_root.is_dir() or is_redirected_path(log_root):
        return {}
    candidates = sorted(
        (
            path
            for pattern in ("install-*.log", "install-*.jsonl")
            for path in log_root.glob(pattern)
        ),
        key=lambda path: path.name,
        reverse=True,
    )[:MAX_LEGACY_LOG_FILES]
    found: set[str] = set()
    for path in candidates:
        try:
            if is_redirected_path(path) or not path.is_file() or path.stat().st_size > MAX_LEGACY_LOG_BYTES:
                continue
            if os.name != "nt" and hasattr(os, "getuid"):
                info = path.stat()
                if info.st_uid != os.getuid() or info.st_mode & 0o022:
                    continue
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in content.splitlines():
            candidate = line
            if path.suffix == ".jsonl":
                try:
                    payload = json.loads(line)
                except (TypeError, ValueError):
                    payload = None
                if isinstance(payload, dict):
                    candidate = str(payload.get("message") or "")
                    if payload.get("event") == "complete" and str(payload.get("status") or "").upper() == "DRY_RUN":
                        continue
            if LEGACY_DRY_RUN_EVIDENCE_RE.search(candidate):
                continue
            found.update(LEGACY_GLOBAL_SURFACE_RE.findall(candidate))
        if len(found) == len(SUPPORTED_TOOLS):
            break
    return {tool: ["global"] for tool in sorted(found)}


def effective_global_surfaces(home: Path | None = None) -> dict[str, list[str]]:
    registered = registered_global_surfaces(home)
    ignored = ignored_global_tools(home)
    for tool, scopes in legacy_global_surface_evidence(home).items():
        if tool not in ignored:
            registered.setdefault(tool, scopes)
    return dict(sorted(registered.items()))


def upsert_global_surfaces(tools: list[str], *, home: Path | None = None) -> Path:
    selected = sorted(set(tools))
    if any(tool not in SUPPORTED_TOOLS for tool in selected):
        raise RegistryError("cannot register unsupported global installation surface")
    if not selected:
        return registry_path(home)
    with registry_lock(home):
        registry = load_registry(home)
        global_state = registry["global"]
        surfaces = dict(global_state["surfaces"])
        ignored = set(global_state["ignored_tools"])
        for tool in selected:
            surfaces[tool] = ["global"]
            ignored.discard(tool)
        registry["global"] = {
            "surfaces": dict(sorted(surfaces.items())),
            "ignored_tools": sorted(ignored),
            "last_seen_at": utc_now(),
        }
        return write_registry(registry, home=home)


def forget_global_surfaces(tools: list[str], *, home: Path | None = None) -> Path:
    selected = sorted(set(tools))
    if any(tool not in SUPPORTED_TOOLS for tool in selected):
        raise RegistryError("cannot forget unsupported global installation surface")
    if not selected:
        return registry_path(home)
    with registry_lock(home):
        registry = load_registry(home)
        global_state = registry["global"]
        surfaces = dict(global_state["surfaces"])
        ignored = set(global_state["ignored_tools"])
        for tool in selected:
            surfaces.pop(tool, None)
            ignored.add(tool)
        registry["global"] = {
            "surfaces": dict(sorted(surfaces.items())),
            "ignored_tools": sorted(ignored),
            "last_seen_at": utc_now(),
        }
        return write_registry(registry, home=home)


def upsert_project(
    project: Path,
    surfaces: dict[str, list[str]],
    *,
    governance: dict[str, object] | None = None,
    home: Path | None = None,
) -> Path:
    canonical = canonical_project_path(project, home=home)
    normalized_surfaces = normalize_surfaces(surfaces)
    normalized_governance = normalize_governance(governance)
    if not normalized_surfaces and normalized_governance is None:
        return registry_path(home)
    with registry_lock(home):
        registry = load_registry(home)
        previous = next(
            (item for item in registry["projects"] if item["path"] == str(canonical)),
            None,
        )
        projects = [item for item in registry["projects"] if item["path"] != str(canonical)]
        if previous is not None:
            if not normalized_surfaces:
                normalized_surfaces = dict(previous.get("surfaces", {}))
            if normalized_governance is None and isinstance(previous.get("governance"), dict):
                normalized_governance = dict(previous["governance"])
        projects.append(
            {
                "path": str(canonical),
                "project_id": project_id(canonical),
                "surfaces": normalized_surfaces,
                **({"governance": normalized_governance} if normalized_governance is not None else {}),
                "last_seen_at": utc_now(),
            }
        )
        registry["projects"] = projects
        return write_registry(registry, home=home)


def registry_project_expired(entry: dict[str, object], *, now: datetime | None = None) -> bool:
    """Return whether a stale project has exceeded the bounded hygiene window."""

    raw = entry.get("last_seen_at")
    if not isinstance(raw, str) or not raw:
        return False
    try:
        seen = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return current - seen.astimezone(timezone.utc) >= timedelta(days=REGISTRY_STALE_DAYS)


def prune_projects(paths: list[Path], *, home: Path | None = None) -> Path:
    canonical = {str(canonical_project_path(path, home=home)) for path in paths}
    with registry_lock(home):
        registry = load_registry(home)
        registry["projects"] = [item for item in registry["projects"] if item["path"] not in canonical]
        return write_registry(registry, home=home)


def is_reserved_project_root(path: Path, *, home: Path | None = None) -> bool:
    """Return whether a path is a user or Tenetora machine home, never a project."""
    try:
        candidate = Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return False

    reserved: set[Path] = set()

    def add(raw: Path | str, label: str) -> None:
        try:
            normalized = validate_managed_home_path(raw, label=label).resolve(strict=False)
        except (OSError, RuntimeError):
            return
        reserved.update({Path(normalized.anchor), normalized})

    # Windows may expose USERPROFILE while callers or shells still provide HOME;
    # reserve both identities so a configured home cannot become a project root.
    add(Path.home(), "user home")
    add(user_home(), "configured user home")
    add(machine_home(), "active machine home")
    add(default_machine_home(), "default machine home")
    add(default_legacy_machine_home(), "legacy machine home")
    add(home if home is not None else harness_home(), "Tenetora registry home")
    return candidate in reserved


def looks_like_tenetora_project(path: Path) -> bool:
    if is_reserved_project_root(path):
        return False
    return (
        any((path / marker).is_file() for marker in PROJECT_SURFACE_MARKERS)
        or (path / ".tenetora").is_dir()
        or is_redirected_path(path / ".tenetora")
        or (path / ".harness").is_dir()
        or is_redirected_path(path / ".harness")
    )


def detect_project_surfaces(path: Path) -> dict[str, list[str]]:
    """Return project skill surfaces that carry a current or legacy router skill."""
    project = canonical_project_path(path)
    surfaces: dict[str, list[str]] = {}
    for tool, relative in PROJECT_SKILL_DIRECTORIES.items():
        for router_name in ("tenetora", "agent-harness"):
            skill = project / relative / router_name / "SKILL.md"
            try:
                skill = validate_unredirected_file_path(
                    skill,
                    label=f"{tool} {router_name} project skill",
                )
            except RuntimeError:
                continue
            if not skill.is_file():
                continue
            try:
                head = skill.read_text(encoding="utf-8")[:4096]
            except (OSError, UnicodeError):
                continue
            expected_title = "# Tenetora" if router_name == "tenetora" else "# Agent Harness"
            if f"name: {router_name}" in head and (
                expected_title in head or router_name == "agent-harness" and "# Tenetora" in head
            ):
                surfaces[tool] = ["project"]
                break
    return surfaces


def discover_project_candidates(
    roots: list[Path],
    *,
    max_depth: int = DEFAULT_DISCOVERY_DEPTH,
    max_candidates: int = DEFAULT_DISCOVERY_CANDIDATES,
    detector: Callable[[Path], bool] = looks_like_tenetora_project,
) -> list[Path]:
    if not roots:
        raise RegistryError("at least one discovery root is required")
    if not 0 <= max_depth <= MAX_DISCOVERY_DEPTH:
        raise RegistryError(f"discovery depth must be between 0 and {MAX_DISCOVERY_DEPTH}")
    if not 1 <= max_candidates <= MAX_DISCOVERY_CANDIDATES:
        raise RegistryError(f"discovery candidate limit must be between 1 and {MAX_DISCOVERY_CANDIDATES}")
    discovered: dict[tuple[object, ...], Path] = {}
    visited = 0
    for raw_root in roots:
        raw_root_path = Path(raw_root).expanduser()
        if is_redirected_path(raw_root_path):
            raise RegistryError(f"discovery root must not be a directory symlink: {raw_root_path}")
        try:
            root = validate_managed_home_path(
                raw_root_path,
                label="discovery root",
            ).resolve(strict=False)
        except RuntimeError as exc:
            raise RegistryError(str(exc)) from exc
        if (
            not root.is_absolute()
            or root == Path(root.anchor)
            or not root.exists()
            or not root.is_dir()
            or is_redirected_path(root)
        ):
            raise RegistryError(f"discovery root must be an existing regular directory: {root}")
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            visited += 1
            if visited > max_candidates:
                raise RegistryError(f"discovery exceeded candidate limit {max_candidates}")
            if not is_reserved_project_root(current) and detector(current):
                resolved = current.resolve(strict=False)
                discovered.setdefault(_filesystem_identity(resolved), resolved)
            if depth >= max_depth:
                continue
            try:
                children = sorted(current.iterdir(), key=lambda item: item.name)
            except OSError:
                continue
            for child in reversed(children):
                if child.name in SKIP_DIRECTORY_NAMES or is_redirected_path(child):
                    continue
                try:
                    is_directory = child.is_dir()
                except OSError:
                    continue
                if is_directory:
                    stack.append((child, depth + 1))
    return sorted(discovered.values())
