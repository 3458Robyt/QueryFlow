import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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

    def test_policy_check_allows_update_in_full_access_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                "active_profile = 'full-access'\n\n[profiles.full-access]\n"
                "mode = 'full-access'\nallow_update_existing = true\nallow_force_publish = true\n",
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

        self.assertEqual(status, 0)
        self.assertIn('"allowed": true', output.getvalue())

    def test_policy_check_requires_explicit_routine_migration_switch(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                "active_profile = 'migration-batch'\n\n[profiles.migration-batch]\n"
                "mode = 'migration-batch'\n",
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
                        "routine_campaign_publish",
                        "--resource-kind",
                        "routine",
                        "--json",
                    ]
                )

        self.assertEqual(status, 2)
        self.assertIn('"allowed": false', output.getvalue())

    def test_routine_review_cli_writes_local_html_without_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                "{"
                '"schema_version": 1, "campaign_id": "local-review", '
                '"destination_project": "destination-project", '
                '"destination_dataset": "functions", "inventory": {"google_sql_procedures": 0, "included_dependencies": 0}, '
                '"resources": []'
                "}",
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stdout(output):
                status = main(
                    [
                        "migration",
                        "routines",
                        "review",
                        "--manifest",
                        str(manifest_path),
                        "--json",
                    ]
                )
            self.assertEqual(status, 0)
            self.assertTrue((root / "review.html").is_file())
            self.assertIn('"review"', output.getvalue())

    def test_routine_inventory_cli_builds_manifest_and_reports_without_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_routine = {
                "routineReference": {
                    "projectId": "source-project",
                    "datasetId": "source_functions",
                    "routineId": "main_proc",
                },
                "routineType": "PROCEDURE",
                "language": "SQL",
                "definitionBody": "SELECT * FROM source-project.raw.table;",
            }
            dictionary = {
                "schema_version": 1,
                "dictionary_id": "cli-routes",
                "scope": {"source_project": "source-project"},
                "mappings": [
                    {
                        "id": "raw",
                        "zone": "raw",
                        "old": "source-project.raw",
                        "new": "raw-destination.raw",
                    }
                ],
            }
            dictionary_path = root / "routes.json"
            dictionary_path.write_text(json.dumps(dictionary), encoding="utf-8")
            config_path = root / "config.toml"
            config_path.write_text(
                "active_profile = 'migration-batch'\n\n"
                "[profiles.migration-batch]\n"
                "mode = 'migration-batch'\n"
                "allow_routine_migration = true\n"
                "routine_backend = 'direct'\n"
                "allowed_locations = ['us-east1']\n",
                encoding="utf-8",
            )

            class FakeClient:
                request_stats = SimpleNamespace(to_dict=lambda: {"requests_attempted": 0})

                def get_dataset(self, project, dataset):
                    return {
                        "datasetReference": {"projectId": project, "datasetId": dataset},
                        "location": "us-east1",
                        "access": [{"role": "READER", "entityId": "group:analysts"}],
                    }

                def list_datasets(self, project):
                    return [
                        {
                            "datasetReference": {
                                "projectId": project,
                                "datasetId": "source_functions",
                            },
                            "location": "us-east1",
                        }
                    ]

                def list_routines(self, project, dataset):
                    return [source_routine] if project == "source-project" else []

                def get_routine(self, project, dataset, routine_id):
                    return source_routine

            manifest_path = root / "manifest.json"
            output = StringIO()
            with patch("queryflow.cli._routine_client", return_value=(FakeClient(), None)):
                with redirect_stdout(output):
                    status = main(
                        [
                            "migration",
                            "routines",
                            "inventory",
                            "--source-project",
                            "source-project",
                            "--destination-project",
                            "destination-project",
                            "--dictionary",
                            str(dictionary_path),
                            "--output",
                            str(manifest_path),
                            "--config",
                            str(config_path),
                            "--account",
                            "analyst@example.com",
                            "--json",
                        ]
                    )
            self.assertEqual(status, 0)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(1, len(manifest["resources"]))
            self.assertEqual("raw-destination.raw.table", manifest["resources"][0]["proposal"]["definitionBody"].split("FROM ", 1)[1].rstrip(";"))
            self.assertEqual("read", manifest["permissions"]["source"][0]["status"])
            proposal = root / manifest["resources"][0]["proposal_file"]
            self.assertTrue(proposal.is_file())
            self.assertIn("raw-destination.raw.table", proposal.read_text(encoding="utf-8"))
            self.assertTrue((root / "report.md").is_file())

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
