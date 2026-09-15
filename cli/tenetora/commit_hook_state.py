"""Portable commit-hook command state helpers."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import shlex
from pathlib import Path
from typing import Any


STATE_SCHEMA_VERSION = 2
CLI_LOCATOR_FIELD = "cli_locator"
VALID_DECISIONS = frozenset({"pending", "install", "defer", "decline"})
_COMMAND_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def managed_cli_command(*, home: Path | None = None, os_name: str | None = None) -> str:
    """Resolve the managed CLI without persisting its machine-local absolute path."""

    effective_os = os.name if os_name is None else os_name
    configured_home = os.environ.get("TENETORA_HOME")
    managed_home = home
    if managed_home is None:
        managed_home = Path(configured_home).expanduser() if configured_home else Path.home() / ".tenetora"
    suffix = ".cmd" if effective_os == "nt" else ""
    return str(managed_home / "bin" / f"tenetora{suffix}")


def _executable_sha256(command: str) -> str | None:
    if "/" not in command and "\\" not in command:
        return None
    path = Path(command)
    try:
        if not path.is_file():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def portable_cli_locator(cli_command: str | None) -> dict[str, str]:
    """Represent a hook command without storing a machine-local absolute path."""

    command = str(cli_command or managed_cli_command()).strip()
    if command == managed_cli_command():
        return {"kind": "managed-home"}
    if _COMMAND_NAME_RE.fullmatch(command):
        return {"kind": "path-command", "command": command}
    locator = {
        "kind": "command-sha256",
        "sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
    }
    executable_sha256 = _executable_sha256(command)
    if executable_sha256 is not None:
        locator["executable_sha256"] = executable_sha256
    return locator


def valid_cli_locator(locator: Any) -> bool:
    """Validate locator shape without resolving a machine-local executable."""

    if not isinstance(locator, dict):
        return False
    kind = locator.get("kind")
    if kind == "managed-home":
        return set(locator) == {"kind"}
    if kind == "path-command":
        command = locator.get("command")
        return set(locator) == {"kind", "command"} and isinstance(command, str) and bool(
            _COMMAND_NAME_RE.fullmatch(command)
        )
    if kind == "command-sha256":
        if set(locator) not in ({"kind", "sha256"}, {"kind", "sha256", "executable_sha256"}):
            return False
        if not isinstance(locator.get("sha256"), str) or not _SHA256_RE.fullmatch(locator["sha256"]):
            return False
        executable_sha256 = locator.get("executable_sha256")
        return executable_sha256 is None or (
            isinstance(executable_sha256, str) and bool(_SHA256_RE.fullmatch(executable_sha256))
        )
    return False


def valid_state_shape(state: Any) -> bool:
    """Return whether a hook state is safe to consume as the current schema."""

    if not isinstance(state, dict) or state.get("version") != STATE_SCHEMA_VERSION:
        return False
    decision = state.get("decision")
    if decision not in VALID_DECISIONS:
        return False
    if "cli_command" in state:
        return False
    locator = state.get(CLI_LOCATOR_FIELD)
    if decision == "install":
        return valid_cli_locator(locator)
    return locator is None


def extract_managed_hook_command(text: str) -> str | None:
    """Extract the single executable from a current Tenetora shell wrapper."""

    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != "run_tenetora() {":
            continue
        for body_line in lines[index + 1 :]:
            stripped = body_line.strip()
            if stripped == "}":
                return None
            if not stripped:
                continue
            try:
                tokens = shlex.split(stripped, posix=True)
            except ValueError:
                return None
            if len(tokens) != 2 or tokens[1] != "$@":
                return None
            return tokens[0]
    return None


def command_from_state(state: dict[str, Any] | None, hook_text: str) -> str | None:
    """Resolve the command expected by state, including the v1 compatibility path."""

    if (
        isinstance(state, dict)
        and state.get("version") == STATE_SCHEMA_VERSION
        and not isinstance(state.get(CLI_LOCATOR_FIELD), dict)
    ):
        return None
    if isinstance(state, dict) and isinstance(state.get(CLI_LOCATOR_FIELD), dict):
        locator = state[CLI_LOCATOR_FIELD]
        kind = locator.get("kind")
        if kind == "managed-home" and set(locator) == {"kind"}:
            return managed_cli_command()
        if kind == "path-command" and set(locator) == {"kind", "command"}:
            command = locator.get("command")
            return command if isinstance(command, str) and _COMMAND_NAME_RE.fullmatch(command) else None
        if kind == "command-sha256" and set(locator) in (
            {"kind", "sha256"},
            {"kind", "sha256", "executable_sha256"},
        ):
            expected = locator.get("sha256")
            command = extract_managed_hook_command(hook_text)
            if not isinstance(expected, str) or not _SHA256_RE.fullmatch(expected) or command is None:
                return None
            actual = hashlib.sha256(command.encode("utf-8")).hexdigest()
            if not hmac.compare_digest(actual, expected):
                return None
            expected_executable = locator.get("executable_sha256")
            if expected_executable is not None:
                actual_executable = _executable_sha256(command)
                if (
                    not isinstance(expected_executable, str)
                    or not _SHA256_RE.fullmatch(expected_executable)
                    or actual_executable is None
                    or not hmac.compare_digest(actual_executable, expected_executable)
                ):
                    return None
            return command
        return None

    if isinstance(state, dict) and state.get("cli_command"):
        return str(state["cli_command"])
    return managed_cli_command()


def state_has_current_locator(state: dict[str, Any] | None, cli_command: str | None) -> bool:
    """Return whether state already uses the canonical portable locator schema."""

    return bool(
        isinstance(state, dict)
        and state.get("version") == STATE_SCHEMA_VERSION
        and "cli_command" not in state
        and state.get(CLI_LOCATOR_FIELD) == portable_cli_locator(cli_command)
    )
