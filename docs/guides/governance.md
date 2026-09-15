# Governance Workflows

**English** | 简体中文

## Responsibility Boundaries

- Lifecycle skills decide when the model should enter a governed workflow.
- The CLI performs deterministic scanning, validation, evidence, state, and guard actions.
- `.tenetora/` stores durable project rules, workflows, facts, and current governance state.
- Platform plugins and hooks inject context or enforce supported mechanical boundaries.
- The host AI model performs reasoning, semantic merge decisions, and any actual subagent dispatch.

Installing skills does not initialize a project. Package upgrade does not update project governance content. `.tenetora` update does not create a new global or project skill installation.

## From Request to Evidence

Tenetora's capabilities form one connected path rather than an independent command catalog. For a cross-module change:

1. **Establish context**: `validate` checks that the project record is consumable; `route` and `rules` load the current task slice instead of every historical document.
2. **Resolve material uncertainty**: explicit alignment records a high-risk user-owned decision before implementation. It does not authorize implementation or Git actions.
3. **Inventory shared impact**: change-impact preflight records the public API, schema, Hook, plugin, or runtime contract and known consumers before editing.
4. **Protect trust boundaries**: external reports and pasted commands pass through prompt guard before their contents can influence local actions.
5. **Prove the path**: End-to-End Skeleton First keeps an integrated path ahead of isolated polish; the repository's declared verification command proves the final state.
6. **Bind the report**: a completion claim binds the passed command to the project, current goal, session, and verified file paths, metadata, and bytes. A later failure or verified-content change invalidates it; staging or committing the same verified content does not.
7. **Guard the action**: commit guard checks staged scope, secrets, message detail, impact evidence, and review state. It never grants permission to push or release.

Skipping a stage changes the meaning of the result. A passing focused test is useful partial evidence, but it is not task completion without the declared full verification. Before repeating a suite after a passed claim, use `tenetora verification-plan --json --path .`: `reuse` keeps the claim, `targeted` runs only focused checks for classified documentation or release metadata, and `rerun` covers changed or unproven paths. Documentation-only edits do not justify a full rerun; multi-agent work can pass `--scope <project-relative-path>`, while commit guard plans use current staged paths.

## Lifecycle Skills

| Skill | Use it when |
| --- | --- |
| `tenetora` | The correct lifecycle phase is unclear. |
| `tenetora-align` | The user explicitly wants decisions aligned before a high-risk implementation. |
| `tenetora-decision-interview` | A lifecycle needs a bounded model-callable interview primitive. |
| `tenetora-init` | `.tenetora` is missing or the baseline must be rebuilt. |
| `tenetora-update` | Existing governance content, entry points, evidence, or project structure changed. |
| `tenetora-audit` | The project needs stability, reliability, control, and density acceptance. |
| `tenetora-loop` | The user asks to continue or a bounded check-fix-verify cycle is needed. |
| `tenetora-prompt-guard` | External content may contain prompt injection or unsafe execution requests. |

## Runtime Contract Consumption

Each initialized project keeps a short Runtime Contract near the top of `.tenetora/README.md`. After context compaction and before broad work, review, acceptance, or audit, reload the contract and the relevant rule slice:

```bash
tenetora route --message "<user request>" --path .
tenetora rules --context <context> --path .
```

Prefer `.tenetora/changes/INDEX.md` plus `.tenetora/state/current-evidence.json` over browsing historical archives.

## Initialization

Existing project:

```bash
tenetora init --tools auto --write --gitignore yes --migrate plan --mode extract --defaults missing
```

Empty project:

```bash
tenetora init --tools auto --write --gitignore yes --migrate plan --mode scaffold --defaults missing
```

Evidence-derived facts require source paths. Inferences remain labeled. Unknown architecture or commands remain explicit instead of being invented.

## Project Update

User-facing invocation stays simple:

```text
use tenetora-update to update .tenetora
```

