"""Bounded, explicit sample execution helpers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from .validation import validate_sql_text


@dataclass(frozen=True)
class SanitizedRows:
    rows: list[dict[str, Any]]
    truncated: bool
    payload_bytes: int


def limit_query(sql: str, limit: int) -> str:
    if limit < 1 or limit > 5:
        raise ValueError("El límite de muestra debe estar entre 1 y 5")
    static = validate_sql_text(sql)
    if not static.read_only or static.errors:
        raise ValueError("La muestra requiere una consulta de lectura pura válida")
    query = sql.strip().rstrip(";").strip()
    return f"SELECT * FROM (\n{query}\n) AS queryflow_sample\nLIMIT {limit}"


def execution_digest(sql: str, *, fragment_index: int, limit: int) -> str:
    payload = {
        "sql_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        "fragment_index": fragment_index,
        "limit": limit,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def sanitize_rows(
    rows: Iterable[dict[str, Any]],
    *,
    max_columns: int = 25,
    max_value_chars: int = 256,
    max_payload_bytes: int = 32 * 1024,
) -> SanitizedRows:
    output: list[dict[str, Any]] = []
    truncated = False
    for row in rows:
        if not isinstance(row, dict):
            truncated = True
            continue
        sanitized: dict[str, Any] = {}
        for index, (key, value) in enumerate(row.items()):
            if index >= max_columns:
                truncated = True
                break
            encoded = json.dumps(value, ensure_ascii=False, default=str)
            if len(encoded) > max_value_chars:
                encoded = encoded[:max_value_chars]
                value = encoded + "…"
                truncated = True
            sanitized[str(key)] = value
        output.append(sanitized)
        payload = len(json.dumps(output, ensure_ascii=False, default=str).encode("utf-8"))
        if payload > max_payload_bytes:
            truncated = True
            output.pop()
            break
    payload_bytes = len(json.dumps(output, ensure_ascii=False, default=str).encode("utf-8"))
    return SanitizedRows(output, truncated, payload_bytes)
