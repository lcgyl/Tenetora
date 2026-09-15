import { spawnSync } from "node:child_process"
import { join } from "node:path"

const TENETORA_MARKER = "tenetora-runtime-adapter"
const TENETORA_VERSION = "__TENETORA_VERSION__"
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
      windowsVerbatimArguments: invocation.windowsVerbatimArguments,
    },
  )
  if (result.status !== 0 || !result.stdout.trim()) return {}
  try {
    return JSON.parse(result.stdout)
  } catch {
    return {}
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
