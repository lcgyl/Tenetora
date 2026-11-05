/**
 * Runtime configuration for the Tenetora Cordis plugin.
 *
 * The Loader hands `apply(ctx, config)` the row's `config` object from
 * `cordis.patch.yml`. This module keeps that surface small, defensive, and free
 * of any dependency outside Node's standard library: a Harness bundle must not
 * import `@deepseek-ai/*` packages, because the bundle ships without
 * dependencies and resolves from the profile, not from the Harness install.
 *
 * @typedef {object} TenetoraConfig
 * @property {boolean} enabled
 * @property {string} cli                 command used to reach the Tenetora CLI
 * @property {string[]} cliArgs           prefix arguments (e.g. ["-m","tenetora"])
 * @property {Record<string,string>} env  extra environment for the CLI process
 * @property {number} timeoutMs
 * @property {boolean} contextSection
 * @property {'card'|'full'} contextMode
 * @property {string} sectionName
 * @property {number} sectionOrder
 * @property {number} maxSectionChars
 * @property {boolean} guardGit
 * @property {boolean} strictEvidence
 * @property {boolean} guardLocalEnv
 * @property {'deny'|'ask'} onGuardFailure
 * @property {'block'|'annotate'|'off'} externalInput
 * @property {string[]} externalInputTools
 * @property {boolean} recordObservation
 * @property {'auto'|'off'} contextInjection
 * @property {number} injectionMaxChars
 * @property {'auto'|'off'} completionReminder
 * @property {'auto'|'off'} delegation
 * @property {string[]} delegationTools
 * @property {string[]} shellTools
 * @property {string[]} fileTools
 * @property {string[]} pathFields
 * @property {boolean} skills
 * @property {string} skillsRoot
 * @property {boolean} debug
 */

/** @type {TenetoraConfig} */
export const DEFAULTS = {
  enabled: true,
  cli: 'tenetora',
  cliArgs: [],
  env: {},
  timeoutMs: 20000,
  contextSection: true,
  contextMode: 'card',
  sectionName: 'tenetora-governance',
  sectionOrder: 60,
  maxSectionChars: 6000,
  guardGit: true,
  strictEvidence: false,
  guardLocalEnv: true,
  onGuardFailure: 'deny',
  externalInput: 'block',
  externalInputTools: ['web_search', 'web_fetch'],
  recordObservation: true,
  contextInjection: 'auto',
  injectionMaxChars: 2000,
  completionReminder: 'auto',
  delegation: 'auto',
  delegationTools: ['subagent', 'subagent_fork'],
  shellTools: ['pwsh', 'powershell', 'bash', 'sh', 'shell', 'terminal', 'exec', 'run'],
  fileTools: ['write', 'edit', 'delete', 'move', 'apply_patch', 'str_replace_editor'],
  pathFields: ['file_path', 'path', 'target', 'source', 'destination'],
  skills: true,
  skillsRoot: '',
  debug: false,
}

/** @param {unknown} value @returns {boolean | undefined} */
function asBoolean(value) {
  if (typeof value === 'boolean') return value
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase()
    if (normalized === 'true' || normalized === '1' || normalized === 'yes') return true
    if (normalized === 'false' || normalized === '0' || normalized === 'no') return false
  }
  return undefined
}

/** @param {unknown} value @returns {string[] | undefined} */
function asStringArray(value) {
  if (Array.isArray(value)) return value.filter((item) => typeof item === 'string').map((item) => item.trim()).filter(Boolean)
  if (typeof value === 'string' && value.trim()) {
    return value.split(',').map((item) => item.trim()).filter(Boolean)
  }
  return undefined
}

/** @param {unknown} value @param {number} fallback */
function asNumber(value, fallback) {
  const parsed = typeof value === 'number' ? value : Number.parseInt(String(value ?? ''), 10)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback
}

