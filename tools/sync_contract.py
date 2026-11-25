#!/usr/bin/env python3
"""Copy the contract modules from the skill into the MCP server package.

The skill owns the output contract. The server validates features against the
same code before it publishes them, from a vendored copy so the server can be
built and deployed on its own. Run this after changing the skill's contract;
server/tests/test_contract_sync.py fails if the copies drift.

    python tools/sync_contract.py [--check]
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "skills" / "spatialise-anything" / "scripts"
DST = ROOT / "server" / "spatialise_mcp" / "_contract"
FILES = ("contract.py", "validate_features.py")


def main() -> int:
    check = "--check" in sys.argv
    stale = [f for f in FILES if not (DST / f).exists() or (DST / f).read_bytes() != (SRC / f).read_bytes()]
    if check:
        if stale:
            print(f"out of date: {stale} - run python tools/sync_contract.py")
            return 1
        print("in sync")
        return 0
    DST.mkdir(parents=True, exist_ok=True)
    for f in FILES:
        shutil.copyfile(SRC / f, DST / f)
    print(f"copied {list(FILES)} ({len(stale)} changed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
