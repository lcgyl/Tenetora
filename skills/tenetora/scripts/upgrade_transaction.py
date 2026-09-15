#!/usr/bin/env python3
"""Durable, revisioned machine-upgrade transaction journal."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
TERMINAL = {"completed", "rolled-back", "blocked"}

try:
    from tenetora.file_lock import locked_file
    from tenetora.path_security import validate_unredirected_file_path, validate_unredirected_path
except ImportError:
    import sys

    CLI_DIR = Path(__file__).resolve().parents[1] / "cli"
    if str(CLI_DIR) not in sys.path:
        sys.path.insert(0, str(CLI_DIR))
    from tenetora.file_lock import locked_file
    from tenetora.path_security import validate_unredirected_file_path, validate_unredirected_path


class UpgradeTransactionError(RuntimeError):
    """Raised when transaction state changes concurrently or is invalid."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def transaction_path(home: Path) -> Path:
    try:
        managed_home = validate_unredirected_path(home.expanduser(), label="Tenetora upgrade home")
        return validate_unredirected_file_path(
            managed_home / "state" / "upgrade-transaction.json",
            label="upgrade transaction journal",
        )
    except RuntimeError as exc:
        raise UpgradeTransactionError(str(exc)) from exc


@contextmanager
def transaction_lock(home: Path):
    path = transaction_path(home)
    with locked_file(path.with_name(".upgrade-transaction.lock")):
        yield


def _write(path: Path, payload: dict[str, Any]) -> None:
    try:
        path = validate_unredirected_file_path(path, label="upgrade transaction journal")
    except RuntimeError as exc:
        raise UpgradeTransactionError(str(exc)) from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def canonical_plan_sha256(plan: dict[str, Any]) -> str:
    canonical = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def process_alive(process_id: int) -> bool:
    if process_id <= 0:
        return False
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def load(home: Path) -> dict[str, Any] | None:
    path = transaction_path(home)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpgradeTransactionError("upgrade transaction journal is unreadable") from exc
    if not isinstance(payload, dict) or payload.get("version") != SCHEMA_VERSION:
        raise UpgradeTransactionError("upgrade transaction journal schema is invalid")
    if type(payload.get("revision")) is not int or payload["revision"] < 1:
        raise UpgradeTransactionError("upgrade transaction revision is invalid")
    return payload


def begin(home: Path, *, source_version: str, target_version: str, plan: dict[str, Any]) -> dict[str, Any]:
    with transaction_lock(home):
        current = load(home)
        if current is not None and current.get("status") not in TERMINAL:
            raise UpgradeTransactionError("an unfinished upgrade transaction already exists")
        now = utc_now()
        payload = {
            "version": SCHEMA_VERSION,
            "revision": 1,
            "transaction_id": f"upgrade-{uuid.uuid4().hex}",
            "status": "running",
            "stage": "planned",
            "source_version": source_version,
            "target_version": target_version,
            "plan_sha256": canonical_plan_sha256(plan),
            "process_id": os.getpid(),
            "owner_id": f"process-{uuid.uuid4().hex}",
            "started_at": now,
            "updated_at": now,
            "last_error_code": "",
            "receipts": [{"stage": "planned", "status": "running", "at": now, "error_code": ""}],
        }
        _write(transaction_path(home), payload)
        return payload


def resume(home: Path, *, target_version: str, plan: dict[str, Any]) -> dict[str, Any]:
    with transaction_lock(home):
        payload = load(home)
        if payload is None or payload.get("status") in TERMINAL:
            raise UpgradeTransactionError("no unfinished upgrade transaction is available to resume")
        if payload.get("target_version") != target_version:
            raise UpgradeTransactionError("unfinished upgrade transaction targets another release")
        if payload.get("plan_sha256") != canonical_plan_sha256(plan):
            raise UpgradeTransactionError("unfinished upgrade transaction plan no longer matches")
        prior_process = payload.get("process_id")
        if type(prior_process) is int and prior_process != os.getpid() and process_alive(prior_process):
            raise UpgradeTransactionError("unfinished upgrade transaction still belongs to a live process")
        updated = dict(payload)
        updated["revision"] = int(payload["revision"]) + 1
        updated["process_id"] = os.getpid()
        updated["owner_id"] = f"process-{uuid.uuid4().hex}"
        updated["updated_at"] = utc_now()
        receipts = list(payload.get("receipts", [])) if isinstance(payload.get("receipts"), list) else []
        receipts.append({"stage": str(payload.get("stage", "planned")), "status": "resumed", "at": updated["updated_at"], "error_code": ""})
        updated["receipts"] = receipts[-100:]
        _write(transaction_path(home), updated)
        return updated


def advance(
    home: Path,
    transaction_id: str,
    expected_revision: int,
    *,
    stage: str,
    status: str = "running",
    error_code: str = "",
) -> dict[str, Any]:
    with transaction_lock(home):
        payload = load(home)
        if payload is None or payload.get("transaction_id") != transaction_id:
            raise UpgradeTransactionError("upgrade transaction identity mismatch")
        if payload.get("revision") != expected_revision:
            raise UpgradeTransactionError("upgrade transaction changed concurrently")
        updated = dict(payload)
        updated.update(
            revision=expected_revision + 1,
            stage=stage,
            status=status,
            updated_at=utc_now(),
            last_error_code=error_code,
        )
        receipts = list(payload.get("receipts", [])) if isinstance(payload.get("receipts"), list) else []
        receipts.append({"stage": stage, "status": status, "at": updated["updated_at"], "error_code": error_code})
        updated["receipts"] = receipts[-100:]
        _write(transaction_path(home), updated)
        return updated
