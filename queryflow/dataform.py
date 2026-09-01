from __future__ import annotations

import base64
import binascii
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .catalog import ResourceRef
from .gcloud import GcloudContext


class DataformError(RuntimeError):
    """A Dataform request could not be completed safely."""


class DataformRateLimitError(DataformError):
    """Dataform rejected a request because a rate quota was exhausted."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


@dataclass
class DataformRequestStats:
    """Counters for one client run; no query or row content is retained."""

    requests_attempted: int = 0
    requests_succeeded: int = 0
    retries: int = 0
    rate_limit_responses: int = 0
    rate_limit_wait_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests_attempted": self.requests_attempted,
            "requests_succeeded": self.requests_succeeded,
            "retries": self.retries,
            "rate_limit_responses": self.rate_limit_responses,
            "rate_limit_wait_seconds": round(self.rate_limit_wait_seconds, 3),
        }


class DataformRateLimiter:
    """A per-client sliding-window limiter for Dataform requests."""

    def __init__(
        self,
        requests_per_minute: int | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if requests_per_minute is not None and requests_per_minute <= 0:
            raise ValueError("requests_per_minute debe ser mayor que cero")
        self.requests_per_minute = requests_per_minute
        self._clock = clock
        self._sleeper = sleeper
        self._events: deque[float] = deque()

    def wait(self, stats: DataformRequestStats | None = None) -> None:
        if self.requests_per_minute is None:
            return
        while True:
            now = self._clock()
            cutoff = now - 60.0
            while self._events and self._events[0] <= cutoff:
                self._events.popleft()
            if len(self._events) < self.requests_per_minute:
                self._events.append(now)
                return
            delay = max(0.0, 60.0 - (now - self._events[0]))
            self._sleeper(delay)
            if stats is not None:
                stats.rate_limit_wait_seconds += delay


@dataclass(frozen=True)
class ExportedAsset:
    resource: ResourceRef
    filename: str
    content: bytes
    metadata: dict[str, Any]
    head_commit: str


Transport = Callable[[str, str, dict[str, Any], Optional[dict[str, Any]]], dict[str, Any]]


class TokenProvider:
    def __init__(self, account: str, *, context: Optional[GcloudContext] = None) -> None:
        self.account = account
        self.context = context or GcloudContext.default(account=account)
        self._token = ""
        self._created_at = 0.0

    def get(self) -> str:
        if self._token and time.monotonic() - self._created_at < 3000:
            return self._token
        completed = self.context.run(["auth", "print-access-token"], account=self.account)
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
        gcloud_context: Optional[GcloudContext] = None,
        transport: Optional[Transport] = None,
        request_timeout_seconds: float = 180.0,
        requests_per_minute: int | None = None,
        max_retries: int = 5,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds debe ser mayor que cero")
        if max_retries < 0:
            raise ValueError("max_retries no puede ser negativo")
        self.account = account
        self.allowed_write_project = allowed_write_project
        self.source_project = source_project
        self._transport = transport
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.max_retries = int(max_retries)
        self.request_stats = DataformRequestStats()
        self._sleeper = sleeper
        self._rate_limiter = DataformRateLimiter(
            requests_per_minute,
            clock=clock,
            sleeper=sleeper,
        )
        self._tokens = TokenProvider(account, context=gcloud_context)

    def set_gcloud_context(self, context: GcloudContext) -> None:
        """Update the credential context without changing the client API."""
        self._tokens.context = context

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
            with urllib.request.urlopen(request, timeout=self.request_timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            if error.code == 429:
                retry_after: float | None = None
                raw_retry_after = error.headers.get("Retry-After") if error.headers else None
                if raw_retry_after:
                    try:
                        retry_after = max(0.0, float(raw_retry_after))
                    except ValueError:
                        retry_after = None
                raise DataformRateLimitError(
                    f"Dataform {method} {resource}: {error.code} {detail}",
                    retry_after_seconds=retry_after,
                ) from error
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
        method = method.upper()
        transport = self._transport or self._request_http
        retry_count = 0
        while True:
            # The quota is defined over Dataform API requests, not only reads.
            # Writes are still never retried, but they share the same pacing
            # window so a campaign cannot burst past the project limit.
            self._rate_limiter.wait(self.request_stats)
            self.request_stats.requests_attempted += 1
            try:
                response = transport(method, resource, query, body)
            except DataformRateLimitError as error:
                self.request_stats.rate_limit_responses += 1
                if method != "GET" or retry_count >= self.max_retries:
                    raise
                retry_count += 1
                self.request_stats.retries += 1
                delay = error.retry_after_seconds
                if delay is None:
                    delay = min(5.0 * (2 ** (retry_count - 1)), 60.0)
                self._sleeper(delay)
                self.request_stats.rate_limit_wait_seconds += delay
                continue
            self.request_stats.requests_succeeded += 1
            return response

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
                    # Keep the pagination state shape consistent with the
                    # initial queue entry.  Nested notebooks often expose
                    # directories; appending the bare string makes the next
                    # pop fail while unpacking ``(directory, page_token)``.
                    pending.append((entry["directory"], ""))
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

    def delete_repository(self, repository: str, *, force: bool = False) -> dict[str, Any]:
        """Delete one explicitly approved destination repository.

        QueryFlow never uses Dataform's cascading ``force`` deletion.  The
        migration cleanup command passes ``force=False`` and verifies the
        exact repository/head before reaching this method.
        """
        if force:
            raise DataformError("La limpieza de QueryFlow no permite force=true")
        project = self._project_from_resource(repository)
        if project != self.allowed_write_project:
            raise DataformError("SEGURIDAD: solo se pueden limpiar repositorios del proyecto destino")
        return self.request("DELETE", repository, {"force": "false"}, None)
