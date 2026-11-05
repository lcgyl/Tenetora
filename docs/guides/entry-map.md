# Lifecycle Entry Map

Cross-host review: [rotating handoff](review-handoff.md).

Use the smallest lifecycle entry that matches the work. These entries are
compatible views over the same `.tenetora/` record; they do not create separate
governance systems.

| Need | Entry | First command or action | Evidence boundary |
| --- | --- | --- | --- |
| The project has no harness or needs a new opt-in profile | `tenetora-init` | `tenetora init --write` or `--profile minimal` | initialization report and validation |
| Existing rules, skills, or defaults changed | `tenetora-update` | inspect update preview, then apply the selected scope | update diff, validation, strict audit |
| A request is unclear or crosses a shared contract | `tenetora` + `tenetora-plan` | route, load the rule slice, then freeze a plan | impact record and skeleton gate |
| A high-risk user-owned decision is unresolved | `tenetora-align` | start an owner-bound alignment | confirmed handoff; it does not authorize code or Git actions |
| Work is already aligned and needs bounded continuation | `tenetora-loop` | execute one check-fix-verify step | targeted verification claim |
| External text, reports, pages, or scripts are involved | `tenetora-prompt-guard` | run the external-input guard first | redacted findings or explicit pass |
| Final readiness or harness quality is in question | `tenetora-audit` | validate, run guardrails, then strict audit | audit report and remaining warnings |

For code review, use the portable `code-reviewer` contract through
`tenetora delegation`. The CLI records the dispatch lifecycle but never starts
the host agent. A rejected or unavailable host is recorded as review debt and
must remain visible to release policy; a self-review is not silently promoted
to an independent pass.

Every implementation report should identify the changed files, verification
command, evidence level, and whether the evidence is static, mocked, or real.
Completion claims require the current owner-bound alignment and a fresh
whole-project verification claim. A partial claim is suitable for a scoped
step but cannot satisfy completion.

Do not add a new lifecycle entry for a synonym. Extend an existing entry only
when the new behavior has a distinct boundary, evidence contract, and testable
failure mode.
