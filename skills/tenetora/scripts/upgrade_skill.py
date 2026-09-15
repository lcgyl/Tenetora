#!/usr/bin/env python3
"""Check or apply Tenetora skill package upgrades."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
CLI_DIR = SKILL_DIR / "cli"
if CLI_DIR.exists():
    sys.path.insert(0, str(CLI_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from install_progress import redact, write_failure_artifacts  # noqa: E402


def use_chinese() -> bool:
    return os.environ.get("TENETORA_LANG", "").strip().lower() in {"zh", "zh-cn", "zh-hans", "chinese", "中文"}


def issue_text(issue: object) -> str:
    text = str(issue)
    if not use_chinese():
        return text
    mappings = {
        "machine-wide upgrade preflight is blocked": "机器级升级预检被阻断",
        "release download failed": "发布包下载失败",
        "release source options are mutually exclusive": "发布源选项互斥",
        "release manifest and ZIP URLs must be supplied together": "发布清单地址和 ZIP 地址必须成对提供",
        "release mirror URL is invalid": "发布镜像地址无效",
        "release mirror URL must use an HTTPS URL": "发布镜像地址必须使用 HTTPS",
        "release mirror URL must not contain credentials": "发布镜像地址不能包含凭证",
        "release mirror URL must not contain a query or fragment": "发布镜像地址不能包含 query 或 fragment",
        "release manifest URL is invalid": "发布清单地址无效",
        "release manifest URL must use an HTTPS URL": "发布清单地址必须使用 HTTPS",
        "release manifest URL must not contain credentials": "发布清单地址不能包含凭证",
        "release manifest URL must not contain a query or fragment": "发布清单地址不能包含 query 或 fragment",
        "release ZIP URL is invalid": "发布包 ZIP 地址无效",
        "release ZIP URL must use an HTTPS URL": "发布包 ZIP 地址必须使用 HTTPS",
        "release ZIP URL must not contain credentials": "发布包 ZIP 地址不能包含凭证",
        "release ZIP URL must not contain a query or fragment": "发布包 ZIP 地址不能包含 query 或 fragment",
        "release manifest exceeds the bounded size limit": "发布清单超过大小上限",
        "release manifest must be a JSON object": "发布清单必须是 JSON 对象",
        "release manifest identity or format is invalid": "发布清单身份或格式无效",
        "release commit identity is invalid": "发布提交身份无效",
        "release installer protocol is older than this upgrader supports": "发布包安装协议低于当前升级器支持范围",
        "release bridge protocol is incompatible": "发布包桥接协议不兼容",
        "release zip root is invalid": "发布包 ZIP 根目录无效",
        "release upgrade entrypoint is invalid": "发布包升级入口无效",
        "release file inventory is invalid": "发布包文件清单无效",
        "release file inventory contains duplicates": "发布包文件清单含重复项",
        "release file inventory must be sorted": "发布包文件清单未按稳定顺序排列",
        "release file inventory contains an unsafe path": "发布包文件清单含不安全路径",
        "release file SHA256 inventory does not match files": "发布包文件 SHA256 清单与文件列表不一致",
        "release file SHA256 inventory is invalid": "发布包文件 SHA256 清单无效",
        "release state schema contract is invalid": "发布包状态 schema 合同无效",
        "release artifact name is invalid": "发布包产物名称无效",
        "release SHA256 is invalid": "发布包 SHA256 无效",
        "release ZIP has no valid embedded manifest": "发布包 ZIP 缺少有效的内嵌清单",
        "release ZIP file count is invalid": "发布包 ZIP 文件数量无效",
        "release ZIP contains an unsafe path": "发布包 ZIP 含不安全路径",
        "release ZIP contains duplicate entries": "发布包 ZIP 含重复条目",
        "release ZIP contains portable path collisions": "发布包 ZIP 含跨平台路径冲突",
        "release ZIP contains a symbolic link": "发布包 ZIP 含符号链接",
        "release ZIP contains a file and directory path collision": "发布包 ZIP 含文件与目录路径冲突",
        "release ZIP expands beyond the bounded size limit": "发布包 ZIP 解压后超过大小上限",
        "release ZIP extraction failed": "发布包 ZIP 解压失败",
        "release ZIP root is invalid": "发布包 ZIP 根目录无效",
        "developer source must not be a symbolic link": "开发源码目录不能是符号链接",
        "developer source contains a symbolic link or special file": "开发源码含符号链接或特殊文件",
        "release ZIP exceeds the bounded size limit": "发布包 ZIP 超过大小上限",
        "offline release ZIP requires --sha256 or a sibling .sha256 file": "离线发布包 ZIP 需要 --sha256 或同名 .sha256 文件",
        "release ZIP SHA256 mismatch": "发布包 ZIP SHA256 不匹配",
        "release version is older than the installed version": "发布版本低于已安装版本",
        "external and embedded release manifests disagree": "外部发布清单与 ZIP 内嵌清单不一致",
        "release ZIP file inventory does not match its manifest": "发布包 ZIP 文件列表与清单不一致",
        "release ZIP file contents do not match its manifest": "发布包 ZIP 文件内容与清单不一致",
        "acquired release contents do not match its manifest": "获取到的 release 内容与清单不一致",
        "acquired release does not match the selected manifest": "获取到的 release 与所选清单不一致",
        "existing versioned release is not a valid managed release": "现有版本 release 不是有效的受管 release",
        "versioned release is immutable and already contains different content": "版本化 release 不可变，但现有内容不同",
        "versioned release contents do not match its manifest": "版本化 release 内容与清单不一致",
        "cannot create the managed Windows release junction": "无法创建受管 Windows release junction",
        "managed pointer replacement failed; the prior pointer was restored": "受管指针替换失败，旧指针已恢复",
        "release activation requires a valid immutable managed release": "release 激活需要有效且不可变的受管 release",
        "upgrade transaction journal is unreadable": "升级事务日志不可读",
        "upgrade transaction journal schema is invalid": "升级事务日志 schema 无效",
        "upgrade transaction revision is invalid": "升级事务 revision 无效",
        "an unfinished upgrade transaction already exists": "已有未完成的升级事务",
        "no unfinished upgrade transaction is available to resume": "没有可恢复的未完成升级事务",
        "unfinished upgrade transaction targets another release": "未完成升级事务指向另一个 release",
        "unfinished upgrade transaction plan no longer matches": "未完成升级事务的计划已不匹配",
        "unfinished upgrade transaction still belongs to a live process": "未完成升级事务仍属于存活进程",
        "upgrade transaction identity mismatch": "升级事务身份不匹配",
        "upgrade transaction changed concurrently": "升级事务发生并发变更",
        "shell profile contains duplicate or damaged Tenetora PATH markers": "Shell profile 含重复或损坏的 Tenetora PATH 标记",
        "shell profile contains a damaged Tenetora PATH marker": "Shell profile 含损坏的 Tenetora PATH 标记",
        "refusing to update a symbolic-link shell profile": "拒绝更新符号链接形式的 Shell profile",
        "shell profile is not a regular file": "Shell profile 不是普通文件",
        "shell profile is not owned by the current user": "Shell profile 不属于当前用户",
        "shell profile is writable by group or others": "Shell profile 可被用户组或其他用户写入",
    }
    if text in mappings:
        return mappings[text]
    prefixes = (
        ("release manifest is missing required fields:", "发布清单缺少必需字段："),
        ("release manifest is invalid:", "发布清单无效："),
        ("embedded release manifest is invalid:", "ZIP 内嵌发布清单无效："),
        ("managed release manifest is invalid:", "受管 release 清单无效："),
        ("managed release contains an unsafe Python cache entry", "受管 release 含不安全的 Python 缓存条目"),
        ("managed release contains an unexpected Python cache entry", "受管 release 含意外的 Python 缓存条目"),
        ("release source environment options are mutually exclusive:", "发布源环境选项互斥："),
        ("TENETORA_RELEASE_MANIFEST_URL and TENETORA_RELEASE_ZIP_URL must be configured together", "TENETORA_RELEASE_MANIFEST_URL 和 TENETORA_RELEASE_ZIP_URL 必须成对配置"),
        ("refusing to replace unowned pointer:", "拒绝替换无归属指针："),
        ("managed pointer replacement failed and its backup could not be restored:", "受管指针替换失败且备份无法恢复："),
    )
    for english, chinese in prefixes:
        if text.startswith(english):
            remainder = text[len(english):]
            nested = issue_text(remainder.lstrip()) if remainder.lstrip() else ""
            return chinese + nested
    if text.startswith("installed Tenetora ") and " is older than the direct bridge minimum " in text:
        installed, minimum = text[len("installed Tenetora "):].split(
            " is older than the direct bridge minimum ", 1
        )
        return f"已安装的 Tenetora {installed} 低于直接升级最低版本 {minimum}"
    return text

from tenetora import __version__  # noqa: E402
from tenetora.brand import (  # noqa: E402
    default_legacy_machine_home,
    default_machine_home,
    machine_home,
    validate_managed_home_path,
)
from tenetora.path_security import validate_existing_project_path  # noqa: E402
from tenetora.environment import collect_status, parse_tools  # noqa: E402
from tenetora.install_lock import (  # noqa: E402
    install_lock_is_held,
    install_lock_subprocess_kwargs,
    install_transaction_locks,
)
from path_manager import checkpoint_user_path, configure_user_path, restore_user_path, snapshot_user_path  # noqa: E402
from onboarding_state import derive as derive_onboarding_state  # noqa: E402
from release_store import (  # noqa: E402
    ReleaseStoreError,
    activate_release,
    read_release_manifest,
    restore_release_activation,
    stage_release,
)
from upgrade_acquire import (  # noqa: E402
    UpgradeAcquireError,
    acquire_release,
    inspect_release_manifest,
    release_source_configured,
)
from upgrade_contract import UpgradeContractError, ensure_source_supported, release_contract_fields, version_key  # noqa: E402
from tenetora.update_hint import UpdateHintError, record_failure, record_success  # noqa: E402
from upgrade_transaction import (  # noqa: E402
    UpgradeTransactionError,
    advance as advance_transaction,
    begin as begin_transaction,
    load as load_transaction,
    resume as resume_transaction,
)


def refresh_onboarding_state() -> bool:
    """Refresh derived onboarding data without invalidating a completed upgrade."""

    try:
        derive_onboarding_state(machine_home())
    except (OSError, RuntimeError):
        print(
            "首次对话提醒状态未能刷新；升级已完成，下次项目对话会自动重试，tenetora doctor 可检查整体安装。"
            if use_chinese()
            else "First-conversation reminder state could not be refreshed; the upgrade completed, the next project conversation will retry it, and tenetora doctor can inspect the overall installation.",
            file=sys.stderr,
        )
        return False
    return True


def existing_project_path(raw: str) -> Path:
    try:
        return validate_existing_project_path(raw)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def selected_scope(args: argparse.Namespace) -> str | None:
    if args.scope_flag:
        return args.scope_flag
    return args.scope


def render_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def package_root() -> Path | None:
    candidates = [SKILL_DIR, *SKILL_DIR.parents]
    candidates.append(machine_home() / "current")
    for candidate in candidates:
        if (
            (candidate / ".zcode-plugin" / "plugin.json").is_file()
            and any(
                (candidate / "skills" / name / "SKILL.md").is_file()
                for name in ("tenetora", "agent-harness")
            )
        ):
            return candidate.resolve()
    return None


def same_managed_home(left: Path, right: Path) -> bool:
    """Treat safe filesystem aliases as the same home without changing lock paths."""

    if left == right or os.path.normcase(os.fspath(left)) == os.path.normcase(os.fspath(right)):
        return True
    try:
        return os.path.samefile(left, right)
    except (FileNotFoundError, OSError):
        return False


OUTER_LOCK_ENVIRONMENT = (
    "TENETORA_OUTER_INSTALL_LOCK_HELD",
    "TENETORA_OUTER_INSTALL_LOCK_TOKEN",
    "TENETORA_OUTER_INSTALL_LOCK_FDS",
    "TENETORA_OUTER_INSTALL_LOCK_HANDLES",
    "TENETORA_OUTER_INSTALL_LOCK_PROOFS",
)


def clear_stale_controller_lock_environment() -> bool:
    """Drop stale shell-installer markers before a top-level controller run.

    The Windows PowerShell installer communicates its lock to descendants with
    environment markers.  If that installer is interrupted, the markers can
    remain in the calling PowerShell session after the holder process exits.
    A top-level ``tenetora upgrade`` must not mistake those stale markers for
    an inherited lock; worker mode remains fail-closed for forged markers.
    """

    if os.environ.get("TENETORA_OUTER_INSTALL_LOCK_HELD") != "1":
        return False
    token = os.environ.get("TENETORA_OUTER_INSTALL_LOCK_TOKEN", "")
    try:
        held = len(token) == 64 and install_lock_is_held(machine_home(), token)
    except (OSError, RuntimeError, ValueError):
        held = False
    if held:
        return False
    for name in OUTER_LOCK_ENVIRONMENT:
        os.environ.pop(name, None)
    return True


def developer_checkout() -> Path | None:
    root = package_root()
    if root is not None and (root / ".git").exists():
        return root
    return None


def local_release_manifest(source_root: Path) -> dict[str, object]:
    if (source_root / "manifest.json").is_file():
        return read_release_manifest(source_root)
    if not (source_root / ".git").exists():
        raise ReleaseStoreError("release source has no manifest")
    version = (source_root / "skills" / "tenetora" / "VERSION").read_text(encoding="utf-8").strip()
    files = sorted(
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.relative_to(source_root).as_posix() != "manifest.json"
    )
    return {
        "name": "tenetora",
        "version": version,
        "commit": "developer",
        "format": "tenetora-release-zip-v1",
        "installer_protocol": 3,
        "zip_root": "tenetora",
        "files": files,
        "file_sha256": {
            relative: hashlib.sha256((source_root / relative).read_bytes()).hexdigest()
            for relative in files
        },
        **release_contract_fields(),
    }


def preflight_failure_details(payload: object) -> list[str]:
    """Collect actionable machine-preflight failures from nested worker payloads."""

    details: list[str] = []
    visited: set[int] = set()

    def add(value: object) -> None:
        text = str(value or "").strip()
        if text:
            details.append(text)

    def visit(value: object) -> None:
        if not isinstance(value, dict) or id(value) in visited:
            return
        visited.add(id(value))
        add(value.get("error"))
        failures = value.get("failure_details")
        if isinstance(failures, list):
            for failure in failures:
                add(failure)
        checks = value.get("tools")
        if isinstance(checks, list):
            for check in checks:
                if not isinstance(check, dict):
                    continue
                blocker = str(check.get("blocker") or "").strip()
                remediation = str(check.get("remediation") or "").strip()
                actual = str(check.get("actual") or "")
                planned = str(check.get("planned") or "")
                validation_failed = (
                    check.get("matches_plan") is False
                    or check.get("version_matches_package") is False
                )
                if actual == "PENDING_TRUST" and not validation_failed:
                    continue
                if (
                    not blocker
                    and planned != "BLOCKED"
                    and actual != "BLOCKED"
                    and check.get("matches_plan") is not False
                ):
                    continue
                label = f"{check.get('scope', '?')}:{check.get('tool', '?')}"
                if blocker:
                    add(f"{label}: {blocker}")
                if remediation:
                    add(f"{label}: {remediation}")
        for key in ("machine", "units", "result", "preflight", "postflight", "project_governance"):
            nested = value.get(key)
            if isinstance(nested, dict):
                visit(nested)
            elif isinstance(nested, list):
                for item in nested:
                    visit(item)

    visit(payload)
    return list(dict.fromkeys(details))


def preflight_failure_artifacts_missing(args: argparse.Namespace) -> bool:
    paths = [
        Path(value)
        for value in (getattr(args, "events_jsonl", None), getattr(args, "log_file", None))
        if value is not None
    ]
    return bool(paths) and any(not path.is_file() or path.stat().st_size == 0 for path in paths)


def persist_preflight_failure(args: argparse.Namespace, payload: object, message: str) -> None:
    """Keep failure diagnostics available when a nested worker exits before reporting."""

    event_path = getattr(args, "events_jsonl", None)
    log_path = getattr(args, "log_file", None)
    if not preflight_failure_artifacts_missing(args):
        return
    details = preflight_failure_details(payload)
    try:
        write_failure_artifacts(
            Path(event_path) if event_path is not None else None,
            Path(log_path) if log_path is not None else None,
            title="Tenetora 预检" if use_chinese() else "Tenetora preflight",
            phase="preflight",
            message=message,
            details=details,
        )
    except Exception as exc:
        print(
            issue_text(redact(f"failure diagnostics unavailable: {exc}")),
            file=sys.stderr,
        )


def print_preflight_failure(args: argparse.Namespace, payload: object, message: str) -> None:
    safe_message = redact(message)
    print(issue_text(safe_message), file=sys.stderr)
    if getattr(args, "json", False):
        persist_preflight_failure(args, payload, safe_message)
        return
    for detail in preflight_failure_details(payload):
        print(f"- {issue_text(redact(detail))}", file=sys.stderr)
    persist_preflight_failure(args, payload, safe_message)


def check_payload(args: argparse.Namespace, release_manifest: dict[str, object] | None = None) -> dict[str, object]:
    status = collect_status(
        root=args.path,
        tools=parse_tools(args.tools),
        scope=selected_scope(args) or "both",
        include_diagnostics=True,
    )
    machine_command = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "install-skill.py"),
        "--preflight",
        "--json",
        "--tools",
        args.tools,
        "--mode",
        args.mode,
        "--path",
        str(args.path),
    ]
    scope = selected_scope(args)
    if args.bootstrap_install:
        scope = scope or "global"
    else:
        machine_command.extend(("--existing-only", "--all-existing"))
    if scope is not None:
        machine_command.extend(("--scope", scope))
    elif not args.bootstrap_install:
        machine_command.append("--auto-discover")
    if args.force:
        machine_command.append("--force")
    if args.allow_skills_only:
        machine_command.append("--allow-skills-only")
    if args.require_full:
        machine_command.append("--require-full")
    if args.prune_shadowed:
        machine_command.append("--prune-shadowed")
    if args.no_prune_shadowed:
        machine_command.append("--no-prune-shadowed")
    machine_command.extend(("--codex-hooks", args.codex_hooks))
    if args.allow_tracked_codex_hooks:
        machine_command.append("--allow-tracked-codex-hooks")
    source_root = package_root()
    if source_root is not None:
        machine_command.extend(("--package-root", str(source_root)))
    if getattr(args, "log_file", None) is not None:
        machine_command.extend(("--log-file", str(args.log_file)))
    if getattr(args, "events_jsonl", None) is not None:
        machine_command.extend(("--events-jsonl", str(args.events_jsonl)))
    machine_environment = os.environ.copy()
    machine_environment["PYTHONIOENCODING"] = "utf-8"
    machine_environment["PYTHONUTF8"] = "1"
    machine_result = subprocess.run(
        machine_command,
        env=machine_environment,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        **install_lock_subprocess_kwargs(),
    )
    try:
        machine = json.loads(machine_result.stdout)
    except json.JSONDecodeError:
        machine = {
            "status": "missing" if "No existing Tenetora installation surface" in machine_result.stderr else "blocked",
            "result": "BLOCKED",
            "error": machine_result.stderr.strip() or machine_result.stdout.strip(),
        }
    issues = list(status.issues)
    if machine_result.returncode != 0 and machine.get("status") != "missing":
        issues.append(str(machine.get("error") or "machine-wide upgrade preflight is blocked"))
    payload: dict[str, object] = {
        "status": "checked",
        "action": "check",
        "version": __version__,
        "source": str(SKILL_DIR),
        "tools": [asdict(tool) for tool in status.tools],
        "issues": issues,
        "recommendations": status.recommendations,
        "machine": machine,
    }
    if release_manifest is not None:
        payload["release"] = {
            key: release_manifest.get(key)
            for key in (
                "version",
                "commit",
                "bridge_protocol",
                "minimum_bridge_version",
                "installer_protocol",
                "artifact",
                "sha256",
            )
            if release_manifest.get(key) is not None
        }
        payload["update_available"] = str(release_manifest.get("version") or "") != __version__
    return payload


def add_scope_flags(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-g", "--global", dest="scope_flag", action="store_const", const="global", help="只更新已有全局安装面" if use_chinese() else "Update existing global surfaces only")
    group.add_argument("-i", "--in-project", dest="scope_flag", action="store_const", const="project", help="只更新已有项目安装面" if use_chinese() else "Update existing project surfaces only")
    group.add_argument("-b", "--both", dest="scope_flag", action="store_const", const="both", help="更新两个 scope 中的已有安装面" if use_chinese() else "Update existing surfaces from both scopes")
    group.add_argument("-s", "--scope", choices=("global", "project", "both"), default=None, help="按 scope 过滤已有安装面" if use_chinese() else "Filter existing surfaces by scope")


def install_args(args: argparse.Namespace, hook_journal: Path | None = None) -> list[str]:
    forwarded = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "install-skill.py"),
        "--tools",
        args.tools,
        "--mode",
        args.mode,
        "--path",
        str(args.path),
        "--compact",
        "--progress",
        args.progress,
    ]
    if not args.bootstrap_install:
        forwarded.append("--update")
    scope = selected_scope(args) or ("global" if args.bootstrap_install else None)
    if scope is None:
        forwarded.append("--all-existing")
        forwarded.append("--auto-discover")
    elif scope == "global":
        forwarded.append("-g")
    elif scope == "project":
        forwarded.append("-i")
    elif scope == "both":
        forwarded.append("-b")
    if args.force:
        forwarded.append("--force")
    if args.dry_run:
        forwarded.append("--dry-run")
    if args.allow_skills_only:
        forwarded.append("--allow-skills-only")
    if args.require_full:
        forwarded.append("--require-full")
    if args.prune_shadowed:
        forwarded.append("--prune-shadowed")
    if args.no_prune_shadowed:
        forwarded.append("--no-prune-shadowed")
    forwarded.extend(("--codex-hooks", args.codex_hooks))
    if args.allow_tracked_codex_hooks:
        forwarded.append("--allow-tracked-codex-hooks")
    if args.verbose:
        forwarded.append("--verbose")
    source_root = package_root()
    if source_root is not None:
        forwarded.extend(["--package-root", str(source_root)])
    if hook_journal is not None:
        forwarded.extend(["--hook-rollback-journal", str(hook_journal)])
    if args.log_file is not None:
        forwarded.extend(["--log-file", str(args.log_file)])
    if args.events_jsonl is not None:
        forwarded.extend(["--events-jsonl", str(args.events_jsonl)])
    return forwarded


def controller_child_environment() -> dict[str, str]:
    """Mark nested installers so the outer controller owns the terminal summary."""

    environment = os.environ.copy()
    environment["TENETORA_UPGRADE_CONTROLLER"] = "1"
    return environment


def hook_action_args(args: argparse.Namespace, action: str, journal: Path) -> list[str]:
    command = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "install-skill.py"),
        action,
        "--path",
        str(args.path),
        "--package-root",
        str(package_root() or SKILL_DIR.parent.parent),
        "--hook-rollback-journal",
        str(journal),
        "--json",
    ]
    if selected_scope(args) is None:
        command.append("--all-existing")
    if args.dry_run:
        command.append("--dry-run")
    return command


def finalize_hook_journal(args: argparse.Namespace, journal: Path) -> bool:
    result = subprocess.run(
        hook_action_args(args, "--finalize-hook-journal", journal),
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **install_lock_subprocess_kwargs(),
    )
    return result.returncode == 0


def restore_hook_journal(args: argparse.Namespace, journal: Path) -> bool:
    if not (journal / "manifest.json").is_file():
        return True
    if not finalize_hook_journal(args, journal):
        return False
    result = subprocess.run(
        hook_action_args(args, "--restore-hooks", journal),
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **install_lock_subprocess_kwargs(),
    )
    return result.returncode == 0


def runtime_action_args(args: argparse.Namespace, action: str, journal: Path) -> list[str]:
    source_root = package_root() or SKILL_DIR.parent.parent
    bin_dir = args.bin_dir or (machine_home() / "bin")
    return [
        sys.executable,
        str(source_root / "skills" / "tenetora" / "scripts" / "runtime_transaction.py"),
        action,
        "--bin-dir",
        str(bin_dir),
        "--journal",
        str(journal),
        "--json",
    ]


def checkpoint_runtime_journal(
    args: argparse.Namespace,
    journal: Path,
    release_root: Path,
) -> bool:
    command = runtime_action_args(args, "--checkpoint", journal)
    command.extend(
        [
            "--expect-link",
            f"current={release_root}",
            "--expect-link",
            f"source/tenetora={release_root}",
            "--expect-absent",
            "source/agent-harness",
        ]
    )
    result = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.returncode == 0


def restore_runtime_journal(args: argparse.Namespace, journal: Path) -> bool:
    if not (journal / "manifest.json").is_file():
        return True
    result = subprocess.run(
        runtime_action_args(args, "--restore", journal),
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.returncode == 0


def preserve_failed_transaction_journals(hook_journal: Path, runtime_journal: Path) -> Path:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = machine_home() / "backups" / "failed-transactions" / f"upgrade-{timestamp}-{uuid.uuid4().hex[:8]}"
    destination.mkdir(parents=True, exist_ok=False)
    for source, name in ((hook_journal, "commit-hooks"), (runtime_journal, "managed-runtime")):
        if source.is_dir():
            shutil.copytree(source, destination / name)
    print(
        f"回滚证据已保留，供手动恢复：{destination}"
        if use_chinese()
        else f"Rollback evidence preserved for manual recovery: {destination}",
        file=sys.stderr,
    )
    return destination


def rollback_upgrade(args: argparse.Namespace, hook_journal: Path, runtime_journal: Path) -> bool:
    hook_restored = restore_hook_journal(args, hook_journal)
    runtime_restored = restore_runtime_journal(args, runtime_journal)
    if not (hook_restored and runtime_restored):
        preserve_failed_transaction_journals(hook_journal, runtime_journal)
    return hook_restored and runtime_restored


def migrate_legacy_machine_home(args: argparse.Namespace, runtime_journal: Path, source_root: Path) -> int:
    canonical = validate_managed_home_path(machine_home(), label="Tenetora canonical home")
    default_canonical = validate_managed_home_path(default_machine_home(), label="default Tenetora home")
    script = source_root / "scripts" / "migrate_machine_home.py"
    if not same_managed_home(canonical, default_canonical) or not script.is_file() or args.no_cli:
        return 0
    command = [
        sys.executable,
        str(script),
        "--canonical",
        str(canonical),
        "--legacy",
        str(default_legacy_machine_home()),
        "--json",
        "--runtime-rollback-journal",
        str(runtime_journal),
        "--bin-dir",
        str(args.bin_dir or (machine_home() / "bin")),
    ]
    result = subprocess.run(
        command,
        env=os.environ.copy(),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        **install_lock_subprocess_kwargs(),
    )
    if result.returncode == 0:
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            payload = {}
        if payload.get("status") in {"migrated", "merged"}:
            print(
                f"机器主目录：已迁移到 {canonical}"
                if use_chinese()
                else f"Machine home: migrated to {canonical}"
            )
        elif args.verbose and result.stdout:
            print(result.stdout, end="")
        return 0
    if result.stdout:
        print(result.stdout, end="", file=sys.stderr)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    print(
        "新 release、CLI 与 Git Hook 验证后，旧机器主目录迁移失败。"
        if use_chinese()
        else "Legacy machine-home migration failed after release, CLI, and Git hook verification.",
        file=sys.stderr,
    )
    return result.returncode


def _apply_upgrade(args: argparse.Namespace) -> int:
    source_root = package_root()
    if source_root is None:
        print("升级被阻断：release 来源不可用" if use_chinese() else "Upgrade blocked: release source is unavailable", file=sys.stderr)
        return 1
    try:
        release_manifest = local_release_manifest(source_root)
    except ReleaseStoreError as exc:
        print(f"升级被阻断：{issue_text(exc)}" if use_chinese() else f"Upgrade blocked: {exc}", file=sys.stderr)
        return 1
    controlled_bridge = bool(args.bridge_source_version)
    if controlled_bridge:
        preflight = check_payload(args, release_manifest)
        machine_plan = preflight.get("machine") if isinstance(preflight.get("machine"), dict) else {}
        if machine_plan.get("result") in {"BLOCKED", "PARTIAL"} or machine_plan.get("status") == "blocked":
            print_preflight_failure(
                args,
                machine_plan,
                str(machine_plan.get("error") or "machine-wide upgrade preflight is blocked"),
            )
            return 1
    else:
        machine_plan = {"mode": "legacy-local-worker"}
    print("Tenetora 升级" if use_chinese() else "Tenetora Upgrade")
    print(f"{'来源' if use_chinese() else 'Source'}: {SKILL_DIR}")
    print(f"{'范围' if use_chinese() else 'Scope'}: {selected_scope(args) or 'existing'}")
    print(f"{'工具' if use_chinese() else 'Tools'}: {args.tools}")
    transaction: dict[str, object] | None = None
    if not args.dry_run and controlled_bridge:
        existing = load_transaction(machine_home())
        if (
            existing is not None
            and existing.get("status") == "running"
            and existing.get("target_version") == release_manifest["version"]
        ):
            transaction = resume_transaction(
                machine_home(),
                target_version=str(release_manifest["version"]),
                plan=machine_plan,
            )
        else:
            transaction = begin_transaction(
                machine_home(),
                source_version=str(args.bridge_source_version or __version__),
                target_version=str(release_manifest["version"]),
                plan=machine_plan,
            )

    def record_stage(stage: str, *, status: str = "running", error_code: str = "") -> None:
        nonlocal transaction
        if transaction is None:
            return
        transaction = advance_transaction(
            machine_home(),
            str(transaction["transaction_id"]),
            int(transaction["revision"]),
            stage=stage,
            status=status,
            error_code=error_code,
        )

    path_snapshot: dict[str, object] | None = None
    activation_receipt: dict[str, object] | None = None

    def fail(
        code: int,
        stage: str,
        hook_journal: Path,
        runtime_journal: Path,
        *,
        restored_code: int | None = None,
    ) -> int:
        path_restored = path_snapshot is None or restore_user_path(path_snapshot)
        # The runtime journal includes the current/source pointers after the
        # activation checkpoint. Restore that complete snapshot before the
        # release-specific compensator validates the same pointers; reversing
        # these operations makes the compensators report their own change as
        # an external concurrent edit.
        restored = rollback_upgrade(args, hook_journal, runtime_journal)
        release_restored = True
        if activation_receipt is not None:
            try:
                restore_release_activation(machine_home(), activation_receipt)
            except ReleaseStoreError as exc:
                release_restored = False
                print(
                    f"升级被阻断：{issue_text(exc)}"
                    if use_chinese()
                    else f"Upgrade blocked: {exc}",
                    file=sys.stderr,
                )
        fully_restored = restored and path_restored and release_restored
        record_stage(
            "rolled-back" if fully_restored else stage,
            status="rolled-back" if fully_restored else "blocked",
            error_code=stage,
        )
        return (restored_code if restored_code is not None else code) if fully_restored else 1

    with tempfile.TemporaryDirectory(prefix="tenetora-upgrade-hooks-") as temporary:
        hook_journal = Path(temporary) / "commit-hooks"
        runtime_journal = Path(temporary) / "managed-runtime"
        if not args.dry_run:
            print("[0/4] 正在准备升级并保存回滚快照……" if use_chinese() else "[0/4] Preparing the upgrade and saving rollback snapshots...")
            runtime_snapshot = subprocess.run(
                runtime_action_args(args, "--snapshot", runtime_journal),
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **install_lock_subprocess_kwargs(),
            )
            if runtime_snapshot.returncode != 0:
                if runtime_snapshot.stderr:
                    print(runtime_snapshot.stderr, end="", file=sys.stderr)
                record_stage("runtime-snapshot-failed", status="blocked", error_code="runtime-snapshot")
                return runtime_snapshot.returncode
            hook_snapshot = subprocess.run(
                hook_action_args(args, "--snapshot-hooks", hook_journal),
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **install_lock_subprocess_kwargs(),
            )
            if hook_snapshot.returncode != 0:
                if hook_snapshot.stderr:
                    print(hook_snapshot.stderr, end="", file=sys.stderr)
                record_stage("hook-snapshot-failed", status="blocked", error_code="hook-snapshot")
                return hook_snapshot.returncode
            record_stage("snapshotted")

        command = install_args(args, hook_journal if not args.dry_run else None)
        print("[1/4] 正在更新已有安装面……" if use_chinese() else "[1/4] Updating existing installation surfaces...")
        result = subprocess.run(
            command,
            env=controller_child_environment(),
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            **install_lock_subprocess_kwargs(),
        )
        install_result = result.returncode
        if install_result not in {0, 2}:
            return install_result if args.dry_run else fail(install_result, "surface-apply", hook_journal, runtime_journal)
        if not args.dry_run:
            record_stage("surfaces-applied")

        print("[2/4] 正在准备 CLI 并稳定 PATH……" if use_chinese() else "[2/4] Preparing the CLI and stabilizing PATH...")
        ensure_cli = SKILL_DIR / "scripts" / "ensure_cli.py"
        if args.no_cli:
            print("CLI 自举：已跳过" if use_chinese() else "CLI bootstrap: skipped")
        elif args.dry_run:
            print("CLI 自举：仅演练" if use_chinese() else "CLI bootstrap: dry-run")
            return install_result
        else:
            effective_bin_dir = args.bin_dir or (machine_home() / "bin")
            if args.configure_path:
                path_snapshot = snapshot_user_path()
            cli_command = [
                sys.executable,
                str(ensure_cli),
                "--install",
                "--json",
                "--bin-dir",
                str(effective_bin_dir),
            ]
            cli_command.extend(["--runtime-rollback-journal", str(runtime_journal)])
            if args.configure_path:
                cli_command.append("--configure-path")
            cli_result = subprocess.run(
                cli_command,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                **install_lock_subprocess_kwargs(),
            )
            if cli_result.returncode != 0:
                if cli_result.stdout:
                    print(cli_result.stdout, end="")
                if cli_result.stderr:
                    print(cli_result.stderr, end="", file=sys.stderr)
                print(
                    f"CLI 自举失败，退出码：{cli_result.returncode}。"
                    if use_chinese()
                    else f"CLI bootstrap failed with exit code {cli_result.returncode}."
                )
                return fail(
                    cli_result.returncode,
                    "cli-bootstrap",
                    hook_journal,
                    runtime_journal,
                    restored_code=2,
                )
            if args.verbose and cli_result.stdout:
                print(cli_result.stdout, end="")
            print("CLI：已就绪" if use_chinese() else "CLI bootstrap: ready")
            if path_snapshot is not None:
                path_snapshot = checkpoint_user_path(path_snapshot)
            record_stage("cli-bootstrapped")

        print("[3/4] 正在收敛提交 Hook……" if use_chinese() else "[3/4] Converging commit hooks...")
        converge = hook_action_args(args, "--converge-hooks", hook_journal)
        convergence_result = subprocess.run(
            converge,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **install_lock_subprocess_kwargs(),
        )
        if convergence_result.returncode != 0:
            if convergence_result.stdout:
                print(convergence_result.stdout, end="", file=sys.stderr)
            if convergence_result.stderr:
                print(convergence_result.stderr, end="", file=sys.stderr)
            return fail(convergence_result.returncode, "hook-convergence", hook_journal, runtime_journal)
        if args.verbose and convergence_result.stdout:
            print(convergence_result.stdout, end="")
        if args.dry_run:
            return install_result
        if not finalize_hook_journal(args, hook_journal):
            return fail(1, "hook-journal-finalize", hook_journal, runtime_journal)
        if install_result != 0:
            return fail(install_result, "surface-partial", hook_journal, runtime_journal)
        record_stage("hooks-converged")

        print("[4/4] 正在激活 release 并完成复检……" if use_chinese() else "[4/4] Activating the release and completing verification...")
        if controlled_bridge:
            try:
                activation_receipt = activate_release(machine_home(), source_root)
            except ReleaseStoreError as exc:
                print(f"升级被阻断：{issue_text(exc)}" if use_chinese() else f"Upgrade blocked: {exc}", file=sys.stderr)
                return fail(1, "release-activation", hook_journal, runtime_journal)
            if not checkpoint_runtime_journal(args, runtime_journal, source_root):
                return fail(1, "pre-migration-checkpoint", hook_journal, runtime_journal)
            migration_status = migrate_legacy_machine_home(args, runtime_journal, source_root)
            if migration_status != 0:
                return fail(2, "machine-home-migration", hook_journal, runtime_journal)
            if not checkpoint_runtime_journal(args, runtime_journal, source_root):
                return fail(1, "runtime-checkpoint", hook_journal, runtime_journal)
            refresh_onboarding_state()
            record_stage("completed", status="completed")
        return install_result


def apply_upgrade(args: argparse.Namespace) -> int:
    previous_bytecode_setting = os.environ.get("PYTHONDONTWRITEBYTECODE")
    previous_migration_setting = os.environ.get("TENETORA_DEFER_MACHINE_HOME_MIGRATION")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["TENETORA_DEFER_MACHINE_HOME_MIGRATION"] = "1"
    try:
        lock_roots = [machine_home()]
        if args.bin_dir is not None:
            lock_roots.append(args.bin_dir.expanduser().absolute().parent)
        with install_transaction_locks(lock_roots):
            return _apply_upgrade(args)
    except (RuntimeError, UpgradeTransactionError) as exc:
        print(f"升级被阻断：{issue_text(exc)}" if use_chinese() else f"Upgrade blocked: {exc}", file=sys.stderr)
        return 1
    finally:
        if previous_bytecode_setting is None:
            os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
        else:
            os.environ["PYTHONDONTWRITEBYTECODE"] = previous_bytecode_setting
        if previous_migration_setting is None:
            os.environ.pop("TENETORA_DEFER_MACHINE_HOME_MIGRATION", None)
        else:
            os.environ["TENETORA_DEFER_MACHINE_HOME_MIGRATION"] = previous_migration_setting


def worker_arguments(args: argparse.Namespace, action: str) -> list[str]:
    forwarded = [
        "--worker",
        "--check" if action == "check" else "--apply",
        "--path",
        str(args.path),
        "--tools",
        args.tools,
        "--mode",
        args.mode,
        "--progress",
        args.progress,
        "--bridge-source-version",
        str(args.bridge_source_version or __version__),
    ]
    scope = selected_scope(args)
    if scope:
        forwarded.extend(["--scope", scope])
    forwarded.extend(["--codex-hooks", args.codex_hooks])
    if args.allow_tracked_codex_hooks:
        forwarded.append("--allow-tracked-codex-hooks")
    for enabled, option in (
        (args.force, "--force"),
        (args.dry_run, "--dry-run"),
        (args.verbose, "--verbose"),
        (args.prune_shadowed, "--prune-shadowed"),
        (args.no_prune_shadowed, "--no-prune-shadowed"),
        (args.allow_skills_only, "--allow-skills-only"),
        (args.require_full, "--require-full"),
        (args.no_cli, "--no-cli"),
        (args.configure_path, "--configure-path"),
        (args.bootstrap_bridge, "--bootstrap-bridge"),
        (args.bootstrap_install, "--bootstrap-install"),
    ):
        if enabled:
            forwarded.append(option)
    if args.bin_dir:
        forwarded.extend(["--bin-dir", str(args.bin_dir)])
    if args.log_file is not None:
        forwarded.extend(["--log-file", str(args.log_file)])
    if args.events_jsonl is not None:
        forwarded.extend(["--events-jsonl", str(args.events_jsonl)])
    if action == "check":
        forwarded.append("--json")
    return forwarded


def invoke_worker(source_root: Path, args: argparse.Namespace, action: str, *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    entrypoint = source_root / "skills" / "tenetora" / "scripts" / "upgrade_skill.py"
    if not entrypoint.is_file():
        raise UpgradeAcquireError("release upgrade worker is missing")
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if action == "apply":
        env = controller_child_environment()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, str(entrypoint), *worker_arguments(args, action)],
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=False,
        **install_lock_subprocess_kwargs(),
    )


def render_apply_result(
    result: subprocess.CompletedProcess[str],
    manifest: dict[str, object],
    source_kind: str,
    *,
    dry_run: bool = False,
) -> dict[str, object]:
    transaction: dict[str, object] | None
    try:
        transaction = load_transaction(machine_home())
    except UpgradeTransactionError:
        transaction = None
    return {
        "status": "dry-run" if dry_run and result.returncode == 0 else ("completed" if result.returncode == 0 else "failed"),
        "action": "upgrade",
        "exit_code": result.returncode,
        "release": {
            key: manifest.get(key)
            for key in ("version", "commit", "bridge_protocol", "installer_protocol", "artifact", "sha256")
            if manifest.get(key) is not None
        },
        "acquisition": source_kind,
        "transaction": {
            key: transaction.get(key)
            for key in ("transaction_id", "status", "stage", "source_version", "target_version", "plan_sha256", "revision")
        } if transaction is not None else None,
        "error_code": "" if result.returncode == 0 else "upgrade-worker-failed",
    }


def render_blocked_result(
    manifest: dict[str, object] | None,
    source_kind: str,
    error_code: str,
    exit_code: int = 1,
) -> dict[str, object]:
    return {
        "status": "blocked",
        "action": "upgrade",
        "exit_code": exit_code,
        "release": {
            key: manifest.get(key)
            for key in ("version", "commit", "bridge_protocol", "installer_protocol", "artifact", "sha256")
            if manifest is not None and manifest.get(key) is not None
        },
        "acquisition": source_kind or "unavailable",
        "transaction": None,
        "error_code": error_code,
    }


def render_terminal_apply_summary(
    manifest: dict[str, object] | None,
    source_kind: str,
    args: argparse.Namespace,
    exit_code: int,
    *,
    failure_stage: str | None = None,
    dry_run: bool = False,
) -> str:
    """Render the explicit terminal outcome after the worker has stopped."""

    bootstrap_install = bool(getattr(args, "bootstrap_install", False))
    if dry_run and exit_code == 0:
        status = "演练完成" if use_chinese() else "dry run completed"
    elif exit_code == 0:
        status = "成功" if use_chinese() else "SUCCESS"
    elif exit_code == 2:
        status = "部分失败" if use_chinese() else "PARTIAL FAILURE"
    else:
        status = "失败" if use_chinese() else "FAILED"

    stage_labels = {
        "preflight": "预检" if use_chinese() else "preflight",
        "surface-apply": "安装面更新" if use_chinese() else "installation update",
        "release-acquisition": "发布包获取" if use_chinese() else "release acquisition",
        "worker": (
            "安装执行" if bootstrap_install else "升级执行"
        ) if use_chinese() else (
            "installation execution" if bootstrap_install else "upgrade execution"
        ),
    }
    target_version = str((manifest or {}).get("version") or "未知") if use_chinese() else str((manifest or {}).get("version") or "unknown")
    scope = selected_scope(args) or ("global" if bootstrap_install else "existing")
    result_label = (
        f"安装结果：{status}" if bootstrap_install else f"升级结果：{status}"
    ) if use_chinese() else (
        f"Installation result: {status}" if bootstrap_install else f"Upgrade result: {status}"
    )
    scope_label = (
        f"安装范围：{scope}" if bootstrap_install else f"升级范围：{scope}"
    ) if use_chinese() else (
        f"Installation scope: {scope}" if bootstrap_install else f"Upgrade scope: {scope}"
    )
    lines = [
        result_label,
        f"目标版本：{target_version}" if use_chinese() else f"Target version: {target_version}",
        f"发布来源：{source_kind or ('未知' if use_chinese() else 'unknown')}"
        if use_chinese()
        else f"Release source: {source_kind or 'unknown'}",
        scope_label,
        f"处理工具：{args.tools}" if use_chinese() else f"Tools: {args.tools}",
    ]
    if failure_stage is not None and exit_code != 0:
        label = stage_labels.get(failure_stage, failure_stage)
        lines.append(f"失败阶段：{label}" if use_chinese() else f"Failed stage: {label}")
    if dry_run and exit_code == 0:
        if bootstrap_install:
            lines.append(
                "下一步：确认演练结果后移除 -DryRun，再重新运行安装命令"
                if use_chinese()
                else "Next: review the dry-run result, then rerun the installer without -DryRun",
            )
        else:
            lines.append(
                "下一步：确认演练结果后移除 --dry-run，再运行 tenetora upgrade"
                if use_chinese()
                else "Next: review the dry-run result, then run tenetora upgrade without --dry-run",
            )
    elif exit_code == 0:
        lines.append("下一步：tenetora doctor" if use_chinese() else "Next: tenetora doctor")
    elif exit_code == 2:
        if bootstrap_install:
            lines.append(
                "下一步：检查上方未完成的安装面，然后重新运行安装命令"
                if use_chinese()
                else "Next: review incomplete installation surfaces above, then rerun the installer",
            )
        else:
            lines.append(
                "下一步：检查上方未完成的安装面，然后重新运行 tenetora upgrade"
                if use_chinese()
                else "Next: review incomplete installation surfaces above, then rerun tenetora upgrade",
            )
    else:
        if bootstrap_install:
            lines.append(
                "下一步：检查上方错误；修复后重新运行安装命令"
                if use_chinese()
                else "Next: review the error above, fix it, and rerun the installer",
            )
        else:
            lines.append(
                "下一步：检查上方错误；修复后重新运行 tenetora upgrade"
                if use_chinese()
                else "Next: review the error above, fix it, and rerun tenetora upgrade",
            )
    return "\n".join(lines)


def render_current_result(
    manifest: dict[str, object],
    source_kind: str,
    args: argparse.Namespace,
) -> dict[str, object]:
    return {
        "status": "current",
        "action": "upgrade",
        "exit_code": 0,
        "update_available": False,
        "no_op": True,
        "reason": "already-latest",
        "release": {
            key: manifest.get(key)
            for key in ("version", "commit", "bridge_protocol", "installer_protocol", "artifact", "sha256")
            if manifest.get(key) is not None
        },
        "acquisition": source_kind,
        "scope": selected_scope(args) or "existing",
        "tools": args.tools,
        "transaction": None,
    }


def render_terminal_current_summary(
    manifest: dict[str, object],
    source_kind: str,
    args: argparse.Namespace,
) -> str:
    version = str(manifest.get("version") or "unknown")
    if use_chinese():
        return "\n".join(
            (
                "升级结果：已是最新版本",
                f"当前版本：{version}",
                f"目标版本：{version}",
                f"发布来源：{source_kind}",
                f"升级范围：{selected_scope(args) or 'existing'}",
                f"处理工具：{args.tools}",
                "未下载 ZIP，未执行安装。若需修复安装面，请使用 tenetora upgrade --force。",
                "下一步：tenetora doctor",
            )
        )
    return "\n".join(
        (
            "Upgrade result: already latest",
            f"Current version: {version}",
            f"Target version: {version}",
            f"Release source: {source_kind}",
            f"Upgrade scope: {selected_scope(args) or 'existing'}",
            f"Tools: {args.tools}",
            "ZIP download and installation were skipped. Use tenetora upgrade --force to repair installation surfaces.",
            "Next: tenetora doctor",
        )
    )


def ensure_release_direction(manifest: dict[str, object]) -> bool:
    """Return whether the release is newer and reject accidental downgrades."""

    target = version_key(str(manifest.get("version") or ""))
    current = version_key(__version__)
    if target < current:
        raise UpgradeContractError("release version is older than the installed version")
    return target > current


def cache_update_hint(manifest: dict[str, object], source_kind: str) -> None:
    """Cache only a successfully authenticated remote release observation."""

    if source_kind != "release":
        return
    try:
        record_success(
            machine_home(),
            current_version=__version__,
            manifest=manifest,
        )
    except UpdateHintError:
        # Update hints are advisory. A read-only or damaged state directory
        # must never turn an otherwise valid upgrade check into a failure.
        return


def cache_update_failure(source_override: Path | None, zip_file: Path | None) -> None:
    """Remember a remote acquisition failure without hiding the last release."""

    if source_override is not None or zip_file is not None:
        return
    try:
        record_failure(machine_home(), current_version=__version__)
    except UpdateHintError:
        return


def run_release_controller(args: argparse.Namespace) -> int:
    # A stale Windows PowerShell marker must not poison the controller's
    # read-only release preflight.  Valid inherited locks are left intact and
    # continue through the normal proof/handle propagation path.
    clear_stale_controller_lock_environment()
    source_override = args.source_dir
    if (
        source_override is None
        and args.zip_file is None
        and not args.explicit_remote_source
        and not release_source_configured()
    ):
        source_override = developer_checkout()
    observed_manifest: dict[str, object] | None = None
    observed_source_kind = ""
    try:
        with tempfile.TemporaryDirectory(prefix="tenetora-release-acquire-") as temporary:
            workdir = Path(temporary)
            prevalidated_manifest: dict[str, object] | None = None
            prevalidated_source_kind: str | None = None
            should_inspect_before_download = (
                args.action == "apply"
                and not args.force
                and not args.bootstrap_bridge
                and source_override is None
            )
            if should_inspect_before_download:
                if source_override is not None:
                    prevalidated_manifest = local_release_manifest(source_override)
                    prevalidated_source_kind = "developer-source"
                else:
                    prevalidated_manifest, prevalidated_source_kind = inspect_release_manifest(
                        workdir,
                        manifest_url=args.manifest_url,
                        zip_url=args.zip_url,
                        mirror_url=args.mirror_url,
                        zip_file=args.zip_file,
                        expected_sha256=args.sha256,
                    )
                observed_manifest = prevalidated_manifest
                observed_source_kind = prevalidated_source_kind or ""
                if not ensure_release_direction(prevalidated_manifest):
                    cache_update_hint(prevalidated_manifest, prevalidated_source_kind)
                    if args.json:
                        print(render_json(render_current_result(prevalidated_manifest, prevalidated_source_kind, args)))
                    else:
                        print(render_terminal_current_summary(prevalidated_manifest, prevalidated_source_kind, args))
                    return 0

            acquire_kwargs: dict[str, object] = {
                "manifest_url": args.manifest_url,
                "zip_url": args.zip_url,
                "mirror_url": args.mirror_url,
                "zip_file": args.zip_file,
                "expected_sha256": args.sha256,
                "source_dir": source_override,
            }
            if prevalidated_manifest is not None and prevalidated_source_kind == "release":
                acquire_kwargs["prevalidated_manifest"] = prevalidated_manifest
            source_root, manifest, source_kind = acquire_release(workdir, **acquire_kwargs)
            observed_manifest = manifest
            observed_source_kind = source_kind
            ensure_release_direction(manifest)
            cache_update_hint(manifest, source_kind)
            if not args.bootstrap_bridge:
                ensure_source_supported(str(args.bridge_source_version or __version__), manifest)
            if args.action == "check":
                result = invoke_worker(source_root, args, "check", capture=True)
                if result.stderr:
                    print(result.stderr, end="", file=sys.stderr)
                if result.stdout:
                    try:
                        payload = json.loads(result.stdout)
                    except json.JSONDecodeError:
                        print(result.stdout, end="")
                    else:
                        payload["acquisition"] = source_kind
                        payload["release"] = {
                            key: manifest.get(key)
                            for key in (
                                "version", "commit", "bridge_protocol", "minimum_bridge_version",
                                "installer_protocol", "artifact", "sha256",
                            )
                            if manifest.get(key) is not None
                        }
                        payload["update_available"] = str(manifest.get("version") or "") != __version__
                        print(render_json(payload) if args.json else render_check(payload))
                return result.returncode

            if args.bootstrap_bridge:
                print(
                    "[2/6] 正在检查环境和计划安装能力……"
                    if use_chinese()
                    else "[2/6] Checking environment and planned capabilities..."
                , flush=True)
            preflight = invoke_worker(source_root, args, "check", capture=True)
            if preflight.returncode != 0:
                failure_payload: object
                try:
                    failure_payload = json.loads(preflight.stdout or "{}")
                except json.JSONDecodeError:
                    failure_payload = {
                        "error": (preflight.stderr or preflight.stdout).strip()
                        or f"release worker exited {preflight.returncode}"
                    }
                persist_preflight_failure(
                    args,
                    failure_payload,
                    str(
                        (failure_payload.get("error") if isinstance(failure_payload, dict) else None)
                        or "release worker preflight failed"
                    ),
                )
                if args.json:
                    print(render_json(render_blocked_result(manifest, source_kind, "preflight-failed", preflight.returncode)))
                else:
                    if preflight.stdout:
                        print(preflight.stdout, end="")
                    if preflight.stderr:
                        print(preflight.stderr, end="", file=sys.stderr)
                    print(
                        render_terminal_apply_summary(
                            manifest,
                            source_kind,
                            args,
                            preflight.returncode,
                            failure_stage="preflight",
                        ),
                        file=sys.stderr,
                    )
                return preflight.returncode
            try:
                preflight_payload = json.loads(preflight.stdout or "{}")
            except json.JSONDecodeError:
                persist_preflight_failure(
                    args,
                    {"error": "release worker returned invalid preflight JSON"},
                    "release worker returned invalid preflight JSON",
                )
                print(
                    "升级被阻断：release worker 返回了无效的预检 JSON"
                    if use_chinese()
                    else "Upgrade blocked: release worker returned invalid preflight JSON",
                    file=sys.stderr,
                )
                if not args.json:
                    print(
                        render_terminal_apply_summary(
                            manifest,
                            source_kind,
                            args,
                            1,
                            failure_stage="preflight",
                        ),
                        file=sys.stderr,
                    )
                return 1
            machine = preflight_payload.get("machine")
            if isinstance(machine, dict) and (
                machine.get("result") in {"BLOCKED", "PARTIAL"} or machine.get("status") == "blocked"
            ):
                if args.bootstrap_bridge:
                    print("预检：失败" if use_chinese() else "Preflight: failed", flush=True)
                if args.json:
                    persist_preflight_failure(
                        args,
                        machine,
                        str(machine.get("error") or "machine-wide upgrade preflight is blocked"),
                    )
                    print(render_json(render_blocked_result(manifest, source_kind, "machine-plan-blocked")))
                else:
                    print_preflight_failure(
                        args,
                        machine,
                        str(machine.get("error") or "machine-wide upgrade preflight is blocked"),
                    )
                    print(
                        render_terminal_apply_summary(
                            manifest,
                            source_kind,
                            args,
                            3,
                            failure_stage="preflight",
                        ),
                        file=sys.stderr,
                    )
                return 3
            if args.bootstrap_bridge and not args.json:
                print("预检结果：就绪" if use_chinese() else "Preflight: ready", flush=True)
            if args.dry_run:
                result = invoke_worker(source_root, args, "apply", capture=args.json)
                if args.json:
                    print(render_json(render_apply_result(result, manifest, source_kind, dry_run=True)))
                else:
                    print(
                        render_terminal_apply_summary(
                            manifest,
                            source_kind,
                            args,
                            result.returncode,
                            failure_stage="worker" if result.returncode else None,
                            dry_run=True,
                        ),
                        file=sys.stderr if result.returncode else sys.stdout,
                    )
                return result.returncode
            if args.bootstrap_bridge:
                print(
                    "[3/6] 正在安装或升级 skills……"
                    if use_chinese()
                    else "[3/6] Installing or updating skills..."
                , flush=True)
            lock_roots = [machine_home()]
            if args.bin_dir is not None:
                lock_roots.append(args.bin_dir.expanduser().absolute().parent)
            with install_transaction_locks(lock_roots):
                release_root, _changed = stage_release(source_root, machine_home(), manifest)
                if not args.json:
                    print(
                        (f"目标 release：{manifest['version']}（{source_kind}）")
                        if use_chinese()
                        else f"Target release: {manifest['version']} ({source_kind})"
                    )
                result = invoke_worker(release_root, args, "apply", capture=args.json)
                if args.json:
                    print(render_json(render_apply_result(result, manifest, source_kind)))
                else:
                    print(
                        render_terminal_apply_summary(
                            manifest,
                            source_kind,
                            args,
                            result.returncode,
                            failure_stage="worker" if result.returncode else None,
                        ),
                        file=sys.stderr if result.returncode else sys.stdout,
                    )
                return result.returncode
    except (OSError, UpgradeAcquireError, UpgradeContractError, ReleaseStoreError) as exc:
        cache_update_failure(source_override, args.zip_file)
        if args.json:
            print(render_json(render_blocked_result(observed_manifest, observed_source_kind, "acquisition-or-contract-failed")))
        else:
            print(f"升级被阻断：{issue_text(exc)}" if use_chinese() else f"Upgrade blocked: {exc}", file=sys.stderr)
            print(
                render_terminal_apply_summary(
                    observed_manifest,
                    observed_source_kind,
                    args,
                    1,
                    failure_stage="release-acquisition",
                ),
                file=sys.stderr,
            )
        return 1


def render_check(payload: dict[str, object]) -> str:
    lines = [
        "Tenetora 升级" if use_chinese() else "Tenetora Upgrade",
        f"{'当前版本' if use_chinese() else 'Current version'}: {payload.get('version', 'unknown')}",
    ]
    release = payload.get("release")
    if isinstance(release, dict) and release.get("version"):
        lines.append(f"{'目标版本' if use_chinese() else 'Target version'}: {release['version']}")
    lines.append(f"{'状态' if use_chinese() else 'Status'}: {payload.get('status', 'unknown')}")
    issues = payload.get("issues")
    if isinstance(issues, list) and issues:
        lines.append("问题：" if use_chinese() else "Issues:")
        lines.extend(f"- {issue_text(issue)}" for issue in issues)
    machine = payload.get("machine")
    if isinstance(machine, dict):
        lines.append(
            (
                f"机器计划：项目数={machine.get('project_count', 0)} 安装面={machine.get('surface_count', 0)} 结果={machine.get('result', machine.get('status', 'unknown'))}"
                if use_chinese()
                else f"Machine plan: projects={machine.get('project_count', 0)} surfaces={machine.get('surface_count', 0)} result={machine.get('result', machine.get('status', 'unknown'))}"
            )
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="tenetora upgrade",
        description="从已验证 release 更新全部已有 Tenetora 工具和项目。" if use_chinese() else "Upgrade every existing Tenetora tool and project from a verified release.",
        add_help=False,
    )
    parser.add_argument("--help", action="help", help="显示帮助并退出。" if use_chinese() else "Show this help message and exit.")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", dest="action", action="store_const", const="check", help="只读检查升级计划" if use_chinese() else "Inspect upgrade state without writing")
    action.add_argument("--apply", dest="action", action="store_const", const="apply", help="兼容别名；应用升级（默认动作）" if use_chinese() else "Compatibility alias; apply the upgrade (the default)")
    parser.set_defaults(action="apply")
    parser.add_argument("-p", "--path", "--project", default=".", type=existing_project_path, metavar="<project-dir>", help="项目上下文；选择 scope 时也可作为显式项目过滤器。" if use_chinese() else "Project context or explicit project filter when a scope is selected.")
    parser.add_argument("-t", "--tools", default="auto", help="auto、all 或工具列表：agents,codex,claude,cursor,opencode,pi,zcode" if use_chinese() else "auto, all, or comma-separated tools: agents,codex,claude,cursor,opencode,pi,zcode")
    parser.add_argument("-m", "--mode", choices=("auto", "symlink", "copy"), default="auto", help="升级安装模式" if use_chinese() else "Install mode for the upgrade")
    parser.add_argument("-f", "--force", action="store_true", help="替换冲突的 skill 安装" if use_chinese() else "Replace conflicting skill installs")
    parser.add_argument("--dry-run", action="store_true", help="只显示动作，不写入" if use_chinese() else "Print upgrade actions without writing")
    parser.add_argument(
        "--progress",
        choices=("auto", "always", "never"),
        default="auto",
        help="进度显示：自动、始终或从不" if use_chinese() else "Render upgrade progress automatically, always, or never",
    )
    parser.add_argument("--no-progress", dest="progress", action="store_const", const="never", help="禁用动态进度" if use_chinese() else "Disable dynamic progress output")
    parser.add_argument("--verbose", action="store_true", help="输出详细安装动作" if use_chinese() else "Print detailed installer actions in addition to the summary")
    parser.add_argument(
        "--prune-shadowed",
        action="store_true",
        help="只移除被有效 runtime 遮蔽且未修改的 Tenetora 受管 adapter" if use_chinese() else "Remove only unchanged Tenetora managed runtime adapters shadowed by the selected effective runtime",
    )
    parser.add_argument(
        "--no-prune-shadowed",
        action="store_true",
        help="禁用对未修改受管 shadow runtime 的自动清理" if use_chinese() else "Disable automatic removal of unchanged managed shadow runtimes",
    )
    parser.add_argument(
        "--codex-hooks",
        choices=("auto", "native", "project", "off"),
        default="auto",
        help="Codex Hook 模式：auto、native、project 或 off" if use_chinese() else "Codex hook mode: auto, native, project, or off",
    )
    parser.add_argument(
        "--allow-tracked-codex-hooks",
        action="store_true",
        help="允许结构化更新 Git 已跟踪的项目 Codex hooks" if use_chinese() else "Allow structured updates to a Git-tracked project Codex hooks file",
    )
    fallback = parser.add_mutually_exclusive_group()
    fallback.add_argument(
        "--allow-skills-only",
        action="store_true",
        help="明确允许低于宿主最大原生能力" if use_chinese() else "Explicitly allow fallback below a tool's native maximum capability",
    )
    fallback.add_argument(
        "--require-full",
        action="store_true",
        help="所选工具无法达到最大能力时在写入前阻断" if use_chinese() else "Block before writes when a selected tool cannot reach its maximum capability",
    )
    parser.add_argument("--no-cli", action="store_true", help="跳过 CLI shim 自举" if use_chinese() else "Skip CLI shim bootstrap")
    parser.add_argument("--bin-dir", type=Path, default=None, help="CLI shim bin 目录" if use_chinese() else "CLI shim bin directory")
    path_group = parser.add_mutually_exclusive_group()
    path_group.add_argument("--configure-path", dest="configure_path", action="store_true", help="安装或刷新受管用户 PATH" if use_chinese() else "Install or refresh the managed user PATH entry")
    path_group.add_argument("--no-configure-path", dest="configure_path", action="store_false", help="不修改用户 PATH" if use_chinese() else "Do not modify the user PATH entry")
    parser.set_defaults(configure_path=None)
    parser.add_argument("--manifest-url", default=None, help="已验证 release manifest URL；必须和 --zip-url 成对使用" if use_chinese() else "Verified release manifest URL; must be used with --zip-url")
    parser.add_argument("--zip-url", default=None, help="release ZIP URL；必须和 --manifest-url 成对使用" if use_chinese() else "Release ZIP URL; must be used with --manifest-url")
    parser.add_argument("--mirror-url", default=None, help="企业 HTTPS 镜像基址；目录下必须有 manifest.json 和 tenetora-latest.zip" if use_chinese() else "Enterprise HTTPS mirror base; must contain manifest.json and tenetora-latest.zip")
    parser.add_argument("--zip-file", type=Path, default=None, help="离线 release ZIP；需要 --sha256 或同名 sidecar" if use_chinese() else "Offline release ZIP; requires --sha256 or a sibling sidecar")
    parser.add_argument("--sha256", default="", help="--zip-file 的预期 SHA256" if use_chinese() else "Expected SHA256 for --zip-file")
    parser.add_argument("--source-dir", type=Path, default=None, help="仅开发使用的 release 源目录" if use_chinese() else "Developer-only release source directory")
    parser.add_argument("--log-file", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--events-jsonl", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--bridge-source-version", default="", help=argparse.SUPPRESS)
    parser.add_argument("--bootstrap-bridge", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--bootstrap-install", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("-j", "--json", action="store_true", help="输出机器可读 JSON" if use_chinese() else "Print machine-readable JSON")
    add_scope_flags(parser)
    args = parser.parse_args(effective_argv)
    args.compat_apply = "--apply" in effective_argv
    args.explicit_remote_source = any(
        token in {"--manifest-url", "--zip-url", "--mirror-url"}
        or token.startswith("--manifest-url=")
        or token.startswith("--zip-url=")
        or token.startswith("--mirror-url=")
        for token in effective_argv
    )
    if args.configure_path is None:
        args.configure_path = not args.worker

    if not args.worker:
        if args.compat_apply:
            print(
                "提示：--apply 是 0.3.x 兼容别名；以后直接使用 tenetora upgrade。"
                if use_chinese()
                else "Notice: --apply is a 0.3.x compatibility alias; use tenetora upgrade from now on.",
                file=sys.stderr,
            )
        return run_release_controller(args)

    if args.action == "check":
        # An older installed controller may fetch this newer release and invoke
        # its worker directly.  Clean a proven-stale PowerShell lock marker
        # here as well, before the worker launches install-skill preflight.
        # A live outer lock remains intact because the helper verifies it.
        clear_stale_controller_lock_environment()
        release_manifest = None
        root = package_root()
        if root is not None and (root / "manifest.json").is_file():
            try:
                release_manifest = read_release_manifest(root)
            except ReleaseStoreError:
                release_manifest = None
        payload = check_payload(args, release_manifest)
        if args.json:
            print(render_json(payload))
        else:
            print(render_check(payload))
        return 0
    return apply_upgrade(args)


if __name__ == "__main__":
    raise SystemExit(main())
