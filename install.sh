#!/usr/bin/env bash
set -euo pipefail

MINIMUM_PYTHON_MAJOR=3
MINIMUM_PYTHON_MINOR=9
REQUIRED_INSTALLER_PROTOCOL=3
MAX_ARCHIVE_BYTES=$((128*1024*1024))
DEFAULT_RAW_URL="https://raw.githubusercontent.com/lcgyl/Tenetora/main/install.sh"
DEFAULT_ZIP_URL="https://github.com/lcgyl/Tenetora/releases/latest/download/tenetora-latest.zip"
DEFAULT_MANIFEST_URL="https://github.com/lcgyl/Tenetora/releases/latest/download/manifest.json"
DEFAULT_SOURCE_ZIP_URL="https://github.com/lcgyl/Tenetora/archive/refs/heads/main.zip"

ZIP_URL="${TENETORA_ZIP_URL:-$DEFAULT_ZIP_URL}"
ZIP_URL_EXPLICIT=0
if [[ -n "${TENETORA_ZIP_URL:-}" ]]; then
  ZIP_URL_EXPLICIT=1
fi
ZIP_FILE="${TENETORA_ZIP_FILE:-}"
ZIP_SHA256="${TENETORA_SHA256:-}"
MANIFEST_URL="${TENETORA_MANIFEST_URL:-$DEFAULT_MANIFEST_URL}"
export PYTHONDONTWRITEBYTECODE=1
external_manifest_sha=""
HARNESS_HOME="${TENETORA_HOME:-}"
USER_HOME="${HOME:-}"
SOURCE_DIR="${TENETORA_SOURCE_DIR:-}"
TOOLS="${TENETORA_TOOLS:-auto}"
SCOPE="${TENETORA_SCOPE:-global}"
SCOPE_EXPLICIT=0
if [[ -n "${TENETORA_SCOPE:-}" ]]; then
  SCOPE_EXPLICIT=1
fi
MODE="${TENETORA_MODE:-auto}"
PROJECT_PATH="${TENETORA_PATH:-$(pwd)}"
FORCE=0
DRY_RUN=0
INSTALL_CLI=1
ALLOW_SKILLS_ONLY=0
REQUIRE_FULL=0
CODEX_HOOKS="${TENETORA_CODEX_HOOKS:-auto}"
ALLOW_TRACKED_CODEX_HOOKS=0
PRUNE_SHADOWED=0
NO_PRUNE_SHADOWED=0
PROGRESS_MODE="${TENETORA_PROGRESS:-auto}"
VERBOSE=0
STAGED_RELEASE_DIR=""
PREVIOUS_RELEASE_BACKUP=""
INSTALL_LANG="${TENETORA_LANG:-}"
TEMP_DIRS=()
FAILED_TRANSACTION_BACKUP=""
INSTALL_LOCK_DIR=""
INSTALL_LOCK_FIFO=""
INSTALL_LOCK_READY=""
INSTALL_LOCK_PID=""
INSTALL_LOCK_FD_OPEN=0
INSTALL_LOCK_ENV_WAS_SET=0
INSTALL_LOCK_ENV_VALUE=""
INSTALL_LOCK_TOKEN=""
INSTALL_LOCK_TOKEN_ENV_WAS_SET=0
INSTALL_LOCK_TOKEN_ENV_VALUE=""

cleanup_temp_dirs() {
  local path
  if [[ "$INSTALL_LOCK_FD_OPEN" == "1" ]]; then
    exec 9>&-
    INSTALL_LOCK_FD_OPEN=0
  fi
  if [[ -n "$INSTALL_LOCK_PID" ]]; then
    wait "$INSTALL_LOCK_PID" 2>/dev/null || true
    INSTALL_LOCK_PID=""
  fi
  if [[ "$INSTALL_LOCK_ENV_WAS_SET" == "1" ]]; then
    export TENETORA_OUTER_INSTALL_LOCK_HELD="$INSTALL_LOCK_ENV_VALUE"
  else
    unset TENETORA_OUTER_INSTALL_LOCK_HELD || true
  fi
  if [[ "$INSTALL_LOCK_TOKEN_ENV_WAS_SET" == "1" ]]; then
    export TENETORA_OUTER_INSTALL_LOCK_TOKEN="$INSTALL_LOCK_TOKEN_ENV_VALUE"
  else
    unset TENETORA_OUTER_INSTALL_LOCK_TOKEN || true
  fi
  for path in "${TEMP_DIRS[@]-}"; do
    [[ -n "$path" ]] && rm -rf "$path"
  done
  return 0
}
trap cleanup_temp_dirs EXIT

start_install_lock() {
  [[ "$DRY_RUN" == "1" ]] && return 0
  local holder
  holder="${SOURCE_DIR}/skills/tenetora/scripts/install_lock_holder.py"
  [[ -f "$holder" ]] || return 1
  INSTALL_LOCK_DIR="$(mktemp -d)"
  TEMP_DIRS+=("$INSTALL_LOCK_DIR")
  INSTALL_LOCK_FIFO="${INSTALL_LOCK_DIR}/stdin"
  INSTALL_LOCK_READY="${INSTALL_LOCK_DIR}/ready"
  mkfifo "$INSTALL_LOCK_FIFO"
  if [[ "${TENETORA_OUTER_INSTALL_LOCK_HELD+x}" == "x" ]]; then
    INSTALL_LOCK_ENV_WAS_SET=1
    INSTALL_LOCK_ENV_VALUE="$TENETORA_OUTER_INSTALL_LOCK_HELD"
  fi
  if [[ "${TENETORA_OUTER_INSTALL_LOCK_TOKEN+x}" == "x" ]]; then
    INSTALL_LOCK_TOKEN_ENV_WAS_SET=1
    INSTALL_LOCK_TOKEN_ENV_VALUE="$TENETORA_OUTER_INSTALL_LOCK_TOKEN"
  fi
  "$PYTHON_BIN" "$holder" --home "$HARNESS_HOME" --ready-file "$INSTALL_LOCK_READY" \
    <"$INSTALL_LOCK_FIFO" >/dev/null 2>&1 &
  INSTALL_LOCK_PID=$!
  exec 9>"$INSTALL_LOCK_FIFO"
  INSTALL_LOCK_FD_OPEN=1
  while [[ ! -f "$INSTALL_LOCK_READY" ]]; do
    if ! kill -0 "$INSTALL_LOCK_PID" 2>/dev/null; then
      return 1
    fi
    sleep 0.01
  done
  [[ -f "$INSTALL_LOCK_READY" ]] || return 1
  INSTALL_LOCK_TOKEN="$(tr -d '\r\n' <"$INSTALL_LOCK_READY")"
  [[ "$INSTALL_LOCK_TOKEN" =~ ^[0-9a-f]{64}$ ]] || return 1
  export TENETORA_OUTER_INSTALL_LOCK_HELD=1
  export TENETORA_OUTER_INSTALL_LOCK_TOKEN="$INSTALL_LOCK_TOKEN"
}

preserve_failed_transaction_journals() {
  local destination timestamp
  timestamp="$(date -u '+%Y%m%dT%H%M%SZ')"
  destination="${HARNESS_HOME}/backups/failed-transactions/install-${timestamp}-$$"
  mkdir -p "$destination"
  if [[ -d "$HOOK_ROLLBACK_JOURNAL" ]]; then
    cp -R "$HOOK_ROLLBACK_JOURNAL" "${destination}/commit-hooks"
  fi
  if [[ -d "$MANAGED_RUNTIME_SNAPSHOT" ]]; then
    cp -R "$MANAGED_RUNTIME_SNAPSHOT" "${destination}/managed-runtime"
  fi
  if [[ -n "$PREVIOUS_RELEASE_BACKUP" && ( -e "$PREVIOUS_RELEASE_BACKUP" || -L "$PREVIOUS_RELEASE_BACKUP" ) ]]; then
    cp -R "$PREVIOUS_RELEASE_BACKUP" "${destination}/previous-release"
  fi
  FAILED_TRANSACTION_BACKUP="$destination"
  say \
    "Rollback evidence preserved for manual recovery: ${destination}" \
    "回滚证据已保留，供人工恢复：${destination}"
}

i18n_text() {
  local english="$1"
  local chinese="$2"
  if [[ "$INSTALL_LANG" == "zh" ]]; then
    printf '%s' "$chinese"
  else
    printf '%s' "$english"
  fi
}

say() {
  i18n_text "$1" "$2"
  printf '\n'
}

warn() {
  i18n_text "$1" "$2" >&2
  printf '\n' >&2
}

normalize_language() {
  case "$1" in
    en|EN|en-US|en-us|English|english) printf 'en' ;;
    zh|ZH|zh-CN|zh-cn|zh-Hans|zh-hans|Chinese|chinese|中文) printf 'zh' ;;
    *) return 1 ;;
  esac
}

