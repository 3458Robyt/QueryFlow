"""Explicit batch migrations for Shared Queries and notebooks.

The batch workflow is deliberately separate from the historical 10+10 pilot.
It resolves a human-maintained selection against the canonical catalog, exports
each source asset, rewrites only code, statically classifies the proposal for
human review, and records a single immutable digest. Copy operations create a
new repository; update operations bind to the current destination repository
and commit only the route rewrite. No SQL (including a BigQuery dry-run) is
executed by this module.
"""

from __future__ import annotations

import copy
import hashlib
import html
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from .catalog import ResourceRef
from .migration import RouteDictionary, UnknownRoute, rewrite_text
from .notebooks import analyze_sql_fragments
from .validation import validate_sql_fragments, validate_sql_text


BATCH_SCHEMA_VERSION = 1
BATCH_KINDS = ("shared_query", "notebook")
BATCH_SOURCE_PROJECT = "SOURCE_PROJECT"
BATCH_DESTINATION_PROJECT = "DESTINATION_PROJECT"
BATCH_LOCATION = "us-east1"
BATCH_REQUESTS_PER_MINUTE = 180
BATCH_QUOTA_REQUESTS_PER_MINUTE = 300
REVIEW_WARNING_KINDS = frozenset({"unknown_route", "dynamic_sql", "mutating", "unknown"})
BATCH_PENDING_STATUSES = frozenset({"pending"})
BATCH_TERMINAL_STATUSES = frozenset({"published", "already_present", "already_compliant"})
SECRET_HANDLING_MODES = frozenset({"block", "sealed_copy"})


class BatchError(RuntimeError):
    """The explicit batch cannot proceed safely."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalize_display_name(value: str) -> str:
    """Normalize catalog display names for matching, not for publication."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.strip().strip("*").strip()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.casefold().split())


@dataclass(frozen=True)
class BatchResourceSpec:
    kind: str
    display_name: str
    name: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "BatchResourceSpec":
        if not isinstance(raw, Mapping):
            raise BatchError("Cada recurso de la selección debe ser un objeto")
        kind = str(raw.get("kind") or "").strip()
        raw_name = str(raw.get("name") or "").strip()
        display_name = str(raw.get("display_name") or raw_name or "").strip()
        canonical_name = str(
            raw.get("canonical_name") or raw.get("resource_name") or (raw_name if raw.get("display_name") else "")
        ).strip()
        if kind not in BATCH_KINDS:
            raise BatchError(f"Tipo de recurso no permitido en el lote: {kind or '<vacío>'}")
        if not display_name:
            raise BatchError("Cada recurso de la selección debe tener display_name")
        return cls(kind, display_name, canonical_name)

    def to_dict(self) -> dict[str, str]:
        value = {"kind": self.kind, "display_name": self.display_name}
        if self.name:
            value["name"] = self.name
        return value

    @property
    def key(self) -> tuple[str, str]:
        return self.kind, self.name or normalize_display_name(self.display_name)


@dataclass(frozen=True)
class BatchSelection:
    campaign_id: str
    source_project: str
    destination_project: str
    source_location: str
    destination_location: str
    resources: tuple[BatchResourceSpec, ...]
    schema_version: int = BATCH_SCHEMA_VERSION
    operation: str = "copy"
    naming: str = "preserve_display_name"
    publish_incidents: bool = True
    execute_sql: bool = False
    secret_handling: str = "block"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "BatchSelection":
        if not isinstance(raw, Mapping):
            raise BatchError("La selección del lote debe ser un objeto JSON")
        try:
            schema_version = int(raw.get("schema_version", BATCH_SCHEMA_VERSION))
        except (TypeError, ValueError) as error:
            raise BatchError("schema_version de la selección debe ser entero") from error
        if schema_version != BATCH_SCHEMA_VERSION:
            raise BatchError("schema_version de la selección no es compatible")
        operation = str(raw.get("operation") or "copy").strip().casefold()
        if operation not in {"copy", "update"}:
            raise BatchError("operation debe ser copy o update")
        legacy_location = str(raw.get("location") or "").strip()
        source_location = str(raw.get("source_location") or legacy_location).strip()
        destination_location = str(raw.get("destination_location") or legacy_location).strip()
        required = ("campaign_id", "source_project", "destination_project")
        missing = [key for key in required if not str(raw.get(key) or "").strip()]
        if not source_location:
            missing.append("source_location")
        if not destination_location:
            missing.append("destination_location")
        if missing:
            raise BatchError("La selección no contiene: " + ", ".join(missing))
        if operation == "update" and str(raw.get("source_project") or "").strip() != str(raw.get("destination_project") or "").strip():
            raise BatchError("operation=update requiere que source_project y destination_project sean el mismo proyecto")
        raw_resources = raw.get("resources")
        if not isinstance(raw_resources, list) or not raw_resources:
            raise BatchError("resources debe ser una lista no vacía")
        resources = tuple(BatchResourceSpec.from_mapping(item) for item in raw_resources)
        keys = [item.key for item in resources]
        if len(keys) != len(set(keys)):
            raise BatchError("La selección contiene recursos duplicados por tipo y nombre")
        naming = str(raw.get("naming") or "preserve_display_name")
        if naming != "preserve_display_name":
            raise BatchError("La selección solo admite naming=preserve_display_name")
        if bool(raw.get("execute_sql", False)):
            raise BatchError("Los lotes de migración no ejecutan SQL ni dry-run")
        secret_handling = str(raw.get("secret_handling") or "block").strip().casefold()
        if secret_handling not in SECRET_HANDLING_MODES:
            raise BatchError("secret_handling debe ser block o sealed_copy")
        return cls(
            campaign_id=str(raw["campaign_id"]).strip(),
            source_project=str(raw["source_project"]).strip(),
            destination_project=str(raw["destination_project"]).strip(),
            source_location=source_location,
            destination_location=destination_location,
            resources=resources,
            naming=naming,
            operation=operation,
            publish_incidents=bool(raw.get("publish_incidents", True)),
            execute_sql=False,
            secret_handling=secret_handling,
        )

    @classmethod
    def load(cls, path: Path) -> "BatchSelection":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise BatchError(f"No se pudo leer la selección: {error}") from error
        return cls.from_mapping(raw)

    def validate_counts(self, expected: Mapping[str, int] | None = None) -> None:
        if expected is None:
            return
        counts = {kind: sum(item.kind == kind for item in self.resources) for kind in BATCH_KINDS}
        for kind, required in expected.items():
            if counts.get(kind, 0) != int(required):
                raise BatchError(f"El lote requiere {required} {kind}; recibió {counts.get(kind, 0)}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "campaign_id": self.campaign_id,
            "source_project": self.source_project,
            "destination_project": self.destination_project,
            "source_location": self.source_location,
            "destination_location": self.destination_location,
            # ``location`` remains a destination alias for readers of schema v1.
            "location": self.destination_location,
            "operation": self.operation,
            "naming": self.naming,
            "publish_incidents": self.publish_incidents,
            "execute_sql": False,
            "secret_handling": self.secret_handling,
            "resources": [item.to_dict() for item in self.resources],
        }

    @property
    def location(self) -> str:
        """Legacy single-region alias retained for schema-v1 callers."""
        return self.destination_location


