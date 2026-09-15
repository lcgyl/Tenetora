# Tool Config Registry

Updated: 2026-07-28

This registry records AI-tool instruction and configuration entrypoints that Tenetora must recognize during init, update, audit, and rule capture. Treat it as a conservative compatibility table, not as a replacement for each tool's official documentation.

## Common Policy

- Stable team rules belong in `.tenetora/rules/`, `.tenetora/workflows/`, `.tenetora/agents/`, or `.tenetora/wiki/`.
- Tool-native files should become thin adapters when the user chooses `--entrypoints merge`.
- Local or personal instruction files must be backed up and listed, but their content should not be copied into shared harness files unless the user explicitly asks.
- Tool configuration files may contain permissions, paths, commands, or credentials. Migration records should keep provenance and omit raw content by default.
- Tool-private local config, such as `.claude/settings.local.json`, is skipped from migration plans, evidence, backups, and shared docs.

## Supported Entrypoints

| Tool | Entrypoint | Type | Default Harness Handling |
| --- | --- | --- | --- |
| Generic agents | `AGENTS.md` | Project instruction | Migrate or merge into `.tenetora/agents/generic.md`, then keep root file thin. |
| Codex | `AGENTS.md` | Project instruction | Same as generic. Codex also supports nested `AGENTS.md`. |
| Codex | `AGENTS.override.md` | Override instruction | Treat as high-priority agent guidance; migrate with provenance. |
| Codex | `.codex/config.toml` | Tool config | Record in `.tenetora/wiki/tool-config.md`; omit raw content by default. |
| Codex | `.codex/rules/*.rules` | Tool permission rules | Record as tool config, not general project rules. |
| Codex | `.codex-plugin/plugin.json` | Native plugin manifest | Install through the Codex marketplace/plugin CLI when available; bundled hooks are active only when Codex `hooks/list` reports the current definitions trusted. |
| Codex | `hooks/hooks.json` | Runtime hooks source | Uses Codex default plugin hook discovery with `${PLUGIN_ROOT}`. Includes bounded `SubagentStart`/`SubagentStop` observation; hooks never spawn agents. Hosted tools such as `WebSearch` do not enter the local tool-hook path. Windows uses the bundled `run-hook.cmd`. |
| Codex | `.codex/hooks.json` | Project Hook fallback | Use only after explicit project authorization when native plugin management is unavailable and no Tenetora native Hook remains enabled. Merge managed entries structurally, preserve user Hook content, back up before writes, keep ownership state outside the repository, and use the self-contained stable machine runtime under `~/.tenetora/runtime/`. |
| Claude Code | `CLAUDE.md` | Project instruction | Migrate or merge into `.tenetora/agents/claude.md`, then keep root file thin. |
| Claude Code | `CLAUDE.local.md` | Local-personal instruction | Detect, back up, and mark as `local-agent`; route root file as a thin adapter. If `.tenetora/` is ignored, store local content in `.tenetora/agents/claude-local.md`; if `.tenetora/` is tracked, store local content in ignored `.tenetora/local/agents/claude-local.md`. |
| Claude Code | `.claude/CLAUDE.md` | Project instruction | Migrate with provenance into `.tenetora/agents/claude.md`. |
| Claude Code | `.claude/rules/*` | Path or tool-specific rules | Migrate into `.tenetora/rules/`, then keep rule files thin if `--entrypoints merge` is chosen. |
| Claude Code | `.claude/settings.json` | Tool config | Record in `.tenetora/wiki/tool-config.md`; omit raw content by default. |
| Claude Code | `.claude/settings.local.json` | Local tool config | Skip from shared migration plans, evidence, backups, and docs. |
| Claude Code | `.claude-plugin/plugin.json` | Native plugin manifest | Install through the Claude Code marketplace/plugin CLI at user scope. |
| Claude Code | `hooks/hooks-claude.json` | Runtime hooks source | The Claude plugin manifest points to this file; use command hooks with `${CLAUDE_PLUGIN_ROOT}` for session, prompt, tool, external-input, `SubagentStart`/`SubagentStop`, and Stop events. |
| Cursor | `.cursor/rules/*.mdc` | Structured project rules | Migrate into `.tenetora/rules/`; keep `.mdc` metadata in adapter when possible. |
| Cursor | `AGENTS.md` | Plain instruction | Supported in root and subdirectories; keep as thin adapter to `.tenetora`. |
| Cursor | User Rules / Team Rules | External tool state | Do not migrate automatically; document only if the user provides exported content. |
| Cursor | `.cursor/hooks.json` / `~/.cursor/hooks.json` | Runtime hooks | Merge only Tenetora managed `sessionStart`, `beforeShellExecution`, and `stop` entries; preserve unrelated user hooks. Global/project adapters are mutually exclusive for one project. |
| OpenCode | `AGENTS.md` | Project instruction | Keep as thin adapter to `.tenetora`. |
| OpenCode | `opencode.json` | Tool config and instruction includes | Record config and referenced instruction files; omit raw config content by default. |
| OpenCode | `opencode.toml` | Tool config | Record as tool config if present. |
| OpenCode | `.opencode/plugins/*.js` / `${XDG_CONFIG_HOME:-~/.config}/opencode/plugins/*.js` | Runtime plugin | Install an auto-loaded JS plugin for system context, compaction context, and pre-tool Git guards. Report `active-partial` because no equivalent enforced Stop event is available. Global/project adapters are mutually exclusive for one project. |
| Pi | `AGENTS.md` | Project instruction | Keep as a thin adapter to `.tenetora`. |
| Pi | `.pi/skills/*` / `~/.pi/agent/skills/*` | Lifecycle skills | Install lifecycle skills in the official project or user skill directory. Skill presence alone does not prove runtime activation. |
| Pi | `.pi/extensions/*.ts` / `~/.pi/agent/extensions/*.ts` | Runtime extension | Install the managed TypeScript extension for session, prompt, compaction, and pre-tool Git boundaries. Global loading is automatic; project loading is Pi-trust-gated and must remain `needs-trust-review` until observed. Project and global adapters are mutually exclusive for one project. |
| ZCode | `.zcode-plugin/plugin.json` | Native plugin manifest | User-scoped only. Install under `~/.zcode/cli/plugins/cache/tenetora-local/tenetora/<version>`, register as `tenetora@tenetora-local` in `cli/plugins/installed_plugins.json`, and enable through `cli/config.json -> plugins.enabledPlugins`. A proven `agent-harness@agent-harness-local` registration is removed during upgrade. Project scope installs skills only under `<project>/.zcode/skills`. |
| ZCode | `hooks/hooks-zcode.json` | Runtime hooks source | Installed as the plugin's `hooks/hooks.json`; uses `type: process`, `${ZCODE_PLUGIN_ROOT}`, `args`, and `timeoutMs`, including observed subagent lifecycle events when the host emits them. |
| ZCode | `.zcode/skills/*` | Installed skills fallback | Use only when the native plugin package source is unavailable; fallback does not prove hooks are active. |

