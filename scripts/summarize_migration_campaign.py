#!/usr/bin/env python3
"""Create a row-free aggregate report for prepared migration lots."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from queryflow.migration_batch import BatchError, build_batch_digest, validate_batch_manifest


def summarize(root: Path, *, expected_count: int | None = None) -> dict[str, Any]:
    manifests = sorted(
        (
            path
            for path in root.glob("*/manifest.json")
            if path.parent != root and path.parent.is_dir()
        ),
        key=lambda path: path.parent.name,
    )
    if not manifests:
        raise BatchError(f"No hay manifests de lote en {root}")
    resources: list[dict[str, Any]] = []
    lots: list[dict[str, Any]] = []
    for path in manifests:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        validate_batch_manifest(
            manifest,
            allow_blocked=True,
        )
        if manifest.get("publication_digest") != build_batch_digest(manifest):
            raise BatchError(f"Digest inconsistente: {path}")
        records = [item for item in (manifest.get("resources") or []) if isinstance(item, dict)]
        resources.extend(records)
        lots.append(
            {
                "lot": path.parent.name,
                "campaign_id": manifest.get("campaign_id"),
                "manifest": str(path),
                "report_markdown": str(path.with_name("migration-report.md")),
                "review": str(path.with_name("review.html")),
                "status": manifest.get("status"),
                "resource_count": len(records),
                "published_count": sum(1 for item in records if item.get("status") == "published"),
                "pending_count": sum(1 for item in records if item.get("status") == "pending"),
                "security_pending_count": sum(1 for item in records if item.get("status") == "security_pending" or bool((item.get("security") or {}).get("sealed"))),
                "publication_digest": manifest.get("publication_digest"),
                "sealed_publication_digest": manifest.get("sealed_publication_digest", ""),
                "warning_count": len(manifest.get("warnings") or []),
            }
        )
    if expected_count is not None and len(resources) != expected_count:
        raise BatchError(f"Se esperaban {expected_count} recursos preparados; se encontraron {len(resources)}")
    keys = [(str((item.get("resource") or {}).get("kind")), str((item.get("resource") or {}).get("name"))) for item in resources]
    if len(keys) != len(set(keys)):
        raise BatchError("La campaña contiene recursos duplicados")
    classes = Counter(str((item.get("classification") or {}).get("statement_class") or "not_evaluated") for item in resources)
    reasons = Counter(str(reason) for item in resources for reason in (item.get("review") or {}).get("reasons") or [])
    warnings = Counter(str(item.get("kind") or "unknown") for record in resources for item in record.get("warnings") or [])
    route_replacements = sum(len((item.get("rewrite") or {}).get("applied_routes") or []) for item in resources)
    changed_files = sum(len((item.get("rewrite") or {}).get("changed_files") or []) for item in resources)
    return {
        "schema_version": 1,
        "campaign_root": str(root),
        "resource_count": len(resources),
        "lot_count": len(lots),
        "published_count": sum(1 for item in resources if item.get("status") == "published"),
        "pending_count": sum(1 for item in resources if item.get("status") == "pending"),
        "security_pending_count": sum(1 for item in resources if item.get("status") == "security_pending" or bool((item.get("security") or {}).get("sealed"))),
        "sealed_publication_digest_count": sum(1 for item in lots if item.get("sealed_publication_digest")),
        "pending_resources": [
            str((item.get("resource") or {}).get("display_name") or "")
            for item in resources
            if item.get("status") == "pending"
        ],
        "kind_counts": dict(Counter(str((item.get("resource") or {}).get("kind") or "unknown") for item in resources)),
        "classification_counts": dict(classes),
        "review_reason_counts": dict(reasons),
        "warning_counts": dict(warnings),
        "review_required_count": sum(1 for item in resources if (item.get("review") or {}).get("required")),
        "execution_eligible_count": sum(1 for item in resources if item.get("execution_eligible") is True),
        "route_replacement_group_count": route_replacements,
        "changed_file_count": changed_files,
        "sql_executed": any((item.get("rewrite") or {}).get("sql_executed") for item in resources),
        "lots": lots,
    }


def render(report: dict[str, Any]) -> str:
    lines = [
        f"# Informe agregado de migración: `{Path(report['campaign_root']).name}`",
        "",
        "Informe de solo código. No se ejecutó SQL ni dry-run.",
        "",
        f"- Recursos: **{report['resource_count']}** en **{report['lot_count']}** lotes · publicados: **{report['published_count']}** · pendientes: **{report['pending_count']}** · sellados pendientes: **{report['security_pending_count']}**",
        f"- Revisión humana requerida: **{report['review_required_count']}**",
        f"- Ejecución habilitada por QueryFlow: **{report['execution_eligible_count']}**",
        f"- Reemplazos de rutas: **{report['route_replacement_group_count']}** grupos · archivos/celdas modificados: **{report['changed_file_count']}**",
        f"- Clases: `{json.dumps(report['classification_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- Razones de revisión: `{json.dumps(report['review_reason_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- Avisos: `{json.dumps(report['warning_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- Recursos pendientes: `{json.dumps(report['pending_resources'], ensure_ascii=False)}`",
        "",
        "| Lote | Recursos | Publicados | Pendientes | Avisos | Digest | Preview |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for lot in report["lots"]:
        lines.append(f"| `{lot['campaign_id']}` | {lot['resource_count']} | {lot['published_count']} | {lot['pending_count']} | {lot['warning_count']} | `{lot['publication_digest']}` | `{lot['review']}` |")
    lines.extend(
        [
            "",
            "Las advertencias (ruta desconocida, SQL dinámico, mutante o no clasificable) quedan como código pendiente de revisión humana. Secretos, contenido vacío, conflictos, drift y fallos de integridad permanecen separados o bloqueados.",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--expected-count", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = summarize(args.campaign_root.expanduser(), expected_count=args.expected_count)
    root = args.campaign_root.expanduser()
    (root / "campaign-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (root / "campaign-report.md").write_text(render(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) if args.json else f"Informe escrito en {root / 'campaign-report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