def _commit_time(resource: ResourceRef) -> datetime | None:
    metadata = resource.metadata or {}
    raw = next(
        (
            metadata.get(key)
            for key in ("commit_time", "head_commit_time", "update_time", "updateTime")
            if metadata.get(key)
        ),
        None,
    )
    if raw is None and "T" in resource.fingerprint:
        raw = resource.fingerprint
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def resolve_batch_resources(
    selection: BatchSelection,
    catalog_resources: Iterable[ResourceRef],
    *,
    discarded: list[dict[str, Any]] | None = None,
) -> list[ResourceRef]:
    """Resolve canonical names, safely deduplicating legacy visible names."""
    catalog = list(catalog_resources)
    resolved: list[ResourceRef] = []
    for spec in selection.resources:
        candidates = [
            resource
            for resource in catalog
            if resource.kind == spec.kind
            and resource.project == selection.source_project
            and resource.location == selection.source_location
        ]
        if spec.name:
            matches = [resource for resource in candidates if resource.name == spec.name]
        else:
            matches = [
                resource
                for resource in candidates
                if normalize_display_name(resource.display_name) == normalize_display_name(spec.display_name)
            ]
        if not matches:
            raise BatchError(
                f"No se encontró un {spec.kind} llamado {spec.display_name!r} en "
                f"{selection.source_project}/{selection.source_location}"
            )
        if len(matches) > 1:
            ordered_names = ", ".join(sorted(item.name for item in matches))
            dated = [(item, _commit_time(item)) for item in matches]
            if any(commit_time is None for _, commit_time in dated):
                raise BatchError(
                    f"El recurso {spec.display_name!r} es ambiguo y no tiene fecha de commit segura: {ordered_names}"
                )
            latest_time = max(commit_time for _, commit_time in dated if commit_time is not None)
            latest = [item for item, commit_time in dated if commit_time == latest_time]
            if len(latest) != 1:
                raise BatchError(
                    f"El recurso {spec.display_name!r} tiene un empate en el commit más reciente: {ordered_names}"
                )
            selected = latest[0]
            if discarded is not None:
                for item, commit_time in sorted(dated, key=lambda pair: pair[0].name):
                    if item.name == selected.name:
                        continue
                    discarded.append(
                        {
                            "status": "superseded_duplicate",
                            "resource": item.to_dict(),
                            "selected_resource": selected.name,
                            "commit_time": commit_time.isoformat().replace("+00:00", "Z") if commit_time else "",
                        }
                    )
            matches = [selected]
        resolved.append(matches[0])
    return resolved


def partition_batch_resources(resources: Sequence[ResourceRef], *, batch_size: int = 25) -> list[list[ResourceRef]]:
    """Sort canonical resources and split them into bounded deterministic lots."""
    try:
        batch_size = int(batch_size)
    except (TypeError, ValueError) as error:
        raise BatchError("batch_size debe ser un entero") from error
    if batch_size <= 0:
        raise BatchError("batch_size debe ser mayor que cero")
    kind_order = {"shared_query": 0, "notebook": 1}
    ordered = sorted(
        resources,
        key=lambda item: (kind_order.get(item.kind, 99), normalize_display_name(item.display_name), item.name),
    )
    return [ordered[offset : offset + batch_size] for offset in range(0, len(ordered), batch_size)]


