from __future__ import annotations

import unittest

from app.db.postgres import translate_sql


class PostgresAdapterTest(unittest.TestCase):
    def test_translates_placeholders_outside_string_literals(self) -> None:
        translated = translate_sql(
            "SELECT * FROM stat_tables WHERE code = ? AND title LIKE '%?%'"
        )
        self.assertEqual(
            translated,
            ("SELECT * FROM stat_tables WHERE code = %s AND title LIKE '%?%'", False),
        )

    def test_returns_insert_id_for_tables_that_used_lastrowid(self) -> None:
        translated = translate_sql("INSERT INTO annual_reports (year) VALUES (?)")
        self.assertEqual(
            translated,
            ("INSERT INTO annual_reports (year) VALUES (%s) RETURNING id", True),
        )

    def test_translates_sqlite_ignore_insert(self) -> None:
        translated = translate_sql(
            "INSERT OR IGNORE INTO linguistic_review_candidates (run_id) VALUES (?)"
        )
        self.assertEqual(
            translated,
            (
                "INSERT INTO linguistic_review_candidates (run_id) VALUES (%s) "
                "ON CONFLICT DO NOTHING",
                False,
            ),
        )

    def test_translates_sqlite_null_safe_is_placeholder(self) -> None:
        translated = translate_sql(
            "DELETE FROM validation_checks WHERE row_index IS ? AND col_index IS ?"
        )
        self.assertEqual(
            translated,
            (
                "DELETE FROM validation_checks WHERE row_index IS NOT DISTINCT FROM %s "
                "AND col_index IS NOT DISTINCT FROM %s",
                False,
            ),
        )


if __name__ == "__main__":
    unittest.main()
