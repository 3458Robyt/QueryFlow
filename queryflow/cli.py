from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import random
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from .audit import AuditError, GcsAuditStore, LocalAuditStore
from . import __version__ as PACKAGE_VERSION
from .catalog import (
    ResourceRef,
    load_catalog,
    refresh_catalog,
    save_catalog,
)
from .config import QueryflowConfig, load_config
from .config_store import CURRENT_SCHEMA_VERSION, ConfigStore, ConfigStoreError, default_config_path
from .errors import (
    Diagnostic,
    classify_message,
    clear_latest_diagnostic,
    exception_digest,
    make_diagnostic,
    provider_identifiers,
    redact_message,
    read_diagnostic,
    write_diagnostic,
)
from .dataform import DataformClient, DataformRateLimitError, ExportedAsset, DataformError
from .gcloud import GcloudContext
from .notebooks import (
    analyze_sql_fragments,
    empty_notebook,
    extract_code_cells,
    initialize_new_notebook_workspace,
    write_cell_workspace,
)
from .profile import ProfileError, profile_table
from .review import write_review_html
from .review_server import serve_review
from .state import task_state, validation_is_publishable
from .task import TaskError, approval_digest, baseline_file, mark_validation, preview_notebook_task, sync_notebook_task
from .transfer import TransferClient, TransferError
from .validation import (
    dry_run_sql,
    dry_run_sql_fragments,
    execute_read_only_sql,
    execute_read_only_sql_fragments,
    validate_sql_fragments,
    validate_sql_text,
)
from .policy import Policy, evaluate_policy
from .sample import execution_digest
from .workbench import WorkbenchSettings, execute_workbench_sample, validate_workbench_fragments
from .workspace import create_workspace, read_manifest, update_manifest
from .schedule import ScheduleError, ScheduleSpec
from .migration import (
    MigrationDictionaryError,
    load_dictionary,
    render_markdown,
    rewrite_task,
    validate_dictionary,
)
from .migration_pilot import (
    CampaignError,
    InventoryCheckpoint,
    PilotManifest,
    PilotQuotas,
    build_manifest,
    campaign_publish_digest,
    cleanup_digest,
    load_manifest,
    load_inventory_checkpoint,
    make_cleanup_plan,
    make_inventory_incident_report,
    migration_publish_allowed,
    make_incident_report,
    render_campaign_review,
    save_manifest,
    save_inventory_checkpoint,
    select_classified,
    _selection,
    validate_pilot_manifest,
)
from .migration_batch import (
    BATCH_KINDS,
    BATCH_PENDING_STATUSES,
    BATCH_QUOTA_REQUESTS_PER_MINUTE,
    BATCH_REQUESTS_PER_MINUTE,
    BatchError,
    BatchSelection,
    build_batch_digest,
    build_batch_manifest,
    build_sealed_digest,
    classify_asset,
    load_batch_manifest,
    mask_sensitive_content,
    normalize_display_name,
    rewrite_asset,
    partition_batch_records,
    resolve_batch_resources,
    render_batch_review,
    save_batch_manifest,
    serve_batch_review,
    validate_batch_manifest,
    write_batch_reports,
)
from .finops import load_assessment, run_assessment, serve_assessment
from .routines import (
    BigQueryRoutineClient,
    RoutineError,
    RoutineSnapshot,
    WorkbenchRoutineTransport,
    ROUTINE_BATCH_SIZE,
    ROUTINE_DESTINATION_DATASET,
    ROUTINE_REQUESTS_PER_MINUTE,
    build_routine_digest,
    build_routine_manifest,
    build_routine_sealed_digest,
    inventory_routine_snapshots,
    load_routine_manifest,
    publish_routines,
    render_routine_review,
    save_routine_manifest,
    serve_routine_review,
    validate_routine_manifest,
    write_routine_audit,
    write_routine_reports,
)


class CliError(RuntimeError):
    """A safe, user-facing QueryFlow failure."""


DEFAULT_REPOSITORY = "3458Robyt/QueryFlow"
DEFAULT_MARKETPLACE = "queryflow"


def _json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _config(args: argparse.Namespace) -> QueryflowConfig:
    return load_config(Path(args.config) if getattr(args, "config", None) else None)


def _gcloud_context(config: QueryflowConfig, account: Optional[str] = None) -> GcloudContext:
    return GcloudContext(config.gcloud_config_dir, account=account or config.account)


def _configure_dataform_client(client: Any, config: QueryflowConfig, account: Optional[str]) -> Any:
    setter = getattr(client, "set_gcloud_context", None)
    if callable(setter):
        setter(_gcloud_context(config, account))
    return client


def _effective_max_bytes(args: argparse.Namespace, config: QueryflowConfig) -> int:
    requested = getattr(args, "max_bytes", None)
    if requested is not None and requested > config.policy_max_bytes:
        raise CliError(
            f"--max-bytes supera el máximo de política ({config.policy_max_bytes} bytes)"
        )
    return int(requested or config.profile_max_bytes)


def _modern_policy(config: QueryflowConfig) -> Policy:
    return Policy(
        max_bytes=config.policy_max_bytes,
        default_max_bytes=config.profile_max_bytes,
        sample_default_rows=config.sample_default_rows,
        sample_max_rows=config.sample_max_rows,
        allow_update_existing=config.allow_update_existing and config.mode in {"team", "full-access"},
        allow_force_publish=config.allow_force_publish and config.mode == "full-access",
        allow_static_exception=config.allow_static_exception and config.mode == "team",
        allow_migration_pilot=config.mode == "migration-pilot",
        allow_migration_batch=config.mode in {"migration-batch", "migration-pilot"},
        allow_routine_migration=config.allow_routine_migration and config.mode in {"migration-batch", "migration-pilot"},
        allow_migration_cleanup=config.mode == "migration-pilot",
        allowed_resource_kinds=("notebook", "shared_query", "routine"),
        allowed_source_projects=config.source_projects,
        allowed_destination_projects=config.destination_projects,
        allowed_locations=config.allowed_locations,
    )


def _parse_config_value(raw: str) -> Any:
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    if "," in raw:
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw


def _cmd_version(args: argparse.Namespace) -> int:
    payload = {
        "queryflow": PACKAGE_VERSION,
        "config_schema": CURRENT_SCHEMA_VERSION,
        "task_schema": 2,
        "diagnostic_schema": 1,
        "plugin": PACKAGE_VERSION,
    }
    _print_result(payload, args.json)
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser() if args.path else default_config_path()
    profile = args.profile or "pilot"
    if profile not in {"pilot", "team", "full-access", "migration-pilot", "migration-batch"}:
        raise CliError("--profile debe ser pilot, team, full-access, migration-pilot o migration-batch")
    aliases: dict[str, str] = {}
    for raw_alias in args.project_alias or []:
        if "=" not in raw_alias:
            raise CliError("--project-alias debe tener formato alias=PROJECT_ID")
        alias, project = raw_alias.split("=", 1)
        if not alias.strip() or not project.strip():
            raise CliError("--project-alias debe tener formato alias=PROJECT_ID")
        aliases[alias.strip()] = project.strip()
    values = {
        "mode": profile,
        "account": args.account or "",
        "gcloud_config_dir": args.gcloud_config_dir or str(Path.home() / ".config" / "gcloud"),
        "source_projects": [item for item in (args.source_projects or "").split(",") if item.strip()],
        "destination_projects": [item for item in (args.destination_projects or "").split(",") if item.strip()],
        "workbench_instance_project": args.workbench_instance_project or args.workbench_project or "",
        "workbench_instance_location": args.workbench_instance_location or args.workbench_location or "",
        "workbench_instance_name": args.workbench_instance_name or args.workbench_instance or "",
        "workbench_job_project": args.workbench_job_project or "",
        "validation_backend": args.validation_backend or "workbench",
        "max_bytes": args.max_bytes or 5 * 1024 * 1024 * 1024,
        "sample_default_rows": 3,
        "sample_max_rows": 5,
        "allow_update_existing": profile in {"team", "full-access"},
        "allow_force_publish": profile == "full-access",
        "allow_migration_pilot": profile == "migration-pilot",
        "allow_migration_batch": profile == "migration-batch",
        "allow_migration_cleanup": False,
        "allow_routine_migration": bool(args.allow_routine_migration),
        "routine_backend": args.routine_backend or "auto",
        "routine_destination_dataset": args.routine_destination_dataset or "functions",
        "routine_batch_size": args.routine_batch_size or ROUTINE_BATCH_SIZE,
        "routine_requests_per_minute": args.routine_requests_per_minute or ROUTINE_REQUESTS_PER_MINUTE,
        "finops_projects": [item for item in (args.finops_projects or "").split(",") if item.strip()],
        "billing_export_table": args.billing_export_table or "",
        "business_context_path": args.business_context_path or "",
        "finops_window_days": args.finops_window_days or 30,
    }
    try:
        store = ConfigStore(path)
        document = store.initialize(profile=profile, values=values)
        for alias, project in aliases.items():
            document = store.set_alias(alias, project)
        if args.source_project or args.destination_project:
            source = args.source_project or args.destination_project
            destination = args.destination_project or args.source_project
            if not source or not destination:
                raise CliError("--source-project y --destination-project deben ir juntos")
            document = store.set_context(source_project=source, destination_project=destination)
    except ConfigStoreError as error:
        raise CliError(str(error)) from error
    payload = {
        "path": str(path),
        "active_profile": document.active_profile,
        "profiles": document.profiles,
        "project_aliases": document.project_aliases,
        "context": document.context,
    }
    _print_result(payload, args.json)
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    store = ConfigStore(Path(args.path).expanduser() if args.path else default_config_path())
    try:
        if args.config_command == "path":
            _print_result(str(store.path), args.json)
            return 0
        if args.config_command == "set":
            document = store.set_value(args.key, _parse_config_value(args.value))
            _print_result(document.to_dict(), args.json)
            return 0
        document = store.load()
    except ConfigStoreError as error:
        raise CliError(str(error)) from error
    if args.config_command == "list":
        _print_result(document.to_dict(), args.json)
        return 0
    if args.config_command == "validate":
        _print_result({"ok": True, "path": str(store.path), "schema_version": document.schema_version}, args.json)
        return 0
    if args.config_command == "get":
        current: Any = document.to_dict()
        for part in args.key.split("."):
            if not isinstance(current, dict) or part not in current:
                raise CliError(f"No existe la clave de configuración: {args.key}")
            current = current[part]
        _print_result(current, args.json)
        return 0
    raise CliError(f"Comando config no soportado: {args.config_command}")


def _resolve_project_reference(document: Any, value: str) -> str:
    aliases = getattr(document, "project_aliases", {}) or {}
    if value in aliases:
        return aliases[value]
    if value in aliases.values() or re.fullmatch(r"[a-z][a-z0-9-]{4,29}", value):
        return value
    raise CliError(f"No existe el alias de proyecto: {value}")


