from __future__ import annotations

import base64
import binascii
import json
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .catalog import ResourceRef


class DataformError(RuntimeError):
    """A Dataform request could not be completed safely."""


@dataclass(frozen=True)
class ExportedAsset:
    resource: ResourceRef
    filename: str
    content: bytes
    metadata: dict[str, Any]
    head_commit: str


Transport = Callable[[str, str, dict[str, Any], Optional[dict[str, Any]]], dict[str, Any]]


class TokenProvider:
    def __init__(self, account: str) -> None:
        self.account = account
        self._token = ""
        self._created_at = 0.0

    def get(self) -> str:
        if self._token and time.monotonic() - self._created_at < 3000:
            return self._token
        completed = subprocess.run(
            ["gcloud", f"--account={self.account}", "auth", "print-access-token"],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            raise DataformError("No fue posible obtener un token de Dataform")
        self._token = completed.stdout.strip()
        self._created_at = time.monotonic()
        return self._token


class DataformClient:
    api_root = "https://dataform.googleapis.com/v1"

    def __init__(
        self,
        account: str,
        allowed_write_project: str,
        *,
        source_project: str = "",
        transport: Optional[Transport] = None,
    ) -> None:
        self.account = account
        self.allowed_write_project = allowed_write_project
        self.source_project = source_project
        self._transport = transport
        self._tokens = TokenProvider(account)

    def _request_http(
        self,
        method: str,
        resource: str,
        query: dict[str, Any],
        body: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        url = f"{self.api_root}/{resource.lstrip('/')}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None
        headers = {"Authorization": f"Bearer {self._tokens.get()}", "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise DataformError(f"Dataform {method} {resource}: {error.code} {detail}") from error
        except urllib.error.URLError as error:
            raise DataformError(f"No fue posible conectar con Dataform: {error}") from error
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError as error:
            raise DataformError("Dataform no devolvió JSON válido") from error

    @staticmethod
    def _project_from_resource(resource: str) -> str:
        match = re.search(r"(?:^|/)projects/([^/]+)/", resource)
        if not match:
            raise DataformError(f"No se pudo identificar el proyecto en {resource}")
        return match.group(1)

    def request(
        self,
        method: str,
        resource: str,
        query: dict[str, Any],
        body: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        project = self._project_from_resource(resource)
        if method.upper() in {"POST", "PATCH", "PUT", "DELETE"}:
            if project == self.source_project:
                raise DataformError("SEGURIDAD: no se permiten escrituras en el proyecto origen")
            if project != self.allowed_write_project:
                raise DataformError(
                    f"SEGURIDAD: escritura fuera del proyecto destino {self.allowed_write_project}"
                )
        transport = self._transport or self._request_http
        return transport(method.upper(), resource, query, body)

    def get_repository(self, resource: ResourceRef) -> dict[str, Any]:
        value = self.request("GET", resource.name, {}, None)
        if not isinstance(value, dict):
            raise DataformError("Dataform devolvió un repositorio inválido")
        return value

    def list_files(self, repository: str) -> list[str]:
        pending: list[tuple[str, str]] = [("", "")]
        files: set[str] = set()
        while pending:
            directory, page_token = pending.pop()
            query: dict[str, Any] = {"pageSize": 1000}
            if directory:
                query["path"] = directory
            if page_token:
                query["pageToken"] = page_token
            response = self.request("GET", f"{repository}:queryDirectoryContents", query, None)
            for entry in response.get("directoryEntries", []):
                if not isinstance(entry, dict):
                    continue
                if isinstance(entry.get("file"), str):
                    files.add(entry["file"])
                elif isinstance(entry.get("directory"), str):
                    pending.append(entry["directory"])
            if response.get("nextPageToken"):
                pending.append((directory, str(response["nextPageToken"])))
        return sorted(files)

    def read_file(self, repository: str, path: str) -> bytes:
        response = self.request("GET", f"{repository}:readFile", {"path": path}, None)
        contents = response.get("contents")
        if not isinstance(contents, str):
            raise DataformError(f"Dataform no devolvió contenido para {path}")
        try:
            return base64.b64decode(contents, validate=True)
        except (ValueError, binascii.Error) as error:
            raise DataformError(f"Contenido base64 inválido para {path}") from error

    def latest_commit(self, repository: str) -> str:
        response = self.request("GET", f"{repository}:fetchHistory", {"pageSize": 1}, None)
        commits = response.get("commits") or []
        if not commits or not isinstance(commits[0], dict) or not commits[0].get("commitSha"):
            raise DataformError("Dataform no devolvió el commit actual")
        return str(commits[0]["commitSha"])

    def export(self, resource: ResourceRef) -> ExportedAsset:
        metadata = self.get_repository(resource)
        files = self.list_files(resource.name)
        if resource.kind == "notebook":
            candidates = [path for path in files if path.lower().endswith(".ipynb")]
        else:
            candidates = [path for path in files if path.lower().endswith((".sql", ".sqlx"))]
        if not candidates:
            raise DataformError(f"No se encontró archivo de código en {resource.name}")
        filename = "content.ipynb" if "content.ipynb" in candidates else (
            "content.sql" if "content.sql" in candidates else candidates[0]
        )
        return ExportedAsset(
            resource=resource,
            filename=filename,
            content=self.read_file(resource.name, filename),
            metadata=metadata,
            head_commit=self.latest_commit(resource.name),
        )

    def create_copy(
        self,
        *,
        source: ExportedAsset,
        destination_project: str,
        destination_repository_id: str,
        display_name: str,
        content: bytes,
        author_name: str,
        author_email: str,
    ) -> dict[str, str]:
        if destination_project != self.allowed_write_project:
            raise DataformError("El proyecto destino no coincide con la política del cliente")
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", destination_repository_id):
            raise DataformError("repository_id inválido")
        location = source.resource.location
        repository = f"projects/{destination_project}/locations/{location}/repositories/{destination_repository_id}"
        labels = dict(source.metadata.get("labels") or {})
        labels["single-file-asset-type"] = "notebook" if source.resource.kind == "notebook" else "sql"
        self.request(
            "POST",
            f"projects/{destination_project}/locations/{location}/repositories",
            {"repositoryId": destination_repository_id},
            {"displayName": display_name, "labels": labels, "setAuthenticatedUserAdmin": True},
        )
        response = self.request(
            "POST",
            f"{repository}:commit",
            {},
            {
                "commitMetadata": {
                    "author": {"name": author_name, "emailAddress": author_email},
                    "commitMessage": "QueryFlow pilot copy",
                },
                "fileOperations": {
                    source.filename: {"writeFile": {"contents": base64.b64encode(content).decode("ascii")}}
                },
            },
        )
        commit_sha = response.get("commitSha")
        if not isinstance(commit_sha, str) or not commit_sha:
            raise DataformError("Dataform no devolvió el SHA de la copia")
        return {"repository": repository, "commit_sha": commit_sha, "filename": source.filename}

    def update_file(
        self,
        repository: str,
        filename: str,
        content: bytes,
        *,
        required_head_commit: str,
        author_name: str,
        author_email: str,
    ) -> dict[str, str]:
        """Write exactly one file to an existing repository head safely."""
        if not required_head_commit:
            raise DataformError("El commit esperado es obligatorio para actualizar")
        response = self.request(
            "POST",
            f"{repository}:commit",
            {},
            {
                "requiredHeadCommitSha": required_head_commit,
                "commitMetadata": {
                    "author": {"name": author_name, "emailAddress": author_email},
                    "commitMessage": "QueryFlow update existing notebook",
                },
                "fileOperations": {
                    filename: {"writeFile": {"contents": base64.b64encode(content).decode("ascii")}}
                },
            },
        )
        commit_sha = response.get("commitSha")
        if not isinstance(commit_sha, str) or not commit_sha:
            raise DataformError("Dataform no devolvió el SHA de la actualización")
        return {"repository": repository, "commit_sha": commit_sha, "filename": filename}
