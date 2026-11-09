"""Immutable completion commits, separate from bounded lifecycle routing state."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import alignment_state
from harness_io import atomic_write_text
from path_security import ensure_unredirected_directory, validate_unredirected_file_path

PROTOCOL = 2
RECEIPT_VERSION = 1
RECEIPT_DIRECTORY = '.tenetora/state/completion-commits'
BINDING_FIELDS = ('session_id', 'owner_id', 'conversation_id', 'conversation_continuity_id',
                  'tool', 'goal_fingerprint', 'handoff_hash', 'project_root_fingerprint',
                  'execution_attempt_id', 'verification_command_hash',
                  'verification_command_hash_version', 'verification_scope',
                  'git_state_fingerprint', 'git_state_fingerprint_version')


def binding(event: dict[str, Any]) -> dict[str, Any]:
    return {key: event.get(key, '') for key in BINDING_FIELDS}


def candidate_digest(event: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(event, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode('utf-8')).hexdigest()


def receipt_path(root: Path, session: str) -> Path:
    if not isinstance(session, str):
        raise ValueError('Completion session identity must be a string.')
    alignment_state.validate_identity(session, 'session_id', alignment_state.SESSION_ID_RE)
    name = hashlib.sha256(session.encode('utf-8')).hexdigest() + '.json'
    return validate_unredirected_file_path(root / RECEIPT_DIRECTORY / name, label='completion commit')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Completion commit contains duplicate fields.')
        result[key] = value
    return result


def validate_receipt(root: Path, value: Any, *, session: str | None = None) -> dict[str, Any]:
    """Validate JSON types before passing any value to identity/path helpers."""
    expected = {'version', 'proof', 'candidate_event_id', 'candidate_sha256', 'binding', 'committed_at'}
    if not isinstance(value, dict) or set(value) != expected or type(value.get('version')) is not int or value['version'] != RECEIPT_VERSION:
        raise ValueError('Completion commit schema is invalid.')
    for key, pattern in (('proof', r'ah-claim-[0-9a-f]{32}'), ('candidate_sha256', r'[0-9a-f]{64}')):
        if not isinstance(value[key], str) or not re.fullmatch(pattern, value[key]):
            raise ValueError('Completion commit proof or digest is invalid.')
    identity = value['candidate_event_id']
    if not isinstance(identity, str) or not identity or len(identity) > 256:
        raise ValueError('Completion commit candidate identity is invalid.')
    fields = value['binding']
    if not isinstance(fields, dict) or set(fields) != set(BINDING_FIELDS):
        raise ValueError('Completion commit binding is invalid.')
    for key in BINDING_FIELDS:
        item = fields[key]
        if key == 'verification_scope':
            valid = isinstance(item, list) and item == []
        elif key in {'verification_command_hash_version', 'git_state_fingerprint_version'}:
            valid = type(item) is int and item > 0
        else:
            valid = isinstance(item, str)
        if not valid:
            raise ValueError('Completion commit binding contains invalid field types.')
    for key in ('goal_fingerprint', 'handoff_hash', 'project_root_fingerprint', 'verification_command_hash', 'git_state_fingerprint'):
        if not re.fullmatch(r'[0-9a-f]{64}', fields[key]):
            raise ValueError('Completion commit binding contains an invalid digest.')
    if not alignment_state.SESSION_ID_RE.fullmatch(fields['session_id']) or session is not None and fields['session_id'] != session:
        raise ValueError('Completion commit session identity is invalid.')
    for key in ('owner_id', 'conversation_id', 'tool'):
        if not alignment_state.OWNER_ID_RE.fullmatch(fields[key]):
            raise ValueError('Completion commit owner identity is invalid.')
    for key in ('conversation_continuity_id', 'execution_attempt_id'):
        if fields[key] and not alignment_state.OWNER_ID_RE.fullmatch(fields[key]):
            raise ValueError('Completion commit execution identity is invalid.')
    if fields['project_root_fingerprint'] != hashlib.sha256(str(root.resolve()).encode('utf-8', errors='surrogatepass')).hexdigest():
        raise ValueError('Completion commit belongs to another project.')
    if not isinstance(value['committed_at'], str) or alignment_state.parse_utc(value['committed_at']) is None:
        raise ValueError('Completion commit timestamp is invalid.')
    return value


def _read_receipt_file(root: Path, path: Path, *, session: str | None = None) -> dict[str, Any]:
    validate_unredirected_file_path(path, label='completion commit')
    value = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique_object)
    return validate_receipt(root, value, session=session)


def read_receipt(root: Path, session: str) -> dict[str, Any] | None:
    path = receipt_path(root, session)
    return _read_receipt_file(root, path, session=session) if path.exists() else None


def wire_candidate(event: dict[str, Any]) -> bool:
    return (event.get('type') == 'verification-claim' and event.get('claim_kind') == 'completion'
        and type(event.get('completion_protocol')) is int and event['completion_protocol'] == PROTOCOL
        and event.get('status') == 'pending' and event.get('verification_status') == 'pending'
        and bool(re.fullmatch(r'ah-claim-[0-9a-f]{32}', str(event.get('claim_proof', '')))))


def valid_preparation(event: dict[str, Any]) -> bool:
    """Only a recognizable, bound reservation/candidate is neutral evidence."""
    if event.get('type') != 'verification-claim' or event.get('claim_kind') != 'completion':
        return False
    if event.get('status') != 'pending' or event.get('verification_status') != 'pending':
        return False
    version = event.get('completion_protocol')
    if version is not None and (type(version) is not int or version not in {1, PROTOCOL}):
        return False
    proof = event.get('claim_proof') or event.get('pending_claim_proof')
    if not isinstance(proof, str) or not re.fullmatch(r'ah-claim-[0-9a-f]{32}', proof):
        return False
    if event.get('claim_proof') and event.get('pending_claim_proof') not in (None, proof):
        return False
    fields = binding(event)
    if fields['verification_scope'] != []:
        return False
    for key in ('session_id', 'owner_id', 'conversation_id', 'tool'):
        if not isinstance(fields[key], str) or not fields[key]:
            return False
    for key in ('goal_fingerprint', 'handoff_hash', 'project_root_fingerprint',
                'verification_command_hash', 'git_state_fingerprint'):
        if not isinstance(fields[key], str) or not re.fullmatch(r'[0-9a-f]{64}', fields[key]):
            return False
    return all(type(fields[key]) is int and fields[key] > 0
        for key in ('verification_command_hash_version', 'git_state_fingerprint_version'))


def committed(root: Path, event: dict[str, Any]) -> bool:
    """Accept only the exact raw candidate covered by an immutable receipt."""
    if not wire_candidate(event):
        return False
    try:
        receipt = read_receipt(root, str(event.get('session_id') or ''))
    except (OSError, RuntimeError, ValueError, alignment_state.AlignmentStateError):
        return False
    return bool(receipt and receipt['proof'] == event['claim_proof']
        and receipt['candidate_event_id'] == event.get('event_id')
        and receipt['binding'] == binding(event) and receipt['candidate_sha256'] == candidate_digest(event))


def project_events(root: Path, events: list[dict[str, Any]], *, history_valid: bool = True,
                   complete_history: bool = False) -> list[dict[str, Any]]:
    """Validate raw wire states first; projection can never conceal a failure."""
    authorities = {}
    candidate_ids = {}
    try:
        directory = validate_unredirected_file_path(root / RECEIPT_DIRECTORY / '.inventory', label='completion commits').parent
        for path in directory.glob('*.json'):
            receipt = _read_receipt_file(root, path)
            if path != receipt_path(root, receipt['binding']['session_id']):
                raise ValueError('Completion commit has an invalid locator.')
            if receipt['proof'] in authorities:
                raise ValueError('Completion commit proof is not unique.')
            authorities[receipt['proof']] = receipt
            if receipt['candidate_event_id'] in candidate_ids:
                raise ValueError('Completion commit candidate identity is not unique.')
            candidate_ids[receipt['candidate_event_id']] = receipt
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, alignment_state.AlignmentStateError):
        history_valid = False
    # Retained readers have active AND archived history. Every receipt must
    # still have its exact raw candidate, even if all identity markers changed.
    if complete_history:
        digests = {candidate_digest(event) for event in events}
        if any(receipt['candidate_sha256'] not in digests for receipt in authorities.values()):
            history_valid = False
    result = []
    for event in events:
        from claim_evidence import unordered_legacy_failure
        from governance_trail import raw_event_digest
        raw_legacy_digest = raw_event_digest(event) if unordered_legacy_failure(event) else None
        authority = authorities.get(event.get('claim_proof')) if isinstance(event.get('claim_proof'), str) else None
        if isinstance(event.get('event_id'), str):
            authority = authority or candidate_ids.get(event['event_id'])
        if event.get('type') == 'verification-claim' and (not history_valid or authority or 'completion_protocol' in event):
            version = event.get('completion_protocol')
            prepared = (event.get('status') == 'pending' and event.get('verification_status') == 'pending'
                and event.get('claim_kind') == 'completion')
            if not history_valid:
                event = {**event, 'status': 'fail', 'verification_status': 'failed', 'completion_phase': 'history-invalid'}
            elif type(version) is not int or version not in {1, PROTOCOL} or not prepared or (version == PROTOCOL and not valid_preparation(event)):
                event = {**event, 'status': 'fail', 'verification_status': 'failed', 'completion_phase': 'raw-candidate-invalid'}
            elif version == 1:
                event = {**event, 'completion_phase': 'legacy-protocol-needs-migration'}
            elif event.get('claim_proof'):
                if committed(root, event):
                    event = {**event, 'status': 'pass', 'verification_status': 'passed'}
                else:
                    try:
                        receipt = read_receipt(root, str(event.get('session_id') or ''))
                        invalid = receipt is not None
                    except (OSError, RuntimeError, ValueError, alignment_state.AlignmentStateError):
                        invalid = True
                    event = {**event, 'status': 'fail' if invalid else 'pending',
                        'verification_status': 'failed' if invalid else 'pending',
                        'completion_phase': 'candidate-commit-mismatch' if invalid else 'awaiting-commit'}
        if raw_legacy_digest:
            event = {**event, '_legacy_raw_sha256': raw_legacy_digest}
        result.append(event)
    return result


def lifecycle_from_receipt(root: Path, session: str, record: dict[str, Any] | None = None) -> dict[str, Any] | None:
    try:
        receipt = read_receipt(root, session)
    except (OSError, RuntimeError, ValueError) as exc:
        raise alignment_state.AlignmentStateError(f'Completion commit cannot be read consistently: {exc}') from exc
    if receipt is None:
        return record
    fields = receipt['binding']
    if record and any(record.get(key, '') != fields[key] for key in ('owner_id', 'goal_fingerprint', 'handoff_hash')):
        raise alignment_state.AlignmentStateError('Lifecycle routing conflicts with a durable completion commit.')
    stamp = receipt['committed_at']
    base = record or {'session_id': session, 'goal': '', 'scope': [], 'non_goals': [], 'acceptance_criteria': [],
        'created_at': stamp, 'confirmed_at': stamp, 'archived_at': '', 'expired_at': '',
        **{key: fields[key] for key in ('owner_id', 'conversation_id', 'conversation_continuity_id', 'tool', 'goal_fingerprint', 'handoff_hash')}}
    return {**base, 'status': 'completed', 'completion_claim_proof': receipt['proof'],
        'completed_at': stamp, 'last_activity_at': stamp}


def project_lifecycle(root: Path, records: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {key: lifecycle_from_receipt(root, key, record) for key, record in records.items()}


def _history(root: Path, *, raw: bool = False, writer_locked: bool = False) -> dict[str, Any]:
    from governance_trail import load_retained_trail

    value = load_retained_trail(root, project=not raw, writer_locked=writer_locked)
    if value.get('invalid') or value.get('conflicts'):
        raise ValueError('Completion evidence history is invalid or conflicting.')
    return value


def durable_candidate(root: Path, event: dict[str, Any], *,
                      raw_events: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Find the exact committed raw authorization behind a projected claim."""
    if event.get('completion_protocol') != PROTOCOL:
        return None
    events = raw_events if raw_events is not None else _history(root, raw=True)['events']
    return next((raw for raw in events if raw.get('event_id') == event.get('event_id')
        and raw.get('claim_proof') == event.get('claim_proof') and binding(raw) == binding(event)
        and committed(root, raw)), None)


