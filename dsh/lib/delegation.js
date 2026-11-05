/**
 * Observe the DeepSeek Harness native subagent tools without pretending to
 * dispatch a child agent. DSH owns execution; this adapter binds the host tool
 * call/result to the caller owner and the real session header, and parses the
 * dispatch acknowledgement (`started subagent <childId>`).
 *
 * A tool result is a dispatch acknowledgement, not completion: DSH returns the
 * child id immediately and reports completion later through a separate notice.
 * This module therefore never infers `completed` from the tool result.
 */

export const DEFAULT_DELEGATION_TOOLS = Object.freeze(['subagent', 'subagent_fork'])

const FIELD_ALIASES = Object.freeze({
  dispatchId: ['dispatchId', 'dispatch_id', 'requestId', 'request_id'],
  childId: ['childId', 'child_id', 'subagentId', 'subagent_id', 'targetAgentId', 'target_agent_id'],
  sessionId: ['sessionId', 'session_id'],
  continuityId: ['continuityId', 'continuity_id', 'conversationContinuityId', 'conversation_continuity_id'],
})

const STARTED_PATTERN = /started\s+subagent\s+["'`]?([^\s"'`]+)["'`]?/i

/** @param {unknown} value @returns {string | undefined} */
function stringValue(value) {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined
}

/** @param {Record<string, unknown>} values @param {string[]} names */
function firstField(values, names) {
  for (const name of names) {
    const value = stringValue(values[name])
    if (value) return value
  }
  return undefined
}

/** @param {unknown} value @returns {Record<string, unknown>} */
function objectValue(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? /** @type {Record<string, unknown>} */ (value) : {}
}

/**
 * @param {unknown} name
 * @param {string[]} [tools]
 */
export function isDelegationTool(name, tools = DEFAULT_DELEGATION_TOOLS) {
  return Array.isArray(tools) && tools.includes(String(name || '').toLowerCase())
}

/**
 * DSH `SessionHeader` exposes the session identity as `id`; it has no
 * `sessionId` and no conversation-continuity concept. Continuity is therefore
 * optional and recorded only when a host actually provides it.
 *
 * @param {any} exec
 * @param {any} session
 * @param {string[]} [tools]
 * @returns {{ status: 'start' | 'unavailable', reason?: string, tool: string, callId: string, dispatchId: string, childId?: string, ownerId?: string, sessionId?: string, continuityId?: string }}
 */
export function delegationStart(exec, session, tools = DEFAULT_DELEGATION_TOOLS) {
  const tool = String((exec && exec.name) || '').toLowerCase()
  if (!isDelegationTool(tool, tools)) return null
  const args = objectValue(exec && exec.arguments)
  const header = objectValue(session && session.header)
  const callId = stringValue(exec && exec.callId) || ''
  const dispatchId = firstField(args, FIELD_ALIASES.dispatchId) || callId
  const childId = firstField(args, FIELD_ALIASES.childId)
  const ownerId = stringValue(exec && exec.agent && exec.agent.id)
  const sessionId =
    firstField(args, FIELD_ALIASES.sessionId) ||
    stringValue(header.id) ||
    firstField(header, FIELD_ALIASES.sessionId)
  const continuityId =
    firstField(args, FIELD_ALIASES.continuityId) ||
    firstField(header, FIELD_ALIASES.continuityId)
  const missing = [
    ['dispatchId', dispatchId],
    ['ownerId', ownerId],
    ['sessionId', sessionId],
  ]
    .filter(([, value]) => !value)
    .map(([name]) => name)
  if (missing.length > 0) {
    return { status: 'unavailable', reason: `delegation identity missing: ${missing.join(', ')}`, tool, callId, dispatchId }
  }
  return { status: 'start', tool, callId, dispatchId, childId, ownerId, sessionId, continuityId }
}

/**
 * Read the child identity from a structured value or from the documented
 * dispatch acknowledgement text (`started subagent <childId>`).
 *
 * @param {any} result
 * @returns {string | undefined}
 */
function childIdFromResult(result) {
  const value = objectValue(result && result.value)
  const structured = firstField(value, FIELD_ALIASES.childId)
  if (structured) return structured
  const content = Array.isArray(result && result.content) ? result.content : []
  for (const block of content) {
    if (!block || typeof block !== 'object' || typeof block.text !== 'string') continue
    const match = STARTED_PATTERN.exec(block.text)
    if (match) return match[1]
  }
  return undefined
}

/**
 * @param {any} exec
 * @param {any} result
 * @param {ReturnType<typeof delegationStart>} start
 * @returns {{ status: 'started' | 'failed' | 'unavailable', phase: 'result', reason?: string, tool: string, callId: string, dispatchId: string, childId?: string, ownerId?: string, sessionId?: string, continuityId?: string } | null}
 */
export function delegationResult(exec, result, start) {
  if (!start) return null
  if (start.status !== 'start') return { ...start, status: 'unavailable', phase: 'result' }
  const value = objectValue(result && result.value)
  const childId = start.childId || childIdFromResult(result)
  if (result && result.isError) {
    return {
      ...start,
      childId,
      status: 'failed',
      phase: 'result',
      reason: 'delegation tool returned an error before the child was accepted',
    }
  }
  if (!childId) {
    return { ...start, status: 'unavailable', phase: 'result', reason: 'delegation identity missing: childId' }
  }
  const mismatches = []
  for (const [label, aliases, expected] of [
    ['dispatchId', FIELD_ALIASES.dispatchId, start.dispatchId],
    ['childId', FIELD_ALIASES.childId, childId],
    ['ownerId', ['ownerId', 'owner_id', 'parentAgentId', 'parent_agent_id'], start.ownerId],
    ['sessionId', FIELD_ALIASES.sessionId, start.sessionId],
    ['continuityId', FIELD_ALIASES.continuityId, start.continuityId],
  ]) {
    if (!expected) continue
    const actual = firstField(value, aliases)
    if (actual && actual !== expected) mismatches.push(label)
  }
  if (mismatches.length > 0) {
    return { ...start, childId, status: 'unavailable', phase: 'result', reason: `delegation identity mismatch: ${mismatches.join(', ')}` }
  }
  return { ...start, childId, status: 'started', phase: 'result' }
}
