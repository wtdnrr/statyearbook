from __future__ import annotations

import re
from dataclasses import dataclass

from app.validation.models import (
    ValidationCheckRecord,
    ValidationIssueRecord,
    ValidationTable,
    clean_display_text,
    normalize_text,
)


PART_SUFFIX_RE = re.compile(r"\s+표\s*\d+$")


@dataclass(frozen=True)
class CrossTableValidationResult:
    issues: list[ValidationIssueRecord]
    checks: list[ValidationCheckRecord]


class AdjacentDuplicateTableRule:
    """Detect likely source/import errors across neighboring logical tables."""

    rule_id = "structure.adjacent_duplicate_table"
    check_type = "표 구조 확인"
    check_label = "인접 표 중복 확인"

    def evaluate(self, tables: list[ValidationTable]) -> CrossTableValidationResult:
        issues: list[ValidationIssueRecord] = []
        checks: list[ValidationCheckRecord] = []
        previous_by_parent: dict[str, ValidationTable] = {}

        for table in tables:
            base_code = base_table_code(table.code)
            parent_code = parent_table_code(base_code)
            previous = previous_by_parent.get(parent_code)

            if previous and base_table_code(previous.code) != base_code:
                similarity = matrix_similarity(previous, table)
                if similarity >= 0.92 and titles_differ(previous, table):
                    check = self._check(previous, table, similarity)
                    checks.append(check)
                    issues.append(issue_from_check(check))

            if parent_code:
                previous_by_parent[parent_code] = table

        return CrossTableValidationResult(issues=issues, checks=checks)

    def _check(
        self,
        previous: ValidationTable,
        table: ValidationTable,
        similarity: float,
    ) -> ValidationCheckRecord:
        return ValidationCheckRecord(
            table_id=table.id,
            rule_id=self.rule_id,
            check_type=self.check_type,
            check_label=self.check_label,
            location="표 전체",
            current_value=f"{previous.code} {previous.title}와 {similarity * 100:.1f}% 유사",
            expected_value="표 제목과 일치하는 독립 표 데이터",
            difference="중복 또는 표 경계 오류 의심",
            status="오류 의심",
            severity="critical",
            detail=(
                f"{table.code} {table.title} 표의 본문이 바로 앞 표인 "
                f"{previous.code} {previous.title}와 거의 같습니다. 원천 문서 또는 DB에서 "
                "서로 다른 표 제목에 같은 표 데이터가 연결되었는지 확인하세요."
            ),
            row_index=None,
            col_index=None,
            formula=None,
            profile_id=None,
            confidence=0.96,
        )


def issue_from_check(check: ValidationCheckRecord) -> ValidationIssueRecord:
    return ValidationIssueRecord(
        table_id=check.table_id,
        rule_id=check.rule_id,
        issue_type=check.check_type,
        location=check.location,
        current_value=check.current_value,
        expected_value=check.expected_value,
        difference=check.difference,
        severity=check.severity,
        detail=check.detail,
        row_index=check.row_index,
        col_index=check.col_index,
        status=check.status,
        formula=check.formula,
    )


def base_table_code(code: str) -> str:
    return PART_SUFFIX_RE.sub("", code).strip()


def parent_table_code(code: str) -> str:
    if code.startswith("부록 "):
        match = re.match(r"^(부록\s+\d+)-\d+$", code)
        return match.group(1) if match else ""

    parts = code.split("-")
    return "-".join(parts[:-1]) if len(parts) > 1 else ""


def titles_differ(left: ValidationTable, right: ValidationTable) -> bool:
    return normalize_text(left.title) != normalize_text(right.title)


def matrix_similarity(left: ValidationTable, right: ValidationTable) -> float:
    left_matrix = left.matrix
    right_matrix = right.matrix
    if not left_matrix or not right_matrix:
        return 0.0

    if len(left_matrix) != len(right_matrix):
        return 0.0

    compared = 0
    matched = 0
    for row_index, left_row in enumerate(left_matrix):
        right_row = right_matrix[row_index]
        if len(left_row) != len(right_row):
            return 0.0
        for col_index, left_cell in enumerate(left_row):
            right_cell = right_row[col_index]
            left_text = comparable_text(left_cell.text_value if left_cell else "")
            right_text = comparable_text(right_cell.text_value if right_cell else "")
            if not left_text and not right_text:
                continue
            compared += 1
            if left_text == right_text:
                matched += 1

    return matched / compared if compared else 0.0


def comparable_text(value: str) -> str:
    return normalize_text(clean_display_text(value))


DEFAULT_CROSS_TABLE_RULES = [AdjacentDuplicateTableRule()]