language_from_args() {
  local args=("$@")
  local index
  for ((index = 0; index < ${#args[@]}; index++)); do
    if [[ "${args[$index]}" == "--lang" ]]; then
      if ((index + 1 >= ${#args[@]})); then
        return 2
      fi
      if [[ "${args[$((index + 1))]}" == -* ]]; then
        return 2
      fi
      printf '%s' "${args[$((index + 1))]}"
      return 0
    fi
  done
  return 1
}

language_from_preferences() {
  local args=("$@")
  local home="${TENETORA_HOME:-${HOME:-}/.tenetora}"
  local index
  for ((index = 0; index < ${#args[@]}; index++)); do
    if [[ "${args[$index]}" == "--home" && $((index + 1)) -lt ${#args[@]} ]]; then
      home="${args[$((index + 1))]}"
      break
    fi
  done
  [[ -n "$home" ]] || return 1
  local preference_python="${TENETORA_PYTHON:-}"
  if [[ -z "$preference_python" ]]; then
    preference_python="$(command -v python3 || command -v python || true)"
  fi
  [[ -n "$preference_python" ]] || return 1
  "$preference_python" - "$home/state/preferences.json" <<'PY'
import json
import sys
from pathlib import Path
try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")).get("language")
except (OSError, json.JSONDecodeError):
    value = None
if value in {"en", "zh"}:
    print(value)
PY
}

can_prompt_for_language() {
  [[ -t 0 || -t 1 || -t 2 ]] || return 1
  (: </dev/tty) 2>/dev/null
}

language_prompt() {
  if [[ -t 2 ]]; then
    printf '%b' "$1" >&2
  else
    printf '%b' "$1"
  fi
}

choose_language() {
  local requested=""
  local arg_status=0
  requested="$(language_from_args "$@")" || arg_status=$?
  if [[ "$arg_status" == "2" ]]; then
    echo "--lang requires a value / --lang 需要指定语言" >&2
    exit 2
  fi
  if [[ -n "$requested" ]]; then
    INSTALL_LANG="$(normalize_language "$requested")" || {
      echo "Unsupported language: ${requested}. Use en or zh. / 不支持的语言：${requested}，请使用 en 或 zh。" >&2
      exit 2
    }
    say "Language: English" "语言：中文"
  elif [[ -n "$INSTALL_LANG" ]]; then
    INSTALL_LANG="$(normalize_language "$INSTALL_LANG")" || {
      echo "Unsupported language environment value: ${INSTALL_LANG}. Use en or zh. / 语言环境变量不受支持，请使用 en 或 zh。" >&2
      exit 2
    }
    say "Language: English" "语言：中文"
  elif preference="$(language_from_preferences "$@")"; [[ "$preference" == "en" || "$preference" == "zh" ]]; then
    INSTALL_LANG="$preference"
    say "Language: English" "语言：中文"
  elif can_prompt_for_language; then
    while true; do
      language_prompt 'Select language / 选择语言:\n  1) English\n  2) 中文\nChoice / 请选择 [1/2]: '
      local choice=""
      IFS= read -r choice </dev/tty || choice=""
      case "$choice" in
        1|en|EN|English|english) INSTALL_LANG="en"; break ;;
        2|zh|ZH|中文) INSTALL_LANG="zh"; break ;;
        *) language_prompt 'Invalid choice, enter 1 or 2. / 选择无效，请输入 1 或 2。\n' ;;
      esac
    done
    say "Language: English" "语言：中文"
  else
    INSTALL_LANG="en"
    say "Language: English (non-interactive default; use --lang zh for Chinese)" "语言：中文"
  fi
  export TENETORA_LANG="$INSTALL_LANG"
}

usage() {
  if [[ "$INSTALL_LANG" == "zh" ]]; then
    cat <<USAGE
Tenetora 在线安装器

用法：
  curl -fsSL ${DEFAULT_RAW_URL} | bash
  curl -fsSL ${DEFAULT_RAW_URL} | bash -s -- --lang zh --tools all
  curl -fsSL ${DEFAULT_RAW_URL} | bash -s -- --lang zh --zip-url <release-zip-url>
  bash install.sh --lang zh --zip-file /path/to/tenetora.zip

范围：
  只安装 Tenetora lifecycle skill 包和 CLI shim，不会创建 .tenetora。
  在项目的 AI 对话中输入“使用 Tenetora 初始化当前项目”；AI 会检查现有规则后调用初始化 skill。
  旧版 Agent Harness 安装仅作为一次性迁移来源读取；新安装不会重新生成旧 CLI、skill 或 release 别名。
  首次安装使用 global、project 或 both 创建安装面；0.2.x 用户可再运行一次完成 0.3 bridge。
  bridge 后日常维护统一使用 tenetora upgrade。该脚本只保留给首次安装、离线恢复或 launcher 修复。
  bridge 会更新每个已登记项目安装面和已有全局安装面，不会创建缺失 scope。

运行时：
  需要 Python ${MINIMUM_PYTHON_MAJOR}.${MINIMUM_PYTHON_MINOR} 或更高版本。存在多个兼容解释器时，
  安装器会选择常见的较新版本并记录到 CLI shim。

语言：
  --lang <en|zh>         选择英文或中文。交互终端会在第一步询问；自动化环境可显式传入。

包来源：
  --zip-url <url>        在线 zip 包地址。默认：${DEFAULT_ZIP_URL}
                          如果默认 latest release 尚未发布或安装协议落后，安装器会回退到主分支源码 zip：
                          ${DEFAULT_SOURCE_ZIP_URL}
                          显式设置 --zip-url 或 TENETORA_ZIP_URL 时不会回退。
  --zip-file <file>      离线 zip 包路径。
  --sha256 <hex>         期望的 zip SHA-256，离线或固定版本安装时建议使用。
  --source-dir <dir>     仅开发使用：直接使用本地 Tenetora 源码目录。

安装选项：
  --home <dir>           Tenetora 主目录。默认：~/.tenetora。
                          Codex --codex-hooks project 需要使用这个默认稳定目录。
  -t, --tools <tools>    auto、all 或工具列表：agents,codex,claude,cursor,opencode,pi,zcode。
  -g, --global           首次安装到用户级目录；升级时只筛选已有全局安装。安装默认值。
  -i, --in-project       首次安装到 --path 项目；升级时只筛选已有项目安装。
  -b, --both             首次安装两个范围；升级时筛选两个范围中的已有安装。
  -s, --scope <scope>    global、project 或 both；升级时仅作为已有安装过滤器。
  -p, --path <dir>       项目上下文。无 scope 升级时不会限制机器级项目清单。默认：当前目录。
                          升级时显式使用 project/both 会强制解释该项目：升级已有项目安装面，
                          或迁移已证明归属的 .harness/.tenetora；空项目或归属不明状态会在写入前失败。
  -m, --mode <mode>      auto、symlink 或 copy。默认：auto。
  -f, --force            替换冲突的 skill 安装。
  --allow-skills-only    明确接受 marketplace/配置退化；宿主 CLI 缺失会自动延期补齐。
  --require-full         任一工具无法达到最大能力时，在写入前阻断。
  --codex-hooks <mode>   Codex Hook 模式：auto、native、project 或 off。
  --allow-tracked-codex-hooks
                          允许结构化更新 Git 已跟踪的 .codex/hooks.json。
  --prune-shadowed       安全删除被遮蔽且未修改的 Tenetora 或已证明归属的旧 runtime adapter。
  --no-prune-shadowed    诊断时禁用升级过程中的安全自动 shadow runtime 清理。
  --progress <mode>      进度显示：auto、always 或 never。默认：auto。
  --no-progress          禁用动态进度显示，保留稳定逐行摘要。
  --verbose              同时在终端输出完整安装动作；默认写入本地日志。
  --dry-run              只展示操作，不写入。
  --no-cli               跳过 CLI shim 自举。
  --help                 显示帮助。

环境变量：
  TENETORA_LANG、TENETORA_ZIP_URL、TENETORA_ZIP_FILE、TENETORA_SHA256、TENETORA_HOME、
  TENETORA_SOURCE_DIR、TENETORA_TOOLS、TENETORA_SCOPE、TENETORA_MODE、TENETORA_PATH、
  TENETORA_CODEX_HOOKS、TENETORA_PROGRESS、CODEX_HOME、
  CLAUDE_CONFIG_DIR、CLAUDE_HOME、CURSOR_HOME、OPENCODE_HOME、
  XDG_CONFIG_HOME、ZCODE_HOME、ZCODE_PLUGIN_HOME。
USAGE
    return
  fi
  cat <<USAGE
Tenetora online installer

Usage:
  curl -fsSL ${DEFAULT_RAW_URL} | bash
  curl -fsSL ${DEFAULT_RAW_URL} | bash -s -- --tools all
  curl -fsSL ${DEFAULT_RAW_URL} | bash -s -- --zip-url <release-zip-url>
  bash install.sh --zip-file /path/to/tenetora.zip

Scope:
  Installs the Tenetora lifecycle skill package and CLI shim only; it does not create .tenetora.
  In the project's AI conversation, say "Use Tenetora to initialize this project"; the AI inspects existing rules and invokes the initialization skill.
  Legacy Agent Harness installs are read only as one-way migration sources; new installs do not recreate old CLI, skill, or release aliases.
  Fresh installation creates the selected global, project, or both surfaces. A 0.2.x user may run this bootstrap
  one final time for the 0.3 bridge. Afterward use tenetora upgrade; keep this script for install or recovery only.
  The bridge updates every registered project surface and existing global surface without creating a missing scope.

Runtime:
  Python ${MINIMUM_PYTHON_MAJOR}.${MINIMUM_PYTHON_MINOR} or newer is required. When several supported interpreters are installed,
  the installer prefers the newest common version and records it in the CLI shim.

Language:
  --lang <en|zh>         Select English or Chinese. Interactive terminals prompt first.

Package source:
  --zip-url <url>         Online zip package URL. Default: ${DEFAULT_ZIP_URL}
                          If the default latest release asset is missing or uses an older installer protocol, the installer
                          falls back to the main branch source zip: ${DEFAULT_SOURCE_ZIP_URL}
                          Explicit --zip-url or TENETORA_ZIP_URL disables this fallback.
  --zip-file <file>       Offline zip package path.
  --sha256 <hex>          Expected zip SHA-256. Recommended for offline or pinned installs.
  --source-dir <dir>      Developer-only: use an existing local Tenetora source tree instead of a zip.

Install options:
  --home <dir>            Tenetora home. Default: ~/.tenetora.
                          Codex --codex-hooks project requires this default stable home.
  -t, --tools <tools>     auto, all, or comma-separated tools: agents,codex,claude,cursor,opencode,pi,zcode.
  -g, --global            Fresh install to user-level directories; upgrade only existing global surfaces. Install default.
  -i, --in-project        Fresh install under --path; upgrade only existing project surfaces.
  -b, --both              Fresh install both scopes; upgrade existing surfaces from both scopes.
  -s, --scope <scope>     global, project, or both; an existing-surface filter during upgrade.
  -p, --path <dir>        Project context; it does not narrow a scope-less machine upgrade. Default: current directory.
                          During upgrade, explicit project/both must account for this project: update an existing
                          project surface or migrate proven .harness/.tenetora governance; empty or ambiguous targets fail before writes.
  -m, --mode <mode>       auto, symlink, or copy. Default: auto.
  -f, --force             Replace conflicting skill installs.
  --allow-skills-only     Explicitly accept marketplace/config degradation; missing host CLIs defer native activation automatically.
  --require-full          Block before writes when any selected tool cannot reach its maximum capability.
  --codex-hooks <mode>    Codex hook mode: auto, native, project, or off. Default: auto.
  --allow-tracked-codex-hooks
                          Allow structured updates to a tracked .codex/hooks.json for --codex-hooks project.
  --prune-shadowed        Remove only Tenetora or proven legacy-owned, unmodified shadow hook runtimes when safe.
  --no-prune-shadowed     Disable safe automatic shadow runtime cleanup during upgrade diagnostics.
  --progress <mode>       Progress rendering: auto, always, or never. Default: auto.
  --no-progress           Disable dynamic progress while keeping stable summary lines.
  --verbose               Also print detailed actions; they are logged locally by default.
  --dry-run               Print actions without writing skill installs.
  --no-cli                Skip initial CLI shim bootstrap.
  --help                  Show this help message.

Environment:
  TENETORA_LANG, TENETORA_ZIP_URL, TENETORA_ZIP_FILE, TENETORA_SHA256, TENETORA_HOME,
  TENETORA_SOURCE_DIR, TENETORA_TOOLS, TENETORA_SCOPE, TENETORA_MODE, TENETORA_PATH,
  TENETORA_CODEX_HOOKS, TENETORA_PROGRESS,
  CODEX_HOME, CLAUDE_CONFIG_DIR, CLAUDE_HOME, CURSOR_HOME,
  OPENCODE_HOME, XDG_CONFIG_HOME, ZCODE_HOME, ZCODE_PLUGIN_HOME.
USAGE
}

die() {
  echo "$*" >&2
  exit 1
}

unknown_arg() {
  warn "Unknown argument: $1" "未知参数：$1"
  warn "Run with --help for usage." "请使用 --help 查看帮助。"
  exit 2
}

need_value() {
  local option="$1"
  local value="${2:-}"
  if [[ -z "$value" ]]; then
    die "$(i18n_text "${option} requires a value" "${option} 需要指定值")"
  fi
}

validate_choice() {
  local name="$1"
  local value="$2"
  local choices="$3"
  case ",${choices}," in
    *",${value},"*) ;;
    *) die "$(i18n_text "Invalid ${name}: ${value}. Expected one of: ${choices//,/ }" "无效的 ${name}：${value}。可选值：${choices//,/ }")" ;;
  esac
}

validate_package_source_selection() {
  if [[ -n "$SOURCE_DIR" && ( -n "$ZIP_FILE" || "$ZIP_URL_EXPLICIT" == "1" || -n "$ZIP_SHA256" ) ]]; then
    die "$(i18n_text "Package source options are mutually exclusive: --source-dir cannot be combined with --zip-file, an explicit --zip-url, or --sha256." "安装包来源选项互斥：--source-dir 不能与 --zip-file、显式 --zip-url 或 --sha256 同时使用。")"
  fi
  if [[ -n "$ZIP_FILE" && "$ZIP_URL_EXPLICIT" == "1" ]]; then
    die "$(i18n_text "Package source options are mutually exclusive: --zip-file cannot be combined with an explicit --zip-url." "安装包来源选项互斥：--zip-file 不能与显式 --zip-url 同时使用。")"
  fi
}

find_python() {
  local candidate candidate_path
  # Prefer the newest common interpreter so a legacy `python3` alias does not
  # pin the generated CLI shim to an unnecessarily old runtime.
  for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
    if ! command -v "$candidate" >/dev/null 2>&1; then
      continue
    fi
    candidate_path="$(command -v "$candidate")"
    if "$candidate_path" -c "import sys; raise SystemExit(0 if sys.version_info >= (${MINIMUM_PYTHON_MAJOR}, ${MINIMUM_PYTHON_MINOR}) else 1)" >/dev/null 2>&1; then
      echo "$candidate_path"
      return 0
    fi
  done
  die "$(i18n_text "Python ${MINIMUM_PYTHON_MAJOR}.${MINIMUM_PYTHON_MINOR} or newer is required (python3/python not found or too old)" "需要 Python ${MINIMUM_PYTHON_MAJOR}.${MINIMUM_PYTHON_MINOR} 或更高版本（未找到 python3/python，或版本过低）")"
}

canonical_dir() {
  local dir="$1"
  (cd "$dir" && pwd -P)
}

resolve_managed_current_source() {
  "$PYTHON_BIN" - "$1" <<'PY'
import stat
import sys
from pathlib import Path

home = Path(sys.argv[1])
current = home / "current"
releases = home / "releases"
try:
    current_info = current.lstat()
    if not (stat.S_ISDIR(current_info.st_mode) or stat.S_ISLNK(current_info.st_mode)):
        raise RuntimeError("current is not a directory or managed pointer")
    releases_info = releases.lstat()
    if not stat.S_ISDIR(releases_info.st_mode) or stat.S_ISLNK(releases_info.st_mode):
        raise RuntimeError("releases root is not a real managed directory")
    target = current.resolve(strict=True)
    releases_root = releases.resolve(strict=True)
    relative = target.relative_to(releases_root)
    if not relative.parts or not target.is_dir():
        raise RuntimeError("current does not point to a release directory")
    cursor = releases
    for part in relative.parts:
        cursor /= part
        info = cursor.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("current release path contains a symbolic link or non-directory")
except (OSError, RuntimeError, ValueError) as exc:
    raise SystemExit(f"managed current release is invalid: {exc}")
print(target)
PY
}

validate_managed_home_path() {
  "$PYTHON_BIN" - "$1" <<'PY'
import os
import stat
import sys
from pathlib import Path

raw = sys.argv[1]
candidate = Path(os.path.abspath(os.path.expanduser(raw)))
if ".." in candidate.parts:
    raise SystemExit(f"Tenetora home contains parent traversal: {raw}")
if candidate == Path(candidate.anchor):
    raise SystemExit(f"Tenetora home cannot be the filesystem root: {candidate}")
allowed_redirects = {
    Path("/var"): "/private/var",
    Path("/tmp"): "/private/tmp",
}
current = Path(candidate.anchor)
for part in candidate.parts[1:]:
    current /= part
    try:
        info = current.lstat()
    except FileNotFoundError:
        continue
    except OSError as exc:
        raise SystemExit(f"cannot inspect Tenetora home component {current}: {exc}")
    if stat.S_ISLNK(info.st_mode):
        target = Path(os.readlink(current))
        if not target.is_absolute():
            target = (current.parent / target).absolute()
        if current not in allowed_redirects or target != Path(allowed_redirects[current]):
            raise SystemExit(f"Tenetora home contains a symbolic link or junction: {current}")
    elif not stat.S_ISDIR(info.st_mode):
        raise SystemExit(f"Tenetora home component is not a directory: {current}")
PY
}

source_version() {
  local dir="$1"
  local version_file="${dir}/skills/tenetora/VERSION"
  if [[ ! -f "$version_file" ]]; then
    version_file="${dir}/skills/agent-harness/VERSION"
  fi
  if [[ -f "$version_file" ]]; then
    local version
    version="$(tr -d '[:space:]' < "$version_file")"
    if [[ -n "$version" ]]; then
      echo "$version"
      return 0
    fi
  fi

  local legacy_version_file="${dir}/cli/agent_harness/__init__.py"
  if [[ ! -f "$legacy_version_file" ]]; then
    legacy_version_file="${dir}/skills/agent-harness/cli/agent_harness/__init__.py"
  fi
  if [[ ! -f "$legacy_version_file" ]]; then
    echo "unknown"
    return 0
  fi
  "$PYTHON_BIN" - "$legacy_version_file" <<'PY'
import ast
import sys
from pathlib import Path

try:
    for node in ast.parse(Path(sys.argv[1]).read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__version__":
                    print(ast.literal_eval(node.value))
                    raise SystemExit(0)
except Exception:
    pass
print("unknown")
PY
}

validate_package_version() {
  local version="$1"
  if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    die "$(i18n_text "Package version is invalid; refusing to use it as a path: ${version}" "安装包版本无效，拒绝将其用于路径：${version}")"
  fi
}

source_installer_protocol() {
  local dir="$1"
  local manifest="${dir}/manifest.json"
  if [[ -f "$manifest" ]]; then
    "$PYTHON_BIN" - "$manifest" <<'PY'
import json
import sys
from pathlib import Path

try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    protocol = int(payload.get("installer_protocol", 1))
except Exception:
    protocol = 1
print(max(protocol, 1))
PY
    return 0
  fi

  local installer="${dir}/scripts/install-skill.py"
  if [[ -f "${dir}/scripts/migrate_machine_home.py" ]] \
    && [[ -f "$installer" ]] \
    && grep -q -- "--all-existing" "$installer" \
    && grep -q -- "--events-jsonl" "$installer"; then
    echo "3"
    return 0
  fi
  if [[ -f "$installer" ]] \
    && grep -q -- "--all-existing" "$installer" \
    && grep -q -- "--events-jsonl" "$installer"; then
    echo "2"
    return 0
  fi
  echo "1"
}

source_bridge_protocol() {
  local dir="$1"
  if [[ ! -f "${dir}/manifest.json" ]]; then
    echo 0
    return 0
  fi
  "$PYTHON_BIN" - "${dir}/manifest.json" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
value = payload.get("bridge_protocol", 0)
print(value if isinstance(value, int) else 0)
PY
}

package_contract_missing() {
  local dir="$1"
  local path
  local required_paths=(
    "scripts/install-skill.py"
    "scripts/migrate_machine_home.py"
    "skills/tenetora/scripts/ensure_cli.py"
    "skills/tenetora/scripts/install_lock_holder.py"
    "skills/tenetora/cli/tenetora/install_lock.py"
  )
  for path in "${required_paths[@]}"; do
    if [[ ! -f "${dir}/${path}" ]]; then
      printf '%s\n' "$path"
    fi
  done
}

is_default_latest_package() {
  [[ "$ZIP_URL_EXPLICIT" == "0" && -z "$ZIP_FILE" && -z "$ZIP_SHA256" && "$ZIP_URL" == "$DEFAULT_ZIP_URL" ]]
}

stamp_manifest_sha() {
  local dir="$1"
  local sha="$2"
  if [[ ! -f "${dir}/manifest.json" ]]; then
    return 0
  fi
  "$PYTHON_BIN" - "${dir}/manifest.json" "$sha" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["sha256"] = sys.argv[2]
path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

manifest_sha256() {
  local manifest="$1"
  "$PYTHON_BIN" - "$manifest" <<'PY'
import json
import re
import sys
from pathlib import Path

try:
    value = str(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")).get("sha256", "")).lower()
except (OSError, ValueError, json.JSONDecodeError):
    value = ""
print(value if re.fullmatch(r"[0-9a-f]{64}", value) else "")
PY
}

validate_external_manifest() {
  local manifest="$1"
  local source="$2"
  local actual_sha="$3"
  "$PYTHON_BIN" - "$manifest" "${source}/manifest.json" "$actual_sha" <<'PY'
import json
import re
import sys
from pathlib import Path

external_path = Path(sys.argv[1])
embedded_path = Path(sys.argv[2])
actual_sha = sys.argv[3]
try:
    external = json.loads(external_path.read_text(encoding="utf-8"))
    embedded = json.loads(embedded_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"release manifest is unreadable: {exc}")
if not isinstance(external, dict) or not isinstance(embedded, dict):
    raise SystemExit("release manifest must be a JSON object")
version = str(external.get("version") or "")
if (
    external.get("name") != "tenetora"
    or external.get("format") != "tenetora-release-zip-v1"
    or external.get("zip_root") != "tenetora"
    or not re.fullmatch(r"\d+\.\d+\.\d+", version)
    or external.get("artifact") not in {f"tenetora-{version}.zip", "tenetora-latest.zip"}
    or str(external.get("sha256") or "").lower() != actual_sha.lower()
):
    raise SystemExit("external release manifest identity is invalid")
fields = (
    "name", "version", "commit", "format", "installer_protocol", "bridge_protocol",
    "minimum_bridge_version", "upgrade_entrypoint", "state_schemas", "zip_root", "files",
    "file_sha256",
)
if any(external.get(field) != embedded.get(field) for field in fields):
    raise SystemExit("external and embedded release manifests disagree")
files = external.get("files")
hashes = external.get("file_sha256")
if not isinstance(files, list) or files != sorted(set(files)) or not isinstance(hashes, dict) or sorted(hashes) != files:
    raise SystemExit("external release manifest file inventory is invalid")
if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes.values()):
    raise SystemExit("external release manifest file hashes are invalid")
PY
}

source_identity() {
  local dir="$1"
  if [[ -f "${dir}/manifest.json" ]]; then
    "$PYTHON_BIN" - "${dir}/manifest.json" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
version = payload.get("version", "unknown")
commit = payload.get("commit", "unknown")
sha = str(payload.get("sha256", ""))
suffix = f" sha256:{sha[:12]}" if sha else ""
print(f"zip {version} commit:{commit}{suffix}")
PY
    return 0
  fi
  if [[ -d "${dir}/.git" ]]; then
    local commit
    commit="$(git -C "$dir" rev-parse --short HEAD 2>/dev/null || echo "unknown")"
    if [[ "$commit" != "unknown" ]] && ! git -C "$dir" diff --quiet --ignore-submodules -- 2>/dev/null; then
      commit="${commit}+dirty"
    fi
    echo "git ${commit}"
  else
    echo "local ${dir}"
  fi
}

zip_sha256() {
  local file="$1"
  "$PYTHON_BIN" - "$file" <<'PY'
import hashlib
import sys
from pathlib import Path

digest = hashlib.sha256()
with Path(sys.argv[1]).open("rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
print(digest.hexdigest())
PY
}

validate_archive_size() {
  local file="$1"
  "$PYTHON_BIN" - "$file" "$MAX_ARCHIVE_BYTES" <<'PY'
import sys
from pathlib import Path

if Path(sys.argv[1]).stat().st_size > int(sys.argv[2]):
    raise SystemExit(1)
PY
}

validate_source_tree() {
  "$PYTHON_BIN" - "$1" <<'PY'
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1]).expanduser()
if root.is_symlink() or not root.is_dir():
    raise SystemExit(1)
pending = [root]
while pending:
    current = pending.pop()
    try:
        entries = list(current.iterdir())
    except OSError:
        raise SystemExit(1)
    for entry in entries:
        try:
            mode = entry.stat(follow_symlinks=False).st_mode
        except OSError:
            raise SystemExit(1)
        if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise SystemExit(1)
        if stat.S_ISDIR(mode):
            pending.append(entry)
PY
}

download_zip() {
  local url="$1"
  local dest="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$dest"
    return $?
  fi
  if command -v wget >/dev/null 2>&1; then
    wget -qO "$dest" "$url"
    return $?
  fi
  die "$(i18n_text "curl or wget is required to download ${url}" "下载 ${url} 需要 curl 或 wget")"
}

extract_zip() {
  local zip_file="$1"
  local extract_dir="$2"
  local allow_source_archive="${3:-0}"
  "$PYTHON_BIN" - "$zip_file" "$extract_dir" "$allow_source_archive" <<'PY'
import shutil
import sys
import zipfile
import os
import stat
import unicodedata
from pathlib import Path, PurePosixPath

zip_path = Path(sys.argv[1])
extract_dir = Path(sys.argv[2])
allow_source_archive = sys.argv[3] == "1"
if extract_dir.exists():
    shutil.rmtree(extract_dir)
extract_dir.mkdir(parents=True)

if not zipfile.is_zipfile(zip_path):
    message = f"Invalid zip package: {zip_path}"
    if os.environ.get("TENETORA_LANG") == "zh":
        message = f"无效的 Zip 安装包：{zip_path}"
    raise SystemExit(message)

with zipfile.ZipFile(zip_path) as archive:
    all_infos = archive.infolist()
    infos = [info for info in all_infos if not info.is_dir()]
    names = [info.filename for info in infos]
    if not names:
        raise SystemExit("Zip package is empty." if os.environ.get("TENETORA_LANG") != "zh" else "Zip 安装包为空。")
    if len(all_infos) > 4096 or sum(int(info.file_size) for info in infos) > 256 * 1024 * 1024:
        raise SystemExit(
            "Zip 安装包超过有界解压限制。"
            if os.environ.get("TENETORA_LANG") == "zh"
            else "Zip package exceeds the bounded extraction limit."
        )
    seen = set()
    portable_seen = set()
    file_names = set()
    directory_names = set()
    roots = set()
    for info in all_infos:
        name = info.filename
        entry = PurePosixPath(name)
        if entry.is_absolute() or "\x00" in name or "\\" in name or not entry.parts or ".." in entry.parts:
            message = f"Unsafe zip entry: {name}"
            if os.environ.get("TENETORA_LANG") == "zh":
                message = f"Zip 安装包包含不安全路径：{name}"
            raise SystemExit(message)
        roots.add(entry.parts[0])
        normalized = entry.as_posix().rstrip("/")
        if normalized in seen:
            raise SystemExit(
                "Zip 安装包包含重复条目。"
                if os.environ.get("TENETORA_LANG") == "zh"
                else "Zip package contains duplicate entries."
            )
        seen.add(normalized)
        portable_name = unicodedata.normalize("NFC", normalized).casefold()
        if portable_name in portable_seen:
            raise SystemExit(
                "Zip 安装包包含跨平台路径冲突。"
                if os.environ.get("TENETORA_LANG") == "zh"
                else "Zip package contains portable path collisions."
            )
        portable_seen.add(portable_name)
        (directory_names if info.is_dir() else file_names).add(normalized)
        if stat.S_ISLNK((info.external_attr >> 16) & 0o170000):
            raise SystemExit(
                "Zip 安装包包含符号链接。"
                if os.environ.get("TENETORA_LANG") == "zh"
                else "Zip package contains a symbolic link."
            )
    if file_names & directory_names or any(
        any(parent.as_posix() in file_names for parent in PurePosixPath(name).parents)
        for name in file_names
    ):
        raise SystemExit(
            "Zip 安装包包含文件与目录路径冲突。"
            if os.environ.get("TENETORA_LANG") == "zh"
            else "Zip package contains a file and directory path collision."
        )
    if len(roots) != 1 or (not allow_source_archive and roots != {"tenetora"}):
        raise SystemExit(
            "Zip 安装包根目录无效。"
            if os.environ.get("TENETORA_LANG") == "zh"
            else "Zip package root is invalid."
        )
    archive.extractall(extract_dir)
    if os.name != "nt":
        for info in archive.infolist():
            if info.is_dir():
                continue
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                (extract_dir / info.filename).chmod(mode)

candidates = []
for path in [extract_dir, *extract_dir.iterdir()]:
    if path.is_dir() and any(
        (path / "skills" / skill / "SKILL.md").is_file()
        for skill in ("tenetora", "agent-harness")
    ):
        candidates.append(path)
if not candidates:
    message = "Zip package does not contain a Tenetora lifecycle router skill"
    if os.environ.get("TENETORA_LANG") == "zh":
        message = "Zip 安装包缺少 Tenetora lifecycle router skill"
    raise SystemExit(message)
print(candidates[0])
PY
}

prepare_current_links() {
  local current_source="$1"
  local source_parent="${HARNESS_HOME}/source"
  local canonical_source="${source_parent}/tenetora"
  local legacy_source="${source_parent}/agent-harness"
  local current_link="${HARNESS_HOME}/current"
  local canonical_is_current=0

  managed_package_directory() {
    local candidate="$1"
    [[ -d "$candidate" ]] || return 1
    [[ -f "${candidate}/skills/tenetora/SKILL.md" \
      || -f "${candidate}/skills/agent-harness/SKILL.md" \
      || -f "${candidate}/skills/tenetora/VERSION" \
      || -f "${candidate}/skills/agent-harness/VERSION" ]]
  }

  mkdir -p "$source_parent"
  if [[ -e "$current_link" || -L "$current_link" ]]; then
    if ! managed_package_directory "$current_link"; then
      warn \
        "Refusing to replace current because Tenetora ownership cannot be proven: ${current_link}" \
        "current 归属无法证明，拒绝替换：${current_link}"
      return 1
    fi
    if [[ ! -L "$current_link" ]]; then
      local current_backup="${current_link}.bak-$(date +%Y%m%d%H%M%S)"
      mv "$current_link" "$current_backup"
      say "Legacy current: moved ${current_link} -> ${current_backup}" "旧版 current 已移动：${current_link} -> ${current_backup}"
    fi
  fi
  if [[ -L "$legacy_source" ]]; then
    if managed_package_directory "$legacy_source"; then
      rm "$legacy_source"
    else
      warn \
        "Legacy source entry was preserved because ownership is ambiguous: ${legacy_source}" \
        "旧版 source 入口归属不明确，已保留：${legacy_source}"
    fi
  elif [[ -e "$legacy_source" ]]; then
    warn \
      "Legacy source entry was preserved because ownership is ambiguous: ${legacy_source}" \
      "旧版 source 入口归属不明确，已保留：${legacy_source}"
  fi
  if [[ -L "$canonical_source" ]] && ! managed_package_directory "$canonical_source"; then
    canonical_is_current=1
    warn \
      "Canonical source entry was preserved because ownership is ambiguous: ${canonical_source}" \
      "canonical source 入口归属不明确，已保留：${canonical_source}"
  elif [[ -e "$canonical_source" && ! -L "$canonical_source" ]]; then
    if [[ "$(canonical_dir "$canonical_source")" == "$(canonical_dir "$current_source")" ]]; then
      canonical_is_current=1
    elif [[ ! -f "${canonical_source}/skills/tenetora/SKILL.md" \
      && ! -f "${canonical_source}/skills/agent-harness/SKILL.md" ]]; then
      canonical_is_current=1
      warn \
        "Canonical source entry was preserved because ownership is ambiguous: ${canonical_source}" \
        "canonical source 入口归属不明确，已保留：${canonical_source}"
    else
      local backup_root="${HARNESS_HOME}/backups/source-links/$(date +%Y%m%d%H%M%S)"
      mkdir -p "$backup_root"
      mv "$canonical_source" "${backup_root}/tenetora"
      say \
        "Managed source: backed up ${canonical_source} -> ${backup_root}/tenetora" \
        "托管 source 已备份：${canonical_source} -> ${backup_root}/tenetora"
    fi
  fi
  ln -sfn "$current_source" "$current_link"
  if [[ "$canonical_is_current" != "1" ]]; then
    ln -sfn "$current_source" "$canonical_source"
  fi
}

snapshot_managed_runtime() {
  local snapshot="$1"
  "$PYTHON_BIN" "${SOURCE_DIR}/skills/tenetora/scripts/runtime_transaction.py" \
    --snapshot \
    --bin-dir "${HARNESS_HOME}/bin" \
    --journal "$snapshot" \
    --json >/dev/null
}

restore_managed_runtime() {
  local snapshot="$1"
  local output
  if output="$("$PYTHON_BIN" "${SOURCE_DIR}/skills/tenetora/scripts/runtime_transaction.py" \
    --restore \
    --bin-dir "${HARNESS_HOME}/bin" \
    --journal "$snapshot" \
    --json 2>&1)"; then
    return 0
  fi
  [[ -n "$output" ]] && printf '%s\n' "$output" >&2
  return 1
}

restore_previous_release() {
  if [[ -z "$PREVIOUS_RELEASE_BACKUP" ]]; then
    return 0
  fi
  if [[ ! -e "$PREVIOUS_RELEASE_BACKUP" && ! -L "$PREVIOUS_RELEASE_BACKUP" ]]; then
    return 1
  fi
  if [[ -e "$STAGED_RELEASE_DIR" || -L "$STAGED_RELEASE_DIR" ]]; then
    rm -rf "$STAGED_RELEASE_DIR"
  fi
  mkdir -p "$(dirname "$STAGED_RELEASE_DIR")"
  mv "$PREVIOUS_RELEASE_BACKUP" "$STAGED_RELEASE_DIR"
}

choose_language "$@"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --lang)
      need_value "$1" "${2:-}"
      shift 2
      ;;
    --help)
      usage
      exit 0
      ;;
    --zip-url)
      need_value "$1" "${2:-}"
      ZIP_URL="$2"
      ZIP_URL_EXPLICIT=1
      shift 2
      ;;
    --zip-file)
      need_value "$1" "${2:-}"
      ZIP_FILE="$2"
      shift 2
      ;;
    --sha256)
      need_value "$1" "${2:-}"
      ZIP_SHA256="$2"
      shift 2
      ;;
    --home)
      need_value "$1" "${2:-}"
      HARNESS_HOME="$2"
      shift 2
      ;;
    --source-dir)
      need_value "$1" "${2:-}"
      SOURCE_DIR="$2"
      shift 2
      ;;
    -t|--tools)
      need_value "$1" "${2:-}"
      TOOLS="$2"
      shift 2
      ;;
    -g|--global)
      SCOPE="global"
      SCOPE_EXPLICIT=1
      shift
      ;;
    -i|--in-project)
      SCOPE="project"
      SCOPE_EXPLICIT=1
      shift
      ;;
    -b|--both)
      SCOPE="both"
      SCOPE_EXPLICIT=1
      shift
      ;;
    -s|--scope)
      need_value "$1" "${2:-}"
      SCOPE="$2"
      SCOPE_EXPLICIT=1
      shift 2
      ;;
    -p|--path)
      need_value "$1" "${2:-}"
      PROJECT_PATH="$2"
      shift 2
      ;;
    -m|--mode)
      need_value "$1" "${2:-}"
      MODE="$2"
      shift 2
      ;;
    -f|--force)
      FORCE=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --allow-skills-only)
      ALLOW_SKILLS_ONLY=1
      shift
      ;;
    --require-full)
      REQUIRE_FULL=1
      shift
      ;;
    --codex-hooks)
      need_value "$1" "${2:-}"
      CODEX_HOOKS="$2"
      shift 2
      ;;
    --allow-tracked-codex-hooks)
      ALLOW_TRACKED_CODEX_HOOKS=1
      shift
      ;;
    --prune-shadowed)
      PRUNE_SHADOWED=1
      shift
      ;;
    --no-prune-shadowed)
      NO_PRUNE_SHADOWED=1
      shift
      ;;
    --progress)
      need_value "$1" "${2:-}"
      PROGRESS_MODE="$2"
      shift 2
      ;;
    --no-progress)
      PROGRESS_MODE="never"
      shift
      ;;
    --verbose)
      VERBOSE=1
      shift
      ;;
    --no-cli)
      INSTALL_CLI=0
      shift
      ;;
    --repo|--ref)
      die "$(i18n_text "$1 is no longer supported. Tenetora installs from release zip packages; use --zip-url, --zip-file, or developer-only --source-dir." "$1 已不再支持。Tenetora 使用 release zip 安装，请改用 --zip-url、--zip-file 或仅供开发的 --source-dir。")"
      ;;
    *)
      unknown_arg "$1"
      ;;
  esac
