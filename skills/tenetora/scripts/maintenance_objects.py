"""Prove ownership of machine objects without executing installed launchers."""

from __future__ import annotations

import stat
import os
import hashlib
import json
from pathlib import Path

from path_security import (
    is_redirected_path,
    validate_unredirected_file_path,
    validate_unredirected_path,
)
from release_store import managed_release, read_release_manifest
from runtime_transaction import entry_digest


def present(path: Path) -> bool:
    """A redirected entry still exists even when its target is missing."""
    return path.exists() or is_redirected_path(path)


def snapshot(path: Path) -> dict:
    if not path.exists() and not is_redirected_path(path):
        return {"digest": "", "mode": None}
    metadata = path.lstat()
    mode = stat.S_IMODE(metadata.st_mode)
    if not path.is_dir() or is_redirected_path(path):
        return {"digest": entry_digest(path), "mode": mode}
    pending = [path]
    records = []
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            children = list(entries)
        for child in children:
            item = Path(child.path)
            relative = item.relative_to(path).as_posix()
            if relative == ".managed-runtime.lock":
                continue
            info = item.lstat()
            item_mode = stat.S_IMODE(info.st_mode)
            if is_redirected_path(item):
                records.append((relative, "link", item_mode, entry_digest(item)))
            elif stat.S_ISDIR(info.st_mode):
                records.append((relative, "directory", item_mode))
                pending.append(item)
            elif stat.S_ISREG(info.st_mode):
                records.append((relative, "file", item_mode, entry_digest(item)))
            else:
                raise ValueError("Uninstall snapshot contains a special file.")
    digest = hashlib.sha256(
        json.dumps(sorted(records), separators=(",", ":")).encode()
    ).hexdigest()
    return {"digest": digest, "mode": mode}


def regular_tree(path: Path) -> list[Path]:
    if is_redirected_path(path):
        raise ValueError("Uninstall object is redirected: " + path.name)
    paths = [path, *path.rglob("*")] if path.is_dir() else [path]
    for item in paths:
        (
            validate_unredirected_path
            if item.is_dir()
            else validate_unredirected_file_path
        )(item, label="uninstall object")
        if is_redirected_path(item) or not (item.is_file() or item.is_dir()):
            raise ValueError(
                "Uninstall object contains an unknown redirected or special entry."
            )
    return paths


def releases(home: Path) -> list[Path]:
    directory = home / "releases"
    if not present(directory):
        return []
    if is_redirected_path(directory) or not directory.is_dir():
        raise ValueError("Release directory is redirected or invalid.")
    owned = []
    for path in directory.iterdir():
        if not managed_release(path, home):
            raise ValueError(
                "Uninstall refuses an unknown, modified or incomplete release: "
                + path.name
            )
        owned.append(path.resolve())
    return owned


def validate_runtime(home: Path, owned: list[Path]) -> tuple[Path, str] | None:
    runtime = home / "runtime"
    if not present(runtime):
        return None
    files = [p for p in regular_tree(runtime) if p.is_file()]
    pointer = runtime / "python"
    if not pointer.is_file() or pointer.stat().st_size > 4096:
        raise ValueError("Managed runtime Python pointer is invalid.")
    python = pointer.read_text().strip()
    if not Path(python).is_absolute():
        raise ValueError("Managed runtime Python pointer is not absolute.")
    for source in owned:
        version = str(read_release_manifest(source)["version"]) + "\n"
        valid = True
        for path in files:
            relative = path.relative_to(runtime).as_posix()
            data = path.read_bytes()
            if relative in ("VERSION", "hooks/VERSION"):
                expected = version.encode()
            elif relative == "python":
                expected = (python + "\n").encode()
            else:
                original = source / (
                    "skills/tenetora/" + relative
                    if relative.startswith("cli/")
                    else relative
                )
                if not original.is_file():
                    valid = False
                    break
                expected = original.read_bytes()
            if data != expected:
                valid = False
                break
        if valid and (runtime / "VERSION").is_file():
            return source, python
    raise ValueError(
        "Managed runtime is modified or cannot be matched to an authenticated release."
    )


