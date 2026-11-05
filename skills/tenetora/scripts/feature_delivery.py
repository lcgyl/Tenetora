#!/usr/bin/env python3
"""Small, evidence-bound feature delivery package commands."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import shlex
import time
import uuid
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from claim_evidence import is_claim_proof, validate_claim, verification_command_hash  # noqa: E402
from governance_trail import append_event  # noqa: E402
from harness_io import atomic_write_text  # noqa: E402
from path_security import (
    ensure_unredirected_directory,
    validate_existing_project_path,
    validate_unredirected_file_path,  # noqa: E402
)
from prompt_guard import PromptInput, scan_input
from verification_plan import build_plan, observation
from worktree_fingerprint import (
    git_head,
    git_repository_env,
    git_state_fingerprint,
    normalize_scopes,
)

FEATURE_ROOT = Path("docs/features")
STATE_REL = Path(".tenetora/state/features.json")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
TASK_RE = re.compile(r"^T[0-9]{2,4}$")
EVIDENCE_LEVELS = {"L1", "L2", "L3", "L4"}
EVIDENCE_MODES = {"static", "mock", "real", "mixed"}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def project_path(raw: str) -> Path:
    try:
        return validate_existing_project_path(raw)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def validate_feature_id(value: object) -> str:
    """Validate a persisted or user-supplied feature identifier before path use."""
    if not isinstance(value, str) or not SLUG_RE.fullmatch(value):
        raise ValueError('Feature id must be a lowercase project slug.')
    return value


def validate_task_id(value: object) -> str:
    """Validate a persisted or user-supplied task identifier before path use."""
    if not isinstance(value, str) or not TASK_RE.fullmatch(value):
        raise ValueError('Task id must look like T01.')
    return value


def validate_feature_record(feature: object) -> dict[str, Any]:
    """Validate the path-bearing portion of one persisted feature record.

    Feature state is user-editable JSON.  IDs are therefore a security boundary:
    every consumer that derives a projection path must see the same strict
    schema, including read-only status inspection.
    """
    if not isinstance(feature, dict):
        raise ValueError('Feature state contains a non-object feature record.')
    feature_id = validate_feature_id(feature.get('id'))
    expected_package = (FEATURE_ROOT / feature_id).as_posix()
    if feature.get('package') != expected_package:
        raise ValueError('Feature state package must match its project-relative feature id.')
    tasks = feature.get('tasks')
    if not isinstance(tasks, list):
        raise ValueError('Feature state contains an invalid tasks collection.')
    seen: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError('Feature state contains a non-object task record.')
        task_id = validate_task_id(task.get('id'))
        if task_id in seen:
            raise ValueError(f'Duplicate task id in feature state: {task_id}')
        seen.add(task_id)
        dependencies = task.get('depends_on', [])
        if not isinstance(dependencies, list) or any(
            not isinstance(item, str) or not TASK_RE.fullmatch(item) for item in dependencies
        ):
            raise ValueError(f'Invalid task dependencies for {task_id}.')
    return feature


def validate_feature_state(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get('features'), list):
        raise ValueError('Feature state schema is invalid.')
    seen: set[str] = set()
    for feature in payload['features']:
        record = validate_feature_record(feature)
        feature_id = record['id']
        if feature_id in seen:
            raise ValueError(f'Duplicate feature id in feature state: {feature_id}')
        seen.add(feature_id)
    return payload


def load_state(root: Path) -> dict[str, Any]:
    path = validate_unredirected_file_path(root / STATE_REL, label='feature state')
    if not path.is_file():
        return {"version": 2, "features": [], "schema": {"id": "stable feature id"}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError('Feature state is unreadable; refusing to replace it.') from exc
    validate_feature_state(payload)
    payload["version"] = max(2, int(payload.get("version", 1)))
    return payload


def aggregate_task_status(tasks: list[dict[str, Any]]) -> str:
    statuses = {task.get('status') for task in tasks}
    if 'blocked' in statuses:
        return 'blocked'
    if statuses == {'completed'}:
        return 'completed'
    if statuses & {'completed', 'awaiting-acceptance'}:
        return 'awaiting-acceptance'
    return 'planned'


def managed_status(body: str) -> str:
    return '<!-- delivery-status:start -->\n' + body + '\n<!-- delivery-status:end -->'


def projection_paths(feature: dict[str, Any]) -> list[str]:
    validate_feature_record(feature)
    return ['00-plan.md', '90-acceptance.md', *[f"{task['id']}-task.md" for task in feature['tasks']]]


def write_status_projection(path: Path, body: str) -> bool:
    validate_unredirected_file_path(path, label='delivery projection')
    if not path.is_file():
        return False
    text = path.read_text(encoding='utf-8')
    pattern = r'<!-- delivery-status:start -->.*?<!-- delivery-status:end -->'
    if text.count('<!-- delivery-status:start -->') != 1 or text.count('<!-- delivery-status:end -->') != 1:
        return False
    updated = re.sub(pattern, lambda _: managed_status(body), text, count=1, flags=re.DOTALL)
    if updated != text:
        atomic_write_text(path, updated)
    return True


def save_state(root: Path, payload: dict[str, Any]) -> None:
    validate_feature_state(payload)
    for feature in payload['features']:
        feature['status'] = aggregate_task_status(feature['tasks'])
    path = validate_unredirected_file_path(root / STATE_REL, label='feature state')
    ensure_unredirected_directory(path.parent, label='feature state directory')
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    # Only marked blocks are managed. Unmarked legacy/user prose is never guessed.
    for feature in payload['features']:
        if not SLUG_RE.fullmatch(str(feature.get('id', ''))):
            continue
        package = root / FEATURE_ROOT / feature['id']
        view = current_feature_view(root, feature)
        rows = ['| Task | Status | Outcome | Claim |', '| --- | --- | --- | --- |',
            *[f"| {task['id']} | {task['current_status']} | {task.get('outcome', 'unlabeled')} | {task.get('claim_proof') or '-'} |" for task in view['tasks']]]
        body = f"Status: {view['status']}\n\n" + '\n'.join(rows)
        write_status_projection(package / '90-acceptance.md', body)
        write_status_projection(package / '00-plan.md',
            f"Plan approval: {feature.get('plan_approval', 'unrecorded')}\nExecution status: {view['status']}\n\n" + '\n'.join(rows))
        for task in view['tasks']:
            write_status_projection(package / f"{task['id']}-task.md",
                f"Current status: {task['current_status']}\nRecorded status: {task['status']}\n"
                f"Contract: {task_contract_status(task)['status']}\nOutcome: {task.get('outcome', 'unlabeled')}")


def find_feature(state: dict[str, Any], slug: str) -> dict[str, Any] | None:
    validate_feature_id(slug)
    return next((item for item in state["features"] if isinstance(item, dict) and item.get("id") == slug), None)


def parse_report_metadata(text: str) -> tuple[list[str], str | None, str | None]:
    files: list[str] = []
    in_files = False
    level: str | None = None
    mode: str | None = None
    for line in text.splitlines():
        heading = re.match(r"^\s*##+\s+(.+?)\s*$", line)
        if heading:
            normalized = heading.group(1).strip().casefold()
            in_files = normalized in {"changed files", "changed-files", "修改文件", "变更文件"}
            continue
        match = re.match(r"^\s*(?:evidence[_ -]?level|证据等级)\s*[:：]\s*(\S+)\s*$", line, re.IGNORECASE)
        if match:
            level = match.group(1).upper()
        match = re.match(r"^\s*(?:evidence[_ -]?mode|证据模式)\s*[:：]\s*(\S+)\s*$", line, re.IGNORECASE)
        if match:
            mode = match.group(1).lower()
        if in_files:
            item = re.match(r"^\s*[-*]\s+`?([^`]+?)`?\s*$", line)
            if item:
                files.append(item.group(1).strip())
    return sorted(set(files)), level, mode


def normalize_report_files(values: list[str]) -> list[str] | None:
    normalized: list[str] = []
    for raw in values:
        path = Path(raw.replace("\\", "/"))
        if path.is_absolute() or re.match(r'^[A-Za-z]:', raw) or ".." in path.parts or not path.parts:
            return None
        normalized.append(path.as_posix())
    return sorted(set(normalized))


def git_changed_paths(root: Path, base_head: str | None = None) -> set[str]:
    paths: set[str] = set()
    commands = [("diff", "--name-only", '-z'), ("diff", "--cached", "--name-only", '-z'), ("ls-files", "--others", "--exclude-standard", '-z')]
    if base_head and re.fullmatch('[0-9a-f]{40}', base_head):
        commands.append(('diff', '--name-only', '-z', base_head, 'HEAD'))
    for args in commands:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False, env=git_repository_env())
        if result.returncode:
            raise RuntimeError('Git change inventory is unavailable')
        paths.update(path.decode('utf-8', errors='surrogateescape') for path in result.stdout.split(b'\0') if path)
    return paths


def in_scope(path: str, scopes: list[str]) -> bool:
    return not scopes or any(path == parent or path.startswith(parent + '/') for parent in scopes)


def task_check(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    """Explicitly run a check; report import itself never executes content."""
    state = load_state(root)
    feature = find_feature(state, args.feature)
    task = next((item for item in feature['tasks'] if item.get('id') == args.task), None) if feature else None
    if task is None:
        return {'status': 'fail', 'code': 'task-missing'}
    completeness = task_contract_status(task)
    if completeness['status'] != 'pass':
        return completeness
    if verification_command_hash(task['contract']['verification_command']) != verification_command_hash(args.verification_command):
        return {'status': 'fail', 'code': 'task-command-not-frozen'}
    artifacts = task_artifact_status(root, task)
    if artifacts['status'] != 'pass':
        return artifacts
    if args.timeout <= 0:
        return {'status': 'fail', 'code': 'task-check-timeout-invalid'}
    paths = normalize_report_files(args.file)
    if not paths or any(not in_scope(path, task.get('allowed_paths', [])) for path in paths):
        return {'status': 'fail', 'code': 'task-check-scope-invalid'}
    if task.get('verification_command') and verification_command_hash(task['verification_command']) != verification_command_hash(args.verification_command):
        return {'status': 'fail', 'code': 'task-command-not-frozen'}
    subject_paths = sorted(set(paths + artifacts['paths']))
    frozen = git_state_fingerprint(root, subject_paths)
    if not frozen:
        return {'status': 'fail', 'code': 'task-check-subject-unavailable'}
    plan_observation = observation(root, build_plan(root, scopes=subject_paths))
    run = root / '.tenetora/.cache/feature-checks' / uuid.uuid4().hex
    ensure_unredirected_directory(run, label='feature check cache')
    started = time.monotonic()
    try:
        argv = shlex.split(args.verification_command)
        result = subprocess.run(argv, cwd=root, capture_output=True, check=False, timeout=args.timeout, env=git_repository_env())
        output, error, code = result.stdout, result.stderr, result.returncode
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        output, error, code = b'', str(exc).encode(), -1
    duration = round((time.monotonic() - started) * 1000)
    for name, content in (('stdout', output), ('stderr', error)):
        (run / f'{name}.txt').write_bytes(content)
    receipt = {'version': 2, 'contract_sha256': contract_digest(task), 'feature': args.feature, 'task': args.task, 'command_hash': verification_command_hash(args.verification_command), 'paths': paths, 'subject_paths': subject_paths, 'subject_fingerprint': frozen, 'exit_code': code, 'duration_ms': duration, 'level': args.evidence_level, 'mode': args.evidence_mode, 'logs': {name: hashlib.sha256((run / f'{name}.txt').read_bytes()).hexdigest() for name in ('stdout', 'stderr')}}
    atomic_write_text(run / 'receipt.json', json.dumps(receipt, indent=2) + '\n')
    if frozen != git_state_fingerprint(root, subject_paths):
        code = -2
    task['verification_command'] = args.verification_command
    task['execution_receipt'] = (run / 'receipt.json').relative_to(root).as_posix()
    task['execution_receipt_sha256'] = hashlib.sha256((run / 'receipt.json').read_bytes()).hexdigest()
    task['execution_status'] = 'passed' if code == 0 else 'failed'
    if task.get('status') == 'completed':
        task['status'] = 'awaiting-acceptance'
        task['outcome'] = 'reopened'
    save_state(root, state)
    event = {'type': 'feature-delivery', 'action': 'check', 'feature_id': args.feature, 'task_id': args.task,
        'status': 'pass' if code == 0 else 'fail', 'run_id': run.name, 'duration_ms': duration,
        'exit_code': code, 'verification_plan': plan_observation,
        'command_hash': receipt['command_hash'], 'subject_fingerprint': frozen}
    if task.get('outcome') == 'reopened':
        event['outcome'] = 'reopened'
    append_event(root, event)
    return {'status': 'pass' if code == 0 else 'fail', 'code': 'task-check-passed' if code == 0 else 'task-check-failed', 'exit_code': code, 'receipt': task['execution_receipt'], 'duration_ms': duration}


def feature_start(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    slug = args.name.strip()
    if not SLUG_RE.fullmatch(slug):
        return {"status": "fail", "code": "feature-name-invalid", "message": "Feature name must be a lowercase slug."}
    state = load_state(root)
    if find_feature(state, slug) is not None:
        return {"status": "fail", "code": "feature-exists", "message": f"Feature already exists: {slug}"}
    package = root / FEATURE_ROOT / slug
    package.mkdir(parents=True, exist_ok=False)
    title = args.title.strip() or slug.replace("-", " ").title()
    files = {
        "00-plan.md": f"# Feature Plan: {title}\n\n## Goal\n\n## Scope\n\n## Non-goals\n\n## Decisions\n\n## Dependencies\n\n## Skeleton\n\n## Status\n\n" + managed_status("Plan approval: unrecorded\nExecution status: planned") + "\n",
        "T01-task.md": f"# T01: {title}\n\n## Status\n\n" + managed_status("Current status: planned") + "\n\n## Specific Deliverable\n\n## Frozen Contract\n\n## Verification\n\n## Acceptance\n\n",
        "90-acceptance.md": f"# Acceptance: {title}\n\n## Status\n\n" + managed_status("Status: planned") + "\n\n## Evidence\n\n## Open Risks\n\n",
    }
    for relative, text in files.items():
        atomic_write_text(package / relative, text)
    record = {
        "id": slug,
        "title": title,
        "package": (FEATURE_ROOT / slug).as_posix(),
        "status": "planned",
        "plan_approval": "unrecorded",
        "tasks": [
            {
                "id": "T01",
                "title": f"{title} implementation",
                "scope": "cross",
                "depends_on": [],
                "evidence_level": "L1",
                "evidence_mode": "static",
                "allowed_paths": list(normalize_scopes(args.allow_path)),
                "base_head": git_head(root),
                "status": "planned",
                "report": None,
                "claim_proof": None,
            }
        ],
        "created_at": now(),
        "updated_at": now(),
    }
    state["features"].append(record)
    save_state(root, state)
    append_event(root, {"type": "feature-delivery", "action": "start", "feature_id": slug, "status": "pass"})
    return {"status": "pass", "feature": record, "files": sorted(files)}


def task_report(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    if not TASK_RE.fullmatch(args.task):
        return {"status": "fail", "code": "task-id-invalid", "message": "Task id must look like T01."}
    report = Path(args.file).expanduser().resolve()
    if not report.is_file():
        return {"status": "fail", "code": "report-missing", "message": "Implementation report does not exist."}
    try:
        relative = report.relative_to(root).as_posix()
    except ValueError:
        return {"status": "fail", "code": "report-outside-project", "message": "Report must be inside the project."}
    report_bytes = report.read_bytes()
    try:
        report_text = report_bytes.decode('utf-8')
    except UnicodeDecodeError:
        return {'status': 'fail', 'code': 'report-encoding-invalid'}
    findings = scan_input(PromptInput(label=relative, text=report_text, path=relative))
    if findings:
        return {
            "status": "fail",
            "code": "report-untrusted-input",
            "message": "Implementation report contains untrusted instruction patterns.",
            "finding_types": sorted({str(item.get("type")) for item in findings}),
        }
    report_files, report_level, report_mode = parse_report_metadata(report_text)
    normalized_files = normalize_report_files(report_files)
    if normalized_files is None:
        return {"status": "fail", "code": "report-files-invalid", "message": "Changed Files must contain project-relative paths."}
    if not normalized_files:
        return {"status": "fail", "code": "report-files-required", "message": "Implementation report must list files under a Changed Files section."}
    if report_level is not None and report_level not in EVIDENCE_LEVELS:
        return {"status": "fail", "code": "report-evidence-level-invalid", "message": "Evidence level must be L1, L2, L3, or L4."}
    if report_mode is not None and report_mode not in EVIDENCE_MODES:
        return {"status": "fail", "code": "report-evidence-mode-invalid", "message": "Evidence mode must be static, mock, real, or mixed."}
    state = load_state(root)
    feature = find_feature(state, args.feature)
    if feature is None:
        return {"status": "fail", "code": "feature-missing", "message": f"Feature does not exist: {args.feature}"}
    task = next((item for item in feature["tasks"] if item.get("id") == args.task), None)
    if task is None:
        return {"status": "fail", "code": "task-missing", "message": f"Task does not exist: {args.task}"}
    digest = hashlib.sha256(report_bytes).hexdigest()
    task.update({"status": "awaiting-acceptance", "report": relative, "report_sha256": digest, "report_files": normalized_files})
    if report_level:
        task["observed_evidence_level"] = report_level
    if report_mode:
        task["observed_evidence_mode"] = report_mode
    feature["updated_at"] = now()
    save_state(root, state)
    append_event(root, {"type": "feature-delivery", "action": "report", "feature_id": args.feature, "task_id": args.task, "status": "pass", "report": relative, "report_sha256": digest})
    return {"status": "pass", "feature": args.feature, "task": task}


def task_contract_status(task: dict[str, Any]) -> dict[str, Any]:
    contract = task.get('contract')
    fields = ('deliverable', 'verification_command', 'timebox', 'rationale')
    if not isinstance(contract, dict):
        contract = {}
    missing = [field for field in fields if not isinstance(contract.get(field), str) or not contract[field].strip()]
    acceptance = contract.get('acceptance')
    if not isinstance(acceptance, list) or not acceptance or any(not isinstance(item, str) or not item.strip() for item in acceptance):
        missing.append('acceptance')
    if task.get('scope') not in {'backend', 'frontend', 'cross'}:
        missing.append('scope')
    if missing:
        return {'status': 'fail', 'code': 'task-contract-incomplete', 'missing_fields': missing,
            'message': 'Freeze explicit deliverable, observable criteria, scope, check, timebox and rationale using feature task-contract.'}
    return {'status': 'pass', 'code': 'task-contract-complete',
        'assurance': 'mechanical-completeness-only'}


def contract_digest(task: dict[str, Any]) -> str:
    frozen = {key: task.get(key) for key in ('title', 'scope', 'depends_on', 'allowed_paths', 'evidence_level', 'evidence_mode', 'contract')}
    return hashlib.sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def task_artifact_status(root: Path, task: dict[str, Any]) -> dict[str, Any]:
    """Opaque project-owned files, not inferred interfaces or language parsers."""
    artifacts = task.get('contract', {}).get('artifacts', [])
    if not isinstance(artifacts, list):
        return {'status': 'fail', 'code': 'task-contract-artifact-invalid'}
    paths = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not isinstance(artifact.get('path'), str):
            return {'status': 'fail', 'code': 'task-contract-artifact-invalid'}
        relative = artifact['path']
        if normalize_report_files([relative]) is None:
            return {'status': 'fail', 'code': 'task-contract-artifact-invalid'}
        try:
            path = validate_unredirected_file_path(root / relative, label='contract artifact')
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != artifact.get('sha256'):
                return {'status': 'fail', 'code': 'task-contract-artifact-stale', 'path': relative,
                    'message': 'Explicitly refreeze the changed contract and rerun the project-owned check.'}
        except (OSError, RuntimeError, ValueError):
            return {'status': 'fail', 'code': 'task-contract-artifact-invalid', 'path': relative}
        paths.append(relative)
    return {'status': 'pass', 'paths': sorted(set(paths))}


def task_contract(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    state = load_state(root)
    feature = find_feature(state, args.feature)
    task = next((item for item in feature['tasks'] if item.get('id') == args.task), None) if feature else None
    if task is None:
        return {'status': 'fail', 'code': 'task-missing'}
    if (task.get('execution_receipt') or task.get('status') == 'completed') and not args.reset_evidence:
        return {'status': 'fail', 'code': 'task-contract-locked',
            'message': 'Use --reset-evidence explicitly to reopen and invalidate the prior execution evidence.'}
    contract = {key: getattr(args, key).strip() for key in ('deliverable', 'timebox', 'rationale')}
    contract['verification_command'] = args.verification_command
    contract['acceptance'] = [item.strip() for item in args.acceptance]
    artifact_paths = normalize_report_files(args.contract_file)
    if artifact_paths is None:
        return {'status': 'fail', 'code': 'task-contract-artifact-invalid'}
    if artifact_paths:
        artifacts = []
        for relative in artifact_paths:
            try:
                path = validate_unredirected_file_path(root / relative, label='contract artifact')
                if not path.is_file():
                    raise ValueError('contract artifact must be an existing regular file')
                artifacts.append({'path': relative, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
            except (OSError, RuntimeError, ValueError):
                return {'status': 'fail', 'code': 'task-contract-artifact-invalid', 'path': relative}
        contract['artifacts'] = artifacts
    candidate = {**task, 'contract': contract}
    completeness = task_contract_status(candidate)
    if completeness['status'] != 'pass':
        return completeness
    if args.reset_evidence:
        for key in ('execution_receipt', 'execution_receipt_sha256', 'execution_status', 'report', 'report_sha256', 'report_files', 'claim_proof', 'verification_command', 'duration_ms'):
            task.pop(key, None)
        task.update(status='planned', outcome='reopened')
    task['contract'] = contract
    feature['updated_at'] = now()
    event = append_event(root, {'type': 'feature-delivery', 'action': 'task-contract', 'feature_id': args.feature,
        'task_id': args.task, 'status': 'pass', 'contract_sha256': contract_digest(task), 'evidence_reset': args.reset_evidence,
        **({'outcome': 'reopened'} if args.reset_evidence else {})})
    if args.reset_evidence:
        task['evidence_reset_sequence'] = event['_journal_sequence']
    save_state(root, state)
    return {'status': 'pass', 'task': task, 'contract_check': completeness}


def validate_task_evidence(root: Path, feature: dict[str, Any], task: dict[str, Any],
                           proof: str, command: str, *, verify_inventory: bool = True) -> dict[str, Any]:
    """Read the original task evidence without accepting or rewriting history."""
    completeness = task_contract_status(task)
    if completeness['status'] != 'pass':
        return completeness
    artifacts = task_artifact_status(root, task)
    if artifacts['status'] != 'pass':
        return artifacts
    report_files = [str(item) for item in task.get("report_files", []) if isinstance(item, str)]
    if not task.get('report') or normalize_report_files([task['report']]) is None:
        return {'status': 'fail', 'code': 'task-report-required'}
    report_path = validate_unredirected_file_path(root / task['report'], label='task report')
    if not report_path.is_file() or hashlib.sha256(report_path.read_bytes()).hexdigest() != task.get('report_sha256'):
        return {'status': 'fail', 'code': 'report-changed', 'message': 'Import the changed report again before acceptance.'}
    if not report_files or any(not in_scope(path, task.get('allowed_paths', [])) for path in report_files):
        return {'status': 'fail', 'code': 'task-scope-invalid'}
    if report_files and verify_inventory:
        try:
            changed = git_changed_paths(root, task.get('base_head'))
        except RuntimeError as exc:
            return {'status': 'fail', 'code': 'task-scope-unavailable', 'message': str(exc)}
        def delivery_artifact(path: str) -> bool:
            return path == task['report'] or path.startswith(('.tenetora/state/', '.tenetora/.cache/', feature['package'] + '/'))
        scoped = {path for path in changed if in_scope(path, task.get('allowed_paths', [])) and not delivery_artifact(path)}
        missing = sorted(set(report_files) - scoped)
        if missing:
            return {
                "status": "fail",
                "code": "task-scope-mismatch",
                "message": "The report lists files that are not present in the current worktree change set.",
                "missing_files": missing,
            }
        unreported = sorted(scoped - set(report_files))
        if unreported:
            return {'status': 'fail', 'code': 'task-unreported-files', 'files': unreported}
    subject_paths = sorted(set(report_files + artifacts['paths']))
    claim = validate_claim(root, proof, command, subject_paths)
    if claim['status'] != 'pass':
        return claim
    reset = task.get('evidence_reset_sequence', 0)
    if type(reset) is not int or reset < 0 or (reset and claim['event'].get('_journal_sequence', 0) <= reset):
        return {'status': 'fail', 'code': 'claim-predates-contract-reset',
            'message': 'Record a fresh claim after explicitly resetting task evidence.'}
    try:
        receipt_path = root / task['execution_receipt']
        validate_unredirected_file_path(receipt_path, label='execution receipt')
        if not receipt_path.resolve().is_relative_to(root / '.tenetora/.cache/feature-checks'):
            raise ValueError('receipt outside execution cache')
        raw = receipt_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != task['execution_receipt_sha256']:
            raise ValueError('receipt changed')
        receipt = json.loads(raw)
        if not isinstance(receipt, dict) or receipt.get('version') != 2 or type(receipt.get('duration_ms')) is not int or receipt['duration_ms'] < 0 or receipt.get('level') not in EVIDENCE_LEVELS or receipt.get('mode') not in EVIDENCE_MODES or not isinstance(receipt.get('logs'), dict):
            raise ValueError('execution receipt schema invalid')
        if receipt.get('contract_sha256') != contract_digest(task):
            raise ValueError('frozen task contract changed; rerun the declared check')
        if task.get('execution_status') != 'passed' or type(receipt['exit_code']) is not int or receipt['exit_code'] != 0:
            raise ValueError('execution failed')
        if receipt['command_hash'] != verification_command_hash(command) or receipt['feature'] != feature['id'] or receipt['task'] != task['id'] or receipt['paths'] != report_files:
            raise ValueError('execution subject/command differs')
        if receipt.get('subject_paths', report_files) != subject_paths or receipt['subject_fingerprint'] != git_state_fingerprint(root, subject_paths):
            raise ValueError('verified implementation changed')
        for name, digest in receipt['logs'].items():
            validate_unredirected_file_path(receipt_path.parent / f'{name}.txt', label='execution log')
            if name not in {'stdout', 'stderr'} or hashlib.sha256((receipt_path.parent / f'{name}.txt').read_bytes()).hexdigest() != digest:
                raise ValueError('execution log changed')
        if set(receipt['logs']) != {'stdout', 'stderr'}:
            raise ValueError('execution logs missing')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'status': 'fail', 'code': 'task-execution-invalid', 'message': str(exc)}
    required_level = task.get('evidence_level', 'L1')
    required_mode = task.get('evidence_mode', 'static')
    if task.get('observed_evidence_level') != receipt['level'] or task.get('observed_evidence_mode') != receipt['mode']:
        return {'status': 'fail', 'code': 'task-evidence-inconsistent'}
    if receipt['level'] not in EVIDENCE_LEVELS or receipt['level'] < required_level or receipt['mode'] not in EVIDENCE_MODES or (required_mode != 'mixed' and receipt['mode'] != required_mode):
        return {'status': 'fail', 'code': 'task-evidence-insufficient'}
    return {'status': 'pass', 'receipt': receipt}


def dependency_validity(root: Path, feature: dict[str, Any], task: dict[str, Any],
                        visiting: tuple[str, ...] = (),
                        cache: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Revalidate every ancestor; a recorded acceptance alone is insufficient."""
    task_id = str(task.get('id'))
    if len(visiting) >= 128:
        return {'status': 'fail', 'code': 'task-dependency-depth-exceeded'}
    if task_id in visiting:
        return {'status': 'fail', 'code': 'task-dependency-cycle'}
    if cache is None:
        cache = {}
    tasks = {item['id']: item for item in feature['tasks']}
    dependencies = task.get('depends_on', [])
    if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
        return {'status': 'fail', 'code': 'task-dependency-invalid'}
    for dependency in dependencies:
        upstream = tasks.get(dependency)
        if upstream is None or upstream.get('status') != 'completed':
            result = {'status': 'fail', 'code': 'task-dependency-incomplete'}
        elif dependency in cache:
            result = cache[dependency]
        else:
            result = dependency_validity(root, feature, upstream, (*visiting, task_id), cache)
            if result['status'] == 'pass':
                result = validate_task_evidence(root, feature, upstream,
                    upstream.get('claim_proof', ''), upstream.get('verification_command', ''), verify_inventory=False)
            cache[dependency] = result
        if result['status'] != 'pass':
            return {'status': 'fail', 'code': 'task-dependency-invalid',
                'dependency': dependency, 'reason': result}
    return {'status': 'pass'}


