from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Optional

from .notebooks import extract_code_cells, rebuild_notebook, rebuild_notebook_from_workspace
from .state import normalize_validation_status, validation_is_publishable
from .workspace import read_manifest


class TaskError(RuntimeError):
    """A task is not ready for validation or publication."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def approval_digest(manifest: dict[str, Any], validation: dict[str, Any]) -> str:
    payload = {"manifest": manifest, "validation": validation}
    return hashlib.sha256(_canonical(payload)).hexdigest()


def baseline_file(task: Path, filename: str) -> bytes:
    if Path(filename).is_absolute() or ".." in Path(filename).parts:
        raise TaskError("filename fuera del workspace")
    completed = subprocess.run(
        ["git", "-C", str(task), "show", f"HEAD:{filename}"],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise TaskError(f"No existe baseline Git para {filename}")
    return completed.stdout


def sync_notebook_task(task: Path, original: Optional[bytes] = None) -> bytes:
    manifest = read_manifest(task)
    if manifest.get("resource", {}).get("kind") != "notebook":
        raise TaskError("La tarea no es un notebook")
    filename = str(manifest.get("filename") or "content.ipynb")
    baseline = original if original is not None else baseline_file(task, filename)
    cells_directory = task / "cells"
    index_path = cells_directory / "index.json"
    if index_path.exists():
        status = subprocess.run(
            ["git", "-C", str(task), "status", "--porcelain", "--", "cells"],
            check=False,
            capture_output=True,
            text=True,
        )
        if not status.stdout.strip():
            return baseline
        proposed = rebuild_notebook_from_workspace(baseline, cells_directory)
        (task / filename).write_bytes(proposed)
        return proposed
    allowed = [index for index, _language, _source in extract_code_cells(baseline)]
    changed: dict[int, str] = {}
    if cells_directory.exists():
        for path in sorted(cells_directory.iterdir()):
            if path.suffix not in {".sql", ".txt", ".py", ".md"}:
                continue
            try:
                index = int(path.stem)
            except ValueError:
                continue
            if index not in allowed:
                raise TaskError(f"La celda {index} no está en el baseline")
            baseline_cell = subprocess.run(
                ["git", "-C", str(task), "show", f"HEAD:cells/{path.name}"],
                check=False,
                capture_output=True,
            )
            current = path.read_text(encoding="utf-8")
            if baseline_cell.returncode != 0 or baseline_cell.stdout.decode("utf-8") != current:
                changed[index] = current
    proposed = rebuild_notebook(baseline, changed) if changed else (task / filename).read_bytes()
    (task / filename).write_bytes(proposed)
    return proposed


def preview_notebook_task(task: Path) -> bytes:
    """Build the current notebook proposal without writing content.ipynb."""
    manifest = read_manifest(task)
    if manifest.get("resource", {}).get("kind") != "notebook":
        raise TaskError("La tarea no es un notebook")
    filename = str(manifest.get("filename") or "content.ipynb")
    baseline = baseline_file(task, filename)
    cells_directory = task / "cells"
    if (cells_directory / "index.json").exists():
        return rebuild_notebook_from_workspace(baseline, cells_directory)
    return (task / filename).read_bytes()


def mark_validation(task: Path, validation: dict[str, Any]) -> dict[str, Any]:
    status = normalize_validation_status(validation)
    method = "static" if (validation.get("dry_run") or {}).get("skipped") else "bigquery_dry_run"
    filename = str(read_manifest(task)["filename"])
    content_sha256 = hashlib.sha256((task / filename).read_bytes()).hexdigest()
    validation = dict(validation)
    validation.update(
        {
            "schema_version": 2,
            "status": status,
            "method": method,
            "publishable": status == "ready" and method == "bigquery_dry_run" and bool(validation.get("ok")),
            "content_sha256": content_sha256,
        }
    )
    (task / "validation.json").write_text(
        json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest = read_manifest(task)
    manifest["schema_version"] = max(int(manifest.get("schema_version", 1)), 2)
    manifest["validation_status"] = status
    manifest["workflow_state"] = status
    manifest["proposed_sha256"] = content_sha256
    manifest.pop("approval_digest", None)
    if validation_is_publishable(validation):
        manifest["approval_digest"] = approval_digest(manifest, validation)
    # Write the complete manifest rather than merging keys so an old digest
    # is truly removed after a new edit or a static-only precheck.
    (task / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest
