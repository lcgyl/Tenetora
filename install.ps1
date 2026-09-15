param(
  [string]$ZipUrl = $env:TENETORA_ZIP_URL,
  [string]$ZipFile = $env:TENETORA_ZIP_FILE,
  [string]$Sha256 = $env:TENETORA_SHA256,
  [Alias("Home")][string]$TenetoraHome = $env:TENETORA_HOME,
  [string]$SourceDir = $env:TENETORA_SOURCE_DIR,
  [string]$Tools = $(if ($env:TENETORA_TOOLS) { $env:TENETORA_TOOLS } else { "auto" }),
  [ValidateSet("global", "project", "both")][string]$Scope = $(if ($env:TENETORA_SCOPE) { $env:TENETORA_SCOPE } else { "global" }),
  [string]$Path = $(if ($env:TENETORA_PATH) { $env:TENETORA_PATH } else { (Get-Location).Path }),
  [ValidateSet("en", "zh")][string]$Lang = $(if ($env:TENETORA_LANG) { $env:TENETORA_LANG } else { "en" }),
  [ValidateSet("auto", "native", "project", "off")][string]$CodexHooks = $(if ($env:TENETORA_CODEX_HOOKS) { $env:TENETORA_CODEX_HOOKS } else { "auto" }),
  [switch]$AllowTrackedCodexHooks,
  [switch]$PruneShadowed,
  [switch]$NoPruneShadowed,
  [ValidateSet("auto", "always", "never")][string]$Progress = $(if ($env:TENETORA_PROGRESS) { $env:TENETORA_PROGRESS } else { "auto" }),
  [switch]$NoProgress,
  [switch]$Update,
  [switch]$Force,
  [switch]$DryRun,
  [switch]$VerboseOutput,
  [switch]$AllowSkillsOnly,
  [switch]$RequireFull,
  [switch]$NoCli
)

$ErrorActionPreference = "Stop"
$MinimumPython = [version]"3.9"
$DefaultZipUrl = "https://github.com/lcgyl/Tenetora/releases/latest/download/tenetora-latest.zip"
$DefaultManifestUrl = "https://github.com/lcgyl/Tenetora/releases/latest/download/manifest.json"
$DefaultSourceZipUrl = "https://github.com/lcgyl/Tenetora/archive/refs/heads/main.zip"
$IsWindowsHost = ($env:OS -eq "Windows_NT") -or ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT)
$SourceWasExplicitDirectory = [bool]$SourceDir
$ZipUrlWasExplicit = [bool]$env:TENETORA_ZIP_URL -or $PSBoundParameters.ContainsKey("ZipUrl")
$ZipFileWasExplicit = [bool]$env:TENETORA_ZIP_FILE -or $PSBoundParameters.ContainsKey("ZipFile")
$Sha256WasExplicit = [bool]$env:TENETORA_SHA256 -or $PSBoundParameters.ContainsKey("Sha256")
$LanguageWasExplicit = $PSBoundParameters.ContainsKey("Lang")
$LanguageWasConfigured = [bool]$env:TENETORA_LANG
$EffectiveProgress = if ($NoProgress) { "never" } else { $Progress }
$script:EffectiveLanguage = $Lang

function Get-Text([string]$English, [string]$Chinese) {
  if ($script:EffectiveLanguage -eq "zh") { return $Chinese }
  return $English
}

function Test-InteractiveLanguagePrompt {
  try {
    if (-not [Environment]::UserInteractive -or [Console]::IsInputRedirected) { return $false }
    return $null -ne $Host -and $null -ne $Host.UI -and $null -ne $Host.UI.RawUI
  } catch {
    return $false
  }
}

function Select-Language([string]$ManagedHome) {
  if ($LanguageWasExplicit) {
    if ($script:EffectiveLanguage -notin @("en", "zh")) {
      throw (Get-Text "Language must be en or zh." "语言必须是 en 或 zh。")
    }
    $env:TENETORA_LANG = $script:EffectiveLanguage
    Write-Host (Get-Text "Language: English" "语言：中文")
    return
  }

  if ($LanguageWasConfigured) {
    $configuredLanguage = [string]$env:TENETORA_LANG
    if ($configuredLanguage -notin @("en", "zh")) {
      throw (Get-Text "TENETORA_LANG must be en or zh." "TENETORA_LANG 必须是 en 或 zh。")
    }
    $script:EffectiveLanguage = $configuredLanguage
    $env:TENETORA_LANG = $script:EffectiveLanguage
    Write-Host (Get-Text "Language: English" "语言：中文")
    return
  }

  $preferencePath = Join-Path $ManagedHome "state\preferences.json"
  if (Test-Path -LiteralPath $preferencePath -PathType Leaf) {
    try {
      $storedLanguage = ((Get-Content -LiteralPath $preferencePath -Raw) | ConvertFrom-Json).language
      if ($storedLanguage -in @("en", "zh")) {
        $script:EffectiveLanguage = $storedLanguage
        $env:TENETORA_LANG = $script:EffectiveLanguage
        Write-Host (Get-Text "Language: English" "语言：中文")
        return
      }
    } catch { }
  }

  if (Test-InteractiveLanguagePrompt) {
    Write-Host "Select language / 选择语言:"
    Write-Host "  1) English"
    Write-Host "  2) 中文"
    while ($true) {
      try {
        $choice = [string](Read-Host "Choice / 请选择 [1/2]")
        $choice = $choice.Trim()
      } catch {
        $script:EffectiveLanguage = "en"
        break
      }
      if ($choice -in @("1", "en", "EN", "English", "english")) {
        $script:EffectiveLanguage = "en"
        break
      }
      if ($choice -in @("2", "zh", "ZH", "中文")) {
        $script:EffectiveLanguage = "zh"
        break
      }
      Write-Host "Invalid choice, enter 1 or 2. / 选择无效，请输入 1 或 2。"
    }
    if ($script:EffectiveLanguage -in @("en", "zh")) {
      $env:TENETORA_LANG = $script:EffectiveLanguage
      Write-Host (Get-Text "Language: English" "语言：中文")
    }
    return
  }

  $script:EffectiveLanguage = "en"
  $env:TENETORA_LANG = $script:EffectiveLanguage
  Write-Host "Language: English (non-interactive default; use -Lang zh for Chinese)"
}

function Write-InstallerStage([string]$English, [string]$Chinese) {
  Write-Host (Get-Text $English $Chinese)
}

function Assert-PackageSourceSelection {
  if (($SourceWasExplicitDirectory -and ($ZipFileWasExplicit -or $ZipUrlWasExplicit -or $Sha256WasExplicit)) -or
      ($ZipFileWasExplicit -and $ZipUrlWasExplicit)) {
    throw (Get-Text "Package source options are mutually exclusive: -SourceDir cannot be combined with -ZipFile, an explicit -ZipUrl, or -Sha256; -ZipFile cannot be combined with an explicit -ZipUrl." "安装包来源选项互斥：-SourceDir 不能与 -ZipFile、显式 -ZipUrl 或 -Sha256 同时使用；-ZipFile 不能与显式 -ZipUrl 同时使用。")
  }
}

function Resolve-Python {
  $candidates = @()
  if ($env:TENETORA_PYTHON) { $candidates += ,@($env:TENETORA_PYTHON, "") }
  $candidates += @(
    @("py", "-3.13"), @("py", "-3.12"), @("py", "-3.11"), @("py", "-3.10"), @("py", "-3.9"),
    @("python", ""), @("python3", "")
  )
  foreach ($candidate in $candidates) {
    $command = Get-Command $candidate[0] -ErrorAction SilentlyContinue
    if (-not $command) { continue }
    $args = @()
    if ($candidate[1]) { $args += $candidate[1] }
    $versionText = & $command.Source @args -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null
    if (-not $versionText) { continue }
    try { $version = [version]$versionText.Trim() } catch { continue }
    if ($version -ge $MinimumPython) { return @($command.Source, $candidate[1], $versionText.Trim()) }
  }
  throw (Get-Text "Tenetora requires Python 3.9 or newer. Install Python or set TENETORA_PYTHON to an executable path." "Tenetora 需要 Python 3.9 或更高版本。请安装 Python，或将 TENETORA_PYTHON 指向可执行文件。")
}

