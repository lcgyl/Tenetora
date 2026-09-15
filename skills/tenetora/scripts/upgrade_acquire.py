#!/usr/bin/env python3
"""Acquire and safely extract a versioned Tenetora release for self-upgrade."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from upgrade_contract import UpgradeContractError, normalize_manifest, release_contract_fields
from path_security import is_redirected_path, validate_existing_project_path


DEFAULT_MANIFEST_URL = (
    "https://github.com/lcgyl/Tenetora/releases/latest/download/manifest.json"
)
DEFAULT_ZIP_URL = (
    "https://github.com/lcgyl/Tenetora/releases/latest/download/tenetora-latest.zip"
)
RELEASE_MIRROR_ENV = "TENETORA_RELEASE_MIRROR"
RELEASE_MANIFEST_ENV = "TENETORA_RELEASE_MANIFEST_URL"
RELEASE_ZIP_ENV = "TENETORA_RELEASE_ZIP_URL"
MAX_RELEASE_URL_LENGTH = 4096
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_EXTRACTED_FILES = 4096
MAX_EXTRACTED_BYTES = 256 * 1024 * 1024
EXTERNAL_DOWNLOAD_TIMEOUT = 90
EXTERNAL_OUTPUT_PLACEHOLDER = "<tenetora-download-output>"
IS_WINDOWS = os.name == "nt"


class UpgradeAcquireError(RuntimeError):
    """Raised when an upgrade source cannot be authenticated or extracted."""


def _configured_value(environment: Mapping[str, str], name: str) -> str | None:
    raw = environment.get(name)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _validate_release_url(value: str, *, label: str) -> str:
    if not isinstance(value, str):
        raise UpgradeAcquireError(f"{label} must be a string")
    value = value.strip()
    if not value or len(value) > MAX_RELEASE_URL_LENGTH or any(char.isspace() for char in value):
        raise UpgradeAcquireError(f"{label} is invalid")
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise UpgradeAcquireError(f"{label} is invalid") from exc
    if parsed.scheme.lower() != "https" or not hostname:
        raise UpgradeAcquireError(f"{label} must use an HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise UpgradeAcquireError(f"{label} must not contain credentials")
    if parsed.query or parsed.fragment:
        raise UpgradeAcquireError(f"{label} must not contain a query or fragment")
    return value


def resolve_release_urls(
    *,
    manifest_url: str | None = None,
    zip_url: str | None = None,
    mirror_url: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Resolve one release source without persisting its URL or credentials.

    Explicit CLI values take precedence over environment configuration. A
    mirror is a directory containing ``manifest.json`` and
    ``tenetora-latest.zip``; direct URLs must be supplied as a complete pair.
    """

    environment = os.environ if environ is None else environ
    explicit_values = (manifest_url, zip_url, mirror_url)
    if any(value is not None for value in explicit_values):
        if mirror_url is not None and (manifest_url is not None or zip_url is not None):
            raise UpgradeAcquireError("release source options are mutually exclusive")
        if mirror_url is not None:
            base = _validate_release_url(mirror_url, label="release mirror URL")
            base = base.rstrip("/") + "/"
            return (
                urllib.parse.urljoin(base, "manifest.json"),
                urllib.parse.urljoin(base, "tenetora-latest.zip"),
            )
        if manifest_url is None or zip_url is None:
            raise UpgradeAcquireError("release manifest and ZIP URLs must be supplied together")
        return (
            _validate_release_url(manifest_url, label="release manifest URL"),
            _validate_release_url(zip_url, label="release ZIP URL"),
        )

    configured_mirror = _configured_value(environment, RELEASE_MIRROR_ENV)
    configured_manifest = _configured_value(environment, RELEASE_MANIFEST_ENV)
    configured_zip = _configured_value(environment, RELEASE_ZIP_ENV)
    if configured_mirror is not None and (configured_manifest is not None or configured_zip is not None):
        raise UpgradeAcquireError(
            "release source environment options are mutually exclusive: "
            f"{RELEASE_MIRROR_ENV} cannot be combined with {RELEASE_MANIFEST_ENV} or {RELEASE_ZIP_ENV}"
        )
    if configured_manifest is not None or configured_zip is not None:
        if configured_manifest is None or configured_zip is None:
            raise UpgradeAcquireError(
                f"{RELEASE_MANIFEST_ENV} and {RELEASE_ZIP_ENV} must be configured together"
            )
        return (
            _validate_release_url(configured_manifest, label="release manifest URL"),
            _validate_release_url(configured_zip, label="release ZIP URL"),
        )
    if configured_mirror is not None:
        base = _validate_release_url(configured_mirror, label="release mirror URL")
        base = base.rstrip("/") + "/"
        return (
            urllib.parse.urljoin(base, "manifest.json"),
            urllib.parse.urljoin(base, "tenetora-latest.zip"),
        )
    return DEFAULT_MANIFEST_URL, DEFAULT_ZIP_URL


