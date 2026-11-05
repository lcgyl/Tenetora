/**
 * Tool-call policy.
 *
 * Two checks are mirrored from Tenetora's host hooks, adapted to the DSH tool
 * pipeline:
 *
 *   1. Git commit/push must pass `tenetora guard --action commit` first.
 *   2. A destructive write to a registered local-environment path is denied.
 *
 * Everything else delegates to the host with `next()`. An ungoverned workspace,
 * a missing CLI, and an internal error all fall back to allow: the plugin must
 * never be the reason a tool call breaks.
 */

import { cliUnavailable, guardArguments, guardReason, runTenetora } from './cli.js'
import { matchesRegistered, readLocalEnvPaths, resolvePathArgument, toProjectRelative } from './project.js'

/** @typedef {{ kind: 'deny', reason: string, info?: { name: string, code: string } }} DenyDecision */

const GIT_PATTERN = /(?:^|[\n;]|&&|\|\|)\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*git(?:\s+-C\s+(?:"[^"]*"|'[^']*'|\S+))?(?:\s+-c\s+\S+)*\s+(commit|push)\b/i
const COMPOUND_PATTERN = /(?:&&|\|\||[;|&])/
const DESTRUCTIVE_PATTERN = /\b(?:rm|rmdir|mv|del|erase|unlink|truncate|shred|remove-item|move-item|clear-content)\b/i

/**
 * Find the first standalone `git commit` or `git push` invocation.
 *
 * @param {string} command
 * @returns {{ operation: 'commit' | 'push', message: string, compound: boolean } | null}
 */
export function detectGitOperation(command) {
  if (typeof command !== 'string' || !command.trim()) return null
  const match = GIT_PATTERN.exec(command)
  if (!match) return null
  const operation = /** @type {'commit' | 'push'} */ (match[1].toLowerCase())
  return { operation, message: operation === 'commit' ? commitMessage(command) : '', compound: COMPOUND_PATTERN.test(stripQuoted(command)) }
}

/**
 * Extract `-m` / `--message` text so the guard can validate the real message
 * instead of reporting a missing one.
 *
 * @param {string} command
 * @returns {string}
 */
export function commitMessage(command) {
  const patterns = [
    /(?:^|\s)-m\s+"((?:[^"\\]|\\.)*)"/,
    /(?:^|\s)-m\s+'((?:[^'\\]|\\.)*)'/,
    /(?:^|\s)-m\s+([^\s;&|]+)/,
    /--message=("([^"]*)"|'([^']*)'|([^\s;&|]+))/,
    /--message\s+("([^"]*)"|'([^']*)'|([^\s;&|]+))/,
  ]
  for (const pattern of patterns) {
    const match = pattern.exec(command)
    if (!match) continue
    const value = match[2] ?? match[3] ?? match[4] ?? match[1]
    if (typeof value === 'string' && value.trim()) return value.replace(/\\n/g, '\n')
  }
  return ''
}

/**
 * Drop single- and double-quoted spans so operator detection does not fire on
 * `echo "a && b"`.
 *
 * @param {string} text
 * @returns {string}
 */
function stripQuoted(text) {
  let output = ''
  let quote = ''
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index]
    if (quote) {
      if (char === '\\' && index + 1 < text.length) {
        index += 1
        continue
      }
      if (char === quote) quote = ''
      continue
    }
    if (char === '"' || char === "'") {
      quote = char
      continue
    }
    output += char
  }
  return output
}

/**
 * Evaluate one pending tool call.
 *
 * @param {object} input
 * @param {string} input.name tool name
 * @param {unknown} input.arguments parsed arguments
 * @param {string} input.cwd effective working directory for the call
 * @param {string} input.root governed project root
 * @param {import('./config.js').TenetoraConfig} input.settings
 * @param {(settings: import('./config.js').TenetoraConfig, args: string[], options?: { cwd?: string }) => import('./cli.js').CliResult} [input.run]
 * @returns {DenyDecision | null}
 */
export function evaluateToolCall(input) {
  const { name, arguments: rawArguments, cwd, root, settings } = input
  const run = input.run || runTenetora
  const tool = String(name || '').toLowerCase()
  const args = rawArguments && typeof rawArguments === 'object' ? /** @type {Record<string, any>} */ (rawArguments) : {}
  const command = typeof args.command === 'string' ? args.command : ''

  if (settings.guardGit && settings.shellTools.includes(tool) && command) {
    const invocation = detectGitOperation(command)
    if (invocation) {
      const decision = evaluateGit(root, invocation, settings, run)
      if (decision) return decision
    }
  }

  if (settings.guardLocalEnv) {
    const decision = evaluateLocalEnvironment({ tool, args, command, cwd, root, settings })
    if (decision) return decision
  }

  return null
}

