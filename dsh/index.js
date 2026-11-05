/**
 * Tenetora governance for the DeepSeek Harness (Cordis) host.
 *
 * Host-only bundle. Every contribution is scoped to a Tenetora-governed
 * workspace:
 *
 *   1. a system-prompt section with the project protocol and the routed context
 *      for the current user request;
 *   2. `tools/pre-execute` guards for git commit/push and registered local
 *      environment paths, plus a `tools/post-execute` screen for untrusted web
 *      content and one runtime observation per governed root;
 *   3. `agent/turn-stopping` completion claim reminder;
 *   4. a skill provider that publishes the lifecycle skills to the Harness
 *      catalogue.
 *
 * Nothing here re-implements a Tenetora rule: the CLI owns every decision, and
 * the plugin only decides when to ask it. A workspace without `.tenetora/`
 * receives no section, no denials, and no skills. Host APIs that take a
 * `UserMessage` receive a full message object; a bare string corrupts the
 * durable session log.
 *
 * @see types/index.d.ts for the exact Cordis surface this file relies on.
 */

import { randomUUID } from 'node:crypto'

import { evaluateToolCall } from './lib/guard.js'
import { externalWarning, scanToolResult } from './lib/external.js'
import { normalizeConfig } from './lib/config.js'
import { runTenetora } from './lib/cli.js'
import { buildGovernanceContext } from './lib/context.js'
import { findGovernedRoot } from './lib/project.js'
import { messageText, routeContext, textHash, worthRouting } from './lib/routing.js'
import { createSkillProvider, resolveSkillsRoot } from './lib/skills.js'
import { delegationResult, delegationStart } from './lib/delegation.js'

/** Plugin name shown by the Loader and the plugin manager. */
export const name = 'tenetora'

/**
 * Hard dependencies. All three ship in `@deepseek-ai/dsh-base`; a profile that
 * replaces one of them leaves this plugin pending instead of crashing.
 */
export const inject = ['systemPrompt', 'sessions']

const ROOT_CACHE_TTL_MS = 15000

/**
 * @param {import('./lib/config.js').TenetoraConfig} settings
 * @param {() => unknown} [log]
 */
function debugLogger(settings, log) {
  if (!settings.debug) return () => {}
  return (/** @type {unknown[]} */ ...args) => {
    const line = ['[tenetora]', ...args.map((value) => String(value))].join(' ')
    if (typeof log === 'function') log(line)
    else console.error(line)
  }
}

/**
 * Resolve the effective working directory for one tool call.
 *
 * `exec.agent` is the only stable per-call identity the tool pipeline exposes;
 * the session header carries the absolute cwd the Harness created it with.
 *
 * @param {any} ctx
 * @param {any} exec
 * @returns {string}
 */
function workingDirectoryFor(ctx, exec) {
  // A tool-level working directory is the most specific signal: `pwsh` accepts
  // `workdir`, and a governed submodule must not be judged by its parent's
  // governance. Only then fall back to the session header.
  const args = exec && exec.arguments && typeof exec.arguments === 'object' ? exec.arguments : {}
  for (const field of ['workdir', 'cwd', 'working_directory']) {
    if (typeof args[field] === 'string' && args[field].trim()) return args[field]
  }
  const agentId = exec && exec.agent && exec.agent.id
  if (agentId && ctx && ctx.sessions && typeof ctx.sessions.get === 'function') {
    try {
      const session = ctx.sessions.get(agentId)
      const cwd = session && session.header && session.header.cwd
      if (typeof cwd === 'string' && cwd) return cwd
    } catch {
      // fall through to the process default
    }
  }
  return process.cwd()
}

/**
 * @param {any} ctx
 * @param {import('./lib/config.js').TenetoraConfig} settings
 * @param {string} cwd
 * @returns {string | null}
 */
function governedRootFor(ctx, settings, cwd) {
  const cache = ctx.__tenetoraRootCache
  const now = Date.now()
  const hit = cache.get(cwd)
  if (hit && now - hit.at < ROOT_CACHE_TTL_MS) return hit.root
  const root = findGovernedRoot(cwd)
  cache.set(cwd, { root, at: now })
  if (cache.size > 32) cache.clear()
  return root
}

