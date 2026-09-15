# Change-Impact Preflight

Source: Tenetora default workflow.

Use this workflow before editing when a task may change a shared contract: public or exported signatures, constructors, interfaces, schemas, Hook payloads, CLI options, plugin manifests, runtime launch assumptions, cross-module APIs, or behavior with multiple consumers.

## 1. Establish A Baseline

- Run the smallest available compile, typecheck, or focused test before editing.
- If no baseline can run, record the concrete reason; do not describe an untested baseline as passing.
- Keep pre-existing dirty paths separate from this task.

## 2. Classify The Contract

- State the contract kind, target symbols or files, compatibility boundary, intended consumers, and non-goals.
- Include production code, tests, mocks, fixtures, generated code, configuration, docs, release packaging, and runtime environment assumptions when relevant.
- Read `.tenetora/rules/change-impact.md` when the project provides language- or framework-specific impact rules.

## 3. Build The Impact Inventory

Use the strongest available source in this order:

1. CodeGraph, IDE reference search, language server, compiler index, or another structural symbol graph.
2. Language-native compiler, typechecker, dependency report, schema validator, or manifest validator.
3. `rg` or another literal text search only as a fallback or zero-omission rescan.

Do not rebuild a structural call graph with repeated text searches when a structural index is available. Record every known consumer and each expected adaptation type before editing.

## 4. Record Before Editing

```bash
tenetora impact --start \
  --kind <constructor|public-api|interface|schema|cli|hook|manifest|runtime|cross-module|rename|behavior|other> \
  --summary "<bounded change>" \
  --impact-tool <codegraph|ide|language-server|compiler|typechecker|rg> \
  --symbol "<contract or symbol>" \
  --affected-file <project-relative-path> \
  --reference-count <count> \
  --baseline-command "<command>" \
  --baseline-status <passed|failed>
```

Use `--baseline-status skipped --baseline-reason "<reason>"` only when no meaningful baseline can run. Repeat `--symbol`, `--scope`, `--affected-file`, and `--impact-tool` as needed.

If an authorized task must replace a valid active preflight, first read `tenetora impact --status --json`, then bind the replacement to that exact identity with `--replace --expected-preflight-id <id>`. An unbound replacement is rejected so concurrent tasks cannot silently replace each other.

## 5. Adapt In One Bounded Batch

- Update the definition and all inventoried consumers together.
- Check parameters, return values, imports, mocks, implementation order, generated sources, serialized fields, config keys, docs, release assets, launchers, PATH/PYTHONPATH, and platform schemas as applicable.
- Preserve unrelated user changes and stop if an unowned compatibility decision appears.

## 6. Rescan And Verify

- Repeat the same structural impact query after editing.
- Use literal search to confirm old names, signatures, fields, paths, and schema fragments are absent where they must be absent.
- Run the smallest focused compile/typecheck/test, then the broader project gate required by `.tenetora/workflows/verification.md`.
- A compiler error may reveal an unknown impact, but it must not replace the planned inventory and rescan.

## 7. Complete The Preflight

```bash
tenetora impact --complete \
  --rescan-command "<repeated impact query>" \
  --rescan-status passed \
  --verification-command "<focused verification>" \
  --verification-status passed
```

If either result failed, record the real failed status, fix the bounded issue, and rerun completion. Use `tenetora impact --cancel --reason "<reason>"` only when the authorized change is abandoned.

## 8. Reuse Completed Evidence

- An active preflight remains bound to the starting `HEAD`; if `HEAD` changes before completion, stop and start a new preflight. The failure includes the recorded/current heads and a bounded `reflog -1` diagnostic.
- A completed preflight records a content snapshot for its affected paths. Later staging or committing the exact content does not make it stale, and an unrelated commit does not invalidate it.
- A later change to an affected path invalidates the completed preflight even when `HEAD` is unchanged. The commit guard reports the changed paths so the smallest required impact rescan can be selected.
- Legacy completed states without a path snapshot stay strict: any later `HEAD` change requires a new preflight rather than an implicit migration.

## Boundaries

- Impact detection is heuristic and cannot prove all indirect behavior.
- Commit warnings are non-blocking backstops; the pre-edit workflow is the primary control.
- Do not record source text, prompts, tool output, absolute paths, credentials, or private tool configuration in impact state.
- Do not claim counterfactual time savings. Report only observed references, affected paths, rescans, and verification results.
