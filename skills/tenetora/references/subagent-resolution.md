# Built-in Subagent Resolution Reference

This reference records host-specific carrier names and observed permission behavior. It is intentionally separate from stable `.tenetora` rules because platform agents and capabilities can change between host releases.

Verified: 2026-07-23.

## Resolution Order

Resolve a portable Tenetora role in this order:

1. `dedicated`: use a platform-specific or user-defined agent whose enforced tools satisfy the role contract.
2. `builtin-role-injection`: render the project role contract with `tenetora delegation --render-prompt`, then let the host/model pass it to a qualified built-in agent.
3. `main-self-review`: when independent dispatch is unavailable or unqualified, continue in the main context and mark the result `未经独立审查`.

Tenetora only renders, validates declarations, and records compact state. The host AI tool and model perform dispatch. CLI commands, hooks, installers, and repair flows do not create, install, or start platform agents.

## Current-Session Availability Check

Before declaring a role unavailable, inspect the host's actual tool catalog for this session. Include deferred or discoverable tool metadata, not only tools already expanded into the prompt. Look for the host's native/custom dispatch operation (`spawn_agent` or equivalent), result waiting, and result retrieval. If a qualified carrier exists, attempt it once and inspect the returned child result.

Do not infer dispatch unavailability from an inactive plugin, missing Hook observation, stale lifecycle evidence, or `doctor` reporting `session_dispatch_availability: unknown`. Those facts describe Tenetora integration and observability, not the host model's tool inventory. Record `unavailable` only after the current session lacks dispatch, the actual attempt fails, or no carrier satisfies the role's isolation contract. Preserve the bounded reason so a later review can distinguish unsupported, failed, and ineligible cases.

## Isolation Declarations

| Level | Qualification |
| --- | --- |
| `hard` | The host enforces the complete role tool contract through agent configuration, sandbox, allowlist, or an equivalent boundary. |
| `inherited` | The child inherits a parent runtime that already satisfies every role restriction. Inheriting arbitrary parent permissions is not sufficient. |
| `prompt-only` | The role is constrained only by injected instructions. This is a `soft-boundary`, not permission isolation. |
| `unknown` | The host or legacy caller did not declare enough evidence. Never upgrade it by inference. |

`code-reviewer` and `codebase-scout` may use `prompt-only` for a bounded assignment if the soft boundary is recorded. `security-auditor` and `implementer` must reject it. `implementer` also requires an active authorized review cycle.

## Verified Host Carriers

| Host | Built-in carriers | Qualification notes | Evidence status |
| --- | --- | --- | --- |
| Codex | `default`, `worker`, `explorer` | Subagents inherit the parent runtime. Declare `inherited` only when the parent permissions already satisfy the complete role contract; otherwise use `prompt-only` for eligible read-only roles. | Official docs verified 2026-07-23. |
| Claude Code | `general-purpose`, `Explore`, `Plan` | `Explore` and `Plan` are read-only carriers. `general-purpose` has all available tools and is not a strict security carrier without an additional enforced boundary. | Official docs verified 2026-07-23. |
| ZCode | `general-purpose`, `Explore` | Real pilot confirmed role injection. Isolation must be declared from the actual runtime; do not infer it from the carrier name alone. | Real pilot observed 2026-07-23. |
| OpenCode | None fixed here | Host-dependent. Use only capabilities reported by the current host. | Conservative declaration. |
| Cursor | None verified | Recommendation-only until a reliable built-in carrier and lifecycle contract are verified. | Unverified. |
| Generic Agents | None fixed here | Host-dependent rules-only fallback unless the host exposes compatible dispatch. | Conservative declaration. |

Carrier names are candidates, not role qualifications. The model must compare the actual host permissions with the selected `.tenetora/templates/subagents/<role>.spec.md` before declaring `hard` or `inherited`.

## Deterministic Prompt Rendering

Use the project role spec when present; the package default is only a missing-file fallback:

```bash
tenetora delegation --render-prompt \
  --role codebase-scout \
  --assignment "Trace one bounded authentication flow" \
  --host-tool codex \
  --host-agent explorer \
  --isolation-level inherited
```

Rendering returns the selected spec source and SHA-256 hash. It does not write delegation state or spawn an agent. After the host/model actually attempts dispatch, record the outcome separately with `--start`, `--complete`, or `--unavailable` and the same resolution metadata.

## Source Notes

- OpenAI Codex manual, `Subagents`, verified 2026-07-23: built-in `default`, `worker`, and `explorer` roles; child permission behavior follows the parent runtime.
- Claude Code docs, `Subagents`, verified 2026-07-23: built-in `general-purpose`, `Explore`, and `Plan`; `Explore`/`Plan` use read-only tools while `general-purpose` can use all available tools.
- ZCode pilot evidence supplied with `builtin-subagent-fallback-proposal.md`, reviewed 2026-07-23: built-in `general-purpose` and `Explore` accepted bounded role-contract injection.

Reverify this table when a host changes built-in agent names, permission inheritance, or dispatch APIs. Update this reference and runtime capability reporting together; do not copy changing names into `.tenetora/rules/subagent-dispatch.md`.
