---
name: tenetora-loop
description: Use when an existing `.tenetora` governed task needs a bounded execution loop, resume after interruption, repeated check-fix-verify cycles, controlled next-step planning, or user says continue/resume/next step/继续/下一步 without letting the agent run indefinitely.
---

# Tenetora Loop

## Overview

This skill keeps `.tenetora`-governed work inside a bounded loop: reload current context, execute one small step, verify, decide whether to continue, update durable harness facts if needed, and stop clearly. At a commit boundary it can also govern an independent review/fix/review cycle without becoming a general implementation method.

It is not an autonomous background agent. It does not create `.tenetora` from scratch, replace update/audit, or grant permission to keep working without progress.

## Freshness Gate

Before using cached instructions, ensure the CLI and read the latest runtime copy:

```bash
python3 <tenetora-skill-dir>/scripts/ensure_cli.py --install --json
<returned-absolute-command> skill-instructions --skill tenetora-loop
```

`<tenetora-skill-dir>` means this exact skill instance loaded by the current host. Use the JSON `command` absolute path for every later CLI call. Never fall back to another host's `.agents/skills`, `.codex/skills`, `.claude/skills`, `.cursor/skills`, `.opencode/skills`, or `.zcode/skills`; never use or persist `PYTHONPATH=<tool-skill>/cli python -m tenetora.cli` as an alias, rule, or normal invocation. Direct `PYTHONPATH` execution is only a bounded diagnostic from a checked-out Tenetora source tree.

Treat `available: true` plus `path_ready: false` as usable. Continue silently with the absolute `command` value returned by `ensure_cli.py`; PATH is optional terminal ergonomics and should be mentioned only when the user asks about direct terminal invocation.

If the returned content differs from this loaded skill, follow the returned instructions instead of cached text.

## Host Dispatch Availability Gate

Before recording `unavailable` for any portable subagent role, inspect the current host's actual tool catalog, including already visible tools and deferred or discoverable tool metadata, for native or custom dispatch (`spawn_agent` or the host equivalent), waiting, and result retrieval. Static `doctor`, plugin, or Hook status describes platform support and lifecycle observation only; missing or inactive Hooks never prove that the current host session cannot dispatch. When a qualified dispatch tool exists, use it once and inspect the child result before recording a semantic outcome. Record `unavailable` only after the current session capability is absent, fails, or cannot satisfy the role boundary, and persist the bounded reason.

## Trigger Matrix

| Trigger | Use |
| --- | --- |
| User says continue, resume, keep going, next step, or asks to complete a multi-step plan | Use this bounded loop. |
| User says 继续, 下一步, 恢复, 继续推进, or similar natural-language continuation | Use this bounded loop when `.tenetora` exists and the prior task has a concrete goal. |
| Work resumed after compaction, interruption, or a long-running verification step | Reload state, then continue only from current evidence. |
| `.tenetora/state/loop-state.json` is active and the current user message asks to proceed | Load loop state, confirm it matches the newest user message, then continue one bounded cycle. |
| User asks to check, fix, verify, and report until safe | Run one check-fix-verify cycle at a time with explicit stop conditions. |
| A task exposes stale `.tenetora` facts, changed rules, or changed verification workflows | Pair with `tenetora-update`, then continue the loop. |
| Final confidence is needed before handing back | Pair with `tenetora-audit`, then stop with a concise report. |
| A high-risk loop lacks a confirmed goal-matching alignment handoff | Stop and route to `tenetora-align`; the loop does not conduct the interview itself. |
| A commit boundary materially benefits from independent review | Use the portable `code-reviewer` contract and the bounded delegation review cycle below. |

## When Not To Use

- Do not use when `.tenetora` is missing; use `tenetora-init`.
- Do not use to refresh stale harness content directly; use `tenetora-update`.
- Do not use as a substitute for stable, reliable, controlled acceptance; use `tenetora-audit`.
- Do not use for ordinary business code work unless the task is already governed by `.tenetora`.
- Do not run indefinitely, retry blindly, or continue after the same blocker repeats.

