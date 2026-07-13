from __future__ import annotations

import re
from dataclasses import dataclass

from app.validation.models import (
    ValidationCheckRecord,
    ValidationIssueRecord,
    ValidationTable,
    clean_display_text,
    format_number,
    normalize_text,
)
from app.validation.rules import (
    cell_number,
    cell_text,
    column_count,
    combined_row_label,
    leading_label_columns,
)


PART_SUFFIX_RE = re.compile(r"\s+표\s*\d+$")
PART_NUMBER_RE = re.compile(r"\s+표\s*(\d+)$")
SINGLE_MEASURE_SPLIT_KEY = "__single_measure_split__"


@dataclass(frozen=True)
class CrossTableValidationResult:
    issues: list[ValidationIssueRecord]
    checks: list[ValidationCheckRecord]


class AdjacentDuplicateTableRule:
    """Detect likely source/import errors across neighboring logical tables."""

    rule_id = "structure.adjacent_duplicate_table"
    check_type = "표 제목/본문 확인"
    check_label = "인접 표 제목-본문 혼합 확인"

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
            expected_value=f"{table.code} {table.title} 제목과 일치하는 독립 표 데이터",
            difference="제목-본문 혼합 또는 표 경계 오류 의심",
            status="오류 의심",
            severity="critical",
            detail=(
                f"{table.code} {table.title} 표의 본문과 헤더가 바로 앞 표인 "
                f"{previous.code} {previous.title}의 내용과 거의 같습니다. 현재 표 제목은 "
                f"'{table.title}'이지만 실제 표 항목은 '{previous.title}' 계열로 보이므로, "
                "원천 문서 또는 DB에서 제목과 표 데이터가 섞였는지 확인하세요."
            ),
            row_index=None,
            col_index=None,
            formula=None,
            profile_id=None,
            confidence=0.96,
        )


class SplitPartRowTotalRule:
    """Validate row totals whose detail columns are split across table parts."""

    rule_id = "cross.split_part_row_total"
    check_type = "합계 검수"

    def evaluate(self, tables: list[ValidationTable]) -> CrossTableValidationResult:
        issues: list[ValidationIssueRecord] = []
        checks: list[ValidationCheckRecord] = []

        for _, parts in split_part_groups(tables).items():
            if len(parts) < 2:
                continue

            target_part = parts[0]
            target_columns = split_total_columns(target_part)
            if not target_columns:
                continue

            part_rows = {
                part.code: rows_by_label(part)
                for part in parts
            }
            for target_col in target_columns:
                metric_key = split_metric_key(target_part, target_col)
                if not metric_key:
                    continue

                same_part_operand_cols = detail_columns_for_metric(target_part, metric_key, exclude_total=True)
                all_operand_columns = {
                    part.code: detail_columns_for_metric(part, metric_key, exclude_total=True)
                    for part in parts
                }
                if sum(len(columns) for columns in all_operand_columns.values()) < 2:
                    continue

                candidate_checks: list[ValidationCheckRecord] = []
                candidate_issues: list[ValidationIssueRecord] = []
                for row_index, row in target_part.data_rows():
                    row_label = row_label_key(target_part, row)
                    if not row_label:
                        continue

                    current = additive_value(row, target_col, blank_as_zero=True)
                    operand_values: list[float] = []
                    matched_part_rows: dict[str, int] = {}
                    missing_part = False
                    for part in parts:
                        part_row_index = part_rows.get(part.code, {}).get(row_label)
                        if part_row_index is None or part_row_index >= len(part.matrix):
                            missing_part = True
                            break
                        matched_part_rows[part.code] = part_row_index
                        part_row = part.matrix[part_row_index]
                        for col_index in all_operand_columns.get(part.code, []):
                            value = additive_value(part_row, col_index, blank_as_zero=True)
                            if value is None:
                                missing_part = True
                                break
                            operand_values.append(value)
                        if missing_part:
                            break

                    if current is None or missing_part or len(operand_values) < 2:
                        continue

                    expected = sum(operand_values)
                    passed = abs(current - expected) <= 1.0
                    check = split_row_total_check(
                        target_part,
                        parts,
                        row_index=row_index,
                        target_col=target_col,
                        metric_key=metric_key,
                        same_part_operand_cols=same_part_operand_cols,
                        current=current,
                        expected=expected,
                        passed=passed,
                    )
                    candidate_checks.append(check)
                    if not passed:
                        candidate_issues.append(issue_from_check(check))

                    for part in parts[1:]:
                        part_row_index = matched_part_rows.get(part.code)
                        part_operand_columns = all_operand_columns.get(part.code, [])
                        if part_row_index is None or not part_operand_columns:
                            continue
                        candidate_checks.append(
                            split_operand_part_check(
                                target_part,
                                part,
                                parts,
                                row_index=part_row_index,
                                target_col=target_col,
                                metric_key=metric_key,
                                operand_cols=part_operand_columns,
                                current=current,
                                expected=expected,
                                passed=passed,
                            )
                        )

                if split_row_total_candidate_is_reliable(candidate_checks):
                    checks.extend(candidate_checks)
                    issues.extend(candidate_issues)

        return CrossTableValidationResult(issues=issues, checks=checks)


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


