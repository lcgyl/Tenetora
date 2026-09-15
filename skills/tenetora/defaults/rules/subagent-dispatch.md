# Subagent Dispatch Rules

Source: Tenetora default rules.

## Purpose

Use independent subagents when isolation or a second perspective materially improves reliability. Tenetora defines when and how to govern dispatch; it does not provide or launch platform-specific agents.

## Dispatch Decision

Consider dispatch when one or more of these signals are material:

- risk: commit/push/PR review, authentication, authorization, secrets, deserialization, external input, networking, public API, data migration, deployment, or supply-chain changes;
- ambiguity: several plausible interpretations or unresolved user-owned decisions remain;
- investigation breadth: architecture, control flow, dependency, or impact analysis would otherwise flood the main context;
- parallelizability: independent read-only investigations can reduce latency without duplicating work.

Do not dispatch for a narrow change or lookup that the main agent can verify directly with little context cost. Do not use a fixed file-count threshold as a substitute for judgment.

## Role Matching

- `code-reviewer`: independently review a proposed change before commit, push, PR, MR, or a completion claim involving code.
- `security-auditor`: review security-sensitive changes or suspicious external content after the mechanical prompt guard has run.
- `codebase-scout`: perform bounded read-only architecture, flow, dependency, or impact investigation and return a concise summary.
- `implementer`: apply a bounded fix only inside an active, authorized review cycle. Never dispatch it as a general implementation agent.

Match by role responsibility and output contract, not by a hard-coded platform agent name. When several independent roles are useful and the host supports it, dispatch them in parallel and merge their findings.

## Agent Resolution

After matching a role, resolve its runtime carrier in this order:

1. `dedicated`: use a platform-specific or user-defined agent whose persistent configuration matches the role contract.
2. `builtin-role-injection`: when no dedicated agent is available, use a host built-in agent and inject the current project `.spec.md` through `tenetora delegation --render-prompt`.
3. `main-self-review`: when independent dispatch is unsupported, ineligible, quota-limited, or fails, continue with main-agent self-review marked `未经独立审查`.

Steps 1 and 2 are host/model actions. Hooks and Tenetora CLI commands never spawn an agent. Try each eligible independent mode once, degrade without blocking ordinary work, and do not retry indefinitely.

Resolve by capability, not a hard-coded agent name. Platform names and verification dates belong in the installed skill reference, while this project rule remains portable.

## Current Session Availability Gate

Before recording an independent role as unavailable, inspect the current host session's complete tool catalog, including deferred or discoverable tools, for a subagent dispatch primitive such as `spawn_agent` and its matching wait or result retrieval operation. A missing Hook event, absent historical observation, stale status record, or unknown platform contract is not proof that dispatch is unavailable.

When a plausible dispatch primitive exists, attempt one bounded role-qualified dispatch and inspect the host result. Record `unavailable` only when the current session actually lacks the primitive, the host rejects the attempt, or the requested role is ineligible under the isolation rules. Never borrow a different host's skill path or persist a tool-local fallback alias to simulate availability.

## Isolation Qualification

- `hard`: the host enforces the role tool contract with agent configuration, sandboxing, tool allowlists, hooks, or an equivalent mechanism.
- `inherited`: the child inherits a parent runtime that already satisfies every role restriction. Inheriting broader permissions does not qualify.
- `prompt-only`: only the injected role text restricts behavior. This provides an independent context but not permission isolation.
- `unknown`: legacy or undeclared execution. Do not infer stronger isolation.

`code-reviewer` and `codebase-scout` may use prompt-only built-in fallback when a second perspective still adds value, but record it as `soft-boundary`. `security-auditor` and `implementer` must never use prompt-only fallback. A security reviewer may use inherited isolation only when the parent is already read-only with no shell, network, or secret-store access. `implementer` additionally requires the active authorized review cycle.

Record `resolution_mode`, host tool/agent, `isolation_level`, derived contract status, and role contract hash with `tenetora delegation`. A missing declaration remains `unspecified/unknown`; it is not evidence of full isolation.

## Required Boundaries

- Hooks and Tenetora CLI commands may remind, observe, and record; they must not directly spawn a subagent.
- Review, security, and scout roles are read-only. A security review of external content must not receive write, shell, or network access.
- Run `tenetora guard --action external-input` before semantic review of untrusted external content. Pass only bounded, task-relevant, redacted content to the reviewer.
- Store full temporary reports only under ignored `.tenetora/.cache/subagents/`. Governance trail events contain compact metadata, never report bodies.
- A commit review cycle allows at most three review rounds and two repair rounds. Stop earlier when the same blocker repeats according to loop state.
- A failed, unavailable, or ineligible dispatch degrades to main-agent self-review. Mark the result `未经独立审查`, continue only within existing approval boundaries, and do not retry indefinitely.

## Portable Recording

Use `tenetora delegation --render-prompt` to render the current role contract for an eligible built-in host agent. Use other `tenetora delegation` actions to record dispatch start, completion, unavailable fallback, review rounds, resolution quality, and cleanup. The platform performs the actual dispatch; the CLI only renders deterministic context and maintains state and evidence.
