from pathlib import Path
import json
import tempfile
import unittest
import zipfile

from app.db.schema import connect, init_db
from app.services.sqlite_report_service import (
    build_columns,
    clean_label,
    english_label,
    leading_label_column_indexes,
    remove_unmatched_closing_parentheses,
)
from app.validation.blue_review import (
    BLUE_REVIEW_TYPE,
    classify_blue_review_value,
    extract_blue_review_candidates,
    insert_blue_review,
    row_context_matches,
    should_skip_blue_review,
    synchronize_blue_review_checks,
)
from app.validation.region_glossary import region_review_decision


class BlueReviewExtractionTests(unittest.TestCase):
    def test_appendix_blue_cells_and_english_marker_are_retained(self) -> None:
        header = """
        <header><charPr id="1" textColor="#0000FF"/><charPr id="2" textColor="#000000"/></header>
        """
        section = """
        <sec>
          <tbl><tr><tc><p><run charPrIDRef="2"><t>부록3 행정안전부 소속 위원회</t></run></p></tc></tr></tbl>
          <tbl><tr>
            <tc><cellAddr rowAddr="4" colAddr="0"/><p><run charPrIDRef="1"><t>지방공기업정책위원회</t></run></p></tc>
            <tc><cellAddr rowAddr="4" colAddr="1"/><p><run charPrIDRef="1"><t>15</t></run></p></tc>
          </tr></tbl>
          <tbl><tr><tc><p><run charPrIDRef="2"><t>2-1-4-1 가입자 수</t></run><run charPrIDRef="1"><t>(영문)</t></run></p></tc></tr></tbl>
        </sec>
        """
        with tempfile.TemporaryDirectory() as directory:
            hwpx_path = Path(directory) / "sample.hwpx"
            with zipfile.ZipFile(hwpx_path, "w") as archive:
                archive.writestr("Contents/header.xml", header)
                archive.writestr("Contents/section0.xml", section)

            candidates = extract_blue_review_candidates(hwpx_path)

        appendix_candidates = [candidate for candidate in candidates if candidate.table_code == "부록 3"]
        self.assertEqual(
            [candidate.text for candidate in appendix_candidates],
            ["지방공기업정책위원회", "15"],
        )
        self.assertEqual(appendix_candidates[0].source_row_index, 4)
        self.assertEqual(appendix_candidates[0].source_col_index, 0)
        self.assertTrue(
            any(candidate.table_code == "2-1-4-1" and candidate.text == "가입자 수" for candidate in candidates)
        )

    def test_row_context_prevents_repeated_value_from_matching_other_rows(self) -> None:
        source_row = "지방공기업정책위원회 | 지방공기업법 제78조의5 | 차관 | 15 | 자문 Consultation"
        self.assertTrue(
            row_context_matches(
                source_row,
                source_row,
                "자문 Consultation",
            )
        )
        self.assertFalse(
            row_context_matches(
                "국가기록관리위원회 | 공공기록물 관리에 관한 법률 제15조 | 민간인 | 20 | 자문 Consultation",
                source_row,
                "자문 Consultation",
            )
        )

    def test_blue_candidates_are_exposed_once_even_when_language_review_is_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.sqlite"
            with connect(db_path) as connection:
                init_db(connection)
                report_id = connection.execute(
                    """
                    INSERT INTO annual_reports (
                        year, title, source_file_name, source_file_path, file_hash, imported_at
                    ) VALUES (2026, '연보', 'draft.hwpx', 'draft.hwpx', 'blue-test', '2026-07-15')
                    """
                ).lastrowid
                table_id = connection.execute(
                    """
                    INSERT INTO stat_tables (report_id, code, title, table_order)
                    VALUES (?, '부록 3', '위원회 현황', 1)
                    """,
                    (report_id,),
                ).lastrowid
                run_id = connection.execute(
                    """
                    INSERT INTO validation_runs (
                        report_id, rules_version, started_at, completed_at, issue_count
                    ) VALUES (?, 'test', '2026-07-15', '2026-07-15', 0)
                    """,
                    (report_id,),
                ).lastrowid
                normal = json.dumps(
                    {
                        "status": "정상",
                        "expected_value": "자문 Consultation",
                        "difference": "공식 사전 일치",
                        "detail": "공식 표기와 일치합니다.",
                    },
                    ensure_ascii=False,
                )
                connection.execute(
                    """
                    INSERT INTO linguistic_review_candidates (
                        run_id, table_id, review_type, candidate_kind, location,
                        row_index, col_index, current_value, status, review_result_json
                    ) VALUES (?, ?, ?, 'blue_text_bilingual', '5행 5열', 4, 4, ?, 'reviewed', ?)
                    """,
                    (run_id, table_id, BLUE_REVIEW_TYPE, "자문 Consultation", normal),
                )
                connection.execute(
                    """
                    INSERT INTO linguistic_review_candidates (
                        run_id, table_id, review_type, candidate_kind, location,
                        row_index, col_index, current_value, status
                    ) VALUES (?, ?, ?, 'blue_text_korean_translation',
                              '6행 1열', 5, 0, '지방공기업정책위원회', 'pending')
                    """,
                    (run_id, table_id, BLUE_REVIEW_TYPE),
                )
                connection.commit()

            inserted = synchronize_blue_review_checks(db_path, run_id=int(run_id))

            with connect(db_path) as connection:
                checks = connection.execute(
                    """
                    SELECT location, status, row_index, col_index
                    FROM validation_checks
                    WHERE run_id = ? AND check_type = '파란색 표기 확인'
                    ORDER BY location
                    """,
                    (run_id,),
                ).fetchall()
                issue_count = connection.execute(
                    "SELECT COUNT(*) AS count FROM validation_issues WHERE run_id = ?",
                    (run_id,),
                ).fetchone()["count"]

        self.assertEqual(inserted, 2)
        self.assertEqual(
            [(row["location"], row["status"], row["row_index"], row["col_index"]) for row in checks],
            [
                ("5행 5열", "정상", 4, 4),
                ("6행 1열", "확인 필요", 5, 0),
            ],
        )
        self.assertEqual(issue_count, 1)

    def test_numeric_and_metadata_blue_marks_are_not_candidates(self) -> None:
        self.assertTrue(should_skip_blue_review("4행 2열", "3,456"))
        self.assertTrue(should_skip_blue_review("4행 2열", "12.5%"))
        self.assertTrue(should_skip_blue_review("출처", "안전정책과 주무관 홍길동"))
        self.assertTrue(should_skip_blue_review("주석", "잠정치임"))
        self.assertFalse(should_skip_blue_review("4행 2열", "전자메가폰 Electronic Megaphone"))

    def test_embedded_latin_unit_is_not_mistaken_for_bilingual_translation(self) -> None:
        kind, korean, english = classify_blue_review_value(
            "산림작물 3,419ha 등 복구지원액 1조8,809억원"
        )

        self.assertEqual(kind, "blue_text_korean_translation")
        self.assertEqual(korean, "산림작물 3,419ha 등 복구지원액 1조8,809억원")
        self.assertEqual(english, "")

    def test_insert_blue_review_persists_one_combined_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.sqlite"
            with connect(db_path) as connection:
                init_db(connection)
                report_id = connection.execute(
                    """
                    INSERT INTO annual_reports (
                        year, title, source_file_name, source_file_path, file_hash, imported_at
                    ) VALUES (2026, '연보', 'draft.hwpx', 'draft.hwpx', 'insert-blue', '2026-07-16')
                    """
                ).lastrowid
                table_id = connection.execute(
                    "INSERT INTO stat_tables (report_id, code, title) VALUES (?, '1-1', '표')",
                    (report_id,),
                ).lastrowid
                run_id = connection.execute(
                    """
                    INSERT INTO validation_runs (report_id, rules_version, started_at, completed_at)
                    VALUES (?, 'test', '2026-07-16', '2026-07-16')
                    """,
                    (report_id,),
                ).lastrowid

                self.assertTrue(
                    insert_blue_review(
                        connection,
                        run_id=int(run_id),
                        table_id=int(table_id),
                        location="2행 1열",
                        row_index=1,
                        col_index=0,
                        current_value="서울 Seoul",
                    )
                )
                rows = connection.execute(
                    """
                    SELECT review_type, candidate_kind
                    FROM linguistic_review_candidates
                    WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchall()

        self.assertEqual(
            [(row["review_type"], row["candidate_kind"]) for row in rows],
            [(BLUE_REVIEW_TYPE, "blue_text_bilingual")],
        )

    def test_region_dictionary_corrects_romanization_without_llm(self) -> None:
        current = "인천 남동구, 충남 천안시, 제주 서귀포시 Inchoen Namdong-gu, Chungnam Choenan-si, Jeju Seoguipo-si"
        decision = region_review_decision(current)

        self.assertIsNotNone(decision)
        self.assertEqual(decision.status, "확인 필요")  # type: ignore[union-attr]
        self.assertEqual(
            decision.expected_value,  # type: ignore[union-attr]
            "인천 남동구, 충남 천안시, 제주 서귀포시 Incheon Namdong-gu, Chungnam Cheonan-si, Jeju Seogwipo-si",
        )

    def test_region_dictionary_uses_current_official_province_names(self) -> None:
        current = "강원특별자치도, 전북특별자치도"
        decision = region_review_decision(current)

        self.assertIsNotNone(decision)
        self.assertEqual(
            decision.expected_value,  # type: ignore[union-attr]
            "Gangwon State, Jeonbuk State",
        )

    def test_region_dictionary_handles_eup_myeon_dong_and_invalid_suffix(self) -> None:
        idong = region_review_decision("경기 포천시 이동면")
        grouped = region_review_decision(
            "경기 가평군, 충남 서산시·예산시, 전남 담양군, 경남 산청군·합천군"
        )

        self.assertIsNotNone(idong)
        self.assertEqual(
            idong.expected_value,  # type: ignore[union-attr]
            "경기 포천시 이동면 / Gyeonggi Pocheon-si Idong-myeon",
        )
        self.assertIsNotNone(grouped)
        self.assertIn("예산군", grouped.expected_value)  # type: ignore[union-attr]
        self.assertNotIn("Yesan-si", grouped.expected_value)  # type: ignore[union-attr]


class TableDisplayParsingTests(unittest.TestCase):
    def test_service_description_is_kept_as_a_separate_text_column(self) -> None:
        matrix = [
            ["구분\nClassification", "서비스 내용\nService", "이용량\nUsage"],
            ["기관 A", "민원 안내", "10"],
            ["기관 B", "증명서 발급", "20"],
        ]

        label_columns = leading_label_column_indexes(matrix, 1)
        columns = build_columns(matrix, 1, label_columns)

        self.assertEqual(label_columns, [0])
        self.assertEqual([column.source_col_indexes for column in columns], [[0], [1], [2]])

    def test_bilingual_labels_support_decades_curly_apostrophes_and_suffix_hyphens(self) -> None:
        self.assertEqual(clean_label("20대\n20s"), "20대")
        self.assertEqual(english_label("20대\n20s"), "20s")
        self.assertEqual(clean_label("대로\n-daero"), "대로")
        self.assertEqual(english_label("대로\n-daero"), "-daero")
        self.assertEqual(
            clean_label("검찰청\nPublic\nProsecutor’s Office"),
            "검찰청",
        )
        self.assertEqual(
            english_label("검찰청\nPublic\nProsecutor’s Office"),
            "Public Prosecutor’s Office",
        )

    def test_bracketed_type_qualifiers_are_preserved_in_english_headers(self) -> None:
        type_a = "출자기관\nGovernment-funded Organizations\n[Type A]"
        type_b = "출연기관\nGovernment-funded Organizations\n[Type B]"

        self.assertEqual(clean_label(type_a), "출자기관")
        self.assertEqual(english_label(type_a), "Government-funded Organizations [Type A]")
        self.assertEqual(clean_label(type_b), "출연기관")
        self.assertEqual(english_label(type_b), "Government-funded Organizations [Type B]")

    def test_registry_descriptor_columns_are_not_collapsed(self) -> None:
        matrix = [
            ["위원회명\nName", "설치근거\nBasis for Establishment", "위원장\nChairperson", "위원수"],
            ["위원회 A", "법률 제1조", "차관", "10"],
            ["위원회 B", "법률 제2조", "민간인", "12"],
        ]
        self.assertEqual(leading_label_column_indexes(matrix, 1), [0])

    def test_total_measure_column_is_not_merged_into_the_row_label(self) -> None:
        matrix = [
            ["기준일", "", "", ""],
            ["구분\nClassification", "전체\nTotal", "최초 이용자수", ""],
            ["구분\nClassification", "전체\nTotal", "전체\nTotal", "여성\nFemale"],
            ["일반직 계", "24,266", "18,935", "13,849"],
            ["4급 이상", "-", "18", "4"],
        ]

        self.assertEqual(leading_label_column_indexes(matrix, 3), [0])

    def test_public_enterprise_header_shift_is_corrected_for_display(self) -> None:
        matrix = [
            ["구분", "계", "직영기업 소계", "상수도", "하수도", "공영개발", "운송", "공사·공단 소계", "도시철도", "도시개발", "시설·환경", "기타"],
            ["계", "422", "256", "123", "105", "27", "1", "-", "166", "6", "16", "88"],
        ]

        columns = build_columns(matrix, 1, [0], table_code="5-1-9-1")

        self.assertEqual(columns[7].label, "직영기업 / 지역개발기금")
        self.assertEqual(columns[8].label, "공사·공단 등 / 소계")
        self.assertEqual(columns[11].label, "공사·공단 등 / 시설·환경·경륜 등")

    def test_only_unmatched_closing_parentheses_are_removed(self) -> None:
        self.assertEqual(remove_unmatched_closing_parentheses("Number of Persons)"), "Number of Persons")
        self.assertEqual(remove_unmatched_closing_parentheses("Budget (KRW 100 million)"), "Budget (KRW 100 million)")
        self.assertEqual(
            remove_unmatched_closing_parentheses("기타1)", preserve_numbered_markers=True),
            "기타1)",
        )


if __name__ == "__main__":
    unittest.main()