def release_source_configured(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether environment configuration should override a developer checkout."""

    environment = os.environ if environ is None else environ
    return any(
        _configured_value(environment, name) is not None
        for name in (RELEASE_MIRROR_ENV, RELEASE_MANIFEST_ENV, RELEASE_ZIP_ENV)
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_with_command(command: list[str], destination: Path, maximum: int) -> bool:
    """Download to a private temporary file and atomically publish on success."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".part",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        command = [
            str(temporary) if item == EXTERNAL_OUTPUT_PLACEHOLDER else item
            for item in command
        ]
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=EXTERNAL_DOWNLOAD_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode != 0 or not temporary.is_file():
            return False
        try:
            if not stat.S_ISREG(temporary.lstat().st_mode):
                return False
            size = temporary.stat().st_size
        except OSError:
            return False
        if size > maximum:
            raise UpgradeAcquireError("download exceeds the bounded size limit")
        os.replace(temporary, destination)
        return True
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _external_download_commands(url: str, destination: Path) -> list[list[str]]:
    commands: list[list[str]] = []
    curl = shutil.which("curl")
    if curl:
        commands.append(
            [
                curl,
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--retry",
                "2",
                "--connect-timeout",
                "10",
                "--max-time",
                str(EXTERNAL_DOWNLOAD_TIMEOUT),
                "--user-agent",
                "Tenetora-Upgrader/1",
                "--output",
                EXTERNAL_OUTPUT_PLACEHOLDER,
                url,
            ]
        )
    if IS_WINDOWS:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell:
            commands.append(
                [
                    powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "$ErrorActionPreference = 'Stop'; Invoke-WebRequest -Uri $args[0] -OutFile $args[1]",
                    url,
                    EXTERNAL_OUTPUT_PLACEHOLDER,
                ]
            )
    return commands


def _download(url: str, destination: Path, maximum: int) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "Tenetora-Upgrader/1"})
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".part",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    python_error: Exception | None = None
    try:
        try:
            with urllib.request.urlopen(request, timeout=30) as response, temporary.open("wb") as output:
                total = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > maximum:
                        raise UpgradeAcquireError("download exceeds the bounded size limit")
                    output.write(chunk)
            os.replace(temporary, destination)
            return
        except UpgradeAcquireError:
            raise
        except (OSError, urllib.error.URLError) as exc:
            python_error = exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    for command in _external_download_commands(url, destination):
        if _download_with_command(command, destination, maximum):
            return

    raise UpgradeAcquireError("release download failed") from python_error


def read_manifest(path: Path, *, require_artifact: bool) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise UpgradeAcquireError("release manifest exceeds the bounded size limit")
        payload = json.loads(path.read_text(encoding="utf-8"))
        return normalize_manifest(payload, require_artifact=require_artifact)
    except (OSError, json.JSONDecodeError, UpgradeContractError) as exc:
        if isinstance(exc, UpgradeAcquireError):
            raise
        raise UpgradeAcquireError(f"release manifest is invalid: {exc}") from exc


def _validate_acquisition_options(
    *,
    manifest_url: str | None,
    zip_url: str | None,
    mirror_url: str | None,
    zip_file: Path | None,
    source_dir: Path | None,
) -> None:
    source_options = (zip_file is not None, source_dir is not None)
    remote_options = (manifest_url is not None, zip_url is not None, mirror_url is not None)
    if sum(source_options) > 1 or any(remote_options) and any(source_options):
        raise UpgradeAcquireError("release source options are mutually exclusive")


