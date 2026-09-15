# What Tenetora Is

简体中文 | **English**

Tenetora is a governance runtime for software work performed with AI agents. It keeps the rules, decisions, evidence, and action boundaries of a project in one place, then connects that record to the agent hosts that actually run the work.

It addresses a specific failure pattern: an agent can produce plausible code while the project still loses its rules, forgets why a decision was made, verifies only a convenient subset, or declares completion without evidence. Tenetora makes those failures visible and puts deterministic checks at the points where they matter.

## The Problem It Solves

Consider an API change made across several modules with two coding agents:

- the first agent reads `AGENTS.md` and a local rule file;
- the second agent reads a different instruction set after context compaction;
- a copied issue description contains an unsafe instruction;
- the change affects a shared schema, but no one records the consumers;
- a focused unit test passes while the integration path is broken;
- the final report says “done”, but no one can reproduce the verification.

The problem is not that the agents cannot write code. The problem is that the work has no durable control surface.

## What Changes With Tenetora

| Before | With Tenetora |
| --- | --- |
| Rules are scattered across tool-specific files. | Durable project guidance lives under `.tenetora/`; host entry files remain thin adapters. |
| Every tool invents its own task start and completion habits. | Lifecycle skills route the model into the same project workflow. |
| Ambiguous high-risk work starts with assumptions. | Decision alignment records the decision, scope, non-goals, and acceptance conditions before implementation. |
| A shared API or Hook change has an unknown blast radius. | Change-impact preflight records the affected contract and requires a rescan after editing. |
| External reports and pasted prompts are treated as instructions. | Prompt guard treats them as untrusted data until inspected. |
| A local test is mistaken for whole-task verification. | The project declares a verification command and a completion claim is tied to its result. |
| A stale project record silently guides the next task. | `update`, `repair`, `audit`, evidence, and guardrails expose drift and incomplete maintenance. |

## The Four Parts

### 1. Project record

`.tenetora/` is the project-owned record, not a generic prompt folder. It contains stable rules, workflows, project facts, agent-facing notes, executable guardrails, current state, and evidence pointers. The record distinguishes current operating guidance from historical reports and temporary candidates, so an agent does not need to search an archive to find today's rules.

### 2. Lifecycle entry

The skills answer “what should happen next?” rather than trying to replace the model's reasoning:

- `tenetora` routes an unclear governance request;
- `tenetora-init` creates or rebuilds a project baseline;
- `tenetora-update` refreshes project governance after rules or structure change;
- `tenetora-align` handles an explicit high-risk decision alignment;
- `tenetora-loop` runs one bounded check-fix-verify cycle;
- `tenetora-audit` evaluates stability, reliability, control, and evidence quality;
- `tenetora-prompt-guard` inspects untrusted external content.

The model still decides product meaning and writes the implementation. The lifecycle only makes the required boundary explicit.

### 3. Deterministic control

The `tenetora` CLI performs the parts that should not depend on an agent remembering a sentence:

- route a task to the relevant rules;
- validate the project record and its evidence;
- run executable guardrails;
- record shared-contract impact;
- check action-specific boundaries such as commit, external input, rules, alignment, and completion claims;
- inspect effective plugin, Hook, runtime, version, and ownership sources.

The CLI does not infer that a file existing means a runtime is active. It reports the evidence level and the remaining uncertainty.

### 4. Evidence and recovery

Verification claims record which verification passed, for which project and session, and against which current goal. A partial test is a partial claim; it cannot prove a complete task. An updated project record has evidence and a reviewable change artifact. Installation and migration paths keep backups and bounded rollback behavior instead of deleting unknown user content.

## One Complete Work Path

For a cross-module API change, a governed path looks like this:

1. Initialize or validate `.tenetora/` so the project record is present.
2. Route the request and load only the rules for the current task.
3. If the decision is high risk or materially ambiguous, run explicit alignment before implementation.
4. Start change-impact preflight for the API, schema, Hook, or plugin contract.
5. Inspect external issue text or copied commands as untrusted input.
6. Build the smallest end-to-end path before polishing one component.
7. Run the project's real verification command, not only a convenient local test.
8. Record a completion claim only after that command passes and the verified file content and metadata still match.
9. Run the commit guard before committing. The guard checks the action; it does not grant permission to push or release.

