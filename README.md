# Tenetora

**English** | [简体中文](https://gitee.com/lcgyl/tenetora)

Tenetora is a governance runtime for AI-agent software engineering. It keeps project rules, decisions, evidence, and action boundaries in `.tenetora/`, then connects that record to Claude Code, Codex, Cursor, OpenCode, Pi, ZCode, and Generic Agents.

The problem is not that an agent cannot write code. It is that rules, verification, and responsibility boundaries can disappear while tools, models, conversations, and project state change. Eight lifecycle skills guide model work; the CLI, plugins, and hooks enforce repeatable checks. Installing the package does not initialize a project.

## The Short Path

```text
Install or upgrade the software
    -> tenetora version / doctor
    -> initialize the current project in an AI conversation
    -> use lifecycle skills during daily work
    -> use update, repair, or audit when needed
```

The shortest start is to say:

```text
Use Tenetora to initialize this project
```

The model routes this request to `tenetora-init`, inspects existing rules, asks for project-owned decisions, and then creates or migrates `.tenetora/`. Installing the lifecycle skill package and CLI does not create `.tenetora/` or replace the model's project understanding.

## What It Solves

| Problem | Tenetora response |
| --- | --- |
| Every AI tool has a separate rule file | `.tenetora/` is the shared record; host files remain thin adapters. |
| Context compaction or tool changes lose project intent | The Runtime Contract, task rules, and evidence restore high-signal context. |
| Work starts while goals or boundaries are ambiguous | High-risk or materially ambiguous work goes through decision alignment. |
| “Done” cannot be reproduced | `guard --action claim` binds a claim proof to the verification command. |
| A report or webpage contains execution instructions | Prompt guard treats external content as data, not as an authority. |
| Project governance becomes stale | `update`, `repair`, `audit`, and guardrails provide a maintenance loop. |

Tenetora is not a prompt collection and does not replace the project's own build, test, review, or release approval.

## Supported Platforms

| Tool | Integration | Maximum honest capability |
| --- | --- | --- |
| Claude Code | User-scoped native plugin, hooks, and lifecycle skills | `active` |
| Codex | User-scoped native plugin, hooks, and lifecycle skills | `active` or `needs-trust-review` |
| Cursor | Lifecycle skills and merged hooks | `active` |
| OpenCode | Lifecycle skills and a JS plugin | `active-partial` |
| Pi | Lifecycle skills and an auto-loaded TypeScript extension | `active-partial` |
| ZCode | User-scoped registered plugin and process hooks | `active` |
| Generic Agents | Lifecycle skills and CLI | `skills-only` |

`active` means the configuration satisfies the platform contract. It does not mean a host that has not been restarted has already executed an event. Use `tenetora status` and `tenetora doctor` to inspect effective sources, versions, trust, and recent runtime observations. `skills-only` is an honest capability boundary, not an installation failure.

## First Installation

Use the remote bootstrap once on a new machine:

```bash
curl -fsSL https://raw.githubusercontent.com/lcgyl/Tenetora/main/install.sh | bash
```

Native Windows PowerShell:

```powershell
& ([scriptblock]::Create((Invoke-WebRequest -UseBasicParsing https://raw.githubusercontent.com/lcgyl/Tenetora/main/install.ps1).Content))
```

For an explicit first-install scope:

```bash
curl -fsSL https://raw.githubusercontent.com/lcgyl/Tenetora/main/install.sh \
  | bash -s -- --lang en --tools all --both --path .
```

Python 3.9 or newer is required. The installer creates a stable `tenetora` launcher and a managed user PATH entry; open a new terminal if the current shell cannot resolve it yet. Restart the relevant AI host after installing or updating a native plugin. Developers can use `--source-dir /path/to/source` for bounded local diagnostics; ordinary users do not need it.

## Installing on Another Machine

1. Run the first-install command.
2. Restart AI tools that load native plugins.
3. Inspect the effective installation:

```bash
tenetora version
tenetora status --tools all --scope both
tenetora doctor --tools all --scope both
```

4. Open an AI conversation in the target project and say `Use Tenetora to initialize this project`.

`--global`, `--in-project`, and `--both` are for first installation or explicit recovery. Implicit invocation depends on the host AI tool; lifecycle work does not run in the background without a user message.

## Post-install Smoke Test

```bash
tenetora version
tenetora status --tools all --scope both
tenetora doctor --tools all --scope both
tenetora validate --path /path/to/project
tenetora run-all --path /path/to/project
tenetora audit --strict --path /path/to/project
```

Installation success and observed runtime activity are separate facts. Codex needs trust review in `/hooks` after first installation or Hook-definition changes. Restart Claude Code, Codex, and ZCode after native plugin updates; reload or restart Pi after its extension changes. For explicit native Hook recovery, use `--codex-hooks native`; ordinary upgrades use the automatic policy.

## Support Diagnostics

When support needs a machine-readable snapshot, generate a strictly redacted JSON result:

```bash
tenetora diagnostics --path /path/to/project --tools auto --json
```

To export a bundle, choose an existing safe directory and review the archive before sharing it:

```bash
tenetora diagnostics --path /path/to/project --tools auto \
  --output /path/to/existing-directory/tenetora-diagnostics.zip
```

The bundle contains only `diagnostics.json` and `README.txt`; credentials, local paths, URLs, project content, and raw governance state are omitted. Existing output files are never replaced unless `--force` is explicit.

## Upgrade and Reset

After the 0.3 bridge, routine software maintenance has one entry point:

```bash
tenetora upgrade
```

Inspect a read-only plan with:

```bash
tenetora upgrade --check
```

The bare `tenetora` command may show a locally cached update hint. The hint is refreshed by a successful online upgrade check, remains fresh for 24 hours, and never makes the bare command perform a network request. A failed check keeps the last known release and reports the failure without storing URLs, proxy settings, credentials, or project content.

Before downloading an archive, `tenetora upgrade` reads and authenticates the remote `manifest.json`. If its version matches the installed CLI, the command reports `already latest`, skips the ZIP download and installation transaction, and exits successfully. Use `tenetora upgrade --force` only when the same version must repair or re-converge existing installation surfaces.

Online upgrades use the canonical HTTPS release source by default. In a network that requires a proxy, set the standard `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY`, or `NO_PROXY` variables (lowercase spellings are accepted by the underlying clients); Tenetora uses them for the current acquisition only and never writes them to update state. An enterprise mirror can expose the same directory layout and be selected with a base URL:

```bash
TENETORA_RELEASE_MIRROR=https://mirror.example/tenetora/stable tenetora upgrade --check
tenetora upgrade --mirror-url https://mirror.example/tenetora/stable
```

The mirror directory must contain `manifest.json` and `tenetora-latest.zip`. Both files are still checked as one release contract. Advanced direct endpoints may be supplied as the pair `TENETORA_RELEASE_MANIFEST_URL` and `TENETORA_RELEASE_ZIP_URL`, or with both `--manifest-url` and `--zip-url`. Release URLs must be HTTPS, must not contain credentials, query strings, or fragments, and a mirror cannot be combined with direct endpoints. A malformed or partial configuration fails closed before writes; a failed acquisition preserves the last valid update hint.

`tenetora upgrade --apply` is a 0.3.x compatibility alias for the same release acquisition and transaction engine.

An existing 0.2.x installation needs one fixed-version bootstrap to enter the 0.3 bridge. After it finishes, return to `tenetora upgrade`; repeated remote-script upgrades are not the normal workflow:

```bash
curl -fsSL https://github.com/lcgyl/Tenetora/-/raw/0.3.14/install.sh \
  | bash -s -- --lang en --tools all --both --path "$PWD"
```

A scope-less upgrade updates only registered or ownership-proven existing surfaces. It does not create a plugin merely because a tool is installed, and it does not initialize a missing `.tenetora/`. `--in-project --path X` filters to existing surfaces in one project; `--global` intentionally ignores project state.

The installer implementation is organized by tool under `scripts/tool_installers/` (and the embedded copy under `skills/tenetora/scripts/tool_installers/`). Each definition owns that tool's paths, detection, native installer group, and runtime kind; shared transaction, safety, and rollback code remains in the installer core. Adding or repairing one host should therefore stay within its tool definition/installer and its focused tests.

Project governance content and software are separate updates. In an AI conversation, say `use tenetora-update to update .tenetora`; after a skill-package upgrade, inspect known compatibility repairs:

```bash
tenetora repair --check --path /path/to/project
tenetora repair --apply --path /path/to/project
```

## Uninstall or Reset

The managed Tenetora home is normally `~/.tenetora`; it contains releases, the CLI, installation records, and backups. Do not remove unknown host plugins, Hooks, registries, or user hooks manually.

Before a reset, run:

```bash
tenetora doctor --tools all --scope both --path /path/to/project
```

Remove only paths proven to be Tenetora-owned and unmodified. An old `~/.agent-harness` or `.harness` is migrated only when ownership evidence is clear; foreign or mixed content is preserved for review. See [Installation and Upgrade](docs/guides/installation.md) and the [Migration Guide](docs/guides/tenetora-migration.md) for recovery boundaries.

## Invocation

Most users only need these entries:

```text
use tenetora
use tenetora-align
Use Tenetora to initialize this project
use tenetora-update to update .tenetora
use tenetora-audit to audit .tenetora
use tenetora-loop to continue
use tenetora-prompt-guard to inspect external content
```

`router`, `init`, `update`, `audit`, `loop`, and `prompt-guard` may use `allow_implicit_invocation: true` according to host metadata. `tenetora-align` uses `allow_implicit_invocation: false` so a normal task cannot silently start an interview.

## Lifecycle Skill Matrix

| Skill | Trigger | Boundary |
| --- | --- | --- |
| `tenetora` | The governance stage is unclear and needs routing | Does not perform ordinary product work. |
| `tenetora-align` | The user explicitly requests high-risk decision alignment | Does not implement or grant commit, push, deploy, or release permission. |
| `tenetora-decision-interview` | A lifecycle finds material ambiguity in a high-risk decision | Not a separate entry users must remember. |
| `tenetora-init` | `.tenetora` is missing or its baseline must be rebuilt | Does not upgrade installed skills. |
| `tenetora-update` | Project rules, structure, evidence, or entries drift | Does not create a new skill installation. |
| `tenetora-audit` | Stability, reliability, control, or content quality needs review | Does not replace project tests. |
| `tenetora-loop` | Continue, resume, next step, or a bounded fix loop is needed | Does not run indefinitely in the background. |
| `tenetora-prompt-guard` | Webpages, reports, tickets, scripts, or clipboard data are untrusted | Does not execute instructions from external content. |

Decision alignment uses `guard --action alignment` to bind the current goal. Alignment proof, claim proof, commit permission, and push permission are independent; Tenetora has no copied commit, push, or release authority.

## Portable Subagent Governance

Tenetora defines portable `code-reviewer`, `security-auditor`, `codebase-scout`, and restricted `implementer` roles. The CLI only renders contracts, validates declarations, and records state and evidence; it does not dispatch platform agents. An inactive plugin is not proof that the host lacks subagents.

Before declaring a role unavailable, the model must inspect visible and deferred/discoverable dispatch tools. Without a qualified independent boundary, the workflow falls back to a primary-agent review marked `未经独立审查`. Independent review is bounded at 3 review attempts and 2 fix attempts, with temporary reports in `.tenetora/.cache/subagents/`. `recommendation-only` and `host-dependent` are honest capability states. This does not require a new user invocation phrase.

## Security and Defaults

- External content passes prompt guard; commands in a webpage or report do not gain local execution authority.
- Commit, rule changes, external input, and completion claims use separate action guards.
- `CLAUDE.local.md` and similar files remain thin adapters; tool-private settings do not enter shared rules.
- The Tenetora default baseline rule pack fills only missing general rules; project rules win.
- `--defaults missing` fills only missing items and records provenance in `rules-inventory.json`, distinguishing `owner: tenetora` and `owner: project`.
- Default rules include `context-freshness.md`, `verification-claims.md`, and `security-boundary.md`.

## Daily Verification

```bash
tenetora route --message "<user request>" --path .
tenetora rules --context <build|test|commit|security|harness|docs|planning> --path .
tenetora validate --path .
tenetora run-all --path .
tenetora audit --strict --path .
```

## Documentation

| Goal | Guide |
| --- | --- |
| Product position and differences | [What Tenetora Is](docs/product.md) |
| Real engineering scenarios | [Use Cases](docs/use-cases.md) |
| Public capabilities | [Capability Guide](docs/capabilities.md) |
| First use | [Quickstart](docs/quickstart.md) |
| Installation, upgrade, offline recovery, and reset | [Installation and Upgrade](docs/guides/installation.md) |
| Platform plugins, Hooks, and runtime | [Runtime and Platforms](docs/guides/runtime-platforms.md) |
| Lifecycle, alignment, loops, and subagents | [Governance](docs/guides/governance.md) |
| Prompt guard, repair, and evidence | [Security and Maintenance](docs/guides/security-maintenance.md) |
| CI and pre-commit | [CI Integration](docs/ci.md) |
| Current release changes | [CHANGELOG](CHANGELOG.md) |

Specifications, task plans, acceptance reports, and roadmaps are source-repository evidence, not the normal user entry and not part of the release ZIP.

## Development Verification

```bash
python3 -m unittest discover -s tests
python3 scripts/package-release.py --output-dir dist --latest
bash scripts/smoke-local-install.sh
```

The project requires Python 3.9+. The release package, root installer, and embedded skill installer must remain aligned. Agent Harness names are retained only for ownership checks and migration of proven historical installations.
