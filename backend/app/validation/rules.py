from __future__ import annotations

from abc import ABC, abstractmethod
import re

from app.validation.models import (
    ValidationIssueRecord,
    ValidationTable,
    clean_display_text,
    format_number,
    normalize_text,
    parse_numeric_text,
)


REGION_NAMES = {
    "서울",
    "부산",
    "대구",
    "인천",
    "광주",
    "대전",
    "울산",
    "세종",
    "경기",
    "강원",
    "충북",
    "충남",
    "전북",
    "전남",
    "경북",
    "경남",
    "제주",
}


SUM_RATIO_MIN_TOLERANCE = 1.0
TOTAL_LABEL_KEYWORDS = ("계", "합계", "총계", "소계")
RATIO_KEYWORDS = ("비율", "비중", "증감률", "잔액율", "잔액률", "율", "%", "rate", "ratio", "percent", "percentage")
NON_ADDITIVE_KEYWORDS = (
    "평균",
    "비율",
    "비중",
    "증감률",
    "잔액율",
    "잔액률",
    "율",
    "%",
    "rate",
    "average",
    "ratio",
    "percent",
    "percentage",
    "1인당",
    "perperson",
    "percapita",
    "eachworker",
    "assignedtoeach",
)


def column_count(table: ValidationTable) -> int:
    return max((len(row) for row in table.matrix), default=0)


def cell_text(row: list, col_index: int) -> str:
    cell = row[col_index] if col_index < len(row) else None
    return cell.text_value if cell else ""


def cell_number(row: list, col_index: int) -> float | None:
    cell = row[col_index] if col_index < len(row) else None
    if cell is None:
        return None
    if cell.numeric_value is not None:
        return cell.numeric_value
    return parse_numeric_text(cell.text_value)


def leading_label_columns(table: ValidationTable) -> list[int]:
    cached = getattr(table, "_leading_label_columns_cache", None)
    if cached is not None:
        return list(cached)

    rows = [row for _, row in table.data_rows()]
    labels: list[int] = []

    for col_index in range(column_count(table)):
        values = [cell_text(row, col_index) for row in rows if cell_text(row, col_index).strip()]
        if not values:
            if labels:
                labels.append(col_index)
                continue
            break
        numeric_count = sum(1 for row in rows if cell_number(row, col_index) is not None)
        numeric_ratio = numeric_count / max(len(values), 1)
        header_text = table.column_text(col_index)
        year_like_count = sum(
            1
            for value in values
            if re.fullmatch(r"[’']?\d{2,4}(?:\s*\([^)]*\))?", clean_display_text(value))
        )
        if col_index == 0 and ("연도" in header_text or re.search(r"\byear\b", header_text, re.IGNORECASE)) and year_like_count / len(values) >= 0.7:
            labels.append(col_index)
            continue
        if labels and is_schedule_descriptor_column(header_text):
            continue
        if numeric_ratio <= 0.25:
            labels.append(col_index)
            continue
        break

    resolved = [0] if len(labels) > 3 else labels or [0]
    table._leading_label_columns_cache = tuple(resolved)
    return list(resolved)


def combined_row_label(table: ValidationTable, row: list) -> str:
    parts: list[str] = []
    for col_index in leading_label_columns(table):
        value = clean_display_text(cell_text(row, col_index))
        if value and value not in parts:
            parts.append(value)
    return " ".join(parts)


def is_total_like(value: str) -> bool:
    normalized = normalize_text(value)
    return any(normalized.startswith(keyword) for keyword in TOTAL_LABEL_KEYWORDS)


def is_subtotal_like(value: str) -> bool:
    return normalize_text(value).startswith("소계") or "subtotal" in normalize_text(value)


def is_ratio_like(value: str) -> bool:
    normalized = normalize_text(value)
    if any(keyword in normalized for keyword in ("비율", "비중", "증감률", "잔액율", "잔액률", "율")):
        return True
    return bool(re.search(r"\b(rate|ratio|percent|percentage)\b", value, re.IGNORECASE))


def is_schedule_descriptor_column(value: str) -> bool:
    normalized = normalize_text(value)
    if any(keyword in normalized for keyword in ("운영일자", "일자", "일시", "날짜", "기간")):
        return True
    return bool(re.search(r"\b(dates?|period|schedule)\b", value, re.IGNORECASE))


