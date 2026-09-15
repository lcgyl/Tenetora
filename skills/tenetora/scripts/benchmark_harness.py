#!/usr/bin/env python3
"""Benchmark Tenetora project operations."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import alignment_state  # noqa: E402
from harness_io import atomic_write_text  # noqa: E402
from path_security import harness_missing_message, validate_existing_project_path  # noqa: E402


BENCHMARK_HISTORY_REL = ".tenetora/state/benchmark-history.json"


def existing_project_path(raw: str) -> Path:
    try:
        return validate_existing_project_path(raw)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(
        prog="tenetora benchmark",
        description="Measure Tenetora validation, audit, and guardrail operation latency.",
        add_help=False,
    )
    command_parser.add_argument("--help", action="help", help="Show this help message and exit.")
    command_parser.add_argument(
        "-p",
        "--path",
        default=".",
        type=existing_project_path,
        metavar="<project-dir>",
        help="Project directory to benchmark. Defaults to the current directory.",
    )
    command_parser.add_argument("-j", "--json", action="store_true", help="Print JSON instead of Markdown.")
    command_parser.add_argument(
        "--include-refresh",
        action="store_true",
        help="Also time refresh --strategy diff. This can write review artifacts under .tenetora/changes.",
    )
    command_parser.add_argument(
        "--write",
        action="store_true",
        help=f"Append benchmark result to {BENCHMARK_HISTORY_REL}.",
    )
    command_parser.add_argument(
        "--keep-going",
        action="store_true",
        help="Continue timing later steps after a step fails.",
    )
    return command_parser


def script_dir() -> Path:
    return Path(__file__).resolve().parent


def benchmark_steps(root: Path, include_refresh: bool) -> list[tuple[str, list[str]]]:
    scripts = script_dir()
    steps = [
        ("validate", [sys.executable, str(scripts / "validate_harness.py"), "--path", str(root)]),
        (
            "audit-json",
            [sys.executable, str(scripts / "audit_harness_quality.py"), "--path", str(root), "--format", "json"],
        ),
        (
            "run-all-python",
            [sys.executable, str(scripts / "run_all_guardrails.py"), "--path", str(root), "--runner", "python"],
        ),
    ]
    if include_refresh:
        steps.append(
            (
                "refresh-diff",
                [
                    sys.executable,
                    str(scripts / "refresh_harness.py"),
                    "--path",
                    str(root),
                    "--strategy",
                    "diff",
                    "--migrate",
                    "plan",
                    "--entrypoints",
                    "plan",
                ],
            )
        )
    return steps


def run_step(root: Path, name: str, command: list[str]) -> dict[str, object]:
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    elapsed = time.perf_counter() - started
    return {
        "name": name,
        "duration_ms": round(elapsed * 1000, 2),
        "returncode": completed.returncode,
        "stdout_bytes": len(completed.stdout.encode("utf-8")),
        "stderr_bytes": len(completed.stderr.encode("utf-8")),
    }


def load_history(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"version": 1, "runs": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "runs": []}
    if not isinstance(payload, dict):
        return {"version": 1, "runs": []}
    runs = payload.get("runs")
    if not isinstance(runs, list):
        payload["runs"] = []
    payload["version"] = 1
    return payload


def write_history(root: Path, result: dict[str, object]) -> None:
    harness = root / ".tenetora"
    if not harness.is_dir():
        raise FileNotFoundError(
            harness_missing_message(root)
        )
    path = root / BENCHMARK_HISTORY_REL
    with alignment_state.state_lock(path):
        payload = load_history(path)
        runs = payload.get("runs", [])
        assert isinstance(runs, list)
        stored = dict(result)
        stored["root"] = "."
        runs.append(stored)
        payload["runs"] = runs[-100:]
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def print_markdown(result: dict[str, object]) -> None:
    print("# Tenetora Benchmark")
    print()
    print(f"- root: `{result['root']}`")
    print(f"- status: `{result['status']}`")
    print(f"- total_ms: `{result['total_ms']}`")
    print()
    print("| Step | Duration ms | Return Code | Output |")
    print("| --- | ---: | ---: | --- |")
    for step in result["steps"]:
        assert isinstance(step, dict)
        output = f"stdout={step['stdout_bytes']}B stderr={step['stderr_bytes']}B"
        print(f"| `{step['name']}` | {step['duration_ms']} | {step['returncode']} | {output} |")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root: Path = args.path
    started = time.perf_counter()
    steps: list[dict[str, object]] = []
    status = "pass"
    for name, command in benchmark_steps(root, args.include_refresh):
        step = run_step(root, name, command)
        steps.append(step)
        if int(step["returncode"]) != 0:
            status = "fail"
            if not args.keep_going:
                break
    total_ms = round((time.perf_counter() - started) * 1000, 2)
    result = {
        "version": 1,
        "root": str(root),
        "created_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "status": status,
        "include_refresh": bool(args.include_refresh),
        "total_ms": total_ms,
        "steps": steps,
    }
    if args.write:
        try:
            write_history(root, result)
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_markdown(result)
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
