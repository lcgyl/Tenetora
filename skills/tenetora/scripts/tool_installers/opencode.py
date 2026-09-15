"""OpenCode installation contract."""

from typing import Any

from .base import SurfaceOperations, ToolDefinition


DEFINITION = ToolDefinition(
    name="opencode",
    project_directory=".opencode",
    global_home_env="OPENCODE_HOME",
    global_home_suffix=(".config", "opencode"),
    global_home_root_env="XDG_CONFIG_HOME",
    global_home_root_suffix=("opencode",),
    detection_paths=((".config", "opencode"),),
    installer="runtime-adapter",
    runtime_kind="javascript-plugin",
)


class Installer:
    definition = DEFINITION

    def install_native_surface(self, operations: SurfaceOperations, **kwargs: Any) -> Any:
        return None

    def status_native_surface(self, operations: SurfaceOperations, **kwargs: Any) -> Any:
        return None
