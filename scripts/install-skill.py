#!/usr/bin/env python3
"""Install canonical Tenetora lifecycle skills and converge legacy installs."""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, field
import hashlib
import importlib.util
import json
import os
import re
import signal
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePath
from typing import Callable


SCRIPT_ROOT = Path(__file__).resolve().parents[1]
CLI_ROOT = SCRIPT_ROOT / "cli"
if str(CLI_ROOT) not in sys.path:
    sys.path.insert(0, str(CLI_ROOT))

from tenetora.interpreter_check import (  # noqa: E402
    interpreter_meets_minimum,
    python_version_meets_minimum,
)
from tenetora.brand import (  # noqa: E402
    default_legacy_machine_home,
    default_machine_home,
    machine_home,
    migrate_legacy_machine_home,
    inspect_machine_home,
    validate_managed_home_path,
)
from tenetora.path_security import (  # noqa: E402
    ensure_unredirected_directory,
    is_allowed_system_redirect,
    is_redirected_path,
    remove_unredirected_entry,
    validate_existing_project_path,
    validate_unredirected_entry_path,
    validate_unredirected_path,
)
from tenetora.codex_legacy_recovery import (  # noqa: E402
    CodexMarketplaceRepairTransaction,
    RecoveryTransaction,
    assess_legacy_registration,
    assess_codex_marketplace_path_repair,
    begin_codex_marketplace_path_repair,
    begin_recovery,
)
from tenetora.platform_contracts import load_platform_contracts, platform_contract  # noqa: E402
from tenetora.file_lock import locked_file  # noqa: E402
from tenetora.install_lock import (  # noqa: E402
    install_lock_subprocess_kwargs,
    install_transaction_lock as shared_install_transaction_lock,
)

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

from installation_registry import (  # noqa: E402
    AUTO_DISCOVERY_CANDIDATES,
    AUTO_DISCOVERY_DEPTH,
    auto_discovery_roots,
    detect_project_surfaces,
    discover_project_candidates,
    effective_global_surfaces,
    governance_registration_for_classification,
    is_reserved_project_root,
    registered_global_surfaces,
    registered_project_entries,
    registered_projects,
    registry_path,
    upsert_global_surfaces,
    upsert_project,
)
from install_progress import InstallProgress, redact, redact_value, write_failure_artifacts  # noqa: E402
from tool_installers import definitions_for_installer, tool_definition, tool_installer, tool_names  # noqa: E402


@contextmanager
def install_transaction_lock():
    with shared_install_transaction_lock(machine_home()):
        yield


ROUTER_SKILL_NAME = "tenetora"
LEGACY_ROUTER_SKILL_NAME = "agent-harness"
CANONICAL_SKILL_NAMES = (
    "tenetora",
    "tenetora-init",
    "tenetora-update",
    "tenetora-audit",
    "tenetora-loop",
    "tenetora-prompt-guard",
    "tenetora-align",
    "tenetora-decision-interview",
)
LEGACY_SKILL_NAMES = (
    "agent-harness",
    "agent-harness-init",
    "agent-harness-update",
    "agent-harness-audit",
    "agent-harness-loop",
    "agent-harness-prompt-guard",
    "agent-harness-align",
    "agent-harness-decision-interview",
)
PLUGIN_SKILL_NAMES = CANONICAL_SKILL_NAMES
RECOGNIZED_SKILL_NAMES = (*CANONICAL_SKILL_NAMES, *LEGACY_SKILL_NAMES)
SAFE_CANONICAL_GOVERNANCE_CLASSIFICATIONS = frozenset(
    {"canonical", "canonical-with-foreign", "canonical-with-preserved-legacy"}
)
PLUGIN_REGISTRATION_NAME = ROUTER_SKILL_NAME
LEGACY_PLUGIN_REGISTRATION_NAME = LEGACY_ROUTER_SKILL_NAME
COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".mypy_cache", ".ruff_cache")
PLUGIN_COPY_ENTRIES = (
    ".claude-plugin",
    ".codex-plugin",
    ".zcode-plugin",
    "hooks",
    "skills",
    "cli",
    "scripts",
    "tenetora.plugin.json",
    "manifest.json",
    "pyproject.toml",
    "README.md",
    "CHANGELOG.md",
    "install.sh",
)
PLATFORM_CONTRACTS = load_platform_contracts()
ALL_TOOLS = tool_names()
SUPPORTED_TOOLS = set(ALL_TOOLS)
ZCODE_MARKETPLACE_ID = "tenetora-local"
ZCODE_PLUGIN_ID = f"{PLUGIN_REGISTRATION_NAME}@{ZCODE_MARKETPLACE_ID}"
LEGACY_ZCODE_MARKETPLACE_ID = "agent-harness-local"
LEGACY_ZCODE_PLUGIN_ID = f"{LEGACY_PLUGIN_REGISTRATION_NAME}@{LEGACY_ZCODE_MARKETPLACE_ID}"
NATIVE_PLUGIN_TARGETS = {definition.name for definition in definitions_for_installer("native-marketplace")}
NATIVE_MARKETPLACE_ID = "tenetora-local"
NATIVE_PLUGIN_ID = f"{PLUGIN_REGISTRATION_NAME}@{NATIVE_MARKETPLACE_ID}"
LEGACY_NATIVE_MARKETPLACE_ID = "agent-harness-local"
LEGACY_NATIVE_PLUGIN_ID = f"{LEGACY_PLUGIN_REGISTRATION_NAME}@{LEGACY_NATIVE_MARKETPLACE_ID}"
RUNTIME_ADAPTER_TARGETS = {definition.name for definition in definitions_for_installer("runtime-adapter")}
CURSOR_HOOK_MODES = {"session-start", "pre-tool-commit", "stop"}
OPENCODE_RUNTIME_MARKER = "tenetora-runtime-adapter"
LEGACY_OPENCODE_RUNTIME_MARKER = "agent-harness-runtime-adapter"
PI_RUNTIME_MARKER = "tenetora-pi-runtime-adapter"
OPENCODE_RUNTIME_ROOT_RE = re.compile(
    r"TENETORA_ROOT\s*=\s*(\"(?:\\.|[^\"\\])*\")"
)
OPENCODE_RUNTIME_VERSION_RE = re.compile(
    r"TENETORA_VERSION\s*=\s*(\"(?:\\.|[^\"\\])*\")"
)
LEGACY_OPENCODE_RUNTIME_ROOT_RE = re.compile(
    r"AGENT_HARNESS_ROOT\s*=\s*(\"(?:\\.|[^\"\\])*\")"
)
LEGACY_OPENCODE_RUNTIME_VERSION_RE = re.compile(
    r"AGENT_HARNESS_VERSION\s*=\s*(\"(?:\\.|[^\"\\])*\")"
)
PI_RUNTIME_ROOT_RE = re.compile(
    r"TENETORA_ROOT\s*=\s*(\"(?:\\.|[^\"\\])*\")"
)
PI_RUNTIME_VERSION_RE = re.compile(
    r"TENETORA_VERSION\s*=\s*(\"(?:\\.|[^\"\\])*\")"
)
PROJECT_RUNTIME_IGNORE_ENTRIES = (
    "state/alignment-session.json",
    "state/alignment-sessions/",
    "state/alignment-history/",
    "state/alignment-lifecycle.json",
    "state/.*.lock",
)
CODEX_PLUGIN_SECTION_RE = re.compile(
    r"^\s*\[\s*plugins\s*\.\s*(?:\"(?P<double>[^\"]+)\"|'(?P<single>[^']+)'|(?P<bare>[^\]\s]+))\s*\]\s*(?:#.*)?$"
)
CODEX_PLUGIN_ENABLED_RE = re.compile(r"^\s*enabled\s*=\s*(true|false)\s*(?:#.*)?$", re.IGNORECASE)


class NativePluginStateUnavailable(RuntimeError):
    """Raised when a host CLI cannot inspect its configured plugin state."""


CAPABILITY_ACTIVE = "ACTIVE"
CAPABILITY_PENDING_TRUST = "PENDING_TRUST"
CAPABILITY_ACTIVE_PARTIAL = "ACTIVE_PARTIAL"
CAPABILITY_SKILLS_ONLY = "SKILLS_ONLY"
CAPABILITY_BLOCKED = "BLOCKED"
CAPABILITY_SKIPPED = "SKIPPED"

LANGUAGE = "zh" if os.environ.get("TENETORA_LANG", "").strip().lower() in {
    "zh",
    "zh-cn",
    "zh-hans",
    "chinese",
    "中文",
} else "en"

STATUS_LABELS_ZH = {
    "ACTION": "需操作",
    "BLOCKED": "已阻断",
    "CONFLICT": "冲突",
    "CURRENT": "已是最新",
    "FALLBACK": "已降级",
    "INSTALLED": "已安装",
    "MIGRATE": "已迁移",
    "MISSING": "缺失",
    "PRUNED": "已清理",
    "SKIPPED": "已跳过",
    "UPDATED": "已更新",
}

CAPABILITY_LABELS_ZH = {
    CAPABILITY_ACTIVE: "已激活",
    CAPABILITY_PENDING_TRUST: "等待信任",
    CAPABILITY_ACTIVE_PARTIAL: "部分激活",
    CAPABILITY_SKILLS_ONLY: "仅 Skills",
    CAPABILITY_BLOCKED: "已阻断",
    CAPABILITY_SKIPPED: "已跳过",
    "READY": "就绪",
    "FULL": "全功能",
    "PARTIAL": "部分失败",
}


def human_text(english: str, chinese: str) -> str:
    return chinese if LANGUAGE == "zh" else english


def native_plugins_enabled() -> bool:
    return os.environ.get("TENETORA_NATIVE_PLUGINS", "1") != "0"


def display_value(value: str | None) -> str:
    if value is None:
        return "-"
    if LANGUAGE != "zh":
        return value
    return {
        **CAPABILITY_LABELS_ZH,
        "available": "可用",
        "unavailable": "不可用",
        "supported": "支持",
        "unsupported": "不支持",
        "not-applicable": "不适用",
        "skills-only": "仅 Skills",
        "active": "已激活",
        "inactive": "未激活",
        "enabled": "已启用",
        "disabled": "已禁用",
        "healthy": "正常",
        "empty": "空",
        "unreadable": "无法读取",
        "global": "全局",
        "project": "项目",
        "both": "全局和项目",
        "native-plugin": "原生插件",
        "project-fallback": "项目级回退",
        "active-native": "原生插件已激活",
        "pending-native-trust": "等待原生 Hook 信任",
        "active-native-degraded-management": "原生 Hook 已激活，管理能力降级",
        "native-management-unavailable": "原生插件管理不可用",
        "native-hook-blocks-fallback": "原生 Hook 阻止项目级回退",
        "active-project-fallback": "项目级回退已激活",
        "pending-project-trust": "等待项目级 Hook 信任",
        "project-fallback-available": "可启用项目级回退",
        "project-fallback-missing": "项目级回退缺失",
        "project-fallback-unavailable": "项目级回退不可用",
        "project-fallback-conflict": "项目级回退冲突",
        "project-fallback-ownership-conflict": "项目级回退所有权冲突",
        "hook-inventory-unavailable": "Hook 清单不可用",
        "duplicate-hook-runtime": "存在重复 Hook runtime",
        "disabled-by-policy": "已按策略禁用",
        "stable-runtime-home-unsupported": "稳定 runtime 目录不受支持",
        "codex-marketplace-unreadable": "Codex marketplace 无法读取",
    }.get(value, value)


def localize_detail(message: str) -> str:
    if LANGUAGE != "zh":
        return message

    exact = {
        "skipped by preflight": "已由预检跳过",
        "native plugin is installed but disabled": "原生插件已安装但未启用",
        "native hooks are active; marketplace management remains unavailable": "原生 hooks 已激活，但 marketplace 管理仍不可用",
        "--codex-hooks project requests a runtime mode switch, but enabled Tenetora native hooks are still active": "--codex-hooks project 表示切换运行模式，但 Tenetora 原生 Hook 仍处于启用状态",
        "For a normal upgrade, rerun with --codex-hooks auto or --codex-hooks native. To switch to project fallback, disable the existing Tenetora native hooks in Codex /hooks, then rerun --codex-hooks project.": "普通升级请使用 --codex-hooks auto 或 --codex-hooks native；如需切换到项目级回退，请先在 Codex /hooks 中禁用现有 Tenetora 原生 Hook，再重新运行 --codex-hooks project。",
        "restore Codex hook inventory before enabling project fallback": "启用项目级回退前，请先恢复 Codex hook 清单能力",
        "project fallback was not enabled; run tenetora install --path . --tools codex --codex-hooks project": "项目级回退尚未启用；请运行 tenetora install --path . --tools codex --codex-hooks project",
        "Rerun with --allow-skills-only, repair the environment, or omit this tool": "请使用 --allow-skills-only 重试、修复环境，或不安装该工具",
        "Restart Codex and trust the current Tenetora hooks in /hooks": "请重启 Codex，并在 /hooks 中信任当前 Tenetora hooks",
        "Review and trust the current Tenetora hooks in Codex /hooks": "请在 Codex /hooks 中审查并信任当前 Tenetora hooks",
        "Restart Codex and trust the current Agent Harness hooks in /hooks": "请重启 Codex，并在 /hooks 中信任当前 Tenetora hooks",
        "Restart Codex and review the restored Tenetora project hooks in /hooks": "请重启 Codex，并在 /hooks 中审查已恢复的 Tenetora 项目级 hooks",
        "Use --global for codex native plugin and runtime hooks": "请使用 --global 安装或升级 Codex 原生插件和 runtime hooks",
        "Use --global for claude native plugin and runtime hooks": "请使用 --global 安装或升级 Claude Code 原生插件和 runtime hooks",
        "Use --global for the ZCode native plugin and runtime hooks": "请使用 --global 安装或升级 ZCode 原生插件和 runtime hooks",
        "Runtime is intentionally owned by global scope.": "runtime 按设计由全局范围负责",
        "Runtime is intentionally owned by project scope.": "runtime 按设计由项目级范围负责",
        "hooks.json path is not a file": "hooks.json 路径不是文件",
        "hooks.json is missing": "缺少 hooks.json",
        "hooks.json is invalid": "hooks.json 无效",
        "hooks.json has no hooks object": "hooks.json 缺少 hooks 对象",
        "runtime hooks active": "runtime hooks 已激活",
        "runtime plugin path is not a file": "runtime plugin 路径不是文件",
        "runtime plugin is missing": "缺少 runtime plugin",
        "runtime plugin is unreadable": "runtime plugin 无法读取",
        "runtime plugin path is owned by another plugin": "runtime plugin 路径属于其他插件",
        "runtime plugin payload is stale": "runtime plugin 内容已过期",
        "runtime plugin active": "runtime plugin 已激活",
        "runtime hooks included": "包含 runtime hooks",
        "hooks present": "hooks 已就绪",
        "activation enabled": "已启用激活",
        "hooks trusted and active": "hooks 已信任并激活",
        "trust must be reviewed in /hooks": "必须在 /hooks 中审查信任",
        "ZCode hooks, registry, and enabledPlugins": "ZCode hooks、registry 和 enabledPlugins",
        "Tenetora is registered under the canonical plugin ID": "Tenetora 已通过 canonical 插件 ID 注册",
        "tenetora is registered and enabled": "tenetora 已注册并启用",
        "reload ZCode, then verify with tenetora doctor --tools zcode": "重新加载 ZCode，然后运行 tenetora doctor --tools zcode 验证",
        "plugins.enabled is false": "plugins.enabled 为 false",
        "enable the ZCode plugin subsystem, then reload ZCode": "请启用 ZCode 插件子系统，然后重新加载 ZCode",
        "installed plugin cache is missing": "已安装的插件缓存缺失",
        "not inside a Git worktree": "不在 Git 工作区内",
        "no machine-local exclude was written": "未写入本机 Git exclude",
        "Tenetora package root is unavailable; use the release installer or --source-dir package root": "Tenetora 安装包根目录不可用；请使用 release 安装器或通过 --source-dir 指定安装包根目录",
        "Tenetora package root is unavailable": "Tenetora 安装包根目录不可用",
        "ZCode native plugins are user-scoped; use project scope for skills only": "ZCode 原生插件仅支持用户级安装；项目级范围只能安装 skills",
        "No install tool selected": "未选择要安装的工具",
        "No existing Tenetora installation surface matched the requested tools and scope; run tenetora install to create a new installation": "没有与请求工具和范围匹配的现有 Tenetora 安装面；请运行 tenetora install 创建新安装",
        "No existing Tenetora installation surface matched the explicit project target; global matches cannot satisfy --in-project/--both. Run tenetora install to create a project installation, or verify --path.": "显式指定的项目没有匹配到现有 Tenetora 项目安装面；全局安装面不能满足 --in-project/--both。请运行 tenetora install 创建项目安装，或检查 --path。",
        "ZCode native plugin cache requires copy mode": "ZCode 原生插件缓存必须使用 copy 模式",
        "Cursor hooks.json has a non-object hooks field": "Cursor hooks.json 的 hooks 字段不是对象",
        "Tenetora Cursor hook template is invalid": "Tenetora Cursor hook 模板无效",
        "cannot load Codex project hook manager": "无法加载 Codex 项目 hook 管理器",
        "cannot load Tenetora CLI bootstrap": "无法加载 Tenetora CLI 自举模块",
        "Tenetora stable hook runtime failed its empty-PATH check": "Tenetora 稳定 hook runtime 未通过空 PATH 检查",
        "lifecycle skills can be installed, but native runtime hooks remain inactive": "仍可安装 lifecycle skills，但原生 runtime hooks 暂未激活",
    }
    if message in exact:
        return exact[message]

    if "; " in message:
        parts = message.split("; ")
        translated = [localize_detail(part) for part in parts]
        if translated != parts:
            return "；".join(translated)

    patterns = (
        (r"^would update (.+) from marketplace (.+)$", r"将从 marketplace \2 更新 \1"),
        (r"^would install (.+) from marketplace (.+)$", r"将从 marketplace \2 安装 \1"),
        (r"^would update registered copy (.+) from (.+)$", r"将从 \2 更新已注册副本 \1"),
        (r"^would install registered copy (.+) from (.+)$", r"将从 \2 安装已注册副本 \1"),
        (r"^would update (\S+) (.+) from (.+)$", r"将以 \1 模式更新 \2，来源：\3"),
        (r"^would install (\S+) (.+) from (.+)$", r"将以 \1 模式安装 \2，来源：\3"),
        (r"^would write (.+)$", r"将写入 \1"),
        (r"^would remove superseded cache (.+)$", r"将删除已被取代的缓存 \1"),
        (r"^removed superseded cache (.+)$", r"已删除被取代的缓存 \1"),
        (r"^would remove legacy plugin symlink (.+)$", r"将删除旧版插件符号链接 \1"),
        (r"^removed legacy plugin symlink (.+)$", r"已删除旧版插件符号链接 \1"),
        (r"^would move legacy plugin (.+) to (.+)$", r"将把旧版插件 \1 移动到 \2"),
        (r"^moved legacy plugin (.+) to (.+)$", r"已把旧版插件 \1 移动到 \2"),
        (r"^would remove legacy skill symlink (.+)$", r"将删除旧版 skill 符号链接 \1"),
        (r"^removed legacy skill symlink (.+?)(; no directory content deleted)?$", r"已删除旧版 skill 符号链接 \1；未删除目录内容"),
        (r"^would move legacy skill copy (.+) to (.+)$", r"将把旧版 skill 副本 \1 移动到 \2"),
        (r"^moved legacy skill copy (.+) to (.+)$", r"已把旧版 skill 副本 \1 移动到 \2"),
        (r"^refreshed symlink source (.+) -> (.+)$", r"已刷新符号链接来源 \1 -> \2"),
        (r"^already up to date (.+) -> (.+)$", r"已是最新 \1 -> \2"),
        (r"^already installed (.+) -> (.+)$", r"已安装 \1 -> \2"),
        (r"^installed symlink (.+) -> (.+)$", r"已安装符号链接 \1 -> \2"),
        (r"^installed copy (.+)$", r"已安装副本 \1"),
        (r"^installed (copy|symlink) (.+)$", r"已安装 \1 \2"),
        (r"^updated (copy|symlink) (.+)$", r"已更新 \1 \2"),
        (r"^installed (.+) version (.+)$", r"已安装 \1，版本 \2"),
        (r"^native plugin (.+) version (.+)$", r"原生插件 \1，版本 \2"),
        (r"^plugin (.+) version (.+)$", r"插件 \1，版本 \2"),
        (r"^missing (.+)$", r"缺少 \1"),
        (r"^file (.+)$", r"目标是文件 \1"),
        (r"^symlink (.+); expected (.+)$", r"符号链接为 \1；期望 \2"),
        (r"^runtime already absent at (.+); effective runtime is owned by global scope$", r"runtime 已无需清理：\1；有效 runtime 由全局范围承担"),
        (r"^runtime already absent at (.+); effective runtime is owned by project scope$", r"runtime 已无需清理：\1；有效 runtime 由项目范围承担"),
        (r"^no shadowed managed runtime at (.+)$", r"指定位置没有被遮蔽的托管 runtime：\1"),
        (r"^would remove managed runtime from (.+)$", r"将删除托管 runtime：\1"),
        (r"^removed unchanged Tenetora managed runtime from (.+); (.+)$", r"已删除未修改的 Tenetora 托管 runtime：\1；\2"),
        (r"^plugin (.+) version (.+); hooks present$", r"插件 \1，版本 \2；hooks 已就绪"),
        (r"^native plugin (.+) version (.+); hooks present$", r"原生插件 \1，版本 \2；hooks 已就绪"),
        (r"^registered plugin cache is missing (.+)$", r"已注册的插件缓存缺失：\1"),
        (r"^invalid or foreign plugin (.+)$", r"无效或非 Tenetora 插件：\1"),
        (r"^incomplete native plugin (.+): (.+)$", r"原生插件不完整：\1；\2"),
        (r"^stale native plugin (.+) version (.+); expected (.+)$", r"原生插件版本过旧：\1，当前 \2，期望 \3"),
        (r"^installed (.+) version (.+) does not match package (.+)$", r"已安装的 \1 版本 \2 与安装包 \3 不一致"),
        (r"^Rerun tenetora install for (.+) (global|project) scope with --force to converge the installed version$", r"请对 \1 的 \2 范围重新运行 tenetora install --force，使已安装版本收敛"),
        (r"^Rerun (tenetora install .+)$", r"请重新运行：\1"),
        (r"^Rerun agent-harness install (.+)$", r"请重新运行：tenetora install \1"),
        (r"^Use --global for (?:the )?(.+) native plugin and runtime hooks$", r"请使用 --global 安装或升级 \1 原生插件和 runtime hooks"),
        (r"^(.+) native plugins are user-scoped; project scope installs lifecycle skills only$", r"\1 原生插件仅支持用户级安装；项目级范围只安装 lifecycle skills"),
        (r"^(.+) has Tenetora runtime adapters in both global and project scope$", r"\1 同时存在全局和项目级 Tenetora runtime adapter"),
        (r"^Select --both before pruning duplicate (.+) runtime adapters$", r"清理重复的 \1 runtime adapter 前请使用 --both"),
        (r"^install target is not writable: (.+)$", r"安装目标不可写：\1"),
        (r"^Choose a writable scope/path or fix directory permissions$", r"请选择可写的范围或路径，或修复目录权限"),
        (r"^managed events are missing or stale: (.+)$", r"托管事件缺失或已过期：\1"),
        (r"^restart Codex to load the updated package$", r"请重启 Codex 以加载更新后的安装包"),
        (r"^current Tenetora hook definitions are trusted$", r"当前 Tenetora hook 定义已信任"),
        (r"^restart Codex and review the Tenetora hook definitions in /hooks before runtime hooks can execute$", r"请重启 Codex，并在 /hooks 中审查 Tenetora hook 定义，之后 runtime hooks 才能执行"),
        (r"^restart Claude Code, then verify runtime state with (.+)$", r"请重启 Claude Code，然后使用 \1 验证 runtime 状态"),
        (r"^Tenetora is registered as (.+)$", r"Tenetora 已注册为 \1"),
        (r"^installed native Cursor hooks: (.+)$", r"已安装原生 Cursor hooks：\1"),
        (r"^installed native OpenCode plugin: (.+)$", r"已安装原生 OpenCode plugin：\1"),
        (r"^installed native Cursor hooks$", r"已安装原生 Cursor hooks"),
        (r"^session context, git guard, and claim follow-up active$", r"会话上下文、Git guard 和 claim 跟进已激活"),
        (r"^installed native OpenCode plugin$", r"已安装原生 OpenCode plugin"),
        (r"^context and pre-tool git guard active$", r"上下文和工具调用前 Git guard 已激活"),
        (r"^machine-local Git exclude already contains (.+): (.+)$", r"本机 Git exclude 已包含 \1：\2"),
        (r"^would add machine-local Git exclude (.+): (.+)$", r"将向本机 Git exclude 添加 \1：\2"),
        (r"^added machine-local Git exclude (.+): (.+)$", r"已向本机 Git exclude 添加 \1：\2"),
        (r"^Unsupported tool\(s\): (.+)$", r"不支持的工具：\1"),
        (r"^Unsupported tool: (.+)$", r"不支持的工具：\1"),
        (r"^(.+) CLI is unavailable$", r"\1 CLI 不可用"),
        (r"^(.+) CLI is unavailable; lifecycle skills can be installed, but native runtime hooks remain inactive$", r"\1 CLI 不可用；仍可安装 lifecycle skills，但原生 runtime hooks 暂未激活"),
        (r"^Install (.+), then rerun tenetora upgrade --force to activate native runtime hooks$", r"请安装 \1，然后重新运行 tenetora upgrade --force 以激活原生 runtime hooks"),
        (r"^(.+) native plugin CLI is unavailable; skills installed but runtime hooks are not active$", r"\1 原生插件 CLI 不可用；skills 已安装，但 runtime hooks 尚未激活"),
        (r"^(.+) native plugin CLI is unavailable; skills updated but runtime hooks are not active$", r"\1 原生插件 CLI 不可用；skills 已更新，但 runtime hooks 尚未激活"),
        (r"^Invalid JSON file (.+): (.+)$", r"JSON 文件无效：\1；\2"),
        (r"^JSON file must contain an object: (.+)$", r"JSON 文件必须包含对象：\1"),
        (r"^Invalid ZCode hooks template: (.+)$", r"ZCode hooks 模板无效：\1"),
        (r"^ZCode plugin registry has invalid plugins array: (.+)$", r"ZCode 插件注册表的 plugins 数组无效：\1"),
        (r"^ZCode config plugins field is not an object: (.+)$", r"ZCode 配置的 plugins 字段不是对象：\1"),
        (r"^ZCode config plugins\.enabledPlugins field is not an object: (.+)$", r"ZCode 配置的 plugins.enabledPlugins 字段不是对象：\1"),
        (r"^source (.+) plugin payload is incomplete: (.+)$", r"源 \1 插件内容不完整：\2"),
        (r"^(.+) plugin installation did not register (.+)$", r"\1 插件安装后未注册 \2"),
        (r"^installed (.+) plugin is incomplete: (.+)$", r"已安装的 \1 插件不完整：\2"),
        (r"^installed (.+) plugin is disabled$", r"已安装的 \1 插件未启用"),
        (r"^Cursor hooks\.json event (.+) is not a list$", r"Cursor hooks.json 事件 \1 不是列表"),
        (r"^Unsupported runtime adapter target: (.+)$", r"不支持的 runtime adapter 目标：\1"),
        (r"^Skills directory does not exist: (.+)$", r"Skills 目录不存在：\1"),
        (r"^No Tenetora lifecycle skills found under: (.+)$", r"指定目录下没有 Tenetora lifecycle skills：\1"),
        (r"^Skill source does not exist: (.+)$", r"Skill 来源不存在：\1"),
        (r"^Codex project hook manager is missing: (.+)$", r"缺少 Codex 项目 hook 管理器：\1"),
        (r"^(.+): refusing to overwrite existing (.+)$", r"\1：拒绝覆盖现有内容 \2"),
        (r"^(.+): refusing to replace existing file (.+)$", r"\1：拒绝替换现有文件 \2"),
        (r"^(.+): refusing to replace existing non-Agent-Harness plugin (.+)$", r"\1：拒绝替换非 Tenetora 插件 \2"),
        (r"^rerun with --force$", r"请使用 --force 重新执行"),
    )
    for pattern, replacement in patterns:
        translated, count = re.subn(pattern, replacement, message)
        if count:
            return translated

    return message


def localize_status_line(line: str) -> str:
    if LANGUAGE != "zh":
        return line
    status, separator, remainder = line.partition(" ")
    prefix, detail_separator, detail = remainder.partition(": ")
    if not separator or not detail_separator:
        return localize_detail(line)
    return f"{STATUS_LABELS_ZH.get(status, display_value(status))} {prefix}: {localize_detail(detail)}"


