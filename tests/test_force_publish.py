import hashlib
import json
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from queryflow.catalog import ResourceRef
from queryflow.cli import main
from queryflow.dataform import ExportedAsset
from queryflow.workspace import create_workspace


def _config(path: Path, root: Path, mode: str = "full-access") -> None:
    path.write_text(
        f"active_profile = '{mode}'\n\n[profiles.{mode}]\n"
        f"mode = '{mode}'\nallow_update_existing = true\n"
        f"allow_force_publish = {str(mode == 'full-access').lower()}\n"
        "validation_backend = 'local'\n"
        f"workspace_root = '{root / 'tasks'}'\n"
        f"audit_root = '{root / 'audit'}'\n",
        encoding="utf-8",
    )


def _remote_task(root: Path) -> tuple[Path, ResourceRef]:
    resource = ResourceRef(
        "shared_query",
        "projects/source-project/locations/us/repositories/original",
        "source-project",
        "us",
        "original",
        "head",
    )
    task = create_workspace(
        root=root / "tasks",
        task_id="force-update",
        resource=resource,
        content=b"SELECT 1;\n",
        filename="content.sql",
        mode="update",
        account="analyst@example.com",
    )
    (task / "content.sql").write_text("SELECT 2;\n", encoding="utf-8")
    return task, resource


def test_full_access_force_publish_updates_without_validation(tmp_path):
    task, resource = _remote_task(tmp_path)
    config = tmp_path / "config.toml"
    _config(config, tmp_path)

    class FakeClient:
        update_calls = []

        def __init__(self, account, project):
            self.account = account
            self.project = project

        def export(self, requested):
            return ExportedAsset(resource, "content.sql", b"SELECT 1;\n", {}, "head")

        def update_file(self, repository, filename, content, **kwargs):
            self.__class__.update_calls.append((repository, filename, content, kwargs))
            return {"repository": repository, "filename": filename, "commit_sha": "updated"}

        def read_file(self, repository, filename):
            return b"SELECT 2;\n"

    output = StringIO()
    with patch("queryflow.cli.DataformClient", FakeClient), redirect_stdout(output):
        status = main(
            [
                "publish",
                "--task",
                str(task),
                "--force-publish",
                "--reason",
                "Aprobación explícita para incidente VPC",
                "--account",
                "analyst@example.com",
                "--config",
                str(config),
                "--json",
            ]
        )

    assert status == 0
    assert len(FakeClient.update_calls) == 1
    receipt = json.loads(output.getvalue())
    assert receipt["force_published"] is True
    assert receipt["force_authorization"]["reason"] == "Aprobación explícita para incidente VPC"
    authorization = json.loads((task / "force-authorization.json").read_text(encoding="utf-8"))
    assert authorization["content_sha256"] == hashlib.sha256(b"SELECT 2;\n").hexdigest()
    assert (tmp_path / "audit" / "force-update" / "force-authorization.json").exists()


def test_force_publish_is_rejected_outside_full_access(tmp_path):
    task, _resource = _remote_task(tmp_path)
    config = tmp_path / "config.toml"
    _config(config, tmp_path, mode="team")
    errors = StringIO()

    with redirect_stderr(errors):
        status = main(
            [
                "publish",
                "--task",
                str(task),
                "--force-publish",
                "--reason",
                "Aprobación explícita",
                "--destination-project",
                "source-project",
                "--account",
                "analyst@example.com",
                "--config",
                str(config),
            ]
        )

    assert status == 2
    assert "full-access" in errors.getvalue()
