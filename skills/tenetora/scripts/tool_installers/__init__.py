"""Tool-specific installation contracts and deterministic registry."""

from __future__ import annotations

from .agents import DEFINITION as AGENTS, Installer as AgentsInstaller
from .claude import DEFINITION as CLAUDE, Installer as ClaudeInstaller
from .codex import DEFINITION as CODEX, Installer as CodexInstaller
from .cursor import DEFINITION as CURSOR, Installer as CursorInstaller
from .opencode import DEFINITION as OPENCODE, Installer as OpencodeInstaller
from .pi import DEFINITION as PI, Installer as PiInstaller
from .zcode import DEFINITION as ZCODE, Installer as ZcodeInstaller
from .base import ToolDefinition, ToolInstaller


DEFINITIONS: tuple[ToolDefinition, ...] = (AGENTS, CODEX, CLAUDE, CURSOR, OPENCODE, PI, ZCODE)
_BY_NAME = {definition.name: definition for definition in DEFINITIONS}
_INSTALLERS: dict[str, ToolInstaller] = {
    "agents": AgentsInstaller(),
    "codex": CodexInstaller(),
    "claude": ClaudeInstaller(),
    "cursor": CursorInstaller(),
    "opencode": OpencodeInstaller(),
    "pi": PiInstaller(),
    "zcode": ZcodeInstaller(),
}


def tool_definition(name: str) -> ToolDefinition:
    try:
        return _BY_NAME[name]
    except KeyError as exc:
        raise ValueError(f"Unsupported tool: {name}") from exc


def tool_installer(name: str) -> ToolInstaller:
    try:
        return _INSTALLERS[name]
    except KeyError as exc:
        raise ValueError(f"Unsupported tool: {name}") from exc


def tool_names() -> list[str]:
    return [definition.name for definition in DEFINITIONS]


def definitions_for_installer(installer: str) -> tuple[ToolDefinition, ...]:
    return tuple(definition for definition in DEFINITIONS if definition.installer == installer)