function Invoke-Python([string]$Python, [string]$LauncherArg, [string[]]$Arguments) {
  if ($LauncherArg) { & $Python $LauncherArg @Arguments } else { & $Python @Arguments }
  if ($LASTEXITCODE -ne 0) { throw (Get-Text "Python command failed with exit code $LASTEXITCODE." "Python 命令执行失败，退出码：$LASTEXITCODE。") }
}

function Test-ZipSafety([string]$Python, [string]$LauncherArg, [string]$Archive, [bool]$AllowSourceArchive = $false) {
  $code = @'
import os, stat, sys, unicodedata, zipfile
from pathlib import PurePosixPath
path = sys.argv[1]
allow_source_archive = sys.argv[2] == "1"
if os.path.getsize(path) > 128 * 1024 * 1024:
    raise SystemExit("ZIP package exceeds the bounded archive size limit.")
with zipfile.ZipFile(path) as archive:
    all_infos = archive.infolist()
    infos = [item for item in all_infos if not item.is_dir()]
    if not infos:
        raise SystemExit("ZIP package is empty.")
    if len(all_infos) > 4096 or sum(int(item.file_size) for item in infos) > 256 * 1024 * 1024:
        raise SystemExit("ZIP package exceeds the bounded extraction limit.")
    seen = set()
    portable_seen = set()
    file_names = set()
    directory_names = set()
    roots = set()
    for item in all_infos:
        name = PurePosixPath(item.filename)
        if name.is_absolute() or "\x00" in item.filename or "\\" in item.filename or not name.parts or ".." in name.parts:
            raise SystemExit("ZIP package contains an unsafe path.")
        roots.add(name.parts[0])
        normalized = name.as_posix().rstrip("/")
        if normalized in seen:
            raise SystemExit("ZIP package contains duplicate entries.")
        seen.add(normalized)
        portable_name = unicodedata.normalize("NFC", normalized).casefold()
        if portable_name in portable_seen:
            raise SystemExit("ZIP package contains portable path collisions.")
        portable_seen.add(portable_name)
        (directory_names if item.is_dir() else file_names).add(normalized)
        if stat.S_ISLNK((item.external_attr >> 16) & 0o170000):
            raise SystemExit("ZIP package contains a symbolic link.")
    if file_names & directory_names or any(
        any(parent.as_posix() in file_names for parent in PurePosixPath(name).parents)
        for name in file_names
    ):
        raise SystemExit("ZIP package contains a file and directory path collision.")
    if len(roots) != 1 or (not allow_source_archive and roots != {"tenetora"}):
        raise SystemExit("ZIP package root is invalid.")
'@
  $scriptPath = Join-Path ([IO.Path]::GetTempPath()) ("tenetora-zip-safety-" + [guid]::NewGuid().ToString("N") + ".py")
  try {
    [IO.File]::WriteAllText($scriptPath, $code, (New-Object Text.UTF8Encoding($false)))
    Invoke-Python $Python $LauncherArg @($scriptPath, $Archive, $(if ($AllowSourceArchive) { "1" } else { "0" }))
  } finally {
    Remove-Item -LiteralPath $scriptPath -Force -ErrorAction SilentlyContinue
  }
}

function Test-ReleaseManifest([object]$External, [string]$EmbeddedPath, [string]$ActualSha256) {
  if (-not $External -or -not (Test-Path -LiteralPath $EmbeddedPath -PathType Leaf)) {
    throw (Get-Text "The release manifest is unavailable." "Release manifest 不可用。")
  }
  $embedded = Get-Content -LiteralPath $EmbeddedPath -Raw | ConvertFrom-Json
  $version = [string]$External.version
  $allowedArtifacts = @("tenetora-$version.zip", "tenetora-latest.zip")
  if ($External.name -ne "tenetora" -or $External.format -ne "tenetora-release-zip-v1" -or
      $External.zip_root -ne "tenetora" -or $version -notmatch '^\d+\.\d+\.\d+$' -or
      $allowedArtifacts -notcontains [string]$External.artifact -or
      ([string]$External.sha256).ToLowerInvariant() -ne $ActualSha256.ToLowerInvariant()) {
    throw (Get-Text "The external release manifest identity is invalid." "外部 release manifest 身份无效。")
  }
  $fields = @("name", "version", "commit", "format", "installer_protocol", "bridge_protocol",
    "minimum_bridge_version", "upgrade_entrypoint", "state_schemas", "zip_root", "files", "file_sha256")
  foreach ($field in $fields) {
    $externalValue = $External.$field | ConvertTo-Json -Depth 20 -Compress
    $embeddedValue = $embedded.$field | ConvertTo-Json -Depth 20 -Compress
    if ($externalValue -ne $embeddedValue) {
      throw (Get-Text "The external and embedded release manifests disagree." "外部与内嵌 release manifest 不一致。")
    }
  }
  $files = @($External.files)
  $hashProperties = @($External.file_sha256.PSObject.Properties)
  # Python release manifests use ordinal ordering; do not use culture-sensitive pipeline sorting here.
  $sortedFiles = [string[]]$files
  [Array]::Sort($sortedFiles, [StringComparer]::Ordinal)
  $uniqueFiles = @($files | Select-Object -Unique)
  if ($files.Count -eq 0 -or $files.Count -ne $uniqueFiles.Count -or
      (($files -join "`n") -ne ($sortedFiles -join "`n")) -or
      $hashProperties.Count -ne $files.Count) {
    throw (Get-Text "The external release manifest file inventory is invalid." "外部 release manifest 的文件清单无效。")
  }
  foreach ($file in $files) {
    if ($file -isnot [string] -or [string]::IsNullOrWhiteSpace($file) -or
        $file.StartsWith("/") -or $file.Contains("\") -or [IO.Path]::IsPathRooted($file) -or
        $file -match '^[A-Za-z]:' -or $file -match '(^|/)(\.|\.\.)(/|$)' -or $file -match '//') {
      throw (Get-Text "The external release manifest contains an unsafe file path." "外部 release manifest 包含不安全文件路径。")
    }
  }
  $hashNames = [string[]]@($hashProperties | ForEach-Object { $_.Name })
  [Array]::Sort($hashNames, [StringComparer]::Ordinal)
  $fileNames = [string[]]$files
  [Array]::Sort($fileNames, [StringComparer]::Ordinal)
  if (($hashNames -join "`n") -ne ($fileNames -join "`n")) {
    throw (Get-Text "The external release manifest file hashes do not match the file inventory." "外部 release manifest 的文件哈希与文件清单不一致。")
  }
  foreach ($property in $hashProperties) {
    if ($files -notcontains $property.Name -or [string]$property.Value -notmatch '^[0-9a-f]{64}$') {
      throw (Get-Text "The external release manifest file hashes are invalid." "外部 release manifest 的文件哈希无效。")
    }
  }
}

function Download-SourceFallback([string]$Archive, [string]$ReasonEnglish, [string]$ReasonChinese) {
  $warningEnglish = "$ReasonEnglish; falling back to $DefaultSourceZipUrl"
  $warningChinese = "$ReasonChinese；回退到 $DefaultSourceZipUrl"
  Write-Warning (Get-Text $warningEnglish $warningChinese)
  Remove-Item -LiteralPath $Archive -Force -ErrorAction SilentlyContinue
  try {
    Invoke-WebRequest -UseBasicParsing -Uri $DefaultSourceZipUrl -OutFile $Archive -ErrorAction Stop
  } catch {
    throw (Get-Text "Failed to download the source fallback package." "无法下载源码回退包。")
  }
  if (-not (Test-Path -LiteralPath $Archive -PathType Leaf)) {
    throw (Get-Text "The source fallback package was not downloaded." "源码回退包未成功下载。")
  }
  Write-Host (Get-Text "Package: fallback downloaded $DefaultSourceZipUrl" "安装包：已下载回退源码 $DefaultSourceZipUrl")
  return $true
}

function Get-PackageContractMissing([string]$PackageRoot) {
  $requiredPaths = @(
    "scripts\install-skill.py",
    "scripts\migrate_machine_home.py",
    "skills\tenetora\scripts\ensure_cli.py",
    "skills\tenetora\scripts\install_lock_holder.py",
    "skills\tenetora\cli\tenetora\install_lock.py"
  )
  foreach ($relative in $requiredPaths) {
    if (-not (Test-Path -LiteralPath (Join-Path $PackageRoot $relative) -PathType Leaf)) {
      Write-Output $relative
    }
  }
}

function Get-PackageInstallerProtocol([string]$PackageRoot) {
  $manifestPath = Join-Path $PackageRoot "manifest.json"
  if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
    try {
      $protocol = [int]((Get-Content -LiteralPath $manifestPath -Raw) | ConvertFrom-Json).installer_protocol
      return [Math]::Max($protocol, 1)
    } catch {
      return 1
    }
  }
  $installer = Join-Path $PackageRoot "scripts\install-skill.py"
  if ((Test-Path -LiteralPath (Join-Path $PackageRoot "scripts\migrate_machine_home.py") -PathType Leaf) -and
      (Test-Path -LiteralPath $installer -PathType Leaf)) {
    $installerText = Get-Content -LiteralPath $installer -Raw -ErrorAction SilentlyContinue
    if ($installerText -match "--all-existing" -and $installerText -match "--events-jsonl") { return 3 }
  }
  return 1
}