/**
 * Record one runtime observation per governed root per process. The Harness has
 * no Hook process, so the CLI writes the same machine-level record the other
 * adapters produce; `status`/`doctor` then report `runtime-observed`.
 *
 * @param {any} ctx
 * @param {import('./lib/config.js').TenetoraConfig} settings
 * @param {string} root
 * @param {string | undefined} agentId
 * @param {(line: string) => void} log
 */
function recordObservation(ctx, settings, root, agentId, log) {
  if (!settings.recordObservation) return
  const seen = ctx.__tenetoraObservationRoots
  if (seen.has(root)) return
  seen.add(root)
  try {
    const args = ['observe', '--platform', 'dsh', '--event', 'plugin-apply', '--path', root]
    if (agentId) args.push('--session-id', String(agentId))
    const result = runTenetora(settings, args, { cwd: root })
    if (result.error || result.status !== 0) {
      log('runtime observation was not recorded:', result.error || result.stderr.slice(0, 200))
    }
  } catch (error) {
    log('runtime observation failed:', error instanceof Error ? error.message : error)
  }
}

/**
 * Persist one native DSH delegation observation. The CLI hashes identity
 * fields before writing machine or project state; this process only forwards
 * opaque values for the bounded invocation.
 *
 * @param {import('./lib/config.js').TenetoraConfig} settings
 * @param {string} root
 * @param {any} observation
 * @param {(line: string) => void} log
 */
function recordDelegationObservation(settings, root, observation, log) {
  if (!settings.recordObservation || !observation) return
  try {
    const dispatchEvents = { started: 'start', failed: 'failed', unavailable: 'unavailable' }
    const dispatchEvent = dispatchEvents[observation.status] || 'unavailable'
    const args = ['observe', '--platform', 'dsh', '--event', `delegation-${observation.status}`, '--path', root, '--dispatch-event', dispatchEvent]
    for (const [flag, value] of [
      ['--dispatch-id', observation.dispatchId],
      ['--child-id', observation.childId],
      ['--owner-id', observation.ownerId],
      ['--session-id', observation.sessionId],
      ['--continuity-id', observation.continuityId],
      ['--reason', observation.reason],
    ]) {
      if (typeof value === 'string' && value.trim()) args.push(flag, value)
    }
    const result = runTenetora(settings, args, { cwd: root })
    if (result.error || result.status !== 0) {
      log('delegation observation was not recorded:', result.error || result.stderr.slice(0, 200))
    }
  } catch (error) {
    log('delegation observation failed:', error instanceof Error ? error.message : error)
  }
}

/**
 * Per-agent turn bookkeeping for the completion reminder.
 *
 * @param {any} ctx
 * @param {string} agentId
 * @param {number} turn
 */
function turnState(ctx, agentId, turn) {
  const map = ctx.__tenetoraTurns
  const existing = map.get(agentId)
  if (existing) {
    if (turn === undefined) return existing
    if (existing.turn === undefined) {
      existing.turn = turn
      return existing
    }
    if (existing.turn === turn) return existing
  }
  const state = { turn, presented: false, committed: false, claimed: false, reminded: false }
  map.set(agentId, state)
  return state
}

const COMMIT_PATTERN = /\bgit\b[^\n;&|]*\b(?:commit|push)\b/i
const CLAIM_PATTERN = /\bguard\b[^\n;&|]*--action\s+claim\b/i

/**
 * @param {any} ctx
 * @param {any} exec
 */
function noteToolCall(ctx, exec) {
  const agentId = exec && exec.agent && exec.agent.id
  if (!agentId) return
  const state = turnState(ctx, agentId, undefined)
  const name = String((exec && exec.name) || '').toLowerCase()
  if (name === 'present') state.presented = true
  const args = exec && exec.arguments && typeof exec.arguments === 'object' ? exec.arguments : {}
  const command = typeof args.command === 'string' ? args.command : ''
  if (command) {
    if (COMMIT_PATTERN.test(command)) state.committed = true
    if (CLAIM_PATTERN.test(command)) state.claimed = true
  }
}

