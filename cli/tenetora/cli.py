"""Command line entry point for Tenetora."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

from .brand import (
    COMMAND_NAME,
    PRODUCT_NAME,
    invoked_command,
    machine_home,
    normalize_environment,
    read_language_preference,
)

normalize_environment()

from . import __version__
from .commands import (
    add_diagnostics_parser,
    add_doctor_parser,
    add_install_parser,
    add_preflight_parser,
    add_setup_parser,
    add_status_parser,
    add_update_parser,
)
from .completion import run_completion
from .update_hint import read_hint


def discover_source_paths() -> tuple[Path, Path]:
    for candidate in Path(__file__).resolve().parents:
        packaged_skill = candidate / "skills" / "tenetora"
        if (packaged_skill / "SKILL.md").is_file():
            return candidate, packaged_skill
        if candidate.name == "tenetora" and (candidate / "SKILL.md").is_file():
            return candidate, candidate
    fallback = Path(__file__).resolve().parents[2]
    canonical = fallback / "skills" / "tenetora"
    return fallback, canonical


REPO_ROOT, SKILL_DIR = discover_source_paths()
SCRIPT_DIR = SKILL_DIR / "scripts"
LEGACY_COMMANDS = {
    "audit": "audit_harness_quality.py",
    "audit-configs": "audit_ai_configs.py",
    "alignment": "alignment_state.py",
    "alignment-route": "alignment_routing.py",
    "benchmark": "benchmark_harness.py",
    "capture-rule": "capture_rule.py",
    "checkpoint": "checkpoint_state.py",
    "rollback": "rollback_state.py",
    "delegation": "delegation_state.py",
    "guard": "guard_action.py",
    "handoff": "handoff_state.py",
    "profile": "profile_state.py",
    "export": "export_state.py",
    "release-evidence": "release_evidence.py",
    "hooks": "commit_hooks.py",
    "impact": "change_impact.py",
    "cross-impact": "cross_module.py",
    "aggregate-claim": "aggregate_claim.py",
    "repository-units": "repository_units.py",
    "verification-plan": "verification_plan.py",
    "insights": "insights.py",
    "installations": "installations_state.py",
    "local-env": "local_env.py",
    "init": "init_harness.py",
    "onboarding": "onboarding_state.py",
    "loop-state": "loop_state.py",
    "migrate": "migrate_harness.py",
    "prompt-guard": "prompt_guard.py",
    "recap": "recap_harness.py",
    "repair": "repair_harness.py",
    "refresh": "refresh_harness.py",
    "review": "review_harness.py",
    "route": "route_action.py",
    "rules": "rules_context.py",
    "run-all": "run_all_guardrails.py",
    "setup-ci": "setup_ci.py",
    "upgrade": "upgrade_skill.py",
    "validate": "validate_harness.py",
}
LIFECYCLE_SKILLS = {
    "tenetora",
    "tenetora-init",
    "tenetora-update",
    "tenetora-audit",
    "tenetora-loop",
    "tenetora-prompt-guard",
    "tenetora-align",
    "tenetora-decision-interview",
}


def use_chinese() -> bool:
    return os.environ.get("TENETORA_LANG", "").strip().lower() in {"zh", "zh-cn", "zh-hans", "chinese", "中文"}


def ui_text(english: str, chinese: str) -> str:
    return chinese if use_chinese() else english


class LocalizedArgumentParser(argparse.ArgumentParser):
    def _localized(self, value: str) -> str:
        if not use_chinese():
            return value
        return (
            value.replace("usage:", "用法：", 1)
            .replace("options:\n", "选项：\n", 1)
            .replace("positional arguments:\n", "位置参数：\n", 1)
        )

    def format_help(self) -> str:
        return self._localized(super().format_help())

    def format_usage(self) -> str:
        return self._localized(super().format_usage())

    def error(self, message: str) -> None:
        if use_chinese():
            self.print_usage(sys.stderr)
            self.exit(2, f"{self.prog}：错误：{message}\n")
        super().error(message)


def run_script(command: str, script_name: str, forwarded: list[str]) -> int:
    script = SCRIPT_DIR / script_name
    if not script.exists():
        print(ui_text(f"Missing script: {script}", f"缺少脚本：{script}"), file=sys.stderr)
        return 2
    old_argv = sys.argv[:]
    try:
        sys.argv = [f"{invoked_command()} {command}", *forwarded]
        module_name = f"tenetora_script_{command.replace('-', '_')}"
        spec = importlib.util.spec_from_file_location(module_name, script)
        if spec is None or spec.loader is None:
            print(ui_text(f"Cannot load script: {script}", f"无法加载脚本：{script}"), file=sys.stderr)
            return 2
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return int(module.main() or 0)
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        sys.argv = old_argv
    return 0


def handle_version(_: argparse.Namespace) -> int:
    command = invoked_command()
    print(f"{command} {__version__}")
    print(f"product: {PRODUCT_NAME}")
    print(f"source: {REPO_ROOT}")
    print(f"skill: {SKILL_DIR}")
    return 0


def handle_home() -> int:
    root = Path.cwd().resolve(strict=False)
    home = machine_home().resolve(strict=False)
    suffix = ".cmd" if os.name == "nt" else ""
    launcher = home / "bin" / f"tenetora{suffix}"
    transaction_path = home / "state" / "upgrade-transaction.json"
    transaction_status = "none"
    if transaction_path.is_file():
        try:
            transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
            transaction_status = str(transaction.get("status") or "invalid")
        except (OSError, json.JSONDecodeError):
            transaction_status = "invalid"
    if (root / ".tenetora" / "manifest.json").is_file():
        project_state = "initialized"
        next_action = next_action_zh = "tenetora doctor"
    elif (root / ".tenetora").exists():
        project_state = "needs-repair"
        next_action = "Use Tenetora to update or repair this project's governance"
        next_action_zh = "使用 Tenetora 更新或修复当前项目治理"
    elif (root / ".harness").exists():
        project_state = "migration-needed"
        next_action = "Use Tenetora to migrate and initialize this project"
        next_action_zh = "使用 Tenetora 迁移并初始化当前项目"
    else:
        project_state = "not-initialized"
        next_action = "Use Tenetora to initialize this project in your AI conversation"
        next_action_zh = "请在 AI 对话中输入“使用 Tenetora 初始化当前项目”"
    if transaction_status not in {"none", "completed", "rolled-back"}:
        next_action = next_action_zh = "tenetora doctor"
    update_hint = read_hint(home, current_version=__version__)
    project_labels_zh = {
        "initialized": "已初始化",
        "needs-repair": "需要修复",
        "migration-needed": "需要迁移",
        "not-initialized": "尚未初始化",
    }
    transaction_labels_zh = {
        "none": "无",
        "running": "进行中",
        "completed": "已完成",
        "rolled-back": "已回滚",
        "blocked": "已阻断",
        "invalid": "状态损坏",
    }
    print(f"Tenetora {__version__}")
    print()
    print(ui_text(f"Project: {project_state}", f"当前项目：{project_labels_zh.get(project_state, '需要检查')}"))
    print(
        ui_text(
            f"CLI: {'ready' if launcher.is_file() else 'managed launcher missing'}",
            f"CLI：{'已就绪' if launcher.is_file() else '缺少受管 launcher'}",
        )
    )
    print(ui_text(f"Upgrade transaction: {transaction_status}", f"升级事务：{transaction_labels_zh.get(transaction_status, '需要检查')}"))
    if update_hint.get("status") == "fresh" and update_hint.get("update_available"):
        print(
            ui_text(
                f"Update: {update_hint.get('latest_version')} available; run tenetora upgrade",
                f"更新：已有 {update_hint.get('latest_version')}；请运行 tenetora upgrade",
            )
        )
    elif update_hint.get("status") == "stale":
        print(ui_text("Update check: stale; run tenetora upgrade --check", "更新检查：已过期；请运行 tenetora upgrade --check"))
    elif update_hint.get("status") == "network-failed":
        print(ui_text("Update check: last attempt failed; run tenetora upgrade --check", "更新检查：上次检查失败；请运行 tenetora upgrade --check"))
    elif update_hint.get("status") == "invalid":
        print(ui_text("Update check: unavailable; run tenetora upgrade --check", "更新检查：不可用；请运行 tenetora upgrade --check"))
    print()
    print(ui_text(f"Next: {next_action}", f"下一步：{next_action_zh}"))
    return 0


def find_lifecycle_skill_path(skill: str) -> Path | None:
    candidates = [
        REPO_ROOT / "skills" / skill / "SKILL.md",
        SKILL_DIR.parent / skill / "SKILL.md",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def handle_skill_instructions(args: argparse.Namespace) -> int:
    skill_path = find_lifecycle_skill_path(args.skill)
    if skill_path is None:
        print(ui_text(f"Unknown or unavailable Tenetora skill: {args.skill}", f"未知或不可用的 Tenetora skill：{args.skill}"), file=sys.stderr)
        return 2
    text = skill_path.read_text(encoding="utf-8")
    print("# Tenetora Skill Runtime Instructions")
    print()
    print(f"- version: {__version__}")
    print(f"- skill: {args.skill}")
    print(f"- source: {skill_path}")
    print()
    print("These instructions were read from disk at runtime. If they differ from cached skill text, follow the returned instructions instead of cached text.")
    print()
    print(text.rstrip())
    return 0


def handle_completion(args: argparse.Namespace) -> int:
    selected = [args.shell] if args.shell else []
    return run_completion(selected, args.command_names)


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    for index, token in enumerate(raw):
        if token.startswith("--lang="):
            os.environ["TENETORA_LANG"] = token.split("=", 1)[1]
            break
        if token == "--lang" and index + 1 < len(raw):
            os.environ["TENETORA_LANG"] = raw[index + 1]
            break
    if not os.environ.get("TENETORA_LANG"):
        stored_language = read_language_preference()
        if stored_language:
            os.environ["TENETORA_LANG"] = stored_language
    if not raw:
        return handle_home()
    if raw and raw[0] in LEGACY_COMMANDS:
        return run_script(raw[0], LEGACY_COMMANDS[raw[0]], raw[1:])

    command_name = invoked_command()
    examples = ui_text("""Examples:
  {command} status
  {command} status --tools agents
  {command} doctor --tools codex,claude --json
  {command} preflight --tools all --global
  {command} preflight --tools codex --require-full --json
  {command} audit --strict
  {command} benchmark
  {command} alignment --status
  {command} alignment --start --goal "Migrate the public API" --risk-level high
  {command} alignment-route --status --request-id <request-id> --owner-id <owner-id> --conversation-id <conversation-id> --tool <tool>
  {command} guard --action alignment --goal "Migrate the public API" --risk-level high
  {command} impact --status
  {command} insights --days 90
  {command} installations list
  {command} installations discover --auto
  {command} installations discover --root ~/projects --max-depth 5
  {command} impact --start --kind public-api --summary "Change one API" --impact-tool codegraph --symbol Api --baseline-status skipped --baseline-reason "no baseline command"
  {command} capture-rule --text "Do not commit unless the user asks."
  {command} delegation --list-roles
  {command} delegation --status
  {command} delegation --review-start --trigger commit --review-scope src/module-a
  {command} delegation --review-rebind --trigger commit
  {command} route --message "Update the API and tests"
  {command} rules --context build
  {command} guard --action commit --commit-message-file .git/COMMIT_EDITMSG --require-message
  {command} guard --action claim --claim-kind completion --verification-command "<full-command>"
  {command} prompt-guard --text "<untrusted external content>"
  {command} migrate --check
  {command} migrate --plan
  {command} migrate --apply
  {command} init --write --tools auto --mode auto
  {command} onboarding --decline --path <project-dir>
  {command} refresh --strategy diff
  {command} repair --check
  {command} skill-instructions --skill tenetora-update
  {command} upgrade --check
  {command} completion bash
  {command} run-all
  {command} validate
  {command} version

