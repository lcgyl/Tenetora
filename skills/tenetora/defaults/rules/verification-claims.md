# Verification Claim Rules

Source: Tenetora default rules.

- Decide the smallest meaningful verification command before editing when the task changes code, config, build behavior, rules, or workflows.
- Do not claim completion, pass, readiness, or safety until the relevant command has run.
- Report the exact command and result. If a command was skipped, report why and name the closest substitute check.
- Use `--claim-kind partial-verification` for a local or partial check; it must never satisfy a completion gate. Use `--claim-kind completion` only for the full command that covers the declared goal. Use `--claim-kind blocked` or `--claim-kind failed` when verification cannot pass; do not manufacture a passed proof.
- Completion claims must include the exact verification command, current project root, Git HEAD/worktree fingerprint, and the current session/owner binding when an alignment session exists. The guard stores a normalized command hash and length rather than the raw command in the governance trail.
- A completion check must pass `--expected-verification-command` and compare the latest matching event. A newer failed or skipped verification invalidates an older passed claim.
- A completion claim must bind to a confirmed or risk-accepted handoff, its handoff hash, owner, conversation continuity, tool, valid goal fingerprint, and audited confirmation record. An active, paused, blocked, awaiting-confirmation, expired, legacy-unattested, or bare-CLI-confirmed session cannot satisfy completion.
- A `user-message` confirmation record preserves the host event id, actor identity, request revision, goal fingerprint, and conversation continuity. It is auditable provenance, not cryptographic proof of a human actor; do not describe it as stronger assurance than the host provides.
- Without a confirmed handoff and aligned goal, record only partial-verification, blocked, or failed work. A proof from another owner, conversation continuity, tool, handoff hash, or earlier goal revision is not reusable.
- Every passed Git-project claim records a versioned worktree content fingerprint independent of the Git index and HEAD. It covers the verified paths, file types, permissions, symlink targets, and file bytes; the volatile governance trail is excluded so recording the claim does not invalidate itself.
- Use repeatable `--verification-scope <project-relative-path>` for a module-level check. Omit it to cover the whole worktree. Scopes must stay inside the project and are normalized before the claim is recorded.
- Different agents may verify different non-overlapping modules and create separate scoped partial claims. The commit guard maps each staged path to the latest covering claim and can combine those claims for coverage; an uncovered staged path is a warning and must not be reported as complete fresh evidence.
- Scoped partial claims provide commit-coverage evidence only. They cannot be combined into a completion claim; completion still requires one full verification command covering the declared goal, plus the matching confirmed handoff and identity binding.
- A passed verification command is not durable evidence until its claim is recorded. Immediately after the command exits successfully, record the matching claim in the same turn; a prose report alone is not reusable by a later agent or Git authorization.
- Before a later Git authorization, use `--check-proof-only` with the exact command. It may reuse a fresh proof after staging or committing the exact verified content, but changed verified content, metadata, scope, command, binding, or a newer failed verification requires a new check. Do not rerun a full command solely because the next step is commit, push, or tag.
- Independent review remains bound to the final Git index for commit and the final HEAD/tree for push. Worktree fingerprints support verification freshness but do not replace review of the object that will actually be committed or pushed. A review started with repeatable `--review-scope <git-worktree-relative-path>` also records a path-level subject snapshot for that declared scope. The scope is relative to the Git worktree selected by `--git-path`, not the outer governed project. If the full Git subject changes while the declared scope is unchanged, the guard reports `independent-review-rebind-eligible`; run its explicit `tenetora delegation --review-rebind` command and obtain a new reviewer result for the new subject. It never marks the new subject passed automatically. A delta inside the declared scope, an invalid scope snapshot, or a legacy review without an explicit scope remains stale and requires a new manual review.
- Distinguish mechanical checks, project tests, and harness audits; one does not automatically prove the others.
- If verification fails, report the failure and the next concrete fix or blocker instead of smoothing it over.
- For documentation-only changes, run a lightweight text or diff check when available.

## Incremental Verification Planning

Before repeating a verification command after a passed claim, run:

```bash
tenetora verification-plan --json
```

Use the returned decision as follows:

- `reuse`: the verified content is unchanged; reuse the matching claim proof and do not rerun the full suite.
- `targeted`: only classified documentation, release metadata, or other non-impacting paths changed after the claim, or no verification claim exists but the current change is limited to those paths; run only the listed checks, such as `git diff --check` or version-contract tests.
- `rerun`: a verification-impacting path changed, or freshness cannot be proven; run the smallest checks covering that path. Use the full suite only when the affected boundary cannot be narrowed.

Do not rerun the full suite solely because `CHANGELOG`, `README`, release notes, or another classified documentation path changed. Claims without the supported per-path snapshot metadata fail closed when content changed and must be refreshed by verification.

In multi-agent work, pass `--scope <project-relative-path>` for the module being verified. The commit guard analyzes only the paths staged for the current commit; unrelated unstaged work from another agent must not force this commit's plan to rerun. A scoped partial claim covers only its declared module and cannot be used as a whole-project completion claim.
