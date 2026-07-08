from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
import re


@dataclass(frozen=True)
class ValidationCell:
    row_index: int
    col_index: int
    text_value: str
    numeric_value: float | None
    is_header: bool


@dataclass(frozen=True)
class ValidationIssueRecord:
    table_id: int
    rule_id: str
    issue_type: str
    location: str
    current_value: str
    expected_value: str | None
    difference: str | None
    severity: str
    detail: str
    row_index: int | None = None
    col_index: int | None = None
    status: str = "확인 필요"
    formula: str | None = None


@dataclass(frozen=True)
class ValidationCheckRecord:
    table_id: int
    rule_id: str
    check_type: str
    check_label: str
    location: str
    current_value: str
    expected_value: str | None
    difference: str | None
    status: str
    severity: str
    detail: str
    row_index: int | None = None
    col_index: int | None = None
    formula: str | None = None
    profile_id: int | None = None
    confidence: float | None = None


@dataclass
class ValidationTable:
    id: int
    report_id: int
    code: str
    title: str
    unit: str
    base_date: str
    source: str
    note: str
    cells: list[ValidationCell]

    @cached_property
    def header_count(self) -> int:
        header_rows = {cell.row_index for cell in self.cells if cell.is_header}
        if header_rows:
            return max(header_rows) + 1
        return 1 if self.matrix else 0

    @cached_property
    def matrix(self) -> list[list[ValidationCell | None]]:
        if not self.cells:
            return []

        max_row = max(cell.row_index for cell in self.cells)
        max_col = max(cell.col_index for cell in self.cells)
        matrix: list[list[ValidationCell | None]] = [
            [None for _ in range(max_col + 1)] for _ in range(max_row + 1)
        ]
        for cell in self.cells:
            matrix[cell.row_index][cell.col_index] = cell
        return matrix

    def data_rows(self) -> list[tuple[int, list[ValidationCell | None]]]:
        return list(enumerate(self.matrix[self.header_count :], start=self.header_count))

    def row_label(self, row: list[ValidationCell | None]) -> str:
        if not row:
            return ""
        return clean_display_text(row[0].text_value if row[0] else "")

    def column_text(self, col_index: int) -> str:
        parts: list[str] = []
        for row in self.matrix[: self.header_count]:
            if col_index >= len(row):
                continue
            value = row[col_index].text_value if row[col_index] else ""
            cleaned = clean_display_text(value)
            if cleaned and cleaned not in parts:
                parts.append(cleaned)
        return " / ".join(parts) or f"{col_index + 1}열"


def normalize_text(value: str) -> str:
    return re.sub(r"[\s·,._\-()/%]+", "", value).lower()


def restore_hyphenated_line_breaks(value: str) -> str:
    return re.sub(r"(?<=[A-Za-z])(?:\s+-\s*|-\s+)(?=[A-Za-z])", "", value)


def clean_display_text(value: str) -> str:
    restored = restore_hyphenated_line_breaks(value)
    return re.sub(r"\s+", " ", restored).strip()


def parse_numeric_text(value: str) -> float | None:
    cleaned = value.strip().replace(",", "").replace("%", "")
    if re.fullmatch(r"\([-+]?\d+(?:\.\d+)?\)", cleaned):
        cleaned = cleaned[1:-1]
    if not cleaned or cleaned in {"-", "－", "―"}:
        return None
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", cleaned):
        return float(cleaned)
    return None


def format_number(value: float) -> str:
    if value.is_integer():
        return f"{int(value):,}"
    return f"{value:,.1f}"
