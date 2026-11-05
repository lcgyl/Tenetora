"""DeepSeek Harness (DSH) installation contract.

DSH composes plugins through Cordis bundles rather than a marketplace manifest,
so its installation surface is the profile: the dependency entry plus
`dsh.profile.bundles`, or a direct row in the user patch layer. The installer
delegates the managed path to the Harness CLI (`dsh plugin ... add`) and keeps a
reviewable patch row for the offline, application-running case.
"""

from typing import Any

from .base import SurfaceOperations, ToolDefinition

DEFINITION = ToolDefinition(
    name="dsh",
    project_directory=".dsh",
    global_home_env="DSH_HOME",
    global_home_suffix=(".dsh",),
    detection_paths=(
        (".dsh",),
        (".dsh", "profiles", "desktop"),
        ("Library", "Application Support", "DeepSeek Harness"),
    ),
    installer="cordis-bundle",
    runtime_kind="cordis-plugin",
)


class Installer:
    definition = DEFINITION

    def install_native_surface(self, operations: SurfaceOperations, **kwargs: Any) -> Any:
        return operations.install_cordis(**kwargs)

    def status_native_surface(self, operations: SurfaceOperations, **kwargs: Any) -> Any:
        return operations.status_cordis(**kwargs)
