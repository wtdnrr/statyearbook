from __future__ import annotations

import unittest

from app.validation.llm_translation_review import (
    SourceReviewItem,
    normalize_decision,
    looks_like_translation_mismatch,
    review_decision_needs_retry,
)


class LLMTranslationReviewTest(unittest.TestCase):
    def source_item(self, **overrides: object) -> SourceReviewItem:
        values: dict[str, object] = {
            "issue_id": 1,
            "source_rule_id": "source.blue_text_review",
            "candidate_kind": "blue_text",
            "candidate_reason": "파란색 원문",
            "candidate_expected": "",
            "table_id": 1,
            "table_code": "2-1-3-3",
            "table_title": "테스트",
            "table_title_en": "Test",
            "unit": "건",
            "base_date": "2025. 12. 31.",
            "location": "출처",
            "row_index": None,
            "col_index": None,
            "current_value": "www.gov.kr)",
            "cell_text": "정부24(www.gov.kr)",
            "row_label": "",
            "column_label": "",
            "surrounding_rows": [],
        }
        values.update(overrides)
        return SourceReviewItem(**values)  # type: ignore[arg-type]

    def test_url_closing_parenthesis_uses_full_source_context(self) -> None:
        item = self.source_item()
        decision = normalize_decision(
            {
                "status": "오류 의심",
                "issue_type": "오탈자 검수",
                "expected_value": "www.gov.kr",
                "difference": "닫는 괄호",
                "detail": "괄호 오류",
            },
            item,
        )
        self.assertEqual(decision["status"], "정상")
        self.assertEqual(decision["expected_value"], "www.gov.kr)")

    def test_missing_or_explanatory_translation_is_retried(self) -> None:
        item = self.source_item(current_value="고준위방사성폐기물관리위원회")
        self.assertTrue(
            review_decision_needs_retry(
                {"status": "확인 필요", "issue_type": "번역 검수", "expected_value": ""},
                item,
            )
        )
        self.assertTrue(
            review_decision_needs_retry(
                {
                    "status": "확인 필요",
                    "issue_type": "번역 검수",
                    "expected_value": "공식 영문 번역 확인이 필요합니다.",
                },
                item,
            )
        )
        self.assertFalse(
            review_decision_needs_retry(
                {
                    "status": "확인 필요",
                    "issue_type": "번역 검수",
                    "expected_value": "High-Level Radioactive Waste Management Committee",
                },
                item,
            )
        )

    def test_korean_only_blue_text_cannot_finish_with_copied_source(self) -> None:
        item = self.source_item(
            current_value="공공시설",
            cell_text="공공시설",
            location="1행 2열",
        )
        self.assertTrue(
            review_decision_needs_retry(
                {
                    "status": "정상",
                    "issue_type": "번역 검수",
                    "expected_value": "공공시설",
                },
                item,
            )
        )

    def test_semantic_translation_change_is_not_a_spelling_error(self) -> None:
        self.assertTrue(
            looks_like_translation_mismatch(
                "Inspection Target (number of elevators)",
                "Number of Companies (units)",
            )
        )
        item = self.source_item(
            current_value="국가데이터처 Statistics Korea",
            cell_text="국가데이터처 Statistics Korea",
            location="9행 3열",
        )
        decision = normalize_decision(
            {
                "status": "오류 의심",
                "issue_type": "오탈자 검수",
                "current_value": item.current_value,
                "expected_value": "Statistics Korea",
                "difference": "기관명 불일치",
                "detail": "기관의 공식 영문명을 확인해야 합니다.",
            },
            item,
        )
        self.assertEqual(decision["status"], "확인 필요")
        self.assertEqual(decision["issue_type"], "번역 검수")


if __name__ == "__main__":
    unittest.main()
