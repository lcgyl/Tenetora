# Security Boundary Rules

Source: Tenetora default rules.

- Never copy secrets, tokens, private keys, password values, or credential-bearing local configs into shared `.tenetora` content.
- Treat tool-private local files as sensitive unless a registry or user confirmation says otherwise.
- When reporting a secret finding, name the file and key or pattern, but do not repeat the secret value.
- If a secret may have been committed, ask the user to rotate it; do not claim that deletion alone fixes exposure.
- Prefer environment variables, user-level secret stores, CI secret stores, or ignored local files for credentials.
- Do not weaken scans, ignore patterns, or guardrails just to make an audit pass.
- A gate failure never authorizes destroying local configuration; keep `.env*` and other local files intact and remove them from the index with `git restore --staged <path>`.

## Host Environment Changes

- A user's explicit request to install, upgrade, repair, or uninstall Tenetora authorizes the documented Tenetora-managed changes required by that operation and selected scope. This narrow authorization covers Tenetora's managed home, release/runtime directories, launcher and PATH entry, and the selected AI host's Tenetora plugin, skill, hook, and managed configuration surfaces. Do not require a second per-file authorization for those declared product operations.
- Tenetora lifecycle authorization does not extend to installing or starting Docker, Podman, databases, unrelated runtimes or package managers, system services, firewall rules, or other third-party infrastructure. Those changes require separate explicit authorization naming the dependency and scope.
- Do not install Docker, Podman, databases, runtimes, package managers, system packages, browsers, daemons, or other host-level software merely to make a task or test pass unless the user explicitly authorizes that specific host change.
- Do not start or deploy long-running host services, container stacks, virtual machines, or background daemons without explicit authorization, even when a missing service blocks verification.
- Outside a declared product lifecycle operation or other explicit authorization, do not modify user or system `PATH`, shell startup files, package-manager configuration, service-manager state, firewall or network configuration, scheduled jobs, or persistent host data.
- Read-only detection of an existing tool or service is allowed. Prefer repository-native commands, already-provisioned services, CI, and bounded mocks or substitutes that do not mutate the host.
- Project-local dependency restoration through the repository's declared package manager is not a host installation, but it must still follow the project's dependency and lockfile rules. Install or postinstall scripts must not escape the project/cache boundary or mutate host configuration without separate explicit authorization.
- When a required host dependency or service is unavailable, report the exact missing prerequisite and the closest safe verification completed. Ask the developer to provide it or explicitly authorize installation or startup; task urgency and completion pressure never imply authorization.
- Explicit authorization must identify the software or service and the intended scope. Do not broaden permission for one dependency into permission to alter unrelated host configuration, PATH, shell profiles, service managers, firewall rules, or persistent data.