def partition_batch_records(
    records: Sequence[Mapping[str, Any]], *, skip_pending: bool = False
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Separate published, active and explicitly deferred records.

    A pending record is an operator decision (for example, a destination
    collision found after another lot was published).  It is never skipped
    implicitly: callers must pass ``skip_pending=True`` so a partial campaign
    remains an explicit, auditable operation while retaining its digest.
    """
    published: list[Mapping[str, Any]] = []
    active: list[Mapping[str, Any]] = []
    pending: list[Mapping[str, Any]] = []
    for record in records:
        status = str(record.get("status") or "")
        if status in BATCH_TERMINAL_STATUSES:
            published.append(record)
        elif status in BATCH_PENDING_STATUSES:
            pending.append(record)
        else:
            active.append(record)
    if pending and not skip_pending:
        names = ", ".join(
            str((item.get("resource") or {}).get("display_name") or "recurso")
            for item in pending[:3]
        )
        if len(pending) > 3:
            names += ", …"
        raise BatchError(
            f"El lote tiene {len(pending)} recurso(s) pendiente(s) ({names}); "
            "confirma --skip-pending para continuar con los demás"
        )
    return active, published, pending


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    normalized = "".join(char for char in normalized if not unicodedata.combining(char)).casefold()
    value = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    if not value:
        value = "qf-resource"
    if not value[0].isalpha():
        value = "qf-" + value
    if len(value) < 2:
        value += "-q"
    return value[:63].rstrip("-")


def destination_repository_id(display_name: str, kind: str, *, existing_ids: Iterable[str] = ()) -> str:
    """Return a Dataform-safe ID, hashing only when an ID collides."""
    candidate = _slug(display_name)
    existing = {str(item).casefold() for item in existing_ids}
    if candidate.casefold() not in existing:
        return candidate
    suffix = hashlib.sha256(f"{kind}|{display_name}".encode("utf-8")).hexdigest()[:8]
    hashed = (candidate[: 63 - len(suffix) - 1].rstrip("-") + "-" + suffix).lower()
    if hashed.casefold() not in existing:
        return hashed
    # A pre-existing hashed ID is exceptionally unlikely, but deterministic
    # retrying keeps the collision guarantee explicit instead of silently
    # returning an already-occupied repository ID.
    for index in range(2, 100):
        extra = hashlib.sha256(f"{kind}|{display_name}|{index}".encode("utf-8")).hexdigest()[:8]
        candidate_with_extra = (candidate[: 63 - len(extra) - 1].rstrip("-") + "-" + extra).lower()
        if candidate_with_extra.casefold() not in existing:
            return candidate_with_extra
    raise BatchError(f"No se pudo generar un repository_id único para {display_name!r}")


@dataclass(frozen=True)
class RewriteAssetResult:
    proposed_content: bytes
    before_sha256: str
    proposed_sha256: str
    changed_files: tuple[str, ...] = ()
    applied_routes: tuple[dict[str, Any], ...] = ()
    incidents: tuple[dict[str, Any], ...] = ()
    dynamic_cells: tuple[int, ...] = ()
    security_blockers: tuple[dict[str, Any], ...] = ()
    sql_executed: bool = False

    @property
    def warnings(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.incidents) + tuple(
            {"kind": "dynamic_sql", "cell": cell, "line": 1, "path": f"cells/b{cell:04d}"}
            for cell in self.dynamic_cells
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "before_sha256": self.before_sha256,
            "proposed_sha256": self.proposed_sha256,
            "changed_files": list(self.changed_files),
            "applied_routes": list(self.applied_routes),
            "incidents": list(self.incidents),
            "dynamic_cells": list(self.dynamic_cells),
            "security_blockers": list(self.security_blockers),
            "warning_count": len(self.warnings),
            "sql_executed": self.sql_executed,
        }


def classify_asset(kind: str, filename: str, content: bytes) -> dict[str, Any]:
    """Classify proposed code without executing SQL or contacting a provider.

    The classification is deliberately a review signal, not an execution
    permission.  ``migration-batch`` may copy a risky artifact for human
    review, while normal validation and execution paths remain strict.
    """
    if kind == "shared_query":
        try:
            sql = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise BatchError(f"El SQL {filename} no es UTF-8 válido") from error
        result = validate_sql_text(sql)
        return {
            "statement_class": result.statement_class,
            "read_only": result.read_only,
            "references": list(result.references),
            "errors": list(result.errors),
            "warnings": list(result.warnings),
            "dynamic_cells": [],
        }
    if kind != "notebook":
        raise BatchError(f"El lote no admite el tipo {kind}")
    try:
        extraction = analyze_sql_fragments(content)
        result = validate_sql_fragments(extraction.fragments)
    except Exception as error:
        # Keep malformed notebooks in the manifest as blocked resources so a
        # single empty/invalid export does not discard the rest of a lot.  The
        # diagnostic is deliberately generic and content-free.
        return {
            "statement_class": "unknown",
            "read_only": False,
            "references": [],
            "errors": ["notebook_unreadable"],
            "warnings": [],
            "dynamic_cells": [],
        }
    statement_class = result.statement_class
    if not extraction.fragments and not extraction.dynamic_cells and statement_class == "empty":
        # A valid Python notebook may intentionally contain no SQL.  It is a
        # copyable artifact; only an empty/malformed file itself is blocked.
        statement_class = "not_applicable"
    # A notebook with executable SQL assembled at runtime is not statically
    # classifiable.  If literal DML also exists, retain the stronger mutating
    # class while recording dynamic cells as an additional review reason.
    if extraction.dynamic_cells and statement_class != "mutating":
        statement_class = "dynamic"
    return {
        "statement_class": statement_class,
        "read_only": bool(result.read_only and not extraction.dynamic_cells),
        "references": list(result.references),
        "errors": list(result.errors),
        "warnings": list(result.warnings),
        "dynamic_cells": list(extraction.dynamic_cells),
    }


def _classification_warnings(
    classification: Mapping[str, Any],
    *,
    filename: str,
    resource_kind: str,
) -> list[dict[str, Any]]:
    """Convert static classification into safe, content-free report rows."""
    statement_class = str(classification.get("statement_class") or "unknown")
    warnings: list[dict[str, Any]] = []
    if statement_class == "mutating":
        warnings.append({"kind": "mutating", "path": filename, "line": 1, "statement_class": statement_class})
    elif statement_class == "unknown":
        warnings.append({"kind": "unknown", "path": filename, "line": 1, "statement_class": statement_class})
    return warnings


def _review_reasons(
    classification: Mapping[str, Any],
    rewrite: RewriteAssetResult,
    warnings: Sequence[Mapping[str, Any]],
) -> list[str]:
    reasons: set[str] = set()
    for item in warnings:
        kind = str(item.get("kind") or "")
        if kind in REVIEW_WARNING_KINDS:
            reasons.add(kind)
    statement_class = str(classification.get("statement_class") or "")
    if statement_class == "mutating":
        reasons.add("mutating")
    elif statement_class == "unknown":
        reasons.add("unknown")
    if statement_class == "dynamic" or classification.get("dynamic_cells"):
        reasons.add("dynamic_sql")
    if str(statement_class) == "empty":
        reasons.add("empty_sql")
    if rewrite.security_blockers:
        reasons.add("embedded_secret")
    if not reasons and rewrite.changed_files:
        # A clean static copy still gets a review record, but does not need a
        # warning badge.  ``required`` is false in this case.
        return []
    return sorted(reasons)


def _source_string(cell: Mapping[str, Any]) -> tuple[str, bool]:
    value = cell.get("source", "")
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "".join(value), True
    if isinstance(value, str):
        return value, False
    raise BatchError("Una celda del notebook contiene source inválido")


def _line_for_offset(source: str, offset: int) -> int:
    return source.count("\n", 0, max(0, offset)) + 1


def _cell_suffix(notebook: Mapping[str, Any], cell: Mapping[str, Any]) -> str:
    metadata = cell.get("metadata") or {}
    language = str(
        metadata.get("language")
        or (notebook.get("metadata") or {}).get("kernelspec", {}).get("language")
        or (notebook.get("metadata") or {}).get("language_info", {}).get("name")
        or "python"
    ).lower()
    return "sql" if language in {"sql", "bigquery"} else "py"


def _route_incidents(source: str, unknown: Sequence[UnknownRoute], path: str, cell: int | None) -> list[dict[str, Any]]:
    return [
        {
            "kind": "unknown_route",
            "route": item.route,
            "start": item.start,
            "end": item.end,
            "line": _line_for_offset(source, item.start),
            "path": path,
            "cell": cell,
        }
        for item in unknown
    ]


_SECRET_PATTERN = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/=-]{12,}|ghp_[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_-]{20,}|"
    r"(?:password|secret|api[_-]?key)\s*[:=]\s*['\"][^'\"]{8,}['\"])"
)


def _security_findings(source: str, path: str, cell: int | None) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for match in _SECRET_PATTERN.finditer(source):
        findings.append(
            {
                "kind": "embedded_secret",
                "rule": "credential-like literal",
                "path": path,
                "cell": cell,
                "line": _line_for_offset(source, match.start()),
            }
        )
    return findings


def _mask_text(value: str) -> str:
    """Mask credential-like literals without retaining their values."""
    return _SECRET_PATTERN.sub("[REDACTED]", value)


def mask_sensitive_content(kind: str, content: bytes) -> bytes:
    """Return a review-safe copy with credential-like values redacted.

    The raw asset is intentionally never written by this helper.  Notebook
    JSON is parsed before masking so a secret cannot leak through a quoted or
    escaped cell source.  Only string values are transformed; the resulting
    notebook remains valid JSON for the Web Preview and audit archive.
    """
    if not isinstance(content, bytes):
        raise BatchError("El contenido a censurar debe ser bytes")
    if kind == "shared_query":
        try:
            return _mask_text(content.decode("utf-8")).encode("utf-8")
        except UnicodeDecodeError as error:
            raise BatchError("El SQL a censurar no es UTF-8 válido") from error
    if kind != "notebook":
        raise BatchError(f"El lote no admite el tipo {kind}")
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BatchError("El notebook a censurar no es JSON UTF-8 válido") from error

    def mask(value: Any) -> Any:
        if isinstance(value, str):
            return _mask_text(value)
        if isinstance(value, list):
            return [mask(item) for item in value]
        if isinstance(value, dict):
            return {key: mask(item) for key, item in value.items()}
        return value

    return (json.dumps(mask(value), ensure_ascii=False, indent=1) + "\n").encode("utf-8")


def rewrite_asset(kind: str, filename: str, content: bytes, dictionary: RouteDictionary) -> RewriteAssetResult:
    """Rewrite an asset in memory and return a row-free, auditable result."""
    if kind not in BATCH_KINDS:
        raise BatchError(f"El lote no admite el tipo {kind}")
    relative_filename = Path(str(filename or ""))
    if relative_filename.is_absolute() or ".." in relative_filename.parts or not relative_filename.name:
        raise BatchError("El archivo exportado queda fuera del workspace")
    if not isinstance(content, bytes):
        raise BatchError("El contenido del recurso debe ser bytes")
    before_sha = _sha256(content)
    changed_files: list[str] = []
    applied: list[dict[str, Any]] = []
    incidents: list[dict[str, Any]] = []
    dynamic_cells: list[int] = []
    security_blockers: list[dict[str, Any]] = []
    if not content.strip():
        return RewriteAssetResult(
            proposed_content=content,
            before_sha256=before_sha,
            proposed_sha256=before_sha,
            incidents=({"kind": "empty_content", "path": filename, "line": 1, "cell": None},),
        )
    if kind == "shared_query":
        try:
            source = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise BatchError(f"El SQL {filename} no es UTF-8 válido") from error
        rewritten, routes, unknown = rewrite_text(source, dictionary)
        applied.extend(item.to_dict() for item in routes)
        incidents.extend(_route_incidents(source, unknown, filename, None))
        security_blockers.extend(_security_findings(source, filename, None))
        proposed = rewritten.encode("utf-8")
        if proposed != content:
            changed_files.append(filename)
    else:
        try:
            notebook = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            return RewriteAssetResult(
                proposed_content=content,
                before_sha256=before_sha,
                proposed_sha256=before_sha,
                incidents=({"kind": "malformed_notebook", "path": filename, "line": 1, "cell": None},),
            )
        if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
            raise BatchError(f"El notebook {filename} no contiene una lista cells")
        if not isinstance(notebook.get("metadata") or {}, dict):
            raise BatchError(f"El notebook {filename} contiene metadata inválida")
        for index, cell in enumerate(notebook["cells"]):
            if not isinstance(cell, dict):
                raise BatchError(f"La celda {index} del notebook {filename} no es un objeto")
            if not isinstance(cell.get("metadata") or {}, dict):
                raise BatchError(f"La celda {index} del notebook {filename} contiene metadata inválida")
        # Validate cell sources and classify dynamic SQL without executing it.
        try:
            dynamic_cells.extend(analyze_sql_fragments(content).dynamic_cells)
        except Exception as error:
            raise BatchError(f"No se pudo analizar SQL del notebook {filename}: {error}") from error
        for index, cell in enumerate(notebook["cells"]):
            if not isinstance(cell, dict):
                raise BatchError(f"La celda {index} del notebook {filename} no es un objeto")
            cell_type = str(cell.get("cell_type") or "")
            if cell_type not in {"code", "markdown", "raw"}:
                raise BatchError(f"La celda {index} del notebook {filename} tiene un tipo inválido")
            source, was_list = _source_string(cell)
            if cell_type != "code":
                continue
            rewritten, routes, unknown = rewrite_text(source, dictionary)
            applied.extend(item.to_dict() for item in routes)
            path = f"cells/b{index:04d}.{_cell_suffix(notebook, cell)}"
            incidents.extend(_route_incidents(source, unknown, path, index))
            security_blockers.extend(_security_findings(source, path, index))
            if rewritten != source:
                changed_files.append(path)
                cell["source"] = rewritten.splitlines(keepends=True) if was_list else rewritten
        if changed_files:
            proposed = (json.dumps(notebook, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
        else:
            proposed = content
    return RewriteAssetResult(
        proposed_content=proposed,
        before_sha256=before_sha,
        proposed_sha256=_sha256(proposed),
        changed_files=tuple(sorted(set(changed_files))),
        applied_routes=tuple(applied),
        incidents=tuple(incidents),
        dynamic_cells=tuple(sorted(set(dynamic_cells))),
        security_blockers=tuple(security_blockers),
    )


def _asset_values(asset: Any) -> tuple[bytes, str, str]:
    if isinstance(asset, bytes):
        return asset, "content.sql", ""
    content = getattr(asset, "content", None)
    filename = getattr(asset, "filename", None)
    head = getattr(asset, "head_commit", None)
    if isinstance(content, bytes):
        return content, str(filename or "content.sql"), str(head or "")
    if isinstance(asset, Mapping) and isinstance(asset.get("content"), (bytes, bytearray)):
        return bytes(asset["content"]), str(asset.get("filename") or "content.sql"), str(asset.get("head_commit") or "")
    raise BatchError("El inventario no contiene contenido exportado para un recurso")


def _destination_names(resources: Iterable[ResourceRef]) -> set[str]:
    return {normalize_display_name(item.display_name) for item in resources if item.display_name}


def build_batch_manifest(
    selection: BatchSelection,
    resources: Sequence[ResourceRef],
    assets: Mapping[str, Any],
    dictionary: RouteDictionary,
    *,
    destination_resources: Iterable[ResourceRef] = (),
    destination_assets: Mapping[str, Any] | None = None,
    discarded_resources: Iterable[Mapping[str, Any]] = (),
    catalog_generated_at: str = "",
    existing_repository_ids: Iterable[str] = (),
    requests_per_minute: int = BATCH_REQUESTS_PER_MINUTE,
) -> dict[str, Any]:
    """Build an immutable planning manifest from exported source assets."""
    try:
        requests_per_minute = int(requests_per_minute)
    except (TypeError, ValueError) as error:
        raise BatchError("requests_per_minute debe ser un entero") from error
    if requests_per_minute <= 0 or requests_per_minute > BATCH_QUOTA_REQUESTS_PER_MINUTE:
        raise BatchError(
            f"requests_per_minute debe estar entre 1 y {BATCH_QUOTA_REQUESTS_PER_MINUTE}"
        )
    if len(resources) != len(selection.resources):
        raise BatchError("La selección y los recursos resueltos no tienen el mismo tamaño")
    if selection.operation == "update":
        return build_update_manifest(
            selection,
            resources,
            assets,
            dictionary,
            catalog_generated_at=catalog_generated_at,
            discarded_resources=discarded_resources,
            requests_per_minute=requests_per_minute,
        )
    by_name = {
        resource.name: resource
        for resource in destination_resources
        if resource.project == selection.destination_project
        and resource.location == selection.destination_location
        and resource.kind in BATCH_KINDS
    }
    destination_assets = destination_assets or {}
    destination_by_display: dict[tuple[str, str], list[ResourceRef]] = {}
    destination_by_display_any_kind: dict[str, list[ResourceRef]] = {}
    for destination_resource in by_name.values():
        key = (destination_resource.kind, normalize_display_name(destination_resource.display_name))
        destination_by_display.setdefault(key, []).append(destination_resource)
        destination_by_display_any_kind.setdefault(
            normalize_display_name(destination_resource.display_name), []
        ).append(destination_resource)
    existing_ids = {resource.name.rsplit("/", 1)[-1] for resource in by_name.values()}
    existing_ids.update(str(item) for item in existing_repository_ids)
    records: list[dict[str, Any]] = []
    for index, resource in enumerate(resources):
        if resource.name not in assets:
            raise BatchError(f"No hay exportación para {resource.display_name}")
        content, filename, head = _asset_values(assets[resource.name])
        rewrite = rewrite_asset(resource.kind, filename, content, dictionary)
        classification = classify_asset(resource.kind, filename, rewrite.proposed_content)
        # The selection is the source of truth for the destination label.  A
        # catalog may normalize accents or include decorative markers, but the
        # analyst's requested visible name must survive the copy unchanged.
        requested_display_name = selection.resources[index].display_name
        destination_matches = sorted(
            destination_by_display.get((resource.kind, normalize_display_name(requested_display_name)), []),
            key=lambda item: item.name,
        )
        cross_kind_matches = sorted(
            (
                item
                for item in destination_by_display_any_kind.get(
                    normalize_display_name(requested_display_name), []
                )
                if item.kind != resource.kind
            ),
            key=lambda item: (item.kind, item.name),
        )
        existing_destination = destination_matches[0] if len(destination_matches) == 1 else None
        if existing_destination is not None:
            repo_id = existing_destination.name.rsplit("/", 1)[-1]
        else:
            repo_id = destination_repository_id(requested_display_name, resource.kind, existing_ids=existing_ids)
            existing_ids.add(repo_id)
        warnings = [dict(item) for item in rewrite.warnings]
        warnings.extend(
            _classification_warnings(
                classification,
                filename=filename,
                resource_kind=resource.kind,
            )
        )
        warnings.extend(dict(item) for item in rewrite.security_blockers)
        if cross_kind_matches:
            # Dataform repositories share one ID namespace regardless of the
            # single-file asset type.  A shared query with the same visible
            # name as a requested notebook (or vice versa) is therefore a
            # real destination conflict, even though the catalog kinds
            # differ.  Do not silently create a hashed second resource that
            # would confuse analysts in Studio.
            reconciliation_status = "destination_conflict"
            destination_content_sha256 = ""
        elif not destination_matches:
            reconciliation_status = "ready_to_publish"
            destination_content_sha256 = ""
        elif len(destination_matches) > 1:
            reconciliation_status = "destination_conflict"
            destination_content_sha256 = ""
        else:
            destination_asset = destination_assets.get(existing_destination.name) if existing_destination else None
            try:
                destination_content, _destination_filename, _destination_head = _asset_values(destination_asset)
            except BatchError:
                reconciliation_status = "destination_conflict"
                destination_content_sha256 = ""
            else:
                destination_content_sha256 = _sha256(destination_content)
                reconciliation_status = (
                    "already_present" if destination_content == rewrite.proposed_content else "destination_conflict"
                )
        collision = reconciliation_status == "destination_conflict"
        blockers = list(rewrite.security_blockers)
        blockers.extend(
            warning
            for warning in rewrite.incidents
            if str(warning.get("kind") or "") in {"empty_content", "malformed_notebook"}
        )
        if classification.get("statement_class") == "empty":
            blockers.append({"kind": "empty_sql", "path": filename, "line": 1})
        review_reasons = _review_reasons(classification, rewrite, warnings)
        if not selection.publish_incidents:
            blockers.extend(
                warning
                for warning in warnings
                if str(warning.get("kind") or "") in REVIEW_WARNING_KINDS
            )
        secret_blocked = bool(rewrite.security_blockers)
        sealed_secret = secret_blocked and selection.secret_handling == "sealed_copy"
        migration_eligible = reconciliation_status == "ready_to_publish" and not blockers
        status = (
            "destination_conflict"
            if collision
            else ("security_pending" if sealed_secret else ("blocked" if blockers else reconciliation_status))
        )
        records.append(
            {
                "ordinal": index + 1,
                "resource": resource.to_dict(),
                "source": {
                    "filename": filename,
                    "head_commit": head,
                    "content_sha256": rewrite.before_sha256,
                },
                "destination": {
                    "project": selection.destination_project,
                    "location": selection.destination_location,
                    "repository_id": repo_id,
                    "display_name": requested_display_name,
                    "repository": existing_destination.name if existing_destination is not None else "",
                    "content_sha256": destination_content_sha256,
                    "collision": collision,
                    "collision_with": [
                        {
                            "kind": item.kind,
                            "name": item.name,
                            "display_name": item.display_name,
                        }
                        for item in cross_kind_matches
                    ],
                },
                "rewrite": rewrite.to_dict(),
                "classification": classification,
                "review": {
                    "required": bool(review_reasons),
                    "reasons": review_reasons,
                    "policy": "human_review_before_execution" if review_reasons else "standard_review",
                },
                "migration_eligible": migration_eligible,
                "execution_eligible": False,
                "security": {
                    "secret_handling": "sealed_copy" if sealed_secret else "block",
                    "sealed": sealed_secret,
                    "finding_count": len(rewrite.security_blockers),
                },
                "warnings": warnings,
                "blockers": blockers + ([{"kind": "destination_collision"}] if collision else []),
                "status": status,
            }
        )
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    manifest: dict[str, Any] = {
        "schema_version": BATCH_SCHEMA_VERSION,
        "campaign_id": selection.campaign_id,
        "created_at": now,
        "status": "blocked" if any(item["status"] in {"blocked", "destination_conflict"} for item in records) else ("security_pending" if any(item["status"] == "security_pending" for item in records) else "planned"),
        "source_project": selection.source_project,
        "destination_project": selection.destination_project,
        "source_location": selection.source_location,
        "destination_location": selection.destination_location,
        "location": selection.destination_location,
        "dictionary_sha256": dictionary.dictionary_sha256,
        "dictionary_id": dictionary.dictionary_id,
        "catalog_generated_at": catalog_generated_at,
        "selection": selection.to_dict(),
        "policy": {
            "mode": "copy",
            "no_sql_execution": True,
            "dry_run": {"skipped": True, "reason": "Migración de código; no se ejecuta SQL"},
            "warnings": "accept",
            "region": selection.destination_location,
            "requests_per_minute": requests_per_minute,
            "quota_requests_per_minute": BATCH_QUOTA_REQUESTS_PER_MINUTE,
            "secret_handling": selection.secret_handling,
        },
        "resources": records,
        "execution": {},
        "discarded_resources": [dict(item) for item in discarded_resources],
        "warnings": [
            {"resource": item["resource"], **warning}
            for item in records
            for warning in item["warnings"]
        ],
    }
    manifest["publication_digest"] = build_batch_digest(manifest)
    manifest["sealed_publication_digest"] = build_sealed_digest(manifest)
    return manifest


def build_update_manifest(
    selection: BatchSelection,
    resources: Sequence[ResourceRef],
    assets: Mapping[str, Any],
    dictionary: RouteDictionary,
    *,
    discarded_resources: Iterable[Mapping[str, Any]] = (),
    catalog_generated_at: str = "",
    requests_per_minute: int = BATCH_REQUESTS_PER_MINUTE,
) -> dict[str, Any]:
    """Build a plan that rewrites the current destination in place.

    Unlike the copy-only migration, the exported asset is both the baseline
    and the destination.  The repository identity, display name, region and
    current commit are therefore bound into every record and later checked by
    ``batch run`` before the single-file update commit.
    """
    if selection.source_project != selection.destination_project:
        raise BatchError("Un lote de actualización debe permanecer en un solo proyecto")
    if any(resource.project != selection.destination_project for resource in resources):
        raise BatchError("Todos los recursos de un lote de actualización deben pertenecer al proyecto destino")
    records: list[dict[str, Any]] = []
    for index, resource in enumerate(resources):
        if resource.name not in assets:
            raise BatchError(f"No hay exportación para {resource.display_name}")
        content, filename, head = _asset_values(assets[resource.name])
        rewrite = rewrite_asset(resource.kind, filename, content, dictionary)
        classification = classify_asset(resource.kind, filename, rewrite.proposed_content)
        warnings = [dict(item) for item in rewrite.warnings]
        warnings.extend(
            _classification_warnings(
                classification,
                filename=filename,
                resource_kind=resource.kind,
            )
        )
        warnings.extend(dict(item) for item in rewrite.security_blockers)
        blockers = list(rewrite.security_blockers)
        blockers.extend(
            warning
            for warning in rewrite.incidents
            if str(warning.get("kind") or "") in {"empty_content", "malformed_notebook"}
        )
        if classification.get("statement_class") == "empty":
            blockers.append({"kind": "empty_sql", "path": filename, "line": 1})
        if not head:
            blockers.append({"kind": "missing_head_commit", "path": resource.name, "line": 1})
        review_reasons = _review_reasons(classification, rewrite, warnings)
        if not selection.publish_incidents:
            blockers.extend(
                warning
                for warning in warnings
                if str(warning.get("kind") or "") in REVIEW_WARNING_KINDS
            )
        sealed_secret = bool(rewrite.security_blockers) and selection.secret_handling == "sealed_copy"
        compliant = rewrite.proposed_content == content
        status = (
            "already_compliant"
            if compliant and not blockers
            else ("security_pending" if sealed_secret else ("blocked" if blockers else "ready_to_update"))
        )
        resource_snapshot = resource.to_dict()
        resource_snapshot["fingerprint"] = head
        repository_id = resource.name.rsplit("/", 1)[-1]
        records.append(
            {
                "ordinal": index + 1,
                "resource": resource_snapshot,
                "source": {
                    "filename": filename,
                    "head_commit": head,
                    "content_sha256": rewrite.before_sha256,
                },
                "destination": {
                    "project": selection.destination_project,
                    "location": resource.location,
                    "repository_id": repository_id,
                    "display_name": selection.resources[index].display_name or resource.display_name,
                    "repository": resource.name,
                    "content_sha256": rewrite.before_sha256,
                    "collision": False,
                    "collision_with": [],
                },
                "rewrite": rewrite.to_dict(),
                "classification": classification,
                "review": {
                    "required": bool(review_reasons),
                    "reasons": review_reasons,
                    "policy": "human_review_before_execution" if review_reasons else "standard_review",
                },
                "migration_eligible": status == "ready_to_update" and not blockers,
                "execution_eligible": False,
                "security": {
                    "secret_handling": "sealed_copy" if sealed_secret else "block",
                    "sealed": sealed_secret,
                    "finding_count": len(rewrite.security_blockers),
                },
                "warnings": warnings,
                "blockers": blockers,
                "status": status,
            }
        )
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    manifest: dict[str, Any] = {
        "schema_version": BATCH_SCHEMA_VERSION,
        "campaign_id": selection.campaign_id,
        "created_at": now,
        "status": "blocked" if any(item["status"] in {"blocked", "security_pending"} for item in records) else "planned",
        "operation": "update",
        "source_project": selection.source_project,
        "destination_project": selection.destination_project,
        "source_location": selection.source_location,
        "destination_location": selection.destination_location,
        "location": selection.destination_location,
        "dictionary_sha256": dictionary.dictionary_sha256,
        "dictionary_id": dictionary.dictionary_id,
        "catalog_generated_at": catalog_generated_at,
        "selection": selection.to_dict(),
        "policy": {
            "mode": "update",
            "no_sql_execution": True,
            "dry_run": {"skipped": True, "reason": "Actualización de código; SQL y dry-run deshabilitados"},
            "warnings": "accept",
            "region": selection.destination_location,
            "requests_per_minute": requests_per_minute,
            "quota_requests_per_minute": BATCH_QUOTA_REQUESTS_PER_MINUTE,
            "secret_handling": selection.secret_handling,
        },
        "resources": records,
        "execution": {},
        "discarded_resources": [dict(item) for item in discarded_resources],
        "warnings": [
            {"resource": item["resource"], **warning}
            for item in records
            for warning in item["warnings"]
        ],
    }
    manifest["publication_digest"] = build_batch_digest(manifest)
    manifest["sealed_publication_digest"] = build_sealed_digest(manifest)
    return manifest


def _digest_projection(manifest: Mapping[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(dict(manifest))
    for key in ("publication_digest", "sealed_publication_digest", "created_at", "status", "execution", "report", "inventory"):
        value.pop(key, None)
    resources = value.get("resources") or []
    # Manifests produced before the review contract was introduced did not
    # bind presentation-only review fields into their digest.  Preserve their
    # validation compatibility, while making every new classification/review
    # decision part of the immutable approval digest.
    legacy_review = not any(
        isinstance(item, Mapping)
        and any(key in item for key in ("classification", "migration_eligible", "execution_eligible"))
        for item in resources
    )
    if legacy_review:
        value.pop("review", None)
    if isinstance(resources, list):
        immutable: list[dict[str, Any]] = []
        for item in resources:
            if not isinstance(item, Mapping):
                continue
            record = copy.deepcopy(dict(item))
            ignored = ("status", "task", "review_relative", "review_file", "published", "receipt", "error", "changed_files", "proposed_sha256", "sealed_copy", "redacted_review")
            if legacy_review:
                ignored = (*ignored, "review")
            for key in ignored:
                record.pop(key, None)
            immutable.append(record)
        value["resources"] = sorted(
            immutable,
            key=lambda item: (
                str((item.get("resource") or {}).get("kind") if isinstance(item.get("resource"), Mapping) else ""),
                str((item.get("resource") or {}).get("name") if isinstance(item.get("resource"), Mapping) else ""),
            ),
        )
    selection = value.get("selection")
    if isinstance(selection, Mapping) and isinstance(selection.get("resources"), list):
        selection["resources"] = sorted(
            [item for item in selection["resources"] if isinstance(item, Mapping)],
            key=lambda item: (str(item.get("kind") or ""), normalize_display_name(str(item.get("display_name") or ""))),
        )
    warnings = value.get("warnings")
    if isinstance(warnings, list):
        value["warnings"] = sorted(
            [item for item in warnings if isinstance(item, Mapping)],
            key=lambda item: (
                str((item.get("resource") or {}).get("kind") if isinstance(item.get("resource"), Mapping) else ""),
                str((item.get("resource") or {}).get("name") if isinstance(item.get("resource"), Mapping) else ""),
                str(item.get("path") or ""),
                int(item.get("line") or 0),
                str(item.get("route") or item.get("kind") or ""),
            ),
        )
    return value


def build_batch_digest(manifest: Mapping[str, Any]) -> str:
    return _sha256(_canonical(_digest_projection(manifest)))


def _sealed_digest_projection(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Project only non-secret metadata needed to approve sealed writes."""
    resources: list[dict[str, Any]] = []
    for item in manifest.get("resources") or []:
        if not isinstance(item, Mapping):
            continue
        security = item.get("security") or {}
        rewrite = item.get("rewrite") or {}
        raw_blockers = (rewrite.get("security_blockers") or []) or (item.get("security_blockers") or [])
        blockers = [
            {
                "kind": str(finding.get("kind") or ""),
                "rule": str(finding.get("rule") or ""),
                "path": str(finding.get("path") or ""),
                "cell": finding.get("cell"),
                "line": finding.get("line"),
            }
            for finding in raw_blockers
            if isinstance(finding, Mapping)
        ]
        if not blockers and not bool(security.get("sealed")):
            continue
        resource = item.get("resource") or {}
        source = item.get("source") or {}
        destination = item.get("destination") or {}
        resources.append(
            {
                "resource": {
                    "kind": str(resource.get("kind") or ""),
                    "name": str(resource.get("name") or ""),
                    "project": str(resource.get("project") or ""),
                    "location": str(resource.get("location") or ""),
                    "display_name": str(resource.get("display_name") or ""),
                },
                "source": {
                    "filename": str(source.get("filename") or ""),
                    "head_commit": str(source.get("head_commit") or ""),
                    "content_sha256": str(source.get("content_sha256") or ""),
                },
                "destination": {
                    "project": str(destination.get("project") or ""),
                    "location": str(destination.get("location") or ""),
                    "repository_id": str(destination.get("repository_id") or ""),
                    "display_name": str(destination.get("display_name") or ""),
                    "repository": str(destination.get("repository") or ""),
                    "content_sha256": str(destination.get("content_sha256") or ""),
                    "collision": bool(destination.get("collision")),
                },
                "rewrite": {
                    "before_sha256": str(rewrite.get("before_sha256") or ""),
                    "proposed_sha256": str(rewrite.get("proposed_sha256") or ""),
                    "changed_files": sorted(str(path) for path in (rewrite.get("changed_files") or [])),
                },
                "security_blockers": blockers,
            }
        )
    return {
        "schema_version": int(manifest.get("schema_version") or BATCH_SCHEMA_VERSION),
        "campaign_id": str(manifest.get("campaign_id") or ""),
        "source_project": str(manifest.get("source_project") or ""),
        "destination_project": str(manifest.get("destination_project") or ""),
        "source_location": str(manifest.get("source_location") or manifest.get("location") or ""),
        "destination_location": str(manifest.get("destination_location") or manifest.get("location") or ""),
        "dictionary_sha256": str(manifest.get("dictionary_sha256") or ""),
        "secret_handling": str(
            (manifest.get("policy") or {}).get("secret_handling")
            or (manifest.get("selection") or {}).get("secret_handling")
            or "block"
        ),
        "resources": sorted(
            resources,
            key=lambda item: (
                str((item.get("resource") or {}).get("kind") or ""),
                str((item.get("resource") or {}).get("name") or ""),
            ),
        ),
    }


def build_sealed_digest(manifest: Mapping[str, Any]) -> str:
    """Build the independent approval digest for sealed-copy resources."""
    projection = _sealed_digest_projection(manifest)
    if projection["secret_handling"] != "sealed_copy" or not projection["resources"]:
        return ""
    return _sha256(_canonical(projection))


def validate_batch_manifest(
    manifest: Mapping[str, Any],
    *,
    approved_digest: str | None = None,
    allow_blocked: bool = False,
    allow_sealed_pending: bool = False,
) -> None:
    if not isinstance(manifest, Mapping):
        raise BatchError("El manifest de lote debe ser un objeto")
    try:
        schema_version = int(manifest.get("schema_version", 0))
    except (TypeError, ValueError) as error:
        raise BatchError("schema_version del manifest debe ser entero") from error
    if schema_version != BATCH_SCHEMA_VERSION:
        raise BatchError("Manifest de lote no compatible")
    required = ("campaign_id", "source_project", "destination_project", "dictionary_sha256")
    if any(not str(manifest.get(key) or "").strip() for key in required):
        raise BatchError("Manifest de lote incompleta")
    legacy_location = str(manifest.get("location") or "").strip()
    source_location = str(manifest.get("source_location") or legacy_location).strip()
    destination_location = str(manifest.get("destination_location") or legacy_location).strip()
    if not source_location or not destination_location:
        raise BatchError("Manifest de lote sin región origen o destino")
    operation = str(manifest.get("operation") or (manifest.get("selection") or {}).get("operation") or "copy").strip().casefold()
    if operation not in {"copy", "update"}:
        raise BatchError("Manifest contiene una operación inválida")
    selection_operation = str((manifest.get("selection") or {}).get("operation") or operation).strip().casefold()
    if selection_operation != operation:
        raise BatchError("La operación del manifest no coincide con la selección")
    if operation == "update" and str(manifest.get("source_project") or "") != str(manifest.get("destination_project") or ""):
        raise BatchError("Un manifest de actualización debe permanecer en un solo proyecto")
    records = manifest.get("resources")
    if not isinstance(records, list) or not records:
        raise BatchError("Manifest de lote sin recursos")
    keys: set[tuple[str, str]] = set()
    for record in records:
        if not isinstance(record, Mapping) or not isinstance(record.get("resource"), Mapping):
            raise BatchError("Manifest contiene un registro inválido")
        resource = record["resource"]
        key = (str(resource.get("kind") or ""), str(resource.get("name") or ""))
        if key in keys:
            raise BatchError("Manifest contiene recursos repetidos")
        keys.add(key)
        if key[0] not in BATCH_KINDS or not key[1]:
            raise BatchError("Manifest contiene un tipo o nombre inválido")
        rewrite = record.get("rewrite") or {}
        if rewrite.get("sql_executed"):
            raise BatchError("El manifest indica que se ejecutó SQL")
        destination = record.get("destination") or {}
        has_review_contract = any(
            key in record for key in ("classification", "review", "migration_eligible", "execution_eligible")
        )
        if has_review_contract:
            classification = record.get("classification")
            review = record.get("review")
            if not isinstance(classification, Mapping) or not isinstance(review, Mapping):
                raise BatchError("El manifest no contiene clasificación/revisión válidas")
            if str(classification.get("statement_class") or "") not in {"read_only", "mutating", "unknown", "dynamic", "empty", "not_applicable"}:
                raise BatchError("La clasificación estática del manifest no es válida")
            if bool(record.get("execution_eligible", False)):
                raise BatchError("Un lote de migración nunca puede habilitar ejecución")
            blockers_present = bool(destination.get("collision") or record.get("blockers"))
            if not isinstance(record.get("migration_eligible"), bool):
                raise BatchError("migration_eligible debe ser booleano")
            status = str(record.get("status") or "")
            expected_eligible = status in {"ready", "ready_to_publish", "ready_to_update", "prepared", "published"} and not blockers_present
            if bool(record.get("migration_eligible")) != expected_eligible:
                raise BatchError("migration_eligible no coincide con los bloqueos del recurso")
        blockers_allowed_for_sealed = (
            allow_sealed_pending
            and str(record.get("status") or "") in {"security_pending", "prepared", "published", "already_present"}
            and bool((record.get("security") or {}).get("sealed"))
        )
        if (destination.get("collision") or record.get("blockers")) and not (allow_blocked or blockers_allowed_for_sealed):
            raise BatchError(f"El recurso {resource.get('display_name')} tiene un bloqueo de seguridad o destino")
    policy = manifest.get("policy") or {}
    if not bool(policy.get("no_sql_execution", False)) or not (policy.get("dry_run") or {}).get("skipped"):
        raise BatchError("La política del lote debe dejar SQL y dry-run deshabilitados")
    secret_handling = str(
        policy.get("secret_handling")
        or (manifest.get("selection") or {}).get("secret_handling")
        or "block"
    ).strip().casefold()
    if secret_handling not in SECRET_HANDLING_MODES:
        raise BatchError("El manifest contiene un modo de secretos inválido")
    sealed_records = [
        item
        for item in records
        if isinstance(item, Mapping)
        and bool((item.get("security") or {}).get("sealed"))
    ]
    if sealed_records and secret_handling != "sealed_copy":
        raise BatchError("Los registros sellados requieren secret_handling=sealed_copy")
    allowed_sealed_statuses = {"security_pending", "prepared", "published", "already_present"}
    if allow_blocked:
        allowed_sealed_statuses.update({"blocked", "destination_conflict"})
    for item in sealed_records:
        if str(item.get("status") or "") not in allowed_sealed_statuses:
            raise BatchError("Un recurso sellado debe permanecer pendiente/preparado hasta su autorización")
        if not (item.get("rewrite") or {}).get("security_blockers"):
            raise BatchError("Un recurso sellado no contiene evidencia de bloqueo de seguridad")
    sealed_digest = str(manifest.get("sealed_publication_digest") or "")
    expected_sealed_digest = build_sealed_digest(manifest)
    if expected_sealed_digest and sealed_digest != expected_sealed_digest:
        raise BatchError("El sealed_publication_digest del manifest no coincide con su contenido")
    if allow_sealed_pending and sealed_records:
        # Only embedded-secret blockers may be admitted by the explicit
        # sealed path.  Destination collisions and other blockers remain hard
        # stops even after a security reference is supplied.
        for item in sealed_records:
            destination = item.get("destination") or {}
            non_secret_blockers = [
                blocker
                for blocker in (item.get("blockers") or [])
                if str((blocker or {}).get("kind") if isinstance(blocker, Mapping) else "")
                not in {"embedded_secret"}
            ]
            if destination.get("collision") or non_secret_blockers:
                raise BatchError(
                    f"El recurso {(item.get('resource') or {}).get('display_name')} tiene un bloqueo adicional al secreto"
                )
    digest = str(manifest.get("publication_digest") or "")
    if not digest:
        raise BatchError("El manifest no contiene publication_digest")
    expected = build_batch_digest(manifest)
    if digest and digest != expected:
        raise BatchError("El publication_digest del manifest no coincide con su contenido")
    if approved_digest is not None and approved_digest != expected:
        raise BatchError("El digest aprobado no coincide con el manifest actual")


def save_batch_manifest(manifest: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(dict(manifest), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_batch_manifest(path: Path, *, allow_blocked: bool = True) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BatchError(f"No se pudo leer el manifest de lote: {error}") from error
    if not isinstance(raw, dict):
        raise BatchError("El manifest de lote debe ser un objeto")
    validate_batch_manifest(raw, allow_blocked=allow_blocked)
    return raw


def render_batch_report(manifest: Mapping[str, Any]) -> str:
    records = manifest.get("resources") or []
    warnings = [item for item in (manifest.get("warnings") or []) if isinstance(item, Mapping)]
    status = str(manifest.get("status") or "planned")
    review_count = sum(
        1
        for item in records
        if isinstance(item, Mapping) and bool((item.get("review") or {}).get("required") or item.get("warnings"))
    )
    class_counts: dict[str, int] = {}
    for item in records:
        if not isinstance(item, Mapping):
            continue
        classification = item.get("classification") or {}
        statement_class = str(classification.get("statement_class") or "no_evaluada")
        class_counts[statement_class] = class_counts.get(statement_class, 0) + 1
    lines = [
        f"# Informe de migración QueryFlow: `{manifest.get('campaign_id', '')}`",
        "",
        f"- Estado: **{status}**",
        f"- Origen: `{manifest.get('source_project')}` · regiones: `{manifest.get('source_location') or manifest.get('location')}` → `{manifest.get('destination_project')}/{manifest.get('destination_location') or manifest.get('location')}`",
        f"- Recursos: **{len(records)}** · pendientes: **{sum(1 for item in records if isinstance(item, Mapping) and item.get('status') == 'pending')}** · advertencias: **{len(warnings)}** · rutas no cubiertas: **{sum(1 for item in warnings if item.get('kind') == 'unknown_route')}**",
        f"- Revisión humana requerida: **{review_count}** · ejecución habilitada por el lote: **0**",
        f"- Copias selladas pendientes: **{sum(1 for item in records if isinstance(item, Mapping) and (item.get('security') or {}).get('sealed'))}** · digest sellado: `{manifest.get('sealed_publication_digest') or 'no aplica'}`",
        "- Clasificación estática: " + ", ".join(f"`{key}` {value}" for key, value in sorted(class_counts.items())),
        f"- Digest de publicación: `{manifest.get('publication_digest', '')}`",
        "- No se ejecutó SQL ni dry-run (la migración solo reescribe código).",
        "",
        "## Recursos",
        "",
        "| # | Tipo | Recurso | Destino | Cambios | Advertencias | Estado |",
        "|---:|---|---|---|---:|---:|---|",
    ]
    for index, record in enumerate(records, 1):
        # The renderer is also useful for a compact hand-written report where
        # resources are still selection specs rather than full records.
        record_map = record if isinstance(record, Mapping) else {}
        resource = record_map.get("resource") or {}
        if not resource and (record_map.get("kind") or record_map.get("display_name")):
            resource = {"kind": record_map.get("kind"), "display_name": record_map.get("display_name")}
        destination = record_map.get("destination") or {}
        rewrite = record_map.get("rewrite") or {}
        lines.append(
            f"| {index} | {resource.get('kind', '')} | `{resource.get('display_name', '')}` "
            f"| `{destination.get('display_name', '')}` | {len(rewrite.get('changed_files') or [])} "
            f"| {len(record_map.get('warnings') or [])} | {record_map.get('status', 'ready')} |"
        )
    if warnings:
        lines.extend(["", "## Advertencias y rutas no cubiertas", "", "| Recurso | Archivo/celda | Línea | Detalle |", "|---|---|---:|---|"])
        for warning in warnings:
            resource = warning.get("resource") or {}
            detail = warning.get("route") or warning.get("kind") or "advertencia"
            path = warning.get("path") or "-"
            line = warning.get("line") or "-"
            if warning.get("cell") is not None:
                path = f"{path} · celda {warning['cell']}"
            lines.append(f"| `{resource.get('display_name', '')}` | `{path}` | {line} | `{detail}` |")
    lines.extend(
        [
            "",
            "## Política y recuperación",
            "",
        "- Las advertencias se aceptan y se conservan en el destino; no se inventan rutas.",
        "- Las copias con advertencias quedan etiquetadas para revisión humana; QueryFlow no las ejecuta.",
            "- Una colisión, cambio del origen, error de autenticación/VPC o lectura remota inconsistente detiene la publicación.",
            "- La reanudación reutiliza el mismo digest y solo intenta recursos pendientes.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_batch_review(manifest: Mapping[str, Any], *, task_links: Mapping[str, str] | None = None) -> str:
    """Render a compact dark review page with GitHub-like status cues."""
    task_links = task_links or {}
    records = manifest.get("resources") or []
    rows: list[str] = []
    for record in records:
        resource = record.get("resource") or {}
        rewrite = record.get("rewrite") or {}
        warnings = record.get("warnings") or []
        status = str(record.get("status") or "ready")
        tone = "bad" if status in {"blocked", "failed"} else "warn" if status == "pending" or warnings else "good"
        link = task_links.get(str(resource.get("name")))
        action = f'<a href="{html.escape(link, quote=True)}">Ver diff</a>' if link else "Pendiente"
        review = record.get("review") or {}
        review_required = bool(review.get("required") or warnings)
        reasons = ", ".join(str(item) for item in (review.get("reasons") or []))
        review_label = "Revisión humana" if review_required else "Revisión estándar"
        review_class = "review-required" if review_required else "review-standard"
        execution_label = "no ejecutable" if record.get("execution_eligible") is not True else "ejecutable"
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(resource.get('display_name') or ''))}</code><small>{html.escape(str(resource.get('kind') or ''))}</small></td>"
            f"<td><span class=\"badge {tone}\">{html.escape(status)}</span><small>{html.escape(execution_label)}</small></td>"
            f"<td>{len(rewrite.get('changed_files') or [])}</td><td>{len(warnings)}</td>"
            f"<td><span class=\"review {review_class}\">{html.escape(review_label)}</span>"
            f"<small>{html.escape(reasons or 'sin incidencias')}</small><br>{action}</td>"
            "</tr>"
        )
    review_count = sum(1 for item in records if bool((item.get("review") or {}).get("required") or item.get("warnings")))
    pending_count = sum(1 for item in records if str(item.get("status") or "") == "pending")
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>QueryFlow · {html.escape(str(manifest.get('campaign_id') or 'lote'))}</title>
<style>
:root {{ color-scheme: dark; --bg:#0b1020; --panel:#121a2b; --line:#26334d; --text:#e5e7eb; --muted:#94a3b8; --green:#34d399; --amber:#fbbf24; --red:#fb7185; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.45 system-ui,sans-serif; }}
main {{ max-width:1100px; margin:0 auto; padding:28px 20px 48px; }} h1 {{ margin:0 0 4px; font-size:24px; }} p,small {{ color:var(--muted); }} .meta {{ display:flex; gap:10px; flex-wrap:wrap; margin:16px 0; }} .chip {{ border:1px solid var(--line); border-radius:999px; padding:5px 9px; color:var(--muted); }}
.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; overflow:hidden; }} table {{ width:100%; border-collapse:collapse; }} th,td {{ text-align:left; padding:11px 13px; border-bottom:1px solid var(--line); }} th {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.05em; }} td small {{ display:block; margin-top:2px; }} code {{ font-family:ui-monospace,monospace; }} .badge,.review {{ display:inline-flex; border:1px solid var(--line); border-radius:999px; padding:3px 8px; font-size:12px; }} .good,.review-standard {{ color:var(--green); }} .warn,.review-required {{ color:var(--amber); }} .bad {{ color:var(--red); }} a {{ color:#93c5fd; text-decoration:none; }} a:hover {{ text-decoration:underline; }}
</style></head><body><main><h1>Migración de código</h1>
<p>{html.escape(str(manifest.get('campaign_id') or ''))} · no se ejecutó SQL ni dry-run</p>
<div class="meta"><span class="chip">{len(records)} recursos</span><span class="chip">{pending_count} pendientes</span><span class="chip">{len(manifest.get('warnings') or [])} advertencias</span><span class="chip">{review_count} requieren revisión humana</span><span class="chip">digest <code>{html.escape(str(manifest.get('publication_digest') or ''))}</code></span></div>
<section class="panel"><table><thead><tr><th>Recurso</th><th>Estado</th><th>Cambios</th><th>Advertencias</th><th>Revisión</th></tr></thead><tbody>{''.join(rows)}</tbody></table></section>
</main></body></html>"""


def write_batch_reports(manifest: Mapping[str, Any], manifest_path: Path) -> tuple[Path, Path]:
    json_path = manifest_path.with_name("migration-report.json")
    markdown_path = manifest_path.with_name("migration-report.md")
    warnings = [item for item in (manifest.get("warnings") or []) if isinstance(item, Mapping)]
    resources = [item for item in (manifest.get("resources") or []) if isinstance(item, Mapping)]
    review_required_count = sum(
        1 for item in resources if bool((item.get("review") or {}).get("required") or item.get("warnings"))
    )
    classification_counts: dict[str, int] = {}
    for item in resources:
        statement_class = str((item.get("classification") or {}).get("statement_class") or "not_evaluated")
        classification_counts[statement_class] = classification_counts.get(statement_class, 0) + 1
    review_reason_counts: dict[str, int] = {}
    for item in resources:
        for reason in (item.get("review") or {}).get("reasons") or []:
            key = str(reason)
            review_reason_counts[key] = review_reason_counts.get(key, 0) + 1
    report = {
        "schema_version": BATCH_SCHEMA_VERSION,
        "campaign_id": manifest.get("campaign_id"),
        "source_project": manifest.get("source_project"),
        "destination_project": manifest.get("destination_project"),
        "location": manifest.get("location"),
        "source_location": manifest.get("source_location") or manifest.get("location"),
        "destination_location": manifest.get("destination_location") or manifest.get("location"),
        "dictionary_id": manifest.get("dictionary_id"),
        "dictionary_sha256": manifest.get("dictionary_sha256"),
        "publication_digest": manifest.get("publication_digest"),
        "sealed_publication_digest": manifest.get("sealed_publication_digest", ""),
        "secret_handling": (manifest.get("policy") or {}).get("secret_handling", "block"),
        "status": manifest.get("status"),
        "resource_count": len(manifest.get("resources") or []),
        "pending_count": sum(1 for item in resources if str(item.get("status") or "") == "pending"),
        "security_pending_count": sum(1 for item in resources if str(item.get("status") or "") == "security_pending" or bool((item.get("security") or {}).get("sealed"))),
        "pending_resources": [
            str((item.get("resource") or {}).get("display_name") or "")
            for item in resources
            if str(item.get("status") or "") == "pending"
        ],
        "review_required_count": review_required_count,
        "classification_counts": classification_counts,
        "review_reason_counts": review_reason_counts,
        "execution_eligible_count": sum(1 for item in resources if item.get("execution_eligible") is True),
        "warning_count": len(warnings),
        "unknown_route_count": sum(1 for item in warnings if item.get("kind") == "unknown_route"),
        "dynamic_sql_cell_count": sum(1 for item in warnings if item.get("kind") == "dynamic_sql"),
        "sql_executed": False,
        "resources": manifest.get("resources") or [],
        "warnings": warnings,
    }
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_batch_report(manifest), encoding="utf-8")
    return json_path, markdown_path


def serve_batch_review(manifest_path: Path, port: int) -> tuple[ThreadingHTTPServer, str]:
    """Serve the consolidated preview and its individual task diffs."""
    manifest_path = manifest_path.resolve()
    root = manifest_path.parent.resolve()

    class BatchReviewHandler(BaseHTTPRequestHandler):
        server_version = "QueryFlowBatchReview/1"

        def _send(self, body: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            relative = urlsplit(self.path).path.lstrip("/") or "review.html"
            if relative == "review.html":
                try:
                    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                    task_links = {
                        str((item.get("resource") or {}).get("name")): str(item.get("review_relative"))
                        for item in (raw.get("resources") or [])
                        if isinstance(item, Mapping) and item.get("review_relative")
                    }
                    body = render_batch_review(raw, task_links=task_links).encode("utf-8")
                except (OSError, ValueError, BatchError) as error:
                    self._send(str(error).encode("utf-8"), "text/plain; charset=utf-8", HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                self._send(body, "text/html; charset=utf-8")
                return
            candidate = (root / relative).resolve()
            if root not in candidate.parents or not candidate.is_file() or candidate.name == "manifest.json":
                self._send(b"Not found\n", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)
                return
            content_type = "text/html; charset=utf-8" if candidate.suffix == ".html" else "text/plain; charset=utf-8"
            try:
                self._send(candidate.read_bytes(), content_type)
            except OSError as error:
                self._send(str(error).encode("utf-8"), "text/plain; charset=utf-8", HTTPStatus.INTERNAL_SERVER_ERROR)

        def log_message(self, format: str, *args: Any) -> None:
            return None

    server = ThreadingHTTPServer(("0.0.0.0", port), BatchReviewHandler)
    host = os.environ.get("WEB_HOST", "localhost")
    actual_port = int(server.server_address[1])
    return server, f"https://{actual_port}-{host}/review.html"
