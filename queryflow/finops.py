"""Governed FinOps and cloud-health assessments.

The assessment layer deliberately sits beside QueryFlow's SQL/notebook task
flow.  It produces deterministic, read-only snapshots from explicitly allowed
projects and keeps provider responses out of the local evidence artifacts.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.parse import urlparse
import tomllib

from .config import QueryflowConfig
from .gcloud import GcloudContext


ASSESSMENT_SCHEMA_VERSION = 1
DEFAULT_OUTPUT_ROOT = Path.home() / ".queryflow" / "assessments"
PROJECT_PATTERN = re.compile(r"[a-z][a-z0-9-]{4,29}\Z")
LOCATION_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")
TABLE_PATTERN = re.compile(r"[a-z][a-z0-9-]{4,29}\.[A-Za-z_][A-Za-z0-9_]{0,1023}\.[A-Za-z_][A-Za-z0-9_]{0,1023}\Z")
RESOURCE_CONTEXT_FIELDS = ("business_unit", "owner", "cost_center", "criticality")
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
ALLOWED_ASSET_TYPES = (
    "compute.googleapis.com/Instance",
    "compute.googleapis.com/Disk",
    "compute.googleapis.com/Address",
    "container.googleapis.com/Cluster",
    "run.googleapis.com/Service",
    "sqladmin.googleapis.com/Instance",
    "bigquery.googleapis.com/Dataset",
    "bigquery.googleapis.com/Table",
    "bigquerydatatransfer.googleapis.com/TransferConfig",
)
RECOMMENDER_IDS = (
    "google.bigquery.table.PartitionClusterRecommender",
    "google.cloudbilling.commitment.SpendBasedCommitmentRecommender",
    "google.compute.commitment.UsageCommitmentRecommender",
    "google.compute.image.IdleResourceRecommender",
    "google.compute.address.IdleResourceRecommender",
    "google.compute.disk.IdleResourceRecommender",
    "google.compute.instance.IdleResourceRecommender",
    "google.compute.instance.MachineTypeRecommender",
    "google.cloudsql.instance.IdleRecommender",
    "google.cloudsql.instance.OverprovisionedRecommender",
    "google.run.service.CostRecommender",
    "google.container.DiagnosisRecommender",
)
JsonRunner = Callable[[list[str]], object]
AggregateRunner = Callable[..., dict[str, Any]]


class FinOpsError(RuntimeError):
    """The assessment could not be generated safely."""


@dataclass(frozen=True)
class BusinessContext:
    """Sanitized, optional business ownership map."""

    label_keys: dict[str, str]
    projects: dict[str, dict[str, str]]
    resources: dict[str, dict[str, str]]
    fingerprint: Optional[str]

    def resolve(
        self,
        project: str,
        resource_id: str,
        labels: Mapping[str, Any] | None = None,
        tags: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        labels_map = {str(key): str(value) for key, value in (labels or {}).items() if value is not None}
        tags_map = {str(key): str(value) for key, value in (tags or {}).items() if value is not None}
        project_map = self.projects.get(project, {})
        resource_map = self.resources.get(resource_id, {})
        values: dict[str, Optional[str]] = {}
        provenance: dict[str, str] = {}
        for field in RESOURCE_CONTEXT_FIELDS:
            if resource_map.get(field):
                values[field] = resource_map[field]
                provenance[field] = "resource_map"
                continue
            alias = self.label_keys.get(field, field)
            label_value = labels_map.get(alias) or tags_map.get(alias)
            if label_value:
                values[field] = label_value
                provenance[field] = "gcp_label_or_tag"
                continue
            if project_map.get(field):
                values[field] = project_map[field]
                provenance[field] = "project_map"
                continue
            values[field] = None
            provenance[field] = "unknown"
        present = sum(value is not None for value in values.values())
        return {
            **values,
            "provenance": provenance,
            "coverage_pct": round(present / len(RESOURCE_CONTEXT_FIELDS) * 100, 2),
        }


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_project(project: str) -> str:
    project = str(project).strip()
    if not PROJECT_PATTERN.fullmatch(project):
        raise FinOpsError(f"Proyecto GCP inválido: {project}")
    return project


def _parse_location(location: str) -> str:
    location = str(location).strip()
    if not LOCATION_PATTERN.fullmatch(location):
        raise FinOpsError(f"Ubicación GCP inválida: {location}")
    return location


def _parse_table(value: str) -> str:
    value = str(value).strip().replace("`", "")
    if not TABLE_PATTERN.fullmatch(value):
        raise FinOpsError("billing_export_table debe usar el formato project.dataset.table")
    return value


def resolve_projects(config: QueryflowConfig, requested: Iterable[str] | None = None) -> tuple[str, ...]:
    """Resolve an explicit subset without ever expanding the configured scope."""
    def normalize(value: str) -> str:
        alias = config.project_aliases.get(str(value).strip(), str(value).strip())
        return _parse_project(alias)

    configured = config.finops_projects or tuple(dict.fromkeys((*config.source_projects, *config.destination_projects)))
    allowed = tuple(sorted({normalize(project) for project in configured}))
    if not allowed:
        raise FinOpsError(
            "No hay proyectos FinOps permitidos; configura finops_projects o source_projects/destination_projects"
        )
    requested_values: list[str] = []
    for value in requested or ():
        requested_values.extend(part.strip() for part in str(value).split(",") if part.strip())
    if not requested_values:
        return allowed
    selected = tuple(sorted({normalize(project) for project in requested_values}))
    outside = sorted(set(selected) - set(allowed))
    if outside:
        raise FinOpsError("--projects solo puede reducir la allowlist configurada; fuera de alcance: " + ", ".join(outside))
    return selected


def _assert_safe_context(value: Any, path: str = "context") -> None:
    forbidden = {"token", "access_token", "refresh_token", "password", "secret", "private_key"}
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in forbidden:
                raise FinOpsError(f"El mapa de contexto no admite la clave {path}.{key}")
            _assert_safe_context(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_safe_context(child, f"{path}[{index}]")
    elif isinstance(value, str) and any(marker in value.lower() for marker in ("bearer ", "private_key", "-----begin")):
        raise FinOpsError("El mapa de contexto no admite secretos")


def _context_fields(value: Any, path: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise FinOpsError(f"{path} debe ser una tabla")
    result: dict[str, str] = {}
    for field in RESOURCE_CONTEXT_FIELDS:
        item = value.get(field)
        if item is not None:
            text = str(item).strip()
            if text:
                result[field] = text[:256]
    return result


def load_business_context(path: Path | None) -> BusinessContext:
    if path is None:
        return BusinessContext({}, {}, {}, None)
    expanded = path.expanduser()
    try:
        raw = tomllib.loads(expanded.read_text(encoding="utf-8"))
    except OSError as error:
        raise FinOpsError(f"No se pudo leer business_context: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise FinOpsError(f"business_context no es TOML válido: {error}") from error
    _assert_safe_context(raw)
    if not isinstance(raw, dict):
        raise FinOpsError("business_context debe ser una tabla TOML")
    schema_version = int(raw.get("schema_version", 1))
    if schema_version != 1:
        raise FinOpsError("business_context schema_version debe ser 1")
    raw_keys = raw.get("label_keys") or {}
    if not isinstance(raw_keys, dict):
        raise FinOpsError("business_context.label_keys debe ser una tabla")
    label_keys = {
        field: str(raw_keys.get(field, field)).strip()[:128]
        for field in RESOURCE_CONTEXT_FIELDS
        if str(raw_keys.get(field, field)).strip()
    }
    raw_projects = raw.get("projects") or {}
    raw_resources = raw.get("resources") or {}
    if not isinstance(raw_projects, dict) or not isinstance(raw_resources, dict):
        raise FinOpsError("business_context.projects y resources deben ser tablas")
    projects = {_parse_project(str(key)): _context_fields(value, f"projects.{key}") for key, value in raw_projects.items()}
    resources = {
        str(key): _context_fields(value, f"resources.{key}")
        for key, value in raw_resources.items()
        if str(key).strip()
    }
    sanitized = {"schema_version": 1, "label_keys": label_keys, "projects": projects, "resources": resources}
    return BusinessContext(label_keys, projects, resources, _sha256(sanitized))


def _run_json(command: list[str], context: GcloudContext, runner: JsonRunner | None) -> Any:
    return runner(command) if runner else context.json(command)


def _resource_project(name: str, fallback: str) -> str:
    match = re.search(r"(?:^|/)projects/([^/]+)(?:/|$)", name)
    return match.group(1) if match else fallback


def _resource_location(name: str) -> str:
    match = re.search(r"(?:^|/)locations/([^/]+)(?:/|$)", name)
    return match.group(1) if match else "global"


def _strip_provider_prefix(name: str) -> str:
    return re.sub(r"^//[^/]+/", "", str(name))


def _safe_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): str(item)[:256]
        for key, item in value.items()
        if str(key).strip() and item is not None and not isinstance(item, (dict, list))
    }


def collect_asset_inventory(
    projects: Iterable[str],
    context: GcloudContext,
    *,
    runner: JsonRunner | None = None,
    asset_types: Iterable[str] = ALLOWED_ASSET_TYPES,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    resources: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    calls = 0
    successes = 0
    for project in projects:
        for asset_type in asset_types:
            calls += 1
            command = [
                "gcloud",
                "asset",
                "search-all-resources",
                f"--scope=projects/{project}",
                f"--asset-types={asset_type}",
                "--format=json",
                "--quiet",
            ]
            try:
                raw = _run_json(command, context, runner)
            except Exception as error:
                warnings.append(f"{project}/{asset_type}: {error}")
                continue
            if not isinstance(raw, list):
                warnings.append(f"{project}/{asset_type}: Cloud Asset Inventory no devolvió una lista")
                continue
            successes += 1
            for item in raw:
                if not isinstance(item, dict):
                    continue
                name = _strip_provider_prefix(str(item.get("name") or ""))
                if not name:
                    continue
                resource = {
                    "resource_id": name,
                    "asset_type": asset_type,
                    "kind": asset_type.rsplit("/", 1)[-1].lower(),
                    "project": _resource_project(name, project),
                    "location": _resource_location(name),
                    "display_name": str(item.get("displayName") or name.rsplit("/", 1)[-1])[:256],
                    "labels": _safe_map(item.get("labels")),
                    "tags": _safe_map(item.get("tags")),
                    "updated_at": str(item.get("updateTime") or "")[:64],
                }
                resources[name] = resource
    status = "ok" if calls and successes == calls else "partial" if successes else "unavailable"
    return sorted(resources.values(), key=lambda item: (item["project"], item["resource_id"])), {
        "status": status,
        "source": "cloud_asset_inventory",
        "calls": calls,
        "successful_calls": successes,
        "resource_count": len(resources),
        "warnings": warnings,
        "provenance": "gcloud asset search-all-resources with an allowlisted asset-type set",
    }


def _recommendation_impact(item: Mapping[str, Any]) -> dict[str, Any]:
    impact = item.get("primaryImpact") or item.get("primary_impact") or {}
    if not isinstance(impact, dict):
        return {}
    result: dict[str, Any] = {}
    cost = impact.get("costProjection") or impact.get("cost_projection")
    if isinstance(cost, dict):
        cost_value = cost.get("cost")
        if isinstance(cost_value, dict):
            for key in ("units", "nanos", "currencyCode"):
                if key in cost_value:
                    result[key] = cost_value[key]
        elif cost_value is not None:
            result["cost"] = str(cost_value)[:128]
    for key in ("category", "reliabilityProjection", "performanceProjection"):
        if key in impact and not isinstance(impact[key], (dict, list)):
            result[key] = str(impact[key])[:128]
    return result


def _target_resources(item: Mapping[str, Any]) -> list[str]:
    raw = item.get("targetResources") or item.get("target_resources") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return sorted({str(value)[:512] for value in raw if str(value).strip()})[:20]


def collect_recommendations(
    projects: Iterable[str],
    context: GcloudContext,
    *,
    locations: Iterable[str] = ("global",),
    runner: JsonRunner | None = None,
    recommender_ids: Iterable[str] = RECOMMENDER_IDS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    recommendations: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    calls = 0
    successes = 0
    for project in projects:
        for location in locations:
            _parse_location(location)
            for recommender in recommender_ids:
                calls += 1
                command = [
                    "gcloud",
                    "recommender",
                    "recommendations",
                    "list",
                    f"--project={project}",
                    f"--location={location}",
                    f"--recommender={recommender}",
                    "--format=json",
                    "--quiet",
                ]
                try:
                    raw = _run_json(command, context, runner)
                except Exception as error:
                    warnings.append(f"{project}/{location}/{recommender}: {error}")
                    continue
                if not isinstance(raw, list):
                    warnings.append(f"{project}/{location}/{recommender}: respuesta inválida")
                    continue
                successes += 1
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    recommendation_id = str(item.get("name") or "")
                    if not recommendation_id:
                        recommendation_id = _sha256({"project": project, "recommender": recommender, "item": item})
                    recommendations[recommendation_id] = {
                        "recommendation_id": recommendation_id,
                        "project": project,
                        "location": location,
                        "recommender": recommender,
                        "subtype": str(item.get("recommenderSubtype") or item.get("recommender_subtype") or "")[:128],
                        "description": str(item.get("description") or "")[:512],
                        "state": str((item.get("stateInfo") or {}).get("state") or item.get("state") or "")[:64],
                        "resource_ids": _target_resources(item),
                        "impact": _recommendation_impact(item),
                        "last_refresh": str(item.get("lastRefreshTime") or "")[:64],
                    }
    status = "ok" if calls and successes == calls else "partial" if successes else "unavailable"
    return sorted(recommendations.values(), key=lambda item: item["recommendation_id"]), {
        "status": status,
        "source": "recommender",
        "calls": calls,
        "successful_calls": successes,
        "recommendation_count": len(recommendations),
        "warnings": warnings,
        "provenance": "gcloud recommender recommendations list with a reviewed recommender allowlist",
    }


def build_aggregate_queries(
    projects: Iterable[str],
    *,
    current_start: date,
    current_end: date,
    previous_start: date,
    location: str,
    billing_table: str | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Build only fixed, aggregate SQL templates for Workbench execution."""
    location = _parse_location(location)
    region = f"region-{location}"
    values: dict[str, list[dict[str, str]]] = {"jobs": [], "storage": [], "billing": []}
    for project in sorted({_parse_project(item) for item in projects}):
        values["jobs"].append(
            {
                "project": project,
                "sql": (
                    f"SELECT '{project}' AS project_id, COUNT(*) AS job_count, "
                    "SUM(total_bytes_billed) AS total_bytes_billed, SUM(total_slot_ms) AS total_slot_ms "
                    f"FROM `{project}`.`{region}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT "
                    f"WHERE creation_time >= TIMESTAMP('{current_start.isoformat()}') "
                    f"AND creation_time < TIMESTAMP('{current_end.isoformat()}') LIMIT 1"
                ),
            }
        )
        values["storage"].append(
            {
                "project": project,
                "sql": (
                    f"SELECT '{project}' AS project_id, COUNT(*) AS table_count, "
                    "SUM(total_physical_bytes) AS total_physical_bytes "
                    f"FROM `{project}`.`{region}`.INFORMATION_SCHEMA.TABLE_STORAGE_BY_PROJECT LIMIT 1"
                ),
            }
        )
    if billing_table:
        table = _parse_table(billing_table)
        values["billing"].append(
            {
                "project": "*",
                "sql": (
                    "SELECT project.id AS project_id, "
                    "CASE WHEN DATE(usage_start_time) >= DATE('" + current_start.isoformat() + "') "
                    "THEN 'current' ELSE 'previous' END AS period, "
                    "SUM(cost) AS cost, COUNT(*) AS line_count "
                    f"FROM `{table}` WHERE DATE(usage_start_time) >= DATE('{previous_start.isoformat()}') "
                    f"AND DATE(usage_start_time) < DATE('{current_end.isoformat()}') "
                    "GROUP BY project_id, period LIMIT 100"
                ),
            }
        )
    return values


