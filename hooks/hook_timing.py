"""Opt-in measurement around the actual launcher; never alter hook decisions."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'skills/tenetora/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from harness_io import atomic_write_text
from path_security import (
    ensure_unredirected_directory,
    validate_existing_project_path,
    validate_unredirected_path,
    validate_unredirected_file_path,
)


def run_hook_measurement(root: Path, mode: str, payload: bytes, *, platform: str,
                         cache_state: str, origin: str, budget_ms: float,
                         environment: dict[str, str] | None = None) -> tuple[dict, bytes, bytes]:
    """Time the real launcher subprocess, including interpreter discovery/imports."""
    if not math.isfinite(budget_ms) or budget_ms <= 0:
        raise ValueError('soft budget must be a positive finite number')
    launcher = Path(__file__).with_name('run-hook.cmd' if os.name == 'nt' else 'run-hook')
    command = [str(launcher), mode, '--platform', platform]
    if os.name == 'nt':
        command = [os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/c', *command]
    sources = {'launcher': launcher, 'handler': Path(__file__).with_name('tenetora_hook.py')}
    try:
        fingerprints = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in sources.items()}
    except OSError:
        fingerprints = None
    started = time.perf_counter_ns()
    completed = subprocess.run(command, cwd=root, input=payload, capture_output=True,
        check=False, env=environment)
    elapsed = (time.perf_counter_ns() - started) / 1_000_000
    try:
        unchanged = all(hashlib.sha256(path.read_bytes()).hexdigest() == fingerprints[name] for name, path in sources.items()) if fingerprints else None
    except OSError:
        unchanged = None
    sample = {'version': 1, 'sample_id': uuid.uuid4().hex,
        'timestamp': dt.datetime.now(dt.timezone.utc).isoformat(),
        'mode': mode, 'platform': platform, 'cache_state': cache_state,
        'origin': origin, 'provenance': 'caller-declared',
        'boundary': 'launcher-subprocess-wall-time', 'duration_ms': round(elapsed, 3),
        'source_sha256': fingerprints,
        'source_unchanged': unchanged,
        'project_governance_present': (root / '.tenetora/README.md').is_file(),
        'returncode': completed.returncode, 'soft_budget_ms': budget_ms,
        'over_soft_budget': elapsed > budget_ms,
        'stdout_bytes': len(completed.stdout), 'stderr_bytes': len(completed.stderr)}
    return sample, completed.stdout, completed.stderr


def timing_summary(samples: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for sample in samples:
        value = sample.get('duration_ms')
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError('invalid latency sample')
        key = tuple(sample[key] for key in ('platform', 'mode', 'origin', 'cache_state'))
        groups.setdefault(key, []).append(sample)
    result = []
    for key, group in sorted(groups.items()):
        values = sorted(sample['duration_ms'] for sample in group)
        result.append(dict(zip(('platform', 'mode', 'origin', 'cache_state'), key),
            samples=len(values), p50_ms=values[math.ceil(len(values) * .5) - 1],
            p95_ms=values[math.ceil(len(values) * .95) - 1],
            failed=sum(sample['returncode'] != 0 for sample in group),
            over_soft_budget=sum(sample['over_soft_budget'] for sample in group),
            quantile_method='nearest-rank', small_sample=len(values) < 20))
    return result


def load_timing_snapshot(root: Path, *, days: int = 30, now: dt.datetime | None = None) -> dict:
    """Read redacted timing samples for diagnostics and insights.

    Samples are treated as untrusted cache data.  Only bounded metadata needed
    for quantiles is returned; prompt, session, path, and hook output fields are
    never propagated to a consumer.
    """
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
        raise ValueError('timing window must be between 1 and 365 days')
    current = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    directory = root / '.tenetora/.cache/hook-timing'
    if not directory.exists():
        return {'version': 1, 'status': 'not-observed', 'window_days': days, 'samples': 0,
            'invalid_samples': 0, 'summary': [], 'over_soft_budget': 0, 'failed': 0,
            'small_sample_groups': 0}
    try:
        validate_unredirected_path(directory, label='hook timing cache')
    except (OSError, RuntimeError, ValueError):
        return {'version': 1, 'status': 'invalid', 'window_days': days, 'samples': 0,
            'invalid_samples': 1, 'summary': [], 'over_soft_budget': 0, 'failed': 0,
            'small_sample_groups': 0}
    start = current - dt.timedelta(days=days)
    samples: list[dict] = []
    invalid = 0
    for path in sorted(directory.glob('sample-*.json')):
        try:
            validate_unredirected_file_path(path, label='hook timing sample')
            payload = json.loads(path.read_text(encoding='utf-8'))
            timestamp = payload.get('timestamp')
            if not isinstance(timestamp, str):
                raise ValueError('timestamp missing')
            raw_timestamp = timestamp[:-1] + '+00:00' if timestamp.endswith('Z') else timestamp
            observed_at = dt.datetime.fromisoformat(raw_timestamp)
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=dt.timezone.utc)
            observed_at = observed_at.astimezone(dt.timezone.utc)
            if observed_at > current or observed_at < start:
                continue
            platform = payload.get('platform')
            mode = payload.get('mode')
            origin = payload.get('origin')
            cache_state = payload.get('cache_state')
            duration = payload.get('duration_ms')
            returncode = payload.get('returncode')
            budget = payload.get('soft_budget_ms')
            over_budget = payload.get('over_soft_budget')
            if (not isinstance(platform, str) or not platform or len(platform) > 32
                    or not isinstance(mode, str) or not mode or len(mode) > 64
                    or origin not in {'native-host', 'replay'}
                    or cache_state not in {'cold', 'warm', 'unknown'}
                    or type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0
                    or type(returncode) is not int
                    or type(budget) not in (int, float) or not math.isfinite(budget) or budget <= 0
                    or type(over_budget) is not bool):
                raise ValueError('sample schema invalid')
            samples.append({'platform': platform, 'mode': mode, 'origin': origin,
                'cache_state': cache_state, 'duration_ms': duration,
                'returncode': returncode, 'over_soft_budget': over_budget})
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            invalid += 1
    summary = timing_summary(samples)
    return {'version': 1, 'status': 'observed' if samples else ('invalid' if invalid else 'not-observed'),
        'window_days': days, 'samples': len(samples), 'invalid_samples': invalid,
        'summary': summary,
        'over_soft_budget': sum(int(item['over_soft_budget']) for item in samples),
        'failed': sum(item['returncode'] != 0 for item in samples),
        'small_sample_groups': sum(bool(item['small_sample']) for item in summary),
        'limitations': ['samples are opt-in replay or host observations',
            'quantiles use nearest-rank and are descriptive, not a performance guarantee']}


def save_sample(root: Path, sample: dict) -> None:
    directory = root / '.tenetora/.cache/hook-timing'
    ensure_unredirected_directory(directory, label='hook timing cache')
    path = validate_unredirected_file_path(directory / f"sample-{sample['sample_id']}.json", label='hook timing sample')
    atomic_write_text(path, json.dumps(sample, indent=2) + '\n')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode')
    parser.add_argument('--path', default='.')
    parser.add_argument('--platform', choices=('claude', 'codex', 'cursor', 'opencode', 'pi', 'zcode'), required=True)
    parser.add_argument('--cache-state', choices=('cold', 'warm', 'unknown'), default='unknown')
    parser.add_argument('--origin', choices=('native-host', 'replay'), default='replay')
    parser.add_argument('--soft-budget-ms', type=float, default=1000)
    args = parser.parse_args()
    root = validate_existing_project_path(args.path)
    if not math.isfinite(args.soft_budget_ms) or args.soft_budget_ms <= 0:
        parser.error('soft budget must be a positive finite number')
    sample, output, error = run_hook_measurement(root, args.mode, sys.stdin.buffer.read(),
        platform=args.platform, cache_state=args.cache_state, origin=args.origin, budget_ms=args.soft_budget_ms)
    # Pass the decision through exactly, even when measurement storage fails.
    sys.stdout.buffer.write(output)
    sys.stderr.buffer.write(error)
    try:
        save_sample(root, sample)
    except (OSError, RuntimeError, ValueError):
        print('Tenetora hook timing unavailable; original hook result preserved.', file=sys.stderr)
    return sample['returncode']


if __name__ == '__main__':
    raise SystemExit(main())
