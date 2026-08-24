from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from queryflow.audit import AuditError, GcsAuditStore, LocalAuditStore
from queryflow.catalog import Catalog, ResourceRef, catalog_from_json, save_catalog
from queryflow.notebooks import analyze_sql_fragments, extract_code_cells, extract_sql_fragments, rebuild_notebook
from queryflow.config import load_config
from queryflow.validation import classify_error, dry_run_sql, dry_run_sql_fragments, validate_sql_text
from queryflow.workspace import WorkspaceError, create_workspace


class CatalogTests(unittest.TestCase):
    def test_catalog_round_trip_preserves_resource_identity(self) -> None:
        resource = ResourceRef(
            kind="shared_query",
            name="projects/analytics-project/locations/us-east1/repositories/q-sales",
            project="analytics-project",
            location="us-east1",
            display_name="q_sales",
            fingerprint="abc123",
            metadata={"owner": "user@example.com"},
        )
        catalog = Catalog(resources=[resource], generated_at="2026-07-31T00:00:00Z")
        restored = catalog_from_json(catalog.to_json())
        self.assertEqual(resource, restored.resources[0])

    def test_catalog_search_is_case_insensitive_and_type_filterable(self) -> None:
        catalog = Catalog(
            resources=[
                ResourceRef("notebook", "n1", "p", "us", "Siniestros_RCI", "h1"),
                ResourceRef("view", "v1", "p", "us", "siniestros_view", "h2"),
            ],
            generated_at="now",
        )
        result = catalog.search("SINIESTROS", kind="notebook")
        self.assertEqual(["n1"], [item.name for item in result])

    def test_refresh_catalog_classifies_bigquery_views_routines_and_transfers(self) -> None:
        from queryflow.catalog import refresh_catalog

        def runner(command):
            joined = " ".join(command)
            if "projects list" in joined:
                return [{"projectId": "p"}]
            if "dataform.googleapis.com/Repository" in joined:
                return []
            if "bigquery.googleapis.com/Table" in joined:
                return [{
                    "name": "//bigquery.googleapis.com/projects/p/datasets/d/tables/v",
                    "displayName": "v",
                    "additionalAttributes": {"tableType": "VIEW"},
                }]
            if "bigquery.googleapis.com/Routine" in joined:
                return [{"name": "//bigquery.googleapis.com/projects/p/datasets/d/routines/r", "displayName": "r"}]
            if "bigquerydatatransfer.googleapis.com/TransferConfig" in joined:
                return [{"name": "//bigquerydatatransfer.googleapis.com/projects/p/locations/us/transferConfigs/c", "displayName": "c"}]
            return []

        catalog = refresh_catalog(runner=runner)
        self.assertEqual({"view", "routine", "scheduled_query"}, {item.kind for item in catalog.resources})


class NotebookTests(unittest.TestCase):
    def notebook(self) -> bytes:
        return json.dumps(
            {
                "cells": [
                    {"cell_type": "markdown", "metadata": {}, "source": ["# title\n"]},
                    {
                        "cell_type": "code",
                        "execution_count": None,
                        "metadata": {"tag": "keep"},
                        "outputs": [],
                        "source": ["SELECT 1;\n", "SELECT 2;\n"],
                    },
                ],
                "metadata": {"kernelspec": {"name": "python3"}},
                "nbformat": 4,
                "nbformat_minor": 5,
            },
            ensure_ascii=False,
            indent=1,
        ).encode("utf-8")

    def test_extracts_only_code_cells(self) -> None:
        cells = extract_code_cells(self.notebook())
        self.assertEqual([(1, "sql", "SELECT 1;\nSELECT 2;\n")], cells)

    def test_extracts_sql_literals_from_python_notebook_cells(self) -> None:
        raw = json.dumps(
            {
                "metadata": {"kernelspec": {"language": "python"}},
                "cells": [
                    {
                        "cell_type": "code",
                        "source": "%%time\nsql = '''SELECT * FROM p.d.t'''\n",
                    },
                    {
                        "cell_type": "code",
                        "source": "df = client.query(sql).to_dataframe()\n",
                    },
                ],
            }
        ).encode("utf-8")
        self.assertEqual([(0, "SELECT * FROM p.d.t")], extract_sql_fragments(raw))

    def test_resolves_sql_variable_used_by_query_in_a_later_cell(self) -> None:
        raw = json.dumps(
            {
                "metadata": {"kernelspec": {"language": "python"}},
                "cells": [
                    {"cell_type": "code", "source": "sql = '''SELECT * FROM p.d.t'''\n"},
                    {"cell_type": "code", "source": "client.query(sql).to_dataframe()\n"},
                ],
            }
        ).encode("utf-8")
        extraction = analyze_sql_fragments(raw)
        self.assertEqual([(0, "SELECT * FROM p.d.t")], extraction.fragments)
        self.assertEqual([], extraction.dynamic_cells)
        self.assertNotIn("SELECT *", json.dumps(extraction.to_dict()))

    def test_marks_interpolated_sql_as_dynamic(self) -> None:
        raw = json.dumps(
            {
                "metadata": {"kernelspec": {"language": "python"}},
                "cells": [
                    {"cell_type": "code", "source": "table = 'p.d.t'\nsql = f'''SELECT * FROM {table}'''\n"},
                    {"cell_type": "code", "source": "client.query(sql)\n"},
                ],
            }
        ).encode("utf-8")
        extraction = analyze_sql_fragments(raw)
        self.assertEqual([0, 1], extraction.dynamic_cells)
        self.assertEqual([], extraction.fragments)

    def test_rebuild_changes_selected_cell_and_preserves_non_code_semantics(self) -> None:
        original = self.notebook()
        changed = rebuild_notebook(original, {1: "SELECT 3;\n"})
        before = json.loads(original)
        after = json.loads(changed)
        self.assertEqual(before["cells"][0], after["cells"][0])
        self.assertEqual("SELECT 3;\n", "".join(after["cells"][1]["source"]))
        self.assertEqual(before["metadata"], after["metadata"])

    def test_sync_notebook_task_rebuilds_content_from_changed_cell_file(self) -> None:
        from queryflow.task import sync_notebook_task

        with tempfile.TemporaryDirectory() as temp:
            task = Path(temp) / "task"
            task.mkdir()
            original = self.notebook()
            (task / "content.ipynb").write_bytes(original)
            (task / "manifest.json").write_text(
                json.dumps({"task_id": "t1", "resource": {"kind": "notebook"}, "filename": "content.ipynb"}),
                encoding="utf-8",
            )
            (task / "cells").mkdir()
            (task / "cells" / "0001.sql").write_text("SELECT 99;\n", encoding="utf-8")
            updated = sync_notebook_task(task, original)
            parsed = json.loads(updated)
            self.assertEqual("SELECT 99;\n", "".join(parsed["cells"][1]["source"]))

    def test_sync_notebook_task_does_not_reformat_unchanged_content(self) -> None:
        from queryflow.task import sync_notebook_task
        from queryflow.workspace import create_workspace

        with tempfile.TemporaryDirectory() as temp:
            original = self.notebook()
            task = create_workspace(
                root=Path(temp),
                task_id="t1",
                resource=ResourceRef("notebook", "n1", "p", "us", "n1", "h1"),
                content=original,
                filename="content.ipynb",
            )
            write = task / "cells"
            write.mkdir()
            cells = extract_code_cells(original)
            (write / "0001.sql").write_text(cells[0][2], encoding="utf-8")
            subprocess = __import__("subprocess")
            subprocess.run(["git", "-C", str(task), "add", "cells"], check=True)
            subprocess.run(
                ["git", "-C", str(task), "-c", "user.name=QueryFlow", "-c", "user.email=queryflow@localhost", "commit", "--quiet", "-m", "cells"],
                check=True,
            )
            self.assertEqual(original, sync_notebook_task(task))


