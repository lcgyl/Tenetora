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

## Release-tag gate

The canonical `.gitlab-ci.yml` avoids rerunning the generic suite on version tags. A
release tag first runs `verify_main_pipeline.py`, which requires a successful `main`
push pipeline for the exact tagged commit and verifies the required test, Python 3.9,
package, and smoke jobs through GitHub's read-only API. A missing, inaccessible, or
incomplete API response fails closed. The tag pipeline then runs only packaging and
release-specific checks; branch and merge-request pipelines keep the full suite.

## R2 artifact pointer

The private GitHub pipeline remains the only producer. The manual `publish_r2_stable` job and the
automatic protected-preview-tag `publish_r2_preview` job reuse the already gated package artifact, upload immutable objects under
`stable/<version>/` or `preview/<version>/`, verify them, and update that channel's `latest.json`
last. They require protected/masked GitHub variables `R2_ACCOUNT_ID`, `R2_BUCKET`,
`R2_ACCESS_KEY_ID`, and `R2_SECRET_ACCESS_KEY`; the jobs do not read a repository `.env` file.
Because the variables are protected, release tag patterns must also be protected in the
project settings. The jobs fail closed when `CI_COMMIT_REF_PROTECTED` is not `true`.
Keep tag protection and preview retirement permissions aligned: a protected preview tag
may require a maintainer-operated retirement step before its Git tag is deleted.
Stable publication remains manual; a matching protected preview tag publishes automatically and
fails the pipeline if the upload or verification fails. These R2 jobs are the only new artifact publisher. After the Worker download
smoke test passes, public release jobs publish refs and release notes only; historical public
attachments remain untouched. The installer/upgrade use the pointer by default; set
`TENETORA_RELEASE_INDEX_BASE=https://<worker-host>` only for another compatible Worker.

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
