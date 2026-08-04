from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .audit import AuditError, GcsAuditStore, LocalAuditStore
from .catalog import (
    CatalogError,
    ResourceRef,
    discover_projects,
    load_catalog,
    refresh_catalog,
    save_catalog,
)
from .config import ConfigError, QueryflowConfig, load_config
from .config_store import ConfigStore, ConfigStoreError, default_config_path
from .dataform import DataformClient, ExportedAsset, DataformError
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
from .state import validation_is_publishable
from .task import TaskError, approval_digest, baseline_file, mark_validation, sync_notebook_task
from .transfer import TransferClient, TransferError
from .validation import (
    dry_run_sql,
    dry_run_sql_fragments,
    execute_read_only_sql,
    execute_read_only_sql_fragments,
    validate_sql_fragments,
    validate_sql_text,
)
from .policy import Policy, PolicyError, evaluate_policy
from .sample import execution_digest, limit_query, sanitize_rows
from .workbench import WorkbenchError, WorkbenchSettings, execute_workbench_sample, validate_workbench_fragments
from .workspace import WorkspaceError, create_workspace, read_manifest, update_manifest
from .schedule import ScheduleError, ScheduleSpec


class CliError(RuntimeError):
    """A safe, user-facing QueryFlow failure."""


PACKAGE_VERSION = "0.1.0"
DEFAULT_REPOSITORY = "3458Robyt/QueryFlow"
DEFAULT_MARKETPLACE = "queryflow"


def _json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _config(args: argparse.Namespace) -> QueryflowConfig:
    return load_config(Path(args.config) if getattr(args, "config", None) else None)


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
        allow_update_existing=config.allow_update_existing and config.mode == "team",
        allowed_resource_kinds=("notebook", "shared_query"),
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
    payload = {"queryflow": PACKAGE_VERSION, "config_schema": 1, "plugin": PACKAGE_VERSION}
    _print_result(payload, args.json)
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser() if args.path else default_config_path()
    profile = args.profile or "pilot"
    if profile not in {"pilot", "team"}:
        raise CliError("--profile debe ser pilot o team")
    values = {
        "mode": profile,
        "account": args.account or "",
        "source_projects": [item for item in (args.source_projects or "").split(",") if item.strip()],
        "destination_projects": [item for item in (args.destination_projects or "").split(",") if item.strip()],
        "workbench_project": args.workbench_project or "",
        "workbench_location": args.workbench_location or "",
        "workbench_instance": args.workbench_instance or "",
        "workbench_job_project": args.workbench_job_project or "",
        "validation_backend": args.validation_backend or "workbench",
        "max_bytes": args.max_bytes or 5 * 1024 * 1024 * 1024,
        "sample_default_rows": 3,
        "sample_max_rows": 5,
    }
    try:
        document = ConfigStore(path).initialize(profile=profile, values=values)
    except ConfigStoreError as error:
        raise CliError(str(error)) from error
    payload = {"path": str(path), "active_profile": document.active_profile, "profiles": document.profiles}
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


def _audit_store(value: str) -> Any:
    if value.startswith("gs://"):
        return GcsAuditStore(value)
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


def _cmd_doctor(args: argparse.Namespace) -> int:
    config = _config(args)
    checks = {
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
    }
    _print_result(checks, args.json)
    required = checks["git"] and checks["gcloud"] and checks["bq"]
    if config.validation_backend == "workbench":
        required = required and checks["websockets"]
    return 0 if required else 2


def _cmd_catalog_refresh(args: argparse.Namespace) -> int:
    config = _config(args)
    projects = args.projects or None
    catalog = refresh_catalog(projects=projects, account=args.account)
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


