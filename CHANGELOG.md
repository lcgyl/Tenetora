# Tenetora 0.3.40.009-preview

English preview notes | [简体中文](https://gitee.com/lcgyl/tenetora/blob/main/CHANGELOG.md)

## About this preview

This is a **preview build** (`0.3.40.009-preview`, sequence 009) on top of the stable 0.3.40 line. Preview builds ship work-in-progress changes for early testing. They are not recommended for production use.

## Changes in this preview

- Release delivery reliability: artifact publishing now reports each delivery stage and its outcome, so an upload problem is distinguishable from a readback or validation problem.
- Immutable artifacts are detected and reused instead of being uploaded twice.
- Delivery remains fail-closed: validation failures stop the release before the channel pointer moves.

## Upgrade the preview

Installed preview users can run:

```bash
tenetora upgrade --channel preview
```

New users: install the preview build with `--channel preview`. The stable download entry keeps resolving to the stable release.