def part_number(code: str) -> int | None:
    match = PART_NUMBER_RE.search(code)
    return int(match.group(1)) if match else None


def split_part_groups(tables: list[ValidationTable]) -> dict[str, list[ValidationTable]]:
    groups: dict[str, list[ValidationTable]] = {}
    for table in tables:
        number = part_number(table.code)
        if number is None:
            continue
        groups.setdefault(base_table_code(table.code), []).append(table)
    return {
        code: sorted(parts, key=lambda table: part_number(table.code) or 0)
        for code, parts in groups.items()
        if len(parts) >= 2
    }


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


def header_value(table: ValidationTable, header_row_index: int, col_index: int) -> str:
    if header_row_index >= len(table.matrix):
        return ""
    row = table.matrix[header_row_index]
    value = clean_display_text(cell_text(row, col_index))
    if value:
        return value
    for left_col_index in range(col_index - 1, -1, -1):
        left_value = clean_display_text(cell_text(row, left_col_index))
        if left_value:
            return left_value
    return ""


def metric_column_key(table: ValidationTable, col_index: int) -> str:
    if table.header_count <= 1:
        return normalize_text(header_value(table, 0, col_index))
    return normalize_text(header_value(table, table.header_count - 1, col_index))


def split_metric_key(table: ValidationTable, col_index: int) -> str:
    if table.header_count <= 1 and col_index in split_total_columns(table):
        detail_columns = split_single_measure_detail_columns(table, exclude_total=True)
        if detail_columns:
            return SINGLE_MEASURE_SPLIT_KEY
    return metric_column_key(table, col_index)


def split_total_columns(table: ValidationTable) -> list[int]:
    columns: list[int] = []
    for col_index in range(1, column_count(table)):
        top_header = header_value(table, 0, col_index)
        if not top_header:
            continue
        normalized = normalize_text(top_header)
        if normalized.startswith("합계") or normalized in {"계", "total"} or "total" in normalized:
            columns.append(col_index)
    return columns


def detail_columns_for_metric(table: ValidationTable, metric_key: str, *, exclude_total: bool) -> list[int]:
    if metric_key == SINGLE_MEASURE_SPLIT_KEY:
        return split_single_measure_detail_columns(table, exclude_total=exclude_total)

    columns: list[int] = []
    total_columns = set(split_total_columns(table)) if exclude_total else set()
    for col_index in range(1, column_count(table)):
        if col_index in total_columns:
            continue
        if metric_column_key(table, col_index) == metric_key:
            columns.append(col_index)
    return columns


def split_single_measure_detail_columns(table: ValidationTable, *, exclude_total: bool) -> list[int]:
    label_columns = set(leading_label_columns(table))
    total_columns = set(split_total_columns(table)) if exclude_total else set()
    columns: list[int] = []
    for col_index in range(column_count(table)):
        if col_index in label_columns or col_index in total_columns:
            continue
        if split_additive_detail_column(table, col_index):
            columns.append(col_index)
    return columns


def split_additive_detail_column(table: ValidationTable, col_index: int) -> bool:
    if not clean_display_text(header_value(table, 0, col_index)):
        return False

    for _, row in table.data_rows():
        if additive_value(row, col_index, blank_as_zero=False) is not None:
            return True
    return False


def rows_by_label(table: ValidationTable) -> dict[str, int]:
    rows: dict[str, int] = {}
    for row_index, row in table.data_rows():
        key = row_label_key(table, row)
        if key:
            rows[key] = row_index
    return rows


def row_label_key(table: ValidationTable, row: list) -> str:
    return normalize_text(combined_row_label(table, row) or table.row_label(row))


def additive_value(row: list, col_index: int, *, blank_as_zero: bool) -> float | None:
    value = cell_number(row, col_index)
    if value is not None:
        return value
    if col_index >= len(row) or row[col_index] is None:
        return None
    text = clean_display_text(cell_text(row, col_index))
    if text in {"-", "－", "―"} or (blank_as_zero and not text):
        return 0.0
    return None


