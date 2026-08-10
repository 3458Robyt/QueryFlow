"""Non-relaxable safety policy for QueryFlow operations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable


DEFAULT_MAX_BYTES = 10 * 1024 * 1024 * 1024
DEFAULT_PROFILE_MAX_BYTES = 5 * 1024 * 1024 * 1024
DEFAULT_SAMPLE_ROWS = 3
MAX_SAMPLE_ROWS = 5


class PolicyError(RuntimeError):
    """The effective QueryFlow policy is invalid."""


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    rule: str
    message: str
    severity: str = "block"

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "allowed": self.allowed,
            "rule": self.rule,
            "message": self.message,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class Policy:
    """Effective policy after built-in defaults and local restrictions."""

    schema_version: int = 1
    max_bytes: int = DEFAULT_MAX_BYTES
    default_max_bytes: int = DEFAULT_PROFILE_MAX_BYTES
    sample_default_rows: int = DEFAULT_SAMPLE_ROWS
    sample_max_rows: int = MAX_SAMPLE_ROWS
    allow_sql_execution: bool = False
    allow_delete: bool = False
    allow_scheduled_queries: bool = False
    allow_update_existing: bool = False
    allow_static_exception: bool = False
    allowed_resource_kinds: tuple[str, ...] = ("notebook", "shared_query")
    allowed_source_projects: tuple[str, ...] = ()
    allowed_destination_projects: tuple[str, ...] = ()
    allowed_locations: tuple[str, ...] = ()

    @classmethod
    def default(cls) -> "Policy":
        return cls()

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "Policy":
        raw = raw or {}
        base = cls.default()
        max_bytes = _positive_int(raw.get("max_bytes"), base.max_bytes, "max_bytes")
        default_max = _positive_int(
            raw.get("default_max_bytes"), base.default_max_bytes, "default_max_bytes"
        )
        if default_max > max_bytes:
            default_max = max_bytes
        sample_default = _positive_int(
            raw.get("sample_default_rows"), base.sample_default_rows, "sample_default_rows"
        )
        sample_max = _positive_int(raw.get("sample_max_rows"), base.sample_max_rows, "sample_max_rows")
        if sample_max > MAX_SAMPLE_ROWS:
            sample_max = MAX_SAMPLE_ROWS
        if sample_default > sample_max:
            sample_default = sample_max
        allowed_kinds = _string_tuple(raw.get("allowed_resource_kinds"), base.allowed_resource_kinds)
        if not set(allowed_kinds).issubset(set(base.allowed_resource_kinds)):
            raise PolicyError("allowed_resource_kinds contiene tipos no permitidos en v1")
        return cls(
            schema_version=int(raw.get("schema_version", base.schema_version)),
            max_bytes=min(max_bytes, base.max_bytes),
            default_max_bytes=min(default_max, base.default_max_bytes),
            sample_default_rows=sample_default,
            sample_max_rows=sample_max,
            allow_sql_execution=bool(raw.get("allow_sql_execution", base.allow_sql_execution)),
            allow_delete=bool(raw.get("allow_delete", base.allow_delete)),
            allow_scheduled_queries=bool(raw.get("allow_scheduled_queries", base.allow_scheduled_queries)),
            allow_update_existing=bool(raw.get("allow_update_existing", base.allow_update_existing)),
            allow_static_exception=bool(raw.get("allow_static_exception", base.allow_static_exception)),
            allowed_resource_kinds=allowed_kinds,
            allowed_source_projects=_string_tuple(raw.get("allowed_source_projects"), base.allowed_source_projects),
            allowed_destination_projects=_string_tuple(
                raw.get("allowed_destination_projects"), base.allowed_destination_projects
            ),
            allowed_locations=_string_tuple(raw.get("allowed_locations"), base.allowed_locations),
        )

    def with_user_limit(self, requested: int | None) -> "Policy":
        if requested is None:
            return self
        if requested <= 0:
            raise PolicyError("El límite de bytes debe ser mayor que cero")
        return replace(self, max_bytes=min(self.max_bytes, requested))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "max_bytes": self.max_bytes,
            "default_max_bytes": self.default_max_bytes,
            "sample_default_rows": self.sample_default_rows,
            "sample_max_rows": self.sample_max_rows,
            "allow_sql_execution": self.allow_sql_execution,
            "allow_delete": self.allow_delete,
            "allow_scheduled_queries": self.allow_scheduled_queries,
            "allow_update_existing": self.allow_update_existing,
            "allow_static_exception": self.allow_static_exception,
            "allowed_resource_kinds": list(self.allowed_resource_kinds),
            "allowed_source_projects": list(self.allowed_source_projects),
            "allowed_destination_projects": list(self.allowed_destination_projects),
            "allowed_locations": list(self.allowed_locations),
        }


def evaluate_policy(
    policy: Policy,
    *,
    operation: str,
    resource_kind: str,
    mode: str = "copy",
    source_project: str | None = None,
    destination_project: str | None = None,
    location: str | None = None,
) -> PolicyDecision:
    if resource_kind not in policy.allowed_resource_kinds:
        return PolicyDecision(False, f"resource_kind.{resource_kind}", f"El tipo {resource_kind} no está habilitado")
    if resource_kind == "scheduled_query" and not policy.allow_scheduled_queries:
        return PolicyDecision(False, "resource_kind.scheduled_query", "Las consultas programadas están deshabilitadas en v1")
    if operation in {"delete", "remove"} and not policy.allow_delete:
        return PolicyDecision(False, "operation.delete", "QueryFlow no permite eliminar recursos")
    if operation == "execute" and not policy.allow_sql_execution:
        return PolicyDecision(False, "operation.execute", "La ejecución requiere una política explícita")
    if mode == "update" and not policy.allow_update_existing:
        return PolicyDecision(False, "mode.update", "El perfil actual solo permite copias y recursos nuevos")
    if policy.allowed_source_projects and source_project not in policy.allowed_source_projects:
        return PolicyDecision(False, "source_project.allowlist", "El proyecto origen no está permitido")
    if (
        policy.allowed_destination_projects
        and destination_project is not None
        and destination_project not in policy.allowed_destination_projects
    ):
        return PolicyDecision(False, "destination_project.allowlist", "El proyecto destino no está permitido")
    if policy.allowed_locations and location not in policy.allowed_locations:
        return PolicyDecision(False, "location.allowlist", "La ubicación no está permitida")
    return PolicyDecision(True, "policy.ok", "Operación permitida", severity="info")


def _positive_int(value: Any, default: int, name: str) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise PolicyError(f"{name} debe ser entero") from error
    if parsed <= 0:
        raise PolicyError(f"{name} debe ser mayor que cero")
    return parsed


def _string_tuple(value: Any, default: Iterable[str]) -> tuple[str, ...]:
    if value is None:
        return tuple(default)
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, (list, tuple, set)):
        raise PolicyError("Las listas de política deben contener textos")
    return tuple(str(item) for item in value if str(item).strip())
