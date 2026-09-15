"""Per-tool installation contracts used by the compatibility installer."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


class SurfaceOperations(Protocol):
    def install_native(self, **kwargs: Any) -> Any:
        ...

    def install_zcode(self, **kwargs: Any) -> Any:
        ...

    def status_native(self, **kwargs: Any) -> Any:
        ...


@dataclass(frozen=True)
class ToolDefinition:
    """Stable platform-specific facts; mutation and transactions stay in the core."""

    name: str
    project_directory: str
    global_home_env: str | None
    global_home_suffix: tuple[str, ...]
    installer: str
    runtime_kind: str
    global_env_is_skill_base: bool = False
    global_home_aliases: tuple[str, ...] = ()
    global_home_root_env: str | None = None
    global_home_root_suffix: tuple[str, ...] = ()
    detection_paths: tuple[tuple[str, ...], ...] = ()
    always_detected: bool = False
    native_commands: tuple[str, ...] = ()
    native_home_env: str | None = None
    plugin_manifest_directory: str | None = None
    plugin_list_shape: str = "installed"
    versioned_plugin_cache: bool = False

    def skill_base(self, scope: str, project_root: Path) -> Path:
        if scope == "project":
            return project_root / self.project_directory / "skills"
        home = Path.home()
        for variable in (self.global_home_env, *self.global_home_aliases):
            if not variable:
                continue
            configured = os.environ.get(variable)
            if configured:
                if self.global_env_is_skill_base:
                    return Path(configured).expanduser()
                return Path(configured).expanduser() / "skills"
        if self.global_home_root_env and os.environ.get(self.global_home_root_env):
            return Path(os.environ[self.global_home_root_env]).expanduser().joinpath(
                *self.global_home_root_suffix, "skills"
            )
        return home.joinpath(*self.global_home_suffix, "skills")

    def command(self, command_exists: Callable[[str], bool]) -> str | None:
        for candidate in self.native_commands:
            if command_exists(candidate):
                return candidate
        return None

    def native_home(self, project_root: Path) -> Path | None:
        if self.native_home_env is None:
            return None
        configured = next(
            (
                os.environ.get(variable)
                for variable in (self.native_home_env, *self.global_home_aliases)
                if os.environ.get(variable)
            ),
            None,
        )
        return Path(configured or Path.home().joinpath(*self.global_home_suffix)).expanduser()

    def detected(self, home: Path, command_exists: Callable[[str], bool]) -> bool:
        if self.always_detected:
            return True
        if any(command_exists(command) for command in self.native_commands):
            return True
        if any(
            variable and os.environ.get(variable)
            for variable in (self.global_home_env, *self.global_home_aliases)
        ):
            return True
        if self.global_home_root_env and os.environ.get(self.global_home_root_env):
            root = Path(os.environ[self.global_home_root_env]).expanduser()
            if root.joinpath(*self.global_home_root_suffix).exists():
                return True
        paths = self.detection_paths or (self.global_home_suffix,)
        return any(home.joinpath(*relative).exists() for relative in paths)


def project_tool_directory(name: str) -> str:
    return f".{name}"


class ToolInstaller(Protocol):
    definition: ToolDefinition

    def install_native_surface(self, operations: SurfaceOperations, **kwargs: Any) -> Any:
        ...

    def status_native_surface(self, operations: SurfaceOperations, **kwargs: Any) -> Any:
        ...
