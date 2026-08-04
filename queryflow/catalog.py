from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Union


class CatalogError(RuntimeError):
    """A catalog operation could not be completed safely."""


@dataclass(frozen=True)
class ResourceRef:
    kind: str
    name: str
    project: str
    location: str
    display_name: str
    fingerprint: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ResourceRef":
        required = ("kind", "name", "project", "location", "display_name", "fingerprint")
        missing = [key for key in required if not isinstance(value.get(key), str)]
        if missing:
            raise CatalogError("Recurso inválido; faltan campos: " + ", ".join(missing))
        metadata = value.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise CatalogError("Los metadatos del recurso deben ser un objeto")
        return cls(
            kind=value["kind"],
            name=value["name"],
            project=value["project"],
            location=value["location"],
            display_name=value["display_name"],
            fingerprint=value["fingerprint"],
            metadata=metadata,
        )


@dataclass(frozen=True)
class Catalog:
    resources: list[ResourceRef]
    generated_at: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "resources": [resource.to_dict() for resource in self.resources],
            "warnings": list(self.warnings),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    def search(self, text: str, *, kind: Optional[str] = None) -> list[ResourceRef]:
        needle = text.casefold()
        return sorted(
            [
                resource
                for resource in self.resources
                if (kind is None or resource.kind == kind)
                and needle in " ".join(
                    (
                        resource.name,
                        resource.display_name,
                        resource.project,
                        resource.kind,
                    )
                ).casefold()
            ],
            key=lambda resource: (resource.kind, resource.display_name.casefold(), resource.name),
        )


def catalog_from_json(value: Union[str, bytes]) -> Catalog:
    try:
        raw = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise CatalogError("El catálogo no contiene JSON válido") from error
    if not isinstance(raw, dict) or not isinstance(raw.get("resources"), list):
        raise CatalogError("El catálogo debe contener una lista resources")
    resources = [ResourceRef.from_dict(item) for item in raw["resources"]]
    generated_at = raw.get("generated_at")
    if not isinstance(generated_at, str):
        raise CatalogError("El catálogo no contiene generated_at")
    warnings = raw.get("warnings") or []
    if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
        raise CatalogError("warnings debe ser una lista de texto")
    return Catalog(resources=resources, generated_at=generated_at, warnings=warnings)


def _stable_fingerprint(raw: Any) -> str:
    encoded = json.dumps(raw, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


Runner = Callable[[list[str]], Any]


def run_json_command(command: list[str]) -> Any:
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise CatalogError(f"Falló {' '.join(command)}: {detail}")
    try:
        return json.loads(completed.stdout or "null")
    except json.JSONDecodeError as error:
        raise CatalogError(f"{' '.join(command)} no devolvió JSON válido") from error


def _resource_name(raw: dict[str, Any]) -> str:
    name = str(raw.get("name") or "")
    prefix = "//dataform.googleapis.com/"
    return name[len(prefix) :] if name.startswith(prefix) else name


def _split_project_location(name: str) -> tuple[str, str]:
    parts = name.split("/")
    try:
        project = parts[parts.index("projects") + 1]
        location = parts[parts.index("locations") + 1]
    except (ValueError, IndexError) as error:
        raise CatalogError(f"Nombre de recurso inválido: {name}") from error
    return project, location


def discover_projects(*, account: Optional[str] = None, runner: Runner = run_json_command) -> list[str]:
    command = ["gcloud"]
    if account:
        command.append(f"--account={account}")
    command += ["projects", "list", "--format=json", "--quiet"]
    raw = runner(command)
    if not isinstance(raw, list):
        raise CatalogError("gcloud projects list no devolvió una lista")
    projects = []
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("projectId"), str):
            projects.append(item["projectId"])
    return sorted(set(projects))


