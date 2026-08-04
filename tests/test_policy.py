import tempfile
import unittest
from pathlib import Path

from queryflow.config_store import ConfigStore
from queryflow.config import load_config
from queryflow.policy import Policy, evaluate_policy


class PolicyTests(unittest.TestCase):
    def test_defaults_block_mutation_schedule_and_deletion(self):
        policy = Policy.default()

        self.assertFalse(policy.allow_sql_execution)
        self.assertFalse(policy.allow_delete)
        self.assertFalse(policy.allow_scheduled_queries)
        self.assertEqual(policy.sample_default_rows, 3)
        self.assertEqual(policy.sample_max_rows, 5)

    def test_policy_can_only_reduce_byte_limit(self):
        policy = Policy.default()

        self.assertEqual(policy.with_user_limit(1_000_000).max_bytes, 1_000_000)
        self.assertEqual(policy.with_user_limit(policy.max_bytes * 2).max_bytes, policy.max_bytes)

    def test_policy_rejects_scheduled_resource(self):
        decision = evaluate_policy(
            Policy.default(),
            operation="publish",
            resource_kind="scheduled_query",
            mode="new",
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.rule, "resource_kind.scheduled_query")


class ConfigStoreTests(unittest.TestCase):
    def test_init_round_trip_preserves_profile_and_never_writes_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            store = ConfigStore(path)
            store.initialize(
                profile="pilot",
                values={
                    "account": "analyst@example.com",
                    "source_projects": ["source-project"],
                    "destination_project": "analytics-project",
                    "workbench_project": "workbench-project",
                    "workbench_location": "us-east1-b",
                    "workbench_instance": "notebook-instance",
                    "workbench_job_project": "job-project",
                },
            )
            raw = path.read_text(encoding="utf-8")
            loaded = store.load()

        self.assertEqual(loaded.active_profile, "pilot")
        self.assertEqual(loaded.profiles["pilot"]["destination_project"], "analytics-project")
        self.assertNotIn("token", raw.lower())
        self.assertNotIn("password", raw.lower())

    def test_modern_toml_config_loads_profile_limits_and_enforces_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            ConfigStore(path).initialize(
                profile="team",
                values={
                    "mode": "team",
                    "allow_update_existing": True,
                    "max_bytes": 7_000_000_000,
                    "source_projects": ["source-project"],
                },
            )

            config = load_config(path)

        self.assertEqual(config.mode, "team")
        self.assertTrue(config.policy_enforced)
        self.assertEqual(config.profile_max_bytes, 7_000_000_000)
        self.assertEqual(config.source_projects, ("source-project",))


if __name__ == "__main__":
    unittest.main()