## Platform Capability Contract

`hooks/platform-contracts.json` is the machine-readable source of truth for supported platform capability. Runtime code and tests consume it; this prose explains the policy but must not redefine a conflicting matrix.

| Tool | Maximum | Default scope | `--both` runtime | PATH | Trust | Observation |
| --- | --- | --- | --- | --- | --- | --- |
| Generic Agents | `SKILLS_ONLY` | global | global | host-dependent | none | no |
| Codex | `ACTIVE` | global | global native plugin | restricted | `hooks/list` | yes |
| Claude Code | `ACTIVE` | global | global native plugin | standard | plugin registry | yes |
| Cursor | `ACTIVE` | global | project preferred, existing single owner retained | restricted | workspace trust | yes |
| OpenCode | `ACTIVE_PARTIAL` | global | project preferred, existing single owner retained | standard | auto-loaded plugin directory | yes |
| Pi | `ACTIVE_PARTIAL` | global | global preferred, existing single owner retained; project loading is trust-gated | standard | global auto-load / project trust | yes |
| ZCode | `ACTIVE` | global | global native plugin | standard | `enabledPlugins` | yes |

The no-scope installer default remains global for backward compatibility. `both_runtime_preference` applies only when both skill scopes are explicitly requested and no existing single runtime owner must be retained. New platforms or events require a contract entry before implementation, and every contract dimension must map to a deterministic test or an explicit `unknown`/unsupported declaration.

