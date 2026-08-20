from __future__ import annotations

import json
import hashlib
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .gcloud import GcloudContext


class ValidationError(RuntimeError):
    """A validation operation failed before a write could be attempted."""


REFERENCE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])(?:`)?([A-Za-z][A-Za-z0-9_-]{0,62}\.[A-Za-z_][A-Za-z0-9_]{0,1023}\.[A-Za-z0-9_$@*-]+)(?:`)?"
)
READ_ONLY_PREFIXES = ("SELECT", "WITH", "EXPLAIN", "SHOW", "DESCRIBE")
MUTATING_PREFIXES = (
    "CREATE",
    "INSERT",
    "UPDATE",
    "DELETE",
    "MERGE",
    "CALL",
    "ALTER",
    "DROP",
    "TRUNCATE",
    "EXPORT",
    "LOAD",
    "EXECUTE",
    "BEGIN",
    "DECLARE",
    "SET",
    "GRANT",
    "REVOKE",
    "TRANSACTION",
)


@dataclass(frozen=True)
class ValidationResult:
    references: list[str]
    statement_class: str
    read_only: bool
    dry_run_ok: Optional[bool] = None
    bytes_processed: Optional[int] = None
    maximum_bytes_billed: Optional[int] = None
    within_configured_limit: Optional[bool] = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fragments: list["FragmentValidation"] = field(default_factory=list)
    error_kind: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionResult:
    ok: bool
    rows: list[dict[str, Any]]
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FragmentValidation:
    index: int
    sha256: str
    references: list[str]
    statement_class: str
    read_only: bool
    dry_run_ok: Optional[bool] = None
    bytes_processed: Optional[int] = None
    bytes_billed: Optional[int] = None
    maximum_bytes_billed: Optional[int] = None
    within_configured_limit: Optional[bool] = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error_kind: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)
    return re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)


def classify_error(message: str) -> str:
    """Classify common BigQuery failures without exposing provider-specific codes."""
    lowered = message.lower()
    if (
        "vpc service controls" in lowered
        or "organization's policy" in lowered
        or "vpcservicecontrolsuniqueidentifier" in lowered
    ):
        return "vpc"
    if "permission_denied" in lowered or "permission denied" in lowered or "access denied" in lowered:
        return "permission"
    if (
        "syntax error" in lowered
        or "unrecognized name" in lowered
        or "not found: table" in lowered
        or "invalid query" in lowered
    ):
        return "sql"
    if (
        "timed out" in lowered
        or "connection" in lowered
        or "could not connect" in lowered
        or "servernotfounderror" in lowered
        or "network" in lowered
    ):
        return "transport"
    return "unknown"


def _statement_class(sql: str) -> tuple[str, bool]:
    parsed = _sqlglot_statement_class(sql)
    if parsed is not None:
        return parsed
    statements = [part.strip() for part in _strip_comments(sql).split(";") if part.strip()]
    if not statements:
        return "empty", False
    normalized = [statement.upper() for statement in statements]
    prefixes = [statement.split(None, 1)[0] for statement in normalized]
    if any(prefix in MUTATING_PREFIXES for prefix in prefixes):
        return "mutating", False
    # BigQuery permits a WITH clause before some DML statements. Treat any
    # DML keyword after WITH as mutating rather than allowing an optional
    # read-only execution based only on the first token.
    for statement, prefix in zip(normalized, prefixes):
        if prefix == "WITH" and re.search(
            r"\b(?:CREATE|INSERT|UPDATE|DELETE|MERGE|ALTER|DROP|TRUNCATE|EXPORT|LOAD|CALL)\b",
            statement,
        ):
            return "mutating", False
    if all(prefix in READ_ONLY_PREFIXES for prefix in prefixes):
        return "read_only", True
    return "unknown", False