/**
 * @param {string} root
 * @param {{ operation: 'commit' | 'push', message: string, compound: boolean }} invocation
 * @param {import('./config.js').TenetoraConfig} settings
 * @param {(settings: any, args: string[], options?: { cwd?: string }) => import('./cli.js').CliResult} run
 * @returns {DenyDecision | null}
 */
function evaluateGit(root, invocation, settings, run) {
  if (invocation.compound) {
    return {
      kind: 'deny',
      reason: [
        'Tenetora requires a standalone git commit or git push so each authorization boundary can be checked.',
        'Run the commit and the push as separate commands.',
        'Tenetora 要求 git commit 与 git push 独立执行，请拆分为两条命令。',
      ].join('\n'),
      info: { name: 'tenetora', code: 'git-operation-compound' },
    }
  }
  const result = run(settings, guardArguments(root, invocation.operation, { message: invocation.message, strictEvidence: settings.strictEvidence }), { cwd: root })
  if (cliUnavailable(result)) {
    // A missing or broken CLI must not block work; the host hook reports the
    // same fail-open behaviour.
    return null
  }
  if (result.status === 0) return null
  if (result.status === null) return null
  if (result.status === 2) {
    // No harness at this path: not governed, nothing to enforce.
    return null
  }
  if (!result.json) {
    // The CLI crashed or printed no structured verdict. An unparseable failure
    // is not a check result, so it must not block work.
    return null
  }
  const detail = guardReason(result.json, result.stdout || result.stderr)
  const reason = [
    `Tenetora blocked this git ${invocation.operation}.`,
    detail,
    'Fix the reported checks and run the command again. Do not bypass the guard by rewriting the command.',
  ].join('\n')
  if (settings.onGuardFailure === 'ask') {
    // Let the host ask the user instead of hard-denying. A host without
    // approval support turns `ask` into denial, so this stays fail-closed.
    return {
      kind: 'ask',
      reason,
      displayReason: {
        en: `Tenetora gate failed for git ${invocation.operation}. Approve this call?`,
        zh: `Tenetora 对 git ${invocation.operation} 的门禁未通过，是否批准本次执行？`,
      },
    }
  }
  return {
    kind: 'deny',
    reason,
    info: { name: 'tenetora', code: `git-${invocation.operation}-guard-failed` },
  }
}

/**
 * @param {object} input
 * @param {string} input.tool
 * @param {Record<string, any>} input.args
 * @param {string} input.command
 * @param {string} input.cwd
 * @param {string} input.root
 * @param {import('./config.js').TenetoraConfig} input.settings
 * @returns {DenyDecision | null}
 */
function evaluateLocalEnvironment(input) {
  const { tool, args, command, cwd, root, settings } = input
  const registered = readLocalEnvPaths(root)
  if (registered.length === 0) return null

  const isFileTool = settings.fileTools.includes(tool)
  const isShellTool = settings.shellTools.includes(tool)
  if (!isFileTool && !isShellTool) return null

  /** @type {string[]} */
  const candidates = []
  for (const field of settings.pathFields) {
    const value = args[field]
    if (typeof value === 'string') {
      const absolute = resolvePathArgument(value, cwd)
      if (absolute) candidates.push(absolute)
    }
  }
  if (Array.isArray(args.paths)) {
    for (const value of args.paths) {
      if (typeof value !== 'string') continue
      const absolute = resolvePathArgument(value, cwd)
      if (absolute) candidates.push(absolute)
    }
  }

  for (const absolute of candidates) {
    const relativePath = toProjectRelative(root, absolute)
    if (!relativePath) continue
    if (matchesRegistered(relativePath, registered)) {
      return denyLocalEnvironment(relativePath)
    }
  }

  if (isShellTool && command && DESTRUCTIVE_PATTERN.test(command)) {
    const normalized = command.replace(/\\/g, '/').toLowerCase()
    for (const entry of registered) {
      if (normalized.includes(entry.toLowerCase())) {
        return denyLocalEnvironment(entry)
      }
    }
  }

  return null
}

/** @param {string} relativePath @returns {DenyDecision} */
function denyLocalEnvironment(relativePath) {
  return {
    kind: 'deny',
    reason: [
      `Tenetora blocked a destructive tool call against the registered local environment path \`${relativePath}\`.`,
      'Preserve the file: use an explicit recovery or checkpoint workflow instead of rewriting or deleting it.',
      `Tenetora 已阻止针对已登记本地环境路径 \`${relativePath}\` 的破坏性操作，请保留该文件并改用显式恢复流程。`,
    ].join('\n'),
    info: { name: 'tenetora', code: 'local-environment-protected' },
  }
}
