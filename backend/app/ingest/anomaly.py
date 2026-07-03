from __future__ import annotations

import re
from typing import Any, Iterable


PART_SUFFIX_RE = re.compile(r"\s+표\s*\d+$")


def annotate_adjacent_duplicate_tables(tables: Iterable[Any], *, threshold: float = 0.92) -> list[Any]:
    annotated = list(tables)
    previous_by_parent: dict[str, Any] = {}

    for table in annotated:
        base_code = base_table_code(table.code)
        parent_code = parent_table_code(base_code)
        previous = previous_by_parent.get(parent_code)

        if previous and base_table_code(previous.code) != base_code:
            similarity = table_similarity(previous, table)
            if similarity >= threshold and normalize_title(previous.title) != normalize_title(table.title):
                table.notes.append(
                    "#원천 확인 필요) "
                    f"{table.code} {table.title} 표의 본문과 헤더가 "
                    f"{previous.code} {previous.title} 표와 {similarity * 100:.1f}% 유사합니다. "
                    "원천 문서 또는 DB에서 제목과 표 데이터가 섞였는지 확인하세요."
                )

        if parent_code:
            previous_by_parent[parent_code] = table

    return annotated


def base_table_code(code: str) -> str:
    return PART_SUFFIX_RE.sub("", code).strip()


def parent_table_code(code: str) -> str:
    if code.startswith("부록 "):
        match = re.match(r"^(부록\s+\d+)-\d+$", code)
        return match.group(1) if match else ""

    parts = code.split("-")
    return "-".join(parts[:-1]) if len(parts) > 1 else ""


def table_similarity(left: Any, right: Any) -> float:
    left_matrix = normalized_matrix(left)
    right_matrix = normalized_matrix(right)
    if not left_matrix or not right_matrix or len(left_matrix) != len(right_matrix):
        return 0.0

    compared = 0
    matched = 0
    for row_index, left_row in enumerate(left_matrix):
        right_row = right_matrix[row_index]
        if len(left_row) != len(right_row):
            return 0.0
        for col_index, left_value in enumerate(left_row):
            right_value = right_row[col_index]
            if not left_value and not right_value:
                continue
            compared += 1
            if left_value == right_value:
                matched += 1

    return matched / compared if compared else 0.0


def normalized_matrix(table: Any) -> list[list[str]]:
    rows: list[list[str]] = []
    max_cols = 0
    for part in table.parts:
        for source_row in part.matrix:
            row = [normalize_cell(cell) for cell in source_row]
            if not any(row):
                continue
            rows.append(row)
            max_cols = max(max_cols, len(row))
    return [row + [""] * (max_cols - len(row)) for row in rows]


def normalize_cell(value: str) -> str:
    return re.sub(r"[\s·,._\-()/%]+", "", value).lower()


def normalize_title(value: str) -> str:
    return normalize_cell(value)