def discover_package_root(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if (
            (candidate / "skills" / ROUTER_SKILL_NAME / "VERSION").is_file()
            and any(
                (candidate / "skills" / skill_name / "SKILL.md").is_file()
                for skill_name in (ROUTER_SKILL_NAME, LEGACY_ROUTER_SKILL_NAME)
            )
        ):
            return candidate
    return None


PACKAGE_ROOT = discover_package_root(SCRIPT_ROOT)
SKILLS_ROOT = PACKAGE_ROOT / "skills" if PACKAGE_ROOT else SCRIPT_ROOT / "skills"


def configure_package_root(raw: str | None) -> None:
    global PACKAGE_ROOT, SKILLS_ROOT
    candidate = Path(raw).expanduser() if raw else discover_package_root(SCRIPT_ROOT)
    if raw:
        validate_source_tree(candidate)
        candidate = candidate.resolve()
    if raw is None and candidate is None:
        PACKAGE_ROOT = None
        SKILLS_ROOT = SCRIPT_ROOT / "skills" if (SCRIPT_ROOT / "skills").is_dir() else SCRIPT_ROOT.parent
        return
    if candidate is None or not any(
        (candidate / "skills" / skill_name / "SKILL.md").is_file()
        for skill_name in (ROUTER_SKILL_NAME, LEGACY_ROUTER_SKILL_NAME)
    ):
        raise ValueError("Tenetora package root is unavailable; use the release installer or --source-dir package root")
    PACKAGE_ROOT = candidate
    SKILLS_ROOT = candidate / "skills"


@dataclass
class InstallReport:
    lines: list[str] = field(default_factory=list)
    installed: int = 0
    updated: int = 0
    current: int = 0
    missing: int = 0
    conflicts: int = 0
    plugins: int = 0
    runtimes: int = 0

    def extend(self, other: "InstallReport") -> None:
        self.lines.extend(other.lines)
        self.installed += other.installed
        self.updated += other.updated
        self.current += other.current
        self.missing += other.missing
        self.conflicts += other.conflicts
        self.plugins += other.plugins
        self.runtimes += other.runtimes

    def text(self, include_summary: bool = True) -> str:
        lines = [localize_status_line(line) for line in self.lines]
        if include_summary:
            summary = human_text(
                f"Install summary: installed={self.installed} updated={self.updated} current={self.current}",
                f"安装摘要：已安装={self.installed} 已更新={self.updated} 已是最新={self.current}",
            )
            if self.missing or self.conflicts:
                summary += human_text(
                    f" missing={self.missing} conflicts={self.conflicts}",
                    f" 缺失={self.missing} 冲突={self.conflicts}",
                )
            if self.plugins:
                summary += human_text(f" plugins={self.plugins}", f" 插件={self.plugins}")
            if self.runtimes:
                summary += human_text(f" runtimes={self.runtimes}", f" 运行时={self.runtimes}")
            lines.append(summary)
        return "\n".join(lines)

    def summary_text(self) -> str:
        summary = human_text(
            f"Install summary: installed={self.installed} updated={self.updated} current={self.current}",
            f"安装摘要：已安装={self.installed} 已更新={self.updated} 已是最新={self.current}",
        )
        if self.missing or self.conflicts:
            summary += human_text(
                f" missing={self.missing} conflicts={self.conflicts}",
                f" 缺失={self.missing} 冲突={self.conflicts}",
            )
        if self.plugins:
            summary += human_text(f" plugins={self.plugins}", f" 插件={self.plugins}")
        if self.runtimes:
            summary += human_text(f" runtimes={self.runtimes}", f" 运行时={self.runtimes}")
        return summary


class InstallerSurfaceOperations:
    """Bridge tool-specific installers to shared transactional operations."""

    def install_native(self, **kwargs: object) -> InstallReport | None:
        kwargs.pop("mode", None)
        return install_native_cli_plugin(**kwargs)

    def install_zcode(self, **kwargs: object) -> InstallReport | None:
        kwargs.pop("target", None)
        return install_zcode_plugin(**kwargs)

    def status_native(self, **kwargs: object) -> InstallReport | None:
        return status_native_cli_plugin(**kwargs)


SURFACE_OPERATIONS = InstallerSurfaceOperations()


@dataclass
class InstallUnit:
    key: str
    project_root: Path
    scopes_by_target: dict[str, list[str]]
    kind: str
    runtime_owners: dict[str, str] = field(default_factory=dict)
    governance_only: bool = False

    def payload(self) -> dict[str, object]:
        return {
            "key": self.key,
            "kind": self.kind,
            "project_path": str(self.project_root),
            "targets": self.scopes_by_target,
            "runtime_owners": self.runtime_owners,
            "governance_only": self.governance_only,
            "surface_count": sum(len(scopes) for scopes in self.scopes_by_target.values()),
        }


@dataclass
class MachineInstallPlan:
    units: list[InstallUnit]
    stale_projects: list[str] = field(default_factory=list)
    discovered_projects: list[str] = field(default_factory=list)

    @property
    def surface_count(self) -> int:
        return sum(sum(len(scopes) for scopes in unit.scopes_by_target.values()) for unit in self.units)

    @property
    def governance_count(self) -> int:
        return sum(unit.governance_only for unit in self.units)

    @property
    def work_count(self) -> int:
        return self.surface_count + self.governance_count

    def payload(self) -> dict[str, object]:
        return {
            "status": "found" if self.units else "missing",
            "surface_count": self.surface_count,
            "governance_count": self.governance_count,
            "work_count": self.work_count,
            "project_count": sum(unit.kind == "project" for unit in self.units),
            "stale_projects": self.stale_projects,
            "discovered_projects": self.discovered_projects,
            "units": [unit.payload() for unit in self.units],
        }


@dataclass
class CapabilityCheck:
    tool: str
    scope: str
    detected: bool
    cli_path: str | None
    cli_version: str | None
    plugin_api: str
    marketplace: str
    writable: bool
    existing_version: str | None
    maximum: str
    planned: str
    expected_version: str | None = None
    actual_version: str | None = None
    version_source: str | None = None
    version_matches_package: bool | None = None
    version_evidence: list[str] = field(default_factory=list)
    actual: str | None = None
    matches_plan: bool | None = None
    hook_mode: str = "skills-only"
    hooks_state: str = "inactive"
    degraded_reason: str | None = None
    blocker: str | None = None
    remediation: str | None = None
    runtime_role: str = "effective"
    runtime_owner_scope: str | None = None
    runtime_prune_scopes: list[str] = field(default_factory=list)
    fallback_accepted: bool = False


@dataclass
class CapabilityReport:
    phase: str
    package_version: str
    python_version: str
    project_path: str
    checks: list[CapabilityCheck]
    tool_discovery: list[dict[str, object]] = field(default_factory=list)

    def payload(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "package_version": self.package_version,
            "python": {
                "executable": sys.executable,
                "version": self.python_version,
                "supported": python_version_meets_minimum(sys.version_info),
            },
            "project_path": self.project_path,
            "overall": capability_overall(self.checks, actual=self.phase == "postflight"),
            "tools": [asdict(check) for check in self.checks],
            "tool_discovery": self.tool_discovery,
        }


def status_line(status: str, target: str, scope: str, skill: str, message: str) -> str:
    return f"{status} {scope}:{target}:{skill}: {message}"


def existing_project_path(raw: str) -> Path:
    try:
        return validate_existing_project_path(raw)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def path_is_redirected(path: Path) -> bool:
    """Detect POSIX symlinks and Windows junctions without resolving them."""
    return is_redirected_path(path)


def ensure_safe_directory_chain(
    path: Path,
    *,
    label: str,
    boundary: Path | None = None,
    allow_file: bool = False,
) -> Path:
    """Reject redirected or non-directory parents inside a trusted boundary."""
    # Keep an existing Path flavour. Tests and callers may model another host
    # while still passing a concrete path object from the current host.
    candidate = path.expanduser() if isinstance(path, Path) else Path(path).expanduser()
    if ".." in candidate.parts:
        raise RuntimeError(f"{label} contains parent traversal: {candidate}")
    if not candidate.is_absolute():
        candidate = candidate.absolute()
    if boundary is None:
        entries = [candidate.__class__(candidate.anchor)]
        current = entries[0]
        for part in candidate.parts[1:]:
            current /= part
            entries.append(current)
    else:
        root = boundary.expanduser() if isinstance(boundary, Path) else Path(boundary).expanduser()
        if ".." in root.parts:
            raise RuntimeError(f"{label} boundary contains parent traversal: {root}")
        if not root.is_absolute():
            root = root.absolute()
        if path_is_redirected(root) or root.exists() and not root.is_dir():
            raise RuntimeError(f"{label} boundary is not a regular directory: {root}")
        try:
            relative = candidate.relative_to(root)
        except ValueError as exc:
            raise RuntimeError(f"{label} is outside its trusted boundary: {candidate}") from exc
        entries = [root]
        current = root
        for part in relative.parts:
            current = current / part
            entries.append(current)
    for entry in entries:
        try:
            exists = entry.exists() or path_is_redirected(entry)
        except OSError as exc:
            raise RuntimeError(f"cannot inspect {label}: {entry}: {exc}") from exc
        if not exists:
            continue
        if path_is_redirected(entry) and not is_allowed_system_redirect(entry):
            raise RuntimeError(f"{label} contains a symbolic link or junction: {entry}")
        if allow_file and entry == candidate and entry.is_file():
            continue
        if not entry.is_dir():
            raise RuntimeError(f"{label} parent is not a directory: {entry}")
    return candidate


def validate_source_tree(root: Path) -> None:
    """Keep explicit package sources closed before any package file is consumed."""
    source = ensure_safe_directory_chain(root, label="explicit package source")
    if not source.is_dir():
        raise ValueError(f"explicit package source is not a directory: {source}")
    pending = [source]
    while pending:
        current = pending.pop()
        try:
            entries = list(current.iterdir())
        except OSError as exc:
            raise ValueError(f"cannot inspect explicit package source: {current}: {exc}") from exc
        for entry in entries:
            if path_is_redirected(entry):
                raise ValueError(f"explicit package source contains a symbolic link or junction: {entry}")
            if entry.is_dir():
                pending.append(entry)
            elif not entry.is_file():
                raise ValueError(f"explicit package source contains a non-regular file: {entry}")


def runtime_owner_map(raw: str) -> dict[str, str]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid machine runtime owner map: {exc}") from exc
    if not isinstance(payload, dict):
        raise argparse.ArgumentTypeError("machine runtime owner map must be a JSON object")
    normalized: dict[str, str] = {}
    for target, scope in payload.items():
        if target not in RUNTIME_ADAPTER_TARGETS or scope not in {"global", "project"}:
            raise argparse.ArgumentTypeError(f"invalid machine runtime owner: {target}={scope}")
        normalized[str(target)] = str(scope)
    return normalized


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def native_plugin_command(target: str) -> str | None:
    return tool_definition(target).command(command_exists)


def native_plugin_environment(target: str, project_root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    definition = tool_definition(target)
    native_home = definition.native_home(project_root)
    if native_home is not None and definition.native_home_env is not None:
        environment[definition.native_home_env] = str(
            validate_unredirected_path(native_home, label=definition.native_home_env)
        )
    return environment


def run_native_plugin_command(
    target: str,
    project_root: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = native_plugin_command(target)
    if command is None:
        raise FileNotFoundError(f"{target} CLI is unavailable")
    result = subprocess.run(
        [command, *args],
        cwd=project_root,
        env=native_plugin_environment(target, project_root),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=60,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"{target} {' '.join(args)} failed: {detail or f'exit {result.returncode}'}")
    return result


def native_plugin_cli_available(target: str, project_root: Path) -> bool:
    if not native_plugins_enabled() or native_plugin_command(target) is None:
        return False
    try:
        return run_native_plugin_command(target, project_root, "plugin", "--help", check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def native_plugin_cli_supported_readonly(target: str, project_root: Path) -> bool:
    if not native_plugins_enabled():
        return False
    command = native_plugin_command(target)
    if command is None:
        return False
    try:
        with tempfile.TemporaryDirectory(prefix=f"tenetora-{target}-preflight-") as temporary:
            environment = os.environ.copy()
            definition = tool_definition(target)
            if definition.native_home_env is not None:
                environment[definition.native_home_env] = temporary
            result = subprocess.run(
                [command, "plugin", "--help"],
                cwd=project_root,
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
            return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def zcode_detected(home: Path) -> bool:
    return tool_definition("zcode").detected(home, command_exists)


def target_base(target: str, scope: str, project_root: Path) -> Path:
    return tool_definition(target).skill_base(scope, project_root)


def zcode_home(scope: str, project_root: Path) -> Path:
    if scope == "project":
        return project_root / ".zcode"
    return Path(os.environ.get("ZCODE_HOME", Path.home() / ".zcode")).expanduser()


def zcode_plugin_base(scope: str, project_root: Path) -> Path:
    if scope != "global":
        raise ValueError("ZCode native plugins are user-scoped; use project scope for skills only")
    cache_root = Path(
        os.environ.get(
            "ZCODE_PLUGIN_HOME",
            str(zcode_home(scope, project_root) / "cli" / "plugins" / "cache"),
        )
    ).expanduser()
    return cache_root / ZCODE_MARKETPLACE_ID / PLUGIN_REGISTRATION_NAME


def legacy_zcode_plugin_base(scope: str, project_root: Path) -> Path:
    if scope != "global":
        raise ValueError("ZCode native plugins are user-scoped; use project scope for skills only")
    cache_root = Path(
        os.environ.get(
            "ZCODE_PLUGIN_HOME",
            str(zcode_home(scope, project_root) / "cli" / "plugins" / "cache"),
        )
    ).expanduser()
    return cache_root / LEGACY_ZCODE_MARKETPLACE_ID / LEGACY_PLUGIN_REGISTRATION_NAME


def zcode_plugin_path(scope: str, project_root: Path, version: str) -> Path:
    return zcode_plugin_base(scope, project_root) / version


def zcode_registry_path(project_root: Path) -> Path:
    return zcode_home("global", project_root) / "cli" / "plugins" / "installed_plugins.json"


def zcode_config_path(project_root: Path) -> Path:
    configured = os.environ.get("ZCODE_CONFIG")
    if configured:
        return Path(configured).expanduser()
    return zcode_home("global", project_root) / "cli" / "config.json"


def legacy_zcode_plugin_path(project_root: Path) -> Path:
    return zcode_home("global", project_root) / "plugins" / LEGACY_PLUGIN_REGISTRATION_NAME


def legacy_project_zcode_plugin_path(project_root: Path) -> Path:
    return zcode_home("project", project_root) / "plugins" / LEGACY_PLUGIN_REGISTRATION_NAME


def tool_detection_reasons(
    definition: ToolDefinition,
    home: Path,
    command_exists_fn: Callable[[str], bool],
) -> list[str]:
    """Explain detection without exposing command output or environment values."""
    reasons: list[str] = []
    if definition.always_detected:
        reasons.append("always-supported")
    for command in definition.native_commands:
        if command_exists_fn(command):
            reasons.append(f"command:{command}")
    for variable in (definition.global_home_env, *definition.global_home_aliases):
        if variable and os.environ.get(variable):
            reasons.append(f"configured:{variable}")
    if definition.global_home_root_env and os.environ.get(definition.global_home_root_env):
        root = Path(os.environ[definition.global_home_root_env]).expanduser()
        if root.joinpath(*definition.global_home_root_suffix).exists():
            reasons.append(f"configured:{definition.global_home_root_env}")
    paths = definition.detection_paths or (definition.global_home_suffix,)
    for relative in paths:
        if home.joinpath(*relative).exists():
            reasons.append(f"path:{'/'.join(relative)}")
    return list(dict.fromkeys(reasons))


def tool_discovery(selected_targets: list[str]) -> list[dict[str, object]]:
    """Return a stable discovery view for every supported host, including Pi."""
    home = Path.home()
    selected = set(selected_targets)
    discovery: list[dict[str, object]] = []
    for name in ALL_TOOLS:
        definition = tool_definition(name)
        reasons = tool_detection_reasons(definition, home, command_exists)
        discovery.append(
            {
                "tool": name,
                "detected": bool(reasons),
                "selected": name in selected,
                "reasons": reasons or ["not-detected"],
                "installer": definition.installer,
                "runtime_kind": definition.runtime_kind,
            }
        )
    return discovery


def parse_tools(raw: str) -> list[str]:
    requested = [part.strip() for part in raw.split(",") if part.strip()]
    if not requested:
        raise ValueError("No install tool selected")
    if "all" in requested:
        return ALL_TOOLS
    if "auto" not in requested:
        unknown = sorted(set(requested) - SUPPORTED_TOOLS)
        if unknown:
            raise ValueError(f"Unsupported tool(s): {', '.join(unknown)}")
        return list(dict.fromkeys(requested))

    home = Path.home()
    return [
        definition.name
        for definition in (tool_definition(name) for name in ALL_TOOLS)
        if definition.detected(home, command_exists)
    ]


def remove_existing(path: Path) -> None:
    remove_unredirected_entry(path, label="managed install entry")


def read_json_object(path: Path, default: dict[str, object]) -> dict[str, object]:
    ensure_safe_file_path(path, label="JSON file")
    if not path.is_file():
        return dict(default)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid JSON file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return payload


def write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path = ensure_safe_file_path(path, label="JSON file")
    ensure_unredirected_directory(path.parent, label="JSON file parent")
    target_mode = 0o600
    if path.is_file():
        target_mode = path.stat().st_mode & 0o777
        backup = ensure_safe_file_path(
            path.with_name(f"{path.name}.tenetora.bak"),
            label="JSON backup file",
        )
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
            backup_temporary = Path(handle.name)
            handle.write(path.read_bytes())
            handle.flush()
            os.fsync(handle.fileno())
        try:
            backup_temporary.chmod(target_mode)
            os.replace(backup_temporary, backup)
        finally:
            backup_temporary.unlink(missing_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.chmod(target_mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_safe_file_path(path: Path, *, label: str) -> Path:
    """Validate a file path without following a redirected parent or target."""

    candidate = Path(path).expanduser()
    ensure_safe_directory_chain(candidate.parent, label=f"{label} parent")
    if path_is_redirected(candidate):
        raise RuntimeError(f"{label} contains a symbolic link or junction: {candidate}")
    if candidate.exists() and not candidate.is_file():
        raise RuntimeError(f"{label} is not a regular file: {candidate}")
    return candidate


def package_version(path: Path) -> str | None:
    """Read the package version from the canonical lifecycle VERSION file."""

    version_file = path / "skills" / ROUTER_SKILL_NAME / "VERSION"
    try:
        version = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return version or None


def plugin_version(path: Path) -> str | None:
    """Read the installed ZCode plugin manifest version."""

    payload = read_plugin_manifest(path)
    if payload is None:
        return None
    return str(payload.get("version")) if payload.get("version") else None


def read_plugin_manifest(path: Path) -> dict[str, object] | None:
    manifest = path / ".zcode-plugin" / "plugin.json"
    if not manifest.is_file():
        return None
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def plugin_is_tenetora_managed(path: Path) -> bool:
    payload = read_plugin_manifest(path)
    if payload is None or payload.get("name") not in {
        PLUGIN_REGISTRATION_NAME,
        LEGACY_PLUGIN_REGISTRATION_NAME,
    }:
        return False
    markers = (
        path / "hooks" / "tenetora_hook.py",
        path / "hooks" / "agent_harness_hook.py",
        path / "skills" / LEGACY_ROUTER_SKILL_NAME / "SKILL.md",
        path / "skills" / ROUTER_SKILL_NAME / "SKILL.md",
    )
    return sum(marker.is_file() for marker in markers) >= 2


def zcode_entry_is_managed(entry: dict[str, object]) -> bool:
    plugin_id = entry.get("id")
    name = entry.get("name")
    marketplace = entry.get("marketplace")
    if plugin_id == ZCODE_PLUGIN_ID or (
        name == PLUGIN_REGISTRATION_NAME and marketplace == ZCODE_MARKETPLACE_ID
    ):
        return True
    if plugin_id != LEGACY_ZCODE_PLUGIN_ID and not (
        name == LEGACY_PLUGIN_REGISTRATION_NAME
        and marketplace == LEGACY_ZCODE_MARKETPLACE_ID
    ):
        return False
    raw_path = entry.get("installPath")
    if isinstance(raw_path, str) and raw_path:
        path = Path(raw_path).expanduser()
        if path.exists() or path_is_redirected(path):
            return plugin_is_tenetora_managed(path)
    source = str(entry.get("source") or "").lower()
    display_name = str(entry.get("displayName") or "").lower()
    return "agent-harness" in source or "tenetora" in source or display_name in {
        "agent harness",
        "tenetora",
    }


def zcode_hook_issues(path: Path, *, materialized: bool = False) -> list[str]:
    if not path.is_file():
        return [f"missing {path.name}"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [f"invalid {path.name}"]
    hooks = payload.get("hooks") if isinstance(payload, dict) else None
    if not isinstance(hooks, dict) or not hooks:
        return [f"{path.name} has no hooks"]
    issues: list[str] = []
    for event_name, groups in hooks.items():
        if not isinstance(groups, list):
            issues.append(f"{path.name} event {event_name} is not a list")
            continue
        for group in groups:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(entries, list):
                issues.append(f"{path.name} event {event_name} has invalid hook entries")
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    issues.append(f"{path.name} event {event_name} has a non-object hook")
                    continue
                if entry.get("type") != "process":
                    issues.append(f"{path.name} event {event_name} must use type process")
                command = entry.get("command")
                if not isinstance(command, str) or not command:
                    issues.append(f"{path.name} event {event_name} is missing command")
                elif materialized and not interpreter_meets_minimum(command):
                    issues.append(
                        f"{path.name} event {event_name} uses a Python interpreter that does not satisfy the minimum runtime: {command}"
                    )
                args = entry.get("args")
                if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
                    issues.append(f"{path.name} event {event_name} must use a string args array")
                elif not any(
                    arg.replace("\\", "/") == "${ZCODE_PLUGIN_ROOT}/hooks/tenetora_hook.py"
                    for arg in args
                ):
                    issues.append(
                        f"{path.name} event {event_name} must use ${{ZCODE_PLUGIN_ROOT}}/hooks/tenetora_hook.py"
                    )
                if not isinstance(entry.get("timeoutMs"), int):
                    issues.append(f"{path.name} event {event_name} is missing timeoutMs")
    return issues


def plugin_payload_issues(path: Path, *, source: bool = False) -> list[str]:
    issues: list[str] = []
    payload = read_plugin_manifest(path)
    if payload is None:
        issues.append("missing or invalid .zcode-plugin/plugin.json")
        return issues
    if payload.get("name") != PLUGIN_REGISTRATION_NAME:
        issues.append(f"manifest name is not {PLUGIN_REGISTRATION_NAME}")

    hooks_name = "hooks-zcode.json" if source else "hooks.json"
    issues.extend(zcode_hook_issues(path / "hooks" / hooks_name, materialized=not source))

    for skill_name in PLUGIN_SKILL_NAMES:
        if not (path / "skills" / skill_name / "SKILL.md").is_file():
            issues.append(f"missing skills/{skill_name}/SKILL.md")
    if not source:
        for skill_name in LEGACY_SKILL_NAMES:
            if (path / "skills" / skill_name).exists():
                issues.append(f"legacy skills/{skill_name} must not be exposed by the installed plugin")
    return issues


def _claude_plugin_copy_ignore(directory: str, names: list[str]) -> list[str]:
    ignored = set(COPY_IGNORE(directory, names))
    if PurePath(directory).name == "hooks":
        ignored.add("hooks.json")
    return sorted(ignored)


def plugin_copy(source: Path, dest: Path, target: str | None = None) -> None:
    validate_source_tree(source)
    ensure_unredirected_directory(dest, label="plugin destination")
    for entry in PLUGIN_COPY_ENTRIES:
        source_entry = source / entry
        if not source_entry.exists():
            continue
        dest_entry = dest / entry
        if entry == "skills":
            ensure_unredirected_directory(dest_entry, label="plugin skills destination")
            for skill_name in CANONICAL_SKILL_NAMES:
                skill_source = source_entry / skill_name
                if skill_source.is_dir():
                    ensure_safe_directory_chain(dest_entry / skill_name, label="plugin skill destination")
                    shutil.copytree(skill_source, dest_entry / skill_name, ignore=COPY_IGNORE)
            continue
        if source_entry.is_dir():
            ignore = _claude_plugin_copy_ignore if target == "claude" and entry == "hooks" else COPY_IGNORE
            shutil.copytree(source_entry, dest_entry, ignore=ignore)
        else:
            shutil.copy2(source_entry, dest_entry)


def managed_install_home() -> Path:
    registry_home = os.environ.get("TENETORA_REGISTRY_HOME")
    if registry_home:
        return validate_managed_home_path(registry_home, label="TENETORA_REGISTRY_HOME")
    return machine_home()


def native_plugin_source_path(version: str, target: str | None = None) -> Path:
    path = managed_install_home() / "plugin-sources" / version
    return path / target if target else path


def materialize_native_plugin_source(source: Path, version: str, target: str) -> Path:
    """Build a persistent native-plugin payload that exposes canonical skills only."""
    dest = ensure_safe_directory_chain(
        native_plugin_source_path(version, target),
        label="native plugin source destination",
    )
    ensure_unredirected_directory(dest.parent, label="native plugin source parent")
    temporary = dest.__class__(tempfile.mkdtemp(prefix=f".{dest.name}.", dir=str(dest.parent)))
    try:
        plugin_copy(source, temporary, target=target)
        if target == "claude" and os.name == "nt":
            hooks_path = temporary / "hooks" / "hooks-claude.json"
            payload = json.loads(hooks_path.read_text(encoding="utf-8"))
            for groups in payload.get("hooks", {}).values():
                if not isinstance(groups, list):
                    continue
                for group in groups:
                    entries = group.get("hooks") if isinstance(group, dict) else None
                    if not isinstance(entries, list):
                        continue
                    for entry in entries:
                        if isinstance(entry, dict) and isinstance(entry.get("command"), str):
                            entry["command"] = entry["command"].replace(
                                "${CLAUDE_PLUGIN_ROOT}/hooks/run-hook",
                                "${CLAUDE_PLUGIN_ROOT}\\hooks\\run-hook.cmd",
                            )
            hooks_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if dest.exists() or path_is_redirected(dest):
            remove_existing(dest)
        os.replace(temporary, dest)
        return dest
    finally:
        if temporary.exists() or path_is_redirected(temporary):
            remove_existing(temporary)


def materialize_zcode_hooks(source: Path, dest: Path) -> None:
    template = source / "hooks" / "hooks-zcode.json"
    payload = json.loads(template.read_text(encoding="utf-8"))
    hooks = payload.get("hooks") if isinstance(payload, dict) else None
    if not isinstance(hooks, dict):
        raise ValueError(f"Invalid ZCode hooks template: {template}")
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict) and entry.get("type") == "process" and entry.get("command") == "python3":
                    entry["command"] = sys.executable
    output = ensure_safe_file_path(dest / "hooks" / "hooks.json", label="ZCode hooks output")
    ensure_unredirected_directory(output.parent, label="ZCode hooks directory")
    write_json_atomic(output, payload)


def create_plugin_install(source: Path, dest: Path, actual_mode: str) -> str:
    ensure_unredirected_directory(dest.parent, label="ZCode plugin cache parent")
    ensure_safe_directory_chain(dest, label="ZCode plugin cache")
    if actual_mode != "copy":
        raise ValueError("ZCode native plugin cache requires copy mode")
    plugin_copy(source, dest)
    materialize_zcode_hooks(source, dest)
    return f"copy {dest}"


def zcode_registrations(project_root: Path) -> list[dict[str, object]]:
    registry = read_json_object(zcode_registry_path(project_root), {"version": 1, "plugins": []})
    plugins = registry.get("plugins")
    if not isinstance(plugins, list):
        raise ValueError(f"ZCode plugin registry has invalid plugins array: {zcode_registry_path(project_root)}")
    return [entry for entry in plugins if isinstance(entry, dict)]


def zcode_registration(project_root: Path) -> dict[str, object] | None:
    for entry in zcode_registrations(project_root):
        if entry.get("id") == ZCODE_PLUGIN_ID or (
            entry.get("name") == PLUGIN_REGISTRATION_NAME
            and entry.get("marketplace") == ZCODE_MARKETPLACE_ID
        ):
            return entry
    return None


def legacy_zcode_registration(project_root: Path) -> dict[str, object] | None:
    for entry in zcode_registrations(project_root):
        if entry.get("id") == LEGACY_ZCODE_PLUGIN_ID or (
            entry.get("name") == LEGACY_PLUGIN_REGISTRATION_NAME
            and entry.get("marketplace") == LEGACY_ZCODE_MARKETPLACE_ID
        ):
            return entry
    return None


def register_zcode_plugin(project_root: Path, dest: Path, version: str) -> None:
    path = zcode_registry_path(project_root)
    registry = read_json_object(path, {"version": 1, "plugins": []})
    plugins = registry.get("plugins")
    if not isinstance(plugins, list):
        raise ValueError(f"ZCode plugin registry has invalid plugins array: {path}")
    previous = next(
        (
            entry
            for entry in plugins
            if isinstance(entry, dict)
            and (
                entry.get("id") == ZCODE_PLUGIN_ID
                or (
                    entry.get("name") == PLUGIN_REGISTRATION_NAME
                    and entry.get("marketplace") == ZCODE_MARKETPLACE_ID
                )
                or zcode_entry_is_managed(entry)
            )
        ),
        None,
    )
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    entry: dict[str, object] = {
        "id": ZCODE_PLUGIN_ID,
        "name": PLUGIN_REGISTRATION_NAME,
        "displayName": "Tenetora",
        "marketplace": ZCODE_MARKETPLACE_ID,
        "version": version,
        "installPath": str(dest.resolve()),
        "installedAt": previous.get("installedAt", now) if isinstance(previous, dict) else now,
        "updatedAt": now,
        "scope": "user",
        "source": str(PACKAGE_ROOT or dest),
    }
    registry["version"] = 1
    registry["plugins"] = [
        item
        for item in plugins
        if not (isinstance(item, dict) and zcode_entry_is_managed(item))
    ] + [entry]
    write_json_atomic(path, registry)


def enable_zcode_plugin(project_root: Path) -> str:
    path = zcode_config_path(project_root)
    config = read_json_object(path, {})
    plugins = config.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError(f"ZCode config plugins field is not an object: {path}")
    enabled = plugins.setdefault("enabledPlugins", {})
    if not isinstance(enabled, dict):
        raise ValueError(f"ZCode config plugins.enabledPlugins field is not an object: {path}")
    enabled[ZCODE_PLUGIN_ID] = True
    if legacy_zcode_registration(project_root) is None:
        enabled.pop(LEGACY_ZCODE_PLUGIN_ID, None)
    write_json_atomic(path, config)
    return "master-disabled" if plugins.get("enabled") is False else "enabled"


def zcode_activation_state(project_root: Path, plugin_id: str = ZCODE_PLUGIN_ID) -> str:
    path = zcode_config_path(project_root)
    if not path.is_file():
        return "not-configured"
    config = read_json_object(path, {})
    plugins = config.get("plugins")
    if not isinstance(plugins, dict):
        return "not-configured"
    if plugins.get("enabled") is False:
        return "disabled"
    enabled = plugins.get("enabledPlugins")
    if not isinstance(enabled, dict) or plugin_id not in enabled:
        return "not-configured"
    return "enabled" if enabled.get(plugin_id) is True else "disabled"


def managed_legacy_zcode_active(project_root: Path) -> bool:
    entry = legacy_zcode_registration(project_root)
    return bool(
        entry is not None
        and zcode_entry_is_managed(entry)
        and zcode_activation_state(project_root, LEGACY_ZCODE_PLUGIN_ID) == "enabled"
    )


def clean_previous_registered_plugin(
    project_root: Path,
    previous: dict[str, object] | None,
    dest: Path,
    report: InstallReport,
    dry_run: bool,
    transaction: "ZCodeInstallTransaction | None" = None,
) -> None:
    raw = previous.get("installPath") if isinstance(previous, dict) else None
    if not isinstance(raw, str) or not raw:
        return
    old = Path(raw).expanduser()
    if old.resolve(strict=False) == dest.resolve(strict=False):
        return
    allowed_roots = (
        zcode_plugin_base("global", project_root),
        legacy_zcode_plugin_base("global", project_root),
    )
    if not any(
        old.resolve(strict=False).is_relative_to(root.resolve(strict=False))
        for root in allowed_roots
    ):
        return
    if not (old.exists() or path_is_redirected(old)) or not plugin_is_tenetora_managed(old):
        return
    if dry_run:
        report.lines.append(status_line("MIGRATE", "zcode", "global", "plugin", f"would remove superseded cache {old}"))
        return
    if transaction is not None:
        transaction.stage_removal(old)
    else:
        remove_existing(old)
    report.lines.append(status_line("MIGRATE", "zcode", "global", "plugin", f"removed superseded cache {old}"))


def clean_superseded_plugin_caches(
    target: str,
    cache_root: Path,
    current: Path,
    report: InstallReport,
    dry_run: bool,
    transaction: "ZCodeInstallTransaction | None" = None,
) -> None:
    cache_root = ensure_safe_directory_chain(cache_root, label=f"{target} plugin cache root")
    if not cache_root.is_dir():
        return
    current_resolved = current.resolve(strict=False)
    for candidate in sorted(cache_root.iterdir()):
        if path_is_redirected(candidate):
            continue
        if candidate.resolve(strict=False) == current_resolved:
            continue
        managed_cache = plugin_is_tenetora_managed(candidate) or any(
            (candidate / "hooks" / name).is_file()
            for name in ("tenetora_hook.py", "agent_harness_hook.py")
        )
        if not (candidate.is_dir() or path_is_redirected(candidate)) or not managed_cache:
            continue
        if dry_run:
            report.lines.append(
                status_line("MIGRATE", target, "global", "plugin", f"would remove superseded cache {candidate}")
            )
            continue
        if transaction is not None:
            transaction.stage_removal(candidate)
        else:
            remove_existing(candidate)
        report.lines.append(
            status_line("MIGRATE", target, "global", "plugin", f"removed superseded cache {candidate}")
        )


def next_legacy_plugin_backup(project_root: Path, scope: str) -> Path:
    parent = zcode_home(scope, project_root) / "legacy-plugins"
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    candidate = parent / timestamp / LEGACY_PLUGIN_REGISTRATION_NAME
    suffix = 2
    while candidate.exists():
        candidate = parent / f"{timestamp}-{suffix}" / LEGACY_PLUGIN_REGISTRATION_NAME
        suffix += 1
    return candidate


def prune_empty_project_zcode_plugin_root(
    project_root: Path,
    removed: Path,
    report: InstallReport,
    dry_run: bool,
) -> None:
    plugin_root = legacy_project_zcode_plugin_path(project_root).parent
    if not plugin_root.is_dir():
        return
    try:
        remaining = [entry for entry in plugin_root.iterdir() if entry != removed]
    except OSError:
        return
    if remaining:
        return
    detail = (
        f"empty legacy project plugin directory {plugin_root}; "
        "ZCode native plugins are user-scoped and remain active from the global plugin cache"
    )
    if dry_run:
        report.lines.append(status_line("MIGRATE", "zcode", "project", "plugin", f"would remove {detail}"))
        return
    plugin_root.rmdir()
    report.lines.append(status_line("MIGRATE", "zcode", "project", "plugin", f"removed {detail}"))


def clean_legacy_zcode_plugin(
    project_root: Path,
    report: InstallReport,
    dry_run: bool,
    transaction: "ZCodeInstallTransaction | None" = None,
) -> None:
    candidates = (
        ("global", legacy_zcode_plugin_path(project_root)),
        ("project", legacy_project_zcode_plugin_path(project_root)),
    )
    for legacy_scope, legacy in candidates:
        if not (legacy.exists() or path_is_redirected(legacy)) or not plugin_is_tenetora_managed(legacy):
            continue
        if path_is_redirected(legacy):
            if dry_run:
                report.lines.append(status_line("MIGRATE", "zcode", legacy_scope, "plugin", f"would remove legacy plugin symlink {legacy}"))
                continue
            if transaction is not None:
                transaction.stage_removal(legacy)
            else:
                remove_existing(legacy)
            report.lines.append(status_line("MIGRATE", "zcode", legacy_scope, "plugin", f"removed legacy plugin symlink {legacy}"))
            if legacy_scope == "project":
                prune_empty_project_zcode_plugin_root(project_root, legacy, report, dry_run)
            continue
        backup = next_legacy_plugin_backup(project_root, legacy_scope)
        if dry_run:
            report.lines.append(status_line("MIGRATE", "zcode", legacy_scope, "plugin", f"would move legacy plugin {legacy} to {backup}"))
            continue
        if transaction is not None:
            transaction.stage_archive(legacy, backup)
        else:
            ensure_unredirected_directory(backup.parent, label="ZCode legacy plugin backup parent")
            shutil.move(str(legacy), str(backup))
        report.lines.append(status_line("MIGRATE", "zcode", legacy_scope, "plugin", f"moved legacy plugin {legacy} to {backup}"))
        if legacy_scope == "project":
            prune_empty_project_zcode_plugin_root(project_root, legacy, report, dry_run)


def legacy_skill_is_managed(path: Path) -> bool:
    target = path.resolve(strict=False) if path_is_redirected(path) else path
    marker = target / "SKILL.md"
    if not marker.is_file():
        return False
    try:
        text = marker.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return any(
        marker in text
        for marker in (
            "name: agent-harness",
            "name: agent-harness-",
            "name: tenetora",
            "name: tenetora-",
        )
    )


def managed_lifecycle_destination(base: Path, path: Path) -> bool:
    if legacy_skill_is_managed(path):
        return True
    if path.name not in {ROUTER_SKILL_NAME, LEGACY_ROUTER_SKILL_NAME}:
        return False
    router_version = path / "VERSION"
    return router_version.is_file() and not path_is_redirected(router_version)


def next_legacy_backup_root(scope: str, project_root: Path) -> Path:
    parent = zcode_home(scope, project_root) / "legacy-skills"
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    candidate = parent / timestamp
    suffix = 2
    while candidate.exists():
        candidate = parent / f"{timestamp}-{suffix}"
        suffix += 1
    return candidate


def clean_legacy_zcode_install(
    scope: str,
    project_root: Path,
    report: InstallReport,
    dry_run: bool,
    transaction: "ZCodeInstallTransaction | None" = None,
) -> None:
    base = target_base("zcode", scope, project_root)
    backup_root: Path | None = None
    for skill_name in RECOGNIZED_SKILL_NAMES:
        dest = base / skill_name
        if not (dest.exists() or path_is_redirected(dest)) or not legacy_skill_is_managed(dest):
            continue
        if path_is_redirected(dest):
            backup_root = backup_root or next_legacy_backup_root(scope, project_root)
            backup = backup_root / skill_name
            if dry_run:
                report.lines.append(status_line("MIGRATE", "zcode", scope, skill_name, f"would move managed fallback symlink {dest} to {backup}"))
                continue
            if transaction is not None:
                transaction.stage_archive(dest, backup)
            else:
                ensure_unredirected_directory(backup.parent, label="ZCode legacy skill backup parent")
                shutil.move(str(dest), str(backup))
            report.lines.append(status_line("MIGRATE", "zcode", scope, skill_name, f"moved managed fallback symlink {dest} to {backup}"))
            continue
        backup_root = backup_root or next_legacy_backup_root(scope, project_root)
        backup = backup_root / skill_name
        if dry_run:
            report.lines.append(status_line("MIGRATE", "zcode", scope, skill_name, f"would move legacy skill copy {dest} to {backup}"))
            continue
        if transaction is not None:
            transaction.stage_archive(dest, backup)
        else:
            ensure_unredirected_directory(backup.parent, label="ZCode legacy skill backup parent")
            shutil.move(str(dest), str(backup))
        report.lines.append(status_line("MIGRATE", "zcode", scope, skill_name, f"moved legacy skill copy {dest} to {backup}"))


def add_zcode_activation_hint(scope: str, report: InstallReport) -> None:
    report.lines.append(
        status_line(
            "ACTION",
            "zcode",
            scope,
            "plugin",
            f"Tenetora is registered as {ZCODE_PLUGIN_ID}; reload ZCode, then verify with tenetora doctor --tools zcode",
        )
    )


def managed_tree_fingerprint(path: Path) -> str | None:
    if not path.is_dir() or path_is_redirected(path):
        return None

    def redirected_target(entry: Path) -> str:
        try:
            return os.fsdecode(os.readlink(entry))
        except OSError:
            try:
                return str(entry.resolve(strict=False))
            except OSError:
                return str(entry)

    digest = hashlib.sha256()
    walk_errors: list[OSError] = []

    def onerror(error: OSError) -> None:
        walk_errors.append(error)

    for current_root, directory_names, file_names in os.walk(path, followlinks=False, onerror=onerror):
        base = Path(current_root)
        for name in sorted(list(directory_names)):
            child = base / name
            relative = child.relative_to(path).as_posix().encode("utf-8")
            if path_is_redirected(child):
                directory_names.remove(name)
                digest.update(b"L\0" + relative + b"\0" + redirected_target(child).encode("utf-8") + b"\0")
            elif child.is_dir():
                digest.update(b"D\0" + relative + b"\0")
            else:
                digest.update(b"X\0" + relative + b"\0")
        for name in sorted(file_names):
            child = base / name
            relative = child.relative_to(path).as_posix().encode("utf-8")
            if path_is_redirected(child):
                digest.update(b"L\0" + relative + b"\0" + redirected_target(child).encode("utf-8") + b"\0")
            elif child.is_file():
                digest.update(b"F\0" + relative + b"\0")
                with child.open("rb") as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
                digest.update(b"\0")
            else:
                digest.update(b"X\0" + relative + b"\0")
    if walk_errors:
        return None
    return digest.hexdigest()


@dataclass
class ZCodeInstallTransaction:
    registry_path: Path
    config_path: Path
    registry_bytes: bytes | None
    config_bytes: bytes | None
    destination: Path
    destination_backup: Path | None = None
    created_destination: bool = False
    created_destination_fingerprint: str | None = None
    path_moves: list[tuple[Path, Path, bool]] = field(default_factory=list)
    staging_root: Path | None = None
    rollback_conflicts: list[str] = field(default_factory=list)

    @classmethod
    def begin(cls, project_root: Path, destination: Path) -> "ZCodeInstallTransaction":
        registry = ensure_safe_file_path(zcode_registry_path(project_root), label="ZCode plugin registry")
        config = ensure_safe_file_path(zcode_config_path(project_root), label="ZCode plugin config")
        destination = ensure_safe_directory_chain(destination, label="ZCode plugin cache")
        return cls(
            registry,
            config,
            registry.read_bytes() if registry.is_file() else None,
            config.read_bytes() if config.is_file() else None,
            destination,
        )

    def stage_destination(self) -> None:
        if not (self.destination.exists() or path_is_redirected(self.destination)):
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-%fZ")
        backup = self.destination.parent / f".{self.destination.name}.tenetora-rollback-{stamp}"
        ensure_unredirected_directory(self.destination.parent, label="ZCode plugin cache parent")
        validate_unredirected_entry_path(backup, label="ZCode plugin rollback backup")
        self.destination.replace(backup)
        self.destination_backup = backup

    def _transaction_root(self) -> Path:
        if self.staging_root is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-%fZ")
            self.staging_root = self.registry_path.parent / "tenetora-migration-backups" / stamp
            ensure_unredirected_directory(self.staging_root.parent, label="ZCode transaction backup parent")
            ensure_unredirected_directory(self.staging_root, label="ZCode transaction backup directory")
            if os.name != "nt":
                self.staging_root.chmod(0o700)
        return self.staging_root

    def stage_removal(self, path: Path) -> None:
        if not (path.exists() or path_is_redirected(path)):
            return
        root = self._transaction_root()
        staged = root / f"remove-{len(self.path_moves):04d}-{path.name}"
        validate_unredirected_path(path.parent, label="ZCode managed removal parent")
        validate_unredirected_entry_path(staged, label="ZCode removal backup")
        path.replace(staged)
        self.path_moves.append((path, staged, False))

    def stage_archive(self, path: Path, archive: Path) -> None:
        if not (path.exists() or path_is_redirected(path)):
            return
        validate_unredirected_path(path.parent, label="ZCode managed archive source parent")
        ensure_unredirected_directory(archive.parent, label="ZCode legacy archive parent")
        validate_unredirected_entry_path(archive, label="ZCode legacy archive")
        path.replace(archive)
        self.path_moves.append((path, archive, True))

    @staticmethod
    def restore_file(path: Path, content: bytes | None) -> None:
        if content is None:
            remove_unredirected_entry(path, label="ZCode transaction file")
            return
        path = ensure_safe_file_path(path, label="ZCode transaction file")
        ensure_unredirected_directory(path.parent, label="ZCode transaction file parent")
        mode = path.stat().st_mode & 0o777 if path.is_file() else 0o600
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

    @staticmethod
    def _json_bytes(content: bytes | None, default: dict[str, object]) -> dict[str, object]:
        if content is None:
            return json.loads(json.dumps(default))
        payload = json.loads(content.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON root is not an object")
        return payload

    @staticmethod
    def _registry_without_managed(payload: dict[str, object]) -> dict[str, object]:
        clean = json.loads(json.dumps(payload))
        plugins = clean.get("plugins")
        if isinstance(plugins, list):
            clean["plugins"] = [
                entry
                for entry in plugins
                if not (isinstance(entry, dict) and zcode_entry_is_managed(entry))
            ]
        return clean

    @staticmethod
    def _config_without_managed(payload: dict[str, object]) -> dict[str, object]:
        clean = json.loads(json.dumps(payload))
        plugins = clean.get("plugins")
        if isinstance(plugins, dict):
            enabled = plugins.get("enabledPlugins")
            if isinstance(enabled, dict):
                enabled.pop(ZCODE_PLUGIN_ID, None)
                enabled.pop(LEGACY_ZCODE_PLUGIN_ID, None)
                if not enabled:
                    plugins.pop("enabledPlugins", None)
            if not plugins:
                clean.pop("plugins", None)
        return clean

    def _restore_registry(self) -> None:
        try:
            original = self._json_bytes(self.registry_bytes, {"version": 1, "plugins": []})
            current_bytes = self.registry_path.read_bytes() if self.registry_path.is_file() else None
            current = self._json_bytes(current_bytes, {"version": 1, "plugins": []})
            if self._registry_without_managed(current) == self._registry_without_managed(original):
                self.restore_file(self.registry_path, self.registry_bytes)
                return
            original_plugins = original.get("plugins")
            current_plugins = current.get("plugins")
            if not isinstance(original_plugins, list) or not isinstance(current_plugins, list):
                raise ValueError("plugins is not an array")
            original_managed = [
                entry for entry in original_plugins if isinstance(entry, dict) and zcode_entry_is_managed(entry)
            ]
            current["plugins"] = [
                entry
                for entry in current_plugins
                if not (isinstance(entry, dict) and zcode_entry_is_managed(entry))
            ] + original_managed
            write_json_atomic(self.registry_path, current)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            self.rollback_conflicts.append(f"ZCode registry concurrent change was preserved: {exc}")

    def _restore_config(self) -> None:
        try:
            original = self._json_bytes(self.config_bytes, {})
            current_bytes = self.config_path.read_bytes() if self.config_path.is_file() else None
            current = self._json_bytes(current_bytes, {})
            if self._config_without_managed(current) == self._config_without_managed(original):
                self.restore_file(self.config_path, self.config_bytes)
                return
            original_plugins = original.get("plugins")
            original_enabled = original_plugins.get("enabledPlugins") if isinstance(original_plugins, dict) else None
            current_plugins = current.setdefault("plugins", {})
            if not isinstance(current_plugins, dict):
                raise ValueError("plugins is not an object")
            current_enabled = current_plugins.setdefault("enabledPlugins", {})
            if not isinstance(current_enabled, dict):
                raise ValueError("plugins.enabledPlugins is not an object")
            for plugin_id in (ZCODE_PLUGIN_ID, LEGACY_ZCODE_PLUGIN_ID):
                if isinstance(original_enabled, dict) and plugin_id in original_enabled:
                    current_enabled[plugin_id] = original_enabled[plugin_id]
                else:
                    current_enabled.pop(plugin_id, None)
            write_json_atomic(self.config_path, current)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            self.rollback_conflicts.append(f"ZCode config concurrent change was preserved: {exc}")

    def rollback(self) -> None:
        for original, moved, _preserve in reversed(self.path_moves):
            if not (moved.exists() or path_is_redirected(moved)):
                continue
            if original.exists() or path_is_redirected(original):
                self.rollback_conflicts.append(
                    f"ZCode rollback preserved concurrently created path: {original}"
                )
                continue
            ensure_unredirected_directory(original.parent, label="ZCode rollback destination parent")
            validate_unredirected_entry_path(original, label="ZCode rollback destination")
            moved.replace(original)
        self._restore_registry()
        self._restore_config()
        if self.created_destination and (self.destination.exists() or path_is_redirected(self.destination)):
            current_fingerprint = managed_tree_fingerprint(self.destination)
            if self.created_destination_fingerprint and current_fingerprint != self.created_destination_fingerprint:
                self.rollback_conflicts.append(
                    f"ZCode rollback preserved concurrently modified canonical cache: {self.destination}"
                )
            else:
                remove_existing(self.destination)
        if self.destination_backup is not None and self.destination_backup.exists():
            if self.destination.exists() or path_is_redirected(self.destination):
                self.rollback_conflicts.append(
                    f"ZCode rollback could not restore previous cache because destination is occupied: {self.destination}"
                )
            else:
                self.destination_backup.replace(self.destination)
        if self.staging_root is not None:
            if not self.rollback_conflicts:
                shutil.rmtree(self.staging_root, ignore_errors=True)
        if self.rollback_conflicts:
            raise RuntimeError("; ".join(self.rollback_conflicts))

    def commit(self) -> None:
        if self.destination_backup is not None and self.destination_backup.exists():
            root = self._transaction_root()
            archived = root / "previous-destination"
            validate_unredirected_entry_path(archived, label="ZCode archived destination")
            self.destination_backup.replace(archived)
            self.destination_backup = archived
        if self.staging_root is None:
            return
        archive_parent = self.staging_root.parent
        try:
            archives = sorted(
                (item for item in archive_parent.iterdir() if item.is_dir() and not path_is_redirected(item)),
                key=lambda item: item.name,
                reverse=True,
            )
            for expired in archives[3:]:
                remove_unredirected_entry(expired, label="ZCode expired transaction backup")
        except OSError:
            # Historical backup pruning is best-effort and must not invalidate
            # an already verified canonical installation.
            pass


def verify_zcode_plugin_transaction(project_root: Path, destination: Path, version: str) -> None:
    registration = zcode_registration(project_root)
    registered_path = registration.get("installPath") if isinstance(registration, dict) else None
    if (
        not isinstance(registration, dict)
        or registration.get("id") != ZCODE_PLUGIN_ID
        or registration.get("version") != version
        or not isinstance(registered_path, str)
        or Path(registered_path).expanduser().resolve(strict=False) != destination.resolve(strict=False)
    ):
        raise RuntimeError("ZCode canonical plugin registration verification failed")
    issues = plugin_payload_issues(destination)
    if issues:
        raise RuntimeError(f"ZCode canonical plugin payload verification failed: {'; '.join(issues)}")
    if zcode_activation_state(project_root) != "enabled":
        raise RuntimeError("ZCode canonical plugin activation verification failed")
    if managed_legacy_zcode_active(project_root):
        raise RuntimeError(f"legacy managed ZCode plugin {LEGACY_ZCODE_PLUGIN_ID} remains active")


def install_zcode_plugin(
    scope: str,
    project_root: Path,
    mode: str,
    force: bool,
    dry_run: bool,
    update: bool,
    source_changed: bool,
) -> InstallReport | None:
    if PACKAGE_ROOT is None or scope != "global":
        return None
    source = PACKAGE_ROOT
    report = InstallReport()
    source_version = package_version(source) or "unknown"
    plugin_base = ensure_safe_directory_chain(
        zcode_plugin_base(scope, project_root),
        label="ZCode plugin cache",
    )
    legacy_plugin_base = ensure_safe_directory_chain(
        legacy_zcode_plugin_base(scope, project_root),
        label="legacy ZCode plugin cache",
    )
    dest = ensure_safe_directory_chain(
        plugin_base / source_version,
        label="ZCode plugin destination",
    )
    ensure_safe_file_path(zcode_registry_path(project_root), label="ZCode plugin registry")
    ensure_safe_file_path(zcode_config_path(project_root), label="ZCode plugin config")
    source_issues = plugin_payload_issues(source, source=True)
    if source_issues:
        raise FileNotFoundError(f"source ZCode plugin payload is incomplete: {'; '.join(source_issues)}")
    canonical_previous = zcode_registration(project_root)
    legacy_previous = legacy_zcode_registration(project_root)
    previous = canonical_previous or (
        legacy_previous if legacy_previous is not None and zcode_entry_is_managed(legacy_previous) else None
    )
    existing = dest.exists() or path_is_redirected(dest)
    current = existing and plugin_is_tenetora_managed(dest)
    installed_payload_issues = plugin_payload_issues(dest) if current else []
    current_version = plugin_version(dest) if current else None
    registered_path = Path(str(previous.get("installPath"))).expanduser() if isinstance(previous, dict) and previous.get("installPath") else None
    registration_current = bool(
        isinstance(previous, dict)
        and previous.get("id") == ZCODE_PLUGIN_ID
        and previous.get("version") == source_version
        and registered_path is not None
        and registered_path.resolve(strict=False) == dest.resolve(strict=False)
    )
    activation = zcode_activation_state(project_root)
    payload_refresh = force or not current or bool(installed_payload_issues) or (
        update and (source_changed or current_version != source_version)
    )
    metadata_refresh = (
        not registration_current
        or activation != "enabled"
        or (legacy_previous is not None and zcode_entry_is_managed(legacy_previous))
    )
    legacy_plugins = (legacy_zcode_plugin_path(project_root), legacy_project_zcode_plugin_path(project_root))
    had_install = existing or previous is not None or any(path.exists() or path_is_redirected(path) for path in legacy_plugins)

    if existing and not current and not force:
        raise FileExistsError(
            f"{scope}:zcode:plugin: refusing to replace existing non-Agent-Harness plugin {dest}; rerun with --force"
        )
    if current and not payload_refresh and not metadata_refresh:
        transaction = ZCodeInstallTransaction.begin(project_root, dest)
        try:
            clean_superseded_plugin_caches(
                "zcode", zcode_plugin_base(scope, project_root), dest, report, dry_run, transaction
            )
            clean_superseded_plugin_caches(
                "zcode",
                legacy_zcode_plugin_base(scope, project_root),
                dest,
                report,
                dry_run,
                transaction,
            )
            clean_legacy_zcode_plugin(project_root, report, dry_run, transaction)
            clean_legacy_zcode_install(scope, project_root, report, dry_run, transaction)
            verify_zcode_plugin_transaction(project_root, dest, source_version)
            transaction.commit()
        except Exception:
            transaction.rollback()
            raise
        report.lines.insert(
            0,
            status_line(
                "CURRENT", "zcode", scope, "plugin", f"plugin {dest} version {current_version or 'unknown'}; hooks present"
            ),
        )
        report.current += 1
        report.plugins += 1
        add_zcode_activation_hint(scope, report)
        return report

    if dry_run:
        action = "would update" if had_install else "would install"
        details = f"{action} registered copy {dest} from {source}; ZCode hooks, registry, and enabledPlugins"
        report.lines.append(status_line("UPDATED" if had_install else "INSTALLED", "zcode", scope, "plugin", details))
        if had_install:
            report.updated += 1
        else:
            report.installed += 1
        report.plugins += 1
        clean_previous_registered_plugin(project_root, previous, dest, report, dry_run)
        clean_superseded_plugin_caches("zcode", zcode_plugin_base(scope, project_root), dest, report, dry_run)
        clean_superseded_plugin_caches(
            "zcode",
            legacy_zcode_plugin_base(scope, project_root),
            dest,
            report,
            dry_run,
        )
        clean_legacy_zcode_plugin(project_root, report, dry_run)
        clean_legacy_zcode_install(scope, project_root, report, dry_run)
        add_zcode_activation_hint(scope, report)
        return report

    transaction = ZCodeInstallTransaction.begin(project_root, dest)
    try:
        if payload_refresh:
            transaction.stage_destination()
            created = create_plugin_install(source, dest, "copy")
            transaction.created_destination = True
            transaction.created_destination_fingerprint = managed_tree_fingerprint(dest)
        else:
            created = f"existing copy {dest}"
        register_zcode_plugin(project_root, dest, source_version)
        activation_result = enable_zcode_plugin(project_root)
        verify_zcode_plugin_transaction(project_root, dest, source_version)
        clean_previous_registered_plugin(project_root, previous, dest, report, dry_run, transaction)
        clean_superseded_plugin_caches(
            "zcode", zcode_plugin_base(scope, project_root), dest, report, dry_run, transaction
        )
        clean_superseded_plugin_caches(
            "zcode",
            legacy_zcode_plugin_base(scope, project_root),
            dest,
            report,
            dry_run,
            transaction,
        )
        clean_legacy_zcode_plugin(project_root, report, dry_run, transaction)
        clean_legacy_zcode_install(scope, project_root, report, dry_run, transaction)
        verify_zcode_plugin_transaction(project_root, dest, source_version)
        transaction.commit()
    except Exception:
        transaction.rollback()
        raise
    report.lines.append(
        status_line(
            "UPDATED" if had_install else "INSTALLED",
            "zcode",
            scope,
            "plugin",
            f"installed {created}; version {source_version}; registered as {ZCODE_PLUGIN_ID}; hooks present",
        )
    )
    if had_install:
        report.updated += 1
    else:
        report.installed += 1
    report.plugins += 1
    if activation_result == "master-disabled":
        report.lines.append(
            status_line(
                "ACTION",
                "zcode",
                scope,
                "plugin",
                "plugins.enabled is false; enable the ZCode plugin subsystem, then reload ZCode",
            )
        )
    else:
        add_zcode_activation_hint(scope, report)
    return report


def native_plugin_entries(target: str, project_root: Path) -> list[dict[str, object]]:
    try:
        result = run_native_plugin_command(target, project_root, "plugin", "list", "--json")
    except RuntimeError as exc:
        detail = " ".join(str(exc).split())
        if len(detail) > 500:
            detail = detail[:497] + "..."
        raise NativePluginStateUnavailable(detail) from exc
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise NativePluginStateUnavailable(f"{target} plugin list returned invalid JSON") from exc
    if tool_definition(target).plugin_list_shape == "root":
        entries = payload
    else:
        entries = payload.get("installed") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise NativePluginStateUnavailable(f"{target} plugin list did not return an installed plugin list")
    return [entry for entry in entries if isinstance(entry, dict)]


def native_plugin_entry_for_identity(
    target: str,
    project_root: Path,
    plugin_id: str,
    name: str,
    marketplace_id: str,
) -> dict[str, object] | None:
    for entry in native_plugin_entries(target, project_root):
        entry_plugin_id = entry.get("id") or entry.get("pluginId")
        marketplace = entry.get("marketplaceName") or entry.get("marketplace")
        if tool_definition(target).plugin_list_shape == "root" and entry.get("scope") not in {None, "user"}:
            continue
        if entry_plugin_id == plugin_id or (
            entry.get("name") == name and marketplace == marketplace_id
        ):
            return entry
    return None


def native_plugin_entry(target: str, project_root: Path) -> dict[str, object] | None:
    return native_plugin_entry_for_identity(
        target,
        project_root,
        NATIVE_PLUGIN_ID,
        PLUGIN_REGISTRATION_NAME,
        NATIVE_MARKETPLACE_ID,
    )


def legacy_native_plugin_entry(target: str, project_root: Path) -> dict[str, object] | None:
    return native_plugin_entry_for_identity(
        target,
        project_root,
        LEGACY_NATIVE_PLUGIN_ID,
        LEGACY_PLUGIN_REGISTRATION_NAME,
        LEGACY_NATIVE_MARKETPLACE_ID,
    )


def native_plugin_install_path(
    target: str,
    entry: dict[str, object],
    project_root: Path,
    *,
    marketplace_id: str = NATIVE_MARKETPLACE_ID,
    registration_name: str = PLUGIN_REGISTRATION_NAME,
) -> Path | None:
    raw_path = entry.get("installPath") or entry.get("installedPath")
    if isinstance(raw_path, str) and raw_path:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute() or ".." in candidate.parts:
            raise RuntimeError(f"unsafe {target} native plugin install path: {candidate}")
        config_root = target_base(target, "global", project_root).parent
        allowed_root = config_root / "plugins" / "cache" / marketplace_id / registration_name
        try:
            relative = candidate.absolute().relative_to(allowed_root.absolute())
        except ValueError as exc:
            raise RuntimeError(
                f"unsafe {target} native plugin install path outside the managed cache: {candidate}"
            ) from exc
        if not relative.parts:
            raise RuntimeError(f"unsafe {target} native plugin install path: {candidate}")
        ensure_safe_directory_chain(
            candidate,
            label=f"{target} native plugin cache",
            boundary=config_root,
        )
        return candidate
    version = entry.get("version")
    if tool_definition(target).versioned_plugin_cache and isinstance(version, str) and version:
        tool_home = target_base(target, "global", project_root).parent
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", version) is None:
            raise RuntimeError(f"unsafe {target} native plugin version path component: {version!r}")
        candidate = tool_home / "plugins" / "cache" / marketplace_id / registration_name / version
        return ensure_safe_directory_chain(
            candidate,
            label=f"{target} native plugin cache",
            boundary=tool_home,
        )
    return None


def native_plugin_is_managed(target: str, path: Path) -> bool:
    manifest_dir = tool_definition(target).plugin_manifest_directory
    if manifest_dir is None:
        return False
    manifest = path / manifest_dir / "plugin.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or payload.get("name") not in {
        PLUGIN_REGISTRATION_NAME,
        LEGACY_PLUGIN_REGISTRATION_NAME,
    }:
        return False
    markers = (
        path / "hooks" / "tenetora_hook.py",
        path / "hooks" / "agent_harness_hook.py",
        path / "hooks" / "run-hook",
        path / "skills" / ROUTER_SKILL_NAME / "SKILL.md",
        path / "skills" / LEGACY_ROUTER_SKILL_NAME / "SKILL.md",
    )
    return sum(marker.is_file() for marker in markers) >= 2


def managed_legacy_native_plugin_entry(
    target: str,
    project_root: Path,
) -> tuple[dict[str, object], Path] | None:
    entry = legacy_native_plugin_entry(target, project_root)
    if entry is None:
        return None
    path = native_plugin_install_path(
        target,
        entry,
        project_root,
        marketplace_id=LEGACY_NATIVE_MARKETPLACE_ID,
        registration_name=LEGACY_PLUGIN_REGISTRATION_NAME,
    )
    if path is None or not (path.exists() or path_is_redirected(path)) or not native_plugin_is_managed(target, path):
        return None
    return entry, path


def command_hook_issues(path: Path, root_variable: str, *, require_windows_command: bool = False) -> list[str]:
    if not path.is_file():
        return [f"missing {path.name}"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [f"invalid {path.name}"]
    hooks = payload.get("hooks") if isinstance(payload, dict) else None
    if not isinstance(hooks, dict) or not hooks:
        return [f"{path.name} has no hooks"]
    issues: list[str] = []
    for event_name, groups in hooks.items():
        if not isinstance(groups, list):
            issues.append(f"{path.name} event {event_name} is not a list")
            continue
        for group in groups:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(entries, list):
                issues.append(f"{path.name} event {event_name} has no hook list")
                continue
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("type") != "command":
                    issues.append(f"{path.name} event {event_name} must use type command")
                    continue
                command = entry.get("command")
                expected_runner = f"{root_variable}/hooks/run-hook"
                command_valid = isinstance(command, str) and command_uses_expected_runner(command, expected_runner)
                if root_variable == "${CLAUDE_PLUGIN_ROOT}" and os.name == "nt" and isinstance(command, str):
                    command_valid = command_valid or command_uses_expected_runner(
                        command,
                        f"{root_variable}/hooks/run-hook.cmd",
                        windows=True,
                    )
                if not command_valid:
                    issues.append(
                        f"{path.name} event {event_name} must use {expected_runner}"
                    )
                if require_windows_command:
                    windows = entry.get("commandWindows")
                    if not isinstance(windows, str) or not command_uses_expected_runner(
                        windows,
                        "%PLUGIN_ROOT%/hooks/run-hook.cmd",
                        windows=True,
                    ):
                        issues.append(f"{path.name} event {event_name} must use run-hook.cmd on Windows")
    return issues


def windows_runner_issues(path: Path, root_variable: str) -> list[str]:
    """Validate the Windows companion for command hooks without assuming a host shell."""
    issues: list[str] = []
    if not (path.parent / "run-hook.cmd").is_file():
        issues.append(f"{path.name} requires hooks/run-hook.cmd for Windows")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return issues
    hooks = payload.get("hooks") if isinstance(payload, dict) else None
    if not isinstance(hooks, dict):
        return issues
    expected = f"{root_variable}/hooks/run-hook"
    for event_name, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("type") != "command":
                    continue
                command = entry.get("command")
                valid = isinstance(command, str) and command_uses_expected_runner(command, expected)
                if root_variable == "${CLAUDE_PLUGIN_ROOT}" and os.name == "nt" and isinstance(command, str):
                    valid = valid or command_uses_expected_runner(
                        command,
                        f"{root_variable}/hooks/run-hook.cmd",
                        windows=True,
                    )
                if valid:
                    continue
                issues.append(f"{path.name} event {event_name} must use the canonical cross-platform runner")
    return issues


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


def native_plugin_payload_issues(target: str, path: Path, *, source: bool = False) -> list[str]:
    manifest_dir = ".claude-plugin" if target == "claude" else ".codex-plugin"
    manifest = path / manifest_dir / "plugin.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [f"missing or invalid {manifest_dir}/plugin.json"]
    issues: list[str] = []
    if not isinstance(payload, dict) or payload.get("name") != PLUGIN_REGISTRATION_NAME:
        issues.append(f"manifest name is not {PLUGIN_REGISTRATION_NAME}")
    hooks_name = "hooks-claude.json" if target == "claude" else "hooks.json"
    root_variable = "${CLAUDE_PLUGIN_ROOT}" if target == "claude" else "${PLUGIN_ROOT}"
    expected_manifest_hooks = "./hooks/hooks-claude.json" if target == "claude" else None
    if target == "claude" and payload.get("hooks") != expected_manifest_hooks:
        issues.append(f"manifest hooks must be {expected_manifest_hooks}")
    if target == "codex" and "hooks" in payload:
        issues.append("Codex manifest should use default hooks/hooks.json discovery")
    issues.extend(
        command_hook_issues(
            path / "hooks" / hooks_name,
            root_variable,
            require_windows_command=target == "codex",
        )
    )
    issues.extend(windows_runner_issues(path / "hooks" / hooks_name, root_variable))
    runner = path / "hooks" / "run-hook"
    if not runner.is_file():
        issues.append("missing hooks/run-hook")
    elif os.name != "nt" and not os.access(runner, os.X_OK):
        issues.append("hooks/run-hook is not executable")
    if not (path / "hooks" / "run-hook.cmd").is_file():
        issues.append("missing hooks/run-hook.cmd")
    for skill_name in PLUGIN_SKILL_NAMES:
        if not (path / "skills" / skill_name / "SKILL.md").is_file():
            issues.append(f"missing skills/{skill_name}/SKILL.md")
    if not source:
        for skill_name in LEGACY_SKILL_NAMES:
            if (path / "skills" / skill_name).exists():
                issues.append(f"legacy skills/{skill_name} must not be exposed by the installed plugin")
    return issues


def remove_native_marketplace(
    target: str,
    project_root: Path,
    marketplace_id: str,
) -> None:
    if target == "claude":
        run_native_plugin_command(
            target,
            project_root,
            "plugin",
            "marketplace",
            "remove",
            marketplace_id,
            check=False,
        )
        return
    run_native_plugin_command(
        target,
        project_root,
        "plugin",
        "marketplace",
        "remove",
        marketplace_id,
        "--json",
        check=False,
    )


def refresh_native_marketplace(target: str, project_root: Path, source: Path) -> None:
    remove_native_marketplace(target, project_root, NATIVE_MARKETPLACE_ID)
    if target == "claude":
        run_native_plugin_command(target, project_root, "plugin", "marketplace", "add", str(source))
        return
    run_native_plugin_command(target, project_root, "plugin", "marketplace", "add", str(source), "--json")


def remove_native_plugin(
    target: str,
    project_root: Path,
    plugin_id: str = NATIVE_PLUGIN_ID,
) -> None:
    if target == "claude":
        run_native_plugin_command(
            target,
            project_root,
            "plugin",
            "uninstall",
            plugin_id,
            "--scope",
            "user",
            "--keep-data",
            "--yes",
        )
        return
    run_native_plugin_command(target, project_root, "plugin", "remove", plugin_id, "--json")


def add_native_plugin(target: str, project_root: Path) -> None:
    if target == "claude":
        run_native_plugin_command(
            target,
            project_root,
            "plugin",
            "install",
            NATIVE_PLUGIN_ID,
            "--scope",
            "user",
        )
        return
    run_native_plugin_command(target, project_root, "plugin", "add", NATIVE_PLUGIN_ID, "--json")


def legacy_native_skills_root(target: str, project_root: Path) -> Path:
    return target_base(target, "global", project_root).parent / "legacy-skills"


def native_plugin_cache_root(
    target: str,
    project_root: Path,
    *,
    marketplace_id: str = NATIVE_MARKETPLACE_ID,
    registration_name: str = PLUGIN_REGISTRATION_NAME,
) -> Path:
    return (
        target_base(target, "global", project_root).parent
        / "plugins"
        / "cache"
        / marketplace_id
        / registration_name
    )


def legacy_native_plugin_cache_root(target: str, project_root: Path) -> Path:
    return native_plugin_cache_root(
        target,
        project_root,
        marketplace_id=LEGACY_NATIVE_MARKETPLACE_ID,
        registration_name=LEGACY_PLUGIN_REGISTRATION_NAME,
    )


def clean_legacy_native_skills(target: str, project_root: Path, report: InstallReport, dry_run: bool) -> None:
    base = target_base(target, "global", project_root)
    backup_root: Path | None = None
    for skill_name in RECOGNIZED_SKILL_NAMES:
        dest = base / skill_name
        if not (dest.exists() or path_is_redirected(dest)) or not legacy_skill_is_managed(dest):
            continue
        if path_is_redirected(dest):
            if backup_root is None:
                timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                backup_root = legacy_native_skills_root(target, project_root) / timestamp
            backup = backup_root / skill_name
            if dry_run:
                report.lines.append(status_line("MIGRATE", target, "global", skill_name, f"would move managed fallback symlink {dest} to {backup}"))
            else:
                ensure_unredirected_directory(backup.parent, label="legacy native skill backup parent")
                shutil.move(str(dest), str(backup))
                report.lines.append(status_line("MIGRATE", target, "global", skill_name, f"moved managed fallback symlink {dest} to {backup}"))
            continue
        if backup_root is None:
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            backup_root = legacy_native_skills_root(target, project_root) / timestamp
        backup = backup_root / skill_name
        if dry_run:
            report.lines.append(status_line("MIGRATE", target, "global", skill_name, f"would move legacy skill copy {dest} to {backup}"))
        else:
            ensure_unredirected_directory(backup.parent, label="legacy native skill backup parent")
            shutil.move(str(dest), str(backup))
            report.lines.append(status_line("MIGRATE", target, "global", skill_name, f"moved legacy skill copy {dest} to {backup}"))


def _install_native_cli_plugin(
    target: str,
    scope: str,
    project_root: Path,
    force: bool,
    dry_run: bool,
    update: bool,
    source_changed: bool,
) -> InstallReport | None:
    if PACKAGE_ROOT is None or scope != "global" or target not in NATIVE_PLUGIN_TARGETS:
        return None
    config_root = validate_unredirected_path(
        target_base(target, "global", project_root).parent,
        label=f"{target} native plugin config directory",
    )
    config_root_missing = not config_root.exists()
    if not dry_run:
        config_root.mkdir(parents=True, exist_ok=True)
    if not native_plugin_cli_available(target, project_root):
        return None
    package_source = PACKAGE_ROOT
    source_version = package_version(package_source) or "unknown"
    source_issues = native_plugin_payload_issues(target, package_source, source=True)
    if source_issues:
        raise FileNotFoundError(f"source {target} plugin payload is incomplete: {'; '.join(source_issues)}")
    entry = None if dry_run and config_root_missing else native_plugin_entry(target, project_root)
    legacy_entry = None if dry_run and config_root_missing else legacy_native_plugin_entry(target, project_root)
    managed_legacy = (
        None
        if dry_run and config_root_missing
        else managed_legacy_native_plugin_entry(target, project_root)
    )
    if legacy_entry is not None and managed_legacy is None:
        raise RuntimeError(
            f"legacy plugin registration {LEGACY_NATIVE_PLUGIN_ID} exists but its payload ownership cannot be proven; "
            "review it manually before Tenetora can replace or disable it"
        )
    install_path = native_plugin_install_path(target, entry, project_root) if entry else None
    installed_issues = native_plugin_payload_issues(target, install_path) if install_path and install_path.exists() else []
    installed_version = str(entry.get("version")) if entry and entry.get("version") else None
    enabled = bool(entry and entry.get("enabled", True))
    refresh = bool(
        force
        or entry is None
        or install_path is None
        or not install_path.exists()
        or installed_issues
        or installed_version != source_version
        or not enabled
        or managed_legacy is not None
        or (update and source_changed)
    )
    report = InstallReport()
    had_install = entry is not None or managed_legacy is not None
    if dry_run:
        action = "would update" if had_install else "would install"
        report.lines.append(
            status_line(
                "UPDATED" if had_install else "INSTALLED",
                target,
                scope,
                "plugin",
                f"{action} {NATIVE_PLUGIN_ID} from a canonical-only Tenetora plugin payload; runtime hooks included",
            )
        )
        report.updated += int(had_install)
        report.installed += int(not had_install)
        report.plugins += 1
        current_path = install_path or native_plugin_cache_root(target, project_root) / source_version
        clean_superseded_plugin_caches(
            target,
            native_plugin_cache_root(target, project_root),
            current_path,
            report,
            dry_run=True,
        )
        clean_superseded_plugin_caches(
            target,
            legacy_native_plugin_cache_root(target, project_root),
            current_path,
            report,
            dry_run=True,
        )
        clean_legacy_native_skills(target, project_root, report, dry_run=True)
        return report

    if refresh:
        source = materialize_native_plugin_source(package_source, source_version, target)
        if entry is not None:
            remove_native_plugin(target, project_root)
        refresh_native_marketplace(target, project_root, source)
        add_native_plugin(target, project_root)
        entry = native_plugin_entry(target, project_root)
        install_path = native_plugin_install_path(target, entry, project_root) if entry else None
    if entry is None or install_path is None or not install_path.exists():
        raise RuntimeError(f"{target} plugin installation did not register {NATIVE_PLUGIN_ID}")
    final_issues = native_plugin_payload_issues(target, install_path)
    if final_issues:
        raise RuntimeError(f"installed {target} plugin is incomplete: {'; '.join(final_issues)}")
    final_enabled = bool(entry.get("enabled", True))
    if not final_enabled:
        raise RuntimeError(f"installed {target} plugin is disabled")
    if managed_legacy is not None:
        remove_native_plugin(target, project_root, LEGACY_NATIVE_PLUGIN_ID)
        if legacy_native_plugin_entry(target, project_root) is not None:
            raise RuntimeError(
                f"legacy managed plugin {LEGACY_NATIVE_PLUGIN_ID} remains registered after installing {NATIVE_PLUGIN_ID}"
            )
        remove_native_marketplace(target, project_root, LEGACY_NATIVE_MARKETPLACE_ID)
        report.lines.append(
            status_line(
                "MIGRATE",
                target,
                scope,
                "plugin",
                f"replaced legacy registration {LEGACY_NATIVE_PLUGIN_ID} with {NATIVE_PLUGIN_ID}",
            )
        )

    if refresh:
        report.lines.append(
            status_line(
                "UPDATED" if had_install else "INSTALLED",
                target,
                scope,
                "plugin",
                f"installed {NATIVE_PLUGIN_ID} version {entry.get('version')}; hooks present; activation enabled",
            )
        )
        report.updated += int(had_install)
        report.installed += int(not had_install)
    else:
        report.lines.append(
            status_line(
                "CURRENT",
                target,
                scope,
                "plugin",
                f"native plugin {install_path} version {installed_version}; hooks present; activation enabled",
            )
        )
        report.current += 1
    report.plugins += 1
    clean_superseded_plugin_caches(
        target,
        native_plugin_cache_root(target, project_root),
        install_path,
        report,
        dry_run=False,
    )
    clean_superseded_plugin_caches(
        target,
        legacy_native_plugin_cache_root(target, project_root),
        install_path,
        report,
        dry_run=False,
    )
    clean_legacy_native_skills(target, project_root, report, dry_run=False)
    if target == "codex":
        trust = codex_hook_trust(project_root)
        message = (
            "restart Codex to load the updated package; current Tenetora hook definitions are trusted"
            if trust.get("state") == "active"
            else "restart Codex and review the Tenetora hook definitions in /hooks before runtime hooks can execute"
        )
        report.lines.append(status_line("ACTION", target, scope, "plugin", message))
    else:
        report.lines.append(
            status_line(
                "ACTION",
                target,
                scope,
                "plugin",
                "restart Claude Code, then verify runtime state with tenetora doctor --tools claude",
            )
        )
    return report


def install_native_cli_plugin(
    target: str,
    scope: str,
    project_root: Path,
    force: bool,
    dry_run: bool,
    update: bool,
    source_changed: bool,
) -> InstallReport | None:
    """Install a native plugin, recovering only a proven broken legacy Codex registration."""

    recovery = assess_legacy_registration() if target == "codex" and scope == "global" else None
    if recovery is None or not recovery.recoverable:
        return _install_native_cli_plugin(
            target,
            scope,
            project_root,
            force,
            dry_run,
            update,
            source_changed,
        )
    if dry_run:
        report = InstallReport()
        report.lines.append(
            status_line(
                "MIGRATE",
                target,
                scope,
                "plugin",
                f"would back up Codex config, remove {len(recovery.sections)} proven legacy registration sections, "
                f"and install {NATIVE_PLUGIN_ID}",
            )
        )
        report.updated += 1
        report.plugins += 1
        return report

    transaction: RecoveryTransaction = begin_recovery(recovery)
    try:
        report = _install_native_cli_plugin(
            target,
            scope,
            project_root,
            force,
            dry_run,
            update,
            source_changed,
        )
        if report is None:
            raise RuntimeError("Codex native plugin recovery did not enter the native installer")
        transaction.commit()
    except Exception:
        transaction.rollback()
        raise
    report.lines.insert(
        0,
        status_line(
            "MIGRATE",
            target,
            scope,
            "plugin",
            f"recovered proven legacy Codex registration; config backup {transaction.backup_path}; "
            "legacy Hook trust was removed and was not copied",
        ),
    )
    return report


def runtime_home(target: str, scope: str, project_root: Path) -> Path:
    return target_base(target, scope, project_root).parent


def project_managed_path_relative_to_repository(
    project_root: Path,
    repository_root: Path,
    managed_path: Path,
) -> Path | None:
    """Return the Git path without dereferencing a managed symlink."""
    try:
        relative_to_project = managed_path.absolute().relative_to(project_root.absolute())
        project_prefix = project_root.resolve().relative_to(repository_root.resolve())
    except ValueError:
        return None
    return project_prefix / relative_to_project


def project_git_exclude_target(project_root: Path, managed_path: Path) -> tuple[Path, str] | None:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(project_root),
                "rev-parse",
                "--show-toplevel",
                "--git-dir",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None

    if result is not None and result.returncode == 0:
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(lines) >= 2:
            repository_root = Path(lines[0]).expanduser().resolve()
            git_directory = Path(lines[1]).expanduser()
            exclude_path = git_directory / "info" / "exclude"
            if not exclude_path.is_absolute():
                exclude_path = project_root / exclude_path
            relative_path = project_managed_path_relative_to_repository(
                project_root,
                repository_root,
                managed_path,
            )
            if relative_path is None:
                return None
            return exclude_path, f"/{relative_path.as_posix()}"

    dot_git = project_root / ".git"
    if dot_git.is_dir():
        relative_path = project_managed_path_relative_to_repository(
            project_root,
            project_root.resolve(),
            managed_path,
        )
        if relative_path is None:
            return None
        return dot_git / "info" / "exclude", f"/{relative_path.as_posix()}"
    return None


def ensure_project_runtime_git_exclude(
    project_root: Path,
    managed_path: Path,
    dry_run: bool,
) -> tuple[str, str]:
    target = project_git_exclude_target(project_root, managed_path)
    if target is None:
        return "SKIPPED", "not inside a Git worktree; no machine-local exclude was written"

    exclude_path, pattern = target
    write_path = ensure_safe_file_path(exclude_path, label="Git exclude file")
    existing = write_path.read_bytes() if write_path.is_file() else b""
    encoded_pattern = pattern.encode("utf-8")
    if encoded_pattern in {line.strip() for line in existing.splitlines()}:
        return "CURRENT", f"machine-local Git exclude already contains {pattern}: {exclude_path}"
    if dry_run:
        return "PLANNED", f"would add machine-local Git exclude {pattern}: {exclude_path}"

    ensure_unredirected_directory(write_path.parent, label="Git exclude parent")
    target_mode = write_path.stat().st_mode & 0o777 if write_path.is_file() else 0o644
    prefix = existing if not existing or existing.endswith(b"\n") else existing + b"\n"
    temporary = write_path.with_name(f".{write_path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(prefix + encoded_pattern + b"\n")
        temporary.chmod(target_mode)
        os.replace(temporary, write_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return "INSTALLED", f"added machine-local Git exclude {pattern}: {exclude_path}"


def git_path_is_ignored(project_root: Path, path: Path | str) -> bool:
    candidate = str(path)
    if isinstance(path, Path):
        try:
            candidate = path.absolute().relative_to(project_root.absolute()).as_posix()
        except ValueError:
            pass
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "check-ignore", "-q", "--", candidate],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def git_path_has_tracked_entries(project_root: Path, path: Path) -> bool:
    try:
        candidate = path.absolute().relative_to(project_root.absolute()).as_posix()
    except ValueError:
        return False
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "ls-files", "--", candidate],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def ensure_project_skill_compat_git_exclude(
    project_root: Path,
    skill_base: Path,
    dry_run: bool,
) -> tuple[str, str]:
    canonical_router = skill_base / ROUTER_SKILL_NAME
    if git_path_has_tracked_entries(project_root, canonical_router):
        return "SKIPPED", "project lifecycle skills are tracked; machine-local excludes were not added"
    canonical_ignored = git_path_is_ignored(project_root, canonical_router)
    canonical_target = project_git_exclude_target(project_root, canonical_router)
    if canonical_target is None:
        if canonical_ignored:
            return "CURRENT", "canonical project skills are already ignored by Git"
        return "SKIPPED", "not inside a Git worktree; no machine-local skill exclude was written"
    exclude_path, canonical_pattern = canonical_target
    canonical_pattern = f"{canonical_pattern}*"
    legacy_pattern = canonical_pattern.replace(
        f"/{ROUTER_SKILL_NAME}*",
        f"/{LEGACY_ROUTER_SKILL_NAME}*",
    )
    write_path = ensure_safe_file_path(exclude_path, label="Git exclude file")
    existing = write_path.read_bytes() if write_path.is_file() else b""
    canonical_pattern_bytes = canonical_pattern.encode("utf-8")
    legacy_pattern_bytes = legacy_pattern.encode("utf-8")
    normalized_lines: list[bytes] = []
    canonical_seen = False
    removed_legacy = 0
    removed_duplicates = 0
    for line in existing.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == legacy_pattern_bytes:
            removed_legacy += 1
            continue
        if stripped == canonical_pattern_bytes:
            if canonical_seen:
                removed_duplicates += 1
                continue
            canonical_seen = True
        normalized_lines.append(line)
    add_canonical = not canonical_seen and not canonical_ignored
    if add_canonical:
        canonical_seen = True
    changed = bool(removed_legacy or removed_duplicates or add_canonical)
    if not changed:
        return "CURRENT", f"machine-local Git exclude already contains canonical project skills: {exclude_path}"
    if dry_run:
        actions: list[str] = []
        if removed_legacy:
            actions.append(f"remove {removed_legacy} legacy skill exclude(s)")
        if removed_duplicates:
            actions.append(f"remove {removed_duplicates} duplicate canonical exclude(s)")
        if add_canonical:
            actions.append(f"add {canonical_pattern}")
        return "PLANNED", f"would {'; '.join(actions)}: {exclude_path}"

    ensure_unredirected_directory(write_path.parent, label="Git exclude parent")
    target_mode = write_path.stat().st_mode & 0o777 if write_path.is_file() else 0o644
    temporary = write_path.with_name(f".{write_path.name}.{os.getpid()}.tmp")
    try:
        rendered = b"".join(normalized_lines)
        if add_canonical:
            if rendered and not rendered.endswith((b"\n", b"\r")):
                rendered += b"\n"
            rendered += canonical_pattern_bytes + b"\n"
        temporary.write_bytes(rendered)
        temporary.chmod(target_mode)
        os.replace(temporary, write_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    if add_canonical and not removed_legacy and not removed_duplicates:
        return "INSTALLED", f"added machine-local Git exclude {canonical_pattern}: {exclude_path}"
    return "UPDATED", (
        f"normalized machine-local skill excludes at {exclude_path}; "
        f"removed legacy={removed_legacy}, duplicates={removed_duplicates}, added canonical={int(add_canonical)}"
    )


def cursor_command_prefix(source: Path) -> str:
    if os.name == "nt":
        command = subprocess.list2cmdline([str(source / "hooks" / "run-hook.cmd")])
        return (
            'set "TENETORA_HOOK_PLATFORM=cursor"&& '
            f'set "TENETORA_PLUGIN_ROOT={source}"&& call {command}'
        )
    environment = (
        f"TENETORA_HOOK_PLATFORM=cursor "
        f"TENETORA_PLUGIN_ROOT={shlex.quote(str(source))}"
    )
    return f"{environment} {shlex.quote(str(source / 'hooks' / 'run-hook'))}"


def materialized_cursor_hooks(source: Path) -> dict[str, object]:
    template = source / "hooks" / "hooks-cursor.json"
    payload = json.loads(template.read_text(encoding="utf-8"))
    encoded = json.dumps(payload, ensure_ascii=False)
    return json.loads(encoded.replace("__TENETORA_RUN_HOOK__", cursor_command_prefix(source)))


def cursor_hook_is_managed(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    command = entry.get("command")
    if not isinstance(command, str) or not any(mode in command for mode in CURSOR_HOOK_MODES):
        return False
    canonical_launcher = (
        "TENETORA_HOOK_PLATFORM=cursor" in command
        and "TENETORA_PLUGIN_ROOT=" in command
        and ("/hooks/run-hook" in command or "\\hooks\\run-hook.cmd" in command)
    )
    legacy_environment_launcher = (
        "AGENT_HARNESS_HOOK_PLATFORM=cursor" in command
        and "AGENT_HARNESS_PLUGIN_ROOT=" in command
        and ("/hooks/run-hook" in command or "\\hooks\\run-hook.cmd" in command)
    )
    legacy_launcher = any(
        marker in command
        for marker in ("tenetora_hook.py", "agent_harness_hook.py", "agent-harness")
    )
    return canonical_launcher or legacy_environment_launcher or legacy_launcher


def merged_cursor_hooks(existing: dict[str, object], desired: dict[str, object]) -> dict[str, object]:
    merged = dict(existing)
    current_hooks = existing.get("hooks")
    if current_hooks is not None and not isinstance(current_hooks, dict):
        raise ValueError("Cursor hooks.json has a non-object hooks field")
    desired_hooks = desired.get("hooks")
    if not isinstance(desired_hooks, dict):
        raise ValueError("Tenetora Cursor hook template is invalid")
    hooks = dict(current_hooks or {})
    for event_name, desired_entries in desired_hooks.items():
        current_entries = hooks.get(event_name, [])
        if not isinstance(current_entries, list) or not isinstance(desired_entries, list):
            raise ValueError(f"Cursor hooks.json event {event_name} is not a list")
        hooks[event_name] = [entry for entry in current_entries if not cursor_hook_is_managed(entry)] + desired_entries
    merged["version"] = existing.get("version", desired.get("version", 1))
    merged["hooks"] = hooks
    return merged


def runtime_path_safety_error(
    target: str,
    scope: str,
    project_root: Path,
    path: Path,
) -> str | None:
    boundary = project_root if scope == "project" else runtime_home(target, scope, project_root)
    try:
        ensure_safe_directory_chain(
            path,
            label=f"{scope} {target} runtime target",
            boundary=boundary,
            allow_file=True,
        )
    except RuntimeError as exc:
        return str(exc)
    return None


def cursor_runtime_state(scope: str, project_root: Path, source: Path) -> tuple[str, str, Path]:
    path = runtime_home("cursor", scope, project_root) / "hooks.json"
    safety_error = runtime_path_safety_error("cursor", scope, project_root, path)
    if safety_error is not None:
        return "conflict", safety_error, path
    if path.exists() and not path.is_file():
        return "conflict", "hooks.json path is not a file", path
    if not path.is_file():
        return "missing", "hooks.json is missing", path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "conflict", "hooks.json is invalid", path
    if not isinstance(payload, dict) or not isinstance(payload.get("hooks"), dict):
        return "conflict", "hooks.json has no hooks object", path
    expected = materialized_cursor_hooks(source).get("hooks")
    hooks = payload["hooks"]
    missing: list[str] = []
    for event_name, expected_entries in expected.items() if isinstance(expected, dict) else []:
        actual = hooks.get(event_name)
        managed_actual = (
            [entry for entry in actual if cursor_hook_is_managed(entry)]
            if isinstance(actual, list)
            else []
        )
        if not isinstance(actual, list) or managed_actual != expected_entries:
            missing.append(str(event_name))
    if missing:
        return "stale", f"managed events are missing or stale: {', '.join(missing)}", path
    return "ok", "runtime hooks active", path


def cursor_command_source(command: str) -> Path | None:
    for variable in ("TENETORA_PLUGIN_ROOT", "AGENT_HARNESS_PLUGIN_ROOT"):
        windows_match = re.search(rf'set\s+"{variable}=([^\"]+)"', command, re.IGNORECASE)
        if windows_match:
            return Path(windows_match.group(1)).expanduser()
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    for prefix in ("TENETORA_PLUGIN_ROOT=", "AGENT_HARNESS_PLUGIN_ROOT="):
        for token in tokens:
            if token.startswith(prefix) and token[len(prefix) :]:
                return Path(token[len(prefix) :]).expanduser()
    return None


def historical_cursor_runtime_state(path: Path) -> tuple[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unsafe", "hooks.json is unreadable or invalid"
    hooks = payload.get("hooks") if isinstance(payload, dict) else None
    if not isinstance(hooks, dict):
        return "unsafe", "hooks.json has no hooks object"

    managed: dict[str, list[object]] = {}
    source_roots: set[Path] = set()
    for event_name, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        managed_entries = [entry for entry in entries if cursor_hook_is_managed(entry)]
        if not managed_entries:
            continue
        managed[str(event_name)] = managed_entries
        for entry in managed_entries:
            command = entry.get("command") if isinstance(entry, dict) else None
            source = cursor_command_source(command) if isinstance(command, str) else None
            if source is None:
                return "unsafe", "managed Cursor command does not identify its package root"
            source_roots.add(source)

    if not managed:
        return "unsafe", "no Tenetora managed Cursor entries were found"
    if len(source_roots) != 1:
        return "unsafe", "managed Cursor entries reference multiple package roots"
    source = next(iter(source_roots))
    version = package_version(source)
    if version is None:
        return "unsafe", f"historical package root is unavailable: {source}"
    if not (source / "hooks" / "hooks-cursor.json").is_file():
        return "unsafe", f"historical Cursor template is unavailable: {source}"
    try:
        expected_payload = materialized_cursor_hooks(source)
    except (OSError, ValueError, json.JSONDecodeError):
        return "unsafe", f"historical Cursor template is invalid: {source}"
    expected_hooks = expected_payload.get("hooks")
    if not isinstance(expected_hooks, dict) or managed != expected_hooks:
        return "unsafe", "managed Cursor entries differ from their historical package template"
    return "legacy-ok", f"unchanged Tenetora Cursor runtime from {version}: {source}"


def materialized_opencode_plugin(source: Path, version: str | None = None) -> str:
    template = (source / "hooks" / "opencode-plugin.js").read_text(encoding="utf-8")
    resolved_version = version or package_version(source) or "unknown"
    return (
        template.replace("__TENETORA_VERSION__", resolved_version)
        .replace("__TENETORA_PACKAGE_ROOT__", json.dumps(str(source)))
        .replace("__AGENT_HARNESS_VERSION__", resolved_version)
        .replace("__AGENT_HARNESS_PACKAGE_ROOT__", json.dumps(str(source)))
    )


def materialized_pi_extension(source: Path, version: str | None = None) -> str:
    template = (source / "hooks" / "pi-extension.ts").read_text(encoding="utf-8")
    resolved_version = version or package_version(source) or "unknown"
    return template.replace("__TENETORA_VERSION__", resolved_version).replace(
        "__TENETORA_PACKAGE_ROOT__", json.dumps(str(source))
    )


def opencode_runtime_state(scope: str, project_root: Path, source: Path) -> tuple[str, str, Path]:
    path = runtime_home("opencode", scope, project_root) / "plugins" / "tenetora.js"
    legacy_path = path.with_name("agent-harness.js")
    safety_error = runtime_path_safety_error("opencode", scope, project_root, path)
    if safety_error is not None:
        return "conflict", safety_error, path
    if not path.exists() and legacy_path.is_file():
        return "stale", "legacy OpenCode adapter must be replaced", path
    if path.exists() and not path.is_file():
        return "conflict", "runtime plugin path is not a file", path
    if not path.is_file():
        return "missing", "runtime plugin is missing", path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "conflict", "runtime plugin is unreadable", path
    if OPENCODE_RUNTIME_MARKER not in text and LEGACY_OPENCODE_RUNTIME_MARKER in text:
        return "stale", "legacy OpenCode adapter marker must be replaced", path
    if OPENCODE_RUNTIME_MARKER not in text:
        return "conflict", "runtime plugin path is owned by another plugin", path
    if text != materialized_opencode_plugin(source):
        return "stale", "runtime plugin payload is stale", path
    return "ok", "runtime plugin active", path


def pi_runtime_state(scope: str, project_root: Path, source: Path) -> tuple[str, str, Path]:
    path = runtime_home("pi", scope, project_root) / "extensions" / "tenetora.ts"
    safety_error = runtime_path_safety_error("pi", scope, project_root, path)
    if safety_error is not None:
        return "conflict", safety_error, path
    if path.exists() and not path.is_file():
        return "conflict", "Pi extension path is not a file", path
    if not path.is_file():
        return "missing", "Pi extension is missing", path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "conflict", "Pi extension is unreadable", path
    if PI_RUNTIME_MARKER not in text:
        return "conflict", "Pi extension path is owned by another extension", path
    if text != materialized_pi_extension(source):
        return "stale", "Pi extension payload is stale", path
    return "ok", "Pi extension active", path


def historical_opencode_runtime_state(path: Path) -> tuple[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "unsafe", "runtime plugin is unreadable"
    match = OPENCODE_RUNTIME_ROOT_RE.search(text) or LEGACY_OPENCODE_RUNTIME_ROOT_RE.search(text)
    if not match:
        return "unsafe", "runtime plugin does not identify its package root"
    try:
        source = Path(json.loads(match.group(1))).expanduser()
    except (TypeError, ValueError, json.JSONDecodeError):
        return "unsafe", "runtime plugin package root is invalid"
    version = package_version(source)
    if version is None:
        return "unsafe", f"historical package root is unavailable: {source}"
    if not (source / "hooks" / "opencode-plugin.js").is_file():
        return "unsafe", f"historical OpenCode template is unavailable: {source}"
    try:
        candidates = [(version, materialized_opencode_plugin(source, version))]
        version_match = OPENCODE_RUNTIME_VERSION_RE.search(text) or LEGACY_OPENCODE_RUNTIME_VERSION_RE.search(text)
        if version_match:
            embedded_version = json.loads(version_match.group(1))
            if isinstance(embedded_version, str) and embedded_version and embedded_version != version:
                candidates.append((embedded_version, materialized_opencode_plugin(source, embedded_version)))
    except (OSError, json.JSONDecodeError):
        return "unsafe", f"historical OpenCode template is unreadable: {source}"
    matched_version = next((candidate_version for candidate_version, expected in candidates if text == expected), None)
    if matched_version is None:
        return "unsafe", "OpenCode runtime differs from its historical package template"
    return "legacy-ok", f"unchanged Tenetora OpenCode runtime from {matched_version}: {source}"


def historical_pi_runtime_state(path: Path) -> tuple[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "unsafe", "Pi extension is unreadable"
    match = PI_RUNTIME_ROOT_RE.search(text)
    if not match:
        return "unsafe", "Pi extension does not identify its package root"
    try:
        source = Path(json.loads(match.group(1))).expanduser()
    except (TypeError, ValueError, json.JSONDecodeError):
        return "unsafe", "Pi extension package root is invalid"
    version = package_version(source)
    if version is None:
        return "unsafe", f"historical package root is unavailable: {source}"
    if not (source / "hooks" / "pi-extension.ts").is_file():
        return "unsafe", f"historical Pi extension template is unavailable: {source}"
    try:
        candidates = [(version, materialized_pi_extension(source, version))]
        version_match = PI_RUNTIME_VERSION_RE.search(text)
        if version_match:
            embedded_version = json.loads(version_match.group(1))
            if isinstance(embedded_version, str) and embedded_version and embedded_version != version:
                candidates.append((embedded_version, materialized_pi_extension(source, embedded_version)))
    except (OSError, json.JSONDecodeError):
        return "unsafe", f"historical Pi extension template is unreadable: {source}"
    matched_version = next((candidate_version for candidate_version, expected in candidates if text == expected), None)
    if matched_version is None:
        return "unsafe", "Pi extension differs from its historical package template"
    return "legacy-ok", f"unchanged Tenetora Pi extension from {matched_version}: {source}"


def runtime_adapter_state(
    target: str,
    scope: str,
    project_root: Path,
    source: Path,
) -> tuple[str, str, Path]:
    if target == "cursor":
        return cursor_runtime_state(scope, project_root, source)
    if target == "opencode":
        return opencode_runtime_state(scope, project_root, source)
    if target == "pi":
        return pi_runtime_state(scope, project_root, source)
    raise ValueError(f"Unsupported runtime adapter target: {target}")


def runtime_adapter_prune_state(
    target: str,
    scope: str,
    project_root: Path,
    source: Path,
) -> tuple[str, str, Path]:
    state, detail, path = runtime_adapter_state(target, scope, project_root, source)
    if state in {"missing", "ok"}:
        return state, detail, path
    if state != "stale" or not path.is_file():
        return "unsafe", detail, path
    historical_state, historical_detail = (
        historical_cursor_runtime_state(path)
        if target == "cursor"
        else historical_opencode_runtime_state(path)
        if target == "opencode"
        else historical_pi_runtime_state(path)
    )
    return historical_state, historical_detail, path


def runtime_scope_plan(
    target: str,
    scopes: list[str],
    project_root: Path,
    *,
    prune_shadowed: bool,
    owner_override: str | None = None,
) -> dict[str, object]:
    if owner_override is not None:
        if owner_override not in {"global", "project"}:
            raise ValueError(f"invalid runtime owner override for {target}: {owner_override}")
        selected_scope = scopes[0] if scopes else owner_override
        if PACKAGE_ROOT is None or target not in RUNTIME_ADAPTER_TARGETS:
            return {"effective_scope": selected_scope, "conflict": None, "configured": []}
        state, detail, _ = runtime_adapter_state(target, selected_scope, project_root, PACKAGE_ROOT)
        configured = [] if state == "missing" else [selected_scope]
        if selected_scope != owner_override and configured:
            if not prune_shadowed:
                return {
                    "effective_scope": None,
                    "conflict": f"{target} has a shadowed managed runtime in {selected_scope} scope",
                    "configured": configured,
                }
            prune_state, prune_detail, _ = runtime_adapter_prune_state(
                target,
                selected_scope,
                project_root,
                PACKAGE_ROOT,
            )
            if prune_state not in {"ok", "legacy-ok"}:
                return {
                    "effective_scope": None,
                    "conflict": (
                        f"{target} shadowed runtime cannot be safely pruned in {selected_scope} scope: "
                        f"{prune_detail or detail}"
                    ),
                    "configured": configured,
                }
        return {
            "effective_scope": owner_override,
            "conflict": None,
            "configured": configured,
            "machine_owner": owner_override,
        }
    preferred_scope = (
        scopes[0]
        if len(scopes) == 1
        else str(platform_contract(target)["both_runtime_preference"])
    )
    if PACKAGE_ROOT is None or target not in RUNTIME_ADAPTER_TARGETS:
        return {"effective_scope": scopes[0] if scopes else "global", "conflict": None, "configured": []}
    states = {
        scope: runtime_adapter_state(target, scope, project_root, PACKAGE_ROOT)[0]
        for scope in ("global", "project")
    }
    configured = [scope for scope, state in states.items() if state != "missing"]
    if len(configured) > 1:
        if len(scopes) < 2 and not prune_shadowed:
            return {
                "effective_scope": None,
                "conflict": f"{target} has Tenetora runtime adapters in both global and project scope",
                "configured": configured,
                "remediation": f"Select --both before pruning duplicate {target} runtime adapters",
            }
        if not prune_shadowed:
            return {
                "effective_scope": None,
                "conflict": f"{target} has Tenetora runtime adapters in both global and project scope",
                "configured": configured,
            }
        unsafe = [
            scope
            for scope in configured
            if runtime_adapter_prune_state(target, scope, project_root, PACKAGE_ROOT)[0]
            not in {"ok", "legacy-ok"}
        ]
        if unsafe:
            return {
                "effective_scope": None,
                "conflict": (
                    f"{target} shadowed runtime cannot be safely pruned because ownership/content is not proven in: "
                    + ", ".join(unsafe)
                ),
                "configured": configured,
            }
        effective_scope = preferred_scope if preferred_scope in configured else configured[0]
        return {
            "effective_scope": effective_scope,
            "conflict": None,
            "configured": configured,
            "prune_scopes": [scope for scope in configured if scope != effective_scope],
        }
    if configured:
        return {"effective_scope": configured[0], "conflict": None, "configured": configured}
    return {"effective_scope": preferred_scope, "conflict": None, "configured": []}


def install_runtime_adapter(
    target: str,
    scope: str,
    project_root: Path,
    force: bool,
    dry_run: bool,
) -> InstallReport | None:
    if PACKAGE_ROOT is None or target not in RUNTIME_ADAPTER_TARGETS:
        return None
    source = PACKAGE_ROOT
    state, detail, path = runtime_adapter_state(target, scope, project_root, source)
    if scope == "project":
        ensure_safe_directory_chain(
            path,
            label=f"project {target} runtime target",
            boundary=project_root,
            allow_file=True,
        )
    else:
        ensure_safe_directory_chain(
            path,
            label=f"global {target} runtime target",
            boundary=runtime_home(target, scope, project_root),
            allow_file=True,
        )
    report = InstallReport(runtimes=1)
    if state == "conflict" and path.exists() and not path.is_file():
        raise FileExistsError(f"{scope}:{target}:runtime: {detail} at {path}; move or remove that directory first")
    if state == "conflict" and not force:
        raise FileExistsError(f"{scope}:{target}:runtime: {detail} at {path}; rerun with --force")
    if target in {"opencode", "pi"} and scope == "project":
        exclude_status, exclude_detail = ensure_project_runtime_git_exclude(project_root, path, dry_run)
        report.lines.append(status_line(exclude_status, target, scope, "git-exclude", exclude_detail))
    if state == "ok":
        report.lines.append(status_line("CURRENT", target, scope, "runtime", f"{detail}: {path}"))
        report.current += 1
        return report
    status = "INSTALLED" if state == "missing" else "UPDATED"
    if dry_run:
        report.lines.append(status_line(status, target, scope, "runtime", f"would write {path}; {detail}"))
        report.installed += int(state == "missing")
        report.updated += int(state != "missing")
        return report
    ensure_unredirected_directory(path.parent, label=f"{target} runtime parent")
    if target == "cursor":
        existing: dict[str, object] = {}
        if path.is_file() and state != "conflict":
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                existing = payload
        elif path.exists() and state == "conflict":
            backup = path.with_name(f"hooks.json.tenetora-backup-{datetime.now().strftime('%Y%m%d%H%M%S')}")
            shutil.copy2(path, backup)
        merged = merged_cursor_hooks(existing, materialized_cursor_hooks(source))
        write_json_atomic(path, merged)
        message = "installed native Cursor hooks; session context, git guard, and claim follow-up active"
    elif target == "opencode":
        if path.exists() and state == "conflict":
            backup = path.with_name(f"tenetora.js.backup-{datetime.now().strftime('%Y%m%d%H%M%S')}")
            shutil.copy2(path, backup)
        path.write_text(materialized_opencode_plugin(source), encoding="utf-8")
        legacy_path = path.with_name("agent-harness.js")
        if legacy_path.is_file():
            historical_state, _ = historical_opencode_runtime_state(legacy_path)
            if historical_state == "legacy-ok":
                legacy_path.unlink()
        message = "installed native OpenCode plugin; context and pre-tool git guard active"
    else:
        if path.exists() and state == "conflict":
            backup = path.with_name(f"tenetora.ts.backup-{datetime.now().strftime('%Y%m%d%H%M%S')}")
            shutil.copy2(path, backup)
        path.write_text(materialized_pi_extension(source), encoding="utf-8")
        message = "installed native Pi extension; context and pre-tool git guard active-partial"
    report.lines.append(status_line(status, target, scope, "runtime", f"{message}: {path}"))
    report.installed += int(state == "missing")
    report.updated += int(state != "missing")
    return report


def remove_runtime_adapter(
    target: str,
    scope: str,
    project_root: Path,
    dry_run: bool,
    owner_scope: str | None = None,
) -> InstallReport:
    if PACKAGE_ROOT is None or target not in RUNTIME_ADAPTER_TARGETS:
        return InstallReport()
    state, detail, path = runtime_adapter_prune_state(target, scope, project_root, PACKAGE_ROOT)
    report = InstallReport(runtimes=1)
    if state == "missing":
        message = (
            f"runtime already absent at {path}; effective runtime is owned by {owner_scope} scope"
            if owner_scope in {"global", "project"}
            else f"no shadowed managed runtime at {path}"
        )
        report.lines.append(status_line("CURRENT", target, scope, "runtime", message))
        report.current += 1
        return report
    if state not in {"ok", "legacy-ok"}:
        raise FileExistsError(
            f"{scope}:{target}:runtime: refusing to prune {path}; ownership/content is not an unchanged Tenetora adapter ({detail})"
        )
    if dry_run:
        owner_detail = f"; effective runtime remains in {owner_scope} scope" if owner_scope else ""
        report.lines.append(
            status_line("PRUNED", target, scope, "runtime", f"would remove managed runtime from {path}{owner_detail}")
        )
        report.updated += 1
        return report
    if target in {"opencode", "pi"}:
        path.unlink()
        if target == "opencode":
            legacy_path = path.with_name("agent-harness.js")
            if legacy_path.is_file():
                historical_state, _ = historical_opencode_runtime_state(legacy_path)
                if historical_state == "legacy-ok":
                    legacy_path.unlink()
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        hooks = payload.get("hooks") if isinstance(payload, dict) else None
        if not isinstance(payload, dict) or not isinstance(hooks, dict):
            raise FileExistsError(f"{scope}:{target}:runtime: refusing to prune invalid Cursor hooks at {path}")
        remaining_hooks: dict[str, object] = {}
        for event_name, entries in hooks.items():
            if not isinstance(entries, list):
                remaining_hooks[event_name] = entries
                continue
            remaining = [entry for entry in entries if not cursor_hook_is_managed(entry)]
            if remaining:
                remaining_hooks[event_name] = remaining
        remaining_payload = dict(payload)
        remaining_payload["hooks"] = remaining_hooks
        other_fields = {key: value for key, value in remaining_payload.items() if key not in {"version", "hooks"}}
        if remaining_hooks or other_fields:
            write_json_atomic(path, remaining_payload)
        else:
            path.unlink()
    owner_detail = f"; effective runtime remains in {owner_scope} scope" if owner_scope else ""
    report.lines.append(
        status_line(
            "PRUNED",
            target,
            scope,
            "runtime",
            f"removed unchanged Tenetora managed runtime from {path}; {detail}{owner_detail}",
        )
    )
    report.updated += 1
    return report


def status_runtime_adapter(target: str, scope: str, project_root: Path) -> InstallReport | None:
    if PACKAGE_ROOT is None or target not in RUNTIME_ADAPTER_TARGETS:
        return None
    state, detail, path = runtime_adapter_state(target, scope, project_root, PACKAGE_ROOT)
    report = InstallReport(runtimes=1)
    if state == "ok":
        report.lines.append(status_line("CURRENT", target, scope, "runtime", f"{detail}: {path}"))
        report.current += 1
    elif state == "conflict":
        report.lines.append(status_line("CONFLICT", target, scope, "runtime", f"{detail}: {path}"))
        report.conflicts += 1
    else:
        report.lines.append(status_line("MISSING", target, scope, "runtime", f"{detail}: {path}"))
        report.missing += 1
    return report


def resolved_mode(mode: str) -> str:
    if mode == "auto":
        return "copy" if os.name == "nt" else "symlink"
    return mode


def lifecycle_skill_sources() -> list[Path]:
    if not SKILLS_ROOT.exists():
        raise FileNotFoundError(f"Skills directory does not exist: {SKILLS_ROOT}")
    sources = sorted(
        path
        for path in SKILLS_ROOT.iterdir()
        if path.is_dir()
        and path.name in CANONICAL_SKILL_NAMES
        and (path / "SKILL.md").is_file()
    )
    if not sources:
        raise FileNotFoundError(f"No Tenetora lifecycle skills found under: {SKILLS_ROOT}")
    return sources


def skill_declares_name(path: Path, expected_name: str) -> bool:
    target = path.resolve(strict=False) if path_is_redirected(path) else path
    skill_file = target / "SKILL.md"
    if not skill_file.is_file():
        return False
    try:
        text = skill_file.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return re.search(rf"(?m)^name:\s*{re.escape(expected_name)}\s*$", text) is not None


def next_legacy_alias_backup_root(target: str, scope: str) -> Path:
    parent = managed_install_home() / "backups" / "legacy-skill-aliases"
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    candidate = parent / timestamp / target / scope
    suffix = 2
    while candidate.exists():
        candidate = parent / f"{timestamp}-{suffix}" / target / scope
        suffix += 1
    return candidate


def clean_legacy_skill_aliases(
    target: str,
    scope: str,
    project_root: Path,
    report: InstallReport,
    dry_run: bool,
) -> None:
    """Back up proven legacy aliases after canonical skills have been installed."""
    base = target_base(target, scope, project_root)
    backup_root: Path | None = None
    for skill_name in LEGACY_SKILL_NAMES:
        dest = base / skill_name
        if not (dest.exists() or path_is_redirected(dest)):
            continue
        if not skill_declares_name(dest, skill_name):
            report.lines.append(
                status_line(
                    "CONFLICT",
                    target,
                    scope,
                    skill_name,
                    f"legacy-named path is not a provable Tenetora-managed skill and was preserved: {dest}",
                )
            )
            report.conflicts += 1
            continue
        backup_root = backup_root or next_legacy_alias_backup_root(target, scope)
        backup = backup_root / skill_name
        kind = "symlink" if path_is_redirected(dest) else "copy"
        if dry_run:
            report.lines.append(
                status_line(
                    "MIGRATE",
                    target,
                    scope,
                    skill_name,
                    f"would move managed legacy skill {kind} {dest} to {backup}",
                )
            )
            report.updated += 1
            continue
        ensure_unredirected_directory(backup.parent, label="legacy skill alias backup parent")
        shutil.move(str(dest), str(backup))
        report.lines.append(
            status_line(
                "MIGRATE",
                target,
                scope,
                skill_name,
                f"moved managed legacy skill {kind} {dest} to {backup}",
            )
        )
        report.updated += 1


def ensure_source(source: Path) -> None:
    if not source.exists():
        raise FileNotFoundError(f"Skill source does not exist: {source}")


def copy_skill(source: Path, dest: Path) -> None:
    shutil.copytree(source, dest, ignore=COPY_IGNORE)


def create_install(base: Path, dest: Path, source: Path, actual_mode: str) -> str:
    base.mkdir(parents=True, exist_ok=True)
    if actual_mode == "copy":
        copy_skill(source, dest)
        return f"copy {dest}"

    dest.symlink_to(source, target_is_directory=True)
    return f"symlink {dest} -> {source}"


def is_lifecycle_skill_source(path: Path, skill_name: str) -> bool:
    skill_file = path / "SKILL.md"
    if not skill_file.is_file():
        return False
    try:
        text = skill_file.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    if f"name: {skill_name}" not in text:
        return False
    return any(
        (path.parent / router_name / "SKILL.md").is_file()
        for router_name in (ROUTER_SKILL_NAME, LEGACY_ROUTER_SKILL_NAME)
    )


def is_managed_tenetora_symlink_target(path: Path, skill_name: str) -> bool:
    return is_lifecycle_skill_source(path, skill_name)


def status_native_cli_plugin(target: str, scope: str, project_root: Path) -> InstallReport | None:
    if PACKAGE_ROOT is None or scope != "global" or target not in NATIVE_PLUGIN_TARGETS:
        return None
    if not native_plugin_cli_available(target, project_root):
        return None
    report = InstallReport()
    source_version = package_version(PACKAGE_ROOT) or "unknown"
    inspection_error: str | None = None
    try:
        entry = native_plugin_entry(target, project_root)
        legacy_entry = legacy_native_plugin_entry(target, project_root)
    except NativePluginStateUnavailable as exc:
        entry = None
        legacy_entry = None
        inspection_error = str(exc)
    if inspection_error is not None:
        report.lines.append(
            status_line(
                "MISSING",
                target,
                scope,
                "plugin",
                f"native plugin state is unreadable: {inspection_error}; repair the {target} marketplace configuration and rerun the installer",
            )
        )
        report.missing += 1
        report.plugins += 1
    elif legacy_entry is not None:
        legacy_path = native_plugin_install_path(
            target,
            legacy_entry,
            project_root,
            marketplace_id=LEGACY_NATIVE_MARKETPLACE_ID,
            registration_name=LEGACY_PLUGIN_REGISTRATION_NAME,
        )
        ownership = "managed" if legacy_path is not None and native_plugin_is_managed(target, legacy_path) else "unverified"
        report.lines.append(
            status_line(
                "CONFLICT",
                target,
                scope,
                "plugin",
                f"legacy registration {LEGACY_NATIVE_PLUGIN_ID} remains active or installed ({ownership}); rerun update to converge on {NATIVE_PLUGIN_ID}",
            )
        )
        report.conflicts += 1
    elif entry is None:
        report.lines.append(
            status_line(
                "MISSING",
                target,
                scope,
                "plugin",
                f"native plugin {NATIVE_PLUGIN_ID} is not installed; lifecycle skills alone do not activate runtime hooks",
            )
        )
        report.missing += 1
    else:
        install_path = native_plugin_install_path(target, entry, project_root)
        issues = (
            native_plugin_payload_issues(target, install_path)
            if install_path is not None and install_path.exists()
            else ["installed plugin cache is missing"]
        )
        installed_version = str(entry.get("version") or "unknown")
        enabled = bool(entry.get("enabled", True))
        if issues:
            report.lines.append(
                status_line(
                    "MISSING",
                    target,
                    scope,
                    "plugin",
                    f"incomplete native plugin {install_path or '-'}: {'; '.join(issues)}",
                )
            )
            report.missing += 1
        elif installed_version != source_version:
            report.lines.append(
                status_line(
                    "MISSING",
                    target,
                    scope,
                    "plugin",
                    f"stale native plugin {install_path} version {installed_version}; expected {source_version}",
                )
            )
            report.missing += 1
        elif not enabled:
            report.lines.append(status_line("MISSING", target, scope, "plugin", "native plugin is installed but disabled"))
            report.missing += 1
        else:
            note = "hooks present; activation enabled"
            if target == "codex":
                trust = codex_hook_trust(project_root)
                note += "; hooks trusted and active" if trust.get("state") == "active" else "; trust must be reviewed in /hooks"
            report.lines.append(
                status_line("CURRENT", target, scope, "plugin", f"native plugin {install_path} version {installed_version}; {note}")
            )
            report.current += 1
        report.plugins += 1
    legacy_base = target_base(target, scope, project_root)
    if any(
        (legacy_base / skill_name).exists() or path_is_redirected(legacy_base / skill_name)
        for skill_name in RECOGNIZED_SKILL_NAMES
    ):
        report.lines.append(
            status_line(
                "FALLBACK",
                target,
                scope,
                "skills",
                f"direct lifecycle skill fallback remains under {legacy_base}; native plugin is preferred",
            )
        )
    return report


def status_one(target: str, scope: str, project_root: Path) -> InstallReport:
    native_report = tool_installer(target).status_native_surface(
        SURFACE_OPERATIONS,
        target=target,
        scope=scope,
        project_root=project_root,
    )
    if native_report is not None:
        return native_report
    if tool_definition(target).installer == "zcode-native" and scope == "global" and PACKAGE_ROOT is not None:
        report = InstallReport()
        source_version = package_version(PACKAGE_ROOT) or "unknown"
        registration = zcode_registration(project_root)
        legacy_registration = legacy_zcode_registration(project_root)
        if legacy_registration is not None and zcode_entry_is_managed(legacy_registration):
            report.lines.append(
                status_line(
                    "CONFLICT",
                    target,
                    scope,
                    "plugin",
                    f"legacy registration {LEGACY_ZCODE_PLUGIN_ID} remains; rerun update to converge on {ZCODE_PLUGIN_ID}",
                )
            )
            report.conflicts += 1
        registered_path = registration.get("installPath") if isinstance(registration, dict) else None
        dest = (
            Path(str(registered_path)).expanduser()
            if isinstance(registered_path, str) and registered_path
            else zcode_plugin_path(scope, project_root, source_version)
        )
        if registration is None:
            report.lines.append(
                status_line(
                    "MISSING",
                    target,
                    scope,
                    "plugin",
                    f"plugin is not registered in {zcode_registry_path(project_root)}; expected cache {dest}",
                )
            )
            report.missing += 1
        elif not dest.exists() and not path_is_redirected(dest):
            report.lines.append(status_line("MISSING", target, scope, "plugin", f"registered plugin cache is missing {dest}"))
            report.missing += 1
        elif not plugin_is_tenetora_managed(dest):
            report.lines.append(status_line("CONFLICT", target, scope, "plugin", f"invalid or foreign plugin {dest}"))
            report.conflicts += 1
        else:
            payload_issues = plugin_payload_issues(dest)
            if payload_issues:
                report.lines.append(status_line("MISSING", target, scope, "plugin", f"incomplete native plugin {dest}: {'; '.join(payload_issues)}"))
                report.missing += 1
            elif plugin_version(dest) != source_version or registration.get("version") != source_version:
                report.lines.append(
                    status_line(
                        "MISSING",
                        target,
                        scope,
                        "plugin",
                        f"stale native plugin {dest} version {plugin_version(dest) or 'unknown'}; expected {source_version}",
                    )
                )
                report.missing += 1
            else:
                report.lines.append(status_line("CURRENT", target, scope, "plugin", f"native plugin {dest} version {plugin_version(dest) or 'unknown'}; hooks present"))
                report.current += 1
            report.plugins += 1
        activation = zcode_activation_state(project_root)
        if activation != "enabled":
            report.lines.append(status_line("ACTION", target, scope, "plugin", f"activation is {activation}; update plugins.enabledPlugins for {ZCODE_PLUGIN_ID}"))
        if managed_legacy_zcode_active(project_root):
            report.lines.append(
                status_line(
                    "CONFLICT",
                    target,
                    scope,
                    "plugin",
                    f"legacy activation key {LEGACY_ZCODE_PLUGIN_ID} remains enabled",
                )
            )
            report.conflicts += 1
        legacy_plugin = legacy_zcode_plugin_path(project_root)
        if legacy_plugin.exists() or path_is_redirected(legacy_plugin):
            report.lines.append(status_line("FALLBACK", target, scope, "plugin", f"legacy unregistered plugin remains at {legacy_plugin}"))
        legacy_base = target_base(target, scope, project_root)
        if any(
            (legacy_base / skill_name).exists() or path_is_redirected(legacy_base / skill_name)
            for skill_name in RECOGNIZED_SKILL_NAMES
        ):
            report.lines.append(status_line("FALLBACK", target, scope, "skills", f"legacy skill fallback remains under {legacy_base}; native plugin is preferred"))
        return report

    base = target_base(target, scope, project_root)
    report = InstallReport()
    for source_path in lifecycle_skill_sources():
        dest = base / source_path.name
        source = source_path.resolve()
        ensure_source(source)

        if path_is_redirected(dest):
            resolved = dest.resolve(strict=False)
            if resolved == source:
                report.lines.append(status_line("CURRENT", target, scope, source_path.name, f"installed symlink {dest} -> {source}"))
                report.current += 1
            else:
                report.lines.append(status_line("CONFLICT", target, scope, source_path.name, f"symlink {dest} -> {resolved}; expected {source}"))
                report.conflicts += 1
            continue

        if not dest.exists():
            report.lines.append(status_line("MISSING", target, scope, source_path.name, f"missing {dest}"))
            report.missing += 1
            continue

        if dest.is_dir():
            report.lines.append(status_line("CURRENT", target, scope, source_path.name, f"installed copy {dest}"))
            report.current += 1
            continue

        report.lines.append(status_line("CONFLICT", target, scope, source_path.name, f"file {dest}"))
        report.conflicts += 1
    for skill_name in LEGACY_SKILL_NAMES:
        legacy = base / skill_name
        if not (legacy.exists() or path_is_redirected(legacy)):
            continue
        if skill_declares_name(legacy, skill_name):
            report.lines.append(
                status_line(
                    "FALLBACK",
                    target,
                    scope,
                    skill_name,
                    f"managed legacy alias remains and will be backed up by install/update: {legacy}",
                )
            )
            report.missing += 1
        else:
            report.lines.append(
                status_line(
                    "CONFLICT",
                    target,
                    scope,
                    skill_name,
                    f"legacy-named path is not a provable Tenetora-managed skill: {legacy}",
                )
            )
            report.conflicts += 1
    runtime_report = status_runtime_adapter(target, scope, project_root)
    if runtime_report is not None:
        report.extend(runtime_report)
    return report


def install_one(
    target: str,
    scope: str,
    project_root: Path,
    mode: str,
    force: bool,
    dry_run: bool,
    *,
    skip_native: bool = False,
    skip_runtime: bool = False,
) -> InstallReport:
    base = target_base(target, scope, project_root)
    ensure_safe_directory_chain(
        base,
        label=f"{scope} {target} skill target",
        boundary=project_root if scope == "project" else None,
    )
    native_fallback_reason: str | None = None
    native_report = None
    if not skip_native:
        try:
            native_report = tool_installer(target).install_native_surface(
                SURFACE_OPERATIONS,
                target=target,
                scope=scope,
                project_root=project_root,
                mode=mode,
                force=force,
                dry_run=dry_run,
                update=False,
                source_changed=False,
            )
        except NativePluginStateUnavailable as exc:
            native_fallback_reason = str(exc)
    if native_report is not None:
        return native_report
    actual_mode = resolved_mode(mode)
    report = InstallReport()
    for source_path in lifecycle_skill_sources():
        dest = base / source_path.name
        source = source_path.resolve()
        ensure_source(source)

        if path_is_redirected(dest) and dest.resolve() == source:
            report.lines.append(status_line("CURRENT", target, scope, source_path.name, f"already installed {dest} -> {source}"))
            report.current += 1
            continue

        if dest.exists() or path_is_redirected(dest):
            if not force:
                raise FileExistsError(f"{scope}:{target}:{source_path.name}: refusing to overwrite existing {dest}; rerun with --force")
            if not dry_run:
                remove_existing(dest)

        if dry_run:
            report.lines.append(status_line("INSTALLED", target, scope, source_path.name, f"would install {actual_mode} {dest} from {source}"))
            report.installed += 1
            continue

        created = create_install(base, dest, source, actual_mode)
        report.lines.append(status_line("INSTALLED", target, scope, source_path.name, f"installed {created}"))
        report.installed += 1
    clean_legacy_skill_aliases(target, scope, project_root, report, dry_run)
    if scope == "project":
        exclude_status, exclude_detail = ensure_project_skill_compat_git_exclude(
            project_root, base, dry_run
        )
        report.lines.append(
            status_line(exclude_status, target, scope, "skill-git-exclude", exclude_detail)
        )
    runtime_report = None if skip_runtime else install_runtime_adapter(target, scope, project_root, force, dry_run)
    if runtime_report is not None:
        report.extend(runtime_report)
    if target in NATIVE_PLUGIN_TARGETS and scope == "global" and not skip_native:
        message = f"{target} native plugin CLI is unavailable; skills installed but runtime hooks are not active"
        if native_fallback_reason is not None:
            message = (
                f"{target} native plugin state is unreadable: {native_fallback_reason}; skills installed but runtime hooks are not active; "
                f"repair the {target} marketplace configuration and rerun the installer"
            )
        report.lines.append(
            status_line(
                "FALLBACK",
                target,
                scope,
                "skills",
                message,
            )
        )
    return report


def update_one(
    target: str,
    scope: str,
    project_root: Path,
    mode: str,
    force: bool,
    dry_run: bool,
    source_changed: bool,
    *,
    skip_native: bool = False,
    skip_runtime: bool = False,
) -> InstallReport:
    base = target_base(target, scope, project_root)
    ensure_safe_directory_chain(
        base,
        label=f"{scope} {target} skill target",
        boundary=project_root if scope == "project" else None,
    )
    native_fallback_reason: str | None = None
    native_report = None
    if not skip_native:
        try:
            native_report = tool_installer(target).install_native_surface(
                SURFACE_OPERATIONS,
                target=target,
                scope=scope,
                project_root=project_root,
                mode=mode,
                force=force,
                dry_run=dry_run,
                update=True,
                source_changed=source_changed,
            )
        except NativePluginStateUnavailable as exc:
            native_fallback_reason = str(exc)
    if native_report is not None:
        return native_report
    actual_mode = resolved_mode(mode)
    report = InstallReport()
    for source_path in lifecycle_skill_sources():
        dest = base / source_path.name
        source = source_path.resolve()
        skill_mode = actual_mode
        ensure_source(source)

        if mode == "auto" and dest.exists() and not path_is_redirected(dest):
            skill_mode = "copy"

        if path_is_redirected(dest):
            resolved = dest.resolve(strict=False)
            if resolved == source:
                if source_changed:
                    report.lines.append(status_line("UPDATED", target, scope, source_path.name, f"refreshed symlink source {dest} -> {source}"))
                    report.updated += 1
                else:
                    report.lines.append(status_line("CURRENT", target, scope, source_path.name, f"already up to date {dest} -> {source}"))
                    report.current += 1
                continue
            if not force and not is_managed_tenetora_symlink_target(resolved, source_path.name):
                raise FileExistsError(
                    f"{scope}:{target}:{source_path.name}: refusing to replace symlink {dest} -> {resolved}; rerun with --force"
                )
        elif dest.is_dir() and not force and not managed_lifecycle_destination(base, dest):
            raise FileExistsError(
                f"{scope}:{target}:{source_path.name}: refusing to replace non-Agent-Harness directory {dest}; rerun with --force"
            )
        elif dest.exists() and not force and not dest.is_dir():
            raise FileExistsError(f"{scope}:{target}:{source_path.name}: refusing to replace existing file {dest}; rerun with --force")

        if dry_run:
            if dest.exists() or path_is_redirected(dest):
                report.lines.append(status_line("UPDATED", target, scope, source_path.name, f"would update {skill_mode} {dest} from {source}"))
                report.updated += 1
            else:
                report.lines.append(status_line("INSTALLED", target, scope, source_path.name, f"would install {skill_mode} {dest} from {source}"))
                report.installed += 1
            continue

        existed = dest.exists() or path_is_redirected(dest)
        if existed:
            remove_existing(dest)

        created = create_install(base, dest, source, skill_mode)
        if existed:
            report.lines.append(status_line("UPDATED", target, scope, source_path.name, f"updated {created}"))
            report.updated += 1
        else:
            report.lines.append(status_line("INSTALLED", target, scope, source_path.name, f"installed {created}"))
            report.installed += 1
    clean_legacy_skill_aliases(target, scope, project_root, report, dry_run)
    if scope == "project":
        exclude_status, exclude_detail = ensure_project_skill_compat_git_exclude(
            project_root, base, dry_run
        )
        report.lines.append(
            status_line(exclude_status, target, scope, "skill-git-exclude", exclude_detail)
        )
    runtime_report = None if skip_runtime else install_runtime_adapter(target, scope, project_root, force, dry_run)
    if runtime_report is not None:
        report.extend(runtime_report)
    if target in NATIVE_PLUGIN_TARGETS and scope == "global" and not skip_native:
        message = f"{target} native plugin CLI is unavailable; skills updated but runtime hooks are not active"
        if native_fallback_reason is not None:
            message = (
                f"{target} native plugin state is unreadable: {native_fallback_reason}; skills updated but runtime hooks are not active; "
                f"repair the {target} marketplace configuration and rerun the installer"
            )
        report.lines.append(
            status_line(
                "FALLBACK",
                target,
                scope,
                "skills",
                message,
            )
        )
    return report


def command_version(command: str | None) -> str | None:
    if command is None:
        return None
    try:
        result = subprocess.run(
            [command, "--version"],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (result.stdout or result.stderr).strip().splitlines()
    return text[0] if result.returncode == 0 and text else None


def tool_detected(target: str) -> bool:
    definition = tool_definition(target)
    return definition.detected(Path.home(), command_exists)


def writable_without_creating(path: Path) -> bool:
    candidate = path.expanduser()
    while not candidate.exists() and candidate != candidate.parent:
        if path_is_redirected(candidate):
            return False
        candidate = candidate.parent
    return (
        candidate.exists()
        and not path_is_redirected(candidate)
        and candidate.is_dir()
        and os.access(candidate, os.W_OK | os.X_OK)
    )


def install_write_root(target: str, scope: str, project_root: Path) -> Path:
    if target in NATIVE_PLUGIN_TARGETS and scope == "global":
        return target_base(target, scope, project_root).parent
    if tool_definition(target).installer == "zcode-native" and scope == "global":
        return zcode_home(scope, project_root)
    if target in RUNTIME_ADAPTER_TARGETS:
        return runtime_home(target, scope, project_root)
    return target_base(target, scope, project_root)


def installed_skill_version(target: str, scope: str, project_root: Path) -> str | None:
    base = target_base(target, scope, project_root)
    for skill_name in (ROUTER_SKILL_NAME, LEGACY_ROUTER_SKILL_NAME):
        try:
            value = (base / skill_name / "VERSION").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return None


def lifecycle_surface_present(target: str, scope: str, project_root: Path) -> bool:
    base = target_base(target, scope, project_root)
    router_versions = [
        base / skill_name / "VERSION"
        for skill_name in (ROUTER_SKILL_NAME, LEGACY_ROUTER_SKILL_NAME)
    ]
    if any(path.is_file() and not path_is_redirected(path) for path in router_versions):
        return True
    return any(
        legacy_skill_is_managed(base / skill_name)
        for skill_name in RECOGNIZED_SKILL_NAMES
    )


def named_lifecycle_surface_present(target: str, scope: str, project_root: Path) -> bool:
    base = target_base(target, scope, project_root)
    return any(
        (base / skill_name).exists() or path_is_redirected(base / skill_name)
        for skill_name in RECOGNIZED_SKILL_NAMES
    )


def native_plugin_cache_present(target: str, project_root: Path) -> bool:
    if target not in NATIVE_PLUGIN_TARGETS:
        return False
    config_root = target_base(target, "global", project_root).parent
    cache_roots = (
        config_root / "plugins" / "cache" / NATIVE_MARKETPLACE_ID / PLUGIN_REGISTRATION_NAME,
        config_root
        / "plugins"
        / "cache"
        / LEGACY_NATIVE_MARKETPLACE_ID
        / LEGACY_PLUGIN_REGISTRATION_NAME,
    )
    for cache_root in cache_roots:
        if cache_root.is_dir():
            try:
                if any(child.is_dir() for child in cache_root.iterdir()):
                    return True
            except OSError:
                return True
        elif cache_root.exists() or path_is_redirected(cache_root):
            return True
    return False


def native_plugin_surface_present(target: str, project_root: Path) -> bool:
    if target not in NATIVE_PLUGIN_TARGETS:
        return False
    if native_plugin_cache_present(target, project_root):
        return True
    if not native_plugins_enabled():
        return False
    try:
        return (
            native_plugin_entry(target, project_root) is not None
            or legacy_native_plugin_entry(target, project_root) is not None
        )
    except (NativePluginStateUnavailable, FileNotFoundError, OSError, subprocess.SubprocessError):
        return bool(target == "codex" and assess_legacy_registration().recoverable)


def zcode_plugin_surface_present(project_root: Path) -> bool:
    legacy_plugins = (legacy_zcode_plugin_path(project_root), legacy_project_zcode_plugin_path(project_root))
    if any(path.exists() or path_is_redirected(path) for path in legacy_plugins):
        return True
    try:
        if zcode_registration(project_root) is not None or legacy_zcode_registration(project_root) is not None:
            return True
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    cache_root = zcode_plugin_base("global", project_root)
    if cache_root.is_dir():
        try:
            return any(child.is_dir() for child in cache_root.iterdir())
        except OSError:
            return True
    if cache_root.exists() or path_is_redirected(cache_root):
        return True
    legacy_cache_root = legacy_zcode_plugin_base("global", project_root)
    if legacy_cache_root.is_dir():
        try:
            return any(child.is_dir() for child in legacy_cache_root.iterdir())
        except OSError:
            return True
    return legacy_cache_root.exists() or path_is_redirected(legacy_cache_root)


def cursor_runtime_surface_present(scope: str, project_root: Path) -> bool:
    path = runtime_home("cursor", scope, project_root) / "hooks.json"
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    hooks = payload.get("hooks") if isinstance(payload, dict) else None
    if not isinstance(hooks, dict):
        return False
    return any(
        cursor_hook_is_managed(entry)
        for entries in hooks.values()
        if isinstance(entries, list)
        for entry in entries
    )


def opencode_runtime_surface_present(scope: str, project_root: Path) -> bool:
    base = runtime_home("opencode", scope, project_root) / "plugins"
    for name in ("tenetora.js", "agent-harness.js"):
        path = base / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
            if OPENCODE_RUNTIME_MARKER in text or LEGACY_OPENCODE_RUNTIME_MARKER in text:
                return True
        except OSError:
            continue
    return False


def pi_runtime_surface_present(scope: str, project_root: Path) -> bool:
    path = runtime_home("pi", scope, project_root) / "extensions" / "tenetora.ts"
    if not path.is_file():
        return False
    try:
        return PI_RUNTIME_MARKER in path.read_text(encoding="utf-8")
    except OSError:
        return False


def existing_surface_present(target: str, scope: str, project_root: Path) -> bool:
    if scope == "project" and is_reserved_project_root(project_root):
        return False
    if lifecycle_surface_present(target, scope, project_root):
        return True
    if scope == "global" and native_plugin_surface_present(target, project_root):
        return True
    if target == "zcode" and scope == "global" and zcode_plugin_surface_present(project_root):
        return True
    if target == "codex" and scope == "project" and codex_project_fallback_present(project_root):
        return True
    if target == "cursor":
        return cursor_runtime_surface_present(scope, project_root)
    if target == "opencode":
        return opencode_runtime_surface_present(scope, project_root)
    if target == "pi":
        return pi_runtime_surface_present(scope, project_root)
    return False


def maximum_capability(target: str) -> str:
    return str(platform_contract(target)["maximum_capability"])


def codex_hook_trust(project_root: Path) -> dict[str, object]:
    if PACKAGE_ROOT is None:
        return {"state": "unavailable", "detail": "package root unavailable"}
    module_path = PACKAGE_ROOT / "cli" / "tenetora" / "codex_hooks.py"
    if not module_path.is_file():
        return {"state": "unavailable", "detail": f"missing {module_path}"}
    module_name = "tenetora_install_codex_hooks"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        return {"state": "unavailable", "detail": "cannot load Codex hook inspector"}
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        command = native_plugin_command("codex")
        if command is None:
            return {"state": "unavailable", "detail": "Codex CLI unavailable"}
        return module.inspect_codex_plugin_hooks(
            command,
            project_root,
            native_plugin_environment("codex", project_root),
            NATIVE_PLUGIN_ID,
            client_version=package_version(PACKAGE_ROOT) or "unknown",
        )
    except Exception as exc:
        return {"state": "unavailable", "detail": str(exc)}


def codex_hook_inventory(project_root: Path) -> dict[str, object]:
    if PACKAGE_ROOT is None:
        return {"state": "unavailable", "hooks": [], "detail": "package root unavailable"}
    module_path = PACKAGE_ROOT / "cli" / "tenetora" / "codex_hooks.py"
    if not module_path.is_file():
        return {"state": "unavailable", "hooks": [], "detail": f"missing {module_path}"}
    module_name = "tenetora_install_codex_hook_inventory"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        return {"state": "unavailable", "hooks": [], "detail": "cannot load Codex hook inventory"}
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        command = native_plugin_command("codex")
        if command is None:
            return {"state": "unavailable", "hooks": [], "detail": "Codex CLI unavailable"}
        return module.list_codex_hooks(
            command,
            project_root,
            native_plugin_environment("codex", project_root),
            client_version=package_version(PACKAGE_ROOT) or "unknown",
        )
    except Exception as exc:
        return {"state": "unavailable", "hooks": [], "detail": str(exc)}


def codex_tenetora_hook_sets(inventory: dict[str, object]) -> dict[str, list[dict[str, object]]]:
    if PACKAGE_ROOT is None:
        return {"native": [], "legacy_native": [], "project": []}
    module_path = PACKAGE_ROOT / "cli" / "tenetora" / "codex_hooks.py"
    module_name = "tenetora_install_codex_hook_sets"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        return {"native": [], "legacy_native": [], "project": []}
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        canonical = module.tenetora_hook_sets(inventory, NATIVE_PLUGIN_ID)
        legacy = module.tenetora_hook_sets(inventory, LEGACY_NATIVE_PLUGIN_ID)
        return {
            "native": canonical["native"],
            "legacy_native": legacy["native"],
            "project": canonical["project"],
        }
    except Exception:
        return {"native": [], "legacy_native": [], "project": []}


def codex_project_hooks_module() -> object:
    if PACKAGE_ROOT is None:
        raise RuntimeError("Tenetora package root is unavailable")
    module_path = PACKAGE_ROOT / "cli" / "tenetora" / "codex_project_hooks.py"
    if not module_path.is_file():
        raise RuntimeError(f"Codex project hook manager is missing: {module_path}")
    module_name = "tenetora_install_codex_project_hooks"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Codex project hook manager")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def codex_project_fallback_plan(
    project_root: Path,
    *,
    allow_tracked: bool,
) -> object:
    if PACKAGE_ROOT is None:
        raise RuntimeError("Tenetora package root is unavailable")
    module = codex_project_hooks_module()
    return module.install_project_fallback(
        project_root,
        PACKAGE_ROOT / "hooks" / "hooks.json",
        allow_tracked=allow_tracked,
        dry_run=True,
    )


def ensure_codex_project_hook_runtime() -> Path:
    if PACKAGE_ROOT is None:
        raise RuntimeError("Tenetora package root is unavailable")
    module_path = PACKAGE_ROOT / "skills" / ROUTER_SKILL_NAME / "scripts" / "ensure_cli.py"
    module_name = "tenetora_install_ensure_cli"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Tenetora CLI bootstrap")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    bin_dir = module.default_bin_dir()
    module.write_runtime_python(bin_dir)
    destination = module.write_hook_runtime(bin_dir)
    if (
        destination is None
        or not module.hook_runtime_ready(bin_dir)
        or hook_runner_directory_state(destination) != "present"
    ):
        raise RuntimeError("Tenetora stable hook runtime failed its empty-PATH check")
    return destination


def apply_codex_project_hook_policy(
    project_root: Path,
    *,
    policy: str,
    allow_tracked: bool,
    dry_run: bool,
) -> InstallReport:
    module = codex_project_hooks_module()
    report = InstallReport()
    if policy == "project":
        if not dry_run:
            ensure_codex_project_hook_runtime()
        result = module.install_project_fallback(
            project_root,
            PACKAGE_ROOT / "hooks" / "hooks.json",
            allow_tracked=allow_tracked,
            dry_run=dry_run,
        )
        if result.state in {"conflict", "tracked-config", "ownership-conflict"}:
            raise RuntimeError(result.detail)
        status = "INSTALLED" if result.state in {"installed", "planned"} else "CURRENT"
        report.lines.append(status_line(status, "codex", "project", "hooks", result.detail))
        report.runtimes += 1
        return report
    if policy == "off":
        result = module.remove_project_fallback(project_root, dry_run=dry_run)
        if result.state == "ownership-conflict":
            raise RuntimeError(result.detail)
        status = "UPDATED" if result.state in {"removed", "planned"} else "CURRENT"
        report.lines.append(status_line(status, "codex", "project", "hooks", result.detail))
        return report
    return report


def codex_project_fallback_present(project_root: Path) -> bool:
    try:
        module = codex_project_hooks_module()
        return bool(module.project_fallback_owned_and_current(project_root))
    except (OSError, ValueError, RuntimeError, AttributeError):
        return False


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
    environment = {name: os.environ[name] for name in preserved if os.environ.get(name)}
    environment["PATH"] = ""
    return environment


def hook_runner_directory_state(hooks_dir: Path) -> str:
    unix_runner = hooks_dir / "run-hook"
    windows_runner = hooks_dir / "run-hook.cmd"
    if not unix_runner.is_file():
        return "missing-runner"
    if os.name != "nt" and not os.access(unix_runner, os.X_OK):
        return "runner-not-executable"
    if not windows_runner.is_file():
        return "missing-windows-runner"
    if os.name == "nt":
        command_processor = os.environ.get("ComSpec") or str(
            Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "cmd.exe"
        )
        command = [command_processor, "/d", "/s", "/c", subprocess.list2cmdline([str(windows_runner), "--check"])]
    else:
        command = [str(unix_runner), "--check"]
    try:
        result = subprocess.run(
            command,
            cwd=hooks_dir.parent,
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
    if result.returncode != 0 or not result.stdout.startswith("tenetora-hook-runtime "):
        return "runner-check-failed"
    return "present"


def native_hook_runtime_state(hooks: list[dict[str, object]]) -> str:
    enabled = [hook for hook in hooks if hook.get("enabled", True)]
    if not enabled:
        return "missing"
    roots: set[Path] = set()
    for hook in enabled:
        source_path = hook.get("sourcePath")
        if not isinstance(source_path, str) or not source_path:
            return "unknown-source"
        path = Path(source_path).expanduser()
        hooks_dir = path.parent
        if hooks_dir.name != "hooks":
            return "unknown-source"
        roots.add(hooks_dir)
    states = {hook_runner_directory_state(root) for root in roots}
    return "present" if states == {"present"} else sorted(states)[0]


def codex_native_plugin_config_state(project_root: Path) -> str:
    """Conservatively inspect whether Codex can load the Tenetora native plugin.

    Codex plugin CLI inspection can fail when an unrelated marketplace snapshot is
    malformed. In that case the config file is the only stable local source that
    can prove the Tenetora plugin is absent or explicitly disabled. Unknown TOML
    shapes intentionally fail closed.
    """
    config_path = target_base("codex", "global", project_root).parent / "config.toml"
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "not-configured"
    except OSError:
        return "unknown"

    plugin_ids = {NATIVE_PLUGIN_ID, LEGACY_NATIVE_PLUGIN_ID}
    in_plugin_section = False
    found_plugin_section = False
    saw_plugin_id = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped and not stripped.startswith("#") and any(plugin_id in stripped for plugin_id in plugin_ids):
            saw_plugin_id = True
        if stripped.startswith("["):
            match = CODEX_PLUGIN_SECTION_RE.match(raw_line)
            section_name = None
            if match is not None:
                section_name = match.group("double") or match.group("single") or match.group("bare")
            in_plugin_section = section_name in plugin_ids
            found_plugin_section = found_plugin_section or in_plugin_section
            continue
        if not in_plugin_section:
            continue
        enabled = CODEX_PLUGIN_ENABLED_RE.match(raw_line)
        if enabled is not None:
            return "enabled" if enabled.group(1).lower() == "true" else "disabled"

    if found_plugin_section or saw_plugin_id:
        return "unknown"
    return "not-configured"


def codex_config_has_managed_project_hook_receipt(project_root: Path) -> bool:
    """Return true only for the recognizable trust receipt of our old fallback."""
    config_path = target_base("codex", "global", project_root).parent / "config.toml"
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return False
    hooks_path = str((project_root / ".codex" / "hooks.json").resolve())
    managed_events = (
        "pre_tool_use",
        "session_start",
        "user_prompt_submit",
        "subagent_start",
        "subagent_stop",
        "stop",
    )
    matches = sum(f'{hooks_path}:{event}:' in text for event in managed_events)
    return matches == len(managed_events)


def codex_marketplace_unreadable(
    check: CapabilityCheck,
    project_root: Path,
) -> tuple[dict[str, object] | None, bool]:
    config_root = target_base("codex", "global", project_root).parent
    if not config_root.exists():
        check.plugin_api = "supported-new-config"
        check.marketplace = "empty"
        return None, False
    check.plugin_api = "supported"
    try:
        entry = native_plugin_entry("codex", project_root)
    except NativePluginStateUnavailable:
        check.marketplace = "unreadable"
        check.degraded_reason = "codex-marketplace-unreadable"
        return None, True
    check.marketplace = "healthy"
    return entry, False


def codex_preflight(
    scope: str,
    project_root: Path,
    check: CapabilityCheck,
    codex_hooks: str,
    allow_tracked_codex_hooks: bool,
    prune_shadowed: bool,
) -> bool:
    if scope != "global" and codex_hooks != "project":
        return False
    configured_home = os.environ.get("TENETORA_HOME")
    default_home = default_machine_home().resolve(strict=False)
    nondefault_home = bool(
        configured_home
        and Path(configured_home).expanduser().resolve() != default_home
    )
    if codex_hooks == "project" and nondefault_home:
        check.hook_mode = "project-fallback"
        check.planned = CAPABILITY_BLOCKED
        check.hooks_state = "stable-runtime-home-unsupported"
        check.blocker = "Codex project fallback requires the default ~/.tenetora stable runtime home"
        check.remediation = "Use the default Tenetora home, then rerun --codex-hooks project"
        return True
    if not native_plugins_enabled() and codex_hooks not in {"project", "off"}:
        return False
    command = native_plugin_command("codex")
    if command is None:
        if codex_hooks == "project":
            check.hook_mode = "project-fallback"
            check.planned = CAPABILITY_BLOCKED
            check.hooks_state = "hook-inventory-unavailable"
            check.blocker = "Codex CLI is unavailable; cannot inspect native hooks before enabling project fallback"
            check.remediation = "Install or restore Codex, then rerun --codex-hooks project"
            return True
        return False
    check.cli_path = shutil.which(command)
    check.cli_version = command_version(command)
    plugin_supported = native_plugin_cli_supported_readonly("codex", project_root)
    if not plugin_supported and codex_hooks != "project":
        return False
    entry, marketplace_unreadable = codex_marketplace_unreadable(check, project_root)
    if entry is not None and entry.get("version"):
        check.existing_version = str(entry["version"])
    recovery = assess_legacy_registration() if marketplace_unreadable and scope == "global" else None
    if recovery is not None and recovery.recoverable and codex_hooks in {"auto", "native"}:
        check.hook_mode = "native-plugin"
        check.hooks_state = "legacy-registration-recoverable"
        check.planned = CAPABILITY_PENDING_TRUST
        check.degraded_reason = "codex-legacy-registration-recoverable"
        check.blocker = None
        check.remediation = (
            "Tenetora will back up Codex config, recover the proven legacy registration, install the canonical plugin, "
            "and require a fresh /hooks trust review"
        )
        return True
    if recovery is not None and recovery.status == "blocked" and codex_hooks == "native":
        check.hook_mode = "native-plugin"
        check.hooks_state = "legacy-registration-ownership-blocked"
        check.planned = CAPABILITY_BLOCKED
        check.blocker = recovery.reason or "Legacy Codex registration ownership could not be proven"
        check.remediation = "Run tenetora repair --fix codex-legacy-registration --check and review the ownership evidence"
        return True

    inventory = codex_hook_inventory(project_root)
    inventory_available = (
        inventory.get("state") == "available"
        and not inventory.get("errors")
    )
    inventory_reported_errors = bool(
        inventory.get("state") == "available"
        and inventory.get("errors")
    )
    hook_sets = codex_tenetora_hook_sets(inventory)
    enabled_native = [hook for hook in hook_sets["native"] if hook.get("enabled", True)]
    enabled_legacy_native = [
        hook for hook in hook_sets["legacy_native"] if hook.get("enabled", True)
    ]
    enabled_project = [hook for hook in hook_sets["project"] if hook.get("enabled", True)]
    native_runtime = native_hook_runtime_state(enabled_native)
    native_trusted = bool(enabled_native) and all(
        str(hook.get("trustStatus", "")).lower() in {"trusted", "managed"}
        for hook in enabled_native
    )

    managed_project_fallback = codex_project_fallback_present(project_root)
    project_fallback_configured = bool(enabled_project) or managed_project_fallback
    native_config_state = codex_native_plugin_config_state(project_root)
    managed_project_fallback_receipt = codex_config_has_managed_project_hook_receipt(project_root)
    if (enabled_native and enabled_project) or (
        enabled_legacy_native and (enabled_native or enabled_project)
    ):
        check.hook_mode = "conflict"
        check.planned = CAPABILITY_BLOCKED
        check.hooks_state = "duplicate-hook-runtime"
        check.blocker = "Codex enables both the Tenetora and legacy Agent Harness native hook sources"
        check.remediation = (
            "Disable the legacy Agent Harness hooks in Codex /hooks, then rerun with --codex-hooks auto"
        )
        return True
    if enabled_legacy_native:
        check.hook_mode = "native-plugin"
        check.planned = CAPABILITY_BLOCKED
        check.hooks_state = "legacy-native-hook-active"
        check.blocker = "Codex still enables the legacy Agent Harness native hook source; it cannot satisfy the Tenetora runtime contract"
        check.remediation = (
            "Run tenetora repair --fix codex-legacy-registration --check and apply only proven ownership recovery, "
            "or disable the legacy hooks in Codex /hooks before installing the canonical plugin"
        )
        return True
    safe_project_fallback = bool(
        marketplace_unreadable
        and codex_hooks in {"auto", "project"}
        and not enabled_native
        and not inventory_reported_errors
        and native_config_state in {"not-configured", "disabled"}
        and not nondefault_home
        and (
            codex_hooks == "project"
            or managed_project_fallback
            or managed_project_fallback_receipt
        )
    )
    if safe_project_fallback:
        try:
            plan = codex_project_fallback_plan(
                project_root,
                allow_tracked=allow_tracked_codex_hooks,
            )
        except RuntimeError as exc:
            check.hook_mode = "project-fallback"
            check.planned = CAPABILITY_BLOCKED
            check.hooks_state = "project-fallback-unavailable"
            check.blocker = str(exc)
            return True
        if plan.state in {"conflict", "tracked-config", "ownership-conflict"}:
            check.hook_mode = "project-fallback"
            check.planned = CAPABILITY_BLOCKED
            check.hooks_state = "project-fallback-conflict"
            check.blocker = plan.detail
            check.remediation = (
                "Rerun with --allow-tracked-codex-hooks after reviewing the tracked file"
                if plan.state == "tracked-config"
                else "Repair .codex/hooks.json, then rerun"
            )
            return True
        project_trusted = bool(enabled_project) and all(
            str(hook.get("trustStatus", "")).lower() in {"trusted", "managed"}
            for hook in enabled_project
        )
        check.hook_mode = "project-fallback"
        check.planned = CAPABILITY_ACTIVE if project_trusted else CAPABILITY_PENDING_TRUST
        check.hooks_state = "active-project-fallback" if project_trusted else "pending-project-trust"
        check.remediation = (
            "Repair Codex marketplace configuration; the Tenetora project fallback remains active"
            if project_trusted
            else "Restart Codex and review the restored Tenetora project hooks in /hooks"
        )
        return True
    if codex_hooks in {"auto", "native"} and project_fallback_configured:
        check.hook_mode = "native-plugin"
        if not prune_shadowed:
            check.planned = CAPABILITY_BLOCKED
            check.hooks_state = "duplicate-hook-runtime"
            check.blocker = "installing or updating Codex native hooks would leave the project fallback enabled"
            check.remediation = "Rerun with --prune-shadowed after reviewing the Tenetora managed project fallback"
            return True
        if not managed_project_fallback:
            check.planned = CAPABILITY_BLOCKED
            check.hooks_state = "project-fallback-ownership-conflict"
            check.blocker = "Codex project fallback is enabled but Tenetora cannot prove its ownership and current content"
            check.remediation = "Review .codex/hooks.json and keep exactly one Codex runtime; user hooks will not be removed automatically"
            return True
        check.runtime_role = "native-effective-prune-project"

    if codex_hooks == "project" and not inventory_available:
        check.hook_mode = "project-fallback"
        check.planned = CAPABILITY_BLOCKED
        check.hooks_state = "hook-inventory-unavailable"
        check.blocker = "Codex hooks/list inventory is unavailable; cannot prove that native hooks are disabled"
        check.remediation = "Restore Codex app-server hooks/list, then rerun --codex-hooks project"
        return True

    if enabled_native:
        check.hook_mode = "native-plugin"
        if codex_hooks == "project":
            check.planned = CAPABILITY_BLOCKED
            check.hooks_state = "native-hook-blocks-fallback"
            check.blocker = "--codex-hooks project requests a runtime mode switch, but enabled Tenetora native hooks are still active"
            check.remediation = (
                "For a normal upgrade, rerun with --codex-hooks auto or --codex-hooks native. "
                "To switch to project fallback, disable the existing Tenetora native hooks in Codex /hooks, "
                "then rerun --codex-hooks project."
            )
            return True
        if native_runtime != "present":
            check.hooks_state = "native-hook-blocks-fallback"
            if marketplace_unreadable:
                check.planned = CAPABILITY_BLOCKED
                check.blocker = f"enabled Tenetora native hooks have an unhealthy launcher: {native_runtime}"
                check.remediation = "Disable the existing Tenetora native hooks in Codex /hooks before using project fallback"
                return True
        elif native_trusted:
            check.planned = CAPABILITY_ACTIVE
            check.hooks_state = (
                "active-native-degraded-management"
                if marketplace_unreadable
                else "active-native"
            )
            check.remediation = (
                "Repair Codex marketplace configuration before installing, upgrading, or removing the native plugin"
                if marketplace_unreadable
                else None
            )
            return True
        else:
            check.planned = CAPABILITY_PENDING_TRUST
            check.hooks_state = "pending-native-trust"
            check.remediation = "Review and trust the current Tenetora hooks in Codex /hooks"
            return True

    if codex_hooks == "project":
        try:
            plan = codex_project_fallback_plan(
                project_root,
                allow_tracked=allow_tracked_codex_hooks,
            )
        except RuntimeError as exc:
            check.planned = CAPABILITY_BLOCKED
            check.hooks_state = "project-fallback-unavailable"
            check.blocker = str(exc)
            return True
        if plan.state in {"conflict", "tracked-config", "ownership-conflict"}:
            check.planned = CAPABILITY_BLOCKED
            check.hooks_state = "project-fallback-conflict"
            check.blocker = plan.detail
            check.remediation = (
                "Rerun with --allow-tracked-codex-hooks after reviewing the tracked file"
                if plan.state == "tracked-config"
                else "Repair .codex/hooks.json, then rerun"
            )
            return True
        check.hook_mode = "project-fallback"
        check.planned = CAPABILITY_PENDING_TRUST
        check.hooks_state = "active-project-fallback" if enabled_project else "pending-project-trust"
        check.remediation = "Restart Codex and review the Tenetora project hooks in /hooks"
        return True
    if codex_hooks == "off":
        check.hook_mode = "skills-only"
        check.hooks_state = "disabled-by-policy"
        check.planned = CAPABILITY_SKILLS_ONLY
        check.blocker = "Codex hooks are disabled by --codex-hooks off"
        return True
    if codex_hooks == "native" and marketplace_unreadable:
        check.hook_mode = "native-plugin"
        check.hooks_state = "native-management-unavailable"
        check.planned = CAPABILITY_BLOCKED
        check.blocker = "Codex marketplace state is unreadable; native plugin installation cannot continue"
        check.remediation = "Repair Codex marketplace configuration, then rerun with --codex-hooks native"
        return True
    if marketplace_unreadable and not inventory_available:
        check.hook_mode = "skills-only"
        check.hooks_state = "hook-inventory-unavailable"
        check.planned = CAPABILITY_SKILLS_ONLY
        check.blocker = "Codex hooks/list inventory is unavailable; project fallback eligibility cannot be verified"
        check.remediation = "Restore Codex app-server hooks/list before enabling project fallback"
        return True
    if marketplace_unreadable and nondefault_home:
        check.hook_mode = "skills-only"
        check.hooks_state = "stable-runtime-home-unsupported"
        check.planned = CAPABILITY_SKILLS_ONLY
        check.blocker = "Codex project fallback requires the default ~/.tenetora stable runtime home"
        check.remediation = "Reinstall Tenetora with the default home before enabling project fallback"
        return True
    if marketplace_unreadable:
        check.hook_mode = "skills-only"
        check.hooks_state = "project-fallback-available"
        check.planned = CAPABILITY_SKILLS_ONLY
        check.blocker = "Codex marketplace state is unreadable and no enabled Tenetora native hooks were found"
        check.remediation = "tenetora install --path . --tools codex --codex-hooks project"
        return True
    return False


def native_preflight(
    target: str,
    scope: str,
    project_root: Path,
    check: CapabilityCheck,
    codex_hooks: str = "auto",
    allow_tracked_codex_hooks: bool = False,
    prune_shadowed: bool = False,
) -> None:
    if target == "codex" and codex_preflight(
        scope,
        project_root,
        check,
        codex_hooks,
        allow_tracked_codex_hooks,
        prune_shadowed,
    ):
        return
    if scope != "global":
        check.plugin_api = "scope-unsupported"
        check.marketplace = "not-applicable"
        check.planned = CAPABILITY_SKILLS_ONLY
        check.blocker = f"{target} native plugins are user-scoped; project scope installs lifecycle skills only"
        check.remediation = f"Use --global for {target} native plugin and runtime hooks"
        return
    if not native_plugins_enabled():
        check.plugin_api = "disabled"
        check.planned = CAPABILITY_SKILLS_ONLY
        check.blocker = "native plugin installation is disabled by TENETORA_NATIVE_PLUGINS=0"
        check.remediation = "Unset TENETORA_NATIVE_PLUGINS or explicitly allow skills-only"
        return
    command = native_plugin_command(target)
    if command is None:
        check.plugin_api = "unavailable"
        check.planned = CAPABILITY_SKILLS_ONLY
        check.degraded_reason = "native-cli-unavailable"
        check.blocker = (
            f"{target} CLI is unavailable; lifecycle skills can be installed, "
            "but native runtime hooks remain inactive"
        )
        check.remediation = f"Install {target}, then rerun tenetora upgrade --force to activate native runtime hooks"
        return
    check.cli_path = shutil.which(command)
    check.cli_version = command_version(command)
    if not native_plugin_cli_supported_readonly(target, project_root):
        check.plugin_api = "unsupported"
        check.planned = CAPABILITY_SKILLS_ONLY
        check.blocker = f"{target} CLI does not expose a usable plugin command"
        check.remediation = f"Upgrade {target}, then rerun; or use --allow-skills-only"
        return
    config_root = target_base(target, "global", project_root).parent
    entry: dict[str, object] | None = None
    if not config_root.exists():
        check.plugin_api = "supported-new-config"
        check.marketplace = "empty"
    else:
        check.plugin_api = "supported"
        try:
            entry = native_plugin_entry(target, project_root)
        except NativePluginStateUnavailable as exc:
            check.marketplace = "unreadable"
            check.planned = CAPABILITY_SKILLS_ONLY
            check.blocker = f"{target} marketplace state is unreadable: {exc}"
            check.remediation = f"Repair {target} marketplace configuration, then rerun; or use --allow-skills-only"
            return
        check.marketplace = "healthy"
    if entry is not None and entry.get("version"):
        check.existing_version = str(entry["version"])
    if PACKAGE_ROOT is None:
        check.planned = CAPABILITY_BLOCKED
        check.blocker = "Tenetora package root is unavailable"
        check.remediation = "Use the release ZIP installer or pass --package-root"
        return
    source_issues = native_plugin_payload_issues(target, PACKAGE_ROOT, source=True)
    if source_issues:
        check.planned = CAPABILITY_BLOCKED
        check.blocker = f"source {target} plugin payload is incomplete: {'; '.join(source_issues)}"
        check.remediation = "Use an intact Tenetora release package"
        return
    if target == "codex":
        source_version = package_version(PACKAGE_ROOT)
        trust = codex_hook_trust(project_root) if check.existing_version == source_version else {"state": "pending-trust"}
        check.hook_mode = "native-plugin"
        check.hooks_state = "active-native" if trust.get("state") == "active" else "pending-native-trust"
        check.planned = CAPABILITY_ACTIVE if trust.get("state") == "active" else CAPABILITY_PENDING_TRUST
        check.remediation = None if check.planned == CAPABILITY_ACTIVE else "Restart Codex and trust the current Tenetora hooks in /hooks"
    else:
        check.planned = CAPABILITY_ACTIVE


def build_preflight(
    targets: list[str],
    scopes: list[str],
    project_root: Path,
    codex_hooks: str = "auto",
    allow_tracked_codex_hooks: bool = False,
    prune_shadowed: bool = False,
    target_scopes: dict[str, list[str]] | None = None,
    runtime_owners: dict[str, str] | None = None,
) -> CapabilityReport:
    package_version_value = package_version(PACKAGE_ROOT) if PACKAGE_ROOT is not None else None
    checks: list[CapabilityCheck] = []
    runtime_plans = {
        target: runtime_scope_plan(
            target,
            (target_scopes or {}).get(target, scopes),
            project_root,
            prune_shadowed=prune_shadowed,
            owner_override=(runtime_owners or {}).get(target),
        )
        for target in targets
        if target in RUNTIME_ADAPTER_TARGETS
    }
    for target in targets:
        for scope in (target_scopes or {}).get(target, scopes):
            command = native_plugin_command(target) if target in NATIVE_PLUGIN_TARGETS else (
                target if target != "agents" and command_exists(target) else None
            )
            check = CapabilityCheck(
                tool=target,
                scope=scope,
                detected=tool_detected(target),
                cli_path=shutil.which(command) if command else None,
                cli_version=command_version(command),
                plugin_api="not-applicable",
                marketplace="not-applicable",
                writable=writable_without_creating(install_write_root(target, scope, project_root)),
                existing_version=installed_skill_version(target, scope, project_root),
                maximum=maximum_capability(target),
                planned=maximum_capability(target),
                expected_version=package_version_value,
            )
            runtime_plan = runtime_plans.get(target)
            if runtime_plan and runtime_plan.get("effective_scope") in {"global", "project"}:
                check.runtime_owner_scope = str(runtime_plan["effective_scope"])
            if runtime_plan and not runtime_plan.get("conflict") and scope == runtime_plan.get("effective_scope"):
                check.runtime_prune_scopes = [
                    str(item)
                    for item in runtime_plan.get("prune_scopes", [])
                    if str(item) != scope
                ]
            if runtime_plan and runtime_plan.get("conflict"):
                check.planned = CAPABILITY_BLOCKED
                check.blocker = str(runtime_plan["conflict"])
                check.remediation = str(runtime_plan.get("remediation") or (
                    "Rerun with --prune-shadowed only when both adapters are unchanged Tenetora managed files, "
                    "or remove one managed adapter manually after review"
                ))
            elif runtime_plan and scope != runtime_plan.get("effective_scope"):
                check.planned = CAPABILITY_SKILLS_ONLY
                check.runtime_role = "skills-only-shadow"
                check.hook_mode = "skills-only-shadow"
                check.hooks_state = f"shadowed-by-{runtime_plan.get('effective_scope')}-runtime"
                check.remediation = f"Runtime is intentionally owned by {runtime_plan.get('effective_scope')} scope."
            elif not check.writable:
                check.planned = CAPABILITY_BLOCKED
                check.blocker = f"install target is not writable: {install_write_root(target, scope, project_root)}"
                check.remediation = "Choose a writable scope/path or fix directory permissions"
            elif target in NATIVE_PLUGIN_TARGETS:
                native_preflight(
                    target,
                    scope,
                    project_root,
                    check,
                    codex_hooks,
                    allow_tracked_codex_hooks,
                    prune_shadowed,
                )
            elif target == "zcode" and scope == "project":
                check.plugin_api = "scope-unsupported"
                check.planned = CAPABILITY_SKILLS_ONLY
                check.blocker = "ZCode native plugins are user-scoped; project scope installs lifecycle skills only"
                check.remediation = "Use --global for the ZCode native plugin and runtime hooks"
            elif target == "zcode":
                if PACKAGE_ROOT is None:
                    check.planned = CAPABILITY_BLOCKED
                    check.blocker = "Tenetora package root is unavailable"
                else:
                    source_issues = plugin_payload_issues(PACKAGE_ROOT, source=True)
                    if source_issues:
                        check.planned = CAPABILITY_BLOCKED
                        check.blocker = f"source ZCode plugin payload is incomplete: {'; '.join(source_issues)}"
                        check.remediation = "Use an intact Tenetora release package"
                    registration = zcode_registration(project_root)
                    if registration and registration.get("version"):
                        check.existing_version = str(registration["version"])
            checks.append(check)
    return CapabilityReport(
        phase="preflight",
        package_version=package_version_value or "unknown",
        python_version=".".join(str(part) for part in sys.version_info[:3]),
        project_path=str(project_root),
        checks=checks,
        tool_discovery=tool_discovery(targets),
    )


def lifecycle_skills_installed(target: str, scope: str, project_root: Path) -> bool:
    base = target_base(target, scope, project_root)
    return all((base / source.name / "SKILL.md").is_file() for source in lifecycle_skill_sources())


def installed_surface_version(
    check: CapabilityCheck,
    project_root: Path,
) -> tuple[str | None, str, bool | None, list[str]]:
    expected = check.expected_version
    target = check.tool
    scope = check.scope

    if (
        target in NATIVE_PLUGIN_TARGETS
        and scope == "global"
        and check.actual in {CAPABILITY_ACTIVE, CAPABILITY_PENDING_TRUST}
        and check.marketplace != "unreadable"
        and not (target == "codex" and check.hook_mode == "project-fallback")
    ):
        try:
            entry = native_plugin_entry(target, project_root)
        except NativePluginStateUnavailable:
            entry = None
        registered_version = str(entry.get("version")) if isinstance(entry, dict) and entry.get("version") else None
        install_path = native_plugin_install_path(target, entry, project_root) if entry else None
        payload_version = plugin_version(install_path) if install_path and install_path.exists() else None
        evidence = [
            item
            for item in (
                f"registered={registered_version}" if registered_version else None,
                f"payload={payload_version}" if payload_version else None,
            )
            if item is not None
        ]
        versions = [value for value in (registered_version, payload_version) if value]
        matches = bool(versions) and expected is not None and all(value == expected for value in versions)
        return registered_version or payload_version, "registered-native-plugin", matches, evidence

    if target == "zcode" and scope == "global" and check.actual == CAPABILITY_ACTIVE:
        registration = zcode_registration(project_root)
        registered_version = (
            str(registration.get("version"))
            if isinstance(registration, dict) and registration.get("version")
            else None
        )
        raw_path = registration.get("installPath") if isinstance(registration, dict) else None
        install_path = Path(str(raw_path)).expanduser() if raw_path else None
        payload_version = plugin_version(install_path) if install_path and install_path.exists() else None
        evidence = [
            item
            for item in (
                f"registered={registered_version}" if registered_version else None,
                f"payload={payload_version}" if payload_version else None,
            )
            if item is not None
        ]
        versions = [value for value in (registered_version, payload_version) if value]
        matches = bool(versions) and expected is not None and all(value == expected for value in versions)
        return registered_version or payload_version, "registered-zcode-plugin", matches, evidence

    actual = installed_skill_version(target, scope, project_root)
    evidence = [f"skill={actual}"] if actual else []
    matches = actual == expected if expected is not None else None
    source = "lifecycle-skill"
    if target == "codex" and check.hook_mode == "project-fallback":
        source = "lifecycle-skill+project-fallback"
        evidence.append(f"runtime={check.hooks_state}")
    if target in RUNTIME_ADAPTER_TARGETS and check.actual in {CAPABILITY_ACTIVE, CAPABILITY_ACTIVE_PARTIAL}:
        source = "lifecycle-skill+runtime-adapter"
        evidence.append(f"runtime={check.actual}")
    return actual, source, matches, evidence


def actual_capability(check: CapabilityCheck, project_root: Path) -> str:
    if check.planned in {CAPABILITY_BLOCKED, CAPABILITY_SKIPPED}:
        return check.planned
    target = check.tool
    scope = check.scope
    if check.runtime_role == "skills-only-shadow":
        return CAPABILITY_SKILLS_ONLY if lifecycle_skills_installed(target, scope, project_root) else CAPABILITY_BLOCKED
    if target == "codex" and check.hook_mode == "project-fallback":
        inventory = codex_hook_inventory(project_root)
        hook_sets = codex_tenetora_hook_sets(inventory)
        enabled_native = [hook for hook in hook_sets["native"] if hook.get("enabled", True)]
        enabled_legacy_native = [
            hook for hook in hook_sets["legacy_native"] if hook.get("enabled", True)
        ]
        if enabled_native or enabled_legacy_native:
            check.hooks_state = "native-hook-blocks-fallback"
            return CAPABILITY_BLOCKED
        runtime_state = hook_runner_directory_state(default_machine_home() / "runtime" / "hooks")
        if runtime_state != "present":
            check.hooks_state = "project-fallback-runtime-unhealthy"
            return CAPABILITY_BLOCKED
        enabled_project = [hook for hook in hook_sets["project"] if hook.get("enabled", True)]
        if enabled_project:
            trusted = all(
                str(hook.get("trustStatus", "")).lower() in {"trusted", "managed"}
                for hook in enabled_project
            )
            check.hooks_state = "active-project-fallback" if trusted else "pending-project-trust"
            return CAPABILITY_ACTIVE if trusted else CAPABILITY_PENDING_TRUST
        if codex_project_fallback_present(project_root):
            check.hooks_state = "pending-project-trust"
            return CAPABILITY_PENDING_TRUST
        check.hooks_state = "project-fallback-missing"
        return CAPABILITY_BLOCKED
    if target == "codex" and check.hook_mode == "native-plugin" and check.marketplace == "unreadable":
        inventory = codex_hook_inventory(project_root)
        enabled_native = [
            hook
            for hook in codex_tenetora_hook_sets(inventory)["native"]
            if hook.get("enabled", True)
        ]
        enabled_legacy_native = [
            hook
            for hook in codex_tenetora_hook_sets(inventory)["legacy_native"]
            if hook.get("enabled", True)
        ]
        if enabled_legacy_native:
            check.hooks_state = "legacy-native-hook-active"
            return CAPABILITY_BLOCKED
        runtime_state = native_hook_runtime_state(enabled_native)
        trusted = bool(enabled_native) and all(
            str(hook.get("trustStatus", "")).lower() in {"trusted", "managed"}
            for hook in enabled_native
        )
        if runtime_state == "present" and trusted:
            check.hooks_state = "active-native-degraded-management"
            return CAPABILITY_ACTIVE
        check.hooks_state = "native-hook-blocks-fallback" if enabled_native else "native-management-unavailable"
        return CAPABILITY_BLOCKED
    if target in NATIVE_PLUGIN_TARGETS and scope == "global":
        if native_plugin_cli_available(target, project_root):
            try:
                entry = native_plugin_entry(target, project_root)
            except NativePluginStateUnavailable:
                entry = None
            path = native_plugin_install_path(target, entry, project_root) if entry else None
            if entry and path and path.exists() and not native_plugin_payload_issues(target, path) and bool(entry.get("enabled", True)):
                if target == "codex":
                    trust = codex_hook_trust(project_root)
                    if trust.get("state") == "active":
                        return CAPABILITY_ACTIVE
                    if trust.get("state") == "unavailable":
                        check.hooks_state = "hook-inventory-unavailable"
                        check.degraded_reason = "codex-hook-inventory-unavailable"
                        check.remediation = "Restart Codex, then rerun the upgrade or review Tenetora hooks with /hooks"
                    else:
                        check.hooks_state = "pending-native-trust"
                    return CAPABILITY_PENDING_TRUST
                return CAPABILITY_ACTIVE
        return CAPABILITY_SKILLS_ONLY if lifecycle_skills_installed(target, scope, project_root) else CAPABILITY_BLOCKED
    if target == "zcode" and scope == "global":
        registration = zcode_registration(project_root)
        path_raw = registration.get("installPath") if isinstance(registration, dict) else None
        path = Path(str(path_raw)).expanduser() if path_raw else None
        if path and path.exists() and not plugin_payload_issues(path) and zcode_activation_state(project_root) == "enabled":
            return CAPABILITY_ACTIVE
        return CAPABILITY_BLOCKED
    if not lifecycle_skills_installed(target, scope, project_root):
        return CAPABILITY_BLOCKED
    if target == "cursor":
        state, _, _ = runtime_adapter_state(target, scope, project_root, PACKAGE_ROOT or SKILLS_ROOT.parent)
        return CAPABILITY_ACTIVE if state == "ok" else CAPABILITY_SKILLS_ONLY
    if target == "opencode":
        state, _, _ = runtime_adapter_state(target, scope, project_root, PACKAGE_ROOT or SKILLS_ROOT.parent)
        return CAPABILITY_ACTIVE_PARTIAL if state == "ok" else CAPABILITY_SKILLS_ONLY
    if target == "pi":
        state, _, _ = runtime_adapter_state(target, scope, project_root, PACKAGE_ROOT or SKILLS_ROOT.parent)
        return CAPABILITY_ACTIVE_PARTIAL if state == "ok" else CAPABILITY_SKILLS_ONLY
    return CAPABILITY_SKILLS_ONLY


def build_postflight(preflight: CapabilityReport, project_root: Path) -> CapabilityReport:
    checks: list[CapabilityCheck] = []
    for original in preflight.checks:
        check = CapabilityCheck(**asdict(original))
        if (
            check.tool == "codex"
            and check.scope == "global"
            and check.degraded_reason == "codex-legacy-registration-recoverable"
        ):
            _, marketplace_unreadable = codex_marketplace_unreadable(check, project_root)
            if not marketplace_unreadable:
                check.degraded_reason = None
                check.blocker = None
                check.remediation = (
                    "Restart Codex and review the Tenetora hook definitions in /hooks "
                    "before runtime hooks can execute"
                )
        check.actual = actual_capability(check, project_root)
        capability_matches = check.actual == check.planned or (
            check.planned == CAPABILITY_PENDING_TRUST and check.actual == CAPABILITY_ACTIVE
        )
        if (
            check.tool == "codex"
            and check.scope == "global"
            and check.planned == CAPABILITY_ACTIVE
            and check.actual == CAPABILITY_PENDING_TRUST
            and check.hooks_state in {"hook-inventory-unavailable", "pending-native-trust"}
        ):
            # Replacing a native plugin can invalidate its host trust receipt between
            # preflight and postflight. Keep the result pending-trust rather than
            # reporting a false install failure; never claim ACTIVE without evidence.
            capability_matches = True
        (
            check.actual_version,
            check.version_source,
            check.version_matches_package,
            check.version_evidence,
        ) = installed_surface_version(check, project_root)
        check.existing_version = check.actual_version
        check.matches_plan = capability_matches and check.version_matches_package is not False
        if check.version_matches_package is False and check.planned not in {CAPABILITY_BLOCKED, CAPABILITY_SKIPPED}:
            check.blocker = (
                f"installed {check.version_source} version {check.actual_version or 'unknown'} "
                f"does not match package {check.expected_version or 'unknown'}"
            )
            check.remediation = (
                f"Rerun tenetora install --path {project_root} --tools {check.tool} "
                f"{install_scope_option(check.scope)} --force"
            )
        elif not capability_matches and check.blocker is None:
            check.blocker = f"installed capability {check.actual or 'unknown'} does not match planned {check.planned}"
        checks.append(check)
    return CapabilityReport(
        phase="postflight",
        package_version=preflight.package_version,
        python_version=preflight.python_version,
        project_path=preflight.project_path,
        checks=checks,
        tool_discovery=preflight.tool_discovery,
    )


def is_expected_skills_only(check: CapabilityCheck, status: str) -> bool:
    if check.matches_plan is False or check.version_matches_package is False:
        return False
    return status == CAPABILITY_SKILLS_ONLY and (
        check.fallback_accepted
        or check.maximum == CAPABILITY_SKILLS_ONLY
        or check.runtime_role == "skills-only-shadow"
        or (
            check.scope == "project"
            and check.plugin_api == "scope-unsupported"
            and check.planned == CAPABILITY_SKILLS_ONLY
        )
        or is_safe_native_cli_fallback(check)
    )


def is_degraded(check: CapabilityCheck, status: str) -> bool:
    return (
        status == CAPABILITY_SKILLS_ONLY
        and check.maximum != CAPABILITY_SKILLS_ONLY
        and check.runtime_role != "skills-only-shadow"
    )


def is_safe_native_cli_fallback(check: CapabilityCheck) -> bool:
    """Missing host CLIs may safely defer native activation until a later upgrade."""
    return (
        check.tool in NATIVE_PLUGIN_TARGETS
        and check.scope == "global"
        and check.plugin_api == "unavailable"
        and check.planned == CAPABILITY_SKILLS_ONLY
        and check.degraded_reason == "native-cli-unavailable"
    )


def is_partial(check: CapabilityCheck, status: str) -> bool:
    return is_degraded(check, status) and not is_expected_skills_only(check, status)


def capability_overall(checks: list[CapabilityCheck], *, actual: bool) -> str:
    statuses = [check.actual if actual else check.planned for check in checks]
    if actual and any(check.matches_plan is False for check in checks):
        return "BLOCKED"
    if any(status == CAPABILITY_BLOCKED for status in statuses):
        return "BLOCKED"
    if any(status == CAPABILITY_SKIPPED for status in statuses):
        return "PARTIAL"
    if any(status and is_partial(check, status) for check, status in zip(checks, statuses)):
        return "PARTIAL"
    if any(status == CAPABILITY_PENDING_TRUST for status in statuses):
        return "PENDING_TRUST"
    return "FULL" if actual else "READY"


def render_capability_report(report: CapabilityReport) -> str:
    result_label = human_text(
        "Actual" if report.phase == "postflight" else "Planned",
        "实际" if report.phase == "postflight" else "计划",
    )
    lines = [
        human_text(
            "Tenetora Post-install Verification" if report.phase == "postflight" else "Tenetora Installation Preflight",
            "Tenetora 安装后验证" if report.phase == "postflight" else "Tenetora 安装前预检",
        ),
        human_text(f"Package: {report.package_version}", f"安装包：{report.package_version}"),
        human_text(
            f"Python: {report.python_version} ({sys.executable})",
            f"Python：{report.python_version}（{sys.executable}）",
        ),
        human_text(f"Project: {report.project_path}", f"项目：{report.project_path}"),
        "",
        (
            f"{'Tool':<9} {'Scope':<8} {'Detected':<9} {'Plugin API':<17} {'Marketplace':<14} {'Writable':<9} {'Maximum':<15} {result_label}"
            + (" Match" if report.phase == "postflight" else "")
            if LANGUAGE != "zh"
            else f"{'工具':<9} {'范围':<8} {'已检测':<9} {'Plugin API':<17} {'Marketplace':<14} {'可写':<9} {'最大能力':<15} {result_label}"
            + (" 匹配" if report.phase == "postflight" else "")
        ),
    ]
    if report.tool_discovery:
        lines.append("")
        lines.append(human_text("Tool discovery:", "工具发现："))
        for item in report.tool_discovery:
            tool = str(item.get("tool") or "?")
            detected = bool(item.get("detected"))
            selected = bool(item.get("selected"))
            reasons = ", ".join(str(reason) for reason in item.get("reasons", []))
            lines.append(
                human_text(
                    f"  {tool}: {'detected' if detected else 'not detected'}; "
                    f"{'selected' if selected else 'skipped'} ({reasons})",
                    f"  {tool}：{'已检测' if detected else '未检测'}；"
                    f"{'已选择' if selected else '已跳过'}（{reasons}）",
                )
            )
    for check in report.checks:
        status = check.actual if report.phase == "postflight" else check.planned
        line = (
            f"{check.tool:<9} {display_value(check.scope):<8} "
            f"{(human_text('yes', '是') if check.detected else human_text('no', '否')):<9} "
            f"{display_value(check.plugin_api):<17} {display_value(check.marketplace):<14} "
            f"{(human_text('yes', '是') if check.writable else human_text('no', '否')):<9} "
            f"{display_value(check.maximum):<15} {display_value(status)}"
        )
        if report.phase == "postflight":
            line += f" {human_text('yes', '是') if check.matches_plan else human_text('no', '否')}"
        lines.append(line)
        if check.existing_version:
            version_label = human_text(
                "installed" if report.phase == "postflight" else "existing",
                "已安装" if report.phase == "postflight" else "现有版本",
            )
            lines.append(f"  {version_label}: {check.existing_version}")
        if report.phase == "postflight":
            version_match = (
                human_text("yes", "是")
                if check.version_matches_package is True
                else human_text("no", "否")
                if check.version_matches_package is False
                else human_text("unknown", "未知")
            )
            lines.append(
                human_text(
                    f"  version={check.actual_version or 'unknown'} expected={check.expected_version or 'unknown'} match={version_match} source={check.version_source or 'unknown'}",
                    f"  版本={check.actual_version or '未知'} 期望={check.expected_version or '未知'} 匹配={version_match} 来源={check.version_source or '未知'}",
                )
            )
            if check.version_evidence:
                lines.append(human_text(f"  version evidence: {', '.join(check.version_evidence)}", f"  版本证据：{', '.join(check.version_evidence)}"))
        if check.tool == "codex":
            hook_detail = human_text(
                f"mode={check.hook_mode} state={check.hooks_state}",
                f"模式={display_value(check.hook_mode)} 状态={display_value(check.hooks_state)}",
            )
            if check.degraded_reason:
                hook_detail += human_text(
                    f" degraded={check.degraded_reason}",
                    f" 降级原因={display_value(check.degraded_reason)}",
                )
            lines.append(human_text(f"  hooks: {hook_detail}", f"  hooks：{hook_detail}"))
        if check.blocker:
            lines.append(human_text(f"  reason: {check.blocker}", f"  原因：{localize_detail(check.blocker)}"))
        if check.remediation:
            lines.append(human_text(f"  action: {check.remediation}", f"  处理：{localize_detail(check.remediation)}"))
    overall = capability_overall(report.checks, actual=report.phase == "postflight")
    lines.extend(["", human_text(f"Overall: {overall}", f"总体状态：{display_value(overall)}")])
    return "\n".join(lines)


def activation_receipt_payload(report: CapabilityReport) -> dict[str, object]:
    overall = capability_overall(report.checks, actual=True)
    activated: list[dict[str, str]] = []
    follow_up: list[str] = []
    for check in report.checks:
        actual = check.actual or CAPABILITY_BLOCKED
        if check.runtime_role == "skills-only-shadow":
            continue
        activated.append(
            {
                "tool": check.tool,
                "scope": check.scope,
                "capability": actual,
                "runtime_owner": check.scope if actual in {CAPABILITY_ACTIVE, CAPABILITY_ACTIVE_PARTIAL} else "none",
            }
        )
        if (
            actual in {CAPABILITY_PENDING_TRUST, CAPABILITY_BLOCKED}
            or (actual == CAPABILITY_SKILLS_ONLY and is_safe_native_cli_fallback(check))
        ) and check.remediation:
            follow_up.append(check.remediation)
    tools = ",".join(dict.fromkeys(item["tool"] for item in activated))
    return {
        "status": overall,
        "package_version": report.package_version,
        "activated": activated,
        "follow_up": list(dict.fromkeys(follow_up)),
        "verification_command": f"tenetora doctor --tools {tools or 'auto'} --path {report.project_path}",
        "claims_runtime_execution": False,
    }


def render_activation_receipt(report: CapabilityReport) -> str:
    receipt = activation_receipt_payload(report)
    lines = [
        human_text("Tenetora Activation Receipt", "Tenetora 能力激活回执"),
        human_text(
            f"Verified install state: {display_value(str(receipt['status']))}",
            f"已验证安装状态：{display_value(str(receipt['status']))}",
        ),
    ]
    for item in receipt["activated"]:
        lines.append(
            human_text(
                f"  - {item['tool']}: {item['capability']} (scope={item['scope']}, runtime-owner={item['runtime_owner']})",
                f"  - {item['tool']}：{display_value(item['capability'])}（范围={display_value(item['scope'])}，运行时所有者={display_value(item['runtime_owner'])}）",
            )
        )
    if receipt["follow_up"]:
        lines.append(human_text("Required follow-up:", "后续操作："))
        lines.extend(f"  - {localize_detail(item)}" for item in receipt["follow_up"])
    lines.append(
        human_text(
            f"Verify: {receipt['verification_command']}",
            f"验证：{receipt['verification_command']}",
        )
    )
    lines.append(
        human_text(
            "Runtime execution is confirmed only after matching hook observation appears in doctor/status.",
            "只有 doctor/status 出现来源匹配的 Hook observation 后，才能确认运行时已真实执行。",
        )
    )
    return "\n".join(lines)


def render_compact_activation_receipt(report: CapabilityReport) -> str:
    receipt = activation_receipt_payload(report)
    follow_up = [localize_detail(str(item)) for item in receipt["follow_up"]]
    follow_up = list(dict.fromkeys(item for item in follow_up if item))
    if not follow_up:
        return ""
    lines = [human_text("Next action:", "后续操作：")]
    lines.extend(f"- {item}" for item in follow_up)
    return "\n".join(lines)


def resolve_fallback_policy(report: CapabilityReport, args: argparse.Namespace) -> bool:
    degraded = [check for check in report.checks if is_degraded(check, check.planned)]
    if args.require_full:
        for check in degraded:
            check.planned = CAPABILITY_BLOCKED
            check.blocker = f"full {check.tool} capability is required: {check.blocker or 'skills-only fallback predicted'}"
        return not degraded
    explicitly_resolved = [
        check
        for check in degraded
        if (
            check.scope == "project"
            and check.plugin_api == "scope-unsupported"
            and check.planned == CAPABILITY_SKILLS_ONLY
        )
        or is_safe_native_cli_fallback(check)
        or (check.tool == "codex" and args.codex_hooks in {"auto", "off"})
    ]
    for check in explicitly_resolved:
        check.fallback_accepted = True
    degraded = [check for check in degraded if check not in explicitly_resolved]
    if not degraded:
        return True
    if args.allow_skills_only:
        for check in degraded:
            check.fallback_accepted = True
        return True
    if args.dry_run or args.action == "preflight":
        return True
    if not sys.stdin.isatty():
        for check in degraded:
            check.planned = CAPABILITY_BLOCKED
            check.remediation = "Rerun with --allow-skills-only, repair the environment, or omit this tool"
        return False
    for check in degraded:
        while True:
            print(
                human_text(
                    f"{check.tool} ({check.scope}) cannot reach {check.maximum}: {check.blocker or 'skills-only fallback predicted'}",
                    f"{check.tool}（{check.scope}）无法达到 {display_value(check.maximum)}："
                    f"{localize_detail(check.blocker or '预计将降级为 skills-only')}",
                ),
                file=sys.stderr,
            )
            choice = input(
                human_text(
                    "Choose [r] retry after repair, [s] continue skills-only, or [k] skip: ",
                    "请选择：[r] 修复后重试，[s] 以 skills-only 继续，[k] 跳过：",
                )
            ).strip().lower()
            if choice in {"s", "skills-only"}:
                check.fallback_accepted = True
                break
            if choice in {"k", "skip"}:
                check.planned = CAPABILITY_SKIPPED
                break
            if choice in {"r", "retry"}:
                retry_targets = list(getattr(args, "resolved_targets", parse_tools(args.tools)))
                retry_scopes_by_target = dict(
                    getattr(
                        args,
                        "resolved_scopes_by_target",
                        target_scopes_for(retry_targets, args.scope_flag or args.scope),
                    )
                )
                retry_scopes = list(
                    dict.fromkeys(
                        scope
                        for target in retry_targets
                        for scope in retry_scopes_by_target[target]
                    )
                )
                refreshed = build_preflight(
                    retry_targets,
                    retry_scopes,
                    args.path,
                    args.codex_hooks,
                    args.allow_tracked_codex_hooks,
                    args.prune_shadowed,
                    retry_scopes_by_target,
                )
                report.checks = refreshed.checks
                return resolve_fallback_policy(report, args)
    return True


def selected_scopes(raw: str) -> list[str]:
    if raw == "both":
        return ["global", "project"]
    return [raw]


def install_scope_option(scope: str) -> str:
    return {
        "global": "--global",
        "project": "--in-project",
        "both": "--both",
    }[scope]


def target_scopes_for(targets: list[str], explicit_scope: str | None) -> dict[str, list[str]]:
    if explicit_scope:
        selected = selected_scopes(explicit_scope)
        return {target: list(selected) for target in targets}
    return {
        target: [str(platform_contract(target)["default_scope"])]
        for target in targets
    }


def existing_update_targets(raw: str) -> list[str]:
    requested = [part.strip() for part in raw.split(",") if part.strip()]
    if "auto" in requested or "all" in requested:
        return list(ALL_TOOLS)
    return parse_tools(raw)


def existing_target_scopes_for(
    targets: list[str],
    explicit_scope: str | None,
    project_root: Path,
) -> dict[str, list[str]]:
    candidate_scopes = selected_scopes(explicit_scope) if explicit_scope else ["global", "project"]
    registered_global = set(effective_global_surfaces())
    discovered: dict[str, list[str]] = {}
    for target in targets:
        matched = [
            scope
            for scope in candidate_scopes
            if existing_surface_present(target, scope, project_root)
            or named_lifecycle_surface_present(target, scope, project_root)
            or scope == "global" and target in registered_global
        ]
        if matched:
            discovered[target] = matched
    return discovered


def existing_plan_payload(scopes_by_target: dict[str, list[str]]) -> dict[str, object]:
    return {
        "status": "found" if scopes_by_target else "missing",
        "surface_count": sum(len(scopes) for scopes in scopes_by_target.values()),
        "targets": scopes_by_target,
    }


def project_scopes_for(targets: list[str], project_root: Path) -> dict[str, list[str]]:
    if is_reserved_project_root(project_root):
        return {}
    return {
        target: ["project"]
        for target in targets
        if existing_surface_present(target, "project", project_root)
    }


def global_scopes_for(targets: list[str], project_root: Path) -> dict[str, list[str]]:
    registered_global = set(effective_global_surfaces())
    legacy_home = default_legacy_machine_home().resolve(strict=False)
    if inspect_machine_home(legacy_home).get("managed"):
        try:
            registered_global.update(registered_global_surfaces(legacy_home))
        except Exception:
            pass
    return {
        target: ["global"]
        for target in targets
        if existing_surface_present(target, "global", project_root) or target in registered_global
    }


def assign_machine_runtime_owners(units: list[InstallUnit]) -> None:
    """Choose one runtime scope per tool without inspecting another unit's paths."""
    for target in RUNTIME_ADAPTER_TARGETS:
        participating = [unit for unit in units if target in unit.scopes_by_target]
        if not participating:
            continue
        has_global = any(unit.kind == "global" for unit in participating)
        has_project = any(unit.kind == "project" for unit in participating)
        preference = str(platform_contract(target)["both_runtime_preference"])
        if has_global and has_project:
            owner = preference
        elif has_project:
            owner = "project"
        else:
            owner = "global"
        for unit in participating:
            unit.runtime_owners[target] = owner


def machine_existing_plan(
    targets: list[str],
    explicit_scope: str | None,
    current_project: Path,
    discovered_entries: dict[Path, dict[str, object]] | None = None,
) -> MachineInstallPlan:
    include_global = explicit_scope in {None, "global", "both"}
    include_projects = explicit_scope in {None, "project", "both"}
    units: list[InstallUnit] = []
    stale: list[str] = []
    discovered_paths: list[str] = []
    if include_global:
        global_scopes = global_scopes_for(targets, current_project)
        if global_scopes:
            units.append(InstallUnit("global", current_project, global_scopes, "global"))

    if include_projects:
        if explicit_scope in {"project", "both"}:
            project_root = current_project.resolve(strict=False)
            scopes = project_scopes_for(targets, project_root)
            governance_present = project_governance_surface_present(project_root)
            if not scopes and not governance_present:
                raise RuntimeError(
                    "The explicit project target has no existing Tenetora project installation "
                    "or governance directory; global matches cannot satisfy --in-project/--both. "
                    "Run tenetora install to create a project installation, or verify --path."
                )
            units.append(
                InstallUnit(
                    f"project:{project_root}",
                    project_root,
                    scopes,
                    "project",
                    governance_only=not bool(scopes),
                )
            )
        else:
            entries = registered_project_entries()
            entries_by_path = {}
            for item in entries:
                path = Path(str(item["path"])).resolve(strict=False)
                if is_reserved_project_root(path):
                    continue
                entries_by_path[path] = item
            registered_paths = set(entries_by_path)
            nearby_entries = nearby_project_entries(current_project)
            for path, item in nearby_entries.items():
                entries_by_path.setdefault(path, item)
            if discovered_entries:
                discovered_paths = sorted(
                    str(path) for path in set(discovered_entries) - registered_paths
                )
                for path, item in discovered_entries.items():
                    entries_by_path.setdefault(path, item)
            else:
                discovered_paths = sorted(str(path) for path in set(nearby_entries) - registered_paths)
            candidates = set(entries_by_path)
            legacy_home = default_legacy_machine_home().resolve(strict=False)
            if inspect_machine_home(legacy_home).get("managed"):
                try:
                    for item in registered_project_entries(legacy_home):
                        path = Path(str(item["path"])).resolve(strict=False)
                        if is_reserved_project_root(path, home=legacy_home):
                            continue
                        candidates.add(path)
                        entries_by_path.setdefault(path, item)
                except Exception:
                    pass
            current_scopes = project_scopes_for(targets, current_project)
            if current_scopes:
                candidates.add(current_project.resolve(strict=False))
            for project_root in sorted(candidates):
                if not project_root.exists() or not project_root.is_dir() or path_is_redirected(project_root):
                    stale.append(str(project_root))
                    continue
                scopes = project_scopes_for(targets, project_root)
                governance = entries_by_path.get(project_root, {}).get("governance")
                if not scopes and not auto_discovery_entry_relevant(project_root, {}, governance):
                    stale.append(str(project_root))
                    continue
                if not scopes and not isinstance(governance, dict):
                    stale.append(str(project_root))
                    continue
                units.append(
                    InstallUnit(
                        f"project:{project_root}",
                        project_root,
                        scopes,
                        "project",
                        governance_only=not bool(scopes),
                    )
                )
    assign_machine_runtime_owners(units)
    return MachineInstallPlan(
        units=units,
        stale_projects=stale,
        discovered_projects=discovered_paths if include_projects else [],
    )


def register_project_surfaces(project_root: Path, targets: list[str]) -> None:
    surfaces = project_scopes_for(targets, project_root)
    if surfaces:
        upsert_project(project_root, surfaces)


def register_global_surfaces(targets: list[str]) -> None:
    if targets:
        upsert_global_surfaces(targets)


def checked_project_root(project_root: Path, *, require_canonical: bool) -> Path | None:
    """Reject redirected project/governance paths before any hook-state access."""
    raw = Path(project_root).expanduser()
    if path_is_redirected(raw):
        raise RuntimeError(f"project path is a symbolic link: {raw}")
    project = raw.resolve(strict=False)
    if not project.is_dir():
        return None
    if is_reserved_project_root(project):
        return None
    canonical = project / ".tenetora"
    if path_is_redirected(canonical):
        raise RuntimeError(f"project governance directory is a symbolic link: {canonical}")
    if canonical.exists() and not canonical.is_dir():
        raise RuntimeError(f"project governance directory is not a directory: {canonical}")
    if require_canonical and not canonical.is_dir():
        return None
    return project


def project_governance_surface_present(project_root: Path) -> bool:
    if is_reserved_project_root(project_root):
        return False
    return any(
        (project_root / name).exists() or path_is_redirected(project_root / name)
        for name in (".tenetora", ".harness")
    )


def auto_discovery_entry_relevant(
    project_root: Path,
    surfaces: dict[str, list[str]],
    governance: dict[str, object] | None,
) -> bool:
    """Keep automatic discovery bounded to installed surfaces or legacy work."""
    if surfaces:
        return True
    if not isinstance(governance, dict):
        return False
    if governance.get("classification") in SAFE_CANONICAL_GOVERNANCE_CLASSIFICATIONS:
        return False
    # A governance-only entry is actionable when it carries a legacy directory
    # that may need migration or review.  A standalone malformed .tenetora is
    # not enough to expand another repository's upgrade scope.
    return (project_root / ".harness").exists()


def project_governance_migration_module() -> object:
    if PACKAGE_ROOT is None:
        raise RuntimeError("Tenetora package root is unavailable")
    module_path = PACKAGE_ROOT / "skills" / ROUTER_SKILL_NAME / "scripts" / "migrate_harness.py"
    module_name = "tenetora_install_project_governance_migration"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load project governance migration module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def project_governance_migration(
    project_root: Path,
    *,
    dry_run: bool,
) -> dict[str, object] | None:
    if not project_governance_surface_present(project_root):
        return None
    module = project_governance_migration_module()
    classification = module.classify_project(project_root)
    classification_payload = classification.as_json()
    ready_statuses = {
        "canonical",
        "canonical-with-foreign",
        "canonical-with-preserved-legacy",
        "legacy-owned",
    }
    if dry_run:
        ready = classification.status in ready_statuses
        action = "migrate" if classification.status == "legacy-owned" else "keep"
        return {
            "result": "READY" if ready else "BLOCKED",
            "status": "planned" if ready else "review-required",
            "action": action,
            "classification": classification_payload,
            "message": classification.message,
            "project_path": str(project_root),
        }

    result = module.apply_migration(project_root, classification)
    payload = result.as_json()
    success = result.status in {"current", "repaired", "migrated"}
    canonical = project_root / ".tenetora"
    manifest = canonical / "manifest.json"
    owned_legacy_remains = bool(
        (project_root / ".harness").exists()
        and classification.status in {"legacy-owned", "coexistence"}
    )
    if success and (not manifest.is_file() or owned_legacy_remains):
        success = False
        payload["status"] = "failed-verification"
        payload["message"] = (
            "project governance migration did not converge: canonical manifest is missing "
            "or a proven owned .harness remains"
        )
    payload.update(
        {
            "result": "FULL" if success else "BLOCKED",
            "project_path": str(project_root),
            "canonical_manifest": str(manifest),
            "canonical_manifest_present": manifest.is_file(),
            "legacy_present": (project_root / ".harness").exists(),
        }
    )
    return payload


def canonical_governance_registration(project_root: Path) -> dict[str, object]:
    module = project_governance_migration_module()
    classification = module.classify_project(project_root)
    registration = governance_registration_for_classification(
        str(classification.status),
        canonical_present=(project_root / ".tenetora").is_dir(),
        source_fingerprint=getattr(classification, "source_fingerprint", None),
    )
    if registration is None or registration.get("state") != "canonical":
        raise RuntimeError(
            f"project governance did not converge to canonical state: {classification.status}"
        )
    return registration


def governance_registration_for_project(project_root: Path) -> dict[str, object] | None:
    module = project_governance_migration_module()
    classification = module.classify_project(project_root)
    return governance_registration_for_classification(
        str(classification.status),
        canonical_present=(project_root / ".tenetora").is_dir(),
        source_fingerprint=getattr(classification, "source_fingerprint", None),
    )


def nearby_project_entries(current_project: Path) -> dict[Path, dict[str, object]]:
    if is_reserved_project_root(current_project) or not project_governance_surface_present(current_project):
        return {}
    root = current_project.resolve(strict=False).parent
    if root in {Path(root.anchor), Path.home().resolve(strict=False), machine_home().resolve(strict=False)}:
        return {}
    try:
        candidates = discover_project_candidates([root], max_depth=2, max_candidates=5000)
    except Exception:
        return {}
    entries: dict[Path, dict[str, object]] = {}
    for project in candidates:
        # The discovery root is the parent of the active project.  It may be a
        # separately governed repository (for example a submodule's superproject)
        # and must not be promoted into this project's nearby scope.
        if project.resolve(strict=False) == root:
            continue
        surfaces = detect_project_surfaces(project)
        governance = governance_registration_for_project(project)
        if not auto_discovery_entry_relevant(project, surfaces, governance):
            continue
        canonical = project.resolve(strict=False)
        entries[canonical] = {
            "path": str(canonical),
            "surfaces": surfaces,
            **({"governance": governance} if governance is not None else {}),
        }
    return entries


def register_nearby_project_entries(current_project: Path) -> list[str]:
    entries = nearby_project_entries(current_project)
    for project, item in entries.items():
        governance = item.get("governance") if isinstance(item.get("governance"), dict) else None
        upsert_project(project, dict(item.get("surfaces", {})), governance=governance)
    return sorted(str(path) for path in entries)


def auto_discovered_project_entries(
    current_project: Path,
    *,
    include_home: bool,
) -> dict[Path, dict[str, object]]:
    registered_paths = {
        Path(str(item["path"])).resolve(strict=False)
        for item in registered_project_entries()
    }
    entries = {
        project: item
        for project, item in nearby_project_entries(current_project).items()
        if project not in registered_paths
    }
    roots = auto_discovery_roots(current_project, include_home=include_home)
    current_parent = current_project.resolve(strict=False).parent
    candidates = (
        discover_project_candidates(
            roots,
            max_depth=AUTO_DISCOVERY_DEPTH,
            max_candidates=AUTO_DISCOVERY_CANDIDATES,
        )
        if roots
        else []
    )
    for project in candidates:
        # Scope-less discovery may scan the active project's parent to find
        # siblings, but the parent itself is a separate repository boundary.
        if project.resolve(strict=False) == current_parent:
            continue
        surfaces = detect_project_surfaces(project)
        governance = governance_registration_for_project(project)
        if not auto_discovery_entry_relevant(project, surfaces, governance):
            continue
        canonical = project.resolve(strict=False)
        if canonical in registered_paths:
            continue
        entries[canonical] = {
            "path": str(canonical),
            "surfaces": surfaces,
            **({"governance": governance} if governance is not None else {}),
        }
    return entries


def register_auto_discovered_project_entries(
    current_project: Path,
    *,
    include_home: bool,
) -> list[str]:
    entries = auto_discovered_project_entries(current_project, include_home=include_home)
    for project, item in entries.items():
        governance = item.get("governance") if isinstance(item.get("governance"), dict) else None
        upsert_project(project, dict(item.get("surfaces", {})), governance=governance)
    return sorted(str(path) for path in entries)


def converge_project_commit_hooks(
    project_root: Path,
    *,
    dry_run: bool,
    expected_sha256: dict[str, str] | None = None,
) -> dict[str, object]:
    project = checked_project_root(project_root, require_canonical=True)
    if PACKAGE_ROOT is None or project is None:
        return {"status": "not-applicable", "updated": [], "preserved": []}
    module_path = PACKAGE_ROOT / "skills" / ROUTER_SKILL_NAME / "scripts" / "commit_hooks.py"
    module_name = "tenetora_install_commit_hook_convergence"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load commit hook convergence module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    canonical_cli = machine_home() / "bin" / ("tenetora.cmd" if os.name == "nt" else "tenetora")
    return module.converge_managed_hooks(
        project,
        cli_command=str(canonical_cli),
        dry_run=dry_run,
        expected_sha256=expected_sha256,
    )


def converge_project_runtime_state_ignores(
    project_root: Path,
    *,
    dry_run: bool,
    expected_sha256: str | None = None,
) -> dict[str, object]:
    """Append only volatile alignment-state ignores under the hook operation lock."""

    project = checked_project_root(project_root, require_canonical=True)
    if project is None:
        return {"status": "not-applicable", "added": []}
    target = project / ".tenetora" / ".gitignore"
    if path_is_redirected(target) or (target.exists() and not target.is_file()):
        raise RuntimeError(f"project runtime ignore target must be a regular file: {target}")
    module = load_commit_hook_module("tenetora_install_runtime_ignore_convergence")
    with module.hooks_operation_lock(project):
        before = target.read_bytes() if target.is_file() else b""
        before_sha256 = hashlib.sha256(before).hexdigest() if target.is_file() else ""
        if expected_sha256 is not None and before_sha256 != expected_sha256:
            raise RuntimeError(
                "project runtime ignore state changed after the upgrade snapshot; "
                "refusing to overwrite a concurrent edit"
            )
        text = before.decode("utf-8") if before else ""
        existing = {line.strip().rstrip("/") for line in text.splitlines()}
        missing = [entry for entry in PROJECT_RUNTIME_IGNORE_ENTRIES if entry.rstrip("/") not in existing]
        if missing and not dry_run:
            updated = text
            if updated and not updated.endswith("\n"):
                updated += "\n"
            updated += "".join(f"{entry}\n" for entry in missing)
            current = target.read_bytes() if target.is_file() else b""
            current_sha256 = hashlib.sha256(current).hexdigest() if target.is_file() else ""
            if current_sha256 != before_sha256:
                raise RuntimeError(
                    "project runtime ignore state changed during convergence; "
                    "refusing to overwrite a concurrent edit"
                )
            module.atomic_write_text(target, updated)
        after = target.read_bytes() if target.is_file() else before
        return {
            "status": "planned" if dry_run and missing else "updated" if missing else "current",
            "target": str(target.resolve(strict=False)),
            "added": missing,
            "before_sha256": before_sha256,
            "after_sha256": hashlib.sha256(after).hexdigest() if target.is_file() else "",
        }


def registered_project_hook_candidates(current_project: Path, *, all_existing: bool) -> set[Path]:
    candidates: set[Path] = set()
    current = checked_project_root(current_project, require_canonical=False)
    if current is not None:
        candidates.add(current)
    if all_existing:
        for path in registered_projects():
            candidate = checked_project_root(path, require_canonical=False)
            if candidate is not None:
                candidates.add(candidate)
        legacy_home = default_legacy_machine_home().resolve(strict=False)
        if inspect_machine_home(legacy_home).get("managed"):
            for path in registered_projects(legacy_home):
                candidate = checked_project_root(path, require_canonical=False)
                if candidate is not None:
                    candidates.add(candidate)
    return candidates


def load_commit_hook_module(module_name: str) -> object:
    if PACKAGE_ROOT is None:
        raise RuntimeError("package root is unavailable for commit hook migration")
    module_path = PACKAGE_ROOT / "skills" / ROUTER_SKILL_NAME / "scripts" / "commit_hooks.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load commit hook module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def snapshot_registered_project_commit_hooks(
    current_project: Path,
    *,
    all_existing: bool,
    rollback_journal: Path,
) -> dict[str, object]:
    candidates = registered_project_hook_candidates(current_project, all_existing=all_existing)
    module = load_commit_hook_module("tenetora_install_commit_hook_snapshot")
    rollback_journal.mkdir(parents=True, exist_ok=True)
    manifest_path = rollback_journal / "manifest.json"
    if manifest_path.exists():
        raise RuntimeError(f"commit hook rollback journal already exists: {manifest_path}")

    rollback_entries: list[dict[str, object]] = []
    seen_targets: set[str] = set()
    project_count = 0
    for project in sorted(candidates):
        project = checked_project_root(project, require_canonical=True)
        if project is None:
            continue
        project_count += 1
        with module.hooks_operation_lock(project):
            ignore_target = project / ".tenetora" / ".gitignore"
            if path_is_redirected(ignore_target) or (ignore_target.exists() and not ignore_target.is_file()):
                raise RuntimeError(f"project runtime ignore target must be a regular file: {ignore_target}")
            ignore_key = str(ignore_target.resolve(strict=False))
            if ignore_key not in seen_targets:
                seen_targets.add(ignore_key)
                before_exists = ignore_target.is_file()
                content = ignore_target.read_bytes() if before_exists else b""
                backup = rollback_journal / "files" / f"{len(rollback_entries):04d}-harness-gitignore"
                if before_exists:
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    backup.write_bytes(content)
                    shutil.copymode(ignore_target, backup)
                digest = hashlib.sha256(content).hexdigest() if before_exists else ""
                rollback_entries.append(
                    {
                        "project_path": str(project),
                        "target": str(ignore_target),
                        "backup": str(backup) if before_exists else None,
                        "before_sha256": digest,
                        "before_exists": before_exists,
                        "after_sha256": digest,
                        "kind": "project-runtime-ignore",
                    }
                )
            managed_project = False
            if module.is_git_worktree(project):
                hooks_dir = module.git_hooks_dir(project)
                for hook_name in module.HOOK_NAMES:
                    target = hooks_dir / hook_name
                    key = str(target.resolve(strict=False))
                    if key in seen_targets or not target.is_file():
                        continue
                    text = target.read_text(encoding="utf-8", errors="ignore")
                    if not module.has_managed_marker(text):
                        continue
                    managed_project = True
                    seen_targets.add(key)
                    content = target.read_bytes()
                    backup = rollback_journal / "files" / f"{len(rollback_entries):04d}-{hook_name}"
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    backup.write_bytes(content)
                    shutil.copymode(target, backup)
                    digest = hashlib.sha256(content).hexdigest()
                    rollback_entries.append(
                        {
                            "project_path": str(project),
                            "target": str(target),
                            "backup": str(backup),
                            "before_sha256": digest,
                            "before_exists": True,
                            "after_sha256": digest,
                        }
                    )
            state_target = project / module.STATE_REL
            if managed_project or state_target.is_file():
                key = str(state_target.resolve(strict=False))
                if key not in seen_targets:
                    seen_targets.add(key)
                    backup = rollback_journal / "files" / f"{len(rollback_entries):04d}-commit-hooks-state"
                    before_exists = state_target.is_file()
                    content = state_target.read_bytes() if before_exists else b""
                    if before_exists:
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        backup.write_bytes(content)
                        shutil.copymode(state_target, backup)
                    digest = hashlib.sha256(content).hexdigest() if before_exists else ""
                    rollback_entries.append(
                        {
                            "project_path": str(project),
                            "target": str(state_target),
                            "backup": str(backup) if before_exists else None,
                            "before_sha256": digest,
                            "before_exists": before_exists,
                            "after_sha256": digest,
                        }
                    )
    write_json_atomic(
        manifest_path,
        {"version": 2, "status": "snapshotted", "files": rollback_entries},
    )
    return {
        "status": "snapshotted",
        "project_count": project_count,
        "file_count": len(rollback_entries),
    }


def converge_registered_project_commit_hooks(
    current_project: Path,
    *,
    all_existing: bool,
    dry_run: bool,
    rollback_journal: Path | None = None,
) -> dict[str, object]:
    candidates = registered_project_hook_candidates(current_project, all_existing=all_existing)
    rollback_entries: list[dict[str, object]] = []
    if rollback_journal is not None and not dry_run:
        manifest_path = rollback_journal / "manifest.json"
        if not manifest_path.is_file():
            snapshot_registered_project_commit_hooks(
                current_project,
                all_existing=all_existing,
                rollback_journal=rollback_journal,
            )
        manifest_payload = read_json_object(manifest_path, {})
        files = manifest_payload.get("files") if isinstance(manifest_payload, dict) else None
        if not isinstance(files, list):
            raise RuntimeError(f"commit hook rollback journal is missing or invalid: {manifest_path}")
        rollback_entries = [entry for entry in files if isinstance(entry, dict)]

    results: list[dict[str, object]] = []
    updated = 0
    migrated_states = 0
    migrated_runtime_ignores = 0
    deferred = 0
    try:
        for project in sorted(candidates):
            project = checked_project_root(project, require_canonical=True)
            if project is None:
                continue
            project_targets: set[str] = {
                str((project / ".tenetora" / "state" / "commit-hooks.json").resolve(strict=False)),
                str((project / ".tenetora" / ".gitignore").resolve(strict=False)),
            }
            module = load_commit_hook_module("tenetora_install_commit_hook_convergence_targets")
            if module.is_git_worktree(project):
                hooks_dir = module.git_hooks_dir(project)
                project_targets.update(
                    str((hooks_dir / hook_name).resolve(strict=False)) for hook_name in module.HOOK_NAMES
                )
            expected = {
                str(entry.get("target")): str(entry.get("before_sha256") or "")
                for entry in rollback_entries
                if str(entry.get("target")) in project_targets
            }
            ignore_target = str((project / ".tenetora" / ".gitignore").resolve(strict=False))
            hook_expected = expected
            result = converge_project_commit_hooks(
                project,
                dry_run=dry_run,
                expected_sha256=hook_expected,
            )
            result["project_path"] = str(project)
            results.append(result)
            target_sha256 = result.pop("target_sha256", {})
            if isinstance(target_sha256, dict):
                for entry in rollback_entries:
                    target = str(entry.get("target") or "")
                    if target in target_sha256:
                        entry["after_sha256"] = str(target_sha256[target])
            ignore_before = expected.get(ignore_target, "")
            if isinstance(target_sha256, dict) and ignore_target in target_sha256:
                ignore_before = str(target_sha256[ignore_target])
            ignore_result = converge_project_runtime_state_ignores(
                project,
                dry_run=dry_run,
                expected_sha256=ignore_before,
            )
            if ignore_result.get("added"):
                migrated_runtime_ignores += 1
            for entry in rollback_entries:
                if str(entry.get("target") or "") == ignore_target:
                    entry["after_sha256"] = str(ignore_result.get("after_sha256") or "")
            updated += len(result.get("updated") or [])
            migrated_states += int(bool(result.get("state_migration")))
            deferred += int(result.get("status") == "deferred-cli-missing")
    finally:
        if rollback_journal is not None and not dry_run:
            write_json_atomic(
                rollback_journal / "manifest.json",
                {"version": 2, "status": "converged", "files": rollback_entries},
            )
    return {
        "status": "deferred" if deferred else "updated" if updated or migrated_states or migrated_runtime_ignores else "current",
        "project_count": len(results),
        "updated_hook_count": updated,
        "migrated_state_count": migrated_states,
        "migrated_runtime_ignore_count": migrated_runtime_ignores,
        "deferred_project_count": deferred,
        "projects": results,
    }


def restore_commit_hook_journal(rollback_journal: Path) -> dict[str, object]:
    rollback_journal = validate_unredirected_path(
        rollback_journal,
        label="commit hook rollback journal",
    )
    manifest = rollback_journal / "manifest.json"
    payload = read_json_object(manifest, {})
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list) and isinstance(payload, dict):
        files = payload.get("hooks")
    if not isinstance(files, list):
        raise RuntimeError(f"commit hook rollback journal is missing or invalid: {manifest}")
    restored: list[str] = []
    conflicts: list[str] = []
    journal_root = rollback_journal.resolve(strict=False)
    module = load_commit_hook_module("tenetora_install_commit_hook_restore")
    pending: list[tuple[Path, Path | None, bool]] = []
    legacy_allowed_targets: set[str] = set()
    try:
        for candidate in registered_projects():
            project = checked_project_root(Path(candidate).expanduser(), require_canonical=True)
            if project is None:
                continue
            legacy_allowed_targets.add(
                str((project / ".tenetora" / ".gitignore").resolve(strict=False))
            )
            legacy_allowed_targets.add(
                str((project / ".tenetora" / "state" / "commit-hooks.json").resolve(strict=False))
            )
            if module.is_git_worktree(project):
                hooks_dir = module.git_hooks_dir(project)
                legacy_allowed_targets.update(
                    str((hooks_dir / hook_name).resolve(strict=False))
                    for hook_name in module.HOOK_NAMES
                )
    except (OSError, RuntimeError):
        legacy_allowed_targets.clear()

    def validate_items(items: list[dict[str, object]]) -> None:
        for item in items:
            target = Path(str(item.get("target") or "")).expanduser()
            backup = Path(str(item.get("backup") or "")).expanduser()
            before_exists = bool(item.get("before_exists", True))
            project_path = item.get("project_path")
            if isinstance(project_path, str) and project_path:
                project = Path(project_path).expanduser()
                allowed = {
                    str((project / ".tenetora" / ".gitignore").resolve(strict=False)),
                    str((project / ".tenetora" / "state" / "commit-hooks.json").resolve(strict=False)),
                }
                if module.is_git_worktree(project):
                    hooks_dir = module.git_hooks_dir(project)
                    allowed.update(
                        str((hooks_dir / hook_name).resolve(strict=False)) for hook_name in module.HOOK_NAMES
                    )
                if str(target.resolve(strict=False)) not in allowed:
                    conflicts.append(str(target))
                    continue
            elif str(target.resolve(strict=False)) not in legacy_allowed_targets:
                conflicts.append(str(target))
                continue
            if before_exists:
                try:
                    backup.resolve(strict=False).relative_to(journal_root)
                except ValueError:
                    conflicts.append(str(target))
                    continue
            before = str(item.get("before_sha256") or "")
            after = str(item.get("after_sha256") or "")
            current = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else ""
            if current == before:
                continue
            if not after or current != after:
                conflicts.append(str(target))
                continue
            if not before_exists:
                pending.append((target, None, False))
                continue
            if not backup.is_file():
                conflicts.append(str(target))
                continue
            pending.append((target, backup, True))

    def write_file(target: Path, content: bytes, mode: int) -> None:
        ensure_unredirected_directory(target.parent, label="commit hook restore parent")
        validate_unredirected_entry_path(target, label="commit hook restore target")
        with tempfile.NamedTemporaryFile("wb", dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.chmod(mode)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def restore_items() -> None:
        for target, backup, before_exists in pending:
            if not before_exists:
                remove_unredirected_entry(target, label="commit hook restore target")
                restored.append(str(target))
                continue
            assert backup is not None
            write_file(target, backup.read_bytes(), backup.stat().st_mode & 0o777)
            restored.append(str(target))

    grouped: dict[str, list[dict[str, object]]] = {}
    unbound: list[dict[str, object]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        project_path = item.get("project_path")
        if isinstance(project_path, str) and project_path:
            grouped.setdefault(project_path, []).append(item)
        else:
            unbound.append(item)
    projects: list[tuple[Path, list[dict[str, object]]]] = []
    for project_path in sorted(grouped):
        try:
            project = checked_project_root(Path(project_path).expanduser(), require_canonical=True)
        except RuntimeError:
            project = None
        entries = grouped[project_path]
        if project is None:
            conflicts.extend(str(item.get("target") or "") for item in entries)
        else:
            projects.append((project, entries))
    if conflicts:
        return {"status": "blocked", "restored": [], "conflicts": conflicts}
    with ExitStack() as locks:
        for project, _entries in projects:
            locks.enter_context(module.hooks_operation_lock(project))
        validate_items(unbound)
        for _project, entries in projects:
            validate_items(entries)
        if conflicts:
            return {"status": "blocked", "restored": [], "conflicts": conflicts}
        restore_snapshot_root = Path(
            tempfile.mkdtemp(prefix=".restore-", dir=rollback_journal)
        )
        restore_snapshots: list[tuple[Path, bool, Path | None, int]] = []
        try:
            try:
                for index, (target, _backup, _before_exists) in enumerate(pending):
                    current_present = target.is_file()
                    snapshot = restore_snapshot_root / f"{index:04d}"
                    mode = target.stat().st_mode & 0o777 if current_present else 0o600
                    if current_present:
                        snapshot.write_bytes(target.read_bytes())
                        snapshot.chmod(mode)
                    restore_snapshots.append((target, current_present, snapshot, mode))
            except (OSError, UnicodeError) as exc:
                return {
                    "status": "blocked",
                    "restored": [],
                    "conflicts": [f"restore-snapshot: {exc}"],
                }

            try:
                restore_items()
            except Exception as exc:
                try:
                    for target, current_present, snapshot, mode in reversed(restore_snapshots):
                        if current_present:
                            write_file(target, snapshot.read_bytes(), mode)
                        else:
                            remove_unredirected_entry(target, label="commit hook restore target")
                except Exception as rollback_exc:
                    return {
                        "status": "blocked",
                        "restored": [],
                        "conflicts": [f"restore-incomplete: {rollback_exc}"],
                    }
                return {
                    "status": "blocked",
                    "restored": [],
                    "conflicts": [f"restore-apply: {exc}"],
                }
        finally:
            shutil.rmtree(restore_snapshot_root, ignore_errors=True)
    return {
        "status": "restored",
        "restored": restored,
        "conflicts": [],
    }


def finalize_commit_hook_journal(rollback_journal: Path) -> dict[str, object]:
    manifest = rollback_journal / "manifest.json"
    payload = read_json_object(manifest, {})
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list):
        raise RuntimeError(f"commit hook rollback journal is missing or invalid: {manifest}")
    finalized = 0
    conflicts: list[str] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        target = Path(str(item.get("target") or "")).expanduser()
        expected = str(item.get("after_sha256") or "")
        current = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else ""
        if current != expected:
            conflicts.append(str(target))
        finalized += 1
    write_json_atomic(
        manifest,
        {
            "version": 2,
            "status": "blocked" if conflicts else "checkpointed",
            "files": files,
            "conflicts": conflicts,
        },
    )
    return {
        "status": "blocked" if conflicts else "checkpointed",
        "file_count": finalized,
        "conflicts": conflicts,
    }


def combine_governance_result(capability_result: str, governance: dict[str, object] | None) -> str:
    if governance is None or governance.get("result") in {"FULL", "READY"}:
        return capability_result
    if capability_result == "BLOCKED":
        return "BLOCKED"
    return "PARTIAL"


def machine_result_status(statuses: list[str], changed_units: int) -> str:
    normalized = [status for status in statuses if status]
    if not normalized:
        return "BLOCKED"
    blocked = any(status in {"BLOCKED", "PARTIAL"} for status in normalized)
    successful = any(status in {"FULL", "READY", "PENDING_TRUST", "DRY_RUN"} for status in normalized)
    if blocked:
        return "PARTIAL" if successful or changed_units else "BLOCKED"
    if any(status == "PENDING_TRUST" for status in normalized):
        return "PENDING_TRUST"
    if all(status == "READY" for status in normalized):
        return "READY"
    if all(status == "DRY_RUN" for status in normalized):
        return "DRY_RUN"
    return "FULL"


def machine_child_arguments(unit: InstallUnit, args: argparse.Namespace) -> list[str]:
    scopes = {scope for target_scopes in unit.scopes_by_target.values() for scope in target_scopes}
    governance_only = bool(getattr(unit, "governance_only", False))
    if governance_only:
        selected_scope = "project"
    elif len(scopes) != 1:
        raise RuntimeError(f"machine install unit has mixed scopes: {unit.key}")
    else:
        selected_scope = next(iter(scopes))
    forwarded = [
        str(Path(__file__).resolve()),
        "--tools",
        ",".join(unit.scopes_by_target) or "agents",
        "--mode",
        args.mode,
        "--path",
        str(unit.project_root),
        "--scope",
        selected_scope,
        "--json",
        "--single-project",
        "--package-root",
        str(PACKAGE_ROOT or SCRIPT_ROOT),
        "--codex-hooks",
        args.codex_hooks,
    ]
    if args.action == "preflight":
        forwarded.extend(("--preflight", "--existing-only"))
    else:
        forwarded.append("--update")
    if governance_only:
        forwarded.append("--governance-only")
    if args.force:
        forwarded.append("--force")
    if args.dry_run:
        forwarded.append("--dry-run")
    if args.allow_skills_only:
        forwarded.append("--allow-skills-only")
    if args.require_full:
        forwarded.append("--require-full")
    if args.allow_tracked_codex_hooks:
        forwarded.append("--allow-tracked-codex-hooks")
    if args.prune_shadowed:
        forwarded.append("--prune-shadowed")
    if args.no_prune_shadowed:
        forwarded.append("--no-prune-shadowed")
    if args.source_changed:
        forwarded.append("--source-changed")
    hook_rollback_journal = getattr(args, "hook_rollback_journal", None)
    if hook_rollback_journal is not None:
        forwarded.extend(("--hook-rollback-journal", str(hook_rollback_journal)))
    if unit.runtime_owners:
        forwarded.extend(
            (
                "--machine-runtime-owners",
                json.dumps(unit.runtime_owners, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
            )
        )
    return forwarded


def progress_status(result: str, check: CapabilityCheck | None = None) -> str:
    if check is not None and (check.matches_plan is False or check.version_matches_package is False):
        return "blocked"
    if check is not None and is_expected_skills_only(check, result):
        return "ok"
    if result in {"FULL", "READY", "DRY_RUN", "ACTIVE"}:
        return "ok"
    if result == CAPABILITY_ACTIVE_PARTIAL:
        return "ok" if check is not None and check.maximum == CAPABILITY_ACTIVE_PARTIAL else "failed"
    if result == CAPABILITY_SKILLS_ONLY:
        return "pending" if check is None else "failed"
    if result == CAPABILITY_PENDING_TRUST:
        return "pending"
    if result == "PARTIAL":
        return "failed"
    return "blocked"


def is_expected_skills_only_payload(check: dict[str, object]) -> bool:
    if check.get("matches_plan") is False or check.get("version_matches_package") is False:
        return False
    status = str(check.get("actual") or check.get("planned") or "")
    return status == CAPABILITY_SKILLS_ONLY and (
        check.get("fallback_accepted") is True
        or str(check.get("maximum") or "") == CAPABILITY_SKILLS_ONLY
        or str(check.get("runtime_role") or "") == "skills-only-shadow"
        or (
            str(check.get("scope") or "") == "project"
            and str(check.get("plugin_api") or "") == "scope-unsupported"
            and str(check.get("planned") or "") == CAPABILITY_SKILLS_ONLY
        )
    )


def machine_payload_checks(payload: dict[str, object]) -> dict[tuple[str, str], dict[str, object]]:
    """Return the newest per-surface check from a machine child payload."""
    checks_by_surface: dict[tuple[str, str], dict[str, object]] = {}
    reports: list[dict[str, object]] = [payload]
    for key in ("preflight", "postflight"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            reports.append(nested)
    for report in reports:
        checks = report.get("tools")
        if not isinstance(checks, list):
            continue
        for check in checks:
            if not isinstance(check, dict):
                continue
            tool = str(check.get("tool") or "").strip()
            scope = str(check.get("scope") or "").strip()
            if tool and scope:
                checks_by_surface[(scope, tool)] = check
    return checks_by_surface


def progress_status_for_payload(
    check: dict[str, object] | None,
    fallback: str,
) -> str:
    """Map one child capability check to the progress marker users see."""
    if check is None:
        return progress_status(fallback)
    status = str(check.get("actual") or check.get("planned") or fallback)
    if check.get("matches_plan") is False or check.get("version_matches_package") is False:
        return "blocked"
    if is_expected_skills_only_payload(check):
        return "ok"
    if status == CAPABILITY_ACTIVE_PARTIAL:
        return "ok" if str(check.get("maximum") or "") == CAPABILITY_ACTIVE_PARTIAL else "failed"
    if status in {CAPABILITY_ACTIVE, "FULL", "READY", "DRY_RUN"}:
        return "ok"
    if status == CAPABILITY_PENDING_TRUST:
        return "pending"
    if status == CAPABILITY_SKILLS_ONLY:
        return "failed"
    if status in {CAPABILITY_BLOCKED, CAPABILITY_SKIPPED}:
        return "blocked"
    return progress_status(fallback)


def machine_surface_details(
    check: dict[str, object] | None,
    *,
    fallback: list[str],
) -> list[str]:
    if check is None:
        return fallback
    if is_expected_skills_only_payload(check):
        return []
    status = str(check.get("actual") or check.get("planned") or "")
    details: list[str] = []
    blocker = str(check.get("blocker") or "").strip()
    remediation = str(check.get("remediation") or "").strip()
    validation_failed = check.get("matches_plan") is False or check.get("version_matches_package") is False
    if validation_failed:
        if blocker:
            details.append(blocker)
        if remediation:
            details.append(remediation)
    elif status == CAPABILITY_PENDING_TRUST:
        if remediation:
            details.append(remediation)
    elif check.get("matches_plan") is False or status in {CAPABILITY_BLOCKED, CAPABILITY_SKILLS_ONLY}:
        if blocker:
            details.append(blocker)
        if remediation:
            details.append(remediation)
    return list(dict.fromkeys(details))


def progress_event_status(status: object) -> str:
    value = str(status or "").strip().lower()
    labels = {
        "ok": human_text("OK", "完成"),
        "current": human_text("VERIFY", "验证"),
        "pending": human_text("WAIT", "等待"),
        "failed": human_text("FAIL", "失败"),
        "blocked": human_text("BLOCK", "阻断"),
    }
    return labels.get(value, display_value(str(status or "")))


def relayed_progress_detail(label: str, event_label: str, status: object) -> str:
    event_status = progress_event_status(status)
    if label == human_text("global", "全局") and event_label.startswith(f"{display_value('global')}:"):
        return f"{event_label} {event_status}".strip()
    return human_text(
        f"{label}: {event_label} {event_status}".strip(),
        f"{label}：{event_label} {event_status}".strip(),
    )


def progress_title(action: str) -> str:
    if action == "preflight":
        return human_text("Tenetora preflight", "Tenetora 预检")
    if action == "update":
        return human_text("Tenetora upgrade", "Tenetora 升级")
    return human_text("Tenetora install", "Tenetora 安装")


def machine_project_labels(units: list[InstallUnit]) -> dict[str, str]:
    projects = [unit for unit in units if unit.kind == "project"]
    if not projects:
        return {}
    max_depth = max(len(unit.project_root.parts) for unit in projects)
    for depth in range(2, max_depth + 1):
        labels = {
            unit.key: "/".join(unit.project_root.parts[-depth:])
            for unit in projects
        }
        if len(set(labels.values())) == len(labels):
            return labels
    return {unit.key: str(unit.project_root) for unit in projects}


def machine_unit_label(unit: InstallUnit, project_labels: dict[str, str] | None = None) -> str:
    if unit.kind == "global":
        return human_text("global", "全局")
    if project_labels and unit.key in project_labels:
        return project_labels[unit.key]
    parts = unit.project_root.parts
    return "/".join(parts[-2:]) if len(parts) >= 2 else str(unit.project_root)


def machine_surface_label(
    unit: InstallUnit,
    scope: str,
    target: str,
    project_labels: dict[str, str] | None = None,
) -> str:
    surface = f"{display_value(scope)}:{target}"
    return surface if unit.kind == "global" else f"{machine_unit_label(unit, project_labels)} {surface}"


def machine_failure_details(payload: dict[str, object]) -> list[str]:
    details: list[str] = []
    if payload.get("error"):
        details.append(str(payload["error"]))
    governance = payload.get("project_governance")
    if isinstance(governance, dict) and governance.get("result") == "BLOCKED":
        classification = governance.get("classification")
        classification_status = (
            classification.get("status") if isinstance(classification, dict) else "unknown"
        )
        project_path = str(
            governance.get("project_path")
            or payload.get("project_path")
            or ""
        ).strip()
        location = f" [{project_path}]" if project_path else ""
        details.append(
            f"project governance ({classification_status}){location}: "
            f"{governance.get('message') or 'migration requires review'}"
        )
    reports: list[dict[str, object]] = [payload]
    for key in ("preflight", "postflight"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            reports.append(nested)
    for report in reports:
        checks = report.get("tools")
        if not isinstance(checks, list):
            continue
        for check in checks:
            if not isinstance(check, dict):
                continue
            if is_expected_skills_only_payload(check):
                continue
            blocker = str(check.get("blocker") or "").strip()
            remediation = str(check.get("remediation") or "").strip()
            status = str(check.get("actual") or check.get("planned") or "")
            validation_failed = check.get("matches_plan") is False or check.get("version_matches_package") is False
            if status == CAPABILITY_PENDING_TRUST and not validation_failed:
                continue
            if (
                not blocker
                and str(check.get("planned")) != CAPABILITY_BLOCKED
                and str(check.get("actual")) != CAPABILITY_BLOCKED
                and check.get("matches_plan") is not False
            ):
                continue
            label = f"{check.get('scope', '?')}:{check.get('tool', '?')}"
            if blocker:
                details.append(f"{label}: {blocker}")
            if remediation:
                details.append(f"{label}: {remediation}")
    return list(dict.fromkeys(details))


def machine_follow_up_details(payload: dict[str, object]) -> list[str]:
    details: list[str] = []
    receipt = payload.get("activation_receipt")
    if isinstance(receipt, dict):
        follow_up = receipt.get("follow_up")
        if isinstance(follow_up, list):
            details.extend(str(item).strip() for item in follow_up if str(item).strip())
    postflight = payload.get("postflight")
    if isinstance(postflight, dict):
        checks = postflight.get("tools")
        if isinstance(checks, list):
            for check in checks:
                if not isinstance(check, dict) or is_expected_skills_only_payload(check):
                    continue
                status = str(check.get("actual") or "")
                remediation = str(check.get("remediation") or "").strip()
                if status == CAPABILITY_PENDING_TRUST and remediation:
                    details.append(remediation)
    return list(dict.fromkeys(details))


def run_machine_child(
    command: list[str],
    *,
    cwd: Path,
    reporter: InstallProgress,
    label: str,
) -> subprocess.CompletedProcess[str]:
    """Run one unit without pipe deadlocks and keep the TTY visibly alive."""
    reporter.detail(human_text(f"Starting {label}", f"开始处理 {label}"))
    started = time.monotonic()
    process: subprocess.Popen[str] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="tenetora-unit-events-") as event_dir, tempfile.TemporaryFile(
            mode="w+t",
            encoding="utf-8",
            errors="replace",
        ) as stdout_file, tempfile.TemporaryFile(
            mode="w+t",
            encoding="utf-8",
            errors="replace",
        ) as stderr_file:
            event_path = Path(event_dir) / "events.jsonl"
            child_command = [*command, "--events-jsonl", str(event_path)]
            event_offset = 0

            def relay_events() -> None:
                nonlocal event_offset
                if not event_path.is_file():
                    return
                with event_path.open("r", encoding="utf-8") as handle:
                    handle.seek(event_offset)
                    for raw in handle:
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if event.get("event") == "detail" and event.get("message"):
                            message = str(event["message"])
                            if reporter.verbose or message.startswith(("Applying ", "正在应用 ")):
                                reporter.detail(message)
                            else:
                                reporter.log_file.write(f"{label}: {message}")
                        elif event.get("event") == "progress" and event.get("label"):
                            event_label = str(event.get("label"))
                            reporter.detail(relayed_progress_detail(label, event_label, event.get("status")))
                    event_offset = handle.tell()

            child_environment = os.environ.copy()
            child_environment["TENETORA_OUTER_INSTALL_LOCK_HELD"] = "1"
            # Windows defaults redirected Python stdout/stderr to the active
            # code page (often GBK). The parent reads these protocol streams
            # as UTF-8, so make the child encoding explicit.
            child_environment["PYTHONIOENCODING"] = "utf-8"
            child_environment["PYTHONUTF8"] = "1"
            process = subprocess.Popen(
                child_command,
                cwd=cwd,
                env=child_environment,
                text=True,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=os.name != "nt",
                **install_lock_subprocess_kwargs(),
            )
            next_heartbeat = started + 1.0
            while process.poll() is None:
                time.sleep(0.1)
                relay_events()
                current = time.monotonic()
                if current >= next_heartbeat:
                    elapsed = max(1, int(current - started))
                    reporter.detail(
                        human_text(
                            f"Running {label} ({elapsed}s)",
                            f"正在处理 {label}（{elapsed} 秒）",
                        )
                    )
                    next_heartbeat = current + 1.0
            relay_events()
            stdout_file.seek(0)
            stderr_file.seek(0)
            return subprocess.CompletedProcess(
                child_command,
                int(process.returncode or 0),
                stdout_file.read(),
                stderr_file.read(),
            )
    except (KeyboardInterrupt, SystemExit):
        if process is not None and process.poll() is None:
            try:
                if os.name == "nt":
                    process.terminate()
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    if os.name == "nt":
                        process.kill()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        raise
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, "", f"cannot start install unit {label}: {exc}")


def write_readiness_errors(
    preflight: CapabilityReport,
    project_root: Path,
    args: argparse.Namespace,
) -> dict[tuple[str, str], str]:
    """Exercise the real write paths in dry-run mode before any target is changed."""
    errors: dict[tuple[str, str], str] = {}
    update_mode = args.action == "update" or args.existing_only
    for check in preflight.checks:
        if check.planned in {CAPABILITY_BLOCKED, CAPABILITY_SKIPPED}:
            continue
        codex_policy_managed = check.tool == "codex" and (
            check.hook_mode == "project-fallback"
            or args.codex_hooks == "off"
            or check.hooks_state == "active-native-degraded-management"
            or (
                check.hook_mode == "skills-only"
                and check.degraded_reason == "codex-marketplace-unreadable"
            )
        )
        skip_runtime = check.runtime_role == "skills-only-shadow"
        try:
            if update_mode:
                update_one(
                    check.tool,
                    check.scope,
                    project_root,
                    args.mode,
                    args.force,
                    True,
                    args.source_changed,
                    skip_native=codex_policy_managed,
                    skip_runtime=skip_runtime,
                )
            else:
                install_one(
                    check.tool,
                    check.scope,
                    project_root,
                    args.mode,
                    args.force,
                    True,
                    skip_native=codex_policy_managed,
                    skip_runtime=skip_runtime,
                )
            if skip_runtime and args.prune_shadowed:
                remove_runtime_adapter(
                    check.tool,
                    check.scope,
                    project_root,
                    True,
                    owner_scope=check.runtime_owner_scope,
                )
            for prune_scope in check.runtime_prune_scopes:
                remove_runtime_adapter(
                    check.tool,
                    prune_scope,
                    project_root,
                    True,
                    owner_scope=check.runtime_owner_scope or check.scope,
                )
        except Exception as exc:
            errors[(check.scope, check.tool)] = str(exc)
    return errors


def run_machine_plan(
    plan: MachineInstallPlan,
    args: argparse.Namespace,
    reporter: InstallProgress,
) -> tuple[dict[str, object], int]:
    units: list[dict[str, object]] = []
    statuses: list[str] = []
    changed_units = 0
    aggregate_summary = {
        "installed": 0,
        "updated": 0,
        "current": 0,
        "missing": 0,
        "conflicts": 0,
        "plugins": 0,
        "runtimes": 0,
    }
    project_labels = machine_project_labels(plan.units)
    for unit in plan.units:
        reporter.events.write(
            "unit_start",
            unit=unit.key,
            kind=unit.kind,
            project_path=str(unit.project_root),
            surface_count=sum(len(scopes) for scopes in unit.scopes_by_target.values()),
        )
        command = [sys.executable, *machine_child_arguments(unit, args)]
        unit_label = machine_unit_label(unit, project_labels)
        result = run_machine_child(
            command,
            cwd=unit.project_root,
            reporter=reporter,
            label=unit_label,
        )
        safe_stderr = redact(result.stderr.strip())
        try:
            parsed_payload = json.loads(result.stdout)
            sanitized_payload = redact_value(parsed_payload)
            payload = sanitized_payload if isinstance(sanitized_payload, dict) else {
                "result": "BLOCKED",
                "error": "child installer returned a non-object JSON payload",
            }
        except json.JSONDecodeError:
            payload = {
                "result": "BLOCKED",
                "error": redact((result.stderr or result.stdout).strip()) or f"child installer exited {result.returncode}",
            }
        status = str(payload.get("result") or payload.get("overall") or "BLOCKED")
        if result.returncode != 0 and status not in {"BLOCKED", "PARTIAL"}:
            payload["reported_result"] = status
            payload["error"] = safe_stderr or f"child installer exited {result.returncode}"
            status = "BLOCKED"
            payload["result"] = status
        failure_details = machine_failure_details(payload)
        summary = payload.get("install_summary")
        changed = 0
        if isinstance(summary, dict):
            changed = sum(int(summary.get(key, 0) or 0) for key in ("installed", "updated"))
            for key in aggregate_summary:
                aggregate_summary[key] += int(summary.get(key, 0) or 0)
        if unit.governance_only and payload.get("governance_changed") is True:
            changed += 1
        changed_units += int(changed > 0)
        statuses.append(status)
        unit_payload = unit.payload()
        unit_payload.update(
            {
                "status": status,
                "returncode": result.returncode,
                "result": payload,
                "stderr": safe_stderr,
                "failure_details": failure_details,
            }
        )
        units.append(unit_payload)
        action_lines = payload.get("actions")
        actions = [str(line) for line in action_lines] if isinstance(action_lines, list) else []
        for action in actions:
            reporter.log_file.write(f"{unit_label}: {action}")
        details = (
            [localize_status_line(action) for action in actions]
            if reporter.verbose
            else machine_follow_up_details(payload)
        )
        if safe_stderr:
            reporter.log_file.write(f"{unit_label}: {safe_stderr}")
            details.append(safe_stderr)
        details.extend(failure_details)
        checks_by_surface = machine_payload_checks(payload)
        surfaces = [
            (scope, target, machine_surface_label(unit, scope, target, project_labels))
            for target, scopes in unit.scopes_by_target.items()
            for scope in scopes
        ]
        if unit.governance_only:
            reporter.advance(
                f"{unit_label} {human_text('governance', '治理')}",
                status=progress_status_for_payload(None, status),
                details=list(dict.fromkeys(localize_detail(detail) for detail in details if detail)) or None,
            )
        unassigned_details = list(details)
        for index, (scope, target, surface_label) in enumerate(surfaces):
            check = checks_by_surface.get((scope, target))
            surface_details = machine_surface_details(check, fallback=[])
            for assigned in surface_details:
                unassigned_details = [detail for detail in unassigned_details if assigned not in detail]
            if index == len(surfaces) - 1:
                surface_details.extend(unassigned_details)
            visible_details = list(
                dict.fromkeys(localize_detail(detail) for detail in surface_details if detail)
            )
            reporter.advance(
                surface_label,
                status=progress_status_for_payload(check, status),
                details=visible_details or None,
            )
        reporter.events.write("unit_end", unit=unit.key, status=status, returncode=result.returncode)
    overall = machine_result_status(statuses, changed_units)
    payload = {
        "version": 1,
        "mode": "machine-wide-existing-only",
        "result": overall,
        "surface_count": plan.surface_count,
        "governance_count": plan.governance_count,
        "work_count": plan.work_count,
        "project_count": sum(unit.kind == "project" for unit in plan.units),
        "changed_units": changed_units,
        "stale_projects": plan.stale_projects,
        "discovered_projects": plan.discovered_projects,
        "units": units,
        "registry": str(registry_path()),
    }
    if args.action != "preflight":
        payload["install_summary"] = aggregate_summary
    if args.action == "preflight":
        exit_code = 1 if overall in {"BLOCKED", "PARTIAL"} else 0
        reporter.finish(overall, summary=f"units={len(units)} surfaces={plan.surface_count} governance={plan.governance_count}")
        return payload, exit_code
    if overall in {"FULL", "PENDING_TRUST", "DRY_RUN"}:
        reporter.finish(overall, summary=f"units={len(units)} surfaces={plan.surface_count} governance={plan.governance_count}")
        return payload, 0
    reporter.finish(overall, summary=f"units={len(units)} surfaces={plan.surface_count} governance={plan.governance_count}")
    return payload, 2 if overall == "PARTIAL" else 1


def compact_outcome_label(result: str) -> str:
    labels = {
        "READY": human_text("ready", "就绪"),
        "FULL": human_text("successful", "成功"),
        "PENDING_TRUST": human_text("successful; trust or restart required", "成功，需完成信任或重启"),
        "PARTIAL": human_text("partially failed", "部分失败"),
        "BLOCKED": human_text("failed", "失败"),
        "DRY_RUN": human_text("dry run completed", "演练完成"),
    }
    return labels.get(result, display_value(result))


def compact_result_label(result: str, *, phase: str) -> str:
    result_label = compact_outcome_label(result)
    if phase == "preflight":
        return human_text(f"Preflight: {result_label}", f"预检结果：{result_label}")
    return human_text(f"Upgrade result: {result_label}", f"升级结果：{result_label}")


def machine_summary_details(payload: dict[str, object]) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    follow_up: list[str] = []
    for unit in payload.get("units", []):
        if not isinstance(unit, dict):
            continue
        nested_result = unit.get("result")
        if isinstance(nested_result, dict):
            failures.extend(machine_failure_details(nested_result))
            follow_up.extend(machine_follow_up_details(nested_result))
        stored_failures = unit.get("failure_details")
        if isinstance(stored_failures, list):
            failures.extend(str(item).strip() for item in stored_failures if str(item).strip())
        detail = str(unit.get("stderr") or "").strip()
        if detail:
            failures.append(detail)
    return (
        list(dict.fromkeys(localize_detail(item) for item in failures if item)),
        list(dict.fromkeys(localize_detail(item) for item in follow_up if item)),
    )


def render_machine_follow_up(payload: dict[str, object]) -> str:
    _, follow_up = machine_summary_details(payload)
    if not follow_up:
        return ""
    lines = [human_text("Next action:", "后续操作：")]
    lines.extend(f"- {item}" for item in follow_up)
    return "\n".join(lines)


def render_compact_machine_result(payload: dict[str, object], *, phase: str) -> str:
    result = str(payload.get("result") or "BLOCKED")
    lines = [
        compact_result_label(result, phase=phase),
        human_text(
            f"Scope: {payload.get('project_count', 0)} project(s), {payload.get('surface_count', 0)} surface(s), {payload.get('governance_count', 0)} governance-only project(s)",
            f"范围：{payload.get('project_count', 0)} 个项目，{payload.get('surface_count', 0)} 个安装面，{payload.get('governance_count', 0)} 个仅治理项目",
        ),
    ]
    if phase != "preflight":
        install_summary = payload.get("install_summary")
        if isinstance(install_summary, dict):
            lines.append(
                human_text(
                    "Changes: "
                    f"installed={install_summary.get('installed', 0)}, "
                    f"updated={install_summary.get('updated', 0)}, "
                    f"current={install_summary.get('current', 0)}",
                    "变更："
                    f"新增={install_summary.get('installed', 0)}，"
                    f"更新={install_summary.get('updated', 0)}，"
                    f"已是最新={install_summary.get('current', 0)}",
                )
            )

    failures, follow_up = machine_summary_details(payload)
    if result in {"PARTIAL", "BLOCKED"} and failures:
        lines.append(human_text("Failure details:", "失败详情："))
        lines.extend(f"- {item}" for item in failures)
    elif result in {"FULL", "PENDING_TRUST"} and follow_up:
        lines.append(human_text("Next action:", "后续操作："))
        lines.extend(f"- {item}" for item in follow_up)

    stale = payload.get("stale_projects")
    if isinstance(stale, list) and stale:
        lines.append(
            human_text(
                f"Ignored stale registrations: {len(stale)} (see the detailed log)",
                f"已忽略失效登记：{len(stale)} 个（详见日志）",
            )
        )
    log_file = str(payload.get("log_file") or "").strip()
    if result in {"PARTIAL", "BLOCKED"} and log_file and not payload.get("suppress_log_path"):
        lines.append(human_text(f"Detailed log: {log_file}", f"详细日志：{log_file}"))
    return "\n".join(lines)


def render_machine_result(
    payload: dict[str, object],
    *,
    compact: bool = False,
    phase: str = "update",
) -> str:
    sanitized = redact_value(payload)
    if isinstance(sanitized, dict):
        payload = sanitized
    if compact:
        return render_compact_machine_result(payload, phase=phase)
    lines = [
        human_text("Tenetora machine-wide upgrade", "Tenetora 机器级升级"),
        human_text(
            f"Projects: {payload.get('project_count', 0)}; surfaces: {payload.get('surface_count', 0)}; governance-only projects: {payload.get('governance_count', 0)}",
            f"项目：{payload.get('project_count', 0)}；安装面：{payload.get('surface_count', 0)}；仅治理项目：{payload.get('governance_count', 0)}",
        ),
    ]
    for unit in payload.get("units", []):
        if not isinstance(unit, dict):
            continue
        label = human_text("global", "全局") if unit.get("kind") == "global" else str(unit.get("project_path"))
        lines.append(f"- {label}: {display_value(str(unit.get('status', 'BLOCKED')))}")
        detail = str(unit.get("stderr") or "").strip()
        nested_result = unit.get("result")
        if not detail and isinstance(nested_result, dict):
            detail = str(nested_result.get("error") or "").strip()
        if detail:
            lines.append(f"  {localize_detail(detail)}")
        failure_details = unit.get("failure_details")
        if isinstance(failure_details, list):
            for failure in failure_details:
                localized = localize_detail(str(failure))
                if localized and localized != detail:
                    lines.append(f"  {localized}")
    stale = payload.get("stale_projects")
    if isinstance(stale, list) and stale:
        lines.append(human_text("Stale registered projects:", "失效的已登记项目："))
        lines.extend(f"- {path}" for path in stale)
    lines.append(human_text(f"Overall: {payload.get('result')}", f"总体状态：{display_value(str(payload.get('result')))}"))
    install_summary = payload.get("install_summary")
    if isinstance(install_summary, dict):
        lines.append(
            human_text(
                "Install summary: "
                f"installed={install_summary.get('installed', 0)} "
                f"updated={install_summary.get('updated', 0)} "
                f"current={install_summary.get('current', 0)}",
                "安装摘要："
                f"已安装={install_summary.get('installed', 0)} "
                f"已更新={install_summary.get('updated', 0)} "
                f"已是最新={install_summary.get('current', 0)}",
            )
        )
    lines.append(human_text(f"Registry: {payload.get('registry')}", f"安装清单：{payload.get('registry')}"))
    return "\n".join(lines)


def render_machine_terminal_result(
    payload: dict[str, object],
    *,
    compact: bool,
    phase: str,
    tty: bool,
) -> str:
    result = str(payload.get("result") or "BLOCKED")
    controller_managed = os.environ.get("TENETORA_UPGRADE_CONTROLLER") == "1"
    if compact and controller_managed and result not in {"PARTIAL", "BLOCKED"}:
        rendered = render_compact_machine_result(payload, phase=phase)
        return "\n".join(rendered.splitlines()[1:])
    if compact and tty and result not in {"PARTIAL", "BLOCKED"}:
        return render_machine_follow_up(payload) if result == "PENDING_TRUST" else ""
    return render_machine_result(payload, compact=compact, phase=phase)


def render_compact_capability(report: CapabilityReport) -> str:
    overall = capability_overall(report.checks, actual=report.phase == "postflight")
    outcome = compact_outcome_label(overall)
    lines = [
        human_text(
            f"Preflight: {outcome}" if report.phase == "preflight" else f"Verification: {outcome}",
            f"预检结果：{outcome}" if report.phase == "preflight" else f"验证结果：{outcome}",
        )
    ]
    if report.tool_discovery:
        detected = [
            str(item.get("tool"))
            for item in report.tool_discovery
            if item.get("detected")
        ]
        skipped = [
            str(item.get("tool"))
            for item in report.tool_discovery
            if not item.get("detected")
        ]
        lines.append(
            human_text(
                f"Detected tools: {', '.join(detected) or '-'}; not detected/skipped: {', '.join(skipped) or '-'}",
                f"已检测工具：{', '.join(detected) or '-'}；未检测/已跳过：{', '.join(skipped) or '-'}",
            )
        )
    for check in report.checks:
        status = check.actual if report.phase == "postflight" else check.planned
        safe_fallback = is_safe_native_cli_fallback(check)
        if status is None or (is_expected_skills_only(check, status) and not safe_fallback):
            continue
        if safe_fallback:
            lines.append(
                human_text(
                    f"- {check.scope}:{check.tool}: SKILLS_ONLY; {check.remediation or 'install the host CLI, then rerun tenetora upgrade --force'}",
                    f"- {display_value(check.scope)}:{check.tool}：仅 Skills；{localize_detail(check.remediation or '请安装宿主 CLI，然后重新运行 tenetora upgrade --force')}",
                )
            )
            continue
        if report.phase == "postflight":
            if (
                check.matches_plan is not False
                and status not in {CAPABILITY_BLOCKED, CAPABILITY_PENDING_TRUST}
                and not is_partial(check, status)
            ):
                continue
            match = "yes" if check.version_matches_package else "no"
            lines.append(human_text(
                f"- {check.scope}:{check.tool} version={check.actual_version or '-'} "
                f"expected={check.expected_version or '-'} match={match} actual={check.actual or '-'}",
                f"- {display_value(check.scope)}:{check.tool} 版本={check.actual_version or '-'} "
                f"期望={check.expected_version or '-'} 匹配={'是' if match == 'yes' else '否'} 实际={display_value(check.actual or '-')}",
            ))
            if check.remediation:
                lines.append(f"  {localize_detail(check.remediation)}")
        elif check.blocker and (
            status in {CAPABILITY_BLOCKED, CAPABILITY_PENDING_TRUST}
            or is_partial(check, status)
        ):
            lines.append(f"- {display_value(check.scope)}:{check.tool}: {localize_detail(check.blocker)}")
            if check.remediation:
                lines.append(f"  {localize_detail(check.remediation)}")
    return "\n".join(lines)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--help", action="help", help="Show this help message and exit.")
    parser.add_argument(
        "-t",
        "--tools",
        default="auto",
        help="auto, all, or comma-separated tools: agents,codex,claude,cursor,opencode,pi,zcode",
    )
    parser.add_argument(
        "-m",
        "--mode",
        choices=("auto", "symlink", "copy"),
        default="auto",
        help="Install mode. auto uses symlink except copy on Windows.",
    )
    scope_group = parser.add_mutually_exclusive_group()
    scope_group.add_argument(
        "-g",
        "--global",
        dest="scope_flag",
        action="store_const",
        const="global",
        help="Install globally, or filter an update to existing global surfaces",
    )
    scope_group.add_argument(
        "-i",
        "--in-project",
        dest="scope_flag",
        action="store_const",
        const="project",
        help="Install in <project-dir>, or filter an update to existing project surfaces",
    )
    scope_group.add_argument(
        "-b",
        "--both",
        dest="scope_flag",
        action="store_const",
        const="both",
        help="Install both scopes, or filter an update across both existing scopes",
    )
    scope_group.add_argument(
        "-s",
        "--scope",
        choices=("global", "project", "both"),
        default=None,
        help="Install scope, or an existing-surface filter for update mode.",
    )
    parser.add_argument(
        "-p",
        "--path",
        default=".",
        type=existing_project_path,
        metavar="<project-dir>",
        help="Project directory for --scope project or --scope both. Defaults to the current directory.",
    )
    parser.add_argument("-f", "--force", action="store_true", help="Replace an existing installed skill")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing")
    parser.add_argument("--json", action="store_true", help="Print machine-readable preflight and result JSON")
    parser.add_argument(
        "--codex-hooks",
        choices=("auto", "native", "project", "off"),
        default="auto",
        help="Codex hook mode: auto, native plugin, project fallback, or off.",
    )
    parser.add_argument(
        "--allow-tracked-codex-hooks",
        action="store_true",
        help="Allow structured updates to a Git-tracked .codex/hooks.json for this project.",
    )
    parser.add_argument(
        "--prune-shadowed",
        action="store_true",
        help="Remove only unchanged Tenetora managed runtime adapters shadowed by the selected effective runtime.",
    )
    parser.add_argument(
        "--no-prune-shadowed",
        action="store_true",
        help="Disable automatic pruning of unchanged Tenetora managed shadow runtime adapters during upgrade.",
    )
    fallback_group = parser.add_mutually_exclusive_group()
    fallback_group.add_argument(
        "--allow-skills-only",
        action="store_true",
        help="Explicitly accept marketplace/config degradation; missing host CLIs defer native activation automatically",
    )
    fallback_group.add_argument(
        "--require-full",
        action="store_true",
        help="Block before writes when a selected tool can only reach a lower capability tier",
    )
    parser.add_argument(
        "--source-changed",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--package-root",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--quiet-preflight",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--existing-only",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--all-existing", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--auto-discover", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--hook-rollback-journal", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--single-project", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--governance-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--machine-runtime-owners",
        type=runtime_owner_map,
        default={},
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--progress",
        choices=("auto", "always", "never"),
        default="auto",
        help="Render terminal progress automatically, always, or never.",
    )
    parser.add_argument("--compact", action="store_true", help="Show bounded progress and summary output instead of every action.")
    parser.add_argument("--verbose", action="store_true", help="Print detailed action lines in addition to progress output.")
    parser.add_argument("--events-jsonl", type=Path, default=None, help="Append structured installer events as JSON Lines.")
    parser.add_argument("--log-file", type=Path, default=None, help="Write redacted detailed installer diagnostics to this file.")
    action_group = parser.add_mutually_exclusive_group()
    action_group.add_argument(
        "--update",
        dest="action",
        action="store_const",
        const="update",
        help="Refresh existing Tenetora installation surfaces without creating missing tools or scopes",
    )
    action_group.add_argument(
        "--status",
        dest="action",
        action="store_const",
        const="status",
        help="Report install state without writing",
    )
    action_group.add_argument(
        "--preflight",
        dest="action",
        action="store_const",
        const="preflight",
        help="Inspect installation capability without writing",
    )
    action_group.add_argument(
        "--discover-existing",
        dest="action",
        action="store_const",
        const="discover-existing",
        help=argparse.SUPPRESS,
    )
    action_group.add_argument(
        "--snapshot-hooks",
        dest="action",
        action="store_const",
        const="snapshot-hooks",
        help=argparse.SUPPRESS,
    )
    action_group.add_argument(
        "--finalize-hook-journal",
        dest="action",
        action="store_const",
        const="finalize-hook-journal",
        help=argparse.SUPPRESS,
    )
    action_group.add_argument(
        "--converge-hooks",
        dest="action",
        action="store_const",
        const="converge-hooks",
        help=argparse.SUPPRESS,
    )
    action_group.add_argument(
        "--restore-hooks",
        dest="action",
        action="store_const",
        const="restore-hooks",
        help=argparse.SUPPRESS,
    )
    parser.set_defaults(action="install")
    args = parser.parse_args(argv)

    reporter: InstallProgress | None = None
    codex_marketplace_repair: CodexMarketplaceRepairTransaction | None = None

    def finalize_codex_marketplace_repair(commit: bool) -> None:
        if codex_marketplace_repair is None or not codex_marketplace_repair.active:
            return
        if commit:
            codex_marketplace_repair.commit()
        else:
            codex_marketplace_repair.rollback()

    try:
        active_home = validate_managed_home_path(machine_home(), label="Tenetora canonical home")
        default_home = validate_managed_home_path(default_machine_home(), label="default Tenetora home")
        migration_enabled = bool(
            os.environ.get("TENETORA_HOME")
            or os.environ.get("TENETORA_OUTER_INSTALLER")
        )
        migration_deferred = os.environ.get("TENETORA_DEFER_MACHINE_HOME_MIGRATION") == "1"
        migration = (
            {"status": "deferred"}
            if migration_deferred
            else
            migrate_legacy_machine_home(
                canonical=active_home,
                legacy=default_legacy_machine_home(),
                dry_run=True,
            )
            if migration_enabled and active_home == default_home
            else {"status": "custom-home"}
        )
        if migration.get("status") == "blocked-canonical-conflict":
            raise RuntimeError(
                "Cannot migrate the proven legacy machine home because ~/.tenetora contains unrelated data"
            )
        if args.events_jsonl is not None:
            args.events_jsonl = Path(os.path.abspath(str(args.events_jsonl.expanduser())))
        if args.log_file is not None:
            args.log_file = Path(os.path.abspath(str(args.log_file.expanduser())))
        if (
            args.events_jsonl is not None
            and args.log_file is not None
            and args.events_jsonl == args.log_file
        ):
            raise ValueError("--events-jsonl and --log-file must use different paths")
        configure_package_root(args.package_root)
        explicit_scope = args.scope_flag or args.scope
        project_root = args.path
        if args.action == "restore-hooks":
            if args.hook_rollback_journal is None:
                raise ValueError("--restore-hooks requires --hook-rollback-journal")
            payload = restore_commit_hook_journal(args.hook_rollback_journal)
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(
                    f"Tenetora managed commit hooks: {payload['status']}; "
                    f"restored={len(payload['restored'])} conflicts={len(payload['conflicts'])}"
                )
            return 1 if payload["status"] == "blocked" else 0
        if args.action == "snapshot-hooks":
            if args.hook_rollback_journal is None:
                raise ValueError("--snapshot-hooks requires --hook-rollback-journal")
            payload = snapshot_registered_project_commit_hooks(
                project_root,
                all_existing=args.all_existing,
                rollback_journal=args.hook_rollback_journal,
            )
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(
                    f"Tenetora managed commit hooks: {payload['status']}; "
                    f"projects={payload['project_count']} files={payload['file_count']}"
                )
            return 0
        if args.action == "finalize-hook-journal":
            if args.hook_rollback_journal is None:
                raise ValueError("--finalize-hook-journal requires --hook-rollback-journal")
            payload = finalize_commit_hook_journal(args.hook_rollback_journal)
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(
                    f"Tenetora managed commit hooks: {payload['status']}; "
                    f"files={payload['file_count']}"
                )
            return 1 if payload["status"] == "blocked" else 0
        if args.action == "converge-hooks":
            payload = converge_registered_project_commit_hooks(
                project_root,
                all_existing=args.all_existing,
                dry_run=args.dry_run,
                rollback_journal=args.hook_rollback_journal,
            )
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(
                    f"Tenetora managed commit hooks: {payload['status']}; "
                    f"projects={payload['project_count']} updated={payload['updated_hook_count']} "
                    f"state-migrated={payload['migrated_state_count']} "
                    f"runtime-ignores={payload['migrated_runtime_ignore_count']}"
                )
            return 2 if payload["status"] == "deferred" else 0
        existing_only = args.action == "update" or args.existing_only or args.action == "discover-existing"
        if args.governance_only:
            if not args.single_project or not existing_only:
                raise ValueError("--governance-only requires a single-project update or preflight")
            dry_governance = args.action == "preflight" or args.dry_run
            governance_result = project_governance_migration(project_root, dry_run=dry_governance)
            if governance_result is None:
                payload = {
                    "result": "BLOCKED",
                    "error": "governance-only project has no .tenetora or .harness directory",
                    "project_path": str(project_root),
                    "governance_changed": False,
                    "install_summary": {
                        "installed": 0, "updated": 0, "current": 0, "missing": 0,
                        "conflicts": 0, "plugins": 0, "runtimes": 0,
                    },
                }
            else:
                result_status = str(governance_result.get("result") or "BLOCKED")
                governance_changed = bool(
                    not dry_governance
                    and governance_result.get("status") in {"migrated", "repaired"}
                )
                payload = {
                    "result": result_status,
                    "project_path": str(project_root),
                    "project_governance": governance_result,
                    "governance_changed": governance_changed,
                    "install_summary": {
                        "installed": 0, "updated": 0, "current": 0, "missing": 0,
                        "conflicts": 0, "plugins": 0, "runtimes": 0,
                    },
                    "actions": [
                        status_line(
                            "MIGRATE" if result_status in {"FULL", "READY"} else "CONFLICT",
                            "tenetora",
                            "project",
                            "governance",
                            str(governance_result.get("message") or governance_result.get("status") or "unknown"),
                        )
                    ],
                }
                if result_status == "FULL" and not dry_governance:
                    upsert_project(
                        project_root,
                        {},
                        governance=canonical_governance_registration(project_root),
                    )
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print("\n".join(str(item) for item in payload.get("actions", [])))
            return 0 if payload["result"] in {"FULL", "READY", "DRY_RUN"} else 1
        requested_targets = existing_update_targets(args.tools) if existing_only else parse_tools(args.tools)
        explicit_project_scopes = (
            project_scopes_for(requested_targets, project_root)
            if explicit_scope in {"project", "both"}
            else {}
        )
        explicit_governance_present = project_governance_surface_present(project_root)
        if args.action == "discover-existing" and explicit_governance_present:
            governance_registration = governance_registration_for_project(project_root)
            explicit_governance_present = bool(
                governance_registration is not None
                and governance_registration.get("classification") != "canonical-invalid"
            )
        explicit_governance_only = bool(
            explicit_scope in {"project", "both"}
            and not explicit_project_scopes
            and explicit_governance_present
        )
        if (
            existing_only
            and args.action != "discover-existing"
            and explicit_scope in {"project", "both"}
            and not explicit_project_scopes
            and not explicit_governance_only
        ):
            raise RuntimeError(
                "No existing Tenetora installation surface matched the explicit project target; "
                "global matches cannot satisfy --in-project/--both. Run tenetora install to create "
                "a project installation, or verify --path."
            )
        machine_scoped_existing = bool(args.all_existing or explicit_governance_only)
        if existing_only and machine_scoped_existing and not args.single_project:
            discovery_entries: dict[Path, dict[str, object]] = {}
            if args.auto_discover:
                discovery_entries = auto_discovered_project_entries(project_root, include_home=True)
            elif args.action == "discover-existing":
                discovery_entries = nearby_project_entries(project_root)
            plan = machine_existing_plan(
                requested_targets,
                explicit_scope,
                project_root,
                discovered_entries=discovery_entries,
            )
            if args.action == "discover-existing":
                if not args.dry_run:
                    for discovered_project, item in discovery_entries.items():
                        governance = item.get("governance") if isinstance(item.get("governance"), dict) else None
                        upsert_project(
                            discovered_project,
                            dict(item.get("surfaces", {})),
                            governance=governance,
                        )
                payload = plan.payload()
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    print(render_machine_result({**payload, "result": "READY", "registry": str(registry_path())}))
                return 0
            if not plan.units:
                raise RuntimeError(
                    "No existing Tenetora installation surface matched the requested tools and scope; "
                    "run tenetora install to create a new installation"
                )
            args.prune_shadowed = bool(args.prune_shadowed or not args.no_prune_shadowed)
            log_path = args.log_file
            reporter = InstallProgress(
                total=plan.work_count,
                title=progress_title(args.action),
                mode="never" if args.json else args.progress,
                compact=args.compact,
                verbose=args.verbose,
                event_path=args.events_jsonl,
                log_path=log_path,
            )
            reporter.events.write(
                "plan",
                mode="machine-wide-existing-only",
                projects=sum(unit.kind == "project" for unit in plan.units),
                surfaces=plan.surface_count,
                governance_projects=plan.governance_count,
                discovered_projects=len(plan.discovered_projects),
                stale_projects=len(plan.stale_projects),
            )
            payload, exit_code = run_machine_plan(plan, args, reporter)
            payload["log_file"] = str(log_path) if log_path else None
            payload["suppress_log_path"] = os.environ.get("TENETORA_OUTER_INSTALLER") == "1"
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                rendered = render_machine_terminal_result(
                    payload,
                    compact=args.compact,
                    phase=args.action,
                    tty=reporter.tty,
                )
                if rendered:
                    print(rendered)
            return exit_code
        scopes_by_target = (
            existing_target_scopes_for(requested_targets, explicit_scope, project_root)
            if existing_only
            else target_scopes_for(requested_targets, explicit_scope)
        )
        if args.action == "discover-existing":
            payload = existing_plan_payload(scopes_by_target)
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"Existing Tenetora surfaces: {payload['surface_count']}")
                for target, target_scopes in scopes_by_target.items():
                    print(f"- {target}: {', '.join(target_scopes)}")
            return 0
        if existing_only and not scopes_by_target:
            raise RuntimeError(
                "No existing Tenetora installation surface matched the requested tools and scope; "
                "run tenetora install to create a new installation"
            )
        targets = list(scopes_by_target)
        scopes = list(dict.fromkeys(scope for target in targets for scope in scopes_by_target[target]))
        args.resolved_targets = targets
        args.resolved_scopes_by_target = scopes_by_target
        combined = InstallReport()
        if args.action == "status":
            for target in targets:
                for selected_scope in scopes_by_target[target]:
                    combined.extend(status_one(target, selected_scope, project_root))
            print(combined.text())
            return 0

        log_path = args.log_file
        reporter = InstallProgress(
            total=sum(len(target_scopes) for target_scopes in scopes_by_target.values()),
            title=progress_title(args.action),
            mode="never" if args.json else args.progress,
            compact=args.compact,
            verbose=args.verbose,
            event_path=args.events_jsonl,
            log_path=log_path,
        )
        reporter.events.write(
            "plan",
            mode="single-project",
            project_path=str(project_root),
            surfaces=sum(len(target_scopes) for target_scopes in scopes_by_target.values()),
        )

        preflight = build_preflight(
            targets,
            scopes,
            project_root,
            args.codex_hooks,
            args.allow_tracked_codex_hooks,
            bool(args.prune_shadowed or (args.action == "update" and not args.no_prune_shadowed)),
            scopes_by_target,
            args.machine_runtime_owners,
        )
        if (
            args.action in {"install", "update"}
            and not args.dry_run
            and args.codex_hooks in {"auto", "native"}
            and "codex" in targets
            and "global" in scopes_by_target.get("codex", [])
        ):
            marketplace_repair_assessment = assess_codex_marketplace_path_repair()
            if marketplace_repair_assessment.repairable:
                codex_marketplace_repair = begin_codex_marketplace_path_repair(
                    marketplace_repair_assessment
                )
                combined.lines.append(
                    status_line(
                        "MIGRATE",
                        "codex",
                        "global",
                        "marketplace",
                        "normalized the verified Codex official marketplace source path before native plugin installation",
                    )
                )
                reporter.log_file.write(combined.lines[-1])
                preflight = build_preflight(
                    targets,
                    scopes,
                    project_root,
                    args.codex_hooks,
                    args.allow_tracked_codex_hooks,
                    bool(args.prune_shadowed or (args.action == "update" and not args.no_prune_shadowed)),
                    scopes_by_target,
                    args.machine_runtime_owners,
                )
        readiness_errors = write_readiness_errors(preflight, project_root, args)
        for check in preflight.checks:
            readiness_error = readiness_errors.get((check.scope, check.tool))
            if readiness_error is None:
                continue
            check.planned = CAPABILITY_BLOCKED
            check.blocker = f"write preflight failed: {readiness_error}"
            check.remediation = (
                "Review the existing destination, then remove it or rerun with --force only when replacement is intended"
            )
        policy_ready = resolve_fallback_policy(preflight, args)
        hard_blocked = any(check.planned == CAPABILITY_BLOCKED for check in preflight.checks)
        governance_preflight = (
            project_governance_migration(project_root, dry_run=True)
            if existing_only
            and any("project" in target_scopes for target_scopes in scopes_by_target.values())
            and project_governance_surface_present(project_root)
            else None
        )
        governance_blocked = bool(
            governance_preflight is not None
            and governance_preflight.get("result") == "BLOCKED"
        )
        if args.action == "preflight":
            for check in preflight.checks:
                details = (
                    []
                    if is_expected_skills_only(check, check.planned)
                    else [detail for detail in (check.blocker, check.remediation) if detail]
                )
                reporter.advance(
                    f"{display_value(check.scope)}:{check.tool}",
                    status=progress_status(check.planned, check),
                    details=details,
                )
            preflight_status = combine_governance_result(
                capability_overall(preflight.checks, actual=False),
                governance_preflight,
            )
            reporter.finish(preflight_status, summary=f"surfaces={len(preflight.checks)}")
            if args.json:
                payload = preflight.payload()
                payload["result"] = preflight_status
                if governance_preflight is not None:
                    payload["project_governance"] = governance_preflight
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            elif args.compact:
                print(render_compact_capability(preflight))
            else:
                print(render_capability_report(preflight))
            if governance_blocked:
                print(
                    localize_detail(str(governance_preflight.get("message") or "project governance migration requires review")),
                    file=sys.stderr,
                )
            return 1 if hard_blocked or governance_blocked else 0
        if readiness_errors:
            finalize_codex_marketplace_repair(False)
            for (scope, target), detail in readiness_errors.items():
                reporter.advance(
                    f"{display_value(scope)}:{target}",
                    status="blocked",
                    details=[detail],
                )
            reporter.finish("BLOCKED", summary="write preflight failed")
            if args.json:
                print(
                    json.dumps(
                        {
                            "preflight": preflight.payload(),
                            "result": "BLOCKED",
                            "write_readiness_errors": [
                                {"scope": scope, "tool": target, "error": detail}
                                for (scope, target), detail in readiness_errors.items()
                            ],
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                print(render_compact_capability(preflight) if args.compact else render_capability_report(preflight))
                for detail in readiness_errors.values():
                    print(localize_detail(detail), file=sys.stderr)
            return 1
        if governance_blocked:
            finalize_codex_marketplace_repair(False)
            reporter.finish("BLOCKED", summary="project governance migration requires review")
            if args.json:
                print(
                    json.dumps(
                        {
                            "preflight": preflight.payload(),
                            "project_governance": governance_preflight,
                            "result": "BLOCKED",
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                print(
                    localize_detail(str(governance_preflight.get("message") or "project governance migration requires review")),
                    file=sys.stderr,
                )
            return 1
        if args.require_full and (hard_blocked or governance_blocked or not policy_ready):
            finalize_codex_marketplace_repair(False)
            reporter.finish("BLOCKED", summary="blocked before writes by --require-full")
            if args.json:
                print(json.dumps({"preflight": preflight.payload(), "result": "BLOCKED"}, ensure_ascii=False, indent=2))
            else:
                print(render_capability_report(preflight))
                print(
                    human_text(
                        "\nInstallation blocked before writes by --require-full.",
                        "\n--require-full 已在写入前阻断安装。",
                    ),
                    file=sys.stderr,
                )
            return 1
        if not args.json and not args.quiet_preflight:
            print(render_capability_report(preflight))
            print()

        checks = {(check.scope, check.tool): check for check in preflight.checks}
        action_details: dict[tuple[str, str], list[str]] = {}
        codex_project_policy_applied = False
        for target in targets:
            for selected_scope in scopes_by_target[target]:
                check = checks[(selected_scope, target)]
                if check.planned in {CAPABILITY_BLOCKED, CAPABILITY_SKIPPED}:
                    combined.lines.append(
                        status_line(check.planned, target, selected_scope, "capability", check.blocker or "skipped by preflight")
                    )
                    action_details[(selected_scope, target)] = [
                        detail for detail in (check.blocker, check.remediation) if detail
                    ]
                    reporter.advance(
                        f"{display_value(selected_scope)}:{target}",
                        status="blocked" if check.planned == CAPABILITY_BLOCKED else "current",
                        details=[localize_detail(detail) for detail in action_details[(selected_scope, target)]],
                    )
                    continue
                reporter.detail(
                    human_text(
                        f"Applying {selected_scope}:{target}",
                        f"正在应用 {display_value(selected_scope)}:{target}",
                    )
                )
                line_start = len(combined.lines)
                codex_policy_managed = target == "codex" and (
                    check.hook_mode == "project-fallback"
                    or args.codex_hooks == "off"
                    or check.hooks_state == "active-native-degraded-management"
                    or (
                        check.hook_mode == "skills-only"
                        and check.degraded_reason == "codex-marketplace-unreadable"
                    )
                )
                skip_runtime = check.runtime_role == "skills-only-shadow"
                if args.action == "update":
                    combined.extend(
                        update_one(
                            target,
                            selected_scope,
                            project_root,
                            args.mode,
                            args.force,
                            args.dry_run,
                            args.source_changed,
                            skip_native=codex_policy_managed,
                            skip_runtime=skip_runtime,
                        )
                    )
                else:
                    combined.extend(
                        install_one(
                            target,
                            selected_scope,
                            project_root,
                            args.mode,
                            args.force,
                            args.dry_run,
                            skip_native=codex_policy_managed,
                            skip_runtime=skip_runtime,
                        )
                    )
                if skip_runtime and args.prune_shadowed:
                    combined.extend(
                        remove_runtime_adapter(
                            target,
                            selected_scope,
                            project_root,
                            args.dry_run,
                            owner_scope=check.runtime_owner_scope,
                        )
                    )
                for prune_scope in check.runtime_prune_scopes:
                    combined.extend(
                        remove_runtime_adapter(
                            target,
                            prune_scope,
                            project_root,
                            args.dry_run,
                            owner_scope=check.runtime_owner_scope or selected_scope,
                        )
                    )
                if (
                    target == "codex"
                    and selected_scope == "global"
                    and check.runtime_role == "native-effective-prune-project"
                    and not codex_project_policy_applied
                ):
                    combined.extend(
                        apply_codex_project_hook_policy(
                            project_root,
                            policy="off",
                            allow_tracked=args.allow_tracked_codex_hooks,
                            dry_run=args.dry_run,
                        )
                    )
                    codex_project_policy_applied = True
                if (
                    target == "codex"
                    and args.codex_hooks == "off"
                    and not codex_project_policy_applied
                ):
                    combined.extend(
                        apply_codex_project_hook_policy(
                            project_root,
                            policy="off",
                            allow_tracked=args.allow_tracked_codex_hooks,
                            dry_run=args.dry_run,
                        )
                    )
                    codex_project_policy_applied = True
                if target == "codex" and check.hooks_state == "active-native-degraded-management":
                    combined.lines.append(
                        status_line(
                            "CURRENT",
                            "codex",
                            selected_scope,
                            "hooks",
                            "native hooks are active; marketplace management remains unavailable",
                        )
                    )
                elif (
                    target == "codex"
                    and check.hook_mode == "skills-only"
                    and check.degraded_reason == "codex-marketplace-unreadable"
                ):
                    action = (
                        "project fallback was not enabled; run tenetora install --path . --tools codex --codex-hooks project"
                        if check.hooks_state == "project-fallback-available"
                        else (check.remediation or "restore Codex hook inventory before enabling project fallback")
                    )
                    combined.lines.append(
                        status_line(
                            "ACTION",
                            "codex",
                            selected_scope,
                            "hooks",
                            action,
                        )
                    )
                elif (
                    target == "codex"
                    and check.hook_mode == "project-fallback"
                    and not codex_project_policy_applied
                ):
                    combined.extend(
                        apply_codex_project_hook_policy(
                            project_root,
                            policy="project",
                            allow_tracked=args.allow_tracked_codex_hooks,
                            dry_run=args.dry_run,
                        )
                    )
                    codex_project_policy_applied = True

                action_details[(selected_scope, target)] = combined.lines[line_start:]
                for action in action_details[(selected_scope, target)]:
                    reporter.log_file.write(action)
                reporter.advance(
                    f"{display_value(selected_scope)}:{target}",
                    status="current" if not args.dry_run else "ok",
                    details=(
                        [localize_status_line(action) for action in action_details[(selected_scope, target)]]
                        if reporter.verbose
                        else None
                    ),
                )

        governance_result = None
        if governance_preflight is not None:
            governance_result = project_governance_migration(project_root, dry_run=args.dry_run)
            governance_status = str(governance_result.get("status") or "unknown")
            governance_message = str(governance_result.get("message") or "")
            combined.lines.append(
                status_line(
                    "MIGRATE" if governance_result.get("result") in {"FULL", "READY"} else "CONFLICT",
                    "tenetora",
                    "project",
                    "governance",
                    f"{governance_status}: {governance_message}",
                )
            )
            reporter.log_file.write(combined.lines[-1])

        commit_hook_result: dict[str, object] | None = None
        if (
            existing_only
            and args.hook_rollback_journal is None
            and any("project" in target_scopes for target_scopes in scopes_by_target.values())
        ):
            runtime_ignore_result = converge_project_runtime_state_ignores(
                project_root,
                dry_run=args.dry_run,
            )
            commit_hook_result = converge_project_commit_hooks(project_root, dry_run=args.dry_run)
            commit_hook_result["runtime_state_ignores"] = runtime_ignore_result
            if runtime_ignore_result.get("added"):
                combined.lines.append(
                    status_line(
                        "MIGRATE",
                        "tenetora",
                        "project",
                        "runtime-state",
                        "added volatile alignment session/history/lock exclusions to .tenetora/.gitignore",
                    )
                )
                reporter.log_file.write(combined.lines[-1])
            updated_hooks = commit_hook_result.get("updated")
            if isinstance(updated_hooks, list) and updated_hooks:
                combined.lines.append(
                    status_line(
                        "MIGRATE",
                        "tenetora",
                        "project",
                        "git-hooks",
                        f"rewrote managed hooks to the canonical Tenetora shim: {', '.join(str(item) for item in updated_hooks)}",
                    )
                )
                reporter.log_file.write(combined.lines[-1])

        postflight = None if args.dry_run else build_postflight(preflight, project_root)
        if postflight is not None:
            for check in postflight.checks:
                reporter.resolve(
                    f"{display_value(check.scope)}:{check.tool}",
                    status=progress_status(check.actual or CAPABILITY_BLOCKED, check),
                    details=[
                        localize_detail(detail)
                        for detail in (check.blocker, check.remediation)
                        if detail
                        and not is_expected_skills_only(check, check.actual or check.planned)
                    ],
                )
        if postflight is not None and any("project" in target_scopes for target_scopes in scopes_by_target.values()):
            # Persist discovery only after the requested project surfaces verify, and
            # before reporting success so registry failures cannot follow a FULL result.
            verified_project_targets = sorted({
                check.tool
                for check in postflight.checks
                if check.scope == "project"
                and check.actual != CAPABILITY_BLOCKED
                and check.version_matches_package is True
            })
            if verified_project_targets:
                register_project_surfaces(project_root, verified_project_targets)
        if postflight is not None and any("global" in target_scopes for target_scopes in scopes_by_target.values()):
            verified_global_targets = sorted({
                check.tool
                for check in postflight.checks
                if check.scope == "global"
                and check.actual != CAPABILITY_BLOCKED
                and check.version_matches_package is True
            })
            if verified_global_targets:
                register_global_surfaces(verified_global_targets)
        capability_result = (
            capability_overall(postflight.checks, actual=True)
            if postflight is not None
            else "DRY_RUN"
        )
        result_status = combine_governance_result(capability_result, governance_result)
        machine_home_result: dict[str, object] | None = None
        if (
            result_status not in {"BLOCKED", "DRY_RUN"}
            and not args.dry_run
            and migration_enabled
            and not migration_deferred
            and active_home == default_home
        ):
            machine_home_result = migrate_legacy_machine_home(
                canonical=active_home,
                legacy=default_legacy_machine_home(),
                dry_run=False,
            )
            if machine_home_result.get("status") == "blocked-canonical-conflict":
                raise RuntimeError(
                    "Canonical plugin installation succeeded, but the proven legacy machine home could not be migrated "
                    "because ~/.tenetora contains unrelated data"
                )
            if machine_home_result.get("status") in {"migrated", "merged", "normalized"}:
                combined.lines.append(
                    status_line(
                        "MIGRATE",
                        "tenetora",
                        "global",
                        "machine-home",
                        f"{machine_home_result.get('status')} legacy machine home after runtime verification",
                    )
                )
        finalize_codex_marketplace_repair(
            result_status not in {"BLOCKED", "DRY_RUN"}
            and not hard_blocked
            and not governance_blocked
            and policy_ready
        )
        reporter.finish(result_status, summary=combined.summary_text())
        if args.json:
            payload: dict[str, object] = {
                "preflight": preflight.payload(),
                "actions": combined.lines,
                "install_summary": asdict(combined),
            }
            if postflight is not None:
                payload["postflight"] = postflight.payload()
                payload["result"] = result_status
                if payload["result"] != "BLOCKED":
                    payload["activation_receipt"] = activation_receipt_payload(postflight)
            else:
                payload["result"] = result_status
            if governance_result is not None:
                payload["project_governance"] = governance_result
            if commit_hook_result is not None:
                payload["commit_hook_convergence"] = commit_hook_result
            if machine_home_result is not None:
                payload["machine_home_migration"] = machine_home_result
            if codex_marketplace_repair is not None:
                payload["codex_marketplace_repair"] = codex_marketplace_repair.payload()
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            suppress_tty_success = (
                args.compact
                and reporter.tty
                and result_status not in {"PARTIAL", "BLOCKED"}
            )
            if not suppress_tty_success:
                print(combined.text() if not args.compact else combined.summary_text())
            if postflight is not None:
                if not suppress_tty_success:
                    if not args.compact or result_status == "BLOCKED":
                        print()
                        print(render_capability_report(postflight))
                    else:
                        print()
                        print(render_compact_capability(postflight))
                if capability_overall(postflight.checks, actual=True) != "BLOCKED":
                    receipt = (
                        render_compact_activation_receipt(postflight)
                        if args.compact
                        else render_activation_receipt(postflight)
                    )
                    if receipt:
                        print()
                        print(receipt)
        if result_status == "BLOCKED":
            print(
                human_text(
                    "Installation incomplete: post-install capability or project governance verification is BLOCKED.",
                    "安装未完成：安装后能力或项目治理验证状态为已阻断。",
                ),
                file=sys.stderr,
            )
            return 1
        if hard_blocked or not policy_ready:
            print(
                human_text(
                    "Installation incomplete: review the BLOCKED preflight targets and remediation above.",
                    "安装未完成：请检查上方已阻断的预检目标和处理建议。",
                ),
                file=sys.stderr,
            )
            return 1
        if result_status == "PARTIAL":
            return 2
    except Exception as exc:
        safe_error = redact(str(exc))
        try:
            finalize_codex_marketplace_repair(False)
        except Exception as rollback_exc:
            safe_error = f"{safe_error}; automatic Codex marketplace repair rollback failed: {redact(str(rollback_exc))}"
        if reporter is not None:
            reporter.log_file.write(safe_error)
            if reporter.fail_unresolved(localize_detail(safe_error)) == 0:
                reporter.detail(localize_detail(safe_error))
            reporter.finish("BLOCKED", summary=safe_error)
        elif args.log_file is not None or args.events_jsonl is not None:
            try:
                write_failure_artifacts(
                    args.events_jsonl,
                    args.log_file,
                    title=progress_title(args.action),
                    phase=args.action,
                    message=safe_error,
                )
            except Exception as diagnostic_exc:
                safe_error = (
                    f"{safe_error}; failure diagnostics unavailable: "
                    f"{redact(str(diagnostic_exc))}"
                )
        print(
            human_text(safe_error, f"安装失败：{localize_detail(safe_error)}"),
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Serialize machine-level install mutations while allowing child units to run."""

    with install_transaction_lock():
        return _main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
