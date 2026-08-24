import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from queryflow.config import QueryflowConfig
from queryflow.finops import (
    FinOpsError,
    build_aggregate_queries,
    generate_findings,
    load_assessment,
    load_business_context,
    resolve_projects,
    run_assessment,
)
from queryflow.workbench import build_aggregate_payload, parse_aggregate_summary


def _config(**overrides):
    values = {
        "workspace_root": Path("tasks"),
        "catalog_path": Path("catalog.json"),
        "audit_root": None,
        "finops_projects": ("allowed-project",),
    }
    values.update(overrides)
    return QueryflowConfig(**values)


class FinOpsModelTests(unittest.TestCase):
    def test_scope_only_allows_explicit_subset(self):
        config = _config(
            finops_projects=("allowed-project", "second-project"),
            project_aliases={"second": "second-project"},
        )
        self.assertEqual(resolve_projects(config, ["second-project"]), ("second-project",))
        self.assertEqual(resolve_projects(config, ["second"]), ("second-project",))
        with self.assertRaises(FinOpsError):
            resolve_projects(config, ["outside-project"])

    def test_business_context_precedence_and_no_raw_map_in_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "context.toml"
            path.write_text(
                """
schema_version = 1
[label_keys]
owner = "owner_label"
[projects.allowed-project]
owner = "project-owner"
business_unit = "finance"
[resources."projects/allowed-project/locations/us/instances/i"]
owner = "resource-owner"
""",
                encoding="utf-8",
            )
            context = load_business_context(path)
            resolved = context.resolve(
                "allowed-project",
                "projects/allowed-project/locations/us/instances/i",
                {"owner_label": "label-owner"},
            )
            self.assertEqual(resolved["owner"], "resource-owner")
            self.assertEqual(resolved["business_unit"], "finance")
            self.assertEqual(resolved["provenance"]["owner"], "resource_map")

    def test_business_context_rejects_secret_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "unsafe.toml"
            path.write_text("[projects.allowed-project]\nsecret = 'nope'\n", encoding="utf-8")
            with self.assertRaises(FinOpsError):
                load_business_context(path)

    def test_aggregate_queries_are_fixed_and_bounded(self):
        queries = build_aggregate_queries(
            ["allowed-project"],
            current_start=datetime(2026, 8, 1, tzinfo=timezone.utc).date(),
            current_end=datetime(2026, 8, 31, tzinfo=timezone.utc).date(),
            previous_start=datetime(2026, 7, 2, tzinfo=timezone.utc).date(),
            location="us",
            billing_table="billing-project.export.costs",
        )
        self.assertEqual(set(queries), {"jobs", "storage", "billing"})
        self.assertIn("INFORMATION_SCHEMA.JOBS_BY_PROJECT", queries["jobs"][0]["sql"])
        self.assertIn("GROUP BY project_id, period", queries["billing"][0]["sql"])
        self.assertNotIn("SELECT *", " ".join(item["sql"] for items in queries.values() for item in items).upper())

    def test_run_assessment_writes_verifiable_artifacts_and_detects_tamper(self):
        def runner(command):
            if "asset" in command:
                return [{
                    "name": "//compute.googleapis.com/projects/allowed-project/locations/us/instances/i",
                    "displayName": "instance",
                    "labels": {"owner": "platform"},
                }]
            if "recommender" in command:
                return []
            raise AssertionError(command)

        def aggregate_runner(queries, **_kwargs):
            return {
                "aggregates": {
                    "jobs": [{"project_id": "allowed-project", "job_count": 4, "total_bytes_billed": 20}],
                    "storage": [{"project_id": "allowed-project", "table_count": 2, "total_physical_bytes": 30}],
                    "billing": [
                        {"project_id": "allowed-project", "period": "current", "cost": 120},
                        {"project_id": "allowed-project", "period": "previous", "cost": 100},
                    ],
                },
                "statuses": {"jobs": "ok", "storage": "ok", "billing": "ok"},
            }

        with tempfile.TemporaryDirectory() as temporary:
            result = run_assessment(
                _config(
                    workbench_project="workbench-project",
                    workbench_location="us-central1-b",
                    workbench_instance="instance",
                    workbench_job_project="jobs-project",
                ),
                runner=runner,
                aggregate_runner=aggregate_runner,
                billing_table="billing-project.export.costs",
                output_root=Path(temporary),
                now=datetime(2026, 8, 31, tzinfo=timezone.utc),
            )
            loaded = load_assessment(result["directory"])
            self.assertEqual(loaded["manifest"]["action_mode"], "plan_only")
            evidence = json.loads((Path(result["directory"]) / "evidence.json").read_text(encoding="utf-8"))
            self.assertNotIn("labels", evidence["resources"][0])
            report = json.loads((Path(result["directory"]) / "report.json").read_text(encoding="utf-8"))
            self.assertIn("technical_appendix", report)
            self.assertEqual(result["manifest"]["source_status"]["billing_export"], "ok")
            (Path(result["directory"]) / "report.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaises(FinOpsError):
                load_assessment(result["directory"])

    def test_provider_findings_do_not_sum_provider_costs(self):
        findings = generate_findings({
            "resources": [],
            "recommendations": [
                {
                    "recommendation_id": "r1",
                    "project": "allowed-project",
                    "recommender": "google.compute.instance.IdleResourceRecommender",
                    "description": "Idle instance",
                    "subtype": "idle",
                    "resource_ids": [],
                    "impact": {"units": -10, "currencyCode": "USD"},
                },
                {
                    "recommendation_id": "r2",
                    "project": "allowed-project",
                    "recommender": "google.compute.instance.IdleResourceRecommender",
                    "description": "Another idle instance",
                    "subtype": "idle",
                    "resource_ids": [],
                    "impact": {"units": -12, "currencyCode": "USD"},
                },
            ],
            "aggregates": {},
            "context_coverage": {},
        })
        self.assertEqual(len(findings), 2)
        self.assertTrue(all(item["action_mode"] == "plan_only" for item in findings))


class WorkbenchAggregateTests(unittest.TestCase):
    def test_aggregate_payload_and_hash_validation(self):
        queries = {"jobs": [{"project": "allowed-project", "sql": "SELECT 1"}]}
        payload = build_aggregate_payload(queries, maximum_bytes_billed=100)
        record = {
            "kind": "jobs",
            "project": "allowed-project",
            "sha256": payload["queries"][0]["sha256"],
            "ok": True,
            "rows": [{"project_id": "allowed-project", "job_count": 1, "query": "must not persist"}],
        }
        parsed = parse_aggregate_summary({"records": [record]}, queries)
        self.assertEqual(parsed["aggregates"]["jobs"], [{"project_id": "allowed-project", "job_count": 1}])
        record["sha256"] = "wrong"
        tampered = parse_aggregate_summary({"records": [record]}, queries)
        self.assertEqual(tampered["statuses"]["jobs"], "integrity")


if __name__ == "__main__":
    unittest.main()
