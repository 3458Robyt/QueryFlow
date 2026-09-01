"""Planning and orchestration primitives for the copy-only migration pilot.

The pilot is intentionally narrower than the normal QueryFlow workflow:
selection is deterministic, rewriting is local, and publication is only
reachable through the explicit ``--execute-migration`` command.  The module
contains no BigQuery execution path.
"""

from __future__ import annotations

import hashlib
import html
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .catalog import ResourceRef
from .migration import RouteDictionary, UnknownRoute, rewrite_text
from .notebooks import extract_code_cells


class CampaignError(RuntimeError):
    """The migration campaign cannot proceed safely."""


@dataclass(frozen=True)
class PilotSelection:
    resource: ResourceRef
    category: str
    content_sha256: str
    unknown_routes: tuple[UnknownRoute, ...] = ()
    # Optional richer incident records.  Older manifests only had route and
    # character offsets; new inventories also preserve the notebook cell/path
    # without storing SQL text.
    unknown_details: tuple[dict[str, Any], ...] = ()
    applied_mappings: tuple[str, ...] = ()
    filename: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource": self.resource.to_dict(),
            "category": self.category,
            "content_sha256": self.content_sha256,
            "unknown_routes": [dict(item) for item in self.unknown_details]
            if self.unknown_details
            else [item.to_dict() for item in self.unknown_routes],
            "applied_mappings": list(self.applied_mappings),
            "filename": self.filename,
            "status": "selected",
        }


def _selection_from_dict(item: Mapping[str, Any]) -> PilotSelection:
    resource_raw = item.get("resource")
    if not isinstance(resource_raw, Mapping):
        raise CampaignError("La manifest contiene una selección inválida")
    raw_unknown_routes = [route for route in (item.get("unknown_routes") or []) if isinstance(route, Mapping)]
    return PilotSelection(
        resource=ResourceRef.from_dict(dict(resource_raw)),
        category=str(item.get("category") or ""),
        content_sha256=str(item.get("content_sha256") or ""),
        unknown_routes=tuple(
            UnknownRoute(
                str(route.get("route") or ""),
                int(route.get("start", 0)),
                int(route.get("end", 0)),
            )
            for route in raw_unknown_routes
        ),
        unknown_details=tuple(dict(route) for route in raw_unknown_routes),
        applied_mappings=tuple(str(value) for value in (item.get("applied_mappings") or [])),
        filename=str(item.get("filename") or ""),
    )


@dataclass(frozen=True)
class PilotQuotas:
    known: int = 5
    incident: int = 3
    no_source_routes: int = 2

    @property
    def total(self) -> int:
        return self.known + self.incident + self.no_source_routes

    def to_dict(self) -> dict[str, int]:
        return {
            "known": self.known,
            "incident": self.incident,
            "no_source_routes": self.no_source_routes,
        }


