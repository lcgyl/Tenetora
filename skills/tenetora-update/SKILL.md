---
name: tenetora-update
description: Use when an existing `.tenetora` has stale evidence, drift, changed AI rules, changed tool configs, changed project structure, or the user asks to update/refresh `.tenetora`.
---

# Tenetora Update

## Overview

Maintain an existing `.tenetora` so it remains a trustworthy project system of record. CLI output supplies deterministic evidence; the model handles semantic merge decisions.

User-facing invocation should stay simple:

```text
use tenetora-update 更新 .tenetora
```

Do not ask the user to paste the internal repair/refresh/review command sequence as a prompt. The skill owns those steps after it is invoked.

Skill package upgrade and project `.tenetora` maintenance are separate operations. To refresh the currently installed Tenetora package across tools, use `tenetora upgrade --apply` or rerun the online installer without scope flags. Upgrade updates all existing global/project surfaces in the current environment and project, but never creates a missing tool or scope. Before writes it snapshots the managed CLI runtime, Git hooks, ignored Hook state, and `.tenetora/.gitignore`; CLI bootstrap runs under a machine lock, then Hook convergence and volatile alignment-state ignore convergence follow. A later failure restores the snapshot unless a concurrent edit is detected. Use `tenetora install` when a new installation surface is intentionally required.

## Freshness Gate

Before using cached instructions, ensure the CLI and read the latest runtime copy:

```bash
python3 <tenetora-skill-dir>/scripts/ensure_cli.py --install --json
<returned-absolute-command> skill-instructions --skill tenetora-update
```

`<tenetora-skill-dir>` means this exact skill instance loaded by the current host. Use the JSON `command` absolute path for every later CLI call. Never fall back to another host's `.agents/skills`, `.codex/skills`, `.claude/skills`, `.cursor/skills`, `.opencode/skills`, or `.zcode/skills`; never use or persist `PYTHONPATH=<tool-skill>/cli python -m tenetora.cli` as an alias, rule, or normal invocation. Direct `PYTHONPATH` execution is only a bounded diagnostic from a checked-out Tenetora source tree.

If the returned content differs from this loaded skill, follow the returned instructions instead of cached text.

## Host Dispatch Availability Gate

Before recording `unavailable` for any portable subagent role, inspect the current host's actual tool catalog, including already visible tools and deferred or discoverable tool metadata, for native or custom dispatch (`spawn_agent` or the host equivalent), waiting, and result retrieval. Static `doctor`, plugin, or Hook status describes platform support and lifecycle observation only; missing or inactive Hooks never prove that the current host session cannot dispatch. When a qualified dispatch tool exists, use it once and inspect the child result before recording a semantic outcome. Record `unavailable` only after the current session capability is absent, fails, or cannot satisfy the role boundary, and persist the bounded reason.

## When to Use

- `.tenetora/` already exists and may be stale.
- `audit --strict` reports stale evidence, weak project map quality, missing verification, or guardrail drift.
- `AGENTS.md`, `CLAUDE.md`, `CLAUDE.local.md`, `.cursor/rules`, `.claude/rules`, `.claude/skills`, or `.mcp.json` changed.
- The conversation includes a possible new rule or constraint that should persist beyond the current task.
- Build files, CI, test entry points, project structure, or durable project docs changed.
- The user asks to update, refresh, maintain, or resync `.tenetora`.

Ordinary business code edits do not require `.tenetora` updates unless they change structure, rules, verification, architecture facts, or agent-facing context.

## Trigger Matrix

| Trigger | Action |
| --- | --- |
| Existing `.tenetora` has stale evidence or source hash mismatch | Run validate/audit, then preview with `refresh --strategy diff`. |
| `AGENTS.md`, `CLAUDE.md`, `CLAUDE.local.md`, `.cursor/rules`, `.claude/rules`, `.mcp.json`, or another tool config changed | Refresh evidence and decide whether entrypoint governance is needed. |
| tool config changed in `.claude/settings*`, `.codex/config.toml`, `.codex/rules/*.rules`, `opencode.json`, or `opencode.toml` | Record config drift and keep raw local/private content out of shared harness docs. |
| Project structure, CI, build files, verification commands, or durable docs changed | Refresh project evidence and update affected wiki/rules/workflows. |
| Audit reports entrypoint drift, weak project map, missing verification, or guardrail drift | Apply the smallest reviewable update, usually via `review --apply --strategy merge`. |
| Tenetora was upgraded and known old behavior may remain in a project | Run `tenetora repair --check` before normal refresh/audit work. |
| An older project lacks subagent dispatch governance | Apply the missing-only `subagent-governance` repair; never overwrite project-authored rules or role contracts. |
| Conversation includes a possible durable rule | Use chat rule capture before writing anything. |
| A high-risk harness update has unresolved ownership, migration, replacement, or disclosure choices | Use `tenetora-decision-interview` before applying candidates. |