done

validate_package_source_selection

if [[ -z "$HARNESS_HOME" ]]; then
  if [[ -z "$USER_HOME" ]]; then
    die "$(i18n_text "HOME is required unless TENETORA_HOME is set" "必须设置 HOME，或显式设置 TENETORA_HOME")"
  fi
  HARNESS_HOME="${USER_HOME}/.tenetora"
elif [[ -n "$USER_HOME" && "$HARNESS_HOME" == "$USER_HOME/.agent-harness" ]]; then
  HARNESS_HOME="$USER_HOME/.tenetora"
fi

validate_choice "scope" "$SCOPE" "global,project,both"
validate_choice "mode" "$MODE" "auto,symlink,copy"
validate_choice "codex hooks" "$CODEX_HOOKS" "auto,native,project,off"
validate_choice "progress" "$PROGRESS_MODE" "auto,always,never"
if [[ "$ALLOW_SKILLS_ONLY" == "1" && "$REQUIRE_FULL" == "1" ]]; then
  die "$(i18n_text "--allow-skills-only and --require-full cannot be used together" "--allow-skills-only 与 --require-full 不能同时使用")"
fi

PYTHON_BIN="$(find_python)"
PYTHON_VERSION="$($PYTHON_BIN -c 'import platform; print(platform.python_version())')"
if ! HOME_VALIDATION_ERROR="$(validate_managed_home_path "$HARNESS_HOME" 2>&1)"; then
  die "$(i18n_text "Unsafe Tenetora home: ${HOME_VALIDATION_ERROR}" "Tenetora 主目录不安全：${HOME_VALIDATION_ERROR}")"
