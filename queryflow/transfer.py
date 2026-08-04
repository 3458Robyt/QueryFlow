from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

from .dataform import TokenProvider
from .schedule import ScheduleSpec


class TransferError(RuntimeError):
    """A BigQuery or Data Transfer request could not be completed safely."""


class TransferNotFound(TransferError):
    """A requested dataset or transfer configuration does not exist."""


TransferTransport = Callable[[str, str, dict[str, Any], Optional[dict[str, Any]]], dict[str, Any]]


class TransferClient:
    """Safe client for the subset of APIs needed by the scheduled-query pilot."""

    transfer_api_root = "https://bigquerydatatransfer.googleapis.com/v1"
    bigquery_api_root = "https://bigquery.googleapis.com/bigquery/v2"

    def __init__(
        self,
        account: str,
        allowed_write_project: str,
        *,
        source_project: str = "",
        transport: Optional[TransferTransport] = None,
    ) -> None:
        self.account = account
        self.allowed_write_project = allowed_write_project
        self.source_project = source_project
        self._transport = transport
        self._tokens = TokenProvider(account)

    @staticmethod
    def _project_from_resource(resource: str) -> str:
        match = re.search(r"(?:^|/)projects/([^/]+)/", resource)
        if not match:
            raise TransferError(f"No se pudo identificar el proyecto en {resource}")
        return match.group(1)

    def _request_http(
        self,
        method: str,
        resource: str,
        query: dict[str, Any],
        body: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        api_root = self.transfer_api_root if "transferConfigs" in resource else self.bigquery_api_root
        url = f"{api_root}/{resource.lstrip('/')}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        try:
            token = self._tokens.get()
        except Exception as error:
            raise TransferError(f"No fue posible obtener un token para BigQuery: {error}") from error
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            if error.code == 404:
                raise TransferNotFound(resource) from error
            raise TransferError(f"Google API {method} {resource}: {error.code} {detail}") from error
        except urllib.error.URLError as error:
            raise TransferError(f"No fue posible conectar con Google API: {error}") from error
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError as error:
            raise TransferError("Google API no devolvió JSON válido") from error
        if not isinstance(parsed, dict):
            raise TransferError("Google API devolvió una respuesta inválida")
        return parsed

    def request(
        self,
        method: str,
        resource: str,
        query: dict[str, Any],
        body: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        method = method.upper()
        project = self._project_from_resource(resource)
        if method in {"POST", "PATCH", "PUT", "DELETE"}:
            if project == self.source_project:
                raise TransferError("SEGURIDAD: no se permiten escrituras en el proyecto origen")
            if project != self.allowed_write_project:
                raise TransferError(
                    f"SEGURIDAD: escritura fuera del proyecto destino {self.allowed_write_project}"
                )
        transport = self._transport or self._request_http
        try:
            value = transport(method, resource, query, body)
        except TransferError:
            raise
        except Exception as error:
            raise TransferError(str(error)) from error
        if not isinstance(value, dict):
            raise TransferError("Google API devolvió un objeto inválido")
        return value

    def get_dataset(self, project: str, dataset_id: str) -> dict[str, Any]:
        if not project or not dataset_id:
            raise TransferError("project y dataset_id son obligatorios")
        return self.request("GET", f"projects/{project}/datasets/{dataset_id}", {}, None)

    def ensure_dataset(
        self,
        project: str,
        dataset_id: str,
        location: str,
        labels: dict[str, str],
    ) -> dict[str, Any]:
        if project != self.allowed_write_project:
            raise TransferError("El dataset debe estar en el proyecto destino permitido")
        try:
            existing = self.get_dataset(project, dataset_id)
        except TransferNotFound:
            return self.request(
                "POST",
                f"projects/{project}/datasets",
                {},
                {
                    "datasetReference": {"projectId": project, "datasetId": dataset_id},
                    "location": location,
                    "labels": dict(labels),
                },
            )
        existing_location = existing.get("location")
        if existing_location and str(existing_location).lower() != location.lower():
            raise TransferError(
                f"El dataset existente está en {existing_location}, no en {location}"
            )
        return existing

    def create_scheduled_query(
        self,
        *,
        project: str,
        location: str,
        display_name: str,
        query: str,
        destination_dataset: str,
        destination_table: str,
        schedule: str,
        write_disposition: str = "WRITE_APPEND",
        disabled: bool = True,
        service_account_name: Optional[str] = None,
    ) -> dict[str, Any]:
        if project != self.allowed_write_project:
            raise TransferError("La TransferConfig debe estar en el proyecto destino permitido")
        if not str(display_name or "").strip() or "\n" in display_name or "\r" in display_name:
            raise TransferError("display_name es obligatorio y debe ser de una sola línea")
        if not str(query or "").strip():
            raise TransferError("query no puede estar vacío")
        spec = ScheduleSpec.from_values(
            schedule=schedule,
            location=location,
            destination_dataset=destination_dataset,
            destination_table=destination_table,
            write_disposition=write_disposition,
            disabled=disabled,
        )
        body: dict[str, Any] = {
            "displayName": display_name.strip(),
            "dataSourceId": "scheduled_query",
            "destinationDatasetId": spec.destination_dataset,
            "params": {
                "query": query,
                "destination_table_name_template": spec.destination_table,
                "write_disposition": spec.write_disposition,
            },
            "schedule": spec.schedule,
            "disabled": True,
        }
        if service_account_name:
            body["serviceAccountName"] = service_account_name
        result = self.request(
            "POST",
            f"projects/{project}/locations/{spec.location}/transferConfigs",
            {},
            body,
        )
        if not isinstance(result.get("name"), str) or not result["name"]:
            raise TransferError("BigQuery Data Transfer no devolvió el nombre de la configuración")
        return result

    def get_transfer_config(self, name: str) -> dict[str, Any]:
        if not name or "transferConfigs/" not in name:
            raise TransferError("name de TransferConfig inválido")
        return self.request("GET", name, {}, None)
