"""Environment detection for Tenetora CLI commands."""

from __future__ import annotations

import json
import hashlib
import datetime as dt
import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from . import __version__
from .brand import machine_home, validate_managed_home_path
from .commit_hook_state import command_from_state
from .codex_legacy_recovery import assess_legacy_registration
from .codex_hooks import tenetora_hook_sets, inspect_codex_plugin_hooks, list_codex_hooks
from .finding_acknowledgements import apply_acknowledgements, attach_finding_identity
from .interpreter_check import interpreter_meets_minimum
from .path_security import harness_missing_message, is_redirected_path
from .platform_contracts import load_platform_contracts, platform_contract, supported_platforms


SKILL_NAME = "tenetora"
PLUGIN_REGISTRATION_NAME = "tenetora"
LEGACY_PLUGIN_REGISTRATION_NAME = "agent-harness"
LIFECYCLE_SKILLS = [
    "tenetora",
    "tenetora-init",
    "tenetora-update",
    "tenetora-audit",
    "tenetora-loop",
    "tenetora-prompt-guard",
    "tenetora-align",
    "tenetora-decision-interview",
]
COMMIT_HOOK_STATE_REL = ".tenetora/state/commit-hooks.json"
COMMIT_HOOK_NAMES = ("pre-commit", "commit-msg")
COMMIT_HOOK_MARKERS = ("# Tenetora managed hook", "# Agent Harness managed hook")
PLATFORM_CONTRACTS = load_platform_contracts()
ALL_TOOLS = supported_platforms()
SUPPORTED_TOOLS = set(ALL_TOOLS)
ZCODE_MARKETPLACE_ID = "tenetora-local"
ZCODE_PLUGIN_ID = f"{PLUGIN_REGISTRATION_NAME}@{ZCODE_MARKETPLACE_ID}"
LEGACY_ZCODE_MARKETPLACE_ID = "agent-harness-local"
LEGACY_ZCODE_PLUGIN_ID = f"{LEGACY_PLUGIN_REGISTRATION_NAME}@{LEGACY_ZCODE_MARKETPLACE_ID}"
NATIVE_PLUGIN_TARGETS = {
    tool for tool, contract in PLATFORM_CONTRACTS.items() if contract["installer"] == "native-marketplace"
}
NATIVE_MARKETPLACE_ID = "tenetora-local"
NATIVE_PLUGIN_ID = f"{PLUGIN_REGISTRATION_NAME}@{NATIVE_MARKETPLACE_ID}"
LEGACY_NATIVE_MARKETPLACE_ID = "agent-harness-local"
LEGACY_NATIVE_PLUGIN_ID = f"{LEGACY_PLUGIN_REGISTRATION_NAME}@{LEGACY_NATIVE_MARKETPLACE_ID}"
RUNTIME_ADAPTER_TARGETS = {
    tool for tool, contract in PLATFORM_CONTRACTS.items() if contract["installer"] == "runtime-adapter"
}
CURSOR_HOOK_MODES = {"session-start", "pre-tool-commit", "stop"}
OPENCODE_RUNTIME_MARKER = "tenetora-runtime-adapter"
LEGACY_OPENCODE_RUNTIME_MARKER = "agent-harness-runtime-adapter"
PI_RUNTIME_MARKER = "tenetora-pi-runtime-adapter"
CURSOR_RUNTIME_ROOT_PATTERNS = (
    re.compile(r'set\s+"TENETORA_PLUGIN_ROOT=([^"]+)"', re.IGNORECASE),
    re.compile(r"TENETORA_PLUGIN_ROOT=(?:'([^']+)'|\"([^\"]+)\"|([^\s&]+))"),
    re.compile(r'set\s+"AGENT_HARNESS_PLUGIN_ROOT=([^"]+)"', re.IGNORECASE),
    re.compile(r"AGENT_HARNESS_PLUGIN_ROOT=(?:'([^']+)'|\"([^\"]+)\"|([^\s&]+))"),
)
OPENCODE_RUNTIME_ROOT_PATTERN = re.compile(
    r"TENETORA_ROOT\s*=\s*(\"(?:\\.|[^\"\\])*\")"
)
PI_RUNTIME_ROOT_PATTERN = re.compile(
    r"TENETORA_ROOT\s*=\s*(\"(?:\\.|[^\"\\])*\")"
)
PI_RUNTIME_VERSION_PATTERN = re.compile(
    r"TENETORA_VERSION\s*=\s*(\"(?:\\.|[^\"\\])*\")"
)
LEGACY_OPENCODE_RUNTIME_ROOT_PATTERN = re.compile(
    r"AGENT_HARNESS_ROOT\s*=\s*(\"(?:\\.|[^\"\\])*\")"
)
HOOK_RUNTIME_PREFIX = "tenetora-hook-runtime "
OBSERVATION_RECENT_SECONDS = 24 * 60 * 60


def restricted_hook_environment() -> dict[str, str]:
    preserved = (
        "HOME",
        "USERPROFILE",
        "TENETORA_HOME",
        "TENETORA_PYTHON",
        "TENETORA_RUNTIME_PYTHON_FILE",
        "SystemRoot",
        "WINDIR",
        "ComSpec",
    )
    env = {name: os.environ[name] for name in preserved if os.environ.get(name)}
    env["PATH"] = ""
    return env


