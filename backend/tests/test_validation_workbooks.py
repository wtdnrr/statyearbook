from __future__ import annotations

import unittest

from app.export.validation_workbooks import base_table_code, parse_contact_metadata


class ValidationWorkbookTest(unittest.TestCase):
    def test_split_table_code_uses_one_logical_number(self) -> None:
        self.assertEqual(base_table_code("4-1-2-4 표2"), "4-1-2-4")
        self.assertEqual(base_table_code("8-1-6"), "8-1-6")

    def test_contact_fields_are_split_from_source(self) -> None:
        parsed = parse_contact_metadata(
            r"\* 조직기획과 주무관 김일표 (044-205-2315) / 정부조직관리정보시스템(www.org.go.kr)"
        )
        self.assertEqual(parsed.department, "조직기획과")
        self.assertEqual(parsed.officer, "주무관 김일표")
        self.assertEqual(parsed.extension, "044-205-2315")
        self.assertEqual(parsed.source_reference, "정부조직관리정보시스템(www.org.go.kr)")

    def test_contact_source_without_reference_keeps_remainder_only(self) -> None:
        parsed = parse_contact_metadata("* 지방재정경제실 재정정책과 사무관 이나라 044-205-3701 내부행정자료")
        self.assertEqual(parsed.department, "지방재정경제실 재정정책과")
        self.assertEqual(parsed.officer, "사무관 이나라")
        self.assertEqual(parsed.extension, "044-205-3701")
        self.assertEqual(parsed.source_reference, "내부행정자료")


if __name__ == "__main__":
    unittest.main()
