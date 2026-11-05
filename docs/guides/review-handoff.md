# Rotating review handoff

Rotate between already installed, qualified hosts when practical. A separate
Codex context is an independent perspective, not a different host or model.

1. Freeze and stage the selected content. Record baseline, HEAD, index tree,
   paths, actual check commands/exits, acceptance gates and the portable
   `code-reviewer` contract in the [handoff template](../templates/review-handoff.md).
2. Verify the carrier's actual tools. Allow read/search only; omit shell,
   writes, network fetch and approval tools. Prompt-only restrictions must be
   recorded as `prompt-only/soft-boundary`, never permission isolation.
3. Record `tenetora delegation --review-start --trigger commit` with actual
   resolution mode, receiving `--host-tool`, agent, isolation and initiating
   session/owner. CLI records; the host executes. Reviewer execution identity
   does not replace the initiating owner's task alignment or inherit proofs.
4. Dispatch once with a timeout. The reviewer returns findings with file/line,
   trigger, expected/actual behavior, evidence, tools used and observed identity.
   Unknown model/identity information stays unknown. A zero host exit is not a
   passed review. Do not let the reviewer edit, approve or perform Git actions.
5. Save the report under ignored `.tenetora/.cache/subagents/`; record
   `--review-result --dispatch-id <id> --result passed|blocker|failed
   --report-file <relative-path>`. Reproduce blockers before repairing, analyze
   repeated root causes, and retain the three-review/two-repair bound.
6. For an out-of-scope delta, use the explicit `--review-rebind` command and
   obtain a new result. In-scope changes need a fresh review. A merge creates a
   new HEAD; do not label the branch review as review of the merge commit.

If no qualified carrier exists, auth fails or the bounded invocation times out,
record `--unavailable --dispatch-id <id> --reason <bounded-reason>` and label
self-review **not independently reviewed**. Ordinary authorized coding may
continue; release policy retains review debt. Do not install a new host, alter
credentials or retry indefinitely.

An unavailable dispatch stays terminal. A later semantic review accepted for the
current Git subject can cover its debt only for the same initiating owner,
session, conversation, goal, repository and role contract. A historical pass,
general scout result, failed result or narrower review cannot clear it.
Known scout, security-auditor and implementer unavailability is reported in
`non_review_unavailable`, not promoted to a code-review obligation. This gate
does not define an independent security-review policy; unknown legacy review
records remain conservative.

For a changed subject, hand the old debt ID and original fingerprint plus the
delta to a new reviewer of the full current subject. Only after that review
passes, record `--review-result --dispatch-id <new-id> --result passed
--resolve-review-debt <old-id> --report-file <relative-report>` (up to 20 IDs).
The report must name each debt ID, its original fingerprint and the reviewed
current fingerprint, and explain the covered delta. The old unavailable record
is retained. A debt evicted from the smaller dispatch-state window is resolved
from the consistent immutable trail while that evidence is retained. Missing
legacy identity or subject data needs manual investigation;
it is not silently discharged. A stale current review reopens the applicable
gate. These are evidence links, not a successful cross-host or model claim.

An exercise uses an actual bounded read-only review, retaining its handoff,
raw result, subject and owner metadata. A simulated transport is only a contract
exercise and cannot satisfy cross-host observation.
