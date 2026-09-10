#!/usr/bin/env python3
"""Prepare deterministic review-required migration lots from a prior inventory.

This helper only reads private campaign artifacts and writes selection files;
it never calls GCP, executes SQL, or publishes a resource.  The generated
selections are then consumed by ``queryflow migration batch inventory``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Allow the helper to run directly from a checkout before the package is
# installed in the current shell.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from queryflow.catalog import ResourceRef
from queryflow.migration_batch import BatchError, partition_batch_resources


WARNING_CAMPAIGN_DIRS = (
    "analytics-remaining-us-east1-20260903-q01b",
    "analytics-remaining-us-east1-20260903-q02",
    "analytics-remaining-us-east1-20260903-q03b",
    "analytics-remaining-us-east1-20260903-q04",
    "analytics-remaining-us-east1-20260903-q05",
    "analytics-remaining-us-east1-20260903-q06",
    "analytics-remaining-us-east1-20260903-q07",
    "analytics-remaining-us-east1-20260903-nb01",
)
STATIC_CAMPAIGN_DIRS = (
    "analytics-remaining-us-east1-20260903-q01c",
    "analytics-remaining-us-east1-20260903-q02c",
    "analytics-remaining-us-east1-20260903-q03c",
    "analytics-remaining-us-east1-20260903-q04c",
    "analytics-remaining-us-east1-20260903-q05c",
    "analytics-remaining-us-east1-20260903-q06c",
    "analytics-remaining-us-east1-20260903-q07c",
    "analytics-remaining-us-east1-20260903-nb01c",
)
REVIEW_KINDS = frozenset({"unknown_route", "dynamic_sql"})
STATIC_CLASSES = frozenset({"mutating", "unknown"})


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BatchError(f"El JSON no contiene un objeto: {path}")
    return value


def _record_resource(record: dict[str, Any]) -> ResourceRef:
    resource = record.get("resource")
    if not isinstance(resource, dict):
        raise BatchError("Registro sin resource")
    return ResourceRef.from_dict(resource)


def _add_candidate(
    candidates: dict[tuple[str, str], ResourceRef],
    reasons: dict[tuple[str, str], set[str]],
    resource: ResourceRef,
    incident_kinds: Iterable[str],
) -> None:
    key = (resource.kind, resource.name)
    if key in candidates and candidates[key] != resource:
        raise BatchError(f"El recurso aparece con metadatos distintos: {resource.name}")
    candidates[key] = resource
    reasons.setdefault(key, set()).update(str(item) for item in incident_kinds)


def collect_candidates(inventory_root: Path) -> tuple[list[ResourceRef], dict[str, Any]]:
    """Collect accepted warnings plus mutating/unknown static resources."""
    candidates: dict[tuple[str, str], ResourceRef] = {}
    reasons: dict[tuple[str, str], set[str]] = {}
    excluded: list[dict[str, Any]] = []
    for dirname in WARNING_CAMPAIGN_DIRS:
        manifest = _load(inventory_root / dirname / "manifest.json")
        for record in manifest.get("resources") or []:
            if not isinstance(record, dict):
                continue
            resource = _record_resource(record)
            kinds = {str(item.get("kind") or "") for item in (record.get("warnings") or []) if isinstance(item, dict)}
            if not kinds:
                continue
            if "embedded_secret" in kinds:
                excluded.append({"resource": resource.to_dict(), "reasons": sorted(kinds), "source": dirname})
                continue
            accepted = sorted(kinds & REVIEW_KINDS)
            if accepted:
                _add_candidate(candidates, reasons, resource, accepted)
    for dirname in STATIC_CAMPAIGN_DIRS:
        manifest = _load(inventory_root / dirname / "manifest.json")
        for record in manifest.get("resources") or []:
            if not isinstance(record, dict):
                continue
            resource = _record_resource(record)
            task = Path(str(record.get("task") or ""))
            validation_path = task / "validation.json"
            if not validation_path.is_file():
                raise BatchError(f"Falta validation.json para {resource.display_name}")
            validation = _load(validation_path)
            static = validation.get("static") or {}
            statement_class = str(static.get("statement_class") or "")
            if statement_class in STATIC_CLASSES:
                _add_candidate(candidates, reasons, resource, [statement_class])
    ordered = sorted(
        candidates.values(),
        key=lambda item: ({"shared_query": 0, "notebook": 1}.get(item.kind, 99), item.display_name.casefold(), item.name),
    )
    summary = {
        "candidate_count": len(ordered),
        "kind_counts": dict(Counter(item.kind for item in ordered)),
        "reason_counts": dict(Counter(reason for values in reasons.values() for reason in values)),
        "excluded_security": excluded,
        "source_campaigns": {"warnings": list(WARNING_CAMPAIGN_DIRS), "static": list(STATIC_CAMPAIGN_DIRS)},
        "resource_reasons": [
            {"kind": item.kind, "name": item.name, "display_name": item.display_name, "reasons": sorted(reasons[(item.kind, item.name)])}
            for item in ordered
        ],
    }
    return ordered, summary


def prepare_lots(
    resources: list[ResourceRef],
    *,
    output_root: Path,
    campaign_prefix: str,
    batch_size: int,
    expected_count: int | None,
    source_project: str,
    destination_project: str,
    location: str,
) -> dict[str, Any]:
    if expected_count is not None and len(resources) != expected_count:
        raise BatchError(f"Se esperaban {expected_count} candidatos; se encontraron {len(resources)}")
    batches = partition_batch_resources(resources, batch_size=batch_size)
    output_root.mkdir(parents=True, exist_ok=True)
    lots: list[dict[str, Any]] = []
    for index, batch in enumerate(batches, 1):
        campaign_id = f"{campaign_prefix}-r{index:02d}"
        selection = {
            "schema_version": 1,
            "campaign_id": campaign_id,
            "source_project": source_project,
            "destination_project": destination_project,
            "location": location,
            "naming": "preserve_display_name",
            "publish_incidents": True,
            "execute_sql": False,
            "resources": [
                {"kind": resource.kind, "display_name": resource.display_name}
                for resource in batch
            ],
        }
        path = output_root / f"selection-r{index:02d}.json"
        path.write_text(json.dumps(selection, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        lots.append({"campaign_id": campaign_id, "selection": str(path), "count": len(batch), "kind_counts": dict(Counter(item.kind for item in batch))})
    plan = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "campaign_prefix": campaign_prefix,
        "source_project": source_project,
        "destination_project": destination_project,
        "location": location,
        "batch_size": batch_size,
        "resource_count": len(resources),
        "lot_count": len(lots),
        "lots": lots,
    }
    (output_root / "campaign-plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return plan


def _markdown(plan: dict[str, Any], summary: dict[str, Any]) -> str:
    lines = [
        f"# Plan de lotes QueryFlow: `{plan['campaign_prefix']}`",
        "",
        "Selecciones generadas localmente a partir de una campaña ya inventariada. No se ejecutó SQL, dry-run ni publicación.",
        "",
        f"- Recursos elegibles: **{plan['resource_count']}**",
        f"- Lotes: **{plan['lot_count']}** de máximo **{plan['batch_size']}**",
        f"- Clases: `{json.dumps(summary.get('reason_counts', {}), ensure_ascii=False, sort_keys=True)}`",
        f"- Excluidos por secreto: **{len(summary.get('excluded_security') or [])}**",
        "",
        "| Lote | Recursos | Shared Queries | Notebooks | Selección |",
        "|---|---:|---:|---:|---|",
    ]
    for lot in plan["lots"]:
        counts = lot.get("kind_counts") or {}
        lines.append(f"| `{lot['campaign_id']}` | {lot['count']} | {counts.get('shared_query', 0)} | {counts.get('notebook', 0)} | `{lot['selection']}` |")
    lines.extend(["", "Los manifiestos, previews y digest se generan después con `queryflow migration batch inventory` y `prepare` para cada selección.", ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--campaign-prefix", default="analytics-review-required-us-east1-20260904")
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--expected-count", type=int, default=None)
    parser.add_argument("--source-project", default="sbscol-dbreplication-prd")
    parser.add_argument("--destination-project", default="analytics-487218")
    parser.add_argument("--location", default="us-east1")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    resources, summary = collect_candidates(args.inventory_root.expanduser())
    plan = prepare_lots(
        resources,
        output_root=args.output_root.expanduser(),
        campaign_prefix=args.campaign_prefix,
        batch_size=args.batch_size,
        expected_count=args.expected_count,
        source_project=args.source_project,
        destination_project=args.destination_project,
        location=args.location,
    )
    output_root = args.output_root.expanduser()
    (output_root / "candidate-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_root / "campaign-plan.md").write_text(_markdown(plan, summary), encoding="utf-8")
    payload = {"ok": True, "plan": plan, "summary": {key: value for key, value in summary.items() if key != "resource_reasons"}}
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) if args.json else f"{plan['resource_count']} recursos en {plan['lot_count']} lotes: {output_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
