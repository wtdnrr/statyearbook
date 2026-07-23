from __future__ import annotations

from typing import Any

from app.db.connection import DatabaseRow
from app.models.report import ValidationIssue
from app.validation.highlights import ResolvedHighlights, resolve_highlights


DISPLAY_CHECK_TYPE_MAP = {
    "증감액 검수": "합계 검수",
    "증감률 검수": "비율 검수",
    "평균 검수": "비율 검수",
}


def display_check_type(check_type: str) -> str:
    return DISPLAY_CHECK_TYPE_MAP.get(check_type, check_type)


def validation_issue_from_row(
    row: DatabaseRow,
    *,
    issue_id: str,
    check_type: str,
    header_count: int,
    matrix: list[list[Any]] | None,
    spec: dict[str, Any] | None,
    include_highlights: bool = True,
) -> ValidationIssue:
    highlights = (
        resolve_highlights(row, spec=spec, header_count=header_count, matrix=matrix)
        if include_highlights
        else ResolvedHighlights(scope="none", cells=[], rows=[], focus_cell=None)
    )
    return ValidationIssue(
        id=issue_id,
        rule_id=row["rule_id"],
        type=display_check_type(check_type),
        location=row["location"],
        row_index=row["row_index"],
        col_index=row["col_index"],
        current_value=row["current_value"],
        expected_value=row["expected_value"],
        difference=row["difference"],
        status=row["status"],
        severity=row["severity"],
        detail=row["detail"],
        formula=row["formula"],
        highlight_scope=highlights.scope,
        highlight_cells=highlights.cells,
        highlight_rows=highlights.rows,
        focus_cell=highlights.focus_cell,
    )