def split_row_total_candidate_is_reliable(checks: list[ValidationCheckRecord]) -> bool:
    if len(checks) < 5:
        return False
    passed = sum(1 for check in checks if check.status == "정상")
    return passed / len(checks) >= 0.6


def split_row_total_check(
    table: ValidationTable,
    parts: list[ValidationTable],
    *,
    row_index: int,
    target_col: int,
    metric_key: str,
    same_part_operand_cols: list[int],
    current: float,
    expected: float,
    passed: bool,
) -> ValidationCheckRecord:
    row = table.matrix[row_index] if row_index < len(table.matrix) else []
    row_label = combined_row_label(table, row) or table.row_label(row) or f"{row_index + 1}행"
    metric_label = clean_display_text(header_value(table, table.header_count - 1, target_col))
    target_label = metric_label or metric_key
    if metric_key == SINGLE_MEASURE_SPLIT_KEY:
        check_label = f"분할표 합계: {target_label} = 표1·표2 세부 항목 합계"
        location = f"{clean_display_text(row_label)} {target_label}"
        formula = f"{target_label} = 표1·표2 세부 항목 합계"
    else:
        check_label = f"분할표 합계: 합계 {target_label} = 하위표 세부 항목 합계"
        location = f"{clean_display_text(row_label)} 합계 {target_label}"
        formula = f"합계 {target_label} = 표1·표2 세부 {target_label} 항목 합계"

    detail = (
        f"{base_table_code(table.code)}은 표가 {len(parts)}개로 나뉘어 있어, "
        f"{target_label} 합계는 표1과 나머지 하위표의 같은 행 세부 항목을 모두 더해 검수했습니다."
    )
    return ValidationCheckRecord(
        table_id=table.id,
        rule_id=split_part_rule_id("target", target_col, same_part_operand_cols, metric_key),
        check_type=SplitPartRowTotalRule.check_type,
        check_label=check_label,
        location=location,
        current_value=format_number(current),
        expected_value=format_number(expected),
        difference=None if passed else format_number(current - expected),
        status="정상" if passed else "오류 의심",
        severity="info" if passed else "critical",
        detail=detail,
        row_index=row_index,
        col_index=target_col,
        formula=formula,
        profile_id=None,
        confidence=0.98,
    )


def split_operand_part_check(
    target_table: ValidationTable,
    part: ValidationTable,
    parts: list[ValidationTable],
    *,
    row_index: int,
    target_col: int,
    metric_key: str,
    operand_cols: list[int],
    current: float,
    expected: float,
    passed: bool,
) -> ValidationCheckRecord:
    row = part.matrix[row_index] if row_index < len(part.matrix) else []
    row_label = combined_row_label(part, row) or part.row_label(row) or f"{row_index + 1}행"
    target_label = clean_display_text(header_value(target_table, target_table.header_count - 1, target_col)) or metric_key
    part_label = f"표{part_number(part.code) or ''}".strip()
    detail = (
        f"{base_table_code(target_table.code)}은 표가 {len(parts)}개로 나뉘어 있어, "
        f"{part_label}의 같은 행 지역 값도 {target_label} 합계 검수에 포함했습니다."
    )

    return ValidationCheckRecord(
        table_id=part.id,
        rule_id=split_part_rule_id("operand", target_col, operand_cols, metric_key),
        check_type=SplitPartRowTotalRule.check_type,
        check_label=f"분할표 합계 참여: {part_label} 지역 값",
        location=f"{clean_display_text(row_label)} {part_label} 지역 값",
        current_value=format_number(current),
        expected_value=format_number(expected),
        difference=None if passed else format_number(current - expected),
        status="정상" if passed else "오류 의심",
        severity="info" if passed else "critical",
        detail=detail,
        row_index=row_index,
        col_index=None,
        formula=f"{target_label} = 표1·표2 세부 항목 합계",
        profile_id=None,
        confidence=0.98,
    )


def split_part_rule_id(role: str, target_col: int, related_cols: list[int], metric_key: str) -> str:
    related = ",".join(str(col_index) for col_index in related_cols)
    return f"{SplitPartRowTotalRule.rule_id}:role={role}:target={target_col}:related={related}:metric={metric_key}"


DEFAULT_CROSS_TABLE_RULES = [AdjacentDuplicateTableRule(), SplitPartRowTotalRule()]