Path:
  --path defaults to the current directory. Use --path <project-dir> only when operating on another project.

Options:
  Long options use full semantic names. Common options also provide short aliases, such as -g/--global and -t/--tools.
  Help is available only through --help.
""", """示例：
  {command} status
  {command} status --tools agents
  {command} doctor --tools codex,claude --json
  {command} preflight --tools all --global
  {command} audit --strict
  {command} alignment --status
  {command} alignment-route --status --request-id <request-id> --owner-id <owner-id> --conversation-id <conversation-id> --tool <tool>
  {command} guard --action claim --claim-kind completion --verification-command "<full-command>"
  {command} delegation --review-start --trigger commit --review-scope src/module-a
  {command} delegation --review-rebind --trigger commit
  {command} route --message "更新 API 和测试"
  {command} rules --context build
  {command} init --write --tools auto --mode auto
  {command} onboarding --decline --path <project-dir>
  {command} refresh --strategy diff
  {command} upgrade --check
  {command} completion bash
  {command} run-all
  {command} validate
  {command} version

路径：
  --path 默认使用当前目录。仅在操作其他项目时使用 --path <project-dir>。

选项：
  长选项使用完整语义名称。常用选项也提供短别名，例如 -g/--global 和 -t/--tools。
  帮助仅通过 --help 提供。
