"""Copy-only migration workflow for BigQuery stored procedures and routines.

This module uses the BigQuery Routines REST resource instead of submitting SQL
jobs. Definitions are transformed in memory, reviewed through an immutable
manifest, and inserted only after an exact digest approval. A procedure is
never called and an existing destination routine is never overwritten.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .gcloud import GcloudContext
from .migration import RouteDictionary, rewrite_text


ROUTINE_SCHEMA_VERSION = 1
ROUTINE_BATCH_SIZE = 20
ROUTINE_REQUESTS_PER_MINUTE = 120
ROUTINE_MAX_REQUESTS_PER_MINUTE = 300
ROUTINE_DESTINATION_DATASET = "functions"
ROUTINE_KINDS = (
    "PROCEDURE",
    "FUNCTION",
    "SCALAR_FUNCTION",
    "TABLE FUNCTION",
    "TABLE_VALUED_FUNCTION",
    "AGGREGATE FUNCTION",
    "AGGREGATE_FUNCTION",
)
SUPPORTED_DEPENDENCY_LANGUAGES = frozenset({"SQL", "JAVASCRIPT"})
SEMANTIC_FIELDS = (
    "routineType",
    "language",
    "arguments",
    "dataType",
    "definitionBody",
    "description",
    "strictMode",
    "importedLibraries",
    "remoteFunctionOptions",
    "sparkOptions",
    "determinismLevel",
    "returnTableType",
)
_OUTPUT_FIELDS = frozenset(
    {"etag", "creationTime", "lastModifiedTime", "selfLink", "id"}
)


class RoutineError(RuntimeError):
    """A BigQuery routine migration operation could not proceed safely."""

    def __init__(self, message: str, *, kind: str = "routine") -> None:
        super().__init__(message)
        self.kind = kind


class RoutineRateLimitError(RoutineError):
    """BigQuery throttled a read request."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message, kind="rate_limit")
        self.retry_after_seconds = retry_after_seconds


class RoutineTransientError(RoutineError):
    """A temporary provider response that is safe to retry for reads."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message, kind="transient")
        self.retry_after_seconds = retry_after_seconds


@dataclass
class RoutineRequestStats:
    requests_attempted: int = 0
    requests_succeeded: int = 0
    retries: int = 0
    rate_limit_responses: int = 0
    transient_responses: int = 0
    rate_limit_wait_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests_attempted": self.requests_attempted,
            "requests_succeeded": self.requests_succeeded,
            "retries": self.retries,
            "rate_limit_responses": self.rate_limit_responses,
            "transient_responses": self.transient_responses,
            "rate_limit_wait_seconds": round(self.rate_limit_wait_seconds, 3),
        }


class RoutineRateLimiter:
    """Local pacing guard; it is not a claim about a Google quota."""

    def __init__(
        self,
        requests_per_minute: int | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if requests_per_minute is not None and not 1 <= int(requests_per_minute) <= ROUTINE_MAX_REQUESTS_PER_MINUTE:
            raise ValueError(
                f"requests_per_minute debe estar entre 1 y {ROUTINE_MAX_REQUESTS_PER_MINUTE}"
            )
        self.requests_per_minute = int(requests_per_minute) if requests_per_minute is not None else None
        self._clock = clock
        self._sleeper = sleeper
        self._events: deque[float] = deque()

    def wait(self, stats: RoutineRequestStats | None = None) -> None:
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


RoutineTransport = Callable[
    [str, str, dict[str, Any], Optional[dict[str, Any]]], dict[str, Any]
]


class _RoutineTokenProvider:
    def __init__(self, account: str, context: GcloudContext) -> None:
        self.account = account
        self.context = context
        self._token = ""
        self._created_at = 0.0

    def get(self) -> str:
        if self._token and time.monotonic() - self._created_at < 3000:
            return self._token
        completed = self.context.run(["auth", "print-access-token"], account=self.account)
        if completed.returncode != 0 or not completed.stdout.strip():
            detail = completed.stderr.strip() or "No se pudo obtener un token de Google Cloud"
            raise RoutineError(detail, kind="authentication")
        self._token = completed.stdout.strip()
        self._created_at = time.monotonic()
        return self._token


class BigQueryRoutineClient:
    """Minimal REST client with destination-only writes and read retries."""

    api_root = "https://bigquery.googleapis.com/bigquery/v2"

    def __init__(
        self,
        account: str,
        allowed_write_project: str,
        *,
        source_project: str = "",
        gcloud_context: GcloudContext | None = None,
        transport: RoutineTransport | None = None,
        request_timeout_seconds: float = 180.0,
        requests_per_minute: int | None = ROUTINE_REQUESTS_PER_MINUTE,
        max_retries: int = 4,
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
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.max_retries = int(max_retries)
        self._transport = transport
        self.request_stats = RoutineRequestStats()
        self._sleeper = sleeper
        self._limiter = RoutineRateLimiter(
            requests_per_minute, clock=clock, sleeper=sleeper
        )
        self._tokens = _RoutineTokenProvider(
            account, gcloud_context or GcloudContext.default(account=account)
        )

    def set_gcloud_context(self, context: GcloudContext) -> None:
        self._tokens.context = context

    @staticmethod
    def _project_from_path(resource: str) -> str:
        parts = resource.strip("/").split("/")
        try:
            return parts[parts.index("projects") + 1]
        except (ValueError, IndexError) as error:
            raise RoutineError(
                f"Ruta de BigQuery inválida: {resource}", kind="request"
            ) from error

    def _request_http(
        self,
        method: str,
        resource: str,
        query: dict[str, Any],
        body: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        url = f"{self.api_root}/{resource.strip('/')}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = (
            json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if body is not None
            else None
        )
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._tokens.get()}",
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:1000]
            if error.code in {429, 408, 500, 502, 503, 504}:
                raw_retry_after = error.headers.get("Retry-After") if error.headers else None
                try:
                    retry_after = float(raw_retry_after) if raw_retry_after else None
                except ValueError:
                    retry_after = None
                error_type = RoutineRateLimitError if error.code == 429 else RoutineTransientError
                raise error_type(
                    f"BigQuery {method} {resource}: HTTP {error.code} {detail}",
                    retry_after_seconds=retry_after,
                ) from error
            kind = (
                "not_found"
                if error.code == 404
                else "permission"
                if error.code in {401, 403}
                else "http"
            )
            raise RoutineError(
                f"BigQuery {method} {resource}: HTTP {error.code} {detail}", kind=kind
            ) from error
        except urllib.error.URLError as error:
            raise RoutineError(
                f"No se pudo conectar con BigQuery: {error}", kind="network"
            ) from error
        try:
            value = json.loads(raw.decode("utf-8") if raw else "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RoutineError(
                "BigQuery no devolvió JSON válido", kind="transport"
            ) from error
        if not isinstance(value, dict):
            raise RoutineError(
                "BigQuery devolvió una respuesta inválida", kind="transport"
            )
        return value

    def request(
        self,
        method: str,
        resource: str,
        query: dict[str, Any] | None = None,
        body: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        method = str(method).upper()
        query = dict(query or {})
        project = self._project_from_path(resource)
        if method not in {"GET", "POST"}:
            raise RoutineError(
                f"Método no permitido para rutinas: {method}", kind="policy"
            )
        if method == "POST" and project != self.allowed_write_project:
            raise RoutineError(
                f"La escritura de rutinas solo está permitida en el proyecto destino {self.allowed_write_project}",
                kind="policy",
            )
        retry_count = 0
        while True:
            self._limiter.wait(self.request_stats)
            self.request_stats.requests_attempted += 1
            try:
                value = (self._transport or self._request_http)(
                    method, resource, query, body
                )
            except (RoutineRateLimitError, RoutineTransientError) as error:
                if isinstance(error, RoutineRateLimitError):
                    self.request_stats.rate_limit_responses += 1
                else:
                    self.request_stats.transient_responses += 1
                if method != "GET" or retry_count >= self.max_retries:
                    raise
                retry_count += 1
                self.request_stats.retries += 1
                delay = error.retry_after_seconds
                if delay is None:
                    delay = min(2.0 * (2 ** (retry_count - 1)), 30.0)
                self._sleeper(max(0.0, delay))
                self.request_stats.rate_limit_wait_seconds += max(0.0, delay)
                continue
            except RoutineError:
                raise
            self.request_stats.requests_succeeded += 1
            return value

    def get_dataset(self, project: str, dataset: str) -> dict[str, Any]:
        project = _validate_path_segment(project, "proyecto")
        dataset = _validate_path_segment(dataset, "dataset")
        return self.request("GET", f"projects/{project}/datasets/{dataset}")

    def list_datasets(self, project: str) -> list[dict[str, Any]]:
        project = _validate_path_segment(project, "proyecto")
        values: list[dict[str, Any]] = []
        token = ""
        while True:
            query: dict[str, Any] = {"maxResults": 1000}
            if token:
                query["pageToken"] = token
            response = self.request("GET", f"projects/{project}/datasets", query)
            datasets = response.get("datasets") or []
            if isinstance(datasets, list):
                values.extend(item for item in datasets if isinstance(item, dict))
            token = str(response.get("nextPageToken") or "")
            if not token:
                return values

    def list_routines(self, project: str, dataset: str) -> list[dict[str, Any]]:
        project = _validate_path_segment(project, "proyecto")
        dataset = _validate_path_segment(dataset, "dataset")
        values: list[dict[str, Any]] = []
        token = ""
        while True:
            query: dict[str, Any] = {"maxResults": 1000}
            if token:
                query["pageToken"] = token
            response = self.request(
                "GET", f"projects/{project}/datasets/{dataset}/routines", query
            )
            routines = response.get("routines") or []
            if isinstance(routines, list):
                values.extend(item for item in routines if isinstance(item, dict))
            token = str(response.get("nextPageToken") or "")
            if not token:
                return values

    def get_routine(self, project: str, dataset: str, routine_id: str) -> dict[str, Any]:
        project = _validate_path_segment(project, "proyecto")
        dataset = _validate_path_segment(dataset, "dataset")
        routine_id = _validate_path_segment(routine_id, "identificador de rutina")
        value = self.request(
            "GET", f"projects/{project}/datasets/{dataset}/routines/{routine_id}"
        )
        if not isinstance(value, dict):
            raise RoutineError("BigQuery devolvió una rutina inválida", kind="transport")
        return value

    def insert_routine(
        self, project: str, dataset: str, routine: Mapping[str, Any]
    ) -> dict[str, Any]:
        if project != self.allowed_write_project:
            raise RoutineError(
                "El proyecto destino no coincide con la política del cliente", kind="policy"
            )
        project = _validate_path_segment(project, "proyecto")
        dataset = _validate_path_segment(dataset, "dataset")
        payload = copy.deepcopy(dict(routine))
        reference = payload.get("routineReference") or {}
        if (
            not isinstance(reference, dict)
            or str(reference.get("routineId") or "").strip() == ""
        ):
            raise RoutineError(
                "La rutina a crear no tiene routineReference.routineId", kind="request"
            )
        routine_id = _validate_path_segment(reference["routineId"], "identificador de rutina")
        payload["routineReference"] = {
            "projectId": project,
            "datasetId": dataset,
            "routineId": routine_id,
        }
        for key in _OUTPUT_FIELDS:
            payload.pop(key, None)
        return self.request(
            "POST", f"projects/{project}/datasets/{dataset}/routines", {}, payload
        )


def _json_canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256(_json_canonical(value))


def _validate_path_segment(value: Any, label: str) -> str:
    """Validate a BigQuery REST path segment before interpolating it."""
    value = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise RoutineError(f"El {label} contiene caracteres inválidos", kind="invalid")
    return value


def _routine_reference(routine: Mapping[str, Any]) -> dict[str, str]:
    reference = routine.get("routineReference") or {}
    if not isinstance(reference, Mapping):
        raise RoutineError("routineReference no es un objeto", kind="invalid")
    project = str(reference.get("projectId") or "").strip()
    dataset = str(reference.get("datasetId") or "").strip()
    routine_id = str(reference.get("routineId") or "").strip()
    if not project or not dataset or not routine_id:
        raise RoutineError(
            "routineReference requiere projectId, datasetId y routineId", kind="invalid"
        )
    return {"projectId": project, "datasetId": dataset, "routineId": routine_id}


def routine_resource_name(project: str, dataset: str, routine_id: str) -> str:
    return f"projects/{project}/datasets/{dataset}/routines/{routine_id}"


def routine_semantic_payload(
    routine: Mapping[str, Any],
    *,
    project: str | None = None,
    dataset: str | None = None,
    routine_id: str | None = None,
    definition_body: str | None = None,
) -> dict[str, Any]:
    """Return only fields that affect the routine's behavior and signature."""
    reference = _routine_reference(routine)
    result: dict[str, Any] = {
        "routineReference": {
            "projectId": project or reference["projectId"],
            "datasetId": dataset or reference["datasetId"],
            "routineId": routine_id or reference["routineId"],
        }
    }
    for field_name in SEMANTIC_FIELDS:
        if field_name in routine:
            result[field_name] = copy.deepcopy(routine[field_name])
    if definition_body is not None:
        result["definitionBody"] = definition_body
    return result