def current_feature_view(root: Path, feature: dict[str, Any]) -> dict[str, Any]:
    """Separate historical acceptance from read-only current validity."""
    validate_feature_record(feature)
    view = json.loads(json.dumps(feature))
    view['plan_approval'] = feature.get('plan_approval', 'unrecorded')
    package = root / FEATURE_ROOT / feature['id']
    unmanaged: list[str] = []
    for name in projection_paths(feature):
        path = package / name
        try:
            validate_unredirected_file_path(path, label='delivery projection')
            managed = path.is_file() and '<!-- delivery-status:start -->' in path.read_text(encoding='utf-8')
        except (OSError, RuntimeError, ValueError):
            managed = False
        if not managed:
            unmanaged.append(name)
    view['unmanaged_projections'] = unmanaged
    cache: dict[str, dict[str, Any]] = {}
    for task in view['tasks']:
        task['current_status'] = task['status']
        task['contract_check'] = task_contract_status(task)
        if task.get('status') != 'completed':
            continue
        validity = dependency_validity(root, feature, task, cache=cache)
        if validity['status'] == 'pass':
            validity = validate_task_evidence(root, feature, task,
                task.get('claim_proof', ''), task.get('verification_command', ''), verify_inventory=False)
        if validity['status'] != 'pass':
            task['current_status'] = 'awaiting-acceptance'
        task['current_validity'] = {key: value for key, value in validity.items() if key != 'receipt'}
        cache[task['id']] = validity
    view['recorded_status'] = feature['status']
    if view['status'] == 'completed' and any(task.get('current_validity', {}).get('status') == 'fail' for task in view['tasks']):
        view['status'] = 'awaiting-acceptance'
    return view


