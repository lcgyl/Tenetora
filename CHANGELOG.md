# Tenetora 0.3.40.007-preview

English preview notes | [简体中文](https://gitee.com/lcgyl/tenetora/blob/main/CHANGELOG.md)

## About this preview

This preview is based on stable 0.3.40. It makes protected preview tags publish the single GitHub-built artifact to R2 automatically and keeps no-op upgrade output focused. It is not recommended for production use.

## Changes since the previous preview

- Publish a protected preview tag to R2 automatically after the gated package and verification jobs succeed; failures block the pipeline.
- Keep R2 as the only new artifact publisher while preserving historical public attachments and stable/preview pointer separation.
- Stop repeating semantic-review advice in a no-op terminal upgrade summary; structured status and `tenetora doctor` remain available for follow-up.

## Install or upgrade the preview

After the R2 preview publish job completes, the private installer can select this preview. Public-channel installation remains gated on a separate projection release.

```bash
curl -fsSL https://raw.githubusercontent.com/lcgyl/Tenetora/main/install.sh | bash -s -- --channel preview --lang en
```

The preview channel is opt-in and does not replace the stable channel. Windows PowerShell behavior requires native Windows validation before public-channel publication.