def _resolve_config_project(config: QueryflowConfig, value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    aliases = config.project_aliases
    if value in aliases:
        return aliases[value]
    if value in aliases.values() or re.fullmatch(r"[a-z][a-z0-9-]{4,29}", value):
        return value
    raise CliError(f"No existe el alias de proyecto: {value}")


def _active_destination(config: QueryflowConfig) -> Optional[str]:
    return _resolve_config_project(config, config.context_destination_project)


def _active_source(config: QueryflowConfig) -> Optional[str]:
    return _resolve_config_project(config, config.context_source_project)


def _cmd_context(args: argparse.Namespace) -> int:
    store = ConfigStore(Path(args.config).expanduser() if args.config else default_config_path())
    try:
        document = store.load()
        if args.context_command == "show":
            _print_result(
                {
                    "source_project": document.context.get("source_project"),
                    "destination_project": document.context.get("destination_project"),
                    "project_aliases": document.project_aliases,
                },
                args.json,
            )
            return 0
        if args.context_command == "use":
            project = _resolve_project_reference(document, args.project)
            updated = store.set_context(source_project=project, destination_project=project)
        elif args.context_command == "set":
            updated = store.set_context(
                source_project=_resolve_project_reference(document, args.source),
                destination_project=_resolve_project_reference(document, args.destination),
            )
        elif args.context_command == "alias":
            if args.alias_command == "set":
                updated = store.set_alias(args.alias, args.project)
            elif args.alias_command == "list":
                _print_result(document.project_aliases, args.json)
                return 0
            else:
                raise CliError(f"Comando context alias no soportado: {args.alias_command}")
        else:
            raise CliError(f"Comando context no soportado: {args.context_command}")
    except ConfigStoreError as error:
        raise CliError(str(error)) from error
    _print_result({"context": updated.context, "project_aliases": updated.project_aliases}, args.json)
    return 0


def _cmd_permissions(args: argparse.Namespace) -> int:
    store = ConfigStore(Path(args.config).expanduser() if args.config else default_config_path())
    try:
        if args.permissions_command == "show":
            document = store.load()
            profile_summary = {
                name: {
                    "mode": values.get("mode", name),
                    "allow_update_existing": bool(values.get("allow_update_existing", False)),
                    "allow_force_publish": bool(values.get("allow_force_publish", False)),
                    "allow_migration_batch": bool(values.get("allow_migration_batch", False)),
                    "allow_routine_migration": bool(values.get("allow_routine_migration", False)),
                    "validation_backend": values.get("validation_backend", "workbench"),
                }
                for name, values in document.profiles.items()
            }
            _print_result(
                {
                    "active_profile": document.active_profile,
                    "profiles": sorted(document.profiles),
                    "profile_details": profile_summary,
                },
                args.json,
            )
            return 0
        if args.permissions_command != "use":
            raise CliError(f"Comando permissions no soportado: {args.permissions_command}")
        updated = store.activate_profile(args.profile)
    except ConfigStoreError as error:
        raise CliError(str(error)) from error
    _print_result({"active_profile": updated.active_profile, "profiles": sorted(updated.profiles)}, args.json)
    return 0


def _cmd_policy(args: argparse.Namespace) -> int:
    config = _config(args)
    policy = _modern_policy(config)
    if args.policy_command == "show":
        _print_result(policy.to_dict(), args.json)
        return 0
    decision = evaluate_policy(
        policy,
        operation=args.operation,
        resource_kind=args.resource_kind,
        mode=args.mode,
        source_project=args.source_project,
        destination_project=args.destination_project,
        location=args.location,
    )
    _print_result(decision.to_dict(), args.json)
    return 0 if decision.allowed else 2


def _run_command(command: list[str]) -> None:
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise CliError(completed.stderr.strip() or completed.stdout.strip() or f"Falló: {' '.join(command)}")


def _install_commands(ref: str) -> list[list[str]]:
    package = f"git+https://github.com/{DEFAULT_REPOSITORY}.git@{ref}"
    return [
        ["uv", "tool", "install", "--force", package],
        ["codex", "plugin", "marketplace", "add", DEFAULT_REPOSITORY, "--ref", ref],
        ["codex", "plugin", "add", f"queryflow@{DEFAULT_MARKETPLACE}"],
    ]


def _cmd_install(args: argparse.Namespace) -> int:
    ref = args.ref or f"v{PACKAGE_VERSION}"
    commands = _install_commands(ref)
    payload = {"ref": ref, "commands": commands, "next": "queryflow init"}
    if args.dry_run:
        _print_result(payload, args.json)
        return 0
    for command in commands:
        _run_command(command)
    _print_result({**payload, "installed": True}, args.json)
    return 0


def _cmd_self_update(args: argparse.Namespace) -> int:
    ref = args.ref or f"v{PACKAGE_VERSION}"
    commands = _install_commands(ref)
    if args.dry_run:
        _print_result({"ref": ref, "commands": commands}, args.json)
        return 0
    for command in commands:
        _run_command(command)
    _print_result({"ref": ref, "updated": True}, args.json)
    return 0


def _audit_store(value: str, *, gcloud_context: Optional[GcloudContext] = None) -> Any:
    if value.startswith("gs://"):
        return GcsAuditStore(value, gcloud_context=gcloud_context)
    return LocalAuditStore(Path(value).expanduser())


def _resource_from_args(args: argparse.Namespace, catalog: Optional[Any] = None) -> ResourceRef:
    if getattr(args, "resource", None):
        if catalog is None:
            raise CliError("Se requiere catálogo para --resource")
        matches = [item for item in catalog.resources if item.name == args.resource]
        if len(matches) != 1:
            raise CliError(f"El recurso no identifica exactamente un activo: {args.resource}")
        return matches[0]
    if not all(getattr(args, key, None) for key in ("kind", "name", "project", "location")):
        raise CliError("Para una tarea local se requieren --kind, --name, --project y --location")
    return ResourceRef(
        kind=args.kind,
        name=args.name,
        project=args.project,
        location=args.location,
        display_name=getattr(args, "display_name", None) or args.name,
        fingerprint=getattr(args, "fingerprint", None) or "local-input",
    )


def _safe_task_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    if not cleaned:
        raise CliError("No se pudo generar un task_id")
    return cleaned[:80]


def _print_result(value: Any, as_json: bool) -> None:
    if as_json:
        _json_print(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            print(f"{key}: {item}")
    else:
        print(value)


def _task_context(task: Path, manifest: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Return only safe identifiers for a diagnostic context."""
    value = manifest or {}
    resource = value.get("resource") or {}
    return {
        "task": str(task),
        "task_id": value.get("task_id"),
        "project": resource.get("project"),
        "location": resource.get("location"),
        "backend": value.get("validation_backend"),
        "account": value.get("account"),
    }


def _load_validation(task: Path) -> dict[str, Any]:
    path = task / "validation.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CliError(f"La validación de la tarea no es JSON válida: {error}") from error
    if not isinstance(value, dict):
        raise CliError("validation.json debe contener un objeto")
    return value


def _current_task_content_sha(task: Path, manifest: dict[str, Any]) -> str:
    filename = str(manifest.get("filename") or "")
    if not filename:
        raise CliError("La tarea no tiene filename")
    if manifest.get("resource", {}).get("kind") == "notebook":
        try:
            content = preview_notebook_task(task)
        except TaskError as error:
            raise CliError(str(error)) from error
    else:
        try:
            content = (task / filename).read_bytes()
        except OSError as error:
            raise CliError(f"No se pudo leer el contenido de la tarea: {error}") from error
    return hashlib.sha256(content).hexdigest()


def _validation_digest(validation: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(validation, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _diagnostic_for_task(task: Path, manifest: dict[str, Any], validation: dict[str, Any]) -> Diagnostic | None:
    existing = read_diagnostic(task)
    if existing is not None:
        return existing
    errors: list[str] = []
    for section in (validation, validation.get("static") or {}, validation.get("dry_run") or {}):
        errors.extend(str(item) for item in section.get("errors") or [])
    if not errors and validation.get("error_kind"):
        errors.append(str(validation.get("error_kind")))
    if not errors:
        return None
    return make_diagnostic(
        errors[0],
        stage="validate",
        category=str(validation.get("error_kind") or "") or None,
        context=_task_context(task, manifest),
    )


def _redact_validation_messages(value: Any) -> Any:
    if isinstance(value, dict):
        result = {str(key): _redact_validation_messages(child) for key, child in value.items()}
        for key in ("error", "message"):
            if key in result and isinstance(result[key], str):
                result[key] = redact_message(result[key])
        if "errors" in result and isinstance(result["errors"], list):
            result["errors"] = [redact_message(item) if isinstance(item, str) else item for item in result["errors"]]
        return result
    if isinstance(value, list):
        return [_redact_validation_messages(child) for child in value]
    return value


def _gcloud_services_probe(
    project: str,
    required_apis: list[str],
    acceptable_any: list[str] | None = None,
    *,
    gcloud_context: Optional[GcloudContext] = None,
) -> dict[str, Any]:
    """Check enabled APIs without changing the project."""
    if not shutil.which("gcloud"):
        return {
            "attempted": False,
            "ok": False,
            "project": project,
            "required": required_apis,
            "enabled": [],
            "missing": required_apis,
            "acceptable_any": acceptable_any or [],
            "missing_alternatives": acceptable_any or [],
            "error_category": "configuration",
            "provider": {"identifiers": {}},
            "error": "gcloud no está instalado",
        }
    command = [
        "services",
        "list",
        f"--project={project}",
        "--enabled",
        "--format=value(config.name)",
    ]
    completed = (
        gcloud_context.run(command)
        if gcloud_context
        else subprocess.run(["gcloud", *command], check=False, capture_output=True, text=True)
    )
    enabled = sorted({line.strip() for line in completed.stdout.splitlines() if line.strip()})
    alternatives = acceptable_any or []
    missing = sorted(set(required_apis) - set(enabled))
    missing_alternatives = bool(alternatives) and not any(api in enabled for api in alternatives)
    raw_error = completed.stderr if completed.returncode != 0 else ""
    error = redact_message(raw_error) if raw_error else ""
    return {
        "attempted": True,
        "ok": completed.returncode == 0 and not missing and not missing_alternatives,
        "project": project,
        "required": required_apis,
        "enabled": enabled,
        "missing": missing,
        "acceptable_any": alternatives,
        "missing_alternatives": alternatives if missing_alternatives else [],
        "error_category": classify_message(error) if error else None,
        "provider": {"identifiers": provider_identifiers(raw_error)},
        "error": error,
    }


def _gcloud_workbench_probe(
    config: QueryflowConfig,
    *,
    gcloud_context: Optional[GcloudContext] = None,
) -> dict[str, Any]:
    """Describe the configured Workbench instance using read-only commands."""
    if not shutil.which("gcloud"):
        return {
            "attempted": False,
            "ok": False,
            "error_category": "configuration",
            "provider": {"identifiers": {}},
            "error": "gcloud no está instalado",
        }
    if not all((config.workbench_project, config.workbench_location, config.workbench_instance)):
        return {
            "attempted": False,
            "ok": False,
            "error_category": "configuration",
            "provider": {"identifiers": {}},
            "error": "Falta proyecto, ubicación o instancia Workbench",
        }
    common = [
        "--project=" + str(config.workbench_project),
        "--location=" + str(config.workbench_location),
        "--format=json",
    ]
    attempts = [
        ["workbench", "instances", "describe", str(config.workbench_instance), *common],
    ]
    failures: list[str] = []
    identifiers: dict[str, str] = {}
    for command in attempts:
        completed = (
            gcloud_context.run(command)
            if gcloud_context
            else subprocess.run(["gcloud", *command], check=False, capture_output=True, text=True)
        )
        if completed.returncode == 0:
            return {
                "attempted": True,
                "ok": True,
                "command": " ".join(command[:4]),
                "project": config.workbench_project,
                "location": config.workbench_location,
                "instance": config.workbench_instance,
            }
        if completed.stderr.strip():
            failures.append(redact_message(completed.stderr))
            identifiers.update(provider_identifiers(completed.stderr))
        lowered = completed.stderr.casefold()
        if any(token in lowered for token in ("invalid choice", "unknown command", "command not found")):
            legacy = ["notebooks", "instances", "describe", str(config.workbench_instance), *common]
            completed = (
                gcloud_context.run(legacy)
                if gcloud_context
                else subprocess.run(["gcloud", *legacy], check=False, capture_output=True, text=True)
            )
            if completed.returncode == 0:
                return {
                    "attempted": True,
                    "ok": True,
                    "command": "gcloud notebooks instances describe",
                    "project": config.workbench_project,
                    "location": config.workbench_location,
                    "instance": config.workbench_instance,
                }
            if completed.stderr.strip():
                failures.append(redact_message(completed.stderr))
                identifiers.update(provider_identifiers(completed.stderr))
            break
    error = failures[-1] if failures else "No se pudo describir la instancia Workbench"
    return {
        "attempted": True,
        "ok": False,
        "project": config.workbench_project,
        "location": config.workbench_location,
        "instance": config.workbench_instance,
        "error_category": classify_message(error),
        "provider": {"identifiers": identifiers},
        "error": error,
    }


def _cmd_doctor(args: argparse.Namespace) -> int:
    config = _config(args)
    gcloud_context = _gcloud_context(config)
    auth_accounts: list[str] = []
    auth_error = ""
    if shutil.which("gcloud"):
        completed = gcloud_context.run(["auth", "list", "--format=value(account)"])
        auth_accounts = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        auth_error = redact_message(completed.stderr) if completed.returncode != 0 else ""
    workbench_config = {
        "project": bool(config.workbench_project),
        "location": bool(config.workbench_location),
        "instance": bool(config.workbench_instance),
        "job_project": bool(config.workbench_job_project),
        "instance_project": config.workbench_instance_project,
        "instance_location": config.workbench_instance_location,
        "instance_name": config.workbench_instance_name,
        "configured_job_project": config.workbench_job_project,
    }
    checks: dict[str, Any] = {
        "python": sys.version.split()[0],
        "git": shutil.which("git") is not None,
        "gcloud": shutil.which("gcloud") is not None,
        "bq": shutil.which("bq") is not None,
        "cloudshell": shutil.which("cloudshell") is not None,
        "uv": shutil.which("uv") is not None,
        "codex": shutil.which("codex") is not None,
        "websockets": importlib.util.find_spec("websockets") is not None,
        "workspace_root": str(config.workspace_root),
        "catalog_path": str(config.catalog_path),
        "audit_root_configured": config.audit_root is not None,
        "mode": config.mode,
        "validation_backend": config.validation_backend,
        "workbench_instance": config.workbench_instance,
        "profile": config.profile_name,
        "policy_max_bytes": config.policy_max_bytes,
        "gcloud_auth": {
            "ok": bool(auth_accounts) and (not config.account or config.account in auth_accounts),
            "accounts": len(auth_accounts),
            "configured_account": config.account,
            "configured_account_present": not config.account or config.account in auth_accounts,
            "error": auth_error,
        },
        "gcloud_config": {
            "configured": str(config.gcloud_config_dir),
            "account": config.account,
            "inherited": os.environ.get("CLOUDSDK_CONFIG"),
            "temporary_inherited": bool(os.environ.get("CLOUDSDK_CONFIG", "").startswith("/tmp/")),
        },
        "workbench_config": {**workbench_config, "ok": all(workbench_config.values())},
        "preferences": {
            "review_theme": config.review_theme,
            "review_mode": config.review_mode,
            "review_only_changes": config.review_only_changes,
            "review_context_lines": config.review_context_lines,
        },
        "remote_probe": {
            "attempted": False,
            "hint": "Ejecuta doctor --probe-remote para consultar APIs y Workbench sin mutar recursos.",
        },
    }
    if getattr(args, "probe_remote", False):
        required_apis = [
            "bigquery.googleapis.com",
            "cloudasset.googleapis.com",
            "dataform.googleapis.com",
        ]
        acceptable_any = (
            ["notebooks.googleapis.com", "aiplatform.googleapis.com"]
            if config.validation_backend == "workbench"
            else []
        )
        api_project = config.workbench_project or (config.destination_projects[0] if config.destination_projects else "")
        checks["apis"] = _gcloud_services_probe(api_project, required_apis, acceptable_any, gcloud_context=gcloud_context) if api_project else {
            "attempted": False,
            "ok": False,
            "required": required_apis,
            "enabled": [],
            "missing": required_apis,
            "acceptable_any": acceptable_any,
            "missing_alternatives": acceptable_any,
            "error_category": "configuration",
            "provider": {"identifiers": {}},
            "error": "No hay proyecto configurado para consultar APIs",
        }
        checks["workbench_connectivity"] = (
            _gcloud_workbench_probe(config, gcloud_context=gcloud_context)
            if config.validation_backend == "workbench"
            else {"attempted": False, "ok": True, "skipped": True}
        )
        checks["remote_probe"] = {"attempted": True, "ok": bool(checks["apis"]["ok"]) and bool(checks["workbench_connectivity"]["ok"])}
    _print_result(checks, args.json)
    required = checks["git"] and checks["gcloud"] and checks["bq"] and bool(checks["gcloud_auth"]["ok"])
    if config.validation_backend == "workbench":
        required = required and checks["websockets"] and bool(checks["workbench_config"]["ok"])
    if getattr(args, "probe_remote", False):
        required = required and bool(checks["remote_probe"]["ok"])
    return 0 if required else 2


def _cmd_status(args: argparse.Namespace) -> int:
    task = Path(args.task).resolve()
    manifest = read_manifest(task)
    validation = _load_validation(task)
    current_sha = _current_task_content_sha(task, manifest)
    status = task_state(manifest, validation, current_sha256=current_sha)
    diagnostic = _diagnostic_for_task(task, manifest, validation)
    resource = manifest.get("resource") or {}
    result: dict[str, Any] = {
        "schema_version": 1,
        "ok": status not in {"failed", "changes_required", "blocked_vpc", "blocked_permission", "conflict"},
        "task": str(task),
        "task_id": manifest.get("task_id"),
        "status": status,
        "mode": manifest.get("mode"),
        "resource": {
            "kind": resource.get("kind"),
            "name": resource.get("name"),
            "display_name": resource.get("display_name"),
            "project": resource.get("project"),
            "location": resource.get("location"),
        },
        "content_sha256": current_sha,
        "validated_sha256": validation.get("content_sha256") or manifest.get("proposed_sha256"),
        "approval_digest": manifest.get("approval_digest"),
        "published": bool(manifest.get("published")),
        "validation": {
            "status": validation.get("status"),
            "method": validation.get("method"),
            "backend": validation.get("backend"),
            "ok": validation.get("ok"),
            "publishable": validation.get("publishable"),
        },
    }
    if diagnostic:
        result["diagnostic"] = diagnostic.to_dict()
    _print_result(result, True)
    return 0 if result["ok"] else 2


def _cmd_diagnose(args: argparse.Namespace) -> int:
    task = Path(args.task).resolve()
    manifest = read_manifest(task)
    validation = _load_validation(task)
    diagnostic = _diagnostic_for_task(task, manifest, validation)
    if diagnostic and read_diagnostic(task) is None:
        write_diagnostic(task, diagnostic)
    if diagnostic is None:
        payload: Any = {
            "schema_version": 1,
            "ok": True,
            "task": str(task),
            "diagnostic": None,
            "message": "No hay un diagnóstico pendiente para esta tarea.",
        }
        rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n" if args.format == "json" else f"# Diagnóstico QueryFlow\n\nNo hay un diagnóstico pendiente para `{task}`.\n"
    elif args.format == "markdown":
        payload = diagnostic.to_markdown()
        rendered = payload
    else:
        payload = {"ok": False, "task": str(task), "diagnostic": diagnostic.to_dict()}
        rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        if args.json:
            _print_result({"ok": diagnostic is None, "task": str(task), "output": str(output), "error_id": diagnostic.error_id if diagnostic else None}, True)
    else:
        print(rendered, end="")
    return 0 if diagnostic is None else 2


def _cmd_exception_prepare(args: argparse.Namespace) -> int:
    config = _config(args)
    task = Path(args.task).resolve()
    manifest = read_manifest(task)
    validation = _load_validation(task)
    if config.mode != "team" or not config.allow_static_exception:
        raise CliError("La excepción estática requiere perfil team y allow_static_exception: true")
    if manifest.get("published"):
        raise CliError("No se puede preparar una excepción para una tarea ya publicada")
    if not args.reason.strip() or not args.reference.strip():
        raise CliError("La excepción requiere --reason y --reference no vacíos")
    static = validation.get("static") or {}
    if not validation or not validation.get("method") or static.get("errors"):
        raise CliError("La tarea debe tener una validación estática correcta antes de preparar la excepción")
    if not validation.get("ok") and not validation.get("error_kind") and not (validation.get("dry_run") or {}).get("errors"):
        raise CliError("No se pudo identificar el bloqueo remoto que justifica la excepción")
    blocker_raw = str(validation.get("error_kind") or (validation.get("dry_run") or {}).get("error_kind") or "unexpected")
    blocker = {"permission": "iam", "transport": "network", "vpc": "vpc"}.get(blocker_raw, blocker_raw)
    if blocker not in {"authentication", "vpc", "network"}:
        raise CliError(f"El bloqueo {blocker} no admite excepción estática")
    current_sha = _current_task_content_sha(task, manifest)
    if manifest.get("proposed_sha256") and current_sha != manifest.get("proposed_sha256"):
        raise CliError("El contenido cambió; genere una nueva validación antes de preparar la excepción")
    payload = {
        "schema_version": 1,
        "task_id": manifest["task_id"],
        "resource": {
            "kind": (manifest.get("resource") or {}).get("kind"),
            "name": (manifest.get("resource") or {}).get("name"),
            "project": (manifest.get("resource") or {}).get("project"),
            "location": (manifest.get("resource") or {}).get("location"),
        },
        "mode": manifest.get("mode"),
        "content_sha256": current_sha,
        "baseline_sha256": manifest.get("baseline_sha256"),
        "validation_sha256": _validation_digest(validation),
        "blocker_category": blocker,
        "reason": redact_message(args.reason.strip())[:1000],
        "reference": redact_message(args.reference.strip())[:500],
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    digest = exception_digest(payload)
    exception = {**payload, "exception_digest": digest}
    path = task / "exception.json"
    path.write_text(json.dumps(exception, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest["exception_digest"] = digest
    manifest["workflow_state"] = "exception_prepared"
    (task / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _print_result({"ok": True, "task": str(task), "exception": str(path), "exception_digest": digest, "blocker_category": blocker}, args.json)
    return 0


def _cmd_catalog_refresh(args: argparse.Namespace) -> int:
    config = _config(args)
    projects = args.projects or None
    catalog = refresh_catalog(
        projects=projects,
        account=args.account or config.account,
        gcloud_context=_gcloud_context(config, args.account or config.account),
    )
    catalog_path = Path(args.catalog_path) if args.catalog_path else config.catalog_path
    result = {"catalog": str(catalog_path), "resources": len(catalog.resources), "warnings": catalog.warnings}
    _print_result(result, args.json)
    if catalog.warnings and not catalog.resources:
        return 2
    save_catalog(catalog, catalog_path)
    return 0


def _cmd_catalog_search(args: argparse.Namespace) -> int:
    config = _config(args)
    catalog = load_catalog(Path(args.catalog) if args.catalog else config.catalog_path)
    result = [resource.to_dict() for resource in catalog.search(args.text, kind=args.kind)]
    _print_result(result, True)
    return 0


def _cmd_catalog_show(args: argparse.Namespace) -> int:
    config = _config(args)
    catalog = load_catalog(Path(args.catalog) if args.catalog else config.catalog_path)
    matches = [resource for resource in catalog.resources if resource.name == args.resource]
    if len(matches) != 1:
        raise CliError(f"El recurso no identifica exactamente un activo: {args.resource}")
    _print_result(matches[0].to_dict(), True)
    return 0


def _cmd_profile(args: argparse.Namespace) -> int:
    config = _config(args)
    if args.execute and not args.confirm_profile:
        raise CliError("La ejecución del perfil requiere --confirm-profile")
    try:
        schema = json.loads(Path(args.schema_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CliError(f"No se pudo leer --schema-file: {error}") from error
    if not isinstance(schema, list):
        raise CliError("--schema-file debe contener una lista de campos")
    try:
        result = profile_table(
            args.table,
            schema,
            location=args.location,
            execute=args.execute,
            maximum_bytes_billed=min(args.max_bytes, config.policy_max_bytes),
            account=getattr(args, "account", None) or config.account,
            gcloud_context=_gcloud_context(config, getattr(args, "account", None) or config.account),
        )
    except ProfileError as error:
        raise CliError(str(error)) from error
    payload = result.to_dict()
    output_path = args.output
    if output_path is None:
        digest = hashlib.sha256((args.table + "\n" + json.dumps(schema, sort_keys=True)).encode("utf-8")).hexdigest()[:24]
        output_path = str(Path.home() / ".queryflow" / "profiles" / f"{digest}.json")
    if output_path:
        payload["cache_path"] = output_path
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    _print_result(payload, args.json)
    return 0 if not result.dry_run.errors else 2


def _load_content(
    args: argparse.Namespace,
    resource: ResourceRef,
    *,
    config: QueryflowConfig,
    destination_project: Optional[str] = None,
) -> tuple[bytes, str, dict[str, Any], str]:
    # Catalog entries created from local input do not have a remote head to
    # export.  Treat them as an empty local workspace, just like ``--mode
    # new``; this also keeps context-only starts independent of credentials.
    if resource.fingerprint == "local-input" and not args.content_file:
        if resource.kind == "notebook":
            return empty_notebook(), "content.ipynb", {}, "local-input"
        return b"", "content.sql", {}, "local-input"
    if args.mode == "new" and not args.content_file:
        if resource.kind == "notebook":
            return empty_notebook(), "content.ipynb", {}, "local-input"
        return b"", "content.sql", {}, "local-input"
    if args.content_file:
        content_path = Path(args.content_file)
        try:
            return content_path.read_bytes(), content_path.name, {}, resource.fingerprint
        except OSError as error:
            raise CliError(f"No se pudo leer --content-file: {error}") from error
    if not args.account:
        raise CliError("--account es obligatorio cuando se exporta un recurso remoto")
    client = _configure_dataform_client(
        DataformClient(args.account, destination_project or resource.project),
        config,
        args.account,
    )
    try:
        exported = client.export(resource)
    except DataformError as error:
        raise CliError(str(error)) from error
    return exported.content, exported.filename, exported.metadata, exported.head_commit


def _cmd_start(args: argparse.Namespace) -> int:
    config = _config(args)
    if args.mode == "update":
        if not args.resource:
            raise CliError("--mode update requiere --resource con el nombre canónico")
        if args.content_file:
            raise CliError("--mode update no admite --content-file")
        if not args.account:
            raise CliError("--mode update requiere --account para leer el recurso remoto")
    schedule_spec: ScheduleSpec | None = None
    if args.kind == "scheduled_query" or (args.resource and getattr(args, "schedule", None)):
        if args.mode != "new":
            raise CliError("scheduled_query solo se puede crear con --mode new")
        if args.resource:
            raise CliError("scheduled_query nuevo requiere --kind y parámetros locales, no --resource")
        if not args.content_file:
            raise CliError("scheduled_query requiere --content-file")
        try:
            schedule_spec = ScheduleSpec.from_values(
                schedule=args.schedule,
                location=args.location,
                destination_dataset=args.target_dataset,
                destination_table=args.destination_table,
                write_disposition=args.write_disposition,
                disabled=True,
            )
        except ScheduleError as error:
            raise CliError(str(error)) from error
    catalog = None
    if args.resource:
        catalog = load_catalog(config.catalog_path)
    resource = _resource_from_args(args, catalog)
    explicit_destination = _resolve_config_project(config, args.destination_project) if args.destination_project else None
    context_destination = _active_destination(config)
    destination_project = explicit_destination or context_destination
    if not destination_project:
        # A canonical resource carries its source project, which is also the
        # safe default destination for backwards-compatible local starts.
        # An explicit context or --destination-project still takes priority.
        destination_project = resource.project
    if not destination_project:
        raise CliError("No hay proyecto destino; use --destination-project o configure context")
    if config.policy_enforced:
        decision = evaluate_policy(
            _modern_policy(config),
            operation="start",
            resource_kind=resource.kind,
            mode=args.mode,
            source_project=resource.project,
            destination_project=destination_project,
            location=resource.location,
        )
        if not decision.allowed:
            raise CliError(decision.message)
    if resource.kind not in {"notebook", "shared_query", "scheduled_query"}:
        raise CliError(
            f"El conector de extracción para {resource.kind} se habilita en la expansión; "
            "el piloto cubre notebook, shared_query y scheduled_query local"
        )
    if resource.kind == "scheduled_query" and schedule_spec is None:
        raise CliError("scheduled_query requiere especificación de programación")
    content, filename, metadata, head = _load_content(
        args,
        resource,
        config=config,
        destination_project=destination_project,
    )
    if args.task_id:
        task_id = args.task_id
    else:
        task_id = _safe_task_id(
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{resource.display_name}"
        )
    resource = ResourceRef(
        kind=resource.kind,
        name=resource.name,
        project=resource.project,
        location=resource.location,
        display_name=resource.display_name,
        fingerprint=head,
        metadata={**resource.metadata, "source_metadata": metadata},
    )
    workspace_content = b"" if schedule_spec is not None else content
    task = create_workspace(
        root=Path(args.workspace_root) if args.workspace_root else config.workspace_root,
        task_id=task_id,
        resource=resource,
        content=workspace_content,
        filename=filename,
        mode=args.mode,
        account=args.account,
    )
    update_manifest(
        task,
        destination_project=destination_project,
        project_context={
            "source_project": _active_source(config) or resource.project,
            "destination_project": destination_project,
        },
    )
    if schedule_spec is not None:
        (task / filename).write_bytes(content)
        update_manifest(
            task,
            proposed_sha256=hashlib.sha256(content).hexdigest(),
            schedule_spec=schedule_spec.to_dict(),
        )
    if resource.kind == "notebook":
        if args.mode == "new":
            initialize_new_notebook_workspace(
                task / "cells",
                display_name=resource.display_name,
                project=resource.project,
                location=resource.location,
            )
        else:
            write_cell_workspace(content, task / "cells")
        subprocess.run(["git", "-C", str(task), "add", "--", "cells"], check=True)
        if args.mode != "new":
            subprocess.run(
                ["git", "-C", str(task), "-c", "user.name=QueryFlow", "-c", "user.email=queryflow@localhost", "commit", "--quiet", "-m", "notebook review cells"],
                check=True,
            )
    result = {"task_id": task_id, "task": str(task), "resource": resource.to_dict(), "filename": filename}
    _print_result(result, args.json)
    if args.open_editor:
        completed = subprocess.run(["cloudshell", "edit", str(task / filename)], check=False, capture_output=True, text=True)
        if completed.returncode != 0:
            raise CliError(completed.stderr.strip() or "No se pudo abrir Cloud Shell Editor")
    return 0


def _task_sql(task: Path, manifest: dict[str, Any]) -> str:
    filename = str(manifest["filename"])
    content = (task / filename).read_bytes()
    if manifest.get("resource", {}).get("kind") == "notebook":
        return "\n\n".join(source for _index, _language, source in extract_code_cells(content))
    return content.decode("utf-8")


def _cmd_validate(args: argparse.Namespace) -> int:
    config = _config(args)
    task = Path(args.task).resolve()
    manifest = read_manifest(task)
    if config.policy_enforced:
        resource = manifest.get("resource") or {}
        decision = evaluate_policy(
            _modern_policy(config),
            operation="validate",
            resource_kind=str(resource.get("kind") or ""),
            mode=str(manifest.get("mode") or "copy"),
            source_project=str(resource.get("project") or "") or None,
            # Validation does not choose a destination; enforce that allowlist
            # at publish time when --destination-project is explicit.
            destination_project=None,
            location=str(resource.get("location") or "") or None,
        )
        if not decision.allowed:
            raise CliError(decision.message)
    is_notebook = manifest.get("resource", {}).get("kind") == "notebook"
    if is_notebook:
        try:
            sync_notebook_task(task)
        except TaskError as error:
            raise CliError(str(error)) from error
    project_id = manifest.get("resource", {}).get("project")
    account = args.account or manifest.get("account")
    gcloud_context = _gcloud_context(config, account)
    extraction: dict[str, Any] | None = None
    notebook_without_sql = False
    if is_notebook:
        filename = str(manifest["filename"])
        extracted = analyze_sql_fragments((task / filename).read_bytes())
        fragments = extracted.fragments
        extraction = extracted.to_dict()
        sql = "\n\n".join(fragment for _index, fragment in fragments)
        static = validate_sql_fragments(fragments)
        if not fragments and not extracted.dynamic_cells and static.statement_class == "empty":
            notebook_without_sql = True
            static = static.__class__(
                references=[],
                statement_class="not_applicable",
                read_only=False,
                dry_run_ok=None,
                bytes_processed=None,
                maximum_bytes_billed=None,
                within_configured_limit=None,
                errors=[],
                warnings=[],
                fragments=[],
                error_kind=None,
            )
        if extracted.dynamic_cells:
            static = static.__class__(
                references=static.references,
                statement_class=static.statement_class,
                read_only=static.read_only,
                dry_run_ok=static.dry_run_ok,
                bytes_processed=static.bytes_processed,
                errors=static.errors
                + [f"celda {index}: el SQL es dinámico y no se puede validar automáticamente" for index in extracted.dynamic_cells],
                warnings=static.warnings,
                fragments=static.fragments,
                error_kind="dynamic",
            )
    else:
        sql = _task_sql(task, manifest)
        fragments = [(0, sql)]
        static = validate_sql_text(sql)
    backend = args.backend or config.validation_backend
    if backend not in {"local", "workbench"}:
        raise CliError("--backend debe ser local o workbench")
    backend_details: dict[str, Any] = {"backend": backend}
    if args.static_only or notebook_without_sql:
        dry: dict[str, Any] = {"dry_run_ok": None, "skipped": True}
        if notebook_without_sql:
            dry["reason"] = "Notebook sin fragmentos SQL; se valida su estructura sin ejecutar SQL"
    else:
        if backend == "workbench":
            required = {
                "workbench_instance_project": config.workbench_instance_project,
                "workbench_instance_location": config.workbench_instance_location,
                "workbench_instance_name": config.workbench_instance_name,
                "workbench_job_project": config.workbench_job_project,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise CliError(
                    "La configuración Workbench no tiene: " + ", ".join(missing)
                )
            settings = WorkbenchSettings(
                project=str(config.workbench_project),
                location=str(config.workbench_location),
                instance=str(config.workbench_instance),
                job_project=str(config.workbench_job_project),
                timeout_seconds=config.workbench_timeout_seconds,
            )
            workbench = validate_workbench_fragments(
                fragments,
                settings,
                maximum_bytes_billed=_effective_max_bytes(args, config),
                account=account,
                gcloud_context=gcloud_context,
            )
            result = workbench.result
            backend_details = workbench.backend_details
        else:
            if is_notebook:
                result = dry_run_sql_fragments(
                    fragments,
                    location=manifest.get("resource", {}).get("location"),
                    project_id=project_id,
                    maximum_bytes_billed=_effective_max_bytes(args, config),
                    account=account,
                    gcloud_context=gcloud_context,
                )
            else:
                result = dry_run_sql(
                    sql,
                    location=manifest.get("resource", {}).get("location"),
                    project_id=project_id,
                    maximum_bytes_billed=_effective_max_bytes(args, config),
                    account=account,
                    gcloud_context=gcloud_context,
                )
        dry = result.to_dict()
    dry_errors = dry.get("errors")
    errors = list(static.errors) + (list(dry_errors) if isinstance(dry_errors, list) else [])
    execution: dict[str, Any] = {"executed": False}
    if args.execute_read_only:
        if args.static_only or not args.confirm_execution:
            raise CliError("La ejecución requiere dry-run y --confirm-execution")
        if backend == "workbench":
            raise CliError(
                "El backend Workbench del piloto solo admite dry-run; "
                "la ejecución de filas queda deshabilitada para no sacar datos del perímetro"
            )
        if is_notebook:
            executed = execute_read_only_sql_fragments(
                fragments,
                location=manifest.get("resource", {}).get("location"),
                project_id=project_id,
                maximum_bytes_billed=_effective_max_bytes(args, config),
                account=account,
                gcloud_context=gcloud_context,
            )
        else:
            executed = execute_read_only_sql(
                sql,
                location=manifest.get("resource", {}).get("location"),
                project_id=project_id,
                maximum_bytes_billed=_effective_max_bytes(args, config),
                account=account,
                gcloud_context=gcloud_context,
            )
        # Never persist returned rows in validation.json or the audit package.
        # The optional execution is for a bounded smoke check; only its outcome
        # and row count belong in the durable review record.
        execution = {
            "executed": True,
            "ok": executed.ok,
            "row_count": len(executed.rows),
        }
        if executed.error:
            execution["error"] = executed.error
        if not executed.ok:
            errors.append(executed.error or "La ejecución de lectura falló")
    validation = {
        "ok": not errors and (args.static_only or notebook_without_sql or dry.get("dry_run_ok") is True),
        "static": static.to_dict(),
        "dry_run": dry,
        "execution": execution,
        "references": static.references,
        "error_kind": dry.get("error_kind") or static.error_kind,
        "backend": backend,
        "backend_details": backend_details,
    }
    if extraction is not None:
        validation["extraction"] = extraction
    validation = _redact_validation_messages(validation)
    manifest = mark_validation(task, validation)
    diagnostic: Diagnostic | None = None
    diagnostic_path: str | None = None
    if validation["ok"]:
        clear_latest_diagnostic(task)
    else:
        clear_latest_diagnostic(task)
        diagnostic = _diagnostic_for_task(task, manifest, validation)
        if diagnostic is None:
            static_validation = validation.get("static")
            dry_validation = validation.get("dry_run")
            static_errors = static_validation.get("errors") if isinstance(static_validation, dict) else []
            dry_errors = dry_validation.get("errors") if isinstance(dry_validation, dict) else []
            errors_for_diagnostic = (list(static_errors) if isinstance(static_errors, list) else []) + (list(dry_errors) if isinstance(dry_errors, list) else [])
            diagnostic = make_diagnostic(
                errors_for_diagnostic[0] if errors_for_diagnostic else "La validación no fue publicable",
                stage="validate",
                category=str(validation.get("error_kind") or "") or None,
                context=_task_context(task, manifest),
            )
        diagnostic_path = str(write_diagnostic(task, diagnostic))
    output: dict[str, Any] = {
        "task": str(task),
        "validation": validation,
        "approval_digest": manifest.get("approval_digest"),
    }
    if diagnostic:
        output["diagnostic"] = diagnostic.to_dict()
        output["diagnostic_path"] = diagnostic_path
    _print_result(output, args.json)
    return 0 if validation["ok"] else 2


def _sample_fragments(task: Path, manifest: dict[str, Any]) -> list[tuple[int, str]]:
    filename = str(manifest["filename"])
    content = (task / filename).read_bytes()
    if manifest.get("resource", {}).get("kind") == "notebook":
        return analyze_sql_fragments(content).fragments
    return [(0, content.decode("utf-8"))]


def _cmd_sample(args: argparse.Namespace) -> int:
    config = _config(args)
    if not args.approved_digest:
        raise CliError("La muestra requiere --approved-digest")
    limit = int(args.limit or config.sample_default_rows)
    if limit < 1 or limit > config.sample_max_rows or limit > 5:
        raise CliError(f"El límite de muestra debe estar entre 1 y {min(config.sample_max_rows, 5)}")
    task = Path(args.task).resolve()
    manifest = read_manifest(task)
    validation_path = task / "validation.json"
    if not validation_path.exists():
        raise CliError("La tarea debe validarse antes de ejecutar una muestra")
    validation = _load_validation(task)
    if not validation_is_publishable(validation):
        raise CliError("La tarea no tiene un dry-run publicable")
    filename = str(manifest["filename"])
    current_sha = hashlib.sha256((task / filename).read_bytes()).hexdigest()
    if current_sha != manifest.get("proposed_sha256"):
        raise CliError("El archivo cambió después de validate; genere una nueva aprobación")
    fragments = _sample_fragments(task, manifest)
    if not fragments:
        raise CliError("No se encontraron fragmentos SQL para la muestra")
    selected = fragments
    if args.fragment is not None:
        selected = [item for item in fragments if item[0] == args.fragment]
        if not selected:
            raise CliError(f"No existe el fragmento {args.fragment}")
    elif len(fragments) > 1:
        raise CliError("Un notebook con varias consultas requiere --fragment")
    expected_digest = execution_digest(selected[0][1], fragment_index=selected[0][0], limit=limit)
    if getattr(args, "approved_digest", None) != expected_digest:
        raise CliError("El digest de muestra no coincide con el SQL, fragmento o límite actuales")
    if (args.backend or config.validation_backend) != "workbench":
        raise CliError("La ejecución muestral solo está habilitada dentro de Workbench")
    required = {
        "workbench_instance_project": config.workbench_instance_project,
        "workbench_instance_location": config.workbench_instance_location,
        "workbench_instance_name": config.workbench_instance_name,
        "workbench_job_project": config.workbench_job_project,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise CliError("La configuración Workbench no tiene: " + ", ".join(missing))
    settings = WorkbenchSettings(
        project=str(config.workbench_project),
        location=str(config.workbench_location),
        instance=str(config.workbench_instance),
        job_project=str(config.workbench_job_project),
        timeout_seconds=config.workbench_timeout_seconds,
    )
    result = execute_workbench_sample(
        selected,
        settings,
        maximum_bytes_billed=_effective_max_bytes(args, config),
        limit=limit,
        account=args.account or manifest.get("account"),
        gcloud_context=_gcloud_context(config, args.account or manifest.get("account")),
    )
    receipt = {
        "executed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "execution_digest": expected_digest,
        "content_sha256": current_sha,
        "fragment": selected[0][0],
        "limit": limit,
        "ok": result.get("ok", False),
        "row_count": len(result.get("rows") or []),
        "columns": list((result.get("rows") or [{}])[0].keys()) if result.get("rows") else [],
        "truncated": bool(result.get("truncated")),
        "errors": result.get("errors") or [],
    }
    (task / "sample-receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    output = {"task": str(task), "execution_digest": expected_digest, "receipt": receipt, "rows": result.get("rows") or []}
    _print_result(output, args.json)
    return 0 if result.get("ok") else 2


def _cmd_review(args: argparse.Namespace) -> int:
    if args.watch and not args.serve:
        raise CliError("--watch requiere --serve")
    config = _config(args)
    task = Path(args.task).resolve()
    manifest = read_manifest(task)
    if manifest.get("resource", {}).get("kind") == "notebook":
        try:
            sync_notebook_task(task)
        except TaskError as error:
            raise CliError(str(error)) from error
    filename = str(manifest["filename"])
    before_bytes = baseline_file(task, filename)
    after_bytes = (task / filename).read_bytes()
    validation_path = task / "validation.json"
    validation = json.loads(validation_path.read_text(encoding="utf-8")) if validation_path.exists() else {"status": "pending"}
    preferences = {
        "review_theme": config.review_theme,
        "review_mode": config.review_mode,
        "review_only_changes": config.review_only_changes,
        "review_context_lines": config.review_context_lines,
    }
    review = write_review_html(task, before_bytes, after_bytes, validation, preferences)
    result = {"review": str(review), "task": str(task)}
    _print_result(result, args.json)
    if args.serve:
        server, url = serve_review(task, args.port, preferences)
        print(f"Review disponible en {url}", flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
    return 0


def _routine_dictionary(args: argparse.Namespace):
    try:
        return load_dictionary(Path(args.dictionary).expanduser())
    except MigrationDictionaryError as error:
        raise CliError(str(error)) from error


def _routine_manifest_path(config: QueryflowConfig, campaign_id: str, output: str | None = None) -> Path:
    if output:
        return Path(output).expanduser()
    return config.workspace_root.parent / "migrations" / campaign_id / "manifest.json"


def _routine_project(config: QueryflowConfig, value: str | None, *, destination: bool) -> str:
    selected = value or (_active_destination(config) if destination else _active_source(config))
    if not selected:
        raise CliError(
            "Indica --destination-project/--source-project o configura context set antes de la campaña"
        )
    return _resolve_config_project(config, selected) or selected


def _routine_account(config: QueryflowConfig, args: argparse.Namespace) -> str:
    account = str(getattr(args, "account", None) or config.account or "").strip()
    if not account:
        raise CliError("--account es obligatorio para acceder a BigQuery")
    return account


def _routine_settings(config: QueryflowConfig) -> WorkbenchSettings:
    required = {
        "workbench_instance_project": config.workbench_instance_project,
        "workbench_instance_location": config.workbench_instance_location,
        "workbench_instance_name": config.workbench_instance_name,
        "workbench_job_project": config.workbench_job_project,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise CliError("La configuración Workbench no tiene: " + ", ".join(missing))
    return WorkbenchSettings(
        project=str(config.workbench_instance_project),
        location=str(config.workbench_instance_location),
        instance=str(config.workbench_instance_name),
        job_project=str(config.workbench_job_project),
        timeout_seconds=config.workbench_timeout_seconds,
    )


def _routine_client(
    args: argparse.Namespace,
    config: QueryflowConfig,
    *,
    source_project: str,
    destination_project: str,
    destination_dataset: str | None = None,
    backend: str | None = None,
) -> tuple[BigQueryRoutineClient, Any]:
    selected = str(backend or getattr(args, "backend", None) or config.routine_backend or "auto")
    if selected not in {"direct", "workbench", "auto"}:
        raise CliError("El backend de rutinas debe ser direct, workbench o auto")
    account = _routine_account(config, args)
    context = _gcloud_context(config, account)
    transport = None
    if selected == "workbench":
        transport = WorkbenchRoutineTransport(
            _routine_settings(config),
            account=account,
            gcloud_context=context,
        )
        return (
            BigQueryRoutineClient(
                account,
                destination_project,
                source_project=source_project,
                transport=transport,
                requests_per_minute=config.routine_requests_per_minute,
                request_timeout_seconds=float(getattr(args, "request_timeout", 180.0)),
                gcloud_context=context,
            ),
            transport,
        )
    client = BigQueryRoutineClient(
        account,
        destination_project,
        source_project=source_project,
        requests_per_minute=config.routine_requests_per_minute,
        request_timeout_seconds=float(getattr(args, "request_timeout", 180.0)),
        gcloud_context=context,
    )
    if selected == "direct":
        return client, None
    # Auto selection is decided by a read-only perimeter probe. Do not switch
    # backends halfway through a campaign after a partial write.
    try:
        client.list_datasets(source_project)
        client.get_dataset(
            destination_project,
            destination_dataset or config.routine_destination_dataset,
        )
    except RoutineError as error:
        message = str(error).upper()
        if error.kind not in {"network", "permission", "transport"} or not any(
            marker in message
            for marker in ("VPC", "SERVICE_CONTROLS", "SECURITY_POLICY", "PERIMETER")
        ):
            raise
        transport = WorkbenchRoutineTransport(
            _routine_settings(config),
            account=account,
            gcloud_context=context,
        )
        return (
            BigQueryRoutineClient(
                account,
                destination_project,
                source_project=source_project,
                transport=transport,
                requests_per_minute=config.routine_requests_per_minute,
                request_timeout_seconds=float(getattr(args, "request_timeout", 180.0)),
                gcloud_context=context,
            ),
            transport,
        )
    return client, None


def _routine_destination_snapshots(
    client: BigQueryRoutineClient,
    destination_project: str,
    destination_dataset: str,
    destination_location: str,
) -> tuple[list[RoutineSnapshot], list[dict[str, Any]]]:
    values: list[RoutineSnapshot] = []
    errors: list[dict[str, Any]] = []
    try:
        summaries = client.list_routines(destination_project, destination_dataset)
    except RoutineError as error:
        if error.kind == "not_found":
            raise CliError(
                f"El dataset destino {destination_project}.{destination_dataset} no existe; "
                "QueryFlow no crea datasets automáticamente"
            ) from error
        raise
    for summary in summaries:
        try:
            reference = summary.get("routineReference") or {}
            routine_id = str(reference.get("routineId") or "")
            if not routine_id:
                continue
            full = client.get_routine(destination_project, destination_dataset, routine_id)
            values.append(
                RoutineSnapshot.from_routine(
                    destination_project,
                    destination_dataset,
                    destination_location,
                    full,
                )
            )
        except RoutineError as error:
            errors.append(
                {"kind": error.kind, "routine": str(summary.get("routineReference") or ""), "message": str(error)[:500]}
            )
    return values, errors


def _routine_policy_or_error(
    config: QueryflowConfig,
    *,
    operation: str,
    source: str,
    destination: str,
    location: str | None = None,
) -> None:
    policy = _modern_policy(config)
    # Inventory must first read the destination metadata instead of guessing a
    # location.  Keep project/resource checks now and apply an allowlisted
    # location once the provider returns it.
    if location is None and policy.allowed_locations:
        policy = replace(policy, allowed_locations=())
    decision = evaluate_policy(
        policy,
        operation=operation,
        resource_kind="routine",
        mode="copy",
        source_project=source,
        destination_project=destination,
        location=location,
    )
    if not decision.allowed:
        raise CliError(decision.message)


def _cmd_routine_inventory(args: argparse.Namespace) -> int:
    config = _config(args)
    source_project = _routine_project(config, args.source_project, destination=False)
    destination_project = _routine_project(config, args.destination_project, destination=True)
    _routine_policy_or_error(
        config,
        operation="routine_campaign_publish",
        source=source_project,
        destination=destination_project,
    )
    dictionary = _routine_dictionary(args)
    campaign_id = str(args.campaign_id or datetime.now(timezone.utc).strftime("routines-%Y%m%dT%H%M%SZ"))
    output_path = _routine_manifest_path(config, campaign_id, args.output)
    requested_backend = args.backend or config.routine_backend
    destination_dataset = str(
        args.destination_dataset or config.routine_destination_dataset or ROUTINE_DESTINATION_DATASET
    )
    client, transport = _routine_client(
        args,
        config,
        source_project=source_project,
        destination_project=destination_project,
        destination_dataset=destination_dataset,
        backend=requested_backend,
    )
    try:
        destination_meta = client.get_dataset(destination_project, destination_dataset)
        destination_location = str(destination_meta.get("location") or args.destination_location or "").strip()
        if not destination_location:
            raise CliError("No se pudo determinar la ubicación del dataset destino")
        _routine_policy_or_error(
            config,
            operation="routine_campaign_publish",
            source=source_project,
            destination=destination_project,
            location=destination_location,
        )
        source_access: list[dict[str, Any]] = []
        source_snapshots, source_errors = inventory_routine_snapshots(
            client,
            source_project,
            source_datasets=args.source_dataset,
            default_location=str(args.source_location or ""),
            access_report=source_access,
        )
        destination_snapshots, destination_errors = _routine_destination_snapshots(
            client,
            destination_project,
            destination_dataset,
            destination_location,
        )
        manifest = build_routine_manifest(
            campaign_id=campaign_id,
            source_project=source_project,
            destination_project=destination_project,
            destination_dataset=destination_dataset,
            destination_location=destination_location,
            source_snapshots=source_snapshots,
            destination_snapshots=destination_snapshots,
            dictionary=dictionary,
            source_datasets=args.source_dataset,
            secret_handling=args.secret_handling,
            batch_size=args.batch_size or config.routine_batch_size,
            source_inventory_errors=[*source_errors, *destination_errors],
            backend=("workbench" if transport is not None else "direct"),
            permissions={
                "source": source_access,
                "destination": [
                    {
                        "project": destination_project,
                        "dataset": destination_dataset,
                        "location": destination_location,
                        "status": "read",
                        "access": destination_meta.get("access") or [],
                    }
                ],
                "automated_changes": [],
            },
        )
        manifest["request_stats"] = client.request_stats.to_dict()
        manifest["inventory"]["destination_routines"] = len(destination_snapshots)
        _routine_write_proposals(manifest, output_path)
        save_routine_manifest(manifest, output_path)
        reports = write_routine_reports(manifest, output_path)
    finally:
        if transport is not None:
            transport.close()
    _print_result(
        {
            "ok": True,
            "manifest": str(output_path),
            "campaign_id": campaign_id,
            "backend": manifest.get("backend"),
            "inventory": manifest.get("inventory"),
            "publication_digest": manifest.get("publication_digest"),
            "sealed_publication_digest": manifest.get("sealed_publication_digest"),
            "lots": manifest.get("lots"),
            "reports": reports,
        },
        args.json,
    )
    return 0


def _routine_rebuild_from_remote(
    manifest: Mapping[str, Any],
    args: argparse.Namespace,
    config: QueryflowConfig,
    dictionary: Any,
) -> tuple[dict[str, Any], BigQueryRoutineClient, Any]:
    source_project = str(manifest["source_project"])
    destination_project = str(manifest["destination_project"])
    client, transport = _routine_client(
        args,
        config,
        source_project=source_project,
        destination_project=destination_project,
        destination_dataset=str(manifest.get("destination_dataset") or config.routine_destination_dataset),
        backend=str(manifest.get("backend") or "direct"),
    )
    destination_dataset = str(manifest.get("destination_dataset") or config.routine_destination_dataset)
    try:
        destination_meta = client.get_dataset(destination_project, destination_dataset)
        destination_location = str(destination_meta.get("location") or manifest.get("destination_location") or "")
        if not destination_location:
            raise CliError("No se pudo determinar la ubicación del dataset destino")
        _routine_policy_or_error(
            config,
            operation="routine_campaign_publish",
            source=source_project,
            destination=destination_project,
            location=destination_location,
        )
        source_access: list[dict[str, Any]] = []
        source_datasets = [
            str(item)
            for item in (manifest.get("source_datasets") or [])
            if str(item).strip()
        ]
        source_snapshots, source_errors = inventory_routine_snapshots(
            client,
            source_project,
            source_datasets=source_datasets or None,
            access_report=source_access,
        )
        destination_snapshots, destination_errors = _routine_destination_snapshots(
            client, destination_project, destination_dataset, destination_location
        )
        rebuilt = build_routine_manifest(
            campaign_id=str(manifest["campaign_id"]),
            source_project=source_project,
            destination_project=destination_project,
            destination_dataset=destination_dataset,
            destination_location=destination_location,
            source_snapshots=source_snapshots,
            destination_snapshots=destination_snapshots,
            dictionary=dictionary,
            source_datasets=source_datasets or None,
            secret_handling=str((manifest.get("policy") or {}).get("secret_handling") or "block"),
            batch_size=int(manifest.get("batch_size") or config.routine_batch_size),
            source_inventory_errors=[*source_errors, *destination_errors],
            backend=str(manifest.get("backend") or "direct"),
            permissions={
                "source": source_access,
                "destination": [
                    {
                        "project": destination_project,
                        "dataset": destination_dataset,
                        "location": destination_location,
                        "status": "read",
                        "access": destination_meta.get("access") or [],
                    }
                ],
                "automated_changes": [],
            },
        )
        return rebuilt, client, transport
    except Exception:
        if transport is not None:
            transport.close()
        raise


def _routine_write_proposals(manifest: dict[str, Any], manifest_path: Path) -> None:
    root = manifest_path.parent / "proposals"
    root.mkdir(parents=True, exist_ok=True)
    for item in manifest.get("resources") or []:
        source = item.get("source") or {}
        routine_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(source.get("routine_id") or "routine"))
        proposal_path = root / f"{int(item.get('ordinal') or 0):04d}_{routine_id}.sql"
        if bool((item.get("security") or {}).get("sealed")):
            proposal_path.write_text(
                "[Contenido sellado; consultar el digest de seguridad y el ticket autorizado]\\n",
                encoding="utf-8",
            )
        else:
            proposal_path.write_text(
                str((item.get("proposal") or {}).get("definitionBody") or ""),
                encoding="utf-8",
            )
        item["proposal_file"] = str(proposal_path.relative_to(manifest_path.parent))


def _cmd_routine_prepare(args: argparse.Namespace) -> int:
    config = _config(args)
    manifest_path = Path(args.manifest).expanduser()
    manifest = load_routine_manifest(manifest_path)
    _routine_policy_or_error(
        config,
        operation="routine_campaign_publish",
        source=str(manifest.get("source_project") or ""),
        destination=str(manifest.get("destination_project") or ""),
        location=str(manifest.get("destination_location") or "") or None,
    )
    dictionary = _routine_dictionary(args)
    rebuilt, client, transport = _routine_rebuild_from_remote(manifest, args, config, dictionary)
    try:
        _routine_write_proposals(rebuilt, manifest_path)
        rebuilt["prepared_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        rebuilt["request_stats"] = client.request_stats.to_dict()
        rebuilt["publication_digest"] = build_routine_digest(rebuilt)
        rebuilt["sealed_publication_digest"] = build_routine_sealed_digest(rebuilt)
        save_routine_manifest(rebuilt, manifest_path)
        reports = write_routine_reports(rebuilt, manifest_path)
    finally:
        if transport is not None:
            transport.close()
    _print_result(
        {
            "ok": True,
            "manifest": str(manifest_path),
            "publication_digest": rebuilt.get("publication_digest"),
            "sealed_publication_digest": rebuilt.get("sealed_publication_digest"),
            "reports": reports,
            "inventory": rebuilt.get("inventory"),
        },
        args.json,
    )
    return 0


def _cmd_routine_review(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).expanduser()
    manifest = load_routine_manifest(manifest_path)
    html_path = manifest_path.parent / "review.html"
    html_path.write_text(render_routine_review(manifest), encoding="utf-8")
    result: dict[str, Any] = {"review": str(html_path), "manifest": str(manifest_path)}
    if args.serve:
        server, url = serve_routine_review(manifest_path, args.port)
        result["url"] = url
        _print_result(result, args.json)
        print(f"Review disponible en {url}", flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
        return 0
    _print_result(result, args.json)
    return 0


def _cmd_routine_run(args: argparse.Namespace) -> int:
    if not args.execute_migration:
        raise CliError("La publicación requiere --execute-migration y --approved-digest explícitos")
    if not args.approved_digest:
        raise CliError("--approved-digest es obligatorio para publicar rutinas")
    config = _config(args)
    manifest_path = Path(args.manifest).expanduser()
    manifest = load_routine_manifest(manifest_path)
    _routine_policy_or_error(
        config,
        operation="routine_campaign_publish",
        source=str(manifest.get("source_project") or ""),
        destination=str(manifest.get("destination_project") or ""),
        location=str(manifest.get("destination_location") or "") or None,
    )
    dictionary = _routine_dictionary(args)
    lot = int(args.lot) if args.lot is not None else None
    validate_routine_manifest(
        manifest,
        approved_digest=args.approved_digest,
        approved_sealed_digest=args.approved_sealed_digest,
        security_reference=args.security_reference,
        lot=lot,
        allow_blocked=True,
    )
    client, transport = _routine_client(
        args,
        config,
        source_project=str(manifest["source_project"]),
        destination_project=str(manifest["destination_project"]),
        destination_dataset=str(manifest.get("destination_dataset") or config.routine_destination_dataset),
        backend=str(manifest.get("backend") or "direct"),
    )
    audit_path = manifest_path.parent / "routine-audit.json"
    try:
        audit_path = write_routine_audit(
            manifest,
            manifest_path,
            approved_digest=args.approved_digest,
            approved_sealed_digest=args.approved_sealed_digest,
            security_reference=args.security_reference,
            phase="approved",
        )
        receipts = publish_routines(
            manifest,
            client,
            dictionary=dictionary,
            approved_digest=args.approved_digest,
            approved_sealed_digest=args.approved_sealed_digest,
            security_reference=args.security_reference,
            lot=lot,
            allow_blocked=True,
        )
        execution = dict(manifest.get("execution") or {})
        execution["receipts"] = [*execution.get("receipts", []), *receipts]
        execution["published"] = sum(item.get("status") == "published_verified" for item in execution["receipts"])
        execution["already_present"] = sum(item.get("status") == "already_present_identical" for item in execution["receipts"])
        execution["blocked"] = sum(item.get("status") in {"blocked", "destination_conflict", "security_blocked"} for item in execution["receipts"])
        execution["failed"] = sum(item.get("status") == "failed" for item in execution["receipts"])
        execution["last_run_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        manifest["execution"] = execution
        for receipt in receipts:
            for record in manifest.get("resources") or []:
                if (record.get("source") or {}).get("name") == receipt.get("source"):
                    record["receipt"] = receipt
                    if receipt.get("status") == "published_verified":
                        record["status"] = "published_verified"
                    elif receipt.get("status") == "already_present_identical":
                        record["status"] = "already_present_identical"
                    elif receipt.get("status") == "failed":
                        record["status"] = "failed"
        manifest["request_stats"] = client.request_stats.to_dict()
        save_routine_manifest(manifest, manifest_path)
        audit_path = write_routine_audit(
            manifest,
            manifest_path,
            approved_digest=args.approved_digest,
            approved_sealed_digest=args.approved_sealed_digest,
            security_reference=args.security_reference,
            phase="completed",
            receipts=receipts,
        )
        reports = write_routine_reports(manifest, manifest_path)
    except Exception as error:
        # Preserve a failure checkpoint without including routine definitions
        # or provider response bodies in the audit artifact.
        write_routine_audit(
            manifest,
            manifest_path,
            approved_digest=args.approved_digest,
            approved_sealed_digest=args.approved_sealed_digest,
            security_reference=args.security_reference,
            phase="failed",
            receipts=[{"status": "failed", "message": str(error)[:500]}],
        )
        raise
    finally:
        if transport is not None:
            transport.close()
    _print_result(
        {
            "ok": True,
            "manifest": str(manifest_path),
            "lot": lot,
            "approved_digest": args.approved_digest,
            "receipts": receipts,
            "execution": manifest.get("execution"),
            "reports": reports,
            "audit": str(audit_path),
        },
        args.json,
    )
    return 0


def _cmd_routine_report(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).expanduser()
    manifest = load_routine_manifest(manifest_path)
    reports = write_routine_reports(manifest, manifest_path)
    _print_result({"manifest": str(manifest_path), "reports": reports}, args.json)
    return 0


def _cmd_finops_assess(args: argparse.Namespace) -> int:
    config = _config(args)
    result = run_assessment(
        config,
        account=args.account,
        projects=args.projects,
        window_days=args.window_days,
        billing_table=args.billing_table,
        business_context_path=Path(args.business_context).expanduser() if args.business_context else None,
        output_root=Path(args.output_root).expanduser() if args.output_root else None,
    )
    payload = {
        "assessment": result["manifest"],
        "directory": result["directory"],
        "report": {
            "status": result["report"].get("status"),
            "executive_summary": result["report"].get("executive_summary"),
            "finding_count": len(result["report"].get("opportunities", [])),
        },
    }
    _print_result(payload, args.json)
    return 0 if result["manifest"].get("status") != "failed" else 2


def _cmd_finops_show(args: argparse.Namespace) -> int:
    loaded = load_assessment(
        args.assessment,
        output_root=Path(args.output_root).expanduser() if args.output_root else None,
    )
    _print_result(loaded, args.json)
    return 0


def _cmd_finops_review(args: argparse.Namespace) -> int:
    loaded = load_assessment(
        args.assessment,
        output_root=Path(args.output_root).expanduser() if args.output_root else None,
    )
    result = {
        "assessment": loaded["manifest"],
        "directory": loaded["directory"],
        "review": str(Path(loaded["directory"]) / "report.html"),
        "read_only": True,
    }
    _print_result(result, args.json)
    if args.serve:
        server, url = serve_assessment(args.assessment, output_root=Path(args.output_root).expanduser() if args.output_root else None, port=args.port)
        print(f"Review FinOps disponible en {url}", flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
    return 0


def _publish_scheduled_query(
    *,
    args: argparse.Namespace,
    config: QueryflowConfig,
    task: Path,
    manifest: dict[str, Any],
    resource: ResourceRef,
) -> int:
    """Create only a new, disabled scheduled query and verify its readback."""
    if manifest.get("mode") != "new":
        raise CliError("scheduled_query solo se puede publicar desde una tarea new")
    if config.mode != "pilot":
        raise CliError("scheduled_query del piloto requiere mode: pilot")
    if not config.allow_create_dataset:
        raise CliError("La publicación requiere allow_create_dataset: true")
    if args.destination_project != resource.project:
        raise CliError("El proyecto destino debe coincidir con el proyecto de la query nueva")
    if resource.location != (manifest.get("schedule_spec") or {}).get("location"):
        raise CliError("La región del recurso y de la programación no coincide")
    spec_raw = manifest.get("schedule_spec")
    if not isinstance(spec_raw, dict):
        raise CliError("La tarea no contiene schedule_spec")
    try:
        spec = ScheduleSpec.from_values(
            schedule=str(spec_raw.get("schedule") or ""),
            location=str(spec_raw.get("location") or ""),
            destination_dataset=str(spec_raw.get("destination_dataset") or ""),
            destination_table=str(spec_raw.get("destination_table") or ""),
            write_disposition=str(spec_raw.get("write_disposition") or "WRITE_APPEND"),
            disabled=bool(spec_raw.get("disabled", True)),
        )
    except ScheduleError as error:
        raise CliError(str(error)) from error
    filename = str(manifest["filename"])
    try:
        content = (task / filename).read_bytes()
        query = content.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise CliError(f"El SQL de la tarea no es legible: {error}") from error
    current_sha = hashlib.sha256(content).hexdigest()
    if current_sha != manifest.get("proposed_sha256"):
        raise CliError("El archivo cambió después de validate; genere una nueva aprobación")
    if config.audit_root is None and not args.audit_root:
        raise CliError("Configura audit_root antes de publicar")
    audit_root = args.audit_root or config.audit_root
    if audit_root is None:
        raise CliError("audit_root inválido")
    try:
        audit_store = _audit_store(audit_root, gcloud_context=_gcloud_context(config, args.account))
        audit_receipt = audit_store.archive(task)
    except AuditError as error:
        raise CliError(str(error)) from error
    client = TransferClient(args.account, args.destination_project)
    dataset: dict[str, Any] | None = None
    transfer: dict[str, Any] | None = None

    def record_partial(error: Exception) -> None:
        partial = {
            "task_id": manifest["task_id"],
            "mode": "scheduled_query",
            "published": False,
            "audit": audit_receipt,
            "dataset": dataset,
            "transfer_config": transfer,
            "error": str(error),
            "destination_project": args.destination_project,
        }
        try:
            audit_store.record_publish(manifest["task_id"], partial)
        except AuditError:
            # Preserve the original API/verification error for the caller.
            pass

    try:
        dataset = client.ensure_dataset(
            args.destination_project,
            spec.destination_dataset,
            spec.location,
            {"queryflow_pilot": "true"},
        )
        transfer = client.create_scheduled_query(
            project=args.destination_project,
            location=spec.location,
            display_name=args.display_name or resource.display_name,
            query=query,
            destination_dataset=spec.destination_dataset,
            destination_table=spec.destination_table,
            schedule=spec.schedule,
            write_disposition=spec.write_disposition,
            disabled=True,
        )
        name = transfer.get("name") if isinstance(transfer, dict) else None
        if not isinstance(name, str) or not name:
            raise CliError("La TransferConfig creada no tiene name")
        verified = client.get_transfer_config(name)
        params = verified.get("params") or {}
        expected = {
            "dataSourceId": "scheduled_query",
            "destinationDatasetId": spec.destination_dataset,
            "schedule": spec.schedule,
            "disabled": True,
        }
        mismatches = [
            key for key, value in expected.items() if verified.get(key) != value
        ]
        if params.get("query") != query:
            mismatches.append("params.query")
        if params.get("destination_table_name_template") != spec.destination_table:
            mismatches.append("params.destination_table_name_template")
        if params.get("write_disposition") != spec.write_disposition:
            mismatches.append("params.write_disposition")
        if mismatches:
            raise CliError("La TransferConfig no coincide con lo aprobado: " + ", ".join(mismatches))
    except (TransferError, CliError) as error:
        record_partial(error)
        if isinstance(error, CliError):
            raise
        raise CliError(str(error)) from error
    final_receipt = {
        "task_id": manifest["task_id"],
        "mode": "scheduled_query",
        "published": True,
        "audit": audit_receipt,
        "dataset": dataset,
        "transfer_config": verified,
        "verification": {"ok": True, "disabled": True},
        "destination_project": args.destination_project,
    }
    try:
        audit_store.record_publish(manifest["task_id"], final_receipt)
    except AuditError as error:
        raise CliError(str(error)) from error
    manifest.update({"published": True, "destination": verified, "audit_receipt": final_receipt})
    (task / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _print_result(final_receipt, args.json)
    return 0


def _cmd_publish(args: argparse.Namespace) -> int:
    config = _config(args)
    task = Path(args.task).resolve()
    manifest = read_manifest(task)
    force_published = bool(getattr(args, "force_publish", False))
    if force_published:
        if config.mode != "full-access" or not config.allow_force_publish:
            raise CliError("--force-publish requiere el perfil full-access y allow_force_publish: true")
        if not str(getattr(args, "reason", "") or "").strip():
            raise CliError("--force-publish requiere --reason con la autorización del analista")
        if args.approved_digest or args.approved_exception_digest:
            raise CliError("--force-publish no se combina con un digest de aprobación")
        validation = _load_validation(task)
        exception_used = False
    else:
        validation_path = task / "validation.json"
        if not validation_path.exists():
            raise CliError("La tarea no tiene validation.json")
        validation = json.loads(validation_path.read_text(encoding="utf-8"))
        if bool(args.approved_digest) == bool(args.approved_exception_digest):
            raise CliError("Publica con exactamente uno de --approved-digest o --approved-exception-digest")
        exception_used = bool(args.approved_exception_digest)
    if exception_used:
        if config.mode != "team" or not config.allow_static_exception:
            raise CliError("La publicación por excepción requiere perfil team y allow_static_exception: true")
        if (manifest.get("resource") or {}).get("kind") not in {"notebook", "shared_query"}:
            raise CliError("La excepción estática solo aplica a notebooks y shared queries")
        exception_path = task / "exception.json"
        if not exception_path.exists():
            raise CliError("La tarea no tiene exception.json; prepara primero la excepción")
        try:
            exception = json.loads(exception_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CliError(f"exception.json no es válido: {error}") from error
        if not isinstance(exception, dict):
            raise CliError("exception.json debe contener un objeto")
        stored_exception_digest = exception.get("exception_digest")
        unsigned_exception = dict(exception)
        unsigned_exception.pop("exception_digest", None)
        if not isinstance(stored_exception_digest, str) or exception_digest(unsigned_exception) != stored_exception_digest:
            raise CliError("El digest de excepción almacenado no es íntegro")
        if args.approved_exception_digest != stored_exception_digest or args.approved_exception_digest != manifest.get("exception_digest"):
            raise CliError("El digest de excepción aprobado no coincide con la tarea actual")
        exception_resource: dict[str, Any] = dict(exception["resource"]) if isinstance(exception.get("resource"), dict) else {}
        manifest_resource: dict[str, Any] = dict(manifest["resource"]) if isinstance(manifest.get("resource"), dict) else {}
        resource_keys = ("kind", "name", "project", "location")
        if exception.get("task_id") != manifest.get("task_id") or exception.get("mode") != manifest.get("mode"):
            raise CliError("La excepción no está vinculada a la tarea actual")
        if any(exception_resource.get(key) != manifest_resource.get(key) for key in resource_keys):
            raise CliError("La excepción no está vinculada al recurso actual")
        if exception.get("baseline_sha256") != manifest.get("baseline_sha256"):
            raise CliError("La excepción no está vinculada a la línea base actual")
        current_sha = _current_task_content_sha(task, manifest)
        if current_sha != exception.get("content_sha256"):
            raise CliError("El contenido cambió después de preparar la excepción")
        if exception.get("validation_sha256") != _validation_digest(validation):
            raise CliError("La validación cambió después de preparar la excepción")
        static = validation.get("static") or {}
        if static.get("errors"):
            raise CliError("La excepción no puede saltarse errores de sintaxis estática")
        blocker = str(exception.get("blocker_category") or "")
        if blocker not in {"authentication", "vpc", "network"}:
            raise CliError(f"El bloqueo {blocker} no admite excepción estática")
    elif not force_published:
        expected = manifest.get("approval_digest")
        if manifest.get("validation_status") != "ready" or not validation_is_publishable(validation):
            raise CliError("La tarea no está en estado ready")
        unsigned_manifest = dict(manifest)
        unsigned_manifest.pop("approval_digest", None)
        if args.approved_digest != approval_digest(unsigned_manifest, validation) or args.approved_digest != expected:
            raise CliError("El digest aprobado no coincide con el plan actual")
    mode = manifest.get("mode")
    if mode not in {"copy", "new", "update"}:
        raise CliError("La tarea tiene un modo de publicación no soportado")
    resource = ResourceRef.from_dict(manifest["resource"])
    def _publish_destination(value: Any) -> Optional[str]:
        if value is None or not str(value).strip():
            return None
        text = str(value).strip()
        return config.project_aliases.get(text, text)

    requested_destination = _publish_destination(args.destination_project)
    snapshot_value = manifest.get("destination_project")
    # Tasks written before destination snapshots were introduced remain safe:
    # their canonical resource project is the only implicit destination they
    # may use. A caller cannot repoint such a task to another project.
    snapshot_destination = (str(snapshot_value).strip() if snapshot_value else None) or resource.project
    if requested_destination and snapshot_destination and requested_destination != snapshot_destination:
        raise CliError("El proyecto destino explícito no coincide con el snapshot de la tarea")
    destination_project = requested_destination or snapshot_destination
    if not destination_project:
        raise CliError("No hay proyecto destino; usa --destination-project o crea la tarea con un contexto")
    if not args.account:
        raise CliError("--account es obligatorio para publicar")
    # Keep the scheduled-query helper's historical interface while ensuring
    # the destination is resolved from the task snapshot when omitted.
    args.destination_project = destination_project
    if config.policy_enforced:
        decision = evaluate_policy(
            _modern_policy(config),
            operation="publish",
            resource_kind=resource.kind,
            mode=str(manifest.get("mode") or "copy"),
            source_project=resource.project,
            destination_project=destination_project,
            location=resource.location,
        )
        if not decision.allowed:
            raise CliError(decision.message)
    if resource.kind == "scheduled_query":
        if force_published:
            raise CliError("--force-publish no permite consultas programadas")
        if exception_used:
            raise CliError("Las consultas programadas no admiten excepciones estáticas")
        return _publish_scheduled_query(
            args=args,
            config=config,
            task=task,
            manifest=manifest,
            resource=resource,
        )
    if resource.kind not in {"notebook", "shared_query"}:
        raise CliError(f"Tipo no soportado para publicación: {resource.kind}")
    if mode == "update":
        if config.mode not in {"team", "full-access"} or not config.allow_update_existing:
            raise CliError("Actualizar un recurso existente requiere mode: team/full-access y allow_update_existing: true")
        if destination_project != resource.project:
            raise CliError("La actualización debe permanecer en el proyecto del recurso original")
        if args.repository_id:
            raise CliError("--repository-id no se admite al actualizar un recurso existente")
        if resource.fingerprint == "local-input":
            raise CliError("La actualización requiere un recurso remoto con commit base")
    if resource.kind == "notebook":
        try:
            sync_notebook_task(task)
        except TaskError as error:
            raise CliError(str(error)) from error
    filename = str(manifest["filename"])
    current_sha = hashlib.sha256((task / filename).read_bytes()).hexdigest()
    if not force_published and current_sha != manifest.get("proposed_sha256"):
        raise CliError("El archivo cambió después de validate; genere una nueva aprobación")
    force_authorization: dict[str, Any] | None = None
    if force_published:
        force_authorization = {
            "schema_version": 1,
            "task_id": manifest["task_id"],
            "resource": manifest.get("resource"),
            "mode": mode,
            "destination_project": destination_project,
            "account": args.account,
            "profile": config.profile_name,
            "reason": redact_message(str(args.reason).strip())[:1000],
            "requested_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "validation_status": manifest.get("validation_status", "pending"),
            "validation_present": bool(validation),
            "content_sha256": current_sha,
            "baseline_sha256": manifest.get("baseline_sha256"),
        }
        (task / "force-authorization.json").write_text(
            json.dumps(force_authorization, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest["proposed_sha256"] = current_sha
        manifest["force_authorization"] = force_authorization
        (task / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if config.audit_root is None and not args.audit_root:
        raise CliError("Configura audit_root antes de publicar")
    audit_root = args.audit_root or config.audit_root
    if audit_root is None:
        raise CliError("audit_root inválido")
    content = (task / filename).read_bytes()
    source_metadata = resource.metadata.get("source_metadata") or {}
    source_project = config.source_projects[0] if len(config.source_projects) == 1 else ""
    if source_project:
        client = _configure_dataform_client(
            DataformClient(args.account, destination_project, source_project=source_project),
            config,
            args.account,
        )
    else:
        # Keep the constructor compatible with custom transports used by
        # integrations and tests that predate the optional source allowlist.
        client = _configure_dataform_client(
            DataformClient(args.account, destination_project),
            config,
            args.account,
        )
    if resource.fingerprint != "local-input":
        try:
            remote = client.export(resource)
        except DataformError as error:
            raise CliError(str(error)) from error
        if remote.head_commit != resource.fingerprint or hashlib.sha256(remote.content).hexdigest() != manifest.get("baseline_sha256"):
            raise CliError("El recurso remoto cambió después de crear el workspace")
        source_metadata = remote.metadata
        source = ExportedAsset(resource, filename, content, source_metadata, remote.head_commit)
    else:
        source = ExportedAsset(resource, filename, content, source_metadata, resource.fingerprint)
    try:
        audit_store = _audit_store(audit_root, gcloud_context=_gcloud_context(config, args.account))
        audit_receipt = audit_store.archive(task)
    except AuditError as error:
        raise CliError(str(error)) from error
    try:
        if mode == "update":
            published = client.update_file(
                resource.name,
                filename,
                content,
                required_head_commit=resource.fingerprint,
                author_name=args.author_name,
                author_email=args.account,
            )
        else:
            repository_id = args.repository_id or _safe_task_id(f"qflow-pilot-{manifest['task_id']}").lower()
            display_name = args.display_name or f"qflow_pilot_{resource.display_name}_{manifest['task_id']}"
            published = client.create_copy(
                source=source,
                destination_project=destination_project,
                destination_repository_id=repository_id,
                display_name=display_name,
                content=content,
                author_name=args.author_name,
                author_email=args.account,
            )
    except DataformError as error:
        raise CliError(str(error)) from error
    if force_published and (not hasattr(client, "read_file") or not isinstance(published, dict) or not published.get("repository")):
        raise CliError("La publicación force requiere una lectura remota de comprobación")
    if hasattr(client, "read_file") and isinstance(published, dict) and published.get("repository"):
        try:
            saved = client.read_file(str(published["repository"]), filename)
        except DataformError as error:
            raise CliError(f"No se pudo verificar la copia creada: {error}") from error
        if hashlib.sha256(saved).hexdigest() != current_sha:
            raise CliError("La copia remota no coincide con el contenido aprobado")
    final_receipt = {
        "task_id": manifest["task_id"],
        "mode": mode,
        "exception_used": exception_used,
        "exception_digest": args.approved_exception_digest if exception_used else None,
        "force_published": force_published,
        "force_authorization": force_authorization,
        "audit": audit_receipt,
        "published": published,
        "destination_project": destination_project,
    }
    audit_store.record_publish(manifest["task_id"], final_receipt)
    manifest.update({"published": True, "destination": published, "audit_receipt": final_receipt})
    (task / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _print_result(final_receipt, args.json)
    return 0


def _migration_dictionary(args: argparse.Namespace):
    try:
        return load_dictionary(Path(args.dictionary).expanduser())
    except MigrationDictionaryError as error:
        raise CliError(str(error)) from error


def _cmd_migration_dictionary(args: argparse.Namespace) -> int:
    dictionary = _migration_dictionary(args)
    if args.dictionary_command == "validate":
        _print_result(validate_dictionary(Path(args.dictionary).expanduser()), args.json)
        return 0
    if args.dictionary_command == "render":
        rendered = render_markdown(dictionary)
        if args.output:
            output = Path(args.output).expanduser()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered, encoding="utf-8")
            _print_result({"ok": True, "path": str(output), "dictionary_sha256": dictionary.dictionary_sha256}, args.json)
        else:
            print(rendered, end="")
        return 0
    raise CliError(f"Comando migration dictionary no soportado: {args.dictionary_command}")


def _cmd_migration_rewrite(args: argparse.Namespace) -> int:
    dictionary = _migration_dictionary(args)
    task = Path(args.task).expanduser().resolve()
    try:
        report = rewrite_task(
            task,
            dictionary,
            apply=args.rewrite_command == "apply",
            expected_plan_digest=getattr(args, "plan_digest", None),
        )
    except MigrationDictionaryError as error:
        raise CliError(str(error)) from error
    _print_result(report.to_dict(), args.json)
    return 0


def _batch_selection(args: argparse.Namespace) -> BatchSelection:
    try:
        selection = BatchSelection.load(Path(args.selection_file).expanduser())
    except BatchError as error:
        raise CliError(str(error)) from error
    expected: dict[str, int] = {}
    if getattr(args, "expected_shared_queries", None) is not None:
        expected["shared_query"] = int(args.expected_shared_queries)
    if getattr(args, "expected_notebooks", None) is not None:
        expected["notebook"] = int(args.expected_notebooks)
    try:
        selection.validate_counts(expected or None)
    except BatchError as error:
        raise CliError(str(error)) from error
    for argument, value in (
        ("source_project", args.source_project),
        ("destination_project", args.destination_project),
        ("source_location", getattr(args, "source_location", None)),
        ("destination_location", getattr(args, "destination_location", None)),
    ):
        if value and value != getattr(selection, argument):
            raise CliError(f"--{argument.replace('_', '-')} no coincide con la selección del lote")
    if args.location and args.location not in {selection.source_location, selection.destination_location}:
        raise CliError("--location no coincide con la selección del lote")
    return selection


def _batch_catalog(args: argparse.Namespace, config: QueryflowConfig):
    path = Path(args.catalog).expanduser() if getattr(args, "catalog", None) else config.catalog_path
    try:
        return load_catalog(path), path
    except Exception as error:
        raise CliError(f"No se pudo leer el catálogo para el lote: {error}") from error


def _batch_manifest_path(config: QueryflowConfig, campaign_id: str, output: str | None = None) -> Path:
    return (
        Path(output).expanduser()
        if output
        else config.workspace_root.parent / "migrations" / campaign_id / "manifest.json"
    )


def _batch_client(args: argparse.Namespace, config: QueryflowConfig, selection: BatchSelection) -> DataformClient:
    account = getattr(args, "account", None) or config.account
    if not account:
        raise CliError("--account es obligatorio para leer o publicar un lote remoto")
    # Keep one resolved account for author metadata and the configured gcloud
    # context; never fall back to an inherited temporary credential profile.
    args.account = account
    try:
        requests_per_minute = int(getattr(args, "dataform_requests_per_minute", BATCH_REQUESTS_PER_MINUTE))
    except (TypeError, ValueError) as error:
        raise CliError("--dataform-requests-per-minute debe ser un entero") from error
    if requests_per_minute <= 0 or requests_per_minute > BATCH_QUOTA_REQUESTS_PER_MINUTE:
        raise CliError(f"--dataform-requests-per-minute debe estar entre 1 y {BATCH_QUOTA_REQUESTS_PER_MINUTE}")
    # For in-place updates the destination is intentionally also the
    # baseline project.  DataformClient's source-project write guard applies
    # to copy migrations only; policy and the update-mode checks below still
    # constrain writes to this exact allow-listed destination.
    client_source_project = "" if selection.operation == "update" else selection.source_project
    client = DataformClient(
        account,
        selection.destination_project,
        source_project=client_source_project,
        request_timeout_seconds=getattr(args, "request_timeout", 30.0),
        requests_per_minute=requests_per_minute,
        max_retries=DEFAULT_DATAFORM_MAX_RETRIES,
    )
    return _configure_dataform_client(client, config, account)


def _batch_write_review(manifest: Mapping[str, Any], manifest_path: Path, *, output: str | None = None) -> Path:
    target = Path(output).expanduser() if output else manifest_path.with_name("review.html")
    target.parent.mkdir(parents=True, exist_ok=True)
    links = {
        str(item.get("resource", {}).get("name")): str(item.get("review_relative"))
        for item in (manifest.get("resources") or [])
        if isinstance(item, Mapping) and item.get("review_relative")
    }
    target.write_text(render_batch_review(manifest, task_links=links), encoding="utf-8")
    return target


def _cmd_batch_inventory(args: argparse.Namespace) -> int:
    config = _config(args)
    selection = _batch_selection(args)
    dictionary = _migration_dictionary(args)
    try:
        requested_rate = int(args.dataform_requests_per_minute)
    except (TypeError, ValueError) as error:
        raise CliError("--dataform-requests-per-minute debe ser un entero") from error
    if requested_rate <= 0 or requested_rate > BATCH_QUOTA_REQUESTS_PER_MINUTE:
        raise CliError(f"--dataform-requests-per-minute debe estar entre 1 y {BATCH_QUOTA_REQUESTS_PER_MINUTE}")
    catalog, catalog_path = _batch_catalog(args, config)
    discarded_resources: list[dict[str, Any]] = []
    try:
        resources = resolve_batch_resources(selection, catalog.resources, discarded=discarded_resources)
    except BatchError as error:
        raise CliError(str(error)) from error
    requested_destination_names = {
        (spec.kind, normalize_display_name(spec.display_name))
        for spec in selection.resources
    }
    # Keep every Dataform repository in the destination region for ID/name
    # reconciliation.  Filtering this list to the requested kind hid a
    # cross-kind collision (for example an existing Shared Query occupying
    # the repository ID proposed for a notebook).  We still export content
    # only for same-kind names explicitly requested below.
    destination_resources = [
        item for item in catalog.resources
        if item.project == selection.destination_project
        and item.location == selection.destination_location
        and item.kind in BATCH_KINDS
    ]
    # An in-place reconciliation reads the destination resource itself as the
    # baseline.  It must not perform the copy workflow's second destination
    # export or attempt display-name collision resolution.
    if selection.operation == "update":
        destination_resources = []
    output_path = _batch_manifest_path(config, selection.campaign_id, args.output)
    if output_path.exists():
        raise CliError(f"Ya existe el manifest del lote: {output_path}; usa otro campaign_id o --output")
    checkpoint_path = output_path.with_name("inventory-checkpoint.json")
    assets: dict[str, Any] = {}
    client: DataformClient | None = None
    snapshots = Path(args.content_dir).expanduser() if args.content_dir else None
    errors: list[dict[str, Any]] = []
    if snapshots is None:
        client = _batch_client(args, config, selection)
    checkpoint_records: list[dict[str, Any]] = []

    def save_checkpoint() -> None:
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "status": "partial",
            "campaign_id": selection.campaign_id,
            "source_project": selection.source_project,
            "destination_project": selection.destination_project,
            "source_location": selection.source_location,
            "destination_location": selection.destination_location,
            "location": selection.destination_location,
            "dictionary_sha256": dictionary.dictionary_sha256,
            "processed": checkpoint_records,
            "error_count": len(errors),
            "dataform": client.request_stats.to_dict() if client is not None else {"requests_attempted": 0},
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        temporary = checkpoint_path.with_name(f".{checkpoint_path.name}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(checkpoint_path)

    for resource in resources:
        try:
            if snapshots is not None:
                snapshot = _local_snapshot_for(resource, snapshots)
                if snapshot is None:
                    raise BatchError(f"No se encontró snapshot local para {resource.display_name}")
                content, _snapshot_filename = snapshot
                # A private snapshot may be keyed by a hash; the remote
                # Dataform asset still uses its canonical content filename.
                filename = "content.ipynb" if resource.kind == "notebook" else "content.sql"
                assets[resource.name] = {"content": content, "filename": filename, "head_commit": resource.fingerprint}
            else:
                exported = client.export(resource)
                assets[resource.name] = exported
            checkpoint_records.append({"resource": resource.to_dict(), "status": "read"})
        except Exception as error:
            errors.append({"resource": resource.to_dict(), "error": redact_message(str(error))[:500]})
            checkpoint_records.append({"resource": resource.to_dict(), "status": "error", "error": errors[-1]["error"]})
        save_checkpoint()
    if errors:
        error_path = output_path.with_name("inventory-errors.json")
        error_path.parent.mkdir(parents=True, exist_ok=True)
        error_path.write_text(json.dumps({"status": "blocked", "errors": errors}, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        raise CliError(f"El inventario no pudo exportar {len(errors)} recurso(s); revisa {error_path}")
    destination_assets: dict[str, Any] = {}
    for destination_resource in destination_resources:
        if (
            destination_resource.kind,
            normalize_display_name(destination_resource.display_name),
        ) not in requested_destination_names:
            continue
        try:
            if snapshots is not None:
                destination_snapshot = _local_snapshot_for(destination_resource, snapshots)
                if destination_snapshot is None:
                    continue
                destination_content, _snapshot_filename = destination_snapshot
                destination_assets[destination_resource.name] = {
                    "content": destination_content,
                    "filename": "content.ipynb" if destination_resource.kind == "notebook" else "content.sql",
                    "head_commit": destination_resource.fingerprint,
                }
            else:
                destination_assets[destination_resource.name] = client.export(destination_resource)
        except Exception as error:
            errors.append({"resource": destination_resource.to_dict(), "error": redact_message(str(error))[:500]})
    if errors:
        error_path = output_path.with_name("inventory-errors.json")
        error_path.parent.mkdir(parents=True, exist_ok=True)
        error_path.write_text(json.dumps({"status": "blocked", "errors": errors}, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        raise CliError(f"El inventario no pudo exportar {len(errors)} recurso(s); revisa {error_path}")
    try:
        manifest = build_batch_manifest(
            selection,
            resources,
            assets,
            dictionary,
            destination_resources=destination_resources,
            destination_assets=destination_assets,
            discarded_resources=discarded_resources,
            catalog_generated_at=catalog.generated_at,
            requests_per_minute=requested_rate,
        )
    except BatchError as error:
        raise CliError(str(error)) from error
    manifest["inventory"] = {
        "catalog": str(catalog_path),
            "resources_resolved": len(resources),
            "resources_discarded": len(discarded_resources),
        "dataform": client.request_stats.to_dict() if client is not None else {"requests_attempted": 0},
        "checkpoint": str(checkpoint_path),
    }
    # Inventory metadata is informative and must not alter the approval hash.
    manifest["publication_digest"] = build_batch_digest(manifest)
    save_batch_manifest(manifest, output_path)
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        checkpoint["status"] = "complete"
        checkpoint["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        checkpoint_path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        # The manifest remains usable; a missing checkpoint is reported by
        # the caller rather than fabricating inventory state.
        pass
    report_json, report_md = write_batch_reports(manifest, output_path)
    review = _batch_write_review(manifest, output_path)
    blocked = manifest.get("status") == "blocked"
    _print_result(
        {
            "ok": not blocked,
            "campaign_id": selection.campaign_id,
            "manifest": str(output_path),
            "report_json": str(report_json),
            "report_markdown": str(report_md),
            "review": str(review),
            "checkpoint": str(checkpoint_path),
            "publication_digest": manifest.get("publication_digest"),
            "selection_count": len(resources),
            "warnings": len(manifest.get("warnings") or []),
            "review_required": sum(
                1 for item in manifest.get("resources", []) if (item.get("review") or {}).get("required")
            ),
            "destination_collisions": sum(1 for item in manifest.get("resources", []) if (item.get("destination") or {}).get("collision")),
            "write_enabled": False,
            "sql_executed": False,
        },
        args.json,
    )
    return 2 if blocked else 0


def _batch_static_validation(
    report: Any,
    *,
    classification: Mapping[str, Any] | None = None,
    review: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    static = dict(classification or {})
    static.setdefault("statement_class", "unknown")
    static.setdefault("read_only", False)
    static.setdefault("references", [])
    static.setdefault("errors", [])
    static.setdefault("warnings", [])
    static.setdefault("dynamic_cells", [])
    review_data = dict(review or {})
    return {
        "schema_version": 2,
        "status": "ready",
        "method": "migration_rewrite",
        "backend": "local",
        "ok": True,
        "publishable": False,
        "dry_run": {"skipped": True, "reason": "Migración de código: SQL y dry-run deshabilitados"},
        "static": static,
        "review": review_data,
        "errors": [],
        "content_sha256": report.proposed_sha256,
        "migration": report.to_dict(),
    }


def _cmd_batch_prepare(args: argparse.Namespace) -> int:
    config = _config(args)
    manifest_path = Path(args.manifest).expanduser()
    try:
        manifest = load_batch_manifest(manifest_path)
        dictionary = _migration_dictionary(args)
        selection_for_validation = BatchSelection.from_mapping(manifest.get("selection") or {})
        validate_batch_manifest(
            manifest,
            allow_blocked=True,
            allow_sealed_pending=selection_for_validation.secret_handling == "sealed_copy",
        )
    except (BatchError, MigrationDictionaryError) as error:
        raise CliError(str(error)) from error
    if manifest.get("dictionary_sha256") != dictionary.dictionary_sha256:
        raise CliError("El diccionario no coincide con el hash del lote")
    is_update = selection_for_validation.operation == "update"
    if is_update:
        if config.mode not in {"team", "full-access"} or not config.allow_update_existing:
            raise CliError("Preparar una actualización requiere mode team/full-access y allow_update_existing=true")
    elif config.profile_name not in {"migration-batch", "migration-pilot"}:
        raise CliError("La preparación requiere el perfil migration-batch (migration-pilot es alias temporal)")
    source_project = str(manifest["source_project"])
    destination_project = str(manifest["destination_project"])
    if not config.source_projects or source_project not in config.source_projects:
        raise CliError("El lote no coincide con la allowlist de proyectos origen configurada")
    if not config.destination_projects or destination_project not in config.destination_projects:
        raise CliError("El lote no coincide con la allowlist de proyectos destino configurada")
    selection = selection_for_validation
    client = _batch_client(args, config, selection)
    # Keep tasks beside the manifest so the consolidated Web Preview can serve
    # relative links without exposing files outside the campaign directory.
    campaign_root = manifest_path.parent
    execution = dict(manifest.get("execution") or {})
    links: dict[str, str] = {}
    prepared = 0
    for record in manifest.get("resources") or []:
        resource = ResourceRef.from_dict(dict(record["resource"]))
        skipped_statuses = {"already_present", "already_compliant", "destination_conflict", "blocked", "pending"}
        if record.get("status") in skipped_statuses:
            execution[resource.name] = {
                "status": str(record.get("status") or "pending"),
                "reason": (
                    "already_has_correct_routes"
                    if record.get("status") == "already_compliant"
                    else ("not_publishable_in_copy_only_mode" if not is_update else "not_ready_for_update")
                ),
                "repository": str((record.get("destination") or {}).get("repository") or ""),
            }
            continue
        existing = execution.get(resource.name) or {}
        if existing.get("status") == "prepared" and Path(str(existing.get("task") or "")).exists():
            # Keep the immutable review decision in ``record.review``. Older
            # manifests stored the HTML path under that key, so migrate that
            # presentation field without overwriting the risk contract.
            existing_copy = dict(existing)
            legacy_review_path = existing_copy.pop("review", None)
            if legacy_review_path and "review_file" not in existing_copy:
                existing_copy["review_file"] = legacy_review_path
            record.update(existing_copy)
            if existing.get("review_relative"):
                record["review_relative"] = existing["review_relative"]
                links[resource.name] = str(existing["review_relative"])
            prepared += 1
            continue
        exported = client.export(resource)
        source = record.get("source") or {}
        if source.get("head_commit") and exported.head_commit != source.get("head_commit"):
            raise CliError(f"El recurso remoto cambió después del inventario: {resource.display_name}")
        if hashlib.sha256(exported.content).hexdigest() != source.get("content_sha256"):
            raise CliError(f"El contenido remoto cambió después del inventario: {resource.display_name}")
        task_seed = f"{manifest['campaign_id']}-{resource.kind}-{int(record.get('ordinal', prepared + 1)):02d}"
        # Campaign IDs can exceed the workspace task-id limit; truncating
        # alone would make two notebooks share the same directory.  Keep a
        # short canonical-name hash so every resource has a stable unique task.
        task_id = _safe_task_id(
            task_seed[:58].rstrip("-.")
            + "-"
            + hashlib.sha256(resource.name.encode("utf-8")).hexdigest()[:16]
        )
        sealed = bool((record.get("security") or {}).get("sealed"))
        if sealed:
            # Keep only a redacted baseline/proposal in the durable task. The
            # exact source and rewritten bytes stay in memory for publication.
            rewrite_report = rewrite_asset(resource.kind, exported.filename, exported.content, dictionary)
            expected_proposed = str((record.get("rewrite") or {}).get("proposed_sha256") or "")
            if expected_proposed and rewrite_report.proposed_sha256 != expected_proposed:
                raise CliError(f"La reescritura no coincide con el inventario: {resource.display_name}")
            before = mask_sensitive_content(resource.kind, exported.content)
            after = mask_sensitive_content(resource.kind, rewrite_report.proposed_content)
            task = create_workspace(
                root=campaign_root,
                task_id=task_id,
                resource=resource,
                content=before,
                filename=exported.filename,
                mode="update" if is_update else "sealed_copy",
                account=args.account,
            )
            target = task / exported.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(after)
            update_manifest(
                task,
                secret_handling="sealed_copy",
                redacted=True,
                source_sha256=rewrite_report.before_sha256,
                proposed_sha256=rewrite_report.proposed_sha256,
            )
            (task / "rewrite-report.json").write_text(
                json.dumps(rewrite_report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            classification = record.get("classification") or classify_asset(resource.kind, exported.filename, rewrite_report.proposed_content)
        else:
            task = create_workspace(
                root=campaign_root,
                task_id=task_id,
                resource=resource,
                content=exported.content,
                filename=exported.filename,
                mode="update" if is_update else "copy",
                account=args.account,
            )
            if resource.kind == "notebook":
                write_cell_workspace(exported.content, task / "cells")
            rewrite_report = rewrite_task(task, dictionary, apply=True)
            expected_proposed = str((record.get("rewrite") or {}).get("proposed_sha256") or "")
            if expected_proposed and rewrite_report.proposed_sha256 != expected_proposed:
                raise CliError(f"La reescritura no coincide con el inventario: {resource.display_name}")
            before = exported.content
            after = preview_notebook_task(task) if resource.kind == "notebook" else (task / exported.filename).read_bytes()
            classification = classify_asset(resource.kind, exported.filename, after)
            expected_classification = record.get("classification") or {}
            if expected_classification and classification != expected_classification:
                raise CliError(f"La clasificación no coincide con el inventario: {resource.display_name}")
        validation = _batch_static_validation(
            rewrite_report,
            classification=classification,
            review=record.get("review") or {},
        )
        (task / "validation.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        review_path = write_review_html(
            task,
            before,
            after,
            validation,
            {
                "review_theme": config.review_theme,
                "review_mode": config.review_mode,
                "review_only_changes": config.review_only_changes,
                "review_context_lines": config.review_context_lines,
            },
        )
        mutable = {
            "status": "prepared",
            "task": str(task),
            "review_file": str(review_path),
            "review_relative": f"{task_id}/review.html",
            "changed_files": list(rewrite_report.changed_files),
            "proposed_sha256": rewrite_report.proposed_sha256,
            "sealed_copy": sealed,
            "redacted_review": sealed,
        }
        record.update(mutable)
        execution[resource.name] = mutable
        links[resource.name] = mutable["review_relative"]
        prepared += 1
        manifest["execution"] = execution
        manifest["status"] = "prepared"
        save_batch_manifest(manifest, manifest_path)
    manifest["execution"] = execution
    manifest["status"] = "prepared"
    save_batch_manifest(manifest, manifest_path)
    report_json, report_md = write_batch_reports(manifest, manifest_path)
    review = _batch_write_review(manifest, manifest_path)
    _print_result(
        {
            "ok": True,
            "campaign_id": manifest["campaign_id"],
            "prepared_count": prepared,
            "manifest": str(manifest_path),
            "report_json": str(report_json),
            "report_markdown": str(report_md),
            "review": str(review),
            "publication_digest": manifest["publication_digest"],
            "write_enabled": False,
            "sql_executed": False,
            "dataform": client.request_stats.to_dict(),
        },
        args.json,
    )
    return 0


def _dataform_not_found(error: Exception) -> bool:
    text = str(error).casefold()
    return any(token in text for token in ("404", "not found", "not_found", "no encontrado"))


def _cmd_batch_run(args: argparse.Namespace) -> int:
    config = _config(args)
    manifest_path = Path(args.manifest).expanduser()
    if args.execute_migration and not args.approved_digest:
        raise CliError("La publicación del lote requiere --approved-digest explícito")
    try:
        manifest = load_batch_manifest(manifest_path)
        dictionary = _migration_dictionary(args)
        run_selection = BatchSelection.from_mapping(manifest.get("selection") or {})
        sealed_records = [
            item
            for item in (manifest.get("resources") or [])
            if isinstance(item, Mapping) and bool((item.get("security") or {}).get("sealed"))
        ]
        validate_batch_manifest(
            manifest,
            approved_digest=args.approved_digest if args.execute_migration else None,
            allow_blocked=bool(args.execute_migration),
            allow_sealed_pending=bool(args.execute_migration and sealed_records),
        )
    except (BatchError, MigrationDictionaryError) as error:
        raise CliError(str(error)) from error
    is_update = run_selection.operation == "update"
    if manifest.get("dictionary_sha256") != dictionary.dictionary_sha256:
        raise CliError("El diccionario no coincide con el hash del lote")
    if not args.execute_migration:
        _print_result(
            {
                "ok": True,
                "campaign_id": manifest["campaign_id"],
                "status": manifest.get("status"),
                "selection_count": len(manifest.get("resources") or []),
                "publication_digest": manifest.get("publication_digest"),
                "sealed_publication_digest": manifest.get("sealed_publication_digest", ""),
                "write_enabled": False,
                "sql_executed": False,
                "message": "Plan cargado; publicar requiere --execute-migration y --approved-digest explícitos.",
            },
            args.json,
        )
        return 0
    if is_update:
        if config.mode not in {"team", "full-access"} or not config.allow_update_existing:
            raise CliError("La publicación de una actualización requiere mode team/full-access y allow_update_existing=true")
    elif config.profile_name not in {"migration-batch", "migration-pilot"}:
        raise CliError("La publicación requiere el perfil migration-batch (migration-pilot es alias temporal)")
    if sealed_records:
        if run_selection.secret_handling != "sealed_copy":
            raise CliError("El lote contiene secretos pero no está configurado como sealed_copy")
        if not str(getattr(args, "security_reference", "") or "").strip():
            raise CliError("La publicación sellada requiere --security-reference (ticket o autorización auditable)")
        approved_sealed = str(getattr(args, "approved_sealed_digest", "") or "").strip()
        expected_sealed = str(manifest.get("sealed_publication_digest") or "")
        if not approved_sealed or approved_sealed != expected_sealed:
            raise CliError("La publicación sellada requiere --approved-sealed-digest exacto e independiente")
    elif getattr(args, "approved_sealed_digest", None) or getattr(args, "security_reference", None):
        raise CliError("--approved-sealed-digest y --security-reference solo aplican a recursos sellados")
    decision = evaluate_policy(
        _modern_policy(config),
        operation="publish" if is_update else "campaign_publish",
        resource_kind="notebook" if is_update else "shared_query",
        mode="update" if is_update else "copy",
        source_project=manifest["source_project"],
        destination_project=manifest["destination_project"],
        location=str(manifest.get("destination_location") or manifest["location"]),
    )
    if not decision.allowed:
        raise CliError(decision.message)
    if config.audit_root is None and not args.audit_root:
        raise CliError("Configura audit_root antes de publicar el lote")
    selection = run_selection
    client = _batch_client(args, config, selection)
    records = manifest.get("resources") or []
    try:
        active_records, _published_records, pending_records = partition_batch_records(
            records, skip_pending=bool(getattr(args, "skip_pending", False))
        )
    except BatchError as error:
        raise CliError(str(error)) from error

    execution = dict(manifest.get("execution") or {})
    blocked_records = [
        record
        for record in active_records
        if str(record.get("status") or "") in {"blocked", "destination_conflict", "security_pending"}
    ]
    active_records = [
        record
        for record in active_records
        if str(record.get("status") or "") not in {"blocked", "destination_conflict", "security_pending"}
    ]
    for record in blocked_records:
        resource = ResourceRef.from_dict(dict(record["resource"]))
        execution[resource.name] = {
            "status": str(record.get("status") or "blocked"),
            "reason": "blocked_by_inventory_or_destination",
            "task": str(record.get("task") or ""),
        }
        manifest["execution"] = execution
    for record in active_records:
        resource = ResourceRef.from_dict(dict(record["resource"]))
        if not Path(str(record.get("task") or "")).is_dir():
            raise CliError(f"El recurso no tiene tarea preparada: {resource.display_name}; ejecuta batch prepare")
    # Validate every source head and destination collision before the first
    # write.  This makes a stale inventory a hard, pre-publication blocker.
    for record in active_records:
        resource = ResourceRef.from_dict(dict(record["resource"]))
        exported = client.export(resource)
        source = record.get("source") or {}
        if source.get("head_commit") and exported.head_commit != source.get("head_commit"):
            raise CliError(f"El origen cambió antes de publicar: {resource.display_name}")
        if hashlib.sha256(exported.content).hexdigest() != source.get("content_sha256"):
            raise CliError(f"El contenido de origen cambió antes de publicar: {resource.display_name}")
        if is_update:
            destination = record.get("destination") or {}
            if str(destination.get("repository") or "") != resource.name:
                raise CliError(f"La actualización no apunta al repositorio canónico: {resource.display_name}")
            continue
        destination = record.get("destination") or {}
        destination_location = str(manifest.get("destination_location") or manifest["location"])
        repo = str(destination.get("repository") or f"projects/{manifest['destination_project']}/locations/{destination_location}/repositories/{destination['repository_id']}")
        probe = ResourceRef(resource.kind, repo, manifest["destination_project"], destination_location, str(destination.get("display_name") or resource.display_name), "")
        try:
            client.get_repository(probe)
        except DataformError as error:
            if not _dataform_not_found(error):
                raise CliError(f"No se pudo comprobar el destino de {resource.display_name}: {error}") from error
        else:
            # The destination may have changed after inventory (including a
            # repository created by another operator).  Preserve the
            # approved content digest, but record this runtime blocker in the
            # audit manifest so the failed attempt is explainable and can be
            # reconciled in a follow-up selection.  ``error`` and
            # ``execution`` are intentionally outside the publication digest.
            message = f"Ya existe el repositorio destino para {resource.display_name}; no se sobrescribe"
            record["error"] = message
            execution[resource.name] = {
                "status": "destination_conflict",
                "reason": "destination_exists_at_preflight",
                "repository": repo,
                "task": str(record.get("task") or ""),
            }
            manifest["execution"] = execution
            manifest["status"] = "blocked"
            save_batch_manifest(manifest, manifest_path)
            raise CliError(message)
    audit_store = _audit_store(args.audit_root or config.audit_root, gcloud_context=_gcloud_context(config, args.account))
    for record in pending_records:
        resource = ResourceRef.from_dict(dict(record["resource"]))
        execution[resource.name] = {
            "status": "pending",
            "reason": "operator_deferred",
            "task": str(record.get("task") or ""),
        }
    published_count = 0
    failures: list[dict[str, Any]] = []
    for record in active_records:
        resource = ResourceRef.from_dict(dict(record["resource"]))
        task = Path(str(record.get("task") or ""))
        if not task.is_dir():
            raise CliError(f"El recurso no tiene tarea preparada: {resource.display_name}; ejecuta batch prepare")
        try:
            # Recheck the source immediately before this write to catch a race
            # occurring after the global preflight.
            latest = client.export(resource)
            source = record.get("source") or {}
            if source.get("head_commit") and latest.head_commit != source.get("head_commit"):
                raise BatchError("El origen cambió durante la publicación")
            if source.get("content_sha256") and hashlib.sha256(latest.content).hexdigest() != source.get("content_sha256"):
                raise BatchError("El contenido de origen cambió durante la publicación")
            filename = str(source.get("filename") or latest.filename)
            sealed = bool((record.get("security") or {}).get("sealed"))
            if sealed:
                # Recompute the proposal from the verified source in memory;
                # the prepared task contains only the redacted review copy.
                sealed_rewrite = rewrite_asset(resource.kind, filename, latest.content, dictionary)
                expected_before = str((record.get("rewrite") or {}).get("before_sha256") or "")
                expected = str((record.get("rewrite") or {}).get("proposed_sha256") or "")
                if expected_before and sealed_rewrite.before_sha256 != expected_before:
                    raise BatchError("El origen sellado no coincide con el digest del plan")
                if expected and sealed_rewrite.proposed_sha256 != expected:
                    raise BatchError("La tarea sellada no coincide con el digest del plan")
                content = sealed_rewrite.proposed_content
            else:
                content = preview_notebook_task(task) if resource.kind == "notebook" else (task / filename).read_bytes()
            expected = str((record.get("rewrite") or {}).get("proposed_sha256") or "")
            if not sealed and expected and hashlib.sha256(content).hexdigest() != expected:
                raise BatchError("La tarea preparada no coincide con el digest del plan")
            audit_receipt = audit_store.archive(task)
            destination = record["destination"]
            if is_update:
                published = client.update_file(
                    resource.name,
                    filename,
                    content,
                    required_head_commit=latest.head_commit,
                    author_name="QueryFlow",
                    author_email=args.account,
                )
            else:
                published = client.create_copy(
                    source=ExportedAsset(resource, filename, latest.content, latest.metadata, latest.head_commit),
                    destination_project=manifest["destination_project"],
                    destination_location=str(manifest.get("destination_location") or manifest["location"]),
                    destination_repository_id=str(destination["repository_id"]),
                    display_name=str(destination["display_name"]),
                    content=content,
                    author_name="QueryFlow migration batch",
                    author_email=args.account,
                    commit_message="QueryFlow migration batch copy",
                    labels={
                        "queryflow_review": "required" if (record.get("review") or {}).get("required") else "standard",
                        "queryflow_state": "sealed_pending_review" if sealed else "pending",
                        **({"queryflow_secret": "sealed"} if sealed else {}),
                    },
                )
            saved = client.read_file(str(published["repository"]), filename)
            if hashlib.sha256(saved).hexdigest() != hashlib.sha256(content).hexdigest():
                raise BatchError("La lectura posterior no coincide con el contenido aprobado")
            receipt = {
                "task_id": task.name,
                "published": published,
                "audit": audit_receipt,
                "classification": (record.get("classification") or {}).get("statement_class"),
                "review": record.get("review") or {},
                "secret_handling": "sealed_copy" if sealed else "block",
                "security_reference": str(getattr(args, "security_reference", "") or "") if sealed else "",
                "sql_executed": False,
            }
            audit_store.record_publish(task.name, receipt)
            record.update({"status": "published", "published": published, "receipt": receipt})
            execution[resource.name] = {"status": "published", **published, "task": str(task)}
            published_count += 1
        except Exception as error:
            failure = {"resource": resource.to_dict(), "error": redact_message(str(error))[:500]}
            failures.append(failure)
            record.update({"status": "blocked" if _systemic_campaign_error(error) else "failed", "error": failure["error"]})
            execution[resource.name] = {"status": record["status"], "error": failure["error"], "task": str(task)}
            if record["status"] == "blocked":
                manifest["execution"] = execution
                manifest["status"] = "blocked"
                save_batch_manifest(manifest, manifest_path)
                raise CliError(str(error)) from error
        manifest["execution"] = execution
        manifest["status"] = "running"
        save_batch_manifest(manifest, manifest_path)
    manifest["execution"] = execution
    if failures:
        manifest["status"] = "partial"
    elif pending_records:
        manifest["status"] = "published_with_pending"
    elif manifest.get("warnings"):
        manifest["status"] = "published_with_warnings"
    else:
        manifest["status"] = "completed"
    save_batch_manifest(manifest, manifest_path)
    report_json, report_md = write_batch_reports(manifest, manifest_path)
    _batch_write_review(manifest, manifest_path)
    _print_result(
        {
            "ok": not failures,
            "campaign_id": manifest["campaign_id"],
            "status": manifest["status"],
            "published_count": published_count,
            "pending_count": len(pending_records),
            "pending_resources": [
                str((record.get("resource") or {}).get("display_name") or "")
                for record in pending_records
            ],
            "failures": failures,
            "publication_digest": manifest["publication_digest"],
            "report_json": str(report_json),
            "report_markdown": str(report_md),
            "write_enabled": True,
            "sql_executed": False,
            "dataform": client.request_stats.to_dict(),
        },
        args.json,
    )
    return 0 if not failures else 2


def _cmd_batch_review(args: argparse.Namespace) -> int:
    try:
        manifest = load_batch_manifest(Path(args.manifest).expanduser())
    except BatchError as error:
        raise CliError(str(error)) from error
    path = _batch_write_review(manifest, Path(args.manifest).expanduser(), output=args.output)
    result = {"ok": True, "review": str(path), "campaign_id": manifest["campaign_id"], "read_only": True}
    _print_result(result, args.json)
    if args.serve:
        server, url = serve_batch_review(Path(args.manifest).expanduser(), args.port)
        print(f"Review disponible en {url}", flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
    return 0


def _local_snapshot_for(resource: ResourceRef, directory: Path) -> tuple[bytes, str] | None:
    """Resolve a private local snapshot without exposing its filename in a manifest."""
    key = hashlib.sha256(resource.name.encode("utf-8")).hexdigest()
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", resource.display_name).strip("-")
    candidates = [
        directory / key,
        directory / f"{key}.sql",
        directory / f"{key}.ipynb",
        directory / f"{key}.txt",
        directory / safe_name,
        directory / f"{safe_name}.sql",
        directory / f"{safe_name}.ipynb",
    ]
    for path in candidates:
        if path.exists() and path.is_file():
            return path.read_bytes(), path.name
    return None


def _limit_pilot_resource_pool(
    resources: list[ResourceRef],
    *,
    seed: str,
    limit: int | None,
) -> list[ResourceRef]:
    """Choose a deterministic, de-duplicated candidate pool per resource kind."""
    if limit is not None and limit <= 0:
        raise CliError("--max-resources-per-kind debe ser mayor que cero")
    by_name = {resource.name: resource for resource in resources if resource.kind in {"notebook", "shared_query"}}
    selected_by_kind: dict[str, list[ResourceRef]] = {}
    for kind in ("shared_query", "notebook"):
        values = sorted((resource for resource in by_name.values() if resource.kind == kind), key=lambda item: item.name)
        randomizer = random.Random(int(hashlib.sha256(f"{seed}:{kind}".encode("utf-8")).hexdigest()[:16], 16))
        randomizer.shuffle(values)
        selected_by_kind[kind] = values if limit is None else values[:limit]
    selected: list[ResourceRef] = []
    max_count = max((len(values) for values in selected_by_kind.values()), default=0)
    for index in range(max_count):
        for kind in ("shared_query", "notebook"):
            values = selected_by_kind[kind]
            if index < len(values):
                selected.append(values[index])
    return selected


DEFAULT_DATAFORM_REQUESTS_PER_MINUTE = 180
MAX_DATAFORM_REQUESTS_PER_MINUTE = 300
DEFAULT_DATAFORM_MAX_RETRIES = 5
DATAFORM_QUOTA_REGION = "us-east1"


def _dataform_rate_limit(args: argparse.Namespace) -> int:
    """Validate the pilot client limit against the project quota."""
    try:
        value = int(getattr(args, "dataform_requests_per_minute", DEFAULT_DATAFORM_REQUESTS_PER_MINUTE))
    except (TypeError, ValueError) as error:
        raise CliError("--dataform-requests-per-minute debe ser un entero") from error
    if value <= 0 or value > MAX_DATAFORM_REQUESTS_PER_MINUTE:
        raise CliError(
            f"--dataform-requests-per-minute debe estar entre 1 y {MAX_DATAFORM_REQUESTS_PER_MINUTE}"
        )
    return value


def _dataform_policy(args: argparse.Namespace) -> dict[str, Any]:
    """Return the quota policy recorded in pilot artifacts, without secrets."""
    return {
        "region": DATAFORM_QUOTA_REGION,
        "quota_requests_per_minute": MAX_DATAFORM_REQUESTS_PER_MINUTE,
        "client_limit_requests_per_minute": _dataform_rate_limit(args),
        "max_retries": DEFAULT_DATAFORM_MAX_RETRIES,
        "backoff_seconds": [5, 10, 20, 40, 60],
    }


def _campaign_inventory_checkpoint_path(output_path: Path) -> Path:
    return output_path.with_name("inventory-checkpoint.json")


def _merge_dataform_stats(previous: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    integer_fields = ("requests_attempted", "requests_succeeded", "retries", "rate_limit_responses")
    merged = {
        field: int(previous.get(field, 0) or 0) + int(current.get(field, 0) or 0)
        for field in integer_fields
    }
    merged["rate_limit_wait_seconds"] = round(
        float(previous.get("rate_limit_wait_seconds", 0.0) or 0.0)
        + float(current.get("rate_limit_wait_seconds", 0.0) or 0.0),
        3,
    )
    return merged


def _campaign_manifest_path(config: QueryflowConfig, campaign_id: str) -> Path:
    return config.workspace_root.parent / "migrations" / campaign_id / "manifest.json"


def _write_campaign_incident_report(manifest: PilotManifest, manifest_path: Path) -> Path:
    path = manifest_path.with_name("route-incidents.json")
    path.write_text(json.dumps(make_incident_report(manifest), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _write_campaign_inventory_incident_report(
    selections: list[Any],
    manifest_path: Path,
    *,
    campaign_id: str,
) -> Path:
    """Persist route evidence even when strict quotas block a manifest."""
    path = manifest_path.with_name("route-incidents.json")
    payload = make_inventory_incident_report(selections, campaign_id=campaign_id)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _write_campaign_inventory_error_report(
    errors: list[dict[str, Any]],
    manifest_path: Path,
    *,
    dataform_policy: Mapping[str, Any] | None = None,
    dataform_stats: Mapping[str, Any] | None = None,
) -> Path | None:
    if not errors:
        return None
    path = manifest_path.with_name("inventory-errors.json")
    payload = {
        "schema_version": 1,
        "status": "partial_inventory",
        "error_count": len(errors),
        "errors": errors,
        "dataform_policy": dict(dataform_policy or {}),
        "dataform_stats": dict(dataform_stats or {}),
        "policy": "Los recursos con error se omiten de la selección; no se fabrican hashes ni clasificaciones.",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _write_campaign_inventory_shortfall_report(
    manifest_path: Path,
    *,
    source_project: str,
    destination_project: str,
    seed: str,
    candidate_pool_count: int,
    resources_read: int,
    category_counts: Mapping[str, Mapping[str, int]],
    inventory_errors: list[dict[str, Any]],
    dataform_policy: Mapping[str, Any] | None = None,
    dataform_stats: Mapping[str, Any] | None = None,
) -> Path:
    required = PilotQuotas().to_dict()
    available = {
        kind: {category: int(values.get(category, 0)) for category in required}
        for kind, values in category_counts.items()
    }
    missing = {
        kind: {category: max(required[category] - values.get(category, 0), 0) for category in required}
        for kind, values in available.items()
    }
    path = manifest_path.with_name("inventory-shortfall.json")
    payload = {
        "schema_version": 1,
        "status": "quota_shortfall",
        "source_project": source_project,
        "destination_project": destination_project,
        "seed": seed,
        "candidate_pool_count": candidate_pool_count,
        "resources_read": resources_read,
        "required_per_kind": required,
        "available_per_kind": available,
        "missing_per_kind": missing,
        "inventory_errors": inventory_errors,
        "dataform_policy": dict(dataform_policy or {}),
        "dataform_stats": dict(dataform_stats or {}),
        "policy": "No se crea manifest ni se publica mientras falte un estrato; amplía el pool o revisa el diccionario.",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _cmd_pilot_inventory(args: argparse.Namespace) -> int:
    config = _config(args)
    dictionary = _migration_dictionary(args)
    catalog_path = Path(args.catalog).expanduser() if args.catalog else config.catalog_path
    try:
        catalog = load_catalog(catalog_path)
    except Exception as error:
        raise CliError(f"No se pudo leer el catálogo: {error}") from error
    source_project = _resolve_config_project(config, args.source_project) or args.source_project
    destination_project = _resolve_config_project(config, args.destination_project) or args.destination_project
    if not source_project or not destination_project:
        raise CliError("El inventario requiere --source-project y --destination-project")
    destination_names = [
        resource.display_name
        for resource in catalog.resources
        if resource.project == destination_project and resource.kind in {"notebook", "shared_query"}
    ]
    destination_name_set = {value.strip().casefold() for value in destination_names if value.strip()}
    resources = [
        resource
        for resource in catalog.resources
        if resource.project == source_project and resource.kind in {"notebook", "shared_query"}
        and (not resource.display_name.strip() or resource.display_name.strip().casefold() not in destination_name_set)
    ]
    pool_seed = args.seed or dictionary.dictionary_sha256[:16]
    resources = _limit_pilot_resource_pool(
        resources,
        seed=pool_seed,
        limit=args.max_resources_per_kind,
    )
    requested_output = Path(args.output).expanduser() if args.output else None
    checkpoint_manifest_path = requested_output or (
        config.workspace_root.parent / "migrations" / f"inventory-{pool_seed}" / "manifest.json"
    )
    checkpoint_path = _campaign_inventory_checkpoint_path(checkpoint_manifest_path)
    resume_inventory = bool(getattr(args, "resume_inventory", False))
    if resume_inventory and not checkpoint_path.exists():
        raise CliError(f"No existe el checkpoint de inventario: {checkpoint_path}")
    if not resume_inventory and checkpoint_path.exists():
        raise CliError(
            f"Ya existe un checkpoint de inventario en {checkpoint_path}; "
            "usa --resume-inventory o cambia --output"
        )
    # Asset Inventory fingerprints are etags/update timestamps, while the
    # publication conflict check needs the Dataform commit SHA.  Replace the
    # catalog fingerprint with that read-only Dataform head whenever remote
    # inventory is used; local snapshots intentionally retain their catalog
    # fingerprint and are for planning/classification only.
    inventory_resources: dict[str, ResourceRef] = {resource.name: resource for resource in resources}
    checkpoint: InventoryCheckpoint | None = None
    previous_stats: Mapping[str, Any] = {}
    if resume_inventory:
        try:
            checkpoint = load_inventory_checkpoint(checkpoint_path)
        except CampaignError as error:
            raise CliError(str(error)) from error
        expected_context = {
            "source_project": source_project,
            "destination_project": destination_project,
            "dictionary_sha256": dictionary.dictionary_sha256,
            "seed": pool_seed,
            "catalog_generated_at": catalog.generated_at,
        }
        actual_context = {
            "source_project": checkpoint.source_project,
            "destination_project": checkpoint.destination_project,
            "dictionary_sha256": checkpoint.dictionary_sha256,
            "seed": checkpoint.seed,
            "catalog_generated_at": checkpoint.catalog_generated_at,
        }
        if actual_context != expected_context:
            raise CliError("El checkpoint no coincide con el catálogo, diccionario, semilla o proyectos actuales")
        pool_names = set(inventory_resources)
        if any(item.resource.name not in pool_names for item in checkpoint.selections):
            raise CliError("El checkpoint contiene recursos fuera del pool actual; genera un inventario nuevo")
        inspected_selections = list(checkpoint.selections)
        previous_stats = checkpoint.stats.get("dataform", checkpoint.stats) if isinstance(checkpoint.stats, Mapping) else {}
        previous_errors = {
            str(item["resource"]["name"]): dict(item)
            for item in checkpoint.inventory_errors
            if isinstance(item.get("resource"), Mapping) and item["resource"].get("name")
        }
    else:
        inspected_selections = []
        previous_errors = {}
    selected_names = {item.resource.name for item in inspected_selections}
    inventory_errors_by_name = {name: item for name, item in previous_errors.items() if name not in selected_names}
    contents: dict[str, bytes] = {}
    filenames: dict[str, str] = {}
    category_counts: dict[str, dict[str, int]] = {
        kind: {category: 0 for category in PilotQuotas().to_dict()}
        for kind in ("shared_query", "notebook")
    }
    for item in inspected_selections:
        if item.resource.kind in category_counts and item.category in category_counts[item.resource.kind]:
            category_counts[item.resource.kind][item.category] += 1
    resources_read = len(inspected_selections)
    snapshots = Path(args.content_dir).expanduser() if args.content_dir else None
    client = None
    dataform_policy = _dataform_policy(args)
    if snapshots is None:
        if not args.account:
            raise CliError("--account es obligatorio cuando inventory lee recursos remotos")
        requests_per_minute = dataform_policy["client_limit_requests_per_minute"]
        client = _configure_dataform_client(
            DataformClient(
                args.account,
                destination_project,
                source_project=source_project,
                request_timeout_seconds=args.request_timeout,
                requests_per_minute=requests_per_minute,
                max_retries=DEFAULT_DATAFORM_MAX_RETRIES,
            ),
            config,
            args.account,
        )
    def current_errors() -> list[dict[str, Any]]:
        return [inventory_errors_by_name[name] for name in sorted(inventory_errors_by_name)]

    def checkpoint_stats() -> dict[str, Any]:
        current = client.request_stats.to_dict() if client is not None and hasattr(client, "request_stats") else {}
        dataform_stats = _merge_dataform_stats(previous_stats, current)
        return {
            "dataform": dataform_stats,
            "dataform_policy": dataform_policy,
            "candidate_pool_count": len(resources),
            "resources_read": resources_read,
        }

    def persist_checkpoint(status: str) -> None:
        save_inventory_checkpoint(
            InventoryCheckpoint(
                source_project=source_project,
                destination_project=destination_project,
                dictionary_sha256=dictionary.dictionary_sha256,
                seed=pool_seed,
                catalog_generated_at=catalog.generated_at,
                selections=tuple(inspected_selections),
                inventory_errors=tuple(current_errors()),
                stats=checkpoint_stats(),
                status=status,
                updated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            ),
            checkpoint_path,
        )

    retry_names = set(inventory_errors_by_name)
    retry_resources = [resource for resource in resources if resource.name in retry_names]
    fresh_resources = [
        resource
        for resource in resources
        if resource.name not in retry_names and resource.name not in selected_names
    ]
    ordered_resources = retry_resources + fresh_resources
    quota_exhausted = False
    for resource in ordered_resources:
        if resource.name in selected_names:
            continue
        try:
            if snapshots is not None:
                snapshot = _local_snapshot_for(resource, snapshots)
                if snapshot is None:
                    raise CampaignError("No se encontró snapshot local para el recurso")
                content, filename = snapshot
            else:
                exported = client.export(resource)
                content, filename = exported.content, exported.filename
                inventory_resources[resource.name] = replace(resource, fingerprint=exported.head_commit)
            contents[resource.name] = content
            filenames[resource.name] = filename
            inspected = _selection(inventory_resources[resource.name], content, dictionary, filename)
            inspected_selections.append(inspected)
            category_counts[resource.kind][inspected.category] += 1
            resources_read += 1
            selected_names.add(resource.name)
            inventory_errors_by_name.pop(resource.name, None)
            persist_checkpoint("partial")
        except Exception as error:
            # An inventory is read-only; leave the resource out and report the
            # problem instead of fabricating a classification.
            record = {
                "resource": resource.to_dict(),
                "error": redact_message(str(error))[:500],
            }
            if isinstance(error, DataformRateLimitError):
                record["error_kind"] = "quota"
                quota_exhausted = True
            inventory_errors_by_name[resource.name] = record
            persist_checkpoint("rate_limited" if quota_exhausted else "partial")
            if quota_exhausted:
                break
            continue
        if all(
            category_counts[kind][category] >= quota
            for kind in ("shared_query", "notebook")
            for category, quota in PilotQuotas().to_dict().items()
        ):
            persist_checkpoint("quota_reached")
            break
    inventory_errors = current_errors()
    try:
        manifest = select_classified(
            inspected_selections,
            source_project=source_project,
            destination_project=destination_project,
            dictionary=dictionary,
            seed=pool_seed,
            campaign_id=args.campaign_id,
            catalog_generated_at=catalog.generated_at,
        )
    except CampaignError as error:
        # Preserve fetch diagnostics even when the available resources cannot
        # satisfy the 5/3/2 quotas and no manifest can be created yet.
        report_base = requested_output or checkpoint_manifest_path
        report_base.parent.mkdir(parents=True, exist_ok=True)
        provisional_manifest = build_manifest(
            inspected_selections,
            source_project=source_project,
            destination_project=destination_project,
            dictionary=dictionary,
            seed=pool_seed,
            campaign_id=args.campaign_id,
            catalog_generated_at=catalog.generated_at,
        )
        incidents_path = _write_campaign_inventory_incident_report(
            inspected_selections,
            report_base,
            campaign_id=provisional_manifest.campaign_id,
        )
        errors_path = _write_campaign_inventory_error_report(
            inventory_errors,
            report_base,
            dataform_policy=dataform_policy,
            dataform_stats=checkpoint_stats().get("dataform", {}),
        )
        shortfall_path = _write_campaign_inventory_shortfall_report(
            report_base,
            source_project=source_project,
            destination_project=destination_project,
            seed=pool_seed,
            candidate_pool_count=len(resources),
            resources_read=resources_read,
            category_counts=category_counts,
            inventory_errors=inventory_errors,
            dataform_policy=dataform_policy,
            dataform_stats=checkpoint_stats().get("dataform", {}),
        )
        details = f"{error}. Reporte de cupo: {shortfall_path}; incidentes de rutas: {incidents_path}; checkpoint: {checkpoint_path}"
        if errors_path:
            details += f"; errores de lectura: {errors_path}"
        raise CliError(details) from error
    # Keep filename snapshots private to the local inventory directory; the
    # manifest stores only content hashes and classification evidence.
    path = requested_output or _campaign_manifest_path(config, manifest.campaign_id)
    save_manifest(manifest, path)
    persist_checkpoint("complete")
    incidents_path = _write_campaign_incident_report(manifest, path)
    inventory_errors_path = _write_campaign_inventory_error_report(
        inventory_errors,
        path,
        dataform_policy=dataform_policy,
        dataform_stats=checkpoint_stats().get("dataform", {}),
    )
    _print_result(
        {
            "ok": True,
            "manifest": str(path),
            "campaign_id": manifest.campaign_id,
            "source_project": source_project,
            "destination_project": destination_project,
            "seed": manifest.seed,
            "selection_count": len(manifest.selections),
            "kind_quotas": {kind: quota.to_dict() for kind, quota in manifest.kind_quotas.items()},
            "candidate_pool_count": len(resources),
            "resources_read": resources_read,
            "category_counts": category_counts,
            "write_enabled": False,
            "route_incidents": str(incidents_path),
            "inventory_errors": str(inventory_errors_path) if inventory_errors_path else None,
            "inventory_error_count": len(inventory_errors),
            "dataform": checkpoint_stats().get("dataform", {}),
            "dataform_policy": dataform_policy,
            "checkpoint": str(checkpoint_path),
        },
        args.json,
    )
    return 0


def _systemic_campaign_error(error: Exception) -> bool:
    text = str(error).casefold()
    return any(token in text for token in ("vpc", "service controls", "permission", "unauthenticated", "authentication", "credential", "network", "transport"))


def _cmd_pilot_prepare(args: argparse.Namespace) -> int:
    config = _config(args)
    try:
        manifest_path = Path(args.manifest).expanduser()
        manifest = load_manifest(manifest_path)
        dictionary = _migration_dictionary(args)
        validate_pilot_manifest(manifest)
    except (CampaignError, MigrationDictionaryError) as error:
        raise CliError(str(error)) from error
    if manifest.dictionary_sha256 != dictionary.dictionary_sha256:
        raise CliError("El diccionario no coincide con el hash de la campaña")
    if config.profile_name != "migration-pilot":
        raise CliError("La preparación de campaña requiere el perfil migration-pilot")
    if not config.source_projects or manifest.source_project not in config.source_projects:
        raise CliError("La campaña no coincide con la allowlist de proyectos origen configurada")
    if not config.destination_projects or manifest.destination_project not in config.destination_projects:
        raise CliError("La campaña no coincide con la allowlist de proyectos destino configurada")
    if not args.account:
        raise CliError("--account es obligatorio para preparar la campaña")
    dataform_policy = _dataform_policy(args)
    client = _configure_dataform_client(
        DataformClient(
            args.account,
            manifest.destination_project,
            source_project=manifest.source_project,
            request_timeout_seconds=args.request_timeout,
            requests_per_minute=dataform_policy["client_limit_requests_per_minute"],
            max_retries=DEFAULT_DATAFORM_MAX_RETRIES,
        ),
        config,
        args.account,
    )
    campaign_root = config.workspace_root / "migration" / manifest.campaign_id
    raw_manifest = manifest.to_dict()
    execution = dict(raw_manifest.get("execution") or {})
    prepared_count = 0
    preferences = {
        "review_theme": config.review_theme,
        "review_mode": config.review_mode,
        "review_only_changes": config.review_only_changes,
        "review_context_lines": config.review_context_lines,
    }
    task_links: dict[str, str] = {}
    for index, selection in enumerate(manifest.selections):
        task_id = _safe_task_id(f"{manifest.campaign_id}-{selection.resource.kind}-{index:02d}")
        existing = execution.get(selection.resource.name) or {}
        existing_task = Path(str(existing.get("task") or "")) if existing.get("task") else None
        existing_review = Path(str(existing.get("review") or "")) if existing.get("review") else None
        if existing.get("status") == "prepared" and existing_task and existing_task.exists() and existing_review and existing_review.exists():
            prepared_count += 1
            task_links[selection.resource.name] = existing.get("review_relative", f"{task_id}/review.html")
            continue
        exported = client.export(selection.resource)
        if exported.head_commit != selection.resource.fingerprint or hashlib.sha256(exported.content).hexdigest() != selection.content_sha256:
            raise CliError(f"El recurso remoto cambió después del inventario: {selection.resource.name}")
        task = create_workspace(
            root=campaign_root,
            task_id=task_id,
            resource=selection.resource,
            content=exported.content,
            filename=exported.filename,
            mode="copy",
            account=args.account,
        )
        if selection.resource.kind == "notebook":
            write_cell_workspace(exported.content, task / "cells")
        report = rewrite_task(task, dictionary, apply=True)
        validation = {
            "schema_version": 1,
            "status": "ready",
            "method": "migration_rewrite",
            "backend": "local",
            "ok": True,
            "publishable": False,
            "dry_run": {"skipped": True, "reason": "El piloto de migración no ejecuta SQL"},
            "static": {"read_only": True, "statement_class": "not_evaluated", "errors": [], "warnings": []},
            "errors": [],
            "content_sha256": report.proposed_sha256,
            "migration": report.to_dict(),
        }
        update_manifest(
            task,
            validation_status="ready",
            workflow_state="ready",
            proposed_sha256=report.proposed_sha256,
            migration_plan_digest=report.plan_digest,
            migration_status="published_with_incidents" if report.unknown_routes else "rewritten",
        )
        (task / "validation.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        review_path = write_review_html(
            task,
            exported.content,
            (task / exported.filename).read_bytes(),
            validation,
            preferences,
        )
        execution[selection.resource.name] = {
            "status": "prepared",
            "task": str(task),
            "review": str(review_path),
            "review_relative": f"{task_id}/review.html",
            "changed_files": list(report.changed_files),
            "unknown_route_count": len(report.unknown_routes),
            "proposed_sha256": report.proposed_sha256,
        }
        task_links[selection.resource.name] = f"{task_id}/review.html"
        prepared_count += 1
    raw_manifest["execution"] = execution
    raw_manifest["status"] = "prepared"
    prepared_manifest = PilotManifest.from_dict(raw_manifest)
    save_manifest(prepared_manifest, manifest_path)
    campaign_review = campaign_root / "review.html"
    campaign_review.parent.mkdir(parents=True, exist_ok=True)
    campaign_review.write_text(render_campaign_review(prepared_manifest, task_links=task_links), encoding="utf-8")
    _print_result(
        {
            "ok": True,
            "campaign_id": manifest.campaign_id,
            "prepared_count": prepared_count,
            "campaign_review": str(campaign_review),
            "publication_digest": campaign_publish_digest(prepared_manifest),
            "write_enabled": False,
            "dataform": client.request_stats.to_dict(),
            "dataform_policy": dataform_policy,
        },
        args.json,
    )
    return 0


def _cmd_pilot_run(args: argparse.Namespace) -> int:
    config = _config(args)
    try:
        manifest = load_manifest(Path(args.manifest).expanduser())
        dictionary = _migration_dictionary(args)
    except (CampaignError, MigrationDictionaryError) as error:
        raise CliError(str(error)) from error
    if manifest.dictionary_sha256 != dictionary.dictionary_sha256:
        raise CliError("El diccionario no coincide con el hash de la campaña")
    try:
        validate_pilot_manifest(manifest)
    except CampaignError as error:
        raise CliError(str(error)) from error
    dataform_policy = _dataform_policy(args)
    if not args.execute_migration:
        _print_result(
            {
                "ok": True,
                "campaign_id": manifest.campaign_id,
                "status": manifest.status,
                "selection_count": len(manifest.selections),
                "publication_digest": campaign_publish_digest(manifest),
                "write_enabled": False,
                "dataform_policy": dataform_policy,
                "message": "Plan cargado; la publicación requiere --execute-migration y --approved-digest explícitos.",
            },
            args.json,
        )
        return 0
    if not migration_publish_allowed(config.profile_name, execute_migration=True):
        raise CliError("La ejecución del piloto requiere el perfil migration-pilot y --execute-migration")
    expected_digest = campaign_publish_digest(manifest)
    if args.approved_digest != expected_digest:
        raise CliError("El digest de campaña no coincide; usa --approved-digest con el publication_digest exacto del plan")
    if not config.source_projects or manifest.source_project not in config.source_projects:
        raise CliError("La campaña no coincide con la allowlist de proyectos origen configurada")
    if not config.destination_projects or manifest.destination_project not in config.destination_projects:
        raise CliError("La campaña no coincide con la allowlist de proyectos destino configurada")
    decision = evaluate_policy(
        _modern_policy(config),
        operation="campaign_publish",
        resource_kind="shared_query",
        mode="copy",
        source_project=manifest.source_project,
        destination_project=manifest.destination_project,
    )
    if not decision.allowed:
        raise CliError(decision.message)
    if not args.account:
        raise CliError("--account es obligatorio para ejecutar la campaña")
    if config.audit_root is None and not args.audit_root:
        raise CliError("Configura audit_root antes de ejecutar la campaña")
    client = _configure_dataform_client(
        DataformClient(
            args.account,
            manifest.destination_project,
            source_project=manifest.source_project,
            request_timeout_seconds=getattr(args, "request_timeout", 30.0),
            requests_per_minute=dataform_policy["client_limit_requests_per_minute"],
            max_retries=DEFAULT_DATAFORM_MAX_RETRIES,
        ),
        config,
        args.account,
    )
    campaign_root = config.workspace_root / "migration" / manifest.campaign_id
    raw_manifest = manifest.to_dict()
    execution = dict(raw_manifest.get("execution") or {})
    cleanup_records = list((raw_manifest.get("cleanup") or {}).get("records") or [])
    for index, selection in enumerate(manifest.selections):
        key = selection.resource.name
        if execution.get(key, {}).get("status") == "published":
            continue
        task_id = _safe_task_id(f"{manifest.campaign_id}-{selection.resource.kind}-{index:02d}")
        record: dict[str, Any] = {"status": "pending", "task_id": task_id, "resource": selection.resource.to_dict()}
        try:
            exported = client.export(selection.resource)
            if exported.head_commit != selection.resource.fingerprint or hashlib.sha256(exported.content).hexdigest() != selection.content_sha256:
                raise CampaignError("El recurso remoto cambió después del inventario")
            task = create_workspace(
                root=campaign_root,
                task_id=task_id,
                resource=selection.resource,
                content=exported.content,
                filename=exported.filename,
                mode="copy",
                account=args.account,
            )
            if selection.resource.kind == "notebook":
                write_cell_workspace(exported.content, task / "cells")
            report = rewrite_task(task, dictionary, apply=True)
            if config.audit_root is None and not args.audit_root:
                raise CampaignError("audit_root no configurado")
            audit_store = _audit_store(args.audit_root or config.audit_root, gcloud_context=_gcloud_context(config, args.account))
            audit_receipt = audit_store.archive(task)
            published = client.create_copy(
                source=ExportedAsset(selection.resource, exported.filename, exported.content, exported.metadata, exported.head_commit),
                destination_project=manifest.destination_project,
                destination_repository_id=_safe_task_id(f"qflow-mig-{manifest.campaign_id[-10:]}-{index:02d}").lower(),
                display_name=f"{selection.resource.display_name}{manifest.suffix}",
                content=(task / exported.filename).read_bytes(),
                author_name="QueryFlow migration pilot",
                author_email=args.account,
            )
            # Register the repository immediately after the create call.  If
            # read-back/audit fails, cleanup-plan can still present the exact
            # copy for a separately approved cleanup instead of leaking it.
            cleanup_records.append({"repository": published.get("repository"), "commit_sha": published.get("commit_sha"), "task_id": task_id})
            saved = client.read_file(str(published["repository"]), exported.filename)
            if hashlib.sha256(saved).hexdigest() != report.proposed_sha256:
                raise CampaignError("La lectura posterior no coincide con la reescritura")
            receipt = {"task_id": task_id, "published": published, "audit": audit_receipt, "rewrite": report.to_dict()}
            audit_store.record_publish(task_id, receipt)
            record.update({"status": "published", "repository": published.get("repository"), "commit_sha": published.get("commit_sha"), "receipt": receipt})
        except Exception as error:
            record.update({"status": "blocked" if _systemic_campaign_error(error) else "failed", "error": redact_message(str(error))[:500]})
            execution[key] = record
            raw_manifest["execution"] = execution
            raw_manifest["cleanup"] = {"records": cleanup_records, "created_repositories": [item.get("repository") for item in cleanup_records if item.get("repository")]}
            raw_manifest["status"] = "blocked" if record["status"] == "blocked" else "partial"
            save_manifest(PilotManifest.from_dict(raw_manifest), Path(args.manifest).expanduser())
            if record["status"] == "blocked":
                raise CliError(str(error)) from error
            continue
        execution[key] = record
        raw_manifest["execution"] = execution
        raw_manifest["cleanup"] = {"records": cleanup_records, "created_repositories": [item.get("repository") for item in cleanup_records if item.get("repository")]}
        raw_manifest["status"] = "running"
        save_manifest(PilotManifest.from_dict(raw_manifest), Path(args.manifest).expanduser())
    all_published = all(item.get("status") == "published" for item in execution.values())
    has_incidents = any(item.unknown_routes for item in manifest.selections)
    raw_manifest["status"] = "published_with_incidents" if all_published and has_incidents else "completed" if all_published else "partial"
    save_manifest(PilotManifest.from_dict(raw_manifest), Path(args.manifest).expanduser())
    final_manifest = PilotManifest.from_dict(raw_manifest)
    incidents_path = _write_campaign_incident_report(final_manifest, Path(args.manifest).expanduser())
    _print_result(
        {
            "ok": raw_manifest["status"] in {"completed", "published_with_incidents"},
            "campaign_id": manifest.campaign_id,
            "status": raw_manifest["status"],
            "execution": execution,
            "write_enabled": True,
            "route_incidents": str(incidents_path),
            "dataform": client.request_stats.to_dict(),
            "dataform_policy": dataform_policy,
        },
        args.json,
    )
    return 0 if raw_manifest["status"] in {"completed", "published_with_incidents"} else 2


def _cmd_pilot_review(args: argparse.Namespace) -> int:
    try:
        manifest = load_manifest(Path(args.manifest).expanduser())
    except CampaignError as error:
        raise CliError(str(error)) from error
    output = Path(args.output).expanduser() if args.output else Path(args.manifest).expanduser().with_name("review.html")
    output.parent.mkdir(parents=True, exist_ok=True)
    task_links = {
        str(name): str(record.get("review_relative"))
        for name, record in (manifest.execution or {}).items()
        if isinstance(record, Mapping) and record.get("review_relative")
    }
    output.write_text(render_campaign_review(manifest, task_links=task_links), encoding="utf-8")
    _print_result({"ok": True, "review": str(output), "campaign_id": manifest.campaign_id, "read_only": True}, args.json)
    return 0


def _cmd_pilot_cleanup_plan(args: argparse.Namespace) -> int:
    try:
        manifest = load_manifest(Path(args.manifest).expanduser())
    except CampaignError as error:
        raise CliError(str(error)) from error
    plan = make_cleanup_plan(manifest)
    output = Path(args.output).expanduser() if args.output else Path(args.manifest).expanduser().with_name("cleanup-plan.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _print_result({"ok": True, "cleanup_plan": str(output), **plan}, args.json)
    return 0


def _cmd_pilot_cleanup(args: argparse.Namespace) -> int:
    config = _config(args)
    if config.profile_name != "migration-pilot":
        raise CliError("La limpieza solo está disponible en el perfil migration-pilot")
    try:
        manifest = load_manifest(Path(args.manifest).expanduser())
    except CampaignError as error:
        raise CliError(str(error)) from error
    plan_path = Path(args.plan).expanduser() if args.plan else Path(args.manifest).expanduser().with_name("cleanup-plan.json")
    if plan_path.exists():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CliError(f"El plan de limpieza no es JSON válido: {error}") from error
    else:
        plan = make_cleanup_plan(manifest)
    repositories = plan.get("repositories") if isinstance(plan, dict) else None
    digest = plan.get("approved_digest") if isinstance(plan, dict) else None
    if not isinstance(repositories, list) or not isinstance(digest, str):
        raise CliError("El plan de limpieza está incompleto")
    if args.approved_digest != digest or cleanup_digest(repositories) != digest:
        raise CliError("El digest de limpieza no coincide con el conjunto exacto de repositorios")
    if not config.destination_projects or manifest.destination_project not in config.destination_projects:
        raise CliError("La campaña no coincide con la allowlist de proyectos destino configurada")
    decision = evaluate_policy(_modern_policy(config), operation="campaign_cleanup", resource_kind="shared_query", mode="copy", destination_project=manifest.destination_project)
    if not decision.allowed:
        raise CliError(decision.message)
    if not args.account:
        raise CliError("--account es obligatorio para limpiar la campaña")
    client = _configure_dataform_client(DataformClient(args.account, manifest.destination_project), config, args.account)
    records_by_repo = {str(item.get("repository")): item for item in (manifest.cleanup.get("records") or []) if isinstance(item, dict)}
    deleted: list[str] = []
    skipped: list[dict[str, str]] = []
    for repository in repositories:
        expected = records_by_repo.get(repository, {}).get("commit_sha")
        try:
            current = client.latest_commit(repository)
            if expected and current != expected:
                skipped.append({"repository": repository, "reason": "head_changed"})
                continue
            client.delete_repository(repository, force=False)
            try:
                client.get_repository(ResourceRef("shared_query", repository, manifest.destination_project, "", repository.rsplit("/", 1)[-1], ""))
            except DataformError as verify_error:
                if any(token in str(verify_error).casefold() for token in ("404", "not found", "no encontrado")):
                    deleted.append(repository)
                else:
                    skipped.append({"repository": repository, "reason": redact_message(str(verify_error))[:300]})
            else:
                skipped.append({"repository": repository, "reason": "delete_not_verified"})
        except Exception as error:
            skipped.append({"repository": repository, "reason": redact_message(str(error))[:300]})
    _print_result({"ok": not skipped, "campaign_id": manifest.campaign_id, "approved_digest": digest, "deleted": deleted, "skipped": skipped, "force": False}, args.json)
    return 0 if not skipped else 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="queryflow", description="Flujo seguro para SQL en Cloud Shell")
    sub = parser.add_subparsers(dest="command", required=True)

    version = sub.add_parser("version")
    version.add_argument("--json", action="store_true")
    version.set_defaults(func=_cmd_version)

    init = sub.add_parser("init", help="crear o reconfigurar perfiles locales")
    init.add_argument("--path")
    init.add_argument("--profile", choices=("pilot", "team", "full-access", "migration-pilot", "migration-batch"), default="pilot")
    init.add_argument("--account")
    init.add_argument("--gcloud-config-dir")
    init.add_argument("--source-projects")
    init.add_argument("--destination-projects")
    init.add_argument("--project-alias", action="append", default=[])
    init.add_argument("--source-project")
    init.add_argument("--destination-project")
    # Legacy Workbench flags remain accepted and are normalized to the
    # explicit instance_* keys in the generated profile.
    init.add_argument("--workbench-project")
    init.add_argument("--workbench-location")
    init.add_argument("--workbench-instance")
    init.add_argument("--workbench-instance-project")
    init.add_argument("--workbench-instance-location")
    init.add_argument("--workbench-instance-name")
    init.add_argument("--workbench-job-project")
    init.add_argument("--validation-backend", choices=("local", "workbench"), default="workbench")
    init.add_argument("--max-bytes", type=int)
    init.add_argument("--finops-projects", help="proyectos permitidos para evaluaciones FinOps, separados por coma")
    init.add_argument("--billing-export-table", help="tabla opcional de Billing Export project.dataset.table")
    init.add_argument("--business-context-path", help="mapa TOML opcional de contexto empresarial")
    init.add_argument("--finops-window-days", type=int, default=30)
    init.add_argument("--allow-routine-migration", action="store_true", help="habilitar explícitamente la campaña de rutinas")
    init.add_argument("--routine-backend", choices=("direct", "workbench", "auto"), default="auto")
    init.add_argument("--routine-destination-dataset", default="functions")
    init.add_argument("--routine-batch-size", type=int, default=ROUTINE_BATCH_SIZE)
    init.add_argument("--routine-requests-per-minute", type=int, default=ROUTINE_REQUESTS_PER_MINUTE)
    init.add_argument("--json", action="store_true")
    init.set_defaults(func=_cmd_init)

    config_cmd = sub.add_parser("config", help="consultar preferencias locales")
    config_cmd.add_argument("--path")
    config_cmd.add_argument("--json", action="store_true")
    config_sub = config_cmd.add_subparsers(dest="config_command", required=True)
    for name in ("list", "validate", "path"):
        child = config_sub.add_parser(name)
        child.add_argument("--json", action="store_true")
        child.set_defaults(func=_cmd_config)
    get = config_sub.add_parser("get")
    get.add_argument("key")
    get.add_argument("--json", action="store_true")
    get.set_defaults(func=_cmd_config)
    set_cmd = config_sub.add_parser("set")
    set_cmd.add_argument("key")
    set_cmd.add_argument("value")
    set_cmd.add_argument("--json", action="store_true")
    set_cmd.set_defaults(func=_cmd_config)

    context_cmd = sub.add_parser("context", help="seleccionar el contexto de proyectos")
    context_cmd.add_argument("--config")
    context_cmd.add_argument("--json", action="store_true")
    context_sub = context_cmd.add_subparsers(dest="context_command", required=True)
    context_show = context_sub.add_parser("show")
    context_show.add_argument("--config", default=argparse.SUPPRESS)
    context_show.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    context_show.set_defaults(func=_cmd_context)
    context_use = context_sub.add_parser("use")
    context_use.add_argument("project")
    context_use.add_argument("--config", default=argparse.SUPPRESS)
    context_use.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    context_use.set_defaults(func=_cmd_context)
    context_set = context_sub.add_parser("set")
    context_set.add_argument("--source", required=True)
    context_set.add_argument("--destination", required=True)
    context_set.add_argument("--config", default=argparse.SUPPRESS)
    context_set.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    context_set.set_defaults(func=_cmd_context)
    context_alias = context_sub.add_parser("alias", help="gestionar alias de proyectos")
    context_alias_sub = context_alias.add_subparsers(dest="alias_command", required=True)
    context_alias_set = context_alias_sub.add_parser("set")
    context_alias_set.add_argument("alias")
    context_alias_set.add_argument("project")
    context_alias_set.add_argument("--config", default=argparse.SUPPRESS)
    context_alias_set.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    context_alias_set.set_defaults(func=_cmd_context)
    context_alias_list = context_alias_sub.add_parser("list")
    context_alias_list.add_argument("--config", default=argparse.SUPPRESS)
    context_alias_list.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    context_alias_list.set_defaults(func=_cmd_context)

    permissions_cmd = sub.add_parser("permissions", help="cambiar el perfil de permisos")
    permissions_cmd.add_argument("--config")
    permissions_cmd.add_argument("--json", action="store_true")
    permissions_sub = permissions_cmd.add_subparsers(dest="permissions_command", required=True)
    permissions_show = permissions_sub.add_parser("show")
    permissions_show.add_argument("--config", default=argparse.SUPPRESS)
    permissions_show.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    permissions_show.set_defaults(func=_cmd_permissions)
    permissions_use = permissions_sub.add_parser("use")
    permissions_use.add_argument("profile", choices=("pilot", "team", "full-access", "migration-pilot", "migration-batch"))
    permissions_use.add_argument("--config", default=argparse.SUPPRESS)
    permissions_use.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    permissions_use.set_defaults(func=_cmd_permissions)

    policy_cmd = sub.add_parser("policy", help="explicar filtros de seguridad")
    policy_cmd.add_argument("--config")
    policy_cmd.add_argument("--json", action="store_true")
    policy_sub = policy_cmd.add_subparsers(dest="policy_command", required=True)
    show_policy = policy_sub.add_parser("show")
    show_policy.add_argument("--config", default=argparse.SUPPRESS)
    show_policy.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    show_policy.set_defaults(func=_cmd_policy)
    check_policy = policy_sub.add_parser("check")
    check_policy.add_argument("--operation", choices=("validate", "publish", "execute", "delete", "campaign_publish", "routine_campaign_publish", "campaign_cleanup"), required=True)
    check_policy.add_argument("--resource-kind", choices=("notebook", "shared_query", "scheduled_query", "routine"), required=True)
    check_policy.add_argument("--mode", choices=("copy", "new", "update"), default="copy")
    check_policy.add_argument("--source-project")
    check_policy.add_argument("--destination-project")
    check_policy.add_argument("--location")
    check_policy.add_argument("--config", default=argparse.SUPPRESS)
    check_policy.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    check_policy.set_defaults(func=_cmd_policy)

    install = sub.add_parser("install", help="instalar QueryFlow y su plugin de Codex")
    install.add_argument("--ref")
    install.add_argument("--dry-run", action="store_true")
    install.add_argument("--json", action="store_true")
    install.set_defaults(func=_cmd_install)

    self_update = sub.add_parser("self-update", help="actualizar CLI y plugin")
    self_update.add_argument("--ref")
    self_update.add_argument("--dry-run", action="store_true")
    self_update.add_argument("--json", action="store_true")
    self_update.set_defaults(func=_cmd_self_update)

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--config")
    doctor.add_argument("--probe-remote", action="store_true", help="consultar APIs y Workbench con comandos de solo lectura")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=_cmd_doctor)

    status = sub.add_parser("status", help="mostrar el estado seguro de una tarea")
    status.add_argument("--task", required=True)
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=_cmd_status)

    diagnose = sub.add_parser("diagnose", help="exportar el diagnóstico seguro más reciente")
    diagnose.add_argument("--task", required=True)
    diagnose.add_argument("--format", choices=("json", "markdown"), default="json")
    diagnose.add_argument("--output")
    diagnose.add_argument("--json", action="store_true", help="devolver metadatos del archivo generado")
    diagnose.set_defaults(func=_cmd_diagnose)

    exception = sub.add_parser("exception", help="gestionar una excepción estática controlada")
    exception.add_argument("--config")
    exception.add_argument("--json", action="store_true")
    exception_sub = exception.add_subparsers(dest="exception_command", required=True)
    prepare = exception_sub.add_parser("prepare")
    prepare.add_argument("--task", required=True)
    prepare.add_argument("--reason", required=True)
    prepare.add_argument("--reference", required=True)
    prepare.add_argument("--config")
    prepare.add_argument("--json", action="store_true")
    prepare.set_defaults(func=_cmd_exception_prepare)

    catalog = sub.add_parser("catalog")
    catalog_sub = catalog.add_subparsers(dest="catalog_command", required=True)
    refresh = catalog_sub.add_parser("refresh")
    refresh.add_argument("--account")
    refresh.add_argument("--projects", nargs="*")
    refresh.add_argument("--catalog-path")
    refresh.add_argument("--config")
    refresh.add_argument("--json", action="store_true")
    refresh.set_defaults(func=_cmd_catalog_refresh)
    search = catalog_sub.add_parser("search")
    search.add_argument("text")
    search.add_argument("--kind")
    search.add_argument("--catalog")
    search.add_argument("--config")
    search.set_defaults(func=_cmd_catalog_search)
    show = catalog_sub.add_parser("show")
    show.add_argument("resource")
    show.add_argument("--catalog")
    show.add_argument("--config")
    show.set_defaults(func=_cmd_catalog_show)

    profile = sub.add_parser("profile")
    profile.add_argument("--table", required=True)
    profile.add_argument("--schema-file", required=True)
    profile.add_argument("--location")
    profile.add_argument("--account")
    profile.add_argument("--max-bytes", type=int, default=1_073_741_824)
    profile.add_argument("--execute", action="store_true")
    profile.add_argument("--confirm-profile", action="store_true")
    profile.add_argument("--output")
    profile.add_argument("--json", action="store_true")
    profile.set_defaults(func=_cmd_profile)

    finops = sub.add_parser("finops", help="evaluar FinOps y salud cloud sin escrituras")
    finops_sub = finops.add_subparsers(dest="finops_command", required=True)
    assess = finops_sub.add_parser("assess", help="crear un assessment gobernado bajo demanda")
    assess.add_argument("--config")
    assess.add_argument("--account")
    assess.add_argument("--projects", nargs="+")
    assess.add_argument("--window-days", type=int)
    assess.add_argument("--billing-table")
    assess.add_argument("--business-context")
    assess.add_argument("--output-root")
    assess.add_argument("--json", action="store_true")
    assess.set_defaults(func=_cmd_finops_assess)
    show_finops = finops_sub.add_parser("show", help="mostrar un assessment verificado")
    show_finops.add_argument("--assessment", required=True)
    show_finops.add_argument("--output-root")
    show_finops.add_argument("--json", action="store_true")
    show_finops.set_defaults(func=_cmd_finops_show)
    review_finops = finops_sub.add_parser("review", help="abrir el informe FinOps de solo lectura")
    review_finops.add_argument("--assessment", required=True)
    review_finops.add_argument("--output-root")
    review_finops.add_argument("--serve", action="store_true")
    review_finops.add_argument("--port", type=int, default=8080)
    review_finops.add_argument("--json", action="store_true")
    review_finops.set_defaults(func=_cmd_finops_review)

    start = sub.add_parser("start")
    start.add_argument("--resource")
    start.add_argument("--kind")
    start.add_argument("--name")
    start.add_argument("--project")
    start.add_argument("--location")
    start.add_argument("--display-name")
    start.add_argument("--fingerprint")
    start.add_argument("--content-file")
    start.add_argument("--account")
    start.add_argument("--destination-project")
    start.add_argument("--task-id")
    start.add_argument("--workspace-root")
    start.add_argument("--config")
    start.add_argument("--mode", choices=("copy", "new", "update"), default="copy")
    start.add_argument("--schedule")
    start.add_argument("--target-dataset")
    start.add_argument("--destination-table")
    start.add_argument("--write-disposition", default="WRITE_APPEND")
    start.add_argument("--open-editor", action="store_true")
    start.add_argument("--json", action="store_true")
    start.set_defaults(func=_cmd_start)

    validate = sub.add_parser("validate")
    validate.add_argument("--task", required=True)
    validate.add_argument("--account")
    validate.add_argument("--config")
    validate.add_argument("--backend", choices=("local", "workbench"))
    validate.add_argument("--static-only", action="store_true")
    validate.add_argument("--max-bytes", type=int)
    validate.add_argument("--execute-read-only", action="store_true")
    validate.add_argument("--confirm-execution", action="store_true")
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(func=_cmd_validate)

    sample = sub.add_parser("sample", help="ejecutar una muestra explícitamente aprobada")
    sample.add_argument("--task", required=True)
    sample.add_argument("--approved-digest", required=True)
    sample.add_argument("--limit", type=int)
    sample.add_argument("--fragment", type=int)
    sample.add_argument("--account")
    sample.add_argument("--config")
    sample.add_argument("--backend", choices=("workbench", "local"))
    sample.add_argument("--max-bytes", type=int)
    sample.add_argument("--json", action="store_true")
    sample.set_defaults(func=_cmd_sample)

    review = sub.add_parser("review")
    review.add_argument("--task", required=True)
    review.add_argument("--serve", action="store_true")
    review.add_argument("--watch", action="store_true")
    review.add_argument("--port", type=int, default=8080)
    review.add_argument("--config")
    review.add_argument("--json", action="store_true")
    review.set_defaults(func=_cmd_review)

    publish = sub.add_parser("publish")
    publish.add_argument("--task", required=True)
    publish.add_argument("--approved-digest")
    publish.add_argument("--approved-exception-digest")
    publish.add_argument("--destination-project")
    publish.add_argument("--account", required=True)
    publish.add_argument("--repository-id")
    publish.add_argument("--display-name")
    publish.add_argument("--author-name", default="QueryFlow")
    publish.add_argument("--audit-root")
    publish.add_argument("--force-publish", action="store_true", help="publicar con autorización explícita en full-access")
    publish.add_argument("--reason", help="motivo auditable de una publicación force")
    publish.add_argument("--config")
    publish.add_argument("--json", action="store_true")
    publish.set_defaults(func=_cmd_publish)

    migration = sub.add_parser("migration", help="herramientas locales de diccionario y reescritura")
    migration_sub = migration.add_subparsers(dest="migration_command", required=True)
    dictionary = migration_sub.add_parser("dictionary", help="validar o renderizar el diccionario privado")
    dictionary_sub = dictionary.add_subparsers(dest="dictionary_command", required=True)
    dictionary_validate = dictionary_sub.add_parser("validate")
    dictionary_validate.add_argument("--dictionary", required=True)
    dictionary_validate.add_argument("--json", action="store_true")
    dictionary_validate.set_defaults(func=_cmd_migration_dictionary)
    dictionary_render = dictionary_sub.add_parser("render")
    dictionary_render.add_argument("--dictionary", required=True)
    dictionary_render.add_argument("--output")
    dictionary_render.add_argument("--json", action="store_true")
    dictionary_render.set_defaults(func=_cmd_migration_dictionary)

    rewrite = migration_sub.add_parser("rewrite", help="planificar o aplicar rutas en una tarea local")
    rewrite_sub = rewrite.add_subparsers(dest="rewrite_command", required=True)
    rewrite_plan = rewrite_sub.add_parser("plan")
    rewrite_plan.add_argument("--task", required=True)
    rewrite_plan.add_argument("--dictionary", required=True)
    rewrite_plan.add_argument("--json", action="store_true")
    rewrite_plan.set_defaults(func=_cmd_migration_rewrite)
    rewrite_apply = rewrite_sub.add_parser("apply")
    rewrite_apply.add_argument("--task", required=True)
    rewrite_apply.add_argument("--dictionary", required=True)
    rewrite_apply.add_argument("--plan-digest", required=True)
    rewrite_apply.add_argument("--json", action="store_true")
    rewrite_apply.set_defaults(func=_cmd_migration_rewrite)

    batch = migration_sub.add_parser("batch", help="lote explícito de migración (copias o actualizaciones)")
    batch_sub = batch.add_subparsers(dest="batch_command", required=True)
    batch_inventory = batch_sub.add_parser("inventory", help="resolver y exportar la selección sin escribir")
    batch_inventory.add_argument("--selection-file", required=True, help="JSON con la lista explícita de recursos")
    batch_inventory.add_argument("--dictionary", required=True)
    batch_inventory.add_argument("--catalog")
    batch_inventory.add_argument("--source-project")
    batch_inventory.add_argument("--destination-project")
    batch_inventory.add_argument("--location")
    batch_inventory.add_argument("--source-location")
    batch_inventory.add_argument("--destination-location")
    batch_inventory.add_argument("--account")
    batch_inventory.add_argument("--content-dir", help="snapshots privados para pruebas sin llamadas remotas")
    batch_inventory.add_argument("--request-timeout", type=float, default=30.0)
    batch_inventory.add_argument("--dataform-requests-per-minute", type=int, default=BATCH_REQUESTS_PER_MINUTE)
    batch_inventory.add_argument("--expected-shared-queries", type=int)
    batch_inventory.add_argument("--expected-notebooks", type=int)
    batch_inventory.add_argument("--output")
    batch_inventory.add_argument("--config")
    batch_inventory.add_argument("--json", action="store_true")
    batch_inventory.set_defaults(func=_cmd_batch_inventory)
    batch_prepare = batch_sub.add_parser("prepare", help="crear tareas, reescrituras y diffs locales")
    batch_prepare.add_argument("--manifest", required=True)
    batch_prepare.add_argument("--dictionary", required=True)
    batch_prepare.add_argument("--account")
    batch_prepare.add_argument("--request-timeout", type=float, default=30.0)
    batch_prepare.add_argument("--dataform-requests-per-minute", type=int, default=BATCH_REQUESTS_PER_MINUTE)
    batch_prepare.add_argument("--config")
    batch_prepare.add_argument("--json", action="store_true")
    batch_prepare.set_defaults(func=_cmd_batch_prepare)
    batch_run = batch_sub.add_parser("run", help="publicar copias nuevas o actualizaciones aprobadas")
    batch_run.add_argument("--manifest", required=True)
    batch_run.add_argument("--dictionary", required=True)
    batch_run.add_argument("--execute-migration", action="store_true", help="habilita la publicación remota explícita")
    batch_run.add_argument("--approved-digest", help="digest global aprobado explícitamente")
    batch_run.add_argument("--approved-sealed-digest", help="digest independiente aprobado para copias con secretos sellados")
    batch_run.add_argument("--security-reference", help="ticket o referencia auditable que autoriza la copia sellada")
    batch_run.add_argument("--skip-pending", action="store_true", help="continuar con recursos marcados explícitamente como pendientes")
    batch_run.add_argument("--account")
    batch_run.add_argument("--audit-root")
    batch_run.add_argument("--request-timeout", type=float, default=30.0)
    batch_run.add_argument("--dataform-requests-per-minute", type=int, default=BATCH_REQUESTS_PER_MINUTE)
    batch_run.add_argument("--config")
    batch_run.add_argument("--json", action="store_true")
    batch_run.set_defaults(func=_cmd_batch_run)
    batch_resume = batch_sub.add_parser("resume", help="reanudar recursos pendientes con el mismo digest")
    batch_resume.add_argument("--manifest", required=True)
    batch_resume.add_argument("--dictionary", required=True)
    batch_resume.add_argument("--execute-migration", action="store_true")
    batch_resume.add_argument("--approved-digest", required=True)
    batch_resume.add_argument("--approved-sealed-digest", help="digest independiente aprobado para copias con secretos sellados")
    batch_resume.add_argument("--security-reference", help="ticket o referencia auditable que autoriza la copia sellada")
    batch_resume.add_argument("--skip-pending", action="store_true", help="continuar con recursos marcados explícitamente como pendientes")
    batch_resume.add_argument("--account")
    batch_resume.add_argument("--audit-root")
    batch_resume.add_argument("--request-timeout", type=float, default=30.0)
    batch_resume.add_argument("--dataform-requests-per-minute", type=int, default=BATCH_REQUESTS_PER_MINUTE)
    batch_resume.add_argument("--config")
    batch_resume.add_argument("--json", action="store_true")
    batch_resume.set_defaults(func=_cmd_batch_run)
    batch_review = batch_sub.add_parser("review", help="crear el Web Preview consolidado")
    batch_review.add_argument("--manifest", required=True)
    batch_review.add_argument("--output")
    batch_review.add_argument("--serve", action="store_true")
    batch_review.add_argument("--port", type=int, default=8080)
    batch_review.add_argument("--json", action="store_true")
    batch_review.set_defaults(func=_cmd_batch_review)

    routines = migration_sub.add_parser("routines", help="inventariar y migrar procedimientos BigQuery copy-only")
    routines_sub = routines.add_subparsers(dest="routines_command", required=True)
    routine_inventory = routines_sub.add_parser("inventory", help="inventario completo de rutinas sin ejecutar SQL")
    routine_inventory.add_argument("--source-project")
    routine_inventory.add_argument("--destination-project")
    routine_inventory.add_argument("--source-dataset", action="append", help="limitar a un dataset origen; repetir para varios")
    routine_inventory.add_argument("--destination-dataset")
    routine_inventory.add_argument("--source-location")
    routine_inventory.add_argument("--destination-location")
    routine_inventory.add_argument("--dictionary", required=True)
    routine_inventory.add_argument("--campaign-id")
    routine_inventory.add_argument("--backend", choices=("direct", "workbench", "auto"))
    routine_inventory.add_argument("--secret-handling", choices=("block", "sealed_copy"), default="block")
    routine_inventory.add_argument("--batch-size", type=int)
    routine_inventory.add_argument("--account")
    routine_inventory.add_argument("--request-timeout", type=float, default=180.0)
    routine_inventory.add_argument("--output")
    routine_inventory.add_argument("--config")
    routine_inventory.add_argument("--json", action="store_true")
    routine_inventory.set_defaults(func=_cmd_routine_inventory)
    routine_prepare = routines_sub.add_parser("prepare", help="refrescar propuestas, diffs y reportes locales")
    routine_prepare.add_argument("--manifest", required=True)
    routine_prepare.add_argument("--dictionary", required=True)
    routine_prepare.add_argument("--account")
    routine_prepare.add_argument("--request-timeout", type=float, default=180.0)
    routine_prepare.add_argument("--backend", choices=("direct", "workbench", "auto"))
    routine_prepare.add_argument("--config")
    routine_prepare.add_argument("--json", action="store_true")
    routine_prepare.set_defaults(func=_cmd_routine_prepare)
    routine_review = routines_sub.add_parser("review", help="crear o servir el Web Preview de rutinas")
    routine_review.add_argument("--manifest", required=True)
    routine_review.add_argument("--serve", action="store_true")
    routine_review.add_argument("--port", type=int, default=8080)
    routine_review.add_argument("--json", action="store_true")
    routine_review.set_defaults(func=_cmd_routine_review)
    routine_run = routines_sub.add_parser("run", help="publicar únicamente rutinas nuevas con digest aprobado")
    routine_run.add_argument("--manifest", required=True)
    routine_run.add_argument("--dictionary", required=True)
    routine_run.add_argument("--execute-migration", action="store_true")
    routine_run.add_argument("--approved-digest")
    routine_run.add_argument("--approved-sealed-digest")
    routine_run.add_argument("--security-reference")
    routine_run.add_argument("--lot", type=int)
    routine_run.add_argument("--account")
    routine_run.add_argument("--request-timeout", type=float, default=180.0)
    routine_run.add_argument("--backend", choices=("direct", "workbench", "auto"))
    routine_run.add_argument("--config")
    routine_run.add_argument("--json", action="store_true")
    routine_run.set_defaults(func=_cmd_routine_run)
    routine_resume = routines_sub.add_parser("resume", help="reanudar un lote de rutinas con el mismo digest")
    routine_resume.add_argument("--manifest", required=True)
    routine_resume.add_argument("--dictionary", required=True)
    routine_resume.add_argument("--execute-migration", action="store_true")
    routine_resume.add_argument("--approved-digest", required=True)
    routine_resume.add_argument("--approved-sealed-digest")
    routine_resume.add_argument("--security-reference")
    routine_resume.add_argument("--lot", type=int)
    routine_resume.add_argument("--account")
    routine_resume.add_argument("--request-timeout", type=float, default=180.0)
    routine_resume.add_argument("--backend", choices=("direct", "workbench", "auto"))
    routine_resume.add_argument("--config")
    routine_resume.add_argument("--json", action="store_true")
    routine_resume.set_defaults(func=_cmd_routine_run)
    routine_report = routines_sub.add_parser("report", help="regenerar los informes de revisión")
    routine_report.add_argument("--manifest", required=True)
    routine_report.add_argument("--json", action="store_true")
    routine_report.set_defaults(func=_cmd_routine_report)

    pilot = sub.add_parser("pilot", help="piloto de migración de 10 Shared Queries y 10 notebooks")
    pilot_sub = pilot.add_subparsers(dest="pilot_command", required=True)
    inventory = pilot_sub.add_parser("inventory", help="clasificar y seleccionar la muestra sin publicar")
    inventory.add_argument("--dictionary", required=True)
    inventory.add_argument("--catalog")
    inventory.add_argument("--source-project", required=True)
    inventory.add_argument("--destination-project", required=True)
    inventory.add_argument("--account")
    inventory.add_argument("--content-dir", help="directorio privado de snapshots; omite llamadas remotas")
    inventory.add_argument("--request-timeout", type=float, default=30.0, help="timeout por solicitud HTTP de Dataform (segundos)")
    inventory.add_argument(
        "--dataform-requests-per-minute",
        type=int,
        default=DEFAULT_DATAFORM_REQUESTS_PER_MINUTE,
        help="límite local de solicitudes Dataform por minuto (máximo 300)",
    )
    inventory.add_argument("--resume-inventory", action="store_true", help="reanudar desde el checkpoint privado del inventario")
    inventory.add_argument("--seed")
    inventory.add_argument("--max-resources-per-kind", type=int, help="limitar el pool aleatorio antes de exportar (opcional)")
    inventory.add_argument("--campaign-id")
    inventory.add_argument("--output")
    inventory.add_argument("--config")
    inventory.add_argument("--json", action="store_true")
    inventory.set_defaults(func=_cmd_pilot_inventory)
    prepare = pilot_sub.add_parser("prepare", help="crear tareas y diffs locales sin publicar")
    prepare.add_argument("--manifest", required=True)
    prepare.add_argument("--dictionary", required=True)
    prepare.add_argument("--account", required=True)
    prepare.add_argument("--request-timeout", type=float, default=30.0)
    prepare.add_argument("--dataform-requests-per-minute", type=int, default=DEFAULT_DATAFORM_REQUESTS_PER_MINUTE)
    prepare.add_argument("--config")
    prepare.add_argument("--json", action="store_true")
    prepare.set_defaults(func=_cmd_pilot_prepare)
    run = pilot_sub.add_parser("run", help="reescribir y publicar copias del manifiesto")
    run.add_argument("--manifest", required=True)
    run.add_argument("--dictionary", required=True)
    run.add_argument("--execute-migration", action="store_true", help="autoriza las copias nuevas de esta campaña")
    run.add_argument("--approved-digest", help="digest de publicación aprobado explícitamente")
    run.add_argument("--account")
    run.add_argument("--audit-root")
    run.add_argument("--request-timeout", type=float, default=30.0)
    run.add_argument("--dataform-requests-per-minute", type=int, default=DEFAULT_DATAFORM_REQUESTS_PER_MINUTE)
    run.add_argument("--config")
    run.add_argument("--json", action="store_true")
    run.set_defaults(func=_cmd_pilot_run)
    resume = pilot_sub.add_parser("resume", help="reanudar los recursos pendientes de una campaña")
    resume.add_argument("--manifest", required=True)
    resume.add_argument("--dictionary", required=True)
    resume.add_argument("--execute-migration", action="store_true")
    resume.add_argument("--approved-digest", help="digest de publicación aprobado explícitamente")
    resume.add_argument("--account")
    resume.add_argument("--audit-root")
    resume.add_argument("--request-timeout", type=float, default=30.0)
    resume.add_argument("--dataform-requests-per-minute", type=int, default=DEFAULT_DATAFORM_REQUESTS_PER_MINUTE)
    resume.add_argument("--config")
    resume.add_argument("--json", action="store_true")
    resume.set_defaults(func=_cmd_pilot_run)
    pilot_review = pilot_sub.add_parser("review", help="crear preview batch de solo lectura")
    pilot_review.add_argument("--manifest", required=True)
    pilot_review.add_argument("--output")
    pilot_review.add_argument("--json", action="store_true")
    pilot_review.set_defaults(func=_cmd_pilot_review)
    cleanup_plan = pilot_sub.add_parser("cleanup-plan", help="preparar digest de limpieza separado")
    cleanup_plan.add_argument("--manifest", required=True)
    cleanup_plan.add_argument("--output")
    cleanup_plan.add_argument("--json", action="store_true")
    cleanup_plan.set_defaults(func=_cmd_pilot_cleanup_plan)
    cleanup = pilot_sub.add_parser("cleanup", help="eliminar únicamente copias de la campaña aprobada")
    cleanup.add_argument("--manifest", required=True)
    cleanup.add_argument("--plan")
    cleanup.add_argument("--approved-digest", required=True)
    cleanup.add_argument("--account")
    cleanup.add_argument("--config")
    cleanup.add_argument("--json", action="store_true")
    cleanup.set_defaults(func=_cmd_pilot_cleanup)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if getattr(args, "command", None) == "pilot":
        print(
            "Aviso: `queryflow pilot` es un alias de compatibilidad deprecado; "
            "usa `queryflow migration batch` para nuevos lotes.",
            file=sys.stderr,
        )
    try:
        return int(args.func(args))
    except Exception as error:
        task_value = getattr(args, "task", None)
        task_path = Path(task_value).resolve() if task_value else None
        manifest: dict[str, Any] = {}
        if task_path and (task_path / "manifest.json").exists():
            try:
                manifest = read_manifest(task_path)
            except Exception:
                manifest = {}
        kind = getattr(error, "kind", None)
        category_map = {"permission": "iam", "transport": "network", "vpc": "vpc", "authentication": "authentication"}
        diagnostic = make_diagnostic(
            error,
            stage=str(getattr(args, "command", "command")),
            category=category_map.get(str(kind)) if kind else None,
            context=_task_context(task_path, manifest) if task_path else {"stage": getattr(args, "command", "command")},
        )
        diagnostic_path: str | None = None
        if task_path and task_path.exists():
            try:
                diagnostic_path = str(write_diagnostic(task_path, diagnostic))
            except OSError:
                diagnostic_path = None
        if getattr(args, "json", False):
            payload: dict[str, Any] = {"ok": False, "error": diagnostic.to_dict()}
            if diagnostic_path:
                payload["diagnostic_path"] = diagnostic_path
            _json_print(payload)
        else:
            print(f"queryflow: {error}", file=sys.stderr)
            print(f"diagnóstico: {diagnostic.error_id} · usa `queryflow diagnose --task {task_path}`", file=sys.stderr) if task_path else print(f"diagnóstico: {diagnostic.error_id}", file=sys.stderr)
        return 2
