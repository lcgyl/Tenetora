# CI Integration

**English** | 简体中文

Tenetora CI checks `.tenetora` structure, rules, guardrails, and evidence. It does not replace the project's own build, test, review, or release process.

## Generate a Template

Preview first:

```bash
tenetora setup-ci --provider all
```

Write after review:

```bash
tenetora setup-ci --provider all --write
```

Supported providers:

| Provider | File | Contents |
| --- | --- | --- |
| GitHub Actions | `.github/workflows/tenetora.yml` | CLI, validate, audit, run-all, and SARIF. |
| GitHub CI | `.gitlab-ci-tenetora.yml` | An independent sidecar job that does not replace the existing pipeline. |
| pre-commit | `.pre-commit-config.yaml` or a separate config | Staged-content and commit-message checks. |

Existing files are preserved unless `--force` is also supplied. The template is a reviewable starting point; retain the project's own build and test jobs.

## Recommended Check Chain

Run from the project root:

```bash
tenetora validate --path .
tenetora run-all --path .
tenetora audit --strict --path .
```

Windows or minimal images without Bash should select the Python runner:

```bash
tenetora run-all --runner python --path .
```

`run-all` can block an unstable or uncontrolled governance state. `audit` is a quality report and must not be presented as a passing business test.

## Git Hooks

After generating pre-commit configuration, install both Hook types explicitly:

```bash
pre-commit install --hook-type pre-commit --hook-type commit-msg
```

`pre-commit` checks staged content. `commit-msg` reads Git's actual message file. Tenetora migrates only old Hooks with an exact managed marker; unmarked user Hooks are untouched and changed files are backed up first.

## CI Boundaries

- CI can run guardrails but cannot approve alignment, commit, push, deploy, or release for the user.
- Issue text, webpages, and scripts should pass `guard --action external-input` before they enter a check; their content is not an authority.
- SARIF/JUnit reports must not contain secrets, local credentials, or concrete user paths.
- Do not persist a tool-local `PYTHONPATH` in CI. Use the release CLI or the exact skill loaded by the host.

## Custom Checks

Place project checks under:

```text
.tenetora/guardrails/custom/
```

Supported entries include `.py`, `.sh`, and executable files. They run from the project root and receive:

```text
AGENT_HARNESS_ROOT=/path/to/project
```

Custom scripts should read only necessary inputs and return stable exit codes.

## Troubleshooting

```bash
tenetora status --tools all --scope both --path . --json
tenetora doctor --tools all --scope both --path .
tenetora validate --path .
```

Fix `BLOCKED`, missing runtime, version drift, or stale evidence before rerunning CI. A green CI job does not prove that a native plugin has executed in a particular host session.
