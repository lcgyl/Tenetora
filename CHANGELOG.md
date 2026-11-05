# Tenetora 0.3.40.018-preview

English preview notes | [简体中文](https://gitee.com/lcgyl/tenetora/blob/main/CHANGELOG.md)

## About this preview

This preview updates release-package contents on the 0.3.40 stable line.

## Changes

- Release archives contain supported product files and public user documentation.
- Packages include the installer adapters and the feature-delivery, support and review guides referenced by the documentation.
- Distribution checks validate archive contents and release metadata. Failure messages identify the affected file without printing credential values.
- Downloads use `https://r2.lcgylfamily.cn`.

## Install or upgrade

Run the current installer and select the preview channel. It recognizes existing installations and upgrades them:

```bash
curl -fsSL https://raw.githubusercontent.com/lcgyl/Tenetora/main/install.sh \
  | bash -s -- --channel preview --lang en
```

An installed CLI that supports preview channels can also run:

```bash
tenetora upgrade --channel preview
```

## Compatibility

Python 3.9 or newer is required. Offline installation and upgrade remain supported. Host activation and trust requirements follow the selected platform.
