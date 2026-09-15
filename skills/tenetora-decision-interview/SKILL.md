---
name: tenetora-decision-interview
description: Use when a governed engineering task needs a bounded decision alignment interview because material ambiguity, risk, conflicting constraints, or unresolved user-owned choices remain before planning or execution.
---

# Tenetora Decision Interview

## Overview

This skill is the model-callable semantic primitive used by Tenetora lifecycle skills. It separates facts from decisions, investigates what the repository can answer, asks one question at a time, explains recommendations transparently, detects conflicts, and stops at convergence or checkpoint.

Do not answer decisions for the user. Do not plan or implement the engineering task.

## Freshness Gate

Before using cached instructions, ensure the CLI and read the latest runtime copy:

```bash
python3 <tenetora-skill-dir>/scripts/ensure_cli.py --install --json
<returned-absolute-command> skill-instructions --skill tenetora-decision-interview
```

`<tenetora-skill-dir>` means this exact skill instance loaded by the current host. Use the JSON `command` absolute path for every later CLI call. Never fall back to another host's `.agents/skills`, `.codex/skills`, `.claude/skills`, `.cursor/skills`, `.opencode/skills`, or `.zcode/skills`; never use or persist `PYTHONPATH=<tool-skill>/cli python -m tenetora.cli` as an alias, rule, or normal invocation. Direct `PYTHONPATH` execution is only a bounded diagnostic from a checked-out Tenetora source tree.

Treat `available: true` plus `path_ready: false` as usable. Continue silently with the returned absolute command; PATH is optional terminal ergonomics and should be mentioned only when the user asks about direct terminal invocation. If the returned content differs from this loaded skill, follow the returned instructions instead of cached text.

## Host Dispatch Availability Gate

Before recording `unavailable` for any portable subagent role, inspect the current host's actual tool catalog, including already visible tools and deferred or discoverable tool metadata, for native or custom dispatch (`spawn_agent` or the host equivalent), waiting, and result retrieval. Static `doctor`, plugin, or Hook status describes platform support and lifecycle observation only; missing or inactive Hooks never prove that the current host session cannot dispatch. When a qualified dispatch tool exists, use it once and inspect the child result before recording a semantic outcome. Record `unavailable` only after the current session capability is absent, fails, or cannot satisfy the role boundary, and persist the bounded reason.

## Trigger Matrix

| Trigger | Action |
| --- | --- |
| `tenetora-align` has an active session with unresolved material decisions | Continue the bounded interview. |
| Router, init, update, or loop detects high risk plus material ambiguity | Recommend or invoke this primitive only within an explicit governed lifecycle. |
| Repository facts conflict with a proposed decision or acceptance criterion | Surface the conflict and ask the user to resolve the decision. |
| The current batch reaches 8 questions | Stop at checkpoint; do not continue automatically. |

## When Not To Use

- Do not use for general consultation, product discovery, or unrelated business implementation.
- Do not ask the user for facts that can be established from the repository, environment, or existing `.tenetora` evidence.
- Do not continue when there is no material unresolved decision.
- Do not plan or implement the engineering task.

## Session Gate

Before asking a decision question, run `tenetora alignment --status --json` with the current conversation's `--session-id` and `--owner-id` (or the corresponding `TENETORA_ALIGNMENT_*` environment variables). An unscoped `conflict` result is not a usable session.

- Continue only when a governed alignment session is active and the current user message authorizes the next question or resume action.
- Never inherit an unscoped active or legacy session. Use `tenetora alignment --list --json` to show candidates and require an explicit owner/session choice.
- At `checkpoint`, `paused`, `blocked`, or `awaiting-confirmation`, wait for the user's explicit choice before changing state.
- If no active session exists, return control to `tenetora-align`; do not conduct an untracked interview.

## Facts And Decisions

Separate facts from decisions before each question:

- **Fact:** investigate repository facts, current configuration, existing rules, code structure, and verifiable environment state directly.
- **Decision:** ask only when the answer changes scope, architecture, risk acceptance, approval boundaries, rollback, or acceptance criteria and cannot be inferred safely.
- **Assumption:** state it explicitly and either verify it as a fact or ask the user to decide whether to accept it.

For a cross-boundary delivery, use `.tenetora/workflows/end-to-end-skeleton-first.md` to identify repository facts about participating components and possible skeleton verification. Ask the user only about unresolved ownership, contract, safety, rollback, or acceptance choices. A task touching several files or directories is not automatically a cross-boundary delivery.

Never turn a discoverable fact into a user questionnaire item.

## Single-Question Protocol

Ask one question at a time. Each decision question must include:

1. The decision and why it blocks or materially changes the task.
2. Mutually exclusive options, with the recommendation first.
3. The evidence or reasoning behind the recommendation.
4. The assumptions that recommendation depends on.
5. A confidence level.
6. The alternative cost or tradeoff for each non-recommended option.
7. A custom answer path so the user is not forced into the listed options.

After the answer, summarize only the resulting decision and remaining uncertainty. Do not store the full conversation or hidden reasoning.

## Convergence And Checkpoint

- Stop asking when goal, scope, non-goals, material tradeoffs, risks, approval boundaries, rollback expectations, acceptance criteria, and any applicable skeleton boundaries/proof are sufficiently determined for the task's risk level.
- Ask no more than 8 questions in one batch.
- The eighth recorded answer automatically enters `checkpoint`; do not issue a second `--checkpoint` command. Stop and let the user choose whether to continue, pause, move to `--await-confirmation`, or abandon.
- At semantic convergence before the eighth question, use `tenetora alignment --await-confirmation`; use explicit `--checkpoint` only for an earlier voluntary stop.
- If new evidence invalidates an earlier answer, explain the conflict and reopen only the affected decision.
- At convergence, prepare the handoff summary and return control to `tenetora-align`; do not choose the next lifecycle action.

## Safety Contract

- Do not answer decisions for the user.
- Do not manufacture consensus or silently accept risk.
- Do not use the interview as authorization for implementation, commit, push, deployment, release, or destructive action.
- Do not start a background loop or continue after checkpoint without a user response.
