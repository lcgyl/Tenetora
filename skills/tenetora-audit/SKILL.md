---
name: tenetora-audit
description: Use when checking whether `.tenetora` is stable, reliable, controlled, ready for agent work, or when validating init/update results.
---

# Tenetora Audit

## Overview

Audit whether `.tenetora` can support stable, reliable, controlled agent work. This skill does not initialize or update unless the audit identifies the next lifecycle skill to use.

## Freshness Gate

Before using cached instructions, ensure the CLI and read the latest runtime copy:

```bash
python3 <tenetora-skill-dir>/scripts/ensure_cli.py --install --json
<returned-absolute-command> skill-instructions --skill tenetora-audit
```

`<tenetora-skill-dir>` means this exact skill instance loaded by the current host. Use the JSON `command` absolute path for every later CLI call. Never fall back to another host's `.agents/skills`, `.codex/skills`, `.claude/skills`, `.cursor/skills`, `.opencode/skills`, or `.zcode/skills`; never use or persist `PYTHONPATH=<tool-skill>/cli python -m tenetora.cli` as an alias, rule, or normal invocation. Direct `PYTHONPATH` execution is only a bounded diagnostic from a checked-out Tenetora source tree.

If the returned content differs from this loaded skill, follow the returned instructions instead of cached text.

## Host Dispatch Availability Gate

Before recording `unavailable` for any portable subagent role, inspect the current host's actual tool catalog, including already visible tools and deferred or discoverable tool metadata, for native or custom dispatch (`spawn_agent` or the host equivalent), waiting, and result retrieval. Static `doctor`, plugin, or Hook status describes platform support and lifecycle observation only; missing or inactive Hooks never prove that the current host session cannot dispatch. When a qualified dispatch tool exists, use it once and inspect the child result before recording a semantic outcome. Record `unavailable` only after the current session capability is absent, fails, or cannot satisfy the role boundary, and persist the bounded reason.

## When to Use

- The user asks whether `.tenetora` meets the final purpose.
- Initialization or update just finished and needs acceptance.
- A project has conflicting AI instructions or unclear guardrails.
- The user asks for Harness Engineering quality, stability, reliability, or control status.

## Trigger Matrix

| Trigger | Action |
| --- | --- |
| User asks whether `.tenetora` is stable, reliable, controlled, or ready for agent work | Run validation and strict audit before trusting `.tenetora`. |
| Init or update just finished | Treat this as acceptance and report pass, conditional pass, or fail. |
| Before trusting `.tenetora` for autonomous or semi-autonomous agent work | Check entrypoints, evidence freshness, verification workflows, guardrails, and control boundaries. |
| Conflicting AI instructions, unclear guardrails, or suspected drift | Identify blockers/warnings and recommend `tenetora-init` or `tenetora-update`. |
| User asks to compare output with Harness Engineering goals or 285-style best practice | Evaluate stable, reliable, controlled dimensions and list gaps. |
| Audit finds an unresolved high-risk governance decision | Recommend `tenetora-align`; audit reports evidence and does not answer the decision. |
| Subagent governance is enabled or a review cycle was used | Check the portable rule/specs, ignored report cache, bounded state, compact trail evidence, platform capability claims, and fallback label. |

## When Not To Use

- Do not use to modify files.
- Do not use to initialize missing `.tenetora`; recommend `tenetora-init`.
- Do not use to refresh stale evidence or merge rules; recommend `tenetora-update`.
- Do not treat a passing validation as enough if strict audit reports blockers.

## Required Setup

Locate the installed `tenetora` router skill and ensure the shared CLI:

```bash
python3 <tenetora-skill-dir>/scripts/ensure_cli.py --install --json
```

Treat `available: true` plus `path_ready: false` as usable. Continue silently with the absolute `command` value returned by `ensure_cli.py`; PATH is optional terminal ergonomics and should be mentioned only when the user asks about direct terminal invocation.

## Audit Commands

Run:

```bash
tenetora validate
tenetora audit --strict
tenetora delegation --status
```

If guardrails exist, also run:

```bash
tenetora run-all
```

Use `tenetora run-all --runner python` when bash is unavailable or when CI needs a platform-neutral runner.

Finally record session state when appropriate:

```bash
tenetora recap --write
```

Use machine-readable formats when the result needs to feed another tool:

```bash
tenetora audit --strict --json
tenetora audit --strict --format sarif
tenetora audit --strict --format junit
```

