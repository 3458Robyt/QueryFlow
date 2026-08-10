import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from queryflow.cli import main
from queryflow.errors import make_diagnostic
from queryflow.validation import ValidationResult
from queryflow.workspace import create_workspace
from queryflow.catalog import Catalog, ResourceRef
from queryflow.config import load_config
from queryflow.config_store import ConfigStore
from queryflow.review import build_review_model, render_review_model
from queryflow.state import task_state


class V2WorkflowTests(unittest.TestCase):
    def test_review_is_dark_first_and_keeps_the_diff_primary(self):
        html = render_review_model(
            {
                "manifest": {
                    "task_id": "ui",
                    "workflow_state": "blocked_vpc",
                    "resource": {"kind": "shared_query", "display_name": "q"},
                },
                "validation": {
                    "status": "blocked_vpc",
                    "error_kind": "vpc",
                    "dry_run": {"errors": ["VPC Service Controls"]},
                },
                "summary": {"lines_added": 1, "lines_removed": 1},
                "files": [
                    {
                        "path": "content.sql",
                        "label": "content.sql",
                        "before": "SELECT 1",
                        "after": "SELECT 2",
                        "added": 1,
                        "removed": 1,
                        "changed": True,
                    }
                ],
                "preferences": {
                    "review_theme": "dark",
                    "review_mode": "unified",
                    "review_only_changes": True,
                    "review_context_lines": 3,
                },
            }
        )
        self.assertIn("color-scheme: dark", html)
        self.assertIn('data-mode="unified"', html)
        self.assertIn("copy-diagnostic", html)
        self.assertNotIn('class="sidebar"', html)
        self.assertNotIn('class="workflow"', html)

    def test_diagnostic_is_redacted_and_preserves_vpc_identifier(self):
        diagnostic = make_diagnostic(
            "Request is prohibited by organization's policy (VPC Service Controls) "
            "vpcServiceControlsUniqueIdentifier: perimeter-abc123 "
            "Authorization: Bearer very-long-secret-token",
            stage="validate",
            context={"task_id": "demo", "account": "analyst@example.com"},
        )
        payload = diagnostic.to_dict()
        self.assertEqual("vpc", payload["category"])
        self.assertEqual(
            "perimeter-abc123",
            payload["provider"]["identifiers"]["vpcServiceControlsUniqueIdentifier"],
        )
        self.assertNotIn("very-long-secret-token", json.dumps(payload))
        self.assertIn("QF-", payload["error_id"])

        sql_payload = make_diagnostic(
            "Invalid query: SELECT secret_column FROM private_dataset.private_table",
            stage="validate",
        ).to_dict()
        self.assertNotIn("secret_column", json.dumps(sql_payload))
        self.assertIn("SQL_REDACTED", sql_payload["message"])

    def test_status_and_diagnose_expose_safe_task_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tasks"
            task = create_workspace(
                root=root,
                task_id="status-demo",
                resource=ResourceRef(
                    "shared_query", "q", "project", "us", "q", "local-input"
                ),
                content=b"SELECT 1;\n",
                filename="content.sql",
                mode="new",
            )
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(0, main(["status", "--task", str(task), "--json"]))
            status = json.loads(output.getvalue())
            self.assertEqual("draft", status["status"])
            self.assertEqual("shared_query", status["resource"]["kind"])

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    0, main(["diagnose", "--task", str(task), "--format", "json"])
                )
            diagnose = json.loads(output.getvalue())
            self.assertIsNone(diagnose["diagnostic"])

    def test_doctor_remote_probe_checks_apis_and_workbench_without_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            config.write_text(
                "active_profile = 'pilot'\n"
                "\n[profiles.pilot]\n"
                "validation_backend = 'workbench'\n"
                "workbench_project = 'workbench-project'\n"
                "workbench_location = 'us-east1-b'\n"
                "workbench_instance = 'instance'\n"
                "workbench_job_project = 'workbench-project'\n",
                encoding="utf-8",
            )

            def run(command, **kwargs):
                joined = " ".join(command)
                if "auth list" in joined:
                    return subprocess.CompletedProcess(
                        command, 0, "analyst@example.com\n", ""
                    )
                if "services list" in joined:
                    enabled = "\n".join(
                        [
                            "bigquery.googleapis.com",
                            "cloudasset.googleapis.com",
                            "dataform.googleapis.com",
                            "notebooks.googleapis.com",
                        ]
                    )
                    return subprocess.CompletedProcess(command, 0, enabled, "")
                if "workbench instances describe" in joined:
                    return subprocess.CompletedProcess(command, 0, "{}", "")
                return subprocess.CompletedProcess(command, 0, "", "")

            output = StringIO()
            with (
                patch("queryflow.cli.shutil.which", return_value="/usr/bin/tool"),
                patch("queryflow.cli.subprocess.run", side_effect=run),
                redirect_stdout(output),
            ):
                self.assertEqual(
                    0,
                    main(
                        [
                            "doctor",
                            "--config",
                            str(config),
                            "--probe-remote",
                            "--json",
                        ]
                    ),
                )
            report = json.loads(output.getvalue())
            self.assertTrue(report["apis"]["ok"])
            self.assertTrue(report["workbench_connectivity"]["ok"])
            self.assertTrue(report["remote_probe"]["ok"])

    def test_catalog_and_large_diff_stay_bounded(self):
        catalog = Catalog(
            resources=[
                ResourceRef(
                    "shared_query",
                    f"q-{index}",
                    "p",
                    "us",
                    f"sales-{index}",
                    f"h-{index}",
                )
                for index in range(5000)
            ],
            generated_at="now",
        )
        self.assertEqual(111, len(catalog.search("sales-11")))

        before = "\n".join(f"SELECT {index} AS value" for index in range(1000))
        after = before.replace("SELECT 500 AS value", "SELECT 500 + 1 AS value")
        model = build_review_model(
            {
                "task_id": "large-diff",
                "mode": "copy",
                "resource": {"kind": "shared_query", "display_name": "large"},
            },
            {"status": "ready", "ok": True, "publishable": True, "dry_run": {}},
            before.encode(),
            after.encode(),
            preferences={"review_context_lines": 3, "review_only_changes": True},
        )
        html = render_review_model(model)
        self.assertNotIn("SQL_REDACTED", html)
        self.assertIn("líneas de contexto ocultas", html)
        self.assertIn("table.unified .ctx", html)
        self.assertLess(len(html), 100_000)

    def test_v1_configuration_and_task_state_upgrade_without_losing_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(
                "schema_version = 1\nactive_profile = 'pilot'\n\n"
                "[profiles.pilot]\nmode = 'pilot'\nsource_projects = ['source-project']\n",
                encoding="utf-8",
            )
            document = ConfigStore(config_path).load()
            config = load_config(config_path)
        self.assertEqual(2, document.schema_version)
        self.assertEqual(("source-project",), config.source_projects)
        self.assertEqual("dark", config.review_theme)
        self.assertEqual(
            "ready",
            task_state(
                {
                    "schema_version": 1,
                    "proposed_sha256": "new",
                    "baseline_sha256": "old",
                },
                {"ok": True, "dry_run": {"dry_run_ok": True}},
            ),
        )

    def test_team_static_exception_has_independent_digest_and_publishes_code_only(self):
        class FakeClient:
            calls = 0

            def __init__(self, account, project):
                self.account = account
                self.project = project

            def create_copy(self, **kwargs):
                FakeClient.calls += 1
                return {
                    "repository": "projects/d/locations/us/repositories/exception-copy",
                    "commit_sha": "new",
                }

            def read_file(self, repository, filename):
                return b"SELECT 1;\n"

        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            root = temporary_path / "tasks"
            task = create_workspace(
                root=root,
                task_id="exception-demo",
                resource=ResourceRef(
                    "shared_query", "q", "d", "us", "q", "local-input"
                ),
                content=b"SELECT 1;\n",
                filename="content.sql",
                mode="new",
                account="analyst@example.com",
            )
            config = temporary_path / "config.toml"
            config.write_text(
                "active_profile = 'team'\n"
                "\n[profiles.team]\n"
                "mode = 'team'\n"
                "allow_static_exception = true\n"
                "validation_backend = 'local'\n"
                f"workspace_root = '{root}'\n"
                f"audit_root = '{temporary_path / 'audit'}'\n",
                encoding="utf-8",
            )
            blocked = ValidationResult(
                [],
                "read_only",
                True,
                dry_run_ok=False,
                errors=[
                    "Request is prohibited by organization's policy (VPC Service Controls) vpcServiceControlsUniqueIdentifier: perimeter-abc123"
                ],
                error_kind="vpc",
            )
            with patch("queryflow.cli.dry_run_sql", return_value=blocked):
                self.assertEqual(
                    2,
                    main(
                        [
                            "validate",
                            "--task",
                            str(task),
                            "--config",
                            str(config),
                            "--json",
                        ]
                    ),
                )
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    0,
                    main(
                        [
                            "exception",
                            "prepare",
                            "--task",
                            str(task),
                            "--reason",
                            "VPC temporalmente no disponible",
                            "--reference",
                            "SEC-1234",
                            "--config",
                            str(config),
                            "--json",
                        ]
                    ),
                )
            prepared = json.loads(output.getvalue())
            self.assertTrue(prepared["exception_digest"])
            self.assertTrue((task / "exception.json").exists())
            exception = json.loads(
                (task / "exception.json").read_text(encoding="utf-8")
            )
            self.assertNotEqual(
                exception["exception_digest"],
                json.loads((task / "manifest.json").read_text(encoding="utf-8")).get(
                    "approval_digest"
                ),
            )
            with patch("queryflow.cli.DataformClient", FakeClient):
                self.assertEqual(
                    0,
                    main(
                        [
                            "publish",
                            "--task",
                            str(task),
                            "--approved-exception-digest",
                            exception["exception_digest"],
                            "--destination-project",
                            "d",
                            "--account",
                            "analyst@example.com",
                            "--config",
                            str(config),
                            "--json",
                        ]
                    ),
                )
            self.assertEqual(1, FakeClient.calls)
            manifest = json.loads((task / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["published"])


if __name__ == "__main__":
    unittest.main()
