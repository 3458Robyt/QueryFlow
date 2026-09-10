import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from queryflow.catalog import ResourceRef
from queryflow.migration import RouteDictionary
from queryflow.migration_batch import (
    BatchError,
    BatchSelection,
    build_batch_digest,
    build_batch_manifest,
    build_update_manifest,
    save_batch_manifest,
)
from queryflow.migration_report import (
    _portable_report,
    build_detailed_report,
    build_embedded_diff_index,
    render_detailed_html,
    render_detailed_markdown,
)
from queryflow.workspace import create_workspace


class MigrationDetailReportTests(unittest.TestCase):
    @staticmethod
    def _resource_record(task: Path, *, kind: str, display_name: str, filename: str, before: bytes, after: bytes) -> dict:
        return {
            "lot": "r01",
            "ordinal": 1,
            "kind": kind,
            "display_name": display_name,
            "source_name": display_name,
            "source_filename": filename,
            "task": str(task),
            "before_sha256": hashlib.sha256(before).hexdigest(),
            "proposed_sha256": hashlib.sha256(after).hexdigest(),
        }

    def test_embedded_diff_index_contains_changed_hunks_and_context_only(self):
        before = b"linea 0\nlinea 1\nantes\nlinea 3\nlinea 4\n"
        after = b"linea 0\nlinea 1\ndespues\nlinea 3\nlinea 4\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "campaign"
            task = create_workspace(
                root=root,
                task_id="task-sql",
                resource=ResourceRef("shared_query", "q", "source", "us-east1", "q", "head"),
                content=before,
                filename="content.sql",
            )
            (task / "content.sql").write_bytes(after)
            report = {"resources": [self._resource_record(task, kind="shared_query", display_name="q", filename="content.sql", before=before, after=after)]}

            index = build_embedded_diff_index(report, root, context_lines=1)
            resource = next(iter(index["resources"].values()))
            diff_file = resource["files"][0]
            rows = [row for hunk in diff_file["hunks"] for row in hunk["rows"]]

            self.assertEqual(1, diff_file["added"])
            self.assertEqual(1, diff_file["removed"])
            self.assertEqual({"linea 1", "antes", "despues", "linea 3"}, {row["text"] for row in rows})
            self.assertNotIn("linea 0", {row["text"] for row in rows})
            self.assertNotIn("linea 4", {row["text"] for row in rows})

    def test_embedded_diff_index_represents_notebook_changes_by_cell(self):
        def notebook(source: str) -> bytes:
            return json.dumps(
                {
                    "cells": [{"cell_type": "code", "metadata": {"language": "python"}, "source": [source]}],
                    "metadata": {"kernelspec": {"language": "python"}},
                    "nbformat": 4,
                    "nbformat_minor": 5,
                },
                ensure_ascii=False,
            ).encode("utf-8")

        before = notebook("print('antes')\n")
        after = notebook("print('despues')\n")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "campaign"
            task = create_workspace(
                root=root,
                task_id="task-notebook",
                resource=ResourceRef("notebook", "n", "source", "us-east1", "n", "head"),
                content=before,
                filename="content.ipynb",
            )
            (task / "content.ipynb").write_bytes(after)
            report = {"resources": [self._resource_record(task, kind="notebook", display_name="n", filename="content.ipynb", before=before, after=after)]}

            index = build_embedded_diff_index(report, root)
            diff_file = next(iter(index["resources"].values()))["files"][0]

            self.assertEqual("cells/0000", diff_file["path"])
            self.assertIn("Celda 0", diff_file["label"])
            self.assertEqual("python", diff_file["language"])

    def test_embedded_html_escapes_code_and_exposes_analyst_controls(self):
        before = b"SELECT '</script>' AS antes;\n"
        after = b"SELECT '<b>despues</b>' AS despues;\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "campaign"
            task = create_workspace(
                root=root,
                task_id="task-html",
                resource=ResourceRef("shared_query", "q", "source", "us-east1", "q", "head"),
                content=before,
                filename="content.sql",
            )
            (task / "content.sql").write_bytes(after)
            item = self._resource_record(task, kind="shared_query", display_name="q", filename="content.sql", before=before, after=after)
            report = {
                "campaign_id": "campaign",
                "source_project": "source",
                "destination_project": "destination",
                "location": "us-east1",
                "generated_at": "2026-09-07T00:00:00Z",
                "resource_count": 1,
                "published_count": 1,
                "pending_count": 0,
                "route_replacement_occurrence_count": 1,
                "unmatched_route_count": 0,
                "resources": [item],
                "lots": [],
            }

            html = render_detailed_html(report, build_embedded_diff_index(report, root))

            self.assertIn("Buscar recursos", html)
            self.assertIn("Rutas no cubiertas", html)
            self.assertIn("application/json", html)
            self.assertIn("\\u003c/script\\u003e", html)
            self.assertNotIn("SELECT '</script>'", html)

    def test_embedded_diff_index_rejects_hash_mismatch_with_resource_name(self):
        before = b"SELECT 1;\n"
        after = b"SELECT 2;\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "campaign"
            task = create_workspace(
                root=root,
                task_id="task-mismatch",
                resource=ResourceRef("shared_query", "q", "source", "us-east1", "q", "head"),
                content=before,
                filename="content.sql",
            )
            (task / "content.sql").write_bytes(after)
            item = self._resource_record(task, kind="shared_query", display_name="q", filename="content.sql", before=before, after=after)
            item["proposed_sha256"] = "incorrect"

            with self.assertRaisesRegex(Exception, "q"):
                build_embedded_diff_index({"resources": [item]}, root)

    def test_embedded_diff_index_reports_malformed_notebook_by_resource(self):
        before = b"not a notebook"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "campaign"
            task = create_workspace(
                root=root,
                task_id="task-malformed-notebook",
                resource=ResourceRef("notebook", "n", "source", "us-east1", "n", "head"),
                content=before,
                filename="content.ipynb",
            )
            item = self._resource_record(task, kind="notebook", display_name="n", filename="content.ipynb", before=before, after=before)

            with self.assertRaisesRegex(BatchError, "n"):
                build_embedded_diff_index({"resources": [item]}, root)

    def test_embedded_diff_index_keeps_sealed_copy_redacted(self):
        before = b"secret-looking content"
        after = b"rewritten secret-looking content"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "campaign"
            task = create_workspace(
                root=root,
                task_id="task-sealed",
                resource=ResourceRef("notebook", "n", "source", "us-east1", "n", "head"),
                content=before,
                filename="content.ipynb",
            )
            (task / "content.ipynb").write_bytes(after)
            item = self._resource_record(task, kind="notebook", display_name="n", filename="content.ipynb", before=before, after=after)
            item["sealed_copy"] = True

            index = build_embedded_diff_index({"resources": [item]}, root)
            resource = next(iter(index["resources"].values()))
            self.assertEqual("sealed_redacted", resource["hash_status"])
            self.assertTrue(resource["redacted"])
            self.assertEqual([], resource["files"])

    def test_embedded_diff_index_keeps_unprepared_resource_in_index(self):
        item = {
            "kind": "notebook",
            "display_name": "conflicto",
            "source_name": "conflicto",
            "status": "destination_conflict",
        }
        with tempfile.TemporaryDirectory() as directory:
            index = build_embedded_diff_index({"resources": [item]}, Path(directory))
            resource = next(iter(index["resources"].values()))
            self.assertEqual("not_prepared", resource["hash_status"])
            self.assertEqual([], resource["files"])

    def test_embedded_diff_index_requires_baseline_and_proposed_hashes(self):
        before = b"SELECT 1;\n"
        after = b"SELECT 2;\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "campaign"
            task = create_workspace(
                root=root,
                task_id="task-missing-hash",
                resource=ResourceRef("shared_query", "q", "source", "us-east1", "q", "head"),
                content=before,
                filename="content.sql",
            )
            (task / "content.sql").write_bytes(after)
            item = self._resource_record(task, kind="shared_query", display_name="q", filename="content.sql", before=before, after=after)

            for key in ("before_sha256", "proposed_sha256"):
                missing = dict(item)
                missing.pop(key)
                with self.subTest(key=key), self.assertRaisesRegex(BatchError, "hash"):
                    build_embedded_diff_index({"resources": [missing]}, root)

    def test_embedded_diff_index_marks_newline_only_changes(self):
        before = b"SELECT 1;\n"
        after = b"SELECT 1;"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "campaign"
            task = create_workspace(
                root=root,
                task_id="task-newline",
                resource=ResourceRef("shared_query", "q", "source", "us-east1", "q", "head"),
                content=before,
                filename="content.sql",
            )
            (task / "content.sql").write_bytes(after)
            item = self._resource_record(task, kind="shared_query", display_name="q", filename="content.sql", before=before, after=after)

            diff_file = next(iter(build_embedded_diff_index({"resources": [item]}, root)["resources"].values()))["files"][0]

            self.assertTrue(diff_file["changed"])
            self.assertTrue(diff_file["newline_only"])
            self.assertEqual([], diff_file["hunks"])

    def test_report_keeps_each_resource_and_route_incident_details(self):
        dictionary = RouteDictionary.from_mapping(
            {
                "schema_version": 1,
                "dictionary_id": "detail-routes",
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
        selection = BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "detail-campaign-r01",
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
            {
                "q": {
                    "content": (
                        b"SELECT * FROM source-project.raw_dataset.table_a;\n"
                        b"SELECT * FROM source-project.unmapped.table_b;\n"
                    ),
                    "filename": "content.sql",
                    "head_commit": "head",
                }
            },
            dictionary,
        )
        manifest["status"] = "published_with_warnings"
        record = manifest["resources"][0]
        record["status"] = "published"
        record["published"] = {
            "repository": "projects/dest-project/locations/us-east1/repositories/q",
            "commit_sha": "commit-1",
            "filename": "content.sql",
        }
        manifest["publication_digest"] = build_batch_digest(manifest)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "campaign"
            lot = root / "r01"
            save_batch_manifest(manifest, lot / "manifest.json")
            report = build_detailed_report(root, expected_count=1)
            self.assertEqual(1, report["resource_count"])
            self.assertEqual(1, report["published_count"])
            self.assertEqual(1, report["route_replacement_group_count"])
            self.assertEqual(1, report["route_replacement_occurrence_count"])
            self.assertEqual(1, report["unmatched_route_count"])
            detail = report["resources"][0]
            self.assertEqual("q", detail["display_name"])
            self.assertEqual("published", detail["status"])
            self.assertEqual("source-project.unmapped.table_b", detail["unmatched_routes"][0]["route"])
            self.assertEqual("raw-487218.raw_dataset", detail["route_replacements"][0]["new"])
            markdown = render_detailed_markdown(report)
            self.assertIn("Incidentes de ruta no encontrada: **1**", markdown)
            self.assertIn("Cada fila representa un incidente único por recurso", markdown)
            self.assertIn("source-project.unmapped.table_b", markdown)
            self.assertIn("content.sql", markdown)

    def test_report_accepts_multiregion_update_lots(self):
        dictionary = RouteDictionary.from_mapping(
            {
                "schema_version": 1,
                "dictionary_id": "detail-update-routes",
                "scope": {"source_project": "analytics-487218"},
                "mappings": [
                    {
                        "id": "raw-001",
                        "zone": "raw",
                        "old": "analytics-487218.raw_dataset",
                        "new": "analytics-487218.staging",
                    }
                ],
            }
        )
        resources = [
            ResourceRef(
                "notebook",
                "projects/analytics-487218/locations/us-east1/repositories/a",
                "analytics-487218",
                "us-east1",
                "Notebook A",
                "head-a",
            ),
            ResourceRef(
                "notebook",
                "projects/analytics-487218/locations/us-west1/repositories/b",
                "analytics-487218",
                "us-west1",
                "Notebook B",
                "head-b",
            ),
        ]
        contents = {
            resource.name: {
                "content": json.dumps(
                    {
                        "cells": [
                            {
                                "cell_type": "code",
                                "metadata": {},
                                "source": ["SELECT * FROM analytics-487218.raw_dataset.table_a;\n"],
                            }
                        ],
                        "metadata": {},
                        "nbformat": 4,
                        "nbformat_minor": 5,
                    }
                ).encode(),
                "filename": "content.ipynb",
                "head_commit": resource.fingerprint,
            }
            for resource in resources
        }
        manifests = []
        for lot, resource in zip(("lot-01", "lot-02"), resources):
            selection = BatchSelection.from_mapping(
                {
                    "schema_version": 1,
                    "campaign_id": f"analytics-notebook-route-update-20260910-{lot}",
                    "operation": "update",
                    "source_project": "analytics-487218",
                    "destination_project": "analytics-487218",
                    "source_location": resource.location,
                    "destination_location": resource.location,
                    "resources": [
                        {
                            "kind": "notebook",
                            "name": resource.name,
                            "display_name": resource.display_name,
                        }
                    ],
                }
            )
            manifest = build_update_manifest(selection, [resource], contents, dictionary)
            manifests.append(manifest)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "campaign"
            for index, manifest in enumerate(manifests, 1):
                save_batch_manifest(manifest, root / f"lot-{index:02d}" / "manifest.json")
            report = build_detailed_report(
                root,
                expected_count=2,
                campaign_prefix="analytics-notebook-route-update-20260910",
            )
            self.assertEqual("update", report["operation"])
            self.assertEqual(["us-east1", "us-west1"], report["locations"])
            self.assertEqual(2, report["resource_count"])

    def test_report_rejects_lots_from_different_campaigns(self):
        dictionary = RouteDictionary.from_mapping(
            {
                "schema_version": 1,
                "dictionary_id": "mixed-routes",
                "scope": {"source_project": "source-project"},
                "mappings": [
                    {
                        "id": "raw-001",
                        "zone": "raw",
                        "old": "source-project.raw",
                        "new": "raw-487218.raw",
                    }
                ],
            }
        )
        selection = BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "campaign-r01",
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
            {"q": {"content": b"SELECT 1;\n", "filename": "content.sql", "head_commit": "head"}},
            dictionary,
        )
        other = json.loads(json.dumps(manifest))
        other["campaign_id"] = "other-campaign-r02"
        other["resources"][0]["resource"]["name"] = "q2"
        other["resources"][0]["resource"]["display_name"] = "q2"
        other["selection"]["resources"][0]["display_name"] = "q2"
        manifest["publication_digest"] = build_batch_digest(manifest)
        other["publication_digest"] = build_batch_digest(other)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "campaign"
            save_batch_manifest(manifest, root / "r01" / "manifest.json")
            save_batch_manifest(other, root / "r02" / "manifest.json")

            with self.assertRaisesRegex(BatchError, "campañas"):
                build_detailed_report(root)

    def test_portable_report_omits_machine_local_paths(self):
        report = {
            "campaign_root": "/home/analyst/.queryflow/campaign",
            "lots": [{
                "lot": "r01",
                "manifest": "/home/analyst/.queryflow/campaign/r01/manifest.json",
                "report_markdown": "/home/analyst/.queryflow/campaign/r01/migration-report.md",
                "review": "/home/analyst/.queryflow/campaign/r01/review.html",
            }],
            "resources": [{
                "lot": "r01",
                "display_name": "q",
                "manifest": "/home/analyst/.queryflow/campaign/r01/manifest.json",
                "task": "/home/analyst/.queryflow/campaign/r01/tasks/q",
                "review_file": "/home/analyst/.queryflow/campaign/r01/review.html",
                "review_relative": "review.html",
                "changed_files": ["content.sql"],
                "audit": {"task_id": "q", "files": ["/home/analyst/.queryflow/campaign/r01/tasks/q/manifest.json"]},
            }],
        }

        portable = _portable_report(report)
        serialized = json.dumps(portable, ensure_ascii=False)

        self.assertNotIn("/home/analyst", serialized)
        self.assertNotIn("campaign_root", portable)
        self.assertEqual("r01/manifest.json", portable["lots"][0]["manifest_relative"])
        self.assertEqual("r01/q", portable["resources"][0]["task_relative"])


if __name__ == "__main__":
    unittest.main()
