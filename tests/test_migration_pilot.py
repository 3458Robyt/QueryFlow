import json
import io
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from queryflow.catalog import ResourceRef
from queryflow.migration import (
    RouteDictionary,
    find_unknown_routes,
    load_dictionary,
    rewrite_task,
    rewrite_text,
    render_markdown,
)
from queryflow.migration_pilot import (
    CampaignError,
    cleanup_digest,
    campaign_publish_digest,
    classify_text,
    InventoryCheckpoint,
    select_stratified,
    build_manifest,
    load_inventory_checkpoint,
    make_inventory_incident_report,
    save_manifest,
    save_inventory_checkpoint,
    PilotManifest,
    PilotQuotas,
    PilotSelection,
    select_classified,
    select_campaign,
    validate_pilot_manifest,
)
from queryflow.config import load_config
from queryflow.dataform import DataformClient, DataformRateLimitError, ExportedAsset
from queryflow.cli import main
from queryflow.cli import _limit_pilot_resource_pool, _write_campaign_inventory_shortfall_report
from queryflow.notebooks import analyze_sql_fragments, write_cell_workspace
from queryflow.workspace import create_workspace


class MigrationDictionaryTests(unittest.TestCase):
    def setUp(self):
        self.dictionary = RouteDictionary.from_mapping(
            {
                "schema_version": 1,
                "dictionary_id": "test-routes",
                "scope": {
                    "source_project": "source-project",
                    "destination_project": "destination-project",
                },
                "mappings": [
                    {
                        "id": "raw-001",
                        "zone": "raw",
                        "old": "source-project.raw_dataset",
                        "new": "raw-123.raw_dataset",
                        "active": True,
                    },
                    {
                        "id": "staging-001",
                        "zone": "staging",
                        "old": "source-project.analytics_internal",
                        "new": "destination-project.staging",
                        "active": True,
                    },
                ],
            }
        )

    def test_rewrites_known_prefix_and_reports_unknown_route(self):
        source = (
            "SELECT * FROM `source-project.raw_dataset.table_a`;\n"
            "SELECT * FROM source-project.unknown_dataset.table_b;\n"
        )
        rewritten, applied, unknown = rewrite_text(source, self.dictionary)

        self.assertIn("raw-123.raw_dataset.table_a", rewritten)
        self.assertIn("source-project.unknown_dataset.table_b", rewritten)
        self.assertEqual(["raw-001"], [item.mapping_id for item in applied])
        self.assertEqual(1, len(unknown))
        self.assertEqual("source-project.unknown_dataset.table_b", unknown[0].route)

    def test_classification_distinguishes_known_incident_and_no_route(self):
        self.assertEqual("known", classify_text("select * from source-project.raw_dataset.t" , self.dictionary))
        self.assertEqual("incident", classify_text("select * from source-project.other.t", self.dictionary))
        self.assertEqual("no_source_routes", classify_text("select 1", self.dictionary))

    def test_dictionary_markdown_is_safe_and_explicit(self):
        markdown = render_markdown(self.dictionary)
        self.assertIn("test-routes", markdown)
        self.assertIn("raw-001", markdown)
        self.assertIn("staging", markdown)

    def test_dictionary_loader_rejects_duplicate_origins(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "routes.json"
            payload = {
                "schema_version": 1,
                "dictionary_id": "duplicate",
                "mappings": [
                    {"id": "a", "zone": "raw", "old": "p.d", "new": "q.d", "active": True},
                    {"id": "b", "zone": "raw", "old": "p.d", "new": "q.other", "active": True},
                ],
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_dictionary(path)

    def test_unknown_route_scanner_does_not_duplicate_matches(self):
        value = "source-project.unknown.table source-project.unknown.table"
        matches = find_unknown_routes(value, self.dictionary)
        self.assertEqual(1, len(matches))

    def test_notebook_rewrite_changes_code_cells_but_preserves_markdown(self):
        notebook = json.dumps(
            {
                "cells": [
                    {"cell_type": "markdown", "metadata": {}, "source": ["source-project.raw_dataset is documentation"]},
                    {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": ["SELECT * FROM source-project.raw_dataset.t"]},
                ],
                "metadata": {"kernelspec": {"language": "python"}},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as temp:
            task = create_workspace(
                root=Path(temp) / "tasks",
                task_id="notebook-routes",
                resource=ResourceRef("notebook", "repository", "source-project", "us", "demo", "head"),
                content=notebook,
                filename="content.ipynb",
                mode="copy",
            )
            write_cell_workspace(notebook, task / "cells")
            preview = rewrite_task(task, self.dictionary, apply=False)
            self.assertEqual(1, len(preview.changed_files))
            self.assertIn("source-project.raw_dataset", (task / "cells" / "b0000.md").read_text(encoding="utf-8"))
            self.assertIn("source-project.raw_dataset", (task / "cells" / "b0001.py").read_text(encoding="utf-8"))

            applied = rewrite_task(task, self.dictionary, apply=True, expected_plan_digest=preview.plan_digest)
            self.assertEqual(preview.plan_digest, applied.plan_digest)
            self.assertIn("raw-123.raw_dataset", (task / "cells" / "b0001.py").read_text(encoding="utf-8"))
            self.assertIn("source-project.raw_dataset", (task / "cells" / "b0000.md").read_text(encoding="utf-8"))

    def test_notebook_markdown_route_does_not_make_inventory_incident(self):
        dictionary = self.dictionary
        notebook = json.dumps(
            {
                "cells": [
                    {"cell_type": "markdown", "metadata": {}, "source": ["source-project.unknown_dataset.table"]},
                    {"cell_type": "code", "metadata": {}, "source": ["SELECT * FROM source-project.raw_dataset.table"]},
                ],
                "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
            }
        ).encode("utf-8")
        resource = ResourceRef("notebook", "notebook-markdown", "source-project", "us", "notebook-markdown", "head")
        from queryflow.migration_pilot import _selection

        selection = _selection(resource, notebook, dictionary)
        self.assertEqual("known", selection.category)

    def test_notebook_incident_keeps_code_cell_location_for_report(self):
        notebook = json.dumps(
            {
                "cells": [
                    {"cell_type": "code", "metadata": {}, "source": ["SELECT * FROM source-project.unknown_dataset.table"]},
                ],
                "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
            }
        ).encode("utf-8")
        resource = ResourceRef("notebook", "notebook-incident", "source-project", "us", "notebook-incident", "head")
        from queryflow.migration_pilot import _selection

        selection = _selection(resource, notebook, self.dictionary, filename="content.ipynb")
        self.assertEqual("incident", selection.category)
        self.assertEqual(1, len(selection.unknown_details))
        self.assertEqual(0, selection.unknown_details[0]["cell"])
        self.assertEqual("cells/b0000.sql", selection.unknown_details[0]["path"])

    def test_notebook_without_cell_language_uses_language_info(self):
        notebook = json.dumps(
            {
                "cells": [
                    {
                        "cell_type": "code",
                        "metadata": {},
                        "source": [
                            "from google.cloud import bigquery\n",
                            "client = bigquery.Client()\n",
                            "client.query('SELECT * FROM `source-project.raw_dataset.table`')\n",
                        ],
                    }
                ],
                "metadata": {
                    "kernelspec": {"name": "python3", "display_name": "Python 3"},
                    "language_info": {"name": "python"},
                },
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ).encode("utf-8")

        extracted = analyze_sql_fragments(notebook)

        self.assertEqual([(0, "SELECT * FROM `source-project.raw_dataset.table`")], extracted.fragments)
        self.assertEqual([], extracted.dynamic_cells)


class DataformRateLimitTests(unittest.TestCase):
    class Clock:
        def __init__(self):
            self.now = 0.0
            self.sleeps = []

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.now += seconds

    def test_rate_limiter_waits_before_exceeding_300_request_window(self):
        clock = self.Clock()
        calls = []

        def transport(method, resource, query, body):
            calls.append(resource)
            return {}

        client = DataformClient(
            "analyst@example.com",
            "destination",
            transport=transport,
            requests_per_minute=2,
            clock=clock.monotonic,
            sleeper=clock.sleep,
        )
        resource = "projects/destination/locations/us/repositories/r"
        client.request("GET", resource, {}, None)
        client.request("GET", resource, {}, None)
        client.request("GET", resource, {}, None)

        self.assertEqual(3, len(calls))
        self.assertGreaterEqual(sum(clock.sleeps), 60.0)
        self.assertEqual(3, client.request_stats.requests_attempted)

    def test_rate_limit_error_retries_using_retry_after_and_records_stats(self):
        clock = self.Clock()
        attempts = []

        def transport(method, resource, query, body):
            attempts.append(len(attempts) + 1)
            if len(attempts) < 3:
                raise DataformRateLimitError("quota", retry_after_seconds=7)
            return {}

        client = DataformClient(
            "analyst@example.com",
            "destination",
            transport=transport,
            requests_per_minute=300,
            max_retries=3,
            clock=clock.monotonic,
            sleeper=clock.sleep,
        )
        client.request("GET", "projects/destination/locations/us/repositories/r", {}, None)

        self.assertEqual([1, 2, 3], attempts)
        self.assertEqual([7, 7], clock.sleeps)
        self.assertEqual(2, client.request_stats.retries)
        self.assertEqual(2, client.request_stats.rate_limit_responses)

    def test_rate_limit_error_is_not_retried_for_writes(self):
        attempts = []

        def transport(method, resource, query, body):
            attempts.append(method)
            raise DataformRateLimitError("quota")

        client = DataformClient(
            "analyst@example.com",
            "destination",
            transport=transport,
            requests_per_minute=300,
        )
        with self.assertRaises(DataformRateLimitError):
            client.request(
                "POST",
                "projects/destination/locations/us/repositories/r",
                {},
                {},
            )
        self.assertEqual(["POST"], attempts)

    def test_rate_limiter_paces_writes_without_retrying_them(self):
        clock = self.Clock()
        calls = []

        def transport(method, resource, query, body):
            calls.append(method)
            return {}

        client = DataformClient(
            "analyst@example.com",
            "destination",
            transport=transport,
            requests_per_minute=1,
            clock=clock.monotonic,
            sleeper=clock.sleep,
        )
        resource = "projects/destination/locations/us/repositories/r"
        client.request("POST", resource, {}, {})
        client.request("POST", resource, {}, {})

        self.assertEqual(["POST", "POST"], calls)
        self.assertGreaterEqual(sum(clock.sleeps), 60.0)

    def test_http_429_preserves_retry_after_as_rate_limit_error(self):
        error = urllib.error.HTTPError(
            "https://dataform.googleapis.com/v1/projects/destination",
            429,
            "quota",
            {"Retry-After": "12"},
            io.BytesIO(b'{"error":{"status":"RESOURCE_EXHAUSTED"}}'),
        )
        client = DataformClient(
            "analyst@example.com",
            "destination",
            requests_per_minute=300,
            max_retries=0,
        )
        client._tokens.get = lambda: "token"
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(DataformRateLimitError) as raised:
                client.request("GET", "projects/destination/locations/us/repositories/r", {}, None)
        self.assertEqual(12.0, raised.exception.retry_after_seconds)
        self.assertEqual(1, client.request_stats.rate_limit_responses)


class MigrationPilotTests(unittest.TestCase):
    def _resource(self, index: int, kind: str = "shared_query") -> ResourceRef:
        return ResourceRef(
            kind=kind,
            name=f"projects/source/locations/us/repositories/r{index}",
            project="source",
            location="us",
            display_name=f"resource-{index}",
            fingerprint=f"head-{index}",
        )

    def test_inventory_candidate_pool_limit_is_deterministic_per_kind(self):
        resources = [
            ResourceRef(kind, f"projects/source/locations/us/repositories/{kind}-{i}", "source", "us", f"{kind}-{i}", f"head-{kind}-{i}")
            for kind in ("shared_query", "notebook")
            for i in range(10)
        ]
        first = _limit_pilot_resource_pool(resources, seed="pool", limit=3)
        second = _limit_pilot_resource_pool(resources, seed="pool", limit=3)
        self.assertEqual([item.name for item in first], [item.name for item in second])
        self.assertEqual({"shared_query": 3, "notebook": 3}, {kind: sum(1 for item in first if item.kind == kind) for kind in ("shared_query", "notebook")})

    def test_inventory_candidate_pool_interleaves_kinds(self):
        resources = [
            ResourceRef(kind, f"projects/source/locations/us/repositories/{kind}-{i}", "source", "us", f"{kind}-{i}", f"head-{kind}-{i}")
            for kind in ("shared_query", "notebook")
            for i in range(3)
        ]
        pool = _limit_pilot_resource_pool(resources, seed="pool", limit=None)
        self.assertEqual(
            ["shared_query", "notebook", "shared_query", "notebook", "shared_query", "notebook"],
            [item.kind for item in pool],
        )

    def test_inventory_checkpoint_round_trips_without_sql_content(self):
        resource = self._resource(1)
        selection = PilotSelection(resource, "known", "a" * 64, applied_mappings=("raw",))
        checkpoint = InventoryCheckpoint(
            source_project="source",
            destination_project="dest",
            dictionary_sha256="d" * 64,
            seed="seed",
            catalog_generated_at="catalog-time",
            selections=(selection,),
            inventory_errors=({"resource": resource.to_dict(), "error": "429 quota"},),
            stats={"requests_attempted": 4},
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "inventory-checkpoint.json"
            save_inventory_checkpoint(checkpoint, path)
            loaded = load_inventory_checkpoint(path)
            serialized = path.read_text(encoding="utf-8")
        self.assertEqual(checkpoint.source_project, loaded.source_project)
        self.assertEqual(checkpoint.selections[0].content_sha256, loaded.selections[0].content_sha256)
        self.assertNotIn("SELECT", serialized)

    def test_campaign_publish_digest_is_stable_and_binds_selection(self):
        dictionary = RouteDictionary.from_mapping(
            {
                "schema_version": 1,
                "dictionary_id": "digest-routes",
                "mappings": [{"id": "r", "zone": "raw", "old": "source.raw", "new": "dest.raw", "active": True}],
            }
        )
        manifest = build_manifest(
            [PilotSelection(self._resource(1), "known", "a" * 64, applied_mappings=("r",))],
            source_project="source",
            destination_project="dest",
            dictionary=dictionary,
            seed="seed",
        )
        first = campaign_publish_digest(manifest)
        second = campaign_publish_digest(PilotManifest.from_dict(manifest.to_dict()))
        self.assertEqual(first, second)
        self.assertEqual(64, len(first))

    def test_pilot_prepare_creates_local_diffs_without_remote_writes(self):
        dictionary_payload = {
            "schema_version": 1,
            "dictionary_id": "prepare-routes",
            "scope": {"source_project": "source-project", "destination_project": "dest-project"},
            "mappings": [{"id": "r", "zone": "raw", "old": "source-project.raw", "new": "dest-project.raw", "active": True}],
        }
        dictionary = RouteDictionary.from_mapping(dictionary_payload)
        selections = []
        contents = {}
        for kind in ("shared_query", "notebook"):
            for index in range(10):
                category = "known" if index < 5 else "incident" if index < 8 else "no_source_routes"
                resource = ResourceRef(kind, f"projects/source/locations/us/repositories/{kind}-{index}", "source", "us", f"{kind}-{index}", f"head-{kind}-{index}")
                sql = "SELECT * FROM source.raw.table" if category == "known" else "SELECT * FROM source.unknown.table" if category == "incident" else "SELECT 1"
                content = (
                    json.dumps({"cells": [{"cell_type": "code", "metadata": {}, "source": [sql]}], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}).encode("utf-8")
                    if kind == "notebook" else sql.encode("utf-8")
                )
                selections.append(PilotSelection(resource, category, __import__("hashlib").sha256(content).hexdigest()))
                contents[resource.name] = content
        manifest = build_manifest(
            selections,
            source_project="source",
            destination_project="dest",
            dictionary=dictionary,
            seed="prepare",
            kind_quotas={"shared_query": PilotQuotas(), "notebook": PilotQuotas()},
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_path = root / "manifest.json"
            dictionary_path = root / "routes.json"
            save_manifest(manifest, manifest_path)
            dictionary_path.write_text(json.dumps(dictionary_payload), encoding="utf-8")
            config_path = root / "config.toml"
            config_path.write_text(
                "active_profile = 'migration-pilot'\n\n[profiles.migration-pilot]\nmode = 'migration-pilot'\nsource_projects = ['source']\ndestination_projects = ['dest']\nworkspace_root = '" + str(root / "tasks") + "'\naudit_root = '" + str(root / "audit") + "'\n",
                encoding="utf-8",
            )
            writes = []

            class FakeDataformClient:
                def __init__(self, *_args, **_kwargs):
                    self.request_stats = type("Stats", (), {"to_dict": lambda _self: {}})()

                def set_gcloud_context(self, _context):
                    pass

                def export(self, resource):
                    return ExportedAsset(resource, "content.ipynb" if resource.kind == "notebook" else "content.sql", contents[resource.name], {}, resource.fingerprint)

                def create_copy(self, **_kwargs):
                    writes.append("create_copy")
                    raise AssertionError("prepare no debe publicar")

            output = StringIO()
            with patch("queryflow.cli.DataformClient", FakeDataformClient), redirect_stdout(output):
                self.assertEqual(
                    0,
                    main([
                        "pilot", "prepare", "--manifest", str(manifest_path), "--dictionary", str(dictionary_path),
                        "--account", "analyst@example.com", "--config", str(config_path), "--json",
                    ]),
                )
            payload = json.loads(output.getvalue())
            self.assertFalse(writes)
            self.assertEqual(20, payload["prepared_count"])
            self.assertTrue(Path(payload["campaign_review"]).exists())
            self.assertEqual(20, len(list((root / "tasks" / "migration" / manifest.campaign_id).glob("*/review.html"))))
            self.assertIn("Propuesta", Path(payload["campaign_review"]).read_text(encoding="utf-8"))

    def test_classified_selection_builds_manifest_without_reclassifying_content(self):
        dictionary = RouteDictionary.from_mapping(
            {
                "schema_version": 1,
                "dictionary_id": "classified-sample",
                "mappings": [{"id": "r", "zone": "raw", "old": "source.raw", "new": "dest.raw", "active": True}],
            }
        )
        selections = []
        for kind in ("shared_query", "notebook"):
            for index in range(10):
                category = "known" if index < 5 else "incident" if index < 8 else "no_source_routes"
                selections.append(
                    PilotSelection(
                        ResourceRef(kind, f"projects/source/locations/us/repositories/{kind}-{index}", "source", "us", f"{kind}-{index}", f"h-{kind}-{index}"),
                        category,
                        "h" * 64,
                    )
                )
        manifest = select_classified(
            selections,
            source_project="source",
            destination_project="dest",
            dictionary=dictionary,
            seed="classified",
        )
        self.assertEqual(20, len(manifest.selections))
        self.assertEqual({"shared_query": 10, "notebook": 10}, {kind: sum(1 for item in manifest.selections if item.resource.kind == kind) for kind in ("shared_query", "notebook")})

    def test_destination_collision_filter_does_not_treat_blank_display_names_as_collisions(self):
        dictionary = RouteDictionary.from_mapping(
            {
                "schema_version": 1,
                "dictionary_id": "blank-display",
                "mappings": [{"id": "r", "zone": "raw", "old": "source.raw", "new": "dest.raw", "active": True}],
            }
        )
        resources = []
        contents = {}
        for kind in ("shared_query", "notebook"):
            for index in range(20):
                name = f"projects/source/locations/us/repositories/{kind}-{index}"
                display_name = "" if index == 0 else f"{kind}-{index}"
                resource = ResourceRef(kind, name, "source", "us", display_name, f"head-{kind}-{index}")
                resources.append(resource)
                contents[name] = "SELECT * FROM source.raw.table" if index < 5 else "SELECT * FROM source.unknown.table" if index < 13 else "SELECT 1"
        manifest = select_campaign(
            resources,
            contents,
            dictionary,
            source_project="source",
            destination_project="dest",
            seed="blank",
            destination_display_names=[""],
        )
        self.assertEqual(20, len(manifest.selections))

    def test_inventory_shortfall_report_records_missing_strata_without_sql(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = Path(temp) / "manifest.json"
            report = _write_campaign_inventory_shortfall_report(
                manifest_path,
                source_project="source",
                destination_project="dest",
                seed="s",
                candidate_pool_count=20,
                resources_read=18,
                category_counts={
                    "shared_query": {"known": 5, "incident": 3, "no_source_routes": 2},
                    "notebook": {"known": 2, "incident": 3, "no_source_routes": 10},
                },
                inventory_errors=[],
                dataform_policy={"region": "us-east1", "client_limit_requests_per_minute": 180},
                dataform_stats={"requests_attempted": 18, "rate_limit_responses": 1},
            )
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual("quota_shortfall", payload["status"])
            self.assertEqual(3, payload["missing_per_kind"]["notebook"]["known"])
            self.assertEqual(180, payload["dataform_policy"]["client_limit_requests_per_minute"])
            self.assertEqual(18, payload["dataform_stats"]["requests_attempted"])
            self.assertNotIn("SELECT", report.read_text(encoding="utf-8"))

    def test_inventory_incident_report_is_available_before_manifest(self):
        resource = self._resource(99, "notebook")
        selection = PilotSelection(
            resource,
            "incident",
            "a" * 64,
            unknown_details=(
                {"path": "cells/b0002.sql", "cell": 2, "route": "source.unknown.table", "start": 10, "end": 30},
            ),
        )
        report = make_inventory_incident_report([selection], campaign_id="migration-shortfall")
        self.assertEqual("inventory_shortfall", report["status"])
        self.assertEqual("cells/b0002.sql", report["incidents"][0]["path"])
        self.assertNotIn("SELECT", json.dumps(report))

    def test_dataform_policy_rejects_rate_above_project_quota(self):
        dictionary_payload = {
            "schema_version": 1,
            "dictionary_id": "rate-policy",
            "scope": {"source_project": "source", "destination_project": "dest"},
            "mappings": [{"id": "r", "zone": "raw", "old": "source.raw", "new": "dest.raw", "active": True}],
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dictionary_path = root / "routes.json"
            dictionary_path.write_text(json.dumps(dictionary_payload), encoding="utf-8")
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps({"generated_at": "now", "resources": [], "warnings": []}), encoding="utf-8")
            config_path = root / "config.toml"
            config_path.write_text(
                "active_profile = 'migration-pilot'\n\n"
                "[profiles.migration-pilot]\n"
                "mode = 'migration-pilot'\n"
                "source_projects = ['source-project']\n"
                "destination_projects = ['dest-project']\n",
                encoding="utf-8",
            )
            errors = StringIO()
            with redirect_stderr(errors):
                status = main([
                    "pilot", "inventory", "--dictionary", str(dictionary_path), "--catalog", str(catalog_path),
                    "--source-project", "source-project", "--destination-project", "dest-project",
                    "--content-dir", str(root), "--dataform-requests-per-minute", "301", "--config", str(config_path),
                ])
            self.assertEqual(2, status)
            self.assertIn("entre 1 y 300", errors.getvalue())

    def test_stratified_selection_is_deterministic_and_uses_5_3_2_quota(self):
        dictionary = RouteDictionary.from_mapping(
            {
                "schema_version": 1,
                "dictionary_id": "sample",
                "mappings": [
                    {"id": "known", "zone": "raw", "old": "source.raw", "new": "dest.raw", "active": True}
                ],
            }
        )
        resources = [self._resource(i) for i in range(20)]
        contents = {
            resource.name: ("SELECT * FROM source.raw.t" if i < 8 else "SELECT 1" if i < 16 else "SELECT * FROM source.other.t")
            for i, resource in enumerate(resources)
        }
        first = select_stratified(resources, contents, dictionary, seed="fixed")
        second = select_stratified(resources, contents, dictionary, seed="fixed")

        self.assertEqual([item.resource.name for item in first], [item.resource.name for item in second])
        self.assertEqual(10, len(first))
        counts = {}
        for item in first:
            counts[item.category] = counts.get(item.category, 0) + 1
        self.assertEqual({"known": 5, "incident": 3, "no_source_routes": 2}, counts)

    def test_stratified_selection_fails_before_writes_when_stratum_is_too_small(self):
        dictionary = RouteDictionary.from_mapping(
            {
                "schema_version": 1,
                "dictionary_id": "sample",
                "mappings": [
                    {"id": "known", "zone": "raw", "old": "source.raw", "new": "dest.raw", "active": True}
                ],
            }
        )
        resources = [self._resource(i) for i in range(10)]
        contents = {resource.name: "SELECT 1" for resource in resources}
        with self.assertRaises(CampaignError):
            select_stratified(resources, contents, dictionary, seed="fixed")

    def test_stratified_selection_never_returns_duplicate_canonical_resources(self):
        dictionary = RouteDictionary.from_mapping(
            {
                "schema_version": 1,
                "dictionary_id": "duplicate-sample",
                "mappings": [
                    {"id": "known", "zone": "raw", "old": "source.raw", "new": "dest.raw", "active": True}
                ],
            }
        )
        resources = [self._resource(i) for i in range(20)] + [self._resource(0)]
        contents = {
            resource.name: (
                "SELECT * FROM source.raw.t" if int(resource.name.rsplit("/", 1)[-1][1:]) < 8
                else "SELECT 1" if int(resource.name.rsplit("/", 1)[-1][1:]) < 16
                else "SELECT * FROM source.other.t"
            )
            for resource in resources
        }
        selected = select_stratified(resources, contents, dictionary, seed="a")
        names = [item.resource.name for item in selected]
        self.assertEqual(len(names), len(set(names)))

    def test_cleanup_digest_changes_when_exact_repository_set_changes(self):
        first = cleanup_digest(["projects/destination/locations/us/repositories/a"])
        second = cleanup_digest([
            "projects/destination/locations/us/repositories/a",
            "projects/destination/locations/us/repositories/b",
        ])
        self.assertNotEqual(first, second)
        self.assertEqual(64, len(first))

    def test_migration_profile_loads_without_enabling_normal_force_or_sql(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.toml"
            path.write_text(
                "active_profile = 'migration-pilot'\n\n"
                "[profiles.migration-pilot]\n"
                "mode = 'migration-pilot'\n"
                "source_projects = ['source']\n"
                "destination_projects = ['destination']\n",
                encoding="utf-8",
            )
            config = load_config(path)
        self.assertEqual("migration-pilot", config.mode)
        self.assertFalse(config.allow_force_publish)

    def test_dataform_cleanup_request_is_non_force_and_destination_scoped(self):
        calls = []

        def transport(method, resource, query, body):
            calls.append((method, resource, query, body))
            return {}

        client = DataformClient("analyst@example.com", "destination", transport=transport)
        client.delete_repository("projects/destination/locations/us/repositories/pilot-copy", force=False)
        self.assertEqual("DELETE", calls[0][0])
        self.assertEqual({"force": "false"}, calls[0][2])
        with self.assertRaises(Exception):
            client.delete_repository("projects/source/locations/us/repositories/original", force=False)

    def test_dataform_request_timeout_is_configurable_for_inventory(self):
        client = DataformClient("analyst@example.com", "destination", request_timeout_seconds=12)
        self.assertEqual(12, client.request_timeout_seconds)
        with self.assertRaises(ValueError):
            DataformClient("analyst@example.com", "destination", request_timeout_seconds=0)

    def test_dataform_list_files_handles_nested_directories(self):
        def transport(method, resource, query, body):
            self.assertEqual("GET", method)
            self.assertTrue(resource.endswith(":queryDirectoryContents"))
            if query.get("path") == "nested":
                return {"directoryEntries": [{"file": "nested/content.ipynb"}]}
            return {"directoryEntries": [{"directory": "nested"}, {"file": "root.sql"}]}

        client = DataformClient("analyst@example.com", "destination", transport=transport)
        files = client.list_files("projects/destination/locations/us/repositories/pilot")
        self.assertEqual(["nested/content.ipynb", "root.sql"], files)

    def test_cli_dictionary_validate_and_rewrite_plan_are_local(self):
        dictionary_payload = {
            "schema_version": 1,
            "dictionary_id": "cli-routes",
            "scope": {"source_project": "source-project"},
            "mappings": [
                {"id": "raw", "zone": "raw", "old": "source-project.raw", "new": "dest-project.raw", "active": True}
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dictionary_path = root / "routes.json"
            dictionary_path.write_text(json.dumps(dictionary_payload), encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(0, main(["migration", "dictionary", "validate", "--dictionary", str(dictionary_path), "--json"]))
            self.assertIn('"active_mappings": 1', output.getvalue())
            task = create_workspace(
                root=root / "tasks",
                task_id="cli-rewrite",
                resource=ResourceRef("shared_query", "repository", "source-project", "us", "q", "head"),
                content=b"SELECT * FROM source-project.raw.table;\n",
                filename="content.sql",
                mode="copy",
            )
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(0, main(["migration", "rewrite", "plan", "--task", str(task), "--dictionary", str(dictionary_path), "--json"]))
            plan = json.loads(output.getvalue())
            self.assertEqual(["content.sql"], plan["changed_files"])
            self.assertFalse((task / "rewrite-report.json").exists())

    def test_policy_allows_campaign_publish_only_in_migration_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.toml"
            path.write_text(
                "active_profile = 'migration-pilot'\n\n[profiles.migration-pilot]\nmode = 'migration-pilot'\n",
                encoding="utf-8",
            )
            output = StringIO()
            errors = StringIO()
            with redirect_stdout(output):
                with redirect_stderr(errors):
                    status = main([
                    "policy", "check", "--config", str(path), "--operation", "campaign_publish",
                    "--resource-kind", "shared_query", "--mode", "copy", "--json",
                    ])
            self.assertEqual(0, status)
            self.assertIn('"allowed": true', output.getvalue())

    def test_pilot_run_without_switch_is_plan_only(self):
        dictionary = RouteDictionary.from_mapping(
            {
                "schema_version": 1,
                "dictionary_id": "plan-only",
                "mappings": [
                    {"id": "known", "zone": "raw", "old": "source.raw", "new": "dest.raw", "active": True}
                ],
            }
        )
        resources = [
            ResourceRef(kind, f"projects/source/locations/us/repositories/{kind}-{i}", "source", "us", f"{kind}-{i}", f"h-{kind}-{i}")
            for kind in ("shared_query", "notebook")
            for i in range(10)
        ]
        selections = [
            # This test only exercises the command guard; a full inventory is
            # covered by select_stratified above.
            __import__("queryflow.migration_pilot", fromlist=["PilotSelection"]).PilotSelection(
                resource,
                "known" if i % 10 < 5 else "incident" if i % 10 < 8 else "no_source_routes",
                "hash",
            )
            for i, resource in enumerate(resources)
        ]
        manifest = build_manifest(
            selections,
            source_project="source",
            destination_project="dest-project",
            dictionary=dictionary,
            seed="seed",
            kind_quotas={"shared_query": __import__("queryflow.migration_pilot", fromlist=["PilotQuotas"]).PilotQuotas(), "notebook": __import__("queryflow.migration_pilot", fromlist=["PilotQuotas"]).PilotQuotas()},
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_path = root / "manifest.json"
            dictionary_path = root / "routes.json"
            save_manifest(manifest, manifest_path)
            dictionary_path.write_text(json.dumps(dictionary.to_dict()), encoding="utf-8")
            config = root / "config.toml"
            config.write_text(
                "active_profile = 'migration-pilot'\n\n[profiles.migration-pilot]\nmode = 'migration-pilot'\n",
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(0, main([
                    "pilot", "run", "--manifest", str(manifest_path), "--dictionary", str(dictionary_path),
                    "--config", str(config), "--json",
                ]))
            payload = json.loads(output.getvalue())
            self.assertFalse(payload["write_enabled"])
            self.assertEqual(campaign_publish_digest(manifest), payload["publication_digest"])

    def test_pilot_run_execute_requires_campaign_digest(self):
        dictionary = RouteDictionary.from_mapping(
            {
                "schema_version": 1,
                "dictionary_id": "execute-digest",
                "mappings": [{"id": "r", "zone": "raw", "old": "source.raw", "new": "dest.raw", "active": True}],
            }
        )
        selections = []
        for kind in ("shared_query", "notebook"):
            for i in range(10):
                category = "known" if i < 5 else "incident" if i < 8 else "no_source_routes"
                selections.append(
                    PilotSelection(
                        ResourceRef(kind, f"projects/source/locations/us/repositories/{kind}-{i}", "source", "us", f"{kind}-{i}", f"h-{kind}-{i}"),
                        category,
                        "h" * 64,
                    )
                )
        manifest = build_manifest(
            selections,
            source_project="source",
            destination_project="dest-project",
            dictionary=dictionary,
            seed="s",
            kind_quotas={"shared_query": PilotQuotas(), "notebook": PilotQuotas()},
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_path = root / "manifest.json"
            dictionary_path = root / "routes.json"
            save_manifest(manifest, manifest_path)
            dictionary_path.write_text(json.dumps(dictionary.to_dict()), encoding="utf-8")
            config = root / "config.toml"
            config.write_text(
                "active_profile = 'migration-pilot'\n\n[profiles.migration-pilot]\nmode = 'migration-pilot'\nsource_projects = ['source']\ndestination_projects = ['dest-project']\n",
                encoding="utf-8",
            )
            errors = StringIO()
            with redirect_stderr(errors):
                status = main([
                    "pilot", "run", "--manifest", str(manifest_path), "--dictionary", str(dictionary_path),
                    "--execute-migration", "--account", "analyst@example.com", "--config", str(config),
                ])
            self.assertEqual(2, status)
            self.assertIn("approved-digest", errors.getvalue())

    def test_pilot_run_execute_requires_migration_profile(self):
        dictionary = RouteDictionary.from_mapping(
            {
                "schema_version": 1,
                "dictionary_id": "execute-guard",
                "mappings": [{"id": "r", "zone": "raw", "old": "source.raw", "new": "dest.raw", "active": True}],
            }
        )
        from queryflow.migration_pilot import PilotSelection, PilotQuotas
        selections = []
        for kind in ("shared_query", "notebook"):
            for i in range(10):
                selections.append(PilotSelection(ResourceRef(kind, f"projects/source/locations/us/repositories/{kind}-{i}", "source", "us", f"{kind}-{i}", f"h-{kind}-{i}"), "known" if i < 5 else "incident" if i < 8 else "no_source_routes", "hash"))
        manifest = build_manifest(selections, source_project="source", destination_project="dest-project", dictionary=dictionary, seed="s", kind_quotas={"shared_query": PilotQuotas(), "notebook": PilotQuotas()})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_path = root / "manifest.json"
            dictionary_path = root / "routes.json"
            save_manifest(manifest, manifest_path)
            dictionary_path.write_text(json.dumps(dictionary.to_dict()), encoding="utf-8")
            config = root / "config.toml"
            config.write_text("active_profile = 'pilot'\n\n[profiles.pilot]\nmode = 'pilot'\n", encoding="utf-8")
            errors = StringIO()
            with redirect_stderr(errors):
                status = main(["pilot", "run", "--manifest", str(manifest_path), "--dictionary", str(dictionary_path), "--execute-migration", "--config", str(config)])
            self.assertEqual(2, status)
            self.assertIn("migration-pilot", errors.getvalue())

    def test_inventory_builds_ten_resources_per_kind_from_private_snapshots(self):
        dictionary_payload = {
            "schema_version": 1,
            "dictionary_id": "inventory-routes",
            "scope": {"source_project": "source-project", "destination_project": "dest-project"},
            "mappings": [
                {"id": "known", "zone": "raw", "old": "source-project.raw", "new": "dest-project.raw", "active": True}
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dictionary_path = root / "routes.json"
            dictionary_path.write_text(json.dumps(dictionary_payload), encoding="utf-8")
            resources = []
            snapshots = root / "snapshots"
            snapshots.mkdir()
            for kind in ("shared_query", "notebook"):
                for index in range(20):
                    name = f"projects/source-project/locations/us/repositories/{kind}-{index}"
                    resource = ResourceRef(kind, name, "source-project", "us", f"{kind}-{index}", f"head-{kind}-{index}")
                    resources.append(resource.to_dict())
                    if index < 8:
                        source = "SELECT * FROM source-project.raw.table"
                    elif index < 16:
                        source = "SELECT 1"
                    else:
                        source = "SELECT * FROM source-project.unknown.table"
                    if kind == "notebook":
                        source = json.dumps({"cells": [{"cell_type": "code", "metadata": {}, "source": [source]}], "metadata": {}, "nbformat": 4, "nbformat_minor": 5})
                    key = __import__("hashlib").sha256(name.encode("utf-8")).hexdigest()
                    (snapshots / f"{key}.txt").write_text(source, encoding="utf-8")
            (root / "catalog.json").write_text(json.dumps({"generated_at": "now", "resources": resources, "warnings": []}), encoding="utf-8")
            output_path = root / "manifest.json"
            output = StringIO()
            errors = StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                status = main([
                    "pilot", "inventory", "--dictionary", str(dictionary_path), "--catalog", str(root / "catalog.json"),
                    "--content-dir", str(snapshots), "--source-project", "source-project", "--destination-project", "dest-project",
                    "--output", str(output_path), "--json",
                ])
            self.assertEqual(0, status, output.getvalue() + errors.getvalue())
            payload = json.loads(output.getvalue())
            self.assertEqual(20, payload["selection_count"])
            self.assertEqual(180, payload["dataform_policy"]["client_limit_requests_per_minute"])
            self.assertEqual(300, payload["dataform_policy"]["quota_requests_per_minute"])
            self.assertTrue((root / "route-incidents.json").exists())
            saved = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual({"known": 5, "incident": 3, "no_source_routes": 2}, saved["kind_quotas"]["shared_query"])
            self.assertEqual({"known": 5, "incident": 3, "no_source_routes": 2}, saved["kind_quotas"]["notebook"])

    def test_remote_inventory_persists_dataform_head_for_campaign_conflict_check(self):
        dictionary_payload = {
            "schema_version": 1,
            "dictionary_id": "remote-inventory-routes",
            "scope": {"source_project": "source-project", "destination_project": "dest-project"},
            "mappings": [
                {"id": "known", "zone": "raw", "old": "source-project.raw", "new": "dest-project.raw", "active": True}
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dictionary_path = root / "routes.json"
            dictionary_path.write_text(json.dumps(dictionary_payload), encoding="utf-8")
            resources = []
            remote_contents = {}
            for kind in ("shared_query", "notebook"):
                for index in range(20):
                    name = f"projects/source-project/locations/us/repositories/{kind}-{index}"
                    resource = ResourceRef(kind, name, "source-project", "us", f"{kind}-{index}", f"catalog-head-{index}")
                    resources.append(resource.to_dict())
                    sql = (
                        "SELECT * FROM source-project.raw.table" if index < 8
                        else "SELECT 1" if index < 16
                        else "SELECT * FROM source-project.unknown.table"
                    )
                    remote_contents[name] = (
                        json.dumps({"cells": [{"cell_type": "code", "metadata": {}, "source": [sql]}], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}).encode("utf-8")
                        if kind == "notebook" else sql.encode("utf-8")
                    )
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps({"generated_at": "now", "resources": resources, "warnings": []}), encoding="utf-8")
            manifest_path = root / "manifest.json"

            class FakeDataformClient:
                def __init__(self, *_args, **_kwargs):
                    pass

                def set_gcloud_context(self, _context):
                    pass

                def export(self, resource):
                    return ExportedAsset(
                        resource=resource,
                        filename="content.ipynb" if resource.kind == "notebook" else "content.sql",
                        content=remote_contents[resource.name],
                        metadata={},
                        head_commit=f"dataform-head-{resource.name.rsplit('/', 1)[-1]}",
                    )

            config_path = root / "config.toml"
            config_path.write_text("active_profile = 'pilot'\n\n[profiles.pilot]\nmode = 'pilot'\n", encoding="utf-8")
            output = StringIO()
            with patch("queryflow.cli.DataformClient", FakeDataformClient), redirect_stdout(output):
                self.assertEqual(
                    0,
                    main([
                        "pilot", "inventory", "--dictionary", str(dictionary_path), "--catalog", str(catalog_path),
                        "--source-project", "source-project", "--destination-project", "dest-project",
                        "--account", "analyst@example.com", "--output", str(manifest_path), "--config", str(config_path), "--json",
                    ]),
                )
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(saved["selections"])
            self.assertTrue(all(item["resource"]["fingerprint"].startswith("dataform-head-") for item in saved["selections"]))

    def test_inventory_resume_skips_completed_checkpoint_entries(self):
        dictionary_payload = {
            "schema_version": 1,
            "dictionary_id": "resume-routes",
            "scope": {"source_project": "source-project", "destination_project": "dest-project"},
            "mappings": [
                {"id": "known", "zone": "raw", "old": "source-project.raw", "new": "dest-project.raw", "active": True}
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dictionary_path = root / "routes.json"
            dictionary_path.write_text(json.dumps(dictionary_payload), encoding="utf-8")
            resources = []
            remote_contents = {}
            for kind in ("shared_query", "notebook"):
                for index in range(20):
                    name = f"projects/source-project/locations/us/repositories/{kind}-{index}"
                    resource = ResourceRef(kind, name, "source-project", "us", f"{kind}-{index}", f"catalog-head-{kind}-{index}")
                    resources.append(resource.to_dict())
                    sql = "SELECT * FROM source-project.raw.table" if index < 8 else "SELECT 1" if index < 16 else "SELECT * FROM source-project.unknown.table"
                    remote_contents[name] = (
                        json.dumps({"cells": [{"cell_type": "code", "metadata": {}, "source": [sql]}], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}).encode("utf-8")
                        if kind == "notebook" else sql.encode("utf-8")
                    )
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps({"generated_at": "now", "resources": resources, "warnings": []}), encoding="utf-8")
            manifest_path = root / "manifest.json"
            first_name = resources[0]["name"]
            first_resource = ResourceRef.from_dict(resources[0])
            first_content = remote_contents[first_name]
            checkpoint = InventoryCheckpoint(
                source_project="source-project",
                destination_project="dest-project",
                dictionary_sha256=RouteDictionary.from_mapping(dictionary_payload).dictionary_sha256,
                seed="resume-seed",
                catalog_generated_at="now",
                selections=(PilotSelection(first_resource, "known", __import__("hashlib").sha256(first_content).hexdigest()),),
                inventory_errors=(),
                stats={},
            )
            save_inventory_checkpoint(checkpoint, root / "inventory-checkpoint.json")
            calls = []

            class FakeDataformClient:
                def __init__(self, *_args, **_kwargs):
                    pass

                def set_gcloud_context(self, _context):
                    pass

                def export(self, resource):
                    calls.append(resource.name)
                    return ExportedAsset(resource, "content.ipynb" if resource.kind == "notebook" else "content.sql", remote_contents[resource.name], {}, f"dataform-head-{resource.display_name}")

            config_path = root / "config.toml"
            config_path.write_text("active_profile = 'pilot'\n\n[profiles.pilot]\nmode = 'pilot'\n", encoding="utf-8")
            output = StringIO()
            with patch("queryflow.cli.DataformClient", FakeDataformClient), redirect_stdout(output):
                self.assertEqual(
                    0,
                    main([
                        "pilot", "inventory", "--dictionary", str(dictionary_path), "--catalog", str(catalog_path),
                        "--source-project", "source-project", "--destination-project", "dest-project",
                        "--account", "analyst@example.com", "--output", str(manifest_path),
                        "--config", str(config_path), "--seed", "resume-seed", "--resume-inventory", "--json",
                    ]),
                )
            self.assertNotIn(first_name, calls)

    def test_campaign_review_and_cleanup_plan_are_row_free_and_digest_bound(self):
        dictionary = RouteDictionary.from_mapping(
            {
                "schema_version": 1,
                "dictionary_id": "review-routes",
                "mappings": [{"id": "r", "zone": "raw", "old": "source.raw", "new": "dest.raw", "active": True}],
            }
        )
        from queryflow.migration_pilot import PilotSelection

        resource = ResourceRef("shared_query", "projects/source/locations/us/repositories/q", "source", "us", "query", "head")
        manifest = build_manifest([PilotSelection(resource, "known", "hash", applied_mappings=("r",))], source_project="source", destination_project="dest", dictionary=dictionary, seed="s")
        raw = manifest.to_dict()
        raw["cleanup"] = {"created_repositories": ["projects/dest/locations/us/repositories/copy"]}
        manifest = type(manifest).from_dict(raw)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_path = root / "manifest.json"
            save_manifest(manifest, manifest_path)
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(0, main(["pilot", "review", "--manifest", str(manifest_path), "--json"]))
                self.assertEqual(0, main(["pilot", "cleanup-plan", "--manifest", str(manifest_path), "--json"]))
            html = (root / "review.html").read_text(encoding="utf-8")
            self.assertIn("color-scheme: dark", html)
            self.assertNotIn("SELECT", html)
            self.assertIn("approved_digest", output.getvalue())

    def test_manifest_validation_rejects_resource_outside_source_project(self):
        dictionary = RouteDictionary.from_mapping(
            {
                "schema_version": 1,
                "dictionary_id": "manifest-routes",
                "mappings": [{"id": "r", "zone": "raw", "old": "source.raw", "new": "dest.raw", "active": True}],
            }
        )
        selections = []
        for kind in ("shared_query", "notebook"):
            for index in range(10):
                category = "known" if index < 5 else "incident" if index < 8 else "no_source_routes"
                selections.append(
                    PilotSelection(
                        ResourceRef(kind, f"projects/source/locations/us/repositories/{kind}-{index}", "source", "us", f"{kind}-{index}", f"h-{kind}-{index}"),
                        category,
                        "a" * 64,
                    )
                )
        manifest = build_manifest(
            selections,
            source_project="source",
            destination_project="dest",
            dictionary=dictionary,
            seed="s",
            kind_quotas={"shared_query": PilotQuotas(), "notebook": PilotQuotas()},
        )
        validate_pilot_manifest(manifest)
        raw = manifest.to_dict()
        raw["selections"][0]["resource"]["project"] = "other"
        tampered = PilotManifest.from_dict(raw)
        with self.assertRaises(CampaignError):
            validate_pilot_manifest(tampered)


if __name__ == "__main__":
    unittest.main()
