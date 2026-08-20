from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .validation import dry_run_sql, subprocess_runner, ValidationResult
from .gcloud import GcloudContext


class ProfileError(RuntimeError):
    """A safe aggregate profile could not be generated."""


IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")
MIN_MAX_TYPES = {"INT64", "FLOAT64", "NUMERIC", "BIGNUMERIC", "DECIMAL", "BIGDECIMAL", "DATE", "DATETIME", "TIME", "TIMESTAMP"}


def _quote_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ProfileError(f"Nombre de columna inválido: {value}")
    return f"`{value}`"


def build_profile_query(table_ref: str, schema: list[dict[str, Any]]) -> str:
    if not IDENTIFIER.fullmatch(table_ref):
        raise ProfileError("La tabla debe usar proyecto.dataset.tabla")
    expressions = ["COUNT(*) AS __qf_row_count"]
    for field in schema:
        name = field.get("name")
        field_type = str(field.get("type") or "").upper()
        mode = str(field.get("mode") or "NULLABLE").upper()
        if not isinstance(name, str) or mode == "REPEATED" or field_type == "RECORD":
            continue
        quoted = _quote_identifier(name)
        alias = re.sub(r"[^A-Za-z0-9_]", "_", name)
        expressions.append(
            f"SAFE_DIVIDE(COUNTIF({quoted} IS NULL), COUNT(*)) AS __qf_null_pct_{alias}"
        )
        expressions.append(
            f"APPROX_COUNT_DISTINCT({quoted}) AS __qf_distinct_{alias}"
        )
        if field_type in MIN_MAX_TYPES:
            expressions.append(f"MIN({quoted}) AS __qf_min_{alias}")
            expressions.append(f"MAX({quoted}) AS __qf_max_{alias}")
    return "SELECT\n  " + ",\n  ".join(expressions) + f"\nFROM `{table_ref}`"


@dataclass(frozen=True)
class ProfileResult:
    table: str
    query: str
    dry_run: ValidationResult
    executed: bool
    rows: Optional[list[dict[str, Any]]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "query": self.query,
            "dry_run": self.dry_run.to_dict(),
            "executed": self.executed,
            "rows": self.rows,
        }


def profile_table(
    table_ref: str,
    schema: list[dict[str, Any]],
    *,
    location: Optional[str] = None,
    execute: bool = False,
    maximum_bytes_billed: int = 1_073_741_824,
    account: Optional[str] = None,
    runner: Callable[[list[str]], tuple[int, str, str]] = subprocess_runner,
    gcloud_context: Optional[GcloudContext] = None,
) -> ProfileResult:
    if gcloud_context is not None and runner is subprocess_runner:
        def runner(command: list[str]) -> tuple[int, str, str]:
            completed = subprocess.run(
                command,
                env=gcloud_context.environment(),
                check=False,
                capture_output=True,
                text=True,
            )
            return completed.returncode, completed.stdout, completed.stderr

    query = build_profile_query(table_ref, schema)
    dry = dry_run_sql(
        query,
        location=location,
        project_id=table_ref.split(".", 1)[0],
        maximum_bytes_billed=maximum_bytes_billed,
        account=account,
        runner=runner,
        gcloud_context=gcloud_context,
    )
    if not execute or dry.dry_run_ok is not True:
        return ProfileResult(table_ref, query, dry, False, None)
    command = [
        "bq",
        "query",
        "--use_legacy_sql=false",
        "--format=json",
        f"--maximum_bytes_billed={maximum_bytes_billed}",
    ]
    if location:
        command.append(f"--location={location}")
    command.append(f"--project_id={table_ref.split('.', 1)[0]}")
    command.append(query)
    returncode, stdout, stderr = runner(command)
    if returncode != 0:
        failed = ValidationResult(
            references=dry.references,
            statement_class=dry.statement_class,
            read_only=dry.read_only,
            dry_run_ok=dry.dry_run_ok,
            bytes_processed=dry.bytes_processed,
            errors=[stderr.strip() or stdout.strip() or "Falló el perfil agregado"],
            warnings=dry.warnings,
        )
        return ProfileResult(table_ref, query, failed, True, None)
    try:
        rows = json.loads(stdout or "[]")
    except json.JSONDecodeError as error:
        raise ProfileError("bq no devolvió JSON válido para el perfil") from error
    if not isinstance(rows, list):
        rows = [rows] if isinstance(rows, dict) else []
    return ProfileResult(table_ref, query, dry, True, rows)
