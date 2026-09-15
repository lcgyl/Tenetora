# End-to-End Skeleton First

Source: Tenetora default workflow.

Use this planning workflow to prevent deep refinement inside one component while the delivery path is still incomplete. The governing idea is also called a Walking Skeleton or `端到端骨架优先`.

## Applicability Decision

Every implementation plan must record one of these outcomes:

- `applicable`: the requested value crosses meaningful runtime, ownership, process, data, or tool boundaries and needs an integrated proof.
- `exempt`: the workflow does not materially improve delivery; record a concrete reason in the plan or alignment handoff.

Do not classify by file count or directory count alone. A task can touch several files inside one boundary and still be exempt. A small change can be applicable when it changes a producer/consumer contract, UI/API/persistence path, CLI/runtime/plugin path, or another cross-boundary flow.

Common valid exemptions include a narrow single-boundary fix, research whose explicit goal is exploration, and a genuinely sequential dependency where a downstream boundary cannot exist until an upstream result is complete.

## Minimum Safe Skeleton

The skeleton is the smallest runnable path that proves the requested value crosses every participating boundary. "Minimum" never means bypassing essential safety.

Retain what is necessary for:

- authentication, authorization, and secret boundaries;
- data integrity, schema/contract compatibility, and failure handling;
- rollback or a bounded recovery path for risky changes;
- the smallest meaningful smoke or end-to-end verification;
- enough diagnostics to distinguish a failed boundary from a missing implementation.

Defer only non-blocking refinement such as broad edge-case expansion, performance tuning, cosmetic cleanup, optional abstractions, and exhaustive tests that are not required to trust the skeleton.

## Planning Flow

1. Identify participating components and the boundaries between them.
2. Decide contracts first: inputs, outputs, ownership, compatibility, failure behavior, and rollback expectations.
3. Define the minimum safe runnable behavior for each boundary.
4. Order SMART tasks so each early task enables another boundary or the integrated path.
5. Wire the full path and run the declared skeleton verification.
6. Treat the skeleton gate as passed only when the integrated path is observable and the essential safety checks pass.
7. After the gate passes, refine components in the order that provides the most value or risk reduction.

## Decision Test

Before deepening work inside one component, ask:

> Does this step enable another boundary, unblock the integrated path, or provide evidence required to trust the skeleton?

- `yes`: it is skeleton work and can proceed before the gate.
- `no`: it is refinement and should be listed under deferred refinement until the gate passes.

## Skeleton Gate

The plan must define observable evidence for all applicable items:

- every participating boundary has a minimum implementation;
- contracts agree across producers and consumers;
- the main end-to-end path runs through all boundaries;
- essential security and data-integrity checks pass;
- the declared smoke/end-to-end command or manual proof passes;
- deferred refinement is explicit and has not been silently treated as completion.

## Integration Boundaries

- Decision alignment uses this workflow only when cross-boundary planning raises material user-owned choices.
- Change-impact preflight still governs shared-contract edits before implementation.
- The bounded loop prioritizes the next skeleton-enabling step until the gate passes, without adding background execution or an independent state machine.
- Verification and completion-claim rules still determine whether the final result can be reported as complete.