## When Not To Use

- Do not use for ordinary business code edits that do not affect `.tenetora`.
- Do not use to create the first `.tenetora`; use `tenetora-init`.
- Do not use as a substitute for audit acceptance; use `tenetora-audit` after updating.
- Do not apply high-risk changes such as replace, source migration, permission expansion, local path retention, or secret handling without user confirmation.

## Required Setup

Locate the installed `tenetora` router skill and ensure the shared CLI:

```bash
python3 <tenetora-skill-dir>/scripts/ensure_cli.py --install --json
```

Treat `available: true` plus `path_ready: false` as usable. Continue silently with the absolute `command` value returned by `ensure_cli.py`; PATH is optional terminal ergonomics and should be mentioned only when the user asks about direct terminal invocation.

When a project update target contains independent Git submodules, resolve `--repository-scope` before the update pipeline starts. Interactive runs must ask the user to choose `parent`, `all`, or explicit submodule paths; non-interactive runs without a selection must stop before the first write. Parent refreshes keep child source files out of parent evidence, and `all` processes each repository as its own lifecycle target.

## Update Flow

First run the Freshness Gate above and follow the returned runtime instructions before any repair, refresh, or audit command. Do not trust a cached `SKILL.md` after the user says the package was upgraded or after the installer reports a newer version.

Then read the target project `.tenetora/README.md` and reload its `Runtime Contract` after context compaction or before review, scoring, audit, acceptance, or broad repository work. If `.tenetora/changes/INDEX.md` exists, use it plus `.tenetora/state/current-evidence.json` to find current artifacts; do not scan old `.tenetora/changes/archive/` history unless investigating provenance.

Load the harness-maintenance rule slice and record consumption before editing `.tenetora`:

```bash
tenetora rules --context harness
```

Start with known compatibility repairs before normal refresh work:

```bash
tenetora repair --check
```

When project-local commit hooks are missing and reminders have not been declined, interactive update must offer: install now, ask next time, or do not remind on this machine. Non-interactive update records or preserves the pending/deferred state without silently installing. Keep the active command available in all cases:

```bash
tenetora hooks --status
tenetora hooks --install
tenetora hooks --defer
tenetora hooks --decline
```

For an AI chat invocation, run `tenetora hooks --status` before the update. A chat is user-interactive even if the CLI subprocess itself has no TTY: when the status is pending or deferred, ask the user to choose `立即安装`, `下次再说`, or `不需要（不再提醒）`, execute the matching command, and only then continue the update. Skip the question only for explicit unattended/CI operation or an existing decline decision.

After project maintenance, inspect Codex Hook capability. When marketplace management is unreadable, no enabled native Tenetora Hook remains, and the project has not chosen `不再提醒`, ask whether to use `立即启用`, `仅 Skills`, or `不再提醒此项目`. Enable only through:

```bash
tenetora install --path . --tools codex --codex-hooks project
```

Non-interactive update must not create or modify `.codex/hooks.json`; it may only print the explicit command. Do not edit `.codex/hooks.json` directly. The CLI owns the native/fallback conflict gate, tracked-file authorization, stable runtime, backup, ownership, and rollback behavior.

If the command reports a concrete known issue, review the repair report before applying. Apply only recoverable repairs before trusting audit output:

```bash
tenetora repair --apply --fix all
```

If repair reports a skipped or unrecoverable `CLAUDE.local.md` migration, stop and ask the user for the missing backup or explicit manual recovery instructions.

The high-level `tenetora update` command runs three missing-only lifecycle repairs before validation: skeleton-first planning, decision alignment, then subagent governance. They add missing canonical files and minimal task-start consumption gates without replacing project-owned content. To inspect or apply the planning compatibility unit:

```bash
tenetora repair --check --fix skeleton-first-planning
tenetora repair --apply --fix skeleton-first-planning
```

To inspect or apply only the subagent compatibility unit:

```bash
tenetora repair --check --fix subagent-governance
tenetora repair --apply --fix subagent-governance
```

This repair may add the missing dispatch rule, four role specs, cache ignore entries, task-start consumption note, and loop schema v3 fields. New defaults include the three-level resolution and isolation qualification, but the repair must preserve an existing same-name file byte for byte and must not append duplicate managed blocks. Existing project specs are authoritative even when they predate built-in fallback wording.

