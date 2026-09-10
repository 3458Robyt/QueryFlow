#!/usr/bin/env python3
"""Consolidate QueryFlow migration campaigns into one analyst guide.

The main campaign report already contains the query/notebook metadata and its
embedded diff payload.  Routine migration reports use a different schema and
keep the before/after body in ``review``/``proposal``.  This script normalizes
those routine records, creates bounded red/green diffs, and regenerates the
same self-contained dark HTML surface without exposing complete code bodies in
the JSON/Markdown companion reports.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import hashlib
import html
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from queryflow.migration_report import (  # noqa: E402
    _as_int,
    _diff_file_payload,
    _html_resource_id,
    _portable_report,
    render_detailed_html,
    render_detailed_markdown,
)


COMPLETED_STATUSES = frozenset({"published", "published_verified", "already_present_identical"})
STATUS_LABELS = {
    "published": "Publicado",
    "published_verified": "Publicado y verificado",
    "already_present_identical": "Ya existía · idéntico",
    "pending": "Pendiente de revisión",
    "blocked": "Bloqueado",
    "failed": "Falló la publicación",
    "destination_conflict": "Conflicto de destino",
}


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"No se pudo leer JSON: {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"El JSON debe ser un objeto: {path}")
    return value


def load_html_payload(path: Path) -> dict[str, Any]:
    """Extract the self-contained payload from an existing guide HTML."""
    path = Path(path).expanduser()
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"No se pudo leer HTML: {path}: {error}") from error
    marker = '<script type="application/json" id="queryflow-payload">'
    start = source.find(marker)
    if start < 0:
        raise ValueError(f"El HTML no contiene el payload QueryFlow: {path}")
    start += len(marker)
    end = source.find("</script>", start)
    if end < 0:
        raise ValueError(f"El payload QueryFlow no está cerrado: {path}")
    try:
        payload = json.loads(source[start:end])
    except json.JSONDecodeError as error:
        raise ValueError(f"El payload QueryFlow no es JSON válido: {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"El payload QueryFlow debe ser un objeto: {path}")
    if not isinstance(payload.get("diff_index"), Mapping):
        raise ValueError(f"El payload no contiene un diff_index: {path}")
    return payload


def _safe_incident(value: Any, *, blocker: bool = False) -> dict[str, Any]:
    """Keep incident coordinates and identifiers, never code fragments."""
    if not isinstance(value, Mapping):
        return {"kind": "unknown"}
    keys = (
        "kind", "route", "path", "cell", "line", "start", "end", "rule",
        "reason", "resolution", "dataset", "project", "routine_id", "source_name",
    )
    result = {key: value[key] for key in keys if key in value and value[key] is not None}
    if blocker and "kind" not in result:
        result["kind"] = "blocker"
    return result


def _route_rows(rewrite: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for route in rewrite.get("applied_routes") or []:
        if not isinstance(route, Mapping):
            continue
        rows.append(
            {
                "mapping_id": str(route.get("mapping_id") or ""),
                "old": str(route.get("old") or ""),
                "new": str(route.get("new") or ""),
                "occurrences": _as_int(route.get("occurrences")),
            }
        )
    return rows


def _unknown_rows(rewrite: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [_safe_incident(value) for value in (rewrite.get("unknown_routes") or []) if isinstance(value, Mapping)]


def _routine_kind(role: str) -> str:
    return "procedure" if role == "procedure" else "function"


def _routine_status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status or "Sin estado")


def normalize_routine_resource(item: Mapping[str, Any], *, campaign_id: str) -> dict[str, Any]:
    """Convert one routine report record to the analyst report schema.

    The two private body fields are used only while building the HTML diff and
    are removed by :func:`build_consolidated_report` before serialization.
    """
    source = item.get("source") or {}
    destination = item.get("destination") or {}
    rewrite = item.get("rewrite") or {}
    classification = item.get("classification") or {}
    review = item.get("review") or {}
    security = item.get("security") or {}
    receipt = item.get("receipt") or {}
    role = str(item.get("role") or "procedure")
    status = str(item.get("status") or receipt.get("status") or "unknown")
    routine_id = str(source.get("routine_id") or destination.get("routine_id") or "")
    lot_number = _as_int(item.get("lot"), 0)
    lot = f"rutinas-r{lot_number:02d}" if lot_number else "rutinas"
    before = str(review.get("before_definition_body") or "")
    after = str((item.get("proposal") or {}).get("definitionBody") or "")
    route_replacements = _route_rows(rewrite)
    unmatched_routes = _unknown_rows(rewrite)
    warnings = [_safe_incident(value) for value in (item.get("warnings") or []) if isinstance(value, Mapping)]
    blockers = [_safe_incident(value, blocker=True) for value in (item.get("blockers") or []) if isinstance(value, Mapping)]
    language = str((item.get("proposal") or {}).get("language") or source.get("language") or classification.get("language") or "SQL")
    routine_type = str((item.get("proposal") or {}).get("routineType") or source.get("routine_type") or classification.get("routine_type") or "")
    source_name = str(source.get("name") or "")
    destination_name = str(destination.get("name") or "")
    runtime_error = str(receipt.get("message") or "") if status == "failed" else ""
    dependency_values = [value for value in (item.get("dependencies") or []) if isinstance(value, Mapping)]
    missing_dependencies = [
        _safe_incident(value)
        for value in dependency_values
        if str(value.get("status") or "") not in {"", "available", "present", "published"}
    ]
    resource = {
        "lot": lot,
        "ordinal": _as_int(item.get("ordinal")),
        "kind": _routine_kind(role),
        "display_name": routine_id or destination_name.rsplit("/", 1)[-1],
        "source_name": source_name,
        "source_project": str(source.get("project") or ""),
        "source_location": str(source.get("location") or ""),
        "source_filename": "definitionBody",
        "source_head_commit": "",
        "source_content_sha256": str(source.get("source_sha256") or ""),
        "destination_project": str(destination.get("project") or ""),
        "destination_location": str(destination.get("location") or ""),
        # Routines are stored in a BigQuery dataset rather than a Dataform
        # repository.  Keep the dataset visible in the same destination field.
        "destination_repository_id": str(destination.get("dataset") or ""),
        "destination_display_name": str(destination.get("routine_id") or routine_id),
        "destination_repository": destination_name,
        "destination_collision": bool(destination.get("collision")),
        "status": status,
        "status_label": _routine_status_label(status),
        "sealed_copy": bool(security.get("sealed")),
        "redacted_review": bool(security.get("sealed")),
        "security": {
            "sealed": bool(security.get("sealed")),
            "secret_handling": str(security.get("handling") or ""),
            "finding_count": len(security.get("findings") or []),
        },
        "migration_eligible": bool(item.get("migration_eligible")),
        "execution_eligible": bool(item.get("execution_eligible")),
        "classification": {
            "statement_class": _routine_kind(role),
            "read_only": not bool(classification.get("mutating_sql")),
            "references": [],
            "errors": [],
            "warnings": [str(value.get("kind") or "") for value in warnings if value.get("kind")],
            "dynamic_cells": [],
        },
        "review": {
            "required": bool(review.get("required")),
            "reasons": [str(value) for value in (review.get("reasons") or [])],
            "policy": str(review.get("policy") or ""),
        },
        "changed_files": ["definitionBody"] if bool(rewrite.get("changed")) else [],
        "route_replacements": route_replacements,
        "route_replacement_group_count": len(route_replacements),
        "route_replacement_occurrence_count": sum(value["occurrences"] for value in route_replacements),
        "unmatched_routes": unmatched_routes,
        "unmatched_route_count": len(unmatched_routes),
        "dynamic_cells": [],
        "warnings": warnings,
        "blockers": blockers,
        "runtime_error": runtime_error,
        "publication_message": str(receipt.get("message") or ""),
        "before_sha256": str(rewrite.get("before_sha256") or ""),
        "proposed_sha256": str(rewrite.get("proposed_sha256") or ""),
        "sql_executed": False,
        "published_commit_sha": "",
        "published_filename": "definitionBody",
        "audit": {
            "campaign_id": campaign_id,
            "publication_digest": "",
            "receipt_status": status,
        },
        "task_relative": "",
        "review_relative": "",
        "role": role,
        "routine_type": routine_type,
        "language": language,
        "dependency_count": len(dependency_values),
        "missing_dependency_count": len(missing_dependencies),
        "missing_dependencies": missing_dependencies,
        # These fields are deliberately private and stripped before rendering.
        "_definition_before": before,
        "_definition_after": after,
    }
    resource["resource_id"] = _html_resource_id(resource)
    return resource


def _line_delta(before: str, after: str) -> tuple[int, int]:
    old = before.splitlines()
    new = after.splitlines()
    added = removed = 0
    for tag, old_start, old_end, new_start, new_end in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag in {"insert", "replace"}:
            added += new_end - new_start
        if tag in {"delete", "replace"}:
            removed += old_end - old_start
    return added, removed


def build_routine_diff_index(resources: Sequence[Mapping[str, Any]], *, context_lines: int = 3) -> dict[str, Any]:
    """Build compact red/green diffs from normalized routine resources."""
    if int(context_lines) < 0 or int(context_lines) > 20:
        raise ValueError("context_lines debe estar entre 0 y 20")
    result: dict[str, dict[str, Any]] = {}
    changed_file_count = 0
    diff_row_count = 0
    for item in resources:
        resource_id = str(item.get("resource_id") or _html_resource_id(item))
        if resource_id in result:
            raise ValueError(f"Identificador duplicado para rutina: {item.get('display_name') or resource_id}")
        before = str(item.get("_definition_before") or "")
        after = str(item.get("_definition_after") or "")
        expected_before = str(item.get("before_sha256") or "")
        expected_after = str(item.get("proposed_sha256") or "")
        if not expected_before or not expected_after:
            result[resource_id] = {"resource_id": resource_id, "hash_status": "not_prepared", "files": []}
            continue
        actual_before = hashlib.sha256(before.encode("utf-8")).hexdigest()
        actual_after = hashlib.sha256(after.encode("utf-8")).hexdigest()
        if actual_before != expected_before or actual_after != expected_after:
            raise ValueError(f"El hash de definitionBody no coincide para {item.get('display_name') or resource_id}")
        added, removed = _line_delta(before, after)
        file_payload = _diff_file_payload(
            {
                "path": "definitionBody",
                "label": "definitionBody",
                "language": item.get("language") or "SQL",
                "before": before,
                "after": after,
                "added": added,
                "removed": removed,
                "changed": before != after,
            },
            context_lines=int(context_lines),
        )
        if file_payload["changed"]:
            changed_file_count += 1
        diff_row_count += sum(len(hunk.get("rows") or []) for hunk in file_payload.get("hunks") or [])
        result[resource_id] = {
            "resource_id": resource_id,
            "hash_status": "match",
            "observed_before_sha256": actual_before,
            "observed_after_sha256": actual_after,
            "files": [file_payload],
        }
    return {
        "schema_version": 1,
        "context_lines": int(context_lines),
        "resource_count": len(result),
        "changed_file_count": changed_file_count,
        "diff_row_count": diff_row_count,
        "resources": result,
    }


def _without_private_fields(item: Mapping[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(value) for key, value in item.items() if not str(key).startswith("_")}


def _resource_key(item: Mapping[str, Any]) -> tuple[str, str]:
    return str(item.get("kind") or ""), str(item.get("source_name") or item.get("display_name") or "")


def _aggregate_route_replacements(resources: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in resources:
        resource_key = f"{item.get('lot')}/{_as_int(item.get('ordinal')):02d} {item.get('display_name') or item.get('source_name') or ''}"
        for route in item.get("route_replacements") or []:
            key = (str(route.get("mapping_id") or ""), str(route.get("old") or ""), str(route.get("new") or ""))
            group = groups.setdefault(key, {"mapping_id": key[0], "old": key[1], "new": key[2], "occurrences": 0, "resources": []})
            group["occurrences"] += _as_int(route.get("occurrences"))
            if resource_key not in group["resources"]:
                group["resources"].append(resource_key)
    return sorted(groups.values(), key=lambda value: (value["mapping_id"], value["old"], value["new"]))


def _aggregate_unmatched(resources: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in resources:
        for route in item.get("unmatched_routes") or []:
            result.append(
                {
                    "lot": item.get("lot"),
                    "ordinal": item.get("ordinal"),
                    "resource": item.get("display_name") or item.get("source_name"),
                    **dict(route),
                }
            )
    return result


def _routine_lots(resources: Sequence[Mapping[str, Any]], routine_report: Mapping[str, Any]) -> list[dict[str, Any]]:
    by_lot: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in resources:
        by_lot[str(item.get("lot") or "rutinas")].append(item)
    raw_lots = {f"rutinas-r{_as_int(value.get('lot')):02d}": value for value in (routine_report.get("lots") or []) if isinstance(value, Mapping)}
    digest = str(routine_report.get("publication_digest") or "")
    result: list[dict[str, Any]] = []
    for lot, values in sorted(by_lot.items()):
        statuses = Counter(str(value.get("status") or "unknown") for value in values)
        completed = sum(str(value.get("status") or "") in COMPLETED_STATUSES for value in values)
        raw = raw_lots.get(lot) or {}
        result.append(
            {
                "lot": lot,
                "campaign_id": f"{routine_report.get('campaign_id') or 'routines'}-{lot.removeprefix('rutinas-')}",
                "status": str(raw.get("status") or ("ready" if completed == len(values) else "blocked")),
                "resource_count": len(values),
                "published_count": completed,
                "pending_count": len(values) - completed,
                "warning_count": sum(len(value.get("warnings") or []) for value in values),
                "publication_digest": digest,
                "status_counts": dict(sorted(statuses.items())),
            }
        )
    return result


def _excluded_routines(routine_report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Project routine inventory entries that were not part of the campaign."""
    result: list[dict[str, Any]] = []
    for reason, values in (
        ("not_selected", routine_report.get("not_selected") or []),
        ("unsupported", routine_report.get("unsupported") or []),
    ):
        for value in values:
            if not isinstance(value, Mapping):
                continue
            result.append(
                {
                    "status": reason,
                    "reason": (
                        "Dependencia soportada pero no referenciada en la campaña"
                        if reason == "not_selected"
                        else "Rutina no soportada por la API de inventario"
                    ),
                    "routine_id": str(value.get("routine_id") or ""),
                    "routine_type": str(value.get("routine_type") or ""),
                    "language": str(value.get("language") or ""),
                    "dataset": str(value.get("dataset") or ""),
                    "source_name": str(value.get("name") or ""),
                    "source_project": str(value.get("project") or ""),
                    "location": str(value.get("location") or ""),
                }
            )
    return result


