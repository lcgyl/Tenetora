#!/usr/bin/env python3
"""Safely migrate the legacy Agent Harness machine home to Tenetora."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
CLI_ROOT = SCRIPT_DIRECTORY.parent / "cli"
RUNTIME_SCRIPTS = SCRIPT_DIRECTORY
if str(CLI_ROOT) not in sys.path:
    sys.path.insert(0, str(CLI_ROOT))
if str(RUNTIME_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SCRIPTS))

from tenetora.brand import (  # noqa: E402
    default_legacy_machine_home,
    machine_home,
    migrate_legacy_machine_home,
)
from runtime_transaction import run_runtime_update  # noqa: E402
from tenetora.install_lock import install_transaction_lock  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--help", action="help")
    parser.add_argument("--canonical", type=Path, default=None)
    parser.add_argument("--legacy", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--runtime-rollback-journal", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--bin-dir", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    def migrate() -> dict[str, object]:
        return migrate_legacy_machine_home(
            canonical=args.canonical or machine_home(),
            legacy=args.legacy or default_legacy_machine_home(),
            dry_run=args.dry_run,
        )

    with install_transaction_lock(args.canonical or machine_home()):
        if args.runtime_rollback_journal is not None:
            if args.bin_dir is None:
                parser.error("--runtime-rollback-journal requires --bin-dir")
            result = run_runtime_update(args.bin_dir, args.runtime_rollback_journal, migrate)
        else:
            result = migrate()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Tenetora machine home: {result['status']}")
    return 1 if result["status"] == "blocked-canonical-conflict" else 0


if __name__ == "__main__":
    raise SystemExit(main())
