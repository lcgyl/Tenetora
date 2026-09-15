# Decision Interview Guide

Source: Tenetora default skill notes.

Use the installed `tenetora-decision-interview` skill as the runtime authority. This project-local guide keeps the consumption path visible when a tool cannot invoke skills directly.

- Separate repository facts, user decisions, and visible assumptions.
- Investigate facts directly; ask only decisions that materially change scope, architecture, risk, approval boundaries, rollback, or acceptance criteria.
- Ask one question at a time and stop after 8 questions for a checkpoint.
- Check `tenetora alignment --status --session-id <id> --owner-id <owner> --json` first; do not conduct an untracked interview or inherit an unscoped conflict.
- Use `tenetora alignment --list --json` to inspect candidates, and require explicit session and owner selection before resuming or mutating state.
- The eighth recorded answer enters checkpoint automatically. At earlier semantic convergence, use `--await-confirmation`.
- Never answer for the user, manufacture consensus, or silently accept risk.
- Persist concise summaries through `tenetora alignment`; do not save full conversation or hidden reasoning.
- Return a handoff to the lifecycle caller. Do not plan, implement, commit, push, deploy, release, or start background work.

See `.tenetora/workflows/decision-alignment.md` and `.tenetora/templates/alignment-handoff.md`.
