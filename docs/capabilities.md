# Tenetora Capability Guide

简体中文 | **English**

This guide explains the public capabilities individually. It is organized by engineering responsibility, not by internal source file. For the complete product model, start with [What Tenetora Is](product.md); for concrete situations, see [Use Cases](use-cases.md).

## Project Record and Initialization

**Problem:** tool-specific instruction files describe only fragments of the project, and a new agent cannot tell current rules from historical notes.

**How it works:** `tenetora-init` extracts existing instructions and project evidence into `.tenetora/`, creates a manifest, separates stable rules from project facts and state, and leaves tool entry files as thin adapters. Existing projects use extract mode; empty projects use scaffold mode. When independent Git submodules are detected, initialization and update ask for repository scope first; `--repository-scope parent|all|<submodule-path>` makes the choice explicit, and parent evidence never scans child repository source.

**Observable result:** `tenetora validate --path .` can resolve the runtime contract, required structure, source evidence, and tool entry points.

**Boundary:** initialization does not install plugins, decide product policy, or treat inferred architecture as confirmed fact. Unknowns remain explicit.

## Project-Opt-In Governance

**Problem:** a globally installed Tenetora Hook must not impose project rules on a scratch workspace used for conversation, notes, or ordinary documents.

**How it works:** the global installation provides the Hook runtime, but project governance is opt-in. A Git worktree is governed only by its own root `.tenetora/`; the global machine home `~/.tenetora/` is never treated as a project harness. A legacy `.harness/` remains eligible for a migration reminder. Users can explicitly stop onboarding reminders with `tenetora onboarding --decline` for an uninitialized project or `--dismiss` for unresolved legacy governance; this records only bounded machine-local state and never deletes project files.

**Observable result:** a workspace without `.tenetora/` or `.harness/` receives no route/rules context, onboarding reminder, commit gate, external-input gate, or state write from the Hook. Directories in one Git worktree use its root harness; entering a submodule or independent nested repository starts a new governance boundary and never inherits parent rules.

**Boundary:** installing Tenetora globally does not initialize projects or weaken governance inside an explicitly initialized project. Parent governance owns gitlinks, cross-repository impact, and aggregate evidence; each governed submodule owns its code, tests, secrets, release rules, and local state through its own `.tenetora/`.

## Context Routing and Rule Loading

**Problem:** loading every rule wastes context, while loading none makes the agent rely on memory.

**How it works:** `tenetora route --message "<request>"` suggests relevant contexts and guards. `tenetora rules --context <context>` returns the task-specific rule slice and records its consumption.

**Observable result:** the agent can name the rules used for build, test, docs, security, planning, or commit work; governance history shows that the context was loaded.

**Boundary:** routing is heuristic guidance. It does not understand product intent well enough to authorize a risky action or replace a user decision.

## Decision Alignment

**Problem:** high-risk implementation begins while compatibility, migration, rollback, scope, or acceptance decisions are unresolved.

**How it works:** the user explicitly invokes `tenetora-align`. A bounded interview asks one material question at a time and records goal, decisions, non-goals, risks, and acceptance criteria in an owner-bound session.

**Observable result:** planning consumes a goal-matching alignment handoff instead of reconstructing decisions from a transcript. Unknown or other-owner active sessions are reported as conflicts.

**Boundary:** alignment completion is not implementation permission. It does not authorize commit, push, deployment, or release.

## Alignment Goal Lifecycle

**Problem:** a conversation can be archived or deleted by its host, while its local alignment records otherwise remain eligible forever and can make a later goal ambiguous.

**How it works:** confirmed handoffs enter an `open` lifecycle index. A completion claim marks the goal `completed`; the owner can explicitly run `tenetora alignment --archive --session-id <id> --owner-id <owner>` when the host conversation is no longer relevant. Open goals expire after 30 days without owner activity. Terminal lifecycle records are capped at 64, and terminal handoff history is pruned to 128 records or 90 days while retained handoffs remain explicitly readable.

**Observable result:** automatic binding considers only live sessions and registered open goals. One matching goal may bind; multiple goals produce a short-lived, current-turn-bound candidate request that the host model may use to select one goal only when the user request is semantically clear; otherwise the original fail-closed behavior remains. A completed, archived, expired, or abandoned goal cannot authorize a new high-risk action.

