"""Owned entrypoint for CLI subprocesses started from untrusted directories.

Run this file with Python's -I flag. The entrypoint adds only its own CLI
package root; it never relies on cwd, PYTHONPATH, or a project-owned package.
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path


def main() -> int:
    if not sys.flags.isolated:
        print('Tenetora internal CLI entrypoint requires Python isolated mode (-I).', file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parent.parent
    if not (root / 'tenetora' / 'cli.py').is_file():
        print('Tenetora trusted CLI package is unavailable.', file=sys.stderr)
        return 2
    sys.path.insert(0, str(root))
    runpy.run_module('tenetora.cli', run_name='__main__', alter_sys=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
