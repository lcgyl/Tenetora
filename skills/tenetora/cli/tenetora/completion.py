"""Generate deterministic shell completion scripts for the managed CLI."""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable


SUPPORTED_SHELLS = ("bash", "zsh", "fish", "powershell")
COMMON_OPTIONS = (
    "--help",
    "--json",
    "--lang",
    "--path",
    "--tools",
    "--verbose",
)
COMMAND_OPTIONS = {
    "completion": SUPPORTED_SHELLS,
    "status": ("--scope", "--acknowledge", "--unacknowledge", "--reason", "--expires-days"),
    "doctor": ("--scope", "--fix"),
    "diagnostics": ("--output", "--force"),
    "install": (
        "--global",
        "-g",
        "--in-project",
        "-i",
        "--both",
        "-b",
        "--force",
        "--dry-run",
        "--mode",
        "--allow-skills-only",
        "--require-full",
        "--no-configure-path",
    ),
    "preflight": ("--global", "-g", "--in-project", "-i", "--both", "-b", "--require-full"),
    "update": (
        "--global",
        "-g",
        "--in-project",
        "-i",
        "--both",
        "-b",
        "--force",
        "--dry-run",
        "--mode",
        "--allow-skills-only",
        "--require-full",
        "--strategy",
        "--migrate",
        "--entrypoints",
        "--repository-scope",
    ),
    "audit": ("--strict", "--record-history", "--trend", "--min-score", "--module"),
    "alignment": ("--status", "--list", "--start", "--resume", "--confirm", "--handoff", "--goal", "--risk-level"),
    "audit-configs": ("--reconcile", "--write"),
    "benchmark": ("--runs", "--profile"),
    "capture-rule": ("--text", "--destination", "--source", "--apply"),
    "delegation": ("--list-roles", "--status", "--start", "--complete", "--unavailable", "--review-start", "--review-result", "--fix-start", "--fix-complete", "--role", "--result"),
    "guard": ("--action", "--text", "--file", "--stdin", "--source", "--commit-message", "--verification-command", "--claim-kind", "--goal", "--session-id", "--owner-id"),
    "hooks": ("--status", "--ensure", "--install", "--defer", "--decline"),
    "impact": ("--status", "--start", "--complete", "--kind", "--summary", "--impact-tool", "--symbol", "--baseline-status"),
    "cross-impact": ("--check", "--analyze", "--producer", "--changed-file"),
    "aggregate-claim": ("--create", "--check", "--child-proof"),
    "installations": ("list", "discover", "register", "remove", "--auto", "--root", "--max-depth"),
    "local-env": ("--allow", "--list", "--revoke"),
    "init": ("--write", "--mode", "--tools", "--gitignore", "--migrate", "--repository-scope", "--allow-mixed"),
    "onboarding": ("--decline", "--dismiss", "--tool", "--home"),
    "loop-state": ("--status", "--start", "--resume", "--complete", "--blocked", "--next-prompt"),
    "migrate": ("--check", "--plan", "--apply", "--allow-mixed"),
    "prompt-guard": ("--text", "--file", "--stdin", "--source"),
    "recap": ("--write", "--json", "--days"),
    "repair": ("--check", "--apply", "--fix"),
    "refresh": ("--strategy", "--apply", "--migrate", "--entrypoints", "--repository-scope"),
    "review": ("--check", "--apply", "--strategy", "--repository-scope"),
    "route": ("--message",),
    "rules": ("--context",),
    "run-all": ("--runner",),
    "setup-ci": ("--provider", "--write", "--check"),
    "skill-instructions": ("--skill",),
    "upgrade": (
        "--check",
        "--apply",
        "--project",
        "--mode",
        "--dry-run",
        "--progress",
        "--no-progress",
        "--verbose",
        "--prune-shadowed",
        "--no-prune-shadowed",
        "--codex-hooks",
        "--allow-tracked-codex-hooks",
        "--zip-file",
        "--sha256",
        "--manifest-url",
        "--zip-url",
        "--mirror-url",
        "--source-dir",
        "--no-cli",
        "--bin-dir",
        "--configure-path",
        "--no-configure-path",
        "--global",
        "-g",
        "--in-project",
        "-i",
        "--both",
        "-b",
        "--force",
        "--allow-skills-only",
        "--require-full",
        "--scope",
    ),
    "validate": ("--strict",),
}
OPTION_PATTERN = re.compile(r"^(?:-[A-Za-z0-9]|--[A-Za-z0-9][A-Za-z0-9-]*)$")
COMMAND_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")


def normalized_commands(commands: Iterable[str]) -> tuple[str, ...]:
    result = tuple(sorted({str(command) for command in commands if str(command)}))
    if not result or any(not COMMAND_PATTERN.fullmatch(command) for command in result):
        raise ValueError("completion command registry contains an unsafe command name")
    return result


def option_names(command_names: Iterable[str]) -> tuple[str, ...]:
    options = set(COMMON_OPTIONS)
    for command in command_names:
        options.update(
            str(item)
            for item in COMMAND_OPTIONS.get(command, ())
            if str(item).startswith("-")
        )
    options.update(("--check", "--force"))
    if any(not OPTION_PATTERN.fullmatch(option) for option in options):
        raise ValueError("completion option registry contains an unsafe option name")
    return tuple(sorted(options))


