import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from queryflow.cli import main
from queryflow.catalog import Catalog, ResourceRef, save_catalog


def _config(path: Path) -> None:
    path.write_text(
        """
schema_version = 3
active_profile = "pilot"

[project_aliases]
replication = "replication-project"
analytics = "analytics-project"

[context]
source_project = "replication"
destination_project = "replication"

[profiles.pilot]
mode = "pilot"
""",
        encoding="utf-8",
    )


def test_context_use_persists_single_project_context(tmp_path):
    config = tmp_path / "config.toml"
    _config(config)
    output = StringIO()

    with redirect_stdout(output):
        assert main(["context", "use", "analytics", "--config", str(config), "--json"]) == 0

    payload = json.loads(output.getvalue())
    assert payload["context"] == {
        "source_project": "analytics-project",
        "destination_project": "analytics-project",
    }
    raw = config.read_text(encoding="utf-8")
    assert 'source_project = "analytics-project"' in raw
    assert 'destination_project = "analytics-project"' in raw


def test_context_set_persists_migration_context(tmp_path):
    config = tmp_path / "config.toml"
    _config(config)
    output = StringIO()

    with redirect_stdout(output):
        assert main(
            [
                "context",
                "set",
                "--source",
                "replication",
                "--destination",
                "analytics",
                "--config",
                str(config),
                "--json",
            ]
        ) == 0

    payload = json.loads(output.getvalue())
    assert payload["context"] == {
        "source_project": "replication-project",
        "destination_project": "analytics-project",
    }


def test_context_alias_set_persists_project_alias(tmp_path):
    config = tmp_path / "config.toml"
    _config(config)
    output = StringIO()

    with redirect_stdout(output):
        assert main(
            [
                "context",
                "alias",
                "set",
                "billing",
                "billing-project",
                "--config",
                str(config),
                "--json",
            ]
        ) == 0

    payload = json.loads(output.getvalue())
    assert payload["project_aliases"]["billing"] == "billing-project"
    assert 'billing = "billing-project"' in config.read_text(encoding="utf-8")


def test_init_writes_canonical_workbench_context_and_gcloud_path(tmp_path):
    config = tmp_path / "config.toml"
    output = StringIO()

    with redirect_stdout(output):
        assert main(
            [
                "init",
                "--path",
                str(config),
                "--profile",
                "full-access",
                "--account",
                "analyst@example.com",
                "--gcloud-config-dir",
                "/home/analyst/.config/gcloud",
                "--workbench-instance-project",
                "workbench-project",
                "--workbench-instance-location",
                "us-east1-b",
                "--workbench-instance-name",
                "python-notebook",
                "--workbench-job-project",
                "analytics-project",
                "--project-alias",
                "replication=replication-project",
                "--project-alias",
                "analytics=analytics-project",
                "--source-project",
                "replication",
                "--destination-project",
                "analytics",
                "--json",
            ]
        ) == 0

    payload = json.loads(output.getvalue())
    assert payload["active_profile"] == "full-access"
    raw = config.read_text(encoding="utf-8")
    assert "workbench_instance_project" in raw
    assert 'gcloud_config_dir = "/home/analyst/.config/gcloud"' in raw
    assert 'replication = "replication-project"' in raw
    assert 'destination_project = "analytics"' in raw


def test_permissions_use_changes_active_profile(tmp_path):
    config = tmp_path / "config.toml"
    _config(config)
    store = config.read_text(encoding="utf-8")
    config.write_text(
        store
        + "\n[profiles.full-access]\nmode = \"full-access\"\nallow_force_publish = true\n",
        encoding="utf-8",
    )
    output = StringIO()

    with redirect_stdout(output):
        assert main(["permissions", "use", "full-access", "--config", str(config), "--json"]) == 0

    payload = json.loads(output.getvalue())
    assert payload["active_profile"] == "full-access"
    assert 'active_profile = "full-access"' in config.read_text(encoding="utf-8")


def test_start_snapshots_destination_from_active_context(tmp_path):
    config = tmp_path / "config.toml"
    _config(config)
    catalog_path = tmp_path / "catalog.json"
    resource = ResourceRef(
        "shared_query",
        "projects/replication-project/locations/us/repositories/q1",
        "replication-project",
        "us",
        "q1",
        "local-input",
    )
    save_catalog(Catalog([resource], "now"), catalog_path)
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "[profiles.pilot]\nmode = \"pilot\"\n",
            "[profiles.pilot]\nmode = \"pilot\"\n"
            + f'catalog_path = "{catalog_path}"\n'
            + f'workspace_root = "{tmp_path / "tasks"}"\n',
        ),
        encoding="utf-8",
    )
    output = StringIO()
    with redirect_stdout(output):
        assert main(
            [
                "start",
                "--resource",
                resource.name,
                "--account",
                "analyst@example.com",
                "--config",
                str(config),
                "--task-id",
                "context-task",
                "--json",
            ]
        ) == 0
    manifest = json.loads((tmp_path / "tasks" / "context-task" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["destination_project"] == "replication-project"
