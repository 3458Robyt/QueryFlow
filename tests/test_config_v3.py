from pathlib import Path

from queryflow.config import load_config
from queryflow.config_store import ConfigStore


def test_loads_full_access_profile_and_explicit_workbench_fields(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        """
schema_version = 3
active_profile = "full-access"

[project_aliases]
replication = "replication-project"
analytics = "analytics-project"

[context]
source_project = "replication"
destination_project = "analytics"

[profiles.full-access]
mode = "full-access"
account = "analyst@example.com"
gcloud_config_dir = "~/.config/gcloud"
allow_update_existing = true
allow_force_publish = true
workbench_instance_project = "workbench-project"
workbench_instance_location = "us-east1-b"
workbench_instance_name = "notebook-instance"
workbench_job_project = "jobs-project"
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.mode == "full-access"
    assert config.gcloud_config_dir == Path.home() / ".config" / "gcloud"
    assert config.workbench_instance_project == "workbench-project"
    assert config.workbench_instance_name == "notebook-instance"
    assert config.allow_force_publish is True
    assert config.project_aliases == {
        "replication": "replication-project",
        "analytics": "analytics-project",
    }
    assert config.context_source_project == "replication"
    assert config.context_destination_project == "analytics"


def test_migrates_old_workbench_project_key_and_team_mode(tmp_path):
    path = tmp_path / "legacy.toml"
    path.write_text(
        """
active_profile = "team"

[profiles.team]
mode = "team"
workbench_project = "legacy-workbench-project"
workbench_location = "us-east1-b"
workbench_instance = "legacy-instance"
workbench_job_project = "jobs-project"
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.profile_name == "team"
    assert config.workbench_instance_project == "legacy-workbench-project"
    assert config.workbench_instance_location == "us-east1-b"
    assert config.workbench_instance_name == "legacy-instance"


def test_legacy_full_access_profile_name_matches_mode(tmp_path):
    path = tmp_path / "legacy.yaml"
    path.write_text(
        "mode: full-access\nworkspace_root: /tmp/queryflow-tasks\n",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.mode == "full-access"
    assert config.profile_name == "full-access"


def test_config_store_reads_project_context_without_credentials(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        """
schema_version = 3
active_profile = "full-access"

[project_aliases]
analytics = "analytics-project"

[context]
source_project = "analytics"
destination_project = "analytics"

[profiles.full-access]
mode = "full-access"
gcloud_config_dir = "~/.config/gcloud"
""",
        encoding="utf-8",
    )

    document = ConfigStore(path).load()

    assert document.project_aliases == {"analytics": "analytics-project"}
    assert document.context == {
        "source_project": "analytics",
        "destination_project": "analytics",
    }
    assert "token" not in path.read_text(encoding="utf-8").lower()


def test_new_config_writes_schema_three(tmp_path):
    path = tmp_path / "new.toml"
    document = ConfigStore(path).initialize(profile="pilot", values={"mode": "pilot"})

    assert document.schema_version == 3
    assert ConfigStore(path).load().schema_version == 3