class ValidationTests(unittest.TestCase):
    def test_classifies_vpc_and_permission_errors(self) -> None:
        self.assertEqual("vpc", classify_error("Request is prohibited by organization's policy (VPC Service Controls)"))
        self.assertEqual("permission", classify_error("Access Denied: Permission denied while creating job"))
        self.assertEqual("transport", classify_error("ServerNotFoundError('Unable to find the server')"))

    def test_extracts_fully_qualified_references_without_rewriting_them(self) -> None:
        result = validate_sql_text(
            "SELECT * FROM `project_a.dataset_a.table_a` JOIN project_b.dataset_b.table_b USING (id)"
        )
        self.assertEqual(
            ["project_a.dataset_a.table_a", "project_b.dataset_b.table_b"],
            result.references,
        )
        self.assertEqual("read_only", result.statement_class)
        self.assertIn("project_a.dataset_a.table_a", result.references)

    def test_flags_mutating_sql_for_optional_execution(self) -> None:
        result = validate_sql_text("CREATE OR REPLACE TABLE p.d.t AS SELECT 1")
        self.assertEqual("mutating", result.statement_class)
        self.assertFalse(result.read_only)

    def test_flags_dml_hidden_after_with_for_optional_execution(self) -> None:
        result = validate_sql_text(
            "WITH source AS (SELECT 1 AS id) DELETE FROM p.d.t WHERE id IN (SELECT id FROM source)"
        )
        self.assertEqual("mutating", result.statement_class)
        self.assertFalse(result.read_only)

    def test_read_only_execution_requires_no_mutating_statement(self) -> None:
        from queryflow.validation import execute_read_only_sql

        calls = []

        def runner(command):
            calls.append(command)
            return 0, '[{"ok": 1}]', ""

        result = execute_read_only_sql("SELECT 1", runner=runner)
        self.assertEqual([{"ok": 1}], result.rows)
        self.assertIn("--max_rows=20", calls[0])

    def test_dry_run_passes_billing_project_id(self) -> None:
        calls = []

        def runner(command):
            calls.append(command)
            return 0, '{"totalBytesProcessed": "123"}', ""

        result = dry_run_sql("SELECT 1", project_id="analytics-project", runner=runner)
        self.assertTrue(result.dry_run_ok)
        self.assertIn("--project_id=analytics-project", calls[0])

    def test_dry_run_uses_explicit_account_token_without_global_bq_config(self) -> None:
        calls = []

        def runner(command):
            calls.append(command)
            return 0, '{"totalBytesProcessed": "0"}', ""

        result = dry_run_sql(
            "SELECT 1",
            account="user@example.com",
            token_provider=lambda account: "token-for-test",
            runner=runner,
        )
        self.assertTrue(result.dry_run_ok)
        self.assertIn("--use_google_auth=false", calls[0])
        self.assertIn("--oauth_access_token=token-for-test", calls[0])

    def test_dry_run_validates_each_notebook_sql_fragment(self) -> None:
        calls = []

        def runner(command):
            calls.append(command)
            return 0, '{"totalBytesProcessed": "123"}', ""

        result = dry_run_sql_fragments(
            [(4, "SELECT 1"), (9, "SELECT 2")],
            project_id="analytics-project",
            runner=runner,
        )
        self.assertTrue(result.dry_run_ok)
        self.assertEqual(2, len(calls))
        self.assertTrue(all("--project_id=analytics-project" in command for command in calls))
        self.assertEqual(246, result.bytes_processed)

    def test_dry_run_warns_when_estimate_exceeds_configured_limit(self) -> None:
        def runner(command):
            return 0, '{"statistics":{"query":{"totalBytesProcessed":"2048"}}}', ""

        result = dry_run_sql("SELECT 1", maximum_bytes_billed=1024, runner=runner)
        self.assertTrue(result.dry_run_ok)
        self.assertEqual(2048, result.bytes_processed)
        self.assertFalse(result.within_configured_limit)
        self.assertEqual(1024, result.maximum_bytes_billed)
        self.assertTrue(any("límite" in warning.lower() for warning in result.warnings))

    def test_workbench_summary_preserves_hashes_and_cost_warning(self) -> None:
        from queryflow.workbench import parse_workbench_summary

        summary = {
            "ok": True,
            "project": "source-project",
            "location": "us-east1",
            "fragments": [
                {
                    "cell": 4,
                    "sha256": hashlib.sha256(b"SELECT 1").hexdigest(),
                    "ok": True,
                    "dry_run": True,
                    "bytes_processed": 2048,
                    "bytes_billed": 0,
                    "error": None,
                }
            ],
        }
        result = parse_workbench_summary(summary, [(4, "SELECT 1")], maximum_bytes_billed=1024)
        self.assertTrue(result.dry_run_ok)
        self.assertFalse(result.within_configured_limit)
        self.assertEqual(2048, result.fragments[0].bytes_processed)
        self.assertEqual(0, result.fragments[0].bytes_billed)
        self.assertTrue(any("límite" in warning.lower() for warning in result.warnings))

    def test_workbench_summary_classifies_remote_failures(self) -> None:
        from queryflow.workbench import parse_workbench_summary

        sql = "SELECT * FROM p.d.missing"
        summary = {
            "fragments": [{
                "cell": 4,
                "sha256": hashlib.sha256(sql.encode()).hexdigest(),
                "ok": False,
                "dry_run": False,
                "error": "Access Denied: Permission denied while creating job",
            }],
        }
        result = parse_workbench_summary(summary, [(4, sql)], maximum_bytes_billed=1024)
        self.assertEqual("permission", result.error_kind)
        self.assertEqual("permission", result.fragments[0].error_kind)

    def test_workbench_remote_code_is_valid_python_and_uses_bq_dry_run(self) -> None:
        from queryflow.workbench import _remote_code

        code = _remote_code(
            {
                "job_project": "p",
                "location": "us-east1",
                "maximum_bytes_billed": 1024,
                "fragments": [{"cell": 4, "sql": "SELECT 1", "sha256": "h"}],
            }
        )
        compile(code, "remote-validation.py", "exec")
        self.assertIn('"--dry_run"', code)
        self.assertIn('"--project_id={payload[\'job_project\']}"', code)

    def test_workbench_zonal_location_is_reduced_to_bigquery_region(self) -> None:
        from queryflow.workbench import _bigquery_location

        self.assertEqual("us-east1", _bigquery_location("us-east1-b"))
        self.assertEqual("europe-west4", _bigquery_location("europe-west4"))

    def test_workbench_authentication_failure_becomes_validation_evidence(self) -> None:
        from queryflow.workbench import WorkbenchError, WorkbenchSettings, validate_workbench_fragments

        settings = WorkbenchSettings("p", "us-east1-b", "instance", "jobs")
        with patch(
            "queryflow.workbench._access_token",
            side_effect=WorkbenchError("token expirado", kind="permission"),
        ):
            result = validate_workbench_fragments(
                [(4, "SELECT 1")], settings, maximum_bytes_billed=1024,
            )
        self.assertFalse(result.result.dry_run_ok)
        self.assertEqual("permission", result.result.error_kind)

    def test_workbench_proxy_discovery_uses_explicit_account(self) -> None:
        from queryflow.workbench import WorkbenchSettings, discover_proxy

        settings = WorkbenchSettings("p", "us-east1-b", "instance", "jobs")
        with patch(
            "queryflow.workbench._gcloud_json",
            return_value={"proxyUri": "https://workbench.example"},
        ) as describe:
            self.assertEqual("https://workbench.example", discover_proxy(settings, account="a@example.com"))
        self.assertIn("--account=a@example.com", describe.call_args.args[0])

    def test_workbench_uses_active_gcloud_account_when_no_account_is_passed(self) -> None:
        from types import SimpleNamespace
        from queryflow.workbench import _access_token

        with patch(
            "queryflow.workbench.subprocess.run",
            side_effect=[
                SimpleNamespace(returncode=0, stdout="a@example.com\n", stderr=""),
                SimpleNamespace(returncode=0, stdout="token\n", stderr=""),
            ],
        ) as run:
            self.assertEqual("token", _access_token(None))
        self.assertEqual(["gcloud", "--account=a@example.com", "auth", "print-access-token"], run.call_args.args[0])


