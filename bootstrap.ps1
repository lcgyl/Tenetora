#!/usr/bin/env powershell
# Tenetora one-line bootstrap for Windows: powershell -c "irm <this-url> | iex"
#
# Why this file exists:
# The full installer (install.ps1) has a param() block and a UTF-8 BOM (its
# Chinese text requires the BOM on Windows PowerShell 5.1). Both are fatal for
# string-mode execution: "irm <url> | iex" and scriptblock::Create parse the
# script from a string, where a leading U+FEFF breaks the param() block and
# there is no file path for PowerShell to strip the BOM from. This bootstrap is
# deliberately pure ASCII with no param() block, so it survives string-mode
# execution; it downloads the real installer to a file and runs it by path,
# where PowerShell 5.1 handles the BOM and the param() block natively.
#
# Language / channel rule:
#   TENETORA_LANG=en or -Lang en:          primary channel is GitHub, Gitee is fallback.
#   TENETORA_LANG=zh, -Lang zh, or unset:  primary channel is Gitee, GitHub is fallback.
# The environment variable is read so the documented one-liners can pin the
# language without extra arguments; an explicit -Lang argument wins. Both
# endpoints serve the identical canonical main-branch installer, so a
# fallback never changes language, version, or asset layout: the chosen
# -Lang is forwarded to the installer unchanged and the installer downloads
# its versioned release assets from the same platform as its own URL.
# Fallback only happens when the primary download throws (network error,
# HTTP error, timeout); an HTTP 200 response is always trusted.

$ErrorActionPreference = "Stop"

function Bootstrap-Fail([string]$Message) {
  Write-Error $Message
  exit 1
}

# Primary channel follows -Lang (argument wins) or TENETORA_LANG; see the
# rule block above. All arguments are forwarded to the installer verbatim
# after the channel choice.
$GiteeUrl = "https://gitee.com/lcgyl/tenetora/raw/main/install.ps1"
$GithubUrl = "https://raw.githubusercontent.com/lcgyl/Tenetora/main/install.ps1"
$BootstrapLang = $env:TENETORA_LANG
if ($args -contains "-Lang") {
  $BootstrapLang = $args[([array]::IndexOf($args, "-Lang") + 1)]
}
if ("$BootstrapLang".ToLower() -eq "en") {
  $BootstrapInstallerUrls = @($GithubUrl, $GiteeUrl)
} else {
  $BootstrapInstallerUrls = @($GiteeUrl, $GithubUrl)
}
if ($env:TENETORA_BOOTSTRAP_INSTALLER_URL) {
  # Test/diagnostic override: candidates (semicolon-separated) tried first,
  # before both canonical endpoints. @() keeps this an array even when the
  # pipeline yields a single element (PowerShell scalar trap).
  $BootstrapOverride = @(@($env:TENETORA_BOOTSTRAP_INSTALLER_URL -split ';') | Where-Object { $_ })
  $BootstrapInstallerUrls = @($BootstrapOverride) + @($BootstrapInstallerUrls)
}
if ($env:TENETORA_BOOTSTRAP_NO_FALLBACK) {
  # Test/diagnostic seam: keep ONLY the override candidates, never the
  # online endpoints. Production paths never set this, so the default still
  # tries Gitee then GitHub.
  $BootstrapInstallerUrls = $BootstrapOverride
}

$BootstrapDir = Join-Path ([IO.Path]::GetTempPath()) ("tenetora-install-" + [IO.Path]::GetRandomFileName())
New-Item -ItemType Directory -Path $BootstrapDir -Force | Out-Null
$BootstrapTarget = Join-Path $BootstrapDir "install.ps1"

# Every path after the temp dir exists must remove it: total download
# failure, installer throw, installer failure exit code, and success.
$BootstrapExit = 1
try {
  $BootstrapDownloaded = $false
  foreach ($BootstrapUrl in $BootstrapInstallerUrls) {
    try {
      Write-Host "Downloading Tenetora installer from $BootstrapUrl ..."
      # RawContentStream keeps the exact bytes; the file then carries exactly
      # one BOM exactly where the installer build put it.
      $response = Invoke-WebRequest -UseBasicParsing $BootstrapUrl -TimeoutSec 120
      [IO.File]::WriteAllBytes($BootstrapTarget, $response.RawContentStream.ToArray())
      $BootstrapDownloaded = $true
      break
    } catch {
      Write-Host "  download failed: $($_.Exception.Message)"
    }
  }
  if (-not $BootstrapDownloaded) {
    throw "Could not download the Tenetora installer from any known location."
  }

  Write-Host "Running the Tenetora installer ..."
  # install.ps1's early validation failures end in a terminating error
  # (throw), not exit; the catch below turns that into a failure exit so a
  # failed install is never reported as success.
  try {
    & $BootstrapTarget @args
    $BootstrapExit = $LASTEXITCODE
  } catch {
    Write-Host "installer failed: $($_.Exception.Message)"
    $BootstrapExit = 1
  }
  exit $BootstrapExit
} finally {
  if (Test-Path -LiteralPath $BootstrapDir) {
    Remove-Item -Recurse -Force $BootstrapDir -ErrorAction SilentlyContinue
  }
}