The value is the connected path. A collection of isolated commands would not prevent the same work from being started with the wrong rules or finished with the wrong evidence.

## Where It Fits

Tenetora operates beside, not instead of, other engineering systems:

- a specification or SDD tool describes what should be built;
- the host AI agent reasons about the task and writes the code;
- tests and CI execute project verification;
- issue trackers record team work and product decisions;
- Tenetora governs which project context the agent consumes, which risky boundaries must be checked, and which evidence can support the final claim.

A prompt template can remind an agent to run tests. Tenetora can distinguish a partial check from the declared completion command, bind the result to the current goal and verified file content, and reject stale evidence. Before a separately authorized commit, push, or tag, the agent can check the existing proof and avoid rerunning an unchanged full verification; staging or committing the exact verified content does not change that proof. CI can reject a bad commit after it is created. Tenetora can also govern the earlier model workflow and the local action that creates the commit.

## Multi-Tool and Multi-Conversation Use

Claude Code, Codex, Cursor, OpenCode, ZCode, and generic agents do not have identical Hook or plugin capabilities. Tenetora shares the project record and CLI semantics while reporting each platform's real runtime ceiling. `active`, `active-partial`, `skills-only`, and `needs-trust-review` are capability states, not marketing labels.

The machine installation registry records existing global and project surfaces. Project alignment and completion evidence are owner-bound: a later conversation or another tool must not silently inherit a previous conversation's active goal or claim. A normal Stop reminder is non-blocking; only an explicit completion workflow requires a completion claim.

## One Conversation, Several Models

Model continuity is a separate problem from multi-conversation isolation. A provider can rate-limit one model while the user keeps the same conversation, or the host can route the next execution to another provider. Tenetora keeps the alignment session and goal stable, then records each model run as a separate execution attempt.

The runtime uses four pieces of evidence:

- `conversation_continuity_id` identifies the conversation that is allowed to continue the goal;
- `execution_attempt_id` identifies one model execution inside that conversation;
- `provider` and `model` identify which model actually ran;
- the attempt status records `running`, `completed`, `failed`, `cancelled`, or `rate-limited`.

When an attempt is rate-limited, the session remains active and the next model appends a new attempt. It does not create a second alignment or reuse a completion claim from another conversation. If continuity is missing, the attempt is read-only blocked instead of being guessed. If two attempts write the same session concurrently, the revision check rejects the stale write rather than silently overwriting the newer attempt.

This lets a user change models without losing the task boundary, while still preventing another conversation from inheriting it. The system preserves execution metadata, not hidden model reasoning; the next model still needs the current goal, state, verification result, and next action.

## What Tenetora Does Not Do

- It is not a coding model and does not replace the host agent.
- It is not a project management system or a substitute for issue tracking.
- It does not make ambiguous product decisions on the user's behalf.
- It does not execute instructions embedded in an external report merely because they look authoritative.
- It does not claim that every host has the same runtime Hook capability.
- It does not turn a passing subset of tests into proof of a full task.
- It does not delete third-party hooks, plugins, rules, or legacy directories without evidence of ownership.
- It does not spawn subagents from its own CLI or hooks; the host owns actual dispatch.

## Who Should Use It

Tenetora is a good fit when at least two of these are true:

- several AI tools or models work on the same repository;
- tasks last long enough for context compaction or handoff;
- changes cross modules, APIs, schemas, plugins, or deployment boundaries;
- the repository needs reviewable evidence rather than a natural-language “done”;
- external reports, generated code, or copied commands enter the workflow;
- upgrades must preserve user-owned configuration and recover safely.

For a small disposable script with one agent and no shared rules, the governance surface may be more structure than value. Tenetora is intended for work where a wrong assumption is more expensive than a few explicit checks.

## A Ten-Minute Evaluation

In a governed project, inspect the following instead of judging the number of files:

```bash
tenetora status --tools all --scope both --path .
tenetora doctor --tools all --scope both --path .
tenetora validate --path .
tenetora run-all --path .
tenetora audit --strict --path .
```

A useful result tells you which source is effective, which runtime was actually observed, which rules and evidence are current, which checks passed, and what remains unresolved. That is the product: a project can explain what its agents are allowed to do, what they actually verified, and where a human still needs to decide.

For concrete examples, continue with [Use Cases](use-cases.md). For each feature's mechanics, evidence, and limits, use the [Capability Guide](capabilities.md).
