import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.consolidate_migration_guide import (
    build_consolidated_report,
    build_routine_diff_index,
    load_html_payload,
    normalize_routine_resource,
)
from queryflow.migration_report import render_detailed_html


class ConsolidatedMigrationGuideTests(unittest.TestCase):
    def _base_report(self):
        return {
            "schema_version": 1,
            "source_schema_version": 1,
            "campaign_id": "batch-20260904",
            "generated_at": "2026-09-07T17:25:00Z",
            "source_project": "source-project",
            "destination_project": "analytics-487218",
            "location": "us-east1",
            "resource_count": 1,
            "published_count": 1,
            "pending_count": 0,
            "status_counts": {"published": 1},
            "kind_counts": {"shared_query": 1},
            "classification_counts": {"read_only": 1},
            "warning_counts": {},
            "review_reason_counts": {},
            "review_required_count": 1,
            "execution_eligible_count": 0,
            "changed_file_count": 1,
            "route_replacement_group_count": 1,
            "route_replacement_occurrence_count": 2,
            "unique_route_replacement_group_count": 1,
            "unmatched_route_count": 0,
            "unique_unmatched_route_count": 0,
            "lots": [{"lot": "r01", "campaign_id": "batch-20260904-r01", "resource_count": 1, "published_count": 1, "pending_count": 0, "warning_count": 0, "status": "published", "publication_digest": "digest"}],
            "route_replacements": [{"mapping_id": "raw-001", "old": "source.raw", "new": "target.raw", "occurrences": 2, "resources": ["r01/01 q"]}],
            "unmatched_routes": [],
            "resources": [{
                "lot": "r01", "ordinal": 1, "kind": "shared_query", "display_name": "q",
                "source_name": "projects/source/locations/us-east1/repositories/q",
                "source_project": "source-project", "source_location": "us-east1", "source_filename": "content.sql",
                "source_head_commit": "head", "source_content_sha256": "source-hash",
                "destination_project": "analytics-487218", "destination_location": "us-east1",
                "destination_repository_id": "q", "destination_display_name": "q", "destination_repository": "repo",
                "destination_collision": False, "status": "published", "migration_eligible": True,
                "execution_eligible": False, "classification": {"statement_class": "read_only", "read_only": True, "references": [], "errors": [], "warnings": [], "dynamic_cells": []},
                "review": {"required": True, "reasons": [], "policy": "human_review"}, "changed_files": ["content.sql"],
                "route_replacements": [{"mapping_id": "raw-001", "old": "source.raw", "new": "target.raw", "occurrences": 2}],
                "route_replacement_group_count": 1, "route_replacement_occurrence_count": 2, "unmatched_routes": [], "unmatched_route_count": 0,
                "dynamic_cells": [], "warnings": [], "blockers": [], "runtime_error": "", "before_sha256": "before", "proposed_sha256": "after",
                "sql_executed": False, "published_commit_sha": "commit", "published_filename": "content.sql", "audit": {},
            }],
        }

    def _routine(self, *, status="published_verified", before="SELECT source.raw;\n", after="SELECT target.raw;\n"):
        return {
            "lot": 1,
            "ordinal": 2,
            "role": "procedure",
            "status": status,
            "migration_eligible": status == "published_verified",
            "execution_eligible": False,
            "source": {
                "name": "projects/source/datasets/source_functions/routines/p_demo",
                "project": "source-project", "dataset": "source_functions", "location": "us-east1",
                "routine_id": "p_demo", "routine_type": "PROCEDURE", "language": "SQL", "source_sha256": hashlib.sha256(before.encode()).hexdigest(),
            },
            "destination": {
                "name": "projects/analytics-487218/datasets/functions/routines/p_demo",
                "project": "analytics-487218", "dataset": "functions", "location": "us-east1", "routine_id": "p_demo", "collision": False,
            },
            "classification": {"dynamic_sql": False, "language": "SQL", "mutating_sql": True, "routine_type": "PROCEDURE"},
            "security": {"handling": "none", "sealed": False, "findings": []},
            "rewrite": {
                "changed": before != after,
                "before_sha256": hashlib.sha256(before.encode()).hexdigest(),
                "proposed_sha256": hashlib.sha256(after.encode()).hexdigest(),
                "applied_routes": [{"mapping_id": "raw-001", "old": "source.raw", "new": "target.raw", "occurrences": 1}],
                "unknown_routes": [],
            },
            "proposal": {"definitionBody": after, "language": "SQL", "routineType": "PROCEDURE"},
            "review": {"before_definition_body": before, "required": True, "reasons": ["mutating_sql"], "policy": "human_review_before_publication"},
            "warnings": [{"kind": "mutating_sql", "path": "definitionBody", "line": 1}],
            "blockers": [],
            "receipt": {"status": status, "destination": "projects/analytics-487218/datasets/functions/routines/p_demo"},
        }

    def test_normalize_routine_preserves_status_routes_and_coordinates(self):
        item = normalize_routine_resource(self._routine(), campaign_id="routines-full-20260908")
        self.assertEqual("procedure", item["kind"])
        self.assertEqual("Publicado y verificado", item["status_label"])
        self.assertEqual("p_demo", item["display_name"])
        self.assertEqual(1, item["route_replacement_occurrence_count"])
        self.assertEqual("definitionBody", item["changed_files"][0])
        self.assertEqual(hashlib.sha256(b"SELECT target.raw;\n").hexdigest(), item["proposed_sha256"])

    def test_routine_diff_index_contains_red_green_hunks(self):
        item = normalize_routine_resource(self._routine(), campaign_id="routines-full-20260908")
        index = build_routine_diff_index([item])
        diff = index["resources"][item["resource_id"]]
        self.assertEqual("match", diff["hash_status"])
        rows = [row for hunk in diff["files"][0]["hunks"] for row in hunk["rows"]]
        self.assertTrue(any(row["kind"] == "removed" for row in rows))
        self.assertTrue(any(row["kind"] == "added" for row in rows))

    def test_consolidated_report_counts_routines_as_migrated_or_pending(self):
        base = self._base_report()
        failed = self._routine(status="failed", before="SELECT 1;\n", after="SELECT 2;\n")
        failed["ordinal"] = 3
        failed["source"] = dict(failed["source"], name="projects/source/datasets/source_functions/routines/p_demo_2", routine_id="p_demo_2")
        failed["destination"] = dict(failed["destination"], name="projects/analytics-487218/datasets/functions/routines/p_demo_2", routine_id="p_demo_2")
        routine_report = {"campaign_id": "routines-full-20260908", "source_project": "source-project", "destination_project": "analytics-487218", "destination_location": "us-east1", "publication_digest": "routine-digest", "resources": [self._routine(), failed]}
        combined = build_consolidated_report(base, routine_report)
        self.assertEqual(3, combined["resource_count"])
        self.assertEqual(2, combined["completed_count"])
        self.assertEqual(1, combined["pending_count"])
        self.assertEqual({"published": 1, "published_verified": 1, "failed": 1}, combined["status_counts"])
        self.assertEqual(3, combined["kind_counts"]["shared_query"] + combined["kind_counts"]["procedure"])

    def test_load_html_payload_rejects_missing_or_malformed_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guide.html"
            path.write_text("<html></html>", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_html_payload(path)


if __name__ == "__main__":
    unittest.main()
