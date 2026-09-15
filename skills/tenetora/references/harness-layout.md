# Harness Layout

Use this baseline for new projects and as the target shape for existing projects.

```text
.tenetora/
├── INDEX.md
├── README.md
├── agents/
│   ├── generic.md
│   ├── codex.md
│   ├── claude.md
│   ├── cursor.md
│   ├── opencode.md
│   ├── researcher.md
│   ├── planner.md
│   ├── implementer.md
│   ├── reviewer.md
│   ├── debugger.md
│   └── gardener.md
├── rules/
│   ├── project.md
│   ├── build-and-deps.md
│   ├── testing.md
│   ├── documentation.md
│   ├── git.md
│   └── security.md
├── workflows/
│   ├── task-start.md
│   ├── implementation.md
│   ├── verification.md
│   ├── debugging.md
│   └── completion.md
├── skills/
│   ├── code-search.md
│   ├── document-conversion.md
│   └── harness-governance.md
├── templates/
│   ├── feature-design.md
│   └── implementation-plan.md
├── wiki/
│   ├── project-map.md
│   ├── technology.md
│   ├── architecture.md
│   ├── tool-config.md
│   └── glossary.md
├── docs/
│   ├── architecture/
│   ├── conventions/
│   ├── design/
│   ├── plans/
│   └── reference/
├── guardrails/
│   ├── quality-gates.md
│   ├── lint-rules.md
│   ├── ci.md
│   └── checks/
│       ├── run-all.sh
│       ├── secret-scan.sh
│       ├── local-path-scan.sh
│       ├── stale-doc-scan.sh
│       ├── test-framework-drift-scan.sh      # optional, evidence-derived
│       └── dependency-version-drift-scan.sh  # optional, evidence-derived
├── automation/
│   ├── worktree-verify.sh
│   ├── doc-gardening.md
│   ├── cleanup-agent.md
│   ├── environment-review.md
│   └── dependency-review.md
├── state/
│   ├── current-evidence.json
│   ├── features.json
│   ├── loop-state.json
│   ├── score-history.json       # optional, written by audit --record-history
│   ├── benchmark-history.json   # optional, written by benchmark --write
│   ├── modules/
│   │   ├── index.json
│   │   └── <module>/evidence.json
│   └── progress.md
└── changes/
    ├── INDEX.md
    ├── README.md
    ├── archive/
    ├── backups/
    ├── update-candidates/
    ├── YYYY-MM-DD-harness-bootstrap.md
    ├── YYYY-MM-DD-extraction-evidence.json      # schema v2, includes modules/policies/source hashes
    ├── YYYY-MM-DD-extraction-report.md
    ├── YYYY-MM-DD-HHMMSS-update-report.md
    ├── YYYY-MM-DD-HHMMSS-update-diff.patch
    └── YYYY-MM-DD-recap.md
```

## Directory responsibilities

| Path | Lifecycle | Responsibility |
| --- | --- | --- |
| `INDEX.md` | canonical | Short high-signal entry for agents after cold start or context compaction |
| `README.md` | canonical | Full operating contract and directory map |
| `agents/` | canonical | Tool-specific entry notes and compatibility guidance |
| `rules/` | canonical | Stable hard rules that can later become checks |
| `workflows/` | canonical | How agents start, implement, debug, verify, and finish work |
| `skills/` | canonical | Project-local operating guides, not Codex system skills |
| `templates/` | canonical | Project-neutral templates for design, planning, task handoff, and review |
| `wiki/` | canonical | Project background, maps, architecture, vocabulary |
| `docs/` | canonical | Durable project knowledge: architecture, conventions, design, plans, reference |
| `guardrails/` | canonical | Constraint candidates, quality gates, executable baseline checks, evidence-derived optional checks, lint rule design, CI command candidates |
| `automation/` | canonical | Long-running maintenance templates and isolated verification helpers |
| `state/current-evidence.json` | canonical | Pointer to the evidence/report pair current tools should trust |
| `state/modules/index.json` | canonical | Index of per-module evidence files for monorepo-scoped audit |
| `state/modules/<module>/evidence.json` | canonical | Per-module evidence subset used by `audit --module <module>` |
| `state/loop-state.json` | canonical | Bounded loop state, stop signals, next prompt, and transition history |
| `state/features.json` | canonical | Feature state that may be carried across sessions |
| `state/score-history.json` | auditable | Audit score history written by `audit --record-history` |
| `state/benchmark-history.json` | auditable | Benchmark run history written by `benchmark --write` |
| `state/progress.md` | auditable | Latest validation, audit, and recap summary |
| `changes/INDEX.md` | canonical | Short index for current change artifacts |
| `changes/*-extraction-evidence.json` | auditable | Deterministic source evidence and hashes |
| `changes/*-extraction-report.md` | auditable | Human-readable evidence summary |
| `changes/*-update-report.md` | auditable | Update run summary |
| `changes/*-migration-plan.*` | auditable | Migration decisions and provenance |
| `changes/*-recap.md` | auditable | Validation/audit snapshot |
| `changes/archive/` | auditable | Historical evidence, reports, migration plans, and recaps |
| `changes/update-candidates/` | ephemeral | Short-lived semantic candidates awaiting review |
| `changes/archive/update-candidates/` | ephemeral | Archived candidate snapshots ignored by default |
| `changes/backups/` | ephemeral | Recovery copies for approved entrypoint or replacement operations |
| `changes/*-update-diff.patch` | ephemeral | Temporary review patch, safe to archive after decision |

