import subprocess
from pathlib import Path
from unittest.mock import patch

from queryflow.cli import _gcloud_workbench_probe
from queryflow.config import load_config


def _config(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(
        "active_profile = 'pilot'\n\n[profiles.pilot]\n"
        "validation_backend = 'workbench'\n"
        "workbench_instance_project = 'workbench-project'\n"
        "workbench_instance_location = 'us-east1-b'\n"
        "workbench_instance_name = 'python-notebook'\n"
        "workbench_job_project = 'analytics-project'\n",
        encoding="utf-8",
    )
    return load_config(path)


def test_workbench_not_found_does_not_try_legacy_notebooks_command(tmp_path):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", "NOT_FOUND: instance")

    with patch("queryflow.cli.shutil.which", return_value="/usr/bin/gcloud"), patch(
        "queryflow.cli.subprocess.run", side_effect=run
    ):
        result = _gcloud_workbench_probe(_config(tmp_path))

    assert result["ok"] is False
    assert len(calls) == 1
    assert "notebooks" not in calls[0]


def test_workbench_legacy_fallback_only_when_command_is_unavailable(tmp_path):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return subprocess.CompletedProcess(command, 1, "", "Invalid choice: workbench")
        return subprocess.CompletedProcess(command, 0, "{}", "")

    with patch("queryflow.cli.shutil.which", return_value="/usr/bin/gcloud"), patch(
        "queryflow.cli.subprocess.run", side_effect=run
    ):
        result = _gcloud_workbench_probe(_config(tmp_path))

    assert result["ok"] is True
    assert len(calls) == 2
    assert "notebooks" in calls[1]