Status health uses explicit tool selection, command availability, configured runtime and recent/stale observation to determine relevance. A dormant file/cache without an available host, enabled runtime or observation remains visible as a dormant finding but does not lower overall health.

## Runtime State Contract

| State | Meaning |
| --- | --- |
| `active` | Adapter payload exists in the official load path, is current, enabled, and its launcher passes the restricted-PATH `--check` runtime probe. For Codex, all current plugin hook definitions must also be reported trusted by `hooks/list`. This is configuration readiness, not proof that an unopened client process already fired an event. |
| `needs-trust-review` | Plugin and hook payload are installed, but one or more current Codex hook definitions are untrusted or modified and require `/hooks` review. |
| `active-native-degraded-management` | Codex still loads a healthy, trusted Tenetora native Hook, but marketplace/plugin state is unreadable. Runtime governance remains active; native install, upgrade, and removal are degraded. Do not add project fallback. |
| `active-project-fallback` | Codex loads the trusted project-level Tenetora Hook from `.codex/hooks.json`, and the stable machine launcher passes its runtime probe. Marketplace state may remain degraded. |
| `pending-project-trust` | The project fallback is installed locally, but Codex has not yet reloaded or trusted the current project Hook definitions. |
| `duplicate-hook-runtime` | Native and project Tenetora Hooks are both enabled. Treat this as a conflict, not as extra coverage; disable one source before continuing. |
| `hook-inventory-unavailable` | Codex `hooks/list` could not provide an authoritative inventory. Do not infer that native Hooks are absent and do not enable project fallback until inventory is restored. |
| `active-partial` | The verified adapter is auto-loaded but the platform lacks one or more governance events, such as an enforced Stop hook. |
| `inactive` | Adapter is missing, stale, invalid, disabled, or unregistered. |
| `skills-only` | Lifecycle skills/CLI are available but there is no verified runtime hook adapter. Never report this as hooks active. |

## Effective Source Evidence Contract

`status` and `doctor` resolve three independent layers: model-callable skill, host-registered plugin, and executable runtime. Every candidate records scope, path, version, state and basis; each layer separately reports `effective`, `shadowed` and `remediation`. Do not infer a skill winner from a plugin registration or infer runtime execution from a Hook file.

| Basis | Meaning |
| --- | --- |
| `runtime-observed` | A Hook from the same platform/project/source fingerprint executed within the 24-hour observation window. |
| `host-api-verified` | The host API or registry confirms the current registration, enablement or trust state. |
| `config-verified` | Structured configuration is valid and enabled, but the host exposes no execution proof. |
| `inferred` | The likely source is derived from a known discovery order, not a host runtime query. |
| `file-present` | Only file presence is proven. |
| `unknown` | No reliable claim can be made. |

Runtime observations are machine-local under `~/.tenetora/state/runtime-observations/<platform>/<project-hash>.json`. They store only schema, platform, project hash, source fingerprint, package version, event and timestamp. They never enter `.tenetora` and never store an absolute project path, prompt, tool input/output or secret. Observation states are `recent`, `stale`, `not-observed`, `observed-source-mismatch`, `observed-conflict` and `invalid`.

## Single Active Runtime Contract