When the user asks for health trend, repeated acceptance, or whether quality is improving, record and read audit history explicitly:

```bash
tenetora audit --record-history
tenetora audit --trend
```

For monorepos, use module evidence when the question is scoped to one module:

```bash
tenetora audit --module <module-name>
```

When the user asks why update/audit feels slow, or when CI performance is the topic, benchmark without refreshing by default:

```bash
tenetora benchmark
tenetora benchmark --write
```

Before concluding, reload `.tenetora/README.md` and its `Runtime Contract`, especially after context compaction. Runtime consumption checks must confirm `.tenetora/README.md` has a short Runtime Contract and that `task-start.md` plus `verification.md` instruct agents when to run `tenetora run-all` or `.tenetora/guardrails/checks/run-all.sh`.

When reading change history, start from `.tenetora/changes/INDEX.md` and `.tenetora/state/current-evidence.json`. Do not treat older files under `.tenetora/changes/archive/` as current state unless investigating provenance.

## Dimensions

Report five dimensions:

- stable: clear entry points, bounded context size, no obvious rule conflicts, navigable `.tenetora` structure.
- reliable: facts have sources, current evidence is fresh, verification workflows exist, migrated rules are preserved.
- controlled: dangerous operations have approval boundaries, secrets and local paths are guarded, tool-private rules do not drift silently.
- content_density: runtime documents have enough project-specific signal and are not placeholder-only shells.
- governance_effectiveness: rule-context loading, action guards, verification-claim events, and compact subagent dispatch metadata are recorded in `.tenetora/state/governance-trail.json`; verification claims include claim proofs; configured hooks are visibly installed instead of silently absent; hook/CLI status is never misreported as actual dispatch execution.

Subagent checks must confirm `.tenetora/rules/subagent-dispatch.md`, all four `.tenetora/templates/subagents/*.spec.md` files, ignored `.tenetora/.cache/subagents/`, loop schema v3 limits, no report body in governance trail, no unauthorized `implementer`, and no more than 20 active report files. Explicit dispatch records must include role contract hash, resolution mode, host carrier, isolation level, and contract status. Treat `security-auditor`/`implementer` with `soft-boundary` as blockers and ordinary read-only roles with `soft-boundary` as warnings. Platform status must distinguish dedicated dispatch, built-in role injection, host-dependent or recommendation-only support, lifecycle observation, and main-agent fallback without claiming that configuration or state proves execution.

Entrypoint checks must confirm that `AGENTS.md`, `CLAUDE.md`, `CLAUDE.local.md`, `.cursor/rules/*`, and `.claude/rules/*` are thin adapters or mirrored into `.tenetora/`. `CLAUDE.md` must not keep substantive shared rules outside `.tenetora/agents/claude.md`. `CLAUDE.local.md` must also route to the target implied by the current `.tenetora` git policy, and its migrated target must not remain a `Content omitted...` placeholder. If audit reports `entrypoint-drift-risk` or `tool-rule-drift-risk`, recommend `tenetora-update` with `--migrate merge --entrypoints merge`. If audit reports `claude-entrypoint-unmanaged`, `claude-local-entrypoint-unmanaged`, `claude-local-target-mismatch`, or `claude-local-migration-incomplete`, recommend `tenetora repair --check` before normal refresh work.

## Result Format

Lead with the conclusion:

- Pass: stable, reliable, controlled, and content-dense enough for agent work.
- Conditional Pass: usable, but warnings need scheduled maintenance.
- Fail: blockers or stale evidence make agent work unreliable or unsafe.

Then include:

- blocker/error/warning counts.
- concrete issue names.
- audit trend or benchmark data when requested.
- next command or next lifecycle skill.
- files or evidence used.
- latest independent review status or the explicit `未经独立审查` fallback when delegation was unavailable.

If audit reports stale evidence or drift, recommend `tenetora-update`. If `.tenetora` is missing, recommend `tenetora-init`.

Evidence freshness must use `.tenetora/state/current-evidence.json` when present. Source hashes are based on worktree bytes and declare `hash_basis: worktree-bytes`; do not treat older historical evidence files as current unless no pointer exists. If audit reports `transient-source-jitter`, ask the user to rerun after the IDE or formatter is idle instead of refreshing evidence from an unstable snapshot.

If audit or user context indicates the project was initialized by an older Tenetora version, recommend:

```bash
tenetora repair --check
```

Only recommend `repair --apply` when `repair --check` reports a concrete known issue.
