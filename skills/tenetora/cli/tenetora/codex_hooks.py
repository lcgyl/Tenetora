"""Read Codex hook trust state through the host's app-server API."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


TRUSTED_STATES = {"trusted", "managed"}
PROJECT_HOOK_COMMAND_MARKERS = (
    ".tenetora/runtime/hooks/run-hook",
    ".tenetora\\runtime\\hooks\\run-hook.cmd",
)


def _read_messages(stream: Any, messages: "queue.Queue[dict[str, object]]") -> None:
    for line in stream:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            messages.put(payload)


def _wait_for_response(
    messages: "queue.Queue[dict[str, object]]",
    request_id: int,
    deadline: float,
    process: subprocess.Popen[str],
) -> dict[str, object]:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Codex app-server request {request_id} timed out")
        try:
            payload = messages.get(timeout=min(remaining, 0.1))
        except queue.Empty:
            if process.poll() is not None:
                raise RuntimeError(f"Codex app-server exited before response {request_id}")
            continue
        if payload.get("id") == request_id:
            return payload


def list_codex_hooks(
    command: str,
    cwd: Path,
    environment: dict[str, str],
    *,
    client_version: str,
    timeout: float = 8.0,
) -> dict[str, object]:
    """Return Codex's authoritative hook inventory for one project root."""

    unavailable: dict[str, object] = {
        "state": "unavailable",
        "hooks": [],
        "warnings": [],
        "errors": [],
        "detail": "Codex hooks/list is unavailable",
    }
    process: subprocess.Popen[str] | None = None
    reader: threading.Thread | None = None
    try:
        process = subprocess.Popen(
            [command, "app-server", "--stdio"],
            cwd=cwd,
            env=environment,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=1,
        )
        if process.stdin is None or process.stdout is None:
            return unavailable
        messages: "queue.Queue[dict[str, object]]" = queue.Queue()
        reader = threading.Thread(
            target=_read_messages,
            args=(process.stdout, messages),
            daemon=True,
        )
        reader.start()
        initialize = {
            "method": "initialize",
            "id": 0,
            "params": {
                "clientInfo": {
                    "name": "tenetora",
                    "title": "Tenetora",
                    "version": client_version,
                }
            },
        }
        process.stdin.write(json.dumps(initialize, separators=(",", ":")) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + timeout
        initialized = _wait_for_response(messages, 0, deadline, process)
        if "error" in initialized:
            unavailable["detail"] = f"Codex app-server initialization failed: {initialized['error']}"
            return unavailable
        process.stdin.write('{"method":"initialized","params":{}}\n')
        request = {
            "method": "hooks/list",
            "id": 1,
            "params": {"cwds": [str(cwd)]},
        }
        process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        process.stdin.flush()
        response = _wait_for_response(messages, 1, deadline, process)
        if "error" in response:
            unavailable["detail"] = f"Codex hooks/list failed: {response['error']}"
            return unavailable
        response_result = response.get("result")
        data = response_result.get("data") if isinstance(response_result, dict) else None
        hooks: list[dict[str, object]] = []
        warnings: list[object] = []
        errors: list[object] = []
        if isinstance(data, list):
            for item in data:
                item_hooks = item.get("hooks") if isinstance(item, dict) else None
                if isinstance(item_hooks, list):
                    hooks.extend(dict(hook) for hook in item_hooks if isinstance(hook, dict))
                item_warnings = item.get("warnings") if isinstance(item, dict) else None
                if isinstance(item_warnings, list):
                    warnings.extend(item_warnings)
                item_errors = item.get("errors") if isinstance(item, dict) else None
                if isinstance(item_errors, list):
                    errors.extend(item_errors)
        return {
            "state": "available",
            "hooks": hooks,
            "warnings": warnings,
            "errors": errors,
            "detail": f"Codex discovered {len(hooks)} hooks",
        }
    except (OSError, RuntimeError, subprocess.SubprocessError, TimeoutError) as exc:
        unavailable["detail"] = f"Codex hooks/list unavailable: {exc}"
        return unavailable
    finally:
        if process is not None:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            if reader is not None:
                reader.join(timeout=1)
            if process.stdout is not None and not process.stdout.closed:
                process.stdout.close()


def tenetora_hook_sets(
    inventory: dict[str, object],
    plugin_id: str | tuple[str, ...],
) -> dict[str, list[dict[str, object]]]:
    """Split Tenetora hooks by native plugin and project fallback source."""

    plugin_ids = {plugin_id} if isinstance(plugin_id, str) else set(plugin_id)
    raw_hooks = inventory.get("hooks")
    hooks = raw_hooks if isinstance(raw_hooks, list) else []
    native: list[dict[str, object]] = []
    project: list[dict[str, object]] = []
    for raw_hook in hooks:
        if not isinstance(raw_hook, dict):
            continue
        hook = dict(raw_hook)
        if hook.get("pluginId") in plugin_ids:
            native.append(hook)
            continue
        command = hook.get("command")
        if (
            hook.get("source") == "project"
            and hook.get("pluginId") is None
            and isinstance(command, str)
            and any(marker in command for marker in PROJECT_HOOK_COMMAND_MARKERS)
        ):
            project.append(hook)
    return {"native": native, "project": project}


def inspect_codex_plugin_hooks(
    command: str,
    cwd: Path,
    environment: dict[str, str],
    plugin_id: str,
    *,
    client_version: str,
    timeout: float = 8.0,
) -> dict[str, object]:
    """Return authoritative trust state for one enabled Codex plugin's hooks."""

    inventory = list_codex_hooks(
        command,
        cwd,
        environment,
        client_version=client_version,
        timeout=timeout,
    )
    if inventory.get("state") != "available":
        return {
            "state": "unavailable",
            "hooks": 0,
            "trusted": 0,
            "pending": 0,
            "detail": inventory.get("detail", "Codex hooks/list is unavailable"),
        }
    hooks = tenetora_hook_sets(inventory, plugin_id)["native"]
    if not hooks:
        return {
            "state": "missing",
            "hooks": 0,
            "trusted": 0,
            "pending": 0,
            "detail": f"Codex did not discover hooks for {plugin_id}",
        }
    trusted = sum(
        1
        for hook in hooks
        if hook.get("enabled", True) and str(hook.get("trustStatus", "")).lower() in TRUSTED_STATES
    )
    pending = len(hooks) - trusted
    return {
        "state": "active" if pending == 0 else "pending-trust",
        "hooks": len(hooks),
        "trusted": trusted,
        "pending": pending,
        "detail": (
            f"{trusted}/{len(hooks)} Codex plugin hooks are enabled and trusted"
            if pending == 0
            else f"{pending}/{len(hooks)} Codex plugin hooks require trust review"
        ),
    }
