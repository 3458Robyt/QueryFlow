#!/usr/bin/env python3
"""Small repository-local plugin manifest validator for CI."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate_plugin.py PLUGIN_PATH", file=sys.stderr)
        return 2
    root = Path(sys.argv[1]).resolve()
    manifest_path = root / ".codex-plugin" / "plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"plugin manifest invalid: {error}", file=sys.stderr)
        return 1
    required = {"name", "version", "description", "skills"}
    missing = sorted(required - set(manifest))
    if missing or manifest.get("name") != root.name:
        print(f"plugin manifest invalid: missing={missing} name={manifest.get('name')!r}", file=sys.stderr)
        return 1
    skills = root / str(manifest["skills"]).removeprefix("./")
    if not skills.is_dir() or not any(skills.rglob("SKILL.md")):
        print("plugin manifest invalid: skills directory is empty", file=sys.stderr)
        return 1
    print(f"plugin valid: {manifest['name']} {manifest['version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
