# Tenetora 0.3.40

English | [简体中文](https://gitee.com/lcgyl/tenetora/blob/main/CHANGELOG.md)

## Highlights

- Report missing registered local-environment paths from doctor/status without reading their contents.
- Add high-confidence PreToolUse protection for destructive operations targeting registered paths.
- Lock unmanaged-workspace silence with Stop regression coverage and add explicit registry expiry hygiene.

## Verification

- `python -m pytest tests/test_cli_status.py tests/test_claude_hooks.py tests/test_installation_registry.py tests/test_cross_platform_support.py tests/test_hook_launcher.py tests/test_guard_action.py -q`: 375 passed, 3 skipped, 131 subtests.
- After the final Hook boundary adjustment, silence/destructive-operation tests passed 6 and cross-platform/launcher tests passed 45 with 3 skipped.
- Python compilation, Hook JSON validation, root/embedded equality, and `git diff --check` passed.

## Upgrade

```bash
tenetora upgrade --force
```

For a first installation or recovery:

```bash
curl -fsSL https://raw.githubusercontent.com/lcgyl/Tenetora/main/install.sh | bash -s -- --lang en
```

## Compatibility

Directories without `.tenetora` remain free of governance context injection and destructive-operation interception. Local-environment protection applies only to registered paths, and expired registry cleanup is explicit; no project files are deleted automatically.
