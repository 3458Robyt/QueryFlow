#!/usr/bin/env python3
"""Generate a complete resource-level migration report from batch manifests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from queryflow.migration_report import (
    build_embedded_diff_index,
    build_detailed_report,
    _portable_report,
    render_detailed_markdown,
    write_detailed_html,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--expected-count", type=int, default=None)
    parser.add_argument("--campaign-prefix", help="prefijo común para lotes multirregión")
    parser.add_argument("--output-base", type=Path)
    parser.add_argument("--json", action="store_true", help="imprimir un resumen JSON del resultado")
    args = parser.parse_args(argv)
    report = build_detailed_report(
        args.campaign_root.expanduser(),
        expected_count=args.expected_count,
        campaign_prefix=args.campaign_prefix,
    )
    root = args.campaign_root.expanduser()
    base = args.output_base.expanduser() if args.output_base else root / "campaign-detail-report"
    base.parent.mkdir(parents=True, exist_ok=True)
    json_path = base.with_suffix(".json")
    markdown_path = base.with_suffix(".md")
    html_path = base.with_suffix(".html")
    portable = _portable_report(report)
    json_path.write_text(json.dumps(portable, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_detailed_markdown(report) + "\n", encoding="utf-8")
    diff_index = build_embedded_diff_index(report, root)
    write_detailed_html(root, report, output_path=html_path, diff_index=diff_index)
    if args.json:
        print(json.dumps({"ok": True, "json": str(json_path), "markdown": str(markdown_path), "html": str(html_path), "summary": {
            "operation": report.get("operation", "copy"),
            "locations": report.get("locations", [report.get("location", "")]),
            "resource_count": report["resource_count"],
            "completed_count": report.get("completed_count", report.get("published_count", 0)),
            "published_count": report["published_count"],
            "pending_count": report["pending_count"],
            "route_replacement_group_count": report["route_replacement_group_count"],
            "route_replacement_occurrence_count": report["route_replacement_occurrence_count"],
            "unmatched_route_count": report["unmatched_route_count"],
            "embedded_diff_resource_count": diff_index["resource_count"],
            "embedded_diff_changed_file_count": diff_index["changed_file_count"],
            "embedded_diff_row_count": diff_index["diff_row_count"],
            "html_bytes": html_path.stat().st_size,
        }}, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"Reporte JSON escrito en {json_path}")
        print(f"Reporte Markdown escrito en {markdown_path}")
        print(f"Reporte HTML escrito en {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