@dataclass(frozen=True)
class PilotManifest:
    campaign_id: str
    source_project: str
    destination_project: str
    dictionary_id: str
    dictionary_sha256: str
    seed: str
    quotas: PilotQuotas
    selections: tuple[PilotSelection, ...]
    created_at: str = ""
    status: str = "planned"
    suffix: str = "_piloto_migracion"
    catalog_generated_at: str = ""
    cleanup: dict[str, Any] = field(default_factory=dict)
    kind_quotas: dict[str, PilotQuotas] = field(default_factory=dict)
    execution: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "campaign_id": self.campaign_id,
            "source_project": self.source_project,
            "destination_project": self.destination_project,
            "dictionary_id": self.dictionary_id,
            "dictionary_sha256": self.dictionary_sha256,
            "seed": self.seed,
            "quotas": self.quotas.to_dict(),
            "suffix": self.suffix,
            "catalog_generated_at": self.catalog_generated_at,
            "created_at": self.created_at,
            "status": self.status,
            "selections": [item.to_dict() for item in self.selections],
            "cleanup": self.cleanup,
            "kind_quotas": {kind: quota.to_dict() for kind, quota in sorted(self.kind_quotas.items())},
            "execution": self.execution,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PilotManifest":
        if int(raw.get("schema_version", 1)) != 1:
            raise CampaignError("La manifest de migración tiene una versión no soportada")
        quotas_raw = raw.get("quotas") or {}
        quotas = PilotQuotas(
            known=int(quotas_raw.get("known", 5)),
            incident=int(quotas_raw.get("incident", 3)),
            no_source_routes=int(quotas_raw.get("no_source_routes", 2)),
        )
        kind_quotas: dict[str, PilotQuotas] = {}
        raw_kind_quotas = raw.get("kind_quotas") or {}
        if isinstance(raw_kind_quotas, Mapping):
            for kind, value in raw_kind_quotas.items():
                if isinstance(value, Mapping):
                    kind_quotas[str(kind)] = PilotQuotas(
                        known=int(value.get("known", 0)),
                        incident=int(value.get("incident", 0)),
                        no_source_routes=int(value.get("no_source_routes", 0)),
                    )
        selections: list[PilotSelection] = []
        for item in raw.get("selections") or []:
            if not isinstance(item, Mapping):
                raise CampaignError("La manifest contiene una selección inválida")
            selections.append(_selection_from_dict(item))
        required = ("campaign_id", "source_project", "destination_project", "dictionary_id", "dictionary_sha256", "seed")
        if any(not str(raw.get(key) or "").strip() for key in required):
            raise CampaignError("La manifest no contiene el contexto de campaña completo")
        return cls(
            campaign_id=str(raw["campaign_id"]),
            source_project=str(raw["source_project"]),
            destination_project=str(raw["destination_project"]),
            dictionary_id=str(raw["dictionary_id"]),
            dictionary_sha256=str(raw["dictionary_sha256"]),
            seed=str(raw["seed"]),
            quotas=quotas,
            selections=tuple(selections),
            created_at=str(raw.get("created_at") or ""),
            status=str(raw.get("status") or "planned"),
            suffix=str(raw.get("suffix") or "_piloto_migracion"),
            catalog_generated_at=str(raw.get("catalog_generated_at") or ""),
            cleanup=dict(raw.get("cleanup") or {}),
            kind_quotas=kind_quotas,
            execution=dict(raw.get("execution") or {}),
        )


@dataclass(frozen=True)
class InventoryCheckpoint:
    """Private, content-free progress state for a remote inventory."""

    source_project: str
    destination_project: str
    dictionary_sha256: str
    seed: str
    catalog_generated_at: str
    selections: tuple[PilotSelection, ...] = ()
    inventory_errors: tuple[dict[str, Any], ...] = ()
    stats: dict[str, Any] = field(default_factory=dict)
    status: str = "partial"
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": self.status,
            "updated_at": self.updated_at,
            "source_project": self.source_project,
            "destination_project": self.destination_project,
            "dictionary_sha256": self.dictionary_sha256,
            "seed": self.seed,
            "catalog_generated_at": self.catalog_generated_at,
            "selections": [item.to_dict() for item in self.selections],
            "inventory_errors": [dict(item) for item in self.inventory_errors],
            "stats": dict(self.stats),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "InventoryCheckpoint":
        if int(raw.get("schema_version", 1)) != 1:
            raise CampaignError("El checkpoint de inventario tiene una versión no soportada")
        required = ("source_project", "destination_project", "dictionary_sha256", "seed", "catalog_generated_at")
        if any(not str(raw.get(key) or "").strip() for key in required):
            raise CampaignError("El checkpoint de inventario no contiene el contexto completo")
        selections = tuple(
            _selection_from_dict(item)
            for item in (raw.get("selections") or [])
            if isinstance(item, Mapping)
        )
        errors = tuple(dict(item) for item in (raw.get("inventory_errors") or []) if isinstance(item, Mapping))
        stats = raw.get("stats") or {}
        if not isinstance(stats, Mapping):
            raise CampaignError("Los contadores del checkpoint de inventario son inválidos")
        return cls(
            source_project=str(raw["source_project"]),
            destination_project=str(raw["destination_project"]),
            dictionary_sha256=str(raw["dictionary_sha256"]),
            seed=str(raw["seed"]),
            catalog_generated_at=str(raw["catalog_generated_at"]),
            selections=selections,
            inventory_errors=errors,
            stats=dict(stats),
            status=str(raw.get("status") or "partial"),
            updated_at=str(raw.get("updated_at") or ""),
        )


