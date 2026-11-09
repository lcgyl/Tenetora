"""Authenticate native payloads and detach only host references to one home."""

from __future__ import annotations

import json
import hashlib
import re
import tempfile
import subprocess
from copy import deepcopy
from pathlib import Path

from path_security import validate_unredirected_path
from release_store import read_release_manifest
from maintenance_objects import regular_tree, snapshot


def windows_claude_manifest_matches(data: bytes, expected: str, installer) -> bool:
    """Prove both the original manifest digest and the exact known transform.

    Older Windows writers emitted CRLF; the canonical renderer emits LF. No
    arbitrary Hook edit or extra payload file is exempted from authentication.
    """
    renderer = getattr(installer, "windows_claude_hook_bytes", None)
    if not callable(renderer):
        return False
    value = json.loads(data)
    if not isinstance(value, dict) or not isinstance(value.get("hooks"), dict):
        return False
    for groups in value["hooks"].values():
        for group in groups if isinstance(groups, list) else []:
            entries = group.get("hooks") if isinstance(group, dict) else None
            for entry in entries if isinstance(entries, list) else []:
                if isinstance(entry, dict) and isinstance(entry.get("command"), str):
                    entry["command"] = entry["command"].replace(
                        "${CLAUDE_PLUGIN_ROOT}\\hooks\\run-hook.cmd",
                        "${CLAUDE_PLUGIN_ROOT}/hooks/run-hook",
                    )
    original = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    candidates = (original, original.replace(b"\n", b"\r\n"))
    if not any(
        hashlib.sha256(candidate).hexdigest() == expected for candidate in candidates
    ):
        return False
    rendered = renderer(original)
    return data in (rendered, rendered.replace(b"\n", b"\r\n"))


