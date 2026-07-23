from __future__ import annotations

from dataclasses import dataclass
import re
import sqlite3
from typing import Any

from app.db.connection import DatabaseRow
from app.models.report import ValidationHighlightCell, ValidationHighlightRow


@dataclass(frozen=True)
class ResolvedHighlights:
    scope: str
    cells: list[ValidationHighlightCell]
    rows: list[ValidationHighlightRow]
    focus_cell: ValidationHighlightCell | None


def resolve_highlights(
    row: DatabaseRow,
    *,
    spec: dict[str, Any] | None,
    header_count: int,
    matrix: list[list[Any]] | None,
) -> ResolvedHighlights:
    cells = highlight_cells_for(row, spec, matrix)
    focus = next((cell for cell in cells if cell.role == "target"), None)
    return ResolvedHighlights(
        scope=highlight_scope_for(row, spec, header_count),
        cells=cells,
        rows=highlight_rows_for(row, spec),
        focus_cell=focus or next(iter(cells), None),
    )


def int_or_none(value: Any) -> int | None:
    return int(value) if value is not None else None


def highlight_cell(
    row_index: int | None,
    col_index: int | None,
    role: str,
) -> ValidationHighlightCell | None:
    if row_index is None or col_index is None or row_index < 0 or col_index < 0:
        return None
    return ValidationHighlightCell(row_index=row_index, col_index=col_index, role=role)


def highlight_row(row_index: int | None, role: str) -> ValidationHighlightRow | None:
    if row_index is None or row_index < 0:
        return None
    return ValidationHighlightRow(row_index=row_index, role=role)


def unique_highlight_cells(
    cells: list[ValidationHighlightCell | None],
) -> list[ValidationHighlightCell]:
    merged: dict[tuple[int, int], ValidationHighlightCell] = {}
    for cell in cells:
        if cell is None:
            continue
        key = (cell.row_index, cell.col_index)
        current = merged.get(key)
        if current is None or cell.role == "target":
            merged[key] = cell
    return list(merged.values())


def spec_columns(spec: dict[str, Any] | None, key: str) -> list[int]:
    if not spec:
        return []
    return [int(value) for value in spec.get(key, []) if value is not None]


def spec_rows(spec: dict[str, Any] | None, key: str) -> list[int]:
    if not spec:
        return []
    return [int(value) for value in spec.get(key, []) if value is not None]


def relation_cell_for_view(
    descriptor: Any,
    matrix: list[list[Any]] | None,
) -> tuple[int, int] | None:
    """Resolve a profile relation cell for exact table-cell highlighting."""

    if not isinstance(descriptor, dict):
        return None
    column = int(descriptor.get("column", -1))
    if column < 0:
        return None

    if descriptor.get("row") is not None:
        row = int(descriptor.get("row", -1))
        return (row, column) if row >= 0 else None
    if not matrix:
        return None

    selector = str(descriptor.get("row_selector") or "")
    if selector == "latest_year":
        candidates: list[tuple[int, int]] = []
        for row_index, cells in enumerate(matrix):
            label = matrix_row_label(cells)
            match = re.fullmatch(r"(?:19|20)(\d{2})", label)
            if match:
                candidates.append((int(match.group(0)), row_index))
        if candidates:
            return max(candidates)[1], column

    if selector == "cumulative_total":
        for row_index, cells in enumerate(matrix):
            label = normalize_lookup_text(matrix_row_label(cells))
            if "누적계" in label or "cumulativetotal" in label:
                return row_index, column

    return None


def matrix_row_label(cells: list[Any]) -> str:
    if not cells or cells[0] is None:
        return ""
    return clean_table_cell_text(cells[0])


def clean_table_cell_text(cell: Any) -> str:
    if isinstance(cell, str):
        value = cell
    elif isinstance(cell, sqlite3.Row):
        value = cell["text_value"]
    elif isinstance(cell, dict):
        value = cell.get("text_value", "")
    else:
        value = str(cell or "")
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_lookup_text(value: str) -> str:
    return re.sub(r"[\s·,._\-()/%]+", "", value).lower()