def plan(home: Path, *, purge: bool, installer=None) -> dict:
    validate_unredirected_path(home, label="Tenetora uninstall home")
    if not present(home):
        return {
            "remove": [],
            "preserved": [],
            "snapshots": {},
            "purge": purge,
            "native_payloads": [],
        }
    regular_tree(home / "bin") if present(home / "bin") else None
    owned = releases(home)
    runtime = validate_runtime(home, owned)
    targets = []
    preserved = []
    native_payloads = []
    for name in ("current",):
        path = home / name
        if path.exists() or is_redirected_path(path):
            if path.resolve(strict=True) not in owned:
                raise ValueError(
                    "Current release does not point to an authenticated owned release."
                )
            targets.append(path)
    source_dir = home / "source"
    if present(source_dir):
        if is_redirected_path(source_dir) or not source_dir.is_dir():
            raise ValueError("Machine source directory is unsafe.")
        for path in source_dir.iterdir():
            if (
                path.name not in ("tenetora", "agent-harness")
                or path.resolve(strict=True) not in owned
            ):
                raise ValueError("Machine source contains an unowned entry.")
        targets.append(source_dir)
    bin_dir = home / "bin"
    if present(bin_dir):
        if runtime is None:
            raise ValueError("Launcher ownership requires a validated runtime.")
        from ensure_cli import posix_shim, windows_shim

        source, python = runtime
        for path in bin_dir.iterdir():
            name = path.stem if path.suffix == ".cmd" else path.name
            if name not in ("tenetora", "agent-harness") or path.suffix not in (
                "",
                ".cmd",
            ):
                raise ValueError("Machine bin contains an unowned entry.")
            generator = windows_shim if path.suffix == ".cmd" else posix_shim
            expected = generator(
                bin_dir,
                name,
                python_path=python,
                cli_path=source / "skills/tenetora/cli",
            )
            if path.read_text() != expected:
                raise ValueError(
                    "Managed launcher is modified or belongs to another source."
                )
        targets.append(bin_dir)
    if runtime is not None:
        targets.append(home / "runtime")
    if owned:
        targets.append(home / "releases")
    plugin_sources = home / "plugin-sources"
    if present(plugin_sources):
        regular_tree(plugin_sources)
        if any(plugin_sources.iterdir()):
            if installer is None:
                raise ValueError("Native payload ownership helper is unavailable.")
            from maintenance_native import authenticated_sources

            native_payloads = authenticated_sources(home, owned, installer)
        targets.append(plugin_sources)
    registry = home / "state/installations.json"
    if present(registry):
        validate_unredirected_file_path(registry, label="installation registry")
        targets.append(registry)
    known = {
        "current",
        "source",
        "bin",
        "runtime",
        "releases",
        "plugin-sources",
        "state",
        "logs",
        "backups",
        ".managed-runtime.lock",
    }
    for path in home.iterdir():
        if path.name not in known:
            if purge:
                raise ValueError(
                    "Purge refuses an unrecognized home entry: " + path.name
                )
            preserved.append(
                {"path": str(path), "reason": "unrecognized user-owned entry preserved"}
            )
        elif path.name in ("state", "logs", "backups"):
            regular_tree(path)
            if not purge:
                preserved.append(
                    {
                        "path": str(path),
                        "reason": "audit, recovery and preference data retained",
                    }
                )
        elif path.name == ".managed-runtime.lock":
            if not path.is_file() or is_redirected_path(path):
                raise ValueError("Runtime operation lock is not a regular file.")
    selected = [home] if purge else targets
    return {
        "remove": [str(p) for p in selected],
        "preserved": preserved,
        "snapshots": {str(p): snapshot(p) for p in selected},
        "purge": purge,
        "native_payloads": native_payloads,
    }


def validate(plan: dict) -> None:
    for raw, expected in plan["snapshots"].items():
        if snapshot(Path(raw)) != expected:
            raise ValueError(
                "An uninstall object changed after planning; no concurrent edit was overwritten."
            )
