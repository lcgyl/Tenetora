/**
 * Invoke the Tenetora CLI from the Host plugin.
 *
 * The CLI is the single implementation of Tenetora's checks. The plugin never
 * re-implements a rule; it decides *when* to ask the CLI and how to turn the
 * answer into a tool decision.
 */

import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { delimiter, isAbsolute, join } from 'node:path'

const POSIX_SAFE = /^[A-Za-z0-9_@%+=:,./-]+$/

/**
 * Resolve a command name without invoking a shell, so a missing CLI is reported
 * as "unavailable" on every locale instead of being parsed out of a localized
 * shell error message.
 *
 * @param {string} command
 * @returns {boolean}
 */
export function commandExists(command) {
  const value = String(command || '').trim()
  if (!value) return false
  if (isAbsolute(value) || value.includes('/') || value.includes('\\')) return existsSync(value)
  const extensions = process.platform === 'win32' ? ['', '.exe', '.cmd', '.bat', '.com', '.ps1'] : ['']
  const directories = String(process.env.PATH || '').split(delimiter).filter(Boolean)
  for (const directory of directories) {
    for (const extension of extensions) {
      if (existsSync(join(directory, value + extension))) return true
    }
  }
  return false
}

/**
 * Quote one argument for a shell command line. Node joins `command + args` with
 * spaces when `shell: true`, so arguments must be quoted here.
 *
 * @param {string} value
 * @returns {string}
 */
export function quoteArgument(value) {
  const text = String(value)
  if (POSIX_SAFE.test(text)) return text
  if (process.platform === 'win32') {
    return `"${text.replace(/"/g, '""')}"`
  }
  return `'${text.replace(/'/g, `'\\''`)}'`
}

/**
 * @typedef {object} CliResult
 * @property {number | null} status
 * @property {string} stdout
 * @property {string} stderr
 * @property {string | null} error
 * @property {boolean} timedOut
 * @property {any | null} json
 */

/**
 * Run the Tenetora CLI with a bounded timeout.
 *
 * `shell` is required on Windows for a `.cmd` launcher; on POSIX the command is
 * executed directly. Failures are reported, never thrown, so a missing CLI can
 * never break a tool call.
 *
 * @param {import('./config.js').TenetoraConfig} settings
 * @param {string[]} args
 * @param {{ cwd?: string, input?: string }} [options]
 * @returns {CliResult}
 */
export function runTenetora(settings, args, options = {}) {
  const command = String(settings.cli || 'tenetora')
  if (!commandExists(command)) {
    return { status: null, stdout: '', stderr: '', error: 'ENOENT', timedOut: false, json: null }
  }
  const prefix = Array.isArray(settings.cliArgs) ? settings.cliArgs : []
  const argv = [...prefix, ...args]
  const useShell = process.platform === 'win32'
  const commandLine = useShell ? [command, ...argv].map(quoteArgument).join(' ') : command
  const env = { ...process.env, ...(settings.env || {}) }

  const result = spawnSync(useShell ? commandLine : command, useShell ? [] : argv, {
    cwd: options.cwd,
    env,
    input: options.input,
    encoding: 'utf8',
    timeout: Number(settings.timeoutMs) || 20000,
    windowsHide: true,
    shell: useShell,
    maxBuffer: 8 * 1024 * 1024,
  })

  const timedOut = Boolean(result.error && /** @type {any} */ (result.error).code === 'ETIMEDOUT')
  const error = result.error ? String(/** @type {any} */ (result.error).code || result.error.message) : null
  const stdout = typeof result.stdout === 'string' ? result.stdout : ''
  const stderr = typeof result.stderr === 'string' ? result.stderr : ''
  let json = null
  if (stdout.trim().startsWith('{')) {
    try {
      json = JSON.parse(stdout)
    } catch {
      json = null
    }
  }
  return { status: typeof result.status === 'number' ? result.status : null, stdout, stderr, error, timedOut, json }
}

/**
 * True when the process never started: a missing executable, an unresolvable
 * shell command, or a timeout. Such a result must not be read as a failed
 * check, because no check ran.
 *
 * @param {CliResult} result
 * @returns {boolean}
 */
export function cliUnavailable(result) {
  if (result.error) return true
  if (result.timedOut) return true
  return /not recognized as an internal or external command|command not found|no such file or directory|is not recognized/i.test(
    result.stderr || '',
  )
}

/**
 * Build the arguments for `tenetora guard --action commit`.
 *
 * @param {string} root governed project root
 * @param {'commit' | 'push'} operation
 * @param {{ message?: string, strictEvidence?: boolean }} [extra]
 * @returns {string[]}
 */
export function guardArguments(root, operation, extra = {}) {
  const args = ['guard', '--action', 'commit', '--operation', operation, '--path', root, '--git-path', root, '--json']
  if (operation === 'commit' && typeof extra.message === 'string' && extra.message.trim()) {
    args.push('--commit-message', extra.message)
  }
  if (extra.strictEvidence) args.push('--strict-evidence')
  return args
}

/**
 * Turn a guard payload into a single deny reason.
 *
 * @param {any} payload parsed `--json` output, when available
 * @param {string} fallback raw stdout/stderr
 * @returns {string}
 */
export function guardReason(payload, fallback) {
  const lines = []
  if (payload && Array.isArray(payload.checks)) {
    for (const check of payload.checks) {
      if (!check || typeof check !== 'object') continue
      if (check.status !== 'fail') continue
      const name = typeof check.name === 'string' ? check.name : 'check'
      const code = typeof check.code === 'string' ? ` (${check.code})` : ''
      const message = typeof check.message === 'string' ? check.message : ''
      lines.push(`- ${name}${code}: ${message}`.trim())
    }
  }
  if (lines.length === 0) {
    const raw = String(fallback || '').trim()
    // No JSON: keep the tail, because a traceback ends with the exception that
    // matters and a host may clip long denial reasons.
    const detail = raw.length > 600 ? `…${raw.slice(-600)}` : raw
    lines.push(detail ? detail : 'the commit guard did not pass')
  }
  return lines.join('\n')
}
