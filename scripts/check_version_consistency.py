#!/usr/bin/env python3
"""Verify that package, CLI metadata and plugin report one version."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from queryflow import __version__  # noqa: E402


def main() -> int:
    plugin = json.loads(
        (ROOT / "plugins/queryflow/.codex-plugin/plugin.json").read_text(
            encoding="utf-8"
        )
    )
    cli = (ROOT / "queryflow/cli.py").read_text(encoding="utf-8")
    if "from . import __version__ as PACKAGE_VERSION" not in cli:
        print(
            "CLI no usa queryflow.__version__ como fuente de versión", file=sys.stderr
        )
        return 1
    if plugin.get("version") != __version__:
        print(f"plugin={plugin.get('version')} package={__version__}", file=sys.stderr)
        return 1
    ref = os.environ.get("GITHUB_REF_NAME", "")
    if ref.startswith("v"):
        tag_version = ref[1:]
        tag_version = re.sub(r"-beta\.(\d+)$", r"b\1", tag_version)
        if tag_version != __version__:
            print(f"tag={ref} package={__version__}", file=sys.stderr)
            return 1
    print(f"version consistency: {__version__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