def retained_binding(root: Path, session: str) -> dict[str, Any] | None:
    """Durable authorization for explicit lookup after normal history pruning."""
    receipt = read_receipt(root, session)
    if receipt is None:
        return None
    events = _history(root, raw=True)['events']
    raw = next((event for event in events if event.get('claim_proof') == receipt['proof'] and committed(root, event)), None)
    if raw is None or raw.get('confirmation_assurance') != 'recorded-user-assertion':
        raise ValueError('The durable completion authorization is inconsistent.')
    return {**receipt['binding'], 'owner_id': raw['owner_id'], 'binding_kind': 'handoff',
        'alignment_status': 'confirmed', 'risk_level': '', 'continuity_available': False,
        'confirmation_assurance': raw['confirmation_assurance'],
        'confirmation_event_id': raw.get('confirmation_event_id', '')}


def prepared_candidate(root: Path, payload: dict[str, Any]) -> dict[str, Any] | None:
    from execution_receipts import same_execution
    for event in reversed(_history(root, raw=True)['events']):
        if (wire_candidate(event) and event.get('claim_proof') == payload.get('claim_proof') and binding(event) == binding(payload)
            and same_execution(event,payload)):
            return event
    return None


def migration_source(events: list[dict[str, Any]], payload: dict[str, Any], proof: str,
                     digest: str) -> dict[str, Any] | None:
    """An explicit migration must name the exact retained legacy preparation."""
    for event in reversed(events):
        version = event.get('completion_protocol')
        if (event.get('type') == 'verification-claim' and event.get('claim_kind') == 'completion'
            and event.get('status') == 'pending' and event.get('verification_status') == 'pending'
            and (version is None or type(version) is int and version == 1)
            and binding(event) == binding(payload) and candidate_digest(event) == digest
            and (event.get('claim_proof') or event.get('pending_claim_proof') or proof) == proof):
            return event
    return None


