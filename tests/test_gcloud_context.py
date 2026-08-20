import os
import json
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from queryflow.gcloud import GcloudContext


def test_profile_path_overrides_inherited_temporary_config_without_mutating_parent(tmp_path, monkeypatch):
    configured = tmp_path / "gcloud"
    monkeypatch.setenv("CLOUDSDK_CONFIG", "/tmp/queryflow-transient-config")
    context = GcloudContext(configured, account="analyst@example.com")

    child_env = context.environment()

    assert child_env["CLOUDSDK_CONFIG"] == str(configured)
    assert os.environ["CLOUDSDK_CONFIG"] == "/tmp/queryflow-transient-config"


def test_command_pins_account_without_mutating_gcloud_active_account(tmp_path):
    context = GcloudContext(tmp_path / "gcloud", account="analyst@example.com")

    command = context.command(["auth", "list", "--format=value(account)"])

    assert command == [
        "gcloud",
        "--account=analyst@example.com",
        "auth",
        "list",
        "--format=value(account)",
    ]


def test_default_config_dir_is_stable_home_path(monkeypatch):
    monkeypatch.setenv("CLOUDSDK_CONFIG", "/tmp/queryflow-transient-config")

    context = GcloudContext.default()

    assert context.config_dir == Path.home() / ".config" / "gcloud"


def test_validation_token_provider_uses_profile_context(tmp_path):
    from queryflow.validation import gcloud_access_token

    context = GcloudContext(tmp_path / "gcloud", account="analyst@example.com")
    with patch(
        "queryflow.validation.subprocess.run",
        return_value=CompletedProcess(["gcloud"], 0, "token\n", ""),
    ) as run:
        assert gcloud_access_token("analyst@example.com", context=context) == "token"

    assert run.call_args.kwargs["env"]["CLOUDSDK_CONFIG"] == str(tmp_path / "gcloud")


def test_validation_bq_runner_uses_profile_context(tmp_path):
    from queryflow.validation import dry_run_sql

    context = GcloudContext(tmp_path / "gcloud", account="analyst@example.com")
    responses = [
        CompletedProcess(["gcloud"], 0, "token\n", ""),
        CompletedProcess(["bq"], 0, '{"totalBytesProcessed": "1"}', ""),
    ]
    with patch("queryflow.validation.subprocess.run", side_effect=responses) as run:
        result = dry_run_sql("SELECT 1", account="analyst@example.com", gcloud_context=context)

    assert result.dry_run_ok is True
    assert len(run.call_args_list) == 2
    for call in run.call_args_list:
        assert call.kwargs["env"]["CLOUDSDK_CONFIG"] == str(tmp_path / "gcloud")


def test_dataform_token_provider_uses_profile_context(tmp_path):
    from queryflow.dataform import TokenProvider

    context = GcloudContext(tmp_path / "gcloud", account="analyst@example.com")
    with patch(
        "queryflow.dataform.subprocess.run",
        return_value=CompletedProcess(["gcloud"], 0, "token\n", ""),
    ) as run:
        assert TokenProvider("analyst@example.com", context=context).get() == "token"

    assert run.call_args.kwargs["env"]["CLOUDSDK_CONFIG"] == str(tmp_path / "gcloud")


def test_workbench_discovery_uses_profile_context(tmp_path):
    from queryflow.workbench import WorkbenchSettings, discover_proxy

    context = GcloudContext(tmp_path / "gcloud", account="analyst@example.com")
    payload = {"proxyUri": "https://workbench.example"}
    with patch(
        "queryflow.workbench.subprocess.run",
        return_value=CompletedProcess(["gcloud"], 0, json.dumps(payload), ""),
    ) as run:
        result = discover_proxy(
            WorkbenchSettings("workbench-project", "us-east1-b", "instance", "jobs-project"),
            account="analyst@example.com",
            gcloud_context=context,
        )

    assert result == "https://workbench.example"
    assert run.call_args.kwargs["env"]["CLOUDSDK_CONFIG"] == str(tmp_path / "gcloud")
