from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from app.db.schema import connect, init_db
from app.validation.linguistic_review import extract_subtable_caption, prepare_linguistic_reviews
from app.validation.translation_glossary import (
    GlossaryEntry,
    extract_bilingual_pair,
    glossary_entries_for_source,
    infer_subcategory,
    preferred_glossary_entry,
)


class LinguisticReviewTest(unittest.TestCase):
    def test_bilingual_pair_requires_one_unambiguous_korean_english_pair(self) -> None:
        self.assertEqual(extract_bilingual_pair("서울\nSeoul"), ("서울", "Seoul"))
        self.assertEqual(extract_bilingual_pair("지식재산처 Ministry of Intellectual Property"), ("지식재산처", "Ministry of Intellectual Property"))
        self.assertEqual(extract_bilingual_pair("계(A+B) Total"), ("계(A+B)", "Total"))
        self.assertIsNone(extract_bilingual_pair("구분 Classification 연도 Year"))

    def test_subtable_caption_is_reviewed_as_its_own_title(self) -> None:
        raw_context = "▫ 공용차량 정수 Government Vehicles (2025. 12. 31. 기준) 구분\nClassification"
        self.assertEqual(
            extract_subtable_caption(raw_context),
            "공용차량 정수 Government Vehicles",
        )

    def test_official_glossary_is_context_but_all_three_reviews_still_run(self) -> None:
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
                self.assertIn("용어 제안", candidate_types)
                self.assertIn("번역 검수", candidate_types)

                mixed_numeric_candidates = connection.execute(
                    """
                    SELECT COUNT(*) AS candidate_count
                    FROM linguistic_review_candidates
                    WHERE run_id = ? AND row_index = 1 AND col_index = 0
                    """,
                    (run_id,),
                ).fetchone()
                self.assertEqual(mixed_numeric_candidates["candidate_count"], 3)

                repeated_location_candidates = connection.execute(
                    """
                    SELECT COUNT(*) AS candidate_count
                    FROM linguistic_review_candidates
                    WHERE run_id = ? AND row_index = 2 AND col_index = 0
                    """,
                    (run_id,),
                ).fetchone()
                self.assertEqual(repeated_location_candidates["candidate_count"], 3)

                long_value_candidates = connection.execute(
                    """
                    SELECT COUNT(*) AS candidate_count
                    FROM linguistic_review_candidates
                    WHERE run_id = ? AND row_index = 3 AND col_index = 0
                    """,
                    (run_id,),
                ).fetchone()
                self.assertGreaterEqual(long_value_candidates["candidate_count"], 6)

                title_candidates = connection.execute(
                    """
                    SELECT COUNT(*) AS candidate_count
                    FROM linguistic_review_candidates
                    WHERE run_id = ? AND location = '표 제목'
                    """,
                    (run_id,),
                ).fetchone()
                self.assertEqual(title_candidates["candidate_count"], 3)

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