def _as_text(value: bytes | str) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def classify_text(text: str, dictionary: RouteDictionary) -> str:
    _rewritten, applied, unknown = rewrite_text(text, dictionary)
    if unknown:
        return "incident"
    if applied:
        return "known"
    return "no_source_routes"


def _selection(resource: ResourceRef, content: bytes | str, dictionary: RouteDictionary, filename: str = "") -> PilotSelection:
    full_text = _as_text(content)
    applied_ids: list[str] = []
    unknown: list[UnknownRoute] = []
    unknown_details: list[dict[str, Any]] = []
    if resource.kind == "notebook":
        try:
            code_cells = extract_code_cells(full_text.encode("utf-8"))
        except Exception:
            # Invalid notebook JSON is still classified from its raw bytes;
            # the later export/readback step reports the concrete error.
            code_cells = None
        if code_cells is not None:
            for index, language, source in code_cells:
                _rewritten, applied, cell_unknown = rewrite_text(source, dictionary)
                applied_ids.extend(item.mapping_id for item in applied)
                unknown.extend(cell_unknown)
                suffix = "sql" if language in {"sql", "bigquery"} else "py"
                path = f"cells/b{index:04d}.{suffix}"
                unknown_details.extend({"path": path, "cell": index, **route.to_dict()} for route in cell_unknown)
        else:
            _rewritten, applied, unknown = rewrite_text(full_text, dictionary)
            applied_ids.extend(item.mapping_id for item in applied)
            unknown_details.extend({"path": filename or "content", "cell": None, **route.to_dict()} for route in unknown)
    else:
        _rewritten, applied, unknown = rewrite_text(full_text, dictionary)
        applied_ids.extend(item.mapping_id for item in applied)
        unknown_details.extend({"path": filename or "content", "cell": None, **route.to_dict()} for route in unknown)
    return PilotSelection(
        resource=resource,
        category="incident" if unknown else "known" if applied else "no_source_routes",
        content_sha256=hashlib.sha256(full_text.encode("utf-8")).hexdigest(),
        unknown_routes=tuple(unknown),
        unknown_details=tuple(unknown_details),
        applied_mappings=tuple(applied_ids),
        filename=filename,
    )


def select_stratified(
    resources: Sequence[ResourceRef],
    contents: Mapping[str, bytes | str],
    dictionary: RouteDictionary,
    *,
    seed: str,
    quotas: PilotQuotas | None = None,
    filenames: Mapping[str, str] | None = None,
) -> list[PilotSelection]:
    """Select a deterministic 5/3/2 sample from one resource kind.

    The caller supplies exported content so classification is based on the
    actual code while the manifest stores only hashes and route metadata.
    """
    quotas = quotas or PilotQuotas()
    filenames = filenames or {}
    # A catalog can contain the same canonical resource more than once (for
    # example after merging Asset Inventory and a cached catalog).  A sample
    # must never publish the same resource twice, so de-duplicate by canonical
    # name before classifying or sampling.
    candidates: list[ResourceRef] = []
    seen_names: set[str] = set()
    for resource in resources:
        if resource.name not in contents or resource.name in seen_names:
            continue
        seen_names.add(resource.name)
        candidates.append(resource)
    if len(candidates) < quotas.total:
        raise CampaignError(f"No hay {quotas.total} recursos elegibles para el estrato solicitado")
    grouped: dict[str, list[PilotSelection]] = {"known": [], "incident": [], "no_source_routes": []}
    for resource in candidates:
        item = _selection(resource, contents[resource.name], dictionary, filenames.get(resource.name, ""))
        grouped[item.category].append(item)
    for category, quota in quotas.to_dict().items():
        if len(grouped[category]) < quota:
            raise CampaignError(
                f"No hay suficientes recursos para el estrato {category}: "
                f"se requieren {quota}, disponibles {len(grouped[category])}"
            )
    randomizer = random.Random(_seed_int(seed))
    selected: list[PilotSelection] = []
    for category, quota in quotas.to_dict().items():
        values = sorted(grouped[category], key=lambda item: (item.resource.name, item.resource.fingerprint))
        randomizer.shuffle(values)
        selected.extend(values[:quota])
    # Keep manifest ordering stable regardless of the order in which strata
    # were sampled.  The category field still exposes the quota split.
    return sorted(selected, key=lambda item: (item.resource.kind, item.category, item.resource.name))


