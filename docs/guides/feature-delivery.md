# Evidence-bound Feature Delivery

**English** | 简体中文

Use `feature start` and `task-add --allow-path <path>` to freeze task scope,
dependencies and required evidence. An empty path scope covers the whole project.
Task requirements remain fixed when implementation reports arrive.

Before generating an execution prompt, running a check or accepting a task,
freeze its explicit contract:

```sh
tenetora feature task-contract --feature example --task T01 \
  --deliverable 'The scoped implementation and its project check' \
  --acceptance 'The check observes the required result and failure behavior' \
  --verification-command 'python3 -m unittest tests.test_example' \
  --timebox 'This delivery iteration' \
  --rationale 'Why this is relevant and feasible within that iteration'
```

Repeat `--acceptance` for each observable criterion. Scope comes from the task;
an empty allowed-path list explicitly means the whole project. This checks
mechanical completeness, not whether a written criterion is actually achievable,
relevant or sufficiently strong. Reviewers still assess those properties.
Execution receipts bind the entire frozen contract as well as the source.
Command identity revision 2 preserves the exact command text, including quoted,
escaped and edge whitespace, newlines and shell bodies. Use that exact text
when recording or checking a claim; formatting equivalence is not inferred.
Older claims/receipts require fresh verification because their original command
bytes cannot be recovered from the old normalized digest.

Use repeatable `task-contract --contract-file <existing-project-relative-file>`
to declare real schema or contract artifacts consumed by a task. Their exact
bytes are pinned at freeze time and automatically join the execution subject;
the claim must cover both reported source files and these artifacts. Missing,
redirected or changed artifacts block execution and acceptance, including
upstream dependency revalidation. Refreeze explicitly with `--reset-evidence`
and rerun the project's own validation command after a contract changes.
The engine treats artifacts as files; it does not infer API compatibility or
replace the project's schema/generation/runtime checks.
Changing a checked contract requires explicit `--reset-evidence`; this reopens
the task and requires a new check, report and claim without deleting history.

Run `feature check` explicitly with the feature/task, repeatable `--file`,
`--verification-command`, `--evidence-level` and `--evidence-mode` arguments.
The CLI runs argv without an implicit shell and retains bounded, redacted stdout/stderr,
the actual exit code, duration and source fingerprint. Use an explicit shell
command when the check needs pipes or other shell syntax.

Evidence labels describe observed checks; they do not automatically detect a
real database or browser. Label static, mock, real and mixed evidence honestly.
Import a UTF-8 report containing Changed Files, Evidence-Level and Evidence-Mode;
record a fresh claim covering every task file, then use `feature verify` with
`--claim-proof`, the same command and `--outcome accepted`.

Importing a report never executes its instructions. Changed reports/logs,
unreported or uncovered files, downgraded evidence and failed runs cannot
complete a task. Dependencies must be completed before acceptance, and their
claims, source, reports, receipts and logs must still validate recursively.
Revalidate an upstream task after its evidence changes before accepting a child.
`feature outcome --outcome reopened|rejected|unknown` revises acceptance;
any blocked task keeps the whole feature blocked. JSON owns status; acceptance
Markdown is a projection. Plan and task Markdown also carry managed execution
blocks; text outside those markers is preserved. Unmarked legacy documents
remain untouched and appear in `status` under `unmanaged_projections`.
Legacy tasks missing a contract remain readable but require an explicit contract
and fresh evidence before new acceptance. `status` is read-only: historical
task `status` is distinct from `current_status` and `current_validity`.
Markdown records the last mutation's snapshot; use `status` for live validity.
Task completion never grants plan approval (`plan_approval` remains `unrecorded`).
The compatibility `--duration-ms` argument cannot
override the measured check duration.

## Criterion coverage and prompt diagnostics

Newly frozen contracts assign `A01`, `A02`, ... to the ordered acceptance criteria.
A single criterion reuses the main check by default. With multiple criteria,
declare each mapping explicitly using repeatable `--criterion-check A01`
(reuse the main command), `--criterion-check 'A02=python3 check_rollback.py'`,
or `--criterion-review A02`. These options belong to `feature task-contract`.
Unmapped criteria remain `unverified`; a passing task command cannot complete them.
The check runner executes the declared commands without an implicit shell and
binds each exit code and redacted log to the frozen contract and source. Reusing a
command is declared coverage, not proof that its assertions cover the criterion.

Record an explicitly declared review after the current check succeeds:

```sh
tenetora feature criterion-review --feature example --task T01 --criterion A02 \
  --file evidence/rollback-review.md --outcome accepted --reviewer-kind human
```

Use `agent` or `unknown` when appropriate; reviewer kind is a declaration, not
authenticated identity or proof of independent review. The report must be an
unchanged project file. Missing, rejected, stale or altered review evidence blocks
completion. A new execution or contract reset requires a new review. `feature status`
exposes `criterion_coverage`, and the acceptance projection uses `T01/A01` rows
alongside the unchanged task summary. Legacy contracts remain readable as
`legacy-task-level`; they do not acquire invented per-criterion evidence.

Both frontend and backend prompts retain frozen text and return `warnings` for
explicit instructions to re-plan or widen scope. Negations and quoted examples
are excluded by the diagnostic. It is a bounded phrase check, not semantic proof
that a task is well planned; resolve an actual conflict with the planner before
execution. It does not rewrite the contract or grant commit/push permission.

## Observed checks and strict commit evidence

This section describes unreleased main-branch behavior. Published preview `.018`
does not provide `verify run` or the observed-receipt strict policy.

For a check outside a feature, explicitly execute argv after `--`:

```sh
tenetora verify run --timeout 600 --json -- python3 -m unittest tests.test_example
tenetora guard --action claim --claim-kind partial-verification \
  --execution-receipt '<execution_receipt from verify run>'
```

Use repeatable `--scope <project-relative-path>` on `verify run` for a scoped
check. `--command` instead parses command text as argv; it does not enable shell
syntax. Prefer argv for paths/quoting, especially on Windows. A feature check
also returns an observed execution reference. Importing that reference validates
the receipt, source, command, logs and canonical run facts without rerunning it.
Nonzero exit, timeout, changed source, incomplete runs and later overlapping
failures prevent reuse. Logs retain at most 2 MiB per stream plus a truncation
notice, redact known secrets, and record the observed byte count.

Claims without an execution receipt require an explicit `--verification-status`
and are marked `declaration`. They remain available for ordinary workflows;
`guard --action commit --strict-evidence` requires observed evidence and checks
the staged content, type and executable bit against the actual verified source.
Staging identical content after a check preserves reuse. Evidence proves the
controlled process observation, not assertion quality or cryptographic identity
against a host with equal filesystem access. A claim grants no Git permission.
Source fingerprint revision 5 separates verified missing paths from actual
content, so committing a checked deletion or rename preserves reuse. Claims
from older fingerprint revisions require a fresh check; they are not rewritten.

Return to [governance](governance.md) or the [lifecycle entry map](entry-map.md).
