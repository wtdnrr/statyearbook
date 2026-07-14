from __future__ import annotations

import unittest

from app.services.sqlite_report_service import highlight_cells_for


class ValidationHighlightTest(unittest.TestCase):
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
        ]

        for name, row, spec, targets, related in cases:
            with self.subTest(name=name):
                self.assert_highlight_cells(row, spec, targets=targets, related=related)


if __name__ == "__main__":
    unittest.main()