def _sanitize_aggregate_rows(rows: Any, *, limit: int = 100) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    safe: list[dict[str, Any]] = []
    allowed_keys = {
        "project_id", "period", "job_count", "total_bytes_billed", "total_slot_ms",
        "table_count", "total_physical_bytes", "cost", "line_count",
    }
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        clean: dict[str, Any] = {}
        for key, value in row.items():
            if str(key) not in allowed_keys:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                clean[str(key)] = value
        if clean:
            safe.append(clean)
    return safe


def collect_aggregates(
    projects: Iterable[str],
    config: QueryflowConfig,
    *,
    current_start: date,
    current_end: date,
    previous_start: date,
    billing_table: str | None = None,
    account: str | None = None,
    context: GcloudContext | None = None,
    aggregate_runner: AggregateRunner | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    if not (config.workbench_project and config.workbench_location and config.workbench_instance and config.workbench_job_project):
        return {}, {
            "status": "unconfigured",
            "source": "bigquery_aggregate",
            "warnings": ["No hay una instancia Workbench completa configurada"],
            "provenance": "Workbench is the approved execution boundary for aggregate BigQuery metrics",
        }
    location = _bigquery_location(config.workbench_location)
    queries = build_aggregate_queries(
        projects,
        current_start=current_start,
        current_end=current_end,
        previous_start=previous_start,
        location=location,
        billing_table=billing_table if billing_table is not None else config.billing_export_table,
    )
    if aggregate_runner is None:
        from .workbench import execute_workbench_aggregate

        aggregate_runner = execute_workbench_aggregate
    settings = {
        "project": config.workbench_project,
        "location": config.workbench_location,
        "instance": config.workbench_instance,
        "job_project": config.workbench_job_project,
        "timeout_seconds": config.workbench_timeout_seconds,
    }
    try:
        raw = aggregate_runner(
            queries,
            settings=settings,
            account=account,
            gcloud_context=context,
            maximum_bytes_billed=config.policy_max_bytes,
        )
    except Exception as error:
        return {}, {
            "status": "unavailable",
            "source": "bigquery_aggregate",
            "warnings": [str(error)],
            "provenance": "Workbench aggregate execution",
        }
    if not isinstance(raw, dict):
        return {}, {
            "status": "unavailable",
            "source": "bigquery_aggregate",
            "warnings": ["Workbench no devolvió un objeto de agregados"],
            "provenance": "Workbench aggregate execution",
        }
    aggregate_raw = raw.get("aggregates")
    aggregate_data = aggregate_raw if isinstance(aggregate_raw, dict) else raw
    aggregates = {kind: _sanitize_aggregate_rows(rows) for kind, rows in aggregate_data.items() if kind in queries}
    warnings = [str(item)[:512] for item in raw.get("warnings", [])] if isinstance(raw.get("warnings", []), list) else []
    status_value = raw.get("statuses")
    statuses: dict[str, Any] = dict(status_value) if isinstance(status_value, dict) else {}
    failed = [kind for kind in queries if statuses.get(kind) not in {None, "ok"}]
    status = "ok" if not failed else "partial" if any(aggregates.values()) else "unavailable"
    return aggregates, {
        "status": status,
        "source": "bigquery_aggregate",
        "query_kinds": sorted(queries),
        "row_counts": {kind: len(rows) for kind, rows in aggregates.items()},
        "warnings": warnings + [f"{kind}: {statuses[kind]}" for kind in failed],
        "provenance": "Fixed aggregate INFORMATION_SCHEMA/Billing Export SQL executed in approved Workbench",
    }


def _bigquery_location(location: str) -> str:
    match = re.fullmatch(r"[a-z]+-[a-z]+\d+-[a-z]", location)
    return location.rsplit("-", 1)[0] if match else location


def _contextualize_resources(resources: list[dict[str, Any]], context: BusinessContext) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    field_counts = {field: 0 for field in RESOURCE_CONTEXT_FIELDS}
    for item in resources:
        current = dict(item)
        mapping = context.resolve(item["project"], item["resource_id"], item.get("labels"), item.get("tags"))
        # Labels/tags are input to resolution only.  Keep the resolved fields
        # and provenance, never the provider metadata map, in local evidence.
        current.pop("labels", None)
        current.pop("tags", None)
        current["context"] = mapping
        for field in RESOURCE_CONTEXT_FIELDS:
            if mapping.get(field):
                field_counts[field] += 1
        resolved.append(current)
    total = len(resources)
    coverage = {
        "resource_count": total,
        "fields": {
            field: round((count / total) * 100, 2) if total else 0.0
            for field, count in field_counts.items()
        },
        "complete_resource_pct": round(
            sum(all(item.get("context", {}).get(field) for field in RESOURCE_CONTEXT_FIELDS) for item in resources) / total * 100,
            2,
        ) if total else 0.0,
    }
    return resolved, coverage


def _resource_index(evidence: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["resource_id"]): item for item in evidence.get("resources", []) if isinstance(item, dict) and item.get("resource_id")}


