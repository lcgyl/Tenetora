# Agent Control Rules

Source: Tenetora default rules.

- Keep durable project rules in `.tenetora/rules/`; keep root tool files as thin routing entries.
- Keep `CLAUDE.local.md` as a thin local adapter after accepted entrypoint governance. If `.tenetora/` is ignored, local Claude content lives in `.tenetora/agents/claude-local.md`; if `.tenetora/` is tracked, local Claude content lives in ignored `.tenetora/local/agents/claude-local.md`.
- Do not let an AI tool silently rewrite `AGENTS.md`, `CLAUDE.md`, `CLAUDE.local.md`, `.cursor/rules/*`, or `.claude/rules/*` without backup.
- Do not overwrite unrelated user changes.
- Do not keep working after the same blocker repeats without new evidence.
- Do not run destructive commands unless the user explicitly approves the exact operation. Treat commit and push as separate actions and require explicit user authorization for each affected repository.
- If the task needs credentials, private local settings, or product intent that is not present, stop and ask instead of guessing.