**Boundary:** the local runtime cannot observe whether a host conversation was archived or deleted. Explicit archive is the immediate close operation; activity expiry and bounded pruning are the automatic fallback. Model routing selects context only, is prompt-bound and short-lived, and never counts as user confirmation or execution authorization. Unsafe, stale, identity-mismatched, or ambiguous requests remain unselected, and high-risk work still requires the existing guards.

## End-to-End Skeleton First

**Problem:** a cross-component task deeply polishes one module while the user path across boundaries still does not run.

**How it works:** planning records whether the workflow is applicable. When applicable, it defines the smallest safe path across all participating boundaries, its contracts, its verification gate, and explicitly deferred refinement.

**Observable result:** the plan can show one runnable path and a concrete command or manual proof before non-blocking polish begins.

**Boundary:** “minimum” never removes essential authentication, authorization, data integrity, failure handling, or rollback.

## Change-Impact Preflight

**Problem:** a shared API, schema, Hook, plugin manifest, runtime contract, or widely referenced symbol changes without an impact inventory.

**How it works:** `tenetora impact --start` records the contract kind, structural analysis tool, symbols, references, affected files, and baseline. `impact --complete` requires a repeated rescan and verification result.

**Observable result:** commit checks can distinguish an analyzed shared-contract change from an unrecorded edit, and stale preflight evidence is visible.

**Boundary:** the quality of impact evidence depends on CodeGraph, language servers, compilers, schemas, and tests. The preflight cannot prove references that no analysis tool can see.

## External-Input and Prompt Guard

**Problem:** a webpage, report, issue, generated patch, or clipboard payload contains instructions that attempt local execution, secret access, or rule mutation.

**How it works:** `tenetora guard --action external-input` and `tenetora-prompt-guard` inspect untrusted content before its instructions are followed. Findings identify the unsafe pattern without granting it authority.

**Observable result:** suspicious content is classified before local actions; later reviewers receive only bounded, task-relevant, redacted input.

**Boundary:** this is a mechanical first line. Novel social engineering and semantically dangerous advice can still require a qualified security reviewer or human decision.

## Bounded Continuation Loop

**Problem:** “continue” resumes stale work, retries forever, or adopts another conversation's active task.

**How it works:** `tenetora-loop` records goal, exit criteria, latest verified step, blocker, identity, and next prompt. Each cycle performs one small check-fix-verify step. Repeated blockers and approval boundaries stop the loop.

**Observable result:** `tenetora loop-state --json` explains what can resume and why it must stop. Other sessions cannot silently mutate owner-bound state.

**Boundary:** loop state is not ground truth and never starts work in the background. The newest user request and current source remain authoritative.

## Model Continuity and Execution Attempts

**Problem:** a single conversation may move from one provider or model to another after a rate limit, but treating the switch as a new conversation can split the goal or let an unrelated session inherit it.

**How it works:** the host supplies one `conversation_continuity_id` for the conversation and a fresh `execution_attempt_id` for each model run. `provider`, `model`, and attempt status are recorded on the attempt. The alignment session, owner, and goal fingerprint remain unchanged. A missing continuity identifier blocks writes; a stale revision cannot overwrite a newer attempt.

**Observable result:** `alignment --execution-list --conversation-continuity-id <id> --json` exposes the ordered attempts. A rate-limited attempt can be followed by a completed attempt from another model without creating a second alignment session.

**Boundary:** this preserves governance identity, not hidden context or model memory. A new conversation must establish its own owner-bound identity and cannot reuse the old continuity identifier.

## Portable Subagent Governance

**Problem:** a workflow claims independent review without proving that a reviewer ran, had the required boundary, or returned a semantic verdict.

**How it works:** Tenetora defines portable contracts for code review, security audit, codebase scouting, and bounded repair. The CLI renders contracts and records dispatch evidence; the host performs actual dispatch. Review cycles have explicit round limits and subject fingerprints.

**Observable result:** a report distinguishes dedicated, built-in prompt-only, and main-agent self-review; a completed child process is not automatically treated as a passed review.