def inspect_release_manifest(
    workdir: Path,
    *,
    manifest_url: str | None = None,
    zip_url: str | None = None,
    mirror_url: str | None = None,
    zip_file: Path | None = None,
    expected_sha256: str = "",
) -> tuple[dict[str, Any], str]:
    """Authenticate enough release metadata to decide whether a download is needed.

    Remote upgrades intentionally fetch only ``manifest.json`` here. The ZIP is
    verified by :func:`acquire_release` only after the caller has established
    that an update or an explicit repair was requested.
    """

    workdir.mkdir(parents=True, exist_ok=True)
    _validate_acquisition_options(
        manifest_url=manifest_url,
        zip_url=zip_url,
        mirror_url=mirror_url,
        zip_file=zip_file,
        source_dir=None,
    )
    if zip_file is None:
        manifest_url, _zip_url = resolve_release_urls(
            manifest_url=manifest_url,
            zip_url=zip_url,
            mirror_url=mirror_url,
        )
        manifest_path = workdir / "manifest.json"
        _download(manifest_url, manifest_path, MAX_MANIFEST_BYTES)
        return read_manifest(manifest_path, require_artifact=True), "release"

    source_path = zip_file.expanduser().resolve(strict=True)
    try:
        if source_path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise UpgradeAcquireError("release ZIP exceeds the bounded size limit")
    except OSError as exc:
        raise UpgradeAcquireError("release ZIP is unreadable") from exc
    expected = expected_sha256.strip().lower() or companion_sha256(source_path) or ""
    if not expected:
        raise UpgradeAcquireError("offline release ZIP requires --sha256 or a sibling .sha256 file")
    actual = file_sha256(source_path)
    if actual != expected:
        raise UpgradeAcquireError("release ZIP SHA256 mismatch")
    manifest = embedded_manifest(source_path)
    manifest["sha256"] = actual
    return manifest, "offline"


def companion_sha256(path: Path) -> str | None:
    companion = path.with_name(path.name + ".sha256")
    if not companion.is_file():
        return None
    try:
        digest = companion.read_text(encoding="utf-8").split()[0].strip().lower()
    except (OSError, IndexError):
        return None
    return digest if len(digest) == 64 and all(char in "0123456789abcdef" for char in digest) else None