def _seed_int(seed: str) -> int:
    return int(hashlib.sha256(str(seed).encode("utf-8")).hexdigest()[:16], 16)


def campaign_id_for(*, source_project: str, destination_project: str, dictionary: RouteDictionary, seed: str) -> str:
    raw = f"{source_project}|{destination_project}|{dictionary.dictionary_sha256}|{seed}"
    return "migration-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_manifest(
    selections: Iterable[PilotSelection],
    *,
    source_project: str,
    destination_project: str,
    dictionary: RouteDictionary,
    seed: str,
    campaign_id: str | None = None,
    catalog_generated_at: str = "",
    suffix: str = "_piloto_migracion",
    kind_quotas: Mapping[str, PilotQuotas] | None = None,
) -> PilotManifest:
    campaign_id = campaign_id or campaign_id_for(
        source_project=source_project,
        destination_project=destination_project,
        dictionary=dictionary,
        seed=seed,
    )
    values = tuple(selections)
    counts = {category: sum(1 for item in values if item.category == category) for category in ("known", "incident", "no_source_routes")}
    quotas = PilotQuotas(**counts)
    return PilotManifest(
        campaign_id=campaign_id,
        source_project=source_project,
        destination_project=destination_project,
        dictionary_id=dictionary.dictionary_id,
        dictionary_sha256=dictionary.dictionary_sha256,
        seed=seed,
        quotas=quotas,
        selections=values,
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        suffix=suffix,
        catalog_generated_at=catalog_generated_at,
        kind_quotas=dict(kind_quotas or {}),
    )


