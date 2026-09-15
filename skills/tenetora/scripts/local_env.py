"""Register project-local environment paths without storing their contents."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path, PurePosixPath

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from governance_trail import append_event  # noqa: E402
from harness_io import atomic_write_text  # noqa: E402
from path_security import validate_existing_project_path  # noqa: E402
from alignment_state import state_lock  # noqa: E402


REGISTRY_REL = Path(".tenetora/state/local-env.json")
SCHEMA_VERSION = 1
WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")


class LocalEnvError(RuntimeError):
    pass


def project_root(raw: str | Path) -> Path:
    try:
        root = validate_existing_project_path(raw, label="local environment project")
    except RuntimeError as error:
        raise LocalEnvError(str(error)) from error
    if not (root / ".tenetora").is_dir():
        raise LocalEnvError("Tenetora harness is missing; initialize the project first")
    return root


def registry_path(root: Path) -> Path:
    return root / REGISTRY_REL


def normalize_local_path(root: Path, raw: str) -> str:
    value = raw.strip().replace("\\", "/")
    if not value or value.startswith("/") or WINDOWS_ABSOLUTE.match(value):
        raise LocalEnvError("local environment paths must be project-relative")
    parts = [part for part in PurePosixPath(value).parts if part not in {"", "."}]
    if not parts or ".." in parts:
        raise LocalEnvError("local environment paths must stay inside the project")
    relative = "/".join(parts)
    if relative == ".tenetora" or relative.startswith(".tenetora/"):
        raise LocalEnvError("the Tenetora harness directory cannot be registered as local environment")
    candidate = (root / Path(*parts)).resolve(strict=False)
    try:
        candidate.relative_to(root.resolve(strict=False))
    except ValueError as error:
        raise LocalEnvError("local environment paths must stay inside the project") from error
    return relative


def _validate_payload(root: Path, payload: object) -> list[str]:
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise LocalEnvError("local environment registry schema is unsupported")
    paths = payload.get("paths")
    if not isinstance(paths, list) or any(not isinstance(item, str) for item in paths):
        raise LocalEnvError("local environment registry paths are invalid")
    return sorted({normalize_local_path(root, item) for item in paths})


def read_local_env_paths(root: Path) -> list[str]:
    path = registry_path(root)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LocalEnvError("local environment registry is unreadable; repair it explicitly") from error
    return _validate_payload(root, payload)


def load_local_env_paths(root: Path | None) -> set[str]:
    """Load extra protected paths; malformed state never weakens built-in checks."""

    if root is None:
        return set()
    try:
        return set(read_local_env_paths(root))
    except LocalEnvError:
        return set()


def write_local_env_paths(root: Path, paths: list[str]) -> None:
    path = registry_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": SCHEMA_VERSION, "paths": sorted(set(paths))}
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def mutate(args: argparse.Namespace) -> dict[str, object]:
    root = project_root(args.path)
    action = "list"
    changed_path = ""
    with state_lock(registry_path(root)):
        paths = read_local_env_paths(root)
        if args.allow is not None:
            action = "allow"
            changed_path = normalize_local_path(root, args.allow)
            if changed_path not in paths:
                paths.append(changed_path)
                write_local_env_paths(root, paths)
        elif args.revoke is not None:
            action = "revoke"
            changed_path = normalize_local_path(root, args.revoke)
            if changed_path in paths:
                paths.remove(changed_path)
                write_local_env_paths(root, paths)
    if action != "list":
        append_event(
            root,
            {
                "type": "local-env",
                "action": action,
                "status": "pass",
                "path": changed_path,
                "registered_count": len(paths),
            },
        )
    return {
        "status": "pass",
        "action": action,
        "registry": REGISTRY_REL.as_posix(),
        "paths": sorted(paths),
    }


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Protect project-local environment paths from destructive remediation.")
    actions = command.add_mutually_exclusive_group(required=True)
    actions.add_argument("--allow", metavar="PATH", help="Register a project-relative local environment path.")
    actions.add_argument("--list", action="store_true", help="List registered local environment paths.")
    actions.add_argument("--revoke", metavar="PATH", help="Remove a registered project-relative path.")
    command.add_argument("--path", default=".", help="Project path.")
    command.add_argument("--json", action="store_true", help="Print machine-readable output.")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = mutate(args)
    except LocalEnvError as error:
        if args.json:
            print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        else:
            print(f"Local environment error: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"Local environment registry: {result['registry']}")
        for path in result["paths"]:
            print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