**Boundary:** Tenetora hooks and CLI do not spawn agents. Prompt-only isolation is not sufficient for security audit or implementation roles.

## Verification Claims

**Problem:** a local check passes and is later presented as proof that an entire task completed, even after the worktree changed or a newer check failed.

**How it works:** claims distinguish partial verification, completion, blocked, and failed outcomes. A completion claim binds the declared full command to project, owner/session/conversation, goal fingerprint, and a versioned fingerprint of the verified file paths, metadata, and bytes.

**Observable result:** a passed completion returns an `ah-claim-...` proof. A different goal, identity, project, command, newer failure, or changed verified content invalidates completion evidence; staging or committing the same verified content does not. `--check-proof-only` lets an authorized Git action reuse a still-valid proof without rerunning the full command.

**Boundary:** a claim proves the declared command result and binding, not that the project's chosen command is a complete testing strategy.

## Action Guards and Git Boundaries

**Problem:** an agent remembers general rules but skips the check immediately before commit, rule mutation, external input use, or completion reporting.

**How it works:** `tenetora guard --action <action>` runs checks specific to that action. Commit guard can inspect staged scope, secrets, sensitive paths, commit-message detail, impact state, and review evidence. Managed local Git hooks can call the same guard.

**Observable result:** failures name the blocked boundary and remediation. Existing user hooks are preserved and chained when Tenetora-managed wrappers are installed.

**Boundary:** guards never create authorization. Passing commit guard does not permit commit, push, deployment, or release without the user's separate instruction.

## Validation, Audit, and Evidence Refresh

**Problem:** `.tenetora/` exists but no longer reflects project structure, commands, rules, or evidence.

**How it works:** `validate` checks structural contracts; `run-all` executes project guardrails; `audit --strict` evaluates stability, reliability, control, content density, and governance effectiveness. `tenetora-update`, `refresh`, `review`, `repair`, and `recap` maintain evidence through reviewable artifacts.

**Observable result:** current evidence pointers, findings, scores, changed sources, skipped decisions, and remaining drift are explicit instead of hidden behind a regenerated directory.

**Boundary:** audit does not replace project tests. A high score cannot make stale product facts or a failed verification pass.

## Runtime and Platform Evidence

**Problem:** a skill or plugin file exists on disk, but the host is loading another copy, has not trusted the Hook, or supports only part of the runtime contract.

**How it works:** `status` and `doctor` inspect lifecycle skills, native plugin registration, Hook configuration, runtime observation, version, effective source, ownership, and platform maximum capability.

**Observable result:** states such as `active`, `active-partial`, `skills-only`, `covered`, and `needs-trust-review` explain both current capability and required follow-up.

**Boundary:** platforms do not expose identical APIs. Tenetora reports the real ceiling instead of treating a file-presence check as proof of runtime execution.

## Installation, Upgrade, and Migration

**Problem:** one machine has several projects and host integrations; an upgrade refreshes one surface but leaves another project, Hook, plugin, or legacy `.harness` state behind.

**How it works:** the machine registry records existing global and project surfaces. Scope-less upgrade performs bounded discovery and updates existing surfaces only. Ownership classifiers distinguish canonical, proven legacy, foreign, and review-required state. Runtime, hooks, and release pointers use transaction and rollback evidence.

**Observable result:** preflight and final summaries report each installation surface and governance-only project independently. `tenetora installations discover --auto --dry-run` previews bounded discovery.

**Boundary:** unknown third-party state remains untouched. Explicit scopes filter existing inventory; they do not silently create missing installations during upgrade.

## CI, Reports, and Governance Insights

**Problem:** local governance and CI produce unrelated results, or teams collect events without a bounded, privacy-preserving summary.

**How it works:** `setup-ci` creates reviewable GitHub, GitHub, or pre-commit integration; checks can emit SARIF and JUnit. `insights` summarizes governance trends and controlled rule usage without exposing raw prompts, paths, session IDs, or commands. `benchmark` measures validation, audit, and guardrail latency.

**Observable result:** local and CI checks share named boundaries; trends report insufficient data honestly instead of inventing metrics.

**Boundary:** insights are operational signals, not causal proof of developer productivity or model quality.
