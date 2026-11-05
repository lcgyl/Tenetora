"""Cursor installation contract."""

from typing import Any

from .base import SurfaceOperations, ToolDefinition


DEFINITION = ToolDefinition(
    name="cursor",
    project_directory=".cursor",
    global_home_env="CURSOR_HOME",
    global_home_suffix=(".cursor",),
    installer="runtime-adapter",
    runtime_kind="hooks-json",
)


class Installer:
    definition = DEFINITION

    def install_native_surface(self, operations: SurfaceOperations, **kwargs: Any) -> Any:
        return None

    def status_native_surface(self, operations: SurfaceOperations, **kwargs: Any) -> Any:
        return None