def _sqlglot_statement_class(sql: str) -> tuple[str, bool] | None:
    """Use a BigQuery parser when available; return None for legacy fallback."""
    try:
        import sqlglot
    except ImportError:
        return None
    try:
        expressions = sqlglot.parse(sql, read="bigquery")
    except Exception:
        return ("unknown", False)
    if len(expressions) != 1:
        return ("unknown", False)
    expression = expressions[0]
    name = expression.__class__.__name__.upper()
    mutating = {
        "ALTER",
        "CALL",
        "COMMAND",
        "CREATE",
        "DELETE",
        "DROP",
        "INSERT",
        "MERGE",
        "UPDATE",
        "TRUNCATE",
        "COPY",
        "LOAD",
    }
    if name in mutating or any(node.__class__.__name__.upper() in mutating for node in expression.walk()):
        return ("mutating", False)
    if name in {"SELECT", "UNION", "EXPLAIN", "SHOW", "DESCRIBE", "WITH", "SUBQUERY"}:
        return ("read_only", True)
    return ("unknown", False)


def validate_sql_text(sql: str) -> ValidationResult:
    references = []
    for match in REFERENCE_PATTERN.finditer(sql):
        reference = match.group(1)
        if reference not in references:
            references.append(reference)
    statement_class, read_only = _statement_class(sql)
    errors: list[str] = []
    warnings: list[str] = []
    if statement_class == "empty":
        errors.append("La consulta está vacía")
    elif statement_class == "unknown":
        warnings.append("No se pudo clasificar la consulta como lectura pura")
    return ValidationResult(
        references=references,
        statement_class=statement_class,
        read_only=read_only,
        errors=errors,
        warnings=warnings,
    )


def validate_sql_fragments(fragments: Sequence[tuple[int, str]]) -> ValidationResult:
    """Validate SQL fragments extracted from a mixed-language notebook."""
    if not fragments:
        return ValidationResult(
            references=[],
            statement_class="empty",
            read_only=False,
            errors=["No se encontraron fragmentos SQL literales para validar"],
        )
    results = [(index, validate_sql_text(sql)) for index, sql in fragments]
    references: list[str] = []
    errors: list[str] = []
    warnings: list[str] = []
    for index, result in results:
        for reference in result.references:
            if reference not in references:
                references.append(reference)
        errors.extend(f"celda {index}: {error}" for error in result.errors)
        warnings.extend(f"celda {index}: {warning}" for warning in result.warnings)
    if any(result.statement_class == "mutating" for _index, result in results):
        statement_class, read_only = "mutating", False
    elif any(result.statement_class != "read_only" for _index, result in results):
        statement_class, read_only = "unknown", False
    else:
        statement_class, read_only = "read_only", True
    return ValidationResult(
        references=references,
        statement_class=statement_class,
        read_only=read_only,
        errors=errors,
        warnings=warnings,
    )


Runner = Callable[[list[str]], tuple[int, str, str]]
TokenProvider = Callable[[str], str]


def subprocess_runner(command: list[str]) -> tuple[int, str, str]:
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    return completed.returncode, completed.stdout, completed.stderr


def _gcloud_context_runner(context: GcloudContext) -> Runner:
    """Run bq (and related CLIs) with the same persistent gcloud profile."""
    def run(command: list[str]) -> tuple[int, str, str]:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=context.environment(),
        )
        return completed.returncode, completed.stdout, completed.stderr

    return run


def gcloud_access_token(account: str, *, context: Optional[GcloudContext] = None) -> str:
    runner = context or GcloudContext.default()
    completed = runner.run(["auth", "print-access-token"], account=account)
    if completed.returncode != 0 or not completed.stdout.strip():
        raise ValidationError(completed.stderr.strip() or "No se pudo obtener el token de la cuenta indicada")
    return completed.stdout.strip()