fi
HARNESS_HOME="$($PYTHON_BIN -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).expanduser().resolve(strict=False))' "$HARNESS_HOME")"
if [[ -n "$USER_HOME" ]]; then
  DEFAULT_HARNESS_HOME="$($PYTHON_BIN -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).expanduser().resolve(strict=False))' "$USER_HOME/.tenetora")"
else
  DEFAULT_HARNESS_HOME=""
fi
export TENETORA_HOME="$HARNESS_HOME"

write_language_preference() {
  [[ "$DRY_RUN" == "1" ]] && return 0
  local preferences_dir="${HARNESS_HOME}/state"
  local preferences_path="${preferences_dir}/preferences.json"
  local temporary_path="${preferences_path}.tmp.$$"
  mkdir -p "$preferences_dir" || return 0
  printf '{"language":"%s","version":1}\n' "$INSTALL_LANG" >"$temporary_path" || return 0
  chmod 600 "$temporary_path" 2>/dev/null || true
  mv -f "$temporary_path" "$preferences_path" || rm -f "$temporary_path"
}

RUN_ID="$(date -u +%Y%m%d-%H%M%S)-$$"
LOG_DIR="${HARNESS_HOME}/logs"
LOG_FILE="${LOG_DIR}/install-${RUN_ID}.log"
EVENT_FILE="${LOG_DIR}/install-${RUN_ID}.jsonl"
SOURCE_CHANGED=0
BEFORE_ID=""
AFTER_ID=""
BEFORE_VERSION=""
AFTER_VERSION=""
OPERATION="install"
SOURCE_ARCHIVE_FALLBACK=0

say "Tenetora online installer" "Tenetora 在线安装器"
say "Runtime: Python ${PYTHON_VERSION} (${PYTHON_BIN})" "运行时：Python ${PYTHON_VERSION}（${PYTHON_BIN}）"
echo

if [[ -n "$SOURCE_DIR" ]]; then
  say "[1/6] Resolving package from local source directory..." "[1/6] 正在从本地源码目录解析安装包..."
  if [[ ! -d "$SOURCE_DIR" ]]; then
    die "$(i18n_text "Source directory does not exist: $SOURCE_DIR" "源码目录不存在：$SOURCE_DIR")"
  fi
  if ! validate_source_tree "$SOURCE_DIR"; then
    die "$(i18n_text "Source directory contains a symbolic link, junction, or non-regular file: $SOURCE_DIR" "源码目录包含符号链接、junction 或非普通文件：$SOURCE_DIR")"
  fi
  SOURCE_DIR="$(canonical_dir "$SOURCE_DIR")"
