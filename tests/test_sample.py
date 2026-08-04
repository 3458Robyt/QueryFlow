import unittest

from queryflow.sample import execution_digest, limit_query, sanitize_rows


class SampleTests(unittest.TestCase):
    def test_limit_query_wraps_read_query_and_caps_limit(self):
        query = "SELECT mes_mes, tipo_estado_siniestro FROM `source-project.dataset.table` ORDER BY mes_mes"

        limited = limit_query(query, 3)

        self.assertIn("LIMIT 3", limited.upper())
        self.assertIn("source-project.dataset.table", limited)
        self.assertNotIn("LIMIT 20", limited.upper())

    def test_limit_query_rejects_rows_above_five(self):
        with self.assertRaises(ValueError):
            limit_query("SELECT 1", 6)

    def test_limit_query_rejects_mutating_sql(self):
        with self.assertRaises(ValueError):
            limit_query("DELETE FROM `source-project.dataset.table` WHERE id = 1", 3)

    def test_sanitize_rows_bounds_columns_values_and_payload(self):
        rows = [{"a": "x" * 1000, "b": 2, "c": 3}]

        sanitized = sanitize_rows(rows, max_columns=2, max_value_chars=10, max_payload_bytes=100)

        self.assertEqual(list(sanitized.rows[0]), ["a", "b"])
        self.assertTrue(sanitized.truncated)
        self.assertLessEqual(sanitized.payload_bytes, 100)

    def test_execution_digest_changes_with_limit_and_query(self):
        first = execution_digest("SELECT 1", fragment_index=0, limit=3)
        second = execution_digest("SELECT 1", fragment_index=0, limit=5)

        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
