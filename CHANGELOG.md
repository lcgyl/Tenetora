# Tenetora 0.3.40.017-preview

English preview notes | [简体中文](https://gitee.com/lcgyl/tenetora/blob/main/CHANGELOG.md)

## About this preview

This preview follows 0.3.40.016-preview on the 0.3.40 stable line and moves public release downloads to the dedicated R2 domain.

## Changes

- Installers and the upgrader now use `https://r2.lcgylfamily.cn` by default for both stable and preview channels.
- Explicit release-index and mirror settings remain supported. HTTPS verification, channel isolation and complete release-hash checks remain required.
- Published installation links use the dedicated domain. URL handling preserves valid quoted, plain and Markdown addresses and their formatting while keeping other hosts and credentials distinct.

## Upgrade

Existing clients that can reach their configured endpoint can run:

```bash
tenetora upgrade --channel preview
```

If an older client's test-domain endpoint cannot be reached, a POSIX shell can select the new domain for this command only:

```bash
TENETORA_RELEASE_INDEX_BASE=https://r2.lcgylfamily.cn tenetora upgrade --channel preview
```

After the upgrade, the installed client's default is the dedicated domain. Existing explicit overrides continue to take precedence.

## Compatibility

Python 3.9 or newer is required. This preview does not change project governance rules or grant new host permissions. Network failures remain possible and do not establish that a preview was retired.