## Loop Contract

Before editing files or running broad commands, establish:

1. Current user goal and newest user message.
2. Relevant `.tenetora` entry files and project rules.
3. Concrete exit criteria.
4. Allowed approval boundaries for commits, pushes, destructive commands, network access, and secret-bearing files.
5. A small next step that can be verified.
6. For an applicable cross-boundary plan, the declared minimum safe skeleton, current gate evidence, and deferred refinement list.

## Controlled Auto-Trigger

Use automatic triggering only from a user message or from an AI tool's skill router. The policy name is `user-message-or-tool-routing-only`. Never start from state alone, never run in the background, and never start a background loop.

Read `.tenetora/state/loop-state.json` when it exists. Treat it as resumable state, not ground truth. Loop state is owner-bound; an active state from another conversation or a legacy unowned state is reported as `conflict` with its goal withheld:

- `status: active` means a current user message such as continue/resume/next step may resume the loop.
- `next_loop_prompt` is the preferred user-facing prompt for continuation.
- `same_blocker_count >= 2` is a stop signal unless new evidence changes the blocker.
- `transitions[]` records status changes and helps explain how the loop reached the current state.
- `review_cycle` records at most three reviews and two repairs; it is governance state, not permission for background execution.
- `policy.background_execution` must remain false.
- `session_id`, `owner_id`, `conversation_id`, and `revision` identify the writer; never adopt an active state without the matching identities.

Use the CLI to inspect or update this state:

```bash
tenetora loop-state --json --session-id "<session-id>" --owner-id "<owner-id>"
tenetora loop-state --write --session-id "<session-id>" --owner-id "<owner-id>" --status active --goal "<goal>" --exit-criteria "<done condition>" --next-prompt "use tenetora-loop 继续"
tenetora loop-state --write --session-id "<session-id>" --owner-id "<owner-id>" --status done --latest-verification "<check result>"
```

## Continuing Across Conversations

An active loop, alignment handoff, or verification claim belongs to its recorded session and owner. A new conversation must not treat a project-level status file as its own goal:

1. Run `tenetora alignment --list --json` when the session id is unknown.
2. Inspect the selected session with `tenetora alignment --status --session-id <id> --owner-id <owner> --json`.
3. Resume or mutate only with the matching session and owner; verify the conversation id when the host provides one.
4. Run the verification again and create a new claim bound to the current session, owner, conversation, tool, and goal fingerprint.

The Stop hook's missing-proof reminder explains this boundary deliberately. A normal Stop is non-blocking and does not authorize adopting another conversation's goal, require a passed claim, or start a follow-up loop. Fail-closed claim enforcement is reserved for an explicitly marked completion workflow (`completion_workflow: true`, `completion_status: completed`, or `TENETORA_REQUIRE_COMPLETION_CLAIM=1`). Blocked, failed, partial, and awaiting-input reports do not need a passed completion claim. An old claim proof is never a substitute for current verification.

## Reusing Verification Before Git Authorization

After a verification command passes, record its claim in the same turn before stopping. A
prose report is not durable evidence for a later agent or Git authorization. Before starting
another full run, ask the plan for the smallest necessary action:

```bash
tenetora verification-plan --json
```

`reuse` means the claim still covers the verified content and no verification rerun is needed.
`targeted` means only non-impacting paths changed after the claim, or that there is no claim but
the current change is limited to non-impacting paths, so execute only the returned focused checks.
`rerun` means a covered path changed or freshness could not be proven; run the smallest checks that
cover the changed boundary and escalate to the full suite only when the boundary cannot be identified.
A changelog, README, release note, or classified documentation edit alone is never a reason to repeat
the full suite. After a targeted check passes, record its claim before stopping as well.

For concurrent agents, use `--scope` for the module being worked on. A claim's per-path snapshot
is the source of truth for changes after that claim. The commit guard receives only the current
staged path set, so unstaged work belonging to another module does not widen this commit's
verification plan. Do not treat a scoped partial claim as whole-project completion evidence.