def _routine_type(routine: Mapping[str, Any]) -> str:
    return str(routine.get("routineType") or "").strip().upper()


def _routine_language(routine: Mapping[str, Any]) -> str:
    return str(routine.get("language") or "").strip().upper()


def _is_supported_procedure(routine: Mapping[str, Any]) -> bool:
    return (
        _routine_type(routine) == "PROCEDURE"
        and _routine_language(routine) == "SQL"
        and not routine.get("sparkOptions")
    )


def _is_supported_dependency(routine: Mapping[str, Any]) -> bool:
    """Return whether a function can be copied when referenced by a procedure."""
    return (
        _routine_type(routine)
        in {
            "FUNCTION",
            "SCALAR_FUNCTION",
            "TABLE FUNCTION",
            "TABLE_VALUED_FUNCTION",
            "AGGREGATE FUNCTION",
            "AGGREGATE_FUNCTION",
        }
        and _routine_language(routine) in SUPPORTED_DEPENDENCY_LANGUAGES
        and not routine.get("sparkOptions")
    )


@dataclass(frozen=True)
class RoutineSnapshot:
    project: str
    dataset: str
    location: str
    routine_id: str
    routine: dict[str, Any]
    source_name: str
    source_sha256: str

    @classmethod
    def from_routine(
        cls,
        project: str,
        dataset: str,
        location: str,
        routine: Mapping[str, Any],
    ) -> "RoutineSnapshot":
        reference = _routine_reference(routine)
        if reference["projectId"] != project or reference["datasetId"] != dataset:
            raise RoutineError(
                "La referencia de la rutina no coincide con su contenedor", kind="invalid"
            )
        routine_id = reference["routineId"]
        return cls(
            project=project,
            dataset=dataset,
            location=str(location),
            routine_id=routine_id,
            routine=copy.deepcopy(dict(routine)),
            source_name=routine_resource_name(project, dataset, routine_id),
            source_sha256=_sha256_json(routine_semantic_payload(routine)),
        )

    def to_dict(self, *, include_body: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "name": self.source_name,
            "project": self.project,
            "dataset": self.dataset,
            "location": self.location,
            "routine_id": self.routine_id,
            "routine_type": _routine_type(self.routine),
            "language": _routine_language(self.routine),
            "source_sha256": self.source_sha256,
        }
        if include_body:
            value["routine"] = copy.deepcopy(self.routine)
        return value


def _source_location(dataset: Mapping[str, Any], default: str = "") -> str:
    reference = dataset.get("datasetReference") or {}
    return str(
        dataset.get("location")
        or (reference.get("location") if isinstance(reference, Mapping) else "")
        or default
    ).strip()


def inventory_routine_snapshots(
    client: BigQueryRoutineClient,
    source_project: str,
    *,
    source_datasets: list[str] | tuple[str, ...] | None = None,
    default_location: str = "",
    access_report: list[dict[str, Any]] | None = None,
) -> tuple[list[RoutineSnapshot], list[dict[str, Any]]]:
    """Read every routine definition without submitting a query job."""
    datasets: list[tuple[str, str]] = []
    dataset_metadata: dict[str, Mapping[str, Any]] = {}
    if source_datasets:
        for dataset in source_datasets:
            metadata = client.get_dataset(source_project, dataset)
            dataset_metadata[dataset] = metadata
            datasets.append((dataset, _source_location(metadata, default_location)))
    else:
        for item in client.list_datasets(source_project):
            reference = item.get("datasetReference") or {}
            dataset = str(
                reference.get("datasetId") or item.get("id", "").split(".")[-1]
            ).strip()
            if dataset:
                datasets.append((dataset, _source_location(item, default_location)))
    snapshots: list[RoutineSnapshot] = []
    errors: list[dict[str, Any]] = []
    for dataset, location in sorted(set(datasets)):
        if access_report is not None and callable(getattr(client, "get_dataset", None)):
            try:
                metadata = dataset_metadata.get(dataset)
                if metadata is None:
                    metadata = client.get_dataset(source_project, dataset)
                    dataset_metadata[dataset] = metadata
                location = _source_location(metadata, location)
                _append_dataset_access(
                    access_report,
                    project=source_project,
                    dataset=dataset,
                    location=_source_location(metadata, location),
                    metadata=metadata,
                )
            except RoutineError as error:
                # Access evidence is informative.  A failure to read it must
                # not hide an otherwise readable routine, but it is retained
                # as an inventory incident for the analyst.
                access_report.append(
                    {
                        "project": source_project,
                        "dataset": dataset,
                        "location": location,
                        "status": "unavailable",
                        "error_kind": error.kind,
                        "error": str(error)[:500],
                    }
                )
        try:
            summaries = client.list_routines(source_project, dataset)
        except RoutineError as error:
            errors.append(
                {"kind": error.kind, "dataset": dataset, "message": str(error)[:500]}
            )
            continue
        for summary in summaries:
            try:
                reference = _routine_reference(summary)
                full = client.get_routine(
                    source_project, dataset, reference["routineId"]
                )
                snapshots.append(
                    RoutineSnapshot.from_routine(
                        source_project, dataset, location, full
                    )
                )
            except RoutineError as error:
                errors.append(
                    {
                        "kind": error.kind,
                        "dataset": dataset,
                        "routine": str(
                            (summary.get("routineReference") or {}).get("routineId")
                            or summary.get("id")
                            or ""
                        ),
                        "message": str(error)[:500],
                    }
                )
    return snapshots, errors


def _append_dataset_access(
    report: list[dict[str, Any]],
    *,
    project: str,
    dataset: str,
    location: str,
    metadata: Mapping[str, Any],
) -> None:
    """Add bounded, non-mutating dataset ACL evidence to a campaign report."""
    raw_access = metadata.get("access")
    entries: list[dict[str, Any]] = []
    if isinstance(raw_access, list):
        for raw in raw_access[:200]:
            if not isinstance(raw, Mapping):
                continue
            # BigQuery dataset ACL entries contain principals and roles, not
            # credentials.  Keep only the fields useful to a reviewer and
            # avoid copying arbitrary metadata into an approval artifact.
            entry = {
                key: str(raw[key])[:500]
                for key in ("role", "entityType", "entityId", "view", "routine")
                if raw.get(key) is not None
            }
            if entry:
                entries.append(entry)
    report.append(
        {
            "project": project,
            "dataset": dataset,
            "location": location,
            "status": "read",
            "access": entries,
            "access_count": len(entries),
        }
    )


