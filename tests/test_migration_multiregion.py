import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from queryflow.catalog import ResourceRef
from queryflow.cli import main
from queryflow.dataform import DataformClient, ExportedAsset
from queryflow.migration import RouteDictionary
from queryflow.migration_batch import (
    BatchError,
    BatchSelection,
    build_batch_manifest,
    resolve_batch_resources,
)


class MigrationMultiregionTests(unittest.TestCase):
    def setUp(self):
        self.dictionary = RouteDictionary.from_mapping(
            {
                "schema_version": 1,
                "dictionary_id": "multiregion-routes",
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
        )

    def test_selection_supports_distinct_regions_canonical_names_and_legacy_location(self):
        canonical_name = "projects/source-project/locations/europe-west1/repositories/notebook-a"
        selection = BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "multi-region",
                "source_project": "source-project",
                "destination_project": "dest-project",
                "source_location": "europe-west1",
                "destination_location": "us-east1",
                "resources": [
                    {"kind": "notebook", "name": canonical_name, "display_name": "Notebook A"}
                ],
            }
        )
        self.assertEqual("europe-west1", selection.source_location)
        self.assertEqual("us-east1", selection.destination_location)
        self.assertEqual(canonical_name, selection.resources[0].name)
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

    def test_cli_inventory_accepts_explicit_source_and_destination_regions(self):
        canonical_name = "projects/source-project/locations/europe-west1/repositories/q1"
        resource = ResourceRef("shared_query", canonical_name, "source-project", "europe-west1", "q1", "head")
        selection = {
            "schema_version": 1,
            "campaign_id": "batch-cli-regions",
            "source_project": "source-project",
            "destination_project": "dest-project",
            "source_location": "europe-west1",
            "destination_location": "us-east1",
            "resources": [{"kind": "shared_query", "name": canonical_name, "display_name": "q1"}],
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
            catalog_path.write_text(json.dumps({"generated_at": "now", "resources": [resource.to_dict()]}), encoding="utf-8")
            content_dir.mkdir()
            (content_dir / f"{hashlib.sha256(canonical_name.encode()).hexdigest()}.sql").write_bytes(b"SELECT 1\n")
            config_path.write_text(json.dumps({"workspace_root": str(root / "tasks"), "catalog_path": str(catalog_path)}), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = main(
                    [
                        "migration", "batch", "inventory",
                        "--selection-file", str(selection_path),
                        "--dictionary", str(dictionary_path),
                        "--catalog", str(catalog_path),
                        "--content-dir", str(content_dir),
                        "--source-location", "europe-west1",
                        "--destination-location", "us-east1",
                        "--output", str(root / "manifest.json"),
                        "--config", str(config_path),
                        "--json",
                    ]
                )
            self.assertEqual(0, status)
            saved = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("europe-west1", saved["source_location"])
            self.assertEqual("us-east1", saved["destination_location"])
            self.assertFalse(json.loads(output.getvalue())["sql_executed"])


if __name__ == "__main__":
    unittest.main()
