from __future__ import annotations

import unittest

from app.validation.models import ValidationCell, ValidationTable
from app.validation.profile_rules import ProfileSpecRule
from app.validation.profiles import (
    ValidationProfile,
    analyze_table,
    detect_templates,
    infer_profile_checks,
    region_total_check_specs,
    total_label_kind,
)


def make_table(
    matrix: list[list[str]],
    *,
    code: str = "test",
    unit: str = "건",
    header_rows: int = 1,
) -> ValidationTable:
    cells = [
        ValidationCell(
            row_index=row_index,
            col_index=col_index,
            text_value=value,
            numeric_value=None,
            is_header=row_index < header_rows,
        )
        for row_index, row in enumerate(matrix)
        for col_index, value in enumerate(row)
    ]
    return ValidationTable(
        id=1,
        report_id=1,
        code=code,
        title="테스트 표",
        unit=unit,
        base_date="2025. 12. 31.",
        source="담당자",
        note="",
        cells=cells,
    )


def profile_for(table: ValidationTable, checks: list[dict]) -> ValidationProfile:
    return ValidationProfile(
        id=1,
        table_code=table.code,
        table_title=table.title,
        source_report_id=table.report_id,
        structure_signature="test-signature",
        table_type="test",
        status="ready",
        source="test",
        rules={"checks": checks},
        notes="",
        created_at="",
        updated_at="",
    )


