from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .config_store import ConfigStoreError, default_config_path
from .policy import Policy


class ConfigError(RuntimeError):
    """QueryFlow configuration is missing or invalid."""


@dataclass(frozen=True)
class QueryflowConfig:
    workspace_root: Path
    catalog_path: Path
    audit_root: Optional[str]
    mode: str = "pilot"
    profile_max_bytes: int = 1_073_741_824
    catalog_ttl_hours: int = 24
    profile_ttl_hours: int = 168
    allow_update_existing: bool = False
    allow_delete: bool = False
    allow_create_dataset: bool = False
    validation_backend: str = "local"
    workbench_project: Optional[str] = None
    workbench_location: Optional[str] = None
    workbench_instance: Optional[str] = None
    workbench_job_project: Optional[str] = None
    workbench_timeout_seconds: int = 240
    profile_name: str = "pilot"
    source_projects: tuple[str, ...] = ()
    destination_projects: tuple[str, ...] = ()
    allowed_locations: tuple[str, ...] = ()
    sample_default_rows: int = 3
    sample_max_rows: int = 5
    policy_max_bytes: int = 10 * 1024 * 1024 * 1024
    policy_enforced: bool = False


def _value(raw: str) -> Any:
    value = raw.strip()
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "None", ""}:
        return None
    try:
        return int(value)
    except ValueError:
        return value.strip('"\'')


def load_config(path: Optional[Path] = None) -> QueryflowConfig:
    path = path or _discover_config_path()
    if path.suffix.lower() == ".toml":
        return _load_toml_config(path)
    raw: dict[str, Any] = {}
    if path.exists():
        text = path.read_text(encoding="utf-8")
        try:
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise ConfigError("La configuración JSON debe ser un objeto")
            raw = parsed
        except json.JSONDecodeError:
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or ":" not in stripped:
                    continue
                key, value = stripped.split(":", 1)
                raw[key.strip()] = _value(value)
    home = Path.home()
    workspace_root = Path(str(raw.get("workspace_root") or home / "queryflow" / "tasks")).expanduser()
    catalog_path = Path(str(raw.get("catalog_path") or home / "queryflow" / "catalog.json")).expanduser()
    audit_value = raw.get("audit_root")
    audit_root = str(audit_value) if audit_value else None
    mode = str(raw.get("mode") or "pilot")
    if mode not in {"pilot", "team"}:
        raise ConfigError("mode debe ser pilot o team")
    validation_backend = str(raw.get("validation_backend") or "local")
    if validation_backend not in {"local", "workbench"}:
        raise ConfigError("validation_backend debe ser local o workbench")
    workbench_timeout_seconds = int(raw.get("workbench_timeout_seconds") or 240)
    if workbench_timeout_seconds <= 0:
        raise ConfigError("workbench_timeout_seconds debe ser mayor que cero")
    return QueryflowConfig(
        workspace_root=workspace_root,
        catalog_path=catalog_path,
        audit_root=audit_root,
        mode=mode,
        profile_max_bytes=int(raw.get("profile_max_bytes") or 1_073_741_824),
        catalog_ttl_hours=int(raw.get("catalog_ttl_hours") or 24),
        profile_ttl_hours=int(raw.get("profile_ttl_hours") or 168),
        allow_update_existing=bool(raw.get("allow_update_existing", False)),
        allow_delete=bool(raw.get("allow_delete", False)),
        allow_create_dataset=bool(raw.get("allow_create_dataset", False)),
        validation_backend=validation_backend,
        workbench_project=(str(raw["workbench_project"]) if raw.get("workbench_project") else None),
        workbench_location=(str(raw["workbench_location"]) if raw.get("workbench_location") else None),
        workbench_instance=(str(raw["workbench_instance"]) if raw.get("workbench_instance") else None),
        workbench_job_project=(str(raw["workbench_job_project"]) if raw.get("workbench_job_project") else None),
        workbench_timeout_seconds=workbench_timeout_seconds,
        source_projects=_string_tuple(raw.get("source_projects")),
        destination_projects=_string_tuple(raw.get("destination_projects")),
        allowed_locations=_string_tuple(raw.get("allowed_locations")),
        sample_default_rows=int(raw.get("sample_default_rows") or 3),
        sample_max_rows=min(int(raw.get("sample_max_rows") or 5), 5),
        policy_max_bytes=min(int(raw.get("policy_max_bytes") or 10 * 1024 * 1024 * 1024), 10 * 1024 * 1024 * 1024),
    )


