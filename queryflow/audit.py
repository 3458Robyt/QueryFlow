from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditError(RuntimeError):
    """The audit package could not be written or verified."""


class LocalAuditStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def archive(self, task: Path) -> dict[str, Any]:
        manifest_path = task / "manifest.json"
        if not manifest_path.is_file():
            raise AuditError("No se puede auditar una tarea sin manifest.json")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AuditError("El manifest de auditoría no es JSON válido") from error
        task_id = manifest.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise AuditError("El manifest no contiene task_id")
        destination = self.root / task_id
        if destination.exists():
            receipt_path = destination / "audit-receipt.json"
            if receipt_path.is_file():
                try:
                    existing = json.loads(receipt_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as error:
                    raise AuditError(f"La auditoría existente no es válida: {destination}") from error
                current_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                if existing.get("manifest_sha256") == current_hash:
                    return existing
            raise AuditError(f"La auditoría ya existe con contenido diferente: {destination}")
        destination.mkdir(parents=True, exist_ok=False)
        copied: list[str] = []
        for source in sorted(task.rglob("*")):
            if not source.is_file() or ".git" in source.parts:
                continue
            relative = source.relative_to(task)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(relative.as_posix())
        receipt = {
            "task_id": task_id,
            "archived_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "files": copied,
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        }
        (destination / "audit-receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return receipt

    def record_publish(self, task_id: str, receipt: dict[str, Any]) -> Path:
        destination = self.root / task_id
        if not destination.is_dir():
            raise AuditError(f"No existe el paquete de auditoría {destination}")
        target = destination / "publish-receipt.json"
        target.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return target


class GcsAuditStore:
    """Upload only task artifacts to a configured Cloud Storage prefix."""

    def __init__(self, root_uri: str, *, runner: Any = None) -> None:
        if not root_uri.startswith("gs://"):
            raise AuditError("GCS audit_root debe empezar por gs://")
        self.root_uri = root_uri.rstrip("/")
        self.runner = runner or self._run

    @staticmethod
    def _run(command: list[str]) -> int:
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        if completed.returncode != 0:
            raise AuditError(completed.stderr.strip() or "Falló la carga de auditoría")
        return completed.returncode

    def _upload(self, source: Path, destination: str) -> None:
        result = self.runner(["gcloud", "storage", "cp", str(source), destination])
        if isinstance(result, int) and result != 0:
            raise AuditError(f"Falló la carga de {source}")

    def archive(self, task: Path) -> dict[str, Any]:
        manifest_path = task / "manifest.json"
        if not manifest_path.is_file():
            raise AuditError("No se puede auditar una tarea sin manifest.json")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AuditError("El manifest de auditoría no es JSON válido") from error
        task_id = manifest.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise AuditError("El manifest no contiene task_id")
        copied: list[str] = []
        for source in sorted(task.rglob("*")):
            if not source.is_file() or ".git" in source.parts:
                continue
            relative = source.relative_to(task).as_posix()
            self._upload(source, f"{self.root_uri}/{task_id}/{relative}")
            copied.append(relative)
        receipt = {
            "task_id": task_id,
            "archived_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "files": copied,
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "uri": f"{self.root_uri}/{task_id}",
        }
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json") as handle:
            json.dump(receipt, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            self._upload(Path(handle.name), f"{self.root_uri}/{task_id}/audit-receipt.json")
        return receipt

    def record_publish(self, task_id: str, receipt: dict[str, Any]) -> str:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json") as handle:
            json.dump(receipt, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            destination = f"{self.root_uri}/{task_id}/publish-receipt.json"
            self._upload(Path(handle.name), destination)
        return destination
