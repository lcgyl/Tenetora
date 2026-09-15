"""Terminal and JSON rendering for Tenetora CLI commands."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

from .brand import read_language_preference
from .environment import EnvironmentStatus, to_jsonable


def render_json(status: EnvironmentStatus) -> str:
    return json.dumps(to_jsonable(status), ensure_ascii=False, indent=2)


def shorten_path(raw: str) -> str:
    try:
        path = Path(raw)
        home = Path.home()
        return "~" + str(path).removeprefix(str(home)) if str(path).startswith(str(home)) else raw
    except Exception:
        return raw


def render_progress(title: str, steps: list[str]) -> str:
    lines = [title, ""]
    total = len(steps)
    for index, step in enumerate(steps, start=1):
        lines.append(f"[{index}/{total}] {step}")
    return "\n".join(lines)


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    def render_row(values: list[str]) -> str:
        return "  ".join(value.ljust(widths[index]) for index, value in enumerate(values)).rstrip()

    lines = [render_row(headers), render_row(["-" * width for width in widths])]
    lines.extend(render_row(row) for row in rows)
    return "\n".join(lines)


def use_chinese() -> bool:
    configured = os.environ.get("TENETORA_LANG", "").strip().lower()
    if configured:
        return configured in {"zh", "zh-cn", "zh-hans", "chinese", "中文"}
    return read_language_preference() == "zh"


def render_health(status: EnvironmentStatus) -> list[str]:
    health = status.health or {}
    harness = status.project.get("harness") if isinstance(status.project, dict) else None
    project_uninitialized = isinstance(harness, dict) and not harness.get("exists")
    state = str(health.get("status") or "ATTENTION")
    relevant = ", ".join(str(item) for item in health.get("relevant_platforms", [])) or "none"
    active = ", ".join(str(item) for item in health.get("active_platforms", [])) or "none"
    if use_chinese():
        summary = {
            "HEALTHY": "Tenetora 对当前相关平台运行正常。",
            "ATTENTION": f"Tenetora 有 {health.get('actionable_findings', 0)} 个需要关注的问题。",
            "BLOCKED": "Tenetora 治理当前未能完整生效。",
        }.get(state, str(health.get("summary") or "状态未知。"))
        lines = [f"Tenetora 健康状态：{state}", f"  {summary}", f"  相关平台：{relevant}", f"  已激活：{active}"]
        if project_uninitialized:
            lines.insert(0, "当前项目治理状态：未初始化；以下健康摘要仅表示本机工具状态，不代表当前项目已受管。")
        if health.get("dormant_findings"):
            lines.append(f"  非活动平台提示：{health['dormant_findings']}（不影响整体状态）")
        if health.get("acknowledged_findings"):
            lines.append(f"  已确认但未解决：{health['acknowledged_findings']}（暂不计入整体状态，详情仍保留）")
        if isinstance(health.get("acknowledgement_state"), dict) and health["acknowledgement_state"].get("status") == "invalid":
            lines.append("  Finding 确认状态无效：已忽略所有确认记录")
        actions = health.get("next_actions", [])
        if actions:
            lines.append("  优先处理：")
            lines.extend(f"    - {item}" for item in actions)
        return lines
    lines = [
        f"Tenetora Health: {state}",
        f"  {health.get('summary') or 'Health summary is unavailable.'}",
        f"  Relevant platforms: {relevant}",
        f"  Active: {active}",
    ]
    if project_uninitialized:
        lines.insert(0, "Project governance: not initialized; the following health summary is machine-level only.")
    if health.get("dormant_findings"):
        lines.append(f"  Dormant findings: {health['dormant_findings']} (not included in overall health)")
    if health.get("acknowledged_findings"):
        lines.append(
            f"  Acknowledged but unresolved: {health['acknowledged_findings']} "
            "(temporarily excluded from health; details remain visible)"
        )
    if isinstance(health.get("acknowledgement_state"), dict) and health["acknowledgement_state"].get("status") == "invalid":
        lines.append("  Finding acknowledgement state is invalid; all acknowledgements were ignored")
    actions = health.get("next_actions", [])
    if actions:
        lines.append("  Priority actions:")
        lines.extend(f"    - {item}" for item in actions)
    return lines


def render_activity(status: EnvironmentStatus) -> list[str]:
    activity = status.activity or {}
    counts = activity.get("counts") if isinstance(activity.get("counts"), dict) else {}
    if use_chinese():
        if activity.get("status") == "invalid":
            return ["近期治理活动：状态文件无法读取或格式无效，请运行 doctor 检查。"]
        if activity.get("status") != "observed":
            return ["近期治理活动：尚无可验证的运行记录。"]
        return [
            "近期治理活动："
            f"规则加载 {counts.get('rules_loaded', 0)}，"
            f"guard 通过/警告/失败 {counts.get('guards_passed', 0)}/{counts.get('guards_warned', 0)}/{counts.get('guards_failed', 0)}，"
            f"验证声明 {counts.get('verification_claims', 0)}，"
            f"外部输入检查 {counts.get('external_input_checks', 0)}。"
        ]
    if activity.get("status") == "invalid":
        return ["Recent governance activity: state is unreadable or invalid; run doctor to inspect it."]
    if activity.get("status") != "observed":
        return ["Recent governance activity: no verified runtime events yet."]
    return [
        "Recent governance activity: "
        f"rules={counts.get('rules_loaded', 0)} "
        f"guards(pass/warn/fail)={counts.get('guards_passed', 0)}/{counts.get('guards_warned', 0)}/{counts.get('guards_failed', 0)} "
        f"claims={counts.get('verification_claims', 0)} "
        f"external-input={counts.get('external_input_checks', 0)}"
    ]


def _shadowed_count(status: EnvironmentStatus) -> int:
    total = 0
    for tool in status.tools:
        sources = tool.effective_sources or {}
        if not isinstance(sources, dict):
            continue
        for key in ("skill", "plugin", "runtime"):
            source = sources.get(key)
            if isinstance(source, dict) and isinstance(source.get("shadowed"), list):
                total += len(source["shadowed"])
    return total


def finding_summary_text(finding: dict[str, object]) -> str:
    summary = str(finding.get("summary") or "")
    if not use_chinese():
        return summary
    tool = str(finding.get("tool") or "当前平台")
    code = str(finding.get("code") or "unknown")
    messages = {
        "runtime-observation-stale": f"{tool} 已配置，但缺少与当前版本匹配的近期运行证据。",
        "runtime-not-observed": f"{tool} 已配置，但尚未观测到当前 runtime 的执行证据。",
        "runtime-observation-mismatch": f"{tool} 已配置，但最近的运行证据来自其他 source。",
        "runtime-observation-invalid": f"{tool} 的运行证据无效。",
        "runtime-config-unverified": f"{tool} 的 Hook 清单当前不可读取，无法核实实际启用的 runtime 来源。",
        "duplicate-hook-runtime": f"{tool} 同时启用了多套 Hook runtime。",
        "version-drift": f"{tool} 的 Tenetora 安装副本版本不一致。",
        "effective-skill-version-drift": f"{tool} 优先加载了较旧的 Tenetora skill。",
        "native-plugin-missing": f"{tool} 已安装 lifecycle skills，但缺少 native plugin 或 runtime Hook。",
        "plugin-trust": f"{tool} plugin Hook 尚未完成信任。",
        "skill-package-missing": f"{tool} 在请求的范围内缺少完整 lifecycle skill 包。",
        "skill-package-stale": f"{tool} lifecycle skill 包版本过旧。",
        "skill-package-conflict": f"{tool} lifecycle skill 包存在冲突。",
        "harness-missing": "项目尚未初始化 `.tenetora` 治理目录。",
        "marketplace-state": f"{tool} plugin 清单不可读取，无法安全变更 native plugin。",
        "hook-launcher-unhealthy": f"{tool} Hook launcher 状态异常。",
        "legacy-native-hook-active": f"{tool} 仍启用了旧版 Agent Harness native Hook。",
        "plugin-activation": f"{tool} plugin 的激活状态需要处理。",
        "commit-hook-state": "项目 Git Hook 状态需要处理。",
        "alignment-state": "项目决策对齐状态需要处理。",
        "delegation-state": "项目子代理调度状态需要处理。",
        "repository-unit-state": "仓库单元登记或验证证据缺失/无效。",
    }
    return messages.get(code, f"检测到需要处理的问题：`{code}`。请检查下方证据和处理命令。")


def action_text(action: object) -> str:
    text = str(action)
    if not use_chinese():
        return text
    mappings = {
        "In your AI conversation, ask: Use Tenetora to initialize this project.":
            "请在 AI 对话中输入“使用 Tenetora 初始化当前项目”。",
        "In your AI conversation, ask: Use Tenetora to update or repair this project's governance.":
            "请在 AI 对话中输入“使用 Tenetora 更新或修复当前项目治理”。",
    }
    return mappings.get(text, text)


def repository_units_inspect_command(status: EnvironmentStatus) -> str:
    root = str(status.project.get("root") or ".")
    path = subprocess.list2cmdline([root]) if os.name == "nt" else shlex.quote(root)
    return f"tenetora repository-units inspect --path {path} --json"


def repository_unit_guidance(unit: dict[str, object]) -> str:
    problems = {str(item) for item in unit.get("problems", []) if item}
    if "child-head-mismatch" in problems:
        return (
            "先由对应子仓库的 agent 完成提交或同步父仓库 gitlink，再重新检查。"
            if use_chinese()
            else "Have the child-repository agent commit or synchronize the parent gitlink, then inspect again."
        )
    if "dirty" in problems or any("dirty" in problem for problem in problems):
        return (
            "先处理对应子仓库的未提交改动，再重新检查。"
            if use_chinese()
            else "Resolve the child repository's uncommitted changes, then inspect again."
        )
    if any("registration" in problem or "evidence" in problem for problem in problems):
        return (
            "先在该子仓库完成真实验证并取得 claim proof，再登记仓库单元。"
            if use_chinese()
            else "Run real verification in the child repository and register the unit with its claim proof."
        )
    return (
        "先查看 inspect 输出中的具体问题，再决定是否登记或修复。"
        if use_chinese()
        else "Inspect the concrete problem before deciding whether registration or repair is appropriate."
    )


def render_compact_status_text(status: EnvironmentStatus) -> str:
    compact_rows: list[str] = []
    for tool in status.tools:
        runtime = tool.runtime_state or tool.state or "unknown"
        command = ("可用" if tool.command_available else "缺失") if use_chinese() else ("ok" if tool.command_available else "missing")
        finding_count = sum(1 for item in status.findings if item.get("tool") == tool.tool)
        if use_chinese():
            suffix = f"；问题数={finding_count}" if finding_count else ""
            compact_rows.append(f"  {tool.tool}：状态={tool.state}；运行时={runtime}；命令={command}{suffix}")
        else:
            suffix = f"; findings={finding_count}" if finding_count else ""
            compact_rows.append(f"  {tool.tool}: state={tool.state}; runtime={runtime}; command={command}{suffix}")
    shadowed = _shadowed_count(status)
    if use_chinese():
        lines = [
            *render_health(status),
            "",
            *render_activity(status),
            "",
            "工具摘要：",
            *(compact_rows or ["  无已检测工具。"]),
        ]
        if shadowed:
            lines.append(f"  被遮蔽来源：{shadowed} 个（使用 --verbose 查看明细）")
        _append_repository_units(lines, status)
        lines.extend(["", "下一步："])
    else:
        lines = [
            *render_health(status),
            "",
            *render_activity(status),
            "",
            "Tools:",
            *(compact_rows or ["  No tools detected."]),
        ]
        if shadowed:
            lines.append(f"  Shadowed sources: {shadowed} (use --verbose for details)")
        _append_repository_units(lines, status)
        lines.extend(["", "Next:"])
    if status.health.get("next_actions"):
        lines.extend(f"  {action_text(item)}" for item in status.health["next_actions"])
    elif status.recommendations:
        lines.extend(f"  {action_text(item)}" for item in status.recommendations)
    else:
        root = status.project["root"]
        lines.append(f"  tenetora doctor --path {root}")
    return "\n".join(lines)


def _append_repository_units(lines: list[str], status: EnvironmentStatus) -> None:
    snapshot = status.project.get("repository_units")
    if not isinstance(snapshot, dict):
        return
    units = snapshot.get("units")
    if not isinstance(units, list) or not units:
        return
    if use_chinese():
        lines.extend(["", "仓库单元："])
        has_problems = any(
            isinstance(unit, dict) and isinstance(unit.get("problems"), list) and unit.get("problems")
            for unit in units
        )
        if has_problems:
            lines.append(f"  先运行：{repository_units_inspect_command(status)}")
        for unit in units:
            if not isinstance(unit, dict):
                continue
            pointer = str(unit.get("staged_pointer") or "unknown")[:12]
            head = str(unit.get("worktree_head") or "unknown")[:12]
            evidence = str(unit.get("verification_status") or "unknown")
            dirty = "；工作树 dirty" if unit.get("dirty") else ""
            problems = unit.get("problems")
            suffix = "；需要登记/修复" if isinstance(problems, list) and problems else ""
            path = str(unit.get("gitlink_path") or "?")
            lines.append(f"  {path}：指针={pointer}；HEAD={head}；验证={evidence}{dirty}{suffix}")
            if isinstance(problems, list) and problems:
                lines.append(f"    处理：{repository_unit_guidance(unit)}")
    else:
        lines.extend(["", "Repository units:"])
        has_problems = any(
            isinstance(unit, dict) and isinstance(unit.get("problems"), list) and unit.get("problems")
            for unit in units
        )
        if has_problems:
            lines.append(f"  Inspect first: {repository_units_inspect_command(status)}")
        for unit in units:
            if not isinstance(unit, dict):
                continue
            pointer = str(unit.get("staged_pointer") or "unknown")[:12]
            head = str(unit.get("worktree_head") or "unknown")[:12]
            evidence = str(unit.get("verification_status") or "unknown")
            dirty = "; worktree dirty" if unit.get("dirty") else ""
            problems = unit.get("problems")
            suffix = "; needs registration/repair" if isinstance(problems, list) and problems else ""
            lines.append(f"  {unit.get('gitlink_path', '?')}: pointer={pointer}; HEAD={head}; verification={evidence}{dirty}{suffix}")
            if isinstance(problems, list) and problems:
                lines.append(f"    Action: {repository_unit_guidance(unit)}")


def render_status_text(status: EnvironmentStatus, include_diagnostics: bool) -> str:
    if not include_diagnostics:
        return render_compact_status_text(status)
    if use_chinese():
        steps = ["检测本地工具……", "检查 skill 安装……", "检查项目治理目录……"]
        steps.extend(["运行诊断……", "生成建议……"] if include_diagnostics else ["生成下一步……"])
        technical_title = "技术细节："
        effective_title = "有效来源："
        findings_title = "问题："
        table_headers = ["工具", "范围", "路径", "状态", "模式", "运行时", "命令"]
    else:
        steps = ["Detecting local tools...", "Checking skill installs...", "Checking project harness..."]
        steps.extend(["Running diagnostics...", "Building recommendations..."] if include_diagnostics else ["Building next steps..."])
        technical_title = "Technical details:"
        effective_title = "Effective Sources:"
        findings_title = "Findings:"
        table_headers = ["Tool", "Scope", "Path", "State", "Mode", "Runtime", "Command"]

    rows = []
    for tool in status.tools:
        dispatch = (tool.delegation_capabilities or {}).get("dispatch", "-")
        if use_chinese():
            runtime_details = (
                f"插件={tool.plugin_state or '-'} Hook={tool.hooks_state or '-'} "
                f"Hook模式={tool.hook_mode or '-'} 激活={tool.activation_state or '-'} "
                f"运行时={tool.runtime_state or '-'} 调度={dispatch}"
                if tool.plugin_state is not None
                else f"运行时={tool.runtime_state or '-'} 调度={dispatch}"
            )
            command_state = "可用" if tool.command_available else "缺失"
        else:
            runtime_details = (
                f"plugin={tool.plugin_state or '-'} hooks={tool.hooks_state or '-'} "
                f"hook-mode={tool.hook_mode or '-'} active={tool.activation_state or '-'} "
                f"runtime={tool.runtime_state or '-'} delegate={dispatch}"
                if tool.plugin_state is not None
                else f"runtime={tool.runtime_state or '-'} delegate={dispatch}"
            )
            command_state = "ok" if tool.command_available else "missing"
        rows.append(
            [
                tool.tool,
                tool.scope,
                shorten_path(tool.path),
                tool.state,
                tool.mode,
                runtime_details,
                f"{tool.command or '-'}:{command_state}",
            ]
        )
    lines = [
        *render_health(status),
        "",
        *render_activity(status),
        "",
        technical_title,
        render_progress("Tenetora", steps),
        "",
        render_table(table_headers, rows),
        "",
    ]

    lines.append(effective_title)
    rendered_tools: set[str] = set()
    for tool in status.tools:
        if tool.tool in rendered_tools:
            continue
        rendered_tools.add(tool.tool)
        sources = tool.effective_sources or {}
        skill = sources.get("skill") if isinstance(sources, dict) else None
        runtime = sources.get("runtime") if isinstance(sources, dict) else None
        skill_effective = skill.get("effective") if isinstance(skill, dict) else None
        runtime_effective = runtime.get("effective") if isinstance(runtime, dict) else None
        if isinstance(skill_effective, dict):
            skill_text = (
                f"{shorten_path(str(skill_effective.get('path', '-')))} "
                f"v{skill_effective.get('version') or '?'} [{skill_effective.get('basis') or 'unknown'}]"
            )
        else:
            skill_text = "none [unknown]"
        if isinstance(runtime_effective, dict):
            runtime_text = (
                f"{runtime.get('status')}: {runtime_effective.get('kind')} "
                f"v{runtime_effective.get('version') or '?'} [{runtime_effective.get('basis') or 'unknown'}]"
            )
        else:
            runtime_text = str(runtime.get("status") if isinstance(runtime, dict) else "unknown")
        observation = tool.runtime_observation or {}
        alignment = tool.version_alignment or {}
        delegation = tool.delegation_status or {}
        platform_support = delegation.get("platform_support", {})
        session_dispatch = delegation.get("session_dispatch_availability", {})
        lifecycle = delegation.get("lifecycle_observation", {})
        lines.append(f"  {tool.tool}: skill={skill_text}")
        lines.append(
            (
                f"    运行时={runtime_text}；观测={observation.get('state', 'not-observed')} "
                f"[{observation.get('basis', 'unknown')}]；版本={alignment.get('status', 'unknown')}"
                if use_chinese()
                else f"    runtime={runtime_text}; observed={observation.get('state', 'not-observed')} "
                f"[{observation.get('basis', 'unknown')}]; versions={alignment.get('status', 'unknown')}"
            )
        )
        if use_chinese():
            lines.append(
                "    子代理："
                f"平台支持={platform_support.get('state', 'unknown')}；"
                f"当前会话调度={session_dispatch.get('state', 'unknown')}；"
                f"生命周期观测={lifecycle.get('support', 'unknown')}/"
                f"{lifecycle.get('runtime', 'unknown')}/"
                f"{lifecycle.get('evidence', 'not-observed')}"
            )
        else:
            lines.append(
                "    delegation: "
                f"platform={platform_support.get('state', 'unknown')}; "
                f"session-dispatch={session_dispatch.get('state', 'unknown')}; "
                f"lifecycle={lifecycle.get('support', 'unknown')}/"
                f"{lifecycle.get('runtime', 'unknown')}/"
                f"{lifecycle.get('evidence', 'not-observed')}"
            )
        shadowed = []
        if isinstance(skill, dict):
            shadowed.extend(skill.get("shadowed", []))
        plugin = sources.get("plugin") if isinstance(sources, dict) else None
        if isinstance(plugin, dict):
            shadowed.extend(plugin.get("shadowed", []))
        if isinstance(runtime, dict):
            shadowed.extend(runtime.get("shadowed", []))
        for candidate in shadowed:
            if isinstance(candidate, dict):
                prefix = "被遮蔽：" if use_chinese() else "shadowed: "
                lines.append(
                    f"    {prefix}{candidate.get('kind')} "
                    f"{shorten_path(str(candidate.get('path', '-')))} v{candidate.get('version') or '?'}"
                )
    lines.append("")

    if include_diagnostics and status.findings:
        lines.append(findings_title)
        for finding in status.findings:
            dormant = (" 非活动" if use_chinese() else " dormant") if not finding.get("affects_health") else ""
            acknowledged = (" 已确认" if use_chinese() else " acknowledged") if finding.get("acknowledged") else ""
            tool = f" [{finding.get('tool')}]" if finding.get("tool") else ""
            lines.append(
                f"  - {str(finding.get('severity', 'info')).upper()}{dormant}{acknowledged} "
                f"{finding.get('code')}[{finding.get('finding_id', 'no-id')}]{tool}: {finding_summary_text(finding)}"
            )
            acknowledgement = finding.get("acknowledgement")
            if isinstance(acknowledgement, dict):
                lines.append(
                    ("    确认记录：" if use_chinese() else "    acknowledgement: ")
                    + (
                        f"owner={acknowledgement.get('owner_id')} 到期时间={acknowledgement.get('expires_at')} 原因={acknowledgement.get('reason')}"
                        if use_chinese()
                        else f"owner={acknowledgement.get('owner_id')} expires={acknowledgement.get('expires_at')} reason={acknowledgement.get('reason')}"
                    )
                )
            remediation = finding.get("remediation")
            if isinstance(remediation, dict):
                action = remediation.get("command") or remediation.get("description")
                if action:
                    lines.append(f"    {'处理' if use_chinese() else 'fix'}: {action_text(action)}")
        lines.append("")

    lines.append("下一步：" if use_chinese() else "Next:")
    if status.health.get("next_actions"):
        lines.extend(f"  {action_text(item)}" for item in status.health["next_actions"])
    elif status.recommendations:
        lines.extend(f"  {action_text(item)}" for item in status.recommendations)
    else:
        root = status.project["root"]
        lines.append(f"  tenetora doctor --path {root}")
    return "\n".join(lines)