def hook_runner_state(plugin_root: Path) -> str:
    unix_runner = plugin_root / "hooks" / "run-hook"
    windows_runner = plugin_root / "hooks" / "run-hook.cmd"
    if os.name == "nt":
        # Windows installs ship only run-hook.cmd; requiring the POSIX runner
        # first reports every Windows-only deployment as missing-runner.
        if not windows_runner.is_file():
            return "missing-runner"
    else:
        if not unix_runner.is_file():
            return "missing-runner"
        if not os.access(unix_runner, os.X_OK):
            return "runner-not-executable"
        if not windows_runner.is_file():
            return "missing-windows-runner"

    if os.name == "nt":
        command_processor = os.environ.get("ComSpec") or str(
            Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "cmd.exe"
        )
        # Python joins a list with list2cmdline, whose C-runtime quote escaping
        # cmd.exe does not understand: the runner arrives as a literal \"...\"
        # token and fails with "not recognized" (misleadingly mapped to
        # missing-runner below). Pass the full command line as a string instead
        # so CreateProcess forwards it to cmd.exe unescaped.
        command = f'"{command_processor}" /d /s /c ""{windows_runner}" --check"'
    else:
        command = [str(unix_runner), "--check"]
    try:
        result = subprocess.run(
            command,
            cwd=plugin_root,
            env=restricted_hook_environment(),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "runner-check-failed"
    if result.returncode != 0 or not result.stdout.startswith(HOOK_RUNTIME_PREFIX):
        return "runner-check-failed"
    return "present"


def discover_source_paths() -> tuple[Path, Path]:
    for candidate in Path(__file__).resolve().parents:
        packaged_skill = candidate / "skills" / SKILL_NAME
        if (packaged_skill / "SKILL.md").is_file():
            return candidate, packaged_skill
    for candidate in Path(__file__).resolve().parents:
        if candidate.name == SKILL_NAME and (candidate / "SKILL.md").is_file():
            return candidate, candidate
    fallback = Path(__file__).resolve().parents[2]
    return fallback, fallback / "skills" / SKILL_NAME


REPO_ROOT, SKILL_SOURCE = discover_source_paths()


def local_environment_snapshot(project_root: Path) -> dict[str, object]:
    """Report registered local-environment paths without reading their contents."""

    registry_script = SKILL_SOURCE / "scripts" / "local_env.py"
    if not registry_script.is_file():
        return {"status": "unavailable", "registered_paths": [], "missing_paths": []}
    try:
        spec = importlib.util.spec_from_file_location("tenetora_local_env_status", registry_script)
        if spec is None or spec.loader is None:
            raise RuntimeError("unable to load local environment registry")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        paths = [str(item) for item in module.read_local_env_paths(project_root)]
    except Exception as exc:
        return {
            "status": "invalid",
            "registered_paths": [],
            "missing_paths": [],
            "error": type(exc).__name__,
        }
    missing = [path for path in paths if not (project_root / Path(path)).exists()]
    return {
        "status": "missing" if missing else "ok",
        "registered_paths": paths,
        "missing_paths": missing,
    }


def repository_units_snapshot(project_root: Path) -> dict[str, object]:
    script = SKILL_SOURCE / "scripts" / "repository_units.py"
    if not script.is_file():
        return {
            "version": 1,
            "status": "unavailable",
            "staged_status": "unknown",
            "units": [],
            "problems": ["repository-unit-checker-missing"],
        }
    try:
        spec = importlib.util.spec_from_file_location("tenetora_repository_units", script)
        if spec is None or spec.loader is None:
            raise RuntimeError("unable to load repository-unit checker")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return dict(module.inspect_repository_units(project_root, project_root))
    except Exception as exc:
        return {
            "version": 1,
            "status": "unavailable",
            "staged_status": "unknown",
            "units": [],
            "problems": [f"repository-unit-checker-error: {type(exc).__name__}"],
        }


@dataclass
class SkillInstall:
    name: str
    path: str
    state: str
    mode: str
    source: str | None = None
    version: str | None = None


@dataclass
class ToolInstall:
    tool: str
    scope: str
    install_root: str
    path: str
    state: str
    mode: str
    command: str | None = None
    command_available: bool = False
    source: str | None = None
    skills: list[SkillInstall] | None = None
    plugin_root: str | None = None
    plugin_state: str | None = None
    hooks_state: str | None = None
    activation_state: str | None = None
    registration_state: str | None = None
    runtime_state: str | None = None
    hook_mode: str | None = None
    degraded_reason: str | None = None
    delegation_capabilities: dict[str, object] | None = None
    delegation_status: dict[str, object] | None = None
    effective_sources: dict[str, object] | None = None
    runtime_observation: dict[str, object] | None = None
    version_alignment: dict[str, object] | None = None
    capability_contract: dict[str, object] | None = None


@dataclass
class EnvironmentStatus:
    repo: dict[str, str]
    project: dict[str, object]
    tools: list[ToolInstall]
    issues: list[str]
    recommendations: list[str]
    findings: list[dict[str, object]]
    health: dict[str, object]
    activity: dict[str, object]
    acknowledgement_action: dict[str, object] | None = None
    acknowledgement_state: dict[str, object] | None = None


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def zcode_detected(home: Path) -> bool:
    return bool(
        command_exists("zcode")
        or os.environ.get("ZCODE_HOME")
        or (home / ".zcode").exists()
        or (home / "Library" / "Application Support" / "ZCode").exists()
    )


def parse_tools(raw: str) -> list[str]:
    requested = [part.strip() for part in raw.split(",") if part.strip()]
    if not requested:
        raise ValueError("No tool selected")
    if "all" in requested:
        return ALL_TOOLS
    if "auto" not in requested:
        unknown = sorted(set(requested) - SUPPORTED_TOOLS)
        if unknown:
            raise ValueError(f"Unsupported tool(s): {', '.join(unknown)}")
        return list(dict.fromkeys(requested))

    tools = ["agents"]
    home = Path.home()
    if command_exists("codex") or (home / ".codex").exists():
        tools.append("codex")
    claude_home = Path(
        os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_HOME") or home / ".claude"
    ).expanduser()
    if command_exists("claude") or command_exists("claude-code") or claude_home.exists():
        tools.append("claude")
    if command_exists("cursor") or (home / ".cursor").exists() or os.environ.get("CURSOR_HOME"):
        tools.append("cursor")
    opencode_home = Path(
        os.environ.get("OPENCODE_HOME")
        or Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "opencode"
    ).expanduser()
    if command_exists("opencode") or opencode_home.exists():
        tools.append("opencode")
    pi_home = home / ".pi" / "agent"
    if command_exists("pi") or pi_home.exists() or (home / ".pi").exists():
        tools.append("pi")
    if zcode_detected(home):
        tools.append("zcode")
    return tools


def selected_scopes(raw: str) -> list[str]:
    if raw == "both":
        return ["global", "project"]
    return [raw]


def tool_base(tool: str, scope: str, project_root: Path) -> Path:
    home = Path.home()
    if scope == "project":
        return project_root / f".{tool}" / "skills"

    if tool == "agents":
        return Path(os.environ.get("AGENT_SKILLS_HOME", home / ".agents" / "skills")).expanduser()
    if tool == "codex":
        return Path(os.environ.get("CODEX_HOME", home / ".codex")).expanduser() / "skills"
    if tool == "claude":
        claude_home = os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_HOME")
        return Path(claude_home or home / ".claude").expanduser() / "skills"
    if tool == "cursor":
        return Path(os.environ.get("CURSOR_HOME", home / ".cursor")).expanduser() / "skills"
    if tool == "opencode":
        default_home = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "opencode"
        return Path(os.environ.get("OPENCODE_HOME", default_home)).expanduser() / "skills"
    if tool == "pi":
        return home / ".pi" / "agent" / "skills"
    if tool == "zcode" and scope == "global":
        return Path(os.environ.get("ZCODE_HOME", home / ".zcode")).expanduser() / "skills"
    raise ValueError(f"Unsupported tool: {tool}")


def zcode_home(scope: str, project_root: Path) -> Path:
    if scope == "project":
        return project_root / ".zcode"
    return Path(os.environ.get("ZCODE_HOME", Path.home() / ".zcode")).expanduser()


def zcode_plugin_base(project_root: Path) -> Path:
    plugin_home = Path(
        os.environ.get(
            "ZCODE_PLUGIN_HOME",
            str(zcode_home("global", project_root) / "cli" / "plugins" / "cache"),
        )
    ).expanduser()
    return plugin_home / ZCODE_MARKETPLACE_ID / PLUGIN_REGISTRATION_NAME


def legacy_zcode_plugin_base(project_root: Path) -> Path:
    plugin_home = Path(
        os.environ.get(
            "ZCODE_PLUGIN_HOME",
            str(zcode_home("global", project_root) / "cli" / "plugins" / "cache"),
        )
    ).expanduser()
    return plugin_home / LEGACY_ZCODE_MARKETPLACE_ID / LEGACY_PLUGIN_REGISTRATION_NAME


def zcode_plugin_path(project_root: Path, version: str = __version__) -> Path:
    return zcode_plugin_base(project_root) / version


def zcode_registry_path(project_root: Path) -> Path:
    return zcode_home("global", project_root) / "cli" / "plugins" / "installed_plugins.json"


def tool_command(tool: str) -> str | None:
    if tool == "agents":
        return None
    if tool == "claude":
        if command_exists("claude"):
            return "claude"
        if command_exists("claude-code"):
            return "claude-code"
        return None
    return tool


def native_plugin_environment(tool: str, project_root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    if tool == "claude":
        native_home = tool_base("claude", "global", project_root).parent
        environment["CLAUDE_CONFIG_DIR"] = str(
            validate_managed_home_path(native_home, label="CLAUDE_CONFIG_DIR")
        )
    elif tool == "codex":
        native_home = tool_base("codex", "global", project_root).parent
        environment["CODEX_HOME"] = str(validate_managed_home_path(native_home, label="CODEX_HOME"))
    return environment


def native_plugin_listing(tool: str, project_root: Path) -> tuple[list[dict[str, object]] | None, str]:
    if os.environ.get("TENETORA_NATIVE_PLUGINS", "1") == "0":
        return None, "cli-disabled"
    command = tool_command(tool)
    if command is None or not command_exists(command):
        return None, "cli-unavailable"
    try:
        result = subprocess.run(
            [command, "plugin", "list", "--json"],
            cwd=project_root,
            env=native_plugin_environment(tool, project_root),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None, "cli-error"
    if result.returncode != 0:
        diagnostic = f"{result.stdout}\n{result.stderr}".lower()
        unsupported_markers = (
            "unknown command",
            "unrecognized subcommand",
            "unexpected argument",
            "usage:",
        )
        if any(marker in diagnostic for marker in unsupported_markers):
            return None, "cli-unsupported"
        return None, "state-unreadable"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, "invalid"
    entries = payload if tool == "claude" else payload.get("installed") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return None, "invalid"
    return [entry for entry in entries if isinstance(entry, dict)], "present"


def native_plugin_entry_for_identity(
    tool: str,
    project_root: Path,
    plugin_id: str,
    name: str,
    marketplace_id: str,
) -> tuple[dict[str, object] | None, str]:
    entries, state = native_plugin_listing(tool, project_root)
    if entries is None:
        return None, state
    for entry in entries:
        entry_plugin_id = entry.get("id") or entry.get("pluginId")
        marketplace = entry.get("marketplaceName") or entry.get("marketplace")
        if tool == "claude" and entry.get("scope") not in {None, "user"}:
            continue
        if plugin_id == entry_plugin_id or (
            entry.get("name") == name and marketplace == marketplace_id
        ):
            return entry, "present"
    return None, "missing"


def native_plugin_entry(tool: str, project_root: Path) -> tuple[dict[str, object] | None, str]:
    return native_plugin_entry_for_identity(
        tool,
        project_root,
        NATIVE_PLUGIN_ID,
        PLUGIN_REGISTRATION_NAME,
        NATIVE_MARKETPLACE_ID,
    )


def legacy_native_plugin_entry(tool: str, project_root: Path) -> tuple[dict[str, object] | None, str]:
    return native_plugin_entry_for_identity(
        tool,
        project_root,
        LEGACY_NATIVE_PLUGIN_ID,
        LEGACY_PLUGIN_REGISTRATION_NAME,
        LEGACY_NATIVE_MARKETPLACE_ID,
    )


def native_plugin_install_path(
    tool: str,
    entry: dict[str, object],
    project_root: Path,
    *,
    marketplace_id: str = NATIVE_MARKETPLACE_ID,
    registration_name: str = PLUGIN_REGISTRATION_NAME,
) -> Path | None:
    raw = entry.get("installPath") or entry.get("installedPath")
    if isinstance(raw, str) and raw:
        return Path(raw).expanduser()
    version = entry.get("version")
    if tool == "codex" and isinstance(version, str) and version:
        return (
            tool_base("codex", "global", project_root).parent
            / "plugins"
            / "cache"
            / marketplace_id
            / registration_name
            / version
        )
    return None


def native_command_hooks_state(tool: str, plugin_root: Path) -> str:
    hooks_name = "hooks-claude.json" if tool == "claude" else "hooks.json"
    root_variable = "${CLAUDE_PLUGIN_ROOT}" if tool == "claude" else "${PLUGIN_ROOT}"
    manifest_dir = ".claude-plugin" if tool == "claude" else ".codex-plugin"
    manifest_path = plugin_root / manifest_dir / "plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "invalid-manifest"
    if not isinstance(manifest, dict):
        return "invalid-manifest"
    if tool == "claude" and manifest.get("hooks") != "./hooks/hooks-claude.json":
        return "invalid-manifest"
    if tool == "codex" and "hooks" in manifest:
        return "invalid-manifest"
    hooks_path = plugin_root / "hooks" / hooks_name
    if not hooks_path.is_file():
        return "missing"
    try:
        payload = json.loads(hooks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "invalid"
    hooks = payload.get("hooks") if isinstance(payload, dict) else None
    if not isinstance(hooks, dict) or not hooks:
        return "invalid"
    for groups in hooks.values():
        if not isinstance(groups, list):
            return "invalid"
        for group in groups:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(entries, list):
                return "invalid"
            for entry in entries:
                command = entry.get("command") if isinstance(entry, dict) else None
                if not isinstance(entry, dict) or entry.get("type") != "command":
                    return "invalid"
                expected_runner = f"{root_variable}/hooks/run-hook"
                primary_valid = isinstance(command, str) and command_uses_expected_runner(command, expected_runner)
                if tool == "claude" and os.name == "nt" and isinstance(command, str):
                    primary_valid = primary_valid or command_uses_expected_runner(
                        command,
                        f"{root_variable}/hooks/run-hook.cmd",
                        windows=True,
                    )
                if not primary_valid:
                    return "invalid"
                if tool == "codex":
                    windows = entry.get("commandWindows")
                    if not isinstance(windows, str) or not command_uses_expected_runner(
                        windows,
                        "%PLUGIN_ROOT%/hooks/run-hook.cmd",
                        windows=True,
                    ):
                        return "invalid"
    return hook_runner_state(plugin_root)


def command_uses_expected_runner(
    command: str,
    expected_runner: str,
    *,
    windows: bool = False,
) -> bool:
    """Require the canonical runner as the command executable, not incidental text."""
    if "\n" in command or "\r" in command:
        return False
    try:
        tokens = shlex.split(command, posix=not windows)
    except ValueError:
        return False
    if not tokens:
        return False
    executable = tokens[0].strip("\"'").replace("\\", "/")
    if executable != expected_runner.replace("\\", "/"):
        return False
    shell_control_markers = ("#", ";", "|", "&", "<", ">", "`", "$(")
    return not any(
        marker in token
        for token in tokens[1:]
        for marker in shell_control_markers
    )


def native_plugin_info(tool: str, project_root: Path) -> dict[str, str | None]:
    entry, registration = native_plugin_entry(tool, project_root)
    legacy_entry, legacy_registration = legacy_native_plugin_entry(tool, project_root)
    if legacy_entry is not None:
        legacy_path = native_plugin_install_path(
            tool,
            legacy_entry,
            project_root,
            marketplace_id=LEGACY_NATIVE_MARKETPLACE_ID,
            registration_name=LEGACY_PLUGIN_REGISTRATION_NAME,
        )
        return {
            "path": str(legacy_path) if legacy_path else None,
            "state": "conflict",
            "version": str(legacy_entry.get("version")) if legacy_entry.get("version") else None,
            "hooks": "legacy-registration",
            "activation": "enabled" if bool(legacy_entry.get("enabled", True)) else "disabled",
            "registration": "legacy-present" if legacy_registration == "present" else legacy_registration,
            "runtime": "legacy-active",
        }
    if entry is None:
        return {
            "path": None,
            "state": "missing",
            "version": None,
            "hooks": None,
            "activation": "unknown",
            "registration": registration,
            "runtime": "skills-only" if registration in {"cli-unavailable", "cli-unsupported", "cli-disabled"} else "inactive",
        }
    path = native_plugin_install_path(tool, entry, project_root)
    version = str(entry.get("version")) if entry.get("version") else None
    enabled = bool(entry.get("enabled", True))
    errors = entry.get("errors")
    if path is None or not path.exists():
        state = "missing"
        hooks = "missing"
    elif isinstance(errors, list) and errors:
        state = "conflict"
        hooks = "invalid"
    else:
        state = version_state(version) or "conflict"
        hooks = native_command_hooks_state(tool, path)
        if hooks != "present":
            state = "missing"
    activation = "enabled" if enabled else "disabled"
    if state != "ok" or activation != "enabled" or hooks != "present":
        runtime = "inactive"
    elif tool == "codex":
        command = tool_command("codex")
        trust = (
            inspect_codex_plugin_hooks(
                command,
                project_root,
                native_plugin_environment("codex", project_root),
                NATIVE_PLUGIN_ID,
                client_version=__version__,
            )
            if command is not None
            else {"state": "unavailable"}
        )
        runtime = "active" if trust.get("state") == "active" else "needs-trust-review"
    else:
        runtime = "active"
    return {
        "path": str(path) if path else None,
        "state": state,
        "version": version,
        "hooks": hooks,
        "activation": activation,
        "registration": registration,
        "runtime": runtime,
    }


def codex_tenetora_hook_sets(inventory: dict[str, object]) -> dict[str, list[dict[str, object]]]:
    """Classify canonical runtime hooks separately from legacy migration input."""
    canonical = tenetora_hook_sets(inventory, NATIVE_PLUGIN_ID)
    legacy = tenetora_hook_sets(inventory, LEGACY_NATIVE_PLUGIN_ID)
    return {
        "native": canonical["native"],
        "legacy_native": legacy["native"],
        "project": canonical["project"],
    }


def codex_project_config_has_managed_hooks(project_root: Path) -> bool:
    path = project_root / ".codex" / "hooks.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    hooks = payload.get("hooks") if isinstance(payload, dict) else None
    if not isinstance(hooks, dict):
        return False
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                command = entry.get("command")
                if isinstance(command, str) and any(
                    marker in command
                    for marker in (
                        ".tenetora/runtime/hooks/run-hook",
                    )
                ):
                    return True
    return False


def codex_native_inventory_runtime_state(
    hooks: list[dict[str, object]],
    plugin_path: str | None,
) -> str:
    roots: set[Path] = set()
    for hook in hooks:
        source_path = hook.get("sourcePath")
        if isinstance(source_path, str) and source_path:
            path = Path(source_path).expanduser()
            if path.parent.name == "hooks":
                roots.add(path.parent.parent)
                continue
        if plugin_path:
            roots.add(Path(plugin_path).expanduser())
            continue
        return "unknown-source"
    if not roots:
        return "missing"
    states = {hook_runner_state(root) for root in roots}
    return "present" if states == {"present"} else sorted(states)[0]


def codex_hook_runtime_info(
    project_root: Path,
    plugin: dict[str, str | None],
) -> dict[str, str | None]:
    degraded_reason = (
        "codex-marketplace-unreadable"
        if plugin.get("registration") == "state-unreadable"
        else None
    )
    if (
        plugin.get("registration") == "present"
        and plugin.get("hooks") not in {None, "present"}
    ):
        return {
            "hook_mode": "native-plugin",
            "hooks_state": "native-hook-blocks-fallback",
            "runtime_state": "inactive",
            "activation_state": plugin.get("activation"),
            "degraded_reason": "canonical-plugin-payload-invalid",
        }
    command = tool_command("codex")
    inventory = (
        list_codex_hooks(
            command,
            project_root,
            native_plugin_environment("codex", project_root),
            client_version=__version__,
        )
        if command is not None
        else {"state": "unavailable", "hooks": []}
    )
    hook_sets = codex_tenetora_hook_sets(inventory)
    native_hooks = [hook for hook in hook_sets["native"] if hook.get("enabled", True)]
    legacy_native_hooks = [
        hook for hook in hook_sets["legacy_native"] if hook.get("enabled", True)
    ]
    project_hooks = [hook for hook in hook_sets["project"] if hook.get("enabled", True)]
    inventory_available = (
        inventory.get("state") == "available"
        and not inventory.get("errors")
    )
    if not inventory_available:
        project_configured = codex_project_config_has_managed_hooks(project_root)
        if project_configured:
            launcher = hook_runner_state(machine_home() / "runtime")
            if launcher == "present":
                return {
                    "hook_mode": "project-fallback",
                    "hooks_state": "pending-project-trust",
                    "runtime_state": "needs-trust-review",
                    "activation_state": "unknown",
                    "degraded_reason": degraded_reason or "codex-hook-inventory-unavailable",
                }
            return {
                "hook_mode": "project-fallback",
                "hooks_state": "project-fallback-runtime-unhealthy",
                "runtime_state": "inactive",
                "activation_state": "unknown",
                "degraded_reason": degraded_reason or "codex-hook-inventory-unavailable",
            }
        return {
            "hook_mode": "unknown",
            "hooks_state": "hook-inventory-unavailable",
            "runtime_state": "inactive",
            "activation_state": "unknown",
            "degraded_reason": degraded_reason or "codex-hook-inventory-unavailable",
        }
    if (native_hooks and project_hooks) or (
        legacy_native_hooks and (native_hooks or project_hooks)
    ):
        conflict_reason = (
            "codex-native-and-project-hooks"
            if native_hooks and project_hooks and not legacy_native_hooks
            else "codex-canonical-and-legacy-hooks"
        )
        return {
            "hook_mode": "conflict",
            "hooks_state": "duplicate-hook-runtime",
            "runtime_state": "inactive",
            "activation_state": "enabled",
            "degraded_reason": conflict_reason,
        }
    if legacy_native_hooks:
        return {
            "hook_mode": "native-plugin",
            "hooks_state": "legacy-native-hook-active",
            "runtime_state": "inactive",
            "activation_state": "enabled",
            "degraded_reason": degraded_reason or "legacy-codex-hook-source",
        }
    if native_hooks:
        launcher = codex_native_inventory_runtime_state(native_hooks, plugin.get("path"))
        if launcher != "present":
            return {
                "hook_mode": "native-plugin",
                "hooks_state": "native-hook-blocks-fallback",
                "runtime_state": "inactive",
                "activation_state": "enabled",
                "degraded_reason": degraded_reason,
            }
        trusted = all(
            str(hook.get("trustStatus", "")).lower() in {"trusted", "managed"}
            for hook in native_hooks
        )
        if not trusted:
            return {
                "hook_mode": "native-plugin",
                "hooks_state": "pending-native-trust",
                "runtime_state": "needs-trust-review",
                "activation_state": "enabled",
                "degraded_reason": degraded_reason,
            }
        return {
            "hook_mode": "native-plugin",
            "hooks_state": (
                "active-native-degraded-management"
                if degraded_reason
                else "active-native"
            ),
            "runtime_state": "active",
            "activation_state": "enabled",
            "degraded_reason": degraded_reason,
        }
    if project_hooks:
        launcher = hook_runner_state(machine_home() / "runtime")
        if launcher != "present":
            return {
                "hook_mode": "project-fallback",
                "hooks_state": "project-fallback-runtime-unhealthy",
                "runtime_state": "inactive",
                "activation_state": "enabled",
                "degraded_reason": degraded_reason,
            }
        trusted = all(
            str(hook.get("trustStatus", "")).lower() in {"trusted", "managed"}
            for hook in project_hooks
        )
        return {
            "hook_mode": "project-fallback",
            "hooks_state": "active-project-fallback" if trusted else "pending-project-trust",
            "runtime_state": "active" if trusted else "needs-trust-review",
            "activation_state": "enabled",
            "degraded_reason": degraded_reason,
        }
    if codex_project_config_has_managed_hooks(project_root):
        launcher = hook_runner_state(machine_home() / "runtime")
        return {
            "hook_mode": "project-fallback",
            "hooks_state": "pending-project-trust" if launcher == "present" else "project-fallback-runtime-unhealthy",
            "runtime_state": "needs-trust-review" if launcher == "present" else "inactive",
            "activation_state": "unknown",
            "degraded_reason": degraded_reason,
        }
    return {
        "hook_mode": "skills-only",
        "hooks_state": "inactive",
        "runtime_state": "skills-only" if plugin.get("runtime") == "skills-only" else "inactive",
        "activation_state": plugin.get("activation"),
        "degraded_reason": degraded_reason,
    }


def tool_install(
    tool: str,
    scope: str,
    install_root: Path,
    path: Path,
    state: str,
    mode: str,
    source: str | None = None,
    skills: list[SkillInstall] | None = None,
    plugin_root: Path | None = None,
    plugin_state: str | None = None,
    hooks_state: str | None = None,
    activation_state: str | None = None,
    registration_state: str | None = None,
    runtime_state: str | None = None,
    hook_mode: str | None = None,
    degraded_reason: str | None = None,
) -> ToolInstall:
    command = tool_command(tool)
    return ToolInstall(
        tool=tool,
        scope=scope,
        install_root=str(install_root),
        path=str(path),
        state=state,
        mode=mode,
        command=command,
        command_available=command_exists(command) if command else False,
        source=source,
        skills=skills,
        plugin_root=str(plugin_root) if plugin_root else None,
        plugin_state=plugin_state,
        hooks_state=hooks_state,
        activation_state=activation_state,
        registration_state=registration_state,
        runtime_state=runtime_state,
        hook_mode=hook_mode,
        degraded_reason=degraded_reason,
        delegation_capabilities=tool_delegation_capabilities(tool, hooks_state, runtime_state),
        delegation_status=tool_delegation_status(tool, hooks_state, runtime_state),
    )


def tool_delegation_capabilities(
    tool: str,
    hooks_state: str | None,
    runtime_state: str | None,
) -> dict[str, object]:
    delegation = platform_contract(tool)["delegation"]
    dispatch = str(delegation["dispatch_label"])
    builtin_agents = list(delegation["builtin_agents"])
    builtin_role_candidates = dict(delegation["builtin_role_candidates"])
    builtin_role_injection = {
        "codex": "verified-official",
        "claude": "verified-official",
        "zcode": "observed-real-pilot",
        "opencode": "host-dependent",
        "cursor": "unverified",
        "agents": "host-dependent",
        "pi": "host-dependent",
    }[tool]
    isolation_contract = str(delegation["isolation_contract"])
    observable = tool in {"codex", "claude", "zcode", "pi"} and hooks_state == "present"
    if tool == "cursor":
        review_loop = "fallback-only"
        reminder = "rules-and-shell-hook"
    elif tool == "opencode":
        review_loop = "host-dependent"
        reminder = "plugin-context"
    elif tool == "pi":
        review_loop = "host-dependent"
        reminder = "extension-context"
    elif tool == "agents":
        review_loop = "host-dependent"
        reminder = "rules-only"
    else:
        review_loop = "instruction-driven"
        reminder = "rules-and-runtime-hook" if hooks_state == "present" else "rules-only"
    return {
        "dispatch": dispatch,
        "resolution_order": ["dedicated", "builtin-role-injection", "main-self-review"],
        "builtin_role_injection": builtin_role_injection,
        "builtin_agents": builtin_agents,
        "builtin_role_candidates": builtin_role_candidates,
        "conditional_builtin_roles": ["security-auditor"] if builtin_agents else [],
        "isolation_contract": isolation_contract,
        "automatic_trigger": "model-instruction-driven" if tool != "cursor" else "user-or-model-recommendation",
        "lifecycle_observation": observable,
        "runtime_reminder": reminder,
        "bounded_review_loop": review_loop,
        "fallback": "main-agent-self-review-marked-unreviewed",
        "runtime_ready": runtime_state in {"active", "active-partial"},
        "hooks_spawn_agents": False,
    }


def tool_delegation_status(
    tool: str,
    hooks_state: str | None,
    runtime_state: str | None,
    observation: dict[str, object] | None = None,
) -> dict[str, object]:
    contract = platform_contract(tool)
    delegation = contract["delegation"]
    events = contract.get("events", {})
    observation_support = (
        "supported"
        if isinstance(events, dict) and "SubagentStart" in events and "SubagentStop" in events
        else "unsupported"
    )
    if hooks_state == "duplicate-hook-runtime":
        observation_runtime = "conflict"
    elif runtime_state in {"active", "active-partial"}:
        observation_runtime = "active"
    elif hooks_state == "present":
        observation_runtime = "configured"
    elif runtime_state == "skills-only":
        observation_runtime = "skills-only"
    else:
        observation_runtime = "inactive"
    observed = observation or {}
    dispatch_state = str(observed.get("dispatch_observation_state") or "not-observed")
    session_state = "observed" if dispatch_state == "recent" else "unknown"
    evidence = {
        "recent": "observed",
        "stale": "stale",
        "invalid": "invalid",
        "observed-source-mismatch": "mismatch",
        "observed-conflict": "mismatch",
    }.get(dispatch_state, "not-observed")
    return {
        "platform_support": {
            "state": delegation["platform_support"],
            "basis": delegation["support_basis"],
            "modes": list(delegation["modes"]),
        },
        "session_dispatch_availability": {
            "state": session_state,
            "basis": "subagent-lifecycle-observed" if session_state == "observed" else "not-probed",
            "scope": "host-session",
        },
        "lifecycle_observation": {
            "support": observation_support,
            "runtime": observation_runtime,
            "evidence": evidence,
            "basis": observed.get("basis", "unknown"),
            "event": observed.get("dispatch_event"),
            "observed_at": observed.get("dispatch_observed_at"),
            "dispatch_observed_at": observed.get("dispatch_observed_at"),
        },
        "hooks_spawn_agents": False,
        "does_not_imply": "inactive lifecycle observation does not mean host session dispatch is unavailable",
    }


def expected_skill_source(skill_name: str) -> Path | None:
    if skill_name == SKILL_NAME:
        return SKILL_SOURCE.resolve()
    sibling = SKILL_SOURCE.parent / skill_name
    if sibling.is_dir():
        return sibling.resolve()
    return None


def read_skill_version(path: Path) -> str | None:
    for candidate in [
        path / "VERSION",
        path / "hooks" / "VERSION",
        path / "skills" / SKILL_NAME / "VERSION",
        path.parent / SKILL_NAME / "VERSION",
    ]:
        if candidate.is_file():
            try:
                version = candidate.read_text(encoding="utf-8", errors="ignore").strip()
            except OSError:
                continue
            version = normalized_version(version)
            if version:
                return version

    init_file = path / "cli" / "tenetora" / "__init__.py"
    if init_file.is_file():
        try:
            for line in init_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                match = re.fullmatch(r"__version__\s*=\s*(['\"])([^'\"]+)\1", line.strip())
                if match:
                    return normalized_version(match.group(2))
        except OSError:
            return None

    pyproject = path / "pyproject.toml"
    if pyproject.is_file():
        try:
            for line in pyproject.read_text(encoding="utf-8", errors="ignore").splitlines():
                match = re.fullmatch(r"version\s*=\s*(['\"])([^'\"]+)\1", line.strip())
                if match:
                    return normalized_version(match.group(2))
        except OSError:
            return None
    return None


def normalized_version(raw: str | None) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if re.fullmatch(r"\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?", value):
        return value
    return None


def version_state(version: str | None) -> str | None:
    if version is None:
        return None
    if version != __version__:
        return "stale"
    return "ok"


def is_valid_lifecycle_skill_source(path: Path, skill_name: str) -> bool:
    if not path.is_dir() or not (path / "SKILL.md").is_file():
        return False
    try:
        skill_text = (path / "SKILL.md").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    if f"name: {skill_name}" not in skill_text:
        return False
    package_root = path.parent
    return all((package_root / name / "SKILL.md").is_file() for name in LIFECYCLE_SKILLS)


def skill_marker_state(path: Path, skill_name: str) -> str | None:
    marker = path / "SKILL.md"
    if not marker.is_file():
        return "missing"
    try:
        skill_text = marker.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return "conflict"
    if f"name: {skill_name}" not in skill_text:
        return "conflict"
    return None


def legacy_skill_fallback_present(tool: str, scope: str, project_root: Path) -> bool:
    base = tool_base(tool, scope, project_root)
    for skill_name in (
        "agent-harness",
        "agent-harness-init",
        "agent-harness-update",
        "agent-harness-audit",
        "agent-harness-loop",
        "agent-harness-prompt-guard",
        "agent-harness-align",
        "agent-harness-decision-interview",
    ):
        if skill_marker_state(base / skill_name, skill_name) is None:
            return True
    return False


def describe_skill_install(install_root: Path, skill_name: str) -> SkillInstall:
    path = install_root / skill_name
    expected_source = expected_skill_source(skill_name)

    if is_redirected_path(path):
        source = str(path.resolve(strict=False))
        source_path = Path(source)
        installed_version = read_skill_version(source_path)
        detected_state = version_state(installed_version)
        marker_state = skill_marker_state(source_path, skill_name)
        state = marker_state or (
            "ok"
            if expected_source is None or source_path == expected_source or is_valid_lifecycle_skill_source(source_path, skill_name)
            else "conflict"
        )
        if state == "ok" and detected_state == "stale":
            state = "stale"
        return SkillInstall(skill_name, str(path), state, "symlink", source, installed_version)

    if not path.exists():
        return SkillInstall(skill_name, str(path), "missing", "-")

    if path.is_dir():
        installed_version = read_skill_version(path)
        marker_state = skill_marker_state(path, skill_name)
        return SkillInstall(
            skill_name,
            str(path),
            marker_state or version_state(installed_version) or "ok",
            "copy",
            version=installed_version,
        )

    return SkillInstall(skill_name, str(path), "conflict", "file")


def plugin_version(path: Path) -> str | None:
    manifest = path / ".zcode-plugin" / "plugin.json"
    if not manifest.is_file():
        return None
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return str(payload.get("version")) if payload.get("version") else None


def project_legacy_zcode_plugin_path(project_root: Path | str) -> Path:
    return Path(project_root) / ".zcode" / "plugins" / LEGACY_PLUGIN_REGISTRATION_NAME


def project_legacy_zcode_plugin_is_managed(project_root: Path | str) -> bool:
    path = project_legacy_zcode_plugin_path(project_root)
    manifest = path / ".zcode-plugin" / "plugin.json"
    if not manifest.is_file():
        return False
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or payload.get("name") != LEGACY_PLUGIN_REGISTRATION_NAME:
        return False
    markers = (
        path / "hooks" / "agent_harness_hook.py",
        path / "skills" / LEGACY_PLUGIN_REGISTRATION_NAME / "SKILL.md",
        path / "skills" / SKILL_NAME / "SKILL.md",
    )
    return sum(marker.is_file() for marker in markers) >= 2


def zcode_registration(project_root: Path) -> tuple[dict[str, object] | None, str]:
    path = zcode_registry_path(project_root)
    if not path.is_file():
        return None, "missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "invalid"
    plugins = payload.get("plugins") if isinstance(payload, dict) else None
    if not isinstance(plugins, list):
        return None, "invalid"
    for entry in plugins:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") == ZCODE_PLUGIN_ID or (
            entry.get("name") == PLUGIN_REGISTRATION_NAME
            and entry.get("marketplace") == ZCODE_MARKETPLACE_ID
        ):
            return entry, "present"
    return None, "missing"


def legacy_zcode_registration(project_root: Path) -> tuple[dict[str, object] | None, str]:
    path = zcode_registry_path(project_root)
    if not path.is_file():
        return None, "missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "invalid"
    plugins = payload.get("plugins") if isinstance(payload, dict) else None
    if not isinstance(plugins, list):
        return None, "invalid"
    for entry in plugins:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") == LEGACY_ZCODE_PLUGIN_ID or (
            entry.get("name") == LEGACY_PLUGIN_REGISTRATION_NAME
            and entry.get("marketplace") == LEGACY_ZCODE_MARKETPLACE_ID
        ):
            return entry, "present"
    return None, "missing"


def zcode_hooks_state(path: Path) -> str:
    hooks_path = path / "hooks" / "hooks.json"
    if not hooks_path.is_file():
        return "missing"
    try:
        payload = json.loads(hooks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "invalid"
    hooks = payload.get("hooks") if isinstance(payload, dict) else None
    if not isinstance(hooks, dict) or not hooks:
        return "invalid"
    for groups in hooks.values():
        if not isinstance(groups, list):
            return "invalid"
        for group in groups:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(entries, list):
                return "invalid"
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("type") != "process":
                    return "invalid"
                command = entry.get("command")
                if not interpreter_meets_minimum(command):
                    return "incompatible-interpreter"
                args = entry.get("args")
                if not isinstance(args, list) or not any(
                    isinstance(arg, str)
                    and arg.replace("\\", "/") == "${ZCODE_PLUGIN_ROOT}/hooks/tenetora_hook.py"
                    for arg in args
                ):
                    return "invalid"
                if not isinstance(entry.get("timeoutMs"), int):
                    return "invalid"
    return "present"


def zcode_plugin_info(scope: str, project_root: Path) -> dict[str, str | None]:
    registration, registration_state = zcode_registration(project_root)
    legacy_registration, legacy_registration_state = legacy_zcode_registration(project_root)
    if legacy_registration is not None:
        legacy_path = legacy_registration.get("installPath")
        return {
            "path": str(legacy_path) if legacy_path else str(legacy_zcode_plugin_base(project_root)),
            "state": "conflict",
            "version": str(legacy_registration.get("version")) if legacy_registration.get("version") else None,
            "hooks": "legacy-registration",
            "activation": "legacy-enabled",
            "registration": "legacy-present" if legacy_registration_state == "present" else legacy_registration_state,
        }
    registered_path = registration.get("installPath") if isinstance(registration, dict) else None
    path = (
        Path(str(registered_path)).expanduser()
        if isinstance(registered_path, str) and registered_path
        else zcode_plugin_path(project_root)
    )
    if not path.exists() and not is_redirected_path(path):
        return {
            "path": str(path),
            "state": "missing",
            "version": None,
            "hooks": None,
            "activation": "unknown",
            "registration": registration_state,
        }

    manifest = path / ".zcode-plugin" / "plugin.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "path": str(path),
            "state": "conflict",
            "version": None,
            "hooks": "missing",
            "activation": "unknown",
            "registration": registration_state,
        }
    if not isinstance(payload, dict):
        return {
            "path": str(path),
            "state": "conflict",
            "version": None,
            "hooks": "missing",
            "activation": "unknown",
            "registration": registration_state,
        }
    if payload.get("name") != PLUGIN_REGISTRATION_NAME:
        state = "conflict"
    elif registration_state != "present":
        state = "missing"
    else:
        state = version_state(str(payload.get("version"))) or "conflict"
    hooks = zcode_hooks_state(path)
    if hooks in {"invalid", "incompatible-interpreter"} and state == "ok":
        state = "stale"
    elif hooks == "missing" and state == "ok":
        state = "missing"
    return {
        "path": str(path),
        "state": state,
        "version": str(payload.get("version")) if payload.get("version") else None,
        "hooks": hooks,
        "activation": zcode_activation_state(scope, project_root),
        "registration": registration_state,
    }


def zcode_config_candidates(scope: str, project_root: Path) -> list[Path]:
    configured = os.environ.get("ZCODE_CONFIG")
    if configured:
        return [Path(configured).expanduser()]
    if scope == "project":
        return [project_root / ".zcode" / "cli" / "config.json", project_root / ".zcode" / "config.json"]
    home = zcode_home(scope, project_root)
    return [home / "cli" / "config.json", home / "config.json"]


def zcode_plugin_key_matches(raw: object) -> bool:
    key = str(raw)
    return key == PLUGIN_REGISTRATION_NAME or key.startswith(f"{PLUGIN_REGISTRATION_NAME}@")


def zcode_activation_state(scope: str, project_root: Path) -> str:
    saw_config = False
    saw_invalid = False
    matched_values: list[object] = []
    for config_path in zcode_config_candidates(scope, project_root):
        if not config_path.is_file():
            continue
        saw_config = True
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            saw_invalid = True
            continue
        registries = []
        if isinstance(payload, dict):
            registries.append(payload.get("enabledPlugins"))
            plugins = payload.get("plugins")
            if isinstance(plugins, dict):
                if plugins.get("enabled") is False:
                    return "disabled"
                registries.append(plugins.get("enabledPlugins"))
        for registry in registries:
            if not isinstance(registry, dict):
                continue
            matched_values.extend(value for key, value in registry.items() if zcode_plugin_key_matches(key))
    if any(value is True for value in matched_values):
        return "enabled"
    if matched_values:
        return "disabled"
    if saw_invalid:
        return "config-invalid"
    if saw_config:
        return "not-configured"
    return "unknown"


def runtime_package_is_current(root: Path) -> bool:
    version_path = root / "skills" / SKILL_NAME / "VERSION"
    try:
        version = version_path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return (
        version == __version__
        and hook_runner_state(root) == "present"
    )


def cursor_runtime_root(command: str) -> Path | None:
    for pattern in CURSOR_RUNTIME_ROOT_PATTERNS:
        match = pattern.search(command)
        if match:
            raw = next((value for value in match.groups() if value), "")
            if raw:
                return Path(raw).expanduser()
    for runner in (REPO_ROOT / "hooks" / "run-hook", REPO_ROOT / "hooks" / "run-hook.cmd"):
        if str(runner) in command:
            return REPO_ROOT
    return None


def cursor_runtime_command_is_current(command: str, mode: str) -> bool:
    if mode not in command:
        return False
    normalized = command.replace("\\", "/")
    if "AGENT_HARNESS_PLUGIN_ROOT" in command or ".agent-harness/" in normalized:
        return False
    root = cursor_runtime_root(command)
    if root is None or not runtime_package_is_current(root):
        return False
    return any(str(root / "hooks" / name) in command for name in ("run-hook", "run-hook.cmd"))


def opencode_runtime_root(text: str) -> Path | None:
    match = OPENCODE_RUNTIME_ROOT_PATTERN.search(text)
    if match is None:
        return None
    try:
        raw = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return Path(raw).expanduser() if isinstance(raw, str) and raw else None


def pi_runtime_root(text: str) -> Path | None:
    match = PI_RUNTIME_ROOT_PATTERN.search(text)
    if match is None:
        return None
    try:
        raw = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return Path(raw).expanduser() if isinstance(raw, str) and raw else None


def runtime_adapter_info(tool: str, scope: str, project_root: Path) -> dict[str, str | None]:
    home = tool_base(tool, scope, project_root).parent
    if tool == "cursor":
        path = home / "hooks.json"
        if path.exists() and not path.is_file():
            return {
                "path": str(path),
                "state": "conflict",
                "hooks": "invalid",
                "activation": "unknown",
                "registration": "conflict",
                "runtime": "inactive",
            }
        if not path.is_file():
            return {
                "path": str(path),
                "state": "missing",
                "hooks": "missing",
                "activation": "unknown",
                "registration": "missing",
                "runtime": "inactive",
            }
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        hooks = payload.get("hooks") if isinstance(payload, dict) else None
        commands = [
            str(entry.get("command"))
            for entries in hooks.values()
            if isinstance(entries, list)
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("command"), str)
        ] if isinstance(hooks, dict) else []
        runtime_roots = {
            root.resolve(strict=False)
            for root in (cursor_runtime_root(command) for command in commands)
            if root is not None
        }
        modes_present = all(any(mode in command for command in commands) for mode in CURSOR_HOOK_MODES)
        paths_current = all(
            any(cursor_runtime_command_is_current(command, mode) for command in commands)
            for mode in CURSOR_HOOK_MODES
        )
        if not isinstance(hooks, dict):
            state, hooks_state = "conflict", "invalid"
        elif len(runtime_roots) > 1:
            state, hooks_state = "conflict", "duplicate-hook-runtime"
        elif not modes_present:
            state, hooks_state = "missing", "missing"
        elif not paths_current:
            state, hooks_state = "stale", "present"
        else:
            state, hooks_state = "ok", "present"
        return {
            "path": str(path),
            "state": state,
            "hooks": hooks_state,
            "activation": "enabled" if state == "ok" else "unknown",
            "registration": "present",
            "runtime": "active" if state == "ok" else "inactive",
        }

    if tool == "pi":
        path = home / "extensions" / "tenetora.ts"
        if path.exists() and not path.is_file():
            return {
                "path": str(path),
                "state": "conflict",
                "hooks": "invalid",
                "activation": "unknown",
                "registration": "conflict",
                "runtime": "inactive",
            }
        if not path.is_file():
            return {
                "path": str(path),
                "state": "missing",
                "hooks": "missing",
                "activation": "unknown",
                "registration": "missing",
                "runtime": "inactive",
            }
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        runtime_root = pi_runtime_root(text)
        version_match = PI_RUNTIME_VERSION_PATTERN.search(text)
        try:
            embedded_version = json.loads(version_match.group(1)) if version_match else None
        except json.JSONDecodeError:
            embedded_version = None
        if PI_RUNTIME_MARKER not in text:
            state = "conflict"
        elif (
            not isinstance(embedded_version, str)
            or embedded_version != __version__
            or runtime_root is None
            or not runtime_package_is_current(runtime_root)
            or '"--platform", "pi"' not in text
        ):
            state = "stale"
        else:
            state = "ok"
        return {
            "path": str(path),
            "state": state,
            "hooks": (
                "pending-project-trust"
                if scope == "project" and state == "ok"
                else "present" if state in {"ok", "stale"} else "invalid"
            ),
            "activation": (
                "trust-required"
                if scope == "project" and state == "ok"
                else "auto-loaded" if state == "ok" else "unknown"
            ),
            "registration": "present",
            "runtime": (
                "needs-trust-review"
                if scope == "project" and state == "ok"
                else "active-partial" if state == "ok" else "inactive"
            ),
        }

    path = home / "plugins" / "tenetora.js"
    legacy_path = home / "plugins" / "agent-harness.js"
    if path.exists() and not path.is_file():
        return {
            "path": str(path),
            "state": "conflict",
            "hooks": "invalid",
            "activation": "unknown",
            "registration": "conflict",
            "runtime": "inactive",
        }
    if not path.is_file() and legacy_path.is_file():
        return {
            "path": str(legacy_path),
            "state": "stale",
            "hooks": "legacy",
            "activation": "unknown",
            "registration": "legacy-present",
            "runtime": "inactive",
        }
    if not path.is_file():
        return {
            "path": str(path),
            "state": "missing",
            "hooks": "missing",
            "activation": "unknown",
            "registration": "missing",
            "runtime": "inactive",
        }
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    runtime_root = opencode_runtime_root(text)
    if LEGACY_OPENCODE_RUNTIME_MARKER in text and OPENCODE_RUNTIME_MARKER not in text:
        state = "stale"
    elif OPENCODE_RUNTIME_MARKER not in text:
        state = "conflict"
    elif (
        f'TENETORA_VERSION = "{__version__}"' not in text
        or runtime_root is None
        or not runtime_package_is_current(runtime_root)
        or "run-hook" not in text
        or "run-hook.cmd" not in text
    ):
        state = "stale"
    else:
        state = "ok"
    return {
        "path": str(path),
        "state": state,
        "hooks": "present" if state in {"ok", "stale"} else "invalid",
        "activation": "auto-loaded" if state == "ok" else "unknown",
        "registration": "present",
        "runtime": "active-partial" if state == "ok" else "inactive",
    }


def describe_install(tool: str, scope: str, project_root: Path) -> ToolInstall:
    install_root = tool_base(tool, scope, project_root)
    skills = [describe_skill_install(install_root, skill_name) for skill_name in LIFECYCLE_SKILLS]
    router = skills[0]

    if any(skill.state == "conflict" for skill in skills):
        package_state = "conflict"
    elif any(skill.state == "missing" for skill in skills):
        package_state = "missing"
    elif any(skill.state == "stale" for skill in skills):
        package_state = "stale"
    else:
        package_state = "ok"

    if tool in NATIVE_PLUGIN_TARGETS and scope == "global":
        plugin = native_plugin_info(tool, project_root)
        codex_runtime = codex_hook_runtime_info(project_root, plugin) if tool == "codex" else None
        plugin_state = plugin["state"]
        mode = (
            str(codex_runtime["hook_mode"])
            if codex_runtime is not None and codex_runtime.get("hook_mode") != "skills-only"
            else ("plugin" if plugin_state in {"ok", "stale"} else router.mode)
        )
        reported_skills = skills
        if plugin_state in {"ok", "stale"} and plugin["path"]:
            reported_skills = [
                describe_skill_install(Path(str(plugin["path"])) / "skills", skill_name)
                for skill_name in LIFECYCLE_SKILLS
            ]
            if any(skill.state == "conflict" for skill in reported_skills):
                package_state = "conflict"
            elif any(skill.state == "missing" for skill in reported_skills):
                package_state = "missing"
            elif any(skill.state == "stale" for skill in reported_skills) or plugin_state == "stale":
                package_state = "stale"
            else:
                package_state = "ok"
            if plugin["hooks"] != "present":
                package_state = "missing"
        return tool_install(
            tool,
            scope,
            install_root,
            Path(str(plugin["path"])) if plugin["path"] else Path(router.path),
            package_state,
            mode,
            str(plugin["path"]) if plugin["path"] else router.source,
            reported_skills,
            plugin_root=Path(str(plugin["path"])) if plugin["path"] else None,
            plugin_state=plugin["state"],
            hooks_state=(str(codex_runtime["hooks_state"]) if codex_runtime is not None else plugin["hooks"]),
            activation_state=(
                str(codex_runtime["activation_state"])
                if codex_runtime is not None and codex_runtime.get("activation_state") is not None
                else plugin["activation"]
            ),
            registration_state=plugin["registration"],
            runtime_state=(str(codex_runtime["runtime_state"]) if codex_runtime is not None else plugin["runtime"]),
            hook_mode=(str(codex_runtime["hook_mode"]) if codex_runtime is not None else "native-plugin"),
            degraded_reason=(
                str(codex_runtime["degraded_reason"])
                if codex_runtime is not None and codex_runtime.get("degraded_reason") is not None
                else None
            ),
        )

    if tool == "zcode" and scope == "global":
        plugin = zcode_plugin_info(scope, project_root)
        plugin_state = plugin["state"]
        mode = "plugin" if plugin_state in {"ok", "stale"} else router.mode
        reported_skills = skills
        if plugin_state in {"ok", "stale"}:
            reported_skills = [
                describe_skill_install(Path(str(plugin["path"])) / "skills", skill_name)
                for skill_name in LIFECYCLE_SKILLS
            ]
            if any(skill.state == "conflict" for skill in reported_skills):
                package_state = "conflict"
            elif any(skill.state == "missing" for skill in reported_skills):
                package_state = "missing"
            elif any(skill.state == "stale" for skill in reported_skills) or plugin_state == "stale":
                package_state = "stale"
            else:
                package_state = "ok"
            if plugin["hooks"] != "present":
                package_state = "missing"
        else:
            package_state = plugin_state or package_state
        return tool_install(
            tool,
            scope,
            install_root,
            Path(str(plugin["path"])),
            str(package_state),
            mode,
            str(plugin["path"]),
            reported_skills,
            plugin_root=Path(str(plugin["path"])),
            plugin_state=plugin["state"],
            hooks_state=plugin["hooks"],
            activation_state=plugin["activation"],
            registration_state=plugin["registration"],
            runtime_state=(
                "active"
                if plugin["state"] == "ok" and plugin["hooks"] == "present" and plugin["activation"] == "enabled"
                else "inactive"
            ),
        )

    if tool in RUNTIME_ADAPTER_TARGETS:
        runtime = runtime_adapter_info(tool, scope, project_root)
        if package_state == "ok" and runtime["state"] != "ok":
            package_state = str(runtime["state"])
        return tool_install(
            tool,
            scope,
            install_root,
            Path(router.path),
            package_state,
            "hooks" if tool == "cursor" else "extension" if tool == "pi" else "plugin",
            router.source,
            skills,
            plugin_root=Path(str(runtime["path"])),
            plugin_state=runtime["state"],
            hooks_state=runtime["hooks"],
            activation_state=runtime["activation"],
            registration_state=runtime["registration"],
            runtime_state=runtime["runtime"],
        )

    runtime_state = "skills-only"
    return tool_install(
        tool,
        scope,
        install_root,
        Path(router.path),
        package_state,
        router.mode,
        router.source,
        skills,
        runtime_state=runtime_state,
    )


def tenetora_home() -> Path:
    return machine_home()


def source_fingerprint(platform: str, root: Path, version: str | None) -> str:
    identity = f"{platform}\0{root.resolve(strict=False)}\0{version or 'unknown'}"
    return hashlib.sha256(identity.encode("utf-8", errors="surrogatepass")).hexdigest()


def source_candidate(
    *,
    layer: str,
    kind: str,
    scope: str,
    path: Path,
    version: str | None,
    state: str,
    basis: str,
    configured: bool,
    enabled: bool | None = None,
    ready: bool | None = None,
    source_root: Path | None = None,
    canonical_runtime: bool | None = None,
) -> dict[str, object]:
    candidate: dict[str, object] = {
        "layer": layer,
        "kind": kind,
        "scope": scope,
        "path": str(path),
        "version": version,
        "state": state,
        "basis": basis,
        "configured": configured,
        "enabled": enabled,
        "ready": ready,
        "canonical_runtime": canonical_runtime,
    }
    if source_root is not None:
        candidate["source_root"] = str(source_root)
        candidate["source_fingerprint"] = source_fingerprint(kind.split("-")[0], source_root, version)
    return candidate


def existing_skill_candidate(path: Path, scope: str, kind: str, basis: str = "inferred") -> dict[str, object] | None:
    described = describe_skill_install(path.parent, SKILL_NAME)
    if described.state == "missing":
        return None
    return source_candidate(
        layer="skill",
        kind=kind,
        scope=scope,
        path=path,
        version=described.version,
        state=described.state,
        basis=basis,
        configured=True,
        enabled=None,
    )


def plugin_cache_roots(tool: str, project_root: Path) -> list[Path]:
    if tool in {"codex", "claude"}:
        config_root = tool_base(tool, "global", project_root).parent
        bases = [
            config_root / "plugins" / "cache" / NATIVE_MARKETPLACE_ID / PLUGIN_REGISTRATION_NAME,
            config_root
            / "plugins"
            / "cache"
            / LEGACY_NATIVE_MARKETPLACE_ID
            / LEGACY_PLUGIN_REGISTRATION_NAME,
        ]
    elif tool == "zcode":
        bases = [zcode_plugin_base(project_root), legacy_zcode_plugin_base(project_root)]
    else:
        return []
    roots = [
        path
        for base in bases
        if base.is_dir()
        for path in base.iterdir()
        if path.is_dir()
    ]
    return sorted(roots, key=lambda path: (str(path.parent), path.name))


def skill_source_summary(
    tool: str,
    project_root: Path,
    inventory: dict[tuple[str, str], ToolInstall],
) -> dict[str, object]:
    ordered: list[tuple[Path, str, str, str]] = []
    if tool == "zcode":
        ordered.extend(
            [
                (tool_base("zcode", "global", project_root) / SKILL_NAME, "global", "zcode-user-skill", "inferred"),
                (tool_base("agents", "global", project_root) / SKILL_NAME, "global", "agents-user-skill", "inferred"),
                (tool_base("zcode", "project", project_root) / SKILL_NAME, "project", "zcode-project-skill", "inferred"),
                (tool_base("agents", "project", project_root) / SKILL_NAME, "project", "agents-project-skill", "inferred"),
            ]
        )
    else:
        ordered.extend(
            [
                (tool_base(tool, "global", project_root) / SKILL_NAME, "global", f"{tool}-user-skill", "inferred"),
                (tool_base(tool, "project", project_root) / SKILL_NAME, "project", f"{tool}-project-skill", "inferred"),
            ]
        )
    global_install = inventory.get((tool, "global"))
    if global_install and global_install.plugin_root:
        ordered.append(
            (
                Path(global_install.plugin_root) / "skills" / SKILL_NAME,
                "plugin",
                f"{tool}-plugin-skill",
                "host-api-verified" if tool in {"codex", "claude"} else "config-verified",
            )
        )

    candidates: list[dict[str, object]] = []
    seen: set[str] = set()
    for path, scope, kind, basis in ordered:
        key = str(path.resolve(strict=False)) + "\0" + str(path)
        if key in seen:
            continue
        seen.add(key)
        candidate = existing_skill_candidate(path, scope, kind, basis)
        if candidate is not None:
            candidates.append(candidate)
    effective = candidates[0] if candidates else None
    return {
        "status": "resolved" if effective else "missing",
        "effective": effective,
        "candidates": candidates,
        "shadowed": candidates[1:] if effective else [],
        "basis": str(effective.get("basis")) if effective else "unknown",
        "remediation": None if effective else f"tenetora install --tools {tool} --global",
    }


def plugin_source_summary(
    tool: str,
    project_root: Path,
    inventory: dict[tuple[str, str], ToolInstall],
) -> dict[str, object]:
    if tool not in {"codex", "claude", "zcode"}:
        return {
            "status": "not-applicable",
            "effective": None,
            "candidates": [],
            "shadowed": [],
            "basis": "unknown",
            "remediation": None,
        }
    install = inventory.get((tool, "global"))
    registered = Path(install.plugin_root).resolve(strict=False) if install and install.plugin_root else None
    candidates: list[dict[str, object]] = []
    seen: set[str] = set()
    if install and install.plugin_root and install.plugin_state != "missing":
        path = Path(install.plugin_root)
        seen.add(str(path.resolve(strict=False)))
        candidates.append(
            source_candidate(
                layer="plugin",
                kind=f"{tool}-registered-plugin",
                scope="global",
                path=path,
                version=read_skill_version(path) or normalized_version(path.name),
                state=install.plugin_state or "unknown",
                basis="host-api-verified" if tool in {"codex", "claude"} else "config-verified",
                configured=True,
                enabled=install.activation_state == "enabled",
            )
        )
    for path in plugin_cache_roots(tool, project_root):
        canonical = str(path.resolve(strict=False))
        if canonical in seen:
            continue
        seen.add(canonical)
        candidates.append(
            source_candidate(
                layer="plugin",
                kind=f"{tool}-cached-plugin",
                scope="global",
                path=path,
                version=read_skill_version(path) or normalized_version(path.name),
                state="shadowed-cache" if registered is not None else "unregistered-cache",
                basis="file-present",
                configured=False,
                enabled=False,
            )
        )
    effective = candidates[0] if candidates and candidates[0].get("configured") else None
    return {
        "status": "resolved" if effective else ("cached-only" if candidates else "missing"),
        "effective": effective,
        "candidates": candidates,
        "shadowed": [candidate for candidate in candidates if candidate is not effective],
        "basis": str(effective.get("basis")) if effective else "file-present" if candidates else "unknown",
        "remediation": None if effective else f"tenetora install --tools {tool} --global",
    }


def adapter_source_root(tool: str, runtime_path: Path) -> Path | None:
    try:
        text = runtime_path.read_text(encoding="utf-8")
    except OSError:
        return None
    if tool == "cursor":
        roots = {
            root.resolve(strict=False)
            for root in (cursor_runtime_root(line) for line in text.splitlines())
            if root is not None
        }
        return next(iter(roots)) if len(roots) == 1 else None
    if tool == "pi":
        return pi_runtime_root(text)
    return opencode_runtime_root(text)


def runtime_source_summary(
    tool: str,
    project_root: Path,
    inventory: dict[tuple[str, str], ToolInstall],
) -> dict[str, object]:
    candidates: list[dict[str, object]] = []
    global_install = inventory.get((tool, "global"))
    if tool == "codex" and global_install is not None:
        native_possible = global_install.hook_mode == "native-plugin" or (
            global_install.hooks_state == "duplicate-hook-runtime"
            and global_install.degraded_reason
            in {"codex-native-and-project-hooks", "codex-canonical-and-legacy-hooks"}
        )
        if global_install.plugin_root and global_install.plugin_state != "missing":
            source_root = Path(global_install.plugin_root)
            canonical_runtime = (
                global_install.plugin_state == "ok"
                and global_install.hooks_state
                not in {
                    "legacy-native-hook-active",
                    "duplicate-hook-runtime",
                    "native-hook-blocks-fallback",
                    "inactive",
                }
            )
            if native_possible:
                native_enabled: bool | None = True
            elif (
                canonical_runtime
                and global_install.activation_state in {"enabled", "unknown"}
                and global_install.hooks_state == "hook-inventory-unavailable"
            ):
                native_enabled = None
            else:
                native_enabled = False
            candidates.append(
                source_candidate(
                    layer="runtime",
                    kind="codex-native-plugin",
                    scope="global",
                    path=source_root / "hooks" / "hooks.json",
                    source_root=source_root,
                    version=read_skill_version(source_root),
                    state=global_install.hooks_state or "unknown",
                    basis=("host-api-verified" if global_install.hooks_state != "hook-inventory-unavailable" else "config-verified"),
                    configured=True,
                    enabled=native_enabled,
                    ready=global_install.runtime_state == "active",
                    canonical_runtime=canonical_runtime,
                )
            )
        fallback_path = project_root / ".codex" / "hooks.json"
        project_reported_by_host = (
            global_install.hook_mode == "project-fallback"
            or global_install.degraded_reason == "codex-native-and-project-hooks"
        )
        if codex_project_config_has_managed_hooks(project_root) or project_reported_by_host:
            enabled: bool | None
            if project_reported_by_host:
                enabled = True
            elif global_install.hooks_state == "hook-inventory-unavailable":
                enabled = None
            else:
                enabled = False
            source_root = tenetora_home() / "runtime"
            candidates.append(
                source_candidate(
                    layer="runtime",
                    kind="codex-project-fallback",
                    scope="project",
                    path=fallback_path,
                    source_root=source_root,
                    version=read_skill_version(source_root),
                    state=(global_install.hooks_state if global_install.hook_mode in {"project-fallback", "conflict"} else "configured"),
                    basis=("host-api-verified" if global_install.hooks_state != "hook-inventory-unavailable" else "config-verified"),
                    configured=True,
                    enabled=enabled,
                    ready=global_install.runtime_state == "active",
                    canonical_runtime=(
                        codex_project_config_has_managed_hooks(project_root)
                        and hook_runner_state(source_root) == "present"
                    ) or global_install.degraded_reason == "codex-native-and-project-hooks",
                )
            )
    elif tool in {"claude", "zcode"} and global_install is not None:
        if global_install.plugin_root and global_install.plugin_state != "missing":
            source_root = Path(global_install.plugin_root)
            candidates.append(
                source_candidate(
                    layer="runtime",
                    kind=f"{tool}-native-plugin",
                    scope="global",
                    path=source_root / "hooks",
                    source_root=source_root,
                    version=read_skill_version(source_root),
                    state=global_install.hooks_state or "unknown",
                    basis="host-api-verified" if tool == "claude" else "config-verified",
                    configured=True,
                    enabled=(global_install.activation_state == "enabled" and global_install.hooks_state == "present"),
                    ready=global_install.runtime_state == "active",
                    canonical_runtime=(
                        global_install.plugin_state == "ok"
                        and global_install.hooks_state == "present"
                        and global_install.activation_state == "enabled"
                    ),
                )
            )
    elif tool in RUNTIME_ADAPTER_TARGETS:
        for scope in ("global", "project"):
            install = inventory.get((tool, scope))
            if install is None or install.plugin_state == "missing" or not install.plugin_root:
                continue
            runtime_path = Path(install.plugin_root)
            source_root = adapter_source_root(tool, runtime_path)
            candidates.append(
                source_candidate(
                    layer="runtime",
                    kind=f"{tool}-{scope}-adapter",
                    scope=scope,
                    path=runtime_path,
                    source_root=source_root,
                    version=read_skill_version(source_root) if source_root else None,
                    state=install.plugin_state or "unknown",
                    basis="config-verified",
                    configured=True,
                    enabled=(
                        None
                        if install.hooks_state == "pending-project-trust"
                        else install.hooks_state == "present" and install.plugin_state in {"ok", "stale"}
                    ),
                    ready=install.runtime_state in {"active", "active-partial"},
                    canonical_runtime=(
                        install.plugin_state == "ok"
                        and install.hooks_state == "present"
                        and source_root is not None
                        and source_root.exists()
                    ),
                )
            )

    if (
        tool == "codex"
        and global_install is not None
        and global_install.hooks_state == "duplicate-hook-runtime"
    ):
        return {
            "status": "duplicate-hook-runtime",
            "effective": None,
            "candidates": candidates,
            "shadowed": candidates,
            "basis": "host-api-verified",
            "remediation": "Keep one codex runtime for this project and remove only the Tenetora-managed duplicate.",
        }

    duplicate_adapter_runtime = next(
        (
            install
            for (candidate_tool, _scope), install in inventory.items()
            if candidate_tool == tool and install.hooks_state == "duplicate-hook-runtime"
        ),
        None,
    )
    if tool in RUNTIME_ADAPTER_TARGETS and duplicate_adapter_runtime is not None:
        return {
            "status": "duplicate-hook-runtime",
            "effective": None,
            "candidates": candidates,
            "shadowed": candidates,
            "basis": "config-verified",
            "remediation": f"Keep one {tool} runtime for this project and remove only the Tenetora-managed duplicate.",
        }

    enabled = [candidate for candidate in candidates if candidate.get("enabled") is True]
    uncertain = [candidate for candidate in candidates if candidate.get("enabled") is None]
    if len(enabled) > 1:
        status = "duplicate-hook-runtime"
        effective = None
        remediation = f"Keep one {tool} runtime for this project and remove only the Tenetora-managed duplicate."
    elif len(enabled) == 1:
        effective = enabled[0]
        if effective.get("ready") is True:
            status = "active"
            remediation = None
        else:
            status = "configured-not-verified"
            remediation = f"tenetora doctor --tools {tool} --path {project_root}"
    elif len(uncertain) == 1:
        status = "configured-not-verified"
        effective = uncertain[0]
        remediation = f"Restart or reload {tool}, then run tenetora doctor --tools {tool} --path {project_root}"
    elif candidates:
        status = "configured-inactive"
        effective = candidates[0] if len(candidates) == 1 else None
        remediation = f"tenetora doctor --tools {tool} --path {project_root}"
    else:
        status = "skills-only"
        effective = None
        remediation = None
    return {
        "status": status,
        "effective": effective,
        "candidates": candidates,
        "shadowed": [candidate for candidate in candidates if candidate is not effective],
        "basis": str(effective.get("basis")) if effective else "unknown",
        "remediation": remediation,
    }


def observation_file(project_root: Path, platform: str) -> Path:
    explicit = os.environ.get("TENETORA_OBSERVATION_DIR")
    base = Path(explicit).expanduser() if explicit else tenetora_home() / "state" / "runtime-observations"
    project_hash = hashlib.sha256(str(project_root.resolve()).encode("utf-8", errors="surrogatepass")).hexdigest()
    return base / platform / f"{project_hash}.json"


def runtime_observation_summary(
    project_root: Path,
    platform: str,
    runtime: dict[str, object],
) -> dict[str, object]:
    path = observation_file(project_root, platform)
    empty = {
        "state": "not-observed",
        "basis": "unknown",
        "observed_at": None,
        "event": None,
        "dispatch_observation_state": "not-observed",
        "dispatch_observed_at": None,
        "dispatch_event": None,
        "session_hash": None,
    }
    if not path.is_file():
        return empty
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {**empty, "state": "invalid", "basis": "file-present"}
    if not isinstance(payload, dict) or payload.get("platform") != platform:
        return {**empty, "state": "invalid", "basis": "file-present"}
    try:
        observed_at = dt.datetime.fromisoformat(str(payload.get("observed_at", "")).replace("Z", "+00:00"))
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=dt.timezone.utc)
        age_seconds = max(0, int((dt.datetime.now(dt.timezone.utc) - observed_at).total_seconds()))
    except (TypeError, ValueError):
        return {
            **empty,
            "state": "invalid",
            "basis": "file-present",
            "observed_at": payload.get("observed_at"),
            "event": payload.get("event"),
        }
    state = "recent" if age_seconds <= OBSERVATION_RECENT_SECONDS else "stale"
    effective = runtime.get("effective")
    expected = effective.get("source_fingerprint") if isinstance(effective, dict) else None
    actual = payload.get("source_fingerprint")
    observed_version = str(payload.get("version") or "unknown")
    legacy_codex_sources = [tenetora_home() / "runtime", *plugin_cache_roots("codex", project_root)]
    legacy_codex_as_claude = platform == "claude" and any(
        actual == source_fingerprint("claude", source_root, observed_version)
        for source_root in legacy_codex_sources
    )
    if state == "recent" and legacy_codex_as_claude:
        state = "legacy-misattributed"
    elif state == "recent" and expected and actual != expected:
        state = "observed-source-mismatch"
    if state == "recent" and runtime.get("status") == "duplicate-hook-runtime":
        state = "observed-conflict"
    current_session = payload.get("current_session")
    last_dispatch = payload.get("last_dispatch_observation")
    dispatch_state = "not-observed"
    dispatch_observed_at = None
    dispatch_event = None
    session_hash = None
    if isinstance(current_session, dict):
        session_hash = current_session.get("session_hash")
    if (
        isinstance(current_session, dict)
        and current_session.get("dispatch_observed") is True
        and isinstance(last_dispatch, dict)
        and last_dispatch.get("session_hash") == session_hash
    ):
        dispatch_observed_at = last_dispatch.get("observed_at")
        dispatch_event = last_dispatch.get("event")
        try:
            dispatch_time = dt.datetime.fromisoformat(
                str(dispatch_observed_at or "").replace("Z", "+00:00")
            )
            if dispatch_time.tzinfo is None:
                dispatch_time = dispatch_time.replace(tzinfo=dt.timezone.utc)
            dispatch_age = max(
                0,
                int((dt.datetime.now(dt.timezone.utc) - dispatch_time).total_seconds()),
            )
            dispatch_state = "recent" if dispatch_age <= OBSERVATION_RECENT_SECONDS else "stale"
        except (TypeError, ValueError):
            dispatch_state = "invalid"
        if state in {"observed-source-mismatch", "observed-conflict", "invalid"}:
            dispatch_state = state
    return {
        "state": state,
        "basis": "runtime-observed" if state in {"recent", "observed-source-mismatch", "observed-conflict"} else "file-present",
        "observed_at": payload.get("observed_at"),
        "event": payload.get("event"),
        "version": payload.get("version"),
        "source_fingerprint": actual,
        "age_seconds": age_seconds,
        "schema_version": payload.get("schema_version", 1),
        "dispatch_observation_state": dispatch_state,
        "dispatch_observed_at": dispatch_observed_at,
        "dispatch_event": dispatch_event,
        "session_hash": session_hash,
    }