Independent review has a separate object boundary. Start a module-scoped review when the reviewer is intentionally
responsible for only selected paths:

```bash
tenetora delegation --review-start --trigger commit --review-scope src/module-a
```

Each `--review-scope` is relative to the Git worktree selected by `--git-path`; for a nested repository, do not prefix
the scope with the outer governed-project path.

The review still binds to the final Git index or HEAD/tree. When another agent changes paths outside that declared
scope, the guard may report `independent-review-rebind-eligible` with a bounded path delta. Use the returned explicit
`tenetora delegation --review-rebind` command; it creates a new reviewer dispatch for the new Git subject and carries
the delta for review. A new `--review-result --result passed` is required. Never edit review state to copy the old
pass, and never rebind when the delta enters the declared scope. Reviews without an explicit scope keep the legacy
strict whole-subject comparison.

When a full verification has already passed and the next user message only authorizes `commit`, `push`, or `tag`, check the existing completion claim before rerunning the full command. The claim should have been recorded in the same turn as the verification:

```bash
tenetora guard --action claim --claim-kind completion --check-proof-only \
  --expected-verification-command "<same full verification command>" \
  --session-id "<session-id>" --owner-id "<owner-id>" --json
```

If this check passes, reuse the returned claim proof and continue with the separately required action guard. The proof fingerprint covers the verified file paths, file types, permissions, symlink targets, and file bytes; it deliberately ignores Git index state and the commit ID, so staging or committing the exact verified content does not force another full verification. A changed file, path, type, permission, symlink target, verification command, project root, goal, identity, or newer failed verification still invalidates the proof and requires a new verification run. A claim never grants commit, push, tag, or release permission.

For parallel work by multiple agents, record module-level evidence explicitly:

```bash
tenetora guard --action claim --claim-kind partial-verification \
  --verification-command "<module verification command>" --verification-status passed \
  --verification-scope src/module-a --verification-scope tests/module-a
```

Omit `--verification-scope` only when the command verified the whole worktree. Each agent or program should claim its own module and owner-bound session; the commit guard combines the latest fresh claims by staged path. A missing claim for a staged path is reported as uncovered evidence, not silently treated as verified. Scoped partial claims cannot be combined into a completion claim. Completion still needs one full goal-covering verification, a matching confirmed handoff, and its claim proof.

## Independent Review Cycle

Use this cycle only when an independent perspective materially improves a commit, push, PR, MR, or code-completion claim. Read `.tenetora/rules/subagent-dispatch.md` and `.tenetora/templates/subagents/code-reviewer.spec.md` first.

1. Resolve the role in order: a qualified dedicated agent, a qualified built-in carrier with project contract injection, then main-agent self-review. For built-in fallback, render with `tenetora delegation --render-prompt --role code-reviewer ...`; `prompt-only` is allowed only as a recorded `soft-boundary`.
2. Record the actual review attempt with `tenetora delegation --review-start --trigger commit` plus its `--resolution-mode`, `--host-tool`, `--host-agent`, and `--isolation-level`; keep the returned dispatch ID.
3. Ask the host AI tool to perform the dispatch. Tenetora CLI and hooks do not spawn it. Never report rendering or state recording as dispatch execution.
4. If the host cannot dispatch or the carrier cannot satisfy the declared boundary, run `tenetora delegation --unavailable --dispatch-id <id> --reason "<bounded reason>"`, perform main-agent self-review, and label the result `未经独立审查`. Do not retry indefinitely.
5. Record a pass with `tenetora delegation --review-result --dispatch-id <id> --result passed --report-file <project-relative-report>`.
6. For a blocker, record `--result blocker --blocker "<stable summary>"`. When the cycle enters `needs-fix`, call `tenetora delegation --fix-start --dispatch-id <review-id>` with qualified resolution metadata, then let the host dispatch an `implementer` constrained to the approved task and reviewer finding. `implementer` rejects `prompt-only`.
7. Record the repair with `--fix-complete`, then start the next review round. Never use `implementer` outside this active authorized review cycle.
8. Stop after three review rounds, after two repair rounds, or when the same blocker repeats. A third review blocker is final and must not trigger another automatic repair.

