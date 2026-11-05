# Tenetora for DeepSeek Harness (Cordis)

A Host-only Cordis bundle that connects a Tenetora-governed workspace
(`.tenetora/`) to the DeepSeek Harness. It is the DSH adapter for the same
project record that the Claude Code, Codex, Cursor, OpenCode, Pi and ZCode
adapters use.

The bundle does not re-implement a Tenetora check. Every decision is made by the
`tenetora` CLI; the plugin only decides when to ask it and how to turn the answer
into a host decision.

## What it contributes

| Contribution | Cordis surface | Behaviour |
| --- | --- | --- |
| Governance context | `systemPrompt.section` | Adds the project protocol, the rule/workflow files that actually exist, and the required guard commands. Empty in an ungoverned workspace. |
| Git guard | `tools/pre-execute` | `git commit` / `git push` are denied when `tenetora guard --action commit` fails. Compound commands are denied with a "run them separately" reason. |
| Local-environment protection | `tools/pre-execute` | Destructive writes to paths registered by `tenetora local-env --allow` are denied. |
| External-input screening | `tools/post-execute` | `web_search` / `web_fetch` results are scanned for high-confidence prompt-injection patterns. `block` (default) replaces the result with a warning, `annotate` prefixes the warning and keeps the content, `off` disables screening. |
| Routed context | `agent/inbox/inserted` + `systemPrompt.section` | Each substantive user request runs `tenetora route` and `tenetora rules`, and the bounded result is appended to the governance section for that turn. |
| Runtime observation | `tenetora observe` | Records one machine-level observation per governed root so `status`/`doctor` report `runtime-observed` instead of `not-observed`. |
| Native delegation observation | `tools/post-execute` | After a DSH `subagent` / `subagent_fork` call, binds the caller owner and the real `session.header.id`, parses `started subagent <childId>` from the tool result, and records one hashed dispatch observation. The tool result is a dispatch acknowledgement, not completion; missing identities become unavailable review debt. |
| Completion reminder | `agent/turn-stopping` | When a turn used `present` or ran a git commit without a claim, one `agent.steer` reminder points at `tenetora verification-plan --json` and its reuse/targeted/rerun decision, and states that a completion claim needs a confirmed alignment handoff while otherwise only `partial-verification` is available. The reminder is a full `UserMessage` (`id`, `role: 'user'`, text block, `source.kind: 'user'`); `steer()` does not accept a bare string, and a string corrupts the durable session log. `off` disables it. |
| Lifecycle skills | `skills.registerProvider` | Publishes `tenetora`, `tenetora-init`, `tenetora-update`, `tenetora-audit`, `tenetora-align`, `tenetora-plan`, `tenetora-loop`, `tenetora-prompt-guard`, `tenetora-decision-interview` for governed workspaces. |

An ungoverned directory gets no section, no denials, and no skills: the bundle is
inert there. That mirrors Tenetora's opt-in per-worktree contract.

## Requirements

- DSH with the `systemPrompt`, `sessions`, and (optionally) `skills` services.
- The `tenetora` CLI on `PATH`, or `cli`/`cliArgs` configured for a source
  checkout.
- Python 3.9+ and an initialized `.tenetora/` in the governed project.

## Install

From a source checkout of this repository:

```js
plugin_manager({ action: "install_bundle", target: "<checkout>/dsh" })
```

The bundle inserts one row (`tenetora-dsh`) through `cordis.patch.yml`. To point
at a managed launcher or a source checkout, override the row's config in your
layer:

```yaml
- id: tenetora-dsh
  name: '@lcgyl/tenetora-dsh'
  config:
    cli: C:/Users/<user>/.tenetora/bin/tenetora.cmd
```

```yaml
- id: tenetora-dsh
  name: '@lcgyl/tenetora-dsh'
  config:
    cli: python
    cliArgs: ['-m', 'tenetora']
    env:
      PYTHONPATH: <checkout>/cli
```

The row `config` replaces the bundle's default wholesale, so restate every field
you need.

## Configuration

`enabled`, `cli`, `cliArgs`, `env`, `timeoutMs`, `contextSection`, `contextMode` (`card` or `full`),
`sectionName`, `sectionOrder`, `maxSectionChars`, `guardGit`, `strictEvidence`, `guardLocalEnv`,
`onGuardFailure` (`deny` or `ask`), `externalInput` (`block`, `annotate`, or
`off`), `externalInputTools`, `recordObservation`, `contextInjection`
(`auto` or `off`), `injectionMaxChars`, `completionReminder` (`auto` or `off`),
`delegation` (`auto` or `off`), `delegationTools`, `shellTools`, `fileTools`,
`pathFields`, `skills`, `skillsRoot`, `debug`.

See `types/index.d.ts` for the exact meaning and defaults.

## Failure behaviour

- Missing CLI, timeout, or an internal exception: **allow** the tool call and log
  only with `debug: true`. A governance plugin must never be the reason an
  unrelated call fails.
- External-input screening fails open: an unexpected result shape or a thrown
  scanner leaves the tool result unchanged.
- CLI exit `2` (no `.tenetora/` at that path): allow. Not governed.
- CLI exit other than `0` **without** a JSON verdict (a crash or a broken
  install): allow, because no check result exists. Only a structured
  `status: "fail"` payload from `tenetora guard --json` produces a denial.
- CLI exit `1` with a JSON verdict (guard failed): **deny** with the failing
  checks as the reason.
- Native delegation observations are telemetry only. DSH returns
  `started subagent <childId>` and reports completion separately; the plugin
  records the dispatch (owner, real session id, parsed child id) and never
  infers completion from that acknowledgement. Continuity is recorded only when
  the host exposes it.
- `strictEvidence` requires a fresh covering claim for verification-impacting
  staged paths; documentation or release-metadata-only commits pass with their
  targeted checks. A scoped claim is sufficient when the affected boundary is
  narrow, so strict mode never forces a full run for an unrelated path.

## Verify

```bash
node --check dsh/index.js
node dsh/test/run.mjs
python -m unittest tests.test_dsh_plugin
```

The Node test drives `apply()` with a stub CLI and a temporary governed project;
it covers the ungoverned no-op path, the section budget, native delegation
identity binding, git deny/allow,
compound-command denial, local-environment denial, the skill catalogue, and
disposal.

## Limits

- The section is rebuilt per assembly from the files on disk; it does not cache
  or summarize rule bodies.
- The guard inspects `git` invocations in shell tools and path arguments in file
  tools. It does not attempt to become a shell parser.
- `force-push`, history rewriting, and release operations are governed by the
  project rules and the user's authorization, not by this plugin.