def _finding_id(rule: str, key: str) -> str:
    return f"{rule}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}"


def _severity_for_recommendation(item: Mapping[str, Any]) -> str:
    recommender = str(item.get("recommender") or "").lower()
    if any(term in recommender for term in ("idle", "cost", "overprovisioned", "commitment")):
        return "medium"
    if "diagnosis" in recommender:
        return "high"
    return "low"


def _recommendation_category(item: Mapping[str, Any]) -> str:
    recommender = str(item.get("recommender") or "").lower()
    return "cost" if any(term in recommender for term in ("cost", "idle", "commitment", "overprovisioned", "bigquery.table")) else "reliability"


def generate_findings(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    resources = _resource_index(evidence)
    findings: list[dict[str, Any]] = []
    for item in evidence.get("recommendations", []):
        if not isinstance(item, dict):
            continue
        target = next((resources.get(resource_id) for resource_id in item.get("resource_ids", []) if resources.get(resource_id)), None)
        category = _recommendation_category(item)
        severity = _severity_for_recommendation(item)
        recommendation_id = str(item.get("recommendation_id"))
        title = item.get("description") or item.get("subtype") or "Recomendación de optimización GCP"
        findings.append({
            "id": _finding_id("provider-recommendation", recommendation_id),
            "rule": "provider_recommendation",
            "category": category,
            "severity": severity,
            "title": str(title)[:256],
            "executive_summary": "GCP reporta una oportunidad que debe validarse con el responsable antes de actuar.",
            "technical_detail": f"{item.get('recommender')} · {item.get('subtype') or 'sin subtipo'}",
            "project": item.get("project"),
            "resource": (target or {}).get("resource_id") or (item.get("resource_ids") or [None])[0],
            "context": (target or {}).get("context") or {},
            "impact": item.get("impact") or {},
            "source": "recommender",
            "evidence_refs": [f"recommender:{recommendation_id}"],
            "recommended_plan": [
                "Confirmar el propietario y la criticidad del recurso.",
                "Revisar la recomendación en GCP y estimar impacto operativo.",
                "Solicitar aprobación separada antes de cualquier cambio.",
            ],
            "confidence": "high" if item.get("description") else "medium",
            "assumptions": ["La recomendación sigue activa al momento de la consulta."],
            "limitations": ["No se suman ahorros entre recomendaciones ni se ejecutan mutaciones."],
            "action_mode": "plan_only",
        })
    for resource in evidence.get("resources", []):
        if not isinstance(resource, dict):
            continue
        missing = [field for field in RESOURCE_CONTEXT_FIELDS if not (resource.get("context") or {}).get(field)]
        if not missing:
            continue
        resource_id = str(resource.get("resource_id"))
        findings.append({
            "id": _finding_id("context-coverage", resource_id),
            "rule": "context_coverage",
            "category": "governance",
            "severity": "low",
            "title": "Recurso sin contexto empresarial completo",
            "executive_summary": f"Faltan {', '.join(missing)} para asignar esta oportunidad a un responsable.",
            "technical_detail": "Completa labels/tags o el mapa empresarial local antes de priorizar acciones.",
            "project": resource.get("project"),
            "resource": resource_id,
            "context": resource.get("context") or {},
            "impact": {"missing_fields": missing},
            "source": "cloud_asset_inventory",
            "evidence_refs": [f"asset:{resource_id}"],
            "recommended_plan": ["Definir owner, unidad, centro de costo y criticidad según corresponda."],
            "confidence": "high",
            "assumptions": [],
            "limitations": ["La ausencia de contexto no implica que el recurso esté sin propietario."],
            "action_mode": "plan_only",
        })
    trends = _cost_trends(evidence.get("aggregates", {}).get("billing", []))
    for trend in trends:
        delta = trend.get("delta_pct")
        severity = "medium" if isinstance(delta, (int, float)) and abs(delta) >= 20 else "low"
        project = str(trend.get("project_id") or "")
        delta_text = f"{delta:.1f}%" if isinstance(delta, (int, float)) else "sin porcentaje comparable"
        findings.append({
            "id": _finding_id("cost-trend", project),
            "rule": "cost_trend",
            "category": "cost",
            "severity": severity,
            "title": "Variación de costo entre periodos",
            "executive_summary": f"El costo agregado de {project} cambió {delta_text} frente al periodo anterior; es una señal para investigar, no una causalidad.",
            "technical_detail": "Comparación determinista de Billing Export por periodo, sin inferir la causa del cambio.",
            "project": project,
            "resource": None,
            "context": {},
            "impact": {"current_cost": trend.get("current_cost"), "previous_cost": trend.get("previous_cost"), "delta_pct": delta},
            "source": "billing_export",
            "evidence_refs": [f"billing:{project}:current", f"billing:{project}:previous"],
            "recommended_plan": ["Desagregar el cambio por servicio, centro de costo y responsable antes de proponer una acción."],
            "confidence": "medium",
            "assumptions": ["Billing Export está completo para ambos periodos."],
            "limitations": ["La señal no demuestra causalidad ni representa ahorro potencial."],
            "action_mode": "plan_only",
        })
    coverage = evidence.get("context_coverage") or {}
    if coverage.get("resource_count") and coverage.get("complete_resource_pct", 0) < 100:
        findings.append({
            "id": _finding_id("context-summary", str(coverage.get("resource_count"))),
            "rule": "context_summary",
            "category": "governance",
            "severity": "low",
            "title": "Cobertura de contexto empresarial incompleta",
            "executive_summary": "La asignación ejecutiva requiere mejorar la cobertura de contexto de los recursos.",
            "technical_detail": "La cobertura se calculó sobre los recursos resumidos por Cloud Asset Inventory.",
            "project": None,
            "resource": None,
            "context": {},
            "impact": coverage,
            "source": "business_context",
            "evidence_refs": ["context:coverage"],
            "recommended_plan": ["Acordar un contrato mínimo de labels/tags y responsables de actualización."],
            "confidence": "high",
            "assumptions": [],
            "limitations": [],
            "action_mode": "plan_only",
        })
    return sorted(findings, key=lambda item: (SEVERITY_ORDER.get(str(item.get("severity")), 9), str(item.get("id"))))


def _cost_trends(rows: Any) -> list[dict[str, Any]]:
    values: dict[str, dict[str, float]] = {}
    if not isinstance(rows, list):
        return []
    for row in rows:
        if not isinstance(row, dict) or row.get("project_id") is None or row.get("period") not in {"current", "previous"}:
            continue
        try:
            cost = float(row.get("cost") or 0)
        except (TypeError, ValueError):
            continue
        values.setdefault(str(row["project_id"]), {})[str(row["period"])] = cost
    result: list[dict[str, Any]] = []
    for project, periods in sorted(values.items()):
        if "current" not in periods or "previous" not in periods:
            continue
        previous = periods["previous"]
        delta = ((periods["current"] - previous) / previous * 100) if previous else None
        result.append({"project_id": project, "current_cost": periods["current"], "previous_cost": previous, "delta_pct": delta})
    return result


def build_report(evidence: Mapping[str, Any], findings: list[dict[str, Any]]) -> dict[str, Any]:
    sources = evidence.get("sources") or {}
    unavailable = [name for name, value in sources.items() if isinstance(value, dict) and value.get("status") in {"unavailable", "unconfigured"}]
    status = "complete" if not unavailable else "partial" if any(value.get("status") in {"ok", "partial"} for value in sources.values() if isinstance(value, dict)) else "failed"
    trends = _cost_trends(evidence.get("aggregates", {}).get("billing", []))
    metrics = [
        {"id": "projects", "label": "Proyectos evaluados", "value": len(evidence.get("projects", []))},
        {"id": "resources", "label": "Recursos resumidos", "value": len(evidence.get("resources", []))},
        {"id": "findings", "label": "Hallazgos accionables", "value": len(findings)},
        {"id": "context", "label": "Cobertura empresarial completa", "value": (evidence.get("context_coverage") or {}).get("complete_resource_pct", 0), "unit": "%"},
    ]
    if trends:
        metrics.append({"id": "cost_trends", "label": "Tendencias de costo comparables", "value": len(trends)})
    jobs_rows = evidence.get("aggregates", {}).get("jobs", [])
    if isinstance(jobs_rows, list) and jobs_rows:
        metrics.extend(
            [
                {"id": "bq_jobs", "label": "Jobs BigQuery en ventana", "value": int(sum(float(row.get("job_count") or 0) for row in jobs_rows if isinstance(row, dict)))},
                {"id": "bq_bytes_billed", "label": "Bytes facturados estimados", "value": int(sum(float(row.get("total_bytes_billed") or 0) for row in jobs_rows if isinstance(row, dict))), "unit": " bytes"},
                {"id": "bq_slot_ms", "label": "Slot-milisegundos", "value": int(sum(float(row.get("total_slot_ms") or 0) for row in jobs_rows if isinstance(row, dict)))},
            ]
        )
    storage_rows = evidence.get("aggregates", {}).get("storage", [])
    if isinstance(storage_rows, list) and storage_rows:
        metrics.extend(
            [
                {"id": "bq_tables", "label": "Tablas resumidas", "value": int(sum(float(row.get("table_count") or 0) for row in storage_rows if isinstance(row, dict)))},
                {"id": "bq_storage_bytes", "label": "Bytes físicos", "value": int(sum(float(row.get("total_physical_bytes") or 0) for row in storage_rows if isinstance(row, dict))), "unit": " bytes"},
            ]
        )
    verified = [
        f"{name}: {value.get('status')}"
        for name, value in sorted(sources.items())
        if isinstance(value, dict) and value.get("status") in {"ok", "partial"}
    ]
    limitations = [
        "El assessment es de solo lectura y no modifica recursos GCP.",
        "Las recomendaciones de proveedor no se suman como ahorro total.",
        "Las tendencias de costo describen una variación y no prueban causalidad.",
    ]
    limitations.extend(str(item) for name, value in sources.items() if isinstance(value, dict) for item in value.get("warnings", [])[:3])
    return {
        "schema_version": ASSESSMENT_SCHEMA_VERSION,
        "assessment_id": evidence.get("assessment_id"),
        "status": status,
        "executive_summary": (
            f"Se evaluaron {len(evidence.get('projects', []))} proyectos y se identificaron {len(findings)} señales "
            "que requieren validación con negocio, finanzas o plataforma."
        ),
        "key_metrics": metrics,
        "verified_signals": verified,
        "opportunities": [
            {
                "id": item["id"],
                "severity": item["severity"],
                "category": item["category"],
                "title": item["title"],
                "summary": item["executive_summary"],
                "project": item.get("project"),
            }
            for item in findings
        ],
        "coverage": {"sources": sources, "business_context": evidence.get("context_coverage") or {}},
        "governed_plan": [
            {"finding_id": item["id"], "mode": item["action_mode"], "steps": item["recommended_plan"]}
            for item in findings
        ],
        "technical_appendix": {
            "methodology": "Cloud Asset Inventory + Recommender + Workbench aggregate metrics, bounded to the configured project allowlist.",
            "findings": findings,
            "limitations": limitations,
            "source_status": sources,
        },
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        f"# Evaluación FinOps {report.get('assessment_id')}",
        "",
        f"**Estado:** {report.get('status')}  ",
        f"**Resumen:** {report.get('executive_summary')}",
        "",
        "## Lectura ejecutiva",
        "",
    ]
    for metric in report.get("key_metrics", []):
        lines.append(f"- **{metric.get('label')}:** {metric.get('value')}{metric.get('unit', '')}")
    lines.extend(["", "## Oportunidades", ""])
    if not report.get("opportunities"):
        lines.append("No se encontraron oportunidades con evidencia suficiente.")
    for item in report.get("opportunities", []):
        lines.append(f"### [{item.get('severity')}] {item.get('title')} ({item.get('id')})")
        lines.append(str(item.get("summary") or ""))
        lines.append("")
    lines.extend(["## Cobertura", "", "```json", json.dumps(report.get("coverage"), ensure_ascii=False, indent=2, sort_keys=True), "```", ""])
    lines.extend(["## Plan gobernado", ""])
    for item in report.get("governed_plan", []):
        lines.append(f"- **{item.get('finding_id')}** ({item.get('mode')}): " + " ".join(str(step) for step in item.get("steps", [])))
    lines.extend(["", "## Apéndice técnico", "", str((report.get("technical_appendix") or {}).get("methodology")), "", "### Limitaciones", ""])
    for limitation in (report.get("technical_appendix") or {}).get("limitations", []):
        lines.append(f"- {limitation}")
    return "\n".join(lines).rstrip() + "\n"


def render_html(report: Mapping[str, Any]) -> str:
    def esc(value: Any) -> str:
        return html.escape(str(value if value is not None else ""), quote=True)

    metric_html = "".join(
        f'<li><strong>{esc(item.get("label"))}</strong>: {esc(item.get("value"))}{esc(item.get("unit", ""))}</li>'
        for item in report.get("key_metrics", [])
    )
    opportunities = "".join(
        f'<article class="finding finding-{esc(item.get("severity"))}"><h3>{esc(item.get("title"))}</h3>'
        f'<p><strong>{esc(item.get("severity"))}</strong> · {esc(item.get("category"))} · {esc(item.get("id"))}</p>'
        f'<p>{esc(item.get("summary"))}</p></article>'
        for item in report.get("opportunities", [])
    ) or "<p>No se encontraron oportunidades con evidencia suficiente.</p>"
    plan = "".join(
        f'<li><strong>{esc(item.get("finding_id"))}</strong> ({esc(item.get("mode"))}): '
        f'{esc(" ".join(str(step) for step in item.get("steps", [])))}</li>'
        for item in report.get("governed_plan", [])
    )
    limitations = "".join(f"<li>{esc(value)}</li>" for value in (report.get("technical_appendix") or {}).get("limitations", []))
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Evaluación FinOps {esc(report.get('assessment_id'))}</title>
<style>body{{font-family:system-ui,sans-serif;line-height:1.5;max-width:1100px;margin:0 auto;padding:2rem;color:#172033;background:#f6f8fb}}main{{background:#fff;padding:2rem;border-radius:12px;box-shadow:0 2px 12px #17203320}}.finding{{border-left:5px solid #4965a8;padding:.5rem 1rem;margin:1rem 0;background:#f6f8fb}}.finding-high{{border-color:#b33a3a}}.finding-medium{{border-color:#bc7a00}}.finding-low{{border-color:#4965a8}}a{{color:#154c91}}@media(prefers-color-scheme:dark){{body{{background:#111827;color:#e5e7eb}}main,.finding{{background:#1f2937}}}}</style></head>
<body><a href="#contenido">Saltar al contenido</a><main id="contenido"><header><h1>Evaluación FinOps</h1><p><strong>{esc(report.get('assessment_id'))}</strong> · {esc(report.get('status'))}</p><p>{esc(report.get('executive_summary'))}</p></header>
<section aria-labelledby="metricas"><h2 id="metricas">Métricas clave</h2><ul>{metric_html}</ul></section>
<section aria-labelledby="oportunidades"><h2 id="oportunidades">Oportunidades</h2>{opportunities}</section>
<section aria-labelledby="plan"><h2 id="plan">Plan gobernado</h2><ol>{plan}</ol></section>
<section aria-labelledby="limitaciones"><h2 id="limitaciones">Limitaciones</h2><ul>{limitations}</ul></section>
</main></body></html>
"""


def _write_json(path: Path, value: Any) -> None:
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _artifact_hashes(directory: Path) -> dict[str, str]:
    names = ("evidence.json", "findings.json", "report.json", "report.md", "report.html")
    return {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in names}


def verify_assessment(directory: Path) -> dict[str, Any]:
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FinOpsError(f"Assessment inválido: no se pudo leer manifest.json: {error}") from error
    if manifest.get("schema_version") != ASSESSMENT_SCHEMA_VERSION:
        raise FinOpsError("schema_version de assessment no soportado")
    expected_hashes = manifest.get("artifact_hashes") or {}
    actual_hashes = _artifact_hashes(directory)
    if expected_hashes != actual_hashes:
        raise FinOpsError("La integridad del assessment no coincide con manifest.json")
    digest = _sha256(actual_hashes)
    if digest != manifest.get("assessment_digest"):
        raise FinOpsError("El digest del assessment no coincide")
    return manifest


def _assessment_directory(value: str | Path, output_root: Path | None = None) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_dir():
        return candidate
    root = (output_root or DEFAULT_OUTPUT_ROOT).expanduser()
    directory = root / str(value)
    if not directory.is_dir():
        raise FinOpsError(f"No existe el assessment: {value}")
    return directory


def load_assessment(value: str | Path, *, output_root: Path | None = None) -> dict[str, Any]:
    directory = _assessment_directory(value, output_root)
    manifest = verify_assessment(directory)
    try:
        report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FinOpsError(f"No se pudo leer report.json: {error}") from error
    return {"directory": str(directory), "manifest": manifest, "report": report}


def run_assessment(
    config: QueryflowConfig,
    *,
    account: str | None = None,
    projects: Iterable[str] | None = None,
    window_days: int | None = None,
    billing_table: str | None = None,
    business_context_path: Path | None = None,
    output_root: Path | None = None,
    runner: JsonRunner | None = None,
    aggregate_runner: AggregateRunner | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    selected = resolve_projects(config, projects)
    days = int(window_days or config.finops_window_days)
    if days < 1 or days > 365:
        raise FinOpsError("window_days debe estar entre 1 y 365")
    timestamp = now.astimezone(timezone.utc) if now else _now()
    current_end = timestamp.date()
    current_start = current_end - timedelta(days=days)
    previous_start = current_start - timedelta(days=days)
    configured_context = business_context_path or (Path(config.business_context_path).expanduser() if config.business_context_path else None)
    business_context = load_business_context(configured_context)
    chosen_billing = billing_table or config.billing_export_table
    if chosen_billing:
        _parse_table(chosen_billing)
    context = GcloudContext(config.gcloud_config_dir, account=account or config.account)
    resources, asset_status = collect_asset_inventory(selected, context, runner=runner)
    resources, coverage = _contextualize_resources(resources, business_context)
    locations = config.allowed_locations or ("global",)
    recommendations, recommendation_status = collect_recommendations(selected, context, locations=locations, runner=runner)
    aggregates, aggregate_status = collect_aggregates(
        selected,
        config,
        current_start=current_start,
        current_end=current_end,
        previous_start=previous_start,
        billing_table=chosen_billing,
        account=account,
        context=context,
        aggregate_runner=aggregate_runner,
    )
    sources = {
        "asset_inventory": {"collected_at": _iso(timestamp), **asset_status},
        "recommender": {"collected_at": _iso(timestamp), **recommendation_status},
        "bigquery_aggregate": {"collected_at": _iso(timestamp), **aggregate_status},
    }
    if chosen_billing and aggregate_status.get("status") in {"ok", "partial"}:
        sources["billing_export"] = {
            "collected_at": _iso(timestamp),
            "status": "ok" if aggregates.get("billing") else "unavailable",
            "source": "billing_export",
            "table_configured": True,
            "row_count": len(aggregates.get("billing", [])),
            "warnings": [] if aggregates.get("billing") else ["No se recibieron agregados de Billing Export"],
            "provenance": "User-configured Billing Export table through Workbench aggregate query",
        }
    else:
        sources["billing_export"] = {
            "collected_at": _iso(timestamp),
            "status": "unconfigured",
            "source": "billing_export",
            "table_configured": False,
            "warnings": ["Billing Export es opcional y no está configurado"],
            "provenance": "No billing rows are assumed when the source is absent",
        }
    evidence = {
        "schema_version": ASSESSMENT_SCHEMA_VERSION,
        "assessment_id": "pending",
        "generated_at": _iso(timestamp),
        "identity": {"configured": bool(account or config.account)},
        "projects": list(selected),
        "window": {
            "days": days,
            "current_start": current_start.isoformat(),
            "current_end": current_end.isoformat(),
            "previous_start": previous_start.isoformat(),
            "previous_end": current_start.isoformat(),
        },
        "sources": sources,
        "resources": resources,
        "recommendations": recommendations,
        "aggregates": aggregates,
        "business_context": {"configured": business_context.fingerprint is not None, "fingerprint": business_context.fingerprint},
        "context_coverage": coverage,
    }
    evidence["assessment_id"] = "finops-" + timestamp.strftime("%Y%m%dT%H%M%SZ") + "-" + _sha256(evidence)[:10]
    findings = generate_findings(evidence)
    report = build_report(evidence, findings)
    root = (output_root or DEFAULT_OUTPUT_ROOT).expanduser()
    directory = root / str(evidence["assessment_id"])
    directory.mkdir(parents=True, exist_ok=False)
    _write_json(directory / "evidence.json", evidence)
    _write_json(directory / "findings.json", {"schema_version": 1, "assessment_id": evidence["assessment_id"], "findings": findings})
    _write_json(directory / "report.json", report)
    (directory / "report.md").write_text(render_markdown(report), encoding="utf-8")
    (directory / "report.html").write_text(render_html(report), encoding="utf-8")
    artifact_hashes = _artifact_hashes(directory)
    status = report["status"]
    manifest = {
        "schema_version": ASSESSMENT_SCHEMA_VERSION,
        "assessment_id": evidence["assessment_id"],
        "kind": "finops_health",
        "generated_at": evidence["generated_at"],
        "identity": evidence["identity"],
        "projects": selected,
        "window": evidence["window"],
        "source_status": {name: value.get("status") for name, value in sources.items()},
        "business_context_fingerprint": business_context.fingerprint,
        "status": status,
        "artifact_hashes": artifact_hashes,
        "assessment_digest": _sha256(artifact_hashes),
        "action_mode": "plan_only",
    }
    _write_json(directory / "manifest.json", manifest)
    return {"directory": str(directory), "manifest": manifest, "report": report}


def serve_assessment(value: str | Path, *, output_root: Path | None = None, port: int = 8080) -> tuple[ThreadingHTTPServer, str]:
    loaded = load_assessment(value, output_root=output_root)
    directory = Path(str(loaded["directory"])).resolve()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path not in {"/", "/index.html", "/report.html"}:
                self.send_error(404)
                return
            body = (directory / "report.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            self.send_error(405)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", int(port)), Handler)
    return server, f"http://127.0.0.1:{server.server_address[1]}/"


__all__ = [
    "ALLOWED_ASSET_TYPES",
    "ASSESSMENT_SCHEMA_VERSION",
    "BusinessContext",
    "FinOpsError",
    "RECOMMENDER_IDS",
    "build_aggregate_queries",
    "build_report",
    "collect_asset_inventory",
    "collect_recommendations",
    "generate_findings",
    "load_assessment",
    "load_business_context",
    "render_html",
    "render_markdown",
    "resolve_projects",
    "run_assessment",
    "serve_assessment",
    "verify_assessment",
]
