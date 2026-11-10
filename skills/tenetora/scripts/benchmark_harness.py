#!/usr/bin/env python3
"""Benchmark Tenetora project operations."""

from __future__ import annotations

import argparse
import importlib.util
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import alignment_state  # noqa: E402
from harness_io import atomic_write_text  # noqa: E402
from path_security import harness_missing_message, validate_existing_project_path, validate_unredirected_path, validate_unredirected_file_path  # noqa: E402

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
    command_parser.add_argument('--hooks', action='store_true', help='Measure actual launchers using replay payloads in isolated project snapshots.')
    command_parser.add_argument('--samples', type=int, default=10, help='Cold/warm pairs per event for --hooks (1..100).')
    command_parser.add_argument('--hook-platform', choices=('claude', 'codex'), default='codex')
    command_parser.add_argument('--soft-budget-ms', type=float, default=1000)
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


def benchmark_hooks(root: Path, args: argparse.Namespace) -> dict[str, object]:
    governance = root / '.tenetora'
    if governance.is_symlink() or not (governance / 'README.md').is_file():
        raise ValueError('Hook benchmark requires an existing unredirected .tenetora/README.md.')
    import ensure_cli
    hooks = ensure_cli.hook_source_dir()
    if hooks is None:
        bin_dir = ensure_cli.default_bin_dir()
        if not ensure_cli.hook_runtime_ready(bin_dir):
            raise ValueError('Managed Hook timing files are unavailable; upgrade Tenetora and retry.')
        hooks = ensure_cli.runtime_hook_dir(bin_dir)
    validate_unredirected_path(hooks, label='Hook timing directory')
    helper = validate_unredirected_file_path(hooks / 'hook_timing.py', label='Hook timing helper')
    spec = importlib.util.spec_from_file_location('tenetora_benchmark_hook_timing', helper)
    if spec is None or spec.loader is None:
        raise ValueError('Managed Hook timing helper could not be loaded.')
    timing = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(timing)

    samples = []
    for mode, event_name in (('session-start', 'SessionStart'), ('user-prompt-submit', 'UserPromptSubmit'), ('stop', 'Stop')):
        for _ in range(args.samples):
            with tempfile.TemporaryDirectory(prefix='tenetora-hook-benchmark-') as raw:
                snapshot = Path(raw)
                for name in ('README.md', 'INDEX.md', 'rules', 'workflows', 'agents', 'wiki', 'manifest.json', 'profile.json'):
                    source = root / '.tenetora' / name
                    target = snapshot / '.tenetora' / name
                    if source.is_symlink() or (source.is_dir() and any(path.is_symlink() for path in source.rglob('*'))):
                        raise ValueError('Hook benchmark requires unredirected governance snapshot inputs.')
                    if source.is_dir():
                        shutil.copytree(source, target, symlinks=True)
                    elif source.is_file():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, target)
                environment = {**os.environ, 'TENETORA_HOME': str(snapshot / 'machine-home'), 'TENETORA_PYTHON': sys.executable}
                payload = json.dumps({'hook_event_name': event_name, 'cwd': str(snapshot),
                    'session_id': 'latency-observation', 'prompt': 'Explain the existing project'}).encode()
                for phase in ('cold', 'warm'):
                    sample, _, _ = timing.run_hook_measurement(snapshot, mode, payload, platform=args.hook_platform,
                        cache_state=phase, origin='replay', budget_ms=args.soft_budget_ms, environment=environment)
                    samples.append(sample)
    return {'version': 1, 'status': 'pass' if all(sample['returncode'] == 0 for sample in samples) else 'fail',
        'boundary': 'launcher-subprocess-wall-time', 'origin': 'replay',
        'cache_definition': 'cold: first event in fresh governance snapshot; warm: consecutive event in same snapshot. Both use fresh processes; OS disk caches are uncontrolled.',
        'limitations': 'Replay exercises the real launcher/handler; it does not prove native host dispatch, host queue latency, or model latency. Quantiles include all outcomes.',
        'samples': samples, 'summary': timing.timing_summary(samples)}


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
    if args.hooks:
        if not 1 <= args.samples <= 100:
            parser().error('--samples must be in 1..100')
        try:
            result = benchmark_hooks(root, args)
        except (OSError, ValueError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.write:
            write_history(root, result)
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else json.dumps(result, ensure_ascii=False))
        return 0 if result['status'] == 'pass' else 1
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
