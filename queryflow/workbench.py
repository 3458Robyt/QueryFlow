"""Ephemeral Workbench/Jupyter transport for in-perimeter BigQuery validation."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from http.cookiejar import CookieJar
from typing import Any, Optional, Sequence
from urllib.parse import urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

from .validation import (
    FragmentValidation,
    ValidationResult,
    classify_error,
    validate_sql_fragments,
)
from .sample import limit_query, sanitize_rows


class WorkbenchError(RuntimeError):
    """A Workbench validation could not produce trustworthy evidence."""

    def __init__(self, message: str, *, kind: str = "transport") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class WorkbenchSettings:
    project: str
    location: str
    instance: str
    job_project: str
    timeout_seconds: int = 240

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "location": self.location,
            "instance": self.instance,
            "job_project": self.job_project,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class WorkbenchValidation:
    result: ValidationResult
    backend_details: dict[str, Any]


def build_sample_payload(
    fragments: Sequence[tuple[int, str]],
    *,
    maximum_bytes_billed: int,
    limit: int,
) -> dict[str, Any]:
    """Build the exact payload sent to the in-perimeter sample runner."""
    if limit < 1 or limit > 5:
        raise ValueError("El límite de muestra debe estar entre 1 y 5")
    return {
        "maximum_bytes_billed": maximum_bytes_billed,
        "limit": limit,
        "fragments": [
            {
                "cell": index,
                "sql": limit_query(sql, limit),
                "sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            }
            for index, sql in fragments
        ],
    }


def parse_sample_summary(
    summary: dict[str, Any],
    fragments: Sequence[tuple[int, str]],
    *,
    limit: int,
) -> dict[str, Any]:
    """Verify remote sample evidence and bound rows before returning them."""
    remote = {int(item.get("cell")): item for item in summary.get("fragments") or []}
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    truncated = False
    for index, sql in fragments:
        expected = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        item = remote.get(index)
        if item is None or item.get("sha256") != expected:
            errors.append(f"celda {index}: la evidencia de muestra no coincide con el SQL local")
            continue
        if not item.get("ok"):
            errors.append(f"celda {index}: {item.get('error') or 'la muestra falló'}")
            continue
        bounded = sanitize_rows(item.get("rows") or [])
        rows.extend(bounded.rows)
        truncated = truncated or bounded.truncated
        if len(rows) >= limit:
            rows = rows[:limit]
            truncated = True
            break
    return {"ok": not errors, "rows": rows[:limit], "truncated": truncated, "errors": errors}


def _bigquery_location(workbench_location: str) -> str:
    """Convert a zonal Workbench location to its BigQuery region."""
    if re.fullmatch(r"[a-z]+-[a-z]+\d+-[a-z]", workbench_location):
        return workbench_location.rsplit("-", 1)[0]
    return workbench_location


def _resolve_gcloud_account(account: Optional[str]) -> Optional[str]:
    if account:
        return account
    listed = subprocess.run(
        ["gcloud", "auth", "list", "--format=value(account)"],
        check=False,
        capture_output=True,
        text=True,
    )
    candidates = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    return candidates[0] if len(candidates) == 1 else None


def _gcloud_json(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise WorkbenchError(
            completed.stderr.strip() or completed.stdout.strip() or "gcloud no pudo consultar Workbench",
            kind="permission",
        )
    try:
        value = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as error:
        raise WorkbenchError("gcloud devolvió una respuesta inválida al consultar Workbench") from error
    if not isinstance(value, dict):
        raise WorkbenchError("La descripción de Workbench no es un objeto")
    return value


def _access_token(account: Optional[str]) -> str:
    resolved_account = _resolve_gcloud_account(account)
    if resolved_account:
        command = ["gcloud", f"--account={resolved_account}", "auth", "print-access-token"]
    else:
        # Cloud Shell may have credentials but no configured active account.
        # If exactly one account is available, use it without changing the
        # user's gcloud configuration; multiple accounts still require an
        # explicit --account to avoid choosing an identity silently.
        command = ["gcloud", "auth", "print-access-token"]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0 or not completed.stdout.strip():
        raise WorkbenchError(
            completed.stderr.strip() or "No se pudo obtener un token para Workbench",
            kind="permission",
        )
    return completed.stdout.strip()


def discover_proxy(settings: WorkbenchSettings, *, account: Optional[str] = None) -> str:
    command = ["gcloud"]
    resolved_account = _resolve_gcloud_account(account)
    if resolved_account:
        command.append(f"--account={resolved_account}")
    command.extend(
        [
            "workbench",
            "instances",
            "describe",
            settings.instance,
            f"--project={settings.project}",
            f"--location={settings.location}",
            "--format=json",
        ]
    )
    described = _gcloud_json(command)
    proxy = described.get("proxyUri") or described.get("proxyUriV2")
    if not proxy:
        raise WorkbenchError("La instancia Workbench no tiene proxyUri disponible", kind="transport")
    if not str(proxy).startswith(("http://", "https://")):
        proxy = f"https://{proxy}"
    return str(proxy).rstrip("/")


class _JupyterHttp:
    def __init__(self, base_url: str, token: str, *, timeout: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.cookies = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))
        self.xsrf: Optional[str] = None

    def _request(self, method: str, path: str, payload: Optional[dict[str, Any]] = None) -> Any:
        body = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if method != "GET" and self.xsrf:
            headers["X-XSRFToken"] = self.xsrf
        request = Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
        except Exception as error:  # urllib has several transport exception types
            raise WorkbenchError(f"No se pudo acceder al proxy de Workbench: {error}", kind="transport") from error
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as error:
            raise WorkbenchError("Workbench devolvió JSON inválido", kind="transport") from error

    def prepare(self) -> None:
        request = Request(
            self.base_url + "/tree",
            headers={"Authorization": f"Bearer {self.token}"},
            method="GET",
        )
        try:
            with self.opener.open(request, timeout=self.timeout):
                pass
        except Exception as error:
            raise WorkbenchError(f"No se pudo abrir JupyterLab en Workbench: {error}", kind="transport") from error
        for cookie in self.cookies:
            if cookie.name == "_xsrf":
                self.xsrf = cookie.value
                break
        if not self.xsrf:
            raise WorkbenchError("El proxy de Workbench no entregó cookie XSRF", kind="transport")

    def create_kernel(self) -> str:
        payload = self._request("POST", "/api/kernels", {"name": "python3"})
        kernel_id = payload.get("id") if isinstance(payload, dict) else None
        if not kernel_id:
            raise WorkbenchError("Jupyter no devolvió un identificador de kernel", kind="transport")
        return str(kernel_id)

    def delete_kernel(self, kernel_id: str) -> None:
        self._request("DELETE", f"/api/kernels/{kernel_id}")

    def cookie_header(self) -> str:
        return "; ".join(f"{cookie.name}={cookie.value}" for cookie in self.cookies)


def _remote_code(payload: dict[str, Any]) -> str:
    encoded_payload = repr(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return f"""
