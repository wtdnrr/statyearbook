from __future__ import annotations

import unittest

from app.services.sqlite_report_service import fallback_highlight_rows, highlight_cells_for


class ValidationHighlightTest(unittest.TestCase):
    def test_exact_cell_fallback_does_not_highlight_the_entire_row(self) -> None:
        row = {"row_index": 14, "col_index": 0}

        self.assertEqual(fallback_highlight_rows(row), [])  # type: ignore[arg-type]

    def test_row_fallback_is_kept_when_no_cell_coordinate_exists(self) -> None:
        row = {"row_index": 14, "col_index": None}

        highlights = fallback_highlight_rows(row)  # type: ignore[arg-type]

        self.assertEqual([(item.row_index, item.role) for item in highlights], [(14, "target")])

    def assert_highlight_cells(
        self,
        row: dict[str, int | None],
        spec: dict,
        *,
        targets: set[tuple[int, int]],
        related: set[tuple[int, int]],
    ) -> None:
        cells = highlight_cells_for(row, spec)  # type: ignore[arg-type]
        actual_targets = {(cell.row_index, cell.col_index) for cell in cells if cell.role == "target"}
        actual_related = {(cell.row_index, cell.col_index) for cell in cells if cell.role == "related"}
        self.assertEqual(actual_targets, targets)
        self.assertEqual(actual_related, related)

    def test_column_sum_error_highlights_only_the_checked_column(self) -> None:
        row = {"row_index": 5, "col_index": 4}
        spec = {
            "type": "column_sum",
            "target_row": 5,
            "operand_rows": [6, 7, 9, 10, 12],
            "columns": [2, 3, 4],
        }

        cells = highlight_cells_for(row, spec)  # type: ignore[arg-type]

        targets = {(cell.row_index, cell.col_index) for cell in cells if cell.role == "target"}
        related = {(cell.row_index, cell.col_index) for cell in cells if cell.role == "related"}
        self.assertEqual(targets, {(5, 4)})
        self.assertEqual(related, {(row_index, 4) for row_index in [6, 7, 9, 10, 12]})

    def test_ratio_highlight_stops_at_its_direct_numerator_and_denominator(self) -> None:
        row = {"row_index": 2, "col_index": 6}
        spec = {
            "type": "row_ratio",
            "target_column": 6,
            "numerator_column": 4,
            "denominator_column": 5,
            "dependency_columns": [1, 2, 3],
        }

        cells = highlight_cells_for(row, spec)  # type: ignore[arg-type]

        targets = {(cell.row_index, cell.col_index) for cell in cells if cell.role == "target"}
        related = {(cell.row_index, cell.col_index) for cell in cells if cell.role == "related"}
        self.assertEqual(targets, {(2, 6)})
        self.assertEqual(related, {(2, 4), (2, 5)})

    def test_cross_table_cell_match_highlights_only_the_visible_target_cell(self) -> None:
        row = {"row_index": 8, "col_index": 7}
        spec = {
            "type": "cross_table_cell_match",
            "target_row": "latest_data_row",
            "target_column": 7,
            "source_table_code": "7-2-4-1",
            "source_row": 1,
            "source_column": 1,
        }

        self.assert_highlight_cells(row, spec, targets={(8, 7)}, related=set())

    def test_calculation_highlights_use_only_direct_formula_cells(self) -> None:
        cases = [
            (
                "row sum",
                {"row_index": 5, "col_index": 1},
                {"type": "row_sum", "target_column": 1, "operand_columns": [2, 3]},
                {(5, 1)},
                {(5, 2), (5, 3)},
            ),
            (
                "cell sum",
                {"row_index": 5, "col_index": 1},
                {
                    "type": "cell_sum",
                    "target_row": 5,
                    "target_column": 1,
                    "operand_cells": [{"row": 6, "column": 1}, {"row": 7, "column": 2}],
                },
                {(5, 1)},
                {(6, 1), (7, 2)},
            ),
            (
                "cumulative relationship",
                {"row_index": 2, "col_index": 1},
                {
                    "type": "cell_relation_sum",
                    "comparisons": [
                        {
                            "target": {"row": 2, "column": 1},
                            "operand_cells": [{"row": 6, "column": 1}],
                        }
                    ],
                },
                {(2, 1)},
                {(6, 1)},
            ),
            (
                "row arithmetic",
                {"row_index": 5, "col_index": 4},
                {
                    "type": "row_arithmetic",
                    "target_column": 4,
                    "terms": [{"column": 2, "op": "+"}, {"column": 3, "op": "-"}],
                },
                {(5, 4)},
                {(5, 2), (5, 3)},
            ),
            (
                "column share ratio",
                {"row_index": 5, "col_index": 4},
                {
                    "type": "column_share_ratio",
                    "target_column": 4,
                    "numerator_column": 2,
                    "denominator_row": 1,
                    "denominator_column": 2,
                },
                {(5, 4)},
                {(5, 2), (1, 2)},
            ),
            (
                "growth rate",
                {"row_index": 5, "col_index": 4},
                {"type": "row_growth_rate", "target_column": 4, "current_column": 2, "previous_column": 3},
                {(5, 4)},
                {(5, 2), (5, 3)},
            ),
            (
                "ratio by rows",
                {"row_index": 5, "col_index": 2},
                {
                    "type": "row_ratio_by_rows",
                    "target_row": 5,
                    "numerator_row": 6,
                    "denominator_rows": [7, 8],
                    "columns": [2],
                },
                {(5, 2)},
                {(6, 2), (7, 2), (8, 2)},
            ),
            (
                "weighted average",
                {"row_index": 1, "col_index": 2},
                {
                    "type": "weighted_average",
                    "target_row": 1,
                    "target_column": 2,
                    "operand_rows": [3, 4],
                    "value_column": 2,
                    "weight_column": 3,
                },
                {(1, 2)},
                {(3, 2), (3, 3), (4, 2), (4, 3)},
            ),
            (
                "year over year rate",
                {"row_index": 5, "col_index": 4},
                {"type": "row_year_over_year_rate", "target_row": 5, "source_row": 6, "columns": [2, 3, 4]},
                {(5, 4)},
                {(6, 3), (6, 4)},
            ),
            (
                "paired year over year rate",
                {"row_index": 5, "col_index": 4},
                {
                    "type": "row_year_over_year_rate",
                    "row_pairs": [{"target_row": 5, "source_row": 6}],
                    "columns": [2, 3, 4],
                },
                {(5, 4)},
                {(6, 3), (6, 4)},
            ),
            (
                "paired year over year rate with dash values",
                {"row_index": 2, "col_index": 2},
                {
                    "type": "row_year_over_year_rate",
                    "row_pairs": [{"target_row": 2, "source_row": 1}],
                    "columns": [1, 2],
                },
                {(2, 2)},
                {(1, 1), (1, 2)},
            ),
            (
                "year rows rate",
                {"row_index": 5, "col_index": 3},
                {"type": "year_rows_change_rate", "value_column": 1, "rate_column": 3, "row_indices": [4, 5]},
                {(5, 3)},
                {(4, 1), (5, 1)},
            ),
            (
                "year rows amount",
                {"row_index": 5, "col_index": 2},
                {"type": "year_rows_change_amount", "value_column": 1, "change_column": 2, "row_indices": [4, 5]},
                {(5, 2)},
                {(4, 1), (5, 1)},
            ),
            (
                "split table operands",
                {"row_index": 5, "col_index": None},
                {"type": "cross_split_operand_row", "operand_columns": [2, 5, 8]},
                set(),
                {(5, 2), (5, 5), (5, 8)},
            ),
            (
                "cross table total",
                {"row_index": 5, "col_index": 1},
                {
                    "type": "cross_table_row_sum",
                    "target_column": 1,
                    "operand_columns": [2],
                    "source_table_code": "1-2-2 표2",
                    "source_column": 1,
                },
                {(5, 1)},
                {(5, 2)},
            ),
            (
                "cross table weighted average",
                {"row_index": 1, "col_index": 1},
                {
                    "type": "cross_table_weighted_average",
                    "target_row": 1,
                    "target_column": 1,
                    "value_column": 1,
                    "row_pairs": [
                        {"value_row": 2, "weight_row": 3},
                        {"value_row": 3, "weight_row": 4},
                    ],
                },
                {(1, 1)},
                {(2, 1), (3, 1)},
            ),
        ]

        for name, row, spec, targets, related in cases:
            with self.subTest(name=name):
                self.assert_highlight_cells(row, spec, targets=targets, related=related)

    def test_dynamic_cumulative_relation_highlights_the_latest_year_cell(self) -> None:
        row = {"row_index": 1, "col_index": 1}
        spec = {
            "type": "cell_relation_sum",
            "comparisons": [
                {
                    "target": {"row_selector": "cumulative_total", "column": 1},
                    "operand_cells": [{"row_selector": "latest_year", "column": 1}],
                }
            ],
        }
        matrix = [
            [{"text_value": "구분"}, {"text_value": "활용처"}],
            [{"text_value": "누적 계"}, {"text_value": "141"}],
            [{"text_value": "2025"}, {"text_value": "103"}],
            [{"text_value": "2026"}, {"text_value": "141"}],
        ]

        cells = highlight_cells_for(row, spec, matrix)  # type: ignore[arg-type]
        targets = {(cell.row_index, cell.col_index) for cell in cells if cell.role == "target"}
        related = {(cell.row_index, cell.col_index) for cell in cells if cell.role == "related"}

        self.assertEqual(targets, {(1, 1)})
        self.assertEqual(related, {(3, 1)})


if __name__ == "__main__":
    unittest.main()
