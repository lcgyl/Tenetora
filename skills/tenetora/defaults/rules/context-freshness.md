# Context Freshness Rules

Source: Tenetora default rules.

- Treat `.tenetora/README.md`, `.tenetora/INDEX.md`, and the relevant `.tenetora/rules/` files as runtime inputs, not one-time setup notes.
- After context compaction, interruption, tool restart, or a long verification run, reread the runtime entry files before continuing.
- When a lifecycle skill is involved, run the skill Freshness Gate and follow `tenetora skill-instructions --skill <name>` if it differs from cached text.
- Do not rely on old state summaries as ground truth. Verify against source files, current evidence, and command output.
- If `.tenetora/state/current-evidence.json` points to stale or missing evidence, update or audit `.tenetora` before using derived project facts.
