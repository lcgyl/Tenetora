#!/usr/bin/env python3
"""Suggest Tenetora contexts and guards for a user request."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from governance_trail import append_event  # noqa: E402
from path_security import harness_missing_message, validate_existing_project_path  # noqa: E402


def use_chinese() -> bool:
    return os.environ.get("TENETORA_LANG", "").strip().lower() in {"zh", "zh-cn", "zh-hans", "chinese", "中文"}


def route_reason_text(reason: object) -> str:
    text = str(reason)
    if not use_chinese():
        return text
    mappings = (
        ("explicit alignment request:", "显式决策对齐请求："),
        ("approved or complete plan signal:", "已批准或完整计划信号："),
        ("factual request; investigate directly", "事实查询：直接调查即可"),
        ("narrow mechanical change", "范围明确的机械性变更"),
        ("high-risk engineering surface:", "高风险工程范围："),
        ("material decision ambiguity:", "存在实质决策歧义："),
        ("multiple ambiguity signals:", "存在多个歧义信号："),
        ("no material alignment signal detected", "未检测到需要决策对齐的实质信号"),
    )
    for source, target in mappings:
        if text.startswith(source):
            return target + text[len(source) :]
    return text


CONTEXT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "external-input": (
        "external",
        "untrusted",
        "webpage",
        "issue",
        "report",
        "clipboard",
        "paste",
        "script",
        "外部",
        "网页",
        "报告",
        "粘贴",
        "脚本",
    ),
    "security": (
        "secret",
        "token",
        "password",
        "credential",
        "private key",
        "安全",
        "密钥",
        "凭证",
        "密码",
    ),
    "harness": (
        ".tenetora",
        "agent-harness",
        "skill",
        "rule",
        "rules",
        "governance",
        "规则",
        "治理",
        "更新.tenetora",
    ),
    "change-impact": (
        "refactor",
        "impact analysis",
        "shared contract",
        "breaking change",
        "public api",
        "public signature",
        "exported signature",
        "constructor signature",
        "method signature",
        "schema contract",
        "schema migration",
        "hook schema",
        "hook payload",
        "hook contract",
        "cli option",
        "cli contract",
        "command-line option",
        "plugin manifest",
        "runtime contract",
        "runtime assumption",
        "runtime launcher",
        "cross-module api",
        "重构",
        "影响分析",
        "共享契约",
        "破坏性变更",
        "公共 api",
        "公共签名",
        "导出签名",
        "构造签名",
        "方法签名",
        "接口契约",
        "模式契约",
        "命令行参数",
        "命令行契约",
        "钩子协议",
        "钩子载荷",
        "插件清单",
        "运行时契约",
        "运行时假设",
        "跨模块 api",
    ),
    "planning": (
        "plan",
        "planning",
        "implementation plan",
        "task breakdown",
        "multi-module",
        "cross-module",
        "multi-component",
        "cross-component",
        "end-to-end",
        "walking skeleton",
        "workflow",
        "规划",
        "计划",
        "任务拆分",
        "多模块",
        "跨模块",
        "多组件",
        "跨组件",
        "端到端",
        "骨架优先",
        "工作流",
    ),
    "build": (
        "build",
        "compile",
        "dependency",
        "gradle",
        "maven",
        "module",
        "api",
        "interface",
        "async",
        "构建",
        "编译",
        "依赖",
        "模块",
        "接口",
        "异步",
    ),
    "test": (
        "test",
        "verify",
        "verification",
        "coverage",
        "测试",
        "验证",
        "覆盖率",
    ),
    "commit": (
        "commit",
        "push",
        "提交",
        "推送",
    ),
    "docs": (
        "doc",
        "docs",
        "readme",
        "changelog",
        "文档",
        "说明",
    ),
}

CONTEXT_ORDER = (
    "external-input",
    "security",
    "harness",
    "planning",
    "change-impact",
    "build",
    "test",
    "commit",
    "docs",
)
FALLBACK_CONTEXTS = ["default"]
EXPLICIT_ALIGNMENT_PHRASES = (
    "tenetora-align",
    "agent-harness-align",
    "decision alignment",
    "决策对齐",
    "先对齐",
    "先问清楚",
    "质询方案",
    "grill me",
)
HIGH_RISK_KEYWORDS = (
    "security",
    "permission",
    "authorization",
    "database",
    "data migration",
    "schema",
    "delete",
    "overwrite",
    "public api",
    "release",
    "deploy",
    "supply chain",
    "cross-module",
    "architecture",
    "安全",
    "权限",
    "数据库",
    "数据迁移",
    "迁移",
    "删除",
    "覆盖",
    "公共 api",
    "发布",
    "部署",
    "供应链",
    "跨模块",
    "架构",
)
AMBIGUITY_KEYWORDS = (
    "multiple options",
    "tradeoff",
    "unclear",
    "scope",
    "non-goal",
    "acceptance criteria",
    "rollback strategy",
    "多个方案",
    "方案需要选择",
    "如何设计",
    "设计",
    "权衡",
    "不确定",
    "范围",
    "非目标",
    "验收标准",
    "回滚策略",
    "审批边界",
    "权限边界",
    "兼容窗口",
)
COMPLETE_PLAN_RE = re.compile(
    r"(按(?:已|已经)(?:确认|批准)的(?:计划)?|"
    r"计划(?:已|已经)(?:确认|批准)|"
    r"已有完整计划|已完成决策对齐|"
    r"according to the approved plan|approved plan)",
    re.IGNORECASE,
)
UNRESOLVED_DECISION_RE = re.compile(
    r"(?:"
    r"(?:还|尚|仍|未|没有|不).{0,16}(?:确定|确认|决定|选择|解决|明确|完成|清楚)|"
    r"(?:还有|存在)?.{0,8}(?:多个方案|待选方案|未决方案).{0,10}(?:需要选择|待定|未定|权衡)|"
    r"方案需要选择|"
    r"(?:remains?|still|not yet).{0,24}(?:undecided|unresolved|unclear|open)|"
    r"(?:needs?|requires?).{0,20}(?:decision|choice|tradeoff)"
    r")",
    re.IGNORECASE,
)
FACTUAL_PREFIX_RE = re.compile(r"^(解释|说明|查看|查询|告诉我|是什么|为什么|what\b|why\b|explain\b|show\b)", re.IGNORECASE)
MECHANICAL_RE = re.compile(r"(拼写错误|错别字|typo|format only|仅格式|rename only|只重命名)", re.IGNORECASE)
GENERIC_CHANGE_IMPACT_KEYWORDS = {"refactor", "重构"}
GENERIC_PLANNING_KEYWORDS = {"plan", "planning", "计划"}
CODE_REVIEW_KEYWORDS = (
    "code review",
    "review the change",
    "pull request",
    "merge request",
    "pr review",
    "mr review",
    "代码审查",
    "代码评审",
    "审查改动",
    "评审改动",
)
SCOUT_KEYWORDS = (
    "architecture",
    "impact analysis",
    "dependency graph",
    "call flow",
    "control flow",
    "codebase investigation",
    "root cause investigation",
    "cross-module investigation",
    "架构",
    "影响分析",
    "依赖图",
    "依赖链",
    "调用链",
    "控制流",
    "代码库调查",
    "根因调查",
    "跨模块调查",
)
PARALLEL_VALUE_KEYWORDS = (
    "parallel review",
    "independent review",
    "parallel investigation",
    "并行审查",
    "独立审查",
    "并行调查",
)


def existing_project_path(raw: str) -> Path:
    try:
        return validate_existing_project_path(raw)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(
        prog="tenetora route",
        description="Suggest task contexts and action guards for a user request.",
        add_help=False,
    )
    command_parser.add_argument("--help", action="help", help="Show this help message and exit.")
    command_parser.add_argument("--message", required=True, help="User request or task summary to route.")
    command_parser.add_argument(
        "-p",
        "--path",
        default=".",
        type=existing_project_path,
        metavar="<project-dir>",
        help="Project directory. Defaults to the current directory.",
    )
    command_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    command_parser.add_argument("--no-record", action="store_true", help="Do not append a governance-trail event.")
    return command_parser


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


def matches(text: str, keyword: str) -> bool:
    key = keyword.casefold()
    if re.search(r"[\u4e00-\u9fff]", key):
        return key in text
    return bool(re.search(rf"(?<![a-z0-9_-]){re.escape(key)}(?![a-z0-9_-])", text))


def matched_keywords(text: str, keywords: tuple[str, ...]) -> list[str]:
    return [keyword for keyword in keywords if matches(text, keyword)]


def alignment_route(message: str) -> dict[str, Any]:
    normalized = normalize(message)
    explicit = matched_keywords(normalized, EXPLICIT_ALIGNMENT_PHRASES)
    high_risk = matched_keywords(normalized, HIGH_RISK_KEYWORDS)
    ambiguity = matched_keywords(normalized, AMBIGUITY_KEYWORDS)
    complete_plan_match = COMPLETE_PLAN_RE.search(normalized)
    unresolved_decision = UNRESOLVED_DECISION_RE.search(normalized)
    factual = bool(FACTUAL_PREFIX_RE.search(normalized))
    mechanical = bool(MECHANICAL_RE.search(normalized))

    reasons: list[str] = []
    if explicit:
        recommendation = "required"
        reasons.append(f"explicit alignment request: {explicit[0]}")
    elif complete_plan_match and not unresolved_decision:
        recommendation = "not-needed"
        reasons.append(f"approved or complete plan signal: {complete_plan_match.group(0)}")
    elif factual:
        recommendation = "not-needed"
        reasons.append("factual request; investigate directly")
    elif mechanical:
        recommendation = "not-needed"
        reasons.append("narrow mechanical change")
    elif high_risk and ambiguity:
        recommendation = "required"
        reasons.append("high-risk engineering surface: " + ", ".join(high_risk[:3]))
        reasons.append("material decision ambiguity: " + ", ".join(ambiguity[:3]))
    elif high_risk:
        recommendation = "recommended"
        reasons.append("high-risk engineering surface: " + ", ".join(high_risk[:3]))
    elif len(ambiguity) >= 2:
        recommendation = "recommended"
        reasons.append("multiple ambiguity signals: " + ", ".join(ambiguity[:3]))
    else:
        recommendation = "not-needed"
        reasons.append("no material alignment signal detected")

    risk_level = "high" if high_risk else "medium" if ambiguity or explicit else "low"
    return {
        "recommendation": recommendation,
        "risk_level": risk_level,
        "reasons": reasons,
        "confidence": "heuristic",
    }


def delegation_route(message: str, contexts: list[str]) -> dict[str, Any]:
    normalized = normalize(message)
    review_hits = matched_keywords(normalized, CODE_REVIEW_KEYWORDS)
    scout_hits = matched_keywords(normalized, SCOUT_KEYWORDS)
    parallel_hits = matched_keywords(normalized, PARALLEL_VALUE_KEYWORDS)
    roles: list[dict[str, Any]] = []

    def resolution_fields(role: str, trigger: str, recording_command: str) -> dict[str, Any]:
        allowed_isolation = (
            ["hard", "inherited"]
            if role in {"security-auditor", "implementer"}
            else ["hard", "inherited", "prompt-only"]
        )
        return {
            "recording_command": recording_command,
            "resolution_order": ["dedicated", "builtin-role-injection", "main-self-review"],
            "builtin_isolation_allowed": allowed_isolation,
            "render_prompt_command": (
                f'tenetora delegation --render-prompt --role {role} '
                f'--assignment "<bounded assignment>" --host-tool <tool> '
                f'--host-agent <agent> --isolation-level <{ "|".join(allowed_isolation) }>'
            ),
            "recording_command_with_resolution": (
                f"{recording_command} --resolution-mode <dedicated|builtin-role-injection> "
                f"--host-tool <tool> --host-agent <agent> "
                f'--isolation-level <{ "|".join(allowed_isolation) }>'
            ),
            "trigger": trigger,
        }

    if "commit" in contexts or review_hits:
        recording_command = (
            "tenetora delegation --review-start --trigger commit"
            if "commit" in contexts
            else "tenetora delegation --start --role code-reviewer --trigger code-review"
        )
        roles.append(
            {
                "role": "code-reviewer",
                "reason": "an independent correctness and regression perspective can reduce self-review bias",
                **resolution_fields(
                    "code-reviewer",
                    "commit" if "commit" in contexts else "code-review",
                    recording_command,
                ),
            }
        )
    if "security" in contexts or "external-input" in contexts:
        trigger = "external-input" if "external-input" in contexts else "security"
        roles.append(
            {
                "role": "security-auditor",
                "reason": "a bounded read-only security review can add semantic coverage after deterministic guards",
                **resolution_fields(
                    "security-auditor",
                    trigger,
                    f"tenetora delegation --start --role security-auditor --trigger {trigger}",
                ),
            }
        )
    if scout_hits:
        roles.append(
            {
                "role": "codebase-scout",
                "reason": "a bounded read-only investigation can isolate broad repository context",
                **resolution_fields(
                    "codebase-scout",
                    "architecture-investigation",
                    "tenetora delegation --start --role codebase-scout --trigger architecture-investigation",
                ),
            }
        )

    return {
        "recommendation": "recommended" if roles else "not-needed",
        "roles": roles,
        "decision_basis": {
            "risk": [context for context in ("commit", "security", "external-input") if context in contexts],
            "investigation_breadth": scout_hits,
            "parallel_value": parallel_hits,
        },
        "parallel_recommended": len(roles) > 1 or bool(parallel_hits),
        "resolution_order": ["dedicated", "builtin-role-injection", "main-self-review"],
        "resolution_policy": "capability-based; platform names live in the runtime reference, not stable rules",
        "spawns_agents": False,
        "fallback": "main-agent self-review marked `未经独立审查`; do not retry indefinitely",
    }


def route_message(message: str) -> dict[str, Any]:
    normalized = normalize(message)
    contexts: list[str] = []
    reasons: list[dict[str, str]] = []
    for context in CONTEXT_ORDER:
        hits = matched_keywords(normalized, CONTEXT_KEYWORDS[context])
        if (
            context == "planning"
            and COMPLETE_PLAN_RE.search(normalized)
            and not UNRESOLVED_DECISION_RE.search(normalized)
            and hits
            and all(hit in GENERIC_PLANNING_KEYWORDS for hit in hits)
        ):
            continue
        if (
            context == "change-impact"
            and MECHANICAL_RE.search(normalized)
            and hits
            and all(hit in GENERIC_CHANGE_IMPACT_KEYWORDS for hit in hits)
        ):
            continue
        if hits:
            contexts.append(context)
            reasons.append({"context": context, "matched": hits[0]})
    if not contexts:
        contexts = ["build", "test"]
        reasons.append({"context": "build,test", "matched": "fallback: general code task"})
    alignment = alignment_route(message)
    delegation = delegation_route(message, contexts)
    commands = [f"tenetora rules --context {','.join(contexts)}"]
    if "change-impact" in contexts:
        commands.append("tenetora impact --status")
    if alignment["recommendation"] in {"required", "recommended"}:
        commands.append("use tenetora-align")
    if "commit" in contexts:
        commands.append("tenetora guard --action commit")
    if "external-input" in contexts:
        commands.append("tenetora guard --action external-input --file <untrusted-file>")
    if "harness" in contexts:
        commands.append("tenetora guard --action rules")
    commands.append('tenetora guard --action claim --claim-kind completion --verification-command "<full-command>" --verification-status passed')
    return {
        "version": 2,
        "status": "pass",
        "message": message,
        "recommended_contexts": contexts,
        "fallback_contexts": FALLBACK_CONTEXTS,
        "confidence": "heuristic",
        "reasons": reasons,
        "alignment": alignment,
        "delegation": delegation,
        "commands": commands,
    }


def print_text(payload: dict[str, Any]) -> None:
    print("# Tenetora 路由" if use_chinese() else "# Tenetora Route")
    print()
    print(("建议上下文：" if use_chinese() else "Recommended contexts: ") + ", ".join(payload["recommended_contexts"]))
    print(("回退上下文：" if use_chinese() else "Fallback contexts: ") + ", ".join(payload["fallback_contexts"]))
    alignment = payload["alignment"]
    print(
        f"决策对齐：{alignment['recommendation']}（风险等级 {alignment['risk_level']}，heuristic）"
        if use_chinese()
        else f"Decision alignment: {alignment['recommendation']} ({alignment['risk_level']} risk, heuristic)"
    )
    for reason in alignment["reasons"]:
        print(f"- {route_reason_text(reason)}")
    delegation = payload["delegation"]
    print(
        f"子代理调度：{delegation['recommendation']}（heuristic；CLI 不会启动 agent）"
        if use_chinese()
        else f"Subagent delegation: {delegation['recommendation']} (heuristic; CLI does not spawn agents)"
    )
    for role in delegation["roles"]:
        print(f"- {role['role']}: {route_reason_text(role['reason'])}")
        print(("  记录命令：" if use_chinese() else "  record with: ") + role["recording_command"])
        print(("  内置回退提示词：" if use_chinese() else "  built-in fallback prompt: ") + role["render_prompt_command"])
    print()
    print("命令：" if use_chinese() else "Commands:")
    for command in payload["commands"]:
        print(f"- {command}")
    print()
    print(
        "这是 heuristic 路由；如果实际任务范围更广，请同时加载回退上下文。"
        if use_chinese()
        else "This is a heuristic route. If the task is broader than detected, also load the fallback context."
    )


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root: Path = args.path
    if not (root / ".tenetora").is_dir():
        payload = {
            "version": 1,
            "status": "error",
            "message": harness_missing_message(root),
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(payload["message"], file=sys.stderr)
        return 2
    payload = route_message(args.message)
    if not args.no_record:
        append_event(
            root,
            {
                "type": "route",
                "status": payload["status"],
                "recommended_contexts": payload["recommended_contexts"],
                "fallback_contexts": payload["fallback_contexts"],
                "alignment": payload["alignment"],
                "delegation": {
                    "recommendation": payload["delegation"]["recommendation"],
                    "roles": [item["role"] for item in payload["delegation"]["roles"]],
                    "parallel_recommended": payload["delegation"]["parallel_recommended"],
                    "resolution_order": payload["delegation"]["resolution_order"],
                    "spawns_agents": False,
                },
                "source": "tenetora route",
            },
        )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_text(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
