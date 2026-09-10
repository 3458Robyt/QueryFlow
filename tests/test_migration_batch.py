import hashlib
import json
import contextlib
import io
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import urlopen
from pathlib import Path

from queryflow.catalog import ResourceRef
from queryflow.dataform import DataformClient, ExportedAsset
from queryflow.migration import RouteDictionary
from queryflow.migration import rewrite_task
from queryflow.migration_batch import (
    BatchError,
    BatchSelection,
    build_batch_digest,
    build_batch_manifest,
    destination_repository_id,
    partition_batch_records,
    partition_batch_resources,
    render_batch_report,
    render_batch_review,
    resolve_batch_resources,
    rewrite_asset,
    serve_batch_review,
    validate_batch_manifest,
)
from queryflow.notebooks import write_cell_workspace
from queryflow.cli import main
from queryflow.config import load_config
from queryflow.policy import Policy, evaluate_policy
from queryflow.task import preview_notebook_task
from queryflow.workspace import create_workspace


class MigrationBatchTests(unittest.TestCase):
    def setUp(self):
        self.dictionary = RouteDictionary.from_mapping(
            {
                "schema_version": 1,
                "dictionary_id": "batch-routes",
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

    def test_selection_rejects_duplicates_and_accepts_explicit_names(self):
        selection = BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "batch-20260903",
                "source_project": "source-project",
                "destination_project": "destination-project",
                "location": "us-east1",
                "resources": [
                    {"kind": "shared_query", "display_name": "consolidación_cotizadores"},
                    {"kind": "notebook", "display_name": "Geocoding API"},
                ],
            }
        )
        self.assertEqual(2, len(selection.resources))
        with self.assertRaises(BatchError):
            BatchSelection.from_mapping(
                {
                    **selection.to_dict(),
                    "resources": [
                        {"kind": "shared_query", "display_name": "same"},
                        {"kind": "shared_query", "display_name": "same"},
                    ],
                }
            )

    def test_selection_supports_in_place_update_operation(self):
        selection = BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "notebook-update",
                "operation": "update",
                "source_project": "analytics-487218",
                "destination_project": "analytics-487218",
                "location": "us-east1",
                "resources": [
                    {
                        "kind": "notebook",
                        "name": "projects/analytics-487218/locations/us-east1/repositories/nb-a",
                        "display_name": "Notebook A",
                    }
                ],
            }
        )
        self.assertEqual("update", selection.operation)
        self.assertEqual("update", selection.to_dict()["operation"])

    def test_update_manifest_targets_existing_repository_and_preserves_noop_status(self):
        selection = BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "notebook-update-manifest",
                "operation": "update",
                "source_project": "analytics-487218",
                "destination_project": "analytics-487218",
                "source_location": "us-east1",
                "destination_location": "us-east1",
                "resources": [
                    {"kind": "notebook", "name": "target-change", "display_name": "Change"},
                    {"kind": "notebook", "name": "target-noop", "display_name": "Noop"},
                ],
            }
        )
        notebook_change = json.dumps(
            {
                "cells": [
                    {
                        "cell_type": "code",
                        "metadata": {},
                        "source": ["SELECT * FROM source-project.raw_dataset;\n"],
                    }
                ],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            },
            ensure_ascii=False,
        ).encode()
        notebook_noop = json.dumps(
            {
                "cells": [{"cell_type": "code", "metadata": {}, "source": ["print('ok')\n"]}],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            },
            ensure_ascii=False,
        ).encode()
        change_resource = ResourceRef(
            "notebook", "target-change", "analytics-487218", "us-east1", "Change", "catalog-head"
        )
        noop_resource = ResourceRef(
            "notebook", "target-noop", "analytics-487218", "us-east1", "Noop", "catalog-head"
        )
        manifest = build_batch_manifest(
            selection,
            [change_resource, noop_resource],
            {
                "target-change": {
                    "content": notebook_change,
                    "filename": "content.ipynb",
                    "head_commit": "head-change",
                },
                "target-noop": {
                    "content": notebook_noop,
                    "filename": "content.ipynb",
                    "head_commit": "head-noop",
                },
            },
            self.dictionary,
        )
        records = {item["resource"]["name"]: item for item in manifest["resources"]}
        change = records["target-change"]
        noop = records["target-noop"]
        self.assertEqual("update", manifest["operation"])
        self.assertEqual("ready_to_update", change["status"])
        self.assertEqual("already_compliant", noop["status"])
        self.assertFalse(change["destination"]["collision"])
        self.assertEqual("target-change", change["destination"]["repository"])
        self.assertEqual("head-change", change["source"]["head_commit"])
        self.assertEqual("update", manifest["policy"]["mode"])
        validate_batch_manifest(manifest)

    def test_update_manifest_cannot_change_operation_contract(self):
        selection = BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "notebook-update-contract",
                "operation": "update",
                "source_project": "analytics-487218",
                "destination_project": "analytics-487218",
                "location": "us-east1",
                "resources": [
                    {"kind": "notebook", "name": "target-contract", "display_name": "Contract"}
                ],
            }
        )
        content = json.dumps(
            {
                "cells": [{"cell_type": "code", "metadata": {}, "source": ["print('ok')\n"]}],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ).encode()
        manifest = build_batch_manifest(
            selection,
            [ResourceRef("notebook", "target-contract", "analytics-487218", "us-east1", "Contract", "head")],
            {"target-contract": {"content": content, "filename": "content.ipynb", "head_commit": "head"}},
            self.dictionary,
        )
        manifest["operation"] = "copy"
        manifest["publication_digest"] = build_batch_digest(manifest)
        with self.assertRaisesRegex(BatchError, "operación"):
            validate_batch_manifest(manifest)

    def test_selection_supports_distinct_regions_canonical_names_and_legacy_location(self):
        selection = BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "multi-region",
                "source_project": "source-project",
                "destination_project": "dest-project",
                "source_location": "europe-west1",
                "destination_location": "us-east1",
                "resources": [
                    {
                        "kind": "notebook",
                        "name": "projects/source-project/locations/europe-west1/repositories/notebook-a",
                        "display_name": "Notebook A",
                    }
                ],
            }
        )
        self.assertEqual("europe-west1", selection.source_location)
        self.assertEqual("us-east1", selection.destination_location)
        self.assertEqual(
            "projects/source-project/locations/europe-west1/repositories/notebook-a",
            selection.resources[0].name,
        )
        self.assertEqual("us-east1", selection.to_dict()["location"])

        legacy = BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "legacy",
                "source_project": "source-project",
                "destination_project": "dest-project",
                "location": "us-east1",
                "resources": [{"kind": "notebook", "display_name": "Notebook A"}],
            }
        )
        self.assertEqual("us-east1", legacy.source_location)
        self.assertEqual("us-east1", legacy.destination_location)

    def test_resolve_uses_canonical_name_before_ambiguous_display_name(self):
        chosen_name = "projects/source-project/locations/europe-west1/repositories/notebook-new"
        selection = BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "canonical",
                "source_project": "source-project",
                "destination_project": "dest-project",
                "source_location": "europe-west1",
                "destination_location": "us-east1",
                "resources": [
                    {"kind": "notebook", "name": chosen_name, "display_name": "Duplicado"}
                ],
            }
        )
        catalog = [
            ResourceRef("notebook", "projects/source-project/locations/europe-west1/repositories/notebook-old", "source-project", "europe-west1", "Duplicado", "old"),
            ResourceRef("notebook", chosen_name, "source-project", "europe-west1", "Duplicado", "new"),
        ]
        self.assertEqual([chosen_name], [item.name for item in resolve_batch_resources(selection, catalog)])

    def test_resolve_duplicate_display_name_keeps_latest_and_records_discarded(self):
        selection = BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "dedupe",
                "source_project": "source-project",
                "destination_project": "dest-project",
                "source_location": "europe-west1",
                "destination_location": "us-east1",
                "resources": [{"kind": "notebook", "display_name": "Duplicado"}],
            }
        )
        old = ResourceRef(
            "notebook", "old", "source-project", "europe-west1", "Duplicado", "old-sha",
            metadata={"commit_time": "2026-08-01T00:00:00Z"},
        )
        new = ResourceRef(
            "notebook", "new", "source-project", "europe-west1", "Duplicado", "new-sha",
            metadata={"commit_time": "2026-09-01T00:00:00Z"},
        )
        discarded = []
        resolved = resolve_batch_resources(selection, [old, new], discarded=discarded)
        self.assertEqual(["new"], [item.name for item in resolved])
        self.assertEqual("old", discarded[0]["resource"]["name"])
        self.assertEqual("superseded_duplicate", discarded[0]["status"])

    def test_resolve_duplicate_display_name_blocks_missing_or_tied_commit_time(self):
        selection = BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "dedupe-safe",
                "source_project": "source-project",
                "destination_project": "dest-project",
                "location": "us-east1",
                "resources": [{"kind": "notebook", "display_name": "Duplicado"}],
            }
        )
        for metadata in (
            ({}, {}),
            ({"commit_time": "2026-09-01T00:00:00Z"}, {"commit_time": "2026-09-01T00:00:00Z"}),
        ):
            with self.subTest(metadata=metadata):
                catalog = [
                    ResourceRef("notebook", "b", "source-project", "us-east1", "Duplicado", "b", metadata=metadata[0]),
                    ResourceRef("notebook", "a", "source-project", "us-east1", "Duplicado", "a", metadata=metadata[1]),
                ]
                with self.assertRaisesRegex(BatchError, "a, b"):
                    resolve_batch_resources(selection, catalog)

    def test_resolve_handles_catalog_name_variants_but_preserves_display_name(self):
        requested = BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "batch",
                "source_project": "source-project",
                "destination_project": "dest-project",
                "location": "us-east1",
                "resources": [
                    {"kind": "shared_query", "display_name": "consolidación_cotizadores"},
                    {"kind": "notebook", "display_name": "Geocoding API"},
                    {"kind": "shared_query", "display_name": "Clientes_contacto"},
                ],
            }
        )
        catalog = [
            ResourceRef("shared_query", "q1", "source-project", "us-east1", "consolidacion_cotizadores", "h1"),
            ResourceRef("notebook", "n1", "source-project", "us-east1", "**Geocoding API **", "h2"),
            ResourceRef("shared_query", "q2", "source-project", "us-east1", "Clientes_Contacto", "h3"),
        ]
        resolved = resolve_batch_resources(requested, catalog)
        self.assertEqual(["q1", "n1", "q2"], [item.name for item in resolved])

    def test_manifest_preserves_requested_visible_name(self):
        selection = BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "batch",
                "source_project": "source-project",
                "destination_project": "dest-project",
                "location": "us-east1",
                "resources": [{"kind": "shared_query", "display_name": "consolidación_cotizadores"}],
            }
        )
        resource = ResourceRef("shared_query", "q1", "source-project", "us-east1", "consolidacion_cotizadores", "head")
        manifest = build_batch_manifest(
            selection,
            [resource],
            {"q1": {"content": b"SELECT 1", "filename": "content.sql", "head_commit": "head"}},
            self.dictionary,
        )
        self.assertEqual("consolidación_cotizadores", manifest["resources"][0]["destination"]["display_name"])

    def test_batch_accepts_unknown_route_and_marks_human_review(self):
        selection = BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "batch-review",
                "source_project": "source-project",
                "destination_project": "dest-project",
                "location": "us-east1",
                "resources": [{"kind": "shared_query", "display_name": "q"}],
            }
        )
        resource = ResourceRef("shared_query", "q", "source-project", "us-east1", "q", "head")
        manifest = build_batch_manifest(
            selection,
            [resource],
            {"q": {"content": b"SELECT * FROM source-project.unmapped.table;", "filename": "content.sql", "head_commit": "head"}},
            self.dictionary,
        )
        record = manifest["resources"][0]
        self.assertEqual("ready_to_publish", record["status"])
        self.assertEqual("read_only", record["classification"]["statement_class"])
        self.assertTrue(record["review"]["required"])
        self.assertIn("unknown_route", record["review"]["reasons"])
        self.assertTrue(record["migration_eligible"])
        self.assertFalse(record["execution_eligible"])

    def test_batch_can_retain_strict_incident_mode_when_requested(self):
        selection = BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "batch-strict",
                "source_project": "source-project",
                "destination_project": "dest-project",
                "location": "us-east1",
                "publish_incidents": False,
                "resources": [{"kind": "shared_query", "display_name": "q"}],
            }
        )
        resource = ResourceRef("shared_query", "q", "source-project", "us-east1", "q", "head")
        manifest = build_batch_manifest(
            selection,
            [resource],
            {"q": {"content": b"INSERT INTO source-project.raw_dataset.t VALUES (1);", "filename": "content.sql", "head_commit": "head"}},
            self.dictionary,
        )
        self.assertEqual("blocked", manifest["resources"][0]["status"])
        self.assertFalse(manifest["resources"][0]["migration_eligible"])

    def test_batch_accepts_mutating_and_unknown_sql_as_copy_only_review_items(self):
        cases = (("INSERT INTO source-project.raw_dataset.t VALUES (1);", "mutating"), ("CALL procedure_name();", "mutating"), ("THIS IS NOT SQL", "unknown"))
        for sql, statement_class in cases:
            with self.subTest(statement_class=statement_class):
                selection = BatchSelection.from_mapping(
                    {
                        "schema_version": 1,
                        "campaign_id": "batch-review",
                        "source_project": "source-project",
                        "destination_project": "dest-project",
                        "location": "us-east1",
                        "resources": [{"kind": "shared_query", "display_name": "q"}],
                    }
                )
                resource = ResourceRef("shared_query", "q", "source-project", "us-east1", "q", "head")
                manifest = build_batch_manifest(
                    selection,
                    [resource],
                    {"q": {"content": sql.encode(), "filename": "content.sql", "head_commit": "head"}},
                    self.dictionary,
                )
                record = manifest["resources"][0]
                self.assertEqual("ready_to_publish", record["status"])
                self.assertEqual(statement_class, record["classification"]["statement_class"])
                self.assertTrue(record["review"]["required"])
                self.assertIn(statement_class, record["review"]["reasons"])
                self.assertFalse(record["execution_eligible"])

    def test_batch_accepts_dynamic_notebook_but_requires_review(self):
        notebook = json.dumps(
            {
                "cells": [
                    {"cell_type": "code", "metadata": {}, "source": ["table = input('table')\n", "sql = f'SELECT * FROM {table}'\n", "client.query(sql)\n"]}
                ],
                "metadata": {"kernelspec": {"language": "python"}},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ).encode()
        selection = BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "batch-review",
                "source_project": "source-project",
                "destination_project": "dest-project",
                "location": "us-east1",
                "resources": [{"kind": "notebook", "display_name": "dynamic"}],
            }
        )
        resource = ResourceRef("notebook", "n1", "source-project", "us-east1", "dynamic", "head")
        manifest = build_batch_manifest(
            selection,
            [resource],
            {"n1": {"content": notebook, "filename": "content.ipynb", "head_commit": "head"}},
            self.dictionary,
        )
        record = manifest["resources"][0]
        self.assertEqual("ready_to_publish", record["status"])
        self.assertEqual("dynamic", record["classification"]["statement_class"])
        self.assertTrue(record["review"]["required"])
        self.assertIn("dynamic_sql", record["review"]["reasons"])
        self.assertFalse(record["execution_eligible"])

    def test_notebook_without_sql_is_copyable_when_structure_is_valid(self):
        notebook = json.dumps(
            {
                "cells": [{"cell_type": "code", "metadata": {}, "source": ["print('hello')\n"]}],
                "metadata": {"kernelspec": {"language": "python"}},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ).encode()
        selection = BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "notebook-no-sql",
                "source_project": "source-project",
                "destination_project": "dest-project",
                "location": "us-east1",
                "resources": [{"kind": "notebook", "display_name": "python-only"}],
            }
        )
        resource = ResourceRef("notebook", "python-only", "source-project", "us-east1", "python-only", "head")
        manifest = build_batch_manifest(
            selection,
            [resource],
            {"python-only": {"content": notebook, "filename": "content.ipynb", "head_commit": "head"}},
            self.dictionary,
        )
        self.assertEqual("not_applicable", manifest["resources"][0]["classification"]["statement_class"])
        self.assertEqual("ready_to_publish", manifest["resources"][0]["status"])

    def test_batch_keeps_empty_and_embedded_secret_as_hard_blocks(self):
        selection = BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "batch-review",
                "source_project": "source-project",
                "destination_project": "dest-project",
                "location": "us-east1",
                "resources": [{"kind": "shared_query", "display_name": "q"}],
            }
        )
        resource = ResourceRef("shared_query", "q", "source-project", "us-east1", "q", "head")
        for content, blocker_kind in ((b"-- comentario\n", "empty_sql"), (b"SELECT 'ok'; password = 'super-secret-value';", "embedded_secret")):
            with self.subTest(blocker_kind=blocker_kind):
                manifest = build_batch_manifest(
                    selection,
                    [resource],
                    {"q": {"content": content, "filename": "content.sql", "head_commit": "head"}},
                    self.dictionary,
                )
                record = manifest["resources"][0]
                self.assertEqual("blocked", record["status"])
                self.assertIn(blocker_kind, [item["kind"] for item in record["blockers"]])
                self.assertFalse(record["migration_eligible"])

    def test_batch_digest_binds_classification_and_review_contract(self):
        selection = BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "batch-review",
                "source_project": "source-project",
                "destination_project": "dest-project",
                "location": "us-east1",
                "resources": [{"kind": "shared_query", "display_name": "q"}],
            }
        )
        resource = ResourceRef("shared_query", "q", "source-project", "us-east1", "q", "head")
        manifest = build_batch_manifest(
            selection,
            [resource],
            {"q": {"content": b"INSERT INTO source-project.raw_dataset.t VALUES (1);", "filename": "content.sql", "head_commit": "head"}},
            self.dictionary,
        )
        changed = json.loads(json.dumps(manifest))
        changed["resources"][0]["review"]["reasons"] = ["manually_changed"]
        self.assertNotEqual(build_batch_digest(manifest), build_batch_digest(changed))

    def test_dataform_copy_carries_review_labels_without_overriding_asset_type(self):
        requests = []

        def transport(method, resource, query, body):
            requests.append((method, resource, query, body))
            if resource.endswith(":commit"):
                return {"commitSha": "new-commit"}
            return {}

        source = ResourceRef(
            "shared_query",
            "projects/source-project/locations/us-east1/repositories/source",
            "source-project",
            "us-east1",
            "q",
            "head",
            metadata={"labels": {"owner": "analytics"}},
        )
        client = DataformClient("analyst@example.com", "dest-project", source_project="source-project", transport=transport)
        client.create_copy(
            source=ExportedAsset(source, "content.sql", b"SELECT 1", {"labels": {"owner": "analytics"}}, "head"),
            destination_project="dest-project",
            destination_repository_id="q-copy",
            display_name="q",
            content=b"SELECT 1",
            author_name="QueryFlow",
            author_email="analyst@example.com",
            labels={"queryflow_review": "required", "queryflow_state": "pending", "single-file-asset-type": "notebook"},
        )
        create_body = requests[0][3]
        self.assertEqual("sql", create_body["labels"]["single-file-asset-type"])
        self.assertEqual("required", create_body["labels"]["queryflow_review"])
        self.assertEqual("pending", create_body["labels"]["queryflow_state"])
        self.assertEqual("analytics", create_body["labels"]["owner"])

    def test_dataform_copy_always_uses_explicit_destination_location(self):
        requests = []

        def transport(method, resource, query, body):
            requests.append((method, resource, query, body))
            return {"commitSha": "new-commit"} if resource.endswith(":commit") else {}

        source = ResourceRef(
            "notebook",
            "projects/source-project/locations/europe-west1/repositories/source",
            "source-project",
            "europe-west1",
            "Notebook",
            "head",
        )
        client = DataformClient("analyst@example.com", "dest-project", source_project="source-project", transport=transport)
        result = client.create_copy(
            source=ExportedAsset(source, "content.ipynb", b"{}", {}, "head"),
            destination_project="dest-project",
            destination_location="us-east1",
            destination_repository_id="notebook-copy",
            display_name="Notebook",
            content=b"{}",
            author_name="QueryFlow",
            author_email="analyst@example.com",
        )
        self.assertEqual(
            "projects/dest-project/locations/us-east1/repositories/notebook-copy",
            result["repository"],
        )
        self.assertTrue(all("locations/europe-west1" not in resource for _, resource, _, _ in requests))

    def test_resolve_blocks_ambiguous_resource(self):
        requested = BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "batch",
                "source_project": "source-project",
                "destination_project": "dest-project",
                "location": "us-east1",
                "resources": [{"kind": "shared_query", "display_name": "q"}],
            }
        )
        catalog = [
            ResourceRef("shared_query", "q1", "source-project", "us-east1", "q", "h1"),
            ResourceRef("shared_query", "q2", "source-project", "us-east1", "q", "h2"),
        ]
        with self.assertRaises(BatchError):
            resolve_batch_resources(requested, catalog)

    def test_repository_id_is_safe_and_collision_is_hashed(self):
        first = destination_repository_id("Renovaciones Condominios", "shared_query", existing_ids=set())
        second = destination_repository_id("Renovaciones Condominios", "shared_query", existing_ids={first})
        self.assertRegex(first, r"^[a-z][a-z0-9-]{1,62}$")
        self.assertRegex(second, r"^[a-z][a-z0-9-]{1,62}$")
        self.assertNotEqual(first, second)

    def test_partition_batch_resources_is_deterministic_and_respects_quota(self):
        resources = [
            ResourceRef("notebook", "n2", "source-project", "us-east1", "Notebook B", "h2"),
            ResourceRef("shared_query", "q2", "source-project", "us-east1", "b", "h2"),
            ResourceRef("shared_query", "q1", "source-project", "us-east1", "A", "h1"),
        ]
        batches = partition_batch_resources(resources, batch_size=2)
        self.assertEqual([["q1", "q2"], ["n2"]], [[item.name for item in batch] for batch in batches])
        with self.assertRaises(BatchError):
            partition_batch_resources(resources, batch_size=0)

    def test_partition_batch_records_requires_explicit_pending_opt_in(self):
        records = [
            {"resource": {"display_name": "published"}, "status": "published"},
            {"resource": {"display_name": "deferred"}, "status": "pending"},
            {"resource": {"display_name": "ready"}, "status": "prepared"},
        ]
        with self.assertRaisesRegex(BatchError, "skip-pending"):
            partition_batch_records(records)
        active, published, pending = partition_batch_records(records, skip_pending=True)
        self.assertEqual(["ready"], [item["resource"]["display_name"] for item in active])
        self.assertEqual(["published"], [item["resource"]["display_name"] for item in published])
        self.assertEqual(["deferred"], [item["resource"]["display_name"] for item in pending])

    def test_manifest_rate_limit_cannot_exceed_dataform_quota(self):
        selection = BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "batch",
                "source_project": "source-project",
                "destination_project": "dest-project",
                "location": "us-east1",
                "resources": [{"kind": "shared_query", "display_name": "q"}],
            }
        )
        resource = ResourceRef("shared_query", "q", "source-project", "us-east1", "q", "head")
        with self.assertRaises(BatchError):
            build_batch_manifest(
                selection,
                [resource],
                {"q": {"content": b"SELECT 1", "filename": "content.sql", "head_commit": "head"}},
                self.dictionary,
                requests_per_minute=301,
            )

    def test_rewrite_asset_reports_line_and_dynamic_cell_without_sql_execution(self):
        query = b"SELECT * FROM `source-project.raw_dataset.table_a`;\nSELECT * FROM source-project.unknown.table_b;\n"
        result = rewrite_asset("shared_query", "query.sql", query, self.dictionary)
        self.assertIn(b"raw-487218.raw_dataset.table_a", result.proposed_content)
        self.assertEqual(1, len(result.incidents))
        self.assertEqual(2, result.incidents[0]["line"])
        self.assertFalse(result.sql_executed)

        notebook = json.dumps(
            {
                "cells": [
                    {"cell_type": "markdown", "metadata": {}, "source": ["source-project.raw_dataset.table_a"]},
                    {
                        "cell_type": "code",
                        "metadata": {},
                        "source": ["sql = f'SELECT * FROM source-project.raw_dataset.{table}'\n", "client.query(sql)\n"],
                    },
                ],
                "metadata": {"kernelspec": {"language": "python"}},
                "nbformat": 4,
                "nbformat_minor": 5,
            },
            ensure_ascii=False,
        ).encode()
        notebook_result = rewrite_asset("notebook", "content.ipynb", notebook, self.dictionary)
        self.assertEqual((1,), notebook_result.dynamic_cells)
        self.assertIn("source-project.raw_dataset.table_a", notebook_result.proposed_content.decode())
        self.assertNotIn("raw-487218.raw_dataset.table_a", notebook_result.proposed_content.decode().split("cells")[0])

    def test_credential_like_literal_is_a_blocker_without_leaking_value(self):
        result = rewrite_asset(
            "shared_query",
            "query.sql",
            b"SELECT 'ok';\npassword = 'super-secret-value';\n",
            self.dictionary,
        )
        self.assertEqual(1, len(result.security_blockers))
        self.assertNotIn("super-secret-value", json.dumps(result.to_dict()))

    def test_notebook_structure_and_all_cell_sources_are_validated(self):
        malformed = json.dumps(
            {
                "cells": [{"cell_type": "markdown", "source": ["ok", 7]}],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ).encode()
        with self.assertRaises(BatchError):
            rewrite_asset("notebook", "content.ipynb", malformed, self.dictionary)

    def test_empty_or_malformed_notebook_is_reported_as_blocked_without_content(self):
        selection = BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "batch-invalid",
                "source_project": "source-project",
                "destination_project": "dest-project",
                "location": "us-east1",
                "resources": [{"kind": "notebook", "display_name": "empty"}],
            }
        )
        resource = ResourceRef("notebook", "n-empty", "source-project", "us-east1", "empty", "head")
        manifest = build_batch_manifest(
            selection,
            [resource],
            {"n-empty": {"content": b"", "filename": "content.ipynb", "head_commit": "head"}},
            self.dictionary,
        )
        record = manifest["resources"][0]
        self.assertEqual("blocked", record["status"])
        self.assertIn("empty_content", [item["kind"] for item in record["blockers"]])

    def test_rewrite_task_preserves_notebook_hash_when_no_routes_change(self):
        notebook = json.dumps(
            {
                "cells": [
                    {"cell_type": "markdown", "metadata": {}, "source": ["# unchanged\n"]},
                    {"cell_type": "code", "metadata": {}, "source": ["print('unchanged')\n"]},
                ],
                "metadata": {"kernelspec": {"language": "python"}},
                "nbformat": 4,
                "nbformat_minor": 5,
            },
            ensure_ascii=False,
            indent=1,
        ).encode()
        resource = ResourceRef(
            "notebook",
            "projects/source/locations/us-east1/repositories/nb1",
            "source-project",
            "us-east1",
            "unchanged-notebook",
            "head",
        )
        with tempfile.TemporaryDirectory() as temp:
            task = create_workspace(
                root=Path(temp),
                task_id="unchanged-notebook",
                resource=resource,
                content=notebook,
                filename="content.ipynb",
                mode="copy",
            )
            write_cell_workspace(notebook, task / "cells")
            report = rewrite_task(task, self.dictionary, apply=True)
        self.assertEqual(hashlib.sha256(notebook).hexdigest(), report.proposed_sha256)

    def test_preview_notebook_task_preserves_bytes_when_no_routes_change(self):
        notebook = json.dumps(
            {
                "cells": [
                    {"cell_type": "markdown", "metadata": {}, "source": ["# unchanged\n"]},
                    {"cell_type": "code", "metadata": {}, "source": ["print('unchanged')\n"]},
                ],
                "metadata": {"kernelspec": {"language": "python"}},
                "nbformat": 4,
                "nbformat_minor": 5,
            },
            ensure_ascii=False,
            indent=1,
        ).encode()
        resource = ResourceRef(
            "notebook",
            "projects/source/locations/us-east1/repositories/nb2",
            "source-project",
            "us-east1",
            "preview-notebook",
            "head",
        )
        with tempfile.TemporaryDirectory() as temp:
            task = create_workspace(
                root=Path(temp),
                task_id="preview-notebook",
                resource=resource,
                content=notebook,
                filename="content.ipynb",
                mode="copy",
            )
            write_cell_workspace(notebook, task / "cells")
            preview = preview_notebook_task(task)
        self.assertEqual(notebook, preview)

    def test_exported_filename_cannot_escape_the_task(self):
        with self.assertRaises(BatchError):
            rewrite_asset("shared_query", "../content.sql", b"SELECT 1", self.dictionary)

    def test_batch_profile_and_policy_are_explicit(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.toml"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, main(["init", "--profile", "migration-batch", "--path", str(path), "--json"]))
            config = load_config(path)
        self.assertEqual("migration-batch", config.mode)
        policy = Policy(allow_migration_batch=True)
        decision = evaluate_policy(policy, operation="campaign_publish", resource_kind="shared_query", mode="copy")
        self.assertTrue(decision.allowed)

    def test_digest_is_canonical_and_report_is_analyst_friendly(self):
        payload = {
            "schema_version": 1,
            "campaign_id": "batch",
            "source_project": "source-project",
            "destination_project": "dest-project",
            "dictionary_sha256": self.dictionary.dictionary_sha256,
            "resources": [{"kind": "shared_query", "display_name": "q", "status": "ready"}],
            "policy": {"dry_run": False, "warnings": "accept"},
        }
        digest_one = build_batch_digest(payload)
        digest_two = build_batch_digest({**payload, "resources": list(reversed(payload["resources"]))})
        self.assertEqual(digest_one, digest_two)
        self.assertEqual(64, len(digest_one))
        report = render_batch_report({**payload, "publication_digest": digest_one, "warnings": []})
        self.assertIn("batch", report)
        self.assertIn("No se ejecutó SQL", report)
        self.assertIn(digest_one, report)

    def test_digest_ignores_resource_and_warning_presentation_order(self):
        base = {
            "schema_version": 1,
            "campaign_id": "batch",
            "source_project": "source-project",
            "destination_project": "dest-project",
            "location": "us-east1",
            "dictionary_sha256": self.dictionary.dictionary_sha256,
            "selection": {"resources": [{"kind": "shared_query", "display_name": "b"}, {"kind": "notebook", "display_name": "a"}]},
            "resources": [
                {"resource": {"kind": "shared_query", "name": "b"}, "warnings": []},
                {"resource": {"kind": "notebook", "name": "a"}, "warnings": []},
            ],
            "warnings": [
                {"resource": {"kind": "shared_query", "name": "b"}, "kind": "unknown_route", "path": "x", "line": 2},
                {"resource": {"kind": "notebook", "name": "a"}, "kind": "dynamic_sql", "path": "y", "line": 1},
            ],
            "policy": {"no_sql_execution": True, "dry_run": {"skipped": True}},
        }
        shuffled = {**base, "resources": list(reversed(base["resources"])), "warnings": list(reversed(base["warnings"]))}
        shuffled["selection"] = {"resources": list(reversed(base["selection"]["resources"]))}
        self.assertEqual(build_batch_digest(base), build_batch_digest(shuffled))

    def test_destination_name_collision_is_visible_and_blocks_strict_validation(self):
        selection = BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "batch",
                "source_project": "source-project",
                "destination_project": "dest-project",
                "location": "us-east1",
                "resources": [{"kind": "shared_query", "display_name": "q"}],
            }
        )
        source = ResourceRef("shared_query", "q-source", "source-project", "us-east1", "q", "head")
        destination = ResourceRef("shared_query", "q-dest", "dest-project", "us-east1", "q", "head")
        manifest = build_batch_manifest(
            selection,
            [source],
            {"q-source": {"content": b"SELECT 1", "filename": "content.sql", "head_commit": "head"}},
            self.dictionary,
            destination_resources=[destination],
        )
        self.assertEqual("blocked", manifest["status"])
        with self.assertRaises(BatchError):
            from queryflow.migration_batch import validate_batch_manifest

            validate_batch_manifest(manifest)

    def test_cross_kind_destination_name_collision_is_visible(self):
        selection = BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "cross-kind-collision",
                "source_project": "source-project",
                "destination_project": "dest-project",
                "location": "us-east1",
                "resources": [{"kind": "notebook", "display_name": "q"}],
            }
        )
        source = ResourceRef(
            "notebook",
            "projects/source-project/locations/us-east1/repositories/nb-q",
            "source-project",
            "us-east1",
            "q",
            "head",
        )
        destination_shared_query = ResourceRef(
            "shared_query",
            "projects/dest-project/locations/us-east1/repositories/q",
            "dest-project",
            "us-east1",
            "q",
            "dest-head",
        )
        notebook_content = json.dumps(
            {
                "cells": [{"cell_type": "code", "metadata": {}, "source": ["print('q')\n"]}],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            },
            ensure_ascii=False,
        ).encode()
        manifest = build_batch_manifest(
            selection,
            [source],
            {source.name: {"content": notebook_content, "filename": "content.ipynb", "head_commit": "head"}},
            self.dictionary,
            destination_resources=[destination_shared_query],
        )
        record = manifest["resources"][0]
        self.assertEqual("destination_conflict", record["status"])
        self.assertTrue(record["destination"]["collision"])
        self.assertEqual("shared_query", record["destination"]["collision_with"][0]["kind"])
        self.assertEqual("q", record["destination"]["collision_with"][0]["display_name"])

    def test_manifest_reconciles_destination_content_byte_for_byte(self):
        selection = BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "reconcile",
                "source_project": "source-project",
                "destination_project": "dest-project",
                "source_location": "europe-west1",
                "destination_location": "us-east1",
                "resources": [
                    {"kind": "shared_query", "name": "source-same", "display_name": "same"},
                    {"kind": "shared_query", "name": "source-conflict", "display_name": "conflict"},
                    {"kind": "shared_query", "name": "source-new", "display_name": "new"},
                ],
            }
        )
        sources = [
            ResourceRef("shared_query", "source-same", "source-project", "europe-west1", "same", "s1"),
            ResourceRef("shared_query", "source-conflict", "source-project", "europe-west1", "conflict", "s2"),
            ResourceRef("shared_query", "source-new", "source-project", "europe-west1", "new", "s3"),
        ]
        destinations = [
            ResourceRef("shared_query", "dest-same", "dest-project", "us-east1", "same", "d1"),
            ResourceRef("shared_query", "dest-conflict", "dest-project", "us-east1", "conflict", "d2"),
        ]
        manifest = build_batch_manifest(
            selection,
            sources,
            {
                "source-same": {"content": b"SELECT 1\n", "filename": "content.sql", "head_commit": "s1"},
                "source-conflict": {"content": b"SELECT 2\n", "filename": "content.sql", "head_commit": "s2"},
                "source-new": {"content": b"SELECT 3\n", "filename": "content.sql", "head_commit": "s3"},
            },
            self.dictionary,
            destination_resources=destinations,
            destination_assets={
                "dest-same": {"content": b"SELECT 1\n", "filename": "content.sql", "head_commit": "d1"},
                "dest-conflict": {"content": b"SELECT 2\r\n", "filename": "content.sql", "head_commit": "d2"},
            },
        )
        records = {item["resource"]["name"]: item for item in manifest["resources"]}
        self.assertEqual("already_present", records["source-same"]["status"])
        self.assertEqual("destination_conflict", records["source-conflict"]["status"])
        self.assertEqual("ready_to_publish", records["source-new"]["status"])
        self.assertEqual("us-east1", records["source-new"]["destination"]["location"])
        self.assertEqual("europe-west1", manifest["source_location"])
        self.assertEqual("us-east1", manifest["destination_location"])
        self.assertEqual("dest-same", records["source-same"]["destination"]["repository"])

    def test_cli_batch_inventory_uses_local_snapshot_and_never_enables_writes(self):
        resource = ResourceRef(
            "shared_query",
            "projects/source-project/locations/us-east1/repositories/q1",
            "source-project",
            "us-east1",
            "q1",
            "head",
        )
        selection = {
            "schema_version": 1,
            "campaign_id": "batch-cli",
            "source_project": "source-project",
            "destination_project": "dest-project",
            "location": "us-east1",
            "resources": [{"kind": "shared_query", "name": resource.name, "display_name": "q1"}],
        }
        dictionary = {
            "schema_version": 1,
            "dictionary_id": "routes",
            "scope": {"source_project": "source-project"},
            "mappings": [
                {
                    "id": "unused-route",
                    "zone": "raw",
                    "old": "source-project.unused",
                    "new": "dest-project.unused",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            selection_path = root / "selection.json"
            dictionary_path = root / "routes.json"
            catalog_path = root / "catalog.json"
            content_dir = root / "snapshots"
            config_path = root / "config.json"
            selection_path.write_text(json.dumps(selection), encoding="utf-8")
            dictionary_path.write_text(json.dumps(dictionary), encoding="utf-8")
            catalog_path.write_text(
                json.dumps({"generated_at": "now", "resources": [resource.to_dict()]}),
                encoding="utf-8",
            )
            content_dir.mkdir()
            (content_dir / f"{hashlib.sha256(resource.name.encode()).hexdigest()}.sql").write_bytes(
                b"SELECT 1\n"
            )
            config_path.write_text(
                json.dumps(
                    {
                        "workspace_root": str(root / "tasks"),
                        "catalog_path": str(catalog_path),
                        "gcloud_config_dir": str(root / "gcloud"),
                    }
                ),
                encoding="utf-8",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = main(
                    [
                        "migration",
                        "batch",
                        "inventory",
                        "--selection-file",
                        str(selection_path),
                        "--dictionary",
                        str(dictionary_path),
                        "--catalog",
                        str(catalog_path),
                        "--content-dir",
                        str(content_dir),
                        "--output",
                        str(root / "manifest.json"),
                        "--config",
                        str(config_path),
                        "--json",
                    ]
                )
            self.assertEqual(0, status)
            saved = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(saved["policy"]["dry_run"]["skipped"])
            self.assertFalse(saved["policy"]["dry_run"].get("executed", False))
            self.assertFalse(saved["inventory"].get("sql_executed", False))
            self.assertFalse(json.loads(output.getvalue())["write_enabled"])
            self.assertEqual("planned", saved["status"])

    def test_cli_batch_inventory_update_uses_current_destination_as_baseline(self):
        resource = ResourceRef(
            "notebook",
            "projects/dest-project/locations/us-east1/repositories/notebook-a",
            "dest-project",
            "us-east1",
            "Notebook A",
            "head",
        )
        selection = {
            "schema_version": 1,
            "campaign_id": "batch-update-cli",
            "operation": "update",
            "source_project": "dest-project",
            "destination_project": "dest-project",
            "location": "us-east1",
            "resources": [{"kind": "notebook", "name": resource.name, "display_name": "Notebook A"}],
        }
        dictionary = {
            "schema_version": 1,
            "dictionary_id": "routes",
            "scope": {"source_project": "dest-project"},
            "mappings": [
                {
                    "id": "old-route",
                    "zone": "raw",
                    "old": "dest-project.raw_dataset",
                    "new": "dest-project.staging",
                }
            ],
        }
        notebook = json.dumps(
            {
                "cells": [
                    {
                        "cell_type": "code",
                        "metadata": {},
                        "source": ["SELECT * FROM dest-project.raw_dataset.table_a;\n"],
                    }
                ],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            },
            ensure_ascii=False,
        ).encode()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            selection_path = root / "selection.json"
            dictionary_path = root / "routes.json"
            catalog_path = root / "catalog.json"
            content_dir = root / "snapshots"
            config_path = root / "config.json"
            selection_path.write_text(json.dumps(selection), encoding="utf-8")
            dictionary_path.write_text(json.dumps(dictionary), encoding="utf-8")
            catalog_path.write_text(
                json.dumps({"generated_at": "now", "resources": [resource.to_dict()]}),
                encoding="utf-8",
            )
            content_dir.mkdir()
            (content_dir / hashlib.sha256(resource.name.encode()).hexdigest()).write_bytes(notebook)
            config_path.write_text(
                json.dumps(
                    {
                        "mode": "full-access",
                        "allow_update_existing": True,
                        "workspace_root": str(root / "tasks"),
                        "catalog_path": str(catalog_path),
                        "gcloud_config_dir": str(root / "gcloud"),
                    }
                ),
                encoding="utf-8",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = main(
                    [
                        "migration",
                        "batch",
                        "inventory",
                        "--selection-file",
                        str(selection_path),
                        "--dictionary",
                        str(dictionary_path),
                        "--catalog",
                        str(catalog_path),
                        "--content-dir",
                        str(content_dir),
                        "--output",
                        str(root / "manifest.json"),
                        "--config",
                        str(config_path),
                        "--json",
                    ]
                )
            self.assertEqual(0, status)
            saved = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            record = saved["resources"][0]
            self.assertEqual("update", saved["operation"])
            self.assertEqual("ready_to_update", record["status"])
            self.assertEqual(resource.name, record["destination"]["repository"])
            self.assertEqual("head", record["resource"]["fingerprint"])
            self.assertFalse(json.loads(output.getvalue())["write_enabled"])

    def test_cli_batch_update_prepare_and_run_commits_same_repository(self):
        resource = ResourceRef(
            "notebook",
            "projects/dest-project/locations/us-east1/repositories/notebook-a",
            "dest-project",
            "us-east1",
            "Notebook A",
            "head",
        )
        selection = {
            "schema_version": 1,
            "campaign_id": "batch-update-run",
            "operation": "update",
            "source_project": "dest-project",
            "destination_project": "dest-project",
            "location": "us-east1",
            "resources": [{"kind": "notebook", "name": resource.name, "display_name": "Notebook A"}],
        }
        dictionary = {
            "schema_version": 1,
            "dictionary_id": "routes",
            "scope": {"source_project": "dest-project"},
            "mappings": [
                {
                    "id": "old-route",
                    "zone": "raw",
                    "old": "dest-project.raw_dataset",
                    "new": "dest-project.staging",
                }
            ],
        }
        current = json.dumps(
            {
                "cells": [
                    {
                        "cell_type": "code",
                        "metadata": {},
                        "source": ["SELECT * FROM dest-project.raw_dataset.table_a;\n"],
                    }
                ],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            },
            ensure_ascii=False,
        ).encode()

        initial_current = current

        class FakeClient:
            current = initial_current
            update_calls = []

            def __init__(self, *args, **kwargs):
                self.request_stats = type("Stats", (), {"to_dict": lambda self: {"requests_attempted": 0}})()

            def set_gcloud_context(self, _context):
                return None

            def export(self, requested):
                return ExportedAsset(requested, "content.ipynb", self.__class__.current, {}, "head")

            def update_file(self, repository, filename, content, **kwargs):
                self.__class__.update_calls.append((repository, filename, content, kwargs))
                self.__class__.current = content
                return {"repository": repository, "filename": filename, "commit_sha": "updated-head"}

            def read_file(self, repository, filename):
                return self.__class__.current

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            selection_path = root / "selection.json"
            dictionary_path = root / "routes.json"
            catalog_path = root / "catalog.json"
            content_dir = root / "snapshots"
            config_path = root / "config.json"
            manifest_path = root / "manifest.json"
            selection_path.write_text(json.dumps(selection), encoding="utf-8")
            dictionary_path.write_text(json.dumps(dictionary), encoding="utf-8")
            catalog_path.write_text(
                json.dumps({"generated_at": "now", "resources": [resource.to_dict()]}),
                encoding="utf-8",
            )
            content_dir.mkdir()
            (content_dir / hashlib.sha256(resource.name.encode()).hexdigest()).write_bytes(current)
            config_path.write_text(
                json.dumps(
                    {
                        "mode": "full-access",
                        "allow_update_existing": True,
                        "source_projects": ["dest-project"],
                        "destination_projects": ["dest-project"],
                        "workspace_root": str(root / "tasks"),
                        "catalog_path": str(catalog_path),
                        "audit_root": str(root / "audit"),
                        "gcloud_config_dir": str(root / "gcloud"),
                    }
                ),
                encoding="utf-8",
            )
            with patch("queryflow.cli.DataformClient", FakeClient):
                self.assertEqual(
                    0,
                    main(
                        [
                            "migration",
                            "batch",
                            "inventory",
                            "--selection-file",
                            str(selection_path),
                            "--dictionary",
                            str(dictionary_path),
                            "--catalog",
                            str(catalog_path),
                            "--content-dir",
                            str(content_dir),
                            "--output",
                            str(manifest_path),
                            "--config",
                            str(config_path),
                            "--json",
                        ]
                    ),
                )
                self.assertEqual(
                    0,
                    main(
                        [
                            "migration",
                            "batch",
                            "prepare",
                            "--manifest",
                            str(manifest_path),
                            "--dictionary",
                            str(dictionary_path),
                            "--account",
                            "analyst@example.com",
                            "--config",
                            str(config_path),
                            "--json",
                        ]
                    ),
                )
                planned = json.loads(manifest_path.read_text(encoding="utf-8"))
                self.assertEqual("prepared", planned["execution"][resource.name]["status"])
                digest = planned["publication_digest"]
                self.assertEqual(
                    0,
                    main(
                        [
                            "migration",
                            "batch",
                            "run",
                            "--manifest",
                            str(manifest_path),
                            "--dictionary",
                            str(dictionary_path),
                            "--execute-migration",
                            "--approved-digest",
                            digest,
                            "--account",
                            "analyst@example.com",
                            "--config",
                            str(config_path),
                            "--json",
                        ]
                    ),
                )
        self.assertEqual(1, len(FakeClient.update_calls))
        self.assertEqual(resource.name, FakeClient.update_calls[0][0])
        self.assertEqual("content.ipynb", FakeClient.update_calls[0][1])
        self.assertIn(b"dest-project.staging", FakeClient.update_calls[0][2])


if __name__ == "__main__":
    unittest.main()