def save_manifest(manifest: PilotManifest, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def save_inventory_checkpoint(checkpoint: InventoryCheckpoint, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(checkpoint.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_inventory_checkpoint(path: Path) -> InventoryCheckpoint:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CampaignError(f"No se pudo leer el checkpoint de inventario: {error}") from error
    if not isinstance(raw, Mapping):
        raise CampaignError("El checkpoint de inventario debe ser un objeto")
    return InventoryCheckpoint.from_dict(raw)


def load_manifest(path: Path) -> PilotManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CampaignError(f"No se pudo leer la manifest de campaña: {error}") from error
    if not isinstance(raw, Mapping):
        raise CampaignError("La manifest de campaña debe ser un objeto")
    return PilotManifest.from_dict(raw)


def cleanup_digest(repositories: Iterable[str]) -> str:
    values = sorted({str(value).strip() for value in repositories if str(value).strip()})
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode("utf-8")).hexdigest()


def campaign_publish_digest(manifest: PilotManifest) -> str:
    """Hash immutable campaign intent, excluding mutable execution/cleanup state."""
    payload = {
        "schema_version": 1,
        "campaign_id": manifest.campaign_id,
        "source_project": manifest.source_project,
        "destination_project": manifest.destination_project,
        "dictionary_id": manifest.dictionary_id,
        "dictionary_sha256": manifest.dictionary_sha256,
        "seed": manifest.seed,
        "suffix": manifest.suffix,
        "selections": [
            item.to_dict()
            for item in sorted(
                manifest.selections,
                key=lambda value: (value.resource.kind, value.resource.name),
            )
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def make_cleanup_plan(manifest: PilotManifest) -> dict[str, Any]:
    repositories: list[str] = []
    for selection in manifest.selections:
        # Published fields are deliberately read from an optional serialized
        # record, never inferred from a source resource name.
        published = getattr(selection, "published", None)
        if isinstance(published, Mapping) and published.get("repository"):
            repositories.append(str(published["repository"]))
    # The manifest may be loaded from JSON where publication records live in
    # cleanup.created_repositories; this is the canonical cleanup input.
    repositories.extend(str(item) for item in (manifest.cleanup.get("created_repositories") or []))
    values = sorted(set(repositories))
    return {
        "schema_version": 1,
        "campaign_id": manifest.campaign_id,
        "destination_project": manifest.destination_project,
        "repositories": values,
        "approved_digest": cleanup_digest(values),
        "force": False,
        "warning": "Solo incluye repositorios creados por esta campaña; se omiten recursos modificados.",
    }


def make_incident_report(manifest: PilotManifest) -> dict[str, Any]:
    """Create a row-free report of every unknown route found in the pilot."""
    incidents = _incident_records(manifest.selections)
    return {
        "schema_version": 1,
        "campaign_id": manifest.campaign_id,
        "status": "published_with_incidents" if incidents else manifest.status,
        "unknown_route_count": len(incidents),
        "incidents": incidents,
        "policy": "Las rutas desconocidas se conservan sin cambios; requieren revisión manual.",
    }


def make_inventory_incident_report(
    selections: Sequence[PilotSelection],
    *,
    campaign_id: str,
) -> dict[str, Any]:
    """Create an incident report before a strict campaign manifest exists."""
    incidents = _incident_records(selections)
    return {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "status": "inventory_shortfall",
        "unknown_route_count": len(incidents),
        "incidents": incidents,
        "policy": "Las rutas desconocidas se conservan sin cambios; requieren revisión manual. La campaña aún no tiene cupo 5/3/2.",
    }


def _incident_records(selections: Sequence[PilotSelection]) -> list[dict[str, Any]]:
    incidents: list[dict[str, Any]] = []
    for selection in selections:
        details = selection.unknown_details or tuple(route.to_dict() for route in selection.unknown_routes)
        for route in details:
            incident = {
                "resource": selection.resource.to_dict(),
                "category": selection.category,
                **route,
            }
            incident.setdefault("path", selection.filename or "content")
            incidents.append(incident)
    return incidents


def render_campaign_review(manifest: PilotManifest, *, task_links: Mapping[str, str] | None = None) -> str:
    """Render a compact dark, row-free batch review page."""
    task_links = task_links or {}
    rows: list[str] = []
    for item in manifest.selections:
        resource = item.resource
        unknown = len(item.unknown_routes)
        status = "Incidente" if unknown else "Ruta conocida" if item.applied_mappings else "Sin ruta origen"
        tone = "warn" if unknown else "ok" if item.applied_mappings else "muted"
        link = task_links.get(resource.name)
        diff = f'<a href="{html.escape(str(link), quote=True)}">Ver diff</a>' if link else "Pendiente"
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(resource.display_name)}</code><small>{html.escape(resource.kind)}</small></td>"
            f"<td><span class=\"badge {tone}\">{html.escape(status)}</span></td>"
            f"<td>{len(item.applied_mappings)}</td><td>{unknown}</td><td>{diff}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>QueryFlow · {html.escape(manifest.campaign_id)}</title>
<style>
:root {{ color-scheme: dark; --bg:#0b1020; --panel:#121a2b; --line:#26334d; --text:#e5e7eb; --muted:#94a3b8; --green:#34d399; --amber:#fbbf24; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.45 system-ui,sans-serif; }}
main {{ max-width:980px; margin:0 auto; padding:28px 20px 48px; }} h1 {{ margin:0 0 6px; font-size:24px; }} p {{ color:var(--muted); }}
.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; overflow:hidden; }} table {{ width:100%; border-collapse:collapse; }} th,td {{ text-align:left; padding:12px 14px; border-bottom:1px solid var(--line); }} th {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.05em; }} td small {{ display:block; color:var(--muted); margin-top:2px; }} code {{ font-family:ui-monospace,monospace; }} .badge {{ display:inline-flex; border:1px solid var(--line); border-radius:999px; padding:3px 8px; font-size:12px; }} .ok {{ color:var(--green); }} .warn {{ color:var(--amber); }} .muted {{ color:var(--muted); }} @media(max-width:680px) {{ main {{ padding:20px 12px; }} th,td {{ padding:10px 8px; }} th:nth-child(3),td:nth-child(3) {{ display:none; }} }}
</style></head><body><main>
<h1>Piloto de migración</h1><p>{html.escape(manifest.campaign_id)} · {html.escape(manifest.source_project)} → {html.escape(manifest.destination_project)}</p>
<div class="panel"><table><thead><tr><th>Recurso</th><th>Clasificación</th><th>Mappings</th><th>Incidentes</th><th>Diff Original / Propuesta</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
</main></body></html>\n"""


def migration_publish_allowed(profile_name: str, *, execute_migration: bool) -> bool:
    """Return whether the explicit campaign switch authorizes copy writes."""
    return profile_name == "migration-pilot" and execute_migration


def select_classified(
    selections: Sequence[PilotSelection],
    *,
    source_project: str,
    destination_project: str,
    dictionary: RouteDictionary,
    seed: str,
    campaign_id: str | None = None,
    catalog_generated_at: str = "",
) -> PilotManifest:
    """Select the fixed sample from already-inspected resources.

    Keeping classification separate from sampling prevents the inventory
    command from re-reading/reclassifying remote notebooks after it has
    recorded the Dataform head and avoids discrepancies in incident reports.
    """
    grouped: dict[str, dict[str, list[PilotSelection]]] = {
        kind: {category: [] for category in PilotQuotas().to_dict()}
        for kind in ("shared_query", "notebook")
    }
    seen_names: set[str] = set()
    for selection in selections:
        resource = selection.resource
        if resource.kind not in grouped:
            raise CampaignError(f"Tipo de recurso no permitido en la campaña: {resource.kind}")
        if resource.project != source_project:
            raise CampaignError(f"El recurso {resource.name} está fuera del proyecto origen de la campaña")
        if resource.name in seen_names:
            raise CampaignError(f"La campaña contiene dos veces el recurso canónico: {resource.name}")
        if selection.category not in grouped[resource.kind]:
            raise CampaignError(f"Categoría de selección no permitida: {selection.category}")
        seen_names.add(resource.name)
        grouped[resource.kind][selection.category].append(selection)
    selected: list[PilotSelection] = []
    kind_quotas: dict[str, PilotQuotas] = {}
    for kind in ("shared_query", "notebook"):
        quotas = PilotQuotas()
        for category, quota in quotas.to_dict().items():
            available = len(grouped[kind][category])
            if available < quota:
                raise CampaignError(
                    f"No hay suficientes recursos para el estrato {category}: "
                    f"se requieren {quota}, disponibles {available}"
                )
        randomizer = random.Random(_seed_int(f"{seed}:{kind}"))
        for category, quota in quotas.to_dict().items():
            values = sorted(
                grouped[kind][category],
                key=lambda item: (item.resource.name, item.resource.fingerprint),
            )
            randomizer.shuffle(values)
            selected.extend(values[:quota])
        kind_quotas[kind] = quotas
    return build_manifest(
        sorted(selected, key=lambda item: (item.resource.kind, item.category, item.resource.name)),
        source_project=source_project,
        destination_project=destination_project,
        dictionary=dictionary,
        seed=seed,
        campaign_id=campaign_id,
        catalog_generated_at=catalog_generated_at,
        kind_quotas=kind_quotas,
    )


def select_campaign(
    resources: Sequence[ResourceRef],
    contents: Mapping[str, bytes | str],
    dictionary: RouteDictionary,
    *,
    source_project: str,
    destination_project: str,
    seed: str,
    campaign_id: str | None = None,
    catalog_generated_at: str = "",
    destination_display_names: Iterable[str] = (),
    filenames: Mapping[str, str] | None = None,
) -> PilotManifest:
    """Build the 10-query + 10-notebook pilot manifest.

    Collisions are omitted before sampling.  Sampling each kind independently
    guarantees the requested 5/3/2 strata per resource type.
    """
    # Asset Inventory may omit display names.  An empty value is not a real
    # collision and must not exclude every unnamed source notebook.
    names = {str(value).strip().casefold() for value in destination_display_names if str(value).strip()}
    eligible = [
        resource
        for resource in resources
        if resource.project == source_project
        and resource.kind in {"shared_query", "notebook"}
        and (not resource.display_name.strip() or resource.display_name.strip().casefold() not in names)
        and resource.name in contents
    ]
    filenames = filenames or {}
    inspected = [
        _selection(resource, contents[resource.name], dictionary, filenames.get(resource.name, ""))
        for resource in eligible
    ]
    return select_classified(
        inspected,
        source_project=source_project,
        destination_project=destination_project,
        dictionary=dictionary,
        seed=seed,
        campaign_id=campaign_id,
        catalog_generated_at=catalog_generated_at,
    )


def validate_pilot_manifest(manifest: PilotManifest) -> None:
    """Enforce the fixed 10-query/10-notebook campaign contract."""
    if manifest.suffix != "_piloto_migracion":
        raise CampaignError("El sufijo de la campaña no coincide con el piloto aprobado")
    if not manifest.source_project or not manifest.destination_project:
        raise CampaignError("La campaña debe declarar proyectos origen y destino")
    if manifest.source_project == manifest.destination_project:
        raise CampaignError("El proyecto origen y destino no pueden ser el mismo")
    counts = {kind: sum(1 for item in manifest.selections if item.resource.kind == kind) for kind in ("shared_query", "notebook")}
    if counts != {"shared_query": 10, "notebook": 10}:
        raise CampaignError("La campaña debe contener exactamente 10 Shared Queries y 10 notebooks")
    expected_aggregate = {"known": 10, "incident": 6, "no_source_routes": 4}
    actual_aggregate = {
        category: sum(1 for item in manifest.selections if item.category == category)
        for category in expected_aggregate
    }
    if actual_aggregate != expected_aggregate or manifest.quotas.to_dict() != expected_aggregate:
        raise CampaignError("Las cuotas agregadas de la campaña no coinciden con 10 recursos por tipo")
    seen_names: set[str] = set()
    for kind in counts:
        quota = manifest.kind_quotas.get(kind)
        if quota is None or quota.to_dict() != PilotQuotas().to_dict():
            raise CampaignError(f"Las cuotas 5/3/2 no están completas para {kind}")
        actual = {
            category: sum(
                1
                for item in manifest.selections
                if item.resource.kind == kind and item.category == category
            )
            for category in PilotQuotas().to_dict()
        }
        if actual != PilotQuotas().to_dict():
            raise CampaignError(f"Las cuotas 5/3/2 no coinciden con los recursos seleccionados para {kind}")
    if set(manifest.kind_quotas) != {"shared_query", "notebook"}:
        raise CampaignError("La campaña solo puede contener las dos clases de recursos aprobadas")
    for item in manifest.selections:
        resource = item.resource
        if resource.kind not in {"shared_query", "notebook"}:
            raise CampaignError(f"Tipo de recurso no permitido en la campaña: {resource.kind}")
        if resource.project != manifest.source_project:
            raise CampaignError(f"El recurso {resource.name} está fuera del proyecto origen de la campaña")
        if not resource.name or not resource.display_name:
            raise CampaignError("Cada selección debe tener nombre canónico y display_name")
        if item.category not in {"known", "incident", "no_source_routes"}:
            raise CampaignError(f"Categoría de selección no permitida: {item.category}")
        if resource.name in seen_names:
            raise CampaignError(f"La campaña contiene dos veces el recurso canónico: {resource.name}")
        seen_names.add(resource.name)
