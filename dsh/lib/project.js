/**
 * Project-scoped facts the plugin reads from a governed workspace.
 *
 * Only `.tenetora/` state and manifests are touched. Rule bodies are read to
 * build the prompt section; file contents of registered local-environment
 * paths are never read (Tenetora's own contract: registration is a path, not a
 * secret copy).
 */

import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path'
import { homedir } from 'node:os'

export const MARKER = '.tenetora'
const MAX_WALK = 40

/**
 * Tenetora's machine installation home. It is never project governance: the
 * Python runtime enforces the same rule in `is_machine_install_root`, so a
 * global install must not make every directory below `~` look governed.
 *
 * @returns {string}
 */
export function machineHome() {
  const configured = process.env.TENETORA_HOME
  if (configured && configured.trim()) {
    try {
      return resolve(configured.trim())
    } catch {
      // fall through to the default
    }
  }
  return resolve(homedir(), '.tenetora')
}

/**
 * Compare two paths without relying on the filesystem (the machine home may not
 * exist yet). Windows paths compare case-insensitively.
 *
 * @param {string} left
 * @param {string} right
 */
export function samePath(left, right) {
  const normalize = (value) => {
    const text = resolve(value).replace(/[\\/]+$/, '')
    return process.platform === 'win32' ? text.toLowerCase() : text
  }
  return normalize(left) === normalize(right)
}

/**
 * Walk up from `start` until a directory containing `.tenetora/manifest.json`
 * (or a `.tenetora` directory) is found. The machine home is skipped.
 *
 * @param {string | undefined} start
 * @returns {string | null} absolute governed root, or null
 */
export function findGovernedRoot(start) {
  if (!start) return null
  let current
  try {
    current = resolve(start)
  } catch {
    return null
  }
  const home = machineHome()
  for (let depth = 0; depth < MAX_WALK; depth += 1) {
    const marker = join(current, MARKER)
    if (!samePath(marker, home)) {
      if (existsSync(join(marker, 'manifest.json'))) return current
      if (existsSync(marker) && isDirectory(marker)) return current
    }
    const parent = dirname(current)
    if (parent === current) return null
    current = parent
  }
  return null
}

/** @param {string} path */
function isDirectory(path) {
  try {
    return statSync(path).isDirectory()
  } catch {
    return false
  }
}

/** @param {string} path @returns {any | null} */
function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, 'utf8'))
  } catch {
    return null
  }
}

/**
 * Read the small, non-sensitive part of the project manifest.
 * Absolute local paths stay inside the process; nothing is sent to a model.
 *
 * @param {string} root
 */
export function readManifest(root) {
  const payload = readJson(join(root, MARKER, 'manifest.json'))
  if (!payload || typeof payload !== 'object') return null
  return {
    version: typeof payload.created_by_version === 'string' ? payload.created_by_version : '',
    defaultsVersion: typeof payload.defaults_version === 'number' ? payload.defaults_version : null,
    projectId: typeof payload.project_id === 'string' ? payload.project_id : '',
    layout: typeof payload.layout === 'string' ? payload.layout : MARKER,
  }
}

/**
 * Rule and workflow files that exist in the workspace, relative to the root.
 * These are the files the agent is asked to load before acting.
 *
 * @param {string} root
 * @returns {{ rules: string[], workflows: string[] }}
 */
export function listGovernanceFiles(root) {
  return {
    rules: listMarkdown(join(root, MARKER, 'rules')),
    workflows: listMarkdown(join(root, MARKER, 'workflows')),
  }
}

/** @param {string} directory @returns {string[]} */
function listMarkdown(directory) {
  if (!isDirectory(directory)) return []
  try {
    return readdirSync(directory)
      .filter((name) => name.endsWith('.md'))
      .sort()
      .map((name) => name)
  } catch {
    return []
  }
}

/**
 * Registered local-environment paths, normalized to POSIX project-relative
 * form. Mirrors `.tenetora/scripts/local_env.py` validation closely enough for
 * a read-only guard: absolute paths and `..` are rejected.
 *
 * @param {string} root
 * @returns {string[]}
 */
export function readLocalEnvPaths(root) {
  const payload = readJson(join(root, MARKER, 'state', 'local-env.json'))
  if (!payload || typeof payload !== 'object') return []
  if (payload.schema_version !== 1) return []
  const paths = payload.paths
  if (!Array.isArray(paths)) return []
  const normalized = []
  for (const item of paths) {
    if (typeof item !== 'string') continue
    const value = item.trim().replace(/\\/g, '/')
    if (!value || value.startsWith('/') || /^[A-Za-z]:\//.test(value)) continue
    if (value === '.tenetora' || value.startsWith('.tenetora/')) continue
    if (value.split('/').includes('..')) continue
    normalized.push(value)
  }
  return [...new Set(normalized)].sort()
}

/**
 * @param {string} root
 * @param {string} candidate absolute path
 * @returns {boolean} whether candidate sits inside the project root
 */
export function isInside(root, candidate) {
  const rootPath = resolve(root)
  let target
  try {
    target = resolve(candidate)
  } catch {
    return false
  }
  if (target === rootPath) return true
  const prefix = rootPath.endsWith(sep) ? rootPath : rootPath + sep
  return target.toLowerCase().startsWith(prefix.toLowerCase())
}

/**
 * Convert an absolute path to a POSIX project-relative path, or null when the
 * path escapes the project.
 *
 * @param {string} root
 * @param {string} candidate
 * @returns {string | null}
 */
export function toProjectRelative(root, candidate) {
  if (!isInside(root, candidate)) return null
  const value = relative(resolve(root), resolve(candidate)).split(sep).join('/')
  return value || '.'
}

/**
 * True when a relative path equals a registered path or sits below it.
 *
 * @param {string} relativePath
 * @param {string[]} registered
 */
export function matchesRegistered(relativePath, registered) {
  const value = relativePath.replace(/\\/g, '/').replace(/^\.\//, '')
  return registered.some((entry) => value === entry || value.startsWith(entry + '/'))
}

/**
 * Resolve a path argument that the tool call supplied. Absolute paths are kept;
 * relative ones resolve against the effective working directory.
 *
 * @param {string} raw
 * @param {string} cwd
 * @returns {string | null}
 */
export function resolvePathArgument(raw, cwd) {
  if (typeof raw !== 'string' || !raw.trim()) return null
  const value = raw.trim()
  try {
    return isAbsolute(value) ? resolve(value) : resolve(cwd, value)
  } catch {
    return null
  }
}
