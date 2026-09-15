# Installation and Upgrade

**English** | 简体中文

## Requirements

- Python 3.9 or newer.
- Network access for online installation; offline installation requires a verified release ZIP and SHA256.
- Generic Agents can use a skills-only installation; native plugin capability depends on the host CLI, configuration, and trust state.

Use `install.sh` from Bash on macOS/Linux. Use `install.ps1` from native PowerShell on Windows; Git Bash and WSL are not required. The package includes `run-hook.cmd` for native Windows Hook execution.

## First Installation

```bash
curl -fsSL https://raw.githubusercontent.com/lcgyl/Tenetora/main/install.sh | bash
```

Windows:

```powershell
& ([scriptblock]::Create((Invoke-WebRequest -UseBasicParsing https://raw.githubusercontent.com/lcgyl/Tenetora/main/install.ps1).Content))
```

Set the language with `--lang en` or `TENETORA_LANG=en`. First-install scopes:

```bash
# User-level plugins, runtimes, and lifecycle skills
curl -fsSL https://raw.githubusercontent.com/lcgyl/Tenetora/main/install.sh | bash -s -- --lang en --tools all --global

# Project skills and host-supported project runtime
curl -fsSL https://raw.githubusercontent.com/lcgyl/Tenetora/main/install.sh | bash -s -- --lang en --tools all --in-project --path /path/to/project

# Both scopes; each platform still keeps one active runtime owner
curl -fsSL https://raw.githubusercontent.com/lcgyl/Tenetora/main/install.sh | bash -s -- --lang en --tools all --both --path /path/to/project
```

Package installation does not create a project `.tenetora/`. Ask the AI lifecycle skill to initialize the project.

## Existing-only Upgrade

Users on 0.3.x use the installed CLI:

```bash
tenetora upgrade
```

Use `tenetora upgrade --check` for a read-only plan. `tenetora upgrade --apply` is a compatibility alias for older automation. A 0.2.x user enters the 0.3 bridge once with a fixed-version bootstrap, then uses `tenetora upgrade` everywhere:

```bash
curl -fsSL https://github.com/lcgyl/Tenetora/-/raw/0.3.11/install.sh | bash -s -- --lang en --tools all --both --path "$PWD"
```

Routine upgrades do not fetch a remote script. Keep the bootstrap for first installation, the 0.2 bridge, offline recovery, or a broken launcher.
An upgrade first authenticates the remote `manifest.json`. When the advertised version equals the installed CLI version, it reports `already latest`, skips the ZIP download and installation transaction, and exits successfully. Use `tenetora upgrade --force` only to repair or re-converge existing installation surfaces at that same version.

After a successful online `upgrade --check` or `upgrade`, the CLI stores a small update hint under the managed home. A bare `tenetora` command reads this local hint without making a network request: a fresh hint is valid for 24 hours, an expired hint is reported as stale, and a failed network attempt is reported without replacing the last known release. The cache contains only version, commit, package digest, channel, and timestamps; it does not contain URLs, proxy settings, credentials, or project content. A missing or invalid cache is advisory and never initializes a project or blocks ordinary CLI use.

### Proxy and Enterprise Mirror

Online acquisition uses the canonical HTTPS source unless configured otherwise. Standard `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY`, and `NO_PROXY` environment variables are honored by the download clients; lowercase spellings are also honored. Proxy values are process-local and are never copied into the update hint or upgrade transaction state.

An enterprise mirror may expose this layout:

```text
<mirror-base>/manifest.json
<mirror-base>/tenetora-latest.zip
```

Select it for one check or upgrade with `--mirror-url`, or configure it for the process with `TENETORA_RELEASE_MIRROR`:

```bash
TENETORA_RELEASE_MIRROR=https://mirror.example/tenetora/stable tenetora upgrade --check
tenetora upgrade --mirror-url https://mirror.example/tenetora/stable
```

Advanced direct endpoints must be configured as a complete pair: `TENETORA_RELEASE_MANIFEST_URL` and `TENETORA_RELEASE_ZIP_URL`, or `--manifest-url` and `--zip-url`. Release URLs must be HTTPS and must not contain user credentials, query strings, or fragments. Mirror and direct endpoint settings are mutually exclusive. Partial or malformed settings fail closed before any installation write. A failed network check records `network-failed` while retaining the last authenticated release hint.

The no-scope `upgrade` updates registered global and project surfaces and migrates legacy governance only when Tenetora ownership is proven. Detecting a tool never authorizes plugin creation; governance-only migration does not create project skills or runtimes. The launch directory, registered projects, and bounded discoverable projects are considered. Foreign or ambiguous content is preserved for review.
The installer is split by host under `scripts/tool_installers/`. A host definition owns paths, detection, and capability classification, while its installer adapter owns native-surface dispatch. Shared file safety, transaction, and rollback behavior remains centralized so a host-specific repair does not need to duplicate the upgrade engine.

Choose an explicit scope when needed:

```bash
# Update existing surfaces in one project
tenetora upgrade --in-project --path /path/to/project

# Intentionally ignore project state
tenetora upgrade --global
```

`--path X` alone supplies context and does not narrow a no-scope upgrade. An explicit `--in-project` or `--both` target that has no matching existing project surface fails before writes instead of being hidden by a successful global result.