def is_additive_column_label(value: str) -> bool:
    normalized = normalize_text(value)
    if any(keyword in normalized for keyword in ("평균", "비율", "비중", "증감률", "잔액율", "잔액률", "율", "1인당", "피해내용", "내용", "비고", "remarks")):
        return False
    return not bool(
        re.search(
            r"\b(rate|average|ratio|percent|percentage|per\s+person|per\s+capita|each\s+worker|assigned\s+to\s+each|details?|remarks?)\b",
            value,
            re.IGNORECASE,
        )
    )


def header_stack(table: ValidationTable, col_index: int) -> list[str]:
    stack: list[str] = []
    for row in table.matrix[: table.header_count]:
        value = clean_display_text(cell_text(row, col_index))
        stack.append(value)
    return stack


def header_parent_key(table: ValidationTable, col_index: int) -> tuple[str, ...]:
    stack = [value for value in header_stack(table, col_index)[:-1] if value]
    return tuple(stack)


def leaf_header(table: ValidationTable, col_index: int) -> str:
    stack = [value for value in header_stack(table, col_index) if value]
    return stack[-1] if stack else table.column_text(col_index)


def values_match(current: float, expected: float, *, tolerance: float = 1.0) -> bool:
    return abs(current - expected) <= tolerance


def sum_ratio_values_match(current: float, expected: float, *, tolerance: float = 1.0) -> bool:
    return values_match(current, expected, tolerance=max(tolerance, SUM_RATIO_MIN_TOLERANCE))


class ValidationRule(ABC):
    rule_id: str
    issue_type: str

    @abstractmethod
    def validate(self, table: ValidationTable) -> list[ValidationIssueRecord]:
        raise NotImplementedError

    def issue(
        self,
        table: ValidationTable,
        *,
        location: str,
        current_value: str,
        expected_value: str | None,
        difference: str | None,
        severity: str,
        detail: str,
        row_index: int | None = None,
        col_index: int | None = None,
        formula: str | None = None,
    ) -> ValidationIssueRecord:
        return ValidationIssueRecord(
            table_id=table.id,
            rule_id=self.rule_id,
            issue_type=self.issue_type,
            location=location,
            current_value=current_value,
            expected_value=expected_value,
            difference=difference,
            severity=severity,
            detail=detail,
            row_index=row_index,
            col_index=col_index,
            formula=formula,
        )


class RequiredMetadataRule(ValidationRule):
    rule_id = "metadata.required"
    issue_type = "메타정보 확인"

    def validate(self, table: ValidationTable) -> list[ValidationIssueRecord]:
        checks = [
            ("단위", table.unit, "표 단위"),
            ("기준일", table.base_date, "기준일"),
            ("출처", table.source, "담당 부서 또는 출처"),
        ]
        issues: list[ValidationIssueRecord] = []

        for label, value, expected in checks:
            if value.strip():
                continue
            issues.append(
                self.issue(
                    table,
                    location=f"메타정보 {label}",
                    current_value="없음",
                    expected_value=expected,
                    difference="누락",
                    severity="warning",
                    detail=f"{table.code} {table.title} 표의 {label} 정보가 비어 있습니다. 원 시스템 메타데이터 매핑 또는 원문 추출 결과를 확인하세요.",
                )
            )

        return issues


class RowLabelRequiredRule(ValidationRule):
    rule_id = "cell.row_label_required"
    issue_type = "빈값 확인"

    def validate(self, table: ValidationTable) -> list[ValidationIssueRecord]:
        issues: list[ValidationIssueRecord] = []

        for row_index, row in table.data_rows():
            first_cell = row[0] if row else None
            if first_cell and first_cell.text_value.strip():
                continue

            populated_cells = [cell for cell in row[1:] if cell and cell.text_value.strip()]
            if len(populated_cells) < 2:
                continue

            issues.append(
                self.issue(
                    table,
                    location=f"{row_index + 1}행 1열",
                    current_value="",
                    expected_value="항목명",
                    difference="빈값",
                    severity="warning",
                    detail="데이터가 있는 행의 첫 번째 항목명이 비어 있습니다. 행 병합, 줄바꿈, 표 구조화 과정에서 항목명이 누락되었는지 확인하세요.",
                    row_index=row_index,
                    col_index=0,
                )
            )
            if len(issues) >= 5:
                break

        return issues