def _owned_journal_lock(root: Path) -> str | None:
    """Identify this process's exact writer lock, never a replacement writer."""
    try:
        path = validate_unredirected_file_path(root / '.tenetora/state/.governance-trail.json.lock', label='completion writer lock')
        content = path.read_bytes()
        lines = content.decode('ascii').splitlines()
        if len(lines) < 2 or lines[0] != str(os.getpid()) or not re.fullmatch(r'[0-9a-f]{32}', lines[1]):
            return None
        return hashlib.sha256(content).hexdigest()
    except (OSError, RuntimeError, ValueError):
        return None


def published(root: Path, payload: dict[str, Any]) -> bool:
    from claim_evidence import claim_handoff_problem, claim_worktree_freshness, superseding_failure

    warnings = payload.get('write_warnings', [])
    own_lock = _owned_journal_lock(root)
    writer_locked = bool(own_lock and any(isinstance(warning, dict)
        and warning.get('code') == 'completion-journal-committed-cleanup-incomplete'
        and warning.get('writer_lock_fingerprint') == own_lock for warning in warnings))
    try:
        events = _history(root, writer_locked=writer_locked)['events']
        raw_events = _history(root, raw=True, writer_locked=True)['events'] if writer_locked else None
        if writer_locked and _owned_journal_lock(root) != own_lock:
            return False
    except (OSError, RuntimeError, ValueError):
        return False
    selected = next((event for event in reversed(events) if event.get('type') == 'verification-claim'
        and event.get('claim_proof') == payload.get('claim_proof') and binding(event) == binding(payload)), None)
    return bool(selected and selected.get('status') == 'pass' and not superseding_failure(events, selected)
        and claim_worktree_freshness(root, selected, writer_locked=writer_locked)['status'] == 'pass'
        and not claim_handoff_problem(root, selected, raw_events=raw_events))