- Multiple skill copies are allowed; multiple executable Tenetora runtimes for the same `(platform, canonical-project)` are not.
- Codex native and project fallback candidates are resolved together. If both may execute, report `duplicate-hook-runtime`, set the legacy runtime field inactive and require the user to disable one source.
- Cursor/OpenCode/Pi `--both` installs skills in both scopes but chooses one runtime. Cursor and OpenCode retain their existing preference; Pi prefers the global adapter because project loading is trust-gated. With one prior adapter, its scope is retained.
- Existing global/project duplicates block by default. `--prune-shadowed` removes only Tenetora managed entries whose current or historical release template exactly matches. Missing historical source, modified managed content, invalid JSON or another owner's file fails closed.
- Pruning never deletes unrelated Cursor hooks, OpenCode plugins, native plugin caches, registries, trust state or user configuration.

## Hook Output Schema Contract

- Every supported host event is registered in an explicit platform-by-event allowlist before the Hook may emit fields for it.
- Codex Stop uses only Codex-supported top-level fields and never emits `hookSpecificOutput`.
- Claude Code, ZCode and OpenCode retain `hookSpecificOutput` only for registered context/permission events and filter nested fields per event. Pi maps its extension callbacks through the same registered SessionStart, UserPromptSubmit, and PreToolUse contract.
- Cursor maps registered payloads to native `additional_context`, permission or `followup_message` shapes.
- Unknown platforms/events receive only the minimum common top-level fields or an empty payload; extension fields are never passed through unchanged.

## Hook Launcher Zero-PATH Contract

- Unix launchers use an absolute `/bin/sh` shebang and shell builtins for package-path resolution; they must not depend on `env`, `bash`, `dirname`, `sed`, or other PATH-resolved utilities.
- Interpreter resolution order is explicit `TENETORA_PYTHON`, the installer-managed `${TENETORA_HOME:-~/.tenetora}/runtime/python` pointer, conservative stable absolute paths, then PATH lookup as the final fallback. Legacy variable names are migration evidence only and are never active runtime inputs.
- The stable runtime atomically carries `hooks/`, the internal Python package `cli/tenetora/`, and the Hook-required canonical `skills/tenetora/` payload. Runtime health requires all three at the current version; a Hook-only or legacy-skill payload is stale and must be repaired by install/upgrade before project fallback is accepted.
- Windows launchers use the same explicit/runtime-pointer ordering and invoke `%SystemRoot%\\System32\\where.exe` by absolute path only for the final fallback.
- `hooks/run-hook --check` validates the interpreter without executing governance hook logic. `status` and `doctor` run this probe with an empty PATH before claiming a runtime is active.
- Failure diagnostics include the managed pointer, current PATH, and the explicit recovery action; launcher code must never activate or silently translate a legacy environment override.

## Codex Native/Project Hook Contract

- `hooks/list` is the authoritative inventory for both native plugin Hooks (`source: plugin`) and project Hooks (`source: project`). Marketplace listing failure must not erase already loaded Hook facts.
- `--codex-hooks auto` never silently writes `.codex/hooks.json` in non-interactive use. It reports the explicit `--codex-hooks project` recovery command when fallback is eligible.
- `--codex-hooks project` is blocked while any Tenetora native Hook remains enabled, even if its launcher is unhealthy. The user must disable the native source in `/hooks` first.
- `--allow-tracked-codex-hooks` authorizes only a structured merge into a tracked project file. It does not authorize replacing invalid JSON, deleting user Hooks, changing trust, or modifying marketplace/cache data.
- `--codex-hooks off` removes only entries matching the machine-local ownership fingerprint. If managed entries changed after installation, removal fails closed instead of restoring an old full-file backup.
- Tenetora never uses `--dangerously-bypass-hook-trust` and never edits Codex marketplace snapshots, plugin registries, caches, or trust records.
- Global install/upgrade prepares the stable runtime only. Project configuration is created by an explicit CLI request or an interactive init/update choice; lifecycle skills must call the CLI rather than edit `.codex/hooks.json` directly.
- Project fallback currently requires the default `~/.tenetora` home because project commands use that stable cross-release path. Custom `--home` installs fail closed for `--codex-hooks project`; the generated CLI shim preserves its configured home for all other commands.

## Portable Subagent Capability