/**
 * @param {string} root
 * @returns {string}
 */
function completionReminderText(root) {
  return [
    'Tenetora: this turn declared a deliverable or ran a git commit/push. Before ending it, confirm that verification evidence covers the work.',
    `For ${root}, run \`tenetora verification-plan --json\` and follow its decision:`,
    '- reuse / targeted: do not rerun the full suite; reuse the matching claim proof and run only the listed checks.',
    '- rerun: run the smallest command that covers the changed paths.',
    'A full-suite run must be recorded without `--verification-scope` so the whole-worktree plan can reuse it. A completion claim also requires a confirmed alignment handoff; without one, record `--claim-kind partial-verification` and say so.',
    'Tenetora：本轮有交付或提交，但请先确认验证证据覆盖了这次工作。运行 `tenetora verification-plan --json`，按 reuse/targeted/rerun 决定执行；整树全量验证记录 claim 时不要带 --verification-scope；没有已确认的 alignment 时只能记录 partial-verification。',
  ].join('\n')
}

/**
 * Build the reminder as the Harness's `createUserMessage()` would: a user-role
 * message with a fresh identity, one text block, and a valid `MessageSource`.
 *
 * `agent.steer()` takes a `UserMessage`, and the splice it records is rejected
 * at load time when `data.inserted` holds a bare string. Passing text here
 * corrupts the durable session log, so the shape is asserted in the test suite.
 *
 * @param {string} root
 * @returns {{ id: string, role: 'user', content: Array<{ type: 'text', text: string }>, source: { kind: 'user' } }}
 */
function completionReminderMessage(root) {
  return {
    id: randomUUID(),
    role: 'user',
    content: [{ type: 'text', text: completionReminderText(root) }],
    source: { kind: 'user' },
  }
}

/**
 * Cordis plugin entry point.
 *
 * @param {any} ctx
 * @param {any} [config]
 */
