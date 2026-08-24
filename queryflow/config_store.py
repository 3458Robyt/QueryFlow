"""Versioned, credential-free user configuration storage."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigStoreError(RuntimeError):
    """The user configuration cannot be read or written safely."""


FORBIDDEN_KEYS = {"token", "access_token", "refresh_token", "password", "secret", "private_key"}
CURRENT_SCHEMA_VERSION = 4
DEFAULT_PREFERENCES = {
    "review_theme": "dark",
    "review_mode": "unified",
    "review_only_changes": True,
    "review_context_lines": 3,
}


@dataclass(frozen=True)
class ConfigDocument:
    schema_version: int
    active_profile: str
    profiles: dict[str, dict[str, Any]]
    preferences: dict[str, Any]
    project_aliases: dict[str, str] = field(default_factory=dict)
    context: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "active_profile": self.active_profile,
            "profiles": self.profiles,
            "preferences": self.preferences,
            "project_aliases": self.project_aliases,
            "context": self.context,
        }


class ConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or default_config_path()).expanduser()

    def load(self) -> ConfigDocument:
        if not self.path.exists():
            return ConfigDocument(CURRENT_SCHEMA_VERSION, "pilot", {}, dict(DEFAULT_PREFERENCES), {}, {})
        try:
            raw = tomllib.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ConfigStoreError(f"No se pudo leer la configuración: {error}") from error
        if not isinstance(raw, dict):
            raise ConfigStoreError("La configuración debe ser un objeto TOML")
        profiles = raw.get("profiles") or {}
        preferences = raw.get("preferences") or {}
        if not isinstance(profiles, dict) or not isinstance(preferences, dict):
            raise ConfigStoreError("profiles y preferences deben ser tablas TOML")
        preferences = {**DEFAULT_PREFERENCES, **dict(preferences)}
        aliases = raw.get("project_aliases") or {}
        context = raw.get("context") or {}
        if not isinstance(aliases, dict) or not isinstance(context, dict):
            raise ConfigStoreError("project_aliases y context deben ser tablas TOML")
        project_aliases = {str(key): str(value) for key, value in aliases.items()}
        project_context = {
            str(key): str(value)
            for key, value in context.items()
            if str(key) in {"source_project", "destination_project"} and value
        }
        raw_schema_version = int(raw.get("schema_version", 1))
        # Preserve the v1 compatibility marker expected by older task/config
        # readers; any newly written or already-v2 document is upgraded to the
        # context-aware schema v4.
        schema_version = 2 if raw_schema_version <= 1 else max(raw_schema_version, CURRENT_SCHEMA_VERSION)
        return ConfigDocument(
            schema_version=schema_version,
            active_profile=str(raw.get("active_profile", "pilot")),
            profiles={str(key): dict(value) for key, value in profiles.items() if isinstance(value, dict)},
            preferences=preferences,
            project_aliases=project_aliases,
            context=project_context,
        )

    def initialize(self, *, profile: str, values: dict[str, Any], preferences: dict[str, Any] | None = None) -> ConfigDocument:
        _assert_safe_mapping(values)
        document = self.load()
        profiles = dict(document.profiles)
        profiles[profile] = dict(values)
        merged = ConfigDocument(
            schema_version=CURRENT_SCHEMA_VERSION,
            active_profile=profile,
            profiles=profiles,
            preferences={**DEFAULT_PREFERENCES, **document.preferences, **(preferences or {})},
            project_aliases=dict(document.project_aliases),
            context=dict(document.context),
        )
        self.write(merged)
        return merged

    def set_value(self, dotted_key: str, value: Any) -> ConfigDocument:
        parts = [part for part in dotted_key.split(".") if part]
        if not parts or any(part.lower() in FORBIDDEN_KEYS for part in parts):
            raise ConfigStoreError("La configuración no admite credenciales")
        document = self.load()
        data = document.to_dict()
        cursor: dict[str, Any] = data
        for part in parts[:-1]:
            child = cursor.setdefault(part, {})
            if not isinstance(child, dict):
                raise ConfigStoreError(f"La ruta de configuración no es una tabla: {part}")
            cursor = child
        _assert_safe_value(value)
        cursor[parts[-1]] = value
        updated = ConfigDocument(
            schema_version=max(int(data.get("schema_version", 1)), CURRENT_SCHEMA_VERSION),
            active_profile=str(data.get("active_profile", "pilot")),
            profiles=dict(data.get("profiles") or {}),
            preferences={**DEFAULT_PREFERENCES, **dict(data.get("preferences") or {})},
            project_aliases={str(key): str(item) for key, item in (data.get("project_aliases") or {}).items()},
            context={str(key): str(item) for key, item in (data.get("context") or {}).items()},
        )
        self.write(updated)
        return updated

    def activate_profile(self, profile: str) -> ConfigDocument:
        document = self.load()
        if profile not in document.profiles:
            raise ConfigStoreError(f"No existe el perfil: {profile}")
        updated = ConfigDocument(
            schema_version=max(document.schema_version, CURRENT_SCHEMA_VERSION),
            active_profile=profile,
            profiles=dict(document.profiles),
            preferences=dict(document.preferences),
            project_aliases=dict(document.project_aliases),
            context=dict(document.context),
        )
        self.write(updated)
        return updated

    def set_context(self, *, source_project: str, destination_project: str) -> ConfigDocument:
        document = self.load()
        context = {
            "source_project": source_project,
            "destination_project": destination_project,
        }
        updated = ConfigDocument(
            schema_version=max(document.schema_version, CURRENT_SCHEMA_VERSION),
            active_profile=document.active_profile,
            profiles=dict(document.profiles),
            preferences=dict(document.preferences),
            project_aliases=dict(document.project_aliases),
            context=context,
        )
        self.write(updated)
        return updated

    def set_alias(self, alias: str, project: str) -> ConfigDocument:
        alias = str(alias).strip()
        project = str(project).strip()
        if not alias or not project or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in alias):
            raise ConfigStoreError("El alias debe ser un identificador simple y el proyecto no puede estar vacío")
        if "=" in project or any(char.isspace() for char in project):
            raise ConfigStoreError("El proyecto no puede contener espacios ni '='")
        document = self.load()
        aliases = dict(document.project_aliases)
        aliases[alias] = project
        updated = ConfigDocument(
            schema_version=max(document.schema_version, CURRENT_SCHEMA_VERSION),
            active_profile=document.active_profile,
            profiles=dict(document.profiles),
            preferences=dict(document.preferences),
            project_aliases=aliases,
            context=dict(document.context),
        )
        self.write(updated)
        return updated

    def write(self, document: ConfigDocument) -> None:
        _assert_safe_mapping(document.to_dict())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(_to_toml(document.to_dict()), encoding="utf-8")
        os.replace(temporary, self.path)


def default_config_path() -> Path:
    override = os.environ.get("QUERYFLOW_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "queryflow" / "config.toml"


def _assert_safe_mapping(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                raise ConfigStoreError(f"La configuración no admite la clave {key}")
            _assert_safe_mapping(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_safe_mapping(child)
    else:
        _assert_safe_value(value)


def _assert_safe_value(value: Any) -> None:
    if isinstance(value, str) and any(word in value.lower() for word in ("bearer ", "private_key", "-----begin")):
        raise ConfigStoreError("La configuración no admite secretos")
    if isinstance(value, (dict, list, tuple)):
        _assert_safe_mapping(value)


def _to_toml(value: dict[str, Any], prefix: tuple[str, ...] = ()) -> str:
    lines: list[str] = []
    scalars = {key: child for key, child in value.items() if not isinstance(child, dict)}
    tables = {key: child for key, child in value.items() if isinstance(child, dict)}
    for key, child in scalars.items():
        lines.append(f"{key} = {_toml_value(child)}")
    for key, child in tables.items():
        lines.append("")
        table_name = ".".join((*prefix, key))
        lines.append(f"[{table_name}]")
        nested = _to_toml(child, (*prefix, key)).rstrip()
        if nested:
            lines.append(nested)
    return "\n".join(lines).rstrip() + "\n"


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if value is None:
        return '""'
    return json.dumps(str(value), ensure_ascii=False)
