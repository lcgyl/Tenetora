#!/usr/bin/env python3
"""Ensure the canonical Tenetora CLI and runtime are available."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
CLI_PATH = SKILL_DIR / "cli"
if str(CLI_PATH) not in sys.path:
    sys.path.insert(0, str(CLI_PATH))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from tenetora.brand import (  # noqa: E402
    default_legacy_machine_home,
    default_machine_home,
    machine_home,
    migrate_legacy_machine_home,
    validate_managed_home_path,
)
from runtime_transaction import run_runtime_update  # noqa: E402
from path_manager import configure_user_path  # noqa: E402
from path_security import (  # noqa: E402
    ensure_unredirected_directory,
    is_redirected_path,
    remove_unredirected_entry,
    validate_unredirected_file_path,
    validate_unredirected_replace_target,
    validate_unredirected_path,
)
from tenetora.install_lock import install_lock_subprocess_kwargs, install_transaction_lock  # noqa: E402

COMMAND_NAME = "tenetora"
LEGACY_COMMAND_NAME = "agent-harness"
HOOK_SOURCE_FILES = (
    "run-hook",
    "run-hook.cmd",
    "tenetora_hook.py",
    "platform-contracts.json",
)
HOOK_RUNTIME_FILES = (*HOOK_SOURCE_FILES, "VERSION")
RUNTIME_CLI_FILES = ("__init__.py", "__main__.py", "cli.py")
RUNTIME_SKILL_FILES = (
    "SKILL.md",
    "VERSION",
    "scripts/delegation_state.py",
    "scripts/execution_context.py",
    "scripts/guard_action.py",
    "scripts/change_impact.py",
    "scripts/loop_state.py",
    "scripts/prompt_guard.py",
    "scripts/route_action.py",
    "scripts/rules_context.py",
    "scripts/runtime_transaction.py",
    "scripts/upgrade_contract.py",
    "scripts/upgrade_acquire.py",
    "scripts/upgrade_skill.py",
    "scripts/path_manager.py",
    "scripts/release_store.py",
    "scripts/upgrade_transaction.py",
    "scripts/onboarding_state.py",
)
LANGUAGE = "zh" if os.environ.get("TENETORA_LANG", "").strip().lower() in {
    "zh",
    "zh-cn",
    "zh-hans",
    "chinese",
    "中文",
} else "en"


def human_text(english: str, chinese: str) -> str:
    return chinese if LANGUAGE == "zh" else english


def localize_message(message: object) -> str:
    value = str(message)
    if LANGUAGE != "zh":
        return value
    return {
        "Tenetora CLI is already available.": "Tenetora CLI 已可用。",
        "Tenetora CLI is available from another source; run with --install to align the managed shim.":
            "Tenetora CLI 可从其他来源使用；请使用 --install 对齐托管 shim。",
        "Tenetora CLI is not available; run with --install.": "Tenetora CLI 不可用；请使用 --install 安装。",
        "Tenetora CLI shim installed.": "Tenetora CLI shim 已安装。",
    }.get(value, value)


def expected_version() -> str:
    version_file = SKILL_DIR / "VERSION"
    if version_file.is_file():
        version = version_file.read_text(encoding="utf-8").strip()
        if version:
            return version
    init_py = CLI_PATH / "tenetora" / "__init__.py"
    for line in init_py.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise RuntimeError(f"Cannot determine embedded CLI version from {version_file} or {init_py}")


def tenetora_home() -> Path:
    return machine_home()


def prepare_machine_home() -> dict[str, object]:
    """Converge the old default home before writing managed runtime state."""

    global SKILL_DIR, CLI_PATH
    canonical = validate_managed_home_path(machine_home(), label="Tenetora canonical home")
    legacy = validate_managed_home_path(
        default_legacy_machine_home(),
        label="legacy Agent Harness home",
    )
    if os.environ.get("TENETORA_DEFER_MACHINE_HOME_MIGRATION") == "1":
        ensure_unredirected_directory(canonical, label="Tenetora canonical home")
        result = {
            "status": "deferred",
            "canonical_home": str(canonical),
            "legacy_home": str(legacy),
        }
    elif canonical != validate_managed_home_path(
        default_machine_home(),
        label="default Tenetora home",
    ):
        result: dict[str, object] = {"status": "custom-home", "canonical_home": str(canonical)}
    else:
        result = migrate_legacy_machine_home(canonical=canonical, legacy=legacy)
    if result.get("status") in {"migrated", "merged"}:
        try:
            relative = SKILL_DIR.relative_to(legacy)
        except ValueError:
            pass
        else:
            SKILL_DIR = canonical / relative
            CLI_PATH = SKILL_DIR / "cli"
    os.environ["TENETORA_HOME"] = str(canonical)
    return result


def comparable_version(value: str) -> tuple[int, ...] | None:
    if not re.fullmatch(r"\d+(?:\.\d+){1,3}", value.strip()):
        return None
    return tuple(int(part) for part in value.strip().split("."))


def _managed_current_target(home: Path) -> Path | None:
    """Resolve only a current pointer backed by a recognizable package."""
    current = home / "current"
    try:
        if is_redirected_path(current):
            target = current.resolve(strict=True)
        else:
            target = current
        validate_unredirected_file_path(
            target / "skills" / "tenetora" / "scripts" / "ensure_cli.py",
            label="managed current bootstrap",
        )
        required = (
            target / "skills" / "tenetora" / "VERSION",
            target / "skills" / "tenetora" / "cli" / "tenetora" / "cli.py",
        )
        for path in required:
            validate_unredirected_file_path(path, label="managed current package")
            if not path.is_file():
                return None
        if target.is_relative_to(home / "releases"):
            return target
        # Local-source installs intentionally point current at the source
        # checkout, but it must carry the package identity and entrypoint.
        if (target / "install.sh").is_file() and (target / "pyproject.toml").is_file():
            return target
    except (OSError, RuntimeError):
        return None
    return None


def newer_managed_bootstrap() -> Path | None:
    home = tenetora_home()
    current_target = _managed_current_target(home)
    if current_target is None:
        return None
    managed_root = current_target / "skills"
    managed_skill = managed_root / "tenetora"
    candidate = managed_skill / "scripts" / "ensure_cli.py"
    version_path = managed_skill / "VERSION"
    cli_path = managed_skill / "cli" / "tenetora" / "cli.py"
    try:
        validate_unredirected_file_path(candidate, label="managed bootstrap script")
        validate_unredirected_file_path(version_path, label="managed bootstrap version")
        validate_unredirected_file_path(cli_path, label="managed bootstrap CLI")
    except RuntimeError:
        return None
    if not candidate.is_file():
        managed_skill = None
    if managed_skill is None:
        return None
    try:
        managed_version = version_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    loaded_version = expected_version()
    managed_key = comparable_version(managed_version)
    loaded_key = comparable_version(loaded_version)
    if managed_key is None or loaded_key is None or managed_key <= loaded_key:
        return None
    try:
        if candidate.resolve() == Path(__file__).resolve():
            return None
    except OSError:
        return None
    if not candidate.is_file() or not cli_path.is_file():
        return None
    return candidate


def default_bin_dir() -> Path:
    raw = os.environ.get("TENETORA_BIN")
    if raw:
        return Path(raw).expanduser()
    return tenetora_home() / "bin"


def command_path(bin_dir: Path, command_name: str = COMMAND_NAME) -> Path:
    suffix = ".cmd" if os.name == "nt" else ""
    return bin_dir / f"{command_name}{suffix}"


def runtime_python_path(bin_dir: Path) -> Path:
    return bin_dir.parent / "runtime" / "python"


def runtime_root(bin_dir: Path) -> Path:
    return bin_dir.parent / "runtime"


def runtime_hook_dir(bin_dir: Path) -> Path:
    return runtime_root(bin_dir) / "hooks"


def runtime_cli_package_dir(bin_dir: Path) -> Path:
    return runtime_root(bin_dir) / "cli" / "tenetora"


def runtime_skill_source() -> Path:
    canonical = SKILL_DIR.parent / "tenetora"
    try:
        canonical_version = (canonical / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        canonical_version = ""
    if (canonical / "SKILL.md").is_file() and canonical_version == expected_version():
        return canonical
    return SKILL_DIR


def hook_source_dir() -> Path | None:
    candidates = [SKILL_DIR / "hooks"]
    candidates.extend(parent / "hooks" for parent in SKILL_DIR.parents)
    for candidate in candidates:
        if all((candidate / name).is_file() for name in HOOK_SOURCE_FILES):
            return candidate
    return None


def hook_runtime_ready(bin_dir: Path) -> bool:
    destination = runtime_hook_dir(bin_dir)
    cli_destination = runtime_cli_package_dir(bin_dir)
    skill_destinations = [runtime_root(bin_dir) / "skills" / "tenetora"]
    expected = expected_version()
    try:
        required_files = [
            *(destination / name for name in HOOK_RUNTIME_FILES),
            *(cli_destination / name for name in RUNTIME_CLI_FILES),
            *(skill / name for skill in skill_destinations for name in RUNTIME_SKILL_FILES),
        ]
        for path in required_files:
            validate_unredirected_file_path(path, label="Tenetora runtime file")
            if not path.is_file():
                return False
        versions_current = all(
            path.read_text(encoding="utf-8").strip() == expected
            for path in (
                destination / "VERSION",
                runtime_root(bin_dir) / "VERSION",
                *(skill / "VERSION" for skill in skill_destinations),
            )
        )
    except (OSError, RuntimeError):
        return False
    return versions_current


def recorded_runtime_python(bin_dir: Path) -> str | None:
    path = runtime_python_path(bin_dir)
    try:
        path = validate_unredirected_file_path(path, label="recorded runtime Python")
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, RuntimeError):
        return None
    return value or None


def path_contains(directory: Path, path_value: str | None = None) -> bool:
    raw_path = path_value if path_value is not None else os.environ.get("PATH", "")
    needle = directory.expanduser().resolve()
    for entry in raw_path.split(os.pathsep):
        if not entry:
            continue
        try:
            if Path(entry).expanduser().resolve() == needle:
                return True
        except OSError:
            continue
    return False


def validate_command(command: str, command_name: str = COMMAND_NAME) -> bool:
    try:
        result = subprocess.run(
            [command, "--version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=15,
        )
    except Exception:
        return False
    first_line = result.stdout.splitlines()[0].strip() if result.stdout.splitlines() else ""
    return result.returncode == 0 and first_line == f"{command_name} {expected_version()}"


def candidate_commands(bin_dir: Path | None = None) -> list[Path]:
    candidates: list[Path] = []
    if bin_dir is not None:
        candidates.append(command_path(bin_dir))
    found = shutil.which(COMMAND_NAME)
    if found:
        candidates.append(Path(found))
    candidates.append(command_path(default_bin_dir()))

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.expanduser())
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def find_existing_command(bin_dir: Path | None = None) -> str | None:
    for candidate in candidate_commands(bin_dir):
        try:
            path = candidate.expanduser()
            if path.is_file() and validate_command(str(path), COMMAND_NAME):
                return str(path)
        except OSError:
            continue
    return None


def ensure_installable() -> None:
    if not (CLI_PATH / "tenetora" / "cli.py").is_file():
        raise FileNotFoundError(f"Embedded CLI package is missing: {CLI_PATH}")


def isolated_cli_code() -> str:
    encoded_cli_path = base64.b64encode(os.fsencode(CLI_PATH)).decode("ascii")
    return (
        "import base64,os,runpy,sys;"
        f"sys.path.insert(0,os.fsdecode(base64.b64decode({encoded_cli_path!r})));"
        "runpy.run_module('tenetora.cli',run_name='__main__')"
    )


def posix_shim(bin_dir: Path, command_name: str = COMMAND_NAME) -> str:
    python = shlex.quote(sys.executable)
    cli_code = shlex.quote(isolated_cli_code())
    harness_home = shlex.quote(str(bin_dir.parent))
    return f"""#!/bin/sh