Then run checks:

```bash
tenetora validate
tenetora audit --strict
```

Before applying rule, workflow, agent, or documentation changes, run the action guard:

```bash
tenetora guard --action rules
```

If the update is being reviewed across multiple iterations, record score trend:

```bash
tenetora audit --record-history
tenetora audit --trend
```

If drift or stale evidence exists, preview first:

```bash
tenetora update
tenetora refresh --strategy diff
tenetora refresh --tools auto --strategy diff
tenetora review
```

Read the newest update report, patch, evidence, and migration plan before applying anything.

If `.tenetora` changed and verification passed, record the verification claim in the same turn,
before reporting completion or handing the work to a later commit/push/tag step:

```bash
tenetora guard --action claim --claim-kind completion --verification-command "<full-command>" --verification-status passed
```

Use `--claim-kind completion` for the full verification command and a current owner-bound alignment
session with a valid goal fingerprint. Without an aligned goal, record only partial, blocked, or failed
work. Partial checks must use
`--claim-kind partial-verification` and cannot satisfy completion; blocked or failed work must use its
matching kind. Before a later commit/push/tag authorization, completion lookup can reuse the proof without
rerunning the full command:

```bash
tenetora guard --action claim --claim-kind completion --check-proof-only \
  --expected-verification-command "<same full-command>" --session-id "<session-id>" \
  --owner-id "<owner-id>" --json
```

It rejects newer failures, changed verified file content or metadata, changed command, scope, or binding, and old
fingerprint formats. Staging or committing the exact verified content does not invalidate the proof. Include the
returned claim proof in the completion report, and still run the separate action guard for commit or push. Do not
rerun the full verification merely because the next user message authorizes commit, push, or tag.

For parallel agents working in separate modules, use scoped partial claims for the checks each agent actually ran:

```bash
tenetora guard --action claim --claim-kind partial-verification \
  --verification-command "<module verification command>" --verification-status passed \
  --verification-scope src/module-a
```

Repeat `--verification-scope` when one check covers several project-relative paths; omit it for a whole-worktree
check. The commit guard matches staged paths to the latest fresh claim and can combine claims from different agents.
Uncovered paths remain a warning and are not complete fresh evidence. Partial scoped claims cannot satisfy or be
combined into a completion claim; completion still uses one full goal-covering command and the required handoff.
The independent review subject remains the final Git index/tree, so freshness evidence does not broaden what is
reviewed or committed. An explicitly scoped review may record a path-level subject snapshot. If the full subject
changes only outside that scope, the guard exposes an explicit `delegation --review-rebind` command that starts a new
review for the new subject and includes the delta; it never reuses the old pass silently. Scope changes and legacy
unscoped reviews remain stale and require manual review.

## Semantic Merge Rules

Use CLI evidence for facts and model reasoning for semantic decisions.

- Metadata updates: evidence hash, current evidence pointer, evidence archive, recap, progress can be applied automatically when no sensitive content is present.
- Structure fixes: missing directories and neutral placeholder files can be created after validation.
- Semantic updates: wiki, rules, workflows, agents, and docs must be written as candidates first; do not append generic generated templates into rich project guidance.
- high-risk updates: overwrite of non-semantic files, source migration, gitignore changes, permission expansion, secrets, local paths, or replace behavior require user confirmation. `.tenetora` semantic Markdown/Text must remain source-preserving and use update candidates even when `backup` or `replace` is selected. Tool-private local config such as `.claude/settings.local.json` must be skipped by the CLI and must not enter migration plans, evidence, backups, or shared docs.

Preferred apply path:

```bash
tenetora update
tenetora review --apply --strategy merge --migrate plan --tools auto
```

This refreshes deterministic extraction evidence and `.tenetora/state/current-evidence.json` in place, then writes semantic `.tenetora` changes under `.tenetora/changes/update-candidates/` for review.

`tenetora update` is the high-level project `.tenetora` maintenance command. It defaults to `--migrate plan`; pass `--migrate merge` only after new migration sources are approved. `tenetora update -g/-i/-b` is the lower-level skill-install refresh path; use `upgrade --check/--apply` for normal skill package upgrades.

Do not use replace unless the user explicitly approves it.

After upgrading the skill package, check known project-level repairs:

```bash
tenetora repair --check
tenetora repair --apply --fix all
```