def _discover_config_path() -> Path:
    explicit = os.environ.get("QUERYFLOW_CONFIG_PATH")
    if explicit:
        return Path(explicit).expanduser()
    modern = default_config_path()
    if modern.exists():
        return modern
    return Path("queryflow.yaml")


def _load_toml_config(path: Path) -> QueryflowConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, ConfigStoreError) as error:
        raise ConfigError(f"No se pudo leer la configuración TOML: {error}") from error
    if not isinstance(raw, dict):
        raise ConfigError("La configuración TOML debe ser un objeto")
    active = str(raw.get("active_profile") or os.environ.get("QUERYFLOW_PROFILE") or "pilot")
    profiles = raw.get("profiles") or {}
    profile = profiles.get(active) if isinstance(profiles, dict) else None
    if not isinstance(profile, dict):
        profile = {}
    policy = Policy.from_mapping(raw.get("policy") if isinstance(raw.get("policy"), dict) else None)
    mode = str(profile.get("mode") or ("team" if active == "team" else "pilot"))
    if mode not in {"pilot", "team"}:
        raise ConfigError("mode debe ser pilot o team")
    max_bytes = int(profile.get("max_bytes") or policy.default_max_bytes)
    max_bytes = min(max_bytes, policy.max_bytes)
    workspace_root = Path(str(profile.get("workspace_root") or Path.home() / ".queryflow" / "tasks")).expanduser()
    catalog_path = Path(str(profile.get("catalog_path") or Path.home() / ".queryflow" / "catalog.json")).expanduser()
    audit_root = str(profile.get("audit_root") or Path.home() / ".queryflow" / "audit")
    validation_backend = str(profile.get("validation_backend") or "workbench")
    if validation_backend not in {"local", "workbench"}:
        raise ConfigError("validation_backend debe ser local o workbench")
    timeout = int(profile.get("workbench_timeout_seconds") or 240)
    if timeout <= 0:
        raise ConfigError("workbench_timeout_seconds debe ser mayor que cero")
    return QueryflowConfig(
        workspace_root=workspace_root,
        catalog_path=catalog_path,
        audit_root=audit_root,
        mode=mode,
        profile_max_bytes=max_bytes,
        catalog_ttl_hours=int(profile.get("catalog_ttl_hours") or 24),
        profile_ttl_hours=int(profile.get("profile_ttl_hours") or 168),
        allow_update_existing=bool(profile.get("allow_update_existing", mode == "team")),
        allow_delete=False,
        allow_create_dataset=False,
        validation_backend=validation_backend,
        workbench_project=_optional_text(profile.get("workbench_project")),
        workbench_location=_optional_text(profile.get("workbench_location")),
        workbench_instance=_optional_text(profile.get("workbench_instance")),
        workbench_job_project=_optional_text(profile.get("workbench_job_project")),
        workbench_timeout_seconds=timeout,
        profile_name=active,
        source_projects=_string_tuple(profile.get("source_projects")),
        destination_projects=_string_tuple(profile.get("destination_projects")),
        allowed_locations=_string_tuple(profile.get("allowed_locations")),
        sample_default_rows=min(int(profile.get("sample_default_rows") or policy.sample_default_rows), policy.sample_max_rows),
        sample_max_rows=min(int(profile.get("sample_max_rows") or policy.sample_max_rows), policy.sample_max_rows),
        policy_max_bytes=policy.max_bytes,
        policy_enforced=True,
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, (list, tuple, set)):
        raise ConfigError("Las listas de configuración deben contener textos")
    return tuple(str(item) for item in value if str(item).strip())


def _optional_text(value: Any) -> Optional[str]:
    return str(value) if value else None