class RowTotalColumnRule(ValidationRule):
    rule_id = "sum.row_total_column"
    issue_type = "합계 불일치"

    def validate(self, table: ValidationTable) -> list[ValidationIssueRecord]:
        issues: list[ValidationIssueRecord] = []
        label_columns = set(leading_label_columns(table))
        numeric_columns = [
            col_index
            for col_index in range(column_count(table))
            if col_index not in label_columns and is_additive_column_label(table.column_text(col_index))
        ]

        for target_col in numeric_columns:
            if not self._is_total_column(table, target_col):
                continue

            peer_columns = self._peer_columns(table, target_col, numeric_columns)
            if len(peer_columns) < 2:
                continue

            for row_index, row in table.data_rows():
                if self._looks_like_year_label(combined_row_label(table, row)):
                    continue

                current_value = cell_number(row, target_col)
                if current_value is None:
                    continue

                values = [
                    cell_number(row, col_index)
                    for col_index in self._dedupe_peer_columns(table, row, peer_columns)
                ]
                values = [value for value in values if value is not None]
                if len(values) < 2:
                    continue

                expected_value = sum(values)
                difference = current_value - expected_value
                if sum_ratio_values_match(current_value, expected_value):
                    continue

                row_label = combined_row_label(table, row) or f"{row_index + 1}행"
                col_label = table.column_text(target_col)
                issues.append(
                    self.issue(
                        table,
                        location=f"{row_label} {col_label}",
                        current_value=format_number(current_value),
                        expected_value=format_number(expected_value),
                        difference=format_number(difference),
                        severity="critical",
                        detail="같은 행의 세부 항목을 합산한 결과가 계/합계 열의 값과 다릅니다. 성별, 유형별, 구성항목별 합계 입력값을 확인하세요.",
                        row_index=row_index,
                        col_index=target_col,
                        formula="동일 행 세부 항목 합계",
                    )
                )
                if len(issues) >= 30:
                    return issues

        return issues

    def _is_total_column(self, table: ValidationTable, col_index: int) -> bool:
        label = normalize_text(leaf_header(table, col_index))
        full_label = normalize_text(table.column_text(col_index))
        return (
            label in {"계", "합계", "총계", "소계", "total", "subtotal"}
            or full_label.endswith("계total")
            or full_label.endswith("합계total")
        )

    def _peer_columns(
        self,
        table: ValidationTable,
        target_col: int,
        numeric_columns: list[int],
    ) -> list[int]:
        parent_key = header_parent_key(table, target_col)
        peer_columns = [
            col_index
            for col_index in numeric_columns
            if col_index != target_col
            and not self._is_total_column(table, col_index)
            and header_parent_key(table, col_index) == parent_key
        ]
        return peer_columns if parent_key and len(peer_columns) >= 2 else []

    def _dedupe_peer_columns(
        self,
        table: ValidationTable,
        row: list,
        peer_columns: list[int],
    ) -> list[int]:
        deduped: list[int] = []
        seen: set[tuple[str, float | None]] = set()
        for col_index in peer_columns:
            key = (normalize_text(leaf_header(table, col_index)), cell_number(row, col_index))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(col_index)
        return deduped

    def _looks_like_year_label(self, label: str) -> bool:
        return bool(re.fullmatch(r"\d{4}[^\d]*", clean_display_text(label)))


