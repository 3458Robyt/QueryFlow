"""Safe, structured diagnostics shared by the CLI, tasks and Web Preview.

The module deliberately contains no provider client code.  It turns failures
from the different adapters into a stable, redacted envelope that an agent or
an analyst can copy without accidentally exporting credentials, query text or
result rows.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


DIAGNOSTIC_SCHEMA_VERSION = 1
DIAGNOSTIC_DIRECTORY = "diagnostics"

ERROR_CATEGORIES = frozenset(
    {
        "authentication",
        "iam",
        "vpc",
        "network",
        "sql",
        "configuration",
        "policy",
        "not_found",
        "conflict",
        "integrity",
        "unexpected",
    }
)

_VPC_ID = re.compile(
    r"vpcServiceControlsUniqueIdentifier\s*(?:[:=]|is)?\s*[\"']?([A-Za-z0-9_-]{4,128})",
    re.IGNORECASE,
)
_STATUS = re.compile(r"\b(?:HTTP\s*)?(4\d\d|5\d\d)\b")
_SECRET = re.compile(
    r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+|(?:access[_ -]?token|refresh[_ -]?token|password|secret)\s*[:=]\s*[^\s,;]+"
)
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_-]{80,}\b")
_SQL_START = re.compile(
    r"(?is)\b(?:select|with|insert|update|delete|merge|create|alter|drop)\b"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def redact_message(message: str) -> str:
    """Remove obvious credentials and oversized opaque values from a message."""
    value = str(message or "").strip()
    sql_match = _SQL_START.search(value)
    if sql_match:
        value = value[: sql_match.start()].rstrip() + " [SQL_REDACTED]"
    value = _SECRET.sub(
        lambda match: f"{match.group(1) if match.group(1) else ''}[REDACTED]", value
    )
    value = _LONG_TOKEN.sub("[REDACTED]", value)
    return value[:1200] or "QueryFlow no recibió un mensaje del proveedor"


def classify_message(message: str) -> str:
    """Classify common adapter failures using stable product categories."""
    lowered = str(message or "").lower()
    if (
        "vpc service controls" in lowered
        or "organization's policy" in lowered
        or "vpcservicecontrolsuniqueidentifier" in lowered
    ):
        return "vpc"
    if any(
        token in lowered
        for token in (
            "unauthenticated",
            "authentication",
            "invalid_grant",
            "token expired",
            "no se pudo obtener un token",
        )
    ):
        return "authentication"
    if any(
        token in lowered
        for token in (
            "permission_denied",
            "permission denied",
            "access denied",
            "forbidden",
            "iam",
        )
    ):
        return "iam"
    if any(
        token in lowered
        for token in (
            "not_found",
            "not found",
            "no existe",
            "no encontrado",
            "notfound",
        )
    ):
        return "not_found"
    if any(
        token in lowered
        for token in ("conflict", "already exists", "cambió después", "race")
    ):
        return "conflict"
    if any(
        token in lowered
        for token in ("hash", "digest", "integrity", "integridad", "no coincide")
    ):
        return "integrity"
    if any(
        token in lowered
        for token in (
            "syntax error",
            "unrecognized name",
            "invalid query",
            "not found: table",
            "sql",
        )
    ):
        return "sql"
    if any(
        token in lowered
        for token in (
            "timed out",
            "timeout",
            "connection",
            "could not connect",
            "network",
            "servernotfound",
        )
    ):
        return "network"
    if any(
        token in lowered
        for token in ("configuración", "configuration", "config", "argumento", "flag")
    ):
        return "configuration"
    if "policy" in lowered or "política" in lowered:
        return "policy"
    return "unexpected"


def provider_identifiers(message: str) -> dict[str, str]:
    """Extract identifiers useful to a GCP administrator, never payload data."""
    value = str(message or "")
    identifiers: dict[str, str] = {}
    vpc_match = _VPC_ID.search(value)
    if vpc_match:
        identifiers["vpcServiceControlsUniqueIdentifier"] = vpc_match.group(1)
    status_match = _STATUS.search(value)
    if status_match:
        identifiers["http_status"] = status_match.group(1)
    return identifiers


def _safe_context(context: Mapping[str, Any] | None) -> dict[str, Any]:
    allowed = {
        "task",
        "task_id",
        "project",
        "location",
        "backend",
        "stage",
        "workbench_instance",
        "account",
    }
    result: dict[str, Any] = {}
    for key, value in (context or {}).items():
        if key not in allowed or value in (None, ""):
            continue
        text = str(value)
        if key == "account":
            # An account is useful for support but should not become an email
            # harvesting surface in a copied diagnostic.
            text = text[:2] + "…" + text[-12:] if len(text) > 16 else text
        result[key] = text[:240]
    return result


@dataclass(frozen=True)
class Diagnostic:
    error_id: str
    category: str
    stage: str
    message: str
    recovery: tuple[str, ...] = ()
    retryable: bool = False
    occurred_at: str = field(default_factory=_now)
    context: dict[str, Any] = field(default_factory=dict)
    provider: dict[str, Any] = field(default_factory=dict)
    schema_version: int = DIAGNOSTIC_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "error_id": self.error_id,
            "category": self.category,
            "stage": self.stage,
            "message": self.message,
            "recovery": list(self.recovery),
            "retryable": self.retryable,
            "occurred_at": self.occurred_at,
            "context": dict(self.context),
            "provider": dict(self.provider),
        }

    def to_markdown(self) -> str:
        lines = [
            "# Diagnóstico QueryFlow",
            "",
            f"- ID: `{self.error_id}`",
            f"- Categoría: `{self.category}`",
            f"- Etapa: `{self.stage}`",
            f"- Reintentable: `{'sí' if self.retryable else 'no'}`",
            f"- Mensaje: {self.message}",
        ]
        if self.recovery:
            lines += ["", "## Recuperación", ""] + [
                f"- {item}" for item in self.recovery
            ]
        if self.provider:
            lines += [
                "",
                "## Identificadores del proveedor",
                "",
                "```json",
                json.dumps(self.provider, ensure_ascii=False, indent=2, sort_keys=True),
                "```",
            ]
        if self.context:
            lines += [
                "",
                "## Contexto seguro",
                "",
                "```json",
                json.dumps(self.context, ensure_ascii=False, indent=2, sort_keys=True),
                "```",
            ]
        return "\n".join(lines) + "\n"


def make_diagnostic(
    error: BaseException | str,
    *,
    stage: str,
    category: str | None = None,
    context: Mapping[str, Any] | None = None,
    recovery: list[str] | tuple[str, ...] | None = None,
    retryable: bool | None = None,
) -> Diagnostic:
    raw = str(error)
    resolved_category = (
        category if category in ERROR_CATEGORIES else classify_message(raw)
    )
    default_recovery = {
        "authentication": ("Renueva gcloud auth y vuelve a ejecutar la etapa.",),
        "iam": (
            "Solicita el permiso GCP indicado al administrador y repite la operación.",
        ),
        "vpc": (
            "Entrega el error_id y el identificador VPC al administrador de seguridad.",
        ),
        "network": (
            "Comprueba conectividad y configuración de Workbench; después reintenta.",
        ),
        "sql": ("Corrige el SQL señalado y genera una nueva validación.",),
        "configuration": (
            "Revisa queryflow doctor y corrige la configuración indicada.",
        ),
        "policy": ("Ajusta la operación al perfil y las allowlists vigentes.",),
        "not_found": (
            "Actualiza el catálogo y confirma el nombre canónico del recurso.",
        ),
        "conflict": (
            "Vuelve a exportar el recurso y revisa los cambios remotos antes de publicar.",
        ),
        "integrity": (
            "No publiques: regenera la validación y el digest desde la tarea actual.",
        ),
        "unexpected": (
            "Conserva este diagnóstico y revisa los logs locales de la tarea.",
        ),
    }
    can_retry = (
        retryable
        if retryable is not None
        else resolved_category in {"authentication", "network", "vpc"}
    )
    token = secrets.token_hex(4).upper()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return Diagnostic(
        error_id=f"QF-{stamp}-{token}",
        category=resolved_category,
        stage=str(stage or "unknown"),
        message=redact_message(raw),
        recovery=tuple(recovery or default_recovery[resolved_category]),
        retryable=bool(can_retry),
        context=_safe_context(context),
        provider={"identifiers": provider_identifiers(raw)},
    )


def write_diagnostic(task: Path, diagnostic: Diagnostic) -> Path:
    """Persist a diagnostic atomically inside a task and return its path."""
    directory = task / DIAGNOSTIC_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{diagnostic.error_id}.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(diagnostic.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    (task / "latest-diagnostic.json").write_text(
        json.dumps(diagnostic.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return target


def read_diagnostic(task: Path) -> Diagnostic | None:
    path = task / "latest-diagnostic.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return Diagnostic(
            error_id=str(raw["error_id"]),
            category=str(raw["category"]),
            stage=str(raw["stage"]),
            message=str(raw["message"]),
            recovery=tuple(str(item) for item in raw.get("recovery") or []),
            retryable=bool(raw.get("retryable")),
            occurred_at=str(raw.get("occurred_at") or ""),
            context=dict(raw.get("context") or {}),
            provider=dict(raw.get("provider") or {}),
            schema_version=int(raw.get("schema_version", DIAGNOSTIC_SCHEMA_VERSION)),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def clear_latest_diagnostic(task: Path) -> None:
    """Clear only the pointer to the latest failure; historical files remain."""
    try:
        (task / "latest-diagnostic.json").unlink(missing_ok=True)
    except OSError:
        pass


def exception_digest(payload: Mapping[str, Any]) -> str:
    """Compute the independent approval digest for a static exception."""
    canonical = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