else
  say "[1/6] Resolving package from zip..." "[1/6] 正在从 zip 解析安装包..."

  if [[ "$DRY_RUN" == "1" ]]; then
    if [[ -n "$ZIP_FILE" ]]; then
      if [[ ! -f "$ZIP_FILE" ]]; then
        die "$(i18n_text "Zip file does not exist: $ZIP_FILE" "Zip 文件不存在：$ZIP_FILE")"
      fi
      package_zip="$(canonical_dir "$(dirname "$ZIP_FILE")")/$(basename "$ZIP_FILE")"
      actual_sha="$(zip_sha256 "$package_zip")"
      if [[ -n "$ZIP_SHA256" && "$ZIP_SHA256" != "$actual_sha" ]]; then
        die "$(i18n_text "Zip SHA-256 mismatch: expected ${ZIP_SHA256}, got ${actual_sha}" "Zip SHA-256 不匹配：期望 ${ZIP_SHA256}，实际 ${actual_sha}")"
      fi
      say "would install zip file ${ZIP_FILE}" "将安装 zip 文件 ${ZIP_FILE}"
    else
      say "would download ${ZIP_URL}" "将下载 ${ZIP_URL}"
      if [[ -e "${HARNESS_HOME}/current" || -L "${HARNESS_HOME}/current" ]]; then
        if ! resolved_current_source="$(resolve_managed_current_source "${HARNESS_HOME}")"; then
          die "$(i18n_text "Existing managed current release cannot be used for dry-run." "现有受管 current release 无法用于 dry-run。")"
        fi
        SOURCE_DIR="$resolved_current_source"
      fi
    fi
    if [[ -z "$SOURCE_DIR" ]]; then
      SOURCE_DIR="${HARNESS_HOME}/current"
    fi
  else
    tmp_dir="$(mktemp -d)"
    TEMP_DIRS+=("$tmp_dir")
    package_zip="${tmp_dir}/tenetora.zip"
    if [[ -n "$ZIP_FILE" ]]; then
      if [[ ! -f "$ZIP_FILE" ]]; then
        die "$(i18n_text "Zip file does not exist: $ZIP_FILE" "Zip 文件不存在：$ZIP_FILE")"
      fi
      package_zip="$(canonical_dir "$(dirname "$ZIP_FILE")")/$(basename "$ZIP_FILE")"
      say "Package: local ${package_zip}" "安装包：本地 ${package_zip}"
    else
      if download_zip "$ZIP_URL" "$package_zip"; then
        if [[ "$ZIP_URL_EXPLICIT" == "0" && "$ZIP_URL" == "$DEFAULT_ZIP_URL" ]]; then
          external_manifest="${tmp_dir}/external-manifest.json"
          if download_zip "$MANIFEST_URL" "$external_manifest"; then
            external_manifest_sha="$(manifest_sha256 "$external_manifest")"
            if [[ -z "$external_manifest_sha" ]]; then
              die "$(i18n_text "The latest release manifest has no valid SHA256." "latest release manifest 没有有效 SHA256。")"
            fi
          else
            warn \
              "Package: latest release manifest unavailable; falling back to ${DEFAULT_SOURCE_ZIP_URL}" \
              "安装包：latest release manifest 不可用，回退到 ${DEFAULT_SOURCE_ZIP_URL}"
            rm -f "$package_zip"
            if ! download_zip "$DEFAULT_SOURCE_ZIP_URL" "$package_zip"; then
              die "$(i18n_text "Failed to download the source fallback package." "无法下载源码回退包。")"
            fi
            SOURCE_ARCHIVE_FALLBACK=1
            external_manifest_sha=""
            say "Package: fallback downloaded ${DEFAULT_SOURCE_ZIP_URL}" "安装包：已下载回退源码 ${DEFAULT_SOURCE_ZIP_URL}"
          fi
        fi
        if [[ "$SOURCE_ARCHIVE_FALLBACK" != "1" ]]; then
          say "Package: downloaded ${ZIP_URL}" "安装包：已下载 ${ZIP_URL}"
        fi
      elif [[ "$ZIP_URL_EXPLICIT" == "0" && "$ZIP_URL" == "$DEFAULT_ZIP_URL" ]]; then
        warn "Package: latest release zip unavailable; falling back to ${DEFAULT_SOURCE_ZIP_URL}" "安装包：最新 release zip 不可用，回退到 ${DEFAULT_SOURCE_ZIP_URL}"
        rm -f "$package_zip"
        if ! download_zip "$DEFAULT_SOURCE_ZIP_URL" "$package_zip"; then
          die "$(i18n_text "Failed to download default release zip and fallback source zip." "默认 release zip 和回退源码 zip 均下载失败。")"
        fi
        SOURCE_ARCHIVE_FALLBACK=1
        say "Package: fallback downloaded ${DEFAULT_SOURCE_ZIP_URL}" "安装包：已下载回退源码 ${DEFAULT_SOURCE_ZIP_URL}"
      else
        die "$(i18n_text "Failed to download zip: ${ZIP_URL}" "Zip 下载失败：${ZIP_URL}")"
      fi
    fi

    if ! validate_archive_size "$package_zip"; then
      die "$(i18n_text "Zip package exceeds the bounded archive size limit." "Zip 安装包超过有界压缩包大小限制。")"
    fi
    actual_sha="$(zip_sha256 "$package_zip")"
    if [[ -n "$external_manifest_sha" && "$actual_sha" != "$external_manifest_sha" ]]; then
      die "$(i18n_text "Release ZIP does not match its external manifest SHA256." "Release ZIP 与外部 manifest 的 SHA256 不匹配。")"
    fi
    if [[ -n "$ZIP_SHA256" && "$ZIP_SHA256" != "$actual_sha" ]]; then
      die "$(i18n_text "Zip SHA-256 mismatch: expected ${ZIP_SHA256}, got ${actual_sha}" "Zip SHA-256 不匹配：期望 ${ZIP_SHA256}，实际 ${actual_sha}")"
    fi
    say "SHA256: ${actual_sha}" "SHA256：${actual_sha}"

    extracted_source="$(extract_zip "$package_zip" "${tmp_dir}/extract" "$SOURCE_ARCHIVE_FALLBACK")"
    if [[ -n "$external_manifest_sha" ]]; then
      if ! validate_external_manifest "$external_manifest" "$extracted_source" "$actual_sha"; then
        die "$(i18n_text "The external release manifest does not authenticate the downloaded package." "外部 release manifest 无法验证下载的安装包。")"
      fi
    fi
    package_version="$(source_version "$extracted_source")"
    validate_package_version "$package_version"
    STAGED_RELEASE_DIR="${HARNESS_HOME}/releases/${package_version}"
    SOURCE_DIR="$(canonical_dir "$extracted_source")"
    package_protocol="$(source_installer_protocol "$SOURCE_DIR")"
    missing_package_paths="$(package_contract_missing "$SOURCE_DIR")"
    if [[ -n "$missing_package_paths" ]]; then
      if is_default_latest_package; then
        warn \
          "Package: latest release package is incomplete; falling back to ${DEFAULT_SOURCE_ZIP_URL}" \
          "安装包：latest release 包不完整，回退到 ${DEFAULT_SOURCE_ZIP_URL}"
        fallback_zip="${tmp_dir}/tenetora-source.zip"
        if ! download_zip "$DEFAULT_SOURCE_ZIP_URL" "$fallback_zip"; then
          die "$(i18n_text "Failed to download incomplete-package fallback source zip." "无法下载不完整安装包的回退源码 zip。")"
        fi
        if ! validate_archive_size "$fallback_zip"; then
          die "$(i18n_text "Fallback source zip exceeds the bounded archive size limit." "回退源码 zip 超过有界压缩包大小限制。")"
        fi
        actual_sha="$(zip_sha256 "$fallback_zip")"
        package_zip="$fallback_zip"
        SOURCE_ARCHIVE_FALLBACK=1
        say "Package: fallback downloaded ${DEFAULT_SOURCE_ZIP_URL}" "安装包：已下载回退源码 ${DEFAULT_SOURCE_ZIP_URL}"
        say "SHA256: ${actual_sha}" "SHA256：${actual_sha}"
        extracted_source="$(extract_zip "$fallback_zip" "${tmp_dir}/contract-fallback" "1")"
        SOURCE_DIR="$(canonical_dir "$extracted_source")"
        package_version="$(source_version "$SOURCE_DIR")"
        validate_package_version "$package_version"
        package_protocol="$(source_installer_protocol "$SOURCE_DIR")"
        STAGED_RELEASE_DIR="${HARNESS_HOME}/releases/${package_version}"
        missing_package_paths="$(package_contract_missing "$SOURCE_DIR")"
      fi
      if [[ -n "$missing_package_paths" ]]; then
        missing_package_label="$(i18n_text \
          "Package is missing required installer files:" \
          "安装包缺少必需的安装文件：")"
        die "${missing_package_label}
${missing_package_paths}"
      fi
    fi
    if (( package_protocol < REQUIRED_INSTALLER_PROTOCOL )); then
      if is_default_latest_package; then
        warn \
          "Package: latest release installer protocol ${package_protocol} is older than required ${REQUIRED_INSTALLER_PROTOCOL}; falling back to ${DEFAULT_SOURCE_ZIP_URL}" \
          "安装包：latest release 的安装协议 ${package_protocol} 低于所需版本 ${REQUIRED_INSTALLER_PROTOCOL}，回退到 ${DEFAULT_SOURCE_ZIP_URL}"
        fallback_zip="${tmp_dir}/tenetora-source.zip"
        if ! download_zip "$DEFAULT_SOURCE_ZIP_URL" "$fallback_zip"; then
          die "$(i18n_text "Failed to download protocol-compatible fallback source zip." "无法下载协议兼容的回退源码 zip。")"
        fi
        if ! validate_archive_size "$fallback_zip"; then
          die "$(i18n_text "Fallback source zip exceeds the bounded archive size limit." "回退源码 zip 超过有界压缩包大小限制。")"
        fi
        actual_sha="$(zip_sha256 "$fallback_zip")"
        package_zip="$fallback_zip"
        SOURCE_ARCHIVE_FALLBACK=1
        say "Package: fallback downloaded ${DEFAULT_SOURCE_ZIP_URL}" "安装包：已下载回退源码 ${DEFAULT_SOURCE_ZIP_URL}"
        say "SHA256: ${actual_sha}" "SHA256：${actual_sha}"
        extracted_source="$(extract_zip "$fallback_zip" "${tmp_dir}/protocol-fallback" "1")"
        SOURCE_DIR="$(canonical_dir "$extracted_source")"
        package_version="$(source_version "$SOURCE_DIR")"
        validate_package_version "$package_version"
        package_protocol="$(source_installer_protocol "$SOURCE_DIR")"
        STAGED_RELEASE_DIR="${HARNESS_HOME}/releases/${package_version}"
        missing_package_paths="$(package_contract_missing "$SOURCE_DIR")"
        if [[ -n "$missing_package_paths" ]]; then
          die "$(i18n_text \
            "Package is missing required installer files:" \
            "安装包缺少必需的安装文件：")
${missing_package_paths}"
        fi
      fi
    fi
    if (( package_protocol < REQUIRED_INSTALLER_PROTOCOL )); then
      die "$(i18n_text \
        "Package installer protocol ${package_protocol} is incompatible with required protocol ${REQUIRED_INSTALLER_PROTOCOL}; use a matching or newer Tenetora package." \
        "安装包协议 ${package_protocol} 与所需协议 ${REQUIRED_INSTALLER_PROTOCOL} 不兼容；请使用匹配或更新的 Tenetora 安装包。")"
    fi
    stamp_manifest_sha "$SOURCE_DIR" "$actual_sha"
  fi
fi

if [[ "$DRY_RUN" != "1" && ! -f "${SOURCE_DIR}/skills/tenetora/SKILL.md" && ! -f "${SOURCE_DIR}/skills/agent-harness/SKILL.md" ]]; then
  die "$(i18n_text "Source directory is not a Tenetora package: $SOURCE_DIR" "源码目录不是有效的 Tenetora 安装包：$SOURCE_DIR")"
fi

if [[ "$DRY_RUN" != "1" && -n "$STAGED_RELEASE_DIR" ]]; then
  RELEASE_STORAGE_DIR="$(dirname "$STAGED_RELEASE_DIR")"
  if ! RELEASE_STORAGE_ERROR="$(validate_managed_home_path "$RELEASE_STORAGE_DIR" 2>&1)"; then
    die "$(i18n_text "Unsafe Tenetora release storage: ${RELEASE_STORAGE_ERROR}" "Tenetora release 存储目录不安全：${RELEASE_STORAGE_ERROR}")"
  fi
fi

LEGACY_HARNESS_HOME=""
if [[ -n "$USER_HOME" ]]; then
  LEGACY_HARNESS_HOME="$USER_HOME/.agent-harness"
fi
MIGRATION_SCRIPT="${SOURCE_DIR}/scripts/migrate_machine_home.py"
if [[ "$DRY_RUN" != "1" && "$HARNESS_HOME" == "$DEFAULT_HARNESS_HOME" ]]; then
  # The package installer performs machine-home migration only after native
  # plugin convergence succeeds. Moving the legacy home here would invalidate
  # Codex marketplace ownership evidence before recovery can run.
  mkdir -p "$HARNESS_HOME"
  HARNESS_HOME="$(canonical_dir "$HARNESS_HOME")"
  export TENETORA_HOME="$HARNESS_HOME"
elif [[ "$DRY_RUN" != "1" ]]; then
  mkdir -p "$HARNESS_HOME"
  HARNESS_HOME="$(canonical_dir "$HARNESS_HOME")"
  export TENETORA_HOME="$HARNESS_HOME"
fi

if [[ "$DRY_RUN" != "1" ]]; then
  if ! start_install_lock; then
    die "$(i18n_text "Another Tenetora installation is active or the machine lock could not be acquired." "另一个 Tenetora 安装事务正在执行，或无法获取机器安装锁。")"
  fi
fi

if [[ -L "${HARNESS_HOME}/current" || -d "${HARNESS_HOME}/current" ]]; then
  BEFORE_ID="$(source_identity "${HARNESS_HOME}/current" 2>/dev/null || true)"
  BEFORE_VERSION="$(source_version "${HARNESS_HOME}/current" 2>/dev/null || true)"
elif [[ -d "${HARNESS_HOME}/source/tenetora" ]]; then
  BEFORE_ID="$(source_identity "${HARNESS_HOME}/source/tenetora" 2>/dev/null || true)"
  BEFORE_VERSION="$(source_version "${HARNESS_HOME}/source/tenetora" 2>/dev/null || true)"
elif [[ -d "${HARNESS_HOME}/source/agent-harness" ]]; then
  BEFORE_ID="$(source_identity "${HARNESS_HOME}/source/agent-harness" 2>/dev/null || true)"
  BEFORE_VERSION="$(source_version "${HARNESS_HOME}/source/agent-harness" 2>/dev/null || true)"
fi
if [[ -n "$BEFORE_ID" && "$SCOPE_EXPLICIT" != "1" ]]; then
  OPERATION="upgrade"
fi

DISCOVERY_SCRIPT="${SOURCE_DIR}/scripts/install-skill.py"
if [[ -f "$DISCOVERY_SCRIPT" ]]; then
  DISCOVERY_ARGS=(
    "$DISCOVERY_SCRIPT"
    "--discover-existing"
    "--json"
    "--tools" "$TOOLS"
    "--path" "$PROJECT_PATH"
  )
  if [[ "$SCOPE_EXPLICIT" != "1" ]]; then
    DISCOVERY_ARGS+=("--all-existing")
    DISCOVERY_ARGS+=("--auto-discover")
  fi
  if [[ "$SCOPE_EXPLICIT" == "1" ]]; then
    case "$SCOPE" in
      global) DISCOVERY_ARGS+=("-g") ;;
      project) DISCOVERY_ARGS+=("-i") ;;
      both) DISCOVERY_ARGS+=("-b") ;;
    esac
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    DISCOVERY_ARGS+=("--dry-run")
  fi
  DISCOVERY_JSON="$("$PYTHON_BIN" "${DISCOVERY_ARGS[@]}")"
  DISCOVERED_WORK="$("$PYTHON_BIN" -c 'import json, sys; payload=json.load(sys.stdin); print(int(payload.get("work_count", payload.get("surface_count", 0))))' <<<"$DISCOVERY_JSON")"
  if [[ "$DISCOVERED_WORK" -gt 0 ]]; then
    OPERATION="upgrade"
  fi
fi