class ColumnTotalRowRule(ValidationRule):
    rule_id = "sum.column_total_row"
    issue_type = "합계 불일치"

    def validate(self, table: ValidationTable) -> list[ValidationIssueRecord]:
        rows = table.data_rows()
        if len(rows) < 3:
            return []

        label_columns = set(leading_label_columns(table))
        numeric_columns = [
            col_index
            for col_index in range(column_count(table))
            if col_index not in label_columns and is_additive_column_label(table.column_text(col_index))
        ]
        if not numeric_columns:
            return []

        issues: list[ValidationIssueRecord] = []
        for row_position, (row_index, row) in enumerate(rows):
            row_label = combined_row_label(table, row)
            if not is_total_like(row_label):
                continue

            child_rows = self._child_rows(table, rows, row_position, row_label)
            if len(child_rows) < 2:
                continue

            for col_index in numeric_columns:
                current_value = cell_number(row, col_index)
                if current_value is None:
                    continue

                values = [cell_number(child_row, col_index) for _, child_row in child_rows]
                values = [value for value in values if value is not None]
                if len(values) < 2:
                    continue

                expected_value = sum(values)
                difference = current_value - expected_value
                if sum_ratio_values_match(current_value, expected_value):
                    continue

                issues.append(
                    self.issue(
                        table,
                        location=f"{row_label} {table.column_text(col_index)}",
                        current_value=format_number(current_value),
                        expected_value=format_number(expected_value),
                        difference=format_number(difference),
                        severity="critical",
                        detail="하위 행을 합산한 결과가 계/합계/소계 행의 값과 다릅니다. 표의 그룹 범위, 원 시스템 추출 범위, 수기 보정 여부를 확인하세요.",
                        row_index=row_index,
                        col_index=col_index,
                        formula="하위 행 합계",
                    )
                )
                if len(issues) >= 30:
                    return issues

        return issues

    def _child_rows(
        self,
        table: ValidationTable,
        rows: list[tuple[int, list]],
        row_position: int,
        row_label: str,
    ) -> list[tuple[int, list]]:
        if not is_subtotal_like(row_label):
            return []

        group_key = self._group_key(table, rows[row_position][1])
        if not group_key:
            return []

        child_rows: list[tuple[int, list]] = []
        for item in rows[row_position + 1 :]:
            current_label = combined_row_label(table, item[1])
            if self._group_key(table, item[1]) != group_key:
                break
            if is_total_like(current_label):
                continue
            child_rows.append(item)
        return child_rows

    def _group_key(self, table: ValidationTable, row: list) -> str:
        for col_index in leading_label_columns(table):
            value = clean_display_text(cell_text(row, col_index))
            if value and not is_total_like(value):
                return value
        return ""


class ExplicitRatioFormulaRule(ValidationRule):
    rule_id = "formula.explicit_ratio"
    issue_type = "비율 확인"

    RATIO_RE = re.compile(r"\(([a-z])\s*/\s*([a-z])(?:\s*\*\s*100)?\)", re.IGNORECASE)
    SYMBOL_RE = re.compile(r"\(([a-z])(?:\s*=|\))", re.IGNORECASE)

    def validate(self, table: ValidationTable) -> list[ValidationIssueRecord]:
        symbol_to_column = self._symbol_columns(table)
        issues: list[ValidationIssueRecord] = []

        for target_col in range(column_count(table)):
            label = table.column_text(target_col)
            match = self.RATIO_RE.search(label)
            if not match:
                continue

            numerator_symbol = match.group(1).lower()
            denominator_symbol = match.group(2).lower()
            if numerator_symbol not in symbol_to_column or denominator_symbol not in symbol_to_column:
                continue

            numerator_col = symbol_to_column[numerator_symbol]
            denominator_col = symbol_to_column[denominator_symbol]
            multiplier = 100 if ("*" in match.group(0) or is_ratio_like(label)) else 1

            for row_index, row in table.data_rows():
                current_value = cell_number(row, target_col)
                numerator = cell_number(row, numerator_col)
                denominator = cell_number(row, denominator_col)
                if current_value is None or numerator is None or denominator in {None, 0}:
                    continue

                expected_value = numerator / denominator * multiplier
                difference = current_value - expected_value
                if sum_ratio_values_match(current_value, expected_value, tolerance=0.15):
                    continue

                row_label = combined_row_label(table, row) or f"{row_index + 1}행"
                issues.append(
                    self.issue(
                        table,
                        location=f"{row_label} {label}",
                        current_value=format_number(current_value),
                        expected_value=format_number(expected_value),
                        difference=format_number(difference),
                        severity="warning",
                        detail="헤더에 표시된 비율 계산식으로 다시 계산한 값과 현재 표 값이 다릅니다. 분모, 반올림 기준, 단위 표기 여부를 확인하세요.",
                        row_index=row_index,
                        col_index=target_col,
                        formula=f"{numerator_symbol}/{denominator_symbol}" + ("*100" if multiplier == 100 else ""),
                    )
                )
                if len(issues) >= 30:
                    return issues

        return issues

    def _symbol_columns(self, table: ValidationTable) -> dict[str, int]:
        symbol_to_column: dict[str, int] = {}
        for col_index in range(column_count(table)):
            label = table.column_text(col_index)
            for match in HeaderFormulaRule.FORMULA_RE.finditer(label):
                symbol_to_column.setdefault(match.group(1).lower(), col_index)
            for match in self.SYMBOL_RE.finditer(label):
                symbol_to_column.setdefault(match.group(1).lower(), col_index)
        return symbol_to_column


