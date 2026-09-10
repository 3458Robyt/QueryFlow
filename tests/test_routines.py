import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from queryflow.migration import RouteDictionary
from queryflow.routines import (
    BigQueryRoutineClient,
    RoutineError,
    RoutineRateLimitError,
    RoutineTransientError,
    RoutineSnapshot,
    build_workbench_routine_code,
    build_routine_digest,
    build_routine_manifest,
    build_routine_lot_digest,
    inventory_routine_snapshots,
    load_routine_manifest,
    publish_routines,
    render_routine_review,
    routine_semantic_payload,
    parse_workbench_routine_output,
    validate_routine_manifest,
    write_routine_audit,
    write_routine_reports,
)
from queryflow.workbench import _websocket_execute


def procedure(project="source-project", dataset="source_functions", name="main_proc", body="CALL source-project.source_functions.helper();"):
    return {
        "routineReference": {"projectId": project, "datasetId": dataset, "routineId": name},
        "routineType": "PROCEDURE",
        "language": "SQL",
        "definitionBody": body,
        "description": "demo",
        "strictMode": True,
        "arguments": [{"name": "p_id", "argumentKind": "FIXED_TYPE", "dataType": {"typeKind": "INT64"}}],
    }


class FakeRoutineClient:
    def __init__(self, source, destination=None):
        self.source = {self.key(item): item for item in source}
        self.destination = {self.key(item): item for item in (destination or [])}
        self.inserted = []
        self.calls = []

    @staticmethod
    def key(value):
        ref = value.get("routineReference") or {}
        return (str(ref.get("projectId")), str(ref.get("datasetId")), str(ref.get("routineId")))

    def get_routine(self, project, dataset, routine_id):
        self.calls.append(("get", project, dataset, routine_id))
        value = self.source.get((project, dataset, routine_id))
        if value is None:
            value = self.destination.get((project, dataset, routine_id))
        if value is None:
            error = RoutineError("not found", kind="not_found")
            raise error
        return json.loads(json.dumps(value))

    def insert_routine(self, project, dataset, routine):
        self.calls.append(("insert", project, dataset, routine["routineReference"]["routineId"]))
        key = self.key(routine)
        if key in self.destination:
            raise RoutineError("already exists", kind="conflict")
        value = json.loads(json.dumps(routine))
        self.destination[key] = value
        self.inserted.append(value)
        return value