Canonical files are the default runtime surface. Auditable files preserve provenance and should be read only for history or verification. Ephemeral files are process artifacts; keep them ignored or rotated so they do not become normal agent context.

## Provenance rules

- Deterministic scripts may create evidence files under `changes/`.
- Current extraction evidence uses schema v2 when generated by this skill. Key fields include `modules`, `policies`, `guardrail_rules`, `guardrail_recipes`, and `source_hashes`.
- AI agents may refine wiki, rules, and workflows from evidence and source files.
- Factual statements must include `Source: <path>` or `Sources: <path>, <path>`.
- Inferences must be labeled with `Inference:`.
- Unknown or ambiguous items should be labeled with `Unknown:` or recorded in an extraction report.
- Sensitive values must never be copied into `.tenetora/`.

## Harness Engineering audit rules

Run `tenetora audit` after initialization or major harness edits. The audit evaluates:

- Stability: unified entrypoints, role files, readable project context, and no stale copied placeholders.
- Reliability: extraction evidence, verification workflow, quality gates, CI candidates, and provenance markers.
- Control: no secrets, no local-only paths, no permission to skip verification, no unapproved commit/push/delete behavior.
- Runtime content density: canonical runtime files must not be placeholder-only shells.
- Evidence-derived guardrails, such as test framework and dependency version drift checks, must remain executable and wired into `guardrails/checks/run-all.sh` when the evidence supports them.
- Existing projects should keep `wiki/project-map.md` aligned with extracted module profiles, source markers, and verification hints.
- `guardrail_recipes` list evidence-driven test, lint, build, security, and doc-gardening candidates with source, command, status, and prompt-friendly failure format.
- `source_hashes` let audit warn when external project source files changed after the latest extraction evidence, so agents know to refresh before trusting stale context. Generated `.tenetora` files, sensitive files, binaries, large files, and report artifacts should not be hashed as project sources.
- JSON audit output includes `dimensions.stability`, `dimensions.reliability`, `dimensions.control`, `dimensions.content_density`, and `dimensions.governance_effectiveness`, each with status, score, issue codes, and a short summary.

Use `tenetora audit --strict --min-score 80` in CI or release workflows when the harness is expected to enforce agent behavior.

After validation or major harness edits, run `tenetora recap --write` so `.tenetora/state/progress.md` records the latest validate/audit status and `.tenetora/changes/YYYY-MM-DD-recap.md` preserves the detailed snapshot.

## Root files

- `README.md`: keep as the human-facing repository entry.
- `AGENTS.md`: keep or create as a thin AI-agent compatibility entry pointing to `.tenetora/README.md`.
- `CLAUDE.md`: keep as Claude compatibility when present, but point it to `.tenetora/`.
- `CLAUDE.local.md`: keep as Claude local compatibility when present, but point it to `.tenetora/agents/claude-local.md` if `.tenetora/` is ignored or ignored `.tenetora/local/agents/claude-local.md` if `.tenetora/` is tracked.