def formula_previous_column(
    spec: dict[str, Any] | None,
    current_col: int | None,
) -> int | None:
    if spec is None or current_col is None:
        return None
    ordered_columns = sorted(spec_columns(spec, "columns"))
    previous_columns = [col_index for col_index in ordered_columns if col_index < current_col]
    return previous_columns[-1] if previous_columns else None


def highlight_cells_for(
    row: DatabaseRow,
    spec: dict[str, Any] | None,
    matrix: list[list[Any]] | None = None,
) -> list[ValidationHighlightCell]:
    row_index = int_or_none(row["row_index"])
    col_index = int_or_none(row["col_index"])
    if spec is None:
        return fallback_highlight_cells(row)

    rule_type = str(spec.get("type") or "")
    cells: list[ValidationHighlightCell | None] = []

    if rule_type in {"row_sum", "cross_table_row_sum"}:
        target_col = int(spec.get("target_column", col_index if col_index is not None else -1))
        cells.append(highlight_cell(row_index, target_col, "target"))
        cells.extend(
            highlight_cell(row_index, col, "related")
            for col in spec_columns(spec, "operand_columns")
        )
    elif rule_type == "cross_table_weighted_average":
        target_row = int(spec.get("target_row", row_index if row_index is not None else -1))
        target_col = int(spec.get("target_column", col_index if col_index is not None else -1))
        value_col = int(spec.get("value_column", target_col))
        cells.append(highlight_cell(target_row, target_col, "target"))
        for pair in spec.get("row_pairs", []):
            if isinstance(pair, dict):
                cells.append(highlight_cell(int(pair.get("value_row", -1)), value_col, "related"))
    elif rule_type == "cross_table_cell_match":
        cells.append(highlight_cell(row_index, col_index, "target"))
    elif rule_type == "cell_sum":
        target_row = int(spec.get("target_row", row_index if row_index is not None else -1))
        target_col = int(spec.get("target_column", col_index if col_index is not None else -1))
        cells.append(highlight_cell(target_row, target_col, "target"))
        for operand in spec.get("operand_cells", []):
            if isinstance(operand, dict):
                cells.append(
                    highlight_cell(
                        int(operand.get("row", -1)),
                        int(operand.get("column", -1)),
                        "related",
                    )
                )
    elif rule_type == "cell_relation_sum":
        for comparison in spec.get("comparisons", []):
            if not isinstance(comparison, dict):
                continue
            target = relation_cell_for_view(comparison.get("target"), matrix)
            if target is None:
                continue
            target_row, target_col = target
            if target_row != row_index or target_col != col_index:
                continue
            cells.append(highlight_cell(target_row, target_col, "target"))
            for operand in comparison.get("operand_cells", []):
                resolved_operand = relation_cell_for_view(operand, matrix)
                if resolved_operand is not None:
                    cells.append(highlight_cell(*resolved_operand, "related"))
            break
    elif rule_type == "row_arithmetic":
        target_col = int(spec.get("target_column", col_index if col_index is not None else -1))
        cells.append(highlight_cell(row_index, target_col, "target"))
        term_columns = [
            int(term.get("column", -1))
            for term in spec.get("terms", [])
            if isinstance(term, dict) and term.get("column") is not None
        ]
        cells.extend(highlight_cell(row_index, col, "related") for col in term_columns)
    elif rule_type == "row_ratio":
        target_col = int(spec.get("target_column", col_index if col_index is not None else -1))
        numerator_col = int(spec.get("numerator_column", -1))
        denominator_columns = spec_columns(spec, "denominator_columns")
        denominator_col = int(spec.get("denominator_column", -1))
        if not denominator_columns and denominator_col >= 0:
            denominator_columns = [denominator_col]
        cells.extend(
            [
                highlight_cell(row_index, target_col, "target"),
                highlight_cell(row_index, numerator_col, "related"),
            ]
        )
        cells.extend(
            highlight_cell(row_index, denominator_col, "related")
            for denominator_col in denominator_columns
        )
    elif rule_type == "column_share_ratio":
        target_col = int(spec.get("target_column", col_index if col_index is not None else -1))
        numerator_col = int(spec.get("numerator_column", -1))
        denominator_row = int(spec.get("denominator_row", -1))
        denominator_col = int(spec.get("denominator_column", numerator_col))
        cells.extend(
            [
                highlight_cell(row_index, target_col, "target"),
                highlight_cell(row_index, numerator_col, "related"),
                highlight_cell(denominator_row, denominator_col, "related"),
            ]
        )
    elif rule_type == "row_growth_rate":
        target_col = int(spec.get("target_column", col_index if col_index is not None else -1))
        current_col = int(spec.get("current_column", -1))
        previous_col = int(spec.get("previous_column", -1))
        cells.extend(
            [
                highlight_cell(row_index, target_col, "target"),
                highlight_cell(row_index, current_col, "related"),
                highlight_cell(row_index, previous_col, "related"),
            ]
        )
    elif rule_type in {"column_sum", "region_total"}:
        target_row = int(spec.get("target_row", row_index if row_index is not None else -1))
        rule_columns = spec_columns(spec, "columns")
        current_col = col_index if col_index is not None else (rule_columns[0] if rule_columns else -1)
        cells.append(highlight_cell(target_row, current_col, "target"))
        for operand_row in spec_rows(spec, "operand_rows"):
            cells.append(highlight_cell(operand_row, current_col, "related"))
    elif rule_type == "row_ratio_by_rows":
        target_row = int(spec.get("target_row", row_index if row_index is not None else -1))
        numerator_row = int(spec.get("numerator_row", -1))
        cells.extend(
            [
                highlight_cell(target_row, col_index, "target"),
                highlight_cell(numerator_row, col_index, "related"),
            ]
        )
        denominator_rows = spec_rows(spec, "denominator_rows")
        if not denominator_rows:
            denominator_rows = [int(spec.get("denominator_row", -1))]
        cells.extend(
            highlight_cell(denominator_row, col_index, "related")
            for denominator_row in denominator_rows
        )
    elif rule_type == "weighted_average":
        target_row = int(spec.get("target_row", row_index if row_index is not None else -1))
        target_col = int(spec.get("target_column", col_index if col_index is not None else -1))
        value_col = int(spec.get("value_column", -1))
        weight_col = int(spec.get("weight_column", -1))
        cells.append(highlight_cell(target_row, target_col, "target"))
        for operand_row in spec_rows(spec, "operand_rows"):
            cells.append(highlight_cell(operand_row, value_col, "related"))
            cells.append(highlight_cell(operand_row, weight_col, "related"))
    elif rule_type == "row_year_over_year_rate":
        target_row = row_index if row_index is not None else int(spec.get("target_row", -1))
        source_row = next(
            (
                int(pair.get("source_row", -1))
                for pair in spec.get("row_pairs", [])
                if isinstance(pair, dict) and int(pair.get("target_row", -1)) == target_row
            ),
            int(spec.get("source_row", -1)),
        )
        previous_col = formula_previous_column(spec, col_index)
        cells.extend(
            [
                highlight_cell(target_row, col_index, "target"),
                highlight_cell(source_row, col_index, "related"),
                highlight_cell(source_row, previous_col, "related"),
            ]
        )
    elif rule_type == "row_year_over_year_change_amount":
        target_row = int(spec.get("target_row", row_index if row_index is not None else -1))
        source_row = int(spec.get("source_row", -1))
        previous_col = formula_previous_column(spec, col_index)
        cells.extend(
            [
                highlight_cell(target_row, col_index, "target"),
                highlight_cell(source_row, col_index, "related"),
                highlight_cell(source_row, previous_col, "related"),
            ]
        )
    elif rule_type == "year_rows_change_rate":
        value_col = int(spec.get("value_column", -1))
        change_col = spec.get("change_column")
        change_col_value = int(change_col) if change_col is not None else None
        rate_col = int(spec.get("rate_column", -1))
        row_indices = sorted(spec_rows(spec, "row_indices"))
        previous_rows = [
            candidate
            for candidate in row_indices
            if row_index is not None and candidate < row_index
        ]
        previous_row = previous_rows[-1] if previous_rows else None
        if change_col_value is not None and col_index == change_col_value:
            cells.append(highlight_cell(row_index, change_col_value, "target"))
        elif col_index == rate_col:
            cells.append(highlight_cell(row_index, rate_col, "target"))
        else:
            cells.append(highlight_cell(row_index, col_index, "target"))
        cells.extend(
            [
                highlight_cell(row_index, value_col, "related"),
                highlight_cell(previous_row, value_col, "related"),
            ]
        )
    elif rule_type == "year_rows_change_amount":
        value_col = int(spec.get("value_column", -1))
        change_col = int(spec.get("change_column", col_index if col_index is not None else -1))
        row_indices = sorted(spec_rows(spec, "row_indices"))
        previous_rows = [
            candidate
            for candidate in row_indices
            if row_index is not None and candidate < row_index
        ]
        previous_row = previous_rows[-1] if previous_rows else None
        cells.extend(
            [
                highlight_cell(row_index, change_col, "target"),
                highlight_cell(row_index, value_col, "related"),
                highlight_cell(previous_row, value_col, "related"),
            ]
        )
    elif rule_type == "cross_split_operand_row":
        cells.extend(
            highlight_cell(row_index, col, "related")
            for col in spec_columns(spec, "operand_columns")
        )
    else:
        cells.append(highlight_cell(row_index, col_index, "target"))

    return unique_highlight_cells(cells)