Tenetora provides platform-neutral role contracts and deterministic recording. The host AI tool and model perform dispatch; no installer, CLI command, or hook may claim to spawn an agent.

Host-specific built-in carrier names, role-injection qualification, and isolation behavior live in `subagent-resolution.md`. Stable project rules match capabilities rather than those changing names.

| Tool | Dispatch capability | Lifecycle observation | Bounded review behavior |
| --- | --- | --- | --- |
| Codex | dedicated agent, then qualified built-in role injection | `SubagentStart` / `SubagentStop` after hook trust | capability-driven; final fallback is marked main-agent self-review |
| Claude Code | dedicated agent, then qualified built-in role injection | `SubagentStart` / `SubagentStop` | capability-driven; final fallback is marked main-agent self-review |
| ZCode | dedicated agent, then qualified built-in role injection | observed when the host emits configured events | capability-driven; final fallback is marked main-agent self-review |
| OpenCode | host-dependent | adapter does not promise equivalent observation | host-dependent; never report a guaranteed closed loop |
| Pi | host-dependent | adapter does not promise equivalent observation | host-dependent; never report a guaranteed closed loop |
| Cursor | recommendation-only | no reliable programmatic subagent lifecycle | fallback-only main-agent review |
| Generic Agents | host-dependent | no shared hook standard | rules-only unless the host supplies compatible dispatch |

## Source Notes

- Source: OpenAI Codex manual, `Custom instructions with AGENTS.md`: Codex discovers global/project `AGENTS.md`, supports `AGENTS.override.md`, nested discovery, and configured fallback filenames. `Agent Skills` documents `.agents/skills` and skill discovery.
- Source: OpenAI Codex manual, `Rules`: Codex command permission rules live under `rules/` next to an active config layer, including project `.codex/rules/` when trusted.
- Source: OpenAI Codex manual, `Plugins`, `Hooks`, and `Subagents`, verified 2026-07-23: plugin manifests can bundle hooks; `${PLUGIN_ROOT}` points to the installed plugin; non-managed hooks require `/hooks` trust; `SubagentStart`/`SubagentStop` are observable; hosted `WebSearch` bypasses local tool hooks.
- Source: OpenAI Codex app-server `hooks/list`, verified 2026-07-24: each discovered hook exposes source, command, `currentHash`, and `trustStatus`; project Hook discovery remains available independently of marketplace listing, and changed definitions become `modified`, so key presence alone is not sufficient to claim runtime activation.
- Source: Claude Code docs, `How Claude remembers your project`: Claude Code loads `CLAUDE.md`, `CLAUDE.local.md`, and rules files; `/memory` can list loaded files; local memory/config can be machine-local.
- Source: Claude Code plugin CLI validation, 2026-07-23: the release payload exposes eight skills and seven hook event groups from `hooks/hooks-claude.json`.
- Source: Cursor docs, `Rules` and `Hooks`, verified 2026-07-23: Cursor supports `.cursor/rules/*.mdc`, `AGENTS.md`, user/project `hooks.json`, `sessionStart`, `beforeShellExecution`, and `stop` hook outputs.
- Source: OpenCode docs and `@opencode-ai/plugin` hook types, verified 2026-07-23: OpenCode auto-loads JS/TS plugins and exposes system transform, tool-before, and compacting hooks, but no Claude/Codex-equivalent enforced Stop event.
- Source: Pi official extension documentation, verified 2026-09-02: Pi auto-loads TypeScript extensions from `~/.pi/agent/extensions/` and project `.pi/extensions/`, exposes `session_start`, `before_agent_start`, `session_before_compact`, `session_shutdown`, and `tool_call` callbacks, and gates project-local loading on project trust. Tenetora keeps Pi's default compaction summary and applies its own timeout and fail-closed tool policy around the managed runner.
- Source: ZCode official `example-plugin` 0.2.0 and local registry verification, 2026-07-23: ZCode uses a registered plugin cache and process-style hooks that are not Claude Code compatible.
