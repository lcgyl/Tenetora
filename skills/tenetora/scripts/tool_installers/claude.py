"""Claude Code installation contract."""

from typing import Any

from .base import SurfaceOperations, ToolDefinition


DEFINITION = ToolDefinition(
    name="claude",
    project_directory=".claude",
    global_home_env="CLAUDE_CONFIG_DIR",
    global_home_suffix=(".claude",),
    installer="native-marketplace",
    runtime_kind="command",
    global_home_aliases=("CLAUDE_HOME",),
    native_commands=("claude", "claude-code"),
    native_home_env="CLAUDE_CONFIG_DIR",
    plugin_manifest_directory=".claude-plugin",
    plugin_list_shape="root",
)


class Installer:
    definition = DEFINITION

    def install_native_surface(self, operations: SurfaceOperations, **kwargs: Any) -> Any:
        return operations.install_native(**kwargs)

    def status_native_surface(self, operations: SurfaceOperations, **kwargs: Any) -> Any:
        return operations.status_native(**kwargs)
