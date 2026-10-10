---
name: tenetora-decision-interview
description: Use when any governed task needs a bounded decision alignment interview because material ambiguity, risk, conflicting constraints, or unresolved user-owned choices remain before planning, execution, or review.
---

# Tenetora Decision Interview

## Overview

This skill is the model-callable semantic primitive used by Tenetora lifecycle skills and by ordinary governed work in any host mode. It separates facts from decisions, investigates what the repository can answer, asks one question at a time, explains recommendations transparently, detects conflicts, and stops at convergence or checkpoint.

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
| Router, init, update, loop, Plan, review, test, release, or ordinary implementation detects a material choice | Use this primitive within the current lifecycle; do not require Plan mode. |
| The host exposes a synchronous native question selector in the current mode | Use it as an optional presentation enhancement. |
| Repository facts conflict with a proposed decision or acceptance criterion | Surface the conflict and ask the user to resolve the decision. |
| The current batch reaches 8 questions | Stop at checkpoint; do not continue automatically. |

## When Not To Use

- Do not use for general consultation, product discovery, or unrelated business implementation.
- Do not ask the user for facts that can be established from the repository, environment, or existing `.tenetora` evidence.
- Do not continue when there is no material unresolved decision.
- Do not plan or implement the engineering task.

## Session Gate

Ordinary material-choice questions do not require an alignment session. Use the
current task's existing authorization, ask directly, and record the concise
decision in the task's plan or handoff. Do not implicitly invoke `tenetora-align`
or ask for permission again merely to present a question.

For a tracked alignment interview, run `tenetora alignment --status --json` with the current conversation's `--session-id` and `--owner-id` (or the corresponding `TENETORA_ALIGNMENT_*` environment variables). An unscoped `conflict` result is not a usable session.

- Continue only when a governed alignment session is active and the current user message authorizes the next question or resume action.
- Never inherit an unscoped active or legacy session. Use `tenetora alignment --list --json` to show candidates and require an explicit owner/session choice.
- At `checkpoint`, `paused`, `blocked`, or `awaiting-confirmation`, wait for the user's explicit choice before changing state.
- If no active session exists, do not mutate alignment state. Return to the lifecycle caller; ordinary material-choice questions remain available without creating an alignment.

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

## Mode-Independent Question Delivery

Material choices are not a Plan-only concern. They can appear during coding,
testing, review, release, deployment, or troubleshooting. The model must ask
the question whenever the choice is material, regardless of the current host
mode.

Use this transport order:

1. If the current host explicitly exposes a synchronous native question
   selector, use it. The selector is optional; never change modes merely to
   obtain it.
2. Otherwise ask directly in the current assistant message using the text
   template below and stop execution until the user answers.

Do **not** use an asynchronous or queued follow-up input as the fallback. It
is easy to miss in a terminal and does not provide a reliable cross-host
choice interaction. Do not start a blocking `select`, `fzf`, `curses`, or
custom terminal subprocess either: its answer cannot be safely bound back to
the model conversation across hosts and it would introduce an unnecessary
runtime dependency.

The direct text form is the terminal-compatible fallback and must always offer
the recommended option, at least one alternative, and a custom path:

```text
Decision: <what changes and why it matters>
Option A (Recommended): <proposal>
  Reason: <evidence and expected outcome>
  Tradeoff: <cost or risk>
Option B: <proposal and tradeoff>
Custom option: provide another proposal or modify an option.
Please reply with A, B, or a custom proposal. Work is paused until you choose.
```

The Plan-mode material-choice protocol and any `grill-me` integration must
reuse this contract rather than defining a second selector or silently making
the choice.

## Plan Integration

When `tenetora-plan` finds multiple materially different options, this
interview is the decision boundary for the plan. The plan supplies repository
facts and the option set; the interview supplies one question at a time. Keep
the model recommendation first, label its confidence, explain its assumptions
and tradeoffs, list the other viable options, and always offer a custom answer
path. Never turn the recommendation into a default user decision. After the
user answers, return only the decision summary and remaining open questions to
the plan; the plan then reclassifies `MUST`, `SAFE`, and `LATER` items before
freezing execution prompts.

## Convergence And Checkpoint

- Stop asking when goal, scope, non-goals, material tradeoffs, risks, approval boundaries, rollback expectations, acceptance criteria, and any applicable skeleton boundaries/proof are sufficiently determined for the task's risk level.
- Ask no more than 8 questions in one batch.
- In a tracked alignment interview, the eighth recorded answer automatically enters `checkpoint`; do not issue a second `--checkpoint` command. Stop and let the user choose whether to continue, pause, move to `--await-confirmation`, or abandon. Ordinary questions use a conversational checkpoint without mutating alignment state.
- At semantic convergence of a tracked interview before the eighth question, use `tenetora alignment --await-confirmation`; use explicit `--checkpoint` only for an earlier voluntary stop.
- If new evidence invalidates an earlier answer, explain the conflict and reopen only the affected decision.
- At convergence, prepare the handoff summary and return to the lifecycle caller (or `tenetora-align` for a tracked interview); do not choose the next lifecycle action.

## Safety Contract

- Do not answer decisions for the user.
- Do not manufacture consensus or silently accept risk.
- Do not use the interview as authorization for implementation, commit, push, deployment, release, or destructive action.
- Do not start a background loop or continue after checkpoint without a user response.
