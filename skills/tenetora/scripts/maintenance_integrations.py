"""Find live host references before removing a proven machine installation.

Plans contain locators and digests, never the user's configuration contents.
Independent copies remain installed. File edits join the hook restoration undo
transaction; native managers retain their separate, explicitly reported boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from path_security import validate_unredirected_file_path, validate_unredirected_path


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def points_into(raw: object, home: Path) -> bool:
    return (
        isinstance(raw, (str, Path))
        and Path(raw).is_absolute()
        and Path(raw).resolve(strict=False).is_relative_to(home.resolve())
    )


def link_fingerprint(path: Path) -> str:
    validate_unredirected_path(path.parent, label="installed skill link parent")
    if not path.is_symlink():
        if path.exists():
            raise ValueError("Installed skill link was replaced; it was preserved.")
        return ""
    info = path.lstat()
    return digest([stat.S_IMODE(info.st_mode), os.readlink(path)])


def transformed(item: dict, data: bytes) -> bytes | None:
    kind = item["kind"]
    if kind in {"adapter-file", "skill-link"}:
        return None
    value = json.loads(data)
    if kind == "cursor":
        hooks = {}
        for event, entries in value["hooks"].items():
            if not isinstance(entries, list):
                hooks[event] = entries
                continue
            kept = [
                entry for entry in entries if digest(entry) not in item["entry_hashes"]
            ]
            if kept or len(kept) == len(entries):
                hooks[event] = kept
        value["hooks"] = hooks
        if not hooks and not (set(value) - {"version", "hooks"}):
            return None
    elif kind == "codex-fallback":
        from tenetora import codex_project_hooks

        value = codex_project_hooks._remove_managed_hooks(value)
        if not item["originally_existed"] and not codex_project_hooks._has_user_content(
            value
        ):
            return None
    else:
        raise ValueError("Unknown host file removal action.")
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def plan(home: Path, projects: list[Path], installer, fingerprint) -> dict:
    from tenetora import codex_project_hooks

    actions = []
    preserved = []
    seen = set()

    def add(path: Path, kind: str, **extra):
        if path in seen:
            return
        seen.add(path)
        item = {"path": str(path), "kind": kind, **extra}
        if kind == "skill-link":
            item["before"] = link_fingerprint(path)
        else:
            item["before"] = fingerprint(path)
            after = transformed(item, path.read_bytes())
            item["after_sha256"] = (
                hashlib.sha256(after).hexdigest() if after is not None else None
            )
        actions.append(item)

    contexts = [("global", projects[0] if projects else Path.cwd())]
    contexts += [("project", root) for root in sorted(set(projects)) if root.is_dir()]
    for scope, project in contexts:
        for target in sorted(installer.RUNTIME_ADAPTER_TARGETS):
            directory = installer.runtime_home(target, scope, project)
            relative = {
                "cursor": "hooks.json",
                "opencode": "plugins/tenetora.js",
                "pi": "extensions/tenetora.ts",
            }[target]
            paths = [directory / relative]
            if target == "opencode":
                paths.append(paths[0].with_name("agent-harness.js"))
            for path in paths:
                if not path.exists() and not path.is_symlink():
                    continue
                validate_unredirected_file_path(path, label="installed runtime adapter")
                content = path.read_text(encoding="utf-8")
                if target == "cursor":
                    value = json.loads(content)
                    hooks = value.get("hooks", {}) if isinstance(value, dict) else {}
                    if not isinstance(hooks, dict):
                        raise ValueError(
                            "Cursor hooks inventory is invalid; it was preserved."
                        )
                    entries = [
                        entry
                        for values in hooks.values()
                        if isinstance(values, list)
                        for entry in values
                        if installer.cursor_hook_is_managed(entry)
                    ]
                    bound = [
                        entry
                        for entry in entries
                        if points_into(
                            installer.cursor_command_source(entry.get("command", "")),
                            home,
                        )
                    ]
                    if not bound:
                        continue
                    state, _ = installer.historical_cursor_runtime_state(path)
                    if state != "legacy-ok":
                        raise ValueError(
                            "A modified Cursor adapter references this machine home; it was preserved."
                        )
                    add(path, "cursor", entry_hashes=[digest(entry) for entry in bound])
                else:
                    expressions = (
                        [
                            installer.OPENCODE_RUNTIME_ROOT_RE,
                            installer.LEGACY_OPENCODE_RUNTIME_ROOT_RE,
                        ]
                        if target == "opencode"
                        else [installer.PI_RUNTIME_ROOT_RE]
                    )
                    match = next(
                        (
                            match
                            for expression in expressions
                            if (match := expression.search(content))
                        ),
                        None,
                    )
                    if match is None or not points_into(
                        json.loads(match.group(1)), home
                    ):
                        continue
                    state, _ = (
                        installer.historical_opencode_runtime_state
                        if target == "opencode"
                        else installer.historical_pi_runtime_state
                    )(path)
                    if state != "legacy-ok":
                        raise ValueError(
                            "A modified runtime adapter references this machine home; it was preserved."
                        )
                    add(path, "adapter-file")
        # Check bounded, known installation paths; never discover arbitrary projects.
        for target in installer.SUPPORTED_TOOLS:
            base = installer.target_base(target, scope, project)
            if not base.is_dir():
                continue
            for name in installer.CANONICAL_SKILL_NAMES:
                path = base / name
                if path.is_symlink() and points_into(path.resolve(strict=False), home):
                    resolved = path.resolve(strict=True)
                    source = resolved.parent.parent
                    from release_store import managed_release

                    if resolved.name != name or not managed_release(source, home):
                        raise ValueError(
                            "Installed skill link points to an unproven machine object; it was preserved."
                        )
                    add(path, "skill-link")
        if scope == "project":
            state = codex_project_hooks.project_state_path(project, home)
            if state.is_file():
                validate_unredirected_file_path(state, label="Codex fallback ownership")
                ownership = json.loads(state.read_bytes())
                if ownership.get("managed_fingerprint"):
                    result = codex_project_hooks.remove_project_fallback(
                        project, harness_home=home, dry_run=True
                    )
                    if result.state != "planned":
                        raise ValueError(
                            "Codex fallback ownership changed; it was preserved."
                        )
                    add(
                        result.hooks_path,
                        "codex-fallback",
                        originally_existed=bool(ownership.get("originally_existed")),
                    )
    # ZCode's source is provenance, not a runtime dependency: its ordinary
    # self-contained cache remains usable after the original release is removed.
    project = projects[0] if projects else Path.cwd()
    registry = installer.zcode_registry_path(project)
    if registry.exists():
        validate_unredirected_file_path(registry, label="ZCode registration inventory")
        for entry in installer.zcode_registrations(project):
            raw = entry.get("installPath")
            if points_into(raw, home):
                raise ValueError(
                    "A ZCode runtime cache is inside this machine home; relocate it with the ZCode installer before uninstall."
                )
            if installer.zcode_entry_is_managed(entry):
                preserved.append(
                    {
                        "target": "zcode",
                        "reason": "independent runtime cache preserved; source is installation provenance",
                    }
                )
    return {"actions": actions, "preserved": preserved}