class ProfileCalculationTest(unittest.TestCase):
    def test_total_label_does_not_treat_korean_words_as_totals(self) -> None:
        self.assertEqual(total_label_kind("계 Total"), "total")
        self.assertEqual(total_label_kind("소계 Sub-total"), "subtotal")
        self.assertIsNone(total_label_kind("계 곡 Valley"))
        self.assertIsNone(total_label_kind("계층별"))

    def test_grand_total_prefers_subtotals_and_subtotals_keep_leaf_rules(self) -> None:
        table = make_table(
            [
                ["구분", "2025"],
                ["총계", "30"],
                ["소계 A", "10"],
                ["A-1", "4"],
                ["A-2", "6"],
                ["소계 B", "20"],
                ["B-1", "8"],
                ["B-2", "12"],
            ]
        )
        checks = infer_profile_checks(
            table,
            analysis=analyze_table(table),
            templates=detect_templates(table),
        )
        sums = [check for check in checks if check.get("type") == "column_sum"]
        grand_total = next(check for check in sums if check.get("target_row") == 1)
        self.assertEqual(grand_total["operand_rows"], [2, 5])
        self.assertTrue(any(check.get("target_row") == 2 for check in sums))
        self.assertTrue(any(check.get("target_row") == 5 for check in sums))

    def test_ratio_total_is_recalculated_from_total_components(self) -> None:
        table = make_table(
            [
                ["구분", "대상", "전체", "비율(%)"],
                ["총계", "30", "60", "50"],
                ["A", "10", "20", "50"],
                ["B", "8", "16", "50"],
                ["C", "12", "24", "50"],
            ],
            unit="명, %",
        )
        checks = infer_profile_checks(
            table,
            analysis=analyze_table(table),
            templates=detect_templates(table),
        )
        ratio_spec = next(check for check in checks if check.get("type") == "row_ratio")
        self.assertEqual(ratio_spec["aggregate_strategy"], "recalculate_from_numerator_and_denominator")

        profile = profile_for(table, [ratio_spec])
        _, results = ProfileSpecRule({table.code: profile}).evaluate(table)
        total_result = next(result for result in results if result.row_index == 1)
        self.assertEqual(total_result.status, "정상")
        self.assertIn("총계", total_result.detail)

    def test_ratio_can_use_part_plus_complement_as_denominator(self) -> None:
        table = make_table(
            [
                ["구분", "2024", "2025"],
                ["공개율", "80", "75"],
                ["공개 과제 수", "80", "150"],
                ["비공개 과제 수", "20", "50"],
            ],
            unit="건, %",
        )
        checks = infer_profile_checks(table, analysis=analyze_table(table), templates=detect_templates(table))
        ratio = next(check for check in checks if check.get("id", "").endswith("component_ratio_r1"))
        self.assertEqual(ratio["denominator_rows"], [2, 3])

        profile = profile_for(table, [ratio])
        _, results = ProfileSpecRule({table.code: profile}).evaluate(table)
        self.assertEqual([result.status for result in results], ["정상", "정상"])

    def test_wrapped_label_value_blocks_share_one_grand_total(self) -> None:
        table = make_table(
            [
                ["기관", "건수", "기관", "건수"],
                ["계", "10", "", ""],
                ["A", "1", "C", "3"],
                ["B", "2", "D", "4"],
            ]
        )
        checks = infer_profile_checks(table, analysis=analyze_table(table), templates=detect_templates(table))
        cell_sum = next(check for check in checks if check.get("type") == "cell_sum")
        self.assertEqual(cell_sum["target_column"], 1)
        self.assertEqual(
            {(item["row"], item["column"]) for item in cell_sum["operand_cells"]},
            {(2, 1), (2, 3), (3, 1), (3, 3)},
        )

        profile = profile_for(table, [cell_sum])
        _, results = ProfileSpecRule({table.code: profile}).evaluate(table)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "정상")

    def test_nested_header_total_uses_only_direct_child_columns(self) -> None:
        table = make_table(
            [
                ["구분", "붕괴위험지역 지정 현황", "", ""],
                ["연도", "계", "", ""],
                ["연도", "계", "공공시설", "사유시설"],
                ["2024", "30", "20", "10"],
                ["2025", "33", "21", "12"],
            ],
            header_rows=3,
        )
        checks = infer_profile_checks(table, analysis=analyze_table(table), templates=detect_templates(table))
        rule = next(check for check in checks if check.get("target_column") == 1 and check.get("type") == "row_sum")
        self.assertEqual(rule["operand_columns"], [2, 3])

    def test_replaced_header_total_uses_sibling_subgroups(self) -> None:
        table = make_table(
            [
                ["지역", "보호지구 지정", "", ""],
                ["지역", "계", "이용 중", "개발 중"],
                ["지역", "온천", "온천", "온천"],
                ["합계", "30", "20", "10"],
                ["서울", "3", "2", "1"],
            ],
            header_rows=3,
        )
        checks = infer_profile_checks(table, analysis=analyze_table(table), templates=detect_templates(table))
        rule = next(check for check in checks if check.get("target_column") == 1 and check.get("type") == "row_sum")
        self.assertEqual(rule["operand_columns"], [2, 3])

    def test_region_totals_keep_repeated_measure_series_separate(self) -> None:
        regions = ["서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종"]
        matrix = [["지역", "구분", "총계", "재해 A", "재해 B"]]
        matrix.extend([["합계", "세대", "16", "8", "8"], ["합계", "명", "24", "8", "16"]])
        for region in regions:
            matrix.extend([[region, "세대", "2", "1", "1"], [region, "명", "3", "1", "2"]])
        table = make_table(matrix, code="regional", unit="세대, 명")

        rules = region_total_check_specs(table)
        self.assertEqual(len(rules), 2)
        self.assertEqual(len(rules[0]["operand_rows"]), 8)
        self.assertEqual(len(rules[1]["operand_rows"]), 8)
        self.assertTrue(set(rules[0]["operand_rows"]).isdisjoint(rules[1]["operand_rows"]))

    def test_integer_growth_rate_uses_display_rounding_tolerance(self) -> None:
        table = make_table(
            [
                ["구분", "2025 (a)", "2024 (b)", "증감 / 비율"],
                ["예산", "104", "100", "4"],
            ],
            unit="백만원, %",
        )
        checks = infer_profile_checks(table, analysis=analyze_table(table), templates=detect_templates(table))
        rule = next(check for check in checks if check.get("type") == "row_growth_rate")
        self.assertEqual(rule["tolerance"], 0.5)


if __name__ == "__main__":
    unittest.main()