def copied_manifest_matches(path: Path, target: str, installer) -> bool:
    """Verify a retained payload when its source release was already cleaned.

    This is local manifest integrity and ownership, not a signature or proof
    against an actor able to rewrite both payload and manifest.
    """
    manifest = read_release_manifest(path)
    if path.parent.name not in {
        str(manifest["version"]),
        str(manifest.get("adapter_version") or ""),
    }:
        return False
    expected = set()
    for name in manifest["file_sha256"]:
        parts = Path(name).parts
        if not parts or parts[0] not in installer.PLUGIN_COPY_ENTRIES:
            continue
        if parts[0] == "skills" and (
            len(parts) < 2 or parts[1] not in installer.CANONICAL_SKILL_NAMES
        ):
            continue
        if target == "claude" and name == "hooks/hooks.json":
            continue
        expected.add(name)
    actual = {p.relative_to(path).as_posix() for p in regular_tree(path) if p.is_file()}
    if actual != expected | {"manifest.json"}:
        return False
    for name in expected:
        data = (path / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != manifest["file_sha256"][name]:
            if not (
                target == "claude"
                and name == "hooks/hooks-claude.json"
                and windows_claude_manifest_matches(
                    data, manifest["file_sha256"][name], installer
                )
            ):
                return False
    version = path / "skills/tenetora/VERSION"
    plugin = (
        path
        / (".claude-plugin" if target == "claude" else ".codex-plugin")
        / "plugin.json"
    )
    return (
        version.is_file()
        and version.read_text().strip() == manifest["version"]
        and plugin.is_file()
        and json.loads(plugin.read_text()).get("name")
        == installer.PLUGIN_REGISTRATION_NAME
    )


def authenticated_sources(home: Path, releases: list[Path], installer) -> list[dict]:
    base = home / "plugin-sources"
    result = []
    if not base.exists():
        return result
    regular_tree(base)
    for version in sorted(base.iterdir()):
        if not version.is_dir():
            raise ValueError("Native source version is not a directory.")
        matching = [
            source
            for source in releases
            if version.name
            in {
                str(read_release_manifest(source)["version"]),
                str(read_release_manifest(source).get("adapter_version") or ""),
            }
        ]
        children = list(version.iterdir())
        if not children:
            continue
        if any(
            child.name not in {"claude", "codex"} or not child.is_dir()
            for child in children
        ):
            raise ValueError(
                "Native source layout requires an explicit legacy ownership review."
            )
        for path in children:
            actual = snapshot(path)
            valid = False
            for source in matching:
                with tempfile.TemporaryDirectory(
                    prefix="tenetora-native-ownership-"
                ) as raw:
                    expected = Path(raw) / "payload"
                    installer.copy_native_plugin_payload(source, expected, path.name)
                    if actual["digest"] == snapshot(expected)["digest"]:
                        result.append(
                            {
                                "target": path.name,
                                "path": str(path.resolve()),
                                "snapshot": actual,
                                "release": str(source),
                            }
                        )
                        valid = True
                        break
            if not valid:
                try:
                    valid = copied_manifest_matches(path, path.name, installer)
                except (OSError, RuntimeError, ValueError):
                    valid = False
                if valid:
                    result.append(
                        {
                            "target": path.name,
                            "path": str(path.resolve()),
                            "snapshot": actual,
                            "release": None,
                            "ownership": "copied release manifest; no signature assertion",
                        }
                    )
            if not valid:
                raise ValueError(
                    "Native payload is modified or contains unknown files: " + path.name
                )
    return result


def manager(installer, target: str, project: Path, *args):
    prefix = ["--setting-sources", "user"] if target == "claude" else []
    return installer.run_native_plugin_command(target, project, *prefix, *args)


def plugin_entries(
    installer, target: str, project: Path, *, user_only: bool = True
) -> list[dict]:
    result = (
        manager(installer, target, project, "plugin", "list", "--json")
        if user_only
        else installer.run_native_plugin_command(
            target, project, "plugin", "list", "--json"
        )
    )
    value = json.loads(result.stdout)
    entries = (
        value
        if isinstance(value, list)
        else value.get("installed")
        if isinstance(value, dict)
        else None
    )
    if not isinstance(entries, list) or any(
        not isinstance(item, dict) for item in entries
    ):
        raise ValueError("Host plugin inventory is unsupported.")
    return (
        [item for item in entries if item.get("scope") in (None, "user")]
        if user_only
        else entries
    )


def marketplaces(installer, target: str, project: Path) -> list[dict]:
    result = manager(
        installer, target, project, "plugin", "marketplace", "list", "--json"
    )
    value = json.loads(result.stdout)
    entries = (
        value
        if isinstance(value, list)
        else value.get("marketplaces")
        if isinstance(value, dict)
        else None
    )
    if not isinstance(entries, list) or any(
        not isinstance(item, dict) for item in entries
    ):
        raise ValueError("Host marketplace inventory is unsupported.")
    return entries


def record_digest(item) -> str:
    return hashlib.sha256(
        json.dumps(item, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def local_source(item: dict) -> Path | None:
    value = item.get("readFromFolder")
    if isinstance(value, str) and Path(value).is_absolute():
        return Path(value).resolve(strict=False)
    for key in ("source", "marketplaceSource"):
        value = item.get(key)
        if isinstance(value, str) and Path(value).is_absolute():
            return Path(value).resolve(strict=False)
        if isinstance(value, dict):
            for field in ("path", "source"):
                candidate = value.get(field)
                if isinstance(candidate, str) and Path(candidate).is_absolute():
                    return Path(candidate).resolve(strict=False)
    for key in ("path", "root"):
        value = item.get(key)
        if isinstance(value, str) and Path(value).is_absolute():
            return Path(value).resolve(strict=False)
    return None


def independent_remote(item: dict) -> bool:
    if item.get("source") == "github":
        return True
    for key in ("source", "marketplaceSource"):
        value = item.get(key)
        if isinstance(value, str) and value.startswith(
            ("https://", "http://", "ssh://", "git@")
        ):
            return True
        if isinstance(value, dict) and any(
            isinstance(value.get(k), str)
            and value[k].startswith(("https://", "http://", "ssh://", "git@"))
            for k in ("source", "url", "repo")
        ):
            return True
    return False


def identity(item: dict) -> str:
    value = item.get("id") or item.get("pluginId")
    if isinstance(value, str):
        return value
    name = item.get("name")
    market = item.get("marketplaceName") or item.get("marketplace")
    return (
        name + "@" + market if isinstance(name, str) and isinstance(market, str) else ""
    )


def plan(
    home: Path,
    payloads: list[dict],
    installer,
    project: Path,
    *,
    owned_roots: list[Path] | None = None,
    contexts: list[Path] | None = None,
) -> dict:
    home = home.resolve()
    actions = []
    preserved = []
    project_contexts = sorted({project, *(contexts or [])})
    targets = {item["target"] for item in payloads}
    for target in ("claude", "codex"):
        native_home = installer.tool_definition(target).native_home(project)
        if native_home is not None and (native_home / "plugins").is_dir():
            targets.add(target)
    for target in sorted(targets):
        definition = installer.tool_definition(target)
        host_home = definition.native_home(project)
        if installer.native_plugin_command(target) is None:
            if host_home is None or not host_home.exists():
                continue
            raise ValueError(
                "The "
                + target
                + " CLI is required to inspect existing native references before uninstall."
            )
        try:
            plugins = plugin_entries(installer, target, project)
            markets = marketplaces(installer, target, project)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            raise ValueError(
                "Cannot obtain a valid "
                + target
                + " native ownership inventory; machine files were preserved."
            ) from exc
        market = next(
            (
                item
                for item in markets
                if item.get("name") == installer.NATIVE_MARKETPLACE_ID
            ),
            None,
        )
        own = [
            item
            for item in plugins
            if identity(item) == installer.NATIVE_PLUGIN_ID
            or item.get("name") == installer.PLUGIN_REGISTRATION_NAME
            and (item.get("marketplaceName") or item.get("marketplace"))
            == installer.NATIVE_MARKETPLACE_ID
        ]
        if (
            market is not None
            and local_source(market) is None
            and not independent_remote(market)
        ):
            raise ValueError(
                "Native marketplace origin is unavailable; it was preserved."
            )
        if own and market is None and any(local_source(item) is None for item in own):
            raise ValueError("Native plugin origin is unavailable; it was preserved.")
        bound_market = (
            market is not None
            and local_source(market) is not None
            and local_source(market).is_relative_to(home)
        )
        bound_plugins = [
            item
            for item in own
            if local_source(item) is not None
            and local_source(item).is_relative_to(home)
        ]
        if bound_market:
            for item in plugins:
                if (
                    item.get("marketplaceName") or item.get("marketplace")
                ) == installer.NATIVE_MARKETPLACE_ID and item not in own:
                    raise ValueError(
                        "A native marketplace is shared with an unknown plugin; it was preserved."
                    )
            bound_plugins = [
                item
                for item in own
                if local_source(item) is None or local_source(item).is_relative_to(home)
            ]
        if target == "claude":
            for context in project_contexts:
                scoped = plugin_entries(installer, target, context, user_only=False)
                if any(
                    item.get("scope") in {"project", "local"}
                    and (
                        local_source(item) is not None
                        and local_source(item).is_relative_to(home)
                        or bound_market
                        and (
                            identity(item) == installer.NATIVE_PLUGIN_ID
                            or (item.get("marketplaceName") or item.get("marketplace"))
                            == installer.NATIVE_MARKETPLACE_ID
                        )
                    )
                    for item in scoped
                ):
                    raise ValueError(
                        "A project-scoped native registration still references this home; remove it in its own host context first."
                    )
        if not bound_market and not bound_plugins:
            preserved.append(
                {"target": target, "reason": "independent native integration preserved"}
            )
            continue
        allowed_sources = {
            Path(item["path"]) for item in payloads if item["target"] == target
        } | set(owned_roots or [])
        for item in [*bound_plugins, *([market] if bound_market else [])]:
            path = local_source(item)
            if path is not None and path not in allowed_sources:
                raise ValueError(
                    "Native reference points to an unrecognized machine object."
                )
        for item in bound_plugins:
            scope = item.get("scope") or "user"
            if scope not in {"user"}:
                raise ValueError(
                    "Project-scoped native registration requires its own explicit context review."
                )
        cached = []
        for item in bound_plugins:
            raw = item.get("installPath") or item.get("installedPath")
            version = str(item.get("version") or "")
            if (
                not raw
                and host_home is not None
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", version)
            ):
                raw = str(
                    host_home
                    / "plugins/cache"
                    / installer.NATIVE_MARKETPLACE_ID
                    / installer.PLUGIN_REGISTRATION_NAME
                    / version
                )
            if not raw or not isinstance(raw, str):
                raise ValueError("Native cache locator or version is unavailable.")
            if raw:
                path = Path(raw)
                if host_home is None:
                    raise ValueError("Host cache boundary is unavailable.")
                allowed = (
                    host_home
                    / "plugins/cache"
                    / installer.NATIVE_MARKETPLACE_ID
                    / installer.PLUGIN_REGISTRATION_NAME
                ).absolute()
                if not path.is_absolute() or ".." in path.parts:
                    raise ValueError(
                        "Native cache locator is outside the owned namespace."
                    )
                validate_unredirected_path(path, label="native plugin cache")
                validate_unredirected_path(
                    allowed, label="native plugin cache namespace"
                )
                if not path.resolve(strict=False).is_relative_to(
                    allowed.resolve(strict=False)
                ):
                    raise ValueError(
                        "Native cache locator is outside the owned namespace."
                    )
                if path.exists():
                    regular_tree(path)
                    actual = snapshot(path)
                    expected_digests = {
                        payload["snapshot"]["digest"]
                        for payload in payloads
                        if payload["target"] == target
                    }
                    expected_digests.update(
                        snapshot(source)["digest"] for source in owned_roots or []
                    )
                    if actual["digest"] not in expected_digests:
                        raise ValueError("Modified native plugin cache was preserved.")
                    cached.append({"path": str(path), "snapshot": actual})
        actions.append(
            {
                "target": target,
                "project": str(project),
                "home": str(home),
                "project_contexts": [str(path) for path in project_contexts],
                "plugins": [
                    {"id": identity(item), "record_sha256": record_digest(item)}
                    for item in bound_plugins
                ],
                "marketplace": {
                    "name": installer.NATIVE_MARKETPLACE_ID,
                    "record_sha256": record_digest(market),
                }
                if bound_market
                else None,
                "cache_snapshots": cached,
                "other_plugins_sha256": record_digest(
                    [item for item in plugins if item not in own]
                ),
            }
        )
    return {"actions": actions, "preserved": preserved}


def validate(plan: dict, installer) -> None:
    for item in plan["actions"]:
        target = item["target"]
        project = Path(item["project"])
        current = plugin_entries(installer, target, project)
        others = [
            entry for entry in current if identity(entry) != installer.NATIVE_PLUGIN_ID
        ]
        if record_digest(others) != item["other_plugins_sha256"]:
            raise ValueError("Unrelated host registrations changed after planning.")
        bound = [
            entry
            for entry in current
            if identity(entry) == installer.NATIVE_PLUGIN_ID
            and (
                local_source(entry) is not None
                and local_source(entry).is_relative_to(Path(item["home"]))
                or item["marketplace"] is not None
                and local_source(entry) is None
            )
        ]
        if sorted(record_digest(entry) for entry in bound) != sorted(
            entry["record_sha256"] for entry in item["plugins"]
        ):
            raise ValueError(
                "Native plugin dependency inventory changed after planning."
            )
        if target == "claude":
            for context in item["project_contexts"]:
                scoped = plugin_entries(
                    installer, target, Path(context), user_only=False
                )
                if any(
                    entry.get("scope") in {"project", "local"}
                    and (
                        local_source(entry) is not None
                        and local_source(entry).is_relative_to(Path(item["home"]))
                        or item["marketplace"] is not None
                        and (
                            identity(entry) == installer.NATIVE_PLUGIN_ID
                            or (
                                entry.get("marketplaceName") or entry.get("marketplace")
                            )
                            == installer.NATIVE_MARKETPLACE_ID
                        )
                    )
                    for entry in scoped
                ):
                    raise ValueError(
                        "A project-scoped native dependency appeared; machine files were preserved."
                    )
        for expected in item["plugins"]:
            if not any(
                identity(item) == expected["id"]
                and record_digest(item) == expected["record_sha256"]
                for item in current
            ):
                raise ValueError("Native plugin registration changed after planning.")
        if item["marketplace"] is not None and not any(
            record_digest(entry) == item["marketplace"]["record_sha256"]
            for entry in marketplaces(installer, target, project)
        ):
            raise ValueError("Native marketplace changed after planning.")
        for cached in item.get("cache_snapshots", []):
            # The host manager owns its cache transition after uninstall. We
            # authenticate it before that call and never delete it ourselves.
            # Registration/dependency checks above still run before each call.
            if not item["plugins"]:
                continue
            regular_tree(Path(cached["path"]))
            if snapshot(Path(cached["path"])) != cached["snapshot"]:
                raise ValueError("Native plugin cache changed after planning.")


def apply(plan: dict, installer) -> list[dict]:
    pending = deepcopy(plan)
    validate(pending, installer)
    removed = []
    for item in pending["actions"]:
        target = item["target"]
        project = Path(item["project"])
        try:
            for plugin in list(item["plugins"]):
                validate(pending, installer)
                if target == "claude":
                    manager(
                        installer,
                        target,
                        project,
                        "plugin",
                        "uninstall",
                        installer.NATIVE_PLUGIN_ID,
                        "--scope",
                        "user",
                        "--keep-data",
                        "--yes",
                    )
                else:
                    manager(
                        installer,
                        target,
                        project,
                        "plugin",
                        "remove",
                        installer.NATIVE_PLUGIN_ID,
                        "--json",
                    )
                if any(
                    identity(entry) == plugin["id"]
                    for entry in plugin_entries(installer, target, project)
                ):
                    raise ValueError("Native plugin removal was not confirmed.")
                item["plugins"].remove(plugin)
                validate(pending, installer)
            if item["marketplace"] is not None:
                validate(pending, installer)
                manager(
                    installer,
                    target,
                    project,
                    "plugin",
                    "marketplace",
                    "remove",
                    installer.NATIVE_MARKETPLACE_ID,
                    *(["--json"] if target == "codex" else ["--scope", "user"]),
                )
                if any(
                    entry.get("name") == installer.NATIVE_MARKETPLACE_ID
                    for entry in marketplaces(installer, target, project)
                ):
                    raise ValueError("Native marketplace removal was not confirmed.")
                item["marketplace"] = None
                validate(pending, installer)
            others = [
                entry
                for entry in plugin_entries(installer, target, project)
                if identity(entry) != installer.NATIVE_PLUGIN_ID
            ]
            if record_digest(others) != item["other_plugins_sha256"]:
                raise ValueError(
                    "Unrelated host registrations changed; machine home was preserved."
                )
            removed.append(
                {
                    "target": target,
                    "status": "removed",
                    "persistent_plugin_data": "Claude --keep-data; other host cache deletion requires exact ownership",
                }
            )
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            detail = (" " + str(exc)) if type(exc) is ValueError else ""
            raise ValueError(
                "Native integration cleanup did not complete."
                + detail
                + " Machine files were preserved; a Tenetora registration may already have been removed. Inspect the host manager and retry."
            ) from exc
    return removed
