"""Generic Agents installation contract."""

from typing import Any

from .base import SurfaceOperations, ToolDefinition


DEFINITION = ToolDefinition(
    name="agents",
    project_directory=".agents",
    global_home_env="AGENT_SKILLS_HOME",
    global_home_suffix=(".agents",),
    installer="skills",
    runtime_kind="none",
    global_env_is_skill_base=True,
    always_detected=True,
)


class Installer:
    definition = DEFINITION

    def install_native_surface(self, operations: SurfaceOperations, **kwargs: Any) -> Any:
        return None

    def status_native_surface(self, operations: SurfaceOperations, **kwargs: Any) -> Any:
        return None
