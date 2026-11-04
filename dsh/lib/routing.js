/**
 * Per-user-turn routing context.
 *
 * The Harness has no `UserPromptSubmit` hook, so the bundle observes
 * `agent/inbox/inserted`, asks `tenetora route` and `tenetora rules` for the
 * current request, and caches a bounded context block per agent. The existing
 * `systemPrompt.section` provider appends that block to the governance section,
 * so nothing has to mutate session messages.
 */

import { createHash } from 'node:crypto'

import { runTenetora } from './cli.js'

/**
 * @param {any} message
 * @returns {string}
 */
export function messageText(message) {
  if (!message || typeof message !== 'object') return ''
  if (typeof message.text === 'string') return message.text.trim()
  if (typeof message.content === 'string') return message.content.trim()
  if (Array.isArray(message.content)) {
    return message.content
      .filter((block) => block && typeof block === 'object' && typeof block.text === 'string')
      .map((block) => block.text)
      .join('\n')
      .trim()
  }
  return ''
}

/**
 * Short prompts ("ok", "continue", "/command") do not need a route lookup.
 *
 * @param {string} text
 * @returns {boolean}
 */
export function worthRouting(text) {
  const value = String(text || '').trim()
  if (value.length < 8) return false
  if (value.startsWith('/')) return false
  return true
}

/**
 * @param {string} value
 * @returns {string}
 */
export function textHash(value) {
  return createHash('sha256').update(String(value)).digest('hex')
}

/**
 * Ask the CLI for the route contexts and the matching rules, then render one
 * bounded block.
 *
 * @param {import('./config.js').TenetoraConfig} settings
 * @param {string} root
 * @param {string} text
 * @param {number} maxChars
 * @returns {string}
 */
export function routeContext(settings, root, text, maxChars) {
  const routeResult = runTenetora(settings, ['route', '--message', text.slice(0, 2000), '--path', root, '--json'], {
    cwd: root,
  })
  if (!routeResult.json || routeResult.status !== 0) return ''
  const contexts = Array.isArray(routeResult.json.recommended_contexts)
    ? routeResult.json.recommended_contexts.filter((item) => typeof item === 'string')
    : []
  if (contexts.length === 0) return ''
  const rulesResult = runTenetora(
    settings,
    ['rules', '--context', contexts.slice(0, 3).join(','), '--path', root, '--json', '--no-record'],
    { cwd: root },
  )
  const files = []
  if (rulesResult.json && Array.isArray(rulesResult.json.files)) {
    for (const item of rulesResult.json.files) {
      if (item && typeof item.path === 'string' && item.exists) files.push(item.path)
    }
  }
  const lines = [
    '## Tenetora route (this user request)',
    `- recommended contexts: ${contexts.slice(0, 4).join(', ')}`,
  ]
  if (files.length > 0) {
    lines.push(`- relevant rule files: ${files.slice(0, 8).join(', ')}`)
  }
  lines.push('- Treat the routed context as a starting slice; load the full rule files before acting on omitted details.')
  const rendered = lines.join('\n')
  const budget = Math.max(200, Number(maxChars) || 2000)
  return rendered.length <= budget ? rendered : `${rendered.slice(0, budget - 60)}\n[route context shortened]`
}
