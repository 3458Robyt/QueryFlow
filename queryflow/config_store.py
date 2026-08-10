"""Versioned, credential-free user configuration storage."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigStoreError(RuntimeError):
    """The user configuration cannot be read or written safely."""


FORBIDDEN_KEYS = {"token", "access_token", "refresh_token", "password", "secret", "private_key"}
CURRENT_SCHEMA_VERSION = 2
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "active_profile": self.active_profile,
            "profiles": self.profiles,
            "preferences": self.preferences,
        }


class ConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or default_config_path()).expanduser()

    def load(self) -> ConfigDocument:
        if not self.path.exists():
            return ConfigDocument(CURRENT_SCHEMA_VERSION, "pilot", {}, dict(DEFAULT_PREFERENCES))
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
        return ConfigDocument(
            schema_version=max(int(raw.get("schema_version", 1)), CURRENT_SCHEMA_VERSION),
            active_profile=str(raw.get("active_profile", "pilot")),
            profiles={str(key): dict(value) for key, value in profiles.items() if isinstance(value, dict)},
            preferences=preferences,
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