class ChangeAmountRateRule(ValidationRule):
    rule_id = "formula.change_amount_rate"
    issue_type = "증감률 확인"

    def validate(self, table: ValidationTable) -> list[ValidationIssueRecord]:
        current_col = self._symbol_column(table, "a")
        previous_col = self._symbol_column(table, "b")
        if current_col is None or previous_col is None:
            return []

        amount_cols = [
            col_index
            for col_index in range(column_count(table))
            if self._has_change_formula(table.column_text(col_index))
            and any(keyword in normalize_text(table.column_text(col_index)) for keyword in ("금액", "amount", "증감", "change"))
            and not is_ratio_like(table.column_text(col_index))
        ]
        rate_cols = [
            col_index
            for col_index in range(column_count(table))
            if self._has_change_formula(table.column_text(col_index))
            and is_ratio_like(table.column_text(col_index))
        ]

        issues: list[ValidationIssueRecord] = []
        for row_index, row in table.data_rows():
            current = cell_number(row, current_col)
            previous = cell_number(row, previous_col)
            if current is None or previous is None:
                continue

            amount = current - previous
            row_label = combined_row_label(table, row) or f"{row_index + 1}행"

            for col_index in amount_cols:
                actual = cell_number(row, col_index)
                if actual is None or values_match(actual, amount):
                    continue
                issues.append(
                    self.issue(
                        table,
                        location=f"{row_label} {table.column_text(col_index)}",
                        current_value=format_number(actual),
                        expected_value=format_number(amount),
                        difference=format_number(actual - amount),
                        severity="critical",
                        detail="현재년도(A)와 전년도(B)의 차이를 다시 계산한 값과 증감 금액이 다릅니다.",
                        row_index=row_index,
                        col_index=col_index,
                        formula="A-B",
                    )
                )

            if previous == 0:
                continue
            candidate_rates = [amount / previous * 100]
            if current != 0:
                candidate_rates.append(amount / current * 100)
            for col_index in rate_cols:
                actual = cell_number(row, col_index)
                if actual is None:
                    continue
                closest_rate = min(candidate_rates, key=lambda value: abs(actual - value))
                if values_match(actual, closest_rate, tolerance=0.15):
                    continue
                issues.append(
                    self.issue(
                        table,
                        location=f"{row_label} {table.column_text(col_index)}",
                        current_value=format_number(actual),
                        expected_value=format_number(closest_rate),
                        difference=format_number(actual - closest_rate),
                        severity="warning",
                        detail="증감 금액을 전년도 값으로 나눈 증감률 계산값과 현재 표 값이 다릅니다.",
                        row_index=row_index,
                        col_index=col_index,
                        formula="(A-B)/B*100 또는 (A-B)/A*100",
                    )
                )

            if len(issues) >= 30:
                return issues

        return issues

    def _symbol_column(self, table: ValidationTable, symbol: str) -> int | None:
        pattern = re.compile(rf"\({symbol}\)", re.IGNORECASE)
        for col_index in range(column_count(table)):
            if pattern.search(table.column_text(col_index)):
                return col_index
        return None

    def _has_change_formula(self, label: str) -> bool:
        compact = re.sub(r"\s+", "", label).lower()
        return "(a-b)" in compact


