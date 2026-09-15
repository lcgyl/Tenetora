# Git Safety Rules

Source: Tenetora default rules.

## Authorization

- Commit and push are separate externally visible actions. Perform each only after the user explicitly authorizes that action for the affected repository or clearly identified repository set.
- A request to finish, continue, fix, validate, or complete a task is not commit or push authorization.
- Do not infer authorization from a plan, previous task, tool capability, branch name, or the existence of staged changes.
- Do not rewrite history, force-push, delete branches, reset, clean, checkout away changes, or recursively delete work without explicit approval for that exact operation.

## Worktree And Staging

- Run `git status --short --branch --untracked-files=all` before editing, before staging, and before reporting completion.
- Preserve user work. Do not revert, delete, reformat, restage, or include changes you did not make unless the user explicitly asks.
- A rejected commit is repaired by changing Git's index, not by destroying the worktree: use `git restore --staged <path>` for local configuration and never delete, empty, or rename `.env*`, `*.local.*`, or other project configuration files to pass a gate.
- Inspect both `git diff` and `git diff --cached` before committing.
- Stage only files that belong to the requested task. Leave unrelated modified and untracked files untouched.
- Run `git diff --cached --check` and the task-relevant verification before committing.
- Run `tenetora guard --action commit` after staging and before creating the commit.

## Commit Message Quality

- Every agent-created commit must have a descriptive subject and a detailed body; a one-line message is insufficient.
- Use a conventional subject such as `fix(scope): describe the outcome`, keep it at 72 characters or fewer, and describe the result rather than the activity.
- Write the detailed body in Chinese. The Conventional Commit type and scope may remain in English, but English-only body sections are not acceptable.
- The body must explain `背景`, `变更`, `验证`, and `风险/兼容性`. State `无` explicitly when no known risk or compatibility impact exists.
- Record the exact verification commands and observed results. Do not claim tests passed when they were skipped or not run.
- Explain why the change was needed, important implementation decisions, affected boundaries, migrations, and remaining risks. Do not merely repeat filenames or the subject.
- Do not include secrets, credential values, private keys, local absolute paths, or sensitive customer/project data in the commit message.
- Use a commit message file or repeated `-m` arguments for paragraphs. Do not embed literal `\\n` text as a substitute for real line breaks.

## Multi-Repository And Push Safety

- For repositories with submodules or nested repositories, keep content commits and root pointer commits separate and report which repository changed.
- Push submodule or nested-repository commits before committing or pushing the parent pointer when the project policy requires remote reachability.
- Before push, verify the intended branch, local/remote ahead-behind state, and that every commit being pushed belongs to the authorized task.
- After commit or push, report repository, branch, commit SHA, verification, and whether the remote is synchronized.
