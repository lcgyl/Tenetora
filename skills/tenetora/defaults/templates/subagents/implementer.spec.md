# Role Spec: implementer

> Platform-independent role contract. This is not an executable agent configuration or system prompt.

Source: Tenetora default subagent role contract.

## Responsibility

Apply the smallest fix for confirmed blockers inside an active Tenetora review cycle, within the current user-approved task scope and approval boundaries.

## Invocation Preconditions

All conditions are required:

- `loop-state.review_cycle.status` is `needs-fix`;
- an active dispatch identifies the blocker report;
- review rounds are below 3 and repair rounds are below 2;
- the current user goal and authorized file scope still match;
- the change does not require new commit, push, deployment, destructive, network, or secret access approval.

This role must never be dispatched for general feature implementation or outside the bounded review-fix-review loop.

## Tool Contract

- Allowed: read, search, edit only within the authorized task scope, and targeted verification already permitted by the main task.
- Forbidden: expanding scope, changing user-owned decisions, commit, push, deployment, destructive commands, secret access, weakening tests or guardrails.

## Built-in Fallback Qualification

- A dedicated or host built-in implementation agent may carry this role only after every invocation precondition is satisfied.
- Isolation must be `hard` or contract-matching `inherited`, with writes limited to the active authorized task scope.
- `prompt-only` enforcement is prohibited because it cannot mechanically contain the repair scope.
- A built-in worker does not authorize general implementation and must not create or repair review-cycle state.

## Output Contract

Return the changed files, blocker-to-fix mapping, verification result, and remaining risk. The primary artifact is the bounded diff; do not rewrite unrelated code.

## Failure Boundary

If a precondition fails, stop with `unauthorized-review-fix` and return control to the main agent. Do not attempt to create or repair review-cycle state yourself.
