from __future__ import annotations

import unittest

from app.validation.models import ValidationCell, ValidationTable
from app.validation.cross_table_rules import (
    ConfiguredCrossTableCellMatchRule,
    ConfiguredCrossTableRowSumRule,
    ConfiguredCrossTableWeightedAverageRule,
)
from app.validation.curated_profiles import apply_curated_profile, curated_profiles
from app.validation.profile_rules import ProfileSpecRule
from app.validation.rules import leading_label_columns
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
    def test_total_measure_column_is_not_a_validation_row_label(self) -> None:
        table = make_table(
            [
                ["구분", "전체", "최초 이용자수", ""],
                ["구분", "전체", "전체", "여성"],
                ["일반직 계", "24,266", "18,935", "13,849"],
                ["4급 이상", "-", "18", "4"],
            ],
            header_rows=2,
        )

        self.assertEqual(leading_label_columns(table), [0])

    def test_profile_generation_includes_metadata_presence_validation(self) -> None:
        table = make_table([["구분", "2025"], ["계", "10"]])

        checks = infer_profile_checks(table, analysis=analyze_table(table), templates=detect_templates(table))
        metadata_checks = [
            check
            for check in checks
            if check.get("type") in {"unit_required", "metadata_required"}
        ]

        self.assertEqual(len(metadata_checks), 1)
        self.assertEqual(metadata_checks[0]["fields"], ["unit", "base_date", "source"])

    def test_legacy_metadata_specs_are_merged_at_runtime(self) -> None:
        table = make_table([["구분", "2025"], ["계", "10"]])
        table.unit = ""
        table.base_date = ""
        profile = profile_for(
            table,
            [
                {
                    "id": "profile.test.unit_required",
                    "type": "unit_required",
                    "check_type": "단위 검수",
                    "expected_unit": "건",
                },
                {
                    "id": "profile.test.metadata_required",
                    "type": "metadata_required",
                    "check_type": "메타정보 검수",
                    "fields": ["base_date", "source"],
                },
                {"id": "profile.test.retired_structure_check", "type": "retired_structure_check"},
            ],
        )

        issues, checks = ProfileSpecRule({table.code: profile}).evaluate(table)

        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].check_type, "메타정보 검수")
        self.assertEqual(checks[0].status, "확인 필요")
        self.assertIn("단위", checks[0].difference or "")
        self.assertIn("기준일", checks[0].difference or "")
        self.assertEqual(len(issues), 1)

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

    def test_bilingual_per_item_category_remains_in_column_total(self) -> None:
        table = make_table(
            [
                ["구분", "2024", "2025"],
                ["계 Total", "60", "80"],
                ["지방세 세목별 증명 Certificates per Item", "10", "20"],
                ["주민등록 증명 Certificates of Residence", "20", "25"],
                ["소득 증명 Certificates of Income", "30", "35"],
            ],
            code="2-1-3-3",
        )

        checks = infer_profile_checks(table, analysis=analyze_table(table), templates=detect_templates(table))
        total = next(
            check
            for check in checks
            if check.get("type") == "column_sum" and check.get("target_row") == 1
        )

        self.assertEqual(total["operand_rows"], [2, 3, 4])

    def test_rank_rows_are_not_inferred_as_implicit_subtotals(self) -> None:
        table = make_table(
            [
                ["순위", "2024", "2025"],
                ["1", "0.8", "0.9"],
                ["2", "0.3", "0.4"],
                ["3", "0.5", "0.5"],
            ],
            unit="지수",
        )

        checks = infer_profile_checks(table, analysis=analyze_table(table), templates=detect_templates(table))

        self.assertFalse(any("implicit_subtotal" in check.get("id", "") for check in checks))

    def test_unlabeled_but_exact_subtotal_keeps_its_direct_children(self) -> None:
        table = make_table(
            [
                ["구분", "2024", "2025"],
                ["일반직", "30", "40"],
                ["직급 A", "10", "15"],
                ["직급 B", "20", "25"],
                ["특정직", "5", "6"],
            ]
        )

        checks = infer_profile_checks(table, analysis=analyze_table(table), templates=detect_templates(table))
        subtotal = next(check for check in checks if "implicit_subtotal" in check.get("id", ""))

        self.assertEqual(subtotal["target_row"], 1)
        self.assertEqual(subtotal["operand_rows"], [2, 3])

    def test_semantic_subtotal_allows_display_rounding_difference(self) -> None:
        table = make_table(
            [
                ["구분", "2023", "2024", "2025"],
                ["자산", "210", "223.2", "231.7"],
                ["부채", "54.4", "56.3", "61.3"],
                ["자본", "155.6", "166.9", "170.3"],
            ],
            unit="조원",
        )

        checks = infer_profile_checks(table, analysis=analyze_table(table), templates=detect_templates(table))
        subtotal = next(check for check in checks if "implicit_subtotal" in check.get("id", ""))

        self.assertEqual(subtotal["target_row"], 1)
        self.assertEqual(subtotal["operand_rows"], [2, 3])

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

    def test_ratio_row_is_not_treated_as_a_sum_across_years(self) -> None:
        table = make_table(
            [
                ["구분", "계", "2024", "2025"],
                ["전체 과제 수", "30", "10", "20"],
                ["수의계약 과제 수", "12", "4", "8"],
                ["수의계약 비율", "40", "40", "40"],
            ],
            code="2-2-8-3",
            unit="건, %",
        )
        checks = infer_profile_checks(table, analysis=analyze_table(table), templates=detect_templates(table))
        row_sum = next(check for check in checks if check.get("type") == "row_sum")
        profile = profile_for(table, [row_sum])

        _, results = ProfileSpecRule({table.code: profile}).evaluate(table)

        self.assertEqual({result.row_index for result in results}, {1, 2})

    def test_per_task_cost_is_checked_from_total_cost_and_total_tasks(self) -> None:
        table = make_table(
            [
                ["구분", "계", "2024", "2025"],
                ["정책연구용역 실적 Cases of Policy Study Tasks", "100", "40", "60"],
                ["용역비 Service Cost", "1,100", "400", "700"],
                ["건당 비용 Cost Per Task", "10.5", "10", "11.7"],
            ],
            code="2-2-8-1",
            unit="건, 백만원",
        )
        checks = infer_profile_checks(table, analysis=analyze_table(table), templates=detect_templates(table))
        ratio = next(check for check in checks if check.get("id", "").endswith("per_unit_ratio_r3"))
        self.assertEqual(ratio["numerator_row"], 2)
        self.assertEqual(ratio["denominator_row"], 1)
        self.assertEqual(ratio["columns"], [1, 2, 3])

        profile = profile_for(table, [ratio])
        _, results = ProfileSpecRule({table.code: profile}).evaluate(table)
        total_result = next(result for result in results if result.col_index == 1)
        self.assertEqual(total_result.expected_value, "11")
        self.assertEqual(total_result.status, "정상")
        self.assertIn("개별 비율을 더하지 않고", total_result.detail)

    def test_sum_or_ratio_difference_up_to_one_is_not_an_error(self) -> None:
        table = make_table(
            [
                ["구분", "계", "A", "B", "비율"],
                ["합계", "11", "5", "5", "101"],
                ["불일치", "12.1", "5", "5", "101.1"],
            ],
            unit="건, %",
        )
        profile = profile_for(
            table,
            [
                {
                    "id": "test.sum_tolerance",
                    "type": "row_sum",
                    "check_group": "sum",
                    "target_column": 1,
                    "operand_columns": [2, 3],
                    "tolerance": 0.0,
                    "failure_status": "오류 의심",
                    "severity": "critical",
                },
                {
                    "id": "test.ratio_tolerance",
                    "type": "row_ratio",
                    "check_group": "ratio",
                    "target_column": 4,
                    "numerator_column": 2,
                    "denominator_column": 3,
                    "multiplier": 100,
                    "tolerance": 0.15,
                    "failure_status": "오류 의심",
                    "severity": "critical",
                },
            ],
        )

        _, results = ProfileSpecRule({table.code: profile}).evaluate(table)

        sum_results = [result for result in results if result.rule_id == "test.sum_tolerance"]
        ratio_results = [result for result in results if result.rule_id == "test.ratio_tolerance"]
        self.assertEqual([result.status for result in sum_results], ["정상", "오류 의심"])
        self.assertEqual([result.status for result in ratio_results], ["정상", "오류 의심"])

    def test_row_sum_excludes_parenthesized_percentage_annotation(self) -> None:
        table = make_table(
            [
                ["연도", "합계", "장비 A", "장비 B"],
                ["2025", "30\n(120%)", "10\n(110%)", "20\n(130%)"],
            ]
        )
        profile = profile_for(
            table,
            [
                {
                    "id": "test.annotated_total",
                    "type": "row_sum",
                    "check_group": "sum",
                    "target_column": 1,
                    "operand_columns": [2, 3],
                }
            ],
        )

        issues, checks = ProfileSpecRule({table.code: profile}).evaluate(table)

        self.assertEqual(issues, [])
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].current_value, "30")
        self.assertEqual(checks[0].expected_value, "30")

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

    def test_top_level_total_uses_subtotal_and_unaggregated_sibling_columns(self) -> None:
        table = make_table(
            [
                ["기관", "총계", "일반직", "", "", "특정직", ""],
                ["기관", "총계", "소계", "임기제", "고위", "경찰·소방", "교육"],
                ["계", "42", "30", "10", "20", "5", "7"],
                ["기관 A", "21", "15", "5", "10", "2", "4"],
                ["기관 B", "21", "15", "5", "10", "3", "3"],
            ],
            code="nested-top-level",
            header_rows=2,
        )
        checks = infer_profile_checks(table, analysis=analyze_table(table), templates=detect_templates(table))
        sums = [check for check in checks if check.get("type") == "row_sum"]
        grand_total = next(check for check in sums if check.get("target_column") == 1)
        subtotal = next(check for check in sums if check.get("target_column") == 2)

        self.assertEqual(grand_total["operand_columns"], [2, 5, 6])
        self.assertEqual(subtotal["operand_columns"], [3, 4])

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

    def test_targeted_2026_profiles_cover_requested_sum_and_growth_rules(self) -> None:
        profiles = curated_profiles()

        self.assertEqual(
            next(
                check
                for check in profiles["2-1-3-4"]["checks"]
                if check["type"] == "row_sum"
            )["operand_columns"],
            [2, 3, 4, 5, 6],
        )
        self.assertEqual(
            next(
                check
                for check in profiles["2-1-5-2"]["checks"]
                if check["type"] == "year_rows_change_rate"
            )["value_column"],
            2,
        )
        self.assertEqual(
            next(
                check
                for check in profiles["2-2-2-3"]["checks"]
                if check["type"] == "row_sum"
            )["row_indices"],
            list(range(1, 26, 2)),
        )
        self.assertEqual(
            next(
                check
                for check in profiles["2-2-12-1"]["checks"]
                if check["type"] == "row_sum"
            )["operand_columns"],
            [3, 4, 5, 6, 7, 8],
        )

    def test_additional_organization_profiles_cover_growth_hierarchical_and_separate_totals(self) -> None:
        profiles = curated_profiles()

        growth_rule = next(
            check
            for check in profiles["1-2-1-2"]["checks"]
            if check["type"] == "year_rows_change_rate"
        )
        self.assertEqual(growth_rule["value_column"], 1)
        self.assertEqual(growth_rule["change_column"], 2)
        self.assertEqual(growth_rule["rate_column"], 3)

        total_rules = profiles["1-2-1-3"]["checks"]
        self.assertEqual(total_rules[0]["operand_rows"], [2, 3, 4, 5, 6])
        self.assertEqual(total_rules[1]["operand_rows"], [7, 71])
        self.assertEqual(total_rules[2]["target_row"], 7)
        self.assertEqual(total_rules[2]["operand_rows"], list(range(8, 71)))

        separated_totals = profiles["1-2-3-3"]["checks"]
        self.assertEqual(
            [check["target_column"] for check in separated_totals],
            [1, 4, 7],
        )

        self.assertEqual(
            profiles["3-1-3-2"]["checks"][0]["row_pairs"],
            [
                {"target_row": 2, "source_row": 1},
                {"target_row": 4, "source_row": 3},
                {"target_row": 6, "source_row": 5},
                {"target_row": 8, "source_row": 7},
                {"target_row": 10, "source_row": 9},
                {"target_row": 12, "source_row": 11},
                {"target_row": 14, "source_row": 13},
            ],
        )
        self.assertEqual(len(profiles["3-1-6-1"]["checks"][0]["comparisons"]), 5)
        self.assertEqual(len(profiles["3-1-7"]["checks"][0]["comparisons"]), 2)

    def test_cross_table_total_uses_same_label_from_second_part(self) -> None:
        national = make_table(
            [
                ["연도", "계", "국가공무원 소계"],
                ["2024", "100", "60"],
                ["2025", "110", "65"],
            ],
            code="1-2-2 표1",
        )
        local = make_table(
            [
                ["연도", "지방공무원 소계"],
                ["2024", "40"],
                ["2025", "45"],
            ],
            code="1-2-2 표2",
        )
        local.id = 2
        profile = profile_for(
            national,
            [
                {
                    "id": "test.cross_table_total",
                    "type": "cross_table_row_sum",
                    "check_type": "합계 검수",
                    "source_table_code": "1-2-2 표2",
                    "target_column": 1,
                    "operand_columns": [2],
                    "source_column": 1,
                    "row_indices": [1, 2],
                }
            ],
        )

        result = ConfiguredCrossTableRowSumRule({national.code: profile}).evaluate([national, local])

        self.assertEqual(len(result.checks), 2)
        self.assertEqual(result.issues, [])
        self.assertEqual(result.checks[0].expected_value, "100")

    def test_cross_table_total_can_combine_same_part_and_second_part_operands(self) -> None:
        source = make_table(
            [
                ["지역", "온천공보호구역 지정 계"],
                ["합계", "261"],
                ["서울", "8"],
            ],
            code="4-2-12 표2",
        )
        target = make_table(
            [
                ["지역", "계", "지정절차 진행중", "온천원보호지구 지정 계"],
                ["합계", "438", "59", "118"],
                ["서울", "8", "-", "-"],
            ],
            code="4-2-12 표1",
        )
        target.id = 1
        source.id = 2
        profile = profile_for(
            target,
            [
                {
                    "id": "test.cross_table_hot_spring_total",
                    "type": "cross_table_row_sum",
                    "check_type": "합계 검수",
                    "source_table_code": "4-2-12 표2",
                    "target_column": 1,
                    "operand_columns": [2, 3],
                    "source_column": 1,
                }
            ],
        )

        result = ConfiguredCrossTableRowSumRule({target.code: profile}).evaluate([target, source])

        self.assertEqual(len(result.checks), 2)
        self.assertEqual(result.issues, [])
        self.assertEqual(result.checks[0].expected_value, "438")
        self.assertEqual(result.checks[1].expected_value, "8")

    def test_cross_table_total_can_combine_multiple_source_columns(self) -> None:
        source = make_table(
            [
                ["지역", "유형 A", "유형 B", "유형 C"],
                ["합계", "20", "30", "10"],
                ["서울", "4", "5", "1"],
            ],
            code="4-1-2-4 표2",
        )
        target = make_table(
            [
                ["지역", "총계", "표1 소계"],
                ["합계", "100", "40"],
                ["서울", "20", "10"],
            ],
            code="4-1-2-4 표1",
        )
        target.id = 1
        source.id = 2
        profile = profile_for(
            target,
            [
                {
                    "id": "test.cross_table_multiple_source_columns",
                    "type": "cross_table_row_sum",
                    "check_type": "합계 검수",
                    "source_table_code": "4-1-2-4 표2",
                    "target_column": 1,
                    "operand_columns": [2],
                    "source_columns": [1, 2, 3],
                }
            ],
        )

        result = ConfiguredCrossTableRowSumRule({target.code: profile}).evaluate([target, source])

        self.assertEqual(len(result.checks), 2)
        self.assertEqual(result.issues, [])
        self.assertEqual(result.checks[0].expected_value, "100")
        self.assertEqual(result.checks[1].expected_value, "20")

    def test_cross_table_total_can_emit_source_operand_checks(self) -> None:
        source = make_table(
            [
                ["지역", "피해 A", "피해 B"],
                ["서울", "4", "6"],
                ["합계", "20", "30"],
            ],
            code="7-3-2-1 표2",
        )
        target = make_table(
            [
                ["지역", "재산 합계", "표1 피해"],
                ["합계", "90", "40"],
                ["서울", "20", "10"],
            ],
            code="7-3-2-1 표1",
        )
        target.id = 1
        source.id = 2
        profile = profile_for(
            target,
            [
                {
                    "id": "test.cross_table_source_evidence",
                    "type": "cross_table_row_sum",
                    "check_type": "합계 검수",
                    "source_table_code": source.code,
                    "target_column": 1,
                    "operand_columns": [2],
                    "source_columns": [1, 2],
                    "emit_source_checks": True,
                }
            ],
        )

        result = ConfiguredCrossTableRowSumRule({target.code: profile}).evaluate([target, source])

        self.assertEqual(len(result.checks), 4)
        self.assertEqual(result.issues, [])
        source_checks = [check for check in result.checks if check.table_id == source.id]
        self.assertEqual(len(source_checks), 2)
        self.assertEqual(source_checks[0].row_index, 2)
        self.assertTrue(source_checks[0].rule_id.startswith("cross.profile_row_sum_operand:"))

    def test_cumulative_cell_relationships_support_copy_and_component_totals(self) -> None:
        table = make_table(
            [
                ["구분", "이용자 수", "센터 수", "폐쇄"],
                ["누적 계", "100", "12", ""],
                ["소계", "100", "9", "3"],
            ]
        )
        profile = profile_for(
            table,
            [
                {
                    "id": "test.cumulative_summary",
                    "type": "cell_relation_sum",
                    "check_type": "합계 검수",
                    "label": "누적 계 = 소계",
                    "comparisons": [
                        {
                            "target": {"row": 1, "column": 1},
                            "operand_cells": [{"row": 2, "column": 1}],
                        },
                        {
                            "target": {"row": 1, "column": 2},
                            "operand_cells": [
                                {"row": 2, "column": 2},
                                {"row": 2, "column": 3},
                            ],
                        },
                    ],
                }
            ],
        )

        issues, checks = ProfileSpecRule({table.code: profile}).evaluate(table)

        self.assertEqual(issues, [])
        self.assertEqual(len(checks), 2)
        self.assertEqual([check.expected_value for check in checks], ["100", "12"])

    def test_cell_relationship_supports_subtraction(self) -> None:
        table = make_table(
            [
                ["구분", "2026", "2025"],
                ["통합재정수지", "-10", "-20"],
                ["통합재정수입", "100", "80"],
                ["통합재정지출", "110", "100"],
            ]
        )
        profile = profile_for(
            table,
            [
                {
                    "id": "test.balance_subtraction",
                    "type": "cell_relation_sum",
                    "check_type": "합계 검수",
                    "comparisons": [
                        {
                            "target": {"row": 1, "column": 1},
                            "operand_cells": [
                                {"row": 2, "column": 1, "op": "+"},
                                {"row": 3, "column": 1, "op": "-"},
                            ],
                        }
                    ],
                }
            ],
        )

        issues, checks = ProfileSpecRule({table.code: profile}).evaluate(table)

        self.assertEqual(issues, [])
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].expected_value, "-10")

    def test_cross_table_weighted_average_uses_the_linked_weight_table(self) -> None:
        target = make_table(
            [
                ["구분", "평균"],
                ["평균", "37.5"],
                ["A", "30"],
                ["B", "40"],
            ],
            code="ratio-table",
        )
        source = make_table(
            [
                ["구분", "예산"],
                ["A", "100"],
                ["B", "300"],
            ],
            code="budget-table",
        )
        source.id = 2
        profile = profile_for(
            target,
            [
                {
                    "id": "test.cross_weighted_average",
                    "type": "cross_table_weighted_average",
                    "check_type": "평균 검수",
                    "target_row": 1,
                    "target_column": 1,
                    "value_column": 1,
                    "source_table_code": source.code,
                    "source_weight_column": 1,
                    "row_pairs": [
                        {"value_row": 2, "weight_row": 1},
                        {"value_row": 3, "weight_row": 2},
                    ],
                    "tolerance": 0.1,
                }
            ],
        )

        result = ConfiguredCrossTableWeightedAverageRule({target.code: profile}).evaluate([target, source])

        self.assertEqual(result.issues, [])
        self.assertEqual(len(result.checks), 1)
        self.assertEqual(result.checks[0].expected_value, "37.5")

    def test_cross_table_weighted_average_keeps_a_display_check_when_values_are_missing(self) -> None:
        target = make_table(
            [
                ["구분", "평균"],
                ["평균", "-"],
                ["A", "30"],
                ["B", "-"],
            ],
            code="ratio-table",
        )
        source = make_table(
            [
                ["구분", "예산"],
                ["A", "100"],
                ["B", "-"],
            ],
            code="budget-table",
        )
        source.id = 2
        profile = profile_for(
            target,
            [
                {
                    "id": "test.cross_weighted_average_missing_values",
                    "type": "cross_table_weighted_average",
                    "check_type": "평균 검수",
                    "target_row": 1,
                    "target_column": 1,
                    "value_column": 1,
                    "source_table_code": source.code,
                    "source_weight_column": 1,
                    "row_pairs": [
                        {"value_row": 2, "weight_row": 1},
                        {"value_row": 3, "weight_row": 2},
                    ],
                }
            ],
        )

        result = ConfiguredCrossTableWeightedAverageRule({target.code: profile}).evaluate([target, source])

        self.assertEqual(result.issues, [])
        self.assertEqual(len(result.checks), 1)
        self.assertEqual(result.checks[0].status, "정상")
        self.assertEqual(result.checks[0].expected_value, "계산 제외 (연산값 부족)")

    def test_cross_table_cell_match_uses_the_latest_data_row(self) -> None:
        target = make_table(
            [
                ["연도", "서울", "전국일평균"],
                ["2024", "31", "26.8"],
                ["2025", "44", "29.7"],
            ],
            code="yearly-heat-days",
        )
        source = make_table(
            [
                ["지역", "폭염 일수"],
                ["전국", "29.7"],
            ],
            code="national-heat-days",
        )
        source.id = 2
        profile = profile_for(
            target,
            [
                {
                    "id": "test.cross_cell_match",
                    "type": "cross_table_cell_match",
                    "check_type": "평균 검수",
                    "target_row": "latest_data_row",
                    "target_column": 2,
                    "source_table_code": source.code,
                    "source_row": 1,
                    "source_column": 1,
                    "tolerance": 0.1,
                }
            ],
        )

        result = ConfiguredCrossTableCellMatchRule({target.code: profile}).evaluate([target, source])

        self.assertEqual(result.issues, [])
        self.assertEqual(len(result.checks), 1)
        self.assertEqual(result.checks[0].row_index, 2)
        self.assertEqual(result.checks[0].col_index, 2)
        self.assertEqual(result.checks[0].expected_value, "29.7")

    def test_row_arithmetic_display_only_rows_keep_unavailable_inputs_visible(self) -> None:
        table = make_table(
            [
                ["구분", "기준연도", "당해연도", "증감액"],
                ["일반 유형", "10", "20", "10"],
                ["신설 유형", "-", "30", "-"],
            ]
        )
        profile = profile_for(
            table,
            [
                {
                    "id": "test.change_amount",
                    "type": "row_arithmetic",
                    "check_type": "증감액 검수",
                    "label": "당해연도 - 기준연도",
                    "target_column": 3,
                    "terms": [{"column": 2, "op": "+"}, {"column": 1, "op": "-"}],
                    "row_indices": [1],
                    "display_only_row_indices": [2],
                }
            ],
        )

        issues, checks = ProfileSpecRule({table.code: profile}).evaluate(table)

        self.assertEqual(issues, [])
        self.assertEqual(len(checks), 2)
        skipped = checks[1]
        self.assertEqual(skipped.row_index, 2)
        self.assertEqual(skipped.current_value, "-")
        self.assertEqual(skipped.expected_value, "계산 제외 (기준연도 값 없음)")
        self.assertEqual(skipped.status, "정상")

    def test_cumulative_relationship_uses_the_latest_year_added_to_the_table(self) -> None:
        table = make_table(
            [
                ["구분", "활용처"],
                ["누적 계", "141"],
                ["2025", "103"],
                ["2026", "141"],
            ]
        )
        profile = profile_for(
            table,
            [
                {
                    "id": "test.cumulative_latest_year",
                    "type": "cell_relation_sum",
                    "check_type": "합계 검수",
                    "comparisons": [
                        {
                            "target": {"row_selector": "cumulative_total", "column": 1},
                            "operand_cells": [{"row_selector": "latest_year", "column": 1}],
                        }
                    ],
                }
            ],
        )

        issues, checks = ProfileSpecRule({table.code: profile}).evaluate(table)

        self.assertEqual(issues, [])
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].expected_value, "141")

    def test_paired_year_growth_rate_checks_each_value_rate_row_pair(self) -> None:
        table = make_table(
            [
                ["구분", "2024", "2025"],
                ["전체", "100", "110"],
                ["증가율", "", "10"],
                ["중앙", "200", "220"],
                ["증가율", "", "10"],
            ]
        )
        profile = profile_for(
            table,
            [
                {
                    "id": "test.paired_growth_rate",
                    "type": "row_year_over_year_rate",
                    "check_type": "증감률 검수",
                    "row_pairs": [
                        {"target_row": 2, "source_row": 1},
                        {"target_row": 4, "source_row": 3},
                    ],
                    "columns": [1, 2],
                    "multiplier": 100,
                    "tolerance": 0.15,
                }
            ],
        )

        issues, checks = ProfileSpecRule({table.code: profile}).evaluate(table)

        self.assertEqual(issues, [])
        self.assertEqual(len(checks), 2)
        self.assertEqual([check.row_index for check in checks], [2, 4])

    def test_paired_year_growth_rate_includes_dash_as_zero_when_rate_is_defined(self) -> None:
        table = make_table(
            [
                ["구분", "2022", "2023"],
                ["독립기관 시스템 수", "148", "-"],
                ["독립기관 증가율", "2.1", "-"],
            ]
        )
        profile = profile_for(
            table,
            [
                {
                    "id": "test.paired_growth_rate_dash",
                    "type": "row_year_over_year_rate",
                    "check_type": "증감률 검수",
                    "failure_status": "오류 의심",
                    "row_pairs": [{"target_row": 2, "source_row": 1}],
                    "columns": [1, 2],
                    "multiplier": 100,
                    "tolerance": 0.15,
                }
            ],
        )

        issues, checks = ProfileSpecRule({table.code: profile}).evaluate(table)

        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].row_index, 2)
        self.assertEqual(checks[0].col_index, 2)
        self.assertEqual(checks[0].current_value, "0")
        self.assertEqual(checks[0].expected_value, "-100")
        self.assertEqual(checks[0].status, "오류 의심")
        self.assertEqual(len(issues), 1)

    def test_targeted_2026_profile_removes_duplicate_automatic_total_rule(self) -> None:
        application = apply_curated_profile(
            "2-2-1-2",
            checks=[
                {"id": "automatic.arithmetic", "type": "row_arithmetic"},
                {"id": "automatic.total", "type": "row_sum"},
            ],
            table_type="year_trend_table",
            status="ready",
            notes="",
        )

        sum_checks = [check for check in application.checks if check.get("type") == "row_sum"]
        growth_checks = [check for check in application.checks if check.get("type") == "year_rows_change_rate"]
        self.assertEqual(len(sum_checks), 1)
        self.assertEqual(sum_checks[0]["id"], "curated.2-2-1-2.total_by_operation_status")
        self.assertEqual(len(growth_checks), 1)

    def test_direct_profile_is_not_overridden_by_a_generic_group(self) -> None:
        profiles = curated_profiles()

        self.assertEqual(
            profiles["5-1-2-1"]["decision"],
            "curated_weighted_average_profile",
        )
        self.assertEqual(
            profiles["5-2-1-1"]["decision"],
            "curated_total_and_change_profile",
        )

    def test_direct_profile_overrides_shared_group_profile(self) -> None:
        application = apply_curated_profile(
            "4-1-7-3",
            checks=[{"id": "automatic.row_total", "type": "row_sum"}],
            table_type="general",
            status="needs_review",
            notes="",
        )

        self.assertEqual(application.status, "ready")
        self.assertFalse(any(check["id"] == "automatic.row_total" for check in application.checks))
        total_check = next(
            check
            for check in application.checks
            if check["id"] == "curated.4-1-7-3.total_quota_by_institution_layer"
        )
        self.assertTrue(total_check["include_calculation_rows"])

    def test_row_sum_can_explicitly_validate_a_total_row(self) -> None:
        table = make_table(
            [
                ["구분", "계", "시", "군"],
                ["총계", "10", "4", "6"],
            ]
        )
        profile = profile_for(
            table,
            [
                {
                    "id": "test.total_row_sum",
                    "type": "row_sum",
                    "row_indices": [1],
                    "include_calculation_rows": True,
                    "target_column": 1,
                    "operand_columns": [2, 3],
                }
            ],
        )

        issues, checks = ProfileSpecRule({table.code: profile}).evaluate(table)

        self.assertEqual(issues, [])
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].row_index, 1)
        self.assertEqual(checks[0].status, "정상")

    def test_grade_profile_checks_all_service_columns(self) -> None:
        profile = curated_profiles()["4-1-7-4"]
        grade_total = next(
            check
            for check in profile["checks"]
            if check["id"] == "curated.4-1-7-4.general_and_special_grade_total"
        )

        self.assertEqual(grade_total["columns"], [1, 2, 3, 4, 5])

    def test_age_profile_checks_every_female_count_column(self) -> None:
        profile = curated_profiles()["4-1-8-3"]
        checks = {check["id"]: check for check in profile["checks"]}

        self.assertEqual(
            checks["curated.4-1-8-3.female_age_total"]["operand_columns"],
            [4, 6, 8, 10, 12, 14],
        )
        self.assertEqual(
            checks["curated.4-1-8-3.general_service_total"]["columns"],
            list(range(1, 15)),
        )
        self.assertEqual(
            checks["curated.4-1-8-3.grand_total_by_service"]["columns"],
            list(range(1, 15)),
        )

    def test_budget_and_enterprise_profiles_apply_totals_to_every_row(self) -> None:
        profiles = curated_profiles()
        budget_checks = {check["id"]: check for check in profiles["5-1-1-2"]["checks"]}
        enterprise_checks = profiles["5-1-9-1"]["checks"]

        self.assertEqual(
            budget_checks["curated.5-1-1-2.total_general_by_government_level"]["operand_columns"],
            [5, 8],
        )
        self.assertEqual(
            budget_checks["curated.5-1-1-2.total_special_by_government_level"]["operand_columns"],
            [6, 9],
        )
        self.assertTrue(all("row_indices" not in check for check in enterprise_checks))

    def test_disaster_damage_profile_exposes_second_part_operands(self) -> None:
        checks = curated_profiles()["7-3-2-1 표1"]["checks"]

        self.assertTrue(all(check["emit_source_checks"] for check in checks))


if __name__ == "__main__":
    unittest.main()