def observation_matches_effective_runtime(
    runtime: dict[str, object],
    observation: dict[str, object],
) -> bool:
    effective = runtime.get("effective")
    candidates = runtime.get("candidates")
    if (
        not isinstance(effective, dict)
        or not isinstance(candidates, list)
        or len(candidates) != 1
        or observation.get("state") != "recent"
        or effective.get("canonical_runtime") is not True
        or effective.get("enabled") is False
    ):
        return False
    expected = effective.get("source_fingerprint")
    actual = observation.get("source_fingerprint")
    return isinstance(expected, str) and bool(expected) and actual == expected


def version_tuple(raw: str) -> tuple[int, ...] | None:
    match = re.match(r"^(\d+(?:\.\d+)*)", raw)
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def version_relation(version: object) -> str:
    if not isinstance(version, str) or not version:
        return "unknown"
    if version == __version__:
        return "current"
    current = version_tuple(__version__)
    candidate = version_tuple(version)
    if current is None or candidate is None:
        return "different"
    return "behind" if candidate < current else "ahead"


def version_alignment_summary(layers: list[dict[str, object]]) -> dict[str, object]:
    copies: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for layer in layers:
        for raw in layer.get("candidates", []):
            if not isinstance(raw, dict):
                continue
            key = (str(raw.get("layer")), str(raw.get("path")))
            if key in seen:
                continue
            seen.add(key)
            copies.append(
                {
                    "layer": raw.get("layer"),
                    "kind": raw.get("kind"),
                    "path": raw.get("path"),
                    "version": raw.get("version"),
                    "relation": version_relation(raw.get("version")),
                    "alignment_relevant": raw.get("configured") is not False,
                }
            )
    relations = {
        str(copy["relation"])
        for copy in copies
        if copy.get("alignment_relevant") is True
    }
    if relations and relations <= {"current"}:
        status = "aligned"
    elif relations - {"current", "unknown"}:
        status = "drift"
    else:
        status = "unknown"
    return {"status": status, "expected_version": __version__, "copies": copies}


