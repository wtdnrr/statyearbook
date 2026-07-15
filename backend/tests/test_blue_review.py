from pathlib import Path
import tempfile
import unittest
import zipfile

from app.services.sqlite_report_service import (
    leading_label_column_indexes,
    remove_unmatched_closing_parentheses,
)
from app.validation.blue_review import (
    extract_blue_review_candidates,
    row_context_matches,
)


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


class TableDisplayParsingTests(unittest.TestCase):
    def test_registry_descriptor_columns_are_not_collapsed(self) -> None:
        matrix = [
            ["위원회명\nName", "설치근거\nBasis for Establishment", "위원장\nChairperson", "위원수"],
            ["위원회 A", "법률 제1조", "차관", "10"],
            ["위원회 B", "법률 제2조", "민간인", "12"],
        ]
        self.assertEqual(leading_label_column_indexes(matrix, 1), [0])

    def test_only_unmatched_closing_parentheses_are_removed(self) -> None:
        self.assertEqual(remove_unmatched_closing_parentheses("Number of Persons)"), "Number of Persons")
        self.assertEqual(remove_unmatched_closing_parentheses("Budget (KRW 100 million)"), "Budget (KRW 100 million)")
        self.assertEqual(
            remove_unmatched_closing_parentheses("기타1)", preserve_numbered_markers=True),
            "기타1)",
        )


if __name__ == "__main__":
    unittest.main()
