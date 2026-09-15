# Harness Governance Rules

Source: Tenetora default rules.

- Treat `.tenetora/` as the shared AI-agent system of record.
- Keep root tool files such as `AGENTS.md`, `CLAUDE.md`, `.cursor/rules/*`, and tool-local equivalents as thin compatibility entries whenever possible.
- Do not duplicate stable rules in multiple tool entry files; move shared rules into `.tenetora/rules/`, `.tenetora/workflows/`, or `.tenetora/wiki/`.
- User-authored and migrated project rules take precedence over Tenetora defaults.
- Use update candidates for semantic changes that may replace or merge human-authored content.
- After changing durable `.tenetora` content, run validate, audit, and guardrails before claiming the harness is healthy.