if [[ "$OPERATION" == "upgrade" ]]; then
  say "Operation: upgrade existing installations" "操作：升级现有安装"
else
  say "Operation: install" "操作：安装"
fi

# Bridge-capable release packages delegate both fresh installs and upgrades to
# the versioned Python worker. The shell remains only the acquisition/bootstrap
# boundary; source checkouts without a release manifest keep the developer path.
BRIDGE_PROTOCOL="$(source_bridge_protocol "$SOURCE_DIR")"
if [[ "$BRIDGE_PROTOCOL" -ge 1 ]]; then
  BRIDGE_WORKER="${SOURCE_DIR}/skills/tenetora/scripts/upgrade_skill.py"
  if [[ ! -f "$BRIDGE_WORKER" ]]; then
    die "$(i18n_text "Bridge-capable package is missing its upgrade worker." "支持 bridge 的安装包缺少升级 worker。")"
  fi
  BRIDGE_ARGS=(
    "$BRIDGE_WORKER"
    "--bootstrap-bridge"
    "--bridge-source-version" "${BEFORE_VERSION:-0.0.0}"
    "--path" "$PROJECT_PATH"
    "--tools" "$TOOLS"
    "--mode" "$MODE"
    "--codex-hooks" "$CODEX_HOOKS"
    "--progress" "$PROGRESS_MODE"
    "--configure-path"
    "--log-file" "$LOG_FILE"
    "--events-jsonl" "$EVENT_FILE"
  )
  if [[ "$OPERATION" != "upgrade" ]]; then
    BRIDGE_ARGS+=("--bootstrap-install")
  fi
  if [[ -n "${package_zip:-}" && -f "$package_zip" ]]; then
    BRIDGE_ARGS+=("--zip-file" "$package_zip" "--sha256" "$actual_sha")
  else
    BRIDGE_ARGS+=("--source-dir" "$SOURCE_DIR")
  fi
  if [[ "$SCOPE_EXPLICIT" == "1" ]]; then
    BRIDGE_ARGS+=("--scope" "$SCOPE")
  fi
  [[ "$FORCE" == "1" ]] && BRIDGE_ARGS+=("--force")
  [[ "$INSTALL_CLI" == "0" ]] && BRIDGE_ARGS+=("--no-cli")
  [[ "$ALLOW_SKILLS_ONLY" == "1" ]] && BRIDGE_ARGS+=("--allow-skills-only")
  [[ "$REQUIRE_FULL" == "1" ]] && BRIDGE_ARGS+=("--require-full")
  [[ "$ALLOW_TRACKED_CODEX_HOOKS" == "1" ]] && BRIDGE_ARGS+=("--allow-tracked-codex-hooks")
  [[ "$PRUNE_SHADOWED" == "1" ]] && BRIDGE_ARGS+=("--prune-shadowed")
  [[ "$NO_PRUNE_SHADOWED" == "1" ]] && BRIDGE_ARGS+=("--no-prune-shadowed")
  [[ "$VERBOSE" == "1" ]] && BRIDGE_ARGS+=("--verbose")
  [[ "$DRY_RUN" == "1" ]] && BRIDGE_ARGS+=("--dry-run")
  AFTER_ID="$(source_identity "$SOURCE_DIR")"
  AFTER_VERSION="$(source_version "$SOURCE_DIR")"
  if [[ -n "$BEFORE_ID" && "$BEFORE_ID" != "$AFTER_ID" ]]; then
    say "Package: updated ${BEFORE_ID} -> ${AFTER_ID}" "安装包：已更新 ${BEFORE_ID} -> ${AFTER_ID}"
  elif [[ -z "$BEFORE_ID" ]]; then
    say "Package: installed ${AFTER_ID}" "安装包：已安装 ${AFTER_ID}"
  else
    say "Package: current ${AFTER_ID}" "安装包：已是当前版本 ${AFTER_ID}"
  fi
  if [[ -n "$BEFORE_VERSION" && "$BEFORE_VERSION" != "$AFTER_VERSION" ]]; then
    say "Version: ${BEFORE_VERSION} -> ${AFTER_VERSION}" "版本：${BEFORE_VERSION} -> ${AFTER_VERSION}"
  else
    say "Version: ${AFTER_VERSION}" "版本：${AFTER_VERSION}"
  fi
  set +e
  TENETORA_OUTER_INSTALLER=1 "$PYTHON_BIN" "${BRIDGE_ARGS[@]}"
  BRIDGE_STATUS=$?
  set -e
  INSTALL_CAPABILITY_RESULT=""
  if [[ -f "$EVENT_FILE" ]]; then
    INSTALL_CAPABILITY_RESULT="$("$PYTHON_BIN" -c '
import json, sys
status = ""
with open(sys.argv[1], encoding="utf-8") as handle:
    for raw in handle:
        try:
            event = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if event.get("event") == "complete":
            status = str(event.get("status") or "")
print(status)
' "$EVENT_FILE" 2>/dev/null || true)"
  fi
  if [[ "$BRIDGE_STATUS" -eq 0 ]]; then
    write_language_preference
    say "[4/6] Bootstrapping CLI..." "[4/6] 正在自举 CLI..."
    say "CLI: ready" "CLI：已就绪"
    say "[5/6] Summarizing result..." "[5/6] 正在汇总结果..."
    if [[ "$INSTALL_CAPABILITY_RESULT" == "PENDING_TRUST" ]]; then
      if [[ "$OPERATION" == "upgrade" ]]; then
        say "[6/6] Upgrade succeeded; trust or restart required" "[6/6] 升级成功；需完成信任或重启"
      else
        say "[6/6] Installation succeeded; trust or restart required" "[6/6] 安装成功；需完成信任或重启"
      fi
      say "Final result: success with follow-up" "最终结果：成功，等待激活"
      say "Complete the trust or restart action shown above; no reinstall is required." "请完成上方的信任或重启操作，无需重新安装。"
    elif [[ "$OPERATION" == "upgrade" ]]; then
      say "[6/6] Upgrade succeeded" "[6/6] 升级成功"
      say "Final result: success" "最终结果：成功"
    else
      say "[6/6] Installation succeeded" "[6/6] 安装成功"
      say "Final result: success" "最终结果：成功"
    fi
  elif [[ "$BRIDGE_STATUS" -eq 2 ]]; then
    say \
      "Rollback boundary: CLI/runtime/source/Git hooks were restored; lifecycle skill or host plugin surfaces that completed before the failure may already use the new version. Rerun the ordinary upgrade to converge them." \
      "回滚边界：CLI/runtime/source/Git hooks 已恢复；失败前已完成的 lifecycle skill 或宿主插件安装面可能已是新版本。请重新执行普通升级完成收敛。"
    if [[ "$OPERATION" == "upgrade" ]]; then
      say "[6/6] Upgrade partially failed" "[6/6] 升级部分失败"
    else
      say "[6/6] Installation partially failed" "[6/6] 安装部分失败"
    fi
    say "Final result: partial failure" "最终结果：部分失败"
    say "Review the failure details above before retrying." "重试前请检查上方失败详情。"
    say "Detailed log: ${LOG_FILE}" "详细日志：${LOG_FILE}"
    if [[ -f "$EVENT_FILE" ]]; then
      say "Structured events: ${EVENT_FILE}" "结构化事件：${EVENT_FILE}"
    fi
  elif [[ "$BRIDGE_STATUS" -eq 3 ]]; then
    say "Preflight: failed" "预检：失败"
    if [[ "$OPERATION" == "upgrade" ]]; then
      say "[6/6] Upgrade failed during preflight" "[6/6] 升级在预检阶段失败"
    else
      say "[6/6] Installation failed during preflight" "[6/6] 安装在预检阶段失败"
    fi
    say "Final result: failed; no installation changes were applied" "最终结果：失败；未写入安装变更"
    BRIDGE_STATUS=1
  else
    if [[ "$OPERATION" == "upgrade" ]]; then
      say "[6/6] Upgrade failed" "[6/6] 升级失败"
    else
      say "[6/6] Installation failed" "[6/6] 安装失败"
    fi
    say "Final result: failed" "最终结果：失败"
  fi
  exit "$BRIDGE_STATUS"
fi

say "[2/6] Checking environment and planned capabilities..." "[2/6] 正在检查环境和计划安装能力..."
PREFLIGHT_SCRIPT="${SOURCE_DIR}/scripts/install-skill.py"
PREFLIGHT_ARGS=(
  "$PREFLIGHT_SCRIPT"
  "--preflight"
  "--compact"
  "--progress" "$PROGRESS_MODE"
  "--log-file" "$LOG_FILE"
  "--events-jsonl" "$EVENT_FILE"
  "--tools" "$TOOLS"
  "--path" "$PROJECT_PATH"
  "--codex-hooks" "$CODEX_HOOKS"
)
if [[ "$VERBOSE" == "1" ]]; then
  PREFLIGHT_ARGS+=("--verbose")
fi
if [[ "$OPERATION" == "upgrade" ]]; then
  PREFLIGHT_ARGS+=("--existing-only")
  if [[ "$SCOPE_EXPLICIT" != "1" ]]; then
    PREFLIGHT_ARGS+=("--all-existing")
    PREFLIGHT_ARGS+=("--auto-discover")
  fi
fi
if [[ "$OPERATION" != "upgrade" || "$SCOPE_EXPLICIT" == "1" ]]; then
  case "$SCOPE" in
    global) PREFLIGHT_ARGS+=("-g") ;;
    project) PREFLIGHT_ARGS+=("-i") ;;
    both) PREFLIGHT_ARGS+=("-b") ;;
  esac
fi
if [[ "$REQUIRE_FULL" == "1" ]]; then
  PREFLIGHT_ARGS+=("--require-full")
fi
if [[ "$ALLOW_SKILLS_ONLY" == "1" ]]; then
  PREFLIGHT_ARGS+=("--allow-skills-only")
fi
if [[ "$FORCE" == "1" ]]; then
  PREFLIGHT_ARGS+=("--force")
fi
if [[ "$ALLOW_TRACKED_CODEX_HOOKS" == "1" ]]; then
  PREFLIGHT_ARGS+=("--allow-tracked-codex-hooks")
fi
if [[ "$PRUNE_SHADOWED" == "1" ]]; then
  PREFLIGHT_ARGS+=("--prune-shadowed")
fi
if [[ "$NO_PRUNE_SHADOWED" == "1" ]]; then
  PREFLIGHT_ARGS+=("--no-prune-shadowed")
fi
PREFLIGHT_STATUS=0
if [[ "$DRY_RUN" == "1" && ! -f "$PREFLIGHT_SCRIPT" ]]; then
  say "would run ${PYTHON_BIN} ${PREFLIGHT_ARGS[*]}" "将执行 ${PYTHON_BIN} ${PREFLIGHT_ARGS[*]}"
else
  set +e
  TENETORA_OUTER_INSTALLER=1 TENETORA_DEFER_MACHINE_HOME_MIGRATION=1 \
    "$PYTHON_BIN" "${PREFLIGHT_ARGS[@]}"
  PREFLIGHT_STATUS=$?
  set -e
fi

if [[ "$PREFLIGHT_STATUS" -ne 0 ]]; then
  if [[ "$OPERATION" == "upgrade" ]]; then
    say "[6/6] Upgrade failed during preflight" "[6/6] 升级在预检阶段失败"
  else
    say "[6/6] Installation failed during preflight" "[6/6] 安装在预检阶段失败"
  fi
  say "Final result: failed; no installation changes were applied" "最终结果：失败；未写入安装变更"
  [[ -f "$LOG_FILE" ]] && say "Detailed log: ${LOG_FILE}" "详细日志：${LOG_FILE}"
  [[ -f "$EVENT_FILE" ]] && say "Structured events: ${EVENT_FILE}" "结构化事件：${EVENT_FILE}"
  exit 1
fi

RUNTIME_SNAPSHOT_ROOT="$(mktemp -d)"
TEMP_DIRS+=("$RUNTIME_SNAPSHOT_ROOT")
MANAGED_RUNTIME_SNAPSHOT="${RUNTIME_SNAPSHOT_ROOT}/managed-runtime-before"
HOOK_ROLLBACK_JOURNAL="${RUNTIME_SNAPSHOT_ROOT}/commit-hooks-before"
RELEASE_ACTIVATION_STATUS=0
INSTALL_STATUS=0
# Snapshot before any release/current/source mutation. The same transaction
# protects fresh installs as well as upgrades from leaving dangling pointers
# when a later bootstrap or activation step fails.
if [[ "$DRY_RUN" != "1" ]]; then
  snapshot_managed_runtime "$MANAGED_RUNTIME_SNAPSHOT"
fi

INSTALL_SCRIPT="${SOURCE_DIR}/scripts/install-skill.py"
ENSURE_CLI="${SOURCE_DIR}/skills/tenetora/scripts/ensure_cli.py"
if [[ "$DRY_RUN" != "1" && ! -f "$INSTALL_SCRIPT" ]]; then
  die "$(i18n_text "Installer script is missing: $INSTALL_SCRIPT" "缺少安装脚本：$INSTALL_SCRIPT")"
fi

