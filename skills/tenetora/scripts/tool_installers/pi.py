"""Pi installation contract."""

from typing import Any

from .base import SurfaceOperations, ToolDefinition


DEFINITION = ToolDefinition(
    name="pi",
    project_directory=".pi",
    global_home_env=None,
    global_home_suffix=(".pi", "agent"),
    installer="runtime-adapter",
    runtime_kind="typescript-extension",
    detection_paths=((".pi", "agent"), (".pi",)),
    native_commands=("pi",),
)


class Installer:
    definition = DEFINITION

    def install_native_surface(self, operations: SurfaceOperations, **kwargs: Any) -> Any:
        return None

    def status_native_surface(self, operations: SurfaceOperations, **kwargs: Any) -> Any:
        return None