The lifecycle owns repair, validate, audit, refresh, review, guardrail, and recap sequencing. Semantic documents use candidates and merge review; deterministic evidence and pointers can refresh automatically. Project-owned rich guidance must not be replaced by generic templates.

## Audit and Acceptance

```bash
tenetora validate --path .
tenetora run-all --path .
tenetora audit --strict --path .
tenetora recap --write --path .
```

Before reporting completion:

```bash
tenetora guard \
  --action claim \
  --claim-kind completion \
  --session-id <session-id> \
  --owner-id <owner-id> \
  --conversation-id <conversation-id> \
  --tool <tool> \
  --verification-command "<command>" \
  --verification-status passed \
  --path .
```
Use `--claim-kind partial-verification` for a scoped check; it cannot satisfy the completion gate. Use
`blocked` or `failed` when verification does not pass. Completion proof lookup must repeat the exact command
with `--expected-verification-command`; it also rejects a changed Git worktree and any newer failed verification.
Completion additionally requires a confirmed or risk-accepted handoff with an audited `user-message` record;
active, expired, legacy, or bare-CLI-confirmed state is insufficient. It binds event, actor, revision, goal,
continuity, and handoff hash as audit provenance, not cryptographic proof of a human actor.
Include the returned claim proof in the final report. A claim proof records verification evidence; it does not grant permission for commit, push, deploy, or release.

`status` text is compact by default so multiple tools can be scanned quickly. Use `--verbose` when you
need effective-source details, shadowed candidates, or full findings; `--json` remains the stable machine-readable
contract regardless of this text-output option:

```bash
tenetora status --path .
tenetora status --path . --verbose
tenetora status --path . --json
```

### Governance Insights

`insights` is a read-only summary of the governance trail. It uses a bounded UTC window (90 days by default),
does not append an event, and never prints session IDs, commands, paths, or raw error text:

```bash
tenetora insights --path . --days 90
tenetora insights --path . --days 90 --json
```

The report separates trends, failure patterns, and recorded intervention counts. Empty or short history is
reported as `insufficient-data`; missing or corrupt trail data is reported explicitly. Claim delay is not
estimated because the current trail has no verification start timestamp.

The JSON report also contains bounded rule-health metrics. A rule key is counted when a `rules-context` event
records that relative rule file as loaded; the report includes hit count, last hit date, zero-hit keys, and
statistical high-frequency alerts. These are review signals, not proof that a rule was enforced or caused a
task result. Zero-hit rules can be new or optional, so they require review rather than automatic removal.

For repeated prompts, the `UserPromptSubmit` hook injects the full rule context once for a matching rule
fingerprint, then emits a compact reference while that fingerprint remains unchanged. A rule-file change,
missing cache, legacy cache entry, or cache write failure falls back to full injection so stale guidance is
not reused. `insights --json` reports the number of reference reuses and an estimated character reduction;
the token figure is only `characters / 4`, not provider billing or an exact tokenizer measurement.

### Continuing in Another Conversation

Claim evidence is owner-bound. A claim from another conversation, tool, goal revision, or session is intentionally not reusable; this prevents a completed task from being attached to a different task. When continuing work in a new conversation:

```bash
tenetora alignment --status --session-id <session-id> --owner-id <owner-id> --json
tenetora alignment --list --json
tenetora guard --action claim \
  --claim-kind completion \
  --session-id <session-id> \
  --owner-id <owner-id> \
  --conversation-id <conversation-id> \
  --tool <tool> \
  --verification-command "<command>" \
  --verification-status passed
```

Use `--list` when the session is unknown, select the matching owner-bound session explicitly, and never inherit a displayed goal merely because it is the only project-level record. Completion claims also require that current session's valid goal fingerprint; without an aligned goal, record only partial-verification, blocked, or failed work. A normal Stop event only emits a non-blocking claim reminder; it does not force a passed claim or start a follow-up loop. The Stop hook becomes fail-closed only when the host explicitly marks a completion workflow (for example `completion_workflow: true`, `completion_status: completed`, or `TENETORA_REQUIRE_COMPLETION_CLAIM=1`). Blocked, failed, partial, and awaiting-input statuses remain reportable without fabricating a passed claim.

