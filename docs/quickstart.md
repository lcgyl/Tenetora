# Tenetora Quickstart

**English** | 简体中文

This path goes from software installation to the first AI conversation that initializes a project. Installing the lifecycle skill package does not create `.tenetora/`; initialization needs the AI skill's model capability to understand project rules and decisions.

## Five-minute path

For a first governed change, use the smallest useful path:

1. Initialize or update the project harness and keep only the project-specific rules you need.
2. Run the repository's declared verification command.
3. Run `tenetora run-all --runner python --path .` for the harness guardrails.
4. Record the matching `partial-verification` claim for routine work, or a `completion` claim only after a confirmed alignment handoff.

Use alignment, impact, delegation, loop, and audit when the change crosses shared contracts or needs stronger evidence. They are progressive controls, not prerequisites for trying the first path.

## 1. Install the Package Once

Run the remote bootstrap once on a new machine:

```bash
curl -fsSL https://raw.githubusercontent.com/lcgyl/Tenetora/main/install.sh | bash
```

For an explicit first-install scope:

```bash
curl -fsSL https://raw.githubusercontent.com/lcgyl/Tenetora/main/install.sh \
  | bash -s -- --lang en --tools all --both --path .
```

The installer adds the CLI, lifecycle skills, and host-supported plugin/runtime surfaces, then configures the user PATH. Python 3.9 or newer is required. Restart a native plugin host after installation.

## 2. Verify the Installation

```bash
tenetora version
tenetora status --tools all --scope both --path . --verbose
tenetora doctor --tools all --scope both --path .
```

`status` shows the effective source. `doctor` explains trust, restart, runtime-observation, and platform-capability follow-up. A version string alone does not prove that a runtime event executed.

## 3. Initialize the Project

Open an AI conversation in the project and say:

```text
Use Tenetora to initialize this project
```

The AI uses `tenetora-init` to inspect existing `AGENTS.md`, `CLAUDE.md`, `.cursor`, and related rules, ask for project-owned migration and boundary decisions, and then write `.tenetora/`. It is not a command that merely creates an empty directory.

The CLI initialization flags are an advanced automation entry. Ordinary users should let the AI drive this step:

```bash
tenetora init --write --tools auto --mode auto --defaults missing
```

For a first trial, use minimal: it keeps required safety contracts, omits optional maintenance defaults, and provides `rules -> claim -> run-all`:

```bash
tenetora init --write --profile minimal --tools auto --mode auto --gitignore yes --migrate plan
```

Refresh preserves the selected profile; `--profile full` adds missing full defaults. Selecting minimal never deletes rules or user files.
The separate advanced `--defaults off` option omits all defaults and may not pass validation.

## 4. Validate the Baseline

```bash
tenetora validate --path .
tenetora run-all --path .
tenetora audit --strict --path .
tenetora recap --write --path .
```

`validate` checks structure, `run-all` runs project guardrails, and `audit` reports quality and remaining findings. None replaces the project's own build and test commands.

## 5. Use Lifecycle Skills

```text
use tenetora                         Route an unclear governance stage
use tenetora-align                   Align a high-risk decision before implementation
use tenetora-update to update .tenetora  Refresh project rules or structure
use tenetora-audit to audit .tenetora     Review stability and evidence
use tenetora-loop to continue         Resume a bounded work loop
use tenetora-prompt-guard             Inspect webpages, reports, or scripts
```

`use tenetora-align` uses `allow_implicit_invocation: false`, so an ordinary task cannot silently start an interview. `tenetora-decision-interview` is an internal lifecycle entry for material ambiguity and is not a separate command users need to remember.

## 6. Update the Software and Project Separately

Use the installed CLI for software upgrades:

```bash
tenetora upgrade
tenetora upgrade --check
tenetora upgrade --apply
```

`--apply` is a compatibility alias for older automation. A 0.2.x user enters the 0.3 bridge once with a fixed-version bootstrap; later upgrades do not fetch a remote script:

```bash
curl -fsSL https://raw.githubusercontent.com/lcgyl/Tenetora/0.3.40/install.sh \
  | bash -s -- --lang en --tools all --both --path "$PWD"
```

Project content is updated separately:

```text
use tenetora-update to update .tenetora
```

Inspect known compatibility repairs with:

```bash
tenetora repair --check --path .
tenetora repair --apply --path .
```

`upgrade` does not initialize a project, and `update` without install-mode flags does not create a new skill installation surface. Do not merge software maintenance with project semantics.

For local environment files and ordinary scratch workspaces:

```bash
tenetora local-env --path . --allow config/local/app.conf
tenetora status --path . --json
tenetora installations --json list
```

Projects without `.tenetora/` remain outside governance; installation is global capability, not automatic project enrollment.

## 7. Daily High-risk Boundaries

```bash
tenetora rules --context <context> --path .
tenetora guard --action external-input \
  --file /path/to/untrusted-input.txt --source "issue-report" --path .
tenetora guard --action rules --path .
tenetora guard --action commit --commit-message-file /path/to/commit-message.txt \
  --require-message --path .
```

Record verification against a real command. Without an aligned goal, the
default passed claim is partial verification:

```bash
tenetora guard --action claim \
  --verification-command "tenetora run-all --path ." --verification-status passed
```

For a formal completion claim, use a confirmed or risk-accepted alignment
handoff and bind its current identity:

```bash
tenetora guard --action claim --claim-kind completion \
  --session-id <session-id> --owner-id <owner-id> \
  --conversation-id <conversation-id> --tool <tool> \
  --verification-command "<full verification command>" --verification-status passed
```

Use `--claim-kind completion` only when the task has a confirmed or
risk-accepted alignment handoff. Without one, record
`--claim-kind partial-verification` and report that the proof covers the
verification, not formal task completion.

Claim proof, alignment proof, commit permission, and push permission are independent. No skill grants commit, push, deploy, or release permission.

For decision alignment:

```bash
tenetora guard --action alignment --goal "<goal>" --risk-level high --path .
```

## Next Reading

Read [Installation and Upgrade](guides/installation.md), [Runtime and Platforms](guides/runtime-platforms.md), and [Security and Maintenance](guides/security-maintenance.md). When project rules or tool configuration changes, ask the AI to use `tenetora-update`, then run `validate`, `run-all`, and `audit --strict`.