def task_verify(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    if not is_claim_proof(args.claim_proof):
        return {"status": "fail", "code": "claim-proof-invalid", "message": "A valid ah-claim proof is required."}
    if args.duration_ms is not None and args.duration_ms < 0:
        return {"status": "fail", "code": "duration-invalid", "message": "Duration must be a non-negative integer in milliseconds."}
    state = load_state(root)
    feature = find_feature(state, args.feature)
    if feature is None:
        return {"status": "fail", "code": "feature-missing", "message": f"Feature does not exist: {args.feature}"}
    task = next((item for item in feature["tasks"] if item.get("id") == args.task), None)
    if task is None:
        return {"status": "fail", "code": "task-missing", "message": f"Task does not exist: {args.task}"}
    if task.get("status") != "awaiting-acceptance" or not task.get("report"):
        return {"status": "fail", "code": "task-report-required", "message": "A task report must be recorded before verification."}
    dependencies = task.get('depends_on', [])
    incomplete = [item for item in dependencies if not any(upstream.get('id') == item and upstream.get('status') == 'completed' for upstream in feature['tasks'])]
    if incomplete:
        return {'status': 'fail', 'code': 'task-dependency-incomplete', 'dependencies': incomplete}
    validity = dependency_validity(root, feature, task)
    if validity['status'] != 'pass':
        return validity
    evidence = validate_task_evidence(root, feature, task, args.claim_proof, args.verification_command)
    if evidence['status'] != 'pass':
        return evidence
    receipt = evidence['receipt']
    outcome_status = {'accepted': 'completed', 'reopened': 'awaiting-acceptance', 'rejected': 'blocked', 'unknown': 'awaiting-acceptance', 'unlabeled': 'awaiting-acceptance'}
    task.update({"status": outcome_status[args.outcome], "claim_proof": args.claim_proof, "verification_command": args.verification_command})
    task["outcome"] = args.outcome
    task['duration_ms'] = receipt['duration_ms']
    feature["updated_at"] = now()
    save_state(root, state)
    event = {"type": "feature-delivery", "action": "verify", "feature_id": args.feature, "task_id": args.task, "status": "pass", "claim_proof": args.claim_proof, "outcome": args.outcome}
    event['duration_ms'] = receipt['duration_ms']
    append_event(root, event)
    return {"status": "pass", "feature": args.feature, "task": task}


def feature_status(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    state = load_state(root)
    feature = find_feature(state, args.feature)
    if feature is None:
        return {"status": "fail", "code": "feature-missing", "message": f"Feature does not exist: {args.feature}"}
    return {"status": "pass", "feature": current_feature_view(root, feature)}


def task_outcome(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    state = load_state(root)
    feature = find_feature(state, args.feature)
    task = next((item for item in feature['tasks'] if item.get('id') == args.task), None) if feature else None
    if task is None:
        return {'status': 'fail', 'code': 'task-missing'}
    task['outcome'] = args.outcome
    task['status'] = 'blocked' if args.outcome == 'rejected' else 'awaiting-acceptance'
    feature['updated_at'] = now()
    save_state(root, state)
    append_event(root, {'type': 'feature-delivery', 'action': 'outcome', 'feature_id': args.feature, 'task_id': args.task, 'outcome': args.outcome, 'status': 'fail'})
    return {'status': 'pass', 'task': task}


def task_add(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    if not TASK_RE.fullmatch(args.task):
        return {"status": "fail", "code": "task-id-invalid", "message": "Task id must look like T01."}
    if args.scope not in {"backend", "frontend", "cross"}:
        return {"status": "fail", "code": "task-scope-invalid", "message": "Task scope must be backend, frontend, or cross."}
    state = load_state(root)
    feature = find_feature(state, args.feature)
    if feature is None:
        return {"status": "fail", "code": "feature-missing", "message": f"Feature does not exist: {args.feature}"}
    if any(item.get("id") == args.task for item in feature["tasks"]):
        return {"status": "fail", "code": "task-exists", "message": f"Task already exists: {args.task}"}
    dependencies = [item.strip() for item in args.depends_on.split(",") if item.strip()]
    known = {item.get("id") for item in feature["tasks"]}
    if args.task in dependencies:
        return {"status": "fail", "code": "task-dependency-cycle", "message": "A task cannot depend on itself."}
    if any(item not in known for item in dependencies):
        return {"status": "fail", "code": "task-dependency-missing", "message": "Every dependency must name an existing task."}
    task_path = root / FEATURE_ROOT / str(feature["id"]) / f"{args.task}-task.md"
    if task_path.exists():
        return {"status": "fail", "code": "task-file-exists", "message": f"Task file already exists: {task_path.name}"}
    task = {
        "id": args.task,
        "title": args.title.strip() or args.task,
        "scope": args.scope,
        "depends_on": dependencies,
        "evidence_level": args.evidence_level,
        "evidence_mode": args.evidence_mode,
        "allowed_paths": list(normalize_scopes(args.allow_path)),
        "base_head": git_head(root),
        "status": "planned",
        "report": None,
        "claim_proof": None,
    }
    atomic_write_text(
        task_path,
        "\n".join(
            [
                f"# {args.task}: {task['title']}",
                "",
                "## Status",
                "",
                managed_status("Current status: planned"),
                "",
                f"## Scope\n\n{args.scope}",
                "",
                f"## Depends On\n\n{', '.join(dependencies) or 'None'}",
                "",
                "## Specific Deliverable",
                "",
                "## Frozen Contract",
                "",
                "## Verification",
                "",
                "## Acceptance",
                "",
            ]
        ),
        encoding="utf-8",
    )
    feature["tasks"].append(task)
    feature["updated_at"] = now()
    save_state(root, state)
    append_event(root, {"type": "feature-delivery", "action": "task-add", "feature_id": args.feature, "task_id": args.task, "status": "pass", "scope": args.scope})
    return {"status": "pass", "feature": args.feature, "task": task, "file": task_path.relative_to(root).as_posix()}


def task_prompt(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    if args.role not in {"backend", "frontend"}:
        return {"status": "fail", "code": "prompt-role-invalid", "message": "Prompt role must be backend or frontend."}
    state = load_state(root)
    feature = find_feature(state, args.feature)
    if feature is None:
        return {"status": "fail", "code": "feature-missing", "message": f"Feature does not exist: {args.feature}"}
    task = next((item for item in feature["tasks"] if item.get("id") == args.task), None)
    if task is None:
        return {"status": "fail", "code": "task-missing", "message": f"Task does not exist: {args.task}"}
    if task.get('scope', 'cross') not in {'cross', args.role}:
        return {'status': 'fail', 'code': 'prompt-role-outside-task'}
    completeness = task_contract_status(task)
    if completeness['status'] != 'pass':
        return completeness
    lines = [
        f"任务：{task.get('title', task['id'])}",
        f"功能包：{feature.get('title', feature['id'])}",
        f"冻结范围：{task.get('scope', 'cross')}",
        f"交付物：{task['contract']['deliverable']}",
        f"可观察判据：{'; '.join(task['contract']['acceptance'])}",
        f"验证命令：{task['contract']['verification_command']}",
        f"时间边界：{task['contract']['timebox']}",
        f"可行性与相关性依据：{task['contract']['rationale']}",
        f"契约产物：{', '.join(item['path'] for item in task['contract'].get('artifacts', [])) or '无显式文件'}",
        f"前置任务：{', '.join(task.get('depends_on', [])) or '无'}",
        f"证据等级：{task.get('evidence_level', 'L1')}；证据模式：{task.get('evidence_mode', 'static')}",
        "只执行冻结范围，复用现有模式，不重新规划产品决策，不扩大任务，不提交或推送。",
    ]
    if args.role == "backend":
        lines.extend(
            [
                "后端边界：数据/API 契约、权限与隔离、事务与错误语义。",
                "后端验收：真实生产接线、错误路径和声明的项目验证命令。",
            ]
        )
    else:
        lines.extend(
            [
                "前端边界：页面/状态/错误行为、异步竞态和序列化。",
                "前端验收：组件行为、契约检查、异步失败路径和声明的项目验证命令。",
            ]
        )
    return {"status": "pass", "feature": args.feature, "task": args.task, "role": args.role, "prompt": "\n".join(lines)}


def main(argv: list[str] | None = None) -> int:
    root_parser = argparse.ArgumentParser(description="Manage evidence-bound feature delivery packages.")
    root_parser.add_argument("--path", default=".", type=project_path)
    root_parser.add_argument("--json", action="store_true")
    sub = root_parser.add_subparsers(dest="action", required=True)
    start = sub.add_parser("start")
    start.add_argument("--name", required=True)
    start.add_argument("--title", default="")
    start.add_argument('--allow-path', action='append', default=[])
    report = sub.add_parser("report")
    report.add_argument("--feature", required=True)
    report.add_argument("--task", required=True)
    report.add_argument("--file", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--feature", required=True)
    verify.add_argument("--task", required=True)
    verify.add_argument("--claim-proof", required=True)
    verify.add_argument("--verification-command", required=True)
    verify.add_argument("--outcome", choices=("accepted", "reopened", "rejected", "unknown", "unlabeled"), default="unlabeled")
    verify.add_argument("--duration-ms", type=int, default=None)
    status = sub.add_parser("status")
    status.add_argument("--feature", required=True)
    task_add_parser = sub.add_parser("task-add")
    task_add_parser.add_argument("--feature", required=True)
    task_add_parser.add_argument("--task", required=True)
    task_add_parser.add_argument("--title", default="")
    task_add_parser.add_argument("--scope", choices=("backend", "frontend", "cross"), required=True)
    task_add_parser.add_argument("--depends-on", default="")
    task_add_parser.add_argument("--evidence-level", choices=sorted(EVIDENCE_LEVELS), default="L1")
    task_add_parser.add_argument("--evidence-mode", choices=sorted(EVIDENCE_MODES), default="static")
    task_add_parser.add_argument('--allow-path', action='append', default=[])
    contract = sub.add_parser('task-contract')
    contract.add_argument('--feature', required=True)
    contract.add_argument('--task', required=True)
    contract.add_argument('--deliverable', required=True)
    contract.add_argument('--acceptance', action='append', required=True)
    contract.add_argument('--verification-command', required=True)
    contract.add_argument('--timebox', required=True)
    contract.add_argument('--rationale', required=True)
    contract.add_argument('--reset-evidence', action='store_true')
    contract.add_argument('--contract-file', action='append', default=[], help='Existing project-relative schema/contract artifact; repeatable and included in check/claim subjects.')
    prompt = sub.add_parser("prompt")
    prompt.add_argument("--feature", required=True)
    prompt.add_argument("--task", required=True)
    prompt.add_argument("--role", choices=("backend", "frontend"), required=True)
    check = sub.add_parser('check')
    check.add_argument('--feature', required=True)
    check.add_argument('--task', required=True)
    check.add_argument('--verification-command', required=True)
    check.add_argument('--file', action='append', required=True)
    check.add_argument('--evidence-level', choices=sorted(EVIDENCE_LEVELS), required=True)
    check.add_argument('--evidence-mode', choices=sorted(EVIDENCE_MODES), required=True)
    check.add_argument('--timeout', type=int, default=120)
    outcome = sub.add_parser('outcome')
    outcome.add_argument('--feature', required=True)
    outcome.add_argument('--task', required=True)
    outcome.add_argument('--outcome', choices=('reopened', 'rejected', 'unknown'), required=True)
    args = root_parser.parse_args(argv)
    root = args.path.resolve()
    actions = {
        "start": feature_start,
        "report": task_report,
        "verify": task_verify,
        "status": feature_status,
        "task-add": task_add,
        "task-contract": task_contract,
        "prompt": task_prompt,
        "check": task_check,
        "outcome": task_outcome,
    }
    try:
        if args.action in {'start', 'task-add'}:
            ensure_unredirected_directory(root / FEATURE_ROOT, label='feature packages')
        payload = actions[args.action](root, args)
    except (OSError, ValueError, RuntimeError) as exc:
        payload = {'status': 'fail', 'code': 'feature-operation-invalid', 'message': str(exc)}
    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
