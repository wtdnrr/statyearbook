from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from app.db.connection import connect
from app.db.schema import init_db
from app.validation.linguistic_policy import SPELLING_REPLACEMENTS
from app.validation.linguistic_review import (
    clear_linguistic_review_run,
    extract_subtable_caption,
    mixed_bilingual_context,
    prepare_linguistic_reviews,
)
from app.validation.translation_glossary import (
    GlossaryEntry,
    extract_bilingual_pair,
    glossary_entries_for_source,
    infer_subcategory,
    preferred_glossary_entry,
)


class LinguisticReviewTest(unittest.TestCase):
    def test_clear_korean_orthography_errors_remain_spelling_checks(self) -> None:
        replacements = {
            item["current"]: item["expected"]
            for item in SPELLING_REPLACEMENTS
        }
        self.assertEqual(replacements["잔액율"], "잔액률")
        self.assertEqual(replacements["진행율"], "진행률")

    def test_bilingual_pair_requires_one_unambiguous_korean_english_pair(self) -> None:
        self.assertEqual(extract_bilingual_pair("서울\nSeoul"), ("서울", "Seoul"))
        self.assertEqual(extract_bilingual_pair("지식재산처 Ministry of Intellectual Property"), ("지식재산처", "Ministry of Intellectual Property"))
        self.assertEqual(extract_bilingual_pair("계(A+B) Total"), ("계(A+B)", "Total"))
        self.assertIsNone(extract_bilingual_pair("구분 Classification 연도 Year"))

    def test_unit_symbols_do_not_create_translation_pairs(self) -> None:
        self.assertIsNone(
            mixed_bilingual_context("농작물 피해면적 3,419ha, 피해액 12,500백만원")
        )
        self.assertEqual(
            mixed_bilingual_context("지역 Region 합계 Total"),
            ("지역 / 합계", "Region / Total"),
        )

    def test_subtable_caption_is_reviewed_as_its_own_title(self) -> None:
        raw_context = "▫ 공용차량 정수 Government Vehicles (2025. 12. 31. 기준) 구분\nClassification"
        self.assertEqual(
            extract_subtable_caption(raw_context),
            "공용차량 정수 Government Vehicles",
        )

    def test_language_reviews_use_minimal_required_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "review.sqlite"
            with connect(db_path) as connection:
                init_db(connection)
                old_report_id = self.insert_report(connection, 2025, "old")
                old_table_id = self.insert_table(
                    connection,
                    old_report_id,
                    "1-1",
                    "지식재산처",
                    "Ministry of Intellectual Property",
                )
                connection.execute(
                    """
                    INSERT INTO stat_table_cells (
                        table_id, row_index, col_index, text_value, is_header
                    ) VALUES (?, 0, 0, '지식재산처 Ministry of Intellectual Property', 1)
                    """,
                    (old_table_id,),
                )

                current_report_id = self.insert_report(connection, 2026, "current")
                current_table_id = self.insert_table(
                    connection,
                    current_report_id,
                    "1-1",
                    "지식재산처",
                    "Intellectual Property Office",
                )
                connection.execute(
                    """
                    UPDATE stat_tables
                    SET domain = '행정', unit = '건', base_date = '2025. 12. 31.',
                        note = '주석 문장', source = '담당 부서 주무관 (044-000-0000)'
                    WHERE id = ?
                    """,
                    (current_table_id,),
                )
                connection.execute(
                    """
                    INSERT INTO stat_table_cells (
                        table_id, row_index, col_index, text_value, is_header
                    ) VALUES (?, 0, 0, '지식재산처 Intellectual Property Office', 1)
                    """,
                    (current_table_id,),
                )
                connection.execute(
                    """
                    INSERT INTO stat_table_cells (
                        table_id, row_index, col_index, text_value, numeric_value, is_header
                    ) VALUES (?, 2, 0, '2026년 3,445명', 3445, 0)
                    """,
                    (current_table_id,),
                )
                connection.execute(
                    """
                    INSERT INTO stat_table_cells (
                        table_id, row_index, col_index, text_value, is_header
                    ) VALUES (?, 4, 0, '구분 Classification 연도 Year', 1)
                    """,
                    (current_table_id,),
                )
                long_value = " ".join(["행정안전 통계연보 검수 대상 문장"] * 90)
                connection.execute(
                    """
                    INSERT INTO stat_table_cells (
                        table_id, row_index, col_index, text_value, is_header
                    ) VALUES (?, 3, 0, ?, 0)
                    """,
                    (current_table_id, long_value),
                )
                connection.execute(
                    """
                    INSERT INTO stat_table_cells (
                        table_id, row_index, col_index, text_value, numeric_value, is_header
                    ) VALUES (?, 1, 0, '2026년 3,445명', 3445, 0)
                    """,
                    (current_table_id,),
                )
                run_id = int(
                    connection.execute(
                        """
                        INSERT INTO validation_runs (
                            report_id, rules_version, started_at, completed_at
                        ) VALUES (?, 'test', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        """,
                        (current_report_id,),
                    ).lastrowid
                )
                connection.commit()

                summary = prepare_linguistic_reviews(
                    connection,
                    report_id=current_report_id,
                    run_id=run_id,
                )
                self.assertGreater(summary.candidates, 0)
                self.assertEqual(summary.glossary_issues, 0)

                candidate_types = {
                    row["review_type"]
                    for row in connection.execute(
                        """
                        SELECT DISTINCT review_type
                        FROM linguistic_review_candidates
                        WHERE run_id = ?
                        """,
                        (run_id,),
                    ).fetchall()
                }
                self.assertIn("오탈자 검수", candidate_types)
                self.assertIn("번역 검수", candidate_types)
                self.assertNotIn("용어 제안", candidate_types)

                mixed_numeric_candidates = connection.execute(
                    """
                    SELECT COUNT(*) AS candidate_count
                    FROM linguistic_review_candidates
                    WHERE run_id = ? AND row_index = 1 AND col_index = 0
                    """,
                    (run_id,),
                ).fetchone()
                self.assertEqual(mixed_numeric_candidates["candidate_count"], 1)

                repeated_location_candidates = connection.execute(
                    """
                    SELECT COUNT(*) AS candidate_count
                    FROM linguistic_review_candidates
                    WHERE run_id = ? AND row_index = 2 AND col_index = 0
                    """,
                    (run_id,),
                ).fetchone()
                self.assertEqual(repeated_location_candidates["candidate_count"], 1)

                long_value_candidates = connection.execute(
                    """
                    SELECT COUNT(*) AS candidate_count
                    FROM linguistic_review_candidates
                    WHERE run_id = ? AND row_index = 3 AND col_index = 0
                    """,
                    (run_id,),
                ).fetchone()
                self.assertGreaterEqual(long_value_candidates["candidate_count"], 2)

                title_candidates = connection.execute(
                    """
                    SELECT COUNT(*) AS candidate_count
                    FROM linguistic_review_candidates
                    WHERE run_id = ? AND location = '표 제목'
                    """,
                    (run_id,),
                ).fetchone()
                self.assertEqual(title_candidates["candidate_count"], 2)

                multi_pair_candidates = connection.execute(
                    """
                    SELECT COUNT(*) AS candidate_count
                    FROM linguistic_review_candidates
                    WHERE run_id = ? AND row_index = 4 AND col_index = 0
                    """,
                    (run_id,),
                ).fetchone()
                self.assertEqual(multi_pair_candidates["candidate_count"], 2)

                metadata_candidates = connection.execute(
                    """
                    SELECT COUNT(*) AS candidate_count
                    FROM linguistic_review_candidates
                    WHERE run_id = ?
                      AND location IN ('분야', '단위', '기준일', '주석', '출처')
                    """,
                    (run_id,),
                ).fetchone()
                self.assertEqual(metadata_candidates["candidate_count"], 0)

                entries = glossary_entries_for_source(
                    connection,
                    "지식재산처",
                    current_report_id=current_report_id,
                )
                self.assertEqual(entries[0].status, "official_verified")
                self.assertEqual(entries[0].target_text, "Ministry of Intellectual Property")
                self.assertTrue(entries[0].source_url.startswith("https://law.go.kr/"))

                public_institution_entries = glossary_entries_for_source(
                    connection,
                    "한국통계정보원",
                    current_report_id=current_report_id,
                )
                self.assertEqual(public_institution_entries[0].status, "official_name_only")
                self.assertEqual(public_institution_entries[0].subcategory, "공공기관")
                self.assertEqual(public_institution_entries[0].target_text, "")

    def test_language_cleanup_preserves_blue_review_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "review.sqlite"
            with connect(db_path) as connection:
                init_db(connection)
                report_id = self.insert_report(connection, 2026, "current")
                table_id = self.insert_table(connection, report_id, "1-1", "제목", "Title")
                run_id = int(
                    connection.execute(
                        """
                        INSERT INTO validation_runs (
                            report_id, rules_version, started_at, completed_at
                        ) VALUES (?, 'test', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        """,
                        (report_id,),
                    ).lastrowid
                )
                for review_type, candidate_kind in (
                    ("오탈자 검수", "language_spelling"),
                    ("번역 검수", "translation_pair"),
                    ("파란색 표기 확인", "blue_text_bilingual"),
                ):
                    connection.execute(
                        """
                        INSERT INTO linguistic_review_candidates (
                            run_id, table_id, review_type, candidate_kind, location,
                            current_value, status, prompt_version
                        ) VALUES (?, ?, ?, ?, '1행 1열', ?, 'reviewed', ?)
                        """,
                        (
                            run_id,
                            table_id,
                            review_type,
                            candidate_kind,
                            review_type,
                            "blue-review-v1-combined"
                            if candidate_kind.startswith("blue_text")
                            else "language-review-old",
                        ),
                    )
                for rule_id in (
                    "llm.spelling_review",
                    "llm.translation_review",
                    "source.blue_text_review",
                ):
                    connection.execute(
                        """
                        INSERT INTO validation_checks (
                            run_id, table_id, rule_id, check_type, check_label, status, severity,
                            location, current_value, expected_value, difference, detail
                        ) VALUES (?, ?, ?, '언어', '언어 검수', '정상', 'info', '1행 1열', ?, ?, '', '')
                        """,
                        (run_id, table_id, rule_id, rule_id, rule_id),
                    )
                connection.commit()

                clear_linguistic_review_run(
                    connection,
                    report_id=report_id,
                    run_id=run_id,
                )

                remaining_candidates = connection.execute(
                    "SELECT review_type FROM linguistic_review_candidates WHERE run_id = ?",
                    (run_id,),
                ).fetchall()
                self.assertEqual(
                    [row["review_type"] for row in remaining_candidates],
                    ["파란색 표기 확인"],
                )
                remaining_checks = connection.execute(
                    "SELECT rule_id FROM validation_checks WHERE run_id = ?",
                    (run_id,),
                ).fetchall()
                self.assertEqual(
                    [row["rule_id"] for row in remaining_checks],
                    ["source.blue_text_review"],
                )

    def test_conflicting_generic_reference_terms_are_context_only(self) -> None:
        entries = [
            self.glossary_entry(1, "Commission"),
            self.glossary_entry(2, "Committees"),
        ]
        self.assertIsNone(preferred_glossary_entry(entries))

    def test_glossary_subcategories_cover_required_entity_groups(self) -> None:
        self.assertEqual(infer_subcategory("서울특별시", "Seoul Special City"), "시도")
        self.assertEqual(infer_subcategory("종로구", "Jongno-gu", "행정구역명"), "시군구")
        self.assertEqual(infer_subcategory("한솔동", "Hansol-dong", "행정구역명"), "읍면동")
        self.assertEqual(infer_subcategory("공정거래위원회", "Korea Fair Trade Commission"), "위원회")
        self.assertEqual(infer_subcategory("한국통계정보원", "Korea Statistical Information Institute"), "공공기관")
        self.assertEqual(infer_subcategory("증감률", "Rate of Change"), "증감률")

    @staticmethod
    def glossary_entry(entry_id: int, target: str) -> GlossaryEntry:
        return GlossaryEntry(
            id=entry_id,
            source_text="위원회",
            target_text=target,
            category="일반 용어",
            subcategory="",
            source_kind="yearbook",
            status="reference",
            priority=2025,
            source_report_id=1,
            source_year=2025,
            occurrence_count=2,
            evidence="표",
            source_url="",
            source_title="",
            aliases=(),
            valid_from="",
            valid_to="",
            verified_at="",
        )

    @staticmethod
    def insert_report(connection, year: int, suffix: str) -> int:
        return int(
            connection.execute(
                """
                INSERT INTO annual_reports (
                    year, title, source_file_name, source_file_path, file_hash, imported_at
                ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (year, f"{year} 연보", f"{suffix}.md", f"/{suffix}.md", f"hash-{suffix}"),
            ).lastrowid
        )

    @staticmethod
    def insert_table(connection, report_id: int, code: str, title: str, title_en: str) -> int:
        return int(
            connection.execute(
                """
                INSERT INTO stat_tables (
                    report_id, code, title, title_en, table_order
                ) VALUES (?, ?, ?, ?, 1)
                """,
                (report_id, code, title, title_en),
            ).lastrowid
        )


if __name__ == "__main__":
    unittest.main()
