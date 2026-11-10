"""Explicit criterion coverage over real check receipts and review reports."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from claim_evidence import verification_command_hash
from path_security import ensure_unredirected_directory, validate_unredirected_file_path
from prompt_guard import PromptInput, scan_input
from worktree_fingerprint import git_state_fingerprint


def contract_hash(task: dict[str, Any]) -> str:
    frozen = {key: task.get(key) for key in ('title', 'scope', 'depends_on', 'allowed_paths', 'evidence_level', 'evidence_mode', 'contract')}
    return hashlib.sha256(json.dumps(frozen, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def freeze_criteria(acceptance: list[str], command: str, checks: list[str], reviews: list[str]) -> list[dict[str, Any]]:
    criteria = [{'id': f'A{index:02}', 'description': description, 'kind': 'unbound', 'required': True}
                for index, description in enumerate(acceptance, 1)]
    known = {item['id']: item for item in criteria}
    assigned: set[str] = set()
    for value, kind in [(item, 'check') for item in checks] + [(item, 'review') for item in reviews]:
        identifier, separator, specific = value.partition('=')
        if identifier not in known or identifier in assigned or (kind == 'review' and separator):
            raise ValueError('Criterion mapping contains an unknown, duplicate or invalid criterion id.')
        assigned.add(identifier)
        known[identifier]['kind'] = kind
        if kind == 'check':
            selected = specific if separator else command
            if not selected.strip():
                raise ValueError('Criterion check command must not be empty.')
            known[identifier]['command'] = selected
    if len(criteria) == 1 and not assigned:
        criteria[0].update(kind='check', command=command)
    return criteria


def valid_criteria(contract: dict[str, Any]) -> bool:
    if 'criteria' not in contract and 'criteria_version' not in contract:
        return True
    criteria = contract.get('criteria')
    acceptance = contract.get('acceptance')
    if type(contract.get('criteria_version')) is not int or contract['criteria_version'] != 1 or not isinstance(criteria, list) or not isinstance(acceptance, list) or len(criteria) != len(acceptance):
        return False
    for index, item in enumerate(criteria, 1):
        if not isinstance(item, dict) or item.get('id') != f'A{index:02}' or item.get('description') != acceptance[index - 1] or item.get('required') is not True:
            return False
        if item.get('kind') not in {'unbound', 'check', 'review'}:
            return False
        expected_keys = {'id', 'description', 'kind', 'required'} | ({'command'} if item['kind'] == 'check' else set())
        if set(item) != expected_keys:
            return False
        if item['kind'] == 'check' and (not isinstance(item.get('command'), str) or not item['command'].strip()):
            return False
    return True


def execute_criteria(root: Path, task: dict[str, Any], run: Path, main_result: dict[str, Any], timeout: int, *, subject_paths: list[str] | None = None,subject_entries:dict|None=None) -> list[dict[str, Any]]:
    """Execute only commands explicitly frozen by the planning caller."""
    results = []
    for criterion in task['contract'].get('criteria', []):
        item = {'id': criterion['id'], 'kind': criterion['kind'], 'status': 'unverified'}
        if criterion['kind'] == 'check':
            command = criterion['command']
            item['command_hash'] = verification_command_hash(command)
            if command == task['contract']['verification_command']:
                item.update(main_result)
            elif main_result['exit_code'] == 0:
                directory = run / 'criteria' / criterion['id']
                ensure_unredirected_directory(directory, label='criterion check logs')
                from execution_receipts import execute_verification,mirror_logs
                observed = execute_verification(root, command, subject_paths if subject_paths is not None else task.get('allowed_paths',[]), timeout,known_paths=subject_entries or ())
                process_result=observed['receipt']
                code=process_result['exit_code'] if process_result['status']=='passed' or process_result['exit_code']!=0 else -2
                records=mirror_logs(root,process_result,directory)
                logs={name:{'path':record['path'].relative_to(run).as_posix(),'sha256':record['sha256']} for name,record in records.items()}
                item['observed_execution']={key:value for key,value in observed.items() if key!='receipt'}
                item.update(exit_code=code, logs=logs)
            else:
                item['reason'] = 'main-check-failed; criterion-check-not-executed'
            if 'exit_code' in item:
                item['status'] = 'passed' if item['exit_code'] == 0 else 'failed'
        results.append(item)
    return results


def receipt_subject_fingerprint(root:Path,receipt:dict,subject_paths:list[str])->str|None:
    entries=receipt.get('source_entries')
    if entries is not None and not isinstance(entries,dict):raise ValueError('Execution source inventory is invalid.')
    return git_state_fingerprint(root,subject_paths,known_paths=entries if entries is not None else receipt.get('paths',subject_paths))


def read_receipt(root: Path, task: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    path = validate_unredirected_file_path(root / task['execution_receipt'], label='criterion execution receipt')
    if not path.resolve().is_relative_to(root / '.tenetora/.cache/feature-checks'):
        raise ValueError('Criterion receipt must stay within the execution cache.')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != task['execution_receipt_sha256']:
        raise ValueError('Execution receipt changed.')
    receipt = json.loads(raw)
    if receipt.get('contract_sha256') != contract_hash(task) or receipt.get('subject_fingerprint') != receipt_subject_fingerprint(root,receipt,receipt['subject_paths']):
        raise ValueError('Execution subject or frozen contract changed.')
    return path, receipt


def criterion_coverage(root: Path, task: dict[str, Any]) -> dict[str, Any]:
    contract = task.get('contract') or {}
    if not isinstance(contract, dict) or not contract:
        return {'status': 'fail', 'mode': 'not-frozen', 'criteria': []}
    if 'criteria' not in contract and 'criteria_version' not in contract:
        return {'status': 'pass', 'mode': 'legacy-task-level', 'criteria': []}
    if not valid_criteria(contract):
        return {'status': 'fail', 'mode': 'criteria', 'criteria': [], 'reason': 'invalid-criteria-contract'}
    states = [{**item, 'status': 'unverified', 'reason': 'mapping-not-declared' if item['kind'] == 'unbound' else 'check-not-recorded'}
              for item in contract['criteria']]
    try:
        path, receipt = read_receipt(root, task)
        recorded = receipt['criteria']
        if type(receipt.get('version')) is not int or receipt['version'] != 3 or not isinstance(recorded, list) or len(recorded) != len(states):
            raise ValueError('Criterion execution schema is invalid.')
        for item, executed in zip(states, recorded):
            if not isinstance(executed, dict) or (executed.get('id'), executed.get('kind')) != (item['id'], item['kind']):
                raise ValueError('Criterion execution identity differs from its frozen contract.')
            if item['kind'] == 'check':
                if executed.get('command_hash') != verification_command_hash(item['command']):
                    raise ValueError('Criterion command changed.')
                if type(executed.get('exit_code')) is not int:
                    item['reason'] = 'main-check-failed; criterion-check-not-executed' if receipt.get('main_exit_code') else 'check-not-executed'
                    continue
                logs = executed.get('logs')
                if not isinstance(logs, dict) or set(logs) != {'stdout', 'stderr'}:
                    raise ValueError('Criterion logs are missing.')
                for stream, log in logs.items():
                    relative = Path(log['path'])
                    if relative.is_absolute() or '..' in relative.parts:
                        raise ValueError('Criterion log path is unsafe.')
                    expected_path = f'{stream}.txt' if item['command'] == contract['verification_command'] else f"criteria/{item['id']}/{stream}.txt"
                    if log['path'] != expected_path:
                        raise ValueError('Criterion log does not belong to its declared check.')
                    artifact = validate_unredirected_file_path(path.parent / relative, label='criterion log')
                    if not artifact.resolve().is_relative_to(path.parent.resolve()):
                        raise ValueError('Criterion log must stay within its execution receipt.')
                    if hashlib.sha256(artifact.read_bytes()).hexdigest() != log['sha256']:
                        raise ValueError('Criterion log changed.')
                item.update(status='passed' if executed['exit_code'] == 0 else 'failed',
                    reason='check-executed', exit_code=executed['exit_code'])
            elif item['kind'] == 'review':
                review = task.get('criterion_reviews', {}).get(item['id'], {})
                if review.get('reviewer_kind', 'unknown') not in {'human', 'agent', 'unknown'}:
                    item['reason'] = 'review-metadata-invalid'
                    continue
                if (review.get('contract_sha256'), review.get('execution_receipt'), review.get('execution_receipt_sha256')) != (contract_hash(task), task['execution_receipt'], task['execution_receipt_sha256']):
                    item['reason'] = 'review-missing-or-stale'
                    continue
                relative = Path(review['report'])
                if relative.is_absolute() or '..' in relative.parts:
                    raise ValueError('Criterion review path is unsafe.')
                report = validate_unredirected_file_path(root / relative, label='criterion review')
                if not report.resolve().is_relative_to(root.resolve()):
                    raise ValueError('Criterion review must stay within the project.')
                if hashlib.sha256(report.read_bytes()).hexdigest() != review['report_sha256']:
                    item['reason'] = 'review-report-changed'
                    continue
                item['review_report'] = relative.as_posix()
                item.update(status={'accepted': 'passed', 'rejected': 'failed'}.get(review.get('outcome'), 'unverified'),
                    reason='explicit-review-assertion', reviewer_kind=review.get('reviewer_kind', 'unknown'))
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError):
        for item in states:
            item.update(status='unverified', reason='criterion-evidence-missing-stale-or-invalid')
    return {'status': 'pass' if all(item['status'] == 'passed' for item in states) else 'fail',
        'mode': 'criteria', 'criteria': states}


def review_criterion(root: Path, task: dict[str, Any], identifier: str, report_path: str, outcome: str, reviewer_kind: str) -> dict[str, Any]:
    contract = task.get('contract')
    if not isinstance(contract, dict) or not valid_criteria(contract):
        return {'status': 'fail', 'code': 'criterion-review-not-declared'}
    criteria = contract.get('criteria', [])
    if not any(item['id'] == identifier and item['kind'] == 'review' for item in criteria):
        return {'status': 'fail', 'code': 'criterion-review-not-declared'}
    try:
        _, receipt = read_receipt(root, task)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError):
        return {'status': 'fail', 'code': 'criterion-review-execution-required'}
    if task.get('execution_status') != 'passed' or receipt.get('exit_code') != 0:
        return {'status': 'fail', 'code': 'criterion-review-execution-required'}
    report = Path(report_path)
    if not report.is_absolute():
        report = root / report
    report = validate_unredirected_file_path(report, label='criterion review report').resolve()
    relative = report.relative_to(root).as_posix()
    if relative in receipt.get('subject_paths', []):
        return {'status': 'fail', 'code': 'criterion-review-overlaps-implementation'}
    raw = report.read_bytes()
    text = raw.decode('utf-8')
    if scan_input(PromptInput(label=relative, text=text, path=relative)):
        return {'status': 'fail', 'code': 'criterion-review-untrusted-input'}
    if not isinstance(task.get('criterion_reviews', {}), dict):
        return {'status': 'fail', 'code': 'criterion-review-state-invalid'}
    review = {'outcome': outcome, 'reviewer_kind': reviewer_kind,
        'report': relative, 'report_sha256': hashlib.sha256(raw).hexdigest(),
        'contract_sha256': contract_hash(task), 'execution_receipt': task['execution_receipt'],
        'execution_receipt_sha256': task['execution_receipt_sha256'],
        'assurance': 'recorded-review-assertion; reviewer kind is declared, not authenticated'}
    task.setdefault('criterion_reviews', {})[identifier] = review
    return {'status': 'pass', 'criterion': identifier, 'review': review}
