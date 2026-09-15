"""Command handlers for Tenetora."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

from .brand import PRODUCT_NAME, invoked_command, machine_home, read_language_preference, write_language_preference
from .environment import collect_status, parse_tools
from .diagnostics import DiagnosticsError, build_diagnostics, render_text, write_bundle
from .finding_acknowledgements import (
    AcknowledgementStateError,
    acknowledge_finding,
    unacknowledge_finding,
)
from .installer import run_install_script
from .output import render_json, render_status_text
from .path_security import validate_existing_project_path


def normalize_command_language(raw: str) -> str | None:
    value = raw.strip().lower()
    if value in {"en", "en-us", "english"}:
        return "en"
    if value in {"zh", "zh-cn", "zh-hans", "chinese", "中文"}:
        return "zh"
    return None


def select_command_language(args: argparse.Namespace, *, prompt: bool) -> str:
    json_output = bool(getattr(args, "json", False))
    configured = getattr(args, "lang", None) or os.environ.get("TENETORA_LANG", "") or read_language_preference() or ""
    language = normalize_command_language(configured) if configured else None
    if configured and language is None:
        print(
            f"Unsupported language: {configured}. Use en or zh. / "
            f"不支持的语言：{configured}，请使用 en 或 zh。",
            file=sys.stderr,
        )
        raise SystemExit(2)

    prompted = False
    if language is None and prompt and not json_output and sys.stdin.isatty():
        print("Select language / 选择语言:")
        print("  1) English")
        print("  2) 中文")
        while True:
            try:
                choice = input("Choice / 请选择 [1/2]: ").strip()
            except EOFError:
                choice = "1"
            if choice in {"1", "en", "EN", "English", "english"}:
                language = "en"
                break
            if choice in {"2", "zh", "ZH", "中文"}:
                language = "zh"
                break
            print("Invalid choice, enter 1 or 2. / 选择无效，请输入 1 或 2。")
        prompted = True

    language = language or "en"
    os.environ["TENETORA_LANG"] = language
    if configured or prompted:
        write_language_preference(language)
    if not json_output:
        if language == "zh":
            print("语言：中文")
        elif configured or prompted:
            print("Language: English")
        else:
            print("Language: English (non-interactive default; use --lang zh for Chinese)")
    return language


def command_text(language: str, english: str, chinese: str) -> str:
    return chinese if language == "zh" else english


def help_text(english: str, chinese: str) -> str:
    configured = normalize_command_language(os.environ.get("TENETORA_LANG", ""))
    return chinese if configured == "zh" else english


def existing_project_path(raw: str) -> Path:
    try:
        return validate_existing_project_path(raw)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def add_help_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--help", action="help", help=help_text("Show this help message and exit.", "显示此帮助信息并退出。"))


def add_path_argument(parser: argparse.ArgumentParser, purpose: str) -> None:
    parser.add_argument(
        "--path",
        "-p",
        default=".",
        type=existing_project_path,
        metavar="<project-dir>",
        help=help_text(
            f"Project directory to {purpose}. Defaults to the current directory.",
            "项目目录，默认使用当前目录。",
        ),
    )


def add_scope_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-s",
        "--scope",
        choices=("global", "project", "both"),
        default="global",
        help=help_text("Environment scope to inspect or modify.", "要检查或修改的环境范围。"),
    )


def add_scope_flags(parser: argparse.ArgumentParser) -> None:
    scope_group = parser.add_mutually_exclusive_group()
    scope_group.add_argument(
        "-g",
        "--global",
        dest="scope_flag",
        action="store_const",
        const="global",
        help=help_text("Use global skill directories", "使用全局 skill 目录"),
    )
    scope_group.add_argument(
        "-i",
        "--in-project",
        dest="scope_flag",
        action="store_const",
        const="project",
        help=help_text("Use project-level skill directories", "使用项目级 skill 目录"),
    )
    scope_group.add_argument(
        "-b",
        "--both",
        dest="scope_flag",
        action="store_const",
        const="both",
        help=help_text("Use global and project-level skill directories", "同时使用全局和项目级 skill 目录"),
    )
    scope_group.add_argument(
        "-s",
        "--scope",
        choices=("global", "project", "both"),
        default=None,
        help=help_text("Environment scope to modify.", "要修改的环境范围。"),
    )


def add_tools_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-t",
        "--tools",
        default="auto",
        help=help_text(
            "auto, all, or comma-separated tools: agents,codex,claude,cursor,opencode,pi,zcode",
            "使用 auto、all，或逗号分隔的工具：agents,codex,claude,cursor,opencode,pi,zcode",
        ),
    )


def add_status_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("status", help=help_text("Show Tenetora environment status", "显示 Tenetora 环境状态"), add_help=False)
    add_help_argument(parser)
    add_path_argument(parser, "inspect")
    add_tools_argument(parser)
    parser.add_argument("-j", "--json", action="store_true", help=help_text("Print machine-readable JSON", "输出机器可读 JSON"))
    add_language_argument(parser)
    parser.add_argument(
        "--verbose",
        action="store_true",
        help=help_text("Expand technical sources, shadowed candidates, and findings in text output", "在文本输出中展开技术来源、被遮蔽候选项和问题"),
    )
    add_scope_argument(parser)
    action_group = parser.add_mutually_exclusive_group()
    action_group.add_argument(
        "--acknowledge",
        metavar="FINDING_ID",
        help=help_text("Acknowledge one actionable attention finding for a limited time", "在限定时间内确认一项可处理问题"),
    )
    action_group.add_argument(
        "--unacknowledge",
        metavar="FINDING_ID",
        help=help_text("Remove an acknowledgement owned by the current actor", "移除当前操作者拥有的问题确认记录"),
    )
    parser.add_argument("--reason", help=help_text("Required one-line reason for --acknowledge", "--acknowledge 所需的单行原因"))
    parser.add_argument(
        "--expires-days",
        type=int,
        default=30,
        metavar="DAYS",
        help=help_text("Acknowledgement lifetime in days (1-90; default: 30)", "确认记录有效期天数（1-90，默认 30）"),
    )
    parser.add_argument("--owner-id", help=help_text("Portable owner id for acknowledgement audit", "用于确认审计的可移植 owner ID"))
    parser.set_defaults(handler=handle_status)


def add_doctor_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("doctor", help=help_text("Diagnose Tenetora environment issues", "诊断 Tenetora 环境问题"), add_help=False)
    add_help_argument(parser)
    add_path_argument(parser, "inspect")
    add_tools_argument(parser)
    parser.add_argument("-j", "--json", action="store_true", help=help_text("Print machine-readable JSON", "输出机器可读 JSON"))
    add_language_argument(parser)
    add_scope_argument(parser)
    parser.add_argument("--fix", action="store_true", help=help_text("Apply only safe managed launcher and PATH repairs", "仅应用安全的受管 launcher 与 PATH 修复"))
    parser.set_defaults(handler=handle_doctor)


def add_diagnostics_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "diagnostics",
        help=help_text("Build a strictly redacted support diagnostics bundle", "生成严格脱敏的支持诊断包"),
        add_help=False,
    )
    add_help_argument(parser)
    add_path_argument(parser, "diagnose")
    add_tools_argument(parser)
    add_language_argument(parser)
    add_scope_argument(parser)
    parser.add_argument("--json", action="store_true", help=help_text("Print the redacted diagnostics JSON", "输出脱敏诊断 JSON"))
    parser.add_argument(
        "--output",
        type=Path,
        metavar="<zip-file>",
        help=help_text("Write a ZIP bundle to an existing safe directory.", "将 ZIP 诊断包写入已存在的安全目录。"),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=help_text("Replace an explicitly selected existing output file.", "替换显式指定的已有输出文件。"),
    )
    parser.set_defaults(handler=handle_diagnostics)


def add_install_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("install", help=help_text("Install Tenetora skills", "安装 Tenetora skills"), add_help=False)
    add_help_argument(parser)
    add_install_update_arguments(parser)
    parser.set_defaults(handler=handle_install)


def add_preflight_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("preflight", help=help_text("Inspect installation capability without writing", "在不写入的情况下检查安装能力"), add_help=False)
    add_help_argument(parser)
    add_install_update_arguments(parser)
    parser.set_defaults(handler=handle_preflight, dry_run=True)


def add_update_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("update", help=help_text("Update project .tenetora, or skill installs when install flags are used", "更新项目 .tenetora；使用安装参数时更新 skill 安装"), add_help=False)
    add_help_argument(parser)
    add_path_argument(parser, "update")
    parser.add_argument(
        "--repository-scope",
        default="ask",
        metavar="{ask,parent,all,<submodule-path>}",
        help=help_text(
            "Repositories to update: parent, all, or a comma-separated list of detected Git submodule paths. "
            "ask requires interactive confirmation before any write.",
            "要更新的仓库：parent、all，或检测到的 submodule 路径逗号列表；ask 会在写入前询问。",
        ),
    )
    parser.add_argument("-t", "--tools", default=None, help=help_text("Project refresh tools, or install tools when install-mode flags are used", "项目刷新工具；使用安装模式参数时表示要安装的工具"))
    parser.add_argument(
        "--strategy",
        choices=("diff", "merge"),
        default="merge",
        help=help_text("Project harness update strategy. Defaults to merge.", "项目治理目录更新策略，默认为 merge。"),
    )
    parser.add_argument(
        "--migrate",
        choices=("plan", "ignore", "merge"),
        default="plan",
        help=help_text("Project migration handling. Defaults to plan for project-level updates.", "项目迁移处理方式；项目级更新默认使用 plan。"),
    )
    parser.add_argument(
        "--entrypoints",
        choices=("plan", "backup", "merge"),
        default="plan",
        help=help_text("Entrypoint handling for project updates. Defaults to plan.", "项目更新的入口文件处理方式，默认为 plan。"),
    )
    parser.add_argument("--project", action="store_true", help=help_text("Force project .tenetora update mode; useful for clarity when passing --tools", "强制使用项目 .tenetora 更新模式；传入 --tools 时可用于消除歧义"))
    parser.add_argument(
        "--allow-mixed-governance",
        action="store_true",
        help=help_text("Migrate only recognized Tenetora content from a reviewed mixed legacy .harness and preserve the legacy source.", "仅从已审查的混合旧 .harness 迁移可识别的 Tenetora 内容，并保留旧来源。"),
    )
    parser.add_argument(
        "-m",
        "--mode",
        choices=("auto", "symlink", "copy"),
        default=None,
        help=help_text("Install update mode. Presence of this flag selects skill-install update mode.", "安装更新模式；提供此参数即选择 skill 安装更新模式。"),
    )
    parser.add_argument("-f", "--force", action="store_true", help=help_text("Replace conflicting skill installs in install-update mode", "在安装更新模式下替换冲突的 skill 安装"))
    parser.add_argument("--dry-run", action="store_true", help=help_text("Print install-update actions without writing", "仅输出安装更新动作，不写入"))
    add_language_argument(parser)
    add_codex_hook_policy_arguments(parser)
    add_capability_policy_arguments(parser)
    add_progress_arguments(parser, default=None)
    add_scope_flags(parser)
    parser.set_defaults(handler=handle_update)


def add_setup_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("setup", help=help_text("Run the interactive Tenetora setup wizard", "运行交互式 Tenetora 设置向导"), add_help=False)
    add_help_argument(parser)
    add_path_argument(parser, "set up")
    add_tools_argument(parser)
    parser.set_defaults(handler=handle_setup)


def add_install_update_arguments(parser: argparse.ArgumentParser) -> None:
    add_path_argument(parser, "use for project-level installs")
    add_tools_argument(parser)
    parser.add_argument(
        "-m",
        "--mode",
        choices=("auto", "symlink", "copy"),
        default="auto",
        help=help_text("Install mode. auto uses symlink except copy on Windows.", "安装模式；auto 在 Windows 使用 copy，其他系统使用 symlink。"),
    )
    parser.add_argument("-f", "--force", action="store_true", help=help_text("Replace conflicting installs", "替换冲突安装"))
    parser.add_argument("--dry-run", action="store_true", help=help_text("Print actions without writing", "仅输出动作，不写入"))
    parser.add_argument("-j", "--json", action="store_true", help=help_text("Print machine-readable preflight and result JSON", "输出机器可读的预检与结果 JSON"))
    add_language_argument(parser)
    add_codex_hook_policy_arguments(parser)
    add_capability_policy_arguments(parser)
    add_progress_arguments(parser, default="auto")
    add_scope_flags(parser)


def add_language_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--lang",
        choices=("en", "zh"),
        default=None,
        help=help_text("Use English or Chinese output. Interactive install/update prompts when omitted.", "使用英文或中文输出；安装/更新未指定时会交互询问。"),
    )


def add_progress_arguments(parser: argparse.ArgumentParser, *, default: str | None) -> None:
    parser.add_argument(
        "--progress",
        choices=("auto", "always", "never"),
        default=default,
        help=help_text("Render install progress automatically, always, or never", "自动、始终或从不显示安装进度"),
    )
    parser.add_argument("--no-progress", dest="progress", action="store_const", const="never", help=help_text("Disable dynamic progress output", "禁用动态进度输出"))
    parser.add_argument("--verbose", action="store_true", help=help_text("Print detailed installer actions in addition to the summary", "除摘要外同时输出详细安装动作"))


def add_codex_hook_policy_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--codex-hooks",
        choices=("auto", "native", "project", "off"),
        default="auto",
        help=help_text("Codex hook mode: auto, native plugin, project fallback, or off.", "Codex Hook 模式：auto、native plugin、project fallback 或 off。"),
    )
    parser.add_argument(
        "--allow-tracked-codex-hooks",
        action="store_true",
        help=help_text("Allow structured updates to a Git-tracked .codex/hooks.json for this project.", "允许结构化更新项目中被 Git 跟踪的 .codex/hooks.json。"),
    )


def add_capability_policy_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--allow-skills-only",
        action="store_true",
        help=help_text("Explicitly allow fallback below a tool's native maximum capability", "显式允许降级到低于工具原生最大能力的模式"),
    )
    group.add_argument(
        "--require-full",
        action="store_true",
        help=help_text("Block before writes when a selected tool cannot reach its maximum capability", "选中工具无法达到最大能力时，在写入前阻断"),
    )
    parser.add_argument(
        "--prune-shadowed",
        action="store_true",
        help=help_text("Remove only unchanged Tenetora-managed runtime adapters shadowed by the selected effective runtime", "仅移除被当前有效 runtime 遮蔽且未修改的 Tenetora 受管 runtime 适配器"),
    )
    parser.add_argument(
        "--no-prune-shadowed",
        action="store_true",
        help=help_text("Disable automatic removal of unchanged managed shadow runtimes during upgrades", "升级时禁用对未修改受管遮蔽 runtime 的自动移除"),
    )


def handle_status(args: argparse.Namespace) -> int:
    select_command_language(args, prompt=False)
    status = collect_status(
        root=args.path,
        tools=parse_tools(args.tools),
        scope=args.scope,
        include_diagnostics=True,
        tool_selection="auto" if "auto" in args.tools.split(",") else "explicit",
    )
    action = None
    selected_id = args.acknowledge or args.unacknowledge
    if selected_id:
        if args.acknowledge and not args.reason:
            print("--reason is required with --acknowledge", file=sys.stderr)
            return 2
        finding = next(
            (item for item in status.findings if item.get("finding_id") == selected_id),
            None,
        )
        if finding is None:
            print("No matching current finding id; refresh status and use its exact finding_id.", file=sys.stderr)
            return 2
        try:
            if args.acknowledge:
                event = acknowledge_finding(
                    args.path,
                    finding,
                    reason=args.reason or "",
                    owner_id=args.owner_id,
                    expires_days=args.expires_days,
                    audit=lambda payload: append_finding_ack_event(args.path, payload),
                )
                action = {
                    "action": "acknowledge",
                    "status": "pass",
                    "finding_id": event["finding_id"],
                    "expires_at": event["expires_at"],
                }
            else:
                event = unacknowledge_finding(
                    args.path,
                    selected_id,
                    owner_id=args.owner_id,
                    audit=lambda payload: append_finding_ack_event(args.path, payload),
                )
                action = {
                    "action": "unacknowledge",
                    "status": "pass",
                    "finding_id": event["finding_id"],
                }
        except AcknowledgementStateError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        status = collect_status(
            root=args.path,
            tools=parse_tools(args.tools),
            scope=args.scope,
            include_diagnostics=True,
            tool_selection="auto" if "auto" in args.tools.split(",") else "explicit",
        )
        status.acknowledgement_action = action
    if args.json:
        print(render_json(status))
    else:
        if action:
            print(f"Acknowledgement {action['action']}: {action['finding_id']}")
        print(render_status_text(status, include_diagnostics=args.verbose))
    return 0


def append_finding_ack_event(root: Path, event: dict[str, object]) -> None:
    """Append through the canonical governance trail implementation."""

    script = script_dir() / "governance_trail.py"
    script_parent = str(script.parent)
    if script_parent not in sys.path:
        sys.path.insert(0, script_parent)
    spec = importlib.util.spec_from_file_location("tenetora_finding_ack_governance_trail", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("canonical governance trail is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.append_event(root, event)


def handle_doctor(args: argparse.Namespace) -> int:
    select_command_language(args, prompt=False)
    status = collect_status(
        root=args.path,
        tools=parse_tools(args.tools),
        scope=args.scope,
        include_diagnostics=True,
        tool_selection="auto" if "auto" in args.tools.split(",") else "explicit",
    )
    transaction_path = machine_home() / "state" / "upgrade-transaction.json"
    upgrade_state: dict[str, object] = {"status": "none"}
    if transaction_path.is_file():
        try:
            import json

            raw = json.loads(transaction_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                upgrade_state = {
                    "status": str(raw.get("status") or "invalid"),
                    "stage": str(raw.get("stage") or "unknown"),
                    "target_version": str(raw.get("target_version") or ""),
                    "recoverable": raw.get("status") in {"running", "rolled-back", "blocked"},
                }
        except (OSError, ValueError):
            upgrade_state = {"status": "invalid", "recoverable": False}
    fix_result = None
    if args.fix:
        path_script = script_dir() / "path_manager.py"
        spec = importlib.util.spec_from_file_location("tenetora_doctor_path_manager", path_script)
        if spec is None or spec.loader is None:
            raise RuntimeError("PATH manager is unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fix_result = module.configure_user_path(machine_home() / "bin")
    if args.json:
        import json

        payload = json.loads(render_json(status))
        payload["upgrade"] = upgrade_state
        if fix_result is not None:
            payload["safe_fix"] = fix_result
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        state = str(upgrade_state.get("status") or "none")
        if state not in {"none", "completed"}:
            action = "tenetora upgrade" if state in {"running", "rolled-back", "blocked"} else "review the upgrade transaction state"
            print(command_text(os.environ.get("TENETORA_LANG", "en"), f"Upgrade: {state}. Next: {action}", f"升级：{state}。下一步：{action}"))
        if fix_result is not None:
            print(command_text(os.environ.get("TENETORA_LANG", "en"), f"Safe fix: {fix_result['status']}", f"安全修复：{fix_result['status']}"))
        print(render_status_text(status, include_diagnostics=True))
    return 0


def handle_diagnostics(args: argparse.Namespace) -> int:
    select_command_language(args, prompt=False)
    try:
        selected_tools = parse_tools(args.tools)
    except (TypeError, ValueError) as error:
        payload = {
            "schema_version": 1,
            "bundle_type": "tenetora-diagnostics",
            "status": "blocked",
            "error_code": "invalid-tools",
            "error": "Unsupported or empty tool selection",
            "redaction": {"mode": "strict"},
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(f"Diagnostics error: {error}", file=sys.stderr)
        return 2
    payload = build_diagnostics(
        root=args.path,
        tools=selected_tools,
        scope=args.scope,
    )
    output_written = False
    if args.output is not None:
        try:
            write_bundle(payload, args.output, force=args.force)
            output_written = True
        except DiagnosticsError as error:
            if args.json:
                payload = {
                    "schema_version": payload.get("schema_version", 1),
                    "bundle_type": "tenetora-diagnostics",
                    "status": "blocked",
                    "error_code": "output-failed",
                    "error": str(error),
                    "redaction": {"mode": "strict"},
                }
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"Diagnostics error: {error}", file=sys.stderr)
            return 1
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_text(payload, output_written=output_written))
    return 1 if payload.get("status") == "blocked" and payload.get("error_code") else 0


def handle_install(args: argparse.Namespace) -> int:
    return handle_install_or_update("install", args)


def handle_preflight(args: argparse.Namespace) -> int:
    args.project_path = args.path
    select_command_language(args, prompt=True)
    return run_install_script("preflight", args)


def handle_update(args: argparse.Namespace) -> int:
    if update_args_select_install_mode(args):
        args.mode = args.mode or "auto"
        args.tools = args.tools or "auto"
        args.progress = args.progress or "auto"
        args.project_path = args.path
        return handle_install_or_update("update", args)
    return handle_project_update(args)


def update_args_select_install_mode(args: argparse.Namespace) -> bool:
    return bool(
        getattr(args, "scope_flag", None)
        or getattr(args, "scope", None)
        or getattr(args, "mode", None)
        or getattr(args, "force", False)
        or getattr(args, "dry_run", False)
        or getattr(args, "allow_skills_only", False)
        or getattr(args, "require_full", False)
        or getattr(args, "codex_hooks", "auto") != "auto"
        or getattr(args, "allow_tracked_codex_hooks", False)
        or getattr(args, "prune_shadowed", False)
        or getattr(args, "no_prune_shadowed", False)
        or getattr(args, "progress", None) is not None
        or getattr(args, "verbose", False)
    )


def script_dir() -> Path:
    package_root = Path(__file__).resolve().parents[2]
    nested = package_root / "skills" / "tenetora" / "scripts"
    if nested.exists():
        return nested
    direct = package_root / "scripts"
    if direct.exists():
        return direct
    return nested


def resolve_project_repository_targets(root: Path, requested: str) -> list[object]:
    """Resolve parent/submodule targets before the project update mutates anything."""

    script = script_dir() / "repository_scope.py"
    script_parent = str(script.parent)
    if script_parent not in sys.path:
        sys.path.insert(0, script_parent)
    spec = importlib.util.spec_from_file_location("tenetora_project_repository_scope", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load repository scope resolver: {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    try:
        return list(module.resolve_repository_scope(root, requested, write=True))
    except module.RepositoryScopeRequired as exc:
        raise RuntimeError(str(exc)) from exc


def run_harness_script(script_name: str, root: Path, *extra: str) -> int:
    script = script_dir() / script_name
    if not script.exists():
        print(f"Missing script: {script}", file=sys.stderr)
        return 2
    old_argv = sys.argv[:]
    try:
        sys.argv = [f"{invoked_command()} {script_name}", "--path", str(root), *extra]
        module_name = f"tenetora_update_{script_name.replace('-', '_').replace('.', '_')}"
        spec = importlib.util.spec_from_file_location(module_name, script)
        if spec is None or spec.loader is None:
            print(f"Cannot load script: {script}", file=sys.stderr)
            return 2
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return int(module.main() or 0)
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        sys.argv = old_argv


def handle_project_update(args: argparse.Namespace) -> int:
    tools = args.tools or "auto"
    print("Tenetora Project Update")
    try:
        targets = resolve_project_repository_targets(args.path, args.repository_scope)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    missing = [
        target
        for target in targets
        if not (target.path / ".tenetora").is_dir() and not (target.path / ".harness").is_dir()
    ]
    if missing:
        for target in missing:
            print(
                f"Selected repository {target.path} has no .tenetora; initialize it separately with "
                f"tenetora init --path {target.path} --repository-scope parent.",
                file=sys.stderr,
            )
        return 2

    for target in targets:
        root = target.path
        print(f"Path: {root}")
        print(f"Repository scope selected: {target.kind} ({target.relative})")
        migration_args = ["--apply"]
        if args.allow_mixed_governance:
            migration_args.append("--allow-mixed")
        steps = [
            ("[1/9] Governance directory migration", "migrate_harness.py", migration_args),
            ("[2/9] Skeleton-first planning repair", "repair_harness.py", ["--apply", "--fix", "skeleton-first-planning"]),
            ("[3/9] Decision-alignment repair", "repair_harness.py", ["--apply", "--fix", "alignment-lifecycle"]),
            ("[4/9] Subagent-governance repair", "repair_harness.py", ["--apply", "--fix", "subagent-governance"]),
            ("[5/9] Validate before refresh", "validate_harness.py", []),
            ("[6/9] Commit hook decision", "commit_hooks.py", ["--ensure"]),
            ("[7/9] Audit before refresh", "audit_harness_quality.py", ["--profile", "engineering", "--strict"]),
            (
                "[8/9] Refresh and review",
                "review_harness.py",
                [
                    "--apply",
                    "--strategy",
                    args.strategy,
                    "--tools",
                    tools,
                    "--migrate",
                    args.migrate,
                    "--entrypoints",
                    args.entrypoints,
                    "--repository-scope",
                    "parent",
                    "--skip-global-migration-scan",
                ],
            ),
            ("[9/9] Recap", "recap_harness.py", ["--write"]),
        ]
        for label, script, extra in steps:
            print(label)
            result = run_harness_script(script, root, *extra)
            if result != 0:
                return result
        result = run_harness_script(
            "init_harness.py",
            root,
            "--offer-codex-hooks-only",
            "--write",
            "--tools",
            tools,
            "--repository-scope",
            "parent",
        )
        if result != 0:
            return result
    return 0


def handle_install_or_update(action: str, args: argparse.Namespace) -> int:
    args.project_path = args.path
    json_output = getattr(args, "json", False)
    language = select_command_language(args, prompt=True)
    action_zh = "安装" if action == "install" else "升级"
    if not json_output:
        print(PRODUCT_NAME)
        print()
        print(
            command_text(
                language,
                f"[1/3] Resolving {action} tools...",
                f"[1/3] 正在解析{action_zh}工具...",
            )
        )
        print(
            command_text(
                language,
                f"[2/3] Applying {action}...",
                f"[2/3] 正在执行{action_zh}...",
            )
        )
    result = run_install_script(action, args)
    if result == 0 and not json_output:
        print(command_text(language, "[3/3] Done.", "[3/3] 完成。"))
    return result


def handle_setup(args: argparse.Namespace) -> int:
    if not sys.stdin.isatty():
        print(
            "setup requires an interactive terminal; use tenetora status, install, update, and init for non-interactive use.",
            file=sys.stderr,
        )
        return 2

    status = collect_status(
        root=args.path,
        tools=parse_tools(args.tools),
        scope="global",
        include_diagnostics=True,
    )
    print(render_status_text(status, include_diagnostics=True))
    print()
    print("Interactive setup will ask before writing. Run explicit install/update/init commands for automation.")
    return 0
