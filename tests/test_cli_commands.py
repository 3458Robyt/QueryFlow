import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from queryflow.catalog import ResourceRef
from queryflow.cli import main
from queryflow.workspace import create_workspace


class CliCommandTests(unittest.TestCase):
    def test_install_dry_run_exposes_reproducible_commands(self):
        output = StringIO()
        with redirect_stdout(output):
            status = main(["install", "--ref", "v0.1.0", "--dry-run", "--json"])

        self.assertEqual(status, 0)
        self.assertIn("codex", output.getvalue())
        self.assertIn("v0.1.0", output.getvalue())

    def test_sample_rejects_limit_above_policy_before_workbench(self):
        with tempfile.TemporaryDirectory() as temporary:
            task = create_workspace(
                root=Path(temporary),
                task_id="sample-limit",
                resource=ResourceRef(
                    kind="shared_query",
                    name="q",
                    project="analytics-project",
                    location="us",
                    display_name="q",
                    fingerprint="local-input",
                ),
                content=b"SELECT 1",
                filename="content.sql",
                mode="new",
            )
            errors = StringIO()
            with redirect_stderr(errors):
                status = main([
                    "sample",
                    "--task",
                    str(task),
                    "--limit",
                    "6",
                    "--approved-digest",
                    "anything",
                ])

        self.assertEqual(status, 2)
        self.assertIn("límite", errors.getvalue())

    def test_policy_check_blocks_scheduled_queries_in_modern_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                "active_profile = 'pilot'\n\n[profiles.pilot]\nmode = 'pilot'\n",
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stdout(output):
                status = main(
                    [
                        "policy",
                        "--config",
                        str(path),
                        "check",
                        "--operation",
                        "publish",
                        "--resource-kind",
                        "scheduled_query",
                        "--mode",
                        "new",
                        "--json",
                    ]
                )

        self.assertEqual(status, 2)
        self.assertIn('"allowed": false', output.getvalue())

    def test_policy_check_blocks_update_in_pilot_even_if_requested(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                "active_profile = 'pilot'\n\n[profiles.pilot]\nmode = 'pilot'\nallow_update_existing = true\n",
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stdout(output):
                status = main(
                    [
                        "policy",
                        "--config",
                        str(path),
                        "check",
                        "--operation",
                        "publish",
                        "--resource-kind",
                        "shared_query",
                        "--mode",
                        "update",
                        "--json",
                    ]
                )

        self.assertEqual(status, 2)
        self.assertIn('"allowed": false', output.getvalue())

    def test_validate_checks_source_allowlist_without_treating_source_as_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = create_workspace(
                root=root / "tasks",
                task_id="policy-validate",
                resource=ResourceRef(
                    kind="shared_query",
                    name="q",
                    project="source-project",
                    location="us",
                    display_name="q",
                    fingerprint="local-input",
                ),
                content=b"SELECT 1\n",
                filename="content.sql",
                mode="new",
            )
            path = root / "config.toml"
            path.write_text(
                "active_profile = 'pilot'\n\n[profiles.pilot]\n"
                "mode = 'pilot'\nsource_projects = ['source-project']\n"
                "destination_projects = ['analytics-project']\n",
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stdout(output):
                status = main(
                    [
                        "validate",
                        "--task",
                        str(task),
                        "--config",
                        str(path),
                        "--static-only",
                        "--json",
                    ]
                )

        self.assertEqual(status, 0)
        self.assertIn('"ok": true', output.getvalue())


if __name__ == "__main__":
    unittest.main()
