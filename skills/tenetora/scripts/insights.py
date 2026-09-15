#!/usr/bin/env python3
"""Read-only, bounded governance-trail insights."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from governance_trail import TRAIL_REL  # noqa: E402


DEFAULT_DAYS = 90
MAX_DAYS = 365
MIN_EVENTS_FOR_TREND = 3
RULE_KEY_PREFIXES = (".tenetora/rules/", ".tenetora/workflows/", ".tenetora/guardrails/")
CONTEXT_TOKEN_ESTIMATE_BASIS = "heuristic_chars_div_4"
KNOWN_EVENT_TYPES = {
    "alignment-guard",
    "change-impact-preflight",
    "context-injection",
    "guard-action",
    "prompt-guard",
    "rules-context",
    "subagent-dispatch",
    "subagent-lifecycle",
    "verification-claim",
}
KNOWN_ACTIONS = {
    "alignment",
    "claim",
    "commit",
    "external-input",
    "guard",
    "rules",
}


def parse_timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def read_trail(root: Path) -> dict[str, Any]:
    path = root / TRAIL_REL
    if not path.is_file():
        return {"status": "missing", "events": [], "ignored_events": 0, "message": "governance trail is not present"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {
            "status": "invalid-data",
            "events": [],
            "ignored_events": 1,
            "message": "governance trail cannot be read as valid JSON",
        }
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        return {
            "status": "invalid-data",
            "events": [],
            "ignored_events": 1,
            "message": "governance trail has an unsupported shape",
        }
    events = [item for item in payload["events"] if isinstance(item, dict)]
    return {
        "status": "ready",
        "events": events,
        "ignored_events": len(payload["events"]) - len(events),
        "message": "",
    }


def safe_event_type(event: dict[str, Any]) -> str:
    value = event.get("type")
    return value if value in KNOWN_EVENT_TYPES else "unknown"


def safe_action(event: dict[str, Any]) -> str:
    value = event.get("action")
    return value if value in KNOWN_ACTIONS else "unknown"


def event_status(event: dict[str, Any]) -> str:
    value = event.get("status")
    return value if value in {"pass", "fail", "warn", "skip", "error"} else "unknown"


def safe_rule_key(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    key = value.replace("\\", "/").strip()
    if not key.startswith(RULE_KEY_PREFIXES) or ".." in key.split("/"):
        return None
    if not key.endswith((".md", ".json", ".sh", ".yaml", ".yml")):
        return None
    return key


def discover_rule_keys(root: Path) -> list[str]:
    keys: set[str] = set()
    for relative in (".tenetora/README.md",):
        if (root / relative).is_file():
            keys.add(relative)
    for relative_root in (".tenetora/rules", ".tenetora/workflows", ".tenetora/guardrails"):
        directory = root / relative_root
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            try:
                key = safe_rule_key(path.relative_to(root).as_posix())
            except ValueError:
                key = None
            if key:
                keys.add(key)
    return sorted(keys)


def rule_effectiveness(
    root: Path,
    selected: list[tuple[dict[str, Any], dt.datetime]],
) -> dict[str, Any]:
    discovered = set(discover_rule_keys(root))
    observed: dict[str, dict[str, Any]] = {}
    context_events = 0
    for event, timestamp in selected:
        if safe_event_type(event) != "rules-context":
            continue
        context_events += 1
        files = event.get("files")
        if not isinstance(files, list):
            continue
        timestamp_text = timestamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        for raw_key in files:
            key = safe_rule_key(raw_key)
            if key is None:
                continue
            discovered.add(key)
            item = observed.setdefault(key, {"hit_count": 0, "last_hit": None})
            item["hit_count"] += 1
            item["last_hit"] = timestamp_text

    counts = [int(item["hit_count"]) for item in observed.values() if int(item["hit_count"]) > 0]
    average = sum(counts) / len(counts) if counts else 0.0
    threshold = max(5, math.ceil(average * 3)) if counts else None
    items = [
        {
            "rule_key": key,
            "hit_count": int(observed.get(key, {}).get("hit_count", 0)),
            "last_hit": observed.get(key, {}).get("last_hit"),
        }
        for key in sorted(discovered)
    ]
    zero_hit = [item["rule_key"] for item in items if item["hit_count"] == 0]
    high_frequency = [
        item["rule_key"]
        for item in items
        if threshold is not None and item["hit_count"] >= threshold
    ]
    return {
        "status": "ready" if context_events else "insufficient-data",
        "rules_context_events": context_events,
        "observed_rules": len(items),
        "total_hits": sum(item["hit_count"] for item in items),
        "items": items,
        "zero_hit_keys": zero_hit,
        "high_frequency_keys": high_frequency,
        "high_frequency_threshold": threshold,
        "limitations": [
            "hit means a rules-context load was recorded; it does not prove enforcement or task success",
            "zero-hit keys may be newly added, optional, or outside the selected time window",
        ],
    }


def delegation_quality(selected: list[tuple[dict[str, Any], dt.datetime]]) -> dict[str, Any]:
    latest: dict[str, dict[str, Any]] = {}
    for event, _ in selected:
        if safe_event_type(event) != "subagent-dispatch":
            continue
        dispatch_id = event.get("dispatch_id")
        if not isinstance(dispatch_id, str) or not dispatch_id.startswith("ah-dispatch-"):
            continue
        latest[dispatch_id] = event
    completed = [event for event in latest.values() if event.get("status") != "started"]
    status_counts = Counter(str(event.get("status")) for event in completed)
    feedback_events = [
        event
        for event in completed
        if any(
            event.get(field) not in {None, "", "not-recorded"}
            for field in ("finding_outcome", "fix_regression", "verification_status")
        )
    ]
    return {
        "status": "ready" if completed else "insufficient-data",
        "dispatches": len(completed),
        "passed": status_counts.get("passed", 0),
        "blockers": status_counts.get("blocker", 0),
        "failed": status_counts.get("failed", 0),
        "unavailable": status_counts.get("unavailable", 0),
        "feedback_recorded": len(feedback_events),
        "finding_confirmed": sum(event.get("finding_outcome") == "confirmed" for event in feedback_events),
        "fix_regressions": sum(event.get("fix_regression") == "failed" for event in feedback_events),
        "verification_passed": sum(event.get("verification_status") == "passed" for event in feedback_events),
        "verification_failed": sum(event.get("verification_status") == "failed" for event in feedback_events),
        "limitations": [
            "quality counts use explicit bounded metadata; report bodies are not analyzed",
            "a completed host process is not treated as a passed review without an explicit result",
        ],
    }


def empty_payload(*, days: int, now: dt.datetime, source_status: str, message: str = "") -> dict[str, Any]:
    start = now - dt.timedelta(days=days)
    data_status = "empty" if source_status == "ready" else source_status
    return {
        "version": 1,
        "status": data_status,
        "window": {
            "days": days,
            "start": start.date().isoformat(),
            "end": now.date().isoformat(),
            "timezone": "UTC",
        },
        "data_quality": {
            "status": data_status,
            "events_seen": 0,
            "events_in_window": 0,
            "ignored_events": 0,
            "invalid_timestamps": 0,
            "future_events": 0,
            "distinct_days": 0,
            "distinct_sessions": 0,
            "limitations": [message] if message else [],
        },
        "trend": {
            "status": "insufficient-data" if source_status == "ready" else source_status,
            "guard": {"attempts": 0, "passed": 0, "failed": 0, "pass_rate": None, "by_action": {}},
            "claims": {
                "attempts": 0,
                "passed": 0,
                "failed": 0,
                "average_delay": {"status": "unavailable", "reason": "missing-start-timestamp"},
            },
            "failure_types": {},
        },
        "patterns": {
            "alignment_triggers": 0,
            "failure_by_event": {},
            "most_frequent_failure": None,
        },
        "value_proof": {
            "interventions": 0,
            "failed_claims": 0,
            "blocked_commits": 0,
            "alignment_blocks": 0,
            "external_input_blocks": 0,
            "context_reuse_events": 0,
            "estimated_context_chars_saved": 0,
            "estimated_context_tokens_saved": 0.0,
            "context_token_estimate_basis": CONTEXT_TOKEN_ESTIMATE_BASIS,
        },
        "rule_effectiveness": {
            "status": "insufficient-data",
            "rules_context_events": 0,
            "observed_rules": 0,
            "total_hits": 0,
            "items": [],
            "zero_hit_keys": [],
            "high_frequency_keys": [],
            "high_frequency_threshold": None,
            "limitations": ["no rules-context events were available in the selected window"],
        },
        "delegation_quality": {
            "status": "insufficient-data",
            "dispatches": 0,
            "passed": 0,
            "blockers": 0,
            "failed": 0,
            "unavailable": 0,
            "feedback_recorded": 0,
            "finding_confirmed": 0,
            "fix_regressions": 0,
            "verification_passed": 0,
            "verification_failed": 0,
            "limitations": ["no completed dispatch events were available in the selected window"],
        },
    }


def analyze(root: Path, *, days: int = DEFAULT_DAYS, now: dt.datetime | None = None) -> dict[str, Any]:
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_DAYS}")
    current = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    source = read_trail(root)
    if source["status"] != "ready":
        payload = empty_payload(days=days, now=current, source_status=source["status"], message=source["message"])
        payload["data_quality"]["ignored_events"] = source["ignored_events"]
        return payload

    start = current - dt.timedelta(days=days)
    events_seen = len(source["events"])
    ignored_events = int(source["ignored_events"])
    invalid_timestamps = 0
    future_events = 0
    selected: list[tuple[dict[str, Any], dt.datetime]] = []
    for event in source["events"]:
        timestamp = parse_timestamp(event.get("timestamp"))
        if timestamp is None:
            invalid_timestamps += 1
            continue
        if timestamp > current:
            future_events += 1
            continue
        if timestamp >= start:
            selected.append((event, timestamp))

    event_days = {timestamp.date().isoformat() for _, timestamp in selected}
    session_keys = {
        (str(event.get("session_id")), str(event.get("conversation_id")))
        for event, _ in selected
        if event.get("session_id") or event.get("conversation_id")
    }
    event_count = len(selected)
    trend_status = "ready" if event_count >= MIN_EVENTS_FOR_TREND else "insufficient-data"
    if event_count == 0:
        trend_status = "empty"

    guard_events = [
        (event, timestamp)
        for event, timestamp in selected
        if safe_event_type(event) in {"guard-action", "alignment-guard", "prompt-guard"}
    ]
    guard_passed = sum(event_status(event) == "pass" for event, _ in guard_events)
    guard_failed = sum(event_status(event) == "fail" for event, _ in guard_events)
    guard_actions = Counter(safe_action(event) for event, _ in guard_events)

    claim_events = [event for event, _ in selected if safe_event_type(event) == "verification-claim"]
    claim_passed = sum(
        event_status(event) == "pass" and event.get("verification_status") == "passed"
        for event in claim_events
    )
    claim_failed = len(claim_events) - claim_passed

    failure_types: Counter[str] = Counter()
    failure_by_event: Counter[str] = Counter()
    failed_claims = 0
    blocked_commits = 0
    alignment_blocks = 0
    external_input_blocks = 0
    interventions = 0
    context_reuse_events = 0
    estimated_context_chars_saved = 0
    for event, _ in selected:
        if safe_event_type(event) == "context-injection":
            if event.get("mode") == "reference":
                context_reuse_events += 1
            saved = event.get("estimated_chars_saved")
            if isinstance(saved, int) and saved >= 0:
                estimated_context_chars_saved += saved
        status = event_status(event)
        kind = safe_event_type(event)
        action = safe_action(event)
        if status != "fail":
            continue
        interventions += 1
        failure_key = f"{kind}:{action}" if action != "unknown" else kind
        failure_types[failure_key] += 1
        failure_by_event[kind] += 1
        if kind == "verification-claim":
            failed_claims += 1
        if action == "commit":
            blocked_commits += 1
        if kind == "alignment-guard" or action == "alignment":
            alignment_blocks += 1
        if kind == "prompt-guard" or action == "external-input":
            external_input_blocks += 1

    guard_attempts = len(guard_events)
    trend_limitations = [
        "claim delay is unavailable because the trail has no verification start timestamp",
        "failure types are bounded event/action categories, not raw error text",
    ]
    return {
        "version": 1,
        "status": "ready" if trend_status == "ready" else trend_status,
        "window": {
            "days": days,
            "start": start.date().isoformat(),
            "end": current.date().isoformat(),
            "timezone": "UTC",
        },
        "data_quality": {
            "status": trend_status,
            "events_seen": events_seen,
            "events_in_window": event_count,
            "ignored_events": ignored_events,
            "invalid_timestamps": invalid_timestamps,
            "future_events": future_events,
            "distinct_days": len(event_days),
            "distinct_sessions": len(session_keys),
            "limitations": trend_limitations,
        },
        "trend": {
            "status": trend_status,
            "guard": {
                "attempts": guard_attempts,
                "passed": guard_passed,
                "failed": guard_failed,
                "pass_rate": round(guard_passed / guard_attempts, 4) if guard_attempts else None,
                "by_action": dict(sorted(guard_actions.items())),
            },
            "claims": {
                "attempts": len(claim_events),
                "passed": claim_passed,
                "failed": claim_failed,
                "average_delay": {"status": "unavailable", "reason": "missing-start-timestamp"},
            },
            "failure_types": dict(sorted(failure_types.items())),
        },
        "patterns": {
            "alignment_triggers": sum(safe_event_type(event) == "alignment-guard" for event, _ in selected),
            "failure_by_event": dict(sorted(failure_by_event.items())),
            "most_frequent_failure": failure_types.most_common(1)[0][0] if failure_types else None,
        },
        "value_proof": {
            "interventions": interventions,
            "failed_claims": failed_claims,
            "blocked_commits": blocked_commits,
            "alignment_blocks": alignment_blocks,
            "external_input_blocks": external_input_blocks,
            "context_reuse_events": context_reuse_events,
            "estimated_context_chars_saved": estimated_context_chars_saved,
            "estimated_context_tokens_saved": round(estimated_context_chars_saved / 4, 2),
            "context_token_estimate_basis": CONTEXT_TOKEN_ESTIMATE_BASIS,
        },
        "rule_effectiveness": rule_effectiveness(root, selected),
        "delegation_quality": delegation_quality(selected),
    }


def label(value: str | None, language: str) -> str:
    labels = {
        "guard-action:commit": ("commit guard", "提交门禁"),
        "guard-action:claim": ("claim guard", "完成声明门禁"),
        "alignment-guard:alignment": ("alignment guard", "决策对齐门禁"),
        "prompt-guard:external-input": ("external-input guard", "外部输入门禁"),
        "verification-claim": ("verification claim", "验证声明"),
        "unknown": ("unknown", "未知"),
    }
    pair = labels.get(value or "unknown", (value or "unknown", value or "未知"))
    return pair[1] if language == "zh" else pair[0]


def render_text(payload: dict[str, Any], language: str) -> str:
    window = payload["window"]
    quality = payload["data_quality"]
    guard = payload["trend"]["guard"]
    claims = payload["trend"]["claims"]
    patterns = payload["patterns"]
    value = payload["value_proof"]
    rule_health = payload["rule_effectiveness"]
    delegation = payload["delegation_quality"]
    if language == "zh":
        lines = [
            "Tenetora 洞察",
            f"窗口：最近 {window['days']} 天（{window['start']} 至 {window['end']} UTC）",
            f"数据：{payload['status']}；窗口事件 {quality['events_in_window']}；有效日期 {quality['distinct_days']} 天",
            "",
            "趋势：",
            f"  门禁：{guard['attempts']} 次，{guard['passed']} 次通过，{guard['failed']} 次失败，" +
            (f"通过率 {guard['pass_rate']:.1%}" if guard["pass_rate"] is not None else "通过率不可用"),
            f"  完成声明：{claims['attempts']} 次，{claims['passed']} 次通过，{claims['failed']} 次失败",
            f"  最高频失败类型：{label(patterns['most_frequent_failure'], language)}",
            "",
            "模式：",
            f"  决策对齐触发：{patterns['alignment_triggers']} 次",
            f"  失败事件：{', '.join(f'{label(key, language)}={count}' for key, count in patterns['failure_by_event'].items()) or '无'}",
            "",
            "价值证明：",
            f"  记录的干预：{value['interventions']} 次",
            f"  被拦截的完成声明：{value['failed_claims']} 次",
            f"  被拦截的提交：{value['blocked_commits']} 次",
            f"  被阻断的决策对齐：{value['alignment_blocks']} 次",
            f"  被阻断的外部输入：{value['external_input_blocks']} 次",
            f"  规则上下文复用：{value['context_reuse_events']} 次，估算节省 {value['estimated_context_chars_saved']} 字符（约 {value['estimated_context_tokens_saved']} tokens）",
            f"  Token 估算基准：{value['context_token_estimate_basis']}",
            "",
            "规则健康：",
            f"  记录加载 {rule_health['rules_context_events']} 次；规则键 {rule_health['observed_rules']} 个；命中 {rule_health['total_hits']} 次",
            f"  零命中：{len(rule_health['zero_hit_keys'])} 个；高频提示：{len(rule_health['high_frequency_keys'])} 个",
            "  规则命中代表 rules-context 被加载，不等于规则被执行或任务成功。",
            "",
            "派发质量：",
            f"  已完成派发 {delegation['dispatches']} 次；通过 {delegation['passed']}；阻塞 {delegation['blockers']}；不可用 {delegation['unavailable']}",
            f"  已记录后续反馈 {delegation['feedback_recorded']} 次；发现已证实 {delegation['finding_confirmed']}；验证通过 {delegation['verification_passed']}",
            "  仅统计显式元数据；报告正文不进入洞察分析。",
            "",
            "边界：",
            "  完成声明延迟不可用：治理轨迹没有验证开始时间。",
            "  失败类型只使用受控事件/动作类别，不显示原始错误文本。",
            "  上下文节省量只是按字符数除以 4 的粗略估算，不代表供应商计费数据。",
        ]
    else:
        lines = [
            "Tenetora Insights",
            f"Window: last {window['days']} days ({window['start']} to {window['end']} UTC)",
            f"Data: {payload['status']}; {quality['events_in_window']} events in window; {quality['distinct_days']} valid days",
            "",
            "Trend:",
            f"  Guards: {guard['attempts']} attempts, {guard['passed']} passed, {guard['failed']} failed, " +
            (f"pass rate {guard['pass_rate']:.1%}" if guard["pass_rate"] is not None else "pass rate unavailable"),
            f"  Verification claims: {claims['attempts']} attempts, {claims['passed']} passed, {claims['failed']} failed",
            f"  Most frequent failure: {label(patterns['most_frequent_failure'], language)}",
            "",
            "Patterns:",
            f"  Alignment triggers: {patterns['alignment_triggers']}",
            f"  Failed events: {', '.join(f'{label(key, language)}={count}' for key, count in patterns['failure_by_event'].items()) or 'none'}",
            "",
            "Value proof:",
            f"  Recorded interventions: {value['interventions']}",
            f"  Blocked verification claims: {value['failed_claims']}",
            f"  Blocked commits: {value['blocked_commits']}",
            f"  Blocked alignments: {value['alignment_blocks']}",
            f"  Blocked external inputs: {value['external_input_blocks']}",
            f"  Reused rule context: {value['context_reuse_events']} times; estimated savings {value['estimated_context_chars_saved']} chars (~{value['estimated_context_tokens_saved']} tokens)",
            f"  Token estimate basis: {value['context_token_estimate_basis']}",
            "",
            "Rule health:",
            f"  Recorded loads: {rule_health['rules_context_events']}; rule keys: {rule_health['observed_rules']}; hits: {rule_health['total_hits']}",
            f"  Zero-hit: {len(rule_health['zero_hit_keys'])}; high-frequency alerts: {len(rule_health['high_frequency_keys'])}",
            "  A hit means a rules-context load was recorded; it does not prove enforcement or task success.",
            "",
            "Delegation quality:",
            f"  Completed dispatches: {delegation['dispatches']}; passed {delegation['passed']}; blockers {delegation['blockers']}; unavailable {delegation['unavailable']}",
            f"  Feedback recorded: {delegation['feedback_recorded']}; findings confirmed {delegation['finding_confirmed']}; verifications passed {delegation['verification_passed']}",
            "  Only explicit metadata is counted; report bodies are not analyzed.",
            "",
            "Boundaries:",
            "  Claim delay is unavailable because the governance trail has no verification start timestamp.",
            "  Failure types use bounded event/action categories and never expose raw error text.",
            "  Context savings are a rough chars/4 estimate, not provider billing data.",
        ]
    if quality["status"] in {"empty", "insufficient-data", "invalid-data", "missing"}:
        message = "数据不足，不能据此判断趋势。" if language == "zh" else "Insufficient data; no trend should be inferred."
        lines.insert(3, message)
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only governance-trail insights.")
    parser.add_argument("--path", default=".", type=Path, help="Project directory.")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="Lookback window in days (1-365).")
    parser.add_argument("--json", action="store_true", help="Print complete machine-readable output.")
    parser.add_argument("--lang", choices=("en", "zh"), default="en", help="Text output language.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.path.expanduser().resolve()
    try:
        payload = analyze(root, days=args.days)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_text(payload, args.lang))
    return 2 if payload["status"] in {"missing", "invalid-data"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
