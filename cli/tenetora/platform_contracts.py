"""Machine-readable platform capability contracts."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


REQUIRED_PLATFORM_FIELDS = {
    "adapter_id",
    "maximum_capability",
    "default_scope",
    "both_runtime_preference",
    "installer",
    "runtime_kind",
    "path_assumption",
    "locator_environment",
    "discovery_order",
    "trust_mechanism",
    "version_sources",
    "observation",
    "delegation",
    "events",
}
VALID_CAPABILITIES = {"SKILLS_ONLY", "ACTIVE", "ACTIVE_PARTIAL"}
VALID_SCOPES = {"global", "project"}
VALID_INSTALLERS = {"skills", "native-marketplace", "runtime-adapter", "zcode-native"}
VALID_PATH_ASSUMPTIONS = {"host-dependent", "standard", "restricted"}
VALID_DELEGATION_SUPPORT = {"supported", "limited", "unknown", "unsupported"}
EXECUTION_IDENTITY_FIELDS = {"session", "conversation", "continuity", "owner", "provider", "model", "attempt"}


def contract_path() -> Path:
    packaged = Path(__file__).with_name("platform-contracts.json")
    if packaged.is_file():
        return packaged
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "hooks" / "platform-contracts.json"
        if candidate.is_file():
            return candidate
    raise RuntimeError("Tenetora platform capability contract is missing")


@lru_cache(maxsize=1)
def load_platform_contracts() -> dict[str, dict[str, Any]]:
    payload = json.loads(contract_path().read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("platforms"), dict):
        raise RuntimeError("Tenetora platform capability contract is invalid")
    contracts: dict[str, dict[str, Any]] = payload["platforms"]
    for platform, contract in contracts.items():
        if not isinstance(contract, dict) or REQUIRED_PLATFORM_FIELDS - set(contract):
            raise RuntimeError(f"Tenetora platform contract is incomplete: {platform}")
        if contract["maximum_capability"] not in VALID_CAPABILITIES:
            raise RuntimeError(f"Tenetora platform capability is invalid: {platform}")
        if contract["default_scope"] not in VALID_SCOPES or contract["both_runtime_preference"] not in VALID_SCOPES:
            raise RuntimeError(f"Tenetora platform scope is invalid: {platform}")
        if contract["installer"] not in VALID_INSTALLERS:
            raise RuntimeError(f"Tenetora platform installer is invalid: {platform}")
        if contract["path_assumption"] not in VALID_PATH_ASSUMPTIONS:
            raise RuntimeError(f"Tenetora platform PATH contract is invalid: {platform}")
        for field in ("adapter_id", "runtime_kind", "trust_mechanism"):
            if not isinstance(contract[field], str) or not contract[field]:
                raise RuntimeError(f"Tenetora platform contract field is invalid: {platform}.{field}")
        for field in ("locator_environment", "discovery_order", "version_sources"):
            values = contract[field]
            if not isinstance(values, list) or not values or not all(isinstance(item, str) and item for item in values):
                raise RuntimeError(f"Tenetora platform contract list is invalid: {platform}.{field}")
        if not isinstance(contract["observation"], bool) or not isinstance(contract["events"], dict):
            raise RuntimeError(f"Tenetora platform observation contract is invalid: {platform}")
        execution_identity = contract.get("execution_identity")
        if not isinstance(execution_identity, dict) or set(execution_identity) != EXECUTION_IDENTITY_FIELDS:
            raise RuntimeError(f"Tenetora platform execution identity contract is incomplete: {platform}")
        for identity_field in EXECUTION_IDENTITY_FIELDS:
            values = execution_identity[identity_field]
            if (
                not isinstance(values, list)
                or not values
                or not all(isinstance(item, str) and item for item in values)
                or len(values) != len(set(values))
            ):
                raise RuntimeError(f"Tenetora platform execution identity fields are invalid: {platform}.{identity_field}")
        delegation = contract["delegation"]
        required_delegation = {
            "platform_support",
            "support_basis",
            "dispatch_label",
            "modes",
            "builtin_agents",
            "builtin_role_candidates",
            "isolation_contract",
        }
        if not isinstance(delegation, dict) or required_delegation - set(delegation):
            raise RuntimeError(f"Tenetora platform delegation contract is incomplete: {platform}")
        if delegation["platform_support"] not in VALID_DELEGATION_SUPPORT:
            raise RuntimeError(f"Tenetora platform delegation support is invalid: {platform}")
        for field in ("support_basis", "dispatch_label", "isolation_contract"):
            if not isinstance(delegation[field], str) or not delegation[field]:
                raise RuntimeError(f"Tenetora platform delegation field is invalid: {platform}.{field}")
        if not isinstance(delegation["modes"], list) or not all(
            isinstance(item, str) and item for item in delegation["modes"]
        ):
            raise RuntimeError(f"Tenetora platform delegation modes are invalid: {platform}")
        if not isinstance(delegation["builtin_agents"], list) or not all(
            isinstance(item, str) and item for item in delegation["builtin_agents"]
        ):
            raise RuntimeError(f"Tenetora platform builtin agents are invalid: {platform}")
        if not isinstance(delegation["builtin_role_candidates"], dict):
            raise RuntimeError(f"Tenetora platform builtin role candidates are invalid: {platform}")
        for event, keys in contract["events"].items():
            if not isinstance(event, str) or not event or not isinstance(keys, list) or not keys:
                raise RuntimeError(f"Tenetora platform event contract is invalid: {platform}")
            if not all(isinstance(key, str) and key for key in keys) or len(keys) != len(set(keys)):
                raise RuntimeError(f"Tenetora platform event keys are invalid: {platform}.{event}")
    adapter_ids = [contract["adapter_id"] for contract in contracts.values()]
    if len(adapter_ids) != len(set(adapter_ids)):
        raise RuntimeError("Tenetora platform adapter identifiers must be unique")
    return contracts


def platform_contract(platform: str) -> dict[str, Any]:
    try:
        return load_platform_contracts()[platform]
    except KeyError as exc:
        raise ValueError(f"Unsupported tool: {platform}") from exc


def supported_platforms() -> list[str]:
    return list(load_platform_contracts())
