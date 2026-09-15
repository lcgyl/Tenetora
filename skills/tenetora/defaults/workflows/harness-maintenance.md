# Harness Maintenance Workflow

Source: Tenetora default workflow.

1. Run `tenetora status --tools auto` before changing installed skills.
2. Run `tenetora upgrade --check` before applying a skill package update.
3. Run `tenetora repair --check` after upgrading from an older Tenetora version; apply only concrete repairs, and treat skipped `CLAUDE.local.md` recovery as requiring user-provided backup or manual confirmation.
4. If repair reports `claude-entrypoint`, migrate shared Claude rules into `.tenetora/agents/claude.md` and keep root `CLAUDE.md` as a thin adapter.
5. Run `tenetora validate`, `tenetora audit --strict`, and `tenetora run-all` after `.tenetora` changes.
6. Record successful validation with `tenetora recap --write`.