def highlight_rows_for(
    row: DatabaseRow,
    spec: dict[str, Any] | None,
) -> list[ValidationHighlightRow]:
    if spec is None:
        return fallback_highlight_rows(row)
    # Profile-backed checks use exact cells. Row highlights remain only as a
    # fallback for legacy issue records that have no column coordinate.
    return []


def highlight_scope_for(
    row: DatabaseRow,
    spec: dict[str, Any] | None,
    header_count: int,
) -> str:
    row_index = int_or_none(row["row_index"])
    if spec is None:
        return fallback_highlight_scope(row, header_count)

    rule_type = str(spec.get("type") or "")
    if rule_type in {"unit_required", "metadata_required"}:
        return "metadata"
    if row_index is not None and row_index < header_count:
        return "header"
    if rule_type in {
        "row_sum",
        "cell_sum",
        "cell_relation_sum",
        "row_arithmetic",
        "row_ratio",
        "column_share_ratio",
        "row_growth_rate",
        "row_year_over_year_change_amount",
        "year_rows_change_amount",
        "cross_split_operand_row",
        "cross_table_row_sum",
    }:
        return "row"
    if rule_type in {
        "column_sum",
        "region_total",
        "row_ratio_by_rows",
        "cross_table_weighted_average",
        "weighted_average",
    }:
        return "column"
    if rule_type in {"spelling_static", "translation_static"}:
        return "header" if row_index is not None and row_index < header_count else "cell"
    return "cell" if row_index is not None and row["col_index"] is not None else "none"


def fallback_highlight_cells(row: DatabaseRow) -> list[ValidationHighlightCell]:
    cell = highlight_cell(
        int_or_none(row["row_index"]),
        int_or_none(row["col_index"]),
        "target",
    )
    return [cell] if cell else []


def fallback_highlight_rows(row: DatabaseRow) -> list[ValidationHighlightRow]:
    if int_or_none(row["col_index"]) is not None:
        return []
    row_highlight = highlight_row(int_or_none(row["row_index"]), "target")
    return [row_highlight] if row_highlight else []


def fallback_highlight_scope(row: DatabaseRow, header_count: int) -> str:
    row_index = int_or_none(row["row_index"])
    col_index = int_or_none(row["col_index"])
    if row_index is None or col_index is None:
        return "none"
    if row_index < header_count:
        return "header"
    return "cell"
