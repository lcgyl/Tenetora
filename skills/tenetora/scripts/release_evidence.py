#!/usr/bin/env python3
"""Create a stable, fail-closed public release evidence index."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import ipaddress
import json
import re
import sys
import urllib.parse
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from harness_io import atomic_write_text  # noqa: E402
from path_security import validate_existing_project_path, validate_unredirected_file_path  # noqa: E402


COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PLATFORMS = ("linux", "macos", "windows")
SCHEMA_VERSION = 1


class EvidenceError(RuntimeError):
    pass


def parse_date(value: str) -> str:
    if not DATE_RE.fullmatch(value):
        raise EvidenceError("verified-at must be YYYY-MM-DD")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as error:
        raise EvidenceError("verified-at is not a valid calendar date") from error
    if parsed > dt.datetime.now(dt.timezone.utc).date():
        raise EvidenceError("verified-at cannot be in the future")
    return value


def public_url(value: str, label: str) -> str:
    try:
        parsed = urllib.parse.urlparse(value)
        port = parsed.port
    except ValueError as error:
        raise EvidenceError(f"{label} must be a public HTTPS URL") from error
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise EvidenceError(f"{label} must be a public HTTPS URL")
    if port not in (None, 443):
        raise EvidenceError(f"{label} must not use a custom port")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal")):
        raise EvidenceError(f"{label} must use a public hostname")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise EvidenceError(f"{label} must use a public hostname")
    if host.startswith("172."):
        second = host.split(".")[1] if len(host.split(".")) > 1 else ""
        if second.isdigit() and 16 <= int(second) <= 31:
            raise EvidenceError(f"{label} must use a public hostname")
    return value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package_version(artifact: Path) -> str:
    try:
        with zipfile.ZipFile(artifact) as archive:
            candidates = [name for name in archive.namelist() if name.endswith("/manifest.json")]
            if len(candidates) != 1:
                raise EvidenceError("artifact must contain exactly one manifest.json")
            payload = json.loads(archive.read(candidates[0]).decode("utf-8"))
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceError("artifact is not a readable Tenetora release ZIP") from error
    version = payload.get("version") if isinstance(payload, dict) else None
    if not isinstance(version, str) or not version:
        raise EvidenceError("artifact manifest has no version")
    return version


def parse_platforms(values: list[str]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for value in values:
        if "=" not in value:
            raise EvidenceError("platform result must use PLATFORM=passed")
        platform, status = value.split("=", 1)
        if platform not in PLATFORMS or platform in result:
            raise EvidenceError("platform results must contain linux, macos, and windows exactly once")
        if status != "passed":
            raise EvidenceError("all platform results must be passed")
        result[platform] = {"status": status}
    if tuple(sorted(result)) != tuple(sorted(PLATFORMS)):
        raise EvidenceError("platform results must contain linux, macos, and windows exactly once")
    return {platform: result[platform] for platform in PLATFORMS}


def evidence(args: argparse.Namespace) -> dict[str, object]:
    try:
        root = validate_existing_project_path(args.path, label="evidence project")
    except RuntimeError as error:
        raise EvidenceError(str(error)) from error
    artifact = Path(args.artifact).expanduser()
    if not artifact.is_absolute():
        artifact = root / artifact
    try:
        artifact = validate_unredirected_file_path(artifact, label="artifact")
    except RuntimeError as error:
        raise EvidenceError(str(error)) from error
    if not artifact.is_file():
        raise EvidenceError("artifact must be a regular file")
    version_file = root / "skills" / "tenetora" / "VERSION"
    try:
        version = version_file.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise EvidenceError("Tenetora VERSION is missing") from error
    if not version or package_version(artifact) != version:
        raise EvidenceError("artifact version does not match the current Tenetora VERSION")
    if not COMMIT_RE.fullmatch(args.commit):
        raise EvidenceError("commit must be a complete 40-character lowercase SHA-1")
    verified_at = parse_date(args.verified_at)
    workflow_url = public_url(args.workflow_url, "workflow URL")
    artifact_url = public_url(args.artifact_url, "artifact URL")
    platforms = parse_platforms(args.platform)
    digest = file_sha256(artifact)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "verified",
        "product": "tenetora",
        "version": version,
        "commit": args.commit,
        "workflow": {"status": "passed", "run_url": workflow_url},
        "artifact": {
            "name": artifact.name,
            "url": artifact_url,
            "sha256": digest,
        },
        "platforms": platforms,
        "verified_at": verified_at,
    }


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Create a stable public release evidence index.")
    command.add_argument("--path", default=".")
    command.add_argument("--artifact", required=True)
    command.add_argument("--workflow-url", required=True)
    command.add_argument("--artifact-url", required=True)
    command.add_argument("--commit", required=True)
    command.add_argument("--verified-at", required=True)
    command.add_argument("--platform", action="append", default=[])
    command.add_argument("--output", required=True)
    command.add_argument("--json", action="store_true")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        payload = evidence(args)
        output = Path(args.output).expanduser()
        if not output.is_absolute():
            output = validate_existing_project_path(args.path, label="evidence project") / output
        try:
            output = validate_unredirected_file_path(output, label="evidence output")
        except RuntimeError as error:
            raise EvidenceError(str(error)) from error
        output.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if output.exists() and output.read_text(encoding="utf-8") != encoded:
            raise EvidenceError("output exists with different evidence; choose a new path")
        atomic_write_text(output, encoded)
    except EvidenceError as error:
        result = {"status": "error", "error": str(error)}
        print(json.dumps(result, ensure_ascii=False) if args.json else f"Release evidence error: {error}")
        return 1
    result = {"status": "verified", "output": output.name, "version": payload["version"], "sha256": payload["artifact"]["sha256"]}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True) if args.json else "\n".join(f"{key}: {value}" for key, value in result.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
