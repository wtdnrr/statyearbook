from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from app.db.schema import connect, init_db, repair_misaligned_linguistic_reviews
from app.validation.blue_review import insert_blue_review
from app.validation.linguistic_review import prepare_linguistic_reviews
from app.validation.llm_translation_review import (
    ResponsesAPIClient,
    SourceReviewItem,
    compact_prompt_items,
    chunked_by_context,
    decision_current_matches_item,
    group_linguistic_items_by_context,
    glossary_decision_for,
    expected_is_existing_bilingual_text,
    expected_drops_bilingual_context,
    linguistic_review_fingerprint,
    load_linguistic_review_items,
    normalize_decision,
    parse_response_json,
    request_review_with_retries,
    retry_after_seconds,
    translate_temporal_notation,
    review_batch_with_retries,
    looks_like_translation_mismatch,
    review_decision_needs_retry,
    reuse_cached_linguistic_reviews,
    reconcile_non_actionable_bilingual_reviews,
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

    def test_gpt5_review_uses_minimal_reasoning(self) -> None:
        client = ResponsesAPIClient(
            api_key="test-key",
            model="openai/gpt-5-nano",
            base_url="https://example.invalid/v1",
            timeout=10,
            provider="bizrouter",
        )
        captured: dict[str, object] = {}

        def fake_post(body: dict[str, object]) -> dict[str, object]:
            captured.update(body)
            return {"output_text": '{"items": []}'}

        client._post = fake_post  # type: ignore[method-assign]
        client.review([self.source_item()])

        self.assertEqual(captured["reasoning"], {"effort": "minimal"})

    def test_empty_structured_output_reports_response_state(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "status=incomplete"):
            parse_response_json(
                {
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "usage": {"output_tokens": 1800},
                    "output": [],
                }
            )

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

    def test_response_using_another_candidate_value_is_retried_and_not_trusted(self) -> None:
        item = self.source_item(
            source_rule_id="source.linguistic_review",
            candidate_kind="translation_pair",
            requested_review_type="번역 검수",
            location="표 제목",
            current_value="연도별 공무원 정원 Fixed Number of Civil Servants by Year",
        )
        raw = {
            "status": "확인 필요",
            "issue_type": "번역 검수",
            "current_value": "구분",
            "expected_value": "Classification",
            "difference": "번역 확인",
            "detail": "다른 후보의 결과가 섞였습니다.",
        }

        self.assertFalse(decision_current_matches_item(raw["current_value"], item.current_value))
        self.assertTrue(review_decision_needs_retry(raw, item))
        self.assertEqual(normalize_decision(raw, item)["current_value"], item.current_value)

    def test_misaligned_stored_results_are_reset_without_touching_other_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "misaligned.sqlite"
            with connect(db_path) as connection:
                init_db(connection)
                report_id = int(
                    connection.execute(
                        """
                        INSERT INTO annual_reports (
                            year, title, source_file_name, source_file_path, file_hash, imported_at
                        ) VALUES (2026, '연보', 'test.hwpx', 'test.hwpx', 'misaligned', CURRENT_TIMESTAMP)
                        """
                    ).lastrowid
                )
                table_id = int(
                    connection.execute(
                        "INSERT INTO stat_tables (report_id, code, title) VALUES (?, '1-2-1-2', '공무원 정원')",
                        (report_id,),
                    ).lastrowid
                )
                run_id = self.insert_run(connection, report_id)
                candidates = (
                    (
                        "오탈자 검수",
                        "language_spelling",
                        "연도별 공무원 정원 Fixed Number of Civil Servants by Year",
                        "구분",
                    ),
                    (
                        "용어 제안",
                        "language_terminology",
                        "연도별 공무원 정원 Fixed Number of Civil Servants by Year",
                        "연도별 공무원 정원 Fixed Number of Civil Servants by Year",
                    ),
                )
                for review_type, candidate_kind, current_value, result_current in candidates:
                    connection.execute(
                        """
                        INSERT INTO linguistic_review_candidates (
                            run_id, table_id, review_type, candidate_kind, location,
                            current_value, status, resolution_source, review_result_json
                        ) VALUES (?, ?, ?, ?, '표 제목', ?, 'reviewed', 'llm', ?)
                        """,
                        (
                            run_id,
                            table_id,
                            review_type,
                            candidate_kind,
                            current_value,
                            json.dumps(
                                {
                                    "current_value": result_current,
                                    "expected_value": "Classification",
                                    "status": "확인 필요",
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    )

                for rule_id, check_type in (
                    ("llm.spelling_review", "오탈자 검수"),
                    ("llm.terminology_review", "용어 제안"),
                ):
                    connection.execute(
                        """
                        INSERT INTO validation_checks (
                            run_id, table_id, rule_id, check_type, check_label, location,
                            current_value, expected_value, status, severity, detail
                        ) VALUES (?, ?, ?, ?, ?, '표 제목', '구분', 'Classification',
                                  '확인 필요', 'warning', '잘못 연결된 결과')
                        """,
                        (run_id, table_id, rule_id, check_type, check_type),
                    )
                    connection.execute(
                        """
                        INSERT INTO validation_issues (
                            run_id, table_id, rule_id, issue_type, location,
                            current_value, expected_value, status, severity, detail
                        ) VALUES (?, ?, ?, ?, '표 제목', '구분', 'Classification',
                                  '확인 필요', 'warning', '잘못 연결된 결과')
                        """,
                        (run_id, table_id, rule_id, check_type),
                    )
                connection.execute(
                    """
                    INSERT INTO validation_issues (
                        run_id, table_id, rule_id, issue_type, location,
                        current_value, expected_value, status, severity, detail
                    ) VALUES (?, ?, 'metadata.required', '메타정보 검수', '출처·메타정보',
                              '단위: 없음', '단위 입력', '확인 필요', 'warning', '단위 누락')
                    """,
                    (run_id, table_id),
                )

                repair_misaligned_linguistic_reviews(connection)

                candidate_rows = connection.execute(
                    "SELECT status, resolution_source FROM linguistic_review_candidates ORDER BY id"
                ).fetchall()
                linguistic_issue_count = connection.execute(
                    "SELECT COUNT(*) FROM validation_issues WHERE rule_id LIKE 'llm.%'"
                ).fetchone()[0]
                metadata_issue_count = connection.execute(
                    "SELECT COUNT(*) FROM validation_issues WHERE issue_type = '메타정보 검수'"
                ).fetchone()[0]

                self.assertEqual(
                    [(row["status"], row["resolution_source"]) for row in candidate_rows],
                    [("pending", ""), ("pending", "")],
                )
                self.assertEqual(linguistic_issue_count, 0)
                self.assertEqual(metadata_issue_count, 1)

    def test_english_only_spelling_replacement_cannot_drop_korean_source(self) -> None:
        item = self.source_item(
            requested_review_type="오탈자 검수",
            current_value="정부조직 Government Organiztion",
        )
        raw = {
            "status": "오류 의심",
            "issue_type": "오탈자 검수",
            "expected_value": "Government Organization",
            "difference": "영문 철자 오류",
            "detail": "영문 철자를 교정했습니다.",
        }

        self.assertTrue(review_decision_needs_retry(raw, item))

    def test_spelling_review_cannot_translate_a_korean_name(self) -> None:
        item = self.source_item(
            source_rule_id="source.linguistic_review",
            candidate_kind="language_spelling",
            requested_review_type="오탈자 검수",
            current_value="정선용",
        )
        raw = {
            "status": "오류 의심",
            "issue_type": "오탈자 검수",
            "current_value": "정선용",
            "expected_value": "Jeong Seon-yong",
            "difference": "국문·영문 표기 교정 제안",
            "detail": "이름을 영문으로 표기했습니다.",
        }

        self.assertTrue(review_decision_needs_retry(raw, item))

    def test_bilingual_translation_cannot_use_an_unrelated_english_only_value(self) -> None:
        item = self.source_item(
            source_rule_id="source.linguistic_review",
            candidate_kind="translation_pair",
            requested_review_type="번역 검수",
            current_value="주택 House",
            korean_text="주택",
            english_text="House",
        )
        raw = {
            "status": "확인 필요",
            "issue_type": "번역 검수",
            "current_value": "주택 House",
            "expected_value": "Disaster Insurance Division",
            "difference": "국문과 영문 번역 표현 검토",
            "detail": "번역을 검토했습니다.",
        }

        self.assertTrue(review_decision_needs_retry(raw, item))
        raw["expected_value"] = "주택 Dwelling"
        self.assertFalse(review_decision_needs_retry(raw, item))

    def test_existing_bilingual_text_is_not_treated_as_duplicate_error(self) -> None:
        current = "정부조직 변천 The Change in the Number of Government Organizations by Year"
        item = self.source_item(
            requested_review_type="오탈자 검수",
            current_value=current,
        )
        raw = {
            "status": "확인 필요",
            "issue_type": "오탈자 검수",
            "expected_value": "The Change in the Number of Government Organizations by Year",
            "difference": "한영 중복 표기",
            "detail": "한글과 영문이 함께 표기되어 있습니다.",
        }

        self.assertTrue(expected_is_existing_bilingual_text(current, raw["expected_value"]))
        decision = normalize_decision(raw, item)
        self.assertEqual(decision["status"], "정상")
        self.assertEqual(decision["expected_value"], current)

    def test_english_only_replacement_cannot_discard_bilingual_source(self) -> None:
        current = "국방부 Ministry of National Defence"
        item = self.source_item(
            source_rule_id="source.linguistic_review",
            candidate_kind="translation_pair",
            requested_review_type="번역 검수",
            current_value=current,
        )
        raw = {
            "status": "확인 필요",
            "issue_type": "번역 검수",
            "expected_value": "Ministry of National Defense",
            "difference": "승인 사전 영문명 적용",
            "detail": "공식 사전 표기를 적용했습니다.",
        }

        self.assertTrue(expected_drops_bilingual_context(current, raw["expected_value"]))
        decision = normalize_decision(raw, item)
        self.assertEqual(decision["status"], "정상")
        self.assertEqual(decision["expected_value"], current)

    def test_complete_bilingual_correction_remains_actionable(self) -> None:
        current = "지방자치단체 Local Goverments"
        expected = "지방자치단체 Local Governments"
        item = self.source_item(
            source_rule_id="source.linguistic_review",
            candidate_kind="translation_pair",
            requested_review_type="오탈자 검수",
            current_value=current,
        )

        decision = normalize_decision(
            {
                "status": "오류 의심",
                "issue_type": "오탈자 검수",
                "expected_value": expected,
                "difference": "영문 철자 교정",
                "detail": "영문 철자를 교정했습니다.",
            },
            item,
        )

        self.assertFalse(expected_drops_bilingual_context(current, expected))
        self.assertEqual(decision["status"], "오류 의심")
        self.assertEqual(decision["expected_value"], expected)

    def test_generated_translation_requires_review(self) -> None:
        item = self.source_item(
            source_rule_id="source.linguistic_review",
            candidate_kind="korean_only",
            requested_review_type="번역 검수",
            current_value="조직",
        )
        decision = normalize_decision(
            {
                "status": "정상",
                "issue_type": "번역 검수",
                "expected_value": "Organization",
                "difference": "No issue",
                "detail": "Translation generated from context.",
            },
            item,
        )

        self.assertEqual(decision["status"], "확인 필요")
        self.assertEqual(decision["expected_value"], "Organization")
        self.assertRegex(decision["detail"], "[가-힣]")

    def test_incomplete_batch_is_retried_by_cell_context(self) -> None:
        first = self.source_item(
            issue_id=1,
            source_rule_id="source.linguistic_review",
            candidate_kind="korean_only",
            requested_review_type="오탈자 검수",
            location="1행 1열",
            current_value="정부조직",
        )
        second = self.source_item(
            issue_id=2,
            source_rule_id="source.linguistic_review",
            candidate_kind="korean_only",
            requested_review_type="오탈자 검수",
            location="2행 1열",
            current_value="지방조직",
        )

        class RetryClient:
            def __init__(self) -> None:
                self.calls = 0

            def review(
                self,
                items: list[SourceReviewItem],
                *,
                require_english_replacement: bool = False,
            ) -> list[dict[str, object]]:
                self.calls += 1
                if self.calls <= 3:
                    return []
                return [
                    {
                        "id": item.issue_id,
                        "status": "정상",
                        "issue_type": item.requested_review_type,
                        "current_value": item.current_value,
                        "expected_value": item.current_value,
                        "difference": "정상",
                        "detail": "문맥을 기준으로 검토했습니다.",
                    }
                    for item in items
                ]

        client = RetryClient()
        decisions = review_batch_with_retries(client, [first, second])  # type: ignore[arg-type]

        self.assertEqual({decision["id"] for decision in decisions}, {1, 2})
        self.assertEqual(client.calls, 5)

    def test_malformed_json_response_is_retried(self) -> None:
        item = self.source_item(
            source_rule_id="source.linguistic_review",
            candidate_kind="translation_pair",
            requested_review_type="번역 검수",
        )

        class MalformedClient:
            def __init__(self) -> None:
                self.calls = 0

            def review(
                self,
                items: list[SourceReviewItem],
                *,
                require_english_replacement: bool = False,
            ) -> list[dict[str, object]]:
                self.calls += 1
                if self.calls == 1:
                    raise json.JSONDecodeError("unterminated", "{", 1)
                return [{"id": items[0].issue_id}]

        client = MalformedClient()
        result = request_review_with_retries(client, [item])  # type: ignore[arg-type]

        self.assertEqual(result, [{"id": item.issue_id}])
        self.assertEqual(client.calls, 2)

    def test_bizrouter_retry_after_is_parsed(self) -> None:
        body = '{"error":{"details":{"retry_after":1.5}}}'
        self.assertEqual(retry_after_seconds(body), 1.5)
        self.assertEqual(retry_after_seconds("not-json"), 0.0)

    def test_numeric_year_translation_does_not_require_english_letters(self) -> None:
        item = self.source_item(
            source_rule_id="source.linguistic_review",
            candidate_kind="korean_only",
            requested_review_type="번역 검수",
            current_value="'21년~",
        )
        raw = {
            "status": "확인 필요",
            "issue_type": "번역 검수",
            "expected_value": "'21년~",
            "difference": "연도 표기",
            "detail": "연도 범위 표기를 검토했습니다.",
        }

        self.assertFalse(review_decision_needs_retry(raw, item))
        self.assertEqual(translate_temporal_notation(item.current_value), "'21~")
        decision = normalize_decision(raw, item)
        self.assertEqual(decision["status"], "확인 필요")
        self.assertEqual(decision["expected_value"], "'21~")

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
                "expected_value": "National Data Agency",
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
            requested_review_type="번역 검수",
            current_value="기록물 이용 Use Of Archives",
        )
        decision = normalize_decision(
            {
                "status": "오류 의심",
                "issue_type": "오탈자 검수",
                "expected_value": "기록물 이용 Use of Archives",
                "difference": "번역 표현 확인",
                "detail": "국문과 영문의 의미 대응을 확인했습니다.",
            },
            item,
        )
        self.assertEqual(decision["issue_type"], "번역 검수")
        self.assertEqual(decision["status"], "확인 필요")

    def test_required_review_types_share_one_prompt_context(self) -> None:
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
                ("오탈자 검수", "번역 검수"),
                start=1,
            )
        ]
        payload = compact_prompt_items(items)
        self.assertEqual(len(payload), 1)
        self.assertEqual(len(payload[0]["review_requests"]), 2)
        self.assertEqual(
            {request["requested_review_type"] for request in payload[0]["review_requests"]},
            {"오탈자 검수", "번역 검수"},
        )

    def test_interleaved_review_types_are_regrouped_before_batching(self) -> None:
        items = [
            self.source_item(
                issue_id=-index,
                source_rule_id="source.linguistic_review",
                requested_review_type=review_type,
                current_value=value,
                location=location,
                row_index=row_index,
                col_index=0,
            )
            for index, (review_type, value, location, row_index) in enumerate(
                (
                    ("오탈자 검수", "서울 Seoul", "2행 1열", 1),
                    ("오탈자 검수", "부산 Busan", "3행 1열", 2),
                    ("번역 검수", "서울 Seoul", "2행 1열", 1),
                    ("번역 검수", "부산 Busan", "3행 1열", 2),
                ),
                start=1,
            )
        ]

        grouped = group_linguistic_items_by_context(items)
        batches = chunked_by_context(grouped, 1)

        self.assertEqual(len(batches), 2)
        self.assertEqual([len(batch) for batch in batches], [2, 2])
        self.assertTrue(all(len(compact_prompt_items(batch)) == 1 for batch in batches))

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

    def test_all_approved_alternatives_are_checked_before_suggesting_replacement(self) -> None:
        item = self.source_item(
            source_rule_id="source.linguistic_review",
            candidate_kind="translation_pair",
            requested_review_type="번역 검수",
            current_value="경기 Gyeonggi",
            korean_text="경기",
            english_text="Gyeonggi",
            glossary_matches=[
                {
                    "source": "경기도",
                    "target": "Gyeonggi-do",
                    "status": "official_verified",
                    "source_title": "국토지리정보원 행정구역명",
                    "aliases": ["경기"],
                },
                {
                    "source": "경기",
                    "target": "Gyeonggi",
                    "status": "approved",
                    "source_title": "행정안전통계연보 반복 표기 승인 사전",
                    "aliases": [],
                },
            ],
        )

        decision = glossary_decision_for(item)

        self.assertIsNotNone(decision)
        self.assertEqual(decision["status"], "정상")  # type: ignore[index]

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

    def test_stored_english_only_bilingual_finding_is_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "reconcile.sqlite"
            with connect(db_path) as connection:
                init_db(connection)
                report_id = int(
                    connection.execute(
                        """
                        INSERT INTO annual_reports (
                            year, title, source_file_name, source_file_path, file_hash, imported_at
                        ) VALUES (2026, '연보', 'test.hwpx', 'test.hwpx', 'reconcile-test', CURRENT_TIMESTAMP)
                        """
                    ).lastrowid
                )
                table_id = int(
                    connection.execute(
                        "INSERT INTO stat_tables (report_id, code, title) VALUES (?, '1-1', '기관')",
                        (report_id,),
                    ).lastrowid
                )
                run_id = self.insert_run(connection, report_id)
                current = "국방부 Ministry of National Defence"
                expected = "Ministry of National Defense"
                decision = json.dumps(
                    {
                        "status": "확인 필요",
                        "issue_type": "번역 검수",
                        "rule_id": "llm.translation_review",
                        "current_value": current,
                        "expected_value": expected,
                        "difference": "승인 사전 영문명 적용",
                        "detail": "공식 사전 표기를 적용했습니다.",
                    },
                    ensure_ascii=False,
                )
                connection.execute(
                    """
                    INSERT INTO linguistic_review_candidates (
                        run_id, table_id, review_type, candidate_kind, location,
                        current_value, korean_text, english_text, status, review_result_json
                    ) VALUES (?, ?, '번역 검수', 'translation_pair', '1행 1열',
                              ?, '국방부', 'Ministry of National Defence', 'reviewed', ?)
                    """,
                    (run_id, table_id, current, decision),
                )
                connection.execute(
                    """
                    INSERT INTO validation_checks (
                        run_id, table_id, rule_id, check_type, check_label, location,
                        current_value, expected_value, difference, status, severity, detail
                    ) VALUES (?, ?, 'llm.translation_review', '번역 검수', '번역 검수',
                              '1행 1열', ?, ?, '승인 사전 영문명 적용', '확인 필요',
                              'warning', '공식 사전 표기를 적용했습니다.')
                    """,
                    (run_id, table_id, current, expected),
                )
                connection.execute(
                    """
                    INSERT INTO validation_issues (
                        run_id, table_id, rule_id, issue_type, location,
                        current_value, expected_value, difference, status, severity, detail
                    ) VALUES (?, ?, 'llm.translation_review', '번역 검수', '1행 1열',
                              ?, ?, '승인 사전 영문명 적용', '확인 필요', 'warning',
                              '공식 사전 표기를 적용했습니다.')
                    """,
                    (run_id, table_id, current, expected),
                )

                counts = reconcile_non_actionable_bilingual_reviews(connection, run_id)
                candidate = connection.execute(
                    "SELECT review_result_json FROM linguistic_review_candidates WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                check = connection.execute(
                    "SELECT status, expected_value FROM validation_checks WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                issue_count = connection.execute(
                    "SELECT COUNT(*) FROM validation_issues WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0]

                self.assertEqual(counts, (1, 1, 1))
                self.assertEqual(json.loads(candidate["review_result_json"])["status"], "정상")
                self.assertEqual(check["status"], "정상")
                self.assertEqual(check["expected_value"], current)
                self.assertEqual(issue_count, 0)

    def test_each_blue_value_queues_one_combined_blue_review(self) -> None:
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
                connection.executemany(
                    """
                    INSERT INTO stat_table_cells (
                        table_id, row_index, col_index, text_value, is_header
                    ) VALUES (?, ?, 0, ?, 1)
                    """,
                    (
                        (table_id, 0, "지식재산처"),
                        (table_id, 1, "서울 Seoul"),
                    ),
                )
                prepare_linguistic_reviews(
                    connection,
                    report_id=report_id,
                    run_id=run_id,
                )
                rows = connection.execute(
                    """
                    SELECT location, review_type, candidate_kind
                    FROM linguistic_review_candidates
                    WHERE run_id = ?
                      AND location IN ('1행 1열', '2행 1열')
                      AND candidate_kind LIKE 'blue_text%'
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
                self.assertEqual([row["review_type"] for row in korean_rows], ["파란색 표기 확인"])
                self.assertEqual([row["review_type"] for row in bilingual_rows], ["파란색 표기 확인"])
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
