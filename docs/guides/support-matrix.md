# Support Matrix

Tenetora reports platform capability separately from installation presence. The
tiers below describe the maintenance promise for each integration; they do not
upgrade a platform's runtime capability.

| Tier | Platforms | Maintenance promise | Current capability boundary |
| --- | --- | --- | --- |
| Tier 1 | Claude Code, Codex | CI contract coverage and release blocking for shared adapter behavior | `active`; Codex may require trust review |
| Tier 2 | Cursor, OpenCode, Pi, ZCode, DeepSeek Harness | Maintained adapters and focused regression coverage; host changes may be released with a documented limitation | Cursor/ZCode `active`; OpenCode/Pi/DSH `active-partial` |
| Tier 3 | Generic Agents | Skills and CLI compatibility; no host runtime contract is promised | `skills-only` |

Use `tenetora status --tools all --scope both` and `tenetora doctor --tools all --scope both` for the current machine. A platform is considered observed only when its host emits the corresponding runtime evidence; package presence alone is not activation evidence.

When changing a Tier 2 adapter, record the host, version, event or hook path,
and the verification date in the change report. A new host requires an explicit
tier decision before it is added to the supported-platform table.
