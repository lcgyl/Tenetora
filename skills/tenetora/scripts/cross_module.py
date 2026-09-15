#!/usr/bin/env python3
"""Validate explicit producer/consumer evidence in the existing module index."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from governance_trail import sanitize_local_paths  # noqa: E402
from path_security import is_redirected_path, validate_existing_project_path  # noqa: E402


INDEX_REL = ".tenetora/state/modules/index.json"
CLAIM_REFERENCE = re.compile(r"^ah-(?:claim|align)-[0-9a-f]{32}$")
SECRET_PATTERN = re.compile(
    r"(?:glpat-[A-Za-z0-9._-]{16,}|ghp_[A-Za-z0-9_]{20,}|"
    r"\b(?:TOKEN|SECRET|PASSWORD)\b\s*[:=]\s*['\"]?\S{12,})",
    re.IGNORECASE,
)
ABSOLUTE_PATH_TEXT = re.compile(r"(?<![A-Za-z0-9_.-])(?:/|[A-Za-z]:[\\/])")
MODULE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RELATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
ALLOWED_EVIDENCE_KINDS = {"source", "document", "test", "claim"}
ALLOWED_CONFIDENCE = {"explicit", "audited", "unknown"}
ALLOWED_STATUS = {"active", "unknown", "retired"}


def existing_project_path(raw: str) -> Path:
    try:
        return validate_existing_project_path(raw)
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(
        prog="tenetora cross-impact",
        description="Validate explicit producer/consumer evidence and its verification matrix.",
        add_help=False,
    )
    command_parser.add_argument("--help", action="help", help="Show this help message and exit.")
    action = command_parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="Validate module relationships.")
    action.add_argument("--analyze", action="store_true", help="Trace changed producers to explicit consumers.")
    command_parser.add_argument("-p", "--path", default=".", type=existing_project_path, metavar="<project-dir>")
    command_parser.add_argument("--producer", action="append", default=[], help="Changed producer module; repeatable.")
    command_parser.add_argument("--changed-file", action="append", default=[], help="Project-relative changed file; repeatable.")
    command_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return command_parser


def safe_text(value: object, field: str, *, required: bool = True) -> str:
    text = str(sanitize_local_paths(value if isinstance(value, str) else "")).strip()
    if required and not text:
        raise ValueError(f"{field} is required")
    if SECRET_PATTERN.search(text):
        raise ValueError(f"{field} may contain a credential")
    return text[:1000]


def normalize_relative(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip().replace("\\", "/")
    if not value or value.startswith(("/", "~/")) or re.match(r"^[A-Za-z]:/", value):
        return None
    parts = [part for part in value.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def unredirected_file_problem(root: Path, relative: str) -> str | None:
    current = root
    for part in relative.split("/"):
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return "relationship-evidence-missing"
        except OSError:
            return "relationship-evidence-unreadable"
        if is_redirected_path(current):
            return "relationship-evidence-symlink"
    try:
        mode = current.lstat().st_mode
    except OSError:
        return "relationship-evidence-unreadable"
    if not stat.S_ISREG(mode):
        return "relationship-evidence-not-file"
    return None


def read_json_file(root: Path, relative: str) -> tuple[dict[str, Any] | None, str | None]:
    problem = unredirected_file_problem(root, relative)
    if problem:
        return None, problem
    try:
        payload = json.loads((root / relative).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None, "module-evidence-unreadable"
    except json.JSONDecodeError:
        return None, "module-evidence-invalid"
    if not isinstance(payload, dict):
        return None, "module-evidence-invalid"
    return payload, None


def load_index(root: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[str]]:
    problems: list[str] = []
    index, problem = read_json_file(root, INDEX_REL)
    if problem:
        return None, [], ["module-index-" + problem.removeprefix("module-evidence-")]
    assert index is not None
    raw_modules = index.get("modules")
    if not isinstance(raw_modules, list):
        return None, [], ["module-index-invalid"]
    modules: list[dict[str, Any]] = []
    names: set[str] = set()
    for item in raw_modules:
        if not isinstance(item, dict):
            problems.append("module-entry-invalid")
            continue
        name = item.get("module")
        if not isinstance(name, str) or not MODULE_NAME.fullmatch(name):
            problems.append("module-name-invalid")
            continue
        if name in names:
            problems.append("module-name-duplicate")
            continue
        names.add(name)
        modules.append(item)
    return index, modules, sorted(set(problems))


def module_evidence_problem(root: Path, module: dict[str, Any]) -> str | None:
    evidence_rel = normalize_relative(module.get("evidence"))
    if evidence_rel is None:
        return "module-evidence-reference-invalid"
    evidence, problem = read_json_file(root, evidence_rel)
    if problem:
        if problem == "relationship-evidence-symlink":
            return "module-evidence-symlink"
        if problem == "relationship-evidence-missing":
            return "module-evidence-missing"
        return problem
    assert evidence is not None
    if evidence.get("module") != module.get("module"):
        return "module-evidence-module-mismatch"
    return None


def evidence_item_problem(root: Path, item: object) -> str | None:
    if not isinstance(item, dict):
        return "relationship-evidence-invalid"
    kind = item.get("kind")
    if kind not in ALLOWED_EVIDENCE_KINDS:
        return "relationship-evidence-kind-invalid"
    locator = item.get("locator")
    if locator is not None:
        if not isinstance(locator, str) or not locator.strip():
            return "relationship-evidence-locator-invalid"
        if ABSOLUTE_PATH_TEXT.search(locator) or SECRET_PATTERN.search(locator):
            return "relationship-evidence-locator-invalid"
    if kind == "claim":
        if not isinstance(item.get("ref"), str) or not CLAIM_REFERENCE.fullmatch(item["ref"]):
            return "relationship-evidence-reference-invalid"
        return None
    relative = normalize_relative(item.get("path"))
    if relative is None:
        return "relationship-evidence-path-invalid"
    return unredirected_file_problem(root, relative)


def safe_evidence_item(item: object) -> object:
    if not isinstance(item, dict):
        return {"kind": "<invalid>"}
    result = dict(item)
    for field in ("path", "locator", "ref"):
        value = result.get(field)
        if not isinstance(value, str):
            continue
        if ABSOLUTE_PATH_TEXT.search(value) or SECRET_PATTERN.search(value):
            result[field] = "<redacted>"
        else:
            result[field] = sanitize_local_paths(value)
    return result


def normalize_verification_commands(
    raw: object,
    consumers: list[str],
) -> tuple[list[dict[str, str]], list[str]]:
    if not isinstance(raw, list):
        return [], ["consumer-verification-missing"]
    commands: list[dict[str, str]] = []
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            problems.append("consumer-verification-invalid")
            continue
        module = item.get("module")
        if not isinstance(module, str) or module not in consumers:
            problems.append("consumer-verification-module-invalid")
            continue
        try:
            command = safe_text(item.get("command"), "verification command")
        except ValueError:
            problems.append("consumer-verification-invalid")
            continue
        key = (module, command)
        if key in seen:
            problems.append("consumer-verification-duplicate")
            continue
        seen.add(key)
        commands.append({"module": module, "command": command})
    if not commands or {item["module"] for item in commands} != set(consumers):
        problems.append("consumer-verification-missing")
    return sorted(commands, key=lambda item: (item["module"], item["command"])), sorted(set(problems))


def validate_relationship(
    root: Path,
    raw: object,
    modules: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    problems: list[str] = []
    if not isinstance(raw, dict):
        return {"problems": ["relationship-invalid"]}, ["relationship-invalid"]
    relation_id = raw.get("id")
    if not isinstance(relation_id, str) or not RELATION_ID.fullmatch(relation_id):
        problems.append("relationship-id-invalid")
        relation_id = str(relation_id or "")[:128]
    producer = raw.get("producer")
    if not isinstance(producer, str) or producer not in modules:
        problems.append("producer-module-missing")
        producer = str(producer or "")
    raw_consumers = raw.get("consumers")
    consumers: list[str] = []
    if not isinstance(raw_consumers, list) or not raw_consumers:
        problems.append("consumer-module-missing")
    else:
        for consumer in raw_consumers:
            if not isinstance(consumer, str) or consumer not in modules:
                problems.append("consumer-module-missing")
                continue
            if consumer == producer:
                problems.append("relationship-self-reference")
                continue
            if consumer not in consumers:
                consumers.append(consumer)
        if len(consumers) != len(raw_consumers):
            problems.append("consumer-module-duplicate")
    consumers.sort()
    try:
        contract = safe_text(raw.get("contract"), "relationship contract")
    except ValueError:
        problems.append("relationship-contract-invalid")
        contract = ""
    confidence = raw.get("confidence")
    if confidence not in ALLOWED_CONFIDENCE:
        problems.append("relationship-confidence-invalid")
        confidence = "unknown"
    status = raw.get("status")
    if status not in ALLOWED_STATUS:
        problems.append("relationship-status-invalid")
        status = "unknown"

    evidence = raw.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        problems.append("relationship-evidence-missing")
        evidence = []
    else:
        for item in evidence:
            problem = evidence_item_problem(root, item)
            if problem:
                problems.append(problem)
    if status == "active" and confidence == "unknown":
        problems.append("active-relationship-confidence-unknown")
    verification_commands, verification_problems = normalize_verification_commands(
        raw.get("verification_commands"), consumers
    )
    if status != "active":
        verification_commands = []
        verification_problems = []
    problems.extend(verification_problems)
    for module_name in [producer, *consumers]:
        if module_name in modules:
            problem = module_evidence_problem(root, modules[module_name])
            if problem:
                problems.append(problem)
    normalized = {
        "id": relation_id,
        "producer": producer,
        "consumers": consumers,
        "contract": contract,
        "confidence": confidence,
        "status": status,
        "evidence": [safe_evidence_item(item) for item in evidence],
        "verification_commands": verification_commands,
        "evidence_status": "pass" if not problems else "fail",
        "verification_status": "pass" if not verification_problems else "fail",
        "problems": sorted(set(problems)),
    }
    return normalized, sorted(set(problems))


def validate_relationship_evidence(root: Path) -> dict[str, Any]:
    index, raw_modules, index_problems = load_index(root)
    if index is None:
        return {
            "version": 1,
            "status": "fail",
            "code": "cross-module-evidence-invalid",
            "coverage_status": "unknown",
            "modules": [],
            "relationships": [],
            "verification_matrix": [],
            "unknown_modules": [],
            "codes": sorted(set(index_problems)),
            "problems": sorted(set(index_problems)),
        }
    modules = {str(item["module"]): item for item in raw_modules if isinstance(item.get("module"), str)}
    raw_relationships = index.get("relationships", [])
    if raw_relationships is None:
        raw_relationships = []
    global_problems = list(index_problems)
    if not isinstance(raw_relationships, list):
        raw_relationships = []
        global_problems.append("relationships-invalid")
    relations: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in raw_relationships:
        relation, problems = validate_relationship(root, raw, modules)
        relation_id = str(relation.get("id") or "")
        if relation_id and relation_id in seen_ids:
            problems = sorted(set(problems) | {"relationship-id-duplicate"})
            relation["problems"] = problems
            relation["evidence_status"] = "fail"
            relation["verification_status"] = "fail"
        if relation_id:
            seen_ids.add(relation_id)
        global_problems.extend(problems)
        relations.append(relation)
    active_contracts: dict[tuple[str, str], set[str]] = {}
    for relation in relations:
        if relation.get("status") != "active" or relation.get("problems"):
            continue
        producer = str(relation.get("producer", ""))
        contract = str(relation.get("contract", ""))
        for consumer in relation.get("consumers", []):
            active_contracts.setdefault((producer, str(consumer)), set()).add(contract)
    conflict_pairs = {pair for pair, contracts in active_contracts.items() if len(contracts) > 1}
    if conflict_pairs:
        for relation in relations:
            if relation.get("status") != "active" or relation.get("problems"):
                continue
            producer = str(relation.get("producer", ""))
            if any((producer, str(consumer)) in conflict_pairs for consumer in relation.get("consumers", [])):
                relation["problems"] = sorted(set(relation.get("problems", [])) | {"relationship-conflict"})
                relation["evidence_status"] = "fail"
                relation["verification_status"] = "fail"
                global_problems.append("relationship-conflict")
    relations.sort(key=lambda item: str(item.get("id", "")))
    active_modules: set[str] = set()
    matrix: list[dict[str, Any]] = []
    for relation in relations:
        if relation.get("status") != "active" or relation.get("problems"):
            continue
        producer = str(relation.get("producer", ""))
        active_modules.add(producer)
        commands_by_module: dict[str, list[str]] = {}
        for item in relation.get("verification_commands", []):
            if isinstance(item, dict):
                module = str(item.get("module", ""))
                commands_by_module.setdefault(module, []).append(str(item.get("command", "")))
        for consumer in relation.get("consumers", []):
            active_modules.add(str(consumer))
            matrix.append(
                {
                    "relationship_id": relation["id"],
                    "producer": producer,
                    "module": consumer,
                    "commands": sorted(commands_by_module.get(str(consumer), [])),
                }
            )
    matrix.sort(key=lambda item: (str(item["module"]), str(item["relationship_id"])))
    unknown_modules = sorted(set(modules) - active_modules)
    if unknown_modules:
        coverage_status = "unknown"
    elif modules and relations:
        coverage_status = "complete"
    else:
        coverage_status = "unknown"
    status = "fail" if global_problems else "pass"
    return {
        "version": 1,
        "status": status,
        "code": "cross-module-evidence-valid" if status == "pass" else "cross-module-evidence-invalid",
        "coverage_status": coverage_status,
        "modules": sorted(modules),
        "relationships": relations,
        "verification_matrix": matrix,
        "unknown_modules": unknown_modules,
        "codes": sorted(set(global_problems)),
        "problems": sorted(set(global_problems)),
    }


def analyze_impact(root: Path, producers: list[str], changed_files: list[str]) -> dict[str, Any]:
    evidence = validate_relationship_evidence(root)
    if evidence["status"] != "pass":
        return {
            **evidence,
            "status": "fail",
            "code": "cross-impact-invalid",
            "producers": sorted(set(producers)),
            "changed_files": changed_files,
            "affected_consumers": [],
            "unknown_producers": [],
        }
    codes: list[str] = []
    normalized_files: list[str] = []
    for changed_file in changed_files:
        relative = normalize_relative(changed_file)
        if relative is None:
            codes.append("changed-file-path-invalid")
        elif relative not in normalized_files:
            normalized_files.append(relative)
    module_names = set(str(item) for item in evidence.get("modules", []))
    selected = sorted(set(producers))
    for producer in selected:
        if producer not in module_names:
            codes.append("producer-module-missing")
    relations = [
        item
        for item in evidence.get("relationships", [])
        if isinstance(item, dict) and item.get("status") == "active" and not item.get("problems")
    ]
    affected: set[str] = set()
    matrix: list[dict[str, Any]] = []
    unknown_producers: list[str] = []
    for producer in selected:
        producer_relations = [item for item in relations if item.get("producer") == producer]
        if not producer_relations:
            unknown_producers.append(producer)
            continue
        for relation in producer_relations:
            for item in relation.get("verification_commands", []):
                if not isinstance(item, dict):
                    continue
                module = str(item.get("module", ""))
                affected.add(module)
            relation_matrix = [
                item
                for item in evidence.get("verification_matrix", [])
                if isinstance(item, dict) and item.get("relationship_id") == relation.get("id")
            ]
            matrix.extend(relation_matrix)
    if codes:
        status = "fail"
        code = "cross-impact-invalid"
    elif unknown_producers:
        status = "unknown"
        code = "cross-impact-unknown"
    else:
        status = "pass"
        code = "cross-impact-complete"
    return {
        "version": 1,
        "status": status,
        "code": code,
        "producers": selected,
        "changed_files": normalized_files,
        "affected_consumers": sorted(affected),
        "unknown_producers": sorted(unknown_producers),
        "verification_matrix": sorted(
            matrix,
            key=lambda item: (str(item.get("module", "")), str(item.get("relationship_id", ""))),
        ),
        "codes": sorted(set(codes)),
        "problems": sorted(set(codes)),
    }


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.analyze:
        if not args.producer:
            payload = {
                "version": 1,
                "status": "fail",
                "code": "cross-impact-invalid",
                "producers": [],
                "changed_files": [],
                "affected_consumers": [],
                "unknown_producers": [],
                "verification_matrix": [],
                "codes": ["producer-required"],
                "problems": ["producer-required"],
            }
        elif not args.changed_file:
            payload = {
                "version": 1,
                "status": "fail",
                "code": "cross-impact-invalid",
                "producers": sorted(set(args.producer)),
                "changed_files": [],
                "affected_consumers": [],
                "unknown_producers": [],
                "verification_matrix": [],
                "codes": ["changed-file-required"],
                "problems": ["changed-file-required"],
            }
        else:
            payload = analyze_impact(args.path, args.producer, args.changed_file)
    else:
        payload = validate_relationship_evidence(args.path)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"tenetora cross-impact: {payload['status']}")
        print(f"- coverage: {payload['coverage_status']}")
        if payload["codes"]:
            print(f"- problems: {', '.join(payload['codes'])}")
    if payload["status"] == "pass":
        return 0
    if payload["status"] == "unknown":
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
