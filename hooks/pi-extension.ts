import { spawn } from "node:child_process"
import { createHash } from "node:crypto"
import { join } from "node:path"

const TENETORA_MARKER = "tenetora-pi-runtime-adapter"
const TENETORA_VERSION = "__TENETORA_VERSION__"
const TENETORA_ROOT = __TENETORA_PACKAGE_ROOT__
const TENETORA_RUNNER = join(
  TENETORA_ROOT,
  "hooks",
  process.platform === "win32" ? "run-hook.cmd" : "run-hook",
)
const DEFAULT_HOOK_TIMEOUT_MS = 2500
const MAX_HOOK_OUTPUT_BYTES = 256 * 1024
const OPAQUE_SESSION_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$/

function hookInvocation(mode) {
  // Node escapes quotes inside an argv element as \" when it builds a Windows
  // command line. cmd.exe has no \" escape, so it receives a literal \"path\" and
  // fails with "not recognized as an internal or external command" (exit 1),
  // which this extension then reports as a failed hook. windowsVerbatimArguments
  // hands cmd.exe the quoted path unescaped; /s strips the outer quote pair and
  // runs the rest as-is. The outer pair is only consumed because /s is present.
  if (process.platform === "win32") {
    return {
      command: process.env.ComSpec || "cmd.exe",
      args: ["/d", "/s", "/c", `""${TENETORA_RUNNER}" ${mode} --platform pi"`],
      windowsVerbatimArguments: true,
    }
  }
  return { command: TENETORA_RUNNER, args: [mode, "--platform", "pi"] }
}

function hookTimeoutMs() {
  const configured = Number.parseInt(process.env.TENETORA_PI_HOOK_TIMEOUT_MS || "", 10)
  if (!Number.isFinite(configured)) return DEFAULT_HOOK_TIMEOUT_MS
  return Math.min(Math.max(configured, 100), 10000)
}

function runHook(mode, payload, cwd) {
  const invocation = hookInvocation(mode)
  return new Promise((resolve) => {
    let child
    try {
      child = spawn(invocation.command, invocation.args, {
        cwd,
        env: {
          ...process.env,
          TENETORA_HOOK_PLATFORM: "pi",
          TENETORA_PLUGIN_ROOT: TENETORA_ROOT,
        },
        stdio: ["pipe", "pipe", "pipe"],
        windowsVerbatimArguments: invocation.windowsVerbatimArguments,
      })
    } catch {
      resolve({ ok: false, payload: {}, reason: "spawn-error" })
      return
    }

    let stdout = ""
    let stdoutBytes = 0
    let stderr = ""
    let settled = false
    let timer

    const finish = (result) => {
      if (settled) return
      settled = true
      if (timer) clearTimeout(timer)
      resolve(result)
    }

    const fail = (reason) => {
      if (settled) return
      try {
        if (process.platform === "win32") child.kill()
        else child.kill("SIGKILL")
      } catch {
        // The result is already fail-closed even when the child exits late.
      }
      finish({ ok: false, payload: {}, reason })
    }

    timer = setTimeout(() => fail("timeout"), hookTimeoutMs())
    timer.unref?.()
    child.stdout.on("data", (chunk) => {
      if (settled) return
      const text = chunk.toString()
      stdoutBytes += Buffer.byteLength(text, "utf8")
      if (stdoutBytes > MAX_HOOK_OUTPUT_BYTES) {
        fail("output-too-large")
        return
      }
      stdout += text
    })
    child.stderr.on("data", (chunk) => {
      // Keep a bounded tail: the launcher writes its real diagnosis (missing or
      // too-old Python, PATH) here, and discarding it makes any hook failure
      // indistinguishable from a crash.
      if (stderr.length < 4096) stderr += chunk.toString()
    })
    child.stdin.on("error", () => {})
    child.on("error", () => fail("spawn-error"))
    child.on("close", (status) => {
      if (settled) return
      if (status !== 0) {
        finish({ ok: false, payload: {}, reason: "exit", stderr })
        return
      }
      if (!stdout.trim()) {
        finish({ ok: true, payload: {} })
        return
      }
      try {
        finish({ ok: true, payload: JSON.parse(stdout) })
      } catch {
        finish({ ok: false, payload: {}, reason: "invalid-json" })
      }
    })
    try {
      child.stdin.end(JSON.stringify(payload))
    } catch {
      fail("stdin-error")
    }
  })
}

function firstLine(text) {
  const line = String(text || "").trim().split("\n")[0].trim()
  return line.length > 200 ? `${line.slice(0, 200)}…` : line
}

