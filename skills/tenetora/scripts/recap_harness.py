#!/usr/bin/env python3
"""Write a concise recap of validation and audit status into .tenetora."""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from harness_io import atomic_write_text
import alignment_state
from path_security import validate_existing_project_path

SCORE_ORDER = ("stability", "reliability", "control", "content_density", "governance_effectiveness", "overall")


def existing_project_path(raw: str) -> Path:
    try:
        return validate_existing_project_path(raw)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def load_script(name: str) -> ModuleType:
    script = Path(__file__).with_name(name)
    module_name = name.removesuffix(".py")
    spec = importlib.util.spec_from_file_location(module_name, script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def collect_validation(root: Path) -> dict[str, object]:
    module = load_script("validate_harness.py")
    failures: list[str] = []
    for rel in module.REQUIRED:
        if not (root / rel).exists():
            failures.append(f"missing {rel}")

    if hasattr(module, "current_evidence_files"):
        evidence_files = module.current_evidence_files(root)
    else:
        evidence_files = sorted((root / ".tenetora" / "changes").glob("*-extraction-evidence.json"))
    if not evidence_files:
        failures.append("missing .tenetora/changes/*-extraction-evidence.json")
    for evidence_file in evidence_files:
        try:
            json.loads(evidence_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            rel = evidence_file.relative_to(root).as_posix()
            failures.append(f"invalid json {rel}: {exc}")

    for json_rel in [".mcp.json", ".claude/settings.json"]:
        path = root / json_rel
        if path.exists():
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                failures.append(f"invalid json {json_rel}: {exc}")

    harness = root / ".tenetora"
    if harness.exists():
        for path in harness.rglob("*"):
            if path.is_file():
                for finding in module.scan_text(path):
                    failures.append(f"{finding} {path.relative_to(root).as_posix()}")

    agents = root / "AGENTS.md"
    if agents.exists() and ".tenetora/README.md" not in agents.read_text(encoding="utf-8", errors="ignore"):
        failures.append("AGENTS.md does not point to .tenetora/README.md")

    return {
        "status": "fail" if failures else "pass",
        "failures": failures,
    }


def collect_audit(root: Path) -> dict[str, object]:
    module = load_script("audit_harness_quality.py")
    return module.audit_engineering(root)


def render_issue_lines(items: object) -> list[str]:
    if not isinstance(items, list) or not items:
        return ["- None."]
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        code = item.get("code", "issue")
        path = item.get("path", "unknown")
        message = item.get("message", "")
        lines.append(f"- `{code}` `{path}`: {message}")
    return lines or ["- None."]


def ordered_score_items(scores: object) -> list[tuple[str, object]]:
    if not isinstance(scores, dict):
        return []
    items: list[tuple[str, object]] = []
    for name in SCORE_ORDER:
        if name in scores:
            items.append((name, scores[name]))
    for name, value in scores.items():
        if name not in SCORE_ORDER:
            items.append((str(name), value))
    return items


def render_recap_markdown(payload: dict[str, object], today: str) -> str:
    validation = payload["validation"]
    audit = payload["audit"]
    assert isinstance(validation, dict)
    assert isinstance(audit, dict)
    scores = audit.get("scores", {})
    score_lines: list[str] = []
    for name, value in ordered_score_items(scores):
        score_lines.append(f"- {name}: {value}/100")

    failures = validation.get("failures", [])
    failure_lines = [f"- {failure}" for failure in failures] if isinstance(failures, list) and failures else ["- None."]

    lines = [
        "# Harness Recap",
        "",
        f"Date: {today}",
        "",
        "- Changes index: `.tenetora/changes/INDEX.md`",
        "",
        "## Validation",
        "",
        f"Status: {validation.get('status', 'unknown')}",
        "",
        *failure_lines,
        "",
        "## Engineering Audit",
        "",
        f"Status: {audit.get('status', 'unknown')}",
        "",
        "### Scores",
        "",
        *(score_lines or ["- Unknown."]),
        "",
        "### Blocking Issues",
        "",
        *render_issue_lines(audit.get("blocking_issues", [])),
        "",
        "### Warnings",
        "",
        *render_issue_lines(audit.get("warnings", [])),
        "",
    ]
    return "\n".join(lines)


def last_recap_block(payload: dict[str, object], today: str, report_rel: str) -> str:
    validation = payload["validation"]
    audit = payload["audit"]
    assert isinstance(validation, dict)
    assert isinstance(audit, dict)
    scores = audit.get("scores", {})
    score_text = "unknown"
    score_items = ordered_score_items(scores)
    if score_items:
        score_text = ", ".join(f"{name} {value}" for name, value in score_items)
    return "\n".join(
        [
            "## Last Recap",
            "",
            f"Date: {today}",
            f"Validation: {validation.get('status', 'unknown')}",
            f"Audit: {audit.get('status', 'unknown')}",
            f"Scores: {score_text}",
            f"Report: {report_rel}",
            "",
        ]
    )


def replace_section(text: str, heading: str, replacement: str) -> str:
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == heading), None)
    if start is None:
        base = text.rstrip()
        return f"{base}\n\n{replacement}" if base else replacement

    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## ") and lines[index].strip() != heading:
            end = index
            break
    updated = lines[:start] + replacement.rstrip().splitlines() + lines[end:]
    return "\n".join(updated).rstrip() + "\n"


def write_recap(root: Path, payload: dict[str, object], today: str) -> str:
    changes = root / ".tenetora" / "changes"
    state = root / ".tenetora" / "state"
    changes.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)

    progress = state / "progress.md"
    report_rel = f".tenetora/changes/{today}-recap.md"
    report = root / report_rel
    with alignment_state.state_lock(progress):
        atomic_write_text(report, render_recap_markdown(payload, today))
        existing = progress.read_text(encoding="utf-8") if progress.exists() else "# Progress\n"
        atomic_write_text(progress, replace_section(existing, "## Last Recap", last_recap_block(payload, today, report_rel)))
        init_harness = load_script("init_harness.py")
        actions: list[str] = []
        init_harness.archive_old_change_artifacts(
            root,
            keep_per_category=3,
            write=True,
            actions=actions,
            protected_paths={report},
        )
        init_harness.write_changes_index(root, write=True, actions=actions)
    return report_rel


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--help", action="help", help="Show this help message and exit.")
    parser.add_argument(
        "-p",
        "--path",
        default=".",
        type=existing_project_path,
        metavar="<project-dir>",
        help="Project directory to recap. Defaults to the current directory.",
    )
    parser.add_argument("-w", "--write", action="store_true", help="Write recap files into .tenetora")
    parser.add_argument("-j", "--json", action="store_true", help="Print JSON instead of Markdown")
    args = parser.parse_args()

    root = args.path
    if args.write and not (root / ".tenetora").exists():
        print(
            ".tenetora does not exist. In your AI conversation, ask: Use Tenetora to initialize this project.",
            file=sys.stderr,
        )
        return 2

    today = dt.date.today().isoformat()
    payload: dict[str, object] = {
        "validation": collect_validation(root),
        "audit": collect_audit(root),
        "written": [],
    }
    if args.write:
        report_rel = write_recap(root, payload, today)
        payload["written"] = [report_rel, ".tenetora/state/progress.md"]

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_recap_markdown(payload, today))

    validation = payload["validation"]
    audit = payload["audit"]
    assert isinstance(validation, dict)
    assert isinstance(audit, dict)
    return 1 if validation.get("status") == "fail" or audit.get("status") == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
