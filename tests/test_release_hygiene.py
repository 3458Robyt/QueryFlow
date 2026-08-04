import tempfile
import unittest
from pathlib import Path

from queryflow.release_hygiene import inspect_paths


class ReleaseHygieneTests(unittest.TestCase):
    def test_rejects_internal_identifiers_and_operational_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            internal_id = "analytics-" + "487218"
            (root / "README.md").write_text(f"projects/{internal_id}", encoding="utf-8")
            (root / "tasks").mkdir()

            findings = inspect_paths(root)

        rules = {finding.rule for finding in findings}
        self.assertIn("internal_identifier", rules)
        self.assertIn("excluded_path", rules)


if __name__ == "__main__":
    unittest.main()