function contextFrom(payload) {
  const context = payload?.hookSpecificOutput?.additionalContext || payload?.systemMessage || ""
  return typeof context === "string" ? context : ""
}

function denialFrom(payload) {
  if (payload?.hookSpecificOutput?.permissionDecision === "deny") {
    return payload.hookSpecificOutput.permissionDecisionReason || "Blocked by Tenetora."
  }
  if (payload?.decision === "block") return payload.reason || "Blocked by Tenetora."
  return ""
}

function stableSessionId(value) {
  const text = typeof value === "string" ? value.trim() : ""
  if (!text) return ""
  if (OPAQUE_SESSION_ID.test(text)) return text
  return `pi-session-${createHash("sha256").update(text.replaceAll("\\", "/"), "utf8").digest("hex").slice(0, 32)}`
}

function sessionId(ctx) {
  let sessionFile = ""
  try {
    sessionFile = ctx?.sessionManager?.getSessionFile?.() || ""
  } catch {
    sessionFile = ""
  }
  return stableSessionId(sessionFile || ctx?.sessionId || ctx?.sessionID)
}

function sessionContextKey(ctx, id) {
  if (id) return `id:${id}`
  if (ctx?.sessionManager && (typeof ctx.sessionManager === "object" || typeof ctx.sessionManager === "function")) {
    return ctx.sessionManager
  }
  if (ctx && (typeof ctx === "object" || typeof ctx === "function")) return ctx
  return null
}

function workingDirectory(event, ctx) {
  return event?.cwd || ctx?.cwd || process.cwd()
}

function readOnlyTool(toolName) {
  return ["read", "grep", "find"].includes(String(toolName || "").trim().toLowerCase())
}

export default function TenetoraPiExtension(pi) {
  const sessionContexts = new Map()

  async function refreshSessionContext(ctx, cwd) {
    const id = sessionId(ctx)
    const key = sessionContextKey(ctx, id)
    if (key !== null) sessionContexts.delete(key)
    const result = await runHook(
      "session-start",
      { hook_event_name: "SessionStart", session_id: id, cwd },
      cwd,
    )
    const context = contextFrom(result.payload)
    if (key !== null && result.ok && context) sessionContexts.set(key, context)
    return result
  }

  pi.on("session_start", async (_event, ctx) => {
    await refreshSessionContext(ctx, ctx?.cwd || process.cwd())
  })

  pi.on("before_agent_start", async (event, ctx) => {
    const cwd = workingDirectory(event, ctx)
    const id = sessionId(ctx)
    const key = sessionContextKey(ctx, id)
    if (key === null || !sessionContexts.has(key)) await refreshSessionContext(ctx, cwd)
    const promptResult = await runHook(
      "user-prompt-submit",
      {
        hook_event_name: "UserPromptSubmit",
        session_id: id,
        prompt: event?.prompt || "",
        cwd,
      },
      cwd,
    )
    const context = [
      key === null ? "" : sessionContexts.get(key),
      contextFrom(promptResult.payload),
    ].filter(Boolean).join("\n\n")
    if (!context) return undefined
    return {
      systemPrompt: [event?.systemPrompt, context].filter(Boolean).join("\n\n"),
    }
  })

  pi.on("session_before_compact", async (_event, ctx) => {
    // Keep Pi's own summary/history intact; inject refreshed context on the next prompt.
    await refreshSessionContext(ctx, ctx?.cwd || process.cwd())
  })

  pi.on("session_shutdown", async () => {
    sessionContexts.clear()
  })

  pi.on("tool_call", async (event, ctx) => {
    const cwd = workingDirectory(event, ctx)
    const result = await runHook(
      "pre-tool-commit",
      {
        hook_event_name: "PreToolUse",
        session_id: sessionId(ctx),
        tool_name: event?.toolName,
        tool_input: event?.input || {},
        cwd,
      },
      cwd,
    )
    if (!result.ok && !readOnlyTool(event?.toolName)) {
      const suffix = result.reason === "timeout" ? " after timeout" : ""
      const detail = firstLine(result.stderr)
      return {
        block: true,
        reason: `Tenetora hook unavailable${suffix}${detail ? ` (${detail})` : ""}; tool execution is blocked until the managed runtime is restored.`,
      }
    }
    const reason = denialFrom(result.payload)
    if (reason) return { block: true, reason }
    return undefined
  })
}

void TENETORA_MARKER
void TENETORA_VERSION