class RowRatioRule(ValidationRule):
    rule_id = "formula.row_ratio"
    issue_type = "비율 확인"

    def validate(self, table: ValidationTable) -> list[ValidationIssueRecord]:
        rows = table.data_rows()
        issues: list[ValidationIssueRecord] = []

        for row_position, (row_index, row) in enumerate(rows):
            ratio_label = combined_row_label(table, row)
            if not self._is_ratio_row(ratio_label):
                continue

            numerator_item = self._find_numerator(rows, row_position, ratio_label, table)
            denominator_item = self._find_denominator(rows, row_position, ratio_label, table)
            if numerator_item is None or denominator_item is None:
                continue

            _, numerator_row = numerator_item
            _, denominator_row = denominator_item
            for col_index in range(1, column_count(table)):
                current_value = cell_number(row, col_index)
                numerator = cell_number(numerator_row, col_index)
                denominator = cell_number(denominator_row, col_index)
                if current_value is None or numerator is None or denominator in {None, 0}:
                    continue

                expected_value = numerator / denominator * 100
                difference = current_value - expected_value
                if sum_ratio_values_match(current_value, expected_value, tolerance=0.15):
                    continue

                issues.append(
                    self.issue(
                        table,
                        location=f"{ratio_label} {table.column_text(col_index)}",
                        current_value=format_number(current_value),
                        expected_value=format_number(expected_value),
                        difference=format_number(difference),
                        severity="warning",
                        detail="비율 행의 분자/분모 후보를 기준으로 다시 계산한 값과 현재 표 값이 다릅니다. 비율 산식과 반올림 기준을 확인하세요.",
                        row_index=row_index,
                        col_index=col_index,
                        formula=f"{combined_row_label(table, numerator_row)} / {combined_row_label(table, denominator_row)} * 100",
                    )
                )
                if len(issues) >= 30:
                    return issues

        return issues

    def _is_ratio_row(self, label: str) -> bool:
        normalized = normalize_text(label)
        blocked = ("증감률", "증가율", "성장률", "rateofgrowth", "increase", "decrease")
        has_ratio_signal = (
            "비율" in normalized
            or "비중" in normalized
            or bool(re.search(r"\b(percent|percentage)\b", label, re.IGNORECASE))
        )
        return has_ratio_signal and not any(keyword in normalized for keyword in blocked)

    def _find_numerator(
        self,
        rows: list[tuple[int, list]],
        row_position: int,
        ratio_label: str,
        table: ValidationTable,
    ) -> tuple[int, list] | None:
        normalized_ratio = normalize_text(ratio_label)
        for item in reversed(rows[:row_position]):
            label = combined_row_label(table, item[1])
            normalized_label = normalize_text(label)
            if not normalized_label or is_ratio_like(label):
                continue
            if normalized_label in normalized_ratio or any(keyword in normalized_label for keyword in ("여성", "female")):
                return item
        return rows[row_position - 1] if row_position > 0 else None

    def _find_denominator(
        self,
        rows: list[tuple[int, list]],
        row_position: int,
        ratio_label: str,
        table: ValidationTable,
    ) -> tuple[int, list] | None:
        normalized_ratio = normalize_text(ratio_label)
        for item in reversed(rows[:row_position]):
            label = combined_row_label(table, item[1])
            normalized_label = normalize_text(label)
            if not normalized_label or is_ratio_like(label):
                continue
            if any(keyword in normalized_label for keyword in ("증감", "increase", "decrease")):
                continue
            has_total_signal = (
                "전체" in normalized_label
                or normalized_label.startswith(("계", "합계", "총계"))
                or bool(re.search(r"\btotal\b", label, re.IGNORECASE))
            )
            if has_total_signal:
                shared_prefix = self._shared_prefix_length(normalized_ratio, normalized_label)
                if shared_prefix >= 2 or "total" in normalized_label or "전체" in normalized_label:
                    return item
        return None

    def _shared_prefix_length(self, left: str, right: str) -> int:
        count = 0
        for left_char, right_char in zip(left, right):
            if left_char != right_char:
                break
            count += 1
        return count


