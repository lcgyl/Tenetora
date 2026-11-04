/**
 * Expose Tenetora's lifecycle skills to the Harness skill registry.
 *
 * DSH skills are catalogued with invocation-neutral summaries and load their
 * body on demand. The provider therefore reads `SKILL.md` frontmatter at list
 * time and the full body only when `get` is called, exactly like the bundled
 * providers.
 *
 * Project scoping: the provider returns nothing unless the requested working
 * directory belongs to a Tenetora-governed workspace, so installing the bundle
 * does not add nine skills to every unrelated project.
 */

import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, isAbsolute, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { findGovernedRoot } from './project.js'

const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')

/**
 * Candidate skill roots, in priority order:
 *   1. the configured root,
 *   2. `dsh/skills` (a packed bundle that ships its own copies),
 *   3. `../skills` (a linked directory inside the Tenetora source checkout).
 *
 * @param {import('./config.js').TenetoraConfig} settings
 * @returns {string | null}
 */
export function resolveSkillsRoot(settings) {
  const candidates = []
  if (settings.skillsRoot) {
    candidates.push(isAbsolute(settings.skillsRoot) ? settings.skillsRoot : resolve(PACKAGE_ROOT, settings.skillsRoot))
  }
  candidates.push(join(PACKAGE_ROOT, 'skills'))
  candidates.push(resolve(PACKAGE_ROOT, '..', 'skills'))
  for (const candidate of candidates) {
    if (isDirectory(candidate)) return candidate
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

/**
 * Parse the leading `---` frontmatter block. Only flat `key: value` pairs are
 * needed; anything else is ignored rather than guessed.
 *
 * @param {string} text
 * @returns {{ attributes: Record<string, string>, body: string }}
 */
export function parseFrontmatter(text) {
  const source = String(text || '').replace(/^\uFEFF/, '')
  if (!source.startsWith('---')) return { attributes: {}, body: source.trim() }
  const end = source.indexOf('\n---', 3)
  if (end === -1) return { attributes: {}, body: source.trim() }
  const header = source.slice(3, end).split(/\r?\n/)
  const attributes = {}
  for (const line of header) {
    const match = /^([A-Za-z0-9_-]+)\s*:\s*(.*)$/.exec(line.trim())
    if (!match) continue
    let value = match[2].trim()
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1)
    }
    attributes[match[1]] = value
  }
  const body = source.slice(end + 4).replace(/^\r?\n/, '')
  return { attributes, body: body.trim() }
}

/**
 * Read every `<dir>/SKILL.md` under the root.
 *
 * @param {string} skillsRoot
 * @returns {Array<{ name: string, description: string, whenToUse?: string, directory: string, file: string }>}
 */
export function readSkills(skillsRoot) {
  if (!skillsRoot || !isDirectory(skillsRoot)) return []
  let entries
  try {
    entries = readdirSync(skillsRoot, { withFileTypes: true })
  } catch {
    return []
  }
  const skills = []
  for (const entry of entries) {
    if (!entry.isDirectory()) continue
    const directory = join(skillsRoot, entry.name)
    const file = join(directory, 'SKILL.md')
    if (!existsSync(file)) continue
    let text
    try {
      text = readFileSync(file, 'utf8')
    } catch {
      continue
    }
    const { attributes } = parseFrontmatter(text)
    const name = (attributes.name || entry.name).trim()
    if (!/^[a-z0-9][a-z0-9-]{1,63}$/.test(name)) continue
    const skill = {
      name,
      description: (attributes.description || `Tenetora lifecycle skill \`${name}\`.`).trim(),
      directory,
      file,
    }
    if (attributes.when_to_use || attributes.whenToUse) {
      skill.whenToUse = String(attributes.when_to_use || attributes.whenToUse).trim()
    }
    skills.push(skill)
  }
  skills.sort((left, right) => left.name.localeCompare(right.name))
  return skills
}

/**
 * Build the provider object expected by `ctx.skills.registerProvider`.
 *
 * @param {import('./config.js').TenetoraConfig} settings
 * @param {{ signal?: AbortSignal, invalidate?: () => void }} [control]
 */
export function createSkillProvider(settings, control = {}) {
  const root = resolveSkillsRoot(settings)
  const provider = {
    name: 'tenetora',
    /**
     * @param {{ cwd?: string, signal?: AbortSignal }} options
     */
    async list(options = {}) {
      const cwd = options.cwd || process.cwd()
      if (!findGovernedRoot(cwd)) return []
      if (control.signal?.aborted || options.signal?.aborted) return []
      return readSkills(root).map((skill) => ({
        name: skill.name,
        description: skill.description,
        ...(skill.whenToUse ? { whenToUse: skill.whenToUse } : {}),
        path: skill.file,
        invocation: { modelInvocable: true, userInvocable: true },
        source: 'custom',
        provider: 'tenetora',
        resourceBase: { kind: 'directory', path: skill.directory },
        rank: 0,
        locator: { path: skill.file },
      }))
    },
    /**
     * @param {{ locator?: { path?: string }, name?: string }} candidate
     */
    async get(candidate) {
      const file = candidate?.locator?.path
      if (typeof file !== 'string') return undefined
      let text
      try {
        text = readFileSync(file, 'utf8')
      } catch {
        return undefined
      }
      const { attributes, body } = parseFrontmatter(text)
      const name = (attributes.name || candidate.name || '').trim()
      if (!name) return undefined
      const directory = dirname(file)
      return {
        name,
        description: (attributes.description || `Tenetora lifecycle skill \`${name}\`.`).trim(),
        content: body,
        path: file,
        invocation: { modelInvocable: true, userInvocable: true },
        source: 'custom',
        provider: 'tenetora',
        resourceBase: { kind: 'directory', path: directory },
        metadata: { root: root || '', bundle: '@lcgyl/tenetora-dsh' },
      }
    },
  }
  return provider
}
