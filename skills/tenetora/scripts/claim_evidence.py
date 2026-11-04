"""Shared claim proof and verification-command semantics."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from governance_trail import iter_events, load_trail
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


def claim_worktree_freshness(root: Path, event: dict[str, Any]) -> dict[str, Any]:
    """Compare a claim's content evidence with the current worktree."""

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
    current = git_state_fingerprint(root, scopes)
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
    trail = load_trail(root)
    if trail.get('invalid') or trail.get('conflicts'):
        return failure('claim-trail-invalid', 'Claim evidence is unreadable or divergent.')
    events = list(iter_events(root))
    index = next((index for index in range(len(events) - 1, -1, -1) if events[index].get('claim_proof') == proof and events[index].get('type') == 'verification-claim'), None)
    if index is None:
        return failure('claim-proof-not-found', 'The verification proof is not in this project.')
    event = events[index]
    if event.get('status') != 'pass' or event.get('verification_status') != 'passed' or event.get('claim_kind') not in {'completion', 'partial-verification'}:
        return failure('claim-not-passed', 'The selected claim is not passed verification.')
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
    for newer in events[index + 1:]:
        if newer.get('type') != 'verification-claim' or (newer.get('status') == 'pass' and newer.get('verification_status') == 'passed'):
            continue
        try:
            raw_scope = newer.get('verification_scope', [])
            if not isinstance(raw_scope, (list, tuple)) or any(not isinstance(item, str) for item in raw_scope):
                raise ValueError('malformed newer scope')
            overlaps = verification_scopes_intersect(scope, normalize_scopes(raw_scope))
        except (TypeError, ValueError):
            overlaps = True
        if overlaps:
            return failure('claim-superseded', 'A newer overlapping verification failed or was blocked.')
    if event.get('git_state_fingerprint_version') != WORKTREE_FINGERPRINT_VERSION:
        return failure('claim-fingerprint-unsupported', 'The claim fingerprint version is unsupported.')
    if claim_worktree_freshness(root, event)['status'] != 'pass':
        return failure('claim-stale', 'Verified content changed after the claim.')
    return {'status': 'pass', 'code': 'claim-fresh-and-covered', 'event': event}