class RegionTotalSumRule(ValidationRule):
    rule_id = "sum.region_total"
    issue_type = "합계 불일치"

    def validate(self, table: ValidationTable) -> list[ValidationIssueRecord]:
        if not self._looks_like_region_table(table):
            return []

        rows = table.data_rows()
        total_index = self._find_total_row(rows, table)
        if total_index is None:
            return []

        total_matrix_row, total_row = rows[total_index]
        child_rows = rows[total_index + 1 :]
        region_child_rows = [item for item in child_rows if self._is_region_label(table.row_label(item[1]))]
        if len(region_child_rows) < 8:
            return []

        issues: list[ValidationIssueRecord] = []

        for col_index in range(1, max((len(row) for _, row in rows), default=0)):
            label = table.column_text(col_index)
            if not self._is_additive_column(label):
                continue

            total_cell = total_row[col_index] if col_index < len(total_row) else None
            current_value = total_cell.numeric_value if total_cell else None
            if current_value is None:
                continue

            values = [
                row[col_index].numeric_value
                for _, row in region_child_rows
                if col_index < len(row)
                and row[col_index]
                and row[col_index].numeric_value is not None
            ]
            if len(values) < 3:
                continue

            expected_value = sum(value for value in values if value is not None)
            difference = current_value - expected_value
            if self._matches(current_value, expected_value):
                continue

            issues.append(
                self.issue(
                    table,
                    location=f"{table.row_label(total_row)} {label}",
                    current_value=format_number(current_value),
                    expected_value=format_number(expected_value),
                    difference=format_number(difference),
                    severity="critical",
                    detail="지역별 세부 값을 합산한 결과가 표의 합계 값과 다릅니다. 원 시스템 값, 수기 보정 여부, 표 구조화 범위를 확인하세요.",
                    row_index=total_matrix_row,
                    col_index=col_index,
                    formula="지역별 세부 값 합계",
                )
            )

        return issues

    def _looks_like_region_table(self, table: ValidationTable) -> bool:
        first_header = normalize_text(table.column_text(0))
        if "지역" in first_header:
            return True

        labels = {table.row_label(row)[:2] for _, row in table.data_rows()}
        return len(labels & REGION_NAMES) >= 8

    def _find_total_row(
        self,
        rows: list[tuple[int, list]],
        table: ValidationTable,
    ) -> int | None:
        for index, (_, row) in enumerate(rows[:5]):
            if self._is_total_label(table.row_label(row)):
                return index
        return None

    def _is_total_label(self, value: str) -> bool:
        normalized = normalize_text(value)
        return normalized.startswith("계") or normalized.startswith("합계") or normalized.startswith("총계")

    def _is_region_label(self, value: str) -> bool:
        return clean_display_text(value)[:2] in REGION_NAMES

    def _is_additive_column(self, label: str) -> bool:
        normalized = normalize_text(label)
        blocked = ["평균", "비율", "비중", "증감", "증감률", "잔액율", "율", "%", "피해내용", "내용", "비고", "rate", "average", "ratio", "detail", "remark"]
        return not any(keyword in normalized for keyword in blocked)

    def _matches(self, current: float, expected: float) -> bool:
        tolerance = 0.5 if current.is_integer() and expected.is_integer() else 0.05
        return sum_ratio_values_match(current, expected, tolerance=tolerance)


class HeaderFormulaRule(ValidationRule):
    rule_id = "formula.header_arithmetic"
    issue_type = "계산식 확인"

    FORMULA_RE = re.compile(r"\(([a-z])\s*=\s*([a-z](?:\s*[+-]\s*[a-z])+)\)", re.IGNORECASE)
    SYMBOL_RE = re.compile(r"\(([a-z])\)", re.IGNORECASE)

    def validate(self, table: ValidationTable) -> list[ValidationIssueRecord]:
        symbol_to_column = self._symbol_columns(table)
        formulas = self._formulas(table, symbol_to_column)
        if not formulas:
            return []

        issues: list[ValidationIssueRecord] = []
        for target_symbol, expression, target_col in formulas:
            operand_columns = self._operand_columns(expression, symbol_to_column)
            if not operand_columns:
                continue

            for row_index, row in table.data_rows():
                target_cell = row[target_col] if target_col < len(row) else None
                current = target_cell.numeric_value if target_cell else None
                if current is None:
                    continue

                expected = self._evaluate(expression, row, symbol_to_column)
                if expected is None:
                    continue

                difference = current - expected
                if abs(difference) <= SUM_RATIO_MIN_TOLERANCE:
                    continue

                label = table.column_text(target_col)
                row_label = table.row_label(row) or f"{row_index + 1}행"
                issues.append(
                    self.issue(
                        table,
                        location=f"{row_label} {label}",
                        current_value=format_number(current),
                        expected_value=format_number(expected),
                        difference=format_number(difference),
                        severity="critical" if abs(difference) > 1 else "warning",
                        detail="헤더에 표시된 계산식으로 다시 계산한 값과 현재 표 값이 다릅니다. 계산식, 반올림 기준, 원 시스템 값을 확인하세요.",
                        row_index=row_index,
                        col_index=target_col,
                        formula=f"{target_symbol}={expression}",
                    )
                )

                if len(issues) >= 20:
                    return issues

        return issues

    def _symbol_columns(self, table: ValidationTable) -> dict[str, int]:
        symbol_to_column: dict[str, int] = {}
        for col_index in range(max((len(row) for row in table.matrix), default=0)):
            label = table.column_text(col_index)
            for match in self.FORMULA_RE.finditer(label):
                symbol_to_column.setdefault(match.group(1).lower(), col_index)
            for match in self.SYMBOL_RE.finditer(label):
                symbol_to_column.setdefault(match.group(1).lower(), col_index)
        return symbol_to_column

    def _formulas(self, table: ValidationTable, symbol_to_column: dict[str, int]) -> list[tuple[str, str, int]]:
        formulas: list[tuple[str, str, int]] = []
        seen: set[tuple[str, str, int]] = set()
        for col_index in range(max((len(row) for row in table.matrix), default=0)):
            label = table.column_text(col_index)
            for match in self.FORMULA_RE.finditer(label):
                target_symbol = match.group(1).lower()
                if target_symbol not in symbol_to_column:
                    continue
                formula = (target_symbol, match.group(2).lower().replace(" ", ""), symbol_to_column[target_symbol])
                if formula in seen:
                    continue
                seen.add(formula)
                formulas.append(formula)
        return formulas

    def _operand_columns(self, expression: str, symbol_to_column: dict[str, int]) -> list[int]:
        symbols = re.findall(r"[a-z]", expression.lower())
        if not symbols or any(symbol not in symbol_to_column for symbol in symbols):
            return []
        return [symbol_to_column[symbol] for symbol in symbols]

    def _evaluate(
        self,
        expression: str,
        row: list,
        symbol_to_column: dict[str, int],
    ) -> float | None:
        tokens = re.findall(r"[+-]?[a-z]", expression.lower())
        total = 0.0
        for token in tokens:
            sign = -1 if token.startswith("-") else 1
            symbol = token[-1]
            col_index = symbol_to_column[symbol]
            cell = row[col_index] if col_index < len(row) else None
            if cell is None or cell.numeric_value is None:
                return None
            total += sign * cell.numeric_value
        return total


