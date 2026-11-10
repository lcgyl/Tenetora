"""Shared claim proof and verification-command semantics."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from governance_trail import load_retained_trail, raw_event_digest
from worktree_fingerprint import (
    WORKTREE_FINGERPRINT_VERSION,
    git_state_fingerprint,
    normalize_scopes,
    verification_scopes_intersect,
)

CLAIM_PROOF_PATTERN = re.compile(r"^ah-claim-[0-9a-f]{32}$")


def is_claim_proof(value: object) -> bool:
    return isinstance(value, str) and CLAIM_PROOF_PATTERN.fullmatch(value) is not None


VERIFICATION_COMMAND_HASH_VERSION = 2


def normalize_verification_command(command: str) -> str:
    """Identity v2 preserves the complete program text, including edge bytes.

    Guard commands can name argv, shells, heredocs or project-owned checks.
    Generic whitespace normalization cannot prove equivalence across them.
    """
    return str(command)


def verification_command_hash(command: str) -> str:
    return hashlib.sha256(normalize_verification_command(command).encode("utf-8")).hexdigest()


def event_verification_scopes(event: dict[str, Any]) -> tuple[str, ...] | None:
    raw = event.get("verification_scope", [])
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        return None
    try:
        return normalize_scopes(raw)
    except ValueError:
        return None


def pending_preparation(event: dict[str, Any]) -> bool:
    from completion_protocol import valid_preparation

    return valid_preparation(event)


def verification_failure(event: dict[str, Any]) -> bool:
    if event.get('type') in ('verification-execution-start','verification-execution'):
        return event.get('type')=='verification-execution-start' or not execution_event_passed(event)
    return (event.get('type') == 'verification-claim' and not pending_preparation(event)
        and not (event.get('status') == 'pass' and event.get('verification_status') == 'passed'
            and event.get('claim_kind') in ('completion', 'partial-verification')))


def execution_event_passed(event:dict)->bool:
    return (event.get('type')=='verification-execution' and event.get('status')=='pass'
        and event.get('verification_status')=='passed' and type(event.get('exit_code')) is int
        and event['exit_code']==0 and type(event.get('verification_command_hash_version')) is int
        and event['verification_command_hash_version']==VERIFICATION_COMMAND_HASH_VERSION
        and all(isinstance(event.get(key),str) and re.fullmatch(r'[0-9a-f]{64}',event[key])
            for key in ('execution_receipt_sha256','verification_command_hash','project_root_fingerprint','git_state_fingerprint')))


def execution_pair_passed(start:dict|None,event:dict)->bool:
    """Only an intact pending start can be resolved by its successful end."""
    return (isinstance(start,dict) and start.get('type')=='verification-execution-start'
        and start.get('status')=='pending' and start.get('verification_status') in (None,'pending')
        and isinstance(start.get('execution_run_id'),str)
        and re.fullmatch(r'vr-[0-9a-f]{32}',start['execution_run_id']) is not None
        and start['execution_run_id']==event.get('execution_run_id')
        and execution_event_passed(event) and event_verification_scopes(start) is not None
        and reliable_sequence(start) is not None and reliable_sequence(event) is not None
        and reliable_sequence(start)<reliable_sequence(event)
        and type(start.get('verification_command_hash_version')) is int
        and all(start.get(key)==event.get(key) for key in ('verification_scope',
            'verification_command_hash','verification_command_hash_version','project_root_fingerprint')))


def reliable_sequence(event: dict[str, Any]) -> int | None:
    sequence = event.get('_journal_sequence')
    identity = event.get('event_id')
    return sequence if isinstance(identity, str) and identity.strip() and type(sequence) is int and sequence > 0 else None


def unordered_legacy_failure(event: dict[str, Any]) -> bool:
    return (verification_failure(event) and reliable_sequence(event) is None
        and 'completion_protocol' not in event and not event.get('claim_proof')
        and event.get('claim_kind') in ('failed', 'blocked') and event.get('status') == 'fail'
        and event.get('verification_status') in ('failed', 'blocked', 'skipped'))


def legacy_failure_orders(events) -> dict[str, int]:
    orders: dict[str, int] = {}
    for event in events:
        sequence = reliable_sequence(event)
        digest = event.get('legacy_failure_sha256')
        if (event.get('type') == 'legacy-verification-order' and sequence is not None
            and isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest)):
            orders[digest] = min(sequence, orders.get(digest, sequence))
    return orders


def superseding_failure(events, selected: dict[str, Any]) -> bool:
    index = next((index for index in range(len(events) - 1, -1, -1)
        if events[index].get('event_id') == selected.get('event_id') and selected.get('event_id')), None)
    if index is None:
        index = next((index for index in range(len(events) - 1, -1, -1) if events[index] == selected), None)
    if index is None:
        return True
    scope = event_verification_scopes(selected)
    if scope is None:
        return True
    orders = legacy_failure_orders(events)
    selected_sequence = reliable_sequence(selected)
    if selected.get('execution_assurance')=='observed':
        origin=selected.get('execution_origin_sequence')
        if type(origin) is not int or origin<1 or selected_sequence is not None and origin>selected_sequence:
            return True
        selected_sequence=origin
    starts={event.get('execution_run_id'):event for event in events
        if event.get('type')=='verification-execution-start' and isinstance(event.get('execution_run_id'),str)}
    completed_runs=set()
    for event in events:
        run_id=event.get('execution_run_id')
        start=starts.get(run_id) if isinstance(run_id,str) else None
        if execution_pair_passed(start,event):
            completed_runs.add(run_id)
    for later_index, newer in enumerate(events):
        if later_index == index:
            continue
        if not verification_failure(newer):
            continue
        if newer.get('type')=='verification-execution-start' and isinstance(newer.get('execution_run_id'),str) and newer.get('execution_run_id') in completed_runs:
            continue
        # Unordered legacy failures may be sorted into a telemetry prefix.
        # They cannot prove an earlier verification state until a writer has
        # accepted them with a real journal identity and positive sequence.
        sequence = reliable_sequence(newer)
        if sequence is None and unordered_legacy_failure(newer):
            digest = newer.get('_legacy_raw_sha256') or raw_event_digest(newer)
            sequence = orders.get(digest)
        if sequence is not None and selected_sequence is not None and sequence < selected_sequence:
            continue
        if sequence is not None and selected_sequence is None and later_index < index:
            continue
        later_scope = event_verification_scopes(newer)
        if later_scope is None or verification_scopes_intersect(scope, later_scope):
            return True
    return False


def claim_handoff_problem(root: Path, event: dict[str, Any], *, required: bool = False,
                          raw_events: list[dict[str, Any]] | None = None) -> str | None:
    """One authorization rule for reuse, including normally pruned handoffs."""
    if not required and event.get('claim_kind') != 'completion' and not event.get('handoff_hash'):
        return None
    import alignment_state
    from completion_protocol import durable_candidate

    for key in ('session_id', 'owner_id', 'conversation_id', 'goal_fingerprint', 'handoff_hash'):
        if not isinstance(event.get(key), str) or not event[key]:
            return 'claim-handoff-invalid'
    if (not re.fullmatch(r'[0-9a-f]{64}', event['goal_fingerprint'])
        or not re.fullmatch(r'[0-9a-f]{64}', event['handoff_hash'])
        or event.get('confirmation_assurance') != 'recorded-user-assertion'):
        return 'claim-handoff-invalid'
    try:
        handoff = alignment_state.load_handoff(root, event['session_id'])
        if handoff is None:
            return None if durable_candidate(root, event, raw_events=raw_events) else 'claim-handoff-unavailable'
        if (handoff.get('status') not in {'confirmed', 'accepted-with-risks'}
            or any(handoff.get(key) != event[key] for key in ('owner_id', 'conversation_id', 'goal_fingerprint'))
            or alignment_state.handoff_hash(handoff) != event['handoff_hash']):
            return 'claim-handoff-invalid'
    except (OSError, RuntimeError, ValueError, alignment_state.AlignmentStateError):
        return 'claim-handoff-invalid'
    return None


def claim_worktree_freshness(root: Path, event: dict[str, Any], *, validate_execution: bool = True, writer_locked: bool = False) -> dict[str, Any]:
    """Compare a claim's content evidence with the current worktree."""
    if validate_execution and event.get('execution_assurance')=='observed':
        from execution_receipts import claim_execution_problem
        problem=claim_execution_problem(root,event,writer_locked=writer_locked)
        if problem:
            return {'status':'fail','code':problem,'message':'Observed verification receipt is invalid or changed.'}

    if event.get('verification_command_hash_version') != VERIFICATION_COMMAND_HASH_VERSION:
        return {'status': 'fail', 'code': 'claim-command-identity-unsupported',
            'message': 'This claim predates exact command identity; rerun verification and record a new claim.'}
    scopes = event_verification_scopes(event)
    if scopes is None:
        return {
            "status": "fail",
            "code": "claim-verification-scope-invalid",
            "message": "The claim contains an invalid verification scope.",
        }
    current = git_state_fingerprint(root, scopes,known_paths=event.get('git_state_entries') or ())
    recorded = event.get("git_state_fingerprint")
    recorded_version = event.get("git_state_fingerprint_version")
    if current is None and recorded is None:
        return {
            "status": "unavailable",
            "code": "claim-worktree-fingerprint-unavailable",
            "message": "A Git content fingerprint is unavailable for this project.",
            "current_fingerprint": None,
            "recorded_fingerprint": None,
        }
    if current is None:
        return {
            "status": "fail",
            "code": "claim-worktree-fingerprint-unavailable",
            "message": "The current Git content fingerprint could not be read.",
            "current_fingerprint": None,
            "recorded_fingerprint": recorded,
        }
    if recorded_version != WORKTREE_FINGERPRINT_VERSION:
        return {
            "status": "fail",
            "code": "claim-worktree-fingerprint-unsupported",
            "message": "The claim uses an older or missing worktree fingerprint version.",
            "recorded_fingerprint_version": recorded_version,
            "current_fingerprint_version": WORKTREE_FINGERPRINT_VERSION,
        }
    if not isinstance(recorded, str) or not re.fullmatch(r"[0-9a-f]{64}", recorded):
        return {
            "status": "fail",
            "code": "claim-worktree-fingerprint-missing",
            "message": "The claim does not contain a valid worktree content fingerprint.",
            "current_fingerprint": current,
            "recorded_fingerprint": recorded,
        }
    if recorded != current:
        return {
            "status": "fail",
            "code": "claim-worktree-changed",
            "message": "Verified file paths, types, permissions, or contents changed after verification.",
            "current_fingerprint": current,
            "recorded_fingerprint": recorded,
        }
    return {
        "status": "pass",
        "code": "claim-worktree-fresh",
        "message": "The claim's verified worktree content is unchanged.",
        "current_fingerprint": current,
        "recorded_fingerprint": recorded,
        "verification_scope": list(scopes),
    }


