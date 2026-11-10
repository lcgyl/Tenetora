import { spawnSync } from "node:child_process"
import { existsSync } from "node:fs"
import { dirname, join, resolve } from "node:path"

const TENETORA_MARKER = "tenetora-runtime-adapter"
const TENETORA_VERSION = "__TENETORA_VERSION__"
const DEFAULT_HOOK_TIMEOUT_MS = 25000
const TENETORA_ROOT = __TENETORA_PACKAGE_ROOT__
const TENETORA_RUNNER = join(
  TENETORA_ROOT,
  "hooks",
  process.platform === "win32" ? "run-hook.cmd" : "run-hook",
)

function hookInvocation(mode) {
  // Node escapes quotes inside an argv element as \" when it builds a Windows
  // command line. cmd.exe has no \" escape, so it receives a literal \"path\" and
  // fails with "not recognized as an internal or external command" (exit 1).
  // windowsVerbatimArguments hands cmd.exe the quoted path unescaped; /s strips
  // the outer quote pair and runs the rest as-is.
  if (process.platform === "win32") {
    return {
      command: process.env.ComSpec || "cmd.exe",
      args: ["/d", "/s", "/c", `""${TENETORA_RUNNER}" ${mode} --platform opencode"`],
      windowsVerbatimArguments: true,
    }
  }
  return { command: TENETORA_RUNNER, args: [mode, "--platform", "opencode"] }
}

function runHook(mode, payload, cwd) {
  const invocation = hookInvocation(mode)
  const result = spawnSync(
    invocation.command,
    invocation.args,
    {
      cwd,
      env: {
        ...process.env,
        TENETORA_HOOK_PLATFORM: "opencode",
        TENETORA_PLUGIN_ROOT: TENETORA_ROOT,
      },
      input: JSON.stringify(payload),
      encoding: "utf8",
      timeout: hookTimeoutMs(),
      maxBuffer: 256 * 1024,
      windowsVerbatimArguments: invocation.windowsVerbatimArguments,
    },
  )
  if (result.error || result.status !== 0) return { _tenetoraFailure: true }
  if (!result.stdout.trim()) return {}
  try {
    const parsed = JSON.parse(result.stdout)
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed : { _tenetoraFailure: true }
  } catch {
    return { _tenetoraFailure: true }
  }
}

function hookTimeoutMs() {
  const configured = Number.parseInt(process.env.TENETORA_OPENCODE_HOOK_TIMEOUT_MS || "", 10)
  return Number.isFinite(configured) ? Math.min(25000, Math.max(100, configured)) : DEFAULT_HOOK_TIMEOUT_MS
}

function gitWriteIntent(command) {
  // Tokenize enough shell syntax to identify intent on a failed runtime path.
  // Quoted operators stay data; this never executes or authorizes a command.
  const tokens = []
  let word = "", quote = "", escaped = false, present = false
  const flush = () => { if (present) tokens.push(word); word = ""; present = false }
  for (const char of command) {
    if (escaped) { if (char !== "\n") word += char; present = true; escaped = false; continue }
    if (quote) {
      if (char === quote) quote = ""
      else if (char === "\\" && quote === '"') escaped = true
      else word += char
      continue
    }
    if (char === "\\") { escaped = true; present = true }
    else if (char === '"' || char === "'") { quote = char; present = true }
    else if (";&|\n".includes(char)) { flush(); tokens.push(null) }
    else if (/\s/.test(char)) flush()
    else { word += char; present = true }
  }
  if (quote || escaped) return false
  flush()
  for (let index = 0; index < tokens.length; index += 1) {
    if (index && tokens[index - 1] !== null) continue
    let cursor = index
    while (typeof tokens[cursor] === "string" && /^[A-Za-z_][A-Za-z0-9_]*=/.test(tokens[cursor])) cursor += 1
    const executable = typeof tokens[cursor] === "string" ? tokens[cursor].replaceAll("\\", "/").split("/").pop().toLowerCase() : ""
    if (!["git", "git.exe"].includes(executable)) continue
    cursor += 1
    while (typeof tokens[cursor] === "string") {
      const token = tokens[cursor]
      if (["-C", "-c", "--git-dir", "--work-tree"].includes(token)) { cursor += 2; continue }
      if (/^(?:-C.+|-c.+|--git-dir=|--work-tree=)/.test(token) || ["--no-pager", "--paginate", "--bare", "--no-optional-locks"].includes(token)) { cursor += 1; continue }
      if (["commit", "push"].includes(token)) return true
      break
    }
  }
  return false
}

function governedGitWrite(command, cwd) {
  // The Python authority rejects compound writes and targets outside the
  // caller's governance context. Do not infer shell execution or a new cwd
  // while that authority is unavailable. Ungoverned callers remain silent.
  if (!gitWriteIntent(command)) return false
  for (let current = resolve(cwd); ; current = dirname(current)) {
    if (existsSync(join(current, ".tenetora"))) return true
    if (existsSync(join(current, ".git")) || dirname(current) === current) return false
  }
}

function contextFrom(payload) {
  return payload?.hookSpecificOutput?.additionalContext || payload?.systemMessage || ""
}

function denialFrom(payload) {
  if (payload?.hookSpecificOutput?.permissionDecision === "deny") {
    return payload.hookSpecificOutput.permissionDecisionReason || "Blocked by Tenetora."
  }
  if (payload?.decision === "block") return payload.reason || "Blocked by Tenetora."
  return ""
}

function validGitResponse(payload) {
  if (payload?.decision === "block") return typeof payload.reason === "string"
  const specific = payload?.hookSpecificOutput
  return specific?.hookEventName === "PreToolUse" && (
    typeof specific.additionalContext === "string" ||
    ["allow", "deny"].includes(specific.permissionDecision)
  )
}

export const TenetoraPlugin = async ({ directory, worktree }) => {
  const injectedSessions = new Set()
  const root = worktree || directory
  return {
    "experimental.chat.system.transform": async (input, output) => {
      const sessionID = input.sessionID || "default"
      if (injectedSessions.has(sessionID)) return
      const payload = runHook(
        "session-start",
        { hook_event_name: "SessionStart", session_id: sessionID, cwd: root },
        root,
      )
      const context = contextFrom(payload)
      if (context) output.system.push(context)
      injectedSessions.add(sessionID)
    },
    "tool.execute.before": async (input, output) => {
      const command = output?.args?.command
      if (typeof command !== "string") return
      const payload = runHook(
        "pre-tool-commit",
        {
          hook_event_name: "PreToolUse",
          session_id: input.sessionID,
          tool_name: input.tool,
          tool_input: output.args,
          cwd: root,
        },
        root,
      )
      if (governedGitWrite(command, root) && (payload._tenetoraFailure || !validGitResponse(payload))) {
        throw new Error("Tenetora Git guard is unavailable or returned invalid output; restore the managed runtime and retry.")
      }
      const reason = denialFrom(payload)
      if (reason) throw new Error(reason)
    },
    "experimental.session.compacting": async (input, output) => {
      const payload = runHook(
        "session-start",
        { hook_event_name: "SessionStart", session_id: input.sessionID, cwd: root },
        root,
      )
      const context = contextFrom(payload)
      if (context) output.context.push(context)
    },
  }
}

void TENETORA_MARKER
void TENETORA_VERSION
