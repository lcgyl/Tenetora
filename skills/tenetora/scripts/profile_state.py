"""Inspect and change the project governance policy profile with protected gates."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from alignment_state import OWNER_ID_RE, state_lock, utc_now, validate_identity  # noqa: E402
from governance_trail import append_event  # noqa: E402
from harness_io import atomic_write_text  # noqa: E402
from path_security import validate_existing_project_path  # noqa: E402


PROFILE_REL = Path(".tenetora/state/policy-profile.json")
SCHEMA_VERSION = 1
PROFILES = ("light", "standard", "strict")
PROFILE_RANK = {name: index for index, name in enumerate(PROFILES)}
PROTECTED_GATES = (
    "secret-scan",
    "local-path-scan",
    "identity-check",
    "destructive-action-confirmation",
)
SAFE_TEXT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+/-]{0,127}")


class ProfileError(RuntimeError):
    pass


def profile_path(root: Path) -> Path:
    return root / PROFILE_REL


def project_root(raw: Path | str) -> Path:
    try:
        return validate_existing_project_path(raw, label="profile project")
    except RuntimeError as error:
        raise ProfileError(str(error)) from error


def parse_profile(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProfileError("policy profile is corrupt; repair it explicitly before use") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ProfileError("policy profile schema is unsupported")
    if payload.get("profile") not in PROFILES:
        raise ProfileError("policy profile value is invalid")
    if not isinstance(payload.get("revision"), int) or payload["revision"] < 0:
        raise ProfileError("policy profile revision is invalid")
    if payload.get("ci_minimum") not in PROFILES:
        raise ProfileError("policy profile CI minimum is invalid")
    if list(payload.get("protected_gates", [])) != list(PROTECTED_GATES):
        raise ProfileError("policy profile protected gates are invalid")
    if not isinstance(payload.get("owner_id"), str) or not payload["owner_id"]:
        raise ProfileError("policy profile owner is missing")
    if not isinstance(payload.get("reason"), str) or not payload["reason"].strip():
        raise ProfileError("policy profile reason is missing")
    return payload


def default_profile() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "profile": "standard",
        "source": "default",
        "owner_id": "",
        "revision": 0,
        "ci_minimum": "standard",
        "protected_gates": list(PROTECTED_GATES),
        "reason": "no project profile configured; standard defaults apply",
        "changed_at": "",
    }


def inspect_profile(args: argparse.Namespace) -> dict[str, object]:
    root = project_root(args.path)
    payload = parse_profile(profile_path(root)) or default_profile()
    result = dict(payload)
    result["project_profile_path"] = PROFILE_REL.as_posix()
    result["effective_profile"] = result["profile"]
    result["protected_gates_cannot_be_disabled"] = True
    return result


def set_profile(args: argparse.Namespace) -> dict[str, object]:
    root = project_root(args.path)
    profile = str(args.profile or "").strip()
    if profile not in PROFILES:
        raise ProfileError("profile must be light, standard, or strict")
    owner = str(args.owner_id or "").strip()
    try:
        owner = validate_identity(owner, "owner id", OWNER_ID_RE)
    except Exception as error:
        raise ProfileError("an owner id is required") from error
    reason = str(args.reason or "").strip()
    if not reason or len(reason) > 256 or not SAFE_TEXT_RE.fullmatch(reason.replace(" ", "-")):
        raise ProfileError("a concise safe reason is required")
    path = profile_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    provided_admin = str(args.admin_id or "").strip()
    configured_admin = os.environ.get("TENETORA_PROFILE_ADMIN_ID", "").strip()
    if provided_admin:
        try:
            provided_admin = validate_identity(provided_admin, "administrator id", OWNER_ID_RE)
        except Exception as error:
            raise ProfileError("administrator id is invalid") from error
        if not configured_admin or provided_admin != configured_admin:
            raise ProfileError("administrator id is not configured")
    if configured_admin:
        try:
            configured_admin = validate_identity(configured_admin, "configured administrator id", OWNER_ID_RE)
        except Exception as error:
            raise ProfileError("configured administrator id is invalid") from error
    is_admin = False
    with state_lock(path):
        current = parse_profile(path)
        if current is not None:
            if args.expected_revision is None:
                raise ProfileError("--expected-revision is required when changing an existing profile")
            if int(args.expected_revision) != int(current["revision"]):
                raise ProfileError("policy profile changed concurrently; reload inspect and retry")
            is_owner = owner == current["owner_id"]
            is_admin = bool(provided_admin and configured_admin and provided_admin == configured_admin)
            if not is_owner and not is_admin:
                raise ProfileError("only the profile owner or configured administrator may change the profile")
            if PROFILE_RANK[profile] < PROFILE_RANK[str(current["profile"])]:
                expected = f"downgrade:{current['revision']}"
                if not is_admin or args.admin_confirmation != expected:
                    raise ProfileError("profile downgrade requires administrator confirmation")
            ci_minimum = str(current["ci_minimum"])
            if PROFILE_RANK[profile] < PROFILE_RANK[ci_minimum]:
                raise ProfileError("profile cannot be lower than the CI minimum profile")
            revision = int(current["revision"]) + 1
        else:
            ci_minimum = str(args.ci_minimum or "standard")
            if ci_minimum not in PROFILES:
                raise ProfileError("CI minimum profile is invalid")
            if PROFILE_RANK[profile] < PROFILE_RANK[ci_minimum]:
                raise ProfileError("profile cannot be lower than the CI minimum profile")
            revision = 1
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "profile": profile,
            "source": "administrator" if is_admin else "owner",
            "owner_id": owner,
            "revision": revision,
            "ci_minimum": ci_minimum,
            "protected_gates": list(PROTECTED_GATES),
            "reason": reason,
            "changed_at": utc_now(),
        }
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    append_event(
        root,
        {
            "type": "policy-profile",
            "action": "set",
            "status": "pass",
            "profile": profile,
            "source": payload["source"],
            "owner_id": owner,
            "revision": revision,
            "ci_minimum": ci_minimum,
            "reason": reason,
        },
    )
    return payload


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Inspect or change a protected governance policy profile.")
    actions = command.add_mutually_exclusive_group(required=True)
    actions.add_argument("--inspect", action="store_true")
    actions.add_argument("--set", action="store_true")
    command.add_argument("--path", default=".")
    command.add_argument("--profile", choices=PROFILES)
    command.add_argument("--owner-id")
    command.add_argument("--admin-id")
    command.add_argument("--admin-confirmation")
    command.add_argument("--expected-revision", type=int)
    command.add_argument("--ci-minimum", choices=PROFILES)
    command.add_argument("--reason")
    command.add_argument("--json", action="store_true")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = inspect_profile(args) if args.inspect else set_profile(args)
    except ProfileError as error:
        if args.json:
            print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        else:
            print(f"Profile error: {error}")
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        for key, value in result.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