def validate_claim(root: Path, proof: str, command: str, required_paths: list[str]) -> dict[str, Any]:
    """Validate the actual evidence subject, not only a proof's existence.

    Consumers supply the files they need verified. A claim over a disjoint
    scope, foreign project, superseded result, or changed tree cannot cover it.
    This function is read-only; it neither runs nor claims the supplied command.
    """
    def failure(code: str, message: str) -> dict[str, Any]:
        return {"status": "fail", "code": code, "message": message}

    if not is_claim_proof(proof) or not command.strip():
        return failure('claim-proof-invalid', 'A proof and its actual verification command are required.')
    trail = load_retained_trail(root)
    if trail.get('invalid') or trail.get('conflicts'):
        return failure('claim-trail-invalid', 'Claim evidence is unreadable or divergent.')
    events = trail['events']
    index = next((index for index in range(len(events) - 1, -1, -1) if events[index].get('claim_proof') == proof and events[index].get('type') == 'verification-claim'), None)
    if index is None:
        return failure('claim-proof-not-found', 'The verification proof is not in this project.')
    event = events[index]
    if event.get('status') != 'pass' or event.get('verification_status') != 'passed' or event.get('claim_kind') not in {'completion', 'partial-verification'}:
        return failure('claim-not-passed', 'The selected claim is not passed verification.')
    if claim_handoff_problem(root, event):
        return failure('claim-handoff-invalid', 'The claim has no valid retained handoff or durable completion authorization.')
    project = hashlib.sha256(str(root.resolve(strict=False)).encode('utf-8', errors='surrogatepass')).hexdigest()
    if event.get('project_root_fingerprint') != project:
        return failure('claim-project-mismatch', 'The claim belongs to another project.')
    if event.get('verification_command_hash_version') != VERIFICATION_COMMAND_HASH_VERSION:
        return failure('claim-command-identity-unsupported', 'Revalidate legacy command evidence; original command bytes are unavailable.')
    if event.get('verification_command_hash') != verification_command_hash(command):
        return failure('claim-command-mismatch', 'The command does not match the recorded claim.')
    try:
        raw_scope = event.get('verification_scope', [])
        if not isinstance(raw_scope, (list, tuple)) or any(not isinstance(item, str) for item in raw_scope):
            raise ValueError('malformed scope')
        scope = normalize_scopes(raw_scope)
        paths = normalize_scopes(required_paths)
    except (TypeError, ValueError):
        return failure('claim-scope-invalid', 'Claim or task paths are invalid.')
    if scope and (not paths or any(not any(path == parent or path.startswith(parent + '/') for parent in scope) for path in paths)):
        return failure('claim-scope-uncovered', 'The claim scope does not cover every required task path.')
    if superseding_failure(events, event):
        return failure('claim-superseded', 'A newer overlapping verification failed or was blocked.')
    if event.get('git_state_fingerprint_version') != WORKTREE_FINGERPRINT_VERSION:
        return failure('claim-fingerprint-unsupported', 'The claim fingerprint version is unsupported.')
    if claim_worktree_freshness(root, event)['status'] != 'pass':
        return failure('claim-stale', 'Verified content changed after the claim.')
    return {'status': 'pass', 'code': 'claim-fresh-and-covered', 'event': event}
