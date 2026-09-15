# Multi-Agent Repository Collaboration

简体中文 | **English**

This guide defines the evidence and Git boundaries when multiple agents or programs work on one project.
Tenetora can detect some shared-state changes, but ordinary file edits cannot be attributed to a specific
agent after the fact. Use real repository boundaries where possible and treat same-directory collaboration as
human-coordinated shared state.

## Choose A Boundary

| Arrangement | Git state | Recommended use | Evidence rule |
| --- | --- | --- | --- |
| Different repositories | Independent indexes and worktrees | Separate services or a parent repository with child repositories | Register changed child gitlinks and bind each child verification to its own proof |
| Same repository, different worktrees | Shared object database, independent indexes and worktrees | Parallel branches or agents editing one repository | Use a worktree per agent; claims remain bound to that worktree's path and content |
| Same repository, same directory | One shared index and worktree | Only when the host requires it | Agents use scoped partial claims; a human decides the final staged set and commit |

The commit guard identifies a shared Git repository without exposing its path. It reports staged-set changes
on the same HEAD, but it cannot prove which agent staged or edited a file. A warning is a coordination signal,
not an attribution record.

## Same-Directory Rules

1. Each agent declares the project-relative paths it owns before editing.
2. A module-level check records `partial-verification` with one or more `--verification-scope` values.
3. Agents must not reset, unstage, or overwrite another agent's paths to make the index look clean.
4. The final committer reviews the complete staged set, runs the full project verification when required, and
   decides which paths belong in the commit.
5. A scoped partial claim is never a completion claim. Completion requires one full goal-covering command and
   the required owner-bound handoff.

Example for an agent that only verified two modules:

```bash
tenetora guard --action claim --claim-kind partial-verification \
  --verification-command "python3 -m unittest tests.test_module_a tests.test_module_b" \
  --verification-status passed \
  --verification-scope src/module-a \
  --verification-scope tests/test_module_a.py
```

When another agent stages a path outside those scopes, the commit guard reports it as uncovered evidence.
That path needs its own fresh claim or a full verification; it must not be silently attached to the first claim.

## Repository Units

A parent repository records a child repository only when Git exposes a real gitlink. Ordinary nested folders
are logical modules, not repository units. Inspect the explicit registry before a parent commit:

```bash
tenetora repository-units inspect --path . --json
```

For a changed gitlink, register evidence after the child repository is clean, its HEAD matches the staged
pointer, and the verification has passed:

```bash
tenetora repository-units repair --path . \
  --gitlink-path services/api \
  --module api \
  --git-head <child-head> \
  --verification-command "python3 -m unittest discover -s tests" \
  --claim-proof <child-claim-proof>
```

The registry is authoritative once it exists. Missing, duplicate, invalid, or orphan entries fail closed;
the older `.tenetora/state/modules/index.json` format remains a migration fallback only when no explicit
registry exists. A claim proof must be a real passed claim in the child repository's governance trail, with a
matching project identity and fresh worktree content fingerprint. A correctly shaped random string is not
evidence.

When the parent has no staged gitlink change, `doctor` reports missing registration or verification evidence
as `ATTENTION` rather than blocking normal AI runtime use. This does not mean commit safety has passed: run
`repository-units inspect` first, then have the owning child-repository agent provide the real verification
command and claim proof. A staged gitlink, index conflict, invalid path, or unreachable target remains
`BLOCKED` in `doctor` and the commit guard. Diagnostics never print repair commands containing
`<passed command>` or `<child-claim-proof>` placeholders.

## Separate Worktrees

Separate worktrees are preferred for concurrent edits in one repository:

```bash
git worktree add ../project-agent-a -b agent-a
git worktree add ../project-agent-b -b agent-b
```

Each worktree has its own index, so an agent cannot accidentally stage the other worktree's changes. The
repository identity remains shared for diagnostics, while verification claims bind to the worktree contents.
Before merging, review the resulting Git index and run the parent project's declared full command.

## What Tenetora Does Not Infer

- A changed ordinary file does not identify the writing agent.
- A Hook or runtime observation does not prove that a model performed a specific edit.
- A passing partial claim does not authorize commit, push, tag, or release.
- A parent claim does not replace a child claim or create evidence for an unregistered logical module.
- A registry entry does not make a dirty child repository safe to commit.

Use `tenetora status --verbose --path .` and the JSON forms of the commands above when a coordination state
needs to be handed from one agent to another. Preserve the human decision about the final index outside the
machine evidence; Tenetora records the boundary and the checks it can actually prove.
