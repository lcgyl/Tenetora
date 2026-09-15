# Tenetora Use Cases

简体中文 | **English**

These scenarios describe where Tenetora changes an engineering outcome. Each scenario states the failure, the governed path, the observable result, and the remaining boundary.

For the mechanics behind each referenced feature, see the [Capability Guide](capabilities.md).

## Several Tools or Models Work on One Repository

**Failure without governance:** Claude Code, Codex, and another model read different instructions. A later model resumes an old conversation goal or uses a completion claim created for a different task.

**Tenetora path:** durable rules live under `.tenetora/`; tool entry files point to that record; SessionStart restores the runtime contract; alignment and loop state are owner/session bound; completion claims include project, goal, identity, and worktree evidence.

**Observable result:** each host can report its effective skill/plugin/runtime source, and another conversation cannot silently mutate or complete the first conversation's alignment.

**Boundary:** Tenetora cannot transfer a model's hidden chain of thought. A handoff still needs an explicit goal, current state, verification result, and next action.

## One Conversation Switches Models After a Limit

**Failure without governance:** model A is rate-limited halfway through a task. The host starts model B with a new local identity, so the task either loses its alignment or creates a second active goal. A later completion report cannot tell which model verified which state.

**Tenetora path:** the host keeps one `conversation_continuity_id` and gives each run a distinct `execution_attempt_id`. It records provider, model, and attempt status. A `rate-limited` attempt leaves the owner-bound alignment active; model B appends a new attempt and reads the same goal. Missing continuity is read-only blocked, and a stale concurrent write is rejected by the session revision check.

**Observable result:** `tenetora alignment --execution-list --conversation-continuity-id <id> --json` shows both attempts, their models, and their statuses. The alignment session has one goal and one owner instead of two competing goals.

**Boundary:** continuity does not transfer hidden context or authorize a new decision. Model B must still consume the current handoff and verify the work; a different conversation must not reuse the continuity identity.

## A High-Risk Refactor Has Unresolved Decisions

**Failure without governance:** implementation starts before the team chooses compatibility, migration, rollback, or non-goals. The first code change silently becomes the decision.

**Tenetora path:** the user explicitly enters decision alignment; the interview asks one material question at a time; the handoff records the decision, scope, non-goals, risks, and acceptance criteria; implementation permission remains separate.

**Observable result:** the implementation plan can point to a goal-matching decision record instead of reconstructing intent from chat history.

**Boundary:** alignment is not product discovery and does not choose business policy for the user. If the owner will not decide, the task remains blocked or proceeds only with explicitly accepted risk.

## A Shared API, Schema, Hook, or Plugin Contract Changes

**Failure without governance:** a definition is edited, known callers are missed, generated artifacts drift, and compilation becomes the first impact analysis.

**Tenetora path:** change-impact preflight records the contract, analysis tool, known references, affected files, and baseline before editing; the same structural query is repeated after the change; project verification completes the preflight.

**Observable result:** a commit can be checked for matching pre-edit inventory, post-edit rescan, and verification evidence.

**Boundary:** impact analysis is still bounded by available indexes and tests. Tenetora records the evidence; it does not pretend the dependency graph is complete when the tools cannot prove it.

## An External Report Contains Commands or Prompt Injection

**Failure without governance:** an agent treats pasted issue text, a web page, or a generated report as trusted instructions and executes a command, exposes local data, or rewrites project rules.

**Tenetora path:** prompt guard inspects the content before semantic use; risky actions remain behind their own guards; only task-relevant, redacted content is passed into later review.

**Observable result:** suspicious instructions are reported as findings, and the report cannot grant itself local execution or rule-edit authority.

**Boundary:** prompt guard is a mechanical first line, not a guarantee that every social-engineering attempt is detectable. Security-sensitive interpretation still needs a qualified reviewer or explicit human decision.

## A Long Task Is Interrupted or Continued Later

**Failure without governance:** “continue” resumes stale work, a loop runs without a stop condition, or a new conversation inherits another owner's active state.

**Tenetora path:** bounded loop state records the current goal, exit criteria, latest verified step, blocker, and next prompt; identity checks prevent automatic adoption by another session; repeated blockers stop the loop.

**Observable result:** continuation begins from a small, inspectable state record and ends when the goal is met, approval is needed, verification passes with no next step, or the same blocker repeats.

**Boundary:** loop state is resumable evidence, not ground truth. The agent must still compare it with the newest user request and current source before acting.

## “Done” Must Mean More Than a Passing Local Test

**Failure without governance:** a focused check passes, a full command fails later, but an older success is still used to claim completion.

**Tenetora path:** partial, completion, blocked, and failed claims are distinct; completion binds the declared full command to the current goal and Git/worktree fingerprint; newer failures invalidate older completion evidence; ordinary Stop remains non-blocking.

**Observable result:** reviewers can distinguish “this component passed” from “this task is complete”, and a changed worktree requires verification again.

**Boundary:** a claim proves that a command ran and satisfied its contract. It does not prove that the command itself is a complete test strategy; the project owns that verification definition.

## One Machine Has Several Existing Projects

**Failure without governance:** upgrading Tenetora from project A updates only A, while project B keeps an old skill, plugin, Hook, or legacy `.harness` directory.

**Tenetora path:** machine installation state records global and project surfaces; SessionStart observes governed projects; a scope-less upgrade performs bounded automatic discovery and updates existing surfaces only; proven legacy governance directories migrate transactionally.

**Observable result:** the upgrade reports project surfaces and governance-only projects separately, can preview discovery, and does not create project skills merely because a legacy governance directory exists.

**Boundary:** automatic discovery is bounded for privacy and performance. Projects outside observed, registered, or configured roots can be added with `tenetora installations discover --root <root>`.

## A Team Needs Reviewable Local and CI Boundaries

**Failure without governance:** local agents bypass repository policy, while CI sees only the final files and cannot explain which rules, decision, or verification evidence led there.

**Tenetora path:** project guardrails run locally and in CI; commit guard checks staged scope, secrets, message detail, impact state, and review evidence; SARIF/JUnit outputs integrate findings with existing CI systems.

**Observable result:** a failed boundary has a named check and remediation rather than a vague instruction violation.

**Boundary:** Tenetora guards do not grant commit, push, deployment, or release authority. Authorization remains with the user and the repository's own access controls.
