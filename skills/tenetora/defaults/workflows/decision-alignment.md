# Decision Alignment Workflow

Source: Tenetora default workflow.

Use this workflow only for `.tenetora`-governed engineering work with material risk or unresolved user-owned decisions.

1. Run `tenetora route --message "<request>" --json` and inspect `alignment.recommendation`.
2. Investigate repository facts before asking the user. Do not turn discoverable facts into interview questions.
3. For cross-boundary delivery planning, consult `.tenetora/workflows/end-to-end-skeleton-first.md` and record `applicable` or `exempt` with a reason. If applicable, align on participating boundaries and the required skeleton proof only when those contain material user-owned choices.
4. `tenetora-align` is an explicit user entry and must not be model-invoked implicitly. Inside an active lifecycle, `tenetora-decision-interview` may handle material ambiguity.
5. Ask one decision question at a time. Explain the recommendation, evidence, assumptions, confidence, alternatives, costs, and custom-answer path.
6. Record only concise structured decisions. At semantic convergence use `tenetora alignment --await-confirmation`. The eighth recorded answer enters checkpoint automatically; wait for the user instead of issuing another checkpoint command. Awaiting confirmation uses a seven-day review lease rather than the one-hour execution lease; an expired confirmation wait may be explicitly resumed only by its matching owner and must request confirmation again.
7. Run `--confirm` or `--accept-risk` only after an explicit user message. Preserve `--confirmation-source user-message`, the matching owner or conversation-continuity actor id, and the Hook-provided event id. Bare CLI confirmation is rejected. Historical v1-v3 handoffs remain readable but are legacy-unattested and cannot authorize high-risk execution or completion.
8. Before restricted high-risk work, run `tenetora guard --action alignment --goal "<goal>" --risk-level high`.
9. A confirmed or risk-accepted handoff records shared understanding only. It does not authorize implementation, commit, push, deployment, or release. Its user-message event is auditable provenance, not cryptographic proof of a human actor.
10. Let the user choose planning, SMART tasks, bounded loop execution, another lifecycle action, or stop. Never continue in the background.
