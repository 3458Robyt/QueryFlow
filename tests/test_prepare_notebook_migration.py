import json
import tempfile
import unittest
from pathlib import Path

from queryflow.catalog import Catalog, ResourceRef
from scripts.prepare_notebook_migration import build_notebook_campaign


class PrepareNotebookMigrationTests(unittest.TestCase):
    def _catalog(self):
        return Catalog(
            generated_at="2026-09-07T00:00:00Z",
            resources=[
                ResourceRef("notebook", "n1", "source", "us-east1", "Alpha", "h1", {"commit_time": "2026-08-01T00:00:00Z"}),
                ResourceRef("notebook", "n2", "source", "us-central1", "Beta", "h2"),
                ResourceRef("notebook", "n3", "source", "us-west1", "", "h3"),
                ResourceRef("notebook", "n4", "source", "us-east1", "Alpha", "h4", {"commit_time": "2026-09-01T00:00:00Z"}),
                ResourceRef("notebook", "n5", "source", "us-east1", "Alpha", "h5", {"commit_time": "2026-09-02T00:00:00Z"}),
                ResourceRef("notebook", "n6", "source", "us-east1", "Sealed", "h6"),
                ResourceRef("notebook", "n7", "source", "us-east1", "Other", "h7"),
            ],
        )

    def test_builds_region_aware_canonical_selections_and_exclusions(self):
        with tempfile.TemporaryDirectory() as temp:
            result = build_notebook_campaign(
                self._catalog(),
                output_root=Path(temp),
                source_project="source",
                destination_project="dest",
                source_locations=("us-east1", "us-central1", "us-west1"),
                destination_location="us-east1",
                sealed_display_names=("Sealed",),
                normal_batch_size=25,
                sealed_batch_size=5,
            )
            self.assertEqual(4, result["named_count"])
            self.assertEqual(1, result["excluded_unnamed_count"])
            self.assertEqual(2, result["superseded_duplicate_count"])
            self.assertEqual(2, result["normal_lot_count"])
            self.assertEqual(1, result["sealed_lot_count"])
            normal = json.loads(Path(result["normal_lots"][0]["selection"]).read_text())
            sealed = json.loads(Path(result["sealed_lots"][0]["selection"]).read_text())
            self.assertEqual("block", normal["secret_handling"])
            self.assertEqual("sealed_copy", sealed["secret_handling"])
            self.assertEqual("n5", normal["resources"][0]["name"])
            self.assertEqual("n6", sealed["resources"][0]["name"])
            self.assertEqual("us-east1", normal["destination_location"])
            summary = json.loads(Path(temp, "notebook-campaign.json").read_text())
            self.assertEqual(3, len(summary["excluded"]))

    def test_duplicate_without_safe_commit_time_is_excluded_pending(self):
        catalog = Catalog(
            generated_at="now",
            resources=[
                ResourceRef("notebook", "a", "source", "us-east1", "Same", "a"),
                ResourceRef("notebook", "b", "source", "us-east1", "Same", "b"),
            ],
        )
        with tempfile.TemporaryDirectory() as temp:
            result = build_notebook_campaign(
                catalog,
                output_root=Path(temp),
                source_project="source",
                destination_project="dest",
                source_locations=("us-east1",),
                destination_location="us-east1",
            )
            self.assertEqual(0, result["named_count"])
            self.assertEqual("pending_duplicate", result["excluded"][0]["status"])

    def test_only_name_creates_a_follow_up_selection(self):
        with tempfile.TemporaryDirectory() as temp:
            result = build_notebook_campaign(
                self._catalog(),
                output_root=Path(temp),
                source_project="source",
                destination_project="dest",
                source_locations=("us-east1", "us-central1", "us-west1"),
                destination_location="us-east1",
                sealed_display_names=("Sealed",),
                only_display_names=("Sealed",),
            )
            self.assertEqual(1, result["named_count"])
            self.assertEqual(1, result["sealed_lot_count"])
            self.assertEqual(0, result["normal_lot_count"])


if __name__ == "__main__":
    unittest.main()