import json
import subprocess

payload = json.loads({encoded_payload})
fragments = []
for item in payload["fragments"]:
    command = [
        "bq", "query", "--use_legacy_sql=false", "--dry_run", "--format=json",
        f"--maximum_bytes_billed={{payload['maximum_bytes_billed']}}",
        f"--location={{payload['location']}}",
        f"--project_id={{payload['job_project']}}",
        item["sql"],
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    record = {{
        "cell": item["cell"],
        "sha256": item["sha256"],
        "ok": completed.returncode == 0,
        "dry_run": None,
        "bytes_processed": None,
        "bytes_billed": None,
        "error": completed.stderr.strip() or None,
    }}
    if completed.returncode == 0:
        try:
            response = json.loads(completed.stdout or "{{}}")
            configuration = response.get("configuration", {{}})
            statistics = response.get("statistics", {{}}).get("query", {{}})
            record["dry_run"] = configuration.get("dryRun") is True
            if statistics.get("totalBytesProcessed") is not None:
                record["bytes_processed"] = int(statistics["totalBytesProcessed"])
            if statistics.get("totalBytesBilled") is not None:
                record["bytes_billed"] = int(statistics["totalBytesBilled"])
            record["ok"] = record["dry_run"] is True
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            record["ok"] = False
            record["error"] = f"bq devolvió JSON inválido: {{error}}"
    fragments.append(record)

summary = {{
    "ok": all(item["ok"] for item in fragments),
    "project": payload["job_project"],
    "location": payload["location"],
    "maximum_bytes_billed": payload["maximum_bytes_billed"],
    "fragments": fragments,
}}
print("QUERYFLOW_RESULT=" + json.dumps(summary, ensure_ascii=False, sort_keys=True))
"""


def _remote_sample_code(payload: dict[str, Any]) -> str:
    encoded_payload = repr(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return f"""
import json
import subprocess

payload = json.loads({encoded_payload})
fragments = []
for item in payload["fragments"]:
    command = [
        "bq", "query", "--use_legacy_sql=false", "--format=json",
        f"--max_rows={{payload['limit']}}",
        f"--maximum_bytes_billed={{payload['maximum_bytes_billed']}}",
        f"--location={{payload['location']}}",
        f"--project_id={{payload['job_project']}}",
        item["sql"],
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    record = {{
        "cell": item["cell"],
        "sha256": item["sha256"],
        "ok": completed.returncode == 0,
        "rows": [],
        "error": completed.stderr.strip() or None,
    }}
    if completed.returncode == 0:
        try:
            response = json.loads(completed.stdout or "[]")
            record["rows"] = response if isinstance(response, list) else [response]
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            record["ok"] = False
            record["error"] = f"bq devolvió JSON inválido: {{error}}"
    fragments.append(record)

summary = {{"ok": all(item["ok"] for item in fragments), "fragments": fragments}}
print("QUERYFLOW_SAMPLE=" + json.dumps(summary, ensure_ascii=False, sort_keys=True))
"""


def _websocket_execute(
    http: _JupyterHttp,
    kernel_id: str,
    code: str,
    *,
    timeout: int,
) -> str:
    try:
        from websockets.sync.client import connect
    except ImportError as error:
        raise WorkbenchError(
            "Falta la dependencia websockets; instálala con requirements-queryflow.txt",
            kind="transport",
        ) from error
    parsed = urlparse(http.base_url)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    ws_url = f"{ws_scheme}://{parsed.netloc}/api/kernels/{kernel_id}/channels"
    session_id = str(uuid.uuid4())
    message_id = str(uuid.uuid4())
    message = {
        "header": {
            "msg_id": message_id,
            "username": "queryflow",
            "session": session_id,
            "msg_type": "execute_request",
            "version": "5.3",
        },
        "parent_header": {},
        "metadata": {},
        "content": {
            "code": code,
            "silent": False,
            "store_history": False,
            "user_expressions": {},
            "allow_stdin": False,
            "stop_on_error": True,
        },
        "channel": "shell",
    }
    output: list[str] = []
    try:
        with connect(
            ws_url,
            additional_headers={
                "Authorization": f"Bearer {http.token}",
                "Cookie": http.cookie_header(),
            },
            origin=http.base_url,
            open_timeout=timeout,
        ) as websocket:
            websocket.send(json.dumps(message, ensure_ascii=False))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                raw = websocket.recv(timeout=max(1, deadline - time.monotonic()))
                incoming = json.loads(raw)
                parent = incoming.get("parent_header") or {}
                if parent.get("msg_id") != message_id:
                    continue
                msg_type = (incoming.get("header") or {}).get("msg_type")
                content = incoming.get("content") or {}
                if msg_type == "stream":
                    output.append(str(content.get("text") or ""))
                elif msg_type == "error":
                    raise WorkbenchError(
                        f"El kernel de Workbench falló: {content.get('ename', 'Error')}: {content.get('evalue', '')}",
                        kind="transport",
                    )
                elif msg_type == "status" and content.get("execution_state") == "idle":
                    break
            else:
                raise WorkbenchError("La validación Workbench excedió el tiempo configurado", kind="transport")
    except WorkbenchError:
        raise
    except Exception as error:
        raise WorkbenchError(f"Falló el canal WebSocket de Workbench: {error}", kind="transport") from error
    return "".join(output)


def parse_workbench_summary(
    summary: dict[str, Any],
    fragments: Sequence[tuple[int, str]],
    *,
    maximum_bytes_billed: int,
) -> ValidationResult:
    """Convert remote evidence into QueryFlow's normal validation model."""
    static = validate_sql_fragments(fragments)
    remote = {int(item.get("cell")): item for item in summary.get("fragments") or []}
    errors: list[str] = []
    warnings: list[str] = []
    fragment_results: list[FragmentValidation] = []
    for index, sql in fragments:
        expected_hash = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        item = remote.get(index)
        if item is None or item.get("sha256") != expected_hash:
            errors.append(f"celda {index}: la evidencia Workbench no coincide con el SQL local")
            fragment_results.append(
                FragmentValidation(
                    index=index,
                    sha256=expected_hash,
                    references=validate_sql_fragments([(index, sql)]).references,
                    statement_class="read_only",
                    read_only=True,
                    dry_run_ok=False,
                    errors=["La evidencia remota no coincide con el hash local"],
                    error_kind="integrity",
                )
            )
            continue
        bytes_processed = item.get("bytes_processed")
        within_limit = bytes_processed is None or int(bytes_processed) <= maximum_bytes_billed
        if not within_limit:
            warning = (
                f"celda {index}: la estimación de {bytes_processed} bytes supera el límite de "
                f"{maximum_bytes_billed} bytes"
            )
            warnings.append(warning)
        item_error = item.get("error")
        item_error_kind = classify_error(str(item_error)) if item_error else ("unknown" if not item.get("ok") else None)
        if not item.get("ok"):
            errors.append(f"celda {index}: {item_error or 'el dry-run remoto falló'}")
        fragment_results.append(
            FragmentValidation(
                index=index,
                sha256=expected_hash,
                references=validate_sql_fragments([(index, sql)]).references,
                statement_class="read_only",
                read_only=True,
                dry_run_ok=bool(item.get("ok") and item.get("dry_run") is True),
                bytes_processed=int(bytes_processed) if bytes_processed is not None else None,
                bytes_billed=int(item["bytes_billed"]) if item.get("bytes_billed") is not None else None,
                maximum_bytes_billed=maximum_bytes_billed,
                within_configured_limit=within_limit,
                errors=[str(item_error)] if item_error else [],
                warnings=[warning] if not within_limit else [],
                error_kind=item_error_kind,
            )
        )
    error_kinds = {fragment.error_kind for fragment in fragment_results if fragment.error_kind}
    if "integrity" in error_kinds:
        error_kind = "integrity"
    elif len(error_kinds) == 1:
        error_kind = next(iter(error_kinds))
    elif error_kinds:
        error_kind = "unknown"
    else:
        error_kind = None
    return ValidationResult(
        references=static.references,
        statement_class=static.statement_class,
        read_only=static.read_only,
        dry_run_ok=not errors and all(fragment.dry_run_ok is True for fragment in fragment_results),
        bytes_processed=sum(
            fragment.bytes_processed or 0 for fragment in fragment_results
        ) if fragment_results else None,
        maximum_bytes_billed=maximum_bytes_billed,
        within_configured_limit=all(
            fragment.within_configured_limit is not False for fragment in fragment_results
        ),
        errors=errors,
        warnings=warnings,
        fragments=fragment_results,
        error_kind=error_kind,
    )


def validate_workbench_fragments(
    fragments: Sequence[tuple[int, str]],
    settings: WorkbenchSettings,
    *,
    maximum_bytes_billed: int,
    account: Optional[str] = None,
) -> WorkbenchValidation:
    """Run read-only dry-runs in an ephemeral Workbench kernel."""
    static = validate_sql_fragments(fragments)
    if static.errors or not static.read_only:
        return WorkbenchValidation(static, settings.to_dict())
    http: Optional[_JupyterHttp] = None
    kernel_id: Optional[str] = None
    try:
        token = _access_token(account)
        proxy = discover_proxy(settings, account=account)
        http = _JupyterHttp(proxy, token, timeout=settings.timeout_seconds)
        http.prepare()
        kernel_id = http.create_kernel()
        payload = {
            "job_project": settings.job_project,
            "location": _bigquery_location(settings.location),
            "maximum_bytes_billed": maximum_bytes_billed,
            "fragments": [
                {"cell": index, "sql": sql, "sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest()}
                for index, sql in fragments
            ],
        }
        output = _websocket_execute(http, kernel_id, _remote_code(payload), timeout=settings.timeout_seconds)
        marker = "QUERYFLOW_RESULT="
        result_line = next((line for line in output.splitlines() if line.startswith(marker)), None)
        if result_line is None:
            raise WorkbenchError("Workbench no devolvió QUERYFLOW_RESULT", kind="transport")
        summary = json.loads(result_line[len(marker):])
        result = parse_workbench_summary(
            summary,
            fragments,
            maximum_bytes_billed=maximum_bytes_billed,
        )
        details = settings.to_dict()
        details["backend"] = "workbench"
        return WorkbenchValidation(result, details)
    except WorkbenchError as error:
        failure = ValidationResult(
            references=static.references,
            statement_class=static.statement_class,
            read_only=static.read_only,
            dry_run_ok=False,
            maximum_bytes_billed=maximum_bytes_billed,
            within_configured_limit=None,
            errors=[str(error)],
            error_kind=error.kind,
        )
        return WorkbenchValidation(failure, {**settings.to_dict(), "backend": "workbench"})
    except (OSError, json.JSONDecodeError, ValueError) as error:
        failure = ValidationResult(
            references=static.references,
            statement_class=static.statement_class,
            read_only=static.read_only,
            dry_run_ok=False,
            maximum_bytes_billed=maximum_bytes_billed,
            errors=[f"La respuesta Workbench no se pudo interpretar: {error}"],
            error_kind="transport",
        )
        return WorkbenchValidation(failure, {**settings.to_dict(), "backend": "workbench"})
    finally:
        if kernel_id and http:
            try:
                http.delete_kernel(kernel_id)
            except WorkbenchError:
                pass


def execute_workbench_sample(
    fragments: Sequence[tuple[int, str]],
    settings: WorkbenchSettings,
    *,
    maximum_bytes_billed: int,
    limit: int,
    account: Optional[str] = None,
) -> dict[str, Any]:
    """Execute a bounded, explicitly approved sample inside Workbench."""
    static = validate_sql_fragments(fragments)
    if static.errors or not static.read_only:
        return {"ok": False, "rows": [], "truncated": False, "errors": static.errors or ["SQL no es de lectura pura"]}
    http: Optional[_JupyterHttp] = None
    kernel_id: Optional[str] = None
    try:
        token = _access_token(account)
        proxy = discover_proxy(settings, account=account)
        http = _JupyterHttp(proxy, token, timeout=settings.timeout_seconds)
        http.prepare()
        kernel_id = http.create_kernel()
        payload = build_sample_payload(
            fragments,
            maximum_bytes_billed=maximum_bytes_billed,
            limit=limit,
        )
        payload.update({
            "job_project": settings.job_project,
            "location": _bigquery_location(settings.location),
        })
        output = _websocket_execute(http, kernel_id, _remote_sample_code(payload), timeout=settings.timeout_seconds)
        marker = "QUERYFLOW_SAMPLE="
        result_line = next((line for line in output.splitlines() if line.startswith(marker)), None)
        if result_line is None:
            raise WorkbenchError("Workbench no devolvió QUERYFLOW_SAMPLE", kind="transport")
        summary = json.loads(result_line[len(marker):])
        return parse_sample_summary(summary, fragments, limit=limit)
    except WorkbenchError as error:
        return {"ok": False, "rows": [], "truncated": False, "errors": [str(error)], "error_kind": error.kind}
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return {"ok": False, "rows": [], "truncated": False, "errors": [str(error)], "error_kind": "transport"}
    finally:
        if kernel_id and http:
            try:
                http.delete_kernel(kernel_id)
            except WorkbenchError:
                pass