""").format(command=command_name)
    parser = LocalizedArgumentParser(
        prog=command_name,
        description=ui_text(
            "Align decisions and govern shared .tenetora project context with Tenetora.",
            "使用 Tenetora 对齐决策并治理共享的 .tenetora 项目上下文。",
        ),
        add_help=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=examples,
    )
    parser.add_argument("--help", action="help", help=ui_text("Show this help message and exit.", "显示此帮助信息并退出。"))
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help=ui_text("Show the program version and exit.", "显示程序版本并退出。"),
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        title=ui_text("Commands", "命令"),
        parser_class=LocalizedArgumentParser,
    )
    add_status_parser(subparsers)
    add_doctor_parser(subparsers)
    add_diagnostics_parser(subparsers)
    add_install_parser(subparsers)
    add_preflight_parser(subparsers)
    add_update_parser(subparsers)
    add_setup_parser(subparsers)
    command_help = {
        "audit": ("Run the Harness Engineering audit", "运行治理工程审计"),
        "alignment": ("Manage bounded pre-execution decision alignment", "管理执行前的有界决策对齐"),
        "alignment-route": ("Select a short-lived alignment target for the current user turn", "为当前用户轮次选择短期 alignment 目标"),
        "audit-configs": ("Run the AI tool config audit", "运行 AI 工具配置审计"),
        "benchmark": ("Benchmark Tenetora validation, audit, and guardrails", "评测 Tenetora 验证、审计与 guardrail"),
        "capture-rule": ("Check and record a confirmed chat rule", "检查并记录已确认的对话规则"),
        "completion": ("Print shell completion for the managed CLI", "输出受管 CLI 的 Shell 补全脚本"),
        "delegation": ("Record portable subagent dispatch and bounded review state", "记录可移植的子代理调度与有界审查状态"),
        "guard": ("Run action-triggered guard checks", "运行由动作触发的 guard 检查"),
        "hooks": ("Inspect or install local commit governance hooks", "检查或安装本地提交治理 Hook"),
        "impact": ("Record and verify shared-contract change impact", "记录并验证共享契约变更影响"),
        "cross-impact": ("Validate explicit cross-module relationships and verification", "验证显式跨模块关系与验证矩阵"),
        "aggregate-claim": ("Create or validate a parent repository aggregate proof", "创建或验证父仓库聚合证据"),
        "repository-units": ("Inspect or repair repository-unit registrations", "检查或修复仓库单元登记"),
        "verification-plan": ("Classify changes and choose the smallest verification action", "分类变更并选择最小验证动作"),
        "installations": ("Inspect or maintain registered project installations", "检查或维护已登记项目安装"),
        "local-env": ("Register project-local environment paths", "登记项目本地环境路径"),
        "init": ("Initialize or update a project .tenetora", "初始化或更新项目 .tenetora"),
        "onboarding": ("Record an explicit onboarding reminder decision", "记录明确的 onboarding 提醒决定"),
        "loop-state": ("Read or update bounded loop state", "读取或更新有界循环状态"),
        "migrate": ("Classify or migrate legacy .harness governance into .tenetora", "分类或迁移旧 .harness 治理到 .tenetora"),
        "prompt-guard": ("Scan untrusted content for prompt-injection and unsafe local actions", "扫描不受信任内容中的提示词注入和不安全本地动作"),
        "recap": ("Write validation and audit recap into .tenetora state", "将验证与审计摘要写入 .tenetora 状态"),
        "repair": ("Repair known Tenetora compatibility issues", "修复已知 Tenetora 兼容性问题"),
        "refresh": ("Refresh .tenetora with reviewable update artifacts", "使用可审查更新产物刷新 .tenetora"),
        "review": ("Review latest .tenetora update and quality state", "审查最新 .tenetora 更新与质量状态"),
        "route": ("Suggest task rules, guards, and portable delegation roles", "建议任务规则、guard 与可移植调度角色"),
        "rules": ("Print task-relevant .tenetora rules and record consumption", "输出任务相关 .tenetora 规则并记录使用"),
        "run-all": ("Run project guardrail checks", "运行项目 guardrail 检查"),
        "setup-ci": ("Generate reviewable CI integration files", "生成可审查的 CI 集成文件"),
        "upgrade": ("Check or apply Tenetora skill package upgrades", "检查或应用 Tenetora skill 包升级"),
        "validate": ("Validate project .tenetora structure", "验证项目 .tenetora 结构"),
        "version": ("Show Tenetora version and source", "显示 Tenetora 版本与来源"),
    }
    for name in (
        "audit", "alignment", "alignment-route", "audit-configs", "benchmark", "capture-rule", "delegation", "guard", "hooks",
        "impact", "installations", "local-env", "init", "onboarding", "loop-state", "migrate", "prompt-guard", "recap", "repair", "refresh",
        "review", "route", "rules", "run-all", "setup-ci", "cross-impact", "aggregate-claim", "repository-units",
        "verification-plan",
    ):
        subparsers.add_parser(name, help=ui_text(*command_help[name]), add_help=False)
    skill_parser = subparsers.add_parser(
        "skill-instructions",
        help=ui_text("Print latest runtime instructions for a lifecycle skill", "输出 lifecycle skill 的最新运行时说明"),
        add_help=False,
    )
    skill_parser.add_argument("--help", action="help", help=ui_text("Show this help message and exit.", "显示此帮助信息并退出。"))
    skill_parser.add_argument("--skill", required=True, choices=sorted(LIFECYCLE_SKILLS), help=ui_text("Lifecycle skill name to read from the current Tenetora package.", "从当前 Tenetora 包读取的 lifecycle skill 名称。"))
    skill_parser.set_defaults(handler=handle_skill_instructions)
    subparsers.add_parser("upgrade", help=ui_text(*command_help["upgrade"]), add_help=False)
    subparsers.add_parser("validate", help=ui_text(*command_help["validate"]), add_help=False)
    version_parser = subparsers.add_parser("version", help=ui_text(*command_help["version"]), add_help=False)
    version_parser.set_defaults(handler=handle_version)
    completion_parser = subparsers.add_parser(
        "completion",
        help=ui_text(*command_help["completion"]),
        add_help=False,
    )
    completion_parser.add_argument(
        "-h",
        "--help",
        action="help",
        help=ui_text("Show this help message and exit.", "显示此帮助信息并退出。"),
    )
    completion_parser.add_argument(
        "shell",
        nargs="?",
        metavar="{bash,zsh,fish,powershell}",
        help=ui_text("Shell to generate completion for.", "要生成补全脚本的 Shell。"),
    )
    completion_parser.set_defaults(
        handler=handle_completion,
        command_names=tuple(sorted(subparsers.choices)),
    )
    ns = parser.parse_args(raw)
    return ns.handler(ns)


if __name__ == "__main__":
    raise SystemExit(main())