class ConfigTests(unittest.TestCase):
    def test_load_config_reads_shared_workbench_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "queryflow.yaml"
            path.write_text(
                "\n".join(
                    [
                        "validation_backend: workbench",
                        "workbench_project: source-project",
                        "workbench_location: us-east1-b",
                        "workbench_instance: sbs-analytics-python-notebook",
                        "workbench_job_project: source-project",
                        "workbench_timeout_seconds: 240",
                    ]
                ),
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual("workbench", config.validation_backend)
            self.assertEqual("sbs-analytics-python-notebook", config.workbench_instance)
            self.assertEqual(240, config.workbench_timeout_seconds)

    def test_load_config_reads_dataset_creation_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "queryflow.yaml"
            path.write_text("allow_create_dataset: true\n", encoding="utf-8")
            config = load_config(path)
            self.assertTrue(config.allow_create_dataset)


class ScheduleTests(unittest.TestCase):
    def test_schedule_spec_accepts_daily_off_hour_and_is_stable(self) -> None:
        from queryflow.schedule import ScheduleSpec

        spec = ScheduleSpec.from_values(
            schedule="every day 14:03",
            location="us-east1",
            destination_dataset="queryflow_pilot",
            destination_table="scheduled_query_heartbeat",
        )
        self.assertTrue(spec.disabled)
        self.assertEqual("WRITE_APPEND", spec.write_disposition)
        self.assertEqual(
            {
                "schedule": "every day 14:03",
                "location": "us-east1",
                "destination_dataset": "queryflow_pilot",
                "destination_table": "scheduled_query_heartbeat",
                "write_disposition": "WRITE_APPEND",
                "disabled": True,
            },
            spec.to_dict(),
        )

    def test_schedule_spec_rejects_unsafe_values(self) -> None:
        from queryflow.schedule import ScheduleError, ScheduleSpec

        with self.assertRaises(ScheduleError):
            ScheduleSpec.from_values(
                schedule="",
                location="us-east1",
                destination_dataset="queryflow_pilot",
                destination_table="scheduled_query_heartbeat",
            )
        with self.assertRaises(ScheduleError):
            ScheduleSpec.from_values(
                schedule="every day 14:03",
                location="us-east1",
                destination_dataset="queryflow-pilot",
                destination_table="scheduled_query_heartbeat",
            )
        with self.assertRaises(ScheduleError):
            ScheduleSpec.from_values(
                schedule="every day 14:03",
                location="us-east1",
                destination_dataset="queryflow_pilot",
                destination_table="scheduled_query_heartbeat",
                write_disposition="WRITE_TRUNCATE",
            )
        with self.assertRaises(ScheduleError):
            ScheduleSpec.from_values(
                schedule="every day 14:03",
                location="us-east1",
                destination_dataset="queryflow_pilot",
                destination_table="scheduled_query_heartbeat",
                disabled=False,
            )


class TransferTests(unittest.TestCase):
    def test_ensure_dataset_is_idempotent_when_dataset_exists(self) -> None:
        from queryflow.transfer import TransferClient

        calls = []

        def transport(method, resource, query, body):
            calls.append((method, resource, query, body))
            return {"datasetReference": {"projectId": "p", "datasetId": "d"}, "location": "us-east1"}

        result = TransferClient("user@example.com", "p", transport=transport).ensure_dataset(
            "p", "d", "us-east1", {"queryflow_pilot": "true"}
        )
        self.assertEqual("d", result["datasetReference"]["datasetId"])
        self.assertEqual(["GET"], [call[0] for call in calls])

    def test_ensure_dataset_creates_only_when_missing(self) -> None:
        from queryflow.transfer import TransferClient, TransferNotFound

        calls = []

        def transport(method, resource, query, body):
            calls.append((method, resource, query, body))
            if method == "GET":
                raise TransferNotFound(resource)
            return {"datasetReference": {"projectId": "p", "datasetId": "d"}, "location": "us-east1"}

        result = TransferClient("user@example.com", "p", transport=transport).ensure_dataset(
            "p", "d", "us-east1", {"queryflow_pilot": "true"}
        )
        self.assertEqual("us-east1", result["location"])
        self.assertEqual(["GET", "POST"], [call[0] for call in calls])
        self.assertEqual("us-east1", calls[1][3]["location"])
        self.assertEqual("true", calls[1][3]["labels"]["queryflow_pilot"])

    def test_transfer_client_rejects_writes_outside_allowed_project_before_transport(self) -> None:
        from queryflow.transfer import TransferClient, TransferError

        client = TransferClient("user@example.com", "p", transport=lambda *args: self.fail("transport called"))
        with self.assertRaises(TransferError):
            client.ensure_dataset("other", "d", "us-east1", {})

    def test_create_scheduled_query_sends_disabled_safe_body(self) -> None:
        from queryflow.transfer import TransferClient

        calls = []

        def transport(method, resource, query, body):
            calls.append((method, resource, query, body))
            return {"name": "projects/p/locations/us-east1/transferConfigs/c1", "disabled": True}

        result = TransferClient("user@example.com", "p", transport=transport).create_scheduled_query(
            project="p",
            location="us-east1",
            display_name="qflow_daily_heartbeat",
            query="SELECT 1",
            destination_dataset="queryflow_pilot",
            destination_table="scheduled_query_heartbeat",
            schedule="every day 14:03",
        )
        self.assertEqual("projects/p/locations/us-east1/transferConfigs/c1", result["name"])
        body = calls[0][3]
        self.assertEqual("scheduled_query", body["dataSourceId"])
        self.assertEqual("queryflow_pilot", body["destinationDatasetId"])
        self.assertEqual("SELECT 1", body["params"]["query"])
        self.assertEqual("scheduled_query_heartbeat", body["params"]["destination_table_name_template"])
        self.assertEqual("WRITE_APPEND", body["params"]["write_disposition"])
        self.assertEqual("every day 14:03", body["schedule"])
        self.assertTrue(body["disabled"])

    def test_create_scheduled_query_requires_remote_name(self) -> None:
        from queryflow.transfer import TransferClient, TransferError

        client = TransferClient("user@example.com", "p", transport=lambda *args: {})
        with self.assertRaises(TransferError):
            client.create_scheduled_query(
                project="p",
                location="us-east1",
                display_name="qflow_daily_heartbeat",
                query="SELECT 1",
                destination_dataset="queryflow_pilot",
                destination_table="scheduled_query_heartbeat",
                schedule="every day 14:03",
            )


class StateTests(unittest.TestCase):
    def test_normalizes_workbench_error_kinds_to_safe_states(self) -> None:
        from queryflow.state import normalize_validation_status

        self.assertEqual(
            "blocked_vpc",
            normalize_validation_status({"error_kind": "vpc", "dry_run": {"dry_run_ok": False}}),
        )
        self.assertEqual(
            "blocked_permission",
            normalize_validation_status({"error_kind": "permission", "dry_run": {"dry_run_ok": False}}),
        )
        self.assertEqual(
            "changes_required",
            normalize_validation_status({"error_kind": "sql", "dry_run": {"dry_run_ok": False}}),
        )


class ProfileTests(unittest.TestCase):
    def test_profile_query_contains_only_aggregates_and_no_raw_values(self) -> None:
        from queryflow.profile import build_profile_query

        query = build_profile_query(
            "p.d.table",
            [
                {"name": "id", "type": "INT64"},
                {"name": "name", "type": "STRING"},
                {"name": "created", "type": "DATE"},
            ],
        )
        self.assertIn("COUNT(*)", query)
        self.assertIn("APPROX_COUNT_DISTINCT", query)
        self.assertIn("MIN(`created`)", query)
        self.assertNotIn("SELECT `name`", query)


class WorkspaceTests(unittest.TestCase):
    def test_workspace_has_local_git_baseline_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = create_workspace(
                root=root,
                task_id="task-001",
                resource=ResourceRef("shared_query", "r1", "p", "us", "query", "h0"),
                content=b"SELECT 1;\n",
                filename="query.sql",
            )
            self.assertTrue((task / ".git").exists())
            self.assertTrue((task / "manifest.json").exists())
            self.assertEqual("SELECT 1;\n", (task / "query.sql").read_text())
            self.assertEqual(0, __import__("subprocess").run(["git", "-C", str(task), "status", "--porcelain"], capture_output=True, text=True).stdout.strip().__len__())

    def test_workspace_refuses_path_traversal_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(WorkspaceError):
                create_workspace(
                    root=Path(temp),
                    task_id="../escape",
                    resource=ResourceRef("view", "r1", "p", "us", "view", "h0"),
                    content=b"SELECT 1;\n",
                    filename="query.sql",
                )


class AuditTests(unittest.TestCase):
    def test_local_audit_store_writes_manifest_and_content_without_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task = Path(temp) / "task"
            task.mkdir()
            (task / "manifest.json").write_text('{"task_id":"t1"}', encoding="utf-8")
            (task / "query.sql").write_text("SELECT 1;\n", encoding="utf-8")
            (task / "validation.json").write_text('{"ok":true}', encoding="utf-8")
            destination = Path(temp) / "audit"
            receipt = LocalAuditStore(destination).archive(task)
            self.assertTrue((destination / "t1" / "manifest.json").exists())
            self.assertEqual("t1", receipt["task_id"])

    def test_audit_requires_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task = Path(temp) / "task"
            task.mkdir()
            with self.assertRaises(AuditError):
                LocalAuditStore(Path(temp) / "audit").archive(task)

    def test_gcs_audit_uploads_task_files_without_git_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task = Path(temp) / "task"
            task.mkdir()
            (task / "manifest.json").write_text('{"task_id":"t1"}', encoding="utf-8")
            (task / "query.sql").write_text("SELECT 1;\n", encoding="utf-8")
            (task / ".git").mkdir()
            (task / ".git" / "config").write_text("secret", encoding="utf-8")
            commands = []
            store = GcsAuditStore("gs://bucket/audit", runner=lambda command: commands.append(command) or 0)
            receipt = store.archive(task)
            self.assertEqual("t1", receipt["task_id"])
            self.assertTrue(any("query.sql" in " ".join(command) for command in commands))
            self.assertFalse(any(".git" in " ".join(command) for command in commands))


class ReviewTests(unittest.TestCase):
    def test_review_html_escapes_sql(self) -> None:
        from queryflow.review import render_review_html

        html = render_review_html("SELECT '<unsafe>';", "SELECT '<changed>';", {"ok": True})
        self.assertIn("&lt;", html)
        self.assertNotIn("<changed>", html)

    def test_review_model_groups_notebook_cells_and_counts_lines(self) -> None:
        from queryflow.review import build_review_model

        before = json.dumps(
            {
                "cells": [
                    {"cell_type": "markdown", "metadata": {}, "source": ["# title\n"]},
                    {"cell_type": "code", "metadata": {}, "source": ["SELECT 1\n"]},
                ],
                "metadata": {"kernelspec": {"language": "sql"}},
            }
        ).encode()
        after = json.dumps(
            {
                "cells": [
                    {"cell_type": "markdown", "metadata": {}, "source": ["# title\n"]},
                    {"cell_type": "code", "metadata": {}, "source": ["SELECT 2\n", "WHERE TRUE\n"]},
                    {"cell_type": "code", "metadata": {}, "source": ["SELECT 3\n"]},
                ],
                "metadata": {"kernelspec": {"language": "sql"}},
            }
        ).encode()
        model = build_review_model(
            {
                "task_id": "t1",
                "mode": "copy",
                "workflow_state": "changed",
                "resource": {"kind": "notebook", "display_name": "n", "location": "us"},
            },
            {"status": "prechecked", "method": "static", "publishable": False},
            before,
            after,
        )
        self.assertEqual(3, len(model["files"]))
        self.assertEqual(2, model["summary"]["files_changed"])
        self.assertGreaterEqual(model["summary"]["lines_added"], 2)

    def test_review_server_exposes_only_read_only_review_routes(self) -> None:
        from queryflow.review_server import serve_review
        from queryflow.workspace import create_workspace
        from queryflow.catalog import ResourceRef
        import threading
        from urllib.request import urlopen
        from urllib.error import HTTPError

        with tempfile.TemporaryDirectory() as temp:
            task = create_workspace(
                root=Path(temp),
                task_id="t-review",
                resource=ResourceRef("shared_query", "r1", "p", "us", "q", "h0"),
                content=b"SELECT 1\n",
                filename="query.sql",
            )
            (task / "query.sql").write_text("SELECT 2\n", encoding="utf-8")
            server, _url = serve_review(task, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                page_response = urlopen(base + "/review.html")
                page = page_response.read().decode("utf-8")
                self.assertEqual("nosniff", page_response.headers["X-Content-Type-Options"])
                self.assertEqual("DENY", page_response.headers["X-Frame-Options"])
                self.assertIn("default-src 'none'", page_response.headers["Content-Security-Policy"])
                api = json.loads(urlopen(base + "/api/review").read())
                self.assertIn("diff_add", page)
                self.assertEqual(1, api["summary"]["files_changed"])
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + "/manifest.json")
                self.assertEqual(404, error.exception.code)
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()

    def test_review_model_exposes_guided_workflow_and_changed_filter(self) -> None:
        from queryflow.review import build_review_model, render_review_model

        manifest = {
            "task_id": "t-guided",
            "mode": "copy",
            "workflow_state": "ready",
            "validation_status": "ready",
            "resource": {"kind": "shared_query", "display_name": "q", "location": "us-east1"},
            "proposed_sha256": "new",
        }
        validation = {
            "status": "ready",
            "method": "bigquery_dry_run",
            "backend": "workbench",
            "publishable": True,
            "ok": True,
            "dry_run": {"dry_run_ok": True, "within_configured_limit": False, "warnings": ["límite"]},
        }
        before = b"SELECT 1\n"
        after = b"SELECT 2\n"
        model = build_review_model(manifest, validation, before, after)
        self.assertEqual(["Edición", "Validación", "Muestra", "Aprobación", "Publicación"], [step["label"] for step in model["workflow_steps"]])
        rendered = render_review_model(model)
        self.assertIn("skip-link", rendered)
        self.assertIn("Mostrar solo cambios", rendered)
        self.assertIn("prefers-color-scheme", rendered)

    def test_review_shows_bounded_sample_metadata_without_row_values(self) -> None:
        from queryflow.review import write_review_html
        from queryflow.workspace import create_workspace
        from queryflow.catalog import ResourceRef

        with tempfile.TemporaryDirectory() as temp:
            task = create_workspace(
                root=Path(temp),
                task_id="t-sample-review",
                resource=ResourceRef("shared_query", "q", "analytics-project", "us", "q", "h0"),
                content=b"SELECT 1\n",
                filename="query.sql",
            )
            (task / "query.sql").write_text("SELECT 1 WHERE TRUE\n", encoding="utf-8")
            (task / "sample-receipt.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "execution_digest": "a" * 64,
                        "limit": 3,
                        "row_count": 2,
                        "columns": ["id", "status"],
                        "truncated": False,
                        "executed_at": "2026-08-04T00:00:00Z",
                        "rows": [{"secret": "must-not-render"}],
                    }
                ),
                encoding="utf-8",
            )
            rendered = write_review_html(
                task,
                b"SELECT 1\n",
                b"SELECT 1 WHERE TRUE\n",
                {"status": "ready", "publishable": True, "ok": True},
            ).read_text(encoding="utf-8")

        self.assertIn("Muestra", rendered)
        self.assertIn("aaaaaaaaaaaaaaaa", rendered)
        self.assertIn("2 filas", rendered)
        self.assertNotIn("must-not-render", rendered)

    def test_review_model_uses_scheduled_query_filename(self) -> None:
        from queryflow.review import build_review_model

        model = build_review_model(
            {
                "task_id": "scheduled",
                "mode": "new",
                "filename": "heartbeat.sql",
                "resource": {"kind": "scheduled_query", "display_name": "heartbeat", "location": "us-east1"},
            },
            {},
            b"",
            b"SELECT 1\n",
        )
        self.assertEqual("heartbeat.sql", model["files"][0]["path"])

    def test_scheduled_query_review_does_not_call_sql_file_a_fragment(self) -> None:
        from queryflow.review import render_review_model

        model = {
            "manifest": {"task_id": "scheduled", "workflow_state": "blocked_permission", "resource": {"kind": "scheduled_query"}},
            "validation": {"status": "blocked_permission", "dry_run": {"bytes_processed": None}},
            "files": [],
            "summary": {"files_changed": 1, "lines_added": 1, "lines_removed": 0},
            "workflow_steps": [],
        }
        html = render_review_model(model)
        self.assertIn("consulta SQL", html)
        self.assertNotIn("sin fragmentos SQL extraíbles", html)

    def test_scheduled_query_workflow_describes_disabled_creation(self) -> None:
        from queryflow.review import build_review_model

        model = build_review_model(
            {
                "task_id": "scheduled",
                "mode": "new",
                "filename": "heartbeat.sql",
                "resource": {"kind": "scheduled_query", "display_name": "heartbeat", "location": "us-east1"},
            },
            {},
            b"",
            b"SELECT 1\n",
        )
        self.assertIn("deshabilitada", model["workflow_steps"][-1]["description"])