def _load_content(args: argparse.Namespace, resource: ResourceRef) -> tuple[bytes, str, dict[str, Any], str]:
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
    client = DataformClient(args.account, args.destination_project or resource.project)
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
    if config.policy_enforced:
        decision = evaluate_policy(
            _modern_policy(config),
            operation="start",
            resource_kind=resource.kind,
            mode=args.mode,
            source_project=resource.project,
            destination_project=args.destination_project,
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
    content, filename, metadata, head = _load_content(args, resource)
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
    extraction: dict[str, Any] | None = None
    if is_notebook:
        filename = str(manifest["filename"])
        extracted = analyze_sql_fragments((task / filename).read_bytes())
        fragments = extracted.fragments
        extraction = extracted.to_dict()
        sql = "\n\n".join(fragment for _index, fragment in fragments)
        static = validate_sql_fragments(fragments)
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
    if args.static_only:
        dry = {"dry_run_ok": None, "skipped": True}
    else:
        if backend == "workbench":
            required = {
                "workbench_project": config.workbench_project,
                "workbench_location": config.workbench_location,
                "workbench_instance": config.workbench_instance,
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
                )
            else:
                result = dry_run_sql(
                    sql,
                    location=manifest.get("resource", {}).get("location"),
                    project_id=project_id,
                    maximum_bytes_billed=_effective_max_bytes(args, config),
                    account=account,
                )
        dry = result.to_dict()
    errors = list(static.errors) + list(dry.get("errors") or [])
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
            )
        else:
            executed = execute_read_only_sql(
                sql,
                location=manifest.get("resource", {}).get("location"),
                project_id=project_id,
                maximum_bytes_billed=_effective_max_bytes(args, config),
                account=account,
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
        "ok": not errors and (args.static_only or dry.get("dry_run_ok") is True),
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
    manifest = mark_validation(task, validation)
    result = {
        "task": str(task),
        "validation": validation,
        "approval_digest": manifest.get("approval_digest"),
    }
    _print_result(result, args.json)
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
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
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
    if args.approved_digest != expected_digest:
        raise CliError("El digest de muestra no coincide con el SQL, fragmento o límite actuales")
    if (args.backend or config.validation_backend) != "workbench":
        raise CliError("La ejecución muestral solo está habilitada dentro de Workbench")
    required = {
        "workbench_project": config.workbench_project,
        "workbench_location": config.workbench_location,
        "workbench_instance": config.workbench_instance,
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
    review = write_review_html(task, before_bytes, after_bytes, validation)
    result = {"review": str(review), "task": str(task)}
    _print_result(result, args.json)
    if args.serve:
        server, url = serve_review(task, args.port)
        print(f"Review disponible en {url}", flush=True)
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
            schedule=spec_raw.get("schedule"),
            location=spec_raw.get("location"),
            destination_dataset=spec_raw.get("destination_dataset"),
            destination_table=spec_raw.get("destination_table"),
            write_disposition=spec_raw.get("write_disposition", "WRITE_APPEND"),
            disabled=spec_raw.get("disabled", True),
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
        audit_store = _audit_store(audit_root)
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
    validation_path = task / "validation.json"
    if not validation_path.exists():
        raise CliError("La tarea no tiene validation.json")
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
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
    destination_project = args.destination_project
    if not destination_project:
        raise CliError("--destination-project es obligatorio")
    if not args.account:
        raise CliError("--account es obligatorio para publicar")
    resource = ResourceRef.from_dict(manifest["resource"])
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
        if config.mode != "team" or not config.allow_update_existing:
            raise CliError("Actualizar un recurso existente requiere mode: team y allow_update_existing: true")
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
    if current_sha != manifest.get("proposed_sha256"):
        raise CliError("El archivo cambió después de validate; genere una nueva aprobación")
    if config.audit_root is None and not args.audit_root:
        raise CliError("Configura audit_root antes de publicar")
    audit_root = args.audit_root or config.audit_root
    if audit_root is None:
        raise CliError("audit_root inválido")
    content = (task / filename).read_bytes()
    source_metadata = resource.metadata.get("source_metadata") or {}
    source_project = config.source_projects[0] if len(config.source_projects) == 1 else ""
    if source_project:
        client = DataformClient(args.account, destination_project, source_project=source_project)
    else:
        # Keep the constructor compatible with custom transports used by
        # integrations and tests that predate the optional source allowlist.
        client = DataformClient(args.account, destination_project)
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
        audit_store = _audit_store(audit_root)
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
        "audit": audit_receipt,
        "published": published,
        "destination_project": destination_project,
    }
    audit_store.record_publish(manifest["task_id"], final_receipt)
    manifest.update({"published": True, "destination": published, "audit_receipt": final_receipt})
    (task / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _print_result(final_receipt, args.json)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="queryflow", description="Flujo seguro para SQL en Cloud Shell")
    sub = parser.add_subparsers(dest="command", required=True)

    version = sub.add_parser("version")
    version.add_argument("--json", action="store_true")
    version.set_defaults(func=_cmd_version)

    init = sub.add_parser("init", help="crear o reconfigurar perfiles locales")
    init.add_argument("--path")
    init.add_argument("--profile", choices=("pilot", "team"), default="pilot")
    init.add_argument("--account")
    init.add_argument("--source-projects")
    init.add_argument("--destination-projects")
    init.add_argument("--workbench-project")
    init.add_argument("--workbench-location")
    init.add_argument("--workbench-instance")
    init.add_argument("--workbench-job-project")
    init.add_argument("--validation-backend", choices=("local", "workbench"), default="workbench")
    init.add_argument("--max-bytes", type=int)
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

    policy_cmd = sub.add_parser("policy", help="explicar filtros de seguridad")
    policy_cmd.add_argument("--config")
    policy_cmd.add_argument("--json", action="store_true")
    policy_sub = policy_cmd.add_subparsers(dest="policy_command", required=True)
    show_policy = policy_sub.add_parser("show")
    show_policy.add_argument("--json", action="store_true")
    show_policy.set_defaults(func=_cmd_policy)
    check_policy = policy_sub.add_parser("check")
    check_policy.add_argument("--operation", choices=("validate", "publish", "execute", "delete"), required=True)
    check_policy.add_argument("--resource-kind", choices=("notebook", "shared_query", "scheduled_query"), required=True)
    check_policy.add_argument("--mode", choices=("copy", "new", "update"), default="copy")
    check_policy.add_argument("--source-project")
    check_policy.add_argument("--destination-project")
    check_policy.add_argument("--location")
    check_policy.add_argument("--json", action="store_true")
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
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=_cmd_doctor)

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
    profile.add_argument("--max-bytes", type=int, default=1_073_741_824)
    profile.add_argument("--execute", action="store_true")
    profile.add_argument("--confirm-profile", action="store_true")
    profile.add_argument("--output")
    profile.add_argument("--json", action="store_true")
    profile.set_defaults(func=_cmd_profile)

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
    review.add_argument("--json", action="store_true")
    review.set_defaults(func=_cmd_review)

    publish = sub.add_parser("publish")
    publish.add_argument("--task", required=True)
    publish.add_argument("--approved-digest", required=True)
    publish.add_argument("--destination-project", required=True)
    publish.add_argument("--account", required=True)
    publish.add_argument("--repository-id")
    publish.add_argument("--display-name")
    publish.add_argument("--author-name", default="QueryFlow")
    publish.add_argument("--audit-root")
    publish.add_argument("--config")
    publish.add_argument("--json", action="store_true")
    publish.set_defaults(func=_cmd_publish)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (CliError, CatalogError, ConfigError, PolicyError, WorkspaceError, TaskError, DataformError, ProfileError, WorkbenchError, OSError) as error:
        print(f"queryflow: {error}", file=sys.stderr)
        return 2