def build_consolidated_report(base_report: Mapping[str, Any], routine_report: Mapping[str, Any]) -> dict[str, Any]:
    """Merge the final query/notebook campaign with the routine campaign."""
    result = copy.deepcopy(dict(base_report))
    base_resources = [dict(value) for value in (base_report.get("resources") or []) if isinstance(value, Mapping)]
    routine_resources = [normalize_routine_resource(value, campaign_id=str(routine_report.get("campaign_id") or "routines")) for value in (routine_report.get("resources") or []) if isinstance(value, Mapping)]
    public_routines = [_without_private_fields(value) for value in routine_resources]
    resources = base_resources + public_routines
    keys = [_resource_key(value) for value in resources]
    if len(keys) != len(set(keys)):
        duplicates = [key for key, count in Counter(keys).items() if count > 1]
        raise ValueError(f"La guía contiene recursos duplicados: {duplicates[:3]}")
    statuses = Counter(str(value.get("status") or "unknown") for value in resources)
    completed_count = sum(status in COMPLETED_STATUSES for status in statuses.elements())
    unmatched = _aggregate_unmatched(resources)
    routes = _aggregate_route_replacements(resources)
    warning_counts = Counter(str(warning.get("kind") or "unknown") for value in resources for warning in (value.get("warnings") or []) if isinstance(warning, Mapping))
    reason_counts = Counter(str(reason) for value in resources for reason in ((value.get("review") or {}).get("reasons") or []))
    class_counts = Counter(str((value.get("classification") or {}).get("statement_class") or "not_evaluated") for value in resources)
    kind_counts = Counter(str(value.get("kind") or "unknown") for value in resources)
    base_campaign_id = str(base_report.get("campaign_id") or "query-notebooks")
    routine_campaign_id = str(routine_report.get("campaign_id") or "routines")
    excluded_resources = _excluded_routines(routine_report)
    result.update(
        {
            "schema_version": 1,
            "campaign_id": "queryflow-migration-guide-consolidated",
            "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z"),
            "resource_count": len(resources),
            "completed_count": completed_count,
            "published_count": completed_count,
            "pending_count": len(resources) - completed_count,
            "status_counts": dict(sorted(statuses.items())),
            "kind_counts": dict(sorted(kind_counts.items())),
            "classification_counts": dict(sorted(class_counts.items())),
            "warning_counts": dict(sorted(warning_counts.items())),
            "review_reason_counts": dict(sorted(reason_counts.items())),
            "review_required_count": sum(bool((value.get("review") or {}).get("required")) for value in resources),
            "execution_eligible_count": sum(bool(value.get("execution_eligible")) for value in resources),
            "sql_executed": any(bool(value.get("sql_executed")) for value in resources),
            "changed_file_count": sum(len(value.get("changed_files") or []) for value in resources),
            "route_replacement_group_count": sum(_as_int(value.get("route_replacement_group_count")) for value in resources),
            "route_replacement_occurrence_count": sum(_as_int(value.get("route_replacement_occurrence_count")) for value in resources),
            "unique_route_replacement_group_count": len(routes),
            "unmatched_route_count": len(unmatched),
            "unique_unmatched_route_count": len({str(value.get("route") or "") for value in unmatched}),
            "pending_resources": [str(value.get("display_name") or value.get("source_name") or "") for value in resources if str(value.get("status") or "") not in COMPLETED_STATUSES],
            "route_replacements": routes,
            "unmatched_routes": unmatched,
            "resources": resources,
            "lots": list(base_report.get("lots") or []) + _routine_lots(public_routines, routine_report),
            "sources": [
                {
                    "campaign_id": base_campaign_id,
                    "label": "Querys y notebooks",
                    "resource_count": len(base_resources),
                    "status_counts": dict(sorted(Counter(str(value.get("status") or "unknown") for value in base_resources).items())),
                },
                {
                    "campaign_id": routine_campaign_id,
                    "label": "Procedimientos y funciones",
                    "resource_count": len(public_routines),
                    "status_counts": dict(sorted(Counter(str(value.get("status") or "unknown") for value in public_routines).items())),
                    "publication_digest": str(routine_report.get("publication_digest") or ""),
                },
            ],
            "scope_description": "Guía consolidada para revisión humana. Incluye la campaña final de querys/notebooks y la migración posterior de procedimientos y funciones; no ejecuta SQL.",
            "path_policy": "Guía autocontenida: se omitieron rutas locales y cuerpos completos fuera de los hunks del diff.",
            "excluded_resource_count": len(excluded_resources),
            "excluded_resources": excluded_resources,
        }
    )
    return result


