#!/usr/bin/env python3
"""Record a bounded runtime observation for a host that has no Hook runtime.

The Claude/Codex/Cursor/OpenCode/Pi/ZCode adapters record observations from
their Hook processes. The DeepSeek Harness Cordis plugin has no Hook process, so
it calls this command once per session instead. The record uses the same machine
directory and schema the runtime `status`/`doctor` readers already understand.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent


def discover_cli_root() -> Path | None:
    candidates = [
        SCRIPT_DIR.parent / "cli",
        SCRIPT_DIR.parents[3] / "cli" if len(SCRIPT_DIR.parents) > 3 else None,
    ]
    for candidate in candidates:
        if candidate is not None and (candidate / "tenetora" / "__init__.py").is_file():
            return candidate
    return None


CLI_ROOT = discover_cli_root()
if CLI_ROOT is not None and str(CLI_ROOT) not in sys.path:
    sys.path.insert(0, str(CLI_ROOT))
if str(SCRIPT_DIR) not in sys.path:
    # The CLI loads command scripts through importlib without adding the
    # script directory, so sibling imports such as governance_trail need this.
    sys.path.insert(0, str(SCRIPT_DIR))

from tenetora.brand import machine_home  # noqa: E402
from tenetora.platform_contracts import supported_platforms  # noqa: E402
from tenetora.path_security import validate_existing_project_path  # noqa: E402
from governance_trail import append_event  # noqa: E402

try:
    from tenetora import __version__ as runtime_version
except Exception:  # noqa: BLE001 - a version is optional metadata
    runtime_version = "unknown"

SCHEMA_VERSION = 2
LOCK_TIMEOUT_SECONDS = 0.5
LOCK_STALE_SECONDS = 10.0


def observation_root() -> Path:
    configured = os.environ.get("TENETORA_OBSERVATION_DIR")
    if configured:
        return Path(configured).expanduser()
    return machine_home() / "state" / "runtime-observations"


def source_fingerprint(platform: str, root: Path, version: str) -> str:
    identity = f"{platform}\0{root.resolve(strict=False)}\0{version}"
    return hashlib.sha256(identity.encode("utf-8", errors="surrogatepass")).hexdigest()


def session_hash(session_id: str | None) -> str | None:
    if not session_id:
        return None
    return hashlib.sha256(session_id.encode("utf-8", errors="surrogatepass")).hexdigest()


def _hash_optional(value: str | None) -> str | None:
    return session_hash(value)


def record(
    root: Path,
    platform: str,
    event: str,
    session_id: str | None,
    *,
    dispatch_event: str | None = None,
    dispatch_id: str | None = None,
    child_id: str | None = None,
    owner_id: str | None = None,
    continuity_id: str | None = None,
    reason: str | None = None,
) -> dict[str, object]:
    project_hash = hashlib.sha256(str(root.resolve()).encode("utf-8", errors="surrogatepass")).hexdigest()
    directory = observation_root() / platform
    path = directory / f"{project_hash}.json"
    lock_path = directory / f".{project_hash}.lock"
    observed_at = dt.datetime.now(dt.timezone.utc).isoformat()
    existing: dict[str, object] = {}
    try:
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                existing = raw
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        existing = {}
    previous_session = existing.get("current_session") if isinstance(existing.get("current_session"), dict) else {}
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "platform": platform,
        "project_hash": project_hash,
        "source_fingerprint": source_fingerprint(platform, root, str(runtime_version)),
        "version": str(runtime_version),
        "event": event,
        "observed_at": observed_at,
        "current_session": {
            "session_hash": _hash_optional(session_id) or previous_session.get("session_hash"),
            "continuity_hash": _hash_optional(continuity_id) or previous_session.get("continuity_hash"),
            "provider": None,
            "model": None,
            "attempt_hash": _hash_optional(dispatch_id) or previous_session.get("attempt_hash"),
            "last_event": event,
            "observed_at": observed_at,
            "dispatch_observed": bool(previous_session.get("dispatch_observed")),
        },
        "last_dispatch_observation": existing.get("last_dispatch_observation"),
    }
    if dispatch_event:
        payload["current_session"]["dispatch_observed"] = dispatch_event in {"start", "completed", "failed"}
        payload["last_dispatch_observation"] = {
            "event": dispatch_event,
            "dispatch_hash": _hash_optional(dispatch_id),
            "child_hash": _hash_optional(child_id),
            "owner_hash": _hash_optional(owner_id),
            "session_hash": _hash_optional(session_id),
            "continuity_hash": _hash_optional(continuity_id),
            "reason": reason if dispatch_event == "unavailable" else None,
            "observed_at": observed_at,
        }
    directory.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    lock_fd: int | None = None
    try:
        while lock_fd is None:
            try:
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                try:
                    if time.time() - lock_path.stat().st_mtime > LOCK_STALE_SECONDS:
                        lock_path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError("runtime observation lock is busy")
                time.sleep(0.01)
        os.write(lock_fd, f"{os.getpid()}\n".encode("ascii"))
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{project_hash}.", suffix=".tmp", dir=str(directory)
        )
        temporary = Path(temporary_name)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    finally:
        lock_path.unlink(missing_ok=True)
    if dispatch_event:
        hashed_dispatch = _hash_optional(dispatch_id)
        if hashed_dispatch:
            stable_dispatch = f"ah-dispatch-{hashed_dispatch[:16]}"
        else:
            stable_dispatch = f"ah-dispatch-{project_hash[:16]}"
        if dispatch_event == "unavailable":
            trail_status = "unavailable"
            trail_type = "subagent-dispatch"
        elif dispatch_event == "failed":
            trail_status = "fail"
            trail_type = "subagent-lifecycle"
        else:
            trail_status = "pass"
            trail_type = "subagent-lifecycle"
        append_event(
            root,
            {
                "type": trail_type,
                "action": dispatch_event,
                "status": trail_status,
                "platform": platform,
                "dispatch_id": stable_dispatch,
                "dispatch_id_hash": hashed_dispatch,
                "child_id_hash": _hash_optional(child_id),
                "owner_id_hash": _hash_optional(owner_id),
                "session_id_hash": _hash_optional(session_id),
                "continuity_id_hash": _hash_optional(continuity_id),
                "reason_code": reason if dispatch_event == "unavailable" else None,
                "result_source": "dsh-native-tool-observation",
            },
        )
    return payload


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="tenetora observe",
        description="Record one bounded runtime observation for a Hook-less host.",
    )
    command.add_argument("--path", default=".", help="Governed project directory.")
    command.add_argument("--platform", required=True, help="Platform identifier recorded with the observation.")
    command.add_argument("--event", default="plugin-apply", help="Bounded event label.")
    command.add_argument("--session-id", default=None, help="Opaque host session id; stored only as a hash.")
    command.add_argument("--dispatch-event", choices=("start", "completed", "failed", "unavailable"), default=None)
    command.add_argument("--dispatch-id", default=None)
    command.add_argument("--child-id", default=None)
    command.add_argument("--owner-id", default=None)
    command.add_argument("--continuity-id", default=None)
    command.add_argument("--reason", default=None)
    command.add_argument("--json", action="store_true")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    platform = str(args.platform).strip().lower()
    if platform not in supported_platforms():
        print(f"Unsupported platform: {platform}", file=sys.stderr)
        return 2
    try:
        root = validate_existing_project_path(args.path)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not (root / ".tenetora").is_dir():
        print("Tenetora harness is missing; initialize the project first", file=sys.stderr)
        return 2
    try:
        payload = record(
            root,
            platform,
            str(args.event or "plugin-apply")[:64],
            args.session_id,
            dispatch_event=args.dispatch_event,
            dispatch_id=args.dispatch_id,
            child_id=args.child_id,
            owner_id=args.owner_id,
            continuity_id=args.continuity_id,
            reason=args.reason,
        )
    except (OSError, TimeoutError) as exc:
        print(f"Runtime observation was not recorded: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"status": "recorded", **payload}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
