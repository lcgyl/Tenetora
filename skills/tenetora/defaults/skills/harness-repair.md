# Harness Repair Notes

Source: Tenetora default skill notes.

Use this note when `.tenetora` exists but older Tenetora behavior left known compatibility issues behind.

## Checks

- `CLAUDE.local.md` should remain local-only and route to the target implied by `.tenetora` git policy: `.tenetora/agents/claude-local.md` when `.tenetora/` is ignored, or ignored `.tenetora/local/agents/claude-local.md` when `.tenetora/` is tracked.
- Tool entry files should be backed up before being rewritten as thin adapters.
- A root `CLAUDE.local.md` that is already a thin adapter still needs repair when the `.tenetora` target only contains omitted placeholder text instead of recovered local rules.
- If no earlier `CLAUDE.local.md` backup exists, do not claim automatic repair; ask for manual recovery or user confirmation.
- Repair actions should write a report under `.tenetora/changes/`.

## Command

```bash
tenetora repair --check
tenetora repair --apply --fix all
```
