# Decision Interview Guide

Source: Tenetora default skill notes.

Use the installed `tenetora-decision-interview` skill as the runtime authority. This project-local guide keeps the consumption path visible when a tool cannot invoke skills directly.

Material choices apply in every task mode, not only Plan mode. Ask one direct
question with a recommended option, at least one alternative, and a custom option;
pause until the user answers. A native synchronous selector is only an
optional host presentation. Do not use Codex queued follow-up inputs or start
a blocking terminal picker as a fallback.

- Separate repository facts, user decisions, and visible assumptions.
- Investigate facts directly; ask only decisions that materially change scope, architecture, risk, approval boundaries, rollback, or acceptance criteria.
- Ask one question at a time and stop after 8 questions for a checkpoint.
- Ordinary material-choice questions do not require an alignment session or renewed task permission. Ask directly under the current task authorization and record the decision in its plan/handoff.
- For tracked alignment interviews only, check `tenetora alignment --status --session-id <id> --owner-id <owner> --json` first; never inherit an unscoped conflict.
- Use `tenetora alignment --list --json` to inspect candidates, and require explicit session and owner selection before resuming or mutating state.
- In a tracked alignment interview, the eighth recorded answer enters checkpoint automatically. At earlier semantic convergence, use `--await-confirmation`. Ordinary questions use a conversational checkpoint without writing alignment state.
- Never answer for the user, manufacture consensus, or silently accept risk.
- Persist tracked interview summaries through `tenetora alignment`; ordinary decisions belong to the task plan/handoff. Do not save full conversation or hidden reasoning.
- Return a handoff to the lifecycle caller. Do not plan, implement, commit, push, deploy, release, or start background work.

See `.tenetora/workflows/decision-alignment.md` and `.tenetora/templates/alignment-handoff.md`.