export function apply(ctx, config) {
  const settings = normalizeConfig(config)
  if (!settings.enabled) return

  const log = debugLogger(settings, ctx && typeof ctx.logger === 'function' ? (line) => ctx.logger(line) : undefined)
  ctx.__tenetoraRootCache = new Map()
  ctx.__tenetoraObservationRoots = new Set()
  ctx.__tenetoraRouteContexts = new Map()
  ctx.__tenetoraTurns = new Map()

  if (settings.contextSection) {
    ctx.effect(() =>
      ctx.systemPrompt.section({
        name: settings.sectionName,
        order: settings.sectionOrder,
        text: (assembly) => {
          const scope = assembly && assembly.scope
          const cwd = scope ? workingDirectoryForScope(ctx, scope) : process.cwd()
          try {
            const base = buildGovernanceContext(settings, cwd)
            const agentId = scope && scope.id
            const routed = agentId ? ctx.__tenetoraRouteContexts.get(agentId) : null
            if (!routed || !routed.text) return base
            return base ? `${base}\n\n${routed.text}` : routed.text
          } catch (error) {
            log('context section failed:', error instanceof Error ? error.message : error)
            return ''
          }
        },
      }),
    )
  }

  if (settings.contextInjection !== 'off') {
    ctx.on('agent/inbox/inserted', (payload) => {
      try {
        const agent = payload && payload.agent
        const text = messageText(payload && payload.message)
        if (!agent || !agent.id || !worthRouting(text)) return
        const cache = ctx.__tenetoraRouteContexts
        const hash = textHash(text)
        const existing = cache.get(agent.id)
        if (existing && existing.hash === hash) return
        const cwd = workingDirectoryFor(ctx, { agent })
        const root = governedRootFor(ctx, settings, cwd)
        if (!root) return
        const rendered = routeContext(settings, root, text, settings.injectionMaxChars)
        if (rendered) cache.set(agent.id, { hash, text: rendered })
      } catch (error) {
        log('route context failed:', error instanceof Error ? error.message : error)
      }
    })
  }

  ctx.on('agent/pre-step', async (payload, next) => {
    const decision = await next()
    try {
      if (payload && payload.agent && payload.agent.id) {
        turnState(ctx, payload.agent.id, payload.turn)
      }
    } catch {
      // Turn bookkeeping must never affect the step decision.
    }
    return decision
  })

  if (settings.completionReminder !== 'off') {
    ctx.on('agent/turn-stopping', async (payload) => {
      try {
        const agent = payload && payload.agent
        if (!agent || typeof agent.steer !== 'function') return
        const state = turnState(ctx, agent.id, payload.turn)
        if (state.reminded || state.claimed || !(state.presented || state.committed)) return
        const cwd = workingDirectoryFor(ctx, { agent })
        const root = governedRootFor(ctx, settings, cwd)
        if (!root) return
        state.reminded = true
        agent.steer(completionReminderMessage(root))
      } catch (error) {
        log('completion reminder failed:', error instanceof Error ? error.message : error)
      }
    })
  }

  ctx.on('tools/pre-execute', async (exec, next) => {
    try {
      noteToolCall(ctx, exec)
      const cwd = workingDirectoryFor(ctx, exec)
      const root = governedRootFor(ctx, settings, cwd)
      if (!root) return next()
      recordObservation(ctx, settings, root, exec && exec.agent && exec.agent.id, log)
      const decision = evaluateToolCall({
        name: exec && exec.name,
        arguments: exec && exec.arguments,
        cwd,
        root,
        settings,
      })
      if (decision) {
        log(`denied ${exec && exec.name}: ${decision.info ? decision.info.code : 'policy'}`)
        return decision
      }
    } catch (error) {
      // Fail open: a governance plugin must never break an unrelated tool call.
      log('guard failed:', error instanceof Error ? error.message : error)
    }
    return next()
  })

  if (settings.delegation !== 'off') {
    ctx.on('tools/post-execute', async (exec, result, next) => {
      const decision = await next()
      try {
        const cwd = workingDirectoryFor(ctx, exec)
        const root = governedRootFor(ctx, settings, cwd)
        if (!root) return decision
        const agentId = exec && exec.agent && exec.agent.id
        const session = agentId && ctx.sessions && typeof ctx.sessions.get === 'function' ? ctx.sessions.get(agentId) : undefined
        const start = delegationStart(exec, session, settings.delegationTools)
        if (!start) return decision
        recordDelegationObservation(settings, root, delegationResult(exec, result, start), log)
      } catch (error) {
        log('delegation result observation failed:', error instanceof Error ? error.message : error)
      }
      return decision
    })
  }

  if (settings.externalInput !== 'off') {
    ctx.on('tools/post-execute', async (exec, result, next) => {
      try {
        const tool = String((exec && exec.name) || '').toLowerCase()
        if (!settings.externalInputTools.includes(tool)) return next()
        if (!result || result.isError) return next()
        const findings = scanToolResult(result)
        if (findings.length === 0) return next()
        const warning = externalWarning(tool, findings)
        if (settings.externalInput === 'annotate') {
          const content = Array.isArray(result.content) ? result.content : []
          return { kind: 'accept', content: [{ type: 'text', text: warning }, ...content] }
        }
        return { kind: 'block', feedback: [{ type: 'text', text: warning }] }
      } catch (error) {
        // Fail open: screening must never break an unrelated tool result.
        log('external screening failed:', error instanceof Error ? error.message : error)
        return next()
      }
    })
  }

  if (settings.skills) {
    const skills = typeof ctx.get === 'function' ? ctx.get('skills') : ctx.skills
    if (skills && typeof skills.registerProvider === 'function') {
      if (!resolveSkillsRoot(settings)) {
        log('no skills root found; skill catalogue contribution skipped')
      } else {
        ctx.effect(() => skills.registerProvider((control) => createSkillProvider(settings, control)))
      }
    }
  }
}

/**
 * The prompt section is assembled per scope; an agent scope carries an id, so
 * the same session lookup applies.
 *
 * @param {any} ctx
 * @param {any} scope
 * @returns {string}
 */
function workingDirectoryForScope(ctx, scope) {
  return workingDirectoryFor(ctx, { agent: scope })
}