def commit(root: Path, payload: dict[str, Any]) -> list[dict[str, str]]:
    """Serialize evidence, validate immediately before the immutable commit."""
    from claim_evidence import claim_handoff_problem, claim_worktree_freshness, superseding_failure
    from governance_trail import trail_lock
    from execution_receipts import same_execution

    session = payload['session_id']
    path = receipt_path(root, session)
    warnings: list[dict[str, str]] = []
    expected = None
    owned_journal_lock = None
    try:
        # All state mutations use goal -> lifecycle -> journal lock order.
        with alignment_state.state_lock(alignment_state.lifecycle_path(root)):
            current = alignment_state.load_lifecycle(root)
            state = current['records'].get(session)
            with trail_lock(root):
                owned_journal_lock = _owned_journal_lock(root)
                events = _history(root, raw=True, writer_locked=True)['events']
                candidate = next((event for event in reversed(events) if wire_candidate(event)
                    and event.get('claim_proof') == payload['claim_proof'] and binding(event) == binding(payload)
                    and same_execution(event,payload)), None)
                if candidate is None:
                    raise ValueError('No exact raw prepared candidate is available for committing.')
                projected = project_events(root, events, complete_history=True)
                if any(event.get('completion_phase') in {'history-invalid', 'raw-candidate-invalid', 'candidate-commit-mismatch'} for event in projected):
                    raise ValueError('Completion authority is invalid or conflicting.')
                if superseding_failure(projected, candidate):
                    raise ValueError('A newer overlapping failure supersedes this candidate before commit.')
                if claim_handoff_problem(root, candidate, required=True, raw_events=events):
                    raise ValueError('Completion authorization changed or is unavailable before commit.')
                if claim_worktree_freshness(root, candidate, writer_locked=True)['status'] != 'pass':
                    raise ValueError('Verified source changed before completion commit; verify the current source again.')
                expected = {'version': RECEIPT_VERSION, 'proof': payload['claim_proof'],
                    'candidate_event_id': candidate['event_id'], 'candidate_sha256': candidate_digest(candidate),
                    'binding': binding(candidate),
                    'committed_at': dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')}
                existing = read_receipt(root, session)
                if existing:
                    if any(existing[key] != expected[key] for key in ('proof', 'candidate_event_id', 'candidate_sha256', 'binding')):
                        raise ValueError('An immutable completion commit already belongs to different evidence.')
                    expected = existing
                else:
                    if not state or any(state.get(key) != payload.get(key) for key in ('owner_id', 'goal_fingerprint', 'handoff_hash')):
                        raise ValueError('Completion lifecycle identity does not match this candidate.')
                    legacy_recovery = bool(payload.get('completion_legacy_source_sha256')
                        and migration_source(events, payload, payload['claim_proof'], payload['completion_legacy_source_sha256']))
                    if state['status'] != 'open' and not (legacy_recovery and state['status'] == 'completed' and state.get('completion_claim_proof') == payload['claim_proof']):
                        raise ValueError('This lifecycle cannot accept the completion candidate.')
                    ensure_unredirected_directory(path.parent, label='completion commits')
                    try:
                        atomic_write_text(path, json.dumps(expected, ensure_ascii=False, sort_keys=True, indent=2) + '\n')
                    except OSError:
                        if read_receipt(root, session) != expected:
                            raise
                        warnings.append({'code': 'completion-commit-cleanup-incomplete',
                            'message': 'The durable completion commit was written; write cleanup reported an error.'})
            # The routing cache follows the committed fact and cannot revoke it.
            try:
                raw = alignment_state.read_json_object(alignment_state.lifecycle_path(root), 'alignment lifecycle')
                raw = raw or alignment_state.empty_lifecycle()
                if session in raw['records']:
                    raw['records'][session] = lifecycle_from_receipt(root, session, raw['records'][session])
                    alignment_state._write_lifecycle_locked(alignment_state.lifecycle_path(root), raw, raw.get('revision', 0))
            except (OSError, ValueError, alignment_state.AlignmentStateError):
                warnings.append({'code': 'completion-routing-reconcile-pending',
                    'message': 'Completion is durably committed; its routing cache could not be reconciled.'})
    except OSError:
        if expected is None or read_receipt(root, session) != expected:
            raise
        if owned_journal_lock and _owned_journal_lock(root) == owned_journal_lock:
            warnings.append({'code': 'completion-journal-committed-cleanup-incomplete',
                'message': 'Completion is durably committed; its own journal lock needs cleanup before further writes.',
                'writer_lock_fingerprint': owned_journal_lock})
        else:
            warnings.append({'code': 'completion-commit-cleanup-incomplete',
                'message': 'Completion is durably committed; transaction cleanup reported an error.'})
    return warnings


def resume_candidate(root: Path, payload: dict[str, Any], *, explicit: bool = False) -> tuple[str | None, str | None]:
    from claim_evidence import is_claim_proof, superseding_failure

    try:
        events = _history(root)['events']
        state = alignment_state.lifecycle_record(root, payload['session_id'])
    except (OSError, RuntimeError, ValueError, alignment_state.AlignmentStateError):
        return None, 'Completion history or lifecycle is invalid, inconsistent or redirected.'
    closed = state and state.get('status') != 'open'
    if closed and (not explicit or state.get('status') != 'completed'):
        return None, 'The goal is closed; only an explicit matching recovery is allowed.'
    selected = next((event for event in reversed(events) if event.get('type') == 'verification-claim'
        and event.get('session_id') == payload['session_id'] and event.get('owner_id') == payload['owner_id']), None)
    if not selected:
        return None, 'No pending completion candidate is available.' if closed or explicit else None
    candidate = selected.get('pending_claim_proof') or selected.get('claim_proof')
    version = selected.get('completion_protocol')
    protocol_candidate = (type(version) is int and version == PROTOCOL and selected.get('claim_kind') == 'completion'
        and selected.get('status') in {'pending', 'pass'} and selected.get('verification_status') in {'pending', 'passed'})
    legacy_candidate = (closed and explicit and (version is None or type(version) is int and version == 1)
        and selected.get('status') == 'pending' and selected.get('verification_status') == 'pending'
        and selected.get('claim_kind') == 'completion')
    if legacy_candidate and not candidate:
        candidate = state.get('completion_claim_proof')
    if not protocol_candidate and not legacy_candidate:
        return None, 'The latest evidence is not a recoverable candidate.' if closed or explicit else None
    if closed and state.get('completion_claim_proof') != candidate:
        return None, 'The closed goal belongs to another completion candidate.'
    from execution_receipts import same_execution
    if not is_claim_proof(candidate) or binding(selected) != binding(payload) or not same_execution(selected,payload):
        if not closed and not explicit:
            return None, None  # A new verified snapshot gets a new proof, never the stale one.
        return None, 'Pending identity, command or verified source changed; do not reuse its proof.'
    if superseding_failure(events, selected):
        if not closed and not explicit:
            return None, None
        return None, 'A newer overlapping failed or blocked verification supersedes this candidate.'
    if closed and not read_receipt(root, payload['session_id']):
        raw = _history(root, raw=True)['events']
        original = next((event for event in raw if event.get('event_id') == selected.get('event_id')), None)
        digest = candidate_digest(original) if legacy_candidate and original else selected.get('completion_legacy_source_sha256')
        if not explicit or not digest or not migration_source(raw, payload, candidate, digest):
            return None, 'The durable completion commit is missing; routing state cannot replace it.'
        payload['completion_legacy_source_sha256'] = digest
    return candidate, None