def apply_effective_source_model(
    installs: list[ToolInstall],
    inventory: dict[tuple[str, str], ToolInstall],
    project_root: Path,
) -> None:
    summaries: dict[str, tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object], dict[str, object]]] = {}
    for tool in {install.tool for install in installs}:
        skill = skill_source_summary(tool, project_root, inventory)
        plugin = plugin_source_summary(tool, project_root, inventory)
        runtime = runtime_source_summary(tool, project_root, inventory)
        observation = runtime_observation_summary(project_root, tool, runtime)
        if runtime.get("status") == "active":
            runtime["configured_status"] = "active"
            observation_state = observation.get("state")
            if observation_state == "recent":
                runtime["basis"] = "runtime-observed"
            elif observation_state == "stale":
                runtime["status"] = "configured-observation-stale"
                runtime["remediation"] = f"Restart or reload {tool}, then run tenetora doctor --tools {tool} --path {project_root}"
            elif observation_state == "invalid":
                runtime["status"] = "configured-observation-invalid"
                runtime["remediation"] = f"tenetora doctor --tools {tool} --path {project_root}"
            elif observation_state == "observed-source-mismatch":
                runtime["status"] = "configured-observation-mismatch"
                runtime["remediation"] = f"tenetora doctor --tools {tool} --path {project_root}"
            else:
                runtime["status"] = "configured-not-observed"
                runtime["remediation"] = f"Restart or reload {tool}, then run tenetora doctor --tools {tool} --path {project_root}"
        elif (
            runtime.get("status") in {"configured-inactive", "configured-not-verified"}
            and observation_matches_effective_runtime(runtime, observation)
        ):
            runtime["configured_status"] = "active"
            runtime["status"] = "active-observed-config-unverified"
            runtime["basis"] = "runtime-observed"
            runtime["remediation"] = f"tenetora doctor --tools {tool} --path {project_root}"
        alignment = version_alignment_summary([skill, plugin, runtime])
        summaries[tool] = (skill, plugin, runtime, observation, alignment)
    for install in installs:
        skill, plugin, runtime, observation, alignment = summaries[install.tool]
        install.effective_sources = {"skill": skill, "plugin": plugin, "runtime": runtime}
        install.runtime_observation = observation
        install.version_alignment = alignment
        effective_runtime = runtime.get("effective") if isinstance(runtime, dict) else None
        if (
            install.tool in RUNTIME_ADAPTER_TARGETS
            and install.state == "missing"
            and install.plugin_state == "missing"
            and lifecycle_skill_package_state(install) == "ok"
            and isinstance(effective_runtime, dict)
            and effective_runtime.get("scope") != install.scope
            and effective_runtime.get("enabled") is True
            and effective_runtime.get("ready") is True
        ):
            install.state = "covered"
            install.degraded_reason = f"runtime-provided-by-{effective_runtime.get('scope')}-scope"
        if install.delegation_capabilities is not None:
            install.delegation_capabilities["lifecycle_observation"] = (
                observation.get("dispatch_observation_state") == "recent"
            )
            install.delegation_capabilities["runtime_ready"] = runtime.get("status") in {
                "active",
                "active-observed-config-unverified",
            }
        if runtime.get("status") == "duplicate-hook-runtime":
            install.hooks_state = "duplicate-hook-runtime"
            install.runtime_state = "inactive"
            install.hook_mode = "conflict"
        install.delegation_status = tool_delegation_status(
            install.tool,
            install.hooks_state,
            (
                "active"
                if runtime.get("status") in {"active", "active-observed-config-unverified"}
                else install.runtime_state
            ),
            observation,
        )


