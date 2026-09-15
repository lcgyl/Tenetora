# Migration Patterns

## New repository

1. Detect project type and tools.
2. Ask the user which AI tools to support if detection is ambiguous.
3. Ask whether generated `.tenetora/` should be tracked or ignored by Git. Do not decide silently in non-interactive AI runs.
4. Create `.tenetora/` with the baseline layout.
5. Create a thin `AGENTS.md` unless the user declines.
6. Optionally create compatibility notes for detected tools.
7. Validate structure and secret hygiene.

## Existing repository

1. Run read-only audit first.
2. Identify existing rule sources:
   - `AGENTS.md`
   - `CLAUDE.md`
   - `CLAUDE.local.md`
   - `.claude/rules/*`
   - `.cursor/rules/*`
   - `.mcp.json`
   - docs or reports that act as rules
3. Classify each source:
   - project-level tracked rule
   - local/private config
   - historical report
   - generated artifact
4. Ask whether `.tenetora/` should be tracked or ignored. Use ignored for local generated baselines only after the user confirms; use tracked when the user wants project-shared harness context.
5. Ask how to handle each existing rule or skill. Offer `ignore`, `plan`, `transfer`, and `merge`.
6. Create `.tenetora/` as the shared source.
7. Convert old entries into thin compatibility entries only after user approval.
8. Do not delete old tool-specific entries unless the user asks.

## Migration actions

| Action | Behavior |
| --- | --- |
| `ignore` | Leave the source untouched and do not include it in `.tenetora/`. |
| `plan` | Record the source, target, and suggested action in `.tenetora/changes/YYYY-MM-DD-migration-plan.md`. |
| `transfer` | Leave the source untouched and create a standalone migrated file in the corresponding `.tenetora/` directory. |
| `merge` | Leave the source untouched and append the source content, with provenance metadata, into the corresponding `.tenetora/` target file. |

Never copy raw content from files that appear to contain tokens, passwords, private keys, or local-only MCP configuration. For those, record only provenance and risk notes.

`CLAUDE.local.md` is a valid Claude Code local entrypoint and should not remain as a substantive root file after accepted entrypoint governance. Migrate its content with provenance, then rewrite the root file as a thin adapter. If `.tenetora/` is ignored, use `.tenetora/agents/claude-local.md`; if `.tenetora/` is tracked, use `.tenetora/local/agents/claude-local.md` and ensure `.tenetora/local/` is ignored.

## Tool choice

Support detected tools by default. If a tool is installed locally but the project has no config for it, add only a compatibility note, not tool-specific generated config.

Supported baseline tools:

- `generic`: always include.
- `codex`: include when `AGENTS.md`, `~/.codex`, or `codex` is present.
- `claude`: include when `CLAUDE.md`, `CLAUDE.local.md`, `.claude/`, `~/.claude`, or `claude` is present.
- `cursor`: include when `.cursor/`, Cursor app traces, or `cursor` is present.
- `opencode`: include when `opencode` command or config is present.
- `pi`: include when `.pi/`, `~/.pi/agent`, or `pi` is present. Use the official `.pi/extensions/` or `~/.pi/agent/extensions/` location for the managed TypeScript extension; do not infer full runtime coverage from skill installation.
- `zcode`: include when `.zcode/`, `~/.zcode`, `ZCODE_HOME`, ZCode app traces, or `zcode` is present. Prefer native `.zcode-plugin/plugin.json` for package distribution; use `.zcode/skills` only as installer fallback.

## Overwrite policy

- Default: never overwrite existing files.
- If a file exists, write a warning and leave it unchanged.
- Use `--update-strategy diff` to refresh deterministic extraction evidence, write low-risk non-semantic patches, and write semantic `.tenetora` Markdown/Text changes under `.tenetora/changes/update-candidates/`.
- Use `--update-strategy backup` when the user wants generated files refreshed with a recoverable backup under `.tenetora/changes/backups/`.
- Use `--update-strategy merge` when the user wants deterministic extraction evidence refreshed while semantic `.tenetora` Markdown/Text and non-Markdown differences are written under `.tenetora/changes/update-candidates/`.
- Use `--update-strategy replace` only after explicit user approval.
- For `review --apply`, prefer `--strategy merge --migrate merge` when the user wants existing project rules collected into `.tenetora` without losing current harness content.
- Use `--force` only as a compatibility direct-overwrite switch after the user approves.
- Use `--write` to perform changes; without it, scripts dry-run.
- Use `--gitignore ask` only in an interactive terminal. Non-interactive AI or CI runs must ask the user first and then pass `--gitignore yes` or `--gitignore no`.
- Use `--migrate ask` only in an interactive terminal. Non-interactive AI or CI runs must ask the user first and then pass `--migrate plan|ignore|transfer|merge`.
- Use `--migrate merge` only after the user accepts that generated `.tenetora/` files will include content copied from existing sources.
- Global skill migration is opt-in. Use `--include-global-migration-scan` only when the user explicitly wants user-level skills considered for this project.
- Repair and refresh must preserve the same local entrypoint policy used by init; do not downgrade `CLAUDE.local.md` to an unmanaged private file during update.
