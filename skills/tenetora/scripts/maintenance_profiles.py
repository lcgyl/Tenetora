"""Remove only byte-exact Tenetora PATH blocks; keep other profile content."""

from __future__ import annotations

import os
from pathlib import Path

from path_manager import _assert_profile_safe, managed_block, profile_path


def plan(home: Path, fingerprint) -> dict:
    actions = []
    preserved = []
    if os.name == "nt":
        return {
            "actions": [],
            "preserved": [
                {
                    "path": "HKCU Environment Path",
                    "reason": "historical PATH segment ownership is not proven; preserved",
                }
            ],
        }
    for kind in ("bash", "zsh", "fish"):
        path = profile_path(Path.home(), kind)
        if not path.exists():
            continue
        try:
            _assert_profile_safe(path)
            if path.stat().st_size > 1024 * 1024:
                raise ValueError("profile exceeds inspection budget")
            data = path.read_text(encoding="utf-8")
            candidates = {
                managed_block(home / "bin", kind),
                managed_block(home.resolve() / "bin", kind),
            }
            matching = [block for block in candidates if data.count(block) == 1]
            if len(matching) == 1:
                actions.append(
                    {
                        "path": str(path),
                        "before": fingerprint(path),
                        "block": matching[0],
                    }
                )
            elif "tenetora managed PATH" in data:
                preserved.append(
                    {
                        "path": str(path),
                        "reason": "PATH block is modified, duplicated or belongs to another home; preserved",
                    }
                )
        except (OSError, RuntimeError, ValueError):
            preserved.append(
                {
                    "path": str(path),
                    "reason": "profile ownership or safe path is unavailable; preserved",
                }
            )
    return {"actions": actions, "preserved": preserved}