## Project Content Updates

Software maintenance and `.tenetora` content are separate actions. When project rules, structure, or evidence change, ask the AI:

```text
use tenetora-update to update .tenetora
```

The CLI `update` command without install flags enters project-update mode. Scope, mode, force, and related install flags select skill-install update mode:

```bash
tenetora update --path /path/to/project
tenetora update --global --tools all --force
```

After a skill-package upgrade, inspect known compatibility repairs:

```bash
tenetora repair --check --path /path/to/project
tenetora repair --apply --path /path/to/project
```

## Preflight and Capability States

```bash
tenetora preflight --tools all --global
tenetora preflight --tools codex --global --require-full --json
```

- `FULL`: requested surfaces reached the accepted plan.
- `PENDING_TRUST`: installation converged, but the host needs trust review or restart.
- `PARTIAL`: a real installation unit failed; exit code 2.
- `BLOCKED`: the operation could not proceed safely; exit code 1.

`skills-only` is an honest state for Generic Agents or a host with limited capabilities. Use `--allow-skills-only` only when that limitation is accepted; `--require-full` blocks below-maximum capability before writes.

## Support Diagnostics

Generate a strictly redacted machine-readable snapshot for support:

```bash
tenetora diagnostics --path /path/to/project --tools auto --json
```

Export a ZIP only into an existing safe directory:

```bash
tenetora diagnostics --path /path/to/project --tools auto \
  --output /path/to/existing-directory/tenetora-diagnostics.zip
```

The archive contains only `diagnostics.json` and `README.txt`. Credentials, local paths, URLs, project content, raw governance events, and unrecognized state fields are omitted. Review the archive before sharing it. Existing output files require explicit `--force` to replace.

## Discovering Historical Projects

Preview the bounded discovery used by a no-scope upgrade:

```bash
tenetora installations discover --auto --dry-run
```

Register projects outside automatic roots explicitly:

```bash
tenetora installations discover --root ~/projects --max-depth 5
tenetora installations list
tenetora installations prune --stale
```

Discovery does not follow directory symlinks or scan unspecified roots. When a global integration was intentionally removed and must not be restored automatically:

```bash
tenetora installations forget-global --tools zcode
```

## Codex and Hook Recovery

After first Codex installation or a Hook-definition change, trust the current Tenetora definition in `/hooks` and restart when requested. Native Hooks and project fallback Hooks are mutually exclusive.

Only use the explicit repair for a known broken legacy Agent Harness registration that makes Codex inventory unreadable:

```bash
tenetora repair --fix codex-legacy-registration --check
tenetora repair --fix codex-legacy-registration --apply
```

The repair backs up `config.toml`, changes only ownership-proven legacy registration, preserves unknown third-party configuration, and rolls back on failure.

## Offline Installation

When the installed CLI is available, use it for an offline upgrade:

```bash
tenetora upgrade --zip-file /path/to/tenetora-<version>.zip --sha256 <expected-sha256>
```

This uses the same existing-only plan, release activation, and rollback transaction as an online upgrade. If a later upgrade step fails before a concurrent external change is detected, the previous `current`, `source/tenetora`, and legacy source pointers are restored and the same verified ZIP can be retried.

Use the bootstrap only for first installation, the 0.2.x bridge, a broken launcher, or a machine without a working CLI. Bash on macOS/Linux:

```bash
bash install.sh --lang en --zip-file /path/to/tenetora-<version>.zip --sha256 <expected-sha256> --tools all --global
```

Native Windows PowerShell:

```powershell
.\install.ps1 -Lang en -ZipFile C:\path\to\tenetora-<version>.zip -Sha256 <expected-sha256> -Scope global
```

`--sha256`/`-Sha256` may be omitted when the ZIP has a sibling file named exactly `<zip-file>.sha256`; the first whitespace-delimited token must be the 64-character SHA256. A local ZIP and its checksum are one source selection. A source directory cannot be combined with a ZIP file, an explicit ZIP URL, or a checksum; a ZIP file cannot be combined with an explicit ZIP URL. Both bootstrap entrypoints reject these combinations before resolving or writing any package.

Before invoking packaged scripts, the installer validates the manifest, installer protocol, ZIP SHA256, and file inventory. Current assets use `tenetora-<version>.zip` or `tenetora-latest.zip`; new Agent Harness ZIP aliases are not generated.

## PATH, Logs, and Reset

First installation and the bridge install `~/.tenetora/bin/tenetora` and an idempotent user PATH entry. Open a new terminal when the current shell has not reloaded it; do not reinstall because `path_ready=false`. `TENETORA_HOME` can select a managed home, but it must resolve to a safe regular directory.

Interactive output has preflight and install progress. Use `--no-progress`, `--progress always|never`, or `--verbose` to control it. Failure details are retained in `~/.tenetora/logs/` with bounded rotation.

Before a reset:

```bash
tenetora doctor --tools all --scope both --path /path/to/project
```

Remove only content proven to be Tenetora-owned and unmodified. Preserve user Hooks, host registries, trust decisions, third-party plugins, and ambiguous legacy homes for review; do not delete migration backups before acceptance.