HOOK_SNAPSHOT_STATUS=0
if [[ "$DRY_RUN" != "1" && "$OPERATION" == "upgrade" ]]; then
  HOOK_SNAPSHOT_ARGS=(
    "$INSTALL_SCRIPT"
    "--snapshot-hooks"
    "--path" "$PROJECT_PATH"
    "--package-root" "$SOURCE_DIR"
    "--hook-rollback-journal" "$HOOK_ROLLBACK_JOURNAL"
    "--json"
    "--all-existing"
  )
  set +e
  HOOK_SNAPSHOT_OUTPUT="$("$PYTHON_BIN" "${HOOK_SNAPSHOT_ARGS[@]}" 2>&1)"
  HOOK_SNAPSHOT_STATUS=$?
  set -e
  if [[ "$HOOK_SNAPSHOT_STATUS" -ne 0 ]]; then
    [[ -n "$HOOK_SNAPSHOT_OUTPUT" ]] && printf '%s\n' "$HOOK_SNAPSHOT_OUTPUT" >&2
    die "$(i18n_text "Cannot snapshot managed Git hooks before upgrade." "升级前无法快照受管 Git Hook。")"
  fi
fi

if [[ "$DRY_RUN" != "1" && -n "$STAGED_RELEASE_DIR" ]]; then
  RELEASE_STAGE_STATUS=0
  set +e
  RELEASE_STORAGE_DIR="$(dirname "$STAGED_RELEASE_DIR")"
  RELEASE_STORAGE_ERROR="$(validate_managed_home_path "$RELEASE_STORAGE_DIR" 2>&1)"
  if [[ "$?" -ne 0 ]]; then
    RELEASE_STAGE_STATUS=1
  fi
  if [[ -e "$STAGED_RELEASE_DIR" || -L "$STAGED_RELEASE_DIR" ]]; then
    if [[ "$RELEASE_STAGE_STATUS" -eq 0 ]]; then
      PREVIOUS_RELEASE_BACKUP="${RUNTIME_SNAPSHOT_ROOT}/previous-release"
      mv "$STAGED_RELEASE_DIR" "$PREVIOUS_RELEASE_BACKUP"
      RELEASE_STAGE_STATUS=$?
    fi
  fi
  if [[ "$RELEASE_STAGE_STATUS" -eq 0 ]]; then
    mkdir -p "$(dirname "$STAGED_RELEASE_DIR")"
    RELEASE_STAGE_STATUS=$?
  fi
  if [[ "$RELEASE_STAGE_STATUS" -eq 0 ]]; then
    RELEASE_STORAGE_ERROR="$(validate_managed_home_path "$RELEASE_STORAGE_DIR" 2>&1)"
    if [[ "$?" -ne 0 ]]; then
      RELEASE_STAGE_STATUS=1
    fi
  fi
  if [[ "$RELEASE_STAGE_STATUS" -eq 0 ]]; then
    mv "$SOURCE_DIR" "$STAGED_RELEASE_DIR"
    RELEASE_STAGE_STATUS=$?
  fi
  RELEASE_ACTIVATION_STATUS=$RELEASE_STAGE_STATUS
  set -e
  if [[ "$RELEASE_ACTIVATION_STATUS" -eq 0 ]]; then
    SOURCE_DIR="$(canonical_dir "$STAGED_RELEASE_DIR")"
  else
    if [[ -n "$RELEASE_STORAGE_ERROR" ]]; then
      die "$(i18n_text "Unsafe Tenetora release storage: ${RELEASE_STORAGE_ERROR}" "Tenetora release 存储目录不安全：${RELEASE_STORAGE_ERROR}")"
    fi
    say \
      "Package staging failed; the previous release remains recoverable." \
      "安装包暂存失败；此前 release 仍可恢复。"
  fi
fi
MIGRATION_SCRIPT="${SOURCE_DIR}/scripts/migrate_machine_home.py"
INSTALL_SCRIPT="${SOURCE_DIR}/scripts/install-skill.py"
ENSURE_CLI="${SOURCE_DIR}/skills/tenetora/scripts/ensure_cli.py"

if [[ "$DRY_RUN" != "1" ]]; then
  AFTER_ID="$(source_identity "$SOURCE_DIR")"
  AFTER_VERSION="$(source_version "$SOURCE_DIR")"
  if [[ -n "$BEFORE_ID" && "$BEFORE_ID" != "$AFTER_ID" ]]; then
    SOURCE_CHANGED=1
    say "Package: updated ${BEFORE_ID} -> ${AFTER_ID}" "安装包：已更新 ${BEFORE_ID} -> ${AFTER_ID}"
  elif [[ -z "$BEFORE_ID" ]]; then
    SOURCE_CHANGED=1
    say "Package: installed ${AFTER_ID}" "安装包：已安装 ${AFTER_ID}"
  else
    say "Package: current ${AFTER_ID}" "安装包：已是当前版本 ${AFTER_ID}"
  fi
  if [[ -n "$BEFORE_VERSION" && "$BEFORE_VERSION" != "$AFTER_VERSION" ]]; then
    say "Version: ${BEFORE_VERSION} -> ${AFTER_VERSION}" "版本：${BEFORE_VERSION} -> ${AFTER_VERSION}"
  else
    say "Version: ${AFTER_VERSION}" "版本：${AFTER_VERSION}"
  fi
fi

say "[3/6] Installing or updating skills..." "[3/6] 正在安装或升级 skills..."
DISPLAY_SCOPE="$SCOPE"
if [[ "$OPERATION" == "upgrade" && "$SCOPE_EXPLICIT" != "1" ]]; then
  DISPLAY_SCOPE="existing"
fi
say "Tools: ${TOOLS}; scope: ${DISPLAY_SCOPE}; mode: ${MODE}" "工具：${TOOLS}；范围：${DISPLAY_SCOPE}；模式：${MODE}"
INSTALL_ARGS=(
  "$INSTALL_SCRIPT"
  "--compact"
  "--progress" "$PROGRESS_MODE"
  "--log-file" "$LOG_FILE"
  "--events-jsonl" "$EVENT_FILE"
  "--tools" "$TOOLS"
  "--mode" "$MODE"
  "--path" "$PROJECT_PATH"
  "--quiet-preflight"
  "--codex-hooks" "$CODEX_HOOKS"
)
if [[ "$OPERATION" == "upgrade" ]]; then
  INSTALL_ARGS+=("--update")
  if [[ "$SCOPE_EXPLICIT" != "1" ]]; then
    INSTALL_ARGS+=("--all-existing")
    INSTALL_ARGS+=("--auto-discover")
  fi
fi
if [[ "$DRY_RUN" != "1" && -f "${HOOK_ROLLBACK_JOURNAL}/manifest.json" ]]; then
  INSTALL_ARGS+=("--hook-rollback-journal" "$HOOK_ROLLBACK_JOURNAL")
fi
if [[ "$OPERATION" != "upgrade" || "$SCOPE_EXPLICIT" == "1" ]]; then
  case "$SCOPE" in
    global) INSTALL_ARGS+=("-g") ;;
    project) INSTALL_ARGS+=("-i") ;;
    both) INSTALL_ARGS+=("-b") ;;
  esac
fi
if [[ "$FORCE" == "1" ]]; then
  INSTALL_ARGS+=("--force")
fi
if [[ "$DRY_RUN" == "1" ]]; then
  INSTALL_ARGS+=("--dry-run")
fi
if [[ "$ALLOW_SKILLS_ONLY" == "1" ]]; then
  INSTALL_ARGS+=("--allow-skills-only")
fi
if [[ "$REQUIRE_FULL" == "1" ]]; then
  INSTALL_ARGS+=("--require-full")
fi
if [[ "$ALLOW_TRACKED_CODEX_HOOKS" == "1" ]]; then
  INSTALL_ARGS+=("--allow-tracked-codex-hooks")
fi
if [[ "$PRUNE_SHADOWED" == "1" ]]; then
  INSTALL_ARGS+=("--prune-shadowed")
fi
if [[ "$NO_PRUNE_SHADOWED" == "1" ]]; then
  INSTALL_ARGS+=("--no-prune-shadowed")
fi
if [[ "$SOURCE_CHANGED" == "1" ]]; then
  INSTALL_ARGS+=("--source-changed")
fi
if [[ "$VERBOSE" == "1" ]]; then
  INSTALL_ARGS+=("--verbose")
fi

LEGACY_HOME_PRESENT_BEFORE=0
if [[ -n "$LEGACY_HARNESS_HOME" && -d "$LEGACY_HARNESS_HOME" ]]; then
  LEGACY_HOME_PRESENT_BEFORE=1
fi
if [[ "$RELEASE_ACTIVATION_STATUS" -ne 0 ]]; then
  INSTALL_STATUS=1
elif [[ "$DRY_RUN" == "1" && ! -f "$INSTALL_SCRIPT" ]]; then
  say "would run ${PYTHON_BIN} ${INSTALL_ARGS[*]}" "将执行 ${PYTHON_BIN} ${INSTALL_ARGS[*]}"
else
  set +e
  TENETORA_OUTER_INSTALLER=1 TENETORA_DEFER_MACHINE_HOME_MIGRATION=1 \
    "$PYTHON_BIN" "${INSTALL_ARGS[@]}"
  INSTALL_STATUS=$?
  set -e
fi
say "[4/6] Bootstrapping CLI..." "[4/6] 正在自举 CLI..."
CLI_STATUS=0
CLI_ARGS=(
  "$ENSURE_CLI"
  "--install"
  "--configure-path"
  "--bin-dir" "${HARNESS_HOME}/bin"
)
if [[ -f "${MANAGED_RUNTIME_SNAPSHOT}/manifest.json" ]]; then
  CLI_ARGS+=("--runtime-rollback-journal" "$MANAGED_RUNTIME_SNAPSHOT")
fi
if [[ "$FORCE" == "1" ]]; then
  CLI_ARGS+=("--force")
fi
if [[ "$INSTALL_CLI" == "0" ]]; then
  say "skipped by --no-cli" "已按 --no-cli 跳过"
elif [[ "$DRY_RUN" == "1" ]]; then
  say "would run ${PYTHON_BIN} ${CLI_ARGS[*]}" "将执行 ${PYTHON_BIN} ${CLI_ARGS[*]}"
else
  set +e
  CLI_OUTPUT="$(TENETORA_DEFER_MACHINE_HOME_MIGRATION=1 \
    "$PYTHON_BIN" "${CLI_ARGS[@]}" 2>&1)"
  CLI_STATUS=$?
  set -e
  if [[ "$CLI_STATUS" -ne 0 ]]; then
    [[ -n "$CLI_OUTPUT" ]] && printf '%s\n' "$CLI_OUTPUT" >&2
    say "CLI bootstrap failed with exit code ${CLI_STATUS}." "CLI 自举失败，退出码：${CLI_STATUS}。"
  else
    say "CLI: ready" "CLI：已就绪"
    if [[ "$VERBOSE" == "1" && -n "$CLI_OUTPUT" ]]; then
      printf '%s\n' "$CLI_OUTPUT"
    fi
  fi
fi

HOOK_CONVERGENCE_STATUS=0
if [[ "$CLI_STATUS" -eq 0 && "$DRY_RUN" != "1" && -f "${HOOK_ROLLBACK_JOURNAL}/manifest.json" ]]; then
  HOOK_CONVERGENCE_ARGS=(
    "$INSTALL_SCRIPT"
    "--converge-hooks"
    "--path" "$PROJECT_PATH"
    "--package-root" "$SOURCE_DIR"
    "--hook-rollback-journal" "$HOOK_ROLLBACK_JOURNAL"
    "--json"
  )
  if [[ "$OPERATION" == "upgrade" ]]; then
    HOOK_CONVERGENCE_ARGS+=("--all-existing")
  fi
  set +e
  HOOK_CONVERGENCE_OUTPUT="$("$PYTHON_BIN" "${HOOK_CONVERGENCE_ARGS[@]}" 2>&1)"
  HOOK_CONVERGENCE_STATUS=$?
  set -e
  if [[ "$HOOK_CONVERGENCE_STATUS" -ne 0 ]]; then
    [[ -n "$HOOK_CONVERGENCE_OUTPUT" ]] && printf '%s\n' "$HOOK_CONVERGENCE_OUTPUT" >&2
    say \
      "Managed Git hook convergence failed after CLI bootstrap." \
      "CLI 自举后，受管 Git Hook 收敛失败。"
  elif [[ "$VERBOSE" == "1" && -n "$HOOK_CONVERGENCE_OUTPUT" ]]; then
    printf '%s\n' "$HOOK_CONVERGENCE_OUTPUT"
  fi
fi

if [[ "$DRY_RUN" != "1" && -f "${HOOK_ROLLBACK_JOURNAL}/manifest.json" ]]; then
  set +e
  "$PYTHON_BIN" "$INSTALL_SCRIPT" \
    --finalize-hook-journal \
    --hook-rollback-journal "$HOOK_ROLLBACK_JOURNAL" \
    --package-root "$SOURCE_DIR" \
    --json >/dev/null
  HOOK_FINALIZE_STATUS=$?
  set -e
  if [[ "$HOOK_FINALIZE_STATUS" -ne 0 ]]; then
    say \
      "Managed Git hook rollback journal could not record the post-update state." \
      "受管 Git Hook 回滚 journal 无法记录更新后的状态。"
    HOOK_CONVERGENCE_STATUS=1
  fi
fi

if [[ "$INSTALL_STATUS" -eq 0 && "$CLI_STATUS" -eq 0 && "$HOOK_CONVERGENCE_STATUS" -eq 0 \
  && "$DRY_RUN" != "1" ]]; then
  set +e
  prepare_current_links "$SOURCE_DIR"
  RELEASE_ACTIVATION_STATUS=$?
  set -e
  if [[ "$RELEASE_ACTIVATION_STATUS" -ne 0 ]]; then
    say \
      "Package activation failed; the previous current release remains selected." \
      "安装包激活失败；current 仍保持此前版本。"
  fi
fi