/**
 * Merge a Loader row config onto the defaults. Unknown keys are dropped so a
 * stale patch cannot smuggle behaviour into the plugin.
 *
 * @param {unknown} raw
 * @returns {TenetoraConfig}
 */
export function normalizeConfig(raw) {
  const input = raw && typeof raw === 'object' ? /** @type {Record<string, unknown>} */ (raw) : {}
  const config = { ...DEFAULTS }

  const enabled = asBoolean(input.enabled)
  if (enabled !== undefined) config.enabled = enabled

  if (typeof input.cli === 'string' && input.cli.trim()) config.cli = input.cli.trim()
  const cliArgs = asStringArray(input.cliArgs)
  if (cliArgs) config.cliArgs = cliArgs

  if (input.env && typeof input.env === 'object' && !Array.isArray(input.env)) {
    /** @type {Record<string,string>} */
    const env = {}
    for (const [key, value] of Object.entries(input.env)) {
      if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
        env[key] = String(value)
      }
    }
    config.env = env
  }

  config.timeoutMs = asNumber(input.timeoutMs, config.timeoutMs)

  const contextSection = asBoolean(input.contextSection)
  if (contextSection !== undefined) config.contextSection = contextSection
  if (input.contextMode === 'card' || input.contextMode === 'full') config.contextMode = input.contextMode
  if (typeof input.sectionName === 'string' && input.sectionName.trim()) config.sectionName = input.sectionName.trim()
  config.sectionOrder = asNumber(input.sectionOrder, config.sectionOrder)
  config.maxSectionChars = asNumber(input.maxSectionChars, config.maxSectionChars)

  const guardGit = asBoolean(input.guardGit)
  if (guardGit !== undefined) config.guardGit = guardGit
  const strictEvidence = asBoolean(input.strictEvidence)
  if (strictEvidence !== undefined) config.strictEvidence = strictEvidence
  const guardLocalEnv = asBoolean(input.guardLocalEnv)
  if (guardLocalEnv !== undefined) config.guardLocalEnv = guardLocalEnv
  if (input.onGuardFailure === 'ask' || input.onGuardFailure === 'deny') {
    config.onGuardFailure = input.onGuardFailure
  }
  if (input.externalInput === 'block' || input.externalInput === 'annotate' || input.externalInput === 'off') {
    config.externalInput = input.externalInput
  }
  const externalTools = asStringArray(input.externalInputTools)
  if (externalTools) config.externalInputTools = externalTools.map((item) => item.toLowerCase())
  const recordObservation = asBoolean(input.recordObservation)
  if (recordObservation !== undefined) config.recordObservation = recordObservation
  if (input.contextInjection === 'off' || input.contextInjection === 'auto') {
    config.contextInjection = input.contextInjection
  }
  config.injectionMaxChars = asNumber(input.injectionMaxChars, config.injectionMaxChars)
  if (input.completionReminder === 'off' || input.completionReminder === 'auto') {
    config.completionReminder = input.completionReminder
  }
  if (input.delegation === 'off' || input.delegation === 'auto') {
    config.delegation = input.delegation
  }
  const delegationTools = asStringArray(input.delegationTools)
  if (delegationTools) config.delegationTools = delegationTools.map((item) => item.toLowerCase())

  const shellTools = asStringArray(input.shellTools)
  if (shellTools) config.shellTools = shellTools.map((item) => item.toLowerCase())
  const fileTools = asStringArray(input.fileTools)
  if (fileTools) config.fileTools = fileTools.map((item) => item.toLowerCase())
  const pathFields = asStringArray(input.pathFields)
  if (pathFields) config.pathFields = pathFields

  const skills = asBoolean(input.skills)
  if (skills !== undefined) config.skills = skills
  if (typeof input.skillsRoot === 'string' && input.skillsRoot.trim()) config.skillsRoot = input.skillsRoot.trim()

  const debug = asBoolean(input.debug)
  if (debug !== undefined) config.debug = debug

  return config
}