class StaticKoreanSpellingRule(ValidationRule):
    rule_id = "spelling.ko.static"
    issue_type = "용어 제안"

    TERMS = {
        "잔액율": "잔액률",
    }

    def validate(self, table: ValidationTable) -> list[ValidationIssueRecord]:
        issues: list[ValidationIssueRecord] = []
        for row_index, row in enumerate(table.matrix):
            for col_index, cell in enumerate(row):
                if cell is None:
                    continue
                for current, expected in self.TERMS.items():
                    if current not in cell.text_value:
                        continue
                    issues.append(
                        self.issue(
                            table,
                            location=f"{row_index + 1}행 {col_index + 1}열",
                            current_value=current,
                            expected_value=expected,
                            difference="발간 표준 용어 확인",
                            severity="warning",
                            detail=f"국문 용어집 기준으로 '{current}'보다 '{expected}' 표기를 제안합니다. 실제 발간 기준 용어와 일치하는지 확인하세요.",
                            row_index=row_index,
                            col_index=col_index,
                        )
                    )
        return issues[:10]


class StaticEnglishSpellingRule(ValidationRule):
    rule_id = "spelling.en.static"
    issue_type = "영문 표기 확인"

    TERMS = {
        "Claasification": "Classification",
        "Claasifi-cation": "Classification",
        "Ele7ction": "Election",
        "Nuber": "Number",
    }

    def validate(self, table: ValidationTable) -> list[ValidationIssueRecord]:
        issues: list[ValidationIssueRecord] = []
        for row_index, row in enumerate(table.matrix):
            for col_index, cell in enumerate(row):
                if cell is None:
                    continue
                for current, expected in self.TERMS.items():
                    if current not in cell.text_value:
                        continue
                    issues.append(
                        self.issue(
                            table,
                            location=f"{row_index + 1}행 {col_index + 1}열",
                            current_value=current,
                            expected_value=expected,
                            difference="철자 확인",
                            severity="critical",
                            detail=f"영문 오탈자 사전에서 '{current}'는 '{expected}'로 교정하는 것이 적절합니다. 원문 이미지 또는 표준 영문명을 확인하세요.",
                            row_index=row_index,
                            col_index=col_index,
                        )
                    )
        return issues[:10]


DEFAULT_RULES: list[ValidationRule] = [
    RequiredMetadataRule(),
    RowLabelRequiredRule(),
    RowTotalColumnRule(),
    ColumnTotalRowRule(),
    RegionTotalSumRule(),
    HeaderFormulaRule(),
    ExplicitRatioFormulaRule(),
    ChangeAmountRateRule(),
    RowRatioRule(),
    StaticKoreanSpellingRule(),
    StaticEnglishSpellingRule(),
]
