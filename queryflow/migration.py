"""Private, deterministic route rewriting for migration pilot tasks.

The module deliberately operates on local QueryFlow task files only.  It does
not inspect BigQuery tables and it never sends SQL to a provider.  A route
dictionary is an auditable input; unknown references are retained and emitted
as incidents instead of being guessed.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


class MigrationDictionaryError(ValueError):
    """The private migration dictionary is invalid."""


@dataclass(frozen=True)
class RouteMapping:
    id: str
    zone: str
    old: str
    new: str
    active: bool = True
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.id,
            "zone": self.zone,
            "old": self.old,
            "new": self.new,
            "active": self.active,
        }
        if self.notes:
            value["notes"] = self.notes
        return value


@dataclass(frozen=True)
class UnknownRoute:
    route: str
    start: int
    end: int

    def to_dict(self) -> dict[str, Any]:
        return {"route": self.route, "start": self.start, "end": self.end}


@dataclass(frozen=True)
class AppliedRoute:
    mapping_id: str
    old: str
    new: str
    occurrences: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "mapping_id": self.mapping_id,
            "old": self.old,
            "new": self.new,
            "occurrences": self.occurrences,
        }


@dataclass(frozen=True)
class RewriteReport:
    task: str
    task_id: str
    resource_kind: str
    resource: dict[str, Any]
    changed_files: tuple[str, ...]
    applied_routes: tuple[AppliedRoute, ...]
    unknown_routes: tuple[dict[str, Any], ...]
    before_sha256: str
    proposed_sha256: str
    plan_digest: str
    applied: bool = False

    @property
    def status(self) -> str:
        if self.applied:
            return "published_with_incidents" if self.unknown_routes else "applied"
        return "planned_with_incidents" if self.unknown_routes else "planned"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "task": self.task,
            "task_id": self.task_id,
            "resource_kind": self.resource_kind,
            "resource": self.resource,
            "changed_files": list(self.changed_files),
            "applied_routes": [item.to_dict() for item in self.applied_routes],
            "unknown_routes": list(self.unknown_routes),
            "before_sha256": self.before_sha256,
            "proposed_sha256": self.proposed_sha256,
            "plan_digest": self.plan_digest,
            "applied": self.applied,
            "status": self.status,
            "checks": {
                "table_existence_checked": False,
                "sql_executed": False,
                "markdown_or_outputs_changed": False,
            },
        }


@dataclass(frozen=True)
class RouteDictionary:
    schema_version: int
    dictionary_id: str
    mappings: tuple[RouteMapping, ...]
    source: dict[str, Any] = field(default_factory=dict)
    scope: dict[str, Any] = field(default_factory=dict)
    reference_targets: tuple[dict[str, Any], ...] = ()
    out_of_scope: tuple[dict[str, Any], ...] = ()
    dictionary_sha256: str = ""

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "RouteDictionary":
        if not isinstance(raw, dict):
            raise MigrationDictionaryError("El diccionario debe ser un objeto JSON")
        if int(raw.get("schema_version", 1)) != 1:
            raise MigrationDictionaryError("schema_version del diccionario debe ser 1")
        dictionary_id = str(raw.get("dictionary_id") or "").strip()
        if not dictionary_id or not re.fullmatch(r"[A-Za-z0-9_.-]{3,100}", dictionary_id):
            raise MigrationDictionaryError("dictionary_id no es válido")
        raw_mappings = raw.get("mappings")
        if not isinstance(raw_mappings, list) or not raw_mappings:
            raise MigrationDictionaryError("mappings debe ser una lista no vacía")
        mappings: list[RouteMapping] = []
        ids: set[str] = set()
        origins: set[str] = set()
        destinations: set[str] = set()
        for item in raw_mappings:
            if not isinstance(item, dict):
                raise MigrationDictionaryError("Cada mapping debe ser un objeto")
            mapping_id = str(item.get("id") or "").strip()
            zone = str(item.get("zone") or "").strip().lower()
            old = str(item.get("old") or "").strip().strip("`")
            new = str(item.get("new") or "").strip().strip("`")
            active = bool(item.get("active", True))
            if not mapping_id or mapping_id in ids:
                raise MigrationDictionaryError(f"id de mapping duplicado o vacío: {mapping_id}")
            if zone not in {"raw", "staging"}:
                raise MigrationDictionaryError(f"Zona no aplicable al piloto: {zone}")
            if not _route_prefix.fullmatch(old) or not _route_prefix.fullmatch(new):
                raise MigrationDictionaryError(f"Ruta inválida en mapping {mapping_id}")
            if old == new:
                raise MigrationDictionaryError(f"El mapping {mapping_id} no cambia la ruta")
            if active and old in origins:
                raise MigrationDictionaryError(f"Origen duplicado en el diccionario: {old}")
            if active and new in destinations:
                raise MigrationDictionaryError(f"Destino duplicado en el diccionario: {new}")
            ids.add(mapping_id)
            if active:
                origins.add(old)
                destinations.add(new)
            mappings.append(
                RouteMapping(
                    id=mapping_id,
                    zone=zone,
                    old=old,
                    new=new,
                    active=active,
                    notes=str(item.get("notes") or ""),
                )
            )
        source = raw.get("source") or {}
        scope = raw.get("scope") or {}
        references = raw.get("reference_targets") or []
        out_of_scope = raw.get("out_of_scope") or []
        for label, value in (("source", source), ("scope", scope)):
            if not isinstance(value, dict):
                raise MigrationDictionaryError(f"{label} debe ser un objeto")
        scoped_source = str(scope.get("source_project") or "").strip()
        if scoped_source:
            mismatched = [item.old for item in mappings if item.active and item.old.split(".", 1)[0] != scoped_source]
            if mismatched:
                raise MigrationDictionaryError(
                    "Hay mappings activos fuera del source_project declarado: " + ", ".join(mismatched[:3])
                )
        if not isinstance(references, list) or not all(isinstance(item, dict) for item in references):
            raise MigrationDictionaryError("reference_targets debe ser una lista de objetos")
        if not isinstance(out_of_scope, list) or not all(isinstance(item, dict) for item in out_of_scope):
            raise MigrationDictionaryError("out_of_scope debe ser una lista de objetos")
        canonical = {
            "schema_version": 1,
            "dictionary_id": dictionary_id,
            "source": source,
            "scope": scope,
            "mappings": [item.to_dict() for item in mappings],
            "reference_targets": references,
            "out_of_scope": out_of_scope,
        }
        digest = _sha256_json(canonical)
        return cls(
            schema_version=1,
            dictionary_id=dictionary_id,
            mappings=tuple(mappings),
            source=dict(source),
            scope=dict(scope),
            reference_targets=tuple(dict(item) for item in references),
            out_of_scope=tuple(dict(item) for item in out_of_scope),
            dictionary_sha256=digest,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "dictionary_id": self.dictionary_id,
            "source": self.source,
            "scope": self.scope,
            "mappings": [item.to_dict() for item in self.mappings],
            "reference_targets": list(self.reference_targets),
            "out_of_scope": list(self.out_of_scope),
            "dictionary_sha256": self.dictionary_sha256,
        }

    @property
    def active_mappings(self) -> tuple[RouteMapping, ...]:
        return tuple(item for item in self.mappings if item.active)


_route_prefix = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9][A-Za-z0-9_-]*)+")
_route_candidate = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<route>[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9][A-Za-z0-9_-]*){2,})(?![A-Za-z0-9_-])"
)


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_dictionary(path: Path) -> RouteDictionary:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MigrationDictionaryError(f"No se pudo leer el diccionario: {error}") from error
    return RouteDictionary.from_mapping(raw)


def _mapping_regex(mapping: RouteMapping) -> re.Pattern[str]:
    # BigQuery routes can be quoted with backticks.  A non-identifier boundary
    # avoids replacing a longer project/dataset name accidentally while still
    # allowing the table suffix after the mapped prefix.
    return re.compile(rf"(?<![A-Za-z0-9_-]){re.escape(mapping.old)}(?![A-Za-z0-9_-])")


def _known_prefixes(dictionary: RouteDictionary) -> tuple[str, ...]:
    return tuple(sorted((item.old for item in dictionary.active_mappings), key=len, reverse=True))


def find_unknown_routes(text: str, dictionary: RouteDictionary) -> list[UnknownRoute]:
    known = _known_prefixes(dictionary)
    values: list[UnknownRoute] = []
    seen: set[str] = set()
    for match in _route_candidate.finditer(text):
        route = match.group("route")
        # Only flag routes that belong to the source project(s) captured by the
        # dictionary.  Generic third-party references remain untouched.
        if not any(route == prefix or route.startswith(prefix + ".") for prefix in known):
            source_projects = {
                str(dictionary.scope.get("source_project") or "").strip(),
                *(prefix.split(".", 1)[0] for prefix in known),
            }
            if route.split(".", 1)[0] not in source_projects:
                continue
            # A route candidate can include a mapped suffix after a source
            # prefix.  It is known if any mapping matches its beginning.
            if any(route.startswith(prefix + ".") for prefix in known):
                continue
            if route in seen:
                continue
            seen.add(route)
            values.append(UnknownRoute(route, match.start("route"), match.end("route")))
    return values


def rewrite_text(
    text: str,
    dictionary: RouteDictionary,
) -> tuple[str, list[AppliedRoute], list[UnknownRoute]]:
    result = text
    applied: list[AppliedRoute] = []
    for mapping in sorted(dictionary.active_mappings, key=lambda item: len(item.old), reverse=True):
        pattern = _mapping_regex(mapping)
        result, count = pattern.subn(mapping.new, result)
        if count:
            applied.append(AppliedRoute(mapping.id, mapping.old, mapping.new, count))
    return result, applied, find_unknown_routes(result, dictionary)


def classify_text(text: str, dictionary: RouteDictionary) -> str:
    rewritten, applied, unknown = rewrite_text(text, dictionary)
    del rewritten
    if unknown:
        return "incident"
    if applied:
        return "known"
    return "no_source_routes"


def render_markdown(dictionary: RouteDictionary) -> str:
    lines = [
        f"# Diccionario de rutas: `{dictionary.dictionary_id}`",
        "",
        f"- Versión: `{dictionary.schema_version}`",
        f"- SHA-256: `{dictionary.dictionary_sha256}`",
        "- Alcance: solo rutas activas de zonas Raw y Staging.",
        "- Las rutas desconocidas se conservan y se reportan como incidentes.",
        "",
        "## Rutas activas",
        "",
        "| ID | Zona | Ruta antigua | Ruta nueva |",
        "| --- | --- | --- | --- |",
    ]
    for item in dictionary.active_mappings:
        lines.append(f"| `{item.id}` | {item.zone} | `{item.old}` | `{item.new}` |")
    if dictionary.reference_targets:
        lines.extend(["", "## Referencias fuera de la reescritura", ""])
        for item in dictionary.reference_targets:
            lines.append(f"- {item.get('area', 'Referencia')}: `{item.get('route', '')}`")
    if dictionary.out_of_scope:
        lines.extend(["", "## Fuera de alcance", ""])
        for item in dictionary.out_of_scope:
            lines.append(f"- {item.get('route', item.get('area', 'Elemento no aplicable'))}")
    return "\n".join(lines) + "\n"


def validate_dictionary(path: Path) -> dict[str, Any]:
    dictionary = load_dictionary(path)
    return {
        "ok": True,
        "path": str(path),
        "dictionary_id": dictionary.dictionary_id,
        "dictionary_sha256": dictionary.dictionary_sha256,
        "active_mappings": len(dictionary.active_mappings),
        "reference_targets": len(dictionary.reference_targets),
        "out_of_scope": len(dictionary.out_of_scope),
        "scope": dictionary.scope,
    }


def _safe_task_file(task: Path, relative: str) -> Path:
    candidate = (task / relative).resolve()
    root = task.resolve()
    if root not in candidate.parents or candidate.name == "manifest.json":
        raise MigrationDictionaryError("La ruta del archivo queda fuera de la tarea")
    return candidate


def _plan_digest(
    *,
    task_id: str,
    before_sha256: str,
    dictionary_sha256: str,
    proposed_files: dict[str, str],
    unknown_routes: list[dict[str, Any]],
) -> str:
    payload = {
        "task_id": task_id,
        "before_sha256": before_sha256,
        "dictionary_sha256": dictionary_sha256,
        "proposed_files": proposed_files,
        "unknown_routes": unknown_routes,
    }
    return _sha256_json(payload)


def _notebook_code_files(task: Path, manifest: dict[str, Any]) -> list[tuple[str, str, int | None]]:
    """Return (relative path, source, cell index) for code cells only."""
    cells = task / "cells"
    index_path = cells / "index.json"
    if not index_path.exists():
        return []
    try:
        raw = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MigrationDictionaryError("cells/index.json no es JSON válido") from error
    if not isinstance(raw, dict) or not isinstance(raw.get("cells"), list):
        raise MigrationDictionaryError("cells/index.json tiene un esquema inválido")
    values: list[tuple[str, str, int | None]] = []
    for entry in raw["cells"]:
        if not isinstance(entry, dict) or entry.get("deleted") or entry.get("cell_type") != "code":
            continue
        relative = entry.get("path")
        if not isinstance(relative, str):
            raise MigrationDictionaryError("Una celda de código no tiene path")
        path = _safe_task_file(cells, relative)
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as error:
            raise MigrationDictionaryError(f"No se pudo leer {cells / relative}: {error}") from error
        index = entry.get("baseline_index")
        values.append((f"cells/{relative}", source, int(index) if isinstance(index, int) else None))
    return values


def _write_notebook_code_cell(task: Path, relative: str, source: str) -> None:
    path = _safe_task_file(task, relative)
    path.write_text(source, encoding="utf-8")


def rewrite_task(
    task: Path,
    dictionary: RouteDictionary,
    *,
    apply: bool = False,
    expected_plan_digest: str | None = None,
) -> RewriteReport:
    """Plan or apply dictionary replacements in a local QueryFlow task.

    ``apply=False`` is side-effect free apart from no files at all.  Applying
    changes only touches SQL or notebook code-cell files and writes an audit
    report; Markdown, metadata and outputs are left intact.
    """
    try:
        manifest = json.loads((task / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MigrationDictionaryError(f"Manifest inválido en {task}") from error
    if not isinstance(manifest, dict) or not isinstance(manifest.get("task_id"), str):
        raise MigrationDictionaryError("La tarea no contiene un manifest válido")
    resource = manifest.get("resource") or {}
    kind = str(resource.get("kind") or "")
    filename = str(manifest.get("filename") or "")
    if kind not in {"notebook", "shared_query"}:
        raise MigrationDictionaryError("La reescritura piloto solo admite notebook y shared_query")
    before_file = _safe_task_file(task, filename)
    before_content = before_file.read_bytes()
    before_sha = hashlib.sha256(before_content).hexdigest()
    proposed_files: dict[str, str] = {}
    applied_routes: list[AppliedRoute] = []
    unknown: list[dict[str, Any]] = []
    changed: list[str] = []
    if kind == "notebook":
        code_files = _notebook_code_files(task, manifest)
        if code_files:
            for relative, source, cell_index in code_files:
                rewritten, applied_routes_for_file, unknown_for_file = rewrite_text(source, dictionary)
                proposed_files[relative] = rewritten
                if rewritten != source:
                    changed.append(relative)
                applied_routes.extend(applied_routes_for_file)
                for route in unknown_for_file:
                    unknown.append({
                        "path": relative,
                        "cell": cell_index,
                        **route.to_dict(),
                    })
        else:
            # A task created by an older QueryFlow version may not have a cell
            # workspace.  Preserve notebook structure and rewrite code-cell
            # source in memory, without touching Markdown or outputs.
            try:
                notebook = json.loads(before_content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise MigrationDictionaryError(f"El notebook no es JSON válido: {error}") from error
            for index, cell in enumerate(notebook.get("cells") or []):
                if not isinstance(cell, dict) or cell.get("cell_type") != "code":
                    continue
                source_value = cell.get("source", [])
                source = "".join(source_value) if isinstance(source_value, list) else str(source_value)
                rewritten, applied_routes_for_file, unknown_for_file = rewrite_text(source, dictionary)
                proposed_files[f"cells/{index:04d}"] = rewritten
                if rewritten != source:
                    changed.append(f"cells/{index:04d}")
                applied_routes.extend(applied_routes_for_file)
                unknown.extend({"path": f"cells/{index:04d}", "cell": index, **route.to_dict()} for route in unknown_for_file)
    else:
        try:
            source = before_content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise MigrationDictionaryError(f"El SQL de la tarea no es UTF-8: {error}") from error
        rewritten, applied_routes, unknown_routes = rewrite_text(source, dictionary)
        proposed_files[filename] = rewritten
        if rewritten != source:
            changed.append(filename)
        unknown = [{"path": filename, "cell": None, **route.to_dict()} for route in unknown_routes]
    digest = _plan_digest(
        task_id=str(manifest["task_id"]),
        before_sha256=before_sha,
        dictionary_sha256=dictionary.dictionary_sha256,
        proposed_files=proposed_files,
        unknown_routes=unknown,
    )
    if expected_plan_digest and expected_plan_digest != digest:
        raise MigrationDictionaryError("El digest del plan de reescritura no coincide con la tarea actual")
    # For an existing task, the proposed content hash is the hash of the
    # resource file when possible.  The plan digest protects per-cell changes;
    # this hash is refreshed after apply by QueryFlow's normal sync step.
    proposed_sha = hashlib.sha256(before_content).hexdigest()
    if apply:
        if kind == "notebook" and _notebook_code_files(task, manifest):
            # A freshly exported notebook has a cell workspace even when the
            # dictionary produces no replacement.  Rebuilding it in that case
            # can normalize JSON formatting and change the content hash despite
            # there being no migration edit.  Only sync when a code cell really
            # changed; otherwise preserve the exact exported bytes.
            if changed:
                for relative, source in proposed_files.items():
                    _write_notebook_code_cell(task, relative, source)
                # Import lazily to avoid an import cycle at module load time.
                from .task import sync_notebook_task

                current = sync_notebook_task(task)
                proposed_sha = hashlib.sha256(current).hexdigest()
        elif kind == "shared_query":
            target = _safe_task_file(task, filename)
            target.write_text(proposed_files[filename], encoding="utf-8")
            proposed_sha = hashlib.sha256(target.read_bytes()).hexdigest()
        elif kind == "notebook":
            # Legacy notebook fallback: update only code cell sources and keep
            # all other JSON fields exactly as supplied by the notebook.
            notebook = json.loads(before_content.decode("utf-8"))
            for index, cell in enumerate(notebook.get("cells") or []):
                if not isinstance(cell, dict) or cell.get("cell_type") != "code":
                    continue
                rewritten = proposed_files.get(f"cells/{index:04d}")
                if rewritten is not None:
                    cell["source"] = rewritten.splitlines(keepends=True)
            target = _safe_task_file(task, filename)
            target.write_text(json.dumps(notebook, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            proposed_sha = hashlib.sha256(target.read_bytes()).hexdigest()
        manifest["migration_plan_digest"] = digest
        manifest["migration_status"] = "published_with_incidents" if unknown else "rewritten"
        manifest["proposed_sha256"] = proposed_sha
        (task / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report = RewriteReport(
        task=str(task),
        task_id=str(manifest["task_id"]),
        resource_kind=kind,
        resource=dict(resource),
        changed_files=tuple(sorted(changed)),
        applied_routes=tuple(applied_routes),
        unknown_routes=tuple(unknown),
        before_sha256=before_sha,
        proposed_sha256=proposed_sha,
        plan_digest=digest,
        applied=apply,
    )
    if apply:
        (task / "rewrite-report.json").write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def _text_values(values: Iterable[str]) -> str:
    return "\n".join(values)
