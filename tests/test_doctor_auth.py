import json
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from queryflow.cli import main


def test_doctor_requires_configured_account_to_be_authenticated(tmp_path, capsys):
    config = tmp_path / "config.toml"
    config.write_text(
        "active_profile = 'pilot'\n\n[profiles.pilot]\n"
        "mode = 'pilot'\naccount = 'configured@example.com'\n"
        "validation_backend = 'local'\n",
        encoding="utf-8",
    )

    with patch("queryflow.cli.shutil.which", return_value="/usr/bin/tool"), patch(
        "queryflow.cli.GcloudContext.run",
        return_value=CompletedProcess(
            ["gcloud"], 0, "other@example.com\n", ""
        ),
    ):
        status = main(["doctor", "--config", str(config), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert status == 2
    assert payload["gcloud_auth"]["configured_account"] == "configured@example.com"
    assert payload["gcloud_auth"]["configured_account_present"] is False
    assert payload["gcloud_auth"]["ok"] is False
