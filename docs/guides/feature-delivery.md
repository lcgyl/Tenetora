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
The CLI runs argv without an implicit shell and retains raw stdout/stderr,
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

Return to [governance](governance.md) or the [lifecycle entry map](entry-map.md).
