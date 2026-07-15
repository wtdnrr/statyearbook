from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from app.db.schema import connect, init_db
from app.validation.blue_review import insert_blue_review
from app.validation.llm_translation_review import (
    SourceReviewItem,
    compact_prompt_items,
    glossary_decision_for,
    linguistic_review_fingerprint,
    load_linguistic_review_items,
    normalize_decision,
    looks_like_translation_mismatch,
    review_decision_needs_retry,
    reuse_cached_linguistic_reviews,
    save_llm_review_decisions,
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
                {
                    "status": "확인 필요",
                    "issue_type": "번역 검수",
                    "expected_value": "",
                    "difference": "번역 누락",
                    "detail": "영문 번역이 필요합니다.",
                },
                item,
            )
        )
        self.assertTrue(
            review_decision_needs_retry(
                {
                    "status": "확인 필요",
                    "issue_type": "번역 검수",
                    "expected_value": "공식 영문 번역 확인이 필요합니다.",
                    "difference": "번역 누락",
                    "detail": "영문 번역이 필요합니다.",
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
                    "difference": "번역 제안",
                    "detail": "문맥에 맞는 영문 번역을 제안했습니다.",
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
                    "difference": "번역 누락",
                    "detail": "영문 번역이 필요합니다.",
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
            source_rule_id="source.linguistic_review",
            candidate_kind="translation_pair",
            requested_review_type="번역 검수",
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

    def test_requested_review_type_cannot_be_reclassified(self) -> None:
        item = self.source_item(
            source_rule_id="source.linguistic_review",
            requested_review_type="용어 제안",
            current_value="Use Of Archives",
        )
        decision = normalize_decision(
            {
                "status": "오류 의심",
                "issue_type": "오탈자 검수",
                "expected_value": "Use of Archives",
                "difference": "표현 개선",
                "detail": "영문 제목식 표기를 권장합니다.",
            },
            item,
        )
        self.assertEqual(decision["issue_type"], "용어 제안")
        self.assertEqual(decision["status"], "확인 필요")

    def test_three_review_types_share_one_prompt_context(self) -> None:
        items = [
            self.source_item(
                issue_id=-index,
                source_rule_id="source.linguistic_review",
                candidate_kind=f"kind-{index}",
                requested_review_type=review_type,
                current_value="서울 Seoul",
                location="2행 1열",
            )
            for index, review_type in enumerate(
                ("오탈자 검수", "용어 제안", "번역 검수"),
                start=1,
            )
        ]
        payload = compact_prompt_items(items)
        self.assertEqual(len(payload), 1)
        self.assertEqual(len(payload[0]["review_requests"]), 3)
        self.assertEqual(
            {request["requested_review_type"] for request in payload[0]["review_requests"]},
            {"오탈자 검수", "용어 제안", "번역 검수"},
        )

    def test_exact_official_translation_is_resolved_without_llm(self) -> None:
        item = self.source_item(
            source_rule_id="source.linguistic_review",
            candidate_kind="translation_pair",
            requested_review_type="번역 검수",
            current_value="서울 Seoul",
            korean_text="서울",
            english_text="Seoul",
            glossary_matches=[
                {
                    "source": "서울",
                    "target": "Seoul",
                    "status": "official_verified",
                    "source_title": "국토지리정보원 행정구역명",
                    "aliases": [],
                }
            ],
        )
        decision = glossary_decision_for(item)
        self.assertIsNotNone(decision)
        self.assertEqual(decision["status"], "정상")  # type: ignore[index]
        self.assertEqual(decision["expected_value"], "서울 Seoul")  # type: ignore[index]

    def test_official_name_only_does_not_skip_english_translation(self) -> None:
        item = self.source_item(
            source_rule_id="source.linguistic_review",
            candidate_kind="translation_missing",
            requested_review_type="번역 검수",
            current_value="한국통계정보원",
            korean_text="한국통계정보원",
            glossary_matches=[
                {
                    "source": "한국통계정보원",
                    "target": "",
                    "status": "official_name_only",
                    "source_title": "JOB-ALIO",
                    "aliases": [],
                }
            ],
        )
        self.assertIsNone(glossary_decision_for(item))

    def test_cache_fingerprint_includes_model_and_prompt_version(self) -> None:
        item = self.source_item(
            source_rule_id="source.linguistic_review",
            requested_review_type="오탈자 검수",
            prompt_version="prompt-v1",
        )
        first = linguistic_review_fingerprint(item, reviewed_model="model-a")
        second = linguistic_review_fingerprint(item, reviewed_model="model-b")
        third = linguistic_review_fingerprint(
            self.source_item(
                source_rule_id="source.linguistic_review",
                requested_review_type="오탈자 검수",
                prompt_version="prompt-v2",
            ),
            reviewed_model="model-a",
        )
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, third)

    def test_blue_review_outputs_direct_result_without_owner_placeholder(self) -> None:
        item = self.source_item(current_value="지식재산처", cell_text="지식재산처")
        decision = normalize_decision(
            {
                "status": "확인 필요",
                "issue_type": "번역 검수",
                "current_value": "지식재산처",
                "expected_value": "Ministry of Intellectual Property",
                "difference": "영문 번역 제안",
                "detail": "담당자 확인이 필요합니다",
            },
            item,
        )
        self.assertEqual(decision["issue_type"], "파란색 표기 확인")
        self.assertEqual(decision["expected_value"], "Ministry of Intellectual Property")
        self.assertNotIn("담당자", decision["detail"])

    def test_same_context_reuses_saved_llm_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "cache.sqlite"
            with connect(db_path) as connection:
                init_db(connection)
                report_id = int(
                    connection.execute(
                        """
                        INSERT INTO annual_reports (
                            year, title, source_file_name, source_file_path, file_hash, imported_at
                        ) VALUES (2026, '연보', 'test.hwpx', 'test.hwpx', 'cache-test', CURRENT_TIMESTAMP)
                        """
                    ).lastrowid
                )
                table_id = int(
                    connection.execute(
                        """
                        INSERT INTO stat_tables (report_id, code, title, title_en)
                        VALUES (?, '1-1', '지역', 'Region')
                        """,
                        (report_id,),
                    ).lastrowid
                )
                connection.execute(
                    """
                    INSERT INTO stat_table_cells (
                        table_id, row_index, col_index, text_value, is_header
                    ) VALUES (?, 0, 0, '서울 Seoul', 1)
                    """,
                    (table_id,),
                )
                run_one = self.insert_run(connection, report_id)
                self.insert_candidate(connection, run_one, table_id)
                first_item = load_linguistic_review_items(connection, run_one)[0]
                save_llm_review_decisions(
                    connection,
                    run_one,
                    [first_item],
                    [
                        {
                            "id": first_item.issue_id,
                            "status": "정상",
                            "issue_type": "오탈자 검수",
                            "current_value": "서울 Seoul",
                            "expected_value": "서울 Seoul",
                            "difference": "오탈자 없음",
                            "detail": "국문과 영문 철자를 검토했습니다.",
                        }
                    ],
                    reviewed_model="model-a",
                )

                run_two = self.insert_run(connection, report_id)
                candidate_id = self.insert_candidate(connection, run_two, table_id)
                counts = reuse_cached_linguistic_reviews(
                    connection,
                    run_two,
                    reviewed_model="model-a",
                )
                row = connection.execute(
                    """
                    SELECT status, resolution_source
                    FROM linguistic_review_candidates
                    WHERE id = ?
                    """,
                    (candidate_id,),
                ).fetchone()
                self.assertEqual(counts, (0, 1, 1))
                self.assertEqual(row["status"], "reviewed")
                self.assertEqual(row["resolution_source"], "cache")

    def test_blue_korean_text_queues_translation_and_bilingual_text_queues_three_reviews(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "blue.sqlite"
            with connect(db_path) as connection:
                init_db(connection)
                report_id = int(
                    connection.execute(
                        """
                        INSERT INTO annual_reports (
                            year, title, source_file_name, source_file_path, file_hash, imported_at
                        ) VALUES (2026, '연보', 'blue.hwpx', 'blue.hwpx', 'blue-test', CURRENT_TIMESTAMP)
                        """
                    ).lastrowid
                )
                table_id = int(
                    connection.execute(
                        """
                        INSERT INTO stat_tables (report_id, code, title)
                        VALUES (?, '1-1', '파란색 검수')
                        """,
                        (report_id,),
                    ).lastrowid
                )
                run_id = self.insert_run(connection, report_id)
                insert_blue_review(
                    connection,
                    run_id=run_id,
                    table_id=table_id,
                    location="1행 1열",
                    row_index=0,
                    col_index=0,
                    current_value="지식재산처",
                )
                insert_blue_review(
                    connection,
                    run_id=run_id,
                    table_id=table_id,
                    location="2행 1열",
                    row_index=1,
                    col_index=0,
                    current_value="서울 Seoul",
                )
                rows = connection.execute(
                    """
                    SELECT location, review_type, candidate_kind
                    FROM linguistic_review_candidates
                    WHERE run_id = ?
                    ORDER BY location, review_type
                    """,
                    (run_id,),
                ).fetchall()
                placeholder_count = connection.execute(
                    """
                    SELECT COUNT(*) AS item_count
                    FROM validation_issues
                    WHERE run_id = ? AND expected_value = '담당자 확인'
                    """,
                    (run_id,),
                ).fetchone()["item_count"]

                korean_rows = [row for row in rows if row["location"] == "1행 1열"]
                bilingual_rows = [row for row in rows if row["location"] == "2행 1열"]
                self.assertEqual([row["review_type"] for row in korean_rows], ["번역 검수"])
                self.assertEqual(
                    {row["review_type"] for row in bilingual_rows},
                    {"오탈자 검수", "용어 제안", "번역 검수"},
                )
                self.assertTrue(all(str(row["candidate_kind"]).startswith("blue_text") for row in rows))
                self.assertEqual(placeholder_count, 0)

    @staticmethod
    def insert_run(connection, report_id: int) -> int:
        return int(
            connection.execute(
                """
                INSERT INTO validation_runs (
                    report_id, rules_version, started_at, completed_at
                ) VALUES (?, 'test', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (report_id,),
            ).lastrowid
        )

    @staticmethod
    def insert_candidate(connection, run_id: int, table_id: int) -> int:
        return int(
            connection.execute(
                """
                INSERT INTO linguistic_review_candidates (
                    run_id, table_id, review_type, candidate_kind, location,
                    row_index, col_index, current_value, korean_text,
                    english_text, prompt_version
                ) VALUES (?, ?, '오탈자 검수', 'language_spelling', '1행 1열',
                          0, 0, '서울 Seoul', '서울', 'Seoul',
                          'language-review-v3-dictionary-cache')
                """,
                (run_id, table_id),
            ).lastrowid
        )


if __name__ == "__main__":
    unittest.main()
