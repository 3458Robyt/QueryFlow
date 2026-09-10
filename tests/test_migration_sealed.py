import json
import unittest

from queryflow.catalog import ResourceRef
from queryflow.migration import RouteDictionary
from queryflow.migration_batch import (
    BatchError,
    BatchSelection,
    build_batch_digest,
    build_batch_manifest,
    build_sealed_digest,
    mask_sensitive_content,
)


class MigrationSealedTests(unittest.TestCase):
    def setUp(self):
        self.dictionary = RouteDictionary.from_mapping(
            {
                "schema_version": 1,
                "dictionary_id": "sealed-routes",
                "scope": {"source_project": "source-project"},
                "mappings": [
                    {
                        "id": "raw",
                        "zone": "raw",
                        "old": "source-project.raw",
                        "new": "dest-project.raw",
                    }
                ],
            }
        )

    def _selection(self, handling="sealed_copy"):
        return BatchSelection.from_mapping(
            {
                "schema_version": 1,
                "campaign_id": "sealed-campaign",
                "source_project": "source-project",
                "destination_project": "dest-project",
                "source_location": "europe-west1",
                "destination_location": "us-east1",
                "secret_handling": handling,
                "resources": [{"kind": "notebook", "display_name": "secret notebook"}],
            }
        )

    def test_selection_defaults_to_block_and_accepts_sealed_copy(self):
        selection = self._selection()
        self.assertEqual("sealed_copy", selection.secret_handling)
        self.assertEqual("sealed_copy", selection.to_dict()["secret_handling"])
        self.assertEqual("block", self._selection("block").secret_handling)
        with self.assertRaises(BatchError):
            self._selection("publish_anyway")

    def test_secret_manifest_is_pending_and_has_separate_safe_digest(self):
        selection = self._selection()
        resource = ResourceRef(
            "notebook",
            "projects/source-project/locations/europe-west1/repositories/secret-notebook",
            "source-project",
            "europe-west1",
            "secret notebook",
            "head",
        )
        notebook = json.dumps(
            {
                "cells": [
                    {
                        "cell_type": "code",
                        "metadata": {},
                        "source": ["password = 'super-secret-value'\n", "print('ok')\n"],
                    }
                ],
                "metadata": {"kernelspec": {"language": "python"}},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ).encode()
        manifest = build_batch_manifest(
            selection,
            [resource],
            {resource.name: {"content": notebook, "filename": "content.ipynb", "head_commit": "head"}},
            self.dictionary,
        )
        record = manifest["resources"][0]
        self.assertEqual("security_pending", record["status"])
        self.assertFalse(record["migration_eligible"])
        self.assertIn("embedded_secret", [item["kind"] for item in record["blockers"]])
        sealed_digest = manifest.get("sealed_publication_digest")
        self.assertEqual(sealed_digest, build_sealed_digest(manifest))
        self.assertEqual(64, len(sealed_digest))
        self.assertNotIn("super-secret-value", json.dumps(manifest))
        self.assertNotEqual(build_batch_digest(manifest), sealed_digest)
        prepared = json.loads(json.dumps(manifest))
        prepared["resources"][0]["status"] = "prepared"
        self.assertEqual(sealed_digest, build_sealed_digest(prepared))

    def test_normal_secret_handling_remains_blocked(self):
        selection = self._selection("block")
        resource = ResourceRef(
            "notebook",
            "projects/source-project/locations/europe-west1/repositories/secret-notebook",
            "source-project",
            "europe-west1",
            "secret notebook",
            "head",
        )
        notebook = json.dumps(
            {
                "cells": [{"cell_type": "code", "metadata": {}, "source": ["api_key = 'super-secret-value'\n"]}],
                "metadata": {"kernelspec": {"language": "python"}},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ).encode()
        manifest = build_batch_manifest(
            selection,
            [resource],
            {resource.name: {"content": notebook, "filename": "content.ipynb", "head_commit": "head"}},
            self.dictionary,
        )
        self.assertEqual("blocked", manifest["resources"][0]["status"])
        self.assertEqual("", manifest.get("sealed_publication_digest"))

    def test_masking_removes_secret_from_sql_and_notebook_without_exposing_it(self):
        sql = b"SELECT 1;\npassword = 'super-secret-value';\n"
        masked_sql = mask_sensitive_content("shared_query", sql)
        self.assertNotIn(b"super-secret-value", masked_sql)
        self.assertIn(b"[REDACTED]", masked_sql)

        notebook = json.dumps(
            {
                "cells": [{"cell_type": "code", "metadata": {}, "source": ["token = 'ghp_abcdefghijklmnopqrstuvwxyz'\n"]}],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ).encode()
        masked_notebook = mask_sensitive_content("notebook", notebook)
        self.assertNotIn(b"ghp_abcdefghijklmnopqrstuvwxyz", masked_notebook)
        json.loads(masked_notebook.decode("utf-8"))

    def test_sealed_digest_changes_when_security_reference_material_changes(self):
        manifest = {
            "schema_version": 1,
            "campaign_id": "sealed",
            "source_project": "source",
            "destination_project": "dest",
            "source_location": "europe-west1",
            "destination_location": "us-east1",
            "dictionary_sha256": "d" * 64,
            "selection": {"secret_handling": "sealed_copy"},
            "resources": [
                {
                    "resource": {"kind": "notebook", "name": "source"},
                    "source": {"filename": "content.ipynb", "content_sha256": "a" * 64},
                    "destination": {"repository_id": "dest", "display_name": "Notebook"},
                    "rewrite": {"before_sha256": "a" * 64, "proposed_sha256": "b" * 64},
                    "security_blockers": [{"kind": "embedded_secret", "path": "cells/b0000.py", "line": 1}],
                    "status": "security_pending",
                }
            ],
        }
        first = build_sealed_digest(manifest)
        changed = json.loads(json.dumps(manifest))
        changed["resources"][0]["source"]["content_sha256"] = "c" * 64
        self.assertNotEqual(first, build_sealed_digest(changed))


if __name__ == "__main__":
    unittest.main()