def _bash(commands: tuple[str, ...], options: tuple[str, ...]) -> str:
    command_words = " ".join(commands)
    option_words = " ".join(options)
    shell_words = " ".join(SUPPORTED_SHELLS)
    return f'''# Tenetora shell completion. Generated by `tenetora completion bash`.
_tenetora_completion() {{
    local cur="${{COMP_WORDS[COMP_CWORD]}}"
    local command_words="{command_words}"
    local option_words="{option_words}"
    if [[ $COMP_CWORD -eq 1 ]]; then
        COMPREPLY=( $(compgen -W "$command_words" -- "$cur") )
        return
    fi
    if [[ "${{COMP_WORDS[1]}}" == "completion" && $COMP_CWORD -eq 2 ]]; then
        COMPREPLY=( $(compgen -W "{shell_words}" -- "$cur") )
        return
    fi
    COMPREPLY=( $(compgen -W "$option_words" -- "$cur") )
}}
complete -F _tenetora_completion tenetora
'''


def _zsh(commands: tuple[str, ...], options: tuple[str, ...]) -> str:
    command_words = " ".join(commands)
    option_words = " ".join(options)
    shell_words = " ".join(SUPPORTED_SHELLS)
    return f'''#compdef tenetora
# Tenetora shell completion. Generated by `tenetora completion zsh`.
_tenetora() {{
    local -a commands options shells
    commands=({command_words})
    options=({option_words})
    shells=({shell_words})
    if (( CURRENT == 2 )); then
        _describe 'command' commands
        return
    fi
    if [[ $words[2] == completion && $CURRENT == 3 ]]; then
        _describe 'shell' shells
        return
    fi
    _describe 'option' options
}}
compdef _tenetora tenetora
'''


def _fish(commands: tuple[str, ...], options: tuple[str, ...]) -> str:
    command_words = " ".join(commands)
    option_words = " ".join(options)
    all_commands = command_words
    lines = [
        "# Tenetora shell completion. Generated by `tenetora completion fish`.",
        f"# Options: {option_words}",
        "complete -c tenetora -f",
        f"complete -c tenetora -n '__fish_use_subcommand' -a '{command_words}'",
        "complete -c tenetora -n '__fish_seen_subcommand_from completion' -a 'bash zsh fish powershell'",
    ]
    for option in options:
        long_name = option[2:] if option.startswith("--") else ""
        short_name = option[1:] if option.startswith("-") and not option.startswith("--") else ""
        if long_name:
            lines.append(
                f"complete -c tenetora -n '__fish_seen_subcommand_from {all_commands}' -l {long_name}"
            )
        elif short_name:
            lines.append(
                f"complete -c tenetora -n '__fish_seen_subcommand_from {all_commands}' -s {short_name}"
            )
    return "\n".join(lines) + "\n"


def _powershell(commands: tuple[str, ...], options: tuple[str, ...]) -> str:
    command_values = ", ".join(f"'{command}'" for command in commands)
    option_values = ", ".join(f"'{option}'" for option in options)
    shell_values = ", ".join(f"'{shell}'" for shell in SUPPORTED_SHELLS)
    return f'''# Tenetora shell completion. Generated by `tenetora completion powershell`.
Register-ArgumentCompleter -Native -CommandName tenetora -ScriptBlock {{
    param($wordToComplete, $commandAst, $cursorPosition)
    $commands = @({command_values})
    $options = @({option_values})
    $shells = @({shell_values})
    $elements = @($commandAst.CommandElements | Select-Object -Skip 1 | ForEach-Object {{ $_.Value }})
    if ($elements.Count -eq 0) {{
        $candidates = $commands
    }} elseif ($elements[0] -eq 'completion' -and $elements.Count -eq 1) {{
        $candidates = $shells
    }} else {{
        $candidates = $options
    }}
    $candidates |
        Where-Object {{ $_ -like "$wordToComplete*" }} |
        ForEach-Object {{
            [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
        }}
}}
'''


def render_completion(shell: str, commands: Iterable[str]) -> str:
    normalized_shell = str(shell).strip().lower()
    if normalized_shell not in SUPPORTED_SHELLS:
        raise ValueError(f"unsupported shell: {shell}; choose one of {', '.join(SUPPORTED_SHELLS)}")
    command_names = normalized_commands(commands)
    options = option_names(command_names)
    renderers = {"bash": _bash, "zsh": _zsh, "fish": _fish, "powershell": _powershell}
    return renderers[normalized_shell](command_names, options)


def print_help() -> None:
    print("Usage: tenetora completion <bash|zsh|fish|powershell>")
    print("Print a deterministic completion script to stdout; it does not modify shell profiles.")


def run_completion(argv: list[str], commands: Iterable[str]) -> int:
    if not argv or argv == ["--help"] or argv == ["-h"]:
        print_help()
        return 0 if argv else 2
    if len(argv) != 1:
        print("completion accepts exactly one shell name; use --help for supported shells", file=sys.stderr)
        return 2
    try:
        print(render_completion(argv[0], commands), end="")
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0
