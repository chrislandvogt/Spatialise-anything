#!/usr/bin/env python3
"""Build upload zips into dist/.

    python tools/package.py

dist/spatialise-plugin-<version>.zip   the plugin: manifest, command, skill
dist/spatialise-anything-skill-<version>.zip   the skill folder on its own

Only the files a user needs ship: no tests, corpus, eval or tools, no
caches. The build fails if anything that looks like a secret or a
top-level bin/ directory would be included.
"""

from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_PATHS = [".claude-plugin/plugin.json", "commands", "skills", "LICENSE"]
SKIP = re.compile(r"(__pycache__|\.pyc$|\.DS_Store$|\.pytest_cache)")
SECRETISH = re.compile(r"(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]{8,}", re.I)


def files(paths):
    for p in paths:
        full = ROOT / p
        if full.is_file():
            yield full
        else:
            for f in sorted(full.rglob("*")):
                if f.is_file() and not SKIP.search(str(f)):
                    yield f


def check(f: Path):
    if f.suffix in (".py", ".md", ".json", ".txt"):
        if SECRETISH.search(f.read_text(encoding="utf-8", errors="ignore")):
            raise SystemExit(f"refusing to package {f}: looks like it holds a secret")


def build(name, paths, base):
    out = ROOT / "dist" / name
    out.parent.mkdir(exist_ok=True)
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in files(paths):
            check(f)
            z.write(f, f.relative_to(base))
            n += 1
    return out, n


def main():
    if (ROOT / "bin").exists():
        raise SystemExit("a top-level bin/ directory stops claude.ai and Cowork installing the plugin")
    version = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]
    results = []
    out, n = build(f"spatialise-plugin-{version}.zip", PLUGIN_PATHS, ROOT)
    results.append({"zip": str(out.relative_to(ROOT)), "files": n, "bytes": out.stat().st_size})
    skill = ROOT / "skills" / "spatialise-anything"
    out, n = build(f"spatialise-anything-skill-{version}.zip", [str(skill.relative_to(ROOT))], skill.parent)
    results.append({"zip": str(out.relative_to(ROOT)), "files": n, "bytes": out.stat().st_size})
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    sys.exit(main())
