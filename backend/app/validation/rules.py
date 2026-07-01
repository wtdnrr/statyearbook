from __future__ import annotations

from abc import ABC, abstractmethod
import re

from app.validation.models import (
    ValidationIssueRecord,
    ValidationTable,
    clean_display_text,
    format_number,
    normalize_text,
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
        blocked = ["평균", "비율", "비중", "증감", "증감률", "잔액율", "율", "%", "rate", "average", "ratio"]
        return not any(keyword in normalized for keyword in blocked)

    def _matches(self, current: float, expected: float) -> bool:
        tolerance = 0.5 if current.is_integer() and expected.is_integer() else 0.05
        return abs(current - expected) <= tolerance


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
                if abs(difference) <= 0.5:
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


DEFAULT_RULES: list[ValidationRule] = [
    RequiredMetadataRule(),
    RowLabelRequiredRule(),
    RegionTotalSumRule(),
    HeaderFormulaRule(),
]