def harness_ignored(project_root: Path) -> bool:
    gitignore = project_root / ".gitignore"
    if not gitignore.exists():
        return False
    lines = gitignore.read_text(encoding="utf-8").splitlines()
    return any(line.strip().rstrip("/") == ".tenetora" for line in lines)


def alignment_handoff_hash(payload: dict[str, object]) -> str:
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {"handoff_hash", "handoff_relative_path", "revision"}
    }
    canonical = json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def alignment_snapshot(root: Path) -> dict[str, object]:
    state_dir = root / ".tenetora" / "state"
    legacy_path = state_dir / "alignment-session.json"
    isolated_dir = state_dir / "alignment-sessions"
    session_paths = ([legacy_path] if legacy_path.is_file() else []) + sorted(
        path for path in isolated_dir.glob("*.json") if path.is_file()
    )
    records: list[dict[str, object]] = []
    for selected in session_paths:
        try:
            payload = json.loads(selected.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"status": "corrupt", "stale": True, "reason": f"{selected.name} is unreadable: {exc}"}
        if not isinstance(payload, dict):
            return {"status": "corrupt", "stale": True, "reason": f"{selected.name} is not a JSON object"}
        status = str(payload.get("status") or "corrupt")
        expired_from_status = str(payload.get("expired_from_status") or "")
        attempts = payload.get("execution_attempts", [])
        running_execution_attempts = (
            sum(
                1
                for attempt in attempts
                if isinstance(attempt, dict) and attempt.get("status") == "running"
            )
            if isinstance(attempts, list)
            else 0
        )
        expires_at = payload.get("expires_at")
        if status in {"active", "checkpoint", "paused", "blocked", "awaiting-confirmation"} and isinstance(expires_at, str):
            try:
                parsed_expiry = dt.datetime.fromisoformat(expires_at[:-1] + "+00:00" if expires_at.endswith("Z") else expires_at)
                if parsed_expiry.tzinfo is None:
                    raise ValueError("expiry must include a timezone")
                if parsed_expiry.astimezone(dt.timezone.utc) <= dt.datetime.now(dt.timezone.utc):
                    expired_from_status = status
                    status = "expired"
            except ValueError:
                return {"status": "corrupt", "stale": True, "reason": f"{selected.name} has an invalid expiry"}
        if selected == legacy_path:
            status = "legacy-unowned" if status in {"active", "checkpoint", "paused", "blocked", "awaiting-confirmation"} else status
        records.append(
            {
                "session_id": payload.get("session_id", ""),
                "owner_id": payload.get("owner_id", "") or "unowned",
                "conversation_id": payload.get("conversation_id", ""),
                "tool": payload.get("tool", "unknown"),
                "status": status,
                "goal_fingerprint": payload.get("goal_fingerprint", ""),
                "created_at": payload.get("created_at", ""),
                "updated_at": payload.get("updated_at", ""),
                "heartbeat_at": payload.get("heartbeat_at", ""),
                "expires_at": payload.get("expires_at", ""),
                "expired_from_status": expired_from_status,
                "running_execution_attempts": running_execution_attempts,
                "legacy_unowned": selected == legacy_path,
            }
        )
    active = [item for item in records if item.get("status") in {"active", "checkpoint", "paused", "blocked", "awaiting-confirmation", "legacy-unowned"}]
    expired = [item for item in records if item.get("status") == "expired"]
    actionable_expired = [
        item
        for item in expired
        if item.get("expired_from_status") == "awaiting-confirmation"
        or int(item.get("running_execution_attempts", 0) or 0) > 0
    ]
    historical_expired = [item for item in expired if item not in actionable_expired]
    if active:
        snapshot = {
            "status": "conflict",
            "stale": False,
            "reason": "explicit alignment session identity is required",
            "active_session_count": len(active),
            "active_sessions": active,
        }
        if actionable_expired:
            snapshot["expired_sessions"] = actionable_expired
        if historical_expired:
            snapshot["historical_expired_sessions"] = historical_expired
        return snapshot
    if actionable_expired:
        snapshot = {
            "status": "stale",
            "stale": True,
            "reason": "one or more expired alignment sessions still have pending work",
            "expired_session_count": len(actionable_expired),
            "expired_sessions": actionable_expired,
        }
        if historical_expired:
            snapshot["historical_expired_sessions"] = historical_expired
        return snapshot
    current_path = state_dir / "current-alignment.json"
    if not current_path.is_file():
        snapshot = {"status": "not-used", "stale": False}
        if historical_expired:
            snapshot.update(
                {
                    "reason": "no active alignment session; expired sessions have no pending work",
                    "ignored_expired_session_count": len(historical_expired),
                    "historical_expired_sessions": historical_expired,
                }
            )
        return snapshot
    selected = current_path
    try:
        payload = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "corrupt", "stale": True, "reason": f"{selected.name} is unreadable: {exc}"}
    if not isinstance(payload, dict):
        return {"status": "corrupt", "stale": True, "reason": f"{selected.name} is not a JSON object"}
    status = str(payload.get("status") or "corrupt")
    snapshot: dict[str, object] = {
        "status": "historical" if status in {"confirmed", "accepted-with-risks"} else status,
        "stale": False,
        "alignment_id": payload.get("session_id") or payload.get("alignment_id") or "",
        "goal_fingerprint": payload.get("goal_fingerprint") or "",
        "owner_id": payload.get("owner_id", "") or "unowned",
        "updated_at": payload.get("confirmed_at", ""),
    }
    if status not in {"confirmed", "accepted-with-risks"}:
        snapshot.update({"status": "corrupt", "stale": True, "reason": "current alignment has an invalid status"})
        return snapshot
    expected_hash = str(payload.get("handoff_hash") or "")
    relative = payload.get("handoff_relative_path")
    stale_reasons: list[str] = []
    if not expected_hash or expected_hash != alignment_handoff_hash(payload):
        stale_reasons.append("handoff hash mismatch")
    if status == "accepted-with-risks" and not payload.get("accepted_risks"):
        stale_reasons.append("accepted risks are missing")
    if relative:
        path = Path(str(relative))
        if path.is_absolute() or ".." in path.parts or not (root / path).is_file():
            stale_reasons.append("handoff document path is invalid or missing")
    if stale_reasons:
        snapshot.update({"stale": True, "reason": "; ".join(stale_reasons)})
    snapshot["handoff_relative_path"] = relative
    if historical_expired:
        snapshot.update(
            {
                "ignored_expired_session_count": len(historical_expired),
                "historical_expired_sessions": historical_expired,
            }
        )
    return snapshot


