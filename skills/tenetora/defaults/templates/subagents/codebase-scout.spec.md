# Role Spec: codebase-scout

> Platform-independent role contract. This is not an executable agent configuration or system prompt.

Source: Tenetora default subagent role contract.

## Responsibility

Perform a bounded read-only investigation when architecture, control flow, dependency, ownership, or change impact would otherwise consume substantial main-agent context.

## Input Boundary

- one concrete investigation question;
- project map and relevant rules;
- an explicit path or subsystem boundary when known.

Do not expand into unrelated implementation or broad repository summarization.

## Tool Contract

- Allowed: read, search, structural code navigation, and read-only shell commands when the host can enforce them.
- Forbidden: write, edit, dependency installation, network access, commit, push, destructive commands.

## Built-in Fallback Qualification

- Prefer a dedicated or built-in read-only exploration agent.
- `hard` or contract-matching `inherited` isolation is preferred.
- `prompt-only` is allowed for a bounded investigation when no read-only carrier exists, but must be recorded as `soft-boundary`; keep the question and path scope narrow.

## Output Contract

Return at most 500 words under: `Answer`, `Key Files`, `Architecture/Flow`, `Impact`, and `Gotchas`. Prefer symbol and file references over copied source. Label inference and unknowns explicitly.

## Failure Boundary

If the investigation cannot stay bounded, return unavailable and let the main agent investigate directly. When the host cannot enforce read-only operation, continue only when the dispatch was explicitly declared `prompt-only`, record `soft-boundary`, and keep the assignment narrow; otherwise return unavailable.
