# Tenetora Migration Guide

**English** | 简体中文

This guide covers the transition from the Agent Harness naming to Tenetora 0.3.x. The active contract is now singular: the CLI and Python package are `tenetora`, lifecycle skills are `tenetora-*`, Hook code is `tenetora_hook.py`, environment inputs are `TENETORA_*`, and project governance lives in `.tenetora/`. Old names are migration inputs only when ownership is proven.

## What Changes

| Surface | Current contract | Treatment of legacy content |
| --- | --- | --- |
| CLI | `tenetora` | A proven old shim is migrated; no old alias is generated. |
| Lifecycle skills | `tenetora-*` | `agent-harness-*` is backed up and removed from active paths only when `SKILL.md` proves ownership. |
| Native plugin | `tenetora@tenetora-local` | Install the canonical plugin first, then handle proven `agent-harness@agent-harness-local`. |
| Machine home | `~/.tenetora` | A proven `~/.agent-harness` is migrated once; an ambiguous one is preserved. |
| Project directory | `.tenetora/` | `.harness/` is a legacy input and is not used for normal operation. |
| Release asset | `tenetora-<version>.zip` | New releases do not generate Agent Harness ZIP aliases. |
| Environment protocol | `TENETORA_*` | Old variables do not select the current home, plugin, or runtime behavior. |

## Upgrade an Existing Machine

A 0.2.x user enters the bridge once with a fixed-version bootstrap, then every platform uses the installed CLI:

```bash
curl -fsSL https://github.com/lcgyl/Tenetora/-/raw/0.3.11/install.sh | bash -s -- --lang en --tools all --both --path "$PWD"
tenetora upgrade
```

Users already on 0.3.x should not repeatedly fetch a script. Inspect the plan first when needed:

```bash
tenetora upgrade --check
```

The upgrader validates the release, converges plugins, CLI, runtimes, and managed Git Hooks, then migrates the legacy machine home. Rewritable configuration is backed up before changes and restored on failure. Only content supported by multiple independent ownership signals is migrated; foreign, mixed, or ambiguous content is preserved.

After the upgrade:

```bash
tenetora version
tenetora status --tools all --scope both
tenetora doctor --tools all --scope both
```

Restart Claude Code, Codex, or ZCode after native plugin updates, and reload or restart Pi after its extension changes. Codex also needs a new definition trust review in `/hooks`; old trust hashes are never copied to the new plugin identity.

## Project Migration

Do not manually rename `.harness`, `AGENTS.md`, or `CLAUDE.md`. Classify first:

```bash
tenetora migrate --check --path /path/to/project
tenetora migrate --plan --path /path/to/project
```

After confirming that the content belongs entirely to Tenetora:

```bash
tenetora migrate --apply --path /path/to/project
```

Migration creates `.tenetora/manifest.json` and a recovery backup. A foreign `.harness` is not renamed or deleted. Mixed content is blocked by default; after review, `--allow-mixed` copies only recognized Tenetora content and preserves the source directory.

Package installation does not create a missing `.tenetora/`, and a workspace without `.tenetora/` or legacy `.harness/` remains outside Hook governance. When shared project governance is wanted, open an AI conversation and say `Use Tenetora to initialize this project`; `tenetora-init` must understand existing rules and project decisions before writing. Initialize each independent submodule separately: parent governance coordinates gitlinks and cross-repository evidence, while the child repository owns its own rules and verification.

## Legacy Skills and Plugins

The 0.2.x package installs only canonical skills. An `agent-harness-*` skill is backed up only when its `SKILL.md`, managed version, and other product signals prove ownership; proven copies move to `~/.tenetora/backups/legacy-skill-aliases/` before leaving active paths. An unproven same-named directory is preserved and reported as a conflict.

The canonical native plugin identity is `tenetora@tenetora-local`. Upgrade installs and verifies it first, then removes a proven legacy registration so old and new Hooks cannot run together. If an old Codex marketplace registration makes inventory unreadable, use the bounded recovery only when needed:

```bash
tenetora repair --fix codex-legacy-registration --check
tenetora repair --fix codex-legacy-registration --apply
```

This repair changes only an exact ownership-proven legacy configuration and saves a `config.toml` backup. Unknown configuration and third-party plugins are not replaced.

## Rollback and Non-goals

- A release is validated before `~/.tenetora/current` changes; failure does not overwrite the active version.
- Machine-home, plugin, runtime, and Hook migrations retain recovery records.
- Project rules are not rewritten merely because the product was renamed.
- Ambiguous legacy homes, plugins, Hooks, and `.harness` directories are not deleted.
- User files, private settings, and old trust decisions are not copied into shared `.tenetora/`.

See [Installation and Upgrade](installation.md) for installation flags and [Runtime and Platforms](runtime-platforms.md) for effective runtime diagnosis.