class ApprovalTests(unittest.TestCase):
    def test_approval_digest_changes_when_validation_changes(self) -> None:
        from queryflow.task import approval_digest

        first = approval_digest({"task_id": "t1", "proposed_sha256": "a"}, {"dry_run_ok": True})
        second = approval_digest({"task_id": "t1", "proposed_sha256": "a"}, {"dry_run_ok": False})
        self.assertNotEqual(first, second)

    def test_static_revalidation_removes_previous_approval_digest(self) -> None:
        from queryflow.catalog import ResourceRef
        from queryflow.task import mark_validation
        from queryflow.workspace import create_workspace, read_manifest

        with tempfile.TemporaryDirectory() as temp:
            task = create_workspace(
                root=Path(temp),
                task_id="digest-reset",
                resource=ResourceRef("shared_query", "q1", "p", "us", "q1", "h1"),
                content=b"SELECT 1;\n",
                filename="query.sql",
            )
            manifest = read_manifest(task)
            manifest["approval_digest"] = "old-digest"
            (task / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            result = mark_validation(
                task,
                {
                    "ok": True,
                    "static": {"errors": []},
                    "dry_run": {"skipped": True},
                },
            )
            self.assertNotIn("approval_digest", result)
            self.assertNotIn("approval_digest", read_manifest(task))


class DataformTests(unittest.TestCase):
    def test_export_reads_only_the_selected_code_file(self) -> None:
        from queryflow.dataform import DataformClient

        calls = []

        def transport(method, resource, query, body):
            calls.append((method, resource, query, body))
            if resource.endswith(":queryDirectoryContents"):
                return {"directoryEntries": [{"file": "content.sql"}, {"file": "actions.yaml"}]}
            if resource.endswith(":readFile"):
                return {"contents": "U0VMRUNUIDE7Cg=="}
            if resource.endswith(":fetchHistory"):
                return {"commits": [{"commitSha": "head"}]}
            return {"labels": {"single-file-asset-type": "sql"}, "displayName": "query"}

        resource = ResourceRef(
            "shared_query",
            "projects/p/locations/us/repositories/r",
            "p",
            "us",
            "query",
            "head",
        )
        asset = DataformClient("user@example.com", "p", transport=transport).export(resource)
        self.assertEqual("content.sql", asset.filename)
        self.assertEqual(b"SELECT 1;\n", asset.content)
        self.assertTrue(all(call[0] == "GET" for call in calls))

    def test_write_to_source_project_is_rejected_before_transport(self) -> None:
        from queryflow.dataform import DataformClient, DataformError

        client = DataformClient("user@example.com", "dest", transport=lambda *args: self.fail("transport called"))
        with self.assertRaises(DataformError):
            client.request("POST", "projects/source/locations/us/repositories/r", {}, {})

    def test_update_file_sends_required_head_and_one_file_operation(self) -> None:
        from queryflow.dataform import DataformClient

        calls = []

        def transport(method, resource, query, body):
            calls.append((method, resource, query, body))
            return {"commitSha": "updated"}

        client = DataformClient("user@example.com", "p", transport=transport)
        result = client.update_file(
            "projects/p/locations/us/repositories/r",
            "content.ipynb",
            b"{}",
            required_head_commit="old",
            author_name="QueryFlow",
            author_email="user@example.com",
        )

        self.assertEqual(
            {
                "repository": "projects/p/locations/us/repositories/r",
                "filename": "content.ipynb",
                "commit_sha": "updated",
            },
            result,
        )
        self.assertEqual("POST", calls[0][0])
        self.assertEqual("old", calls[0][3]["requiredHeadCommitSha"])
        self.assertEqual(["content.ipynb"], list(calls[0][3]["fileOperations"]))


class CliTests(unittest.TestCase):
    def test_start_new_scheduled_query_writes_stable_schedule_spec(self) -> None:
        from queryflow.cli import main

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "tasks"
            content = Path(temp) / "heartbeat.sql"
            content.write_text(
                "SELECT @run_time AS scheduled_for_utc, @run_date AS scheduled_date_utc;\n",
                encoding="utf-8",
            )
            result = main(
                [
                    "start",
                    "--mode",
                    "new",
                    "--kind",
                    "scheduled_query",
                    "--name",
                    "qflow_daily_heartbeat",
                    "--project",
                    "analytics-project",
                    "--location",
                    "us-east1",
                    "--content-file",
                    str(content),
                    "--schedule",
                    "every day 14:03",
                    "--target-dataset",
                    "queryflow_pilot",
                    "--destination-table",
                    "scheduled_query_heartbeat",
                    "--task-id",
                    "scheduled-pilot",
                    "--workspace-root",
                    str(root),
                    "--json",
                ]
            )
            self.assertEqual(0, result)
            task = root / "scheduled-pilot"
            manifest = json.loads((task / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("scheduled_query", manifest["resource"]["kind"])
            self.assertEqual(hashlib.sha256(b"").hexdigest(), manifest["baseline_sha256"])
            self.assertNotEqual(manifest["baseline_sha256"], manifest["proposed_sha256"])
            self.assertEqual("SELECT @run_time AS scheduled_for_utc, @run_date AS scheduled_date_utc;\n", (task / "heartbeat.sql").read_text(encoding="utf-8"))
            self.assertEqual(
                {
                    "schedule": "every day 14:03",
                    "location": "us-east1",
                    "destination_dataset": "queryflow_pilot",
                    "destination_table": "scheduled_query_heartbeat",
                    "write_disposition": "WRITE_APPEND",
                    "disabled": True,
                },
                manifest["schedule_spec"],
            )

    def test_publish_scheduled_query_creates_disabled_transfer_and_audit(self) -> None:
        from queryflow.cli import main
        from queryflow.validation import ValidationResult

        class FakeTransfer:
            instances = []

            def __init__(self, account, project):
                self.account = account
                self.project = project
                self.ensure_calls = []
                self.create_calls = []
                self.transfer = None
                self.__class__.instances.append(self)

            def ensure_dataset(self, project, dataset_id, location, labels):
                self.ensure_calls.append((project, dataset_id, location, labels))
                return {
                    "datasetReference": {"projectId": project, "datasetId": dataset_id},
                    "location": location,
                }

            def create_scheduled_query(self, **kwargs):
                self.create_calls.append(kwargs)
                self.transfer = {
                    "name": "projects/analytics-project/locations/us-east1/transferConfigs/c1",
                    "displayName": kwargs["display_name"],
                    "dataSourceId": "scheduled_query",
                    "destinationDatasetId": kwargs["destination_dataset"],
                    "params": {
                        "query": kwargs["query"],
                        "destination_table_name_template": kwargs["destination_table"],
                        "write_disposition": kwargs["write_disposition"],
                    },
                    "schedule": kwargs["schedule"],
                    "disabled": True,
                }
                return self.transfer

            def get_transfer_config(self, name):
                if name != self.transfer["name"]:
                    raise AssertionError(f"unexpected transfer name: {name}")
                return self.transfer

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "tasks"
            content = Path(temp) / "heartbeat.sql"
            content.write_text("SELECT @run_time AS scheduled_for_utc;\n", encoding="utf-8")
            config = Path(temp) / "pilot.yaml"
            config.write_text(
                f"workspace_root: {root}\naudit_root: {Path(temp) / 'audit'}\nallow_create_dataset: true\n",
                encoding="utf-8",
            )
            self.assertEqual(
                0,
                main(
                    [
                        "start", "--mode", "new", "--kind", "scheduled_query",
                        "--name", "qflow_daily_heartbeat", "--project", "analytics-project",
                        "--location", "us-east1", "--content-file", str(content),
                        "--schedule", "every day 14:03", "--target-dataset", "queryflow_pilot",
                        "--destination-table", "scheduled_query_heartbeat", "--task-id", "scheduled-publish",
                        "--config", str(config), "--json",
                    ]
                ),
            )
            task = root / "scheduled-publish"
            with patch(
                "queryflow.cli.dry_run_sql",
                return_value=ValidationResult([], "read_only", True, dry_run_ok=True),
            ):
                self.assertEqual(0, main(["validate", "--task", str(task), "--config", str(config), "--json"]))
            manifest = json.loads((task / "manifest.json").read_text(encoding="utf-8"))
            with patch("queryflow.cli.TransferClient", FakeTransfer):
                self.assertEqual(
                    0,
                    main(
                        [
                            "publish", "--task", str(task), "--approved-digest", manifest["approval_digest"],
                            "--destination-project", "analytics-project", "--account", "user@example.com",
                            "--config", str(config), "--json",
                        ]
                    ),
                )
            publisher = FakeTransfer.instances[-1]
            self.assertEqual([("analytics-project", "queryflow_pilot", "us-east1", {"queryflow_pilot": "true"})], publisher.ensure_calls)
            self.assertTrue(publisher.create_calls[0]["disabled"])
            receipt = json.loads((Path(temp) / "audit" / "scheduled-publish" / "publish-receipt.json").read_text(encoding="utf-8"))
            self.assertEqual("scheduled_query", receipt["mode"])
            self.assertEqual("projects/analytics-project/locations/us-east1/transferConfigs/c1", receipt["transfer_config"]["name"])
            self.assertTrue(json.loads((task / "manifest.json").read_text(encoding="utf-8"))["published"])

    def test_publish_scheduled_query_rejects_enabled_readback(self) -> None:
        from queryflow.cli import main
        from queryflow.validation import ValidationResult

        class FakeTransfer:
            def __init__(self, account, project):
                self.transfer = {
                    "name": "projects/analytics-project/locations/us-east1/transferConfigs/c1",
                    "dataSourceId": "scheduled_query", "destinationDatasetId": "queryflow_pilot",
                    "params": {"query": "SELECT @run_time AS scheduled_for_utc;", "destination_table_name_template": "scheduled_query_heartbeat", "write_disposition": "WRITE_APPEND"},
                    "schedule": "every day 14:03", "disabled": False,
                }

            def ensure_dataset(self, *args, **kwargs):
                return {"location": "us-east1"}

            def create_scheduled_query(self, **kwargs):
                return self.transfer

            def get_transfer_config(self, name):
                return self.transfer

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "tasks"
            content = Path(temp) / "heartbeat.sql"
            content.write_text("SELECT @run_time AS scheduled_for_utc;\n", encoding="utf-8")
            config = Path(temp) / "pilot.yaml"
            config.write_text(
                f"workspace_root: {root}\naudit_root: {Path(temp) / 'audit'}\nallow_create_dataset: true\n",
                encoding="utf-8",
            )
            self.assertEqual(
                0,
                main([
                    "start", "--mode", "new", "--kind", "scheduled_query", "--name", "qflow_daily_heartbeat",
                    "--project", "analytics-project", "--location", "us-east1", "--content-file", str(content),
                    "--schedule", "every day 14:03", "--target-dataset", "queryflow_pilot", "--destination-table", "scheduled_query_heartbeat",
                    "--task-id", "scheduled-enabled", "--config", str(config), "--json",
                ]),
            )
            task = root / "scheduled-enabled"
            with patch("queryflow.cli.dry_run_sql", return_value=ValidationResult([], "read_only", True, dry_run_ok=True)):
                self.assertEqual(0, main(["validate", "--task", str(task), "--config", str(config), "--json"]))
            manifest = json.loads((task / "manifest.json").read_text(encoding="utf-8"))
            with patch("queryflow.cli.TransferClient", FakeTransfer):
                result = main([
                    "publish", "--task", str(task), "--approved-digest", manifest["approval_digest"],
                    "--destination-project", "analytics-project", "--account", "user@example.com",
                    "--config", str(config), "--json",
                ])
            self.assertEqual(2, result)
            self.assertFalse(json.loads((task / "manifest.json").read_text(encoding="utf-8"))["published"])

    def test_start_update_exports_a_remote_resource(self) -> None:
        from queryflow.cli import main
        from queryflow.dataform import ExportedAsset

        resource = ResourceRef(
            "notebook",
            "projects/p/locations/us/repositories/r",
            "p",
            "us",
            "original",
            "head",
        )

        class FakeClient:
            def __init__(self, account, project):
                self.account = account
                self.project = project

            def export(self, requested):
                self.requested = requested
                return ExportedAsset(resource, "content.ipynb", b'{"cells": []}', {}, "head")

        with tempfile.TemporaryDirectory() as temp:
            catalog_path = Path(temp) / "catalog.json"
            save_catalog(Catalog([resource], "now", []), catalog_path)
            config_path = Path(temp) / "queryflow.yaml"
            config_path.write_text(f"catalog_path: {catalog_path}\n", encoding="utf-8")
            with patch("queryflow.cli.DataformClient", FakeClient):
                result = main(
                    [
                        "start",
                        "--mode",
                        "update",
                        "--resource",
                        resource.name,
                        "--account",
                        "user@example.com",
                        "--config",
                        str(config_path),
                        "--task-id",
                        "update-1",
                        "--workspace-root",
                        str(Path(temp) / "tasks"),
                        "--json",
                    ]
                )
            self.assertEqual(0, result)
            manifest = json.loads((Path(temp) / "tasks" / "update-1" / "manifest.json").read_text())
            self.assertEqual("update", manifest["mode"])
            self.assertEqual("head", manifest["resource"]["fingerprint"])

    def test_publish_update_requires_team_opt_in(self) -> None:
        from queryflow.cli import main
        from queryflow.validation import ValidationResult

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "tasks"
            content = Path(temp) / "query.sql"
            content.write_text("SELECT 1;\n", encoding="utf-8")
            self.assertEqual(
                0,
                main(
                    [
                        "start",
                        "--kind",
                        "shared_query",
                        "--name",
                        "q1",
                        "--project",
                        "p",
                        "--location",
                        "us",
                        "--content-file",
                        str(content),
                        "--task-id",
                        "update-guard",
                        "--workspace-root",
                        str(root),
                        "--json",
                    ]
                ),
            )
            task = root / "update-guard"
            manifest = json.loads((task / "manifest.json").read_text())
            manifest["mode"] = "update"
            (task / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with patch(
                "queryflow.cli.dry_run_sql",
                return_value=ValidationResult([], "read_only", True, dry_run_ok=True),
            ):
                self.assertEqual(0, main(["validate", "--task", str(task), "--json"]))
            manifest = json.loads((task / "manifest.json").read_text())
            config = Path(temp) / "pilot.yaml"
            config.write_text("mode: pilot\nallow_update_existing: true\n", encoding="utf-8")
            with patch("queryflow.cli.DataformClient") as client:
                result = main(
                    [
                        "publish",
                        "--task",
                        str(task),
                        "--approved-digest",
                        manifest["approval_digest"],
                        "--destination-project",
                        "p",
                        "--account",
                        "user@example.com",
                        "--audit-root",
                        str(Path(temp) / "audit"),
                        "--config",
                        str(config),
                        "--json",
                    ]
                )
            self.assertEqual(2, result)
            client.assert_not_called()

    def test_publish_update_requires_original_destination_project(self) -> None:
        from queryflow.cli import main
        from queryflow.validation import ValidationResult

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "tasks"
            content = Path(temp) / "query.sql"
            content.write_text("SELECT 1;\n", encoding="utf-8")
            self.assertEqual(
                0,
                main(
                    [
                        "start",
                        "--kind",
                        "shared_query",
                        "--name",
                        "q1",
                        "--project",
                        "p",
                        "--location",
                        "us",
                        "--content-file",
                        str(content),
                        "--task-id",
                        "update-destination",
                        "--workspace-root",
                        str(root),
                        "--json",
                    ]
                ),
            )
            task = root / "update-destination"
            manifest = json.loads((task / "manifest.json").read_text())
            manifest["mode"] = "update"
            (task / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with patch(
                "queryflow.cli.dry_run_sql",
                return_value=ValidationResult([], "read_only", True, dry_run_ok=True),
            ):
                self.assertEqual(0, main(["validate", "--task", str(task), "--json"]))
            manifest = json.loads((task / "manifest.json").read_text())
            config = Path(temp) / "team.yaml"
            config.write_text("mode: team\nallow_update_existing: true\n", encoding="utf-8")
            with patch("queryflow.cli.DataformClient") as client:
                result = main(
                    [
                        "publish",
                        "--task",
                        str(task),
                        "--approved-digest",
                        manifest["approval_digest"],
                        "--destination-project",
                        "other-project",
                        "--account",
                        "user@example.com",
                        "--audit-root",
                        str(Path(temp) / "audit"),
                        "--config",
                        str(config),
                        "--json",
                    ]
                )
            self.assertEqual(2, result)
            client.assert_not_called()

    def test_catalog_refresh_does_not_overwrite_catalog_on_empty_warning_result(self) -> None:
        from queryflow.cli import main

        with tempfile.TemporaryDirectory() as temp:
            catalog_path = Path(temp) / "catalog.json"
            with patch(
                "queryflow.cli.refresh_catalog",
                return_value=Catalog(resources=[], generated_at="now", warnings=["sin credenciales"]),
            ):
                result = main(
                    [
                        "catalog", "refresh", "--catalog-path", str(catalog_path), "--json",
                    ]
                )
            self.assertEqual(2, result)
            self.assertFalse(catalog_path.exists())

    def test_start_from_local_file_creates_task_and_review_files(self) -> None:
        from queryflow.cli import main

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "tasks"
            content = Path(temp) / "query.sql"
            content.write_text("SELECT 1;\n", encoding="utf-8")
            result = main(
                [
                    "start",
                    "--kind",
                    "shared_query",
                    "--name",
                    "q1",
                    "--project",
                    "p",
                    "--location",
                    "us",
                    "--content-file",
                    str(content),
                    "--task-id",
                    "t1",
                    "--workspace-root",
                    str(root),
                    "--json",
                ]
            )
            self.assertEqual(0, result)
            self.assertTrue((root / "t1" / "manifest.json").exists())

    def test_start_new_notebook_creates_structured_python_template_as_proposal(self) -> None:
        from queryflow.cli import main
        from queryflow.task import sync_notebook_task

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "tasks"
            result = main(
                [
                    "start",
                    "--mode",
                    "new",
                    "--kind",
                    "notebook",
                    "--name",
                    "nuevo",
                    "--project",
                    "analytics-project",
                    "--location",
                    "us-east1",
                    "--task-id",
                    "t-new",
                    "--workspace-root",
                    str(root),
                    "--json",
                ]
            )
            self.assertEqual(0, result)
            task = root / "t-new"
            baseline = json.loads(__import__("subprocess").check_output(["git", "-C", str(task), "show", "HEAD:content.ipynb"]))
            self.assertEqual([], baseline["cells"])
            self.assertTrue((task / "cells" / "index.json").exists())
            proposed = json.loads(sync_notebook_task(task))
            self.assertEqual(5, len(proposed["cells"]))
            self.assertIn("google.cloud", "".join(proposed["cells"][1]["source"]))

    def test_start_new_saved_query_has_empty_baseline(self) -> None:
        from queryflow.cli import main

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "tasks"
            self.assertEqual(
                0,
                main(
                    [
                        "start",
                        "--mode",
                        "new",
                        "--kind",
                        "shared_query",
                        "--name",
                        "nuevo",
                        "--project",
                        "analytics-project",
                        "--location",
                        "us-east1",
                        "--task-id",
                        "q-new",
                        "--workspace-root",
                        str(root),
                        "--json",
                    ]
                ),
            )
            self.assertEqual("", (root / "q-new" / "content.sql").read_text(encoding="utf-8"))

    def test_validate_static_only_is_precheck_and_not_publishable(self) -> None:
        from queryflow.cli import main

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "tasks"
            content = Path(temp) / "query.sql"
            content.write_text("SELECT 1;\n", encoding="utf-8")
            self.assertEqual(
                0,
                main(
                    [
                        "start", "--kind", "shared_query", "--name", "q1",
                        "--project", "p", "--location", "us", "--content-file", str(content),
                        "--task-id", "t1", "--workspace-root", str(root), "--json",
                    ]
                ),
            )
            self.assertEqual(0, main(["validate", "--task", str(root / "t1"), "--static-only", "--json"]))
            task = root / "t1"
            validation = json.loads((task / "validation.json").read_text(encoding="utf-8"))
            manifest = json.loads((task / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("prechecked", validation["status"])
            self.assertEqual("static", validation["method"])
            self.assertTrue(validation["ok"])
            self.assertFalse(validation["publishable"])
            self.assertEqual("prechecked", manifest["validation_status"])
            self.assertNotIn("approval_digest", manifest)
            with patch("queryflow.cli.DataformClient") as client:
                self.assertEqual(
                    2,
                    main(
                        [
                            "publish",
                            "--task",
                            str(task),
                            "--approved-digest",
                            "not-valid",
                            "--destination-project",
                            "d",
                            "--account",
                            "user@example.com",
                            "--audit-root",
                            str(Path(temp) / "audit"),
                            "--json",
                        ]
                    ),
                )
                client.assert_not_called()

    def test_validate_uses_configured_workbench_backend(self) -> None:
        from queryflow.cli import main
        from queryflow.validation import ValidationResult
        from queryflow.workbench import WorkbenchValidation

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "tasks"
            content = Path(temp) / "query.sql"
            content.write_text("SELECT 1;\n", encoding="utf-8")
            self.assertEqual(
                0,
                main(
                    [
                        "start", "--kind", "shared_query", "--name", "q1",
                        "--project", "analytics-project", "--location", "us-east1",
                        "--content-file", str(content), "--task-id", "t1",
                        "--workspace-root", str(root), "--json",
                    ]
                ),
            )
            config = Path(temp) / "queryflow.yaml"
            config.write_text(
                "\n".join(
                    [
                        "validation_backend: workbench",
                        "workbench_project: source-project",
                        "workbench_location: us-east1-b",
                        "workbench_instance: sbs-analytics-python-notebook",
                        "workbench_job_project: source-project",
                    ]
                ),
                encoding="utf-8",
            )
            fake = WorkbenchValidation(
                ValidationResult(
                    [], "read_only", True, dry_run_ok=True,
                    maximum_bytes_billed=1024, within_configured_limit=True,
                ),
                {"backend": "workbench", "project": "source-project"},
            )
            with patch("queryflow.cli.validate_workbench_fragments", return_value=fake) as validate:
                self.assertEqual(
                    0,
                    main(["validate", "--task", str(root / "t1"), "--config", str(config), "--json"]),
                )
            validate.assert_called_once()
            validation = json.loads((root / "t1" / "validation.json").read_text(encoding="utf-8"))
            self.assertEqual("workbench", validation["backend"])
            self.assertEqual("source-project", validation["backend_details"]["project"])
            self.assertTrue(validation["publishable"])

    def test_validate_rejects_read_execution_with_workbench_backend(self) -> None:
        from queryflow.cli import main
        from queryflow.validation import ValidationResult
        from queryflow.workbench import WorkbenchValidation

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "tasks"
            content = Path(temp) / "query.sql"
            content.write_text("SELECT 1;\n", encoding="utf-8")
            self.assertEqual(
                0,
                main([
                    "start", "--kind", "shared_query", "--name", "q1",
                    "--project", "p", "--location", "us-east1", "--content-file", str(content),
                    "--task-id", "t1", "--workspace-root", str(root), "--json",
                ]),
            )
            config = Path(temp) / "queryflow.yaml"
            config.write_text(
                "\n".join([
                    "validation_backend: workbench",
                    "workbench_project: wb-project",
                    "workbench_location: us-east1-b",
                    "workbench_instance: wb-instance",
                    "workbench_job_project: jobs-project",
                ]),
                encoding="utf-8",
            )
            fake = WorkbenchValidation(
                ValidationResult([], "read_only", True, dry_run_ok=True),
                {"backend": "workbench"},
            )
            with patch("queryflow.cli.validate_workbench_fragments", return_value=fake), patch(
                "queryflow.cli.execute_read_only_sql"
            ) as execute:
                result = main([
                    "validate", "--task", str(root / "t1"), "--config", str(config),
                    "--execute-read-only", "--confirm-execution", "--json",
                ])
            self.assertEqual(2, result)
            execute.assert_not_called()

    def test_validate_does_not_persist_optional_query_rows(self) -> None:
        from queryflow.cli import main
        from queryflow.validation import ExecutionResult, ValidationResult

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "tasks"
            content = Path(temp) / "query.sql"
            content.write_text("SELECT secret FROM p.d.t;\n", encoding="utf-8")
            self.assertEqual(
                0,
                main(
                    [
                        "start", "--kind", "shared_query", "--name", "q1",
                        "--project", "p", "--location", "us", "--content-file", str(content),
                        "--task-id", "t1", "--workspace-root", str(root), "--json",
                    ]
                ),
            )
            task = root / "t1"
            dry = ValidationResult([], "read_only", True, dry_run_ok=True)
            execution = ExecutionResult(True, [{"secret": "must-not-be-archived"}])
            with patch("queryflow.cli.dry_run_sql", return_value=dry), patch(
                "queryflow.cli.execute_read_only_sql", return_value=execution
            ):
                self.assertEqual(
                    0,
                    main(
                        [
                            "validate", "--task", str(task), "--execute-read-only",
                            "--confirm-execution", "--json",
                        ]
                    ),
                )
            validation_path = task / "validation.json"
            validation = json.loads(validation_path.read_text(encoding="utf-8"))
            self.assertTrue(validation["execution"]["executed"])
            self.assertEqual(1, validation["execution"]["row_count"])
            self.assertNotIn("rows", validation["execution"])
            self.assertNotIn("must-not-be-archived", validation_path.read_text(encoding="utf-8"))

    def test_publish_requires_approval_and_archives_before_fake_dataform_write(self) -> None:
        from queryflow.cli import main

        class FakeClient:
            def __init__(self, account, project):
                self.account = account
                self.project = project

            def create_copy(self, **kwargs):
                return {"repository": "projects/d/locations/us/repositories/copy", "commit_sha": "new"}

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "tasks"
            audit = Path(temp) / "audit"
            content = Path(temp) / "query.sql"
            content.write_text("SELECT 1;\n", encoding="utf-8")
            self.assertEqual(
                0,
                main([
                    "start", "--kind", "shared_query", "--name", "q1", "--project", "d",
                    "--location", "us", "--content-file", str(content), "--task-id", "t1",
                    "--workspace-root", str(root), "--json",
                ]),
            )
            task = root / "t1"
            from queryflow.validation import ValidationResult

            with patch(
                "queryflow.cli.dry_run_sql",
                return_value=ValidationResult([], "read_only", True, dry_run_ok=True),
            ):
                self.assertEqual(0, main(["validate", "--task", str(task), "--json"]))
            manifest = json.loads((task / "manifest.json").read_text(encoding="utf-8"))
            with patch("queryflow.cli.DataformClient", FakeClient):
                self.assertEqual(
                    0,
                    main([
                        "publish", "--task", str(task), "--approved-digest", manifest["approval_digest"],
                        "--destination-project", "d", "--account", "user@example.com",
                        "--audit-root", str(audit), "--json",
                    ]),
                )
            self.assertTrue((audit / "t1" / "publish-receipt.json").exists())

    def test_publish_update_commits_original_and_verifies_readback(self) -> None:
        from queryflow.cli import main
        from queryflow.dataform import ExportedAsset
        from queryflow.validation import ValidationResult

        resource = ResourceRef(
            "shared_query",
            "projects/p/locations/us/repositories/original",
            "p",
            "us",
            "original",
            "head",
        )

        class FakeClient:
            instances = []

            def __init__(self, account, project):
                self.account = account
                self.project = project
                self.update_calls = []
                self.__class__.instances.append(self)

            def export(self, requested):
                return ExportedAsset(resource, "content.sql", b"SELECT 1;\n", {}, "head")

            def update_file(self, repository, filename, content, **kwargs):
                self.update_calls.append((repository, filename, content, kwargs))
                return {"repository": repository, "filename": filename, "commit_sha": "updated"}

            def read_file(self, repository, filename):
                return b"SELECT 2;\n"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "tasks"
            catalog_path = Path(temp) / "catalog.json"
            config = Path(temp) / "team.yaml"
            save_catalog(Catalog([resource], "now", []), catalog_path)
            config.write_text(
                f"catalog_path: {catalog_path}\nmode: team\nallow_update_existing: true\n",
                encoding="utf-8",
            )
            with patch("queryflow.cli.DataformClient", FakeClient):
                self.assertEqual(
                    0,
                    main(
                        [
                            "start",
                            "--mode",
                            "update",
                            "--resource",
                            resource.name,
                            "--account",
                            "user@example.com",
                            "--config",
                            str(config),
                            "--task-id",
                            "update-publish",
                            "--workspace-root",
                            str(root),
                            "--json",
                        ]
                    ),
                )
            task = root / "update-publish"
            # Keep the fixture byte-identical across platforms: publication
            # verifies the exact content returned by the remote provider.
            (task / "content.sql").write_bytes(b"SELECT 2;\n")
            with patch(
                "queryflow.cli.dry_run_sql",
                return_value=ValidationResult([], "read_only", True, dry_run_ok=True),
            ):
                self.assertEqual(0, main(["validate", "--task", str(task), "--config", str(config), "--json"]))
            manifest = json.loads((task / "manifest.json").read_text())
            audit = Path(temp) / "audit"
            with patch("queryflow.cli.DataformClient", FakeClient):
                self.assertEqual(
                    0,
                    main(
                        [
                            "publish",
                            "--task",
                            str(task),
                            "--approved-digest",
                            manifest["approval_digest"],
                            "--destination-project",
                            "p",
                            "--account",
                            "user@example.com",
                            "--audit-root",
                            str(audit),
                            "--config",
                            str(config),
                            "--json",
                        ]
                    ),
                )
            publisher = FakeClient.instances[-1]
            self.assertEqual(1, len(publisher.update_calls))
            self.assertEqual("head", publisher.update_calls[0][3]["required_head_commit"])
            receipt = json.loads((audit / "update-publish" / "publish-receipt.json").read_text())
            self.assertEqual("update", receipt["mode"])
            self.assertTrue(json.loads((task / "manifest.json").read_text())["published"])

    def test_publish_update_rejects_remote_head_change_before_commit(self) -> None:
        from queryflow.cli import main
        from queryflow.dataform import ExportedAsset
        from queryflow.validation import ValidationResult

        resource = ResourceRef(
            "shared_query",
            "projects/p/locations/us/repositories/original",
            "p",
            "us",
            "original",
            "head",
        )

        class FakeClient:
            update_calls = 0
            export_total = 0

            def __init__(self, account, project):
                self.account = account
                self.project = project
                self.export_calls = 0

            def export(self, requested):
                self.__class__.export_total += 1
                head = "head" if self.__class__.export_total == 1 else "changed-head"
                return ExportedAsset(resource, "content.sql", b"SELECT 1;\n", {}, head)

            def update_file(self, *args, **kwargs):
                self.__class__.update_calls += 1
                return {"repository": resource.name, "filename": "content.sql", "commit_sha": "unexpected"}

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "tasks"
            catalog_path = Path(temp) / "catalog.json"
            config = Path(temp) / "team.yaml"
            save_catalog(Catalog([resource], "now", []), catalog_path)
            config.write_text(
                f"catalog_path: {catalog_path}\nmode: team\nallow_update_existing: true\n",
                encoding="utf-8",
            )
            with patch("queryflow.cli.DataformClient", FakeClient):
                self.assertEqual(
                    0,
                    main(
                        [
                            "start",
                            "--mode",
                            "update",
                            "--resource",
                            resource.name,
                            "--account",
                            "user@example.com",
                            "--config",
                            str(config),
                            "--task-id",
                            "update-race",
                            "--workspace-root",
                            str(root),
                            "--json",
                        ]
                    ),
                )
            task = root / "update-race"
            (task / "content.sql").write_text("SELECT 2;\n", encoding="utf-8")
            with patch(
                "queryflow.cli.dry_run_sql",
                return_value=ValidationResult([], "read_only", True, dry_run_ok=True),
            ):
                self.assertEqual(0, main(["validate", "--task", str(task), "--config", str(config), "--json"]))
            manifest = json.loads((task / "manifest.json").read_text())
            with patch("queryflow.cli.DataformClient", FakeClient):
                result = main(
                    [
                        "publish",
                        "--task",
                        str(task),
                        "--approved-digest",
                        manifest["approval_digest"],
                        "--destination-project",
                        "p",
                        "--account",
                        "user@example.com",
                        "--audit-root",
                        str(Path(temp) / "audit"),
                        "--config",
                        str(config),
                        "--json",
                    ]
                )
            self.assertEqual(2, result)
            self.assertEqual(0, FakeClient.update_calls)

    def test_publish_blocks_file_changed_after_validation(self) -> None:
        from queryflow.cli import main

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "tasks"
            audit = Path(temp) / "audit"
            content = Path(temp) / "query.sql"
            content.write_text("SELECT 1;\n", encoding="utf-8")
            self.assertEqual(
                0,
                main([
                    "start", "--kind", "shared_query", "--name", "q1", "--project", "p",
                    "--location", "us", "--content-file", str(content), "--task-id", "t1",
                    "--workspace-root", str(root), "--json",
                ]),
            )
            task = root / "t1"
            from queryflow.validation import ValidationResult

            with patch(
                "queryflow.cli.dry_run_sql",
                return_value=ValidationResult([], "read_only", True, dry_run_ok=True),
            ):
                self.assertEqual(0, main(["validate", "--task", str(task), "--json"]))
            manifest = json.loads((task / "manifest.json").read_text(encoding="utf-8"))
            (task / "query.sql").write_text("SELECT 2;\n", encoding="utf-8")
            with patch("queryflow.cli.DataformClient") as client:
                result = main([
                    "publish", "--task", str(task), "--approved-digest", manifest["approval_digest"],
                    "--destination-project", "d", "--account", "user@example.com",
                    "--audit-root", str(audit), "--json",
                ])
                self.assertEqual(2, result)
                client.assert_not_called()

    def test_publish_new_saved_query_creates_copy_after_real_validation(self) -> None:
        from queryflow.cli import main
        from queryflow.validation import ValidationResult

        class FakeClient:
            def __init__(self, account, project):
                self.account = account
                self.project = project

            def create_copy(self, **kwargs):
                return {"repository": "projects/d/locations/us/repositories/new", "commit_sha": "new"}

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "tasks"
            content = Path(temp) / "new.sql"
            content.write_text("SELECT 1;\n", encoding="utf-8")
            self.assertEqual(
                0,
                main(
                    [
                        "start",
                        "--mode",
                        "new",
                        "--kind",
                        "shared_query",
                        "--name",
                        "nuevo",
                        "--project",
                        "d",
                        "--location",
                        "us",
                        "--content-file",
                        str(content),
                        "--task-id",
                        "new-1",
                        "--workspace-root",
                        str(root),
                        "--json",
                    ]
                ),
            )
            task = root / "new-1"
            with patch(
                "queryflow.cli.dry_run_sql",
                return_value=ValidationResult([], "read_only", True, dry_run_ok=True),
            ):
                self.assertEqual(0, main(["validate", "--task", str(task), "--json"]))
            manifest = json.loads((task / "manifest.json").read_text(encoding="utf-8"))
            with patch("queryflow.cli.DataformClient", FakeClient):
                self.assertEqual(
                    0,
                    main(
                        [
                            "publish",
                            "--task",
                            str(task),
                            "--approved-digest",
                            manifest["approval_digest"],
                            "--destination-project",
                            "d",
                            "--account",
                            "user@example.com",
                            "--audit-root",
                            str(Path(temp) / "audit"),
                            "--json",
                        ]
                    ),
                )


if __name__ == "__main__":
    unittest.main()