Temporary full reports live only under ignored `.tenetora/.cache/subagents/`; goal completion/change or active overflow moves them into its `archive/`, where the seven-day retention starts at archive time. Governance trail events keep compact metadata only. Independent roles may run in parallel when their scopes do not overlap, but the commit repair cycle remains ordered review/fix/review.

## Bounded Loop Flow

Repeat only while there is measurable progress and no approval blocker:

1. Reload: read the current `.tenetora` entry points needed for this task, then use `tenetora rules --context <context>` for the task-specific rule slice.
2. Snapshot: inspect git status, owner-bound `.tenetora/state/loop-state.json`, relevant state files, latest evidence pointer, and latest verification result. Stop on `status: conflict` and select or create the current owner-bound session.
3. Plan: choose one small step with an expected result. When End-to-End Skeleton First is applicable and its gate has not passed, choose a skeleton-enabling step; defer non-blocking component refinement.
4. Execute: make the smallest safe change or run the narrowest useful command.
5. Verify: first use `--check-proof-only` when the step only advances an already verified Git authorization boundary; otherwise run the targeted check, `tenetora run-all`, or project verification command that matches the step. For parallel module work, prefer a scoped partial claim for the module that changed.
6. Decide: continue only if the result gives a concrete next step; otherwise stop and report.
7. Persist: if the step changes durable rules, workflows, architecture facts, or tool entrypoints, call `tenetora-update`.
8. Record: update `.tenetora/state/loop-state.json` with status, latest step, latest verification, blocker, and next prompt when useful. Use `--claim-kind partial-verification` for an intermediate check, and use `--claim-kind completion` only with the full verification command before claiming completion. Include the returned claim proof in the report.
9. Accept: when done, call `tenetora-audit` if the user asked for readiness or the `.tenetora` layer changed.

## Alignment Handoff

For high-risk work, the loop does not conduct the decision interview. Before activating the loop:

1. Run `tenetora guard --action alignment --goal "<goal>" --risk-level high`.
2. Stop if the guard reports an active, missing, invalid, or stale alignment.
3. Pass the returned proof to `tenetora loop-state --write --session-id "<session-id>" --owner-id "<owner-id>" --status active --goal "<goal>" --alignment-proof "<ah-align-proof>"`.
4. Consume only a goal-matching `confirmed` or `accepted-with-risks` handoff. Keep accepted risks visible in loop state and verification reports.

Alignment proof records decision alignment only. It does not authorize implementation, commit, push, deployment, or release, and it never starts the loop in the background.

## Stop Conditions

Stop and report when any condition is true:

- The user goal is met.
- The next step requires user approval.
- The same blocker appears twice without new evidence.
- Verification is passing and no concrete improvement remains.
- The loop would require guessing product intent, credentials, private local settings, or destructive cleanup.
- Continuing would mean broad refactoring unrelated to the requested goal.

## Safety Rules

- Do not commit or push unless the user explicitly asks.
- Do not run destructive commands without explicit approval.
- Do not move secrets, tokens, private keys, or tool-private local configs into shared `.tenetora` content.
- Do not treat stale `.tenetora/state/*` as ground truth; verify against source files, current evidence, and command output.
- Do not hide failed checks. Report command failures, skipped checks, and residual risk.
- Do not describe a hook or CLI state transition as an actual subagent dispatch. The platform/model owns dispatch execution.
- Do not invent a `skeleton_passed` loop-state field. Consume the active plan and verification evidence until a future schema explicitly adds such state.

## Reporting

When the loop stops, report:

- What changed or what was verified.
- Which checks ran and whether they passed.
- Whether `.tenetora` was updated, audited, or left unchanged.
- For applicable cross-boundary work, whether the skeleton gate passed and which refinement remains deferred; for exempt work, the recorded exemption reason.
- The next prompt the user can give if another bounded loop should continue.