def dry_run_sql(
    sql: str,
    *,
    location: Optional[str] = None,
    project_id: Optional[str] = None,
    maximum_bytes_billed: int = 1_073_741_824,
    runner: Runner = subprocess_runner,
    account: Optional[str] = None,
    token_provider: TokenProvider = gcloud_access_token,
    gcloud_context: Optional[GcloudContext] = None,
) -> ValidationResult:
    static = validate_sql_text(sql)
    if static.errors:
        return static
    effective_runner = _gcloud_context_runner(gcloud_context) if gcloud_context and runner is subprocess_runner else runner
    command = [
        "bq",
        "query",
        "--use_legacy_sql=false",
        "--dry_run",
        "--format=json",
        f"--maximum_bytes_billed={maximum_bytes_billed}",
    ]
    if location:
        command.append(f"--location={location}")
    if project_id:
        command.append(f"--project_id={project_id}")
    if account:
        try:
            command.append("--use_google_auth=false")
            if token_provider is gcloud_access_token:
                token = token_provider(account, context=gcloud_context)
            else:
                token = token_provider(account)
            command.append(f"--oauth_access_token={token}")
        except ValidationError as error:
            return ValidationResult(
                references=static.references,
                statement_class=static.statement_class,
                read_only=static.read_only,
                dry_run_ok=False,
                errors=[str(error)],
                warnings=static.warnings,
                error_kind="authentication",
            )
    command.append(sql)
    returncode, stdout, stderr = effective_runner(command)
    if returncode != 0:
        message = stderr.strip() or stdout.strip() or "BigQuery dry-run falló"
        return ValidationResult(
            references=static.references,
            statement_class=static.statement_class,
            read_only=static.read_only,
            dry_run_ok=False,
            errors=[message],
            warnings=static.warnings,
            error_kind=classify_error(message),
        )
    bytes_processed: Optional[int] = None
    try:
        parsed = json.loads(stdout or "{}")
        candidates = (
            parsed.get("totalBytesProcessed"),
            parsed.get("statistics", {}).get("query", {}).get("totalBytesProcessed"),
        ) if isinstance(parsed, dict) else ()
        for candidate in candidates:
            if candidate is not None:
                bytes_processed = int(candidate)
                break
    except (ValueError, TypeError, json.JSONDecodeError):
        # bq versions can return a human-readable success message; validation still passed.
        pass
    within_limit = bytes_processed is None or bytes_processed <= maximum_bytes_billed
    warnings = list(static.warnings)
    if bytes_processed is not None and not within_limit:
        warnings.append(
            f"La estimación de {bytes_processed} bytes supera el límite configurado de "
            f"{maximum_bytes_billed} bytes"
        )
    return ValidationResult(
        references=static.references,
        statement_class=static.statement_class,
        read_only=static.read_only,
        dry_run_ok=True,
        bytes_processed=bytes_processed,
        maximum_bytes_billed=maximum_bytes_billed,
        within_configured_limit=within_limit,
        warnings=warnings,
    )


def dry_run_sql_fragments(
    fragments: Sequence[tuple[int, str]],
    *,
    location: Optional[str] = None,
    project_id: Optional[str] = None,
    maximum_bytes_billed: int = 1_073_741_824,
    runner: Runner = subprocess_runner,
    account: Optional[str] = None,
    token_provider: TokenProvider = gcloud_access_token,
    gcloud_context: Optional[GcloudContext] = None,
) -> ValidationResult:
    """Dry-run each SQL fragment and aggregate the safe review result."""
    static = validate_sql_fragments(fragments)
    if static.errors:
        return static
    effective_runner = _gcloud_context_runner(gcloud_context) if gcloud_context and runner is subprocess_runner else runner
    results = [
        (index, sql, dry_run_sql(
            sql,
            location=location,
            project_id=project_id,
            maximum_bytes_billed=maximum_bytes_billed,
            runner=effective_runner,
            account=account,
            token_provider=token_provider,
            gcloud_context=gcloud_context,
        ))
        for index, sql in fragments
    ]
    errors: list[str] = []
    warnings = list(static.warnings)
    bytes_processed = 0
    saw_bytes = False
    fragment_results: list[FragmentValidation] = []
    for index, sql, result in results:
        errors.extend(f"celda {index}: {error}" for error in result.errors)
        warnings.extend(f"celda {index}: {warning}" for warning in result.warnings)
        if result.bytes_processed is not None:
            bytes_processed += result.bytes_processed
            saw_bytes = True
        fragment_results.append(
            FragmentValidation(
                index=index,
                sha256=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                references=result.references,
                statement_class=result.statement_class,
                read_only=result.read_only,
                dry_run_ok=result.dry_run_ok,
                bytes_processed=result.bytes_processed,
                maximum_bytes_billed=result.maximum_bytes_billed,
                within_configured_limit=result.within_configured_limit,
                errors=result.errors,
                warnings=result.warnings,
                error_kind=result.error_kind,
            )
        )
    error_kinds = {fragment.error_kind for fragment in fragment_results if fragment.error_kind}
    error_kind = next(iter(error_kinds)) if len(error_kinds) == 1 else ("unknown" if error_kinds else None)
    return ValidationResult(
        references=static.references,
        statement_class=static.statement_class,
        read_only=static.read_only,
        dry_run_ok=not errors and all(result.dry_run_ok is True for _index, _sql, result in results),
        bytes_processed=bytes_processed if saw_bytes else None,
        maximum_bytes_billed=maximum_bytes_billed,
        within_configured_limit=all(
            fragment.within_configured_limit is not False
            for fragment in fragment_results
        ),
        errors=errors,
        warnings=warnings,
        fragments=fragment_results,
        error_kind=error_kind,
    )