function Quote-ProcessArgument([string]$Value) {
  $escaped = $Value -replace '(\\*)"', '$1$1\"'
  $escaped = $escaped -replace '(\\+)$', '$1$1'
  return '"' + $escaped + '"'
}

$installLockProcess = $null
$installLockInput = $null
$installLockReady = $null
$installLockEnvironmentWasSet = $false
$installLockEnvironmentValue = $null
$installLockTokenEnvironmentWasSet = $false
$installLockTokenEnvironmentValue = $null
$installLockProofsEnvironmentWasSet = $false
$installLockProofsEnvironmentValue = $null
$installLockProofFile = $null
$installLockProofEndpoint = $null

function Start-InstallLock([string]$Python, [string]$LauncherArg, [string]$ManagedHome, [string]$Holder) {
  if ($DryRun) { return }
  if (-not (Test-Path -LiteralPath $Holder -PathType Leaf)) {
    throw (Get-Text "Tenetora package is missing the shared install lock holder: $Holder" "Tenetora 安装包缺少共享安装锁执行器：$Holder")
  }
  $script:installLockReady = Join-Path ([IO.Path]::GetTempPath()) ("tenetora-install-ready-" + [guid]::NewGuid().ToString("N"))
  $script:installLockProofFile = Join-Path ([IO.Path]::GetTempPath()) ("tenetora-install-proof-" + [guid]::NewGuid().ToString("N"))
  $arguments = @()
  if ($LauncherArg) { $arguments += $LauncherArg }
  $arguments += (Quote-ProcessArgument $Holder)
  $arguments += "--home"
  $arguments += (Quote-ProcessArgument $ManagedHome)
  $arguments += "--ready-file"
  $arguments += (Quote-ProcessArgument $script:installLockReady)
  $arguments += "--proof-file"
  $arguments += (Quote-ProcessArgument $script:installLockProofFile)
  $info = New-Object Diagnostics.ProcessStartInfo
  $info.FileName = $Python
  $info.Arguments = ($arguments -join " ")
  $info.UseShellExecute = $false
  $info.CreateNoWindow = $true
  $info.RedirectStandardInput = $true
  $script:installLockProcess = [Diagnostics.Process]::Start($info)
  $script:installLockInput = $script:installLockProcess.StandardInput
  while (-not (Test-Path -LiteralPath $script:installLockReady -PathType Leaf) -or
         -not (Test-Path -LiteralPath $script:installLockProofFile -PathType Leaf)) {
    if ($script:installLockProcess.HasExited) {
      throw (Get-Text "Tenetora machine install lock holder exited before acquiring the lock." "Tenetora 机器安装锁执行器在获得锁之前退出。")
    }
    Start-Sleep -Milliseconds 10
  }
  $ownerToken = (Get-Content -LiteralPath $script:installLockReady -Raw).Trim()
  if ($ownerToken -notmatch '^[0-9a-f]{64}$') {
    throw (Get-Text "Tenetora machine install lock holder returned an invalid transaction token." "Tenetora 机器安装锁执行器返回了无效事务令牌。")
  }
  $proofEndpoint = (Get-Content -LiteralPath $script:installLockProofFile -Raw).Trim()
  if ($proofEndpoint -notmatch '^\\\\\.\\pipe\\tenetora-install-[0-9a-f]{32}$') {
    throw (Get-Text "Tenetora machine install lock holder returned an invalid proof endpoint." "Tenetora 机器安装锁执行器返回了无效锁证明端点。")
  }
  $script:installLockProofEndpoint = $proofEndpoint
  if (Test-Path Env:TENETORA_OUTER_INSTALL_LOCK_HELD) {
    $script:installLockEnvironmentWasSet = $true
    $script:installLockEnvironmentValue = $env:TENETORA_OUTER_INSTALL_LOCK_HELD
  }
  if (Test-Path Env:TENETORA_OUTER_INSTALL_LOCK_TOKEN) {
    $script:installLockTokenEnvironmentWasSet = $true
    $script:installLockTokenEnvironmentValue = $env:TENETORA_OUTER_INSTALL_LOCK_TOKEN
  }
  if (Test-Path Env:TENETORA_OUTER_INSTALL_LOCK_PROOFS) {
    $script:installLockProofsEnvironmentWasSet = $true
    $script:installLockProofsEnvironmentValue = $env:TENETORA_OUTER_INSTALL_LOCK_PROOFS
  }
  $env:TENETORA_OUTER_INSTALL_LOCK_HELD = "1"
  $env:TENETORA_OUTER_INSTALL_LOCK_TOKEN = $ownerToken
  $proofMap = @{}
  $proofMap[$ManagedHome] = $script:installLockProofEndpoint
  $env:TENETORA_OUTER_INSTALL_LOCK_PROOFS = $proofMap | ConvertTo-Json -Compress
}

function Stop-InstallLock {
  if ($script:installLockInput) {
    $script:installLockInput.Close()
    $script:installLockInput = $null
  }
  if ($script:installLockProcess) {
    $script:installLockProcess.WaitForExit(5000)
    if (-not $script:installLockProcess.HasExited) { $script:installLockProcess.Kill() }
    $script:installLockProcess.Dispose()
    $script:installLockProcess = $null
  }
  if ($script:installLockReady -and (Test-Path -LiteralPath $script:installLockReady)) {
    Remove-Item -LiteralPath $script:installLockReady -Force -ErrorAction SilentlyContinue
  }
  if ($script:installLockProofFile -and (Test-Path -LiteralPath $script:installLockProofFile)) {
    Remove-Item -LiteralPath $script:installLockProofFile -Force -ErrorAction SilentlyContinue
  }
  if ($script:installLockEnvironmentWasSet) {
    $env:TENETORA_OUTER_INSTALL_LOCK_HELD = $script:installLockEnvironmentValue
  } else {
    Remove-Item Env:TENETORA_OUTER_INSTALL_LOCK_HELD -ErrorAction SilentlyContinue
  }
  if ($script:installLockTokenEnvironmentWasSet) {
    $env:TENETORA_OUTER_INSTALL_LOCK_TOKEN = $script:installLockTokenEnvironmentValue
  } else {
    Remove-Item Env:TENETORA_OUTER_INSTALL_LOCK_TOKEN -ErrorAction SilentlyContinue
  }
  if ($script:installLockProofsEnvironmentWasSet) {
    $env:TENETORA_OUTER_INSTALL_LOCK_PROOFS = $script:installLockProofsEnvironmentValue
  } else {
    Remove-Item Env:TENETORA_OUTER_INSTALL_LOCK_PROOFS -ErrorAction SilentlyContinue
  }
}