class RoutineTests(unittest.TestCase):
    def setUp(self):
        self.dictionary = RouteDictionary.from_mapping(
            {
                "schema_version": 1,
                "dictionary_id": "routine-routes",
                "scope": {"source_project": "source-project"},
                "mappings": [
                    {
                        "id": "raw-001",
                        "zone": "raw",
                        "old": "source-project.raw_dataset",
                        "new": "raw-487218.raw_dataset",
                    }
                ],
            }
        )

    def test_client_lists_paginated_routines_and_pins_destination_writes(self):
        pages = [
            {"routines": [{"routineReference": {"routineId": "a"}}], "nextPageToken": "next"},
            {"routines": [{"routineReference": {"routineId": "b"}}]},
        ]
        requests = []

        def transport(method, resource, query, body):
            requests.append((method, resource, query, body))
            if resource.endswith("/routines"):
                return pages.pop(0)
            return {}

        client = BigQueryRoutineClient(
            account="analyst@example.com",
            allowed_write_project="destination-project",
            source_project="source-project",
            transport=transport,
        )
        result = client.list_routines("source-project", "source_functions")
        self.assertEqual(["a", "b"], [item["routineReference"]["routineId"] for item in result])
        self.assertEqual("next", requests[1][2]["pageToken"])
        with self.assertRaisesRegex(RoutineError, "proyecto destino"):
            client.insert_routine("source-project", "source_functions", procedure())
        with self.assertRaisesRegex(RoutineError, "dataset"):
            client.insert_routine("destination-project", "functions/unsafe", procedure(project="destination-project", dataset="functions"))

    def test_client_http_request_builds_request_without_duplicating_data_argument(self):
        client = BigQueryRoutineClient(
            account="analyst@example.com",
            allowed_write_project="destination-project",
            requests_per_minute=None,
        )
        client._tokens.get = lambda: "token"  # type: ignore[method-assign]

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"ok": true}'

        captured = {}

        def open_request(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

        with patch("urllib.request.urlopen", side_effect=open_request):
            response = client.request(
                "POST",
                "projects/destination-project/datasets/functions/routines",
                body=procedure(project="destination-project", dataset="functions"),
            )
        self.assertTrue(response["ok"])
        self.assertEqual("POST", captured["request"].method)
        self.assertEqual(
            "destination-project",
            json.loads(captured["request"].data)["routineReference"]["projectId"],
        )

    def test_client_retries_transient_read_but_never_writes(self):
        attempts = []

        def transport(method, resource, query, body):
            attempts.append(method)
            if method == "GET" and len(attempts) == 1:
                raise RoutineTransientError("temporary", retry_after_seconds=0)
            if method == "POST":
                raise RoutineTransientError("temporary", retry_after_seconds=0)
            return {"routines": []}

        client = BigQueryRoutineClient(
            account="analyst@example.com",
            allowed_write_project="destination-project",
            transport=transport,
            requests_per_minute=None,
            sleeper=lambda _delay: None,
        )
        self.assertEqual([], client.list_routines("source-project", "source_functions"))
        self.assertEqual(["GET", "GET"], attempts)
        self.assertEqual(1, client.request_stats.retries)
        with self.assertRaises(RoutineTransientError):
            client.request(
                "POST",
                "projects/destination-project/datasets/functions/routines",
                body=procedure(project="destination-project", dataset="functions"),
            )
        self.assertEqual(["GET", "GET", "POST"], attempts)

    def test_inventory_can_capture_bounded_dataset_acl_evidence(self):
        routine = procedure(body="SELECT 1;")

        class InventoryClient:
            def get_dataset(self, project, dataset):
                return {
                    "datasetReference": {"projectId": project, "datasetId": dataset},
                    "location": "us-east1",
                    "access": [
                        {"role": "READER", "entityType": "groupByEmail", "entityId": "analysts@example.com"},
                        {"role": "OWNER", "entityId": "ignored-extra"},
                    ],
                }

            def list_datasets(self, project):
                return [{"datasetReference": {"projectId": project, "datasetId": "source_functions"}, "location": "us-east1"}]

            def list_routines(self, project, dataset):
                return [routine]

            def get_routine(self, project, dataset, routine_id):
                return routine

        access = []
        snapshots, errors = inventory_routine_snapshots(
            InventoryClient(), "source-project", access_report=access
        )
        self.assertEqual(1, len(snapshots))
        self.assertEqual([], errors)
        self.assertEqual("READER", access[0]["access"][0]["role"])
        self.assertEqual("analysts@example.com", access[0]["access"][0]["entityId"])

    def test_manifest_rewrites_body_includes_transitive_dependency_and_reports_risky_sql(self):
        helper = procedure(name="helper", body="SELECT 1;")
        helper["routineType"] = "FUNCTION"
        helper["dataType"] = {"typeKind": "INT64"}
        main = procedure(body="CALL source-project.source_functions.helper();\nEXECUTE IMMEDIATE 'SELECT * FROM source-project.raw_dataset.table';\nUPDATE source-project.raw_dataset.table SET x = 1;")
        source = [
            RoutineSnapshot.from_routine("source-project", "source_functions", "us-east1", main),
            RoutineSnapshot.from_routine("source-project", "source_functions", "us-east1", helper),
        ]
        manifest = build_routine_manifest(
            campaign_id="routines-test",
            source_project="source-project",
            destination_project="destination-project",
            destination_dataset="functions",
            destination_location="us-east1",
            source_snapshots=source,
            destination_snapshots=[],
            dictionary=self.dictionary,
            secret_handling="block",
            batch_size=20,
        )
        records = {item["destination"]["routine_id"]: item for item in manifest["resources"]}
        self.assertEqual({"main_proc", "helper"}, set(records))
        self.assertIn("CALL `destination-project.functions.helper`", records["main_proc"]["proposal"]["definitionBody"])
        self.assertNotIn("CALL source-project.source_functions.helper", records["main_proc"]["proposal"]["definitionBody"])
        self.assertIn("raw-487218.raw_dataset", records["main_proc"]["proposal"]["definitionBody"])
        self.assertIn("dynamic_sql", records["main_proc"]["review"]["reasons"])
        self.assertIn("mutating_sql", records["main_proc"]["review"]["reasons"])
        self.assertEqual("dependency", records["helper"]["role"])
        self.assertNotIn(
            "helper",
            [item["routine_id"] for item in manifest["unsupported"]],
        )
        self.assertEqual(manifest["publication_digest"], build_routine_digest(manifest))
        self.assertTrue(manifest["policy"]["no_sql_execution"])
        self.assertTrue(manifest["policy"]["dry_run"]["skipped"])

    def test_manifest_blocks_unqualified_procedure_calls(self):
        main = procedure(body="CALL helper();")
        helper = procedure(name="helper", body="SELECT 1;")
        manifest = build_routine_manifest(
            campaign_id="unqualified-call",
            source_project="source-project",
            destination_project="destination-project",
            destination_dataset="functions",
            destination_location="us-east1",
            source_snapshots=[
                RoutineSnapshot.from_routine(
                    "source-project", "source_functions", "us-east1", main
                ),
                RoutineSnapshot.from_routine(
                    "source-project", "source_functions", "us-east1", helper
                ),
            ],
            destination_snapshots=[],
            dictionary=self.dictionary,
        )
        record = next(
            item
            for item in manifest["resources"]
            if item["source"]["routine_id"] == "main_proc"
        )
        self.assertEqual("blocked", record["status"])
        self.assertIn(
            "unqualified_dependency",
            [item["kind"] for item in record["blockers"]],
        )

    def test_dependency_rewrite_wins_before_route_dictionary_can_change_old_dataset(self):
        dictionary = RouteDictionary.from_mapping(
            {
                "schema_version": 1,
                "dictionary_id": "routine-routes-call",
                "scope": {"source_project": "source-project"},
                "mappings": [
                    {
                        "id": "old-functions",
                        "zone": "raw",
                        "old": "source-project.source_functions",
                        "new": "legacy-487218.functions",
                    }
                ],
            }
        )
        helper = procedure(name="helper", body="SELECT 1;")
        main = procedure(body="CALL source-project.source_functions.helper();")
        manifest = build_routine_manifest(
            campaign_id="dependency-order",
            source_project="source-project",
            destination_project="destination-project",
            destination_dataset="functions",
            destination_location="us-east1",
            source_snapshots=[
                RoutineSnapshot.from_routine("source-project", "source_functions", "us-east1", main),
                RoutineSnapshot.from_routine("source-project", "source_functions", "us-east1", helper),
            ],
            destination_snapshots=[],
            dictionary=dictionary,
        )
        record = next(item for item in manifest["resources"] if item["source"]["routine_id"] == "main_proc")
        self.assertIn("CALL `destination-project.functions.helper`", record["proposal"]["definitionBody"])
        self.assertNotIn("legacy-487218.functions", record["proposal"]["definitionBody"])

    def test_dependency_rewrite_does_not_rewrite_its_own_destination_when_dataset_names_match(self):
        helper = procedure(dataset="functions", name="helper", body="SELECT 1;")
        main = procedure(dataset="functions", body="CALL source-project.functions.helper();")
        manifest = build_routine_manifest(
            campaign_id="dependency-target-loop",
            source_project="source-project",
            destination_project="destination-project",
            destination_dataset="functions",
            destination_location="us-east1",
            source_snapshots=[
                RoutineSnapshot.from_routine("source-project", "functions", "us-east1", main),
                RoutineSnapshot.from_routine("source-project", "functions", "us-east1", helper),
            ],
            destination_snapshots=[],
            dictionary=self.dictionary,
        )
        record = next(item for item in manifest["resources"] if item["source"]["routine_id"] == "main_proc")
        self.assertEqual("CALL `destination-project.functions.helper`();", record["proposal"]["definitionBody"])

    def test_manifest_records_permissions_as_read_only_evidence(self):
        main = procedure(body="SELECT 1;")
        manifest = build_routine_manifest(
            campaign_id="permissions",
            source_project="source-project",
            destination_project="destination-project",
            destination_dataset="functions",
            destination_location="us-east1",
            source_snapshots=[
                RoutineSnapshot.from_routine(
                    "source-project", "source_functions", "us-east1", main
                )
            ],
            destination_snapshots=[],
            dictionary=self.dictionary,
            permissions={
                "source": [
                    {"dataset": "source_functions", "role": "READER", "entityId": "group:analysts"}
                ],
                "destination": {
                    "dataset": "functions",
                    "access": [{"role": "WRITER", "entityId": "group:platform"}],
                },
                "automated_changes": [],
            },
        )
        self.assertEqual("READER", manifest["permissions"]["source"][0]["role"])
        self.assertEqual([], manifest["permissions"]["automated_changes"])
        self.assertEqual(manifest["publication_digest"], build_routine_digest(manifest))

    def test_operational_request_stats_do_not_change_approval_digest(self):
        main = procedure(body="SELECT 1;")
        manifest = build_routine_manifest(
            campaign_id="digest-stats",
            source_project="source-project",
            destination_project="destination-project",
            destination_dataset="functions",
            destination_location="us-east1",
            source_snapshots=[
                RoutineSnapshot.from_routine(
                    "source-project", "source_functions", "us-east1", main
                )
            ],
            destination_snapshots=[],
            dictionary=self.dictionary,
        )
        original_digest = manifest["publication_digest"]
        manifest["request_stats"] = {
            "requests_attempted": 7,
            "requests_succeeded": 7,
            "retries": 1,
        }
        self.assertEqual(original_digest, build_routine_digest(manifest))

    def test_manifest_rejects_destination_dataset_path_injection(self):
        with self.assertRaisesRegex(RoutineError, "dataset destino"):
            build_routine_manifest(
                campaign_id="invalid-dataset",
                source_project="source-project",
                destination_project="destination-project",
                destination_dataset="functions/other",
                destination_location="us-east1",
                source_snapshots=[],
                destination_snapshots=[],
                dictionary=self.dictionary,
            )

    def test_manifest_blocks_duplicate_names_and_destination_conflicts_without_rename(self):
        first = procedure(dataset="dataset_a", name="same", body="SELECT 1;")
        second = procedure(dataset="dataset_b", name="same", body="SELECT 2;")
        destination = procedure(project="destination-project", dataset="functions", name="other", body="SELECT 9;")
        destination["routineReference"]["routineId"] = "same"
        manifest = build_routine_manifest(
            campaign_id="collision",
            source_project="source-project",
            destination_project="destination-project",
            destination_dataset="functions",
            destination_location="us-east1",
            source_snapshots=[
                RoutineSnapshot.from_routine("source-project", "dataset_a", "us-east1", first),
                RoutineSnapshot.from_routine("source-project", "dataset_b", "us-east1", second),
            ],
            destination_snapshots=[RoutineSnapshot.from_routine("destination-project", "functions", "us-east1", destination)],
            dictionary=self.dictionary,
        )
        self.assertTrue(all(item["status"] == "destination_conflict" for item in manifest["resources"]))
        self.assertTrue(all(item["destination"]["routine_id"] == "same" for item in manifest["resources"]))
        self.assertEqual(1, len(manifest["lots"]))
        self.assertEqual(manifest["lots"][0]["digest"], build_routine_lot_digest(manifest, 1))

    def test_manifest_blocks_caller_when_dependency_has_destination_conflict(self):
        helper = procedure(name="helper", body="SELECT 1;")
        main = procedure(body="CALL source-project.source_functions.helper();")
        conflicting_helper = procedure(
            project="destination-project",
            dataset="functions",
            name="helper",
            body="SELECT 999;",
        )
        manifest = build_routine_manifest(
            campaign_id="dependency-conflict",
            source_project="source-project",
            destination_project="destination-project",
            destination_dataset="functions",
            destination_location="us-east1",
            source_snapshots=[
                RoutineSnapshot.from_routine(
                    "source-project", "source_functions", "us-east1", main
                ),
                RoutineSnapshot.from_routine(
                    "source-project", "source_functions", "us-east1", helper
                ),
            ],
            destination_snapshots=[
                RoutineSnapshot.from_routine(
                    "destination-project", "functions", "us-east1", conflicting_helper
                )
            ],
            dictionary=self.dictionary,
        )
        records = {
            item["source"]["routine_id"]: item for item in manifest["resources"]
        }
        self.assertEqual("destination_conflict", records["helper"]["status"])
        self.assertEqual("blocked", records["main_proc"]["status"])
        self.assertIn(
            "dependency_destination_conflict",
            [item["kind"] for item in records["main_proc"]["blockers"]],
        )

    def test_secret_is_redacted_and_requires_sealed_approval(self):
        secret = procedure(body="DECLARE token STRING DEFAULT 'ghp_123456789012345678901234567890'; SELECT token;")
        snapshot = RoutineSnapshot.from_routine("source-project", "source_functions", "us-east1", secret)
        manifest = build_routine_manifest(
            campaign_id="secret",
            source_project="source-project",
            destination_project="destination-project",
            destination_dataset="functions",
            destination_location="us-east1",
            source_snapshots=[snapshot],
            destination_snapshots=[],
            dictionary=self.dictionary,
            secret_handling="sealed_copy",
        )
        record = manifest["resources"][0]
        self.assertEqual("security_pending", record["status"])
        self.assertNotIn("ghp_", json.dumps(record))
        self.assertTrue(manifest["sealed_publication_digest"])
        with self.assertRaisesRegex(RoutineError, "seguridad"):
            validate_routine_manifest(manifest, approved_digest=manifest["publication_digest"])

    def test_publish_inserts_only_missing_resources_and_reads_back_without_call(self):
        main = procedure(body="SELECT * FROM source-project.raw_dataset.table;")
        snapshot = RoutineSnapshot.from_routine("source-project", "source_functions", "us-east1", main)
        manifest = build_routine_manifest(
            campaign_id="publish",
            source_project="source-project",
            destination_project="destination-project",
            destination_dataset="functions",
            destination_location="us-east1",
            source_snapshots=[snapshot],
            destination_snapshots=[],
            dictionary=self.dictionary,
        )
        client = FakeRoutineClient([main])
        receipts = publish_routines(
            manifest,
            client,
            dictionary=self.dictionary,
            approved_digest=manifest["publication_digest"],
        )
        self.assertEqual(1, len(receipts))
        self.assertEqual("published_verified", receipts[0]["status"])
        self.assertEqual(["insert"], [call[0] for call in client.calls if call[0] == "insert"])
        self.assertNotIn("CALL", json.dumps(receipts))
        with self.assertRaisesRegex(RoutineError, "digest"):
            publish_routines(manifest, client, dictionary=self.dictionary, approved_digest="wrong")

    def test_publish_records_permission_failure_and_continues_with_other_routines(self):
        first = procedure(name="a_blocked", body="SELECT 1;")
        second = procedure(name="b_publish", body="SELECT 2;")
        manifest = build_routine_manifest(
            campaign_id="publish-continues-after-permission",
            source_project="source-project",
            destination_project="destination-project",
            destination_dataset="functions",
            destination_location="us-east1",
            source_snapshots=[
                RoutineSnapshot.from_routine("source-project", "source_functions", "us-east1", first),
                RoutineSnapshot.from_routine("source-project", "source_functions", "us-east1", second),
            ],
            destination_snapshots=[],
            dictionary=self.dictionary,
        )

        class PermissionOnFirst(FakeRoutineClient):
            def get_routine(self, project, dataset, routine_id):
                if project == "source-project" and routine_id == "a_blocked":
                    raise RoutineError("VPC policy blocked routine", kind="permission")
                return super().get_routine(project, dataset, routine_id)

        client = PermissionOnFirst([first, second])
        receipts = publish_routines(
            manifest,
            client,
            dictionary=self.dictionary,
            approved_digest=manifest["publication_digest"],
        )

        self.assertEqual(["failed", "published_verified"], [item["status"] for item in receipts])
        self.assertEqual("permission", receipts[0]["kind"])
        self.assertFalse(receipts[0]["call_executed"])
        self.assertFalse(receipts[0]["dry_run"])
        self.assertEqual(["b_publish"], [item["routineReference"]["routineId"] for item in client.inserted])

    def test_publish_rejects_a_different_route_dictionary(self):
        main = procedure(body="SELECT 1;")
        snapshot = RoutineSnapshot.from_routine(
            "source-project", "source_functions", "us-east1", main
        )
        manifest = build_routine_manifest(
            campaign_id="dictionary-integrity",
            source_project="source-project",
            destination_project="destination-project",
            destination_dataset="functions",
            destination_location="us-east1",
            source_snapshots=[snapshot],
            destination_snapshots=[],
            dictionary=self.dictionary,
        )
        changed_dictionary = RouteDictionary.from_mapping(
            {
                "schema_version": 1,
                "dictionary_id": "routine-routes-changed",
                "scope": {"source_project": "source-project"},
                "mappings": [
                    {
                        "id": "raw-001",
                        "zone": "raw",
                        "old": "source-project.raw_dataset",
                        "new": "raw-487218.raw_dataset",
                    },
                    {
                        "id": "raw-002",
                        "zone": "raw",
                        "old": "source-project.other_dataset",
                        "new": "raw-487218.other_dataset",
                    },
                ],
            }
        )
        with self.assertRaisesRegex(RoutineError, "diccionario"):
            publish_routines(
                manifest,
                FakeRoutineClient([main]),
                dictionary=changed_dictionary,
                approved_digest=manifest["publication_digest"],
            )

    def test_routine_audit_contains_hashes_and_receipts_but_not_definition_body(self):
        with tempfile.TemporaryDirectory() as temporary:
            main = procedure(body="SELECT 'not a credential';")
            manifest = build_routine_manifest(
                campaign_id="audit",
                source_project="source-project",
                destination_project="destination-project",
                destination_dataset="functions",
                destination_location="us-east1",
                source_snapshots=[RoutineSnapshot.from_routine("source-project", "source_functions", "us-east1", main)],
                destination_snapshots=[],
                dictionary=self.dictionary,
            )
            manifest_path = Path(temporary) / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            path = write_routine_audit(
                manifest,
                manifest_path,
                approved_digest=manifest["publication_digest"],
                phase="completed",
                receipts=[
                    {
                        "source": "source",
                        "status": "failed",
                        "kind": "permission",
                        "message": "VPC policy blocked routine",
                        "proposed_sha256": "abc",
                    }
                ],
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("completed", payload["phase"])
            self.assertEqual("abc", payload["receipts"][0]["proposed_sha256"])
            self.assertEqual("permission", payload["receipts"][0]["kind"])
            self.assertNotIn("definitionBody", json.dumps(payload))

    def test_routine_markdown_report_lists_route_and_dependency_coordinates(self):
        main = procedure(
            body="CALL source-project.source_functions.helper();\n"
            "SELECT * FROM source-project.unknown_dataset.table;"
        )
        helper = procedure(name="helper", body="SELECT 1;")
        unsupported = procedure(name="spark_proc", body="SELECT 1;")
        unsupported["language"] = "PYTHON"
        unsupported["sparkOptions"] = {"mainFileUri": "gs://example/main.py"}
        manifest = build_routine_manifest(
            campaign_id="report-detail",
            source_project="source-project",
            destination_project="destination-project",
            destination_dataset="functions",
            destination_location="us-east1",
            source_snapshots=[
                RoutineSnapshot.from_routine("source-project", "source_functions", "us-east1", main),
                RoutineSnapshot.from_routine("source-project", "source_functions", "us-east1", helper),
                RoutineSnapshot.from_routine("source-project", "source_functions", "us-east1", unsupported),
            ],
            destination_snapshots=[],
            dictionary=self.dictionary,
        )
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = Path(temporary) / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            reports = write_routine_reports(manifest, manifest_path)
            report = Path(reports["markdown"]).read_text(encoding="utf-8")
        self.assertIn("unknown_dataset.table", report)
        self.assertIn("línea", report)
        self.assertIn("helper", report)
        self.assertIn("spark_proc", report)
        self.assertIn("no seleccionadas", report)

    def test_review_is_dark_and_contains_red_green_diff_without_secret_values(self):
        main = procedure(body="SELECT * FROM source-project.raw_dataset.table;")
        manifest = build_routine_manifest(
            campaign_id="review",
            source_project="source-project",
            destination_project="destination-project",
            destination_dataset="functions",
            destination_location="us-east1",
            source_snapshots=[RoutineSnapshot.from_routine("source-project", "source_functions", "us-east1", main)],
            destination_snapshots=[],
            dictionary=self.dictionary,
        )
        html = render_routine_review(manifest)
        self.assertIn("background:#0b1020", html)
        self.assertIn("diff-add", html)
        self.assertIn("diff-del", html)
        self.assertIn("source-project.raw_dataset.table", html)
        self.assertIn("main_proc", html)

    def test_workbench_gateway_code_uses_rest_without_sql_execution_and_validates_marker(self):
        code = build_workbench_routine_code(
            "GET",
            "projects/source-project/datasets/source_functions/routines/main_proc",
            {"alt": "json"},
            None,
            account="analyst@example.com",
        )
        self.assertIn("bigquery.googleapis.com/bigquery/v2", code)
        self.assertIn("QUERYFLOW_ROUTINE=", code)
        self.assertIn('"application-default", "print-access-token"', code)
        self.assertNotIn("bq query", code)
        pinned_code = build_workbench_routine_code(
            "GET",
            "projects/source-project/datasets/source_functions/routines/main_proc",
            None,
            None,
            account="analyst@example.com",
            gcloud_config_dir="/home/analyst/.config/gcloud",
        )
        self.assertIn("CLOUDSDK_CONFIG", pinned_code)
        self.assertIn("/home/analyst/.config/gcloud", pinned_code)
        self.assertIn("os.path.isdir(payload[\"gcloud_config_dir\"])", pinned_code)
        parsed = parse_workbench_routine_output(
            "noise\nQUERYFLOW_ROUTINE={\"ok\": true, \"response\": {\"routineType\": \"PROCEDURE\"}}\n"
        )
        self.assertEqual("PROCEDURE", parsed["routineType"])
        with self.assertRaisesRegex(RoutineError, "marker"):
            parse_workbench_routine_output("no marker")
        with self.assertRaises(RoutineRateLimitError):
            parse_workbench_routine_output(
                'QUERYFLOW_ROUTINE={"ok": false, "kind": "rate_limit", "status": 429}'
            )

    def test_workbench_websocket_uses_bounded_close_timeout(self):
        class FakeWebSocket:
            def __init__(self):
                self.message_id = ""

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def send(self, payload):
                self.message_id = json.loads(payload)["header"]["msg_id"]

            def recv(self, timeout=None):
                return json.dumps(
                    {
                        "parent_header": {"msg_id": self.message_id},
                        "header": {"msg_type": "status"},
                        "content": {"execution_state": "idle"},
                    }
                )

        fake = FakeWebSocket()
        with patch("websockets.sync.client.connect", return_value=fake) as connect:
            result = _websocket_execute(
                type(
                    "Http",
                    (),
                    {
                        "base_url": "https://workbench.example",
                        "token": "token",
                        "cookie_header": lambda self: "",
                    },
                )(),
                "kernel-id",
                "print('ok')",
                timeout=10,
            )
        self.assertEqual("", result)
        self.assertLessEqual(connect.call_args.kwargs["close_timeout"], 2)
        self.assertGreaterEqual(connect.call_args.kwargs["max_size"], 16 * 1024 * 1024)

        with patch("websockets.sync.client.connect", side_effect=AssertionError("no reconnect")):
            reused = _websocket_execute(
                type(
                    "Http",
                    (),
                    {
                        "base_url": "https://workbench.example",
                        "token": "token",
                        "cookie_header": lambda self: "",
                    },
                )(),
                "kernel-id",
                "print('reused')",
                timeout=10,
                websocket=fake,
            )
        self.assertEqual("", reused)


if __name__ == "__main__":
    unittest.main()