if [ -z "${{TENETORA_HOME:-}}" ]; then
  TENETORA_HOME={harness_home}
  export TENETORA_HOME
fi
TENETORA_INVOKED_AS={shlex.quote(command_name)}
export TENETORA_INVOKED_AS
exec {python} -B -I -c {cli_code} "$@"
"""


def windows_shim(bin_dir: Path, command_name: str = COMMAND_NAME) -> str:
    python = str(Path(sys.executable)).replace("%", "%%")
    cli_code = isolated_cli_code()
    harness_home = str(bin_dir.parent)
    return f"""@echo off
if not defined TENETORA_HOME set "TENETORA_HOME={harness_home}"
set "TENETORA_INVOKED_AS={command_name}"
"{python}" -B -I -c "{cli_code}" %*
"""


def shim_is_current(path: Path) -> bool:
    try:
        if path.is_symlink() or not path.is_file():
            return False
        command_name = LEGACY_COMMAND_NAME if path.stem == LEGACY_COMMAND_NAME else COMMAND_NAME
        expected = windows_shim(path.parent, command_name) if os.name == "nt" else posix_shim(path.parent, command_name)
        if path.read_text(encoding="utf-8") != expected:
            return False
        return os.name == "nt" or os.access(path, os.X_OK)
    except OSError:
        return False


def shim_is_managed(path: Path, command_name: str = COMMAND_NAME) -> bool:
    """Recognize a valid managed shim without binding status to one skill copy.

    Different host skill copies can use the same package version while resolving
    Python or the embedded CLI source through different absolute paths. Exact
    content equality remains the install-time rewrite test; status only needs to
    prove that the canonical command is a structurally managed, working shim.
    """

    try:
        if path.is_symlink() or not path.is_file():
            return False
        content = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if os.name == "nt":
        required = (
            "@echo off",
            "TENETORA_HOME=",
            f"TENETORA_INVOKED_AS={command_name}",
            "tenetora.cli",
            " -I -c ",
        )
    else:
        required = (
            "#!/bin/sh",
            "TENETORA_HOME=",
            f"TENETORA_INVOKED_AS={command_name}",
            "tenetora.cli",
            " -I -c ",
        )
        if not os.access(path, os.X_OK):
            return False
    return all(marker in content for marker in required)


def write_shim(bin_dir: Path, force: bool, command_name: str = COMMAND_NAME) -> Path:
    ensure_installable()
    bin_dir = validate_managed_home_path(bin_dir, label="Tenetora CLI bin directory")
    ensure_unredirected_directory(bin_dir, label="Tenetora CLI bin directory")
    dest = validate_unredirected_replace_target(
        command_path(bin_dir, command_name),
        label="Tenetora CLI shim",
    )
    if not force and shim_is_current(dest):
        return dest

    content = windows_shim(bin_dir, command_name) if os.name == "nt" else posix_shim(bin_dir, command_name)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{dest.name}.", dir=bin_dir)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        if os.name != "nt":
            mode = temporary.stat().st_mode
            temporary.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        validate_unredirected_replace_target(dest, label="Tenetora CLI shim")
        os.replace(temporary, dest)
    finally:
        if temporary.exists():
            temporary.unlink()
    return dest


def remove_managed_legacy_shim(bin_dir: Path) -> bool:
    """Remove only an old CLI alias whose content proves Tenetora ownership."""

    path = command_path(bin_dir, LEGACY_COMMAND_NAME)
    if not path.is_file() or path.is_symlink():
        return False
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    ownership_markers = (
        "TENETORA_INVOKED_AS=agent-harness",
        "AGENT_HARNESS_INVOKED_AS=agent-harness",
        "runpy.run_module('tenetora.cli'",
        "runpy.run_module('agent_harness.cli'",
    )
    legacy_pythonpath_wrapper = (
        "PYTHONPATH=" in content
        and "skills/agent-harness/cli" in content.replace("\\", "/")
        and "agent-harness " in content
        and "--version" in content
    )
    if not any(marker in content for marker in ownership_markers) and not legacy_pythonpath_wrapper:
        return False
    path.unlink()
    return True


def write_runtime_python(bin_dir: Path) -> Path:
    validate_managed_home_path(runtime_root(bin_dir), label="Tenetora runtime directory")
    dest = validate_unredirected_file_path(runtime_python_path(bin_dir), label="runtime Python path")
    ensure_unredirected_directory(dest.parent, label="runtime Python directory")
    content = f"{Path(sys.executable)}\n"
    if dest.is_file() and dest.read_text(encoding="utf-8") == content:
        return dest

    fd, temporary_name = tempfile.mkstemp(prefix=f".{dest.name}.", dir=dest.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
        validate_unredirected_file_path(dest, label="runtime Python path")
        os.replace(temporary, dest)
    finally:
        if temporary.exists():
            temporary.unlink()
    return dest


def write_hook_runtime(bin_dir: Path) -> Path | None:
    source = hook_source_dir()
    destination = runtime_hook_dir(bin_dir)
    validate_managed_home_path(runtime_root(bin_dir), label="Tenetora runtime directory")
    if source is None:
        return destination if hook_runtime_ready(bin_dir) else None

    destination_root = runtime_root(bin_dir)
    runtime_parent = destination_root.parent
    ensure_unredirected_directory(runtime_parent, label="Tenetora runtime parent")
    validate_unredirected_path(destination_root, label="Tenetora runtime directory")
    staging = Path(tempfile.mkdtemp(prefix=".runtime.", dir=runtime_parent))
    previous = Path(tempfile.mkdtemp(prefix=".runtime.previous.", dir=runtime_parent))
    previous.rmdir()
    try:
        staging_hooks = staging / "hooks"
        staging_hooks.mkdir()
        for name in HOOK_SOURCE_FILES:
            shutil.copy2(source / name, staging_hooks / name)
        version = expected_version() + "\n"
        (staging_hooks / "VERSION").write_text(version, encoding="utf-8")
        (staging / "VERSION").write_text(version, encoding="utf-8")
        (staging / "python").write_text(f"{Path(sys.executable)}\n", encoding="utf-8")
        (staging / "python").chmod(stat.S_IRUSR | stat.S_IWUSR)
        shutil.copytree(
            CLI_PATH / "tenetora",
            staging / "cli" / "tenetora",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        shutil.copytree(
            runtime_skill_source(),
            staging / "skills" / "tenetora",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        if os.name != "nt":
            mode = (staging_hooks / "run-hook").stat().st_mode
            (staging_hooks / "run-hook").chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        if destination_root.exists():
            validate_unredirected_path(destination_root, label="Tenetora runtime directory")
            os.replace(destination_root, previous)
        try:
            validate_unredirected_path(destination_root.parent, label="Tenetora runtime parent")
            os.replace(staging, destination_root)
        except Exception:
            if previous.exists() and not destination_root.exists():
                os.replace(previous, destination_root)
            raise
        if previous.exists():
            remove_unredirected_entry(previous, label="previous Tenetora runtime")
        return runtime_hook_dir(bin_dir)
    finally:
        if staging.exists():
            remove_unredirected_entry(staging, label="staged Tenetora runtime")
        if previous.exists():
            remove_unredirected_entry(previous, label="previous Tenetora runtime")


def report(
    *,
    available: bool,
    installed: bool,
    installable: bool,
    command: str | None,
    bin_dir: Path,
    message: str,
) -> dict[str, object]:
    managed_command = command_path(bin_dir).expanduser().absolute()
    resolved_command = Path(command).expanduser().absolute() if command else None
    command_source = "managed-shim" if resolved_command == managed_command else "external-command"
    path_ready = path_contains(bin_dir)
    return {
        "available": available,
        "installed": installed,
        "installable": installable,
        "command": str(resolved_command) if resolved_command else None,
        "managed_command": str(managed_command),
        "command_source": command_source,
        "bin_dir": str(bin_dir),
        "hook_python": recorded_runtime_python(bin_dir),
        "hook_python_file": str(runtime_python_path(bin_dir)),
        "hook_runtime_dir": str(runtime_hook_dir(bin_dir)) if hook_runtime_ready(bin_dir) else None,
        "path_ready": path_ready,
        "path_status": "ready" if path_ready else "optional-missing" if available else "not-applicable",
        "skill_dir": str(SKILL_DIR),
        "expected_version": expected_version(),
        "execution_contract": {
            "invoke": "absolute-command",
            "use_returned_command": True,
            "path_required": False,
            "continue_without_path": True,
            "narrate_path_warning": False,
            "allow_cross_tool_skill_fallback": False,
            "allow_pythonpath_fallback": False,
            "persist_alias": False,
        },
        "message": message,
    }


def existing_status(bin_dir: Path) -> dict[str, object]:
    requested_command = command_path(bin_dir)
    installable = (CLI_PATH / "tenetora" / "cli.py").is_file()
    if shim_is_managed(requested_command) and validate_command(str(requested_command)):
        return report(
            available=True,
            installed=False,
            installable=installable,
            command=str(requested_command),
            bin_dir=bin_dir,
            message="Tenetora CLI is already available.",
        )
    existing = find_existing_command(bin_dir)
    if existing:
        return report(
            available=True,
            installed=False,
            installable=installable,
            command=existing,
            bin_dir=bin_dir,
            message="Tenetora CLI is available from another source; run with --install to align the managed shim.",
        )
    return report(
        available=False,
        installed=False,
        installable=installable,
        command=str(command_path(bin_dir)),
        bin_dir=bin_dir,
        message="Tenetora CLI is not available; run with --install.",
    )


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--help", action="help", help="Show this help message and exit.")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--status", dest="action", action="store_const", const="status")
    action.add_argument("--install", dest="action", action="store_const", const="install")
    parser.add_argument("--bin-dir", default=str(default_bin_dir()), metavar="<dir>", help="Directory for the CLI shim.")
    parser.add_argument("--force", action="store_true", help="Replace an existing shim in --bin-dir.")
    parser.add_argument(
        "--configure-path",
        action="store_true",
        help="Add the managed shim directory to the current user's shell or Windows PATH.",
    )
    parser.add_argument("--runtime-rollback-journal", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.set_defaults(action="status")
    return parser.parse_args(argv)


def _main_locked(args: argparse.Namespace, bin_dir: Path) -> int:
    default_home = validate_managed_home_path(default_machine_home(), label="default Tenetora home")
    if args.action == "install" and bin_dir.parent == default_home:
        migration = prepare_machine_home()
        if migration.get("status") == "blocked-canonical-conflict":
            print(
                human_text(
                    "Cannot migrate the proven legacy machine home because ~/.tenetora contains unrelated data.",
                    "无法迁移已确认归属的旧机器目录，因为 ~/.tenetora 中存在无法确认归属的数据。",
                ),
                file=sys.stderr,
            )
            return 1
        bin_dir = validate_managed_home_path(
            default_bin_dir().expanduser().absolute(),
            label="Tenetora CLI bin directory",
        )
    try:
        if args.action == "install":
            def install_runtime() -> dict[str, object]:
                requested_command = command_path(bin_dir)
                write_runtime_python(bin_dir)
                write_hook_runtime(bin_dir)
                if (
                    shim_is_current(requested_command)
                    and validate_command(str(requested_command), COMMAND_NAME)
                    and not args.force
                ):
                    remove_managed_legacy_shim(bin_dir)
                    return report(
                        available=True,
                        installed=False,
                        installable=True,
                        command=str(requested_command),
                        bin_dir=bin_dir,
                        message="Tenetora CLI is already available.",
                    )
                command = write_shim(bin_dir, force=args.force, command_name=COMMAND_NAME)
                remove_managed_legacy_shim(bin_dir)
                return report(
                    available=True,
                    installed=True,
                    installable=True,
                    command=str(command),
                    bin_dir=bin_dir,
                    message="Tenetora CLI shim installed.",
                )

            payload = run_runtime_update(bin_dir, args.runtime_rollback_journal, install_runtime)
            if args.configure_path:
                payload["path_configuration"] = configure_user_path(bin_dir)
                payload["path_ready_after_restart"] = True
        else:
            payload = existing_status(bin_dir)
    except Exception as exc:
        print(human_text(str(exc), f"CLI 自举失败：{exc}"), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(localize_message(payload["message"]))
        print(human_text(f"Command: {payload['command']}", f"命令：{payload['command']}"))
        if payload["available"] and not payload["path_ready"]:
            print(
                human_text(
                    "CLI is usable; the shim directory is not on PATH. This only affects typing tenetora directly in a terminal. Continue with the absolute command above.",
                    "CLI 已可用；shim 目录未加入 PATH，仅影响终端直接输入 tenetora。当前流程可使用上方绝对命令继续。",
                )
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        newer_bootstrap = newer_managed_bootstrap()
    except Exception as exc:
        print(human_text(str(exc), f"CLI 自举失败：{exc}"), file=sys.stderr)
        return 1
    if newer_bootstrap is not None:
        delegated = subprocess.run(
            [sys.executable, str(newer_bootstrap), *effective_argv],
            check=False,
            **install_lock_subprocess_kwargs(),
        )
        return delegated.returncode

    args = parse_args(effective_argv)
    try:
        bin_dir = validate_managed_home_path(
            Path(args.bin_dir).expanduser().absolute(),
            label="Tenetora CLI bin directory",
        )
        context = install_transaction_lock(bin_dir.parent) if args.action == "install" else nullcontext()
        with context:
            return _main_locked(args, bin_dir)
    except Exception as exc:
        print(human_text(str(exc), f"CLI 自举失败：{exc}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