function Remove-Pointer([string]$Target) {
  $item = Get-Item -LiteralPath $Target -Force -ErrorAction SilentlyContinue
  if (-not $item) { return }
  if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    Remove-Item -LiteralPath $Target -Force
  } else {
    throw (Get-Text "Refusing to replace a non-managed directory or file: $Target" "拒绝替换非 Tenetora 托管的目录或文件：$Target")
  }
}

function Remove-Backup([string]$Target) {
  $item = Get-Item -LiteralPath $Target -Force -ErrorAction SilentlyContinue
  if (-not $item) { return }
  if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or -not $item.PSIsContainer) {
    Remove-Item -LiteralPath $Target -Force
  } else {
    Remove-Item -LiteralPath $Target -Recurse -Force
  }
}

function Set-Junction([string]$Target, [string]$Destination) {
  Remove-Pointer $Target
  New-Item -ItemType Junction -Path $Target -Target $Destination | Out-Null
}

function Test-ManagedPackageDirectory([string]$Target) {
  $item = Get-Item -LiteralPath $Target -Force -ErrorAction SilentlyContinue
  if (-not $item -or -not $item.PSIsContainer) { return $false }
  if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) { return $false }
  try { $resolved = (Resolve-Path -LiteralPath $Target -ErrorAction Stop).Path } catch { return $false }
  try { $resolved = [IO.Path]::GetFullPath($resolved).TrimEnd('\', '/') } catch { return $false }
  $knownRoots = @(
    [IO.Path]::GetFullPath((Join-Path $TenetoraHome "releases")).TrimEnd('\', '/'),
    [IO.Path]::GetFullPath((Join-Path $profileHome ".agent-harness\releases")).TrimEnd('\', '/')
  )
  $underKnownRoot = $false
  foreach ($knownRoot in $knownRoots) {
    if ($resolved.Equals($knownRoot, [StringComparison]::OrdinalIgnoreCase) -or
        $resolved.StartsWith($knownRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or
        $resolved.StartsWith($knownRoot + [IO.Path]::AltDirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
      $underKnownRoot = $true
      break
    }
  }
  if (-not $underKnownRoot) { return $false }
  $canonical = Test-Path -LiteralPath (Join-Path $Target "skills\tenetora\SKILL.md") -PathType Leaf
  $legacy = Test-Path -LiteralPath (Join-Path $Target "skills\agent-harness\SKILL.md") -PathType Leaf
  $canonicalVersion = Test-Path -LiteralPath (Join-Path $Target "skills\tenetora\VERSION") -PathType Leaf
  $legacyVersion = Test-Path -LiteralPath (Join-Path $Target "skills\agent-harness\VERSION") -PathType Leaf
  return $canonical -or $legacy -or $canonicalVersion -or $legacyVersion
}

function Get-SafeSourceDirectory([string]$Path) {
  $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
  if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw (Get-Text "The explicit source directory must not be a symbolic link or junction: $Path" "显式源目录不能是符号链接或 junction：$Path")
  }
  if (-not $item.PSIsContainer) {
    throw (Get-Text "The explicit source path must be a directory: $Path" "显式源路径必须是目录：$Path")
  }
  $pending = New-Object 'System.Collections.Generic.Queue[string]'
  $pending.Enqueue($item.FullName)
  while ($pending.Count -gt 0) {
    $current = $pending.Dequeue()
    foreach ($entry in Get-ChildItem -LiteralPath $current -Force -ErrorAction Stop) {
      if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw (Get-Text "The explicit source directory contains a symbolic link or junction: $($entry.FullName)" "显式源码目录包含符号链接或 junction：$($entry.FullName)")
      }
      if ($entry.PSIsContainer) {
        $pending.Enqueue($entry.FullName)
      } elseif (-not [IO.File]::Exists($entry.FullName)) {
        throw (Get-Text "The explicit source directory contains a non-regular file: $($entry.FullName)" "显式源码目录包含非普通文件：$($entry.FullName)")
      }
    }
  }
  return $item.FullName
}

function Get-SafeManagedHome([string]$Path) {
  if ([string]::IsNullOrWhiteSpace($Path)) {
    throw (Get-Text "Tenetora home cannot be empty." "Tenetora 主目录不能为空。")
  }
  if ($Path -match '(^|[\\/])\.\.([\\/]|$)') {
    throw (Get-Text "Tenetora home contains parent traversal: $Path" "Tenetora 主目录包含父级穿越：$Path")
  }
  try {
    $full = [IO.Path]::GetFullPath($Path)
  } catch {
    throw (Get-Text "Tenetora home path is invalid: $Path" "Tenetora 主目录路径无效：$Path")
  }
  if (-not $full -or $full -eq [IO.Path]::GetPathRoot($full)) {
    throw (Get-Text "Tenetora home cannot be the filesystem root: $full" "Tenetora 主目录不能是文件系统根目录：$full")
  }

  $chain = New-Object 'System.Collections.Generic.List[string]'
  $current = $full
  while ($current) {
    $chain.Add($current)
    $parent = [IO.Directory]::GetParent($current)
    if ($null -eq $parent -or $parent.FullName -eq $current) { break }
    $current = $parent.FullName
  }
  for ($index = $chain.Count - 1; $index -ge 0; $index--) {
    $item = Get-Item -LiteralPath $chain[$index] -Force -ErrorAction SilentlyContinue
    if (-not $item) { continue }
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
      throw (Get-Text "Tenetora home contains a symbolic link or junction: $($item.FullName)" "Tenetora 主目录包含符号链接或 junction：$($item.FullName)")
    }
    if (-not $item.PSIsContainer) {
      throw (Get-Text "Tenetora home component is not a directory: $($item.FullName)" "Tenetora 主目录组件不是目录：$($item.FullName)")
    }
  }
  return $full
}

function Write-LanguagePreference([string]$ManagedHome, [string]$Language) {
  if ($DryRun) { return }
  try {
    $preferencesDir = Join-Path $ManagedHome "state"
    $preferencesPath = Join-Path $preferencesDir "preferences.json"
    $temporaryPath = Join-Path $preferencesDir (".preferences." + [guid]::NewGuid().ToString("N") + ".tmp")
    New-Item -ItemType Directory -Force -Path $preferencesDir | Out-Null
    $payload = @{ version = 1; language = $Language } | ConvertTo-Json -Compress
    [IO.File]::WriteAllText($temporaryPath, $payload + "`n", (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temporaryPath -Destination $preferencesPath -Force
  } catch { }
}

function Read-PackageRoot([string]$ExtractRoot) {
  $candidates = @(
    (Join-Path $ExtractRoot "tenetora"),
    (Get-ChildItem -LiteralPath $ExtractRoot -Directory -ErrorAction SilentlyContinue | Select-Object -First 1).FullName
  )
  foreach ($candidate in $candidates) {
    if ($candidate -and (Test-Path (Join-Path $candidate "skills\tenetora\SKILL.md"))) { return $candidate }
  }
  throw (Get-Text "ZIP package does not contain a Tenetora lifecycle router skill." "ZIP 安装包不包含 Tenetora lifecycle router skill。")
}

function Test-ProvenLegacyMachineHome([string]$LegacyHome) {
  if (-not (Test-Path -LiteralPath $LegacyHome -PathType Container)) { return $false }
  $fingerprints = 0
  $packageRoots = @(
    (Join-Path $LegacyHome "current"),
    (Join-Path $LegacyHome "source\agent-harness")
  )
  if ($packageRoots | Where-Object {
      (Test-Path -LiteralPath (Join-Path $_ "skills\agent-harness\SKILL.md") -PathType Leaf) -or
      (Test-Path -LiteralPath (Join-Path $_ "skills\tenetora\SKILL.md") -PathType Leaf)
    }) { $fingerprints++ }
  if ((Test-Path -LiteralPath (Join-Path $LegacyHome "runtime\VERSION") -PathType Leaf) -and
      (Test-Path -LiteralPath (Join-Path $LegacyHome "runtime\hooks\agent_harness_hook.py") -PathType Leaf) -and
      (Test-Path -LiteralPath (Join-Path $LegacyHome "runtime\cli\agent_harness\cli.py") -PathType Leaf)) {
    $fingerprints++
  }
  $legacyShim = Join-Path $LegacyHome "bin\agent-harness.cmd"
  if (Test-Path -LiteralPath $legacyShim -PathType Leaf) {
    $shimText = Get-Content -LiteralPath $legacyShim -Raw -ErrorAction SilentlyContinue
    if ($shimText -match "agent_harness\.cli" -and $shimText -match "AGENT_HARNESS_(HOME|INVOKED_AS)") {
      $fingerprints++
    }
  }
  $registry = Join-Path $LegacyHome "state\installations.json"
  if (Test-Path -LiteralPath $registry -PathType Leaf) {
    try {
      $registryPayload = Get-Content -LiteralPath $registry -Raw | ConvertFrom-Json
      if ($registryPayload.version -eq 1 -and $null -ne $registryPayload.projects) { $fingerprints++ }
    } catch { }
  }
  return $fingerprints -ge 2
}

$profileHome = [Environment]::GetFolderPath("UserProfile")
if (-not $TenetoraHome) { $TenetoraHome = Join-Path $profileHome ".tenetora" }
$TenetoraHome = Get-SafeManagedHome $TenetoraHome
$TenetoraHome = [IO.Path]::GetFullPath($TenetoraHome)
Select-Language $TenetoraHome
$pythonInfo = Resolve-Python
$python = $pythonInfo[0]
$pythonArg = $pythonInfo[1]
$pythonVersion = $pythonInfo[2]
if (-not $ZipUrl) { $ZipUrl = $DefaultZipUrl }
Assert-PackageSourceSelection
$temporary = $null
$releaseDir = $null
$stageDir = $null
$previousCurrent = $null
$previousSource = $null
$previousRelease = $null
$previousCurrentWasPointer = $false
$previousSourceWasPointer = $false
$releaseInstalled = $false
$currentPointerInstalled = $false
$sourcePointerInstalled = $false
$preserveSourcePointer = $false
$releaseManifest = $null
$fallbackUsed = $false
$hookRollbackJournal = Join-Path ([IO.Path]::GetTempPath()) ("tenetora-hook-rollback-" + [guid]::NewGuid().ToString("N"))
$hookRollbackReady = $false
$runtimeRollbackJournal = Join-Path ([IO.Path]::GetTempPath()) ("tenetora-runtime-rollback-" + [guid]::NewGuid().ToString("N"))
$runtimeRollbackReady = $false
$hookRollbackFailed = $false
$runtimeRollbackFailed = $false
$failedTransactionBackup = $null
$scopeExplicit = $PSBoundParameters.ContainsKey("Scope") -or [bool]$env:TENETORA_SCOPE
$existingInstall = (Test-Path -LiteralPath (Join-Path $TenetoraHome "current")) -or
  (Test-Path -LiteralPath (Join-Path $TenetoraHome "source\tenetora")) -or
  (Test-Path -LiteralPath (Join-Path $TenetoraHome "state\installations.json")) -or
  (Test-ProvenLegacyMachineHome (Join-Path $profileHome ".agent-harness"))
$effectiveUpdate = $Update -or $existingInstall

try {
  Write-InstallerStage "Tenetora online installer" "Tenetora 在线安装器"
  Write-InstallerStage "Runtime: Python $pythonVersion ($python)" "运行时：Python $pythonVersion（$python）"
  Write-Host ""
  if ($SourceDir) {
    Write-InstallerStage "[1/6] Resolving package from local source directory..." "[1/6] 正在从本地源码目录解析安装包..."
  } else {
    Write-InstallerStage "[1/6] Resolving package from zip..." "[1/6] 正在从 zip 解析安装包..."
  }
  if ($SourceDir) {
    $sourceRoot = Get-SafeSourceDirectory $SourceDir
  } else {
    if (-not $ZipFile) {
      $temporary = Join-Path ([IO.Path]::GetTempPath()) ("tenetora-" + [guid]::NewGuid().ToString("N"))
      New-Item -ItemType Directory -Force -Path $temporary | Out-Null
      $ZipFile = Join-Path $temporary "tenetora.zip"
      Write-Host (Get-Text "Downloading Tenetora package..." "正在下载 Tenetora 安装包……")
      $defaultLatest = ($ZipUrl -eq $DefaultZipUrl -and -not $ZipUrlWasExplicit -and -not $Sha256WasExplicit)
      if (-not $Sha256) {
        if (-not $defaultLatest) {
          throw (Get-Text "An explicit ZIP URL requires -Sha256." "显式 ZIP URL 必须同时提供 -Sha256。")
        }
        try {
          $releaseManifest = Invoke-RestMethod -UseBasicParsing -Uri $DefaultManifestUrl -ErrorAction Stop
        } catch {
          $fallbackUsed = Download-SourceFallback $ZipFile `
            "The latest release manifest was unavailable" `
            "latest release manifest 不可用"
        }
        if (-not $fallbackUsed) {
          $Sha256 = [string]$releaseManifest.sha256
          if ($Sha256 -notmatch '^[0-9a-fA-F]{64}$') { throw (Get-Text "Release manifest has no valid SHA256." "Release manifest 没有有效 SHA256。") }
        }
      }
      if (-not $fallbackUsed) {
        try {
          Invoke-WebRequest -UseBasicParsing -Uri $ZipUrl -OutFile $ZipFile -ErrorAction Stop
        } catch {
          if (-not $defaultLatest) { throw }
          $fallbackUsed = Download-SourceFallback $ZipFile `
            "The latest release ZIP was unavailable" `
            "latest release ZIP 不可用"
        }
      }
      if ($fallbackUsed) {
        $releaseManifest = $null
        $Sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $ZipFile).Hash.ToLowerInvariant()
      }
    }
    if (-not (Test-Path -LiteralPath $ZipFile -PathType Leaf)) { throw (Get-Text "ZIP file not found: $ZipFile" "找不到 ZIP 文件：$ZipFile") }
    if (-not $Sha256) {
      $sidecar = $ZipFile + ".sha256"
      if (-not (Test-Path -LiteralPath $sidecar -PathType Leaf)) {
        throw (Get-Text "An offline ZIP requires -Sha256 or a sibling .sha256 file." "离线 ZIP 必须提供 -Sha256 或同名 .sha256 文件。")
      }
      $Sha256 = ((Get-Content -LiteralPath $sidecar -Raw).Trim() -split '\s+')[0]
    }
    if ($Sha256) {
      $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $ZipFile).Hash.ToLowerInvariant()
      if ($actual -ne $Sha256.ToLowerInvariant()) { throw (Get-Text "SHA256 mismatch for the Tenetora package." "Tenetora 安装包的 SHA256 不匹配。") }
    }
    Test-ZipSafety $python $pythonArg $ZipFile ([bool]$fallbackUsed)
    if (-not $temporary) { $temporary = Join-Path ([IO.Path]::GetTempPath()) ("tenetora-" + [guid]::NewGuid().ToString("N")) }
    $extract = Join-Path $temporary "extract"
    Expand-Archive -LiteralPath $ZipFile -DestinationPath $extract -Force
    $sourceRoot = Read-PackageRoot $extract
    $sourceRoot = Get-SafeSourceDirectory $sourceRoot
    if ($releaseManifest) {
      Test-ReleaseManifest $releaseManifest (Join-Path $sourceRoot "manifest.json") $actual
    }
    $missingPackagePaths = @(Get-PackageContractMissing $sourceRoot)
    $packageProtocol = Get-PackageInstallerProtocol $sourceRoot
    if (($missingPackagePaths.Count -gt 0 -or $packageProtocol -lt 3) -and $defaultLatest -and -not $fallbackUsed) {
      if ($missingPackagePaths.Count -gt 0) {
        $fallbackUsed = Download-SourceFallback $ZipFile `
          "The latest release package is incomplete" `
          "latest release 包不完整"
      } else {
        $fallbackUsed = Download-SourceFallback $ZipFile `
          "The latest release installer protocol is older than required" `
          "latest release 的安装协议低于所需版本"
      }
      $releaseManifest = $null
      $Sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $ZipFile).Hash.ToLowerInvariant()
      $fallbackExtractName = if ($missingPackagePaths.Count -gt 0) { "contract-fallback" } else { "protocol-fallback" }
      $extract = Join-Path $temporary $fallbackExtractName
      Test-ZipSafety $python $pythonArg $ZipFile $true
      Expand-Archive -LiteralPath $ZipFile -DestinationPath $extract -Force
      $sourceRoot = Read-PackageRoot $extract
      $sourceRoot = Get-SafeSourceDirectory $sourceRoot
      $missingPackagePaths = @(Get-PackageContractMissing $sourceRoot)
      $packageProtocol = Get-PackageInstallerProtocol $sourceRoot
    }
    if ($missingPackagePaths.Count -gt 0) {
      $missingEnglish = "Package is missing required installer files:`n" + ($missingPackagePaths -join "`n")
      $missingChinese = "安装包缺少必需的安装文件：`n" + ($missingPackagePaths -join "`n")
      throw (Get-Text $missingEnglish $missingChinese)
    }
    if ($packageProtocol -lt 3) {
      throw (Get-Text "Package installer protocol is incompatible with this installer." "安装包协议与当前安装器不兼容。")
    }
  }

  $version = (Get-Content -LiteralPath (Join-Path $sourceRoot "skills\tenetora\VERSION") -Raw).Trim()
  if ($version -notmatch '^\d+\.\d+\.\d+$') { throw (Get-Text "Tenetora package version is invalid." "Tenetora 安装包版本无效。") }
  if ($effectiveUpdate) {
    Write-InstallerStage "Operation: upgrade existing installations" "操作：升级现有安装"
  } else {
    Write-InstallerStage "Operation: install" "操作：安装"
  }
  Write-InstallerStage "Version: $version" "版本：$version"
  $env:TENETORA_HOME = $TenetoraHome
  $env:TENETORA_LANG = $script:EffectiveLanguage
  $env:TENETORA_DEFER_MACHINE_HOME_MIGRATION = "1"
  $env:PYTHONDONTWRITEBYTECODE = "1"
  $install = Join-Path $sourceRoot "scripts\install-skill.py"
  $runtimeTransaction = Join-Path $sourceRoot "skills\tenetora\scripts\runtime_transaction.py"
  $bridgeProtocol = 0
  $packageManifestPath = Join-Path $sourceRoot "manifest.json"
  if (Test-Path -LiteralPath $packageManifestPath -PathType Leaf) {
    try { $bridgeProtocol = [int](((Get-Content -LiteralPath $packageManifestPath -Raw) | ConvertFrom-Json).bridge_protocol) } catch { $bridgeProtocol = 0 }
  }
  # The bridge worker delegates the mutating phase to a nested process that
  # requires proof of this outer transaction lock. Acquire it before either
  # bridge or legacy execution so both paths share the same serialization.
  Start-InstallLock $python $pythonArg $TenetoraHome (Join-Path $sourceRoot "skills\tenetora\scripts\install_lock_holder.py")
  if ($bridgeProtocol -ge 1) {
    $worker = Join-Path $sourceRoot "skills\tenetora\scripts\upgrade_skill.py"
    if (-not (Test-Path -LiteralPath $worker -PathType Leaf)) { throw (Get-Text "Bridge-capable package is missing its upgrade worker." "支持 bridge 的安装包缺少升级 worker。") }
    $sourceVersion = "0.0.0"
    $currentVersion = Join-Path $TenetoraHome "current\skills\tenetora\VERSION"
    if (Test-Path -LiteralPath $currentVersion -PathType Leaf) { $sourceVersion = (Get-Content -LiteralPath $currentVersion -Raw).Trim() }
    $bridgeArgs = @($worker, "--bootstrap-bridge", "--bridge-source-version", $sourceVersion,
      "--path", $Path, "--tools", $Tools, "--mode", "copy", "--codex-hooks", $CodexHooks,
      "--progress", $EffectiveProgress, "--configure-path")
    $logDirectory = Join-Path $TenetoraHome "logs"
    New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
    $logStamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss") + "-" + $PID
    $logFile = Join-Path $logDirectory ("install-" + $logStamp + ".log")
    $eventFile = Join-Path $logDirectory ("install-" + $logStamp + ".jsonl")
    $bridgeArgs += @("--log-file", $logFile, "--events-jsonl", $eventFile)
    if (-not $effectiveUpdate) { $bridgeArgs += "--bootstrap-install" }
    if ($SourceWasExplicitDirectory) {
      $bridgeArgs += @("--source-dir", $sourceRoot)
    } else {
      $bridgeArgs += @("--zip-file", $ZipFile, "--sha256", $Sha256)
    }
    if ($scopeExplicit) { $bridgeArgs += @("--scope", $Scope) }
    if ($Force) { $bridgeArgs += "--force" }
    if ($VerboseOutput) { $bridgeArgs += "--verbose" }
    if ($AllowSkillsOnly) { $bridgeArgs += "--allow-skills-only" }
    if ($RequireFull) { $bridgeArgs += "--require-full" }
    if ($AllowTrackedCodexHooks) { $bridgeArgs += "--allow-tracked-codex-hooks" }
    if ($PruneShadowed) { $bridgeArgs += "--prune-shadowed" }
    if ($NoPruneShadowed) { $bridgeArgs += "--no-prune-shadowed" }
    if ($NoCli) { $bridgeArgs += "--no-cli" }
    if ($DryRun) { $bridgeArgs += "--dry-run" }
    if ($pythonArg) { & $python $pythonArg @bridgeArgs } else { & $python @bridgeArgs }
    $bridgeStatus = $LASTEXITCODE
    $capabilityStatus = ""
    if (Test-Path -LiteralPath $eventFile -PathType Leaf) {
      foreach ($line in Get-Content -LiteralPath $eventFile) {
        try {
          $event = $line | ConvertFrom-Json
          if ($event.event -eq "complete") { $capabilityStatus = [string]$event.status }
        } catch { }
      }
    }
    if ($bridgeStatus -eq 0) {
      Write-LanguagePreference $TenetoraHome $script:EffectiveLanguage
      Write-InstallerStage "[4/6] Bootstrapping CLI..." "[4/6] 正在自举 CLI..."
      if ($NoCli) {
        Write-InstallerStage "CLI: skipped" "CLI：已跳过"
      } else {
        Write-InstallerStage "CLI: ready" "CLI：已就绪"
      }
      Write-InstallerStage "[5/6] Summarizing result..." "[5/6] 正在汇总结果..."
      if ($capabilityStatus -eq "PENDING_TRUST") {
        if ($effectiveUpdate) {
          Write-InstallerStage "[6/6] Upgrade succeeded; trust or restart required" "[6/6] 升级成功；需完成信任或重启"
        } else {
          Write-InstallerStage "[6/6] Installation succeeded; trust or restart required" "[6/6] 安装成功；需完成信任或重启"
        }
        Write-InstallerStage "Final result: success with follow-up" "最终结果：成功，等待激活"
        Write-InstallerStage "Complete the trust or restart action shown above; no reinstall is needed." "请完成上方的信任或重启操作，无需重新安装。"
      } else {
        if ($effectiveUpdate) {
          Write-InstallerStage "[6/6] Upgrade succeeded" "[6/6] 升级成功"
        } else {
          Write-InstallerStage "[6/6] Installation succeeded" "[6/6] 安装成功"
        }
        Write-InstallerStage "Final result: success" "最终结果：成功"
      }
      Write-InstallerStage "Next: tenetora doctor" "下一步：tenetora doctor"
      Write-InstallerStage "Tenetora operation completed. Future upgrades use: tenetora upgrade" "Tenetora 操作完成。后续升级请使用：tenetora upgrade"
      return
    }
    if ($bridgeStatus -eq 2) {
      Write-Host (Get-Text "Rollback boundary: CLI/runtime/source/Git hooks were restored; lifecycle skill or host plugin surfaces completed before the failure may already use the new version. Rerun tenetora upgrade to converge them." "回滚边界：CLI/runtime/source/Git hooks 已恢复；失败前完成的 lifecycle skill 或宿主插件安装面可能已是新版本。请重新运行 tenetora upgrade 完成收敛。")
      if ($effectiveUpdate) {
        Write-InstallerStage "[6/6] Upgrade partially failed" "[6/6] 升级部分失败"
      } else {
        Write-InstallerStage "[6/6] Installation partially failed" "[6/6] 安装部分失败"
      }
      Write-InstallerStage "Final result: partial failure" "最终结果：部分失败"
      Write-InstallerStage "Review the failure details above before retrying." "重试前请检查上方失败详情。"
      Write-InstallerStage "Tenetora operation partially failed." "Tenetora 操作部分失败。"
      Write-Host (Get-Text "Detailed log: $logFile" "详细日志：$logFile")
      Write-Host (Get-Text "Structured events: $eventFile" "结构化事件：$eventFile")
      exit 2
    }
    if ($bridgeStatus -eq 3) {
      if ($effectiveUpdate) {
        Write-InstallerStage "[6/6] Upgrade failed during preflight" "[6/6] 升级在预检阶段失败"
      } else {
        Write-InstallerStage "[6/6] Installation failed during preflight" "[6/6] 安装在预检阶段失败"
      }
      Write-InstallerStage "Final result: failed; no installation changes were applied" "最终结果：失败；未写入安装变更"
      Write-Host (Get-Text "Detailed log: $logFile" "详细日志：$logFile")
      Write-Host (Get-Text "Structured events: $eventFile" "结构化事件：$eventFile")
      exit 1
    }
    if ($effectiveUpdate) {
      Write-InstallerStage "[6/6] Upgrade failed" "[6/6] 升级失败"
    } else {
      Write-InstallerStage "[6/6] Installation failed" "[6/6] 安装失败"
    }
    Write-InstallerStage "Final result: failed" "最终结果：失败"
    Write-InstallerStage "Review the failure details above before retrying." "重试前请检查上方失败详情。"
    Write-Host (Get-Text "Detailed log: $logFile" "详细日志：$logFile")
    Write-Host (Get-Text "Structured events: $eventFile" "结构化事件：$eventFile")
    exit $bridgeStatus
  }
  if ($effectiveUpdate -and -not $scopeExplicit) {
    $discoveryArgs = @($install, "--discover-existing", "--auto-discover", "--all-existing", "--tools", $Tools, "--path", $Path, "--json")
    if ($DryRun) { $discoveryArgs += "--dry-run" }
    Invoke-Python $python $pythonArg $discoveryArgs | Out-Null
  }
  if (-not $DryRun) {
    # Snapshot before moving current/source. The runtime journal owns those
    # managed pointers as well as the CLI/runtime entries and restores them by
    # CAS, so a later failure cannot leave a fresh install with dangling
    # pointers or overwrite a concurrent edit.
    Invoke-Python $python $pythonArg @($runtimeTransaction, "--snapshot", "--bin-dir", (Join-Path $TenetoraHome "bin"),
      "--journal", $runtimeRollbackJournal, "--json")
    $runtimeRollbackReady = $true
  }
  if (-not $DryRun -and $effectiveUpdate) {
    $snapshotArgs = @($install, "--snapshot-hooks", "--path", $Path, "--package-root", $sourceRoot,
      "--hook-rollback-journal", $hookRollbackJournal, "--json")
    if ($effectiveUpdate) { $snapshotArgs += "--all-existing" }
    Invoke-Python $python $pythonArg $snapshotArgs
    $hookRollbackReady = $true
  }
  $releaseParent = Join-Path $TenetoraHome "releases"
  if (-not $DryRun) {
    $null = Get-SafeManagedHome $releaseParent
    New-Item -ItemType Directory -Force -Path $releaseParent | Out-Null
    $null = Get-SafeManagedHome $releaseParent
    $releaseDir = Join-Path $releaseParent $version
    $stageDir = Join-Path $releaseParent ("." + $version + "." + [guid]::NewGuid().ToString("N") + ".tmp")
    New-Item -ItemType Directory -Force -Path $stageDir | Out-Null
    Get-ChildItem -LiteralPath $sourceRoot -Force | Copy-Item -Destination $stageDir -Recurse -Force

    $current = Join-Path $TenetoraHome "current"
    $sourcePointer = Join-Path $TenetoraHome "source\tenetora"
    $currentItem = Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue
    if ($currentItem) {
      $previousCurrentWasPointer = ($currentItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
      if (-not (Test-ManagedPackageDirectory $current)) {
        throw (Get-Text "Refusing to replace current because Tenetora ownership cannot be proven: $current" "current 的 Tenetora 归属无法证明，拒绝替换：$current")
      }
      $previousCurrent = Join-Path $TenetoraHome ("current.backup." + [guid]::NewGuid().ToString("N"))
      Move-Item -LiteralPath $current -Destination $previousCurrent -Force
    }
    $sourceItem = Get-Item -LiteralPath $sourcePointer -Force -ErrorAction SilentlyContinue
    if ($sourceItem) {
      $previousSourceWasPointer = ($sourceItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
      if (Test-ManagedPackageDirectory $sourcePointer) {
        $previousSource = Join-Path $TenetoraHome ("source-tenetora.backup." + [guid]::NewGuid().ToString("N"))
        Move-Item -LiteralPath $sourcePointer -Destination $previousSource -Force
      } else {
        $preserveSourcePointer = $true
        Write-Warning (Get-Text "Preserving source\tenetora because Tenetora ownership cannot be proven." "source\tenetora 的归属无法证明，已保留原内容。")
      }
    }
    if (Test-Path -LiteralPath $releaseDir) {
      $previousRelease = Join-Path $releaseParent ("." + $version + ".backup." + [guid]::NewGuid().ToString("N"))
      Move-Item -LiteralPath $releaseDir -Destination $previousRelease
    }
    Move-Item -LiteralPath $stageDir -Destination $releaseDir
    $releaseInstalled = $true
    New-Item -ItemType Directory -Force -Path (Join-Path $TenetoraHome "source") | Out-Null
    Set-Junction $current $releaseDir
    $currentPointerInstalled = $true
    if (-not $preserveSourcePointer) {
      Set-Junction $sourcePointer $releaseDir
      $sourcePointerInstalled = $true
    }
    $sourceRoot = $releaseDir
    if ($runtimeRollbackReady) {
      Invoke-Python $python $pythonArg @($runtimeTransaction, "--checkpoint", "--bin-dir", (Join-Path $TenetoraHome "bin"),
        "--journal", $runtimeRollbackJournal,
        "--expect-link", ("current=" + $sourceRoot),
        "--expect-link", ("source/tenetora=" + $sourceRoot), "--json")
    }
  }

  $installArgs = @($install, "--tools", $Tools, "--mode", "copy", "--path", $Path, "--codex-hooks", $CodexHooks,
    "--progress", $EffectiveProgress)
  if ($effectiveUpdate) {
    $installArgs += "--update"
    if (-not $scopeExplicit) {
      $installArgs += "--all-existing"
      $installArgs += "--auto-discover"
    }
    elseif ($Scope -eq "global") { $installArgs += "--global" }
    elseif ($Scope -eq "project") { $installArgs += "--in-project" }
    else { $installArgs += "--both" }
  } elseif ($Scope -eq "global") { $installArgs += "--global" }
  elseif ($Scope -eq "project") { $installArgs += "--in-project" }
  else { $installArgs += "--both" }
  if ($Force) { $installArgs += "--force" }
  if ($DryRun) { $installArgs += "--dry-run" }
  if ($VerboseOutput) { $installArgs += "--verbose" }
  if ($AllowSkillsOnly) { $installArgs += "--allow-skills-only" }
  if ($RequireFull) { $installArgs += "--require-full" }
  if ($AllowTrackedCodexHooks) { $installArgs += "--allow-tracked-codex-hooks" }
  if ($PruneShadowed) { $installArgs += "--prune-shadowed" }
  if ($NoPruneShadowed) { $installArgs += "--no-prune-shadowed" }
  if (-not $DryRun -and $hookRollbackReady) {
    $installArgs += @("--hook-rollback-journal", $hookRollbackJournal)
  }
  Invoke-Python $python $pythonArg $installArgs

  if (-not $DryRun) {
    if (-not $NoCli) {
      $cli = Join-Path $sourceRoot "skills\tenetora\scripts\ensure_cli.py"
      $cliArgs = @($cli, "--install", "--configure-path", "--bin-dir", (Join-Path $TenetoraHome "bin"))
      if ($runtimeRollbackReady) {
        $cliArgs += @("--runtime-rollback-journal", $runtimeRollbackJournal)
      }
      Invoke-Python $python $pythonArg $cliArgs
    }
    if ($hookRollbackReady) {
      $convergeArgs = @($install, "--converge-hooks", "--path", $Path, "--package-root", $sourceRoot,
        "--hook-rollback-journal", $hookRollbackJournal, "--json")
      if ($effectiveUpdate) { $convergeArgs += "--all-existing" }
      Invoke-Python $python $pythonArg $convergeArgs
      Invoke-Python $python $pythonArg @($install, "--finalize-hook-journal",
        "--hook-rollback-journal", $hookRollbackJournal, "--package-root", $sourceRoot, "--json")
    }
    if (-not $NoCli) {
      $migration = Join-Path $sourceRoot "scripts\migrate_machine_home.py"
      if (Test-Path $migration) {
        $migrationArgs = @($migration, "--canonical", $TenetoraHome, "--legacy", (Join-Path $profileHome ".agent-harness"), "--json")
        if ($runtimeRollbackReady) {
          $migrationArgs += @("--runtime-rollback-journal", $runtimeRollbackJournal,
            "--bin-dir", (Join-Path $TenetoraHome "bin"))
        }
        Invoke-Python $python $pythonArg $migrationArgs
      }
    }
    if ($runtimeRollbackReady) {
      Invoke-Python $python $pythonArg @($runtimeTransaction, "--checkpoint", "--bin-dir", (Join-Path $TenetoraHome "bin"),
        "--journal", $runtimeRollbackJournal,
        "--expect-link", ("current=" + $sourceRoot),
        "--expect-link", ("source/tenetora=" + $sourceRoot), "--json")
    }
  }
  if ($previousRelease -and (Test-Path -LiteralPath $previousRelease)) { Remove-Item -LiteralPath $previousRelease -Recurse -Force }
  if ($previousCurrent) { Remove-Backup $previousCurrent }
  if ($previousSource) { Remove-Backup $previousSource }
  Write-LanguagePreference $TenetoraHome $script:EffectiveLanguage
  if ($DryRun) {
    Write-Host (Get-Text "Tenetora dry run completed. No files were changed." "Tenetora 演练完成，未修改任何文件。")
  } else {
    $cliName = if ($IsWindowsHost) { "tenetora.cmd" } else { "tenetora" }
    Write-Host (Get-Text "Tenetora installation completed. CLI: $(Join-Path $TenetoraHome (Join-Path 'bin' $cliName))" "Tenetora 安装完成。CLI：$(Join-Path $TenetoraHome (Join-Path 'bin' $cliName))")
  }
} catch {
  if ($hookRollbackReady -and (Test-Path -LiteralPath (Join-Path $hookRollbackJournal "manifest.json"))) {
    try {
      Invoke-Python $python $pythonArg @($install, "--finalize-hook-journal",
        "--hook-rollback-journal", $hookRollbackJournal, "--package-root", $sourceRoot, "--json")
      Invoke-Python $python $pythonArg @($install, "--restore-hooks",
        "--hook-rollback-journal", $hookRollbackJournal, "--package-root", $sourceRoot, "--json")
    } catch {
      $hookRollbackFailed = $true
      Write-Warning (Get-Text "Managed Git hook rollback failed: $($_.Exception.Message)" "托管 Git Hook 回滚失败：$($_.Exception.Message)")
    }
  }
  if ($runtimeRollbackReady -and (Test-Path -LiteralPath (Join-Path $runtimeRollbackJournal "manifest.json"))) {
    try {
      Invoke-Python $python $pythonArg @($runtimeTransaction, "--restore", "--bin-dir", (Join-Path $TenetoraHome "bin"),
        "--journal", $runtimeRollbackJournal, "--json")
    } catch {
      $runtimeRollbackFailed = $true
      Write-Warning (Get-Text "Managed CLI runtime rollback failed: $($_.Exception.Message)" "托管 CLI runtime 回滚失败：$($_.Exception.Message)")
    }
  }
  if ($hookRollbackFailed -or $runtimeRollbackFailed) {
    $timestamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
    $failedTransactionBackup = Join-Path $TenetoraHome (Join-Path "backups\failed-transactions" ("install-" + $timestamp + "-" + [guid]::NewGuid().ToString("N").Substring(0, 8)))
    New-Item -ItemType Directory -Force -Path $failedTransactionBackup | Out-Null
    if (Test-Path -LiteralPath $hookRollbackJournal) {
      Copy-Item -LiteralPath $hookRollbackJournal -Destination (Join-Path $failedTransactionBackup "commit-hooks") -Recurse -Force
    }
    if (Test-Path -LiteralPath $runtimeRollbackJournal) {
      Copy-Item -LiteralPath $runtimeRollbackJournal -Destination (Join-Path $failedTransactionBackup "managed-runtime") -Recurse -Force
    }
    Write-Warning (Get-Text "Rollback evidence preserved for manual recovery: $failedTransactionBackup" "已保留回滚证据，供手动恢复：$failedTransactionBackup")
    throw (Get-Text "Automatic rollback was blocked; managed state was preserved for manual recovery." "自动回滚被阻断；托管状态已保留，需手动恢复。")
  }
  if ($stageDir -and (Test-Path -LiteralPath $stageDir)) {
    Remove-Item -LiteralPath $stageDir -Recurse -Force -ErrorAction SilentlyContinue
  }
  if ($releaseInstalled -and $releaseDir -and (Test-Path -LiteralPath $releaseDir)) {
    Remove-Item -LiteralPath $releaseDir -Recurse -Force -ErrorAction SilentlyContinue
  }
  if ($previousRelease -and (Test-Path -LiteralPath $previousRelease)) { Move-Item $previousRelease $releaseDir -Force }
  if ($previousCurrent) { Remove-Backup $previousCurrent }
  if ($previousSource) { Remove-Backup $previousSource }
  throw
} finally {
  Stop-InstallLock
  if ($temporary -and (Test-Path -LiteralPath $temporary)) { Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue }
  if (Test-Path -LiteralPath $hookRollbackJournal) { Remove-Item -LiteralPath $hookRollbackJournal -Recurse -Force -ErrorAction SilentlyContinue }
  if (Test-Path -LiteralPath $runtimeRollbackJournal) { Remove-Item -LiteralPath $runtimeRollbackJournal -Recurse -Force -ErrorAction SilentlyContinue }
}
