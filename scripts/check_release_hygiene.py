#!/usr/bin/env python3
"""Fail CI when a tracked public-release file contains operational data."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from queryflow.release_hygiene import EXCLUDED_PARTS, INTERNAL_PATTERNS, HygieneFinding


def main() -> int:
    root = ROOT
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        print(completed.stderr.decode("utf-8", errors="replace"), file=sys.stderr)
        return 2
    findings: list[HygieneFinding] = []
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(raw.decode("utf-8"))
        if any(part in EXCLUDED_PARTS for part in relative.parts):
            findings.append(HygieneFinding("excluded_path", str(relative), "Ruta operativa no publicable"))
            continue
        path = root / relative
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for pattern in INTERNAL_PATTERNS:
            if pattern.search(text):
                rule = "secret" if "PRIVATE KEY" in pattern.pattern or "Bearer" in pattern.pattern or "ghp" in pattern.pattern else "internal_identifier"
                findings.append(HygieneFinding(rule, str(relative), "Contenido sensible o identificador interno detectado"))
                break
    if findings:
        for finding in findings:
            print(f"{finding.rule}: {finding.path}: {finding.message}", file=sys.stderr)
        return 1
    print("release hygiene: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
