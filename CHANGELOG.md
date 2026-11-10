# Tenetora 0.4.3

## Upgrade preflight fixes

- Allow normal symbolic links inside temporary caches without blocking software upgrades. For an existing canonical governance directory, classification, operational text updates and the upgrade snapshot inventory consistently avoid the contents of ordinary top-level `.cache` and `tmp` directories. npm dependencies, virtual environments and historical cache data remain untouched.
- Redirected governance or cache roots, and unsafe links in rules, manifests or other actual governance inputs, remain blocked. Legacy `.harness` migration retains strict checks.
- Reuse the accepted skills-only rule in failure summaries so normal project-scope capability notices are omitted. Actual BLOCKED results and version or plan mismatches remain visible. Diagnostics identify project-relative entries.
- An `upgrade --check` exit code describes whether inspection completed; machine and installation-unit readiness must still be checked.

## Install or upgrade

Rerun the original installer for an existing installation and select stable explicitly. The upgrade does not require moving or deleting project caches:

```bash
curl -fsSL https://raw.githubusercontent.com/lcgyl/Tenetora/main/install.sh | bash -s -- --channel stable --lang en
```

Windows PowerShell:

```powershell
$env:TENETORA_LANG='en'; $env:TENETORA_CHANNEL='stable'; irm https://raw.githubusercontent.com/lcgyl/Tenetora/main/bootstrap.ps1 | iex
```

A channel-aware CLI can also use `tenetora upgrade --channel stable`. If an older client's preflight fails, rerun the installer to obtain the corrected package. Then run `tenetora --version` and `tenetora doctor`; the version should be `0.4.3`.

## Compatibility

Python 3.9+ and offline ZIPs remain supported. `--force` and `--allow-skills-only` do not bypass actual governance safety failures. Software upgrades do not automatically overwrite project rules. Host trust decisions and partial runtime coverage remain explicit.
