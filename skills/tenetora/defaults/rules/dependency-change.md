# Dependency Change Rules

Source: Tenetora default rules.

- Do not upgrade major runtimes, frameworks, package managers, or build plugins without explicit user approval.
- Prefer the project-native dependency mechanism: lockfiles, centralized version definitions, package manager scripts, or existing dependency constraints.
- Do not add direct versions when the project centralizes dependency versions elsewhere.
- Before changing dependencies, identify why the change is needed, the affected modules, and the smallest verification command.
- After changing dependencies, update lockfiles or generated dependency metadata only through the project-native tool.
- If a dependency change is only a workaround, stop and report the root-cause gap instead of hiding it.
