from __future__ import annotations

import unittest

from app.validation.models import restore_hyphenated_line_breaks
from app.validation.source_review import SOURCE_FORMAT_TYPE, suspicious_punctuation_reason


class SourceReviewTest(unittest.TestCase):
    def test_source_format_candidates_are_reported_as_spelling_checks(self) -> None:
        self.assertEqual(SOURCE_FORMAT_TYPE, "오탈자 검수")

    def test_layout_hyphen_is_restored_without_creating_an_issue(self) -> None:
        self.assertEqual(restore_hyphenated_line_breaks("Classifi -cation"), "Classification")
        self.assertEqual(suspicious_punctuation_reason("Classifi -cation"), "")

    def test_semantic_compound_hyphen_is_preserved(self) -> None:
        self.assertEqual(restore_hyphenated_line_breaks("e-learning"), "e-learning")
        self.assertEqual(suspicious_punctuation_reason("e-learning"), "")


if __name__ == "__main__":
    unittest.main()