def discover_dataform_assets(
    project: str,
    *,
    account: Optional[str] = None,
    runner: Runner = run_json_command,
) -> tuple[list[ResourceRef], list[str]]:
    command = ["gcloud"]
    if account:
        command.append(f"--account={account}")
    command += [
        "asset",
        "search-all-resources",
        f"--scope=projects/{project}",
        "--asset-types=dataform.googleapis.com/Repository",
        "--format=json",
        "--quiet",
    ]
    try:
        raw = runner(command)
    except Exception as error:
        return [], [f"{project}: no se pudo consultar Dataform: {error}"]
    if not isinstance(raw, list):
        return [], [f"{project}: Cloud Asset Inventory no devolvió una lista"]
    resources: list[ResourceRef] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = _resource_name(item)
        resource_project, location = _split_project_location(name)
        labels = item.get("labels") or {}
        asset_type = labels.get("single-file-asset-type")
        if asset_type == "notebook":
            kind = "notebook"
        elif asset_type in {"sql", "query", "shared_query"}:
            kind = "shared_query"
        else:
            kind = "dataform_repository"
        resources.append(
            ResourceRef(
                kind=kind,
                name=name,
                project=resource_project,
                location=location,
                display_name=str(item.get("displayName") or ""),
                fingerprint=str(item.get("etag") or item.get("updateTime") or _stable_fingerprint(item)),
                metadata={"labels": labels, "asset_type": "dataform.googleapis.com/Repository"},
            )
        )
    return resources, []


def discover_bigquery_assets(
    project: str,
    *,
    account: Optional[str] = None,
    runner: Runner = run_json_command,
) -> tuple[list[ResourceRef], list[str]]:
    asset_types = (
        ("bigquery.googleapis.com/Table", "table"),
        ("bigquery.googleapis.com/Routine", "routine"),
        ("bigquerydatatransfer.googleapis.com/TransferConfig", "scheduled_query"),
    )
    resources: list[ResourceRef] = []
    warnings: list[str] = []
    for asset_type, default_kind in asset_types:
        command = ["gcloud"]
        if account:
            command.append(f"--account={account}")
        command += [
            "asset",
            "search-all-resources",
            f"--scope=projects/{project}",
            f"--asset-types={asset_type}",
            "--format=json",
            "--quiet",
        ]
        try:
            raw = runner(command)
        except Exception as error:
            warnings.append(f"{project}: no se pudo consultar {asset_type}: {error}")
            continue
        if not isinstance(raw, list):
            warnings.append(f"{project}: respuesta inválida para {asset_type}")
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            prefix = "//bigquery.googleapis.com/"
            transfer_prefix = "//bigquerydatatransfer.googleapis.com/"
            if name.startswith(prefix):
                name = name[len(prefix) :]
            elif name.startswith(transfer_prefix):
                name = name[len(transfer_prefix) :]
            if not name:
                continue
            additional = item.get("additionalAttributes") or {}
            kind = default_kind
            if default_kind == "table" and str(additional.get("tableType") or "").upper() in {"VIEW", "MATERIALIZED_VIEW"}:
                kind = "view" if str(additional.get("tableType")).upper() == "VIEW" else "materialized_view"
            parts = name.split("/")
            try:
                resource_project = parts[parts.index("projects") + 1]
            except (ValueError, IndexError):
                warnings.append(f"Nombre BigQuery inválido: {name}")
                continue
            location = ""
            if "locations" in parts:
                try:
                    location = parts[parts.index("locations") + 1]
                except IndexError:
                    location = ""
            elif default_kind in {"table", "routine"}:
                location = str(additional.get("location") or "")
            resources.append(
                ResourceRef(
                    kind=kind,
                    name=name,
                    project=resource_project,
                    location=location,
                    display_name=str(item.get("displayName") or parts[-1]),
                    fingerprint=str(item.get("etag") or item.get("updateTime") or _stable_fingerprint(item)),
                    metadata={"asset_type": asset_type, "additional_attributes": additional},
                )
            )
    return resources, warnings


def refresh_catalog(
    *,
    projects: Optional[Iterable[str]] = None,
    account: Optional[str] = None,
    runner: Runner = run_json_command,
) -> Catalog:
    project_ids = sorted(set(projects or discover_projects(account=account, runner=runner)))
    resources: list[ResourceRef] = []
    warnings: list[str] = []
    for project in project_ids:
        discovered, project_warnings = discover_dataform_assets(
            project, account=account, runner=runner
        )
        resources.extend(discovered)
        warnings.extend(project_warnings)
        discovered, project_warnings = discover_bigquery_assets(
            project, account=account, runner=runner
        )
        resources.extend(discovered)
        warnings.extend(project_warnings)
    return Catalog(
        resources=sorted(resources, key=lambda item: (item.project, item.location, item.kind, item.name)),
        generated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        warnings=warnings,
    )


def save_catalog(catalog: Catalog, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(catalog.to_json() + "\n", encoding="utf-8")


def load_catalog(path: Path) -> Catalog:
    try:
        return catalog_from_json(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise CatalogError(f"No se pudo leer el catálogo {path}: {error}") from error
