# Tenetora 0.4.0

English | [简体中文](https://gitee.com/lcgyl/tenetora/blob/main/CHANGELOG.md)

## Highlights

- Add a dedicated planning skill for scope, dependencies, acceptance criteria, and a runnable path across participating components before implementation.
- Improve criterion-level delivery evidence, prompt-conflict diagnostics, audit-score explanations, and stable upgrade error codes.
- Add `tenetora verify run` to record actual exit codes, bounded redacted logs, and source fingerprints. Strict verification distinguishes declared results from observed receipts and checks the staged content.
- Make completion receipts durable while preserving invalidation after source changes, later failures, and history maintenance.
- Strengthen registered local-environment protection and failure handling for governed Git writes while retaining the existing boundaries for read-only operations and unmanaged workspaces.
- Improve maintenance plans and authenticated uninstall, preserving third-party configuration, user edits, original Git Hooks, and shared installation references. Cache inventory and cleanup previews remain read-only.
- Add the DeepSeek Harness Cordis bundle for governance context, pre-tool checks, and lifecycle skills. Its capability remains `active-partial`.
- Fix Windows CRLF checks, paths containing spaces, interpreter probes with restricted PATH, redirected HOME, uninstall locks, and localized diagnostics. Release-like entry names no longer suppress valid project governance.
- Use `https://r2.lcgylfamily.cn` for authenticated public product packages, with digest and disclosure-boundary checks and continued offline upgrade support.

## Install or upgrade

After the stable release is published, rerun the installer and select stable explicitly. It detects existing installations and upgrades them; preview users can use the same entry to return to stable:

```bash
curl -fsSL https://raw.githubusercontent.com/lcgyl/Tenetora/main/install.sh \
  | bash -s -- --channel stable --lang en
```

Windows PowerShell:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/lcgyl/Tenetora/main/bootstrap.ps1))) -Channel stable -Lang en
```

This explicitly selects stable instead of inheriting an older preview-channel setting. A CLI that already supports channel selection can also run:

```bash
tenetora upgrade --channel stable
tenetora doctor
```

Software upgrades do not automatically overwrite project rules. When an upgrade reports pending project governance, ask the project's AI conversation to use `tenetora-update` to update `.tenetora`, then complete the indicated review and audit.

## Compatibility and boundaries

Python 3.9 or later is required. Codex Hooks may need trust review after installation or definition changes. A missing host CLI is reported as skills-only fallback. OpenCode, Pi, and DSH retain their partial runtime coverage; installation alone does not prove full host acceptance.

Cache inventory does not delete files automatically. Uninstall preserves project governance and user data. Execution receipts are controlled-process observations, not cryptographic proof against a host with equivalent file-write access.
