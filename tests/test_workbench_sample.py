import unittest

from queryflow.workbench import build_sample_payload, parse_sample_summary


class WorkbenchSampleTests(unittest.TestCase):
    def test_payload_contains_bounded_query_and_hash(self):
        payload = build_sample_payload([(0, "SELECT 1")], maximum_bytes_billed=1000, limit=3)

        self.assertEqual(payload["maximum_bytes_billed"], 1000)
        self.assertEqual(payload["limit"], 3)
        self.assertIn("LIMIT 3", payload["fragments"][0]["sql"].upper())
        self.assertEqual(len(payload["fragments"][0]["sha256"]), 64)

    def test_summary_returns_rows_without_accepting_wrong_hash(self):
        fragments = [(0, "SELECT 1")]
        payload = build_sample_payload(fragments, maximum_bytes_billed=1000, limit=3)
        summary = {
            "ok": True,
            "fragments": [{
                "cell": 0,
                "sha256": payload["fragments"][0]["sha256"],
                "ok": True,
                "rows": [{"f0_": 1}],
            }],
        }

        result = parse_sample_summary(summary, fragments, limit=3)

        self.assertEqual(result["rows"], [{"f0_": 1}])
        self.assertFalse(result["truncated"])


if __name__ == "__main__":
    unittest.main()
