"""ZCode installation contract."""

from typing import Any

from .base import SurfaceOperations, ToolDefinition


DEFINITION = ToolDefinition(
    name="zcode",
    project_directory=".zcode",
    global_home_env="ZCODE_HOME",
    global_home_suffix=(".zcode",),
    detection_paths=((".zcode",), ("Library", "Application Support", "ZCode")),
    installer="zcode-native",
    runtime_kind="process",
)


class Installer:
    definition = DEFINITION

    def install_native_surface(self, operations: SurfaceOperations, **kwargs: Any) -> Any:
        return operations.install_zcode(**kwargs)

    def status_native_surface(self, operations: SurfaceOperations, **kwargs: Any) -> Any:
        return None
