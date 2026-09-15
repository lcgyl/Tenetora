# Runtime and Platforms

**English** | 简体中文

Tenetora shares lifecycle skills and CLI behavior across AI tools, but it does not assume that every host provides the same plugin, Hook, or session API. Platform facts are maintained by the packaged platform contract and boundary tests.

## Capability Matrix

| Platform | Distribution | Runtime coverage | Maximum state |
| --- | --- | --- | --- |
| Claude Code | User-scoped native plugin, hooks, and skills | Session, prompt, tool, subagent observation, stop | `active` |
| Codex | User-scoped native plugin, hooks, and skills | Session, shell guard, stop | `active` or `needs-trust-review` |
| Cursor | Skills and merged `hooks.json` | Session, shell guard, stop | `active` |
| OpenCode | Skills and auto-loaded JS plugin | Context and pre-tool Git guard | `active-partial` |
| Pi | Skills and auto-loaded TypeScript extension | Session, prompt, compaction, pre-tool Git guard | `active-partial` or `needs-trust-review` |
| ZCode | User-scoped registered plugin and process hooks | Session, prompt, tool, stop | `active` |
| Generic Agents | Skills and CLI | No portable Hook standard | `skills-only` |

`active` means the configuration satisfies the platform contract; it does not prove that a host has executed an event. `active-partial` honestly records that OpenCode and Pi do not expose the full mandatory stop/subagent contract used by the stronger integrations. `skills-only` is the capability boundary when no portable runtime exists.

## Inspect the Effective Source

An installation directory is not proof that a host is using it:

```bash
tenetora status --tools all --scope both --path .
tenetora doctor --tools all --scope both --path .
tenetora status --tools all --scope both --path . --json
```

Reports separate lifecycle skills, plugin registration, runtime adapter, version, evidence level, and remediation. Common states include:

- `active`: configuration and capability satisfy the platform contract;
- `covered`: this scope has the lifecycle package while another scope owns the executable runtime;
- `needs-trust-review`: the host needs trust confirmation or restart;
- `runtime-not-observed`: configuration exists but no recent event is recorded;
- `observed-source-mismatch`: recent evidence came from a non-effective source, so full activation cannot be claimed;
- `BLOCKED`: duplicate runtime, unknown ownership, or an unsafe-to-verify state was found.

Evidence levels, strongest first, are `runtime-observed`, `host-api-verified`, `config-verified`, `inferred`, `file-present`, and `unknown`. Runtime observations remain local and store a project hash, source, version, event, and time; prompts, tool payloads, absolute paths, and secrets are not stored.

## One Runtime per Platform and Project

Lifecycle skills may exist in both global and project scopes, but two executable runtime adapters must not run for the same platform and project.

- `--both` can install both skill scopes, while Cursor, OpenCode, and Pi still retain one runtime owner.
- Codex native plugin Hooks and project fallback Hooks are mutually exclusive.
- A healthy runtime in the other scope is reused while the selected scope can refresh skills.
- A duplicate runtime is `BLOCKED`, not extra protection.
- Shadow cleanup removes only an unmodified adapter matching a known Tenetora release template.

## Host Notes

### Claude Code

The native plugin is user-scoped; a project install supplies lifecycle skills only. Restart Claude Code after plugin updates. Hooks can provide context, Git/external-input protection, subagent observation, and completion follow-up.

### Codex

After first installation or a Hook-definition change, trust the current Tenetora definition in `/hooks` and restart when requested. Native and project fallback modes are mutually exclusive. When marketplace or Hook inventory is damaged, ordinary `upgrade` performs bounded recovery only with clear ownership evidence; unknown configuration remains blocked. See [Installation and Upgrade](installation.md) for legacy Agent Harness recovery.

### ZCode

The native plugin is user-scoped and the host registry is authoritative for registration. If the canonical cache is valid but registration is missing, upgrade rebuilds the registration without recopying the payload. Doctor does not report full activation unless registration, payload, enabled state, and Hooks are verifiable.

### Cursor

The installer merges only Tenetora-owned Hook entries and preserves user hooks. The runtime covers session context, shell Git guard, and completion follow-up.

### OpenCode

The auto-loaded JS plugin provides context and a pre-tool Git guard. Because there is no equivalent mandatory stop event, the maximum honest state is `active-partial`.

### Pi

The auto-loaded TypeScript extension is installed at `~/.pi/agent/extensions/tenetora.ts` for a user scope or `.pi/extensions/tenetora.ts` for a project scope. The global extension can load automatically; a project extension remains trust-gated by Pi and is reported as `needs-trust-review` until that host-side decision is observable. The extension supplies session and prompt context, refreshes context before compaction without replacing Pi's own summary, and applies the pre-tool Git guard. Pi does not expose the full mandatory stop and subagent contract, so the maximum honest state is `active-partial`. Hook calls are bounded; prompt/context failures degrade without terminating the turn, while an unavailable runner blocks mutating, command, and unknown tools. A small read-only allowlist may continue.

### Generic Agents

Generic Agents receive lifecycle skills and CLI only. A skills directory does not prove that runtime Hooks executed.

## Runtime CLI Contract

Each host must bootstrap the CLI from the exact Tenetora skill it loaded and use the returned absolute command. Do not borrow a skill directory from another host or persist a tool-local `PYTHONPATH`. An older project skill may delegate forward to the managed current release, but it must not downgrade the CLI shim.

## Activation Check

An installation receipt proves installation state, not event execution. Restart the host, run a supported smoke action, and use `doctor` to check for a matching recent runtime observation. Plugin inactivity limits Tenetora observation and reminders; it is not evidence that native host subagents are absent.
