"""Codex installation contract."""

from typing import Any

from .base import SurfaceOperations, ToolDefinition


DEFINITION = ToolDefinition(
    name="codex",
    project_directory=".codex",
    global_home_env="CODEX_HOME",
    global_home_suffix=(".codex",),
    installer="native-marketplace",
    runtime_kind="command",
    native_commands=("codex",),
    native_home_env="CODEX_HOME",
    plugin_manifest_directory=".codex-plugin",
    versioned_plugin_cache=True,
)


class Installer:
    definition = DEFINITION

    def install_native_surface(self, operations: SurfaceOperations, **kwargs: Any) -> Any:
        return operations.install_native(**kwargs)

    def status_native_surface(self, operations: SurfaceOperations, **kwargs: Any) -> Any:
        return operations.status_native(**kwargs)