def embedded_manifest(archive_path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            payload = json.loads(archive.read("tenetora/manifest.json"))
    except (OSError, KeyError, RuntimeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise UpgradeAcquireError("release ZIP has no valid embedded manifest") from exc
    try:
        return normalize_manifest(payload, require_artifact=False)
    except UpgradeContractError as exc:
        raise UpgradeAcquireError(f"embedded release manifest is invalid: {exc}") from exc


def safe_extract(archive_path: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    total = 0
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_EXTRACTED_FILES:
                raise UpgradeAcquireError("release ZIP file count is invalid")
            seen: set[str] = set()
            portable_seen: set[str] = set()
            file_names: set[str] = set()
            directory_names: set[str] = set()
            for info in infos:
                path = PurePosixPath(info.filename)
                if path.is_absolute() or "\x00" in info.filename or "\\" in info.filename or not path.parts or path.parts[0] != "tenetora" or ".." in path.parts:
                    raise UpgradeAcquireError("release ZIP contains an unsafe path")
                normalized = path.as_posix().rstrip("/")
                if normalized in seen:
                    raise UpgradeAcquireError("release ZIP contains duplicate entries")
                seen.add(normalized)
                portable_name = unicodedata.normalize("NFC", normalized).casefold()
                if portable_name in portable_seen:
                    raise UpgradeAcquireError("release ZIP contains portable path collisions")
                portable_seen.add(portable_name)
                mode = (info.external_attr >> 16) & 0o170000
                if stat.S_ISLNK(mode):
                    raise UpgradeAcquireError("release ZIP contains a symbolic link")
                if info.is_dir():
                    directory_names.add(normalized)
                else:
                    file_names.add(normalized)
            if file_names & directory_names or any(
                any(parent in file_names for parent in PurePosixPath(name).parents)
                for name in file_names
            ):
                raise UpgradeAcquireError("release ZIP contains a file and directory path collision")
            total = sum(int(info.file_size) for info in infos)
            if total > MAX_EXTRACTED_BYTES:
                raise UpgradeAcquireError("release ZIP expands beyond the bounded size limit")
            archive.extractall(destination)
            if os.name != "nt":
                for info in infos:
                    if info.is_dir():
                        continue
                    permissions = (info.external_attr >> 16) & 0o777
                    if permissions:
                        (destination / PurePosixPath(info.filename)).chmod(permissions)
    except UpgradeAcquireError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise UpgradeAcquireError("release ZIP extraction failed") from exc
    root = destination / "tenetora"
    if not root.is_dir() or is_redirected_path(root):
        raise UpgradeAcquireError("release ZIP root is invalid")
    return root


def acquire_release(
    workdir: Path,
    *,
    manifest_url: str | None = None,
    zip_url: str | None = None,
    mirror_url: str | None = None,
    zip_file: Path | None = None,
    expected_sha256: str = "",
    source_dir: Path | None = None,
    prevalidated_manifest: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any], str]:
    workdir.mkdir(parents=True, exist_ok=True)
    _validate_acquisition_options(
        manifest_url=manifest_url,
        zip_url=zip_url,
        mirror_url=mirror_url,
        zip_file=zip_file,
        source_dir=source_dir,
    )
    if prevalidated_manifest is not None and (zip_file is not None or source_dir is not None):
        raise UpgradeAcquireError("prevalidated release manifest requires a remote release source")
    if source_dir is not None:
        try:
            requested_source = validate_existing_project_path(
                source_dir,
                label="developer source",
            )
        except RuntimeError as exc:
            raise UpgradeAcquireError(str(exc)) from exc
        if is_redirected_path(requested_source):
            raise UpgradeAcquireError("developer source must not be a symbolic link")
        original = requested_source.resolve(strict=True)
        root = workdir / "developer-source" / "tenetora"
        shutil.copytree(
            original,
            root,
            symlinks=True,
            ignore=shutil.ignore_patterns(
                ".git", "__pycache__", "*.pyc", ".pytest_cache", "dist", "tests", "internal"
            ),
        )
        entries = list(root.rglob("*"))
        if any(is_redirected_path(path) or (not path.is_file() and not path.is_dir()) for path in entries):
            raise UpgradeAcquireError("developer source contains a symbolic link or special file")
        version = (root / "skills" / "tenetora" / "VERSION").read_text(encoding="utf-8").strip()
        commit_result = subprocess.run(
            ["git", "-C", str(original), "rev-parse", "--short", "HEAD"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        files = sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
            and not is_redirected_path(path)
            and path.relative_to(root).as_posix() != "manifest.json"
        )
        manifest = {
            "name": "tenetora",
            "version": version,
            "commit": commit_result.stdout.strip() or "developer",
            "format": "tenetora-release-zip-v1",
            "installer_protocol": 3,
            "zip_root": "tenetora",
            "files": files,
            "file_sha256": {
                relative: file_sha256(root / relative)
                for relative in files
            },
            **release_contract_fields(),
        }
        (root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return root, normalize_manifest(manifest, require_artifact=False), "developer-source"

    archive = workdir / "tenetora.zip"
    external: dict[str, Any] | None = None
    if zip_file is None:
        manifest_url, zip_url = resolve_release_urls(
            manifest_url=manifest_url,
            zip_url=zip_url,
            mirror_url=mirror_url,
        )
        if prevalidated_manifest is None:
            manifest_path = workdir / "manifest.json"
            _download(manifest_url, manifest_path, MAX_MANIFEST_BYTES)
            external = read_manifest(manifest_path, require_artifact=True)
        else:
            external = normalize_manifest(prevalidated_manifest, require_artifact=True)
        _download(zip_url, archive, MAX_ARCHIVE_BYTES)
        expected = str(external["sha256"])
        source = "release"
    else:
        source_path = zip_file.expanduser().resolve(strict=True)
        if source_path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise UpgradeAcquireError("release ZIP exceeds the bounded size limit")
        archive.write_bytes(source_path.read_bytes())
        expected = expected_sha256.strip().lower() or companion_sha256(source_path) or ""
        if not expected:
            raise UpgradeAcquireError("offline release ZIP requires --sha256 or a sibling .sha256 file")
        source = "offline"
    actual = file_sha256(archive)
    if actual != expected:
        raise UpgradeAcquireError("release ZIP SHA256 mismatch")
    embedded = embedded_manifest(archive)
    if external is not None:
        for field in (
            "name", "version", "commit", "format", "installer_protocol", "bridge_protocol",
            "minimum_bridge_version", "upgrade_entrypoint", "state_schemas", "zip_root", "files",
            "file_sha256",
        ):
            if external.get(field) != embedded.get(field):
                raise UpgradeAcquireError("external and embedded release manifests disagree")
    root = safe_extract(archive, workdir / "extract")
    actual_files = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not is_redirected_path(path) and path.relative_to(root).as_posix() != "manifest.json"
    )
    if actual_files != embedded["files"]:
        raise UpgradeAcquireError("release ZIP file inventory does not match its manifest")
    actual_hashes = {
        relative: file_sha256(root / relative).lower()
        for relative in actual_files
    }
    if actual_hashes != embedded["file_sha256"]:
        raise UpgradeAcquireError("release ZIP file contents do not match its manifest")
    # The package digest authenticates the archive rather than an individual
    # payload file, so it is intentionally excluded from file_sha256. Persist
    # it only after both the archive and extracted inventory have been verified.
    embedded["sha256"] = actual
    (root / "manifest.json").write_text(
        json.dumps(embedded, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    selected = dict(external or embedded)
    selected["sha256"] = actual
    return root, selected, source
