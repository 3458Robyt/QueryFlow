#!/usr/bin/env python3
"""Build deterministic QueryFlow release artifacts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from queryflow.release_build import build_release  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    sdist, wheel, checksum = build_release(ROOT, args.out_dir)
    print(f"built: {sdist}")
    print(f"built: {wheel}")
    print(f"checksums: {checksum}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
