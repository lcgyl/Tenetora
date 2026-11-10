/**
 * Type surface for the Tenetora DSH bundle.
 *
 * These declarations mirror the exact contracts reported by
 * `cordis_inspect_query` (Service `systemPrompt`, Service `sessions`, Service
 * `skills`, Event `tools/pre-execute`, Event `tools/post-execute`) at DSH 24.12. They are documentation for
 * the JavaScript implementation: the bundle ships no `@deepseek-ai/*` imports,
 * so nothing here is a runtime dependency.
 *
 * @packageDocumentation
 */

/** Row config accepted from `cordis.patch.yml`. */
export interface TenetoraDshConfig {
  /** Disable every contribution without removing the row. Default `true`. */
  enabled?: boolean
  /** Command that reaches the Tenetora CLI. Default `tenetora`. */
  cli?: string
  /** Prefix arguments, e.g. `["-m", "tenetora"]` for a source checkout. */
  cliArgs?: string[]
  /** Extra environment variables for the CLI process. */
  env?: Record<string, string>
  /** Per-invocation timeout. Default `20000`. */
  timeoutMs?: number
  /** Register the governance system-prompt section. Default `true`. */
  contextSection?: boolean
  /** Prompt context shape: compact entry card or full rule/workflow listing. Default `card`. */
  contextMode?: 'card' | 'full'
  /** Section name; must be unique within one layer. Default `tenetora-governance`. */
  sectionName?: string
  /** Section sort order. Default `60`. */
  sectionOrder?: number
  /** Character budget for the section. Default `6000`. */
  maxSectionChars?: number
  /** Ask `tenetora guard` before git commit/push. Default `true`. */
  guardGit?: boolean
  /** Require observed execution receipts covering verification-impacting staged paths; documentation-only commits pass. Default `false`. */
  strictEvidence?: boolean
  /** Protect registered local-environment paths. Default `true`. */
  guardLocalEnv?: boolean
  /** On a failed git guard: hard-deny, or ask the host to confirm. Default `deny`. */
  onGuardFailure?: 'deny' | 'ask'
  /** Screen untrusted web/search results: `block`, `annotate`, or `off`. Default `block`. */
  externalInput?: 'block' | 'annotate' | 'off'
  /** Tool names whose results are treated as untrusted external content. */
  externalInputTools?: string[]
  /** Record one machine-level runtime observation per governed root. Default `true`. */
  recordObservation?: boolean
  /** Append the routed context for each user request to the prompt section. Default `auto`. */
  contextInjection?: 'auto' | 'off'
  /** Character budget for the injected route block. Default `2000`. */
  injectionMaxChars?: number
  /** Ask for a completion claim at turn end after a delivery or commit. Default `auto`. */
  completionReminder?: 'auto' | 'off'
  /** Observe native DSH subagent tool dispatches; execution remains owned by DSH. Default `auto`. */
  delegation?: 'auto' | 'off'
  /** DSH tool names that represent native delegation. */
  delegationTools?: string[]
  /** Tool names treated as shell execution. */
  shellTools?: string[]
  /** Tool names treated as file mutation. */
  fileTools?: string[]
  /** Argument fields that may carry a path. */
  pathFields?: string[]
  /** Publish the lifecycle skills. Default `true`. */
  skills?: boolean
  /** Skills directory; defaults to `dsh/skills` then `../skills`. */
  skillsRoot?: string
  /** Verbose diagnostics on stderr. Default `false`. */
  debug?: boolean
}

/** Deny decision from the `tools/pre-execute` waterfall. */
export type PreToolDecision =
  | { kind: 'allow' }
  | { kind: 'deny'; reason: string; info?: { name: string; code: string } }
  | { kind: 'cancel' }
  | { kind: 'ask'; reason?: string; displayReason?: { readonly en: string; [locale: string]: string } }

/** A text block carried by a tool result. */
export interface ContentBlock {
  type: string
  text?: string
  [key: string]: unknown
}

/** The subset of `ToolExecutionResult` the plugin reads. */
export interface ToolExecutionResult {
  readonly isError: boolean
  readonly content: ContentBlock[]
  readonly value?: unknown
}

/** Decision returned by the `tools/post-execute` waterfall. */
export type PostToolDecision =
  | { kind: 'accept'; content?: ContentBlock[]; additionalContexts?: unknown[] }
  | { kind: 'accept'; value: unknown; additionalContexts?: unknown[] }
  | { kind: 'block'; feedback: ContentBlock[]; additionalContexts?: unknown[] }

/** The subset of `ToolExecution` the plugin reads. */
export interface ToolExecution {
  readonly callId: string
  readonly name: string
  readonly arguments: unknown
  readonly agent?: { readonly id: string }
  readonly signal?: AbortSignal
}

/** The native DSH subagent tool identity carried by a delegation call. */
export interface NativeDelegationIdentity {
  readonly dispatchId: string
  /** Parsed from the tool result (`started subagent <childId>`) or a structured value. */
  readonly childId: string
  readonly ownerId: string
  /** The real DSH `SessionHeader.id`. */
  readonly sessionId: string
  /** DSH has no continuity field; recorded only when a host provides one. */
  readonly continuityId?: string
}

/** The subset of `Session` the plugin reads. */
export interface Session {
  readonly header: { readonly cwd?: string }
}

/** `ctx.systemPrompt` (hard dependency). */
export interface SystemPromptService {
  section(section: {
    readonly name: string
    readonly order: number
    readonly text: string | ((context: { scope?: unknown; signal?: AbortSignal }) => string)
    readonly interpolate?: boolean
    readonly complete?: boolean
  }): () => void
}

/** `ctx.sessions` (hard dependency). */
export interface SessionsService {
  get(id: string): Session | undefined
}

/** `ctx.skills` (optional). */
export interface SkillsService {
  registerProvider(
    create: (control: { signal: AbortSignal; invalidate: () => void }) => SkillProvider,
  ): () => void
}

export interface SkillSummary {
  name: string
  description: string
  whenToUse?: string
  path?: string
  invocation: { modelInvocable: boolean; userInvocable: boolean }
  source: string
  provider: string
  resourceBase?: { kind: 'directory'; path: string } | { kind: 'url'; url: string } | { kind: 'opaque'; description: string }
}

export interface SkillCandidate extends SkillSummary {
  rank: number
  locator: unknown
}

export interface SkillDefinition extends SkillSummary {
  content: string
  metadata?: Record<string, unknown>
}

export interface SkillProvider {
  readonly name: string
  readonly list: (options: { cwd?: string; signal?: AbortSignal }) => Promise<readonly SkillCandidate[]>
  readonly get: (candidate: SkillCandidate, options: { cwd?: string; signal?: AbortSignal }) => Promise<SkillDefinition | undefined>
}

/** The Cordis context surface this plugin uses. */
export interface TenetoraContext {
  readonly systemPrompt: SystemPromptService
  readonly sessions: SessionsService
  on(event: 'tools/pre-execute', listener: (exec: ToolExecution, next: () => Promise<PreToolDecision>) => Promise<PreToolDecision>): () => void
  on(event: 'tools/post-execute', listener: (exec: ToolExecution, result: ToolExecutionResult, next: () => Promise<PostToolDecision>) => Promise<PostToolDecision>): () => void
  effect(execute: () => (() => void) | void): () => void
  get(name: string): unknown
  skills?: SkillsService
  logger?: (line: string) => void
}

export declare const name: 'tenetora'
export declare const inject: readonly ['systemPrompt', 'sessions']
export declare function apply(ctx: TenetoraContext, config?: TenetoraDshConfig): void
