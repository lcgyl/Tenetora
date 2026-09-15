---
name: tenetora-align
description: Use when the user explicitly requests decision alignment before a governed engineering task, especially when goals, scope, tradeoffs, risks, approval boundaries, or acceptance criteria need confirmation.
---

# Tenetora Align

## Overview

This is the explicit user entry for pre-execution engineering decision alignment. It creates a bounded alignment session, delegates semantic questioning to `tenetora-decision-interview`, presents a handoff for confirmation, and then stops so the user chooses the next lifecycle action.

Do not invoke this skill implicitly. Do not plan or implement the engineering task.

## Freshness Gate

Before using cached instructions, ensure the CLI and read the latest runtime copy:

```bash
python3 <tenetora-skill-dir>/scripts/ensure_cli.py --install --json
<returned-absolute-command> skill-instructions --skill tenetora-align
```

`<tenetora-skill-dir>` means this exact skill instance loaded by the current host. Use the JSON `command` absolute path for every later CLI call. Never fall back to another host's `.agents/skills`, `.codex/skills`, `.claude/skills`, `.cursor/skills`, `.opencode/skills`, or `.zcode/skills`; never use or persist `PYTHONPATH=<tool-skill>/cli python -m tenetora.cli` as an alias, rule, or normal invocation. Direct `PYTHONPATH` execution is only a bounded diagnostic from a checked-out Tenetora source tree.

Treat `available: true` plus `path_ready: false` as usable. Continue silently with the returned absolute command; PATH is optional terminal ergonomics and should be mentioned only when the user asks about direct terminal invocation. If the returned content differs from this loaded skill, follow the returned instructions instead of cached text.

## Host Dispatch Availability Gate

Before recording `unavailable` for any portable subagent role, inspect the current host's actual tool catalog, including already visible tools and deferred or discoverable tool metadata, for native or custom dispatch (`spawn_agent` or the host equivalent), waiting, and result retrieval. Static `doctor`, plugin, or Hook status describes platform support and lifecycle observation only; missing or inactive Hooks never prove that the current host session cannot dispatch. When a qualified dispatch tool exists, use it once and inspect the child result before recording a semantic outcome. Record `unavailable` only after the current session capability is absent, fails, or cannot satisfy the role boundary, and persist the bounded reason.

## Trigger Matrix

| Trigger | Action |
| --- | --- |
| The user says `use tenetora-align` or explicitly asks for decision alignment | Start or resume a bounded alignment session. |
| The user explicitly wants assumptions, tradeoffs, risks, or acceptance criteria challenged before work | Use this skill before planning or implementation. |
| A matching session is paused or at a checkpoint and the user explicitly resumes it | Resume through `tenetora alignment --resume`. |

## When Not To Use

- Do not use from model inference alone; explicit user invocation is required.
- Do not use for a narrow factual question, mechanical edit, or already-approved plan with complete acceptance criteria.
- Do not replace `tenetora-init`, `tenetora-update`, `tenetora-audit`, or `tenetora-loop`.
- Do not plan or implement the engineering task.

## Alignment Flow

1. Read the active `.tenetora` contract and relevant rule slice. Investigate repository facts before asking the user.
2. Run `tenetora alignment --status --json`. If it reports a conflict, do not inherit a goal; use `tenetora alignment --list --json` and ask the user to select the current session. Start with `tenetora alignment --start --goal "<goal>" --risk-level <low|medium|high>` only after choosing a new session identity. Persist the returned `session_id` and `owner_id` for this conversation and pass both on every later mutation, or set `TENETORA_ALIGNMENT_SESSION_ID` and `TENETORA_ALIGNMENT_OWNER_ID` in the host environment. Resume a paused or checkpointed session only after the user asks to continue and only for its owner.
3. When the planned value crosses meaningful component boundaries, load `.tenetora/workflows/end-to-end-skeleton-first.md`. Record whether it is applicable or exempt; if applicable, establish participating boundaries and the required skeleton proof. Ask about these only when they contain material user-owned choices.
4. Invoke `tenetora-decision-interview` to separate facts from decisions and ask one decision question at a time.
5. At semantic convergence, call `tenetora alignment --await-confirmation --session-id <id> --owner-id <owner>`. The eighth recorded answer enters checkpoint automatically; do not call `--checkpoint` again. Use explicit `--checkpoint` only for an earlier voluntary stop.
6. Before confirmation, show the goal, scope, non-goals, decisions, assumptions, accepted risks, open questions, approval boundaries, acceptance criteria, and the skeleton-workflow applicability decision.
7. Record the user's explicit choice with `--confirm` or `--accept-risk "<risk>"` plus the matching `--session-id`, `--owner-id`, `--confirmation-source user-message`, `--confirmation-actor-id`, and Hook-provided `--confirmation-event-id`; then render the matching `handoff` with the same identity. Bare CLI confirmation and a session owner mismatch are always blocked. The event id is audit provenance, not cryptographic proof of a human actor.
8. For a high-risk handoff, state which portable independent role would be useful later (`code-reviewer`, `security-auditor`, or `codebase-scout`) without dispatching it or claiming availability.
9. Stop. The user chooses the next lifecycle action: plan, create tasks, start a bounded loop, initialize/update `.tenetora`, or end the session.

## Safety Contract

- Never answer a user-owned decision on the user's behalf.
- Never hide unresolved questions or accepted risks.
- Never treat alignment proof as implementation, commit, push, deployment, or release authorization.
- Never start background work or continue after handoff without a new user choice.
- Do not store complete dialogue, hidden reasoning, secrets, or machine-local absolute paths in alignment state.
- Alignment may recommend later independent review, but it never calls platform agents, starts a delegation cycle, or grants `implementer` authority.