def delegation_snapshot(root: Path) -> dict[str, object]:
    required = [
        ".tenetora/rules/subagent-dispatch.md",
        ".tenetora/templates/subagents/code-reviewer.spec.md",
        ".tenetora/templates/subagents/security-auditor.spec.md",
        ".tenetora/templates/subagents/codebase-scout.spec.md",
        ".tenetora/templates/subagents/implementer.spec.md",
    ]
    missing = [rel for rel in required if not (root / rel).is_file()]
    state_path = root / ".tenetora" / "state" / "delegation-state.json"
    state_status = "not-used"
    state_schema: int | None = None
    dispatch_count = 0
    latest_resolution: dict[str, object] | None = None
    if state_path.is_file():
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state_status = "corrupt"
        else:
            dispatches = payload.get("dispatches") if isinstance(payload, dict) else None
            state_schema = payload.get("version") if isinstance(payload, dict) and isinstance(payload.get("version"), int) else None
            if state_schema not in {1, 2, 3} or not isinstance(dispatches, list):
                state_status = "corrupt"
            else:
                state_status = "active" if any(
                    isinstance(item, dict) and item.get("status") == "started" for item in dispatches
                ) else "ready"
                dispatch_count = len(dispatches)
                latest = next((item for item in reversed(dispatches) if isinstance(item, dict)), None)
                if latest is not None:
                    latest_resolution = {
                        "dispatch_id": latest.get("dispatch_id"),
                        "role": latest.get("role"),
                        "status": latest.get("status"),
                        "resolution_mode": latest.get("resolution_mode", "unspecified"),
                        "host_tool": latest.get("host_tool", "unknown"),
                        "host_agent": latest.get("host_agent"),
                        "isolation_level": latest.get("isolation_level", "unknown"),
                        "contract_status": latest.get("contract_status", "unknown"),
                        "fallback_resolution_mode": latest.get("fallback_resolution_mode"),
                    }
    loop_path = root / ".tenetora" / "state" / "loop-state.json"
    review_status = "missing"
    if loop_path.is_file():
        try:
            loop = json.loads(loop_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            review_status = "corrupt"
        else:
            review = loop.get("review_cycle") if isinstance(loop, dict) else None
            review_status = str(review.get("status") or "idle") if isinstance(review, dict) else "missing"
    report_dir = root / ".tenetora" / ".cache" / "subagents"
    report_count = len(list(report_dir.glob("*.md"))) if report_dir.is_dir() else 0
    ignore_path = root / ".tenetora" / ".gitignore"
    ignore_text = ignore_path.read_text(encoding="utf-8", errors="ignore") if ignore_path.is_file() else ""
    ignored = any(
        line.strip().rstrip("/") in {".cache", ".cache/subagents"}
        for line in ignore_text.splitlines()
    )
    return {
        "infrastructure": "ready" if not missing and ignored else "incomplete",
        "missing": missing,
        "state": state_status,
        "state_schema": state_schema,
        "dispatch_count": dispatch_count,
        "latest_resolution": latest_resolution,
        "review_cycle": review_status,
        "report_cache_ignored": ignored,
        "report_count": report_count,
        "report_limit": 20,
        "spawns_agents": False,
    }


def commit_hooks_directory(root: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-path", "hooks"],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    raw = Path(result.stdout.strip())
    return raw.resolve() if raw.is_absolute() else (root / raw).resolve()


def commit_hook_active(root: Path, hooks_dir: Path, hook_name: str) -> bool:
    hook = hooks_dir / hook_name
    if not hook.is_file():
        return False
    text = hook.read_text(encoding="utf-8", errors="ignore")
    marker_lines = {line.strip() for line in text.splitlines()}
    if any(marker in marker_lines for marker in COMMIT_HOOK_MARKERS):
        state_path = root / COMMIT_HOOK_STATE_REL
        try:
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            state = {}
        command = command_from_state(state if isinstance(state, dict) else None, text)
        return bool(command and command in text and ".agent-harness/bin/agent-harness" not in text)
    config_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (
            root / ".pre-commit-config.yaml",
            root / ".pre-commit-config.tenetora.yaml",
            root / ".pre-commit-config.agent-harness.yaml",
        )
        if path.is_file()
    )
    canonical_config = "tenetora guard --action commit" in config_text
    return "pre-commit" in text and canonical_config and (
        (hook_name == "pre-commit" and "tenetora-commit-guard" in config_text)
        or (hook_name == "commit-msg" and "tenetora-commit-message" in config_text)
    )


def commit_hook_decision(root: Path) -> str:
    path = root / COMMIT_HOOK_STATE_REL
    if not path.is_file():
        return "pending"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "pending"
    decision = payload.get("decision") if isinstance(payload, dict) else None
    return str(decision) if decision in {"install", "defer", "decline", "pending"} else "pending"


def add_commit_hook_diagnostics(status: EnvironmentStatus, root: Path) -> None:
    hooks_dir = commit_hooks_directory(root)
    if hooks_dir is None or not (root / ".tenetora").is_dir():
        return
    missing = [name for name in COMMIT_HOOK_NAMES if not commit_hook_active(root, hooks_dir, name)]
    if not missing:
        return
    decision = commit_hook_decision(root)
    if decision == "decline":
        return
    if decision == "defer":
        status.issues.append("commit hook installation was deferred and will be offered again")
    elif decision == "install":
        status.issues.append(f"commit hook installation is incomplete: {', '.join(missing)}")
    else:
        status.issues.append("commit hook installation decision is pending")
    status.recommendations.extend(
        item
        for item in (
            "tenetora hooks --install",
            "tenetora hooks --defer",
            "tenetora hooks --decline",
        )
        if item not in status.recommendations
    )


def governance_activity_snapshot(root: Path, event_limit: int = 50) -> dict[str, object]:
    path = root / ".tenetora" / "state" / "governance-trail.json"
    if not path.is_file():
        return {
            "status": "not-observed",
            "events_considered": 0,
            "counts": {},
            "recent": [],
            "message": "No governance activity has been observed yet.",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "status": "invalid",
            "events_considered": 0,
            "counts": {},
            "recent": [],
            "message": "Governance activity state is unreadable.",
        }
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        return {
            "status": "invalid",
            "events_considered": 0,
            "counts": {},
            "recent": [],
            "message": "Governance activity state has an invalid events collection.",
        }
    bounded = [item for item in events[-event_limit:] if isinstance(item, dict)]
    counts = {
        "rules_loaded": sum(item.get("type") == "rules-context" for item in bounded),
        "routes": sum(item.get("type") == "route" for item in bounded),
        "guards_passed": sum(
            item.get("type") in {"guard-action", "alignment-guard"} and item.get("status") == "pass"
            for item in bounded
        ),
        "guards_warned": sum(
            item.get("type") in {"guard-action", "alignment-guard"} and item.get("status") == "warn"
            for item in bounded
        ),
        "guards_failed": sum(
            item.get("type") in {"guard-action", "alignment-guard"} and item.get("status") == "fail"
            for item in bounded
        ),
        "verification_claims": sum(item.get("type") == "verification-claim" for item in bounded),
        "external_input_checks": sum(item.get("type") == "prompt-guard" for item in bounded),
        "change_impact_preflights": sum(item.get("type") == "change-impact-preflight" for item in bounded),
    }
    recent = [
        {
            key: item[key]
            for key in ("type", "action", "status", "timestamp")
            if isinstance(item.get(key), str)
        }
        for item in bounded[-5:]
    ]
    return {
        "status": "observed" if bounded else "not-observed",
        "events_considered": len(bounded),
        "event_limit": event_limit,
        "counts": counts,
        "recent": recent,
        "message": (
            f"Observed {len(bounded)} recent governance events."
            if bounded
            else "No governance activity has been observed yet."
        ),
    }


def remediation_action(
    command: str | None,
    description: str,
    *,
    safe_to_auto_apply: bool = False,
    requires_confirmation: bool = False,
) -> dict[str, object]:
    return {
        "command": command,
        "description": description,
        "safe_to_auto_apply": safe_to_auto_apply,
        "requires_confirmation": requires_confirmation,
    }


def make_finding(
    code: str,
    severity: str,
    summary: str,
    *,
    tool: str | None = None,
    scope: str | None = None,
    evidence: list[str] | None = None,
    affects_health: bool = True,
    remediation: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "code": code,
        "severity": severity,
        "tool": tool,
        "scope": scope,
        "summary": summary,
        "evidence": evidence or [],
        "affects_health": affects_health,
        "remediation": remediation,
    }


def grouped_installs(status: EnvironmentStatus) -> dict[str, list[ToolInstall]]:
    grouped: dict[str, list[ToolInstall]] = {}
    for install in status.tools:
        grouped.setdefault(install.tool, []).append(install)
    return grouped


def relevant_tools(status: EnvironmentStatus) -> set[str]:
    selection_mode = str(status.project.get("tool_selection") or "explicit")
    grouped = grouped_installs(status)
    if selection_mode != "auto":
        return set(grouped)
    relevant = {"agents"} if "agents" in grouped else set()
    for tool, installs in grouped.items():
        if tool == "agents":
            continue
        sample = installs[0]
        effective = sample.effective_sources or {}
        runtime = effective.get("runtime") if isinstance(effective, dict) else None
        runtime_status = runtime.get("status") if isinstance(runtime, dict) else None
        observation = sample.runtime_observation or {}
        if (
            any(item.command_available for item in installs)
            or observation.get("state") not in {None, "not-observed", "unknown"}
            or runtime_status not in {None, "skills-only", "missing"}
        ):
            relevant.add(tool)
    return relevant


def install_scope_flag(scope: str) -> str:
    return {
        "global": "--global",
        "project": "--in-project",
        "both": "--both",
    }[scope]


def existing_install_update_command(tools: list[str] | tuple[str, ...] | set[str]) -> str:
    selected = ",".join(dict.fromkeys(str(tool) for tool in tools if str(tool)))
    return f"tenetora update --tools {selected or 'auto'} --force"


def lifecycle_skill_package_state(install: ToolInstall) -> str:
    skills = install.skills or []
    if not skills:
        return install.state
    states = {skill.state for skill in skills}
    for state in ("conflict", "missing", "stale"):
        if state in states:
            return state
    return "ok"


def affected_skill_package_scope(installs: list[ToolInstall], states: set[str]) -> str:
    scopes = {
        item.scope
        for item in installs
        if lifecycle_skill_package_state(item) in states
    }
    return "both" if scopes == {"global", "project"} else next(iter(scopes))


def command_path_argument(path: str) -> str:
    return subprocess.list2cmdline([path]) if os.name == "nt" else shlex.quote(path)


def core_findings(status: EnvironmentStatus) -> list[dict[str, object]]:
    root = str(status.project["root"])
    root_arg = command_path_argument(root)
    scope = str(status.project.get("inspection_scope") or "global")
    relevant = relevant_tools(status)
    findings: list[dict[str, object]] = []
    if status.activity.get("status") == "invalid":
        findings.append(
            make_finding(
                "governance-activity-invalid",
                "attention",
                "Governance activity state is unreadable or invalid.",
                evidence=[str(status.activity.get("message") or "invalid governance activity state")],
                remediation=remediation_action(
                    f"tenetora doctor --path {root_arg}",
                    "Inspect the governance trail before relying on recent activity summaries.",
                    safe_to_auto_apply=True,
                ),
            )
        )
    harness = status.project.get("harness")
    if isinstance(harness, dict) and not harness.get("exists"):
        findings.append(
            make_finding(
                "harness-missing",
                "blocking",
                harness_missing_message(root),
                evidence=[".tenetora directory not found"],
                remediation=remediation_action(
                    None,
                    harness_missing_message(root),
                    requires_confirmation=True,
                ),
            )
        )

    for tool, installs in grouped_installs(status).items():
        sample = installs[0]
        is_relevant = tool in relevant
        effective = sample.effective_sources or {}
        runtime = effective.get("runtime") if isinstance(effective, dict) else None
        skill = effective.get("skill") if isinstance(effective, dict) else None
        effective_skill = skill.get("effective") if isinstance(skill, dict) else None
        runtime_status = str(runtime.get("status") or "unknown") if isinstance(runtime, dict) else "unknown"
        observation = sample.runtime_observation or {}
        alignment = sample.version_alignment or {}
        evidence = [
            f"runtime={runtime_status}",
            f"observation={observation.get('state', 'not-observed')}",
            f"versions={alignment.get('status', 'unknown')}",
        ]
        if runtime_status == "duplicate-hook-runtime":
            findings.append(
                make_finding(
                    "duplicate-hook-runtime",
                    "blocking" if is_relevant else "attention",
                    f"{tool} has more than one executable Tenetora runtime for this project.",
                    tool=tool,
                    scope="both",
                    evidence=evidence,
                    affects_health=is_relevant,
                    remediation=remediation_action(
                        f"tenetora install --path {root_arg} --tools {tool} --both --prune-shadowed --force",
                        "Keep one managed runtime after ownership and content verification.",
                        requires_confirmation=True,
                    ),
                )
            )
        elif runtime_status in {"configured-observation-stale", "configured-not-observed", "configured-not-verified"}:
            findings.append(
                make_finding(
                    "runtime-observation-stale" if runtime_status == "configured-observation-stale" else "runtime-not-observed",
                    "attention",
                    f"{tool} is configured but lacks matching recent execution evidence.",
                    tool=tool,
                    scope=scope,
                    evidence=evidence,
                    affects_health=is_relevant,
                    remediation=remediation_action(
                        f"tenetora doctor --tools {tool} --path {root_arg}",
                        f"Restart or reload {tool}, trigger one governed session, then verify runtime evidence.",
                        safe_to_auto_apply=True,
                    ),
                )
            )
        elif runtime_status == "active-observed-config-unverified":
            findings.append(
                make_finding(
                    "runtime-config-unverified",
                    "attention",
                    f"{tool} executed recently, but the host configuration inventory is unavailable.",
                    tool=tool,
                    scope=scope,
                    evidence=evidence,
                    affects_health=is_relevant,
                    remediation=remediation_action(
                        f"tenetora doctor --tools {tool} --path {root_arg}",
                        "Restore the host runtime inventory before changing runtime ownership or trust.",
                        safe_to_auto_apply=True,
                    ),
                )
            )
        elif runtime_status in {
            "configured-observation-invalid",
            "configured-observation-mismatch",
            "configured-inactive",
            "observed-source-mismatch",
            "observed-conflict",
        }:
            observation_mismatch = runtime_status == "configured-observation-mismatch"
            findings.append(
                make_finding(
                    (
                        "runtime-observation-invalid"
                        if runtime_status == "configured-observation-invalid"
                        else "runtime-observation-mismatch"
                        if observation_mismatch
                        else runtime_status
                    ),
                    "attention"
                    if runtime_status in {"configured-observation-invalid", "configured-observation-mismatch"}
                    else "blocking",
                    (
                        f"{tool} is configured, but its latest runtime observation came from a different source."
                        if observation_mismatch
                        else f"{tool} runtime configuration is not safely active."
                    ),
                    tool=tool,
                    scope=scope,
                    evidence=evidence,
                    affects_health=is_relevant,
                    remediation=remediation_action(
                        f"tenetora doctor --tools {tool} --path {root_arg}",
                        (
                            f"Restart or reload {tool}, trigger one governed session, then verify the current runtime source."
                            if observation_mismatch
                            else "Inspect the configured and observed runtime sources before changing them."
                        ),
                        safe_to_auto_apply=True,
                    ),
                )
            )

        effective_skill_relation = (
            version_relation(effective_skill.get("version"))
            if isinstance(effective_skill, dict)
            else "unknown"
        )
        if isinstance(effective_skill, dict) and effective_skill_relation not in {"current", "unknown"}:
            effective_kind = str(effective_skill.get("kind") or "unknown")
            repair_tools = [tool]
            if effective_kind.startswith("agents-") and tool != "agents":
                repair_tools.append("agents")
            effective_evidence = [
                *evidence,
                f"effective={effective_kind}@{effective_skill.get('version') or 'unknown'}",
                f"effective-path={effective_skill.get('path') or 'unknown'}",
                f"expected={alignment.get('expected_version') or __version__}",
            ]
            findings.append(
                make_finding(
                    "effective-skill-version-drift",
                    "blocking" if is_relevant else "attention",
                    f"{tool} resolves an older Tenetora skill before newer installed copies.",
                    tool=tool,
                    scope=scope,
                    evidence=effective_evidence,
                    affects_health=is_relevant,
                    remediation=remediation_action(
                        existing_install_update_command(repair_tools),
                        "Update every registered global and project installation surface that participates in host discovery without creating new surfaces.",
                        requires_confirmation=True,
                    ),
                )
            )
        elif alignment.get("status") == "drift":
            findings.append(
                make_finding(
                    "version-drift",
                    "attention",
                    f"{tool} Tenetora copies do not use one version.",
                    tool=tool,
                    scope=scope,
                    evidence=evidence,
                    affects_health=is_relevant,
                    remediation=remediation_action(
                        existing_install_update_command([tool]),
                        "Update all registered copies of this tool to the current package version without creating a new installation surface.",
                        requires_confirmation=True,
                    ),
                )
            )

        global_install = next((item for item in installs if item.scope == "global"), None)
        if global_install is not None and tool in NATIVE_PLUGIN_TARGETS:
            codex_recovery_available = False
            if global_install.registration_state == "state-unreadable":
                recovery = assess_legacy_registration() if tool == "codex" else None
                codex_recovery_available = bool(recovery is not None and recovery.recoverable)
                recovery_command = (
                    "tenetora repair --fix codex-legacy-registration --apply"
                    if codex_recovery_available
                    else None
                )
                recovery_description = (
                    "Back up Codex config, remove only the proven legacy Agent Harness registration, install the canonical Tenetora plugin, and request fresh Hook trust."
                    if recovery_command
                    else f"Repair the {tool} marketplace configuration before attempting native plugin changes."
                )
                findings.append(
                    make_finding(
                        "marketplace-state",
                        "attention",
                        f"{tool} native plugin inventory is unreadable, so Tenetora cannot safely restore or remove the native plugin.",
                        tool=tool,
                        scope="global",
                        evidence=[
                            f"registration={global_install.registration_state}",
                            f"plugin={global_install.plugin_state or 'unknown'}",
                            f"hook-mode={global_install.hook_mode or 'unknown'}",
                        ],
                        affects_health=is_relevant,
                        remediation=remediation_action(
                            recovery_command,
                            recovery_description,
                            requires_confirmation=True,
                        ),
                    )
                )
            codex_native_hook_blocks_fallback = bool(
                tool == "codex"
                and global_install.hook_mode == "native-plugin"
                and global_install.hooks_state == "native-hook-blocks-fallback"
            )
            if tool == "codex" and global_install.hooks_state == "legacy-native-hook-active":
                findings.append(
                    make_finding(
                        "legacy-native-hook-active",
                        "blocking" if is_relevant else "attention",
                        "codex legacy Agent Harness native hooks are enabled and are not a valid Tenetora runtime.",
                        tool=tool,
                        scope="global",
                        evidence=[
                            f"registration={global_install.registration_state or 'unknown'}",
                            f"hooks={global_install.hooks_state}",
                        ],
                        affects_health=is_relevant,
                        remediation=remediation_action(
                            "tenetora repair --fix codex-legacy-registration --check",
                            "Run tenetora repair --fix codex-legacy-registration --check to review ownership evidence, then apply only the proven recovery. If ownership is not proven, disable the legacy hooks in Codex /hooks manually. A fresh Hook trust review is required after canonical installation.",
                            requires_confirmation=True,
                        ),
                    )
                )
            if codex_native_hook_blocks_fallback:
                fallback_command = (
                    f"tenetora install --path {root_arg} --tools codex "
                    "--codex-hooks project --force"
                )
                findings.append(
                    make_finding(
                        "hook-launcher-unhealthy",
                        "blocking" if is_relevant else "attention",
                        "codex still reports enabled Tenetora or legacy Agent Harness native hooks, but their launcher is unhealthy.",
                        tool=tool,
                        scope="global",
                        evidence=[
                            f"registration={global_install.registration_state or 'unknown'}",
                            f"hook-mode={global_install.hook_mode or 'unknown'}",
                            f"hooks={global_install.hooks_state or 'unknown'}",
                        ],
                        affects_health=is_relevant,
                        remediation=remediation_action(
                            None,
                            "Disable the enabled Tenetora or legacy Agent Harness native hooks in Codex /hooks first, "
                            f"then run {fallback_command}. Tenetora will not install a second runtime while the host still enables the stale source.",
                            requires_confirmation=True,
                        ),
                    )
                )
            has_lifecycle_skills = any(
                lifecycle_skill_package_state(item) in {"ok", "stale"}
                for item in installs
            )
            runtime_covered_by_codex_hooks = bool(
                tool == "codex"
                and global_install.hook_mode in {"native-plugin", "project-fallback"}
                and global_install.runtime_state in {"active", "needs-trust-review"}
            )
            if (
                global_install.plugin_state == "missing"
                and has_lifecycle_skills
                and not runtime_covered_by_codex_hooks
                and not codex_native_hook_blocks_fallback
                and global_install.hooks_state != "legacy-native-hook-active"
                and not codex_recovery_available
            ):
                if (
                    tool == "codex"
                    and global_install.registration_state == "state-unreadable"
                    and global_install.hook_mode == "skills-only"
                    and global_install.hooks_state == "inactive"
                ):
                    command = (
                        f"tenetora install --path {root_arg} --tools codex "
                        "--codex-hooks project --force"
                    )
                    description = (
                        "The Codex native plugin cannot be restored through the host CLI while its marketplace inventory is unreadable. "
                        "Install the managed project fallback now; repair Codex and reinstall the native plugin later."
                    )
                else:
                    command = f"tenetora install --global --tools {tool} --force"
                    description = "Restore the missing native plugin and its runtime hooks."
                findings.append(
                    make_finding(
                        "native-plugin-missing",
                        "blocking" if is_relevant else "attention",
                        f"{tool} lifecycle skills are installed but its native plugin and runtime hooks are missing.",
                        tool=tool,
                        scope="global",
                        evidence=[
                            f"registration={global_install.registration_state or 'unknown'}",
                            f"hook-mode={global_install.hook_mode or 'unknown'}",
                            f"hooks={global_install.hooks_state or 'unknown'}",
                        ],
                        affects_health=is_relevant,
                        remediation=remediation_action(
                            command,
                            description,
                            requires_confirmation=True,
                        ),
                    )
                )

        skill_package_states = {lifecycle_skill_package_state(item) for item in installs}
        missing = [
            item
            for item in installs
            if lifecycle_skill_package_state(item) == "missing"
        ]
        if missing:
            target_scope = affected_skill_package_scope(installs, {"missing"})
            findings.append(
                make_finding(
                    "skill-package-missing",
                    "blocking" if len(missing) == len(installs) else "attention",
                    f"{tool} does not have a complete Tenetora lifecycle skill package in the requested scope.",
                    tool=tool,
                    scope=target_scope,
                    evidence=[f"missing-scopes={','.join(sorted(item.scope for item in missing))}"],
                    affects_health=is_relevant,
                    remediation=remediation_action(
                        f"tenetora install --path {root_arg} --tools {tool} {install_scope_flag(target_scope)}",
                        "Install the lifecycle skill package only in the requested missing scope.",
                        requires_confirmation=True,
                    ),
                )
            )
        if "conflict" in skill_package_states or "stale" in skill_package_states:
            state = "conflict" if "conflict" in skill_package_states else "stale"
            target_scope = affected_skill_package_scope(installs, {state})
            findings.append(
                make_finding(
                    f"skill-package-{state}",
                    "blocking" if state == "conflict" else "attention",
                    f"{tool} lifecycle skill package is {state}.",
                    tool=tool,
                    scope=target_scope,
                    evidence=[
                        f"{state}-scopes={','.join(sorted(item.scope for item in installs if lifecycle_skill_package_state(item) == state))}"
                    ],
                    affects_health=is_relevant,
                    remediation=remediation_action(
                        f"tenetora install --path {root_arg} --tools {tool} {install_scope_flag(target_scope)} --force",
                        "Replace only the selected managed lifecycle package.",
                        requires_confirmation=True,
                    ),
                )
            )
    return findings


def issue_code(issue: str) -> str:
    lowered = issue.lower()
    if "repository-unit" in lowered:
        return "repository-unit-state"
    skill_state_codes = {
        "lifecycle skill package is stale": "skill-package-stale",
        "lifecycle skill package has a conflict": "skill-package-conflict",
        "lifecycle skill package is incomplete": "skill-package-missing",
    }
    for marker, code in skill_state_codes.items():
        if marker in lowered:
            return code
    patterns = (
        ("legacy agent harness native hook", "legacy-native-hook-active"),
        ("require explicit trust", "plugin-trust"),
        ("native plugin state is unreadable", "marketplace-state"),
        ("native plugin inventory is unreadable", "marketplace-state"),
        ("native plugin is missing", "native-plugin-missing"),
        ("lifecycle skills are installed but the native plugin is missing", "native-plugin-missing"),
        ("runtime observation does not match", "runtime-observation-mismatch"),
        ("duplicate", "duplicate-hook-runtime"),
        ("version drift", "version-drift"),
        ("configured-observation-invalid", "runtime-observation-invalid"),
        ("configured-not-observed", "runtime-not-observed"),
        ("lacks matching recent execution", "runtime-observation-stale"),
        ("hooks/list inventory is unavailable", "runtime-config-unverified"),
        ("marketplace", "marketplace-state"),
        ("activation", "plugin-activation"),
        ("launcher", "hook-launcher-unhealthy"),
        ("commit hook", "commit-hook-state"),
        ("alignment", "alignment-state"),
        ("delegation", "delegation-state"),
        (".tenetora is missing", "harness-missing"),
    )
    for marker, code in patterns:
        if marker in lowered:
            return code
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return (slug[:72] or "environment-diagnostic")


def issue_severity(issue: str) -> str:
    lowered = issue.lower()
    if any(marker in lowered for marker in ("conflict", "duplicate", "missing", "invalid", "corrupt", "unhealthy", "incomplete")):
        return "blocking"
    return "attention"


def repository_unit_issue_severity(status: EnvironmentStatus) -> str:
    """Keep passive repository-unit drift separate from a pending commit risk."""
    snapshot = status.project.get("repository_units")
    if not isinstance(snapshot, dict):
        return "blocking"
    if snapshot.get("status") == "unavailable":
        return "blocking"
    if snapshot.get("staged_status") in {"changed", "conflicted"}:
        return "blocking"
    problems = snapshot.get("problems")
    if isinstance(problems, list) and any(
        any(marker in str(problem).lower() for marker in ("invalid", "conflict", "symlink", "unreachable"))
        for problem in problems
    ):
        return "blocking"
    return "attention"


def issue_tool(issue: str) -> str | None:
    first = issue.split(maxsplit=1)[0].lower() if issue else ""
    return first if first in SUPPORTED_TOOLS else None


def diagnostic_findings(status: EnvironmentStatus, existing: list[dict[str, object]]) -> list[dict[str, object]]:
    root = str(status.project["root"])
    root_arg = command_path_argument(root)
    relevant = relevant_tools(status)
    findings = list(existing)
    keys = {(str(item.get("code")), item.get("tool")) for item in findings}
    for issue in status.issues:
        code = issue_code(issue)
        tool = issue_tool(issue)
        key = (code, tool)
        if key in keys:
            continue
        def recommendation_matches(item: str) -> bool:
            lowered = item.lower()
            if code == "harness-missing":
                return "tenetora" in lowered and ("initialize" in lowered or "--path" in lowered)
            if code == "commit-hook-state":
                return "tenetora hooks" in lowered
            if code == "alignment-state":
                return "alignment" in lowered
            if code == "delegation-state":
                return "delegation" in lowered
            if code == "repository-unit-state":
                return "repository-units" in lowered or "repository unit" in lowered
            if code == "marketplace-state":
                return bool(tool and tool in lowered and "marketplace" in lowered)
            if code == "plugin-trust":
                return bool(tool and tool in lowered and ("restart" in lowered or "trust" in lowered))
            if code == "native-plugin-missing":
                return bool(tool and tool in lowered and "install" in lowered)
            if code == "runtime-observation-mismatch":
                return bool(tool and tool in lowered and "doctor" in lowered)
            return bool(tool and tool in lowered)

        matching = next((item for item in status.recommendations if recommendation_matches(item)), None)
        matching_command = bool(matching and matching.startswith("tenetora "))
        severity = (
            repository_unit_issue_severity(status)
            if code == "repository-unit-state"
            else issue_severity(issue)
        )
        command = matching if matching_command else (
            f"tenetora doctor --tools {tool} --path {root_arg}"
            if matching is None and tool
            else f"tenetora doctor --path {root_arg}"
            if matching is None
            else None
        )
        findings.append(
            make_finding(
                code,
                severity,
                issue,
                tool=tool,
                evidence=[issue],
                affects_health=tool in relevant if tool else True,
                remediation=remediation_action(
                    command,
                    matching or "Run the focused diagnostic and review its evidence before applying changes.",
                    safe_to_auto_apply=bool(command and command.startswith("tenetora doctor")),
                    requires_confirmation=not bool(command and command.startswith("tenetora doctor")),
                ),
            )
        )
        keys.add(key)
    return findings


def health_summary(status: EnvironmentStatus, findings: list[dict[str, object]]) -> dict[str, object]:
    relevant = relevant_tools(status)
    actionable = [
        item
        for item in findings
        if item.get("affects_health") is True
        and not (
            item.get("acknowledged") is True
            and item.get("severity") == "attention"
        )
    ]
    if any(item.get("severity") == "blocking" for item in actionable):
        state = "BLOCKED"
    elif any(item.get("severity") == "attention" for item in actionable):
        state = "ATTENTION"
    else:
        state = "HEALTHY"
    active: list[str] = []
    for tool, installs in grouped_installs(status).items():
        if tool not in relevant:
            continue
        sample = installs[0]
        runtime = (sample.effective_sources or {}).get("runtime", {})
        if tool == "agents" and any(item.state == "ok" for item in installs):
            active.append("agents:skills-only")
        elif isinstance(runtime, dict) and (
            runtime.get("status") == "active"
            or runtime.get("configured_status") == "active"
            or any(item.runtime_state in {"active", "active-partial"} for item in installs)
        ):
            active.append(tool)
    next_actions = []
    for finding in actionable:
        remediation = finding.get("remediation")
        if not isinstance(remediation, dict):
            continue
        action = remediation.get("command") or remediation.get("description")
        if isinstance(action, str) and action not in next_actions:
            next_actions.append(action)
        if len(next_actions) == 3:
            break
    summary = {
        "HEALTHY": "Tenetora governance is healthy for the relevant platforms.",
        "ATTENTION": f"Tenetora needs attention for {sum(item.get('severity') == 'attention' for item in actionable)} finding(s).",
        "BLOCKED": f"Tenetora governance is blocked by {sum(item.get('severity') == 'blocking' for item in actionable)} finding(s).",
    }[state]
    return {
        "status": state,
        "summary": summary,
        "relevant_platforms": sorted(relevant),
        "active_platforms": active,
        "actionable_findings": len(actionable),
        "dormant_findings": len(findings) - len(actionable),
        "acknowledged_findings": sum(item.get("acknowledged") is True for item in findings),
        "acknowledgement_state": getattr(status, "acknowledgement_state", None),
        "next_actions": next_actions,
    }


def finalize_status_translation(status: EnvironmentStatus, include_diagnostics: bool) -> None:
    findings = core_findings(status)
    if include_diagnostics:
        findings = diagnostic_findings(status, findings)
    acknowledgement_state = apply_acknowledgements(Path(str(status.project["root"])), findings)
    if acknowledgement_state["status"] == "invalid":
        findings.append(
            make_finding(
                "finding-acknowledgement-state-invalid",
                "attention",
                "Finding acknowledgement state is invalid and has been ignored.",
                evidence=["acknowledgement state failed validation"],
                remediation=remediation_action(
                    "tenetora doctor",
                    "Inspect the acknowledgement state before suppressing any finding.",
                    safe_to_auto_apply=False,
                ),
            )
        )
        acknowledgement_state["status"] = "invalid"
    status.acknowledgement_state = acknowledgement_state
    findings[:] = [
        finding if finding.get("finding_id") else attach_finding_identity(finding)
        for finding in findings
    ]
    severity_order = {"blocking": 0, "attention": 1, "info": 2}
    status.findings = sorted(
        findings,
        key=lambda item: (severity_order.get(str(item.get("severity")), 9), str(item.get("tool") or ""), str(item.get("code"))),
    )
    status.health = health_summary(status, status.findings)


def collect_status(
    root: Path,
    tools: list[str],
    scope: str,
    include_diagnostics: bool = False,
    tool_selection: str = "explicit",
) -> EnvironmentStatus:
    project_root = root.resolve()
    selected_tools = list(tools)
    requested_pairs = [
        (tool, selected_scope)
        for selected_scope in selected_scopes(scope)
        for tool in selected_tools
    ]
    inventory = {
        (tool, selected_scope): describe_install(tool, selected_scope, project_root)
        for tool in selected_tools
        for selected_scope in ("global", "project")
    }
    installs = [inventory[pair] for pair in requested_pairs]
    apply_effective_source_model(installs, inventory, project_root)
    for install in installs:
        install.capability_contract = dict(platform_contract(install.tool))
    harness = project_root / ".tenetora"
    status = EnvironmentStatus(
        repo={
            "root": str(REPO_ROOT),
            "skill": str(SKILL_SOURCE),
            "version": __version__,
        },
        project={
            "root": str(project_root),
            "tool_selection": tool_selection,
            "inspection_scope": scope,
            "local_environment": local_environment_snapshot(project_root),
            "repository_units": repository_units_snapshot(project_root),
            "harness": {
                "exists": harness.exists(),
                "ignored": harness_ignored(project_root),
                "alignment": alignment_snapshot(project_root) if harness.exists() else {"status": "not-used", "stale": False},
                "delegation": delegation_snapshot(project_root) if harness.exists() else {"infrastructure": "missing", "spawns_agents": False},
            },
        },
        tools=installs,
        issues=[],
        recommendations=[],
        findings=[],
        health={},
        activity=governance_activity_snapshot(project_root),
    )
    if include_diagnostics:
        add_diagnostics(status)
    finalize_status_translation(status, include_diagnostics)
    return status


def add_diagnostics(status: EnvironmentStatus) -> None:
    project_root = str(status.project["root"])
    root = Path(project_root)
    repository_units = status.project.get("repository_units")
    if isinstance(repository_units, dict):
        repository_problems = repository_units.get("problems")
        if repository_units.get("status") == "unavailable" or (
            repository_units.get("status") == "attention" and repository_problems
        ):
            status.issues.append("repository-unit registration or verification evidence is missing or invalid")
            status.recommendations.append(
                f"tenetora repository-units inspect --path {command_path_argument(project_root)} --json"
            )
    harness = status.project["harness"]
    local_environment = status.project.get("local_environment")
    if isinstance(local_environment, dict):
        local_status = str(local_environment.get("status") or "")
        missing_paths = local_environment.get("missing_paths")
        if local_status == "invalid":
            status.issues.append("local environment registry is unreadable or invalid")
            status.recommendations.append(
                f"tenetora local-env --path {command_path_argument(project_root)} --list --json"
            )
        elif local_status == "missing" and isinstance(missing_paths, list):
            for path in missing_paths:
                status.issues.append(f"registered local environment path is missing: {path}")
            status.recommendations.append(
                f"tenetora checkpoint --list --path {command_path_argument(project_root)}"
            )
    if isinstance(harness, dict) and not harness.get("exists"):
        status.issues.append(".tenetora is missing")
        status.recommendations.append(
            harness_missing_message(project_root)
        )
    elif isinstance(harness, dict):
        required_harness_files = [
            ".tenetora/README.md",
            ".tenetora/wiki/project-map.md",
            ".tenetora/wiki/technology.md",
            ".tenetora/wiki/architecture.md",
            ".tenetora/workflows/verification.md",
            ".tenetora/workflows/decision-alignment.md",
            ".tenetora/skills/decision-interview.md",
            ".tenetora/docs/architecture/overview.md",
            ".tenetora/docs/conventions/README.md",
            ".tenetora/guardrails/quality-gates.md",
            ".tenetora/guardrails/ci.md",
            ".tenetora/state/features.json",
            ".tenetora/automation/worktree-verify.sh",
            ".tenetora/templates/feature-design.md",
            ".tenetora/templates/implementation-plan.md",
            ".tenetora/templates/alignment-handoff.md",
            ".tenetora/rules/subagent-dispatch.md",
            ".tenetora/templates/subagents/code-reviewer.spec.md",
            ".tenetora/templates/subagents/security-auditor.spec.md",
            ".tenetora/templates/subagents/codebase-scout.spec.md",
            ".tenetora/templates/subagents/implementer.spec.md",
        ]
        missing = [rel for rel in required_harness_files if not (root / rel).exists()]
        for rel in missing:
            status.issues.append(f"missing {rel}")
        if not list((root / ".tenetora" / "changes").glob("*-extraction-evidence.json")):
            status.issues.append("missing .tenetora/changes/*-extraction-evidence.json")
        if missing or "missing .tenetora/changes/*-extraction-evidence.json" in status.issues:
            status.recommendations.append(
                "In your AI conversation, ask: Use Tenetora to update or repair this project's governance."
            )

        alignment = harness.get("alignment")
        if isinstance(alignment, dict):
            alignment_status = str(alignment.get("status") or "not-used")
            if alignment_status == "conflict":
                status.issues.append(
                    f"decision alignment has {alignment.get('active_session_count', 0)} active session(s); explicit session identity is required"
                )
                status.recommendations.append("tenetora alignment --status --json or tenetora alignment --list --json")
            elif alignment_status == "stale":
                status.issues.append(
                    f"decision alignment has {alignment.get('expired_session_count', 0)} expired session(s); they cannot be resumed"
                )
                status.recommendations.append("tenetora alignment --list --json and start a new isolated session if alignment is still needed")
            elif alignment_status == "legacy-unowned":
                status.issues.append("decision alignment has an unowned legacy session; it cannot be inherited")
                status.recommendations.append("start a new isolated alignment and review the legacy state separately")
            elif alignment_status in {"active", "checkpoint", "paused", "blocked", "awaiting-confirmation"}:
                status.issues.append(f"decision alignment session is {alignment_status}")
                command = "tenetora alignment --status"
                if alignment_status in {"checkpoint", "paused", "blocked", "awaiting-confirmation"}:
                    command = "tenetora alignment --resume"
                status.recommendations.append(command)
            elif alignment_status == "corrupt" or alignment.get("stale"):
                status.issues.append(f"decision alignment state is stale or corrupt: {alignment.get('reason', 'unknown reason')}")
                status.recommendations.append("use tenetora-align to realign the current goal")

        delegation = harness.get("delegation")
        if isinstance(delegation, dict):
            if delegation.get("infrastructure") != "ready":
                status.issues.append("subagent delegation governance is incomplete")
                status.recommendations.append("tenetora repair --apply --fix subagent-governance")
            if delegation.get("state") == "corrupt" or delegation.get("review_cycle") == "corrupt":
                status.issues.append("subagent delegation or review-cycle state is corrupt")
                status.recommendations.append("inspect tenetora delegation --status before continuing a review loop")
            if int(delegation.get("report_count", 0)) > int(delegation.get("report_limit", 20)):
                status.issues.append("subagent report cache exceeds its bounded limit")
                status.recommendations.append("tenetora delegation --cleanup")

    add_commit_hook_diagnostics(status, root)

    for tool in status.tools:
        effective = tool.effective_sources or {}
        runtime = effective.get("runtime") if isinstance(effective, dict) else None
        observation = tool.runtime_observation or {}
        alignment = tool.version_alignment or {}
        if isinstance(runtime, dict) and runtime.get("status") == "duplicate-hook-runtime":
            issue = f"{tool.tool} has duplicate Tenetora hook runtimes for this project"
            if issue not in status.issues:
                status.issues.append(issue)
            remediation = runtime.get("remediation")
            if isinstance(remediation, str) and remediation not in status.recommendations:
                status.recommendations.append(remediation)
        if observation.get("state") in {"observed-source-mismatch", "observed-conflict"}:
            issue = f"{tool.tool} runtime observation does not match the configured effective source"
            if issue not in status.issues:
                status.issues.append(issue)
            recommendation = f"tenetora doctor --tools {tool.tool} --path {project_root}"
            if recommendation not in status.recommendations:
                status.recommendations.append(recommendation)
        runtime_status = runtime.get("status") if isinstance(runtime, dict) else None
        if runtime_status in {
            "configured-not-observed",
            "configured-observation-stale",
            "configured-observation-invalid",
        }:
            issue = f"{tool.tool} hook runtime is configured but lacks matching recent execution evidence ({runtime_status})"
            if issue not in status.issues:
                status.issues.append(issue)
            remediation = runtime.get("remediation") if isinstance(runtime, dict) else None
            if isinstance(remediation, str) and remediation not in status.recommendations:
                status.recommendations.append(remediation)
        if alignment.get("status") == "drift":
            issue = f"{tool.tool} Tenetora copies have version drift"
            if issue not in status.issues:
                status.issues.append(issue)
            recommendation = existing_install_update_command([tool.tool])
            if recommendation not in status.recommendations:
                status.recommendations.append(recommendation)
        runtime_diagnostic_handled = bool(
            isinstance(runtime, dict) and runtime.get("status") == "duplicate-hook-runtime"
        )
        runtime_execution_observed = bool(
            isinstance(runtime, dict)
            and runtime.get("status") in {"active", "active-observed-config-unverified"}
            and observation.get("state") == "recent"
        )
        if tool.tool in NATIVE_PLUGIN_TARGETS and tool.scope == "global":
            flag = "-g"
            repair_command = f"tenetora install {flag} --tools {tool.tool} --force"
            has_fallback = any(skill.state in {"ok", "stale", "conflict"} for skill in (tool.skills or [])) or legacy_skill_fallback_present(
                tool.tool, tool.scope, project_root
            )
            if tool.registration_state == "state-unreadable":
                status.issues.append(f"{tool.tool} native plugin state is unreadable")
                status.recommendations.append(
                    f"repair {tool.tool} marketplace configuration, then rerun {repair_command}"
                )
                runtime_diagnostic_handled = True
            if tool.tool == "codex" and tool.hooks_state == "legacy-native-hook-active":
                status.issues.append(
                    "codex legacy Agent Harness native hooks are still enabled and are not a valid Tenetora runtime"
                )
                recovery_command = "tenetora repair --fix codex-legacy-registration --apply"
                if recovery_command not in status.recommendations:
                    status.recommendations.append(recovery_command)
                runtime_diagnostic_handled = True
            elif tool.registration_state in {"missing", "invalid", "cli-error"}:
                status.issues.append(f"{tool.tool} native plugin registration is {tool.registration_state}")
                status.recommendations.append(repair_command)
                runtime_diagnostic_handled = True
            elif tool.registration_state in {"cli-unavailable", "cli-unsupported", "cli-disabled"}:
                status.issues.append(
                    f"{tool.tool} lifecycle skills are available, but its native plugin CLI is unavailable; runtime hooks are inactive"
                )
                runtime_diagnostic_handled = True
            if (
                tool.plugin_state == "missing"
                and has_fallback
                and not (tool.tool == "codex" and tool.hook_mode in {"native-plugin", "project-fallback"})
            ):
                status.issues.append(
                    f"{tool.tool} lifecycle skills are installed but the native plugin is missing; runtime hooks are inactive"
                )
                if repair_command not in status.recommendations:
                    status.recommendations.append(repair_command)
                runtime_diagnostic_handled = True
            elif tool.plugin_state == "stale":
                status.issues.append(f"{tool.tool} native plugin is stale")
                if repair_command not in status.recommendations:
                    status.recommendations.append(repair_command)
                runtime_diagnostic_handled = True
            elif tool.plugin_state == "conflict":
                if tool.runtime_state == "legacy-active" or tool.registration_state == "legacy-present":
                    status.issues.append(
                        f"{tool.tool} legacy Agent Harness native plugin is still registered and must be replaced by tenetora@tenetora-local"
                    )
                else:
                    status.issues.append(f"{tool.tool} native plugin has load errors or a conflicting payload")
                if repair_command not in status.recommendations:
                    status.recommendations.append(repair_command)
                runtime_diagnostic_handled = True
            if tool.registration_state == "present" and tool.hooks_state != "present" and tool.tool != "codex":
                status.issues.append(
                    f"{tool.tool} native plugin is installed without valid executable runtime hooks"
                )
                if repair_command not in status.recommendations:
                    status.recommendations.append(repair_command)
                runtime_diagnostic_handled = True
            if (
                tool.plugin_state == "ok"
                and tool.activation_state != "enabled"
                and not runtime_execution_observed
            ):
                status.issues.append(f"{tool.tool} native plugin activation is {tool.activation_state}")
                if repair_command not in status.recommendations:
                    status.recommendations.append(repair_command)
                runtime_diagnostic_handled = True
            if tool.tool == "codex":
                if tool.hooks_state == "duplicate-hook-runtime":
                    status.issues.append("codex native and project Tenetora hooks are both enabled")
                    runtime_diagnostic_handled = True
                elif tool.hooks_state == "hook-inventory-unavailable":
                    status.issues.append("codex hooks/list inventory is unavailable; active hook sources cannot be verified")
                    status.recommendations.append(
                        "restore Codex app-server hooks/list before enabling or changing project fallback"
                    )
                    runtime_diagnostic_handled = True
                elif tool.hooks_state == "native-hook-blocks-fallback":
                    status.issues.append("codex enabled native Tenetora hooks have an unhealthy launcher")
                    status.recommendations.append(
                        "disable the existing Tenetora native hooks in Codex /hooks, then run tenetora install --path . --tools codex --codex-hooks project"
                    )
                    runtime_diagnostic_handled = True
                elif tool.hooks_state == "legacy-native-hook-active":
                    status.issues.append(
                        "codex legacy Agent Harness native hooks are enabled and block a canonical Tenetora runtime"
                    )
                    status.recommendations.append(
                        "run tenetora repair --fix codex-legacy-registration --check, then apply only proven ownership recovery"
                    )
                    runtime_diagnostic_handled = True
                elif (
                    tool.hooks_state in {"pending-native-trust", "pending-project-trust"}
                    and not runtime_execution_observed
                ):
                    status.issues.append(
                        "codex native plugin hooks require explicit trust review before they execute"
                        if tool.hooks_state == "pending-native-trust"
                        else "codex project fallback hooks require explicit trust review before they execute"
                    )
                    status.recommendations.append("restart Codex and review Tenetora hooks with /hooks")
                elif tool.hooks_state == "project-fallback-runtime-unhealthy":
                    status.issues.append("codex project fallback is configured but its stable hook runtime is unhealthy")
                    status.recommendations.append(
                        "rerun the Tenetora installer, then verify tenetora doctor --tools codex"
                    )
                    runtime_diagnostic_handled = True

        if tool.tool == "zcode" and tool.scope == "global":
            has_fallback = any(skill.state in {"ok", "stale", "conflict"} for skill in (tool.skills or [])) or legacy_skill_fallback_present(
                tool.tool, tool.scope, project_root
            )
            zcode_flag = "-g" if tool.scope == "global" else "-i"
            repair_command = f"tenetora install {zcode_flag} --tools zcode --force"
            if project_legacy_zcode_plugin_is_managed(project_root):
                status.issues.append(
                    "zcode project-level legacy Agent Harness plugin may shadow the current native plugin"
                )
                if repair_command not in status.recommendations:
                    status.recommendations.append(repair_command)
                runtime_diagnostic_handled = True
            if tool.registration_state in {"missing", "invalid"}:
                status.issues.append(f"zcode native plugin registration is {tool.registration_state}")
                if repair_command not in status.recommendations:
                    status.recommendations.append(repair_command)
                runtime_diagnostic_handled = True
            if tool.plugin_state == "missing" and has_fallback:
                status.issues.append("zcode legacy skill fallback is installed but the native plugin is missing; hooks may not be active")
                if repair_command not in status.recommendations:
                    status.recommendations.append(repair_command)
                runtime_diagnostic_handled = True
            elif tool.plugin_state == "stale":
                status.issues.append("zcode native plugin is stale")
                if repair_command not in status.recommendations:
                    status.recommendations.append(repair_command)
                runtime_diagnostic_handled = True
            elif tool.plugin_state == "conflict":
                if tool.registration_state == "legacy-present":
                    status.issues.append(
                        "zcode legacy Agent Harness native plugin is still registered and must be replaced by tenetora@tenetora-local"
                    )
                else:
                    status.issues.append("zcode native plugin path contains a conflicting package")
                if repair_command not in status.recommendations:
                    status.recommendations.append(repair_command)
                runtime_diagnostic_handled = True
            if tool.plugin_state in {"ok", "stale"} and tool.hooks_state != "present":
                status.issues.append("zcode native plugin is installed without valid process hooks in hooks/hooks.json")
                if repair_command not in status.recommendations:
                    status.recommendations.append(repair_command)
                runtime_diagnostic_handled = True
            if tool.plugin_state == "ok" and tool.activation_state in {"disabled", "not-configured", "config-invalid"}:
                status.issues.append(f"zcode native plugin activation is {tool.activation_state}")
                status.recommendations.append(repair_command)

        effective_runtime = runtime.get("effective") if isinstance(runtime, dict) else None
        effective_adapter = (
            tool.tool in RUNTIME_ADAPTER_TARGETS
            and isinstance(effective_runtime, dict)
            and effective_runtime.get("enabled") is True
            and effective_runtime.get("kind")
            in {f"{tool.tool}-global-adapter", f"{tool.tool}-project-adapter"}
        )
        if effective_adapter and (
            tool.scope != effective_runtime.get("scope")
            or (tool.plugin_state == "ok" and tool.hooks_state == "present")
        ):
            runtime_diagnostic_handled = True

        if tool.tool in RUNTIME_ADAPTER_TARGETS and not runtime_diagnostic_handled:
            flag = "-g" if tool.scope == "global" else "-i"
            repair_command = f"tenetora install {flag} --tools {tool.tool} --force"
            if tool.plugin_state == "missing":
                status.issues.append(
                    f"{tool.tool} lifecycle skills are installed without its runtime hook adapter"
                )
                status.recommendations.append(repair_command)
                runtime_diagnostic_handled = True
            elif tool.plugin_state == "stale":
                status.issues.append(f"{tool.tool} runtime hook adapter is stale")
                status.recommendations.append(repair_command)
                runtime_diagnostic_handled = True
            elif tool.plugin_state == "conflict":
                status.issues.append(f"{tool.tool} runtime hook adapter conflicts with an existing file")
                status.recommendations.append(repair_command)
                runtime_diagnostic_handled = True
            elif tool.tool == "pi" and tool.scope == "project" and tool.hooks_state == "pending-project-trust":
                status.issues.append("pi project extension requires explicit Pi project trust before it can execute")
                status.recommendations.append("Trust this project in Pi, restart or reload Pi, then run tenetora doctor --tools pi")
                runtime_diagnostic_handled = True
            elif tool.hooks_state != "present":
                status.issues.append(f"{tool.tool} runtime hook adapter is incomplete")
                status.recommendations.append(repair_command)
                runtime_diagnostic_handled = True

        if tool.state == "missing" and not runtime_diagnostic_handled:
            flag = "-g" if tool.scope == "global" else "-i"
            status.issues.append(f"{tool.tool} {tool.scope} lifecycle skill package is incomplete")
            status.recommendations.append(f"tenetora install {flag} --tools {tool.tool}")
        elif tool.state == "conflict" and not runtime_diagnostic_handled:
            flag = "-g" if tool.scope == "global" else "-i"
            status.issues.append(f"{tool.tool} {tool.scope} lifecycle skill package has a conflict")
            status.recommendations.append(f"tenetora install {flag} --tools {tool.tool} --force")
        elif tool.state == "stale" and not runtime_diagnostic_handled:
            flag = "-g" if tool.scope == "global" else "-i"
            status.issues.append(f"{tool.tool} {tool.scope} lifecycle skill package is stale")
            status.recommendations.append(f"tenetora install {flag} --tools {tool.tool} --force")

    status.issues = list(dict.fromkeys(status.issues))
    status.recommendations = list(dict.fromkeys(status.recommendations))


def to_jsonable(status: EnvironmentStatus) -> dict[str, object]:
    return {
        "repo": status.repo,
        "project": status.project,
        "tools": [asdict(tool) for tool in status.tools],
        "issues": status.issues,
        "recommendations": status.recommendations,
        "findings": status.findings,
        "health": status.health,
        "activity": status.activity,
        "acknowledgement_state": status.acknowledgement_state,
        "acknowledgement_action": status.acknowledgement_action,
    }