def merge_diff_indexes(base_index: Mapping[str, Any], routine_index: Mapping[str, Any]) -> dict[str, Any]:
    resources: dict[str, Any] = {}
    for index in (base_index, routine_index):
        for resource_id, value in (index.get("resources") or {}).items():
            if resource_id in resources:
                raise ValueError(f"Identificador duplicado en diff consolidado: {resource_id}")
            resources[str(resource_id)] = value
    return {
        "schema_version": 1,
        "context_lines": max(_as_int(base_index.get("context_lines"), 3), _as_int(routine_index.get("context_lines"), 3)),
        "resource_count": len(resources),
        "changed_file_count": _as_int(base_index.get("changed_file_count")) + _as_int(routine_index.get("changed_file_count")),
        "diff_row_count": _as_int(base_index.get("diff_row_count")) + _as_int(routine_index.get("diff_row_count")),
        "resources": resources,
    }


def _report_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return report metadata from a previously generated self-contained HTML."""
    report = copy.deepcopy(dict(payload))
    report.pop("diff_index", None)
    return report


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-html", type=Path, required=True)
    parser.add_argument("--base-report", type=Path)
    parser.add_argument("--routine-report", type=Path, action="append", required=True)
    parser.add_argument("--output-html", type=Path)
    parser.add_argument("--output-report", type=Path)
    parser.add_argument("--output-markdown", type=Path)
    parser.add_argument("--context-lines", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    base_html = args.base_html.expanduser().resolve()
    base_report_path = (args.base_report or base_html.with_suffix(".json")).expanduser().resolve()
    output_html = (args.output_html or base_html).expanduser().resolve()
    output_report = (args.output_report or base_html.with_name("campaign-detail-report-consolidated.json")).expanduser().resolve()
    output_markdown = (args.output_markdown or base_html.with_name("campaign-detail-report-consolidated.md")).expanduser().resolve()

    payload = load_html_payload(base_html)
    base_report = _load_json(base_report_path)
    # A previous run may have replaced the base HTML in place while its
    # original JSON companion is still supplied on the command line.  Prefer
    # the richer payload in that case and avoid appending the same routines a
    # second time.
    payload_report = _report_from_payload(payload)
    if _as_int(payload_report.get("resource_count")) > _as_int(base_report.get("resource_count")):
        base_report = payload_report
    combined = base_report
    combined_diff = payload["diff_index"]
    for routine_path in args.routine_report:
        routine_report = _load_json(Path(routine_path).expanduser().resolve())
        raw_resources = [value for value in (routine_report.get("resources") or []) if isinstance(value, Mapping)]
        existing_keys = {_resource_key(value) for value in (combined.get("resources") or []) if isinstance(value, Mapping)}
        missing_raw_resources = [
            value
            for value in raw_resources
            if _resource_key(normalize_routine_resource(value, campaign_id=str(routine_report.get("campaign_id") or "routines"))) not in existing_keys
        ]
        if not missing_raw_resources:
            continue
        routine_report = copy.deepcopy(routine_report)
        routine_report["resources"] = missing_raw_resources
        normalized = [normalize_routine_resource(value, campaign_id=str(routine_report.get("campaign_id") or "routines")) for value in missing_raw_resources]
        routine_diff = build_routine_diff_index(normalized, context_lines=args.context_lines)
        combined = build_consolidated_report(combined, routine_report)
        combined_diff = merge_diff_indexes(combined_diff, routine_diff)

    # Keep only public fields in the final serialized report.  The HTML
    # renderer includes the diff payload but never receives full routine bodies.
    portable = _portable_report(combined)
    _write_text(output_report, json.dumps(portable, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    _write_text(output_markdown, render_detailed_markdown(portable) + "\n")
    _write_text(output_html, render_detailed_html(combined, combined_diff) + "\n")
    summary = {
        "ok": True,
        "html": str(output_html),
        "report": str(output_report),
        "markdown": str(output_markdown),
        "resource_count": combined["resource_count"],
        "completed_count": combined.get("completed_count", combined.get("published_count", 0)),
        "pending_count": combined["pending_count"],
        "status_counts": combined["status_counts"],
        "route_replacement_occurrence_count": combined["route_replacement_occurrence_count"],
        "unmatched_route_count": combined["unmatched_route_count"],
        "diff_resource_count": combined_diff["resource_count"],
        "diff_changed_file_count": combined_diff["changed_file_count"],
        "diff_row_count": combined_diff["diff_row_count"],
        "html_bytes": output_html.stat().st_size,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"Guía HTML consolidada: {output_html}")
        print(f"Reporte JSON: {output_report}")
        print(f"Reporte Markdown: {output_markdown}")
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