checkpoint_managed_runtime() {
  local allow_pointer_changes="${1:-0}"
  if [[ "$DRY_RUN" != "1" \
    && -f "${MANAGED_RUNTIME_SNAPSHOT}/manifest.json" ]]; then
    local checkpoint_args=(
      "${SOURCE_DIR}/skills/tenetora/scripts/runtime_transaction.py"
      --checkpoint
      --bin-dir "${HARNESS_HOME}/bin"
      --journal "$MANAGED_RUNTIME_SNAPSHOT"
    )
    if [[ "$allow_pointer_changes" == "1" ]]; then
      checkpoint_args+=(
        --expect-link "current=${SOURCE_DIR}"
        --expect-link "source/tenetora=${SOURCE_DIR}"
      )
    fi
    checkpoint_args+=(--json)
    set +e
    "$PYTHON_BIN" "${checkpoint_args[@]}" >/dev/null
    RUNTIME_CHECKPOINT_STATUS=$?
    set -e
    if [[ "$RUNTIME_CHECKPOINT_STATUS" -ne 0 ]]; then
      say "Managed runtime checkpoint failed after release or machine-home migration." "release 或机器主目录迁移后托管 runtime checkpoint 失败。"
      RELEASE_ACTIVATION_STATUS=1
    fi
  fi
}

# If an earlier stage failed, machine-home migration is skipped. Capture its
# partial managed state before the final rollback. The migration helper has
# its own rollback boundary; on migration failure we deliberately keep the
# pre-migration checkpoint instead of treating a partially failed migration as
# an external edit.
if [[ "$INSTALL_STATUS" -ne 0 || "$CLI_STATUS" -ne 0 || "$HOOK_CONVERGENCE_STATUS" -ne 0 \
  || "$RELEASE_ACTIVATION_STATUS" -ne 0 ]]; then
  checkpoint_managed_runtime
fi

MACHINE_HOME_MIGRATION_STATUS=0
MACHINE_HOME_MIGRATION_OUTPUT=""
# Capture the state that the migration helper is expected to receive. A
# successful migration gets a second checkpoint below; a failed migration is
# responsible for restoring its own partial filesystem changes.
if [[ "$INSTALL_STATUS" -eq 0 && "$INSTALL_CLI" != "0" && "$CLI_STATUS" -eq 0 && "$HOOK_CONVERGENCE_STATUS" -eq 0 \
  && "$RELEASE_ACTIVATION_STATUS" -eq 0 && "$DRY_RUN" != "1" \
  && "$HARNESS_HOME" == "$DEFAULT_HARNESS_HOME" && -f "$MIGRATION_SCRIPT" ]]; then
  checkpoint_managed_runtime 1
fi

if [[ "$INSTALL_STATUS" -eq 0 && "$INSTALL_CLI" != "0" && "$CLI_STATUS" -eq 0 && "$HOOK_CONVERGENCE_STATUS" -eq 0 \
  && "$RELEASE_ACTIVATION_STATUS" -eq 0 && "$DRY_RUN" != "1" \
  && "$HARNESS_HOME" == "$DEFAULT_HARNESS_HOME" && -f "$MIGRATION_SCRIPT" ]]; then
  MIGRATION_ARGS=(
    "$MIGRATION_SCRIPT"
    --canonical "$HARNESS_HOME"
    --legacy "$LEGACY_HARNESS_HOME"
    --json
  )
  if [[ -f "${MANAGED_RUNTIME_SNAPSHOT}/manifest.json" ]]; then
    MIGRATION_ARGS+=(
      --runtime-rollback-journal "$MANAGED_RUNTIME_SNAPSHOT"
      --bin-dir "${HARNESS_HOME}/bin"
    )
  fi
  set +e
  MACHINE_HOME_MIGRATION_OUTPUT="$("$PYTHON_BIN" "${MIGRATION_ARGS[@]}" 2>&1)"
  MACHINE_HOME_MIGRATION_STATUS=$?
  set -e
  if [[ "$MACHINE_HOME_MIGRATION_STATUS" -ne 0 ]]; then
    [[ -n "$MACHINE_HOME_MIGRATION_OUTPUT" ]] && printf '%s\n' "$MACHINE_HOME_MIGRATION_OUTPUT" >&2
    say \
      "Legacy machine-home migration failed after release, CLI, and Git hook verification." \
      "新 release、CLI 与 Git Hook 验证后，旧机器主目录迁移失败。"
  elif [[ "$LEGACY_HOME_PRESENT_BEFORE" == "1" && ! -e "$LEGACY_HARNESS_HOME" && -d "$HARNESS_HOME" ]]; then
    say "Machine home: migrated to ${HARNESS_HOME}" "机器主目录：已迁移到 ${HARNESS_HOME}"
  elif [[ "$VERBOSE" == "1" && -n "$MACHINE_HOME_MIGRATION_OUTPUT" ]]; then
    printf '%s\n' "$MACHINE_HOME_MIGRATION_OUTPUT"
  fi
fi

# Migration may rewrite managed source pointers, so checkpoint only after the
# migration attempt has completed and before the final result is evaluated.
if [[ "$INSTALL_STATUS" -eq 0 && "$CLI_STATUS" -eq 0 && "$HOOK_CONVERGENCE_STATUS" -eq 0 \
  && "$RELEASE_ACTIVATION_STATUS" -eq 0 ]]; then
  checkpoint_managed_runtime 1
fi

say "[5/6] Summarizing result..." "[5/6] 正在汇总结果..."
if [[ "$VERBOSE" == "1" ]]; then
  say "Skill tools: ${TOOLS}" "工具：${TOOLS}"
  say "Scope: ${DISPLAY_SCOPE}" "范围：${DISPLAY_SCOPE}"
  say "Source: ${SOURCE_DIR}" "来源：${SOURCE_DIR}"
  say "CLI bin: ${HARNESS_HOME}/bin" "CLI 目录：${HARNESS_HOME}/bin"
  if [[ -f "$LOG_FILE" ]]; then
    say "Detailed log: ${LOG_FILE}" "详细日志：${LOG_FILE}"
  fi
  if [[ -f "$EVENT_FILE" ]]; then
    say "Structured events: ${EVENT_FILE}" "结构化事件：${EVENT_FILE}"
  fi
fi

INSTALL_CAPABILITY_RESULT=""
if [[ -f "$EVENT_FILE" ]]; then
  INSTALL_CAPABILITY_RESULT="$("$PYTHON_BIN" -c '
import json, sys
status = ""
with open(sys.argv[1], encoding="utf-8") as handle:
    for raw in handle:
        try:
            event = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if event.get("event") == "complete":
            status = str(event.get("status") or "")
print(status)
' "$EVENT_FILE" 2>/dev/null || true)"
fi

FINAL_RESULT="FULL"
FINAL_STATUS=0
if [[ "$INSTALL_STATUS" -eq 1 || "$INSTALL_STATUS" -gt 2 ]]; then
  FINAL_RESULT="BLOCKED"
  FINAL_STATUS=1
elif [[ "$INSTALL_STATUS" -eq 2 || "$CLI_STATUS" -ne 0 || "$HOOK_CONVERGENCE_STATUS" -ne 0 || "$MACHINE_HOME_MIGRATION_STATUS" -ne 0 || "$RELEASE_ACTIVATION_STATUS" -ne 0 ]]; then
  FINAL_RESULT="PARTIAL"
  FINAL_STATUS=2
elif [[ "$INSTALL_CAPABILITY_RESULT" == "PENDING_TRUST" ]]; then
  FINAL_RESULT="PENDING_TRUST"
fi

RUNTIME_ROLLBACK_STATUS=0
HOOK_ROLLBACK_STATUS=0
RELEASE_ROLLBACK_STATUS=0
if [[ "$DRY_RUN" != "1" && "$FINAL_RESULT" != "FULL" && "$FINAL_RESULT" != "PENDING_TRUST" \
  && -f "${MANAGED_RUNTIME_SNAPSHOT}/manifest.json" ]]; then
  if [[ -f "${HOOK_ROLLBACK_JOURNAL}/manifest.json" ]]; then
    set +e
    "$PYTHON_BIN" "$INSTALL_SCRIPT" \
      --finalize-hook-journal \
      --hook-rollback-journal "$HOOK_ROLLBACK_JOURNAL" \
      --package-root "$SOURCE_DIR" \
      --json >/dev/null
    HOOK_ROLLBACK_STATUS=$?
    if [[ "$HOOK_ROLLBACK_STATUS" -eq 0 ]]; then
      "$PYTHON_BIN" "$INSTALL_SCRIPT" \
        --restore-hooks \
        --hook-rollback-journal "$HOOK_ROLLBACK_JOURNAL" \
        --package-root "$SOURCE_DIR" \
        --json >/dev/null
      HOOK_ROLLBACK_STATUS=$?
    fi
    set -e
  fi
  set +e
  restore_previous_release
  RELEASE_ROLLBACK_STATUS=$?
  restore_managed_runtime "$MANAGED_RUNTIME_SNAPSHOT"
  RUNTIME_ROLLBACK_STATUS=$?
  set -e
  if [[ "$RELEASE_ROLLBACK_STATUS" -ne 0 || "$RUNTIME_ROLLBACK_STATUS" -ne 0 || "$HOOK_ROLLBACK_STATUS" -ne 0 ]]; then
    FINAL_RESULT="BLOCKED"
    FINAL_STATUS=1
    preserve_failed_transaction_journals
    say \
      "Managed rollback failed (release=${RELEASE_ROLLBACK_STATUS}, runtime=${RUNTIME_ROLLBACK_STATUS}, hooks=${HOOK_ROLLBACK_STATUS}); inspect the detailed log before retrying." \
      "托管回滚失败（release=${RELEASE_ROLLBACK_STATUS}，runtime=${RUNTIME_ROLLBACK_STATUS}，hooks=${HOOK_ROLLBACK_STATUS}）；重试前请检查详细日志。"
  elif [[ "$VERBOSE" == "1" ]]; then
    say \
      "Managed runtime: restored the previous bin/runtime/current and source entry state after failure." \
      "托管 runtime：失败后已恢复此前的 bin/runtime/current 与 source 入口状态。"
  fi
  if [[ "$RELEASE_ROLLBACK_STATUS" -eq 0 && "$RUNTIME_ROLLBACK_STATUS" -eq 0 && "$HOOK_ROLLBACK_STATUS" -eq 0 ]]; then
    say \
      "Rollback boundary: CLI/runtime/source/Git hooks were restored; lifecycle skill or host plugin surfaces that completed before the failure may already use the new version. Rerun the ordinary upgrade to converge them." \
      "回滚边界：CLI/runtime/source/Git hooks 已恢复；失败前已完成的 lifecycle skill 或宿主插件安装面可能已是新版本。请重新执行普通升级完成收敛。"
  fi
fi

if [[ "$DRY_RUN" != "1" && "$FINAL_RESULT" != "BLOCKED" ]]; then
  write_language_preference
fi

case "$FINAL_RESULT" in
  FULL)
    if [[ "$OPERATION" == "upgrade" ]]; then
      say "[6/6] Upgrade succeeded" "[6/6] 升级成功"
    else
      say "[6/6] Installation succeeded" "[6/6] 安装成功"
    fi
    say "Final result: success" "最终结果：成功"
    ;;
  PENDING_TRUST)
    if [[ "$OPERATION" == "upgrade" ]]; then
      say "[6/6] Upgrade succeeded; trust or restart required" "[6/6] 升级成功；需完成信任或重启"
    else
      say "[6/6] Installation succeeded; trust or restart required" "[6/6] 安装成功；需完成信任或重启"
    fi
    say "Final result: success with follow-up" "最终结果：成功，等待激活"
    say "Complete the trust or restart action shown above; no reinstall is required." "请完成上方的信任或重启操作，无需重新安装。"
    ;;
  PARTIAL)
    if [[ "$OPERATION" == "upgrade" ]]; then
      say "[6/6] Upgrade partially failed" "[6/6] 升级部分失败"
    else
      say "[6/6] Installation partially failed" "[6/6] 安装部分失败"
    fi
    say "Final result: partial failure" "最终结果：部分失败"
    say "Review the failure details above before retrying." "重试前请检查上方失败详情。"
    ;;
  *)
    if [[ "$OPERATION" == "upgrade" ]]; then
      say "[6/6] Upgrade failed" "[6/6] 升级失败"
    else
      say "[6/6] Installation failed" "[6/6] 安装失败"
    fi
    say "Final result: failed" "最终结果：失败"
    say "Review the failure details above before retrying." "重试前请检查上方失败详情。"
    ;;
esac

if [[ "$FINAL_RESULT" == "PARTIAL" || "$FINAL_RESULT" == "BLOCKED" ]]; then
  [[ -f "$LOG_FILE" ]] && say "Detailed log: ${LOG_FILE}" "详细日志：${LOG_FILE}"
  [[ -f "$EVENT_FILE" ]] && say "Structured events: ${EVENT_FILE}" "结构化事件：${EVENT_FILE}"
elif [[ "$OPERATION" != "upgrade" ]]; then
  say "Next: restart or reload your AI tool, then invoke: use tenetora" "下一步：重启或重新加载 AI 工具，然后调用：use tenetora"
fi

if [[ "$VERBOSE" == "1" ]]; then
  say "CLI command: ${HARNESS_HOME}/bin/tenetora" "CLI 命令：${HARNESS_HOME}/bin/tenetora"
  say "Smoke check: ${HARNESS_HOME}/bin/tenetora version && ${HARNESS_HOME}/bin/tenetora status --tools ${TOOLS}" "冒烟检查：${HARNESS_HOME}/bin/tenetora version && ${HARNESS_HOME}/bin/tenetora status --tools ${TOOLS}"
fi
exit "$FINAL_STATUS"