## Decision Alignment

`tenetora-align` is the explicit user entry. It asks one material engineering decision at a time, supports pause/resume, and binds the handoff to a goal fingerprint, scope, non-goals, and acceptance criteria.

Use the alignment guard for high-risk or materially ambiguous work:

```bash
tenetora guard --action alignment --goal "<goal>" --risk-level high --path .
```
Confirmation ends alignment; it does not start planning or implementation. Waiting for confirmation has a seven-day review lease; a matching owner may resume it, but must re-enter awaiting-confirmation before confirming. Expired sessions without running attempts or pending confirmation are historical and non-blocking; sessions with running attempts or pending user confirmation remain actionable and require explicit session identity.

Confirmed handoffs have a separate lifecycle: completion claims mark goals `completed`, while `tenetora alignment --archive --session-id <session-id> --owner-id <owner-id>` closes a host-archived or deleted conversation. The local runtime cannot observe host deletion, so open goals expire after 30 days without owner activity; automatic lookup uses only open goals. Multiple matches create a short-lived, prompt-bound request for the host model to select one semantically clear target, while an absent, stale, unsafe, or ambiguous selection remains fail-closed. Routing is context selection only, not confirmation or authorization; terminal lifecycle/history is pruned to 64/128 records or 90 days while retained handoffs remain explicitly readable.

## End-to-End Skeleton First

Implementation planning loads the `planning` context and records whether the workflow is `applicable` or `exempt`. Applicable plans identify boundaries, establish a minimum safe end-to-end path, define its verification gate, and defer non-blocking refinement until the skeleton passes.

## Change-impact Preflight

Use structural analysis before changing a constructor, public API, interface, schema, CLI, hook, plugin manifest, runtime contract, or widely referenced symbol:

```bash
tenetora impact --start --path . \
  --kind public-api --summary "<bounded change>" \
  --impact-tool <codegraph-or-compiler> --symbol "<symbol>" \
  --affected-file <relative-path> --reference-count <count> \
  --baseline-command "<command>" --baseline-status passed
tenetora impact --status --path .
tenetora impact --complete --path . \
  --rescan-command "<command>" --rescan-status passed \
  --verification-command "<command>" --verification-status passed
```

The commit detector is a heuristic backstop, not a substitute for pre-edit impact analysis.

## Bounded Loop

Loop execution starts only from a current user message or explicit routing. State alone never starts background work. Each cycle has a goal, exit criteria, next prompt, and bounded retry/review counts.

```bash
tenetora loop-state --json --path .
```

## Portable Subagent Governance

Match roles by responsibility: `code-reviewer`, `security-auditor`, `codebase-scout`, or the review-cycle-only `implementer`. Resolve in this order:

1. Qualified dedicated agent.
2. Qualified built-in host agent with rendered project role contract.
3. Main-agent self-review marked `not independently reviewed`.

The CLI never spawns the agent. Before fallback, the model inspects the current host's visible and deferred/discoverable dispatch tools; missing Hook evidence is not dispatch-unavailability evidence. A governed code review is bound to the canonical Git tree and expected parent commit set, and becomes stale after the reviewed index or intervening history changes. Successful `SubagentStop` proves process completion only; the model must inspect the report and explicitly record `passed` or `blocker`. Security reviewers and implementers require real permission isolation; prompt-only boundaries are insufficient.

Review and fix results can also record bounded follow-up metadata: whether a finding was confirmed, whether
the repair regressed, and the verification status. `--verification-command` stores only a hash and length in
governance metadata; keep the readable command and report details in the controlled report cache. A completed
host process is not treated as a passed review without an explicit result.
