from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .catalog import ResourceRef


class WorkspaceError(RuntimeError):
    """A task workspace could not be created or is unsafe to use."""


TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _run_git(task: Path, args: list[str]) -> None:
    completed = subprocess.run(
        ["git", "-C", str(task), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise WorkspaceError(f"Git falló: {detail}")


def create_workspace(
    *,
    root: Path,
    task_id: str,
    resource: ResourceRef,
    content: bytes,
    filename: str,
    mode: str = "copy",
    account: str | None = None,
) -> Path:
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise WorkspaceError("task_id contiene caracteres no permitidos")
    relative_filename = Path(filename)
    if relative_filename.is_absolute() or ".." in relative_filename.parts:
        raise WorkspaceError("filename debe estar dentro del workspace")
    if not relative_filename.name:
        raise WorkspaceError("filename no puede estar vacío")

    task = (root / task_id).resolve()
    resolved_root = root.resolve()
    if resolved_root not in task.parents:
        raise WorkspaceError("El workspace quedaría fuera de la raíz autorizada")
    if task.exists():
        raise WorkspaceError(f"Ya existe el workspace {task}")

    task.mkdir(parents=True)
    try:
        target = task / relative_filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "task_id": task_id,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "mode": mode,
            "account": account,
            "resource": resource.to_dict(),
            "filename": relative_filename.as_posix(),
            "baseline_sha256": _sha256(content),
            "proposed_sha256": _sha256(content),
            "validation_status": "pending",
            "published": False,
        }
        (task / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _run_git(task, ["init", "--quiet"])
        _run_git(task, ["add", "--", "."])
        _run_git(
            task,
            [
                "-c",
                "user.name=QueryFlow",
                "-c",
                "user.email=queryflow@localhost",
                "commit",
                "--quiet",
                "-m",
                "baseline",
            ],
        )
    except Exception:
        # The task directory is intentionally left in place for forensic inspection.
        raise
    return task


def read_manifest(task: Path) -> dict[str, Any]:
    path = task / "manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkspaceError(f"Manifest inválido en {task}") from error
    if not isinstance(value, dict) or not isinstance(value.get("task_id"), str):
        raise WorkspaceError("El manifest no contiene task_id")
    return value


def update_manifest(task: Path, **changes: Any) -> dict[str, Any]:
    manifest = read_manifest(task)
    manifest.update(changes)
    (task / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def working_diff(task: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(task), "diff", "--no-ext-diff", "--", "."],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise WorkspaceError(completed.stderr.strip() or "No se pudo obtener el diff")
    return completed.stdout