def build_workbench_routine_code(
    method: str,
    resource: str,
    query: Mapping[str, Any] | None,
    body: Mapping[str, Any] | None,
    *,
    account: str,
    gcloud_config_dir: str | None = None,
) -> str:
    """Build code for one REST call executed inside an ephemeral kernel.

    The kernel invokes the BigQuery REST API directly. It never submits a SQL
    query or invokes a routine. The response marker is parsed and verified by
    the local client.
    """
    payload = json.dumps(
        {
            "method": str(method).upper(),
            "resource": str(resource).strip("/"),
            "query": dict(query or {}),
            "body": dict(body) if body is not None else None,
            "account": account,
            "gcloud_config_dir": str(gcloud_config_dir or ""),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    encoded = repr(payload)
    return f"""
import json
import os
import subprocess
import urllib.parse
import urllib.request
import urllib.error

payload = json.loads({encoded})
gcloud_environment = os.environ.copy()
if payload.get("gcloud_config_dir") and os.path.isdir(payload["gcloud_config_dir"]):
    gcloud_environment["CLOUDSDK_CONFIG"] = payload["gcloud_config_dir"]
# Workbench normally authenticates to BigQuery with its attached service
# account through Application Default Credentials (ADC).  The Cloud Shell
# user's gcloud profile is only needed to open the Jupyter proxy; it is not
# necessarily present inside the Workbench VM.  Keep ADC isolated from the
# Cloud Shell config and use the explicit account only as a fallback.
token_process = subprocess.run(
    ["gcloud", "auth", "application-default", "print-access-token"],
    capture_output=True,
    text=True,
    check=False,
    env=os.environ.copy(),
)
if token_process.returncode != 0 or not token_process.stdout.strip():
    token_process = subprocess.run(
        ["gcloud", "--account=" + payload["account"], "auth", "print-access-token"],
        capture_output=True,
        text=True,
        check=False,
        env=gcloud_environment,
    )
if token_process.returncode != 0 or not token_process.stdout.strip():
    print("QUERYFLOW_ROUTINE=" + json.dumps({{"ok": False, "kind": "authentication", "error": "No se pudo obtener el token"}}, sort_keys=True))
else:
    url = "https://bigquery.googleapis.com/bigquery/v2/" + payload["resource"]
    if payload["query"]:
        url += "?" + urllib.parse.urlencode(payload["query"])
    data = json.dumps(payload["body"], ensure_ascii=False).encode("utf-8") if payload["body"] is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=payload["method"],
        headers={{"Authorization": "Bearer " + token_process.stdout.strip(), "Accept": "application/json", "Content-Type": "application/json"}},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read()
        result = {{"ok": True, "response": json.loads(raw.decode("utf-8") if raw else "{{}}")}}
    except urllib.error.HTTPError as error:
        result = {{"ok": False, "kind": "not_found" if error.code == 404 else "permission" if error.code in (401, 403) else "rate_limit" if error.code == 429 else "transient" if error.code in (408, 500, 502, 503, 504) else "http", "status": error.code}}
    except Exception:
        result = {{"ok": False, "kind": "network"}}
    print("QUERYFLOW_ROUTINE=" + json.dumps(result, ensure_ascii=False, sort_keys=True))
"""


def parse_workbench_routine_output(output: str) -> dict[str, Any]:
    marker = "QUERYFLOW_ROUTINE="
    line = next((item for item in str(output).splitlines() if item.startswith(marker)), None)
    if line is None:
        raise RoutineError("Workbench no devolvió el marker QUERYFLOW_ROUTINE", kind="transport")
    try:
        value = json.loads(line[len(marker) :])
    except json.JSONDecodeError as error:
        raise RoutineError("Workbench devolvió JSON inválido para la rutina", kind="transport") from error
    if not isinstance(value, dict):
        raise RoutineError("Workbench devolvió una respuesta inválida", kind="transport")
    if not value.get("ok"):
        kind = str(value.get("kind") or "transport")
        message = f"Workbench no pudo consultar BigQuery ({kind})"
        if kind == "rate_limit":
            raise RoutineRateLimitError(message)
        if kind == "transient":
            raise RoutineTransientError(message)
        raise RoutineError(message, kind=kind)
    response = value.get("response")
    if not isinstance(response, dict):
        raise RoutineError("Workbench no devolvió el recurso de rutina", kind="transport")
    return response


class WorkbenchRoutineTransport:
    """REST transport backed by a temporary Workbench Jupyter kernel."""

    def __init__(self, settings: Any, *, account: str, gcloud_context: GcloudContext) -> None:
        self.settings = settings
        self.account = account
        self.gcloud_context = gcloud_context
        self._http: Any = None
        self._kernel_id: str | None = None
        self._websocket: Any = None

    def _ensure_session(self) -> None:
        if self._http is not None and self._kernel_id:
            return
        try:
            from .workbench import (
                _JupyterHttp,
                _access_token,
                discover_proxy,
            )

            token = _access_token(self.account, gcloud_context=self.gcloud_context)
            proxy = discover_proxy(
                self.settings,
                account=self.account,
                gcloud_context=self.gcloud_context,
            )
            http = _JupyterHttp(
                proxy,
                token,
                timeout=int(getattr(self.settings, "timeout_seconds", 240)),
            )
            http.prepare()
            self._kernel_id = http.create_kernel()
            self._http = http
        except Exception as error:
            kind = str(getattr(error, "kind", "") or "transport")
            raise RoutineError(f"No se pudo abrir el gateway Workbench: {error}", kind=kind) from error

    def __call__(
        self,
        method: str,
        resource: str,
        query: dict[str, Any],
        body: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        self._ensure_session()
        try:
            from .workbench import _open_workbench_websocket, _websocket_execute

            if self._websocket is None:
                self._websocket = _open_workbench_websocket(
                    self._http,
                    str(self._kernel_id),
                    timeout=int(getattr(self.settings, "timeout_seconds", 240)),
                )

            output = _websocket_execute(
                self._http,
                str(self._kernel_id),
                build_workbench_routine_code(
                    method,
                    resource,
                    query,
                    body,
                    account=self.account,
                    gcloud_config_dir=str(self.gcloud_context.config_dir),
                ),
                timeout=int(getattr(self.settings, "timeout_seconds", 240)),
                websocket=self._websocket,
            )
            return parse_workbench_routine_output(output)
        except RoutineError:
            raise
        except Exception as error:
            kind = str(getattr(error, "kind", "") or "transport")
            raise RoutineError(f"El gateway Workbench falló: {error}", kind=kind) from error

    def close(self) -> None:
        if self._websocket is not None:
            try:
                self._websocket.close(timeout=1)
            except Exception:
                pass
            self._websocket = None
        if self._http is not None and self._kernel_id:
            try:
                self._http.delete_kernel(self._kernel_id)
            except Exception:
                pass
        self._http = None
        self._kernel_id = None



_FQ_ROUTINE_RE = re.compile(
    r"(?<![A-Za-z0-9_-])[\x60]?(?P<project>[A-Za-z0-9_-]+)\.(?P<dataset>[A-Za-z0-9_-]+)\.(?P<routine>[A-Za-z0-9_-]+)[\x60]?\s*\("
)
_TWO_PART_ROUTINE_RE = re.compile(
    r"(?<![A-Za-z0-9_-])[\x60]?(?P<dataset>[A-Za-z0-9_-]+)\.(?P<routine>[A-Za-z0-9_-]+)[\x60]?\s*\("
)
_DYNAMIC_RE = re.compile(r"\bEXECUTE\s+IMMEDIATE\b", re.IGNORECASE)
_MUTATING_RE = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|MERGE|TRUNCATE|CREATE|ALTER|DROP)\b", re.IGNORECASE
)
_SECRET_RE = re.compile(
    r'''(?i)(?:bearer\s+[A-Za-z0-9._~+/=-]{12,}|ghp_[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_-]{20,}|(?:password|secret|api[_-]?key)\s*[:=]\s*['"][^'"]{8,}['"])'''
)


def _line_for_offset(source: str, offset: int) -> int:
    return source.count("\n", 0, max(0, offset)) + 1


def _finding(kind: str, *, path: str, line: int, **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "path": path, "line": line, **extra}


def _extract_dependency_refs(
    body: str,
    *,
    project: str,
    dataset: str,
    inventory: Mapping[tuple[str, str, str], RoutineSnapshot],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    refs: dict[tuple[str, str, str], dict[str, Any]] = {}
    findings: list[dict[str, Any]] = []
    for match in _FQ_ROUTINE_RE.finditer(body):
        key = (match.group("project"), match.group("dataset"), match.group("routine"))
        refs.setdefault(
            key,
            {
                "raw": match.group(0).strip(),
                "project": key[0],
                "dataset": key[1],
                "routine_id": key[2],
                "line": _line_for_offset(body, match.start()),
                "resolution": "fully_qualified",
            },
        )
    for match in _TWO_PART_ROUTINE_RE.finditer(body):
        start = match.start()
        if start and body[start - 1] == ".":
            continue
        key = (project, match.group("dataset"), match.group("routine"))
        refs.setdefault(
            key,
            {
                "raw": match.group(0).strip(),
                "project": project,
                "dataset": key[1],
                "routine_id": key[2],
                "line": _line_for_offset(body, start),
                "resolution": "dataset_qualified",
            },
        )
    call_re = re.compile(
        r"\bCALL\s+[\x60]?(?P<name>[A-Za-z0-9_.-]+)[\x60]?\s*(?:\(|$)",
        re.IGNORECASE | re.MULTILINE,
    )
    for match in call_re.finditer(body):
        name = match.group("name")
        if "." not in name:
            findings.append(
                _finding(
                    "unqualified_dependency",
                    path="definitionBody",
                    line=_line_for_offset(body, match.start()),
                    routine_id=name,
                )
            )
            continue
        parts = name.split(".")
        if len(parts) == 2:
            key = (project, parts[0], parts[1])
        elif len(parts) == 3:
            key = (parts[0], parts[1], parts[2])
        else:
            continue
        refs.setdefault(
            key,
            {
                "raw": match.group(0).strip(),
                "project": key[0],
                "dataset": key[1],
                "routine_id": key[2],
                "line": _line_for_offset(body, match.start()),
                "resolution": "call",
            },
        )
    dependencies: list[dict[str, Any]] = []
    for key, value in sorted(refs.items(), key=lambda pair: str(pair[0])):
        snapshot = inventory.get(key)
        item = dict(value)
        item["source_name"] = routine_resource_name(*key)
        if key[0] != project:
            item["status"] = "external"
            findings.append(
                _finding(
                    "external_dependency",
                    path="definitionBody",
                    line=int(value["line"]),
                    routine=item["source_name"],
                )
            )
        elif snapshot is None:
            item["status"] = "missing"
            findings.append(
                _finding(
                    "unsupported_dependency",
                    path="definitionBody",
                    line=int(value["line"]),
                    routine=item["source_name"],
                    reason="missing_from_inventory",
                )
            )
        elif (
            _routine_language(snapshot.routine) not in SUPPORTED_DEPENDENCY_LANGUAGES
            or (
                _routine_type(snapshot.routine) == "PROCEDURE"
                and not _is_supported_procedure(snapshot.routine)
            )
        ):
            item["status"] = "unsupported"
            findings.append(
                _finding(
                    "unsupported_dependency",
                    path="definitionBody",
                    line=int(value["line"]),
                    routine=item["source_name"],
                    reason="routine_type_or_language",
                )
            )
        else:
            item["status"] = "resolved"
            item["source_sha256"] = snapshot.source_sha256
        dependencies.append(item)
    return dependencies, findings


def _security_findings(body: str) -> list[dict[str, Any]]:
    return [
        _finding(
            "embedded_secret",
            path="definitionBody",
            line=_line_for_offset(body, match.start()),
            rule="credential-like literal",
        )
        for match in _SECRET_RE.finditer(body)
    ]


def _mask_secrets(value: str) -> str:
    return _SECRET_RE.sub("[REDACTED]", value)


def _mask_routine(routine: Mapping[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(routine, ensure_ascii=False))
    if isinstance(result.get("definitionBody"), str):
        result["definitionBody"] = _mask_secrets(result["definitionBody"])
    return result


def _rewrite_body(
    body: str, dictionary: RouteDictionary
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    rewritten, applied, unknown = rewrite_text(body, dictionary)
    routes = [item.to_dict() for item in applied]
    incidents = [
        _finding(
            "unknown_route",
            path="definitionBody",
            line=_line_for_offset(body, item.start),
            route=item.route,
            start=item.start,
            end=item.end,
        )
        for item in unknown
    ]
    return rewritten, routes, incidents


def _rewrite_dependency_references(
    body: str,
    dependencies: Sequence[Mapping[str, Any]],
    *,
    destination_project: str,
    destination_dataset: str,
) -> str:
    """Point resolved routine calls at the consolidated destination dataset."""
    alternatives: list[str] = []
    targets: dict[str, str] = {}
    for dependency in dependencies:
        if dependency.get("status") != "resolved":
            continue
        project = str(dependency.get("project") or "")
        dataset = str(dependency.get("dataset") or "")
        routine_id = str(dependency.get("routine_id") or "")
        if not project or not dataset or not routine_id:
            continue
        target = f"`{destination_project}.{destination_dataset}.{routine_id}`"
        full = f"{project}.{dataset}.{routine_id}"
        two_part = f"{dataset}.{routine_id}"
        # Build one substitution over the original body.  Sequential full +
        # two-part substitutions can match the destination we just emitted
        # when source and destination datasets share a name.
        targets[full] = target
        targets[two_part] = target
        alternatives.extend(
            [
                rf"(?<![A-Za-z0-9_-]){re.escape(full)}(?![A-Za-z0-9_-])",
                rf"(?<![A-Za-z0-9_-])\x60{re.escape(full)}\x60(?![A-Za-z0-9_-])",
                rf"(?<![A-Za-z0-9_.-]){re.escape(two_part)}(?![A-Za-z0-9_-])",
                rf"(?<![A-Za-z0-9_.-])\x60{re.escape(two_part)}\x60(?![A-Za-z0-9_-])",
            ]
        )
    if not alternatives:
        return body
    pattern = re.compile("(?:" + "|".join(sorted(set(alternatives), key=len, reverse=True)) + ")")

    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        return targets.get(raw.strip("`"), raw)

    return pattern.sub(replace, body)


def _routine_warnings(
    body: str,
    *,
    unknown: Sequence[Mapping[str, Any]],
    dependencies: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    warnings = [dict(item) for item in unknown]
    dynamic_match = _DYNAMIC_RE.search(body)
    mutating_match = _MUTATING_RE.search(body)
    if dynamic_match:
        warnings.append(
            _finding(
                "dynamic_sql",
                path="definitionBody",
                line=_line_for_offset(body, dynamic_match.start()),
            )
        )
    if mutating_match:
        warnings.append(
            _finding(
                "mutating_sql",
                path="definitionBody",
                line=_line_for_offset(body, mutating_match.start()),
            )
        )
    warnings.extend(
        _finding(
            "external_dependency",
            path="definitionBody",
            line=int(item.get("line") or 1),
            routine=str(item.get("source_name") or ""),
        )
        for item in dependencies
        if item.get("status") == "external"
    )
    return warnings


def _dependency_blockers(
    dependencies: Sequence[Mapping[str, Any]],
    findings: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for item in dependencies:
        if item.get("status") in {"missing", "unsupported"}:
            blockers.append(dict(item))
    for item in findings:
        if item.get("kind") in {"unsupported_dependency", "unqualified_dependency"}:
            blockers.append(dict(item))
    return blockers


def _review_reasons(
    warnings: Sequence[Mapping[str, Any]],
    *,
    secret: bool,
    changed: bool,
    blockers: Sequence[Mapping[str, Any]],
) -> list[str]:
    reasons = {
        str(item.get("kind"))
        for item in warnings
        if item.get("kind")
        in {
            "unknown_route",
            "dynamic_sql",
            "mutating_sql",
            "unqualified_dependency",
            "external_dependency",
            "unsupported_dependency",
        }
    }
    if secret:
        reasons.add("embedded_secret")
    if blockers:
        reasons.add("dependency_blocked")
    if changed and not reasons:
        reasons.add("route_rewrite")
    return sorted(item for item in reasons if item)


def _status_for_record(
    *,
    collision: bool,
    secret: bool,
    secret_handling: str,
    blockers: Sequence[Mapping[str, Any]],
    destination_exists: bool,
    identical: bool,
) -> str:
    if collision:
        return "destination_conflict"
    if destination_exists and identical:
        return "already_present_identical"
    if destination_exists:
        return "destination_conflict"
    if blockers:
        return "blocked"
    if secret and secret_handling == "sealed_copy":
        return "security_pending"
    if secret:
        return "security_blocked"
    return "ready_to_publish"


def _record_digest_projection(record: Mapping[str, Any]) -> dict[str, Any]:
    value = json.loads(json.dumps(record, ensure_ascii=False))
    for key in (
        "status",
        "receipt",
        "error",
        "published",
        "task",
        "review_file",
        "proposal_file",
    ):
        value.pop(key, None)
    proposal = value.get("proposal")
    if isinstance(proposal, dict):
        proposal.pop("definitionBody", None)
    # The original body is retained for the local human-review diff, but is
    # deliberately excluded from the digest projection.  The before/after
    # hashes and the proposed semantic payload already bind the approval to
    # the exact content without duplicating SQL in the digest input.
    review = value.get("review")
    if isinstance(review, dict):
        review.pop("before_definition_body", None)
    return value


def _manifest_digest_projection(
    manifest: Mapping[str, Any], *, lot: int | None = None
) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(json.dumps(manifest, ensure_ascii=False))
    for key in (
        "publication_digest",
        "sealed_publication_digest",
        "created_at",
        "prepared_at",
        "execution",
        "request_stats",
        "report",
        "inventory",
        "lots",
    ):
        value.pop(key, None)
    records = [
        item for item in value.get("resources", []) if isinstance(item, Mapping)
    ]
    if lot is not None:
        records = [item for item in records if int(item.get("lot") or 0) == lot]
    value["resources"] = sorted(
        [_record_digest_projection(item) for item in records],
        key=lambda item: str((item.get("source") or {}).get("name") or ""),
    )
    return value


def build_routine_digest(manifest: Mapping[str, Any]) -> str:
    return _sha256_json(_manifest_digest_projection(manifest))


def build_routine_lot_digest(manifest: Mapping[str, Any], lot: int) -> str:
    return _sha256_json(_manifest_digest_projection(manifest, lot=lot))


def _sealed_digest_projection(
    manifest: Mapping[str, Any], lot: int | None = None
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for item in manifest.get("resources", []):
        if not isinstance(item, Mapping) or (
            lot is not None and int(item.get("lot") or 0) != lot
        ):
            continue
        security = item.get("security") or {}
        if not security.get("sealed"):
            continue
        records.append(
            {
                "source": item.get("source"),
                "destination": item.get("destination"),
                "source_sha256": (item.get("rewrite") or {}).get("before_sha256"),
                "proposed_sha256": (item.get("rewrite") or {}).get("proposed_sha256"),
                "findings": security.get("findings") or [],
            }
        )
    return {
        "schema_version": ROUTINE_SCHEMA_VERSION,
        "campaign_id": str(manifest.get("campaign_id") or ""),
        "source_project": str(manifest.get("source_project") or ""),
        "destination_project": str(manifest.get("destination_project") or ""),
        "destination_dataset": str(manifest.get("destination_dataset") or ""),
        "dictionary_sha256": str(manifest.get("dictionary_sha256") or ""),
        "lot": lot,
        "resources": sorted(
            records,
            key=lambda item: str((item.get("source") or {}).get("name") or ""),
        ),
    }


def build_routine_sealed_digest(
    manifest: Mapping[str, Any], lot: int | None = None
) -> str:
    projection = _sealed_digest_projection(manifest, lot=lot)
    return _sha256_json(projection) if projection["resources"] else ""


def _topological_order(
    records: Sequence[Mapping[str, Any]],
) -> tuple[list[str], list[list[str]]]:
    by_name = {
        str((item.get("source") or {}).get("name")): item for item in records
    }
    edges: dict[str, set[str]] = {name: set() for name in by_name}
    indegree: dict[str, int] = {name: 0 for name in by_name}
    for name, record in by_name.items():
        for dependency in record.get("dependencies") or []:
            dep_name = str(dependency.get("source_name") or "")
            if (
                dependency.get("status") == "resolved"
                and dep_name in by_name
                and dep_name != name
            ):
                if name not in edges[dep_name]:
                    edges[dep_name].add(name)
                    indegree[name] += 1
    ready = sorted(name for name, degree in indegree.items() if degree == 0)
    order: list[str] = []
    while ready:
        name = ready.pop(0)
        order.append(name)
        for child in sorted(edges[name]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort()
    cycles: list[list[str]] = []
    remainder = sorted(name for name, degree in indegree.items() if degree > 0)
    if remainder:
        cycles.append(remainder)
    return order, cycles


def _apply_dependency_status_blockers(records: Sequence[dict[str, Any]]) -> None:
    """Prevent callers from publishing against blocked/conflicting dependencies."""
    by_name = {
        str((record.get("source") or {}).get("name") or ""): record
        for record in records
    }
    blocking_statuses = {
        "blocked",
        "destination_conflict",
        "security_blocked",
        "security_pending",
    }
    for record in records:
        dependency_blockers: list[dict[str, Any]] = []
        for dependency in record.get("dependencies") or []:
            if dependency.get("status") != "resolved":
                continue
            dependency_name = str(dependency.get("source_name") or "")
            dependency_record = by_name.get(dependency_name)
            dependency_status = str((dependency_record or {}).get("status") or "")
            if dependency_status not in blocking_statuses:
                continue
            kind = (
                "dependency_destination_conflict"
                if dependency_status == "destination_conflict"
                else "dependency_not_publishable"
            )
            dependency_blockers.append(
                {
                    "kind": kind,
                    "dependency": dependency_name,
                    "dependency_status": dependency_status,
                }
            )
        if not dependency_blockers:
            continue
        existing = record.setdefault("blockers", [])
        known = {
            (item.get("kind"), item.get("dependency"))
            for item in existing
            if isinstance(item, Mapping)
        }
        for blocker in dependency_blockers:
            key = (blocker["kind"], blocker["dependency"])
            if key not in known:
                existing.append(blocker)
        record["status"] = "blocked"
        record["migration_eligible"] = False
        review = record.setdefault("review", {})
        review["required"] = True
        review["reasons"] = sorted(
            set(review.get("reasons") or [])
            | {item["kind"] for item in dependency_blockers}
        )


def _permissions_evidence(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize optional read-only permission evidence for a manifest.

    QueryFlow does not grant, copy, or alter IAM.  This small normalizer keeps
    the report deterministic and bounded when callers provide dataset ACL
    observations gathered during inventory.
    """
    value = value if isinstance(value, Mapping) else {}
    result: dict[str, Any] = {"source": [], "destination": [], "automated_changes": []}
    for key in ("source", "destination"):
        raw = value.get(key)
        if isinstance(raw, Mapping):
            # Destination metadata is commonly represented as one object.
            raw_values: list[Any] = [raw]
        elif isinstance(raw, list):
            raw_values = raw
        else:
            raw_values = []
        normalized: list[dict[str, Any]] = []
        for item in raw_values[:500]:
            if not isinstance(item, Mapping):
                continue
            record: dict[str, Any] = {}
            for field in ("project", "dataset", "location", "status", "error_kind", "error", "access_count"):
                if item.get(field) is not None:
                    record[field] = item[field]
            access = item.get("access")
            if isinstance(access, list):
                record["access"] = [
                    {
                        field: str(entry[field])[:500]
                        for field in ("role", "entityType", "entityId", "view", "routine")
                        if isinstance(entry, Mapping) and entry.get(field) is not None
                    }
                    for entry in access[:200]
                    if isinstance(entry, Mapping)
                ]
            # Permit already-normalized individual ACL entries for callers
            # that construct a report without a dataset wrapper.
            for field in ("role", "entityType", "entityId", "view", "routine"):
                if item.get(field) is not None:
                    record[field] = str(item[field])[:500]
            if record:
                normalized.append(record)
        result[key] = normalized
    changes = value.get("automated_changes")
    if isinstance(changes, list):
        result["automated_changes"] = [str(item)[:500] for item in changes[:100]]
    result["policy"] = "informative_only_no_iam_changes"
    return result


def build_routine_manifest(
    *,
    campaign_id: str,
    source_project: str,
    destination_project: str,
    destination_dataset: str = ROUTINE_DESTINATION_DATASET,
    destination_location: str,
    source_snapshots: Sequence[RoutineSnapshot],
    destination_snapshots: Sequence[RoutineSnapshot],
    dictionary: RouteDictionary,
    source_datasets: Sequence[str] | None = None,
    secret_handling: str = "block",
    batch_size: int = ROUTINE_BATCH_SIZE,
    source_inventory_errors: Sequence[Mapping[str, Any]] = (),
    backend: str = "direct",
    permissions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not str(campaign_id).strip():
        raise RoutineError("campaign_id es obligatorio", kind="invalid")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", str(destination_dataset or "")):
        raise RoutineError("El dataset destino contiene caracteres inválidos", kind="invalid")
    if secret_handling not in {"block", "sealed_copy"}:
        raise RoutineError("secret_handling debe ser block o sealed_copy", kind="invalid")
    try:
        batch_size = int(batch_size)
    except (TypeError, ValueError) as error:
        raise RoutineError("batch_size debe ser entero", kind="invalid") from error
    if not 1 <= batch_size <= 100:
        raise RoutineError("batch_size debe estar entre 1 y 100", kind="invalid")
    source_by_key = {
        (item.project, item.dataset, item.routine_id): item for item in source_snapshots
    }
    destination_by_id = {
        item.routine_id.casefold(): item
        for item in destination_snapshots
        if item.dataset == destination_dataset
    }
    selected = [item for item in source_snapshots if _is_supported_procedure(item.routine)]
    unsupported = [
        item
        for item in source_snapshots
        if not _is_supported_procedure(item.routine)
        and not _is_supported_dependency(item.routine)
    ]
    records_by_name: dict[str, dict[str, Any]] = {}
    queue: deque[RoutineSnapshot] = deque(selected)
    visited: set[str] = set()
    while queue:
        snapshot = queue.popleft()
        if snapshot.source_name in visited:
            continue
        visited.add(snapshot.source_name)
        body_value = snapshot.routine.get("definitionBody")
        body = body_value if isinstance(body_value, str) else ""
        dependencies, dependency_findings = _extract_dependency_refs(
            body,
            project=snapshot.project,
            dataset=snapshot.dataset,
            inventory=source_by_key,
        )
        for dependency in dependencies:
            if dependency.get("status") == "resolved":
                dep_snapshot = source_by_key.get(
                    (
                        str(dependency["project"]),
                        str(dependency["dataset"]),
                        str(dependency["routine_id"]),
                    )
                )
                if dep_snapshot is not None and dep_snapshot.source_name not in visited:
                    queue.append(dep_snapshot)
        # Normalize routine calls first.  A broad route mapping may also
        # match the source routine's dataset; applying it first could turn a
        # local CALL into an unrelated path and defeat dependency resolution.
        dependency_rewritten = _rewrite_dependency_references(
            body,
            dependencies,
            destination_project=destination_project,
            destination_dataset=destination_dataset,
        )
        rewritten, applied, unknown = _rewrite_body(dependency_rewritten, dictionary)
        secret_findings = _security_findings(body)
        warnings = _routine_warnings(
            rewritten, unknown=unknown, dependencies=dependencies
        )
        warnings.extend(dependency_findings)
        blockers = _dependency_blockers(dependencies, dependency_findings)
        if not body.strip():
            blockers.append(_finding("empty_definition", path="definitionBody", line=1))
        destination_id = snapshot.routine_id
        destination = destination_by_id.get(destination_id.casefold())
        proposed_routine = routine_semantic_payload(
            snapshot.routine,
            project=destination_project,
            dataset=destination_dataset,
            routine_id=destination_id,
            definition_body=rewritten,
        )
        proposed_sha = _sha256_json(proposed_routine)
        identical = bool(
            destination
            and routine_semantic_payload(
                destination.routine,
                project=destination_project,
                dataset=destination_dataset,
                routine_id=destination_id,
            )
            == proposed_routine
        )
        collision = bool(destination and not identical)
        secret = bool(secret_findings)
        status = _status_for_record(
            collision=collision,
            secret=secret,
            secret_handling=secret_handling,
            blockers=blockers,
            destination_exists=destination is not None,
            identical=identical,
        )
        reasons = _review_reasons(
            warnings,
            secret=secret,
            changed=body != rewritten,
            blockers=blockers,
        )
        role = (
            "procedure" if _routine_type(snapshot.routine) == "PROCEDURE" else "dependency"
        )
        records_by_name[snapshot.source_name] = {
            "source": {
                **snapshot.to_dict(),
                "name": snapshot.source_name,
                "routine_id": snapshot.routine_id,
            },
            "destination": {
                "project": destination_project,
                "dataset": destination_dataset,
                "location": destination_location,
                "routine_id": destination_id,
                "name": routine_resource_name(
                    destination_project, destination_dataset, destination_id
                ),
                "exists": destination is not None,
                "collision": collision,
            },
            "role": role,
            "rewrite": {
                "before_sha256": _sha256(body.encode("utf-8")),
                "proposed_sha256": _sha256(rewritten.encode("utf-8")),
                "semantic_proposed_sha256": proposed_sha,
                "changed": body != rewritten,
                "applied_routes": applied,
                "unknown_routes": unknown,
            },
            "proposal": _mask_routine(proposed_routine)
            if secret
            else proposed_routine,
            "classification": {
                "routine_type": _routine_type(snapshot.routine),
                "language": _routine_language(snapshot.routine),
                "dynamic_sql": bool(_DYNAMIC_RE.search(rewritten)),
                "mutating_sql": bool(_MUTATING_RE.search(rewritten)),
                "strict_mode": snapshot.routine.get("strictMode"),
            },
            "dependencies": dependencies,
            "security": {
                "sealed": secret and secret_handling == "sealed_copy",
                "handling": "sealed_copy"
                if secret and secret_handling == "sealed_copy"
                else "block"
                if secret
                else "none",
                "findings": secret_findings,
            },
            "warnings": warnings,
            "blockers": blockers,
            "review": {
                "required": bool(reasons),
                "reasons": reasons,
                "policy": "human_review_before_publication",
                # Keep a local, non-secret baseline so the web preview can
                # render a true red/green diff.  Secret-bearing definitions
                # are never persisted in this field.
                "before_definition_body": "[Contenido original sellado]"
                if secret
                else body,
            },
            "status": status,
            "migration_eligible": status == "ready_to_publish",
            "execution_eligible": False,
        }
    records = list(records_by_name.values())
    visited_names = set(records_by_name)
    not_selected = [
        item.to_dict()
        for item in source_snapshots
        if _is_supported_dependency(item.routine)
        and item.source_name not in visited_names
    ]
    order, cycles = _topological_order(records)
    position = {name: index for index, name in enumerate(order)}
    cycle_names = set(cycles[0]) if cycles else set()
    if cycles:
        for record in records:
            if str((record.get("source") or {}).get("name")) in cycle_names:
                record["status"] = "blocked"
                record["migration_eligible"] = False
                record.setdefault("blockers", []).append(
                    {"kind": "dependency_cycle", "members": cycles[0]}
                )
                record["review"]["required"] = True
                record["review"]["reasons"] = sorted(
                    set(record["review"].get("reasons", [])) | {"dependency_cycle"}
                )
    records.sort(
        key=lambda item: (
            position.get(str((item.get("source") or {}).get("name")), 10**9),
            str((item.get("source") or {}).get("name") or ""),
        )
    )
    for index, record in enumerate(records):
        record["ordinal"] = index + 1
        record["lot"] = index // batch_size + 1
    duplicate_ids: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        duplicate_ids[
            str((record.get("destination") or {}).get("routine_id") or "").casefold()
        ].append(record)
    for routine_id, duplicates in duplicate_ids.items():
        if routine_id and len(duplicates) > 1:
            names = [
                str((item.get("source") or {}).get("name") or "")
                for item in duplicates
            ]
            for record in duplicates:
                record["status"] = "destination_conflict"
                record["migration_eligible"] = False
                record["destination"]["collision"] = True
                record.setdefault("blockers", []).append(
                    {
                        "kind": "duplicate_destination_name",
                        "routine_id": routine_id,
                        "sources": names,
                    }
                )
                record["review"]["required"] = True
                record["review"]["reasons"] = sorted(
                    set(record["review"].get("reasons", []))
                    | {"destination_conflict"}
                )
    _apply_dependency_status_blockers(records)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    manifest: dict[str, Any] = {
        "schema_version": ROUTINE_SCHEMA_VERSION,
        "campaign_id": str(campaign_id),
        "created_at": now,
        "source_project": source_project,
        "source_datasets": [str(item) for item in (source_datasets or ())],
        "destination_project": destination_project,
        "destination_dataset": destination_dataset,
        "destination_location": destination_location,
        "batch_size": batch_size,
        "backend": backend,
        "dictionary_id": dictionary.dictionary_id,
        "dictionary_sha256": dictionary.dictionary_sha256,
        "permissions": _permissions_evidence(permissions),
        "policy": {
            "no_sql_execution": True,
            "call_executed": False,
            "dry_run": {
                "skipped": True,
                "reason": "Migración de rutinas mediante BigQuery REST",
            },
            "copy_only": True,
            "warnings": "accept_with_human_review",
            "allow_update_existing": False,
            "secret_handling": secret_handling,
        },
        "inventory": {
            "source_routines": len(source_snapshots),
            "google_sql_procedures": len(selected),
            "included_dependencies": max(0, len(records) - len(selected)),
            "unsupported_routines": len(unsupported),
            "unreferenced_supported_dependencies": len(not_selected),
            "dependency_cycles": cycles,
            "errors": [dict(item) for item in source_inventory_errors],
        },
        "resources": records,
        "unsupported": [item.to_dict() for item in unsupported],
        "not_selected": not_selected,
        "execution": {
            "published": 0,
            "already_present": 0,
            "blocked": 0,
            "receipts": [],
        },
    }
    lots: list[dict[str, Any]] = []
    for lot in sorted({int(item["lot"]) for item in records}):
        lot_records = [item for item in records if int(item["lot"]) == lot]
        lots.append(
            {
                "lot": lot,
                "resources": len(lot_records),
                "digest": build_routine_lot_digest(manifest, lot),
                "sealed_digest": build_routine_sealed_digest(manifest, lot),
                "status": "blocked"
                if any(
                    item["status"]
                    in {"blocked", "destination_conflict", "security_blocked"}
                    for item in lot_records
                )
                else "ready",
            }
        )
    manifest["lots"] = lots
    manifest["publication_digest"] = build_routine_digest(manifest)
    manifest["sealed_publication_digest"] = build_routine_sealed_digest(manifest)
    return manifest


def validate_routine_manifest(
    manifest: Mapping[str, Any],
    *,
    approved_digest: str | None = None,
    approved_sealed_digest: str | None = None,
    security_reference: str | None = None,
    lot: int | None = None,
    allow_blocked: bool = False,
) -> None:
    if not isinstance(manifest, Mapping) or int(manifest.get("schema_version") or 0) != ROUTINE_SCHEMA_VERSION:
        raise RoutineError("Manifest de rutinas no compatible", kind="invalid")
    for key in (
        "campaign_id",
        "source_project",
        "destination_project",
        "destination_dataset",
        "destination_location",
        "dictionary_sha256",
        "publication_digest",
    ):
        if not str(manifest.get(key) or "").strip():
            raise RoutineError(f"Manifest de rutinas sin {key}", kind="invalid")
    expected = build_routine_lot_digest(manifest, lot) if lot is not None else build_routine_digest(manifest)
    if approved_digest is not None and str(approved_digest) != expected:
        raise RoutineError("El digest aprobado no coincide con el manifiesto actual", kind="integrity")
    if approved_digest is None and lot is not None:
        raise RoutineError("El lote requiere --approved-digest", kind="approval")
    if approved_digest is not None and lot is None and str(approved_digest) != str(manifest.get("publication_digest")):
        raise RoutineError("El digest aprobado no coincide con publication_digest", kind="integrity")
    records = manifest.get("resources")
    if not isinstance(records, list) or not records:
        raise RoutineError("Manifest de rutinas sin recursos", kind="invalid")
    selected = [
        item for item in records
        if lot is None or int(item.get("lot") or 0) == lot
    ]
    if not selected:
        raise RoutineError(f"No existe el lote {lot}", kind="invalid")
    hard = [
        item for item in selected
        if str(item.get("status") or "") in {"blocked", "destination_conflict", "security_blocked"}
    ]
    if hard and not allow_blocked:
        names = ", ".join(
            str((item.get("source") or {}).get("routine_id") or "") for item in hard[:5]
        )
        raise RoutineError(f"El manifiesto tiene recursos bloqueados: {names}", kind="policy")
    sealed = [
        item for item in selected if bool((item.get("security") or {}).get("sealed"))
    ]
    if sealed:
        expected_sealed = build_routine_sealed_digest(manifest, lot)
        if not approved_sealed_digest or approved_sealed_digest != expected_sealed:
            raise RoutineError(
                "Las rutinas selladas requieren un digest de seguridad exacto", kind="security"
            )
        if not str(security_reference or "").strip():
            raise RoutineError(
                "Las rutinas selladas requieren una referencia de seguridad", kind="security"
            )


def save_routine_manifest(manifest: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_routine_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RoutineError(
            f"No se pudo leer el manifiesto de rutinas: {error}", kind="invalid"
        ) from error
    if not isinstance(value, dict):
        raise RoutineError("El manifiesto de rutinas debe ser un objeto", kind="invalid")
    return value


def _diff_rows(before: str, after: str) -> str:
    rows: list[str] = []
    for line in difflib.ndiff(before.splitlines(), after.splitlines()):
        marker = line[:2]
        value = html.escape(line[2:])
        if marker == "+ ":
            rows.append(f'<div class="diff-add"><span>+</span>{value}</div>')
        elif marker == "- ":
            rows.append(f'<div class="diff-del"><span>−</span>{value}</div>')
        elif marker == "  ":
            rows.append(f'<div class="diff-ctx"><span> </span>{value}</div>')
    return "".join(rows)


def render_routine_review(manifest: Mapping[str, Any], *, live: bool = False) -> str:
    records = manifest.get("resources") or []
    rows: list[str] = []
    for item in records:
        if not isinstance(item, Mapping):
            continue
        source = item.get("source") or {}
        destination = item.get("destination") or {}
        proposal = item.get("proposal") or {}
        body = str(proposal.get("definitionBody") or "")
        sealed = bool((item.get("security") or {}).get("sealed"))
        if sealed:
            body = "[Contenido sellado: se muestran únicamente hashes y hallazgos]"
        review_meta = item.get("review") or {}
        original = (
            "[Contenido original sellado]"
            if sealed
            else str(
                review_meta.get("before_definition_body")
                or "[Definición original no persistida]"
            )
        )
        warnings = ", ".join(
            str(value.get("kind"))
            for value in item.get("warnings") or []
            if isinstance(value, Mapping)
        ) or "ninguna"
        dependencies = ", ".join(
            str(value.get("routine_id") or value.get("source_name") or "")
            for value in item.get("dependencies") or []
            if isinstance(value, Mapping)
        ) or "ninguna"
        rows.append(
            "<details class='routine-card' data-status='"
            + html.escape(str(item.get("status") or ""))
            + "'><summary><span class='status'>"
            + html.escape(str(item.get("status") or ""))
            + "</span> "
            + html.escape(str(source.get("routine_id") or ""))
            + " <small>→ "
            + html.escape(str(destination.get("name") or ""))
            + "</small></summary><div class='meta'><span>Rol: "
            + html.escape(str(item.get("role") or ""))
            + "</span><span>Rutas: "
            + str(len(item.get("rewrite", {}).get("applied_routes") or []))
            + "</span><span>Dependencias: "
            + html.escape(dependencies)
            + "</span><span>Incidencias: "
            + html.escape(warnings)
            + "</span></div><div class='diff-label'>Definición propuesta</div><div class='diff'>"
            + _diff_rows(original, body)
            + "</div></details>"
        )
    summary = manifest.get("inventory") or {}
    return (
        "<!doctype html><html lang='es'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>QueryFlow · Revisión de rutinas</title><style>"
        ":root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#0b1020;color:#e8edf7;font:14px system-ui,-apple-system,Segoe UI,sans-serif}"
        "main{max-width:1180px;margin:0 auto;padding:32px 22px}h1{margin:0 0 6px;font-size:26px}p{color:#9aa7c1}"
        ".toolbar{display:flex;gap:10px;flex-wrap:wrap;margin:22px 0}.chip{background:#17223d;border:1px solid #2a3a62;border-radius:999px;padding:7px 12px}"
        ".routine-card{background:#111a31;border:1px solid #26375f;border-radius:12px;margin:12px 0;overflow:hidden}.routine-card summary{cursor:pointer;padding:14px 16px;font-weight:600}"
        ".routine-card small{color:#8797b6;font-weight:400}.status{color:#5eead4;font-size:11px;letter-spacing:.05em;text-transform:uppercase;margin-right:8px}"
        ".meta{display:flex;gap:16px;flex-wrap:wrap;padding:0 16px 14px;color:#9aa7c1;font-size:12px}.diff-label{padding:9px 16px;border-top:1px solid #26375f;color:#8fa2c8;font-size:12px;text-transform:uppercase;letter-spacing:.07em}"
        ".diff{background:#080d19;padding:10px 0;font:12px ui-monospace,SFMono-Regular,Menlo,monospace;overflow:auto}.diff>div{padding:3px 16px;white-space:pre}.diff span{display:inline-block;width:22px;color:#7182a8}"
        ".diff-add{background:#123d35;color:#b8f7de}.diff-add span{color:#5eead4}.diff-del{background:#4a202b;color:#ffc5cf}.diff-del span{color:#fb7185}.diff-ctx{color:#c4cce0}"
        ".search{background:#0f172b;border:1px solid #33466f;color:#fff;border-radius:8px;padding:9px 12px;min-width:260px}"
        "</style></head><body><main><h1>Revisión de procedimientos</h1><p>Campaña <code>"
        + html.escape(str(manifest.get("campaign_id") or ""))
        + "</code> · destino <code>"
        + html.escape(str(manifest.get("destination_project") or ""))
        + "."
        + html.escape(str(manifest.get("destination_dataset") or ""))
        + "</code></p><div class='toolbar'><span class='chip'>Procedimientos: "
        + str(summary.get("google_sql_procedures", 0))
        + "</span><span class='chip'>Dependencias: "
        + str(summary.get("included_dependencies", 0))
        + "</span><span class='chip'>Sin ejecución SQL</span><input class='search' placeholder='Filtrar por nombre o estado' oninput='filterCards(this.value)'></div>"
        + "".join(rows)
        + "<script>function filterCards(v){v=v.toLowerCase();document.querySelectorAll('.routine-card').forEach(e=>e.style.display=e.innerText.toLowerCase().includes(v)?'block':'none')}</script></main></body></html>"
    )


def write_routine_reports(
    manifest: Mapping[str, Any], manifest_path: Path
) -> dict[str, str]:
    root = manifest_path.parent
    json_path = root / "report.json"
    md_path = root / "report.md"
    html_path = root / "review.html"
    json_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    counts: dict[str, int] = {}
    for item in manifest.get("resources") or []:
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    lines = [
        f"# Migración de procedimientos · {manifest.get('campaign_id', '')}",
        "",
        f"- Origen: {manifest.get('source_project', '')}",
        f"- Destino: {manifest.get('destination_project', '')}.{manifest.get('destination_dataset', '')}",
        f"- Digest: {manifest.get('publication_digest', '')}",
        "- Ejecución SQL/CALL: no",
        "- Cambios IAM/autorizaciones: no (los accesos son evidencia informativa)",
        f"- Web Preview: `{html_path.name}`",
        "",
        "## Estados",
        "",
        "| Estado | Recursos |",
        "| --- | ---: |",
    ]
    lines.extend(f"| {key} | {value} |" for key, value in sorted(counts.items()))
    inventory = manifest.get("inventory") or {}
    lots = manifest.get("lots") or []
    if lots:
        lines.extend(
            [
                "",
                "## Lotes",
                "",
                "| Lote | Recursos | Estado | Digest | Digest sellado |",
                "| ---: | ---: | --- | --- | --- |",
            ]
        )
        for lot in lots:
            if not isinstance(lot, Mapping):
                continue
            lines.append(
                f"| {lot.get('lot', '')} | {lot.get('resources', 0)} | {lot.get('status', '')} | "
                f"`{lot.get('digest', '')}` | `{lot.get('sealed_digest', '')}` |"
            )
    inventory_errors = inventory.get("errors") if isinstance(inventory, Mapping) else []
    if isinstance(inventory_errors, list) and inventory_errors:
        lines.extend(["", "## Incidencias de inventario", ""])
        for error in inventory_errors:
            if isinstance(error, Mapping):
                location = error.get("dataset") or error.get("routine") or "inventario"
                lines.append(
                    f"- `{error.get('kind', 'error')}` · `{location}` · "
                    f"{str(error.get('message') or error.get('error') or '')[:500]}"
                )
            else:
                lines.append(f"- {str(error)[:500]}")
    unsupported = manifest.get("unsupported") or []
    not_selected = manifest.get("not_selected") or []
    unsupported_items = (
        [(item, "tipo_o_lenguaje_no_soportado") for item in unsupported]
        if isinstance(unsupported, list)
        else []
    )
    unsupported_items.extend(
        [(item, "dependencia_soportada_no_referenciada") for item in not_selected]
        if isinstance(not_selected, list)
        else []
    )
    if unsupported_items:
        lines.extend(
            [
                "",
                "## Rutinas no seleccionadas",
                "",
                "Se inventariaron, pero no se incluyen en la publicación automática.",
                "",
                "| Rutina | Tipo | Lenguaje | Ubicación | Motivo | Hash origen |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for item, reason in unsupported_items:
            if not isinstance(item, Mapping):
                continue
            lines.append(
                f"| `{item.get('name', '')}` | `{item.get('routine_type', '')}` | "
                f"`{item.get('language', '')}` | `{item.get('location', '')}` | "
                f"`{reason}` | `{item.get('source_sha256', '')}` |"
            )
    execution = manifest.get("execution") or {}
    receipts = execution.get("receipts") if isinstance(execution, Mapping) else []
    if isinstance(receipts, list) and receipts:
        lines.extend(
            [
                "",
                "## Resultado de publicación",
                "",
                "| Origen | Destino | Estado |",
                "| --- | --- | --- |",
            ]
        )
        for receipt in receipts:
            if not isinstance(receipt, Mapping):
                continue
            lines.append(
                f"| `{receipt.get('source', '')}` | `{receipt.get('destination', '')}` | "
                f"`{receipt.get('status', '')}` |"
            )
    permissions = manifest.get("permissions") or {}
    lines.extend(["", "## Evidencia de accesos (solo lectura)", ""])
    lines.append("QueryFlow no modifica IAM ni autorizaciones; esta sección resume lo observado durante el inventario.")
    for side in ("source", "destination"):
        values = permissions.get(side) if isinstance(permissions, Mapping) else []
        values = values if isinstance(values, list) else []
        lines.append("")
        lines.append(f"### {side}")
        if not values:
            lines.append("- No se obtuvo evidencia de acceso.")
            continue
        for value in values:
            if not isinstance(value, Mapping):
                continue
            access_count = value.get("access_count")
            status = value.get("status") or "unknown"
            location = value.get("location") or ""
            lines.append(
                f"- `{value.get('project', '')}.{value.get('dataset', '')}` · estado `{status}` · ubicación `{location}` · entradas ACL `{access_count if access_count is not None else len(value.get('access') or [])}`"
            )
    lines.extend(
        [
            "",
            "## Recursos",
            "",
            "| Rutina | Rol | Estado | Rutas | Incidencias |",
            "| --- | --- | --- | ---: | ---: |",
        ]
    )
    for item in manifest.get("resources") or []:
        source = item.get("source") or {}
        rewrite = item.get("rewrite") or {}
        lines.append(
            f"| {source.get('routine_id', '')} | {item.get('role', '')} | {item.get('status', '')} | {len(rewrite.get('applied_routes') or [])} | {len(item.get('warnings') or [])} |"
        )
    lines.extend(["", "## Detalle por rutina", ""])
    for item in manifest.get("resources") or []:
        source = item.get("source") or {}
        destination = item.get("destination") or {}
        rewrite = item.get("rewrite") or {}
        name = str(source.get("routine_id") or source.get("name") or "routine")
        lines.extend(
            [
                f"### `{name}`",
                "",
                f"- Origen: `{source.get('name', '')}`",
                f"- Destino: `{destination.get('name', '')}`",
                f"- Estado: **{item.get('status', '')}** · lote `{item.get('lot', '')}` · rol `{item.get('role', '')}`",
                f"- Hash origen: `{rewrite.get('before_sha256', '')}`",
                f"- Hash propuesta: `{rewrite.get('proposed_sha256', '')}`",
            ]
        )
        applied = rewrite.get("applied_routes") or []
        if applied:
            lines.extend(
                [
                    "",
                    "#### Rutas reemplazadas",
                    "",
                    "| Diccionario | Ruta antigua | Ruta nueva | Ocurrencias |",
                    "| --- | --- | --- | ---: |",
                ]
            )
            for route in applied:
                lines.append(
                    f"| `{route.get('mapping_id', '')}` | `{route.get('old', '')}` | `{route.get('new', '')}` | {route.get('occurrences', 0)} |"
                )
        unknown = rewrite.get("unknown_routes") or []
        if unknown:
            lines.extend(
                [
                    "",
                    "#### Rutas no cubiertas",
                    "",
                    "| Ruta conservada | Ubicación |",
                    "| --- | --- |",
                ]
            )
            for route in unknown:
                path = str(route.get("path") or "definitionBody")
                line = route.get("line")
                coordinate = f"{path}, línea {line}" if line else path
                lines.append(f"| `{route.get('route', '')}` | `{coordinate}` |")
        dependencies = item.get("dependencies") or []
        if dependencies:
            lines.extend(["", "#### Dependencias", ""])
            for dependency in dependencies:
                lines.append(
                    f"- `{dependency.get('source_name', '')}` · estado `{dependency.get('status', '')}` · línea {dependency.get('line', '?')}"
                )
        warnings = item.get("warnings") or []
        if warnings:
            lines.extend(["", "#### Advertencias", ""])
            for warning in warnings:
                if isinstance(warning, Mapping):
                    location = f"{warning.get('path', 'definitionBody')}, línea {warning.get('line', '?')}"
                    lines.append(f"- `{warning.get('kind', 'warning')}` · `{location}`")
                else:
                    lines.append(f"- `{warning}`")
        blockers = item.get("blockers") or []
        if blockers:
            lines.extend(["", "#### Bloqueos", ""])
            for blocker in blockers:
                lines.append(f"- `{blocker.get('kind', 'blocker')}`")
        lines.append("")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    html_path.write_text(render_routine_review(manifest), encoding="utf-8")
    result = {"json": str(json_path), "markdown": str(md_path), "html": str(html_path)}
    audit_path = root / "routine-audit.json"
    if audit_path.exists():
        result["audit"] = str(audit_path)
    return result


def _routine_audit_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only non-content fields in a routine publication audit."""
    allowed = (
        "source",
        "destination",
        "status",
        "kind",
        "message",
        "source_sha256",
        "proposed_sha256",
        "call_executed",
        "dry_run",
    )
    return {key: receipt[key] for key in allowed if receipt.get(key) is not None}


def write_routine_audit(
    manifest: Mapping[str, Any],
    manifest_path: Path,
    *,
    approved_digest: str,
    phase: str,
    receipts: Sequence[Mapping[str, Any]] = (),
    approved_sealed_digest: str | None = None,
    security_reference: str | None = None,
) -> Path:
    """Write a local, content-free audit checkpoint for a campaign.

    The campaign manifest and the Web Preview remain the source artifacts for
    code review.  This checkpoint intentionally contains only identifiers,
    hashes, policy assertions and publication outcomes, so an audit archive
    cannot accidentally become a second copy of routine definitions.
    """
    manifest_sha256 = ""
    try:
        manifest_sha256 = _sha256(manifest_path.read_bytes())
    except OSError:
        # The caller may be creating a first checkpoint before saving the
        # manifest.  Keep the field explicit rather than failing a remote
        # operation solely because a local path is unavailable.
        manifest_sha256 = "unavailable"
    payload = {
        "schema_version": 1,
        "campaign_id": str(manifest.get("campaign_id") or ""),
        "phase": str(phase),
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_project": str(manifest.get("source_project") or ""),
        "destination_project": str(manifest.get("destination_project") or ""),
        "destination_dataset": str(manifest.get("destination_dataset") or ""),
        "approved_digest": str(approved_digest),
        "approved_sealed_digest": str(approved_sealed_digest or ""),
        "security_reference": str(security_reference or ""),
        "manifest_sha256": manifest_sha256,
        "policy": {
            "copy_only": True,
            "allow_update_existing": False,
            "call_executed": False,
            "dry_run": False,
            "iam_changes": False,
        },
        "receipts": [_routine_audit_receipt(item) for item in receipts],
    }
    path = manifest_path.parent / "routine-audit.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def _routine_handler_factory(manifest_path: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "QueryFlowRoutineReview/1"

        def _send(
            self, body: bytes, content_type: str, status: int = HTTPStatus.OK
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            try:
                manifest = load_routine_manifest(manifest_path)
                body = render_routine_review(manifest).encode("utf-8")
            except Exception as error:
                self._send(
                    str(error).encode("utf-8"),
                    "text/plain; charset=utf-8",
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            self._send(body, "text/html; charset=utf-8")

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def serve_routine_review(
    manifest_path: Path, port: int = 8080
) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("0.0.0.0", int(port)), _routine_handler_factory(manifest_path))
    host = os.environ.get("WEB_HOST", "localhost")
    actual_port = int(server.server_address[1])
    return server, f"https://{actual_port}-{host}/review.html"


def _snapshot_from_client(
    client: BigQueryRoutineClient, source: Mapping[str, Any]
) -> RoutineSnapshot:
    project = str(source.get("project") or "")
    dataset = str(source.get("dataset") or "")
    routine_id = str(source.get("routine_id") or "")
    location = str(source.get("location") or "")
    routine = client.get_routine(project, dataset, routine_id)
    snapshot = RoutineSnapshot.from_routine(project, dataset, location, routine)
    expected = str(source.get("source_sha256") or "")
    if expected and snapshot.source_sha256 != expected:
        raise RoutineError(
            f"La rutina cambió después del inventario: {snapshot.source_name}",
            kind="integrity",
        )
    return snapshot


def _publish_routine_record(
    record: Mapping[str, Any],
    client: BigQueryRoutineClient,
    *,
    dictionary: RouteDictionary,
    security_reference: str | None,
) -> dict[str, Any]:
    """Publish one record and return its receipt.

    The caller decides which provider failures are recoverable at campaign
    level.  Keeping one record's work in this helper makes it possible to
    record a failed resource without swallowing integrity or policy errors.
    """
    status = str(record.get("status") or "")
    source = record.get("source") or {}
    destination = record.get("destination") or {}
    if status in {"blocked", "destination_conflict", "security_blocked"}:
        return {
            "source": source.get("name"),
            "status": status,
            "message": "No se escribe un recurso bloqueado",
        }
    if status == "already_present_identical":
        return {
            "source": source.get("name"),
            "destination": destination.get("name"),
            "status": "already_present_identical",
        }
    snapshot = _snapshot_from_client(client, source)
    body = snapshot.routine.get("definitionBody")
    if not isinstance(body, str):
        raise RoutineError(
            f"La rutina no tiene definitionBody: {snapshot.source_name}",
            kind="invalid",
        )
    # The manifest records the same deterministic routine-reference rewrite
    # during preparation; publication recomputes it after the source hash
    # check so no stale proposal can be inserted.
    dependency_rewritten = _rewrite_dependency_references(
        body,
        record.get("dependencies") or [],
        destination_project=str(destination.get("project")),
        destination_dataset=str(destination.get("dataset")),
    )
    rewritten, _routes, _unknown = rewrite_text(dependency_rewritten, dictionary)
    proposed = routine_semantic_payload(
        snapshot.routine,
        project=str(destination.get("project")),
        dataset=str(destination.get("dataset")),
        routine_id=str(destination.get("routine_id")),
        definition_body=rewritten,
    )
    expected_sha = str((record.get("rewrite") or {}).get("proposed_sha256") or "")
    if _sha256(rewritten.encode("utf-8")) != expected_sha:
        raise RoutineError(
            f"La propuesta cambió después de la preparación: {snapshot.source_name}",
            kind="integrity",
        )
    if bool((record.get("security") or {}).get("sealed")) and not security_reference:
        raise RoutineError(
            "La publicación sellada requiere referencia de seguridad",
            kind="security",
        )
    try:
        existing = client.get_routine(
            str(destination.get("project")),
            str(destination.get("dataset")),
            str(destination.get("routine_id")),
        )
    except RoutineError as error:
        if error.kind != "not_found":
            raise
        existing = None
    if existing is not None:
        existing_semantic = routine_semantic_payload(
            existing,
            project=str(destination.get("project")),
            dataset=str(destination.get("dataset")),
            routine_id=str(destination.get("routine_id")),
        )
        if existing_semantic == proposed:
            return {
                "source": source.get("name"),
                "destination": destination.get("name"),
                "status": "already_present_identical",
            }
        raise RoutineError(
            f"Conflicto de destino; no se sobrescribe {destination.get('name')}",
            kind="conflict",
        )
    inserted = client.insert_routine(
        str(destination.get("project")),
        str(destination.get("dataset")),
        proposed,
    )
    read_back = client.get_routine(
        str(destination.get("project")),
        str(destination.get("dataset")),
        str(destination.get("routine_id")),
    )
    read_back_semantic = routine_semantic_payload(
        read_back,
        project=str(destination.get("project")),
        dataset=str(destination.get("dataset")),
        routine_id=str(destination.get("routine_id")),
    )
    if read_back_semantic != proposed:
        raise RoutineError(
            f"La lectura posterior no coincide para {destination.get('name')}",
            kind="integrity",
        )
    return {
        "source": source.get("name"),
        "destination": destination.get("name"),
        "status": "published_verified",
        "insert_response": {
            "routineReference": inserted.get("routineReference")
        }
        if isinstance(inserted, Mapping)
        else {},
        "source_sha256": (record.get("rewrite") or {}).get("before_sha256"),
        "proposed_sha256": (record.get("rewrite") or {}).get("proposed_sha256"),
        "call_executed": False,
        "dry_run": False,
    }


def publish_routines(
    manifest: Mapping[str, Any],
    client: BigQueryRoutineClient,
    *,
    dictionary: RouteDictionary,
    approved_digest: str,
    approved_sealed_digest: str | None = None,
    security_reference: str | None = None,
    lot: int | None = None,
    allow_blocked: bool = True,
) -> list[dict[str, Any]]:
    expected_dictionary_sha = str(manifest.get("dictionary_sha256") or "")
    if expected_dictionary_sha and dictionary.dictionary_sha256 != expected_dictionary_sha:
        raise RoutineError(
            "El diccionario de rutas no coincide con el manifiesto aprobado",
            kind="integrity",
        )
    validate_routine_manifest(
        manifest,
        approved_digest=approved_digest,
        approved_sealed_digest=approved_sealed_digest,
        security_reference=security_reference,
        lot=lot,
        allow_blocked=allow_blocked,
    )
    records = [
        item
        for item in manifest.get("resources") or []
        if isinstance(item, Mapping)
        and (lot is None or int(item.get("lot") or 0) == lot)
    ]
    receipts: list[dict[str, Any]] = []
    for record in records:
        source = record.get("source") or {}
        destination = record.get("destination") or {}
        try:
            receipts.append(
                _publish_routine_record(
                    record,
                    client,
                    dictionary=dictionary,
                    security_reference=security_reference,
                )
            )
        except RoutineError as error:
            # A provider-level failure (for example a VPC perimeter denial)
            # belongs to this resource's audit trail.  Continue with the
            # remaining independent records; integrity and policy failures
            # still abort the campaign rather than being hidden.
            if error.kind not in {
                "permission",
                "network",
                "transport",
                "rate_limit",
                "transient",
                "http",
            }:
                raise
            receipts.append(
                {
                    "source": source.get("name"),
                    "destination": destination.get("name"),
                    "status": "failed",
                    "kind": error.kind,
                    "message": str(error)[:500],
                    "call_executed": False,
                    "dry_run": False,
                }
            )
    return receipts
