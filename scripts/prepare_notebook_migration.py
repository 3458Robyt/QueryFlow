#!/usr/bin/env python3
"""Create safe, region-aware notebook migration selections from a catalog.

The helper is intentionally content-free: it does not export notebooks,
execute SQL, call Dataform or publish anything.  It only chooses canonical
resource names, excludes unnamed/ambiguous entries, and writes bounded
selection files consumed by ``queryflow migration batch inventory``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from queryflow.catalog import Catalog, ResourceRef, load_catalog
from queryflow.migration_batch import BatchError, _commit_time, normalize_display_name


DEFAULT_SOURCE_LOCATIONS = ("us-east1", "us-central1", "us-west1", "northamerica-northeast1")


def _slug(value: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in value.casefold()).strip("-") or "region"


def _safe_latest(candidates: Sequence[ResourceRef]) -> tuple[ResourceRef | None, str]:
    dated = [(resource, _commit_time(resource)) for resource in candidates]
    if any(moment is None for _, moment in dated):
        return None, "missing_commit_time"
    latest_moment = max(moment for _, moment in dated if moment is not None)
    latest = [resource for resource, moment in dated if moment == latest_moment]
    if len(latest) != 1:
        return None, "tied_commit_time"
    return latest[0], ""


def _resource_sort(resource: ResourceRef) -> tuple[int, str, str]:
    return (0, normalize_display_name(resource.display_name), resource.name)


def _write_selection(
    *,
    path: Path,
    campaign_id: str,
    source_project: str,
    destination_project: str,
    source_location: str,
    destination_location: str,
    resources: Sequence[ResourceRef],
    secret_handling: str,
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "source_project": source_project,
        "destination_project": destination_project,
        "source_location": source_location,
        "destination_location": destination_location,
        "location": destination_location,
        "naming": "preserve_display_name",
        "publish_incidents": True,
        "execute_sql": False,
        "secret_handling": secret_handling,
        "resources": [
            {"kind": resource.kind, "name": resource.name, "display_name": resource.display_name}
            for resource in resources
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "campaign_id": campaign_id,
        "selection": str(path),
        "source_location": source_location,
        "destination_location": destination_location,
        "secret_handling": secret_handling,
        "count": len(resources),
        "kind_counts": dict(Counter(resource.kind for resource in resources)),
    }


def build_notebook_campaign(
    catalog: Catalog,
    *,
    output_root: Path,
    source_project: str,
    destination_project: str,
    source_locations: Iterable[str] = DEFAULT_SOURCE_LOCATIONS,
    destination_location: str = "us-east1",
    campaign_prefix: str = "analytics-notebook-migration-20260907",
    sealed_display_names: Iterable[str] = (),
    only_display_names: Iterable[str] | None = None,
    normal_batch_size: int = 25,
    sealed_batch_size: int = 5,
) -> dict[str, Any]:
    """Build selections and return a content-free campaign summary."""
    if normal_batch_size <= 0 or normal_batch_size > 25:
        raise BatchError("normal_batch_size debe estar entre 1 y 25")
    if sealed_batch_size <= 0 or sealed_batch_size > 5:
        raise BatchError("sealed_batch_size debe estar entre 1 y 5")
    locations = tuple(dict.fromkeys(str(location).strip() for location in source_locations if str(location).strip()))
    if not locations:
        raise BatchError("Debe indicar al menos una región origen")
    sealed_names = {normalize_display_name(str(name)) for name in sealed_display_names if str(name).strip()}
    only_names = (
        {normalize_display_name(str(name)) for name in only_display_names if str(name).strip()}
        if only_display_names is not None
        else None
    )

    by_region: dict[str, list[ResourceRef]] = defaultdict(list)
    excluded: list[dict[str, Any]] = []
    for resource in catalog.resources:
        if resource.kind != "notebook" or resource.project != source_project or resource.location not in locations:
            continue
        if only_names is not None and normalize_display_name(resource.display_name) not in only_names:
            continue
        if not resource.display_name.strip():
            excluded.append({"status": "excluded_unnamed", "resource": resource.to_dict(), "reason": "display_name vacío"})
            continue
        by_region[resource.location].append(resource)

    selected: dict[str, list[ResourceRef]] = defaultdict(list)
    for region in locations:
        groups: dict[str, list[ResourceRef]] = defaultdict(list)
        for resource in by_region.get(region, []):
            groups[normalize_display_name(resource.display_name)].append(resource)
        for group in groups.values():
            group = sorted(group, key=lambda item: item.name)
            if len(group) == 1:
                chosen = group[0]
            else:
                chosen, reason = _safe_latest(group)
                if chosen is None:
                    for resource in group:
                        excluded.append(
                            {
                                "status": "pending_duplicate",
                                "resource": resource.to_dict(),
                                "reason": reason,
                                "display_name": resource.display_name,
                            }
                        )
                    continue
                for resource in group:
                    if resource.name != chosen.name:
                        excluded.append(
                            {
                                "status": "superseded_duplicate",
                                "resource": resource.to_dict(),
                                "selected_resource": chosen.name,
                                "commit_time": (_commit_time(resource).isoformat().replace("+00:00", "Z") if _commit_time(resource) else ""),
                            }
                        )
            mode = "sealed" if normalize_display_name(chosen.display_name) in sealed_names else "normal"
            selected[f"{mode}:{region}"].append(chosen)

    lots: list[dict[str, Any]] = []
    normal_lots: list[dict[str, Any]] = []
    sealed_lots: list[dict[str, Any]] = []
    for mode, limit, secret_handling in (("normal", normal_batch_size, "block"), ("sealed", sealed_batch_size, "sealed_copy")):
        for region in locations:
            resources = sorted(selected.get(f"{mode}:{region}", []), key=_resource_sort)
            for offset in range(0, len(resources), limit):
                batch = resources[offset : offset + limit]
                ordinal = offset // limit + 1
                campaign_id = f"{campaign_prefix}-{mode}-{_slug(region)}-r{ordinal:02d}"
                path = output_root / f"{mode}-{_slug(region)}-r{ordinal:02d}.selection.json"
                lot = _write_selection(
                    path=path,
                    campaign_id=campaign_id,
                    source_project=source_project,
                    destination_project=destination_project,
                    source_location=region,
                    destination_location=destination_location,
                    resources=batch,
                    secret_handling=secret_handling,
                )
                lots.append(lot)
                (sealed_lots if mode == "sealed" else normal_lots).append(lot)

    summary = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "catalog_generated_at": catalog.generated_at,
        "source_project": source_project,
        "destination_project": destination_project,
        "source_locations": list(locations),
        "destination_location": destination_location,
        "named_count": sum(len(items) for items in selected.values()),
        "excluded_unnamed_count": sum(item["status"] == "excluded_unnamed" for item in excluded),
        "superseded_duplicate_count": sum(item["status"] == "superseded_duplicate" for item in excluded),
        "pending_duplicate_count": sum(item["status"] == "pending_duplicate" for item in excluded),
        "excluded": excluded,
        "normal_lot_count": len(normal_lots),
        "sealed_lot_count": len(sealed_lots),
        "normal_lots": normal_lots,
        "sealed_lots": sealed_lots,
        "lots": lots,
    }
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "notebook-campaign.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def _markdown(summary: dict[str, Any]) -> str:
    lines = [
        f"# Campaña de notebooks: `{Path(summary['normal_lots'][0]['selection']).parent.name if summary.get('normal_lots') else 'sin lotes'}`",
        "",
        "Selecciones generadas desde un catálogo; no se exportó ni publicó código y no se ejecutó SQL/dry-run.",
        "",
        f"- Notebooks con nombre seleccionados: **{summary['named_count']}**",
        f"- Excluidos sin nombre: **{summary['excluded_unnamed_count']}**",
        f"- Duplicados reemplazados por el commit más reciente: **{summary['superseded_duplicate_count']}**",
        f"- Duplicados pendientes por fecha ausente/empatada: **{summary['pending_duplicate_count']}**",
        f"- Lotes normales (máx. 25): **{summary['normal_lot_count']}** · sellados (máx. 5): **{summary['sealed_lot_count']}**",
        "",
        "| Lote | Región origen | Modo | Recursos | Selección |",
        "|---|---|---|---:|---|",
    ]
    for lot in summary.get("lots") or []:
        lines.append(
            f"| `{lot['campaign_id']}` | `{lot['source_location']}` | `{lot['secret_handling']}` | {lot['count']} | `{lot['selection']}` |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-project", default="sbscol-dbreplication-prd")
    parser.add_argument("--destination-project", default="analytics-487218")
    parser.add_argument("--source-locations", default=",".join(DEFAULT_SOURCE_LOCATIONS))
    parser.add_argument("--destination-location", default="us-east1")
    parser.add_argument("--campaign-prefix", default="analytics-notebook-migration-20260907")
    parser.add_argument("--sealed-name", action="append", default=[], help="display_name que se procesará en sealed_copy; repetir para varios")
    parser.add_argument("--only-name", action="append", default=[], help="limitar la selección a estos display_name; repetir para un seguimiento")
    parser.add_argument("--normal-batch-size", type=int, default=25)
    parser.add_argument("--sealed-batch-size", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    summary = build_notebook_campaign(
        load_catalog(args.catalog.expanduser()),
        output_root=args.output_root.expanduser(),
        source_project=args.source_project,
        destination_project=args.destination_project,
        source_locations=tuple(args.source_locations.split(",")),
        destination_location=args.destination_location,
        campaign_prefix=args.campaign_prefix,
        sealed_display_names=args.sealed_name,
        only_display_names=args.only_name or None,
        normal_batch_size=args.normal_batch_size,
        sealed_batch_size=args.sealed_batch_size,
    )
    output_root = args.output_root.expanduser()
    (output_root / "notebook-campaign.md").write_text(_markdown(summary), encoding="utf-8")
    payload = {key: value for key, value in summary.items() if key not in {"excluded", "lots"}}
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) if args.json else f"{summary['named_count']} notebooks en {len(summary['lots'])} lotes: {output_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