Use repair only for explicit known issues reported by the command. A substantive unmanaged root `CLAUDE.md` is a repair issue because Claude Code can bypass the unified `.tenetora` entrypoint; repair must back it up, migrate shared content to `.tenetora/agents/claude.md`, and rewrite the root file as a thin adapter. A substantive unmanaged root `CLAUDE.local.md` is also a repair issue because it bypasses the unified local `.tenetora` entrypoint. A managed root `CLAUDE.local.md` is not enough to declare the issue fixed; if the migrated `.tenetora` target still contains `Content omitted...`, repair must restore from the earlier backup or report that manual recovery is needed. For normal `.tenetora` content drift, keep using refresh/review.

When the update is caused by root or tool-specific AI entry files, use explicit entrypoint governance:

```bash
tenetora refresh --strategy diff --migrate merge --entrypoints merge --tools auto
```

This backs up `AGENTS.md`, `CLAUDE.md`, `CLAUDE.local.md`, `.cursor/rules/*`, and `.claude/rules/*` before rewriting them as thin adapters. Use `--entrypoints plan` when the user wants only a migration plan.

For read-only AI config drift review, generate a reconcile plan and candidates:

```bash
tenetora audit-configs --reconcile --write
```

This writes review artifacts under `.tenetora/changes/` and does not overwrite source entry files. Use init/update entrypoint governance for actual adapter rewrites after the user approves.

`CLAUDE.local.md` must follow the same unified entrypoint policy during update and repair as during init. If `.tenetora/` is ignored, local Claude content belongs in `.tenetora/agents/claude-local.md`; if `.tenetora/` is tracked, it belongs in ignored `.tenetora/local/agents/claude-local.md`. Do not preserve a substantive root `CLAUDE.local.md` after accepted entrypoint governance.

Use `--entrypoints merge` only after source content has been migrated with `--migrate transfer|merge`. Do not pair `--entrypoints merge` with `--migrate plan|ignore`.

Project refresh defaults to project-level migration sources only. Use `--include-global-migration-scan` only after the user explicitly asks to consider global skills.

## Chat Rule Capture

Use chat rule capture when the user says something that looks like a durable project rule, workflow constraint, safety boundary, preferred verification command, or tool-specific instruction.

Process:

1. Extract the smallest possible candidate rule from the chat.
2. check existing `.tenetora/rules/`, `.tenetora/workflows/`, `.tenetora/agents/`, `AGENTS.md`, `CLAUDE.md`, `CLAUDE.local.md`, `.cursor/rules/`, and `.claude/rules/` for semantic coverage.
3. Use the CLI for deterministic duplicate and destination checks:

   ```bash
   tenetora capture-rule --text "<candidate rule>"
   ```

4. If the rule is already covered, tell the user where it is covered and do not write.
5. If it is new, Ask the user before writing with this shape: "检测到一条可能的规则：<candidate rule>。是否需要记录到 `.tenetora`？"
6. Only after the user confirms, write it:

   ```bash
   tenetora capture-rule --text "<candidate rule>" --apply
   ```

Prefer `.tenetora/rules/` for hard rules, `.tenetora/workflows/` for multi-step process constraints, and `.tenetora/agents/` for tool or role-specific agent behavior. Do not store secrets, credentials, local-only paths, or private personal preferences as shared rules.

## Drift Signals

Treat these as update triggers:

- `stale-extraction-evidence`
- `transient-source-jitter`
- source hash mismatch
- new or changed AI entry files
- managed entrypoint content outside the Tenetora block
- new rule or skill sources
- changed CI/build/test files
- missing required `.tenetora` documents
- guardrail failures

Do not turn guesses into facts. Facts need `Source: <path>`, inferences need `Inference: ...`, and unresolved points need `Unknown:`.

## Verification

After update:

```bash
tenetora validate
tenetora audit --strict
tenetora run-all
tenetora recap --write
```

Run `tenetora run-all` before final review, scoring, or acceptance. Use `tenetora run-all --runner python` in Windows, minimal CI images, or any environment without bash. Custom project checks belong in `.tenetora/guardrails/custom/` and must use the same `ERROR/FIX/SEE` failure style. If guardrails are skipped because the runner is unavailable or out of scope, record the reason explicitly.

Report updated files, skipped high-risk changes, remaining drift, `.tenetora/changes/INDEX.md`, and whether `.tenetora` is now stable, reliable, controlled, and content-dense.

When subagent governance was repaired, also report `tenetora delegation --status`, platform capability/fallback from `doctor`, the latest resolution/isolation metadata, and whether any result is marked `未经独立审查`. Use `delegation --render-prompt` only as a side-effect-free contract check. Do not claim that update, repair, rendering, CLI, or hooks dispatched an agent.

If update latency is part of the issue, run:

```bash
tenetora benchmark --write
```