def execute_read_only_sql(
    sql: str,
    *,
    location: Optional[str] = None,
    project_id: Optional[str] = None,
    maximum_bytes_billed: int = 1_073_741_824,
    runner: Runner = subprocess_runner,
    account: Optional[str] = None,
    token_provider: TokenProvider = gcloud_access_token,
    gcloud_context: Optional[GcloudContext] = None,
) -> ExecutionResult:
    static = validate_sql_text(sql)
    if not static.read_only or static.errors:
        return ExecutionResult(False, [], "La ejecución opcional requiere una consulta de lectura pura")
    effective_runner = _gcloud_context_runner(gcloud_context) if gcloud_context and runner is subprocess_runner else runner
    command = [
        "bq",
        "query",
        "--use_legacy_sql=false",
        "--format=json",
        "--max_rows=20",
        f"--maximum_bytes_billed={maximum_bytes_billed}",
    ]
    if location:
        command.append(f"--location={location}")
    if project_id:
        command.append(f"--project_id={project_id}")
    if account:
        try:
            command.append("--use_google_auth=false")
            if token_provider is gcloud_access_token:
                token = token_provider(account, context=gcloud_context)
            else:
                token = token_provider(account)
            command.append(f"--oauth_access_token={token}")
        except ValidationError as error:
            return ExecutionResult(False, [], str(error))
    command.append(sql)
    returncode, stdout, stderr = effective_runner(command)
    if returncode != 0:
        return ExecutionResult(False, [], stderr.strip() or stdout.strip() or "La consulta falló")
    try:
        rows = json.loads(stdout or "[]")
    except json.JSONDecodeError:
        return ExecutionResult(False, [], "bq no devolvió JSON válido")
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return ExecutionResult(False, [], "bq devolvió un formato de filas inesperado")
    return ExecutionResult(True, rows[:20])


def execute_read_only_sql_fragments(
    fragments: Sequence[tuple[int, str]],
    *,
    location: Optional[str] = None,
    project_id: Optional[str] = None,
    maximum_bytes_billed: int = 1_073_741_824,
    runner: Runner = subprocess_runner,
    account: Optional[str] = None,
    token_provider: TokenProvider = gcloud_access_token,
    gcloud_context: Optional[GcloudContext] = None,
) -> ExecutionResult:
    """Execute notebook SQL fragments independently with bounded output."""
    static = validate_sql_fragments(fragments)
    if not static.read_only or static.errors:
        return ExecutionResult(False, [], "La ejecución opcional requiere consultas de lectura puras")
    effective_runner = _gcloud_context_runner(gcloud_context) if gcloud_context and runner is subprocess_runner else runner
    rows: list[dict[str, Any]] = []
    for index, sql in fragments:
        result = execute_read_only_sql(
            sql,
            location=location,
            project_id=project_id,
            maximum_bytes_billed=maximum_bytes_billed,
            runner=effective_runner,
            account=account,
            token_provider=token_provider,
            gcloud_context=gcloud_context,
        )
        if not result.ok:
            return ExecutionResult(False, [], f"celda {index}: {result.error or 'la consulta falló'}")
        rows.extend(result.rows[:20])
    return ExecutionResult(True, rows[:20])


def load_sql_for_task(task: Path) -> str:
    manifest_path = task / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        filename = manifest["filename"]
        path = task / filename
        return path.read_text(encoding="utf-8")
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValidationError(f"No se pudo cargar el SQL de {task}") from error
