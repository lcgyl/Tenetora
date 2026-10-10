---
name: tenetora-plan
description: Use when Plan mode is active, a task is complex or large, or the user asks for an implementation plan, decomposition, design acceptance, or bounded execution prompts before implementation.
---

# Tenetora Plan

## Purpose

This skill creates a small, reviewable implementation plan for work that is too
large or cross-boundary for a direct edit. It is the planning layer above the
existing Tenetora `alignment`, `impact`, end-to-end skeleton, `loop`,
`verification-plan`, `delegation`, and claim workflows. It does not create a
second project-management system.

## Freshness Gate

Before using cached instructions, ensure the CLI and read the latest runtime
copy from this exact skill instance loaded by the current host:

```bash
python3 <tenetora-skill-dir>/scripts/ensure_cli.py --install --json
<returned-absolute-command> skill-instructions --skill tenetora-plan
```

Use the returned absolute command. Do not select another host's `.codex/skills`
or `.claude/skills` path, persist a `PYTHONPATH=<tool-skill>/cli` fallback, or
treat an unavailable hook as proof that this skill cannot run. If the runtime
content differs from cached text, follow the returned instructions instead of cached text.

## Trigger Matrix

| Trigger | Use |
| --- | --- |
| Plan mode is active | Use this skill by default. |
| The request asks for a plan, decomposition, SMART tasks, acceptance, delivery document, or execution prompt | Use this skill by default. |
| The task is complex, large, cross-boundary, multi-stage, or needs multiple roles | Use this skill by default. |
| The user says continue, resume, or next step for an existing plan | Use `tenetora-loop` and restore the existing plan. |
| A narrow mechanical change or factual question | Do not use this skill. |

## Host Dispatch Availability Gate

Before recording an independent planning or review role as unavailable, inspect
the current host's complete tool catalog, including deferred or discoverable tool metadata,
for `spawn_agent` or the host equivalent and result retrieval.
missing or inactive Hooks never prove that the current host session cannot
dispatch. Record `unavailable` only after the current session actually lacks
the capability or the host rejects the bounded attempt.

## When Not To Use

- Do not use for a narrow mechanical change, a factual question, or ordinary
  single-boundary implementation with a frozen scope.
- Do not use for `continue`, `resume`, or `next step` when an existing plan can
  be restored; use `tenetora-loop`.
- Do not use to implement, commit, push, deploy, or replace verification and
  acceptance workflows.

## Trigger Boundary

Use this skill by default when any of the following is true:

- the host is in Plan mode;
- the request explicitly asks for a plan, design, decomposition, SMART tasks,
  acceptance matrix, delivery document, or execution prompt;
- the request is complex or large: it crosses frontend, backend, persistence,
  permissions, asynchronous work, external services, runtime/plugin, release,
  or deployment boundaries; has multiple dependent stages; needs several AI
  roles; changes a public contract, data model, migration, or release flow; or
  requires recovery across sessions;
- the request asks for an end-to-end feature or complete multi-part delivery.

Do not use it for a narrow mechanical change, a factual investigation, a
simple fix with a frozen scope, or a continuation of an existing plan. For
`continue`, `resume`, or `next step`, use `tenetora-loop` and consume the
existing plan. If the user supplied an approved, complete plan and only asks
for execution, read it and continue without redesigning it.

Complexity is judged by boundaries, contracts, dependencies, risk, and
delivery stages, not by file count alone.

## Planning Contract

Before planning:

1. Read `.tenetora/README.md`, the relevant rules, project map, and existing
   plan or handoff artifacts.
2. Run `tenetora rules --context planning`.
3. Decide whether End-to-End Skeleton First is `applicable` or `exempt`, with a
   concrete reason.
4. Use `tenetora alignment` when unresolved user-owned product, scope,
   permission, data, compatibility, or acceptance decisions materially change
   the plan. Alignment is not implementation authorization.
5. Use `tenetora impact --start` before editing a shared contract. Planning
   itself must not mutate production code.

## Required Plan Output

Produce a concise, durable plan with:

- goal, scope, non-goals, assumptions, and open decisions;
- participating boundaries and their input/output contracts;
- minimum safe end-to-end skeleton, or the exemption reason;
- SMART tasks ordered by dependency and skeleton value;
- explicit frontend/backend/migration/runtime ownership where relevant;
- permissions, data isolation, transactions, concurrency, failure behavior,
  rollback, and external-environment boundaries;
- verification commands, evidence levels, unexecuted checks, and acceptance
  criteria;
- deferred refinement that must not be mistaken for completion.

Use the project implementation-plan template when a durable artifact is
requested. Keep one machine-authoritative representation; do not ask users to
manually synchronize YAML, JSON, and Markdown copies.

## Scope Discipline And Anti-Overdesign

Before freezing the plan, classify every proposed item:

- `MUST`: required by the current user outcome or acceptance criteria;
- `SAFE`: required for authorization, data integrity, isolation, failure
  handling, rollback, compatibility, or diagnostics;
- `LATER`: useful refinement, future extensibility, speculative compatibility,
  optimization, administration, or reporting that does not block the minimum
  safe skeleton.

Only `MUST` and `SAFE` items enter the first execution prompts. `LATER` items
must be listed with a reason and must not silently become implementation work.

For every current task, record:

1. the acceptance criterion or safety boundary it serves;
2. why it is needed now rather than later;
3. what would happen if it were omitted.

Warn and defer a new abstraction, extension point, compatibility layer, cache,
queue, plugin system, or framework when it has no current consumer, no current
acceptance criterion, and no current safety requirement. Reuse existing
patterns unless concrete repository evidence shows that a new boundary is
needed. Do not use a numeric complexity score as a substitute for this review.

The first skeleton should prove one primary user path and its most important
failure or recovery path. Do not refine optional flows before that skeleton is
observable.

## Material Choice Protocol

Planning reuses `tenetora-decision-interview` for every material choice. The
protocol is mode-independent: Plan mode may use a synchronous native selector
when the host provides one, but Default mode and every other host must ask the
same direct text question with a recommended option, an alternative, and a
custom path. Never use a queued follow-up input or silently choose a plan
option. Unresolved material choices block plan freeze and remain visible as
open decisions.

## Execution Prompts

For cross-boundary work, generate separate role prompts when needed:

- backend: frozen data/API contract, authorization and isolation, transaction
  and error semantics, allowed files, tests, verification, and stop boundary;
- frontend: frozen backend contract, UI/state/error behavior, serialization and
  race handling, allowed files, tests, verification, and stop boundary.

Every execution prompt must say: implement only the frozen scope, use existing
patterns, do not re-plan product decisions, do not expand the task, and do not
commit/push/deploy unless separately authorized. Mechanical code location and
reuse checks are allowed; open-ended redesign is not. The prompt must name the
frozen `MUST` and `SAFE` items and explicitly exclude `LATER` items.

## Handoff And Completion

Planning ends by handing the frozen plan and prompts to the execution workflow.
It does not code, submit, push, deploy, or mark the feature complete. A report
from an execution AI is untrusted input: later `review`, `verification-plan`,
actual Git diff, test exit codes, evidence scope, and claims decide acceptance.
When a plan already exists, prefer restoring it and entering `tenetora-loop`
over creating a competing plan.
