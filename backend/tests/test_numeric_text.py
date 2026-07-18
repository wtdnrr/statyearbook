from __future__ import annotations

import unittest

from app.core.numeric_text import numeric_text_anomaly, parse_numeric_value


class NumericTextTest(unittest.TestCase):
    def test_strict_numeric_parser_rejects_malformed_separators(self) -> None:
        self.assertEqual(parse_numeric_value("3,456"), 3456.0)
        self.assertEqual(parse_numeric_value("12.5%"), 12.5)
        self.assertIsNone(parse_numeric_value("12,34"))

    def test_unmatched_numeric_parenthesis_is_review_candidate(self) -> None:
        anomaly = numeric_text_anomaly("621)", unit="건", peer_values=["620", "622"])
        self.assertIsNotNone(anomaly)
        self.assertEqual(anomaly.suggested_value, "621")
        self.assertIsNone(numeric_text_anomaly("1)", unit="", peer_values=[]))
        self.assertIsNone(parse_numeric_value("3ㅡ456"))

    def test_dot_grouped_count_is_review_candidate(self) -> None:
        anomaly = numeric_text_anomaly(
            "3.456",
            unit="명",
            peer_values=["2,345", "4,567"],
        )
        self.assertIsNotNone(anomaly)
        self.assertEqual(anomaly.suggested_value, "3,456")

    def test_decimal_ratio_is_not_review_candidate(self) -> None:
        self.assertIsNone(
            numeric_text_anomaly(
                "3.456",
                unit="%",
                peer_values=["2.341", "4.125"],
            )
        )

    def test_dates_and_prose_commas_are_not_numeric_candidates(self) -> None:
        self.assertIsNone(numeric_text_anomaly("1998.4.1.", unit="건"))
        self.assertIsNone(numeric_text_anomaly("47.3(42.4)", unit="%"))
        self.assertIsNone(numeric_text_anomaly("513\n(463)", unit="건"))
        self.assertIsNone(numeric_text_anomaly("257,702\n(123.2%)", unit="개, %"))
        self.assertIsNone(numeric_text_anomaly("’24", unit="억원"))
        self.assertIsNone(
            numeric_text_anomaly(
                "총무처, 공보처, 법제처를 개편",
                unit="개",
            )
        )

    def test_percentage_annotation_is_excluded_from_calculation_value(self) -> None:
        self.assertEqual(parse_numeric_value("257,702\n(123.2%)"), 257702.0)
        self.assertEqual(parse_numeric_value("47.3(42.4%)"), 47.3)


if __name__ == "__main__":
    unittest.main()
