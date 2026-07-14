from __future__ import annotations

import re
from typing import Any

from app.validation.models import (
    ValidationCheckRecord,
    ValidationIssueRecord,
    ValidationTable,
    clean_display_text,
    format_number,
    normalize_text,
    parse_numeric_text,
)
from app.validation.profiles import ValidationProfile, is_calculation_row_label
from app.validation.rules import (
    ValidationRule,
    cell_number,
    cell_text,
    column_count,
    combined_row_label,
    is_total_like,
    leading_label_columns,
)


def median(values: list[float]) -> float:
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2


def contains_korean_term(text: str, term: str) -> bool:
    if term == "계":
        if re.search(r"(통\s*계\s*청|회\s*계|계\s*곡)", text):
            return False
        return bool(re.search(r"(^|[\s/·])계($|[\s/·])", text))
    if re.fullmatch(r"[가-힣]+", term):
        return bool(re.search(rf"(?<![가-힣A-Za-z]){re.escape(term)}(?![가-힣A-Za-z])", text))
    return term in text


KNOWN_SPELLING_TYPOS = ("Claasification", "Claasifi-cation", "Ele7ction", "Nuber")
SUM_RATIO_MIN_TOLERANCE = 1.0
SUM_RATIO_RULE_TYPES = {
    "region_total",
    "column_sum",
    "cell_sum",
    "row_sum",
    "row_arithmetic",
    "row_ratio",
    "column_share_ratio",
    "row_ratio_by_rows",
}


def contains_known_spelling_typo(text: str) -> bool:
    return any(term in text for term in KNOWN_SPELLING_TYPOS)


def is_repeated_projected_cell(table: ValidationTable, row_index: int, col_index: int, text: str) -> bool:
    if row_index <= 0 or col_index >= len(table.matrix[row_index - 1]):
        return False
    previous_cell = table.matrix[row_index - 1][col_index]
    if previous_cell is None:
        return False
    return clean_display_text(previous_cell.text_value) == text


def displayed_ratio_tolerance(
    row: list,
    col_index: int,
    *,
    base_tolerance: float,
    multiplier: float,
) -> float:
    if base_tolerance > 0.15 or col_index >= len(row):
        return base_tolerance
    cell = row[col_index]
    if cell is None:
        return base_tolerance
    text = clean_display_text(cell.text_value).replace(",", "").replace("%", "")
    if re.fullmatch(r"[-+]?\d+", text):
        return max(base_tolerance, 0.5)
    return base_tolerance


def is_sum_or_ratio_spec(spec: dict[str, Any]) -> bool:
    return spec.get("check_group") in {"sum", "ratio"} or str(spec.get("type") or "") in SUM_RATIO_RULE_TYPES


def sum_ratio_tolerance(spec: dict[str, Any], default: float) -> float:
    tolerance = float(spec.get("tolerance", default))
    if is_sum_or_ratio_spec(spec):
        return max(tolerance, SUM_RATIO_MIN_TOLERANCE)
    return tolerance


def displayed_sum_ratio_tolerance(
    row: list,
    col_index: int,
    spec: dict[str, Any],
    *,
    default: float,
    multiplier: float,
) -> float:
    return max(
        sum_ratio_tolerance(spec, default),
        displayed_ratio_tolerance(
            row,
            col_index,
            base_tolerance=float(spec.get("tolerance", default)),
            multiplier=multiplier,
        ),
    )


def additive_cell_number(row: list, col_index: int) -> float | None:
    value = cell_number(row, col_index)
    if value is not None:
        return value

    text = clean_display_text(cell_text(row, col_index))
    if text in {"-", "－", "―", "_", "＿"}:
        return 0.0
    return None


def additive_operand_cell_number(row: list, col_index: int) -> float | None:
    value = additive_cell_number(row, col_index)
    if value is not None:
        return value
    if col_index < len(row) and row[col_index] is not None and not clean_display_text(cell_text(row, col_index)):
        return 0.0
    return None


def additive_target_cell_number(row: list, col_index: int) -> float | None:
    return additive_cell_number(row, col_index)


class ProfileStateRule(ValidationRule):
    rule_id = "profile.state"
    issue_type = "검수 프로파일 확인"

    def __init__(self, profiles: dict[str, ValidationProfile]) -> None:
        self._profiles = profiles

    def validate(self, table: ValidationTable) -> list[ValidationIssueRecord]:
        profile = self._profiles.get(table.code)
        if profile is None:
            return [
                self.issue(
                    table,
                    location="검수 프로파일",
                    current_value="없음",
                    expected_value="표 구조 해석 프로파일",
                    difference="신규 작성 필요",
                    severity="warning",
                    detail="이 통계표에 적용할 검수 프로파일이 없습니다. 실제 서비스에서는 GPT API가 표 구조를 해석해 초안을 만들고 담당자가 승인해야 합니다.",
                )
            ]

        requires_llm_review = bool(profile.rules.get("requires_llm_review"))
        if not requires_llm_review:
            return []

        return [
            self.issue(
                table,
                location="검수 프로파일",
                current_value=f"{profile.source}/{profile.status}",
                expected_value="approved",
                difference="승인 필요",
                severity="warning",
                detail=f"{profile.notes} 구조 변경 가능성이 있어 GPT API 재해석 및 담당자 승인이 필요합니다.",
            )
        ]


class ProfileSpecRule(ValidationRule):
    rule_id = "profile.spec"
    issue_type = "프로파일 검수"

    def __init__(self, profiles: dict[str, ValidationProfile]) -> None:
        self._profiles = profiles

    def validate(self, table: ValidationTable) -> list[ValidationIssueRecord]:
        issues, _ = self.evaluate(table)
        return issues

    def evaluate(
        self,
        table: ValidationTable,
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        profile = self._profiles.get(table.code)
        if profile is None:
            return [], []

        issues: list[ValidationIssueRecord] = []
        checks: list[ValidationCheckRecord] = []
        for spec in profile.check_specs:
            if not self._should_execute(spec):
                continue
            rule_type = spec.get("type")
            if rule_type == "unit_required":
                spec_issues, spec_checks = self._validate_unit(table, profile, spec)
            elif rule_type == "metadata_required":
                spec_issues, spec_checks = self._validate_metadata(table, profile, spec)
            elif rule_type == "row_label_required":
                spec_issues, spec_checks = self._validate_row_labels(table, profile, spec)
            elif rule_type == "numeric_format":
                spec_issues, spec_checks = self._validate_numeric_format(table, profile, spec)
            elif rule_type == "year_sequence":
                spec_issues, spec_checks = self._validate_year_sequence(table, profile, spec)
            elif rule_type == "region_total":
                spec_issues, spec_checks = self._validate_column_sum(table, profile, spec)
            elif rule_type == "column_sum":
                spec_issues, spec_checks = self._validate_column_sum(table, profile, spec)
            elif rule_type == "cell_sum":
                spec_issues, spec_checks = self._validate_cell_sum(table, profile, spec)
            elif rule_type == "row_sum":
                spec_issues, spec_checks = self._validate_row_sum(table, profile, spec)
            elif rule_type == "row_arithmetic":
                spec_issues, spec_checks = self._validate_row_arithmetic(table, profile, spec)
            elif rule_type == "row_ratio":
                spec_issues, spec_checks = self._validate_row_ratio(table, profile, spec)
            elif rule_type == "column_share_ratio":
                spec_issues, spec_checks = self._validate_column_share_ratio(table, profile, spec)
            elif rule_type == "row_ratio_by_rows":
                spec_issues, spec_checks = self._validate_row_ratio_by_rows(table, profile, spec)
            elif rule_type == "weighted_average":
                spec_issues, spec_checks = self._validate_weighted_average(table, profile, spec)
            elif rule_type == "growth_rate_scan":
                spec_issues, spec_checks = self._validate_growth_rate_scan(table, profile, spec)
            elif rule_type == "row_growth_rate":
                spec_issues, spec_checks = self._validate_row_growth_rate(table, profile, spec)
            elif rule_type == "row_year_over_year_rate":
                spec_issues, spec_checks = self._validate_row_year_over_year_rate(table, profile, spec)
            elif rule_type == "row_year_over_year_change_amount":
                spec_issues, spec_checks = self._validate_row_year_over_year_change_amount(table, profile, spec)
            elif rule_type == "year_rows_change_rate":
                spec_issues, spec_checks = self._validate_year_rows_change_rate(table, profile, spec)
            elif rule_type == "year_rows_change_amount":
                spec_issues, spec_checks = self._validate_year_rows_change_amount(table, profile, spec)
            elif rule_type == "outlier_columns":
                spec_issues, spec_checks = self._validate_outliers(table, profile, spec)
            elif rule_type == "spelling_static":
                spec_issues, spec_checks = self._validate_static_spelling(table, profile, spec)
            elif rule_type == "terminology_static":
                spec_issues, spec_checks = self._validate_static_terminology(table, profile, spec)
            elif rule_type == "translation_static":
                spec_issues, spec_checks = self._validate_static_translation(table, profile, spec)
            elif rule_type == "title_translation_static":
                spec_issues, spec_checks = self._validate_title_translation(table, profile, spec)
            else:
                spec_issues, spec_checks = [], []

            issues.extend(spec_issues)
            checks.extend(spec_checks)

        return issues, checks

    def _should_execute(self, spec: dict[str, Any]) -> bool:
        if spec.get("execute") is False:
            return False
        confidence = float(spec.get("confidence", 1.0))
        if spec.get("check_group") == "sum":
            return confidence >= 0.6
        if spec.get("category") == "template" and confidence < 0.9:
            return False
        return True

    def _check_type(self, spec: dict[str, Any], fallback: str) -> str:
        return str(spec.get("check_type") or fallback)

    def _failure_status(self, spec: dict[str, Any]) -> str:
        return str(spec.get("failure_status") or ("오류 의심" if spec.get("severity") == "critical" else "확인 필요"))

    def _status(self, spec: dict[str, Any], passed: bool) -> str:
        return "정상" if passed else self._failure_status(spec)

    def _severity(self, spec: dict[str, Any], passed: bool) -> str:
        return "info" if passed else str(spec.get("severity") or "warning")

    def _check_from_pass_fail(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
        *,
        fallback_check_type: str,
        location: str,
        current_value: str,
        expected_value: str | None,
        difference: str | None,
        passed: bool,
        detail: str,
        row_index: int | None = None,
        col_index: int | None = None,
        formula: str | None = None,
    ) -> ValidationCheckRecord:
        return self._check_record(
            table,
            profile,
            spec,
            check_type=self._check_type(spec, fallback_check_type),
            location=location,
            current_value=current_value,
            expected_value=expected_value,
            difference=None if passed else difference,
            status=self._status(spec, passed),
            severity=self._severity(spec, passed),
            detail=detail,
            row_index=row_index,
            col_index=col_index,
            formula=formula,
        )

    def _validate_unit(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        current = table.unit.strip()
        expected = str(spec.get("expected_unit") or "").strip()
        has_unit = bool(current)
        matches_profile = not expected or normalize_text(current) == normalize_text(expected)
        passed = has_unit and matches_profile
        difference = "누락" if not has_unit else "프로파일 단위와 다름"
        if not has_unit:
            detail = "단위 메타데이터가 누락되어 있습니다. 원문 또는 원 시스템 DB의 단위 매핑을 확인하세요."
        elif not matches_profile:
            detail = "현재 단위가 저장된 검수 프로파일의 단위 기준과 다릅니다."
        else:
            detail = "단위가 입력되어 있고 저장된 검수 프로파일의 단위 기준과 일치합니다."
        check = self._check_from_pass_fail(
            table,
            profile,
            spec,
            fallback_check_type="단위 검수",
            location="메타정보 단위",
            current_value=current or "없음",
            expected_value=expected or "단위 입력",
            difference=difference,
            passed=passed,
            detail=detail,
        )
        return ([] if passed else [self._issue_from_check(check)]), [check]

    def _validate_metadata(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        values = {
            "unit": ("단위", table.unit),
            "base_date": ("기준일", table.base_date),
            "source": ("출처", table.source),
        }
        issues: list[ValidationIssueRecord] = []
        checks: list[ValidationCheckRecord] = []

        for field in spec.get("fields", []):
            label, value = values.get(field, (field, ""))
            passed = bool(str(value).strip())
            check = self._check_record(
                table,
                profile,
                spec,
                check_type=self._check_type(spec, "빈값 검수"),
                location=f"메타정보 {label}",
                current_value=str(value).strip() or "없음",
                expected_value=label,
                difference=None if passed else "누락",
                status=self._status(spec, passed),
                severity=self._severity(spec, passed),
                detail=f"{label} 메타데이터가 {'입력되어 있습니다' if passed else '누락되어 있습니다'}.",
            )
            checks.append(check)
            if not passed:
                issues.append(self._issue_from_check(check))

        return issues, checks

    def _validate_row_labels(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        missing_rows: list[int] = []
        for row_index, row in table.data_rows():
            label = combined_row_label(table, row) or table.row_label(row)
            populated_cells = [cell for cell in row[1:] if cell and cell.text_value.strip()]
            if not label.strip() and len(populated_cells) >= 2:
                missing_rows.append(row_index)

        passed = not missing_rows
        check = self._check_record(
            table,
            profile,
            spec,
            check_type=self._check_type(spec, "빈값 검수"),
            location="데이터 행 항목명",
            current_value=f"누락 {len(missing_rows)}건",
            expected_value="누락 0건",
            difference=None if passed else f"{len(missing_rows)}건",
            status=self._status(spec, passed),
            severity=self._severity(spec, passed),
            detail="데이터가 있는 행의 항목명 빈값 여부를 확인했습니다.",
            row_index=missing_rows[0] if missing_rows else None,
            col_index=0 if missing_rows else None,
        )
        return ([] if passed else [self._issue_from_check(check)]), [check]

    def _validate_numeric_format(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        suspicious: list[tuple[int, int, str]] = []
        for row_index, row in table.data_rows():
            for col_index, cell in enumerate(row):
                if cell is None or not cell.text_value.strip():
                    continue
                text = cell.text_value.strip()
                if any(char.isdigit() for char in text) and cell.numeric_value is None:
                    looks_like_broken_number = text.startswith("`") or text.endswith("`")
                    looks_like_plain_numeric = bool(re.fullmatch(r"[`+\-.,\d\s%]+", text))
                    if looks_like_broken_number or (looks_like_plain_numeric and parse_numeric_text(text) is None):
                        suspicious.append((row_index, col_index, text))

        passed = not suspicious
        first = suspicious[0] if suspicious else None
        check = self._check_record(
            table,
            profile,
            spec,
            check_type=self._check_type(spec, "계산용 숫자 형식 검수"),
            location=f"{first[0] + 1}행 {first[1] + 1}열" if first else "전체 숫자형 셀",
            current_value=first[2] if first else "모든 숫자형 셀 정상",
            expected_value="합계·비율·증감률 계산에 사용할 수 있는 숫자 형식",
            difference=None if passed else f"{len(suspicious)}건",
            status=self._status(spec, passed),
            severity=self._severity(spec, passed),
            detail="숫자처럼 보이는 값이 실제 계산에 사용할 수 있는 숫자로 파싱되는지 확인했습니다.",
            row_index=first[0] if first else None,
            col_index=first[1] if first else None,
        )
        return ([] if passed else [self._issue_from_check(check)]), [check]

    def _validate_year_sequence(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        years: list[int] = []
        for row_index in spec.get("row_indices", []):
            row = table.matrix[row_index] if row_index < len(table.matrix) else []
            label = combined_row_label(table, row) or table.row_label(row)
            if label.strip().isdigit():
                years.append(int(label.strip()))
        for col_index in spec.get("columns", []):
            label = clean_display_text(table.column_text(int(col_index)))
            if label.isdigit():
                years.append(int(label))

        unique_years = sorted(set(years))
        passed = len(unique_years) >= 2
        expected = f"{unique_years[0]}~{unique_years[-1]}" if passed else "연도 2개 이상"
        check = self._check_record(
            table,
            profile,
            spec,
            check_type=self._check_type(spec, "빈값 검수"),
            location="연도 행/열",
            current_value=", ".join(str(year) for year in unique_years[:8]) + ("..." if len(unique_years) > 8 else ""),
            expected_value=expected,
            difference=None,
            status=self._status(spec, passed),
            severity=self._severity(spec, passed),
            detail="연도별 추이표로 판단된 표에서 연도 축을 인식했습니다.",
        )
        return ([] if passed else [self._issue_from_check(check)]), [check]

    def _validate_row_sum(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        target_col = int(spec.get("target_column", -1))
        operand_columns = [int(col_index) for col_index in spec.get("operand_columns", [])]
        if not self._valid_columns(table, [target_col, *operand_columns]):
            return [], []

        issues: list[ValidationIssueRecord] = []
        checks: list[ValidationCheckRecord] = []
        allowed_rows = {int(row_index) for row_index in spec.get("row_indices", [])}
        for row_index, row in table.data_rows():
            if allowed_rows and row_index not in allowed_rows:
                continue
            if is_calculation_row_label(table, row):
                continue
            current = additive_target_cell_number(row, target_col)
            operands = [additive_operand_cell_number(row, col_index) for col_index in operand_columns]
            if current is None or any(value is None for value in operands):
                continue

            expected = sum(value for value in operands if value is not None)
            passed = abs(current - expected) <= sum_ratio_tolerance(spec, 1.0)
            check = self._calculation_check(
                table,
                profile,
                spec,
                check_type="합계 검수",
                row_index=row_index,
                col_index=target_col,
                current=current,
                expected=expected,
                passed=passed,
                detail="통계표별 검수 기준에 정의된 행 합계 산식을 확인했습니다.",
            )
            checks.append(check)
            if not passed:
                issues.append(self._issue_from_check(check))
        return issues, checks

    def _validate_row_arithmetic(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        target_col = int(spec.get("target_column", -1))
        terms = spec.get("terms", [])
        term_columns = [int(term.get("column", -1)) for term in terms if isinstance(term, dict)]
        if not terms or not self._valid_columns(table, [target_col, *term_columns]):
            return [], []

        issues: list[ValidationIssueRecord] = []
        checks: list[ValidationCheckRecord] = []
        for row_index, row in table.data_rows():
            current = additive_target_cell_number(row, target_col)
            if current is None:
                continue

            expected = 0.0
            for term in terms:
                value = additive_operand_cell_number(row, int(term["column"]))
                if value is None:
                    expected = None
                    break
                expected += -value if term.get("op") == "-" else value
            if expected is None:
                continue

            passed = abs(current - expected) <= sum_ratio_tolerance(spec, 0.5)
            check = self._calculation_check(
                table,
                profile,
                spec,
                check_type=self._check_type(spec, "합계 검수"),
                row_index=row_index,
                col_index=target_col,
                current=current,
                expected=expected,
                passed=passed,
                detail="통계표별 검수 기준에 정의된 계산식을 확인했습니다.",
            )
            checks.append(check)
            if not passed:
                issues.append(self._issue_from_check(check))
        return issues, checks

    def _validate_row_ratio(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        target_col = int(spec.get("target_column", -1))
        numerator_col = int(spec.get("numerator_column", -1))
        denominator_columns = [int(col_index) for col_index in spec.get("denominator_columns", [])]
        denominator_col = int(spec.get("denominator_column", -1))
        if not denominator_columns and denominator_col >= 0:
            denominator_columns = [denominator_col]
        if not denominator_columns or not self._valid_columns(table, [target_col, numerator_col, *denominator_columns]):
            return [], []

        issues: list[ValidationIssueRecord] = []
        checks: list[ValidationCheckRecord] = []
        multiplier = float(spec.get("multiplier", 1))
        for row_index, row in table.data_rows():
            current = cell_number(row, target_col)
            numerator = cell_number(row, numerator_col)
            denominator_values = [cell_number(row, col_index) for col_index in denominator_columns]
            if current is None or numerator is None or any(value is None for value in denominator_values):
                continue
            denominator = sum(value for value in denominator_values if value is not None)
            if denominator == 0:
                continue

            expected = numerator / denominator * multiplier
            tolerance = displayed_sum_ratio_tolerance(
                row,
                target_col,
                spec,
                default=0.15,
                multiplier=multiplier,
            )
            passed = abs(current - expected) <= tolerance
            aggregate_note = (
                " 총계·소계도 비율값을 더하지 않고 해당 행의 합계 분자와 합계 분모로 다시 계산했습니다."
                if spec.get("aggregate_strategy") == "recalculate_from_numerator_and_denominator"
                and (is_total_like(combined_row_label(table, row)) or "소계" in combined_row_label(table, row))
                else ""
            )
            check = self._calculation_check(
                table,
                profile,
                spec,
                check_type="비율 검수",
                row_index=row_index,
                col_index=target_col,
                current=current,
                expected=expected,
                passed=passed,
                detail=f"통계표별 검수 기준에 정의된 비율 산식을 확인했습니다.{aggregate_note}",
            )
            checks.append(check)
            if not passed:
                issues.append(self._issue_from_check(check))
        return issues, checks

    def _validate_column_share_ratio(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        target_col = int(spec.get("target_column", -1))
        numerator_col = int(spec.get("numerator_column", -1))
        denominator_row = int(spec.get("denominator_row", -1))
        denominator_col = int(spec.get("denominator_column", numerator_col))
        if not self._valid_columns(table, [target_col, numerator_col, denominator_col]) or not self._valid_rows(table, [denominator_row]):
            return [], []

        denominator = cell_number(table.matrix[denominator_row], denominator_col)
        if denominator in {None, 0}:
            return [], []

        issues: list[ValidationIssueRecord] = []
        checks: list[ValidationCheckRecord] = []
        multiplier = float(spec.get("multiplier", 100))
        for row_index, row in table.data_rows():
            current = cell_number(row, target_col)
            numerator = cell_number(row, numerator_col)
            if current is None or numerator is None:
                continue

            expected = numerator / denominator * multiplier
            tolerance = displayed_sum_ratio_tolerance(
                row,
                target_col,
                spec,
                default=0.15,
                multiplier=multiplier,
            )
            passed = abs(current - expected) <= tolerance
            check = self._calculation_check(
                table,
                profile,
                spec,
                check_type="비율 검수",
                row_index=row_index,
                col_index=target_col,
                current=current,
                expected=expected,
                passed=passed,
                detail="통계표별 검수 기준에 정의된 전체 대비 비율 산식을 확인했습니다.",
            )
            checks.append(check)
            if not passed:
                issues.append(self._issue_from_check(check))
        return issues, checks

    def _validate_growth_rate_scan(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        keywords = ("증감률", "증감율", "증가율", "감소율")
        matches: list[tuple[int, int, str]] = []
        for row_index, row in enumerate(table.matrix):
            for col_index, cell in enumerate(row):
                if cell is None:
                    continue
                text = clean_display_text(cell.text_value)
                if any(keyword in normalize_text(text) for keyword in keywords):
                    matches.append((row_index, col_index, text))

        has_formula_profile = any(
            check.get("type")
            in {
                "row_growth_rate",
                "row_year_over_year_rate",
                "row_year_over_year_change_amount",
                "year_rows_change_rate",
                "year_rows_change_amount",
            }
            for check in profile.check_specs
        )
        passed = not matches or has_formula_profile
        first = matches[0] if matches else None
        check = self._check_from_pass_fail(
            table,
            profile,
            spec,
            fallback_check_type="증감률 검수",
            location=f"{first[0] + 1}행 {first[1] + 1}열" if first else "전체 표",
            current_value=f"증감률 후보 {len(matches)}건",
            expected_value="산식 프로파일 적용" if matches else "증감률 항목 없음",
            difference=None if passed else "산식 해석 필요",
            passed=passed,
            detail="표 안의 증감률 항목 존재 여부와 실행 가능한 증감률 산식 프로파일 유무를 확인했습니다.",
            row_index=first[0] if first else None,
            col_index=first[1] if first else None,
        )
        return ([] if passed else [self._issue_from_check(check)]), [check]

    def _validate_row_growth_rate(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        target_col = int(spec.get("target_column", -1))
        current_col = int(spec.get("current_column", -1))
        previous_col = int(spec.get("previous_column", -1))
        if not self._valid_columns(table, [target_col, current_col, previous_col]):
            return [], []

        issues: list[ValidationIssueRecord] = []
        checks: list[ValidationCheckRecord] = []
        multiplier = float(spec.get("multiplier", 100))
        for row_index, row in table.data_rows():
            current = cell_number(row, target_col)
            current_value = cell_number(row, current_col)
            previous_value = cell_number(row, previous_col)
            if current is None or current_value is None or previous_value in {None, 0}:
                continue

            expected = (current_value - previous_value) / previous_value * multiplier
            tolerance = displayed_ratio_tolerance(
                row,
                target_col,
                base_tolerance=float(spec.get("tolerance", 0.15)),
                multiplier=multiplier,
            )
            passed = abs(current - expected) <= tolerance
            check = self._calculation_check(
                table,
                profile,
                spec,
                check_type="증감률 검수",
                row_index=row_index,
                col_index=target_col,
                current=current,
                expected=expected,
                passed=passed,
                detail="통계표별 검수 기준에 정의된 전년 대비 증감률 산식을 확인했습니다.",
            )
            checks.append(check)
            if not passed:
                issues.append(self._issue_from_check(check))
        return issues, checks

    def _validate_row_ratio_by_rows(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        target_row = int(spec.get("target_row", -1))
        numerator_row = int(spec.get("numerator_row", -1))
        denominator_rows = [int(value) for value in spec.get("denominator_rows", [])]
        if not denominator_rows:
            denominator_rows = [int(spec.get("denominator_row", -1))]
        columns = [int(col_index) for col_index in spec.get("columns", [])]
        if not columns or not self._valid_rows(table, [target_row, numerator_row, *denominator_rows]):
            return [], []

        issues: list[ValidationIssueRecord] = []
        checks: list[ValidationCheckRecord] = []
        multiplier = float(spec.get("multiplier", 100))
        for col_index in columns:
            if not self._valid_columns(table, [col_index]):
                continue
            target_value = cell_number(table.matrix[target_row], col_index)
            numerator = cell_number(table.matrix[numerator_row], col_index)
            denominator_values = [cell_number(table.matrix[row_index], col_index) for row_index in denominator_rows]
            if target_value is None or numerator is None or any(value is None for value in denominator_values):
                continue
            denominator = sum(value for value in denominator_values if value is not None)
            if denominator == 0:
                continue

            expected = numerator / denominator * multiplier
            target_row_values = table.matrix[target_row]
            tolerance = displayed_sum_ratio_tolerance(
                target_row_values,
                col_index,
                spec,
                default=0.15,
                multiplier=multiplier,
            )
            passed = abs(target_value - expected) <= tolerance
            aggregate_note = (
                " 총계·소계 비율도 개별 비율을 더하지 않고 해당 열의 합계 분자와 합계 분모로 다시 계산했습니다."
                if spec.get("aggregate_strategy") == "recalculate_from_numerator_and_denominator"
                and is_total_like(table.column_text(col_index))
                else ""
            )
            check = self._calculation_check(
                table,
                profile,
                spec,
                check_type="비율 검수",
                row_index=target_row,
                col_index=col_index,
                current=target_value,
                expected=expected,
                passed=passed,
                detail=f"같은 연도 열에서 분자 행과 분모 행으로 계산한 비율을 확인했습니다.{aggregate_note}",
            )
            checks.append(check)
            if not passed:
                issues.append(self._issue_from_check(check))
        return issues, checks

    def _validate_cell_sum(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        target_row = int(spec.get("target_row", -1))
        target_col = int(spec.get("target_column", -1))
        operand_cells = [
            (int(item.get("row", -1)), int(item.get("column", -1)))
            for item in spec.get("operand_cells", [])
            if isinstance(item, dict)
        ]
        if not operand_cells or not self._valid_rows(table, [target_row]) or not self._valid_columns(table, [target_col]):
            return [], []

        current = additive_target_cell_number(table.matrix[target_row], target_col)
        values: list[float] = []
        for row_index, col_index in operand_cells:
            if not self._valid_rows(table, [row_index]) or not self._valid_columns(table, [col_index]):
                continue
            value = additive_operand_cell_number(table.matrix[row_index], col_index)
            if value is None:
                return [], []
            values.append(value)
        if current is None or len(values) < 2:
            return [], []

        expected = sum(values)
        passed = abs(current - expected) <= sum_ratio_tolerance(spec, 1.0)
        check = self._calculation_check(
            table,
            profile,
            spec,
            check_type="합계 검수",
            row_index=target_row,
            col_index=target_col,
            current=current,
            expected=expected,
            passed=passed,
            detail="좌우로 이어진 반복 구역의 모든 세부 셀을 합산해 전체 합계를 확인했습니다.",
        )
        return ([self._issue_from_check(check)] if not passed else []), [check]

    def _validate_row_year_over_year_rate(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        target_row = int(spec.get("target_row", -1))
        source_row = int(spec.get("source_row", -1))
        columns = [int(col_index) for col_index in spec.get("columns", [])]
        if len(columns) < 2 or not self._valid_rows(table, [target_row, source_row]):
            return [], []

        issues: list[ValidationIssueRecord] = []
        checks: list[ValidationCheckRecord] = []
        multiplier = float(spec.get("multiplier", 100))
        ordered_columns = sorted(columns)
        for previous_col, current_col in zip(ordered_columns, ordered_columns[1:]):
            if not self._valid_columns(table, [previous_col, current_col]):
                continue
            target_value = cell_number(table.matrix[target_row], current_col)
            current_value = cell_number(table.matrix[source_row], current_col)
            previous_value = cell_number(table.matrix[source_row], previous_col)
            if target_value is None or current_value is None or previous_value in {None, 0}:
                continue

            expected = (current_value - previous_value) / previous_value * multiplier
            passed = abs(target_value - expected) <= float(spec.get("tolerance", 0.15))
            check = self._calculation_check(
                table,
                profile,
                spec,
                check_type="증감률 검수",
                row_index=target_row,
                col_index=current_col,
                current=target_value,
                expected=expected,
                passed=passed,
                detail="같은 원자료 행에서 전년도 열과 당해연도 열을 비교해 증감률을 확인했습니다.",
            )
            checks.append(check)
            if not passed:
                issues.append(self._issue_from_check(check))
        return issues, checks

    def _validate_row_year_over_year_change_amount(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        target_row = int(spec.get("target_row", -1))
        source_row = int(spec.get("source_row", -1))
        columns = [int(col_index) for col_index in spec.get("columns", [])]
        if len(columns) < 2 or not self._valid_rows(table, [target_row, source_row]):
            return [], []

        issues: list[ValidationIssueRecord] = []
        checks: list[ValidationCheckRecord] = []
        ordered_columns = sorted(columns)
        tolerance = float(spec.get("tolerance", 1.0))
        for previous_col, current_col in zip(ordered_columns, ordered_columns[1:]):
            if not self._valid_columns(table, [previous_col, current_col]):
                continue
            target_value = cell_number(table.matrix[target_row], current_col)
            current_value = cell_number(table.matrix[source_row], current_col)
            previous_value = cell_number(table.matrix[source_row], previous_col)
            if target_value is None or current_value is None or previous_value is None:
                continue

            expected = current_value - previous_value
            passed = abs(target_value - expected) <= tolerance
            check = self._calculation_check(
                table,
                profile,
                spec,
                check_type="증감액 검수",
                row_index=target_row,
                col_index=current_col,
                current=target_value,
                expected=expected,
                passed=passed,
                detail="같은 원자료 행에서 전년도 열과 당해연도 열의 차이를 확인했습니다.",
            )
            checks.append(check)
            if not passed:
                issues.append(self._issue_from_check(check))
        return issues, checks

    def _validate_year_rows_change_rate(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        value_col = int(spec.get("value_column", -1))
        rate_col = int(spec.get("rate_column", -1))
        change_col_raw = spec.get("change_column")
        change_col = int(change_col_raw) if change_col_raw is not None else None
        row_indices = [int(row_index) for row_index in spec.get("row_indices", [])]
        required_columns = [value_col, rate_col] + ([] if change_col is None else [change_col])
        if len(row_indices) < 2 or not self._valid_rows(table, row_indices) or not self._valid_columns(table, required_columns):
            return [], []

        issues: list[ValidationIssueRecord] = []
        checks: list[ValidationCheckRecord] = []
        multiplier = float(spec.get("multiplier", 100))
        change_tolerance = float(spec.get("change_tolerance", 1.0))
        rate_tolerance = float(spec.get("rate_tolerance", 0.15))

        for previous_row_index, current_row_index in zip(row_indices, row_indices[1:]):
            previous_row = table.matrix[previous_row_index]
            current_row = table.matrix[current_row_index]
            previous_value = cell_number(previous_row, value_col)
            current_value = cell_number(current_row, value_col)
            if previous_value in {None, 0} or current_value is None:
                continue

            expected_change = current_value - previous_value
            if change_col is not None:
                current_change = cell_number(current_row, change_col)
                if current_change is not None:
                    passed = abs(current_change - expected_change) <= change_tolerance
                    check = self._calculation_check(
                        table,
                        profile,
                        spec,
                        check_type="증감률 검수",
                        row_index=current_row_index,
                        col_index=change_col,
                        current=current_change,
                        expected=expected_change,
                        passed=passed,
                        detail="전년도 값과 당해연도 값을 비교해 증감 값을 확인했습니다.",
                    )
                    checks.append(check)
                    if not passed:
                        issues.append(self._issue_from_check(check))

            current_rate = cell_number(current_row, rate_col)
            if current_rate is None:
                continue

            expected_rate = expected_change / previous_value * multiplier
            passed = abs(current_rate - expected_rate) <= rate_tolerance
            check = self._calculation_check(
                table,
                profile,
                spec,
                check_type="증감률 검수",
                row_index=current_row_index,
                col_index=rate_col,
                current=current_rate,
                expected=expected_rate,
                passed=passed,
                detail="전년도 값과 당해연도 값을 비교해 증감률을 확인했습니다.",
            )
            checks.append(check)
            if not passed:
                issues.append(self._issue_from_check(check))

        return issues, checks

    def _validate_year_rows_change_amount(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        value_col = int(spec.get("value_column", -1))
        change_col = int(spec.get("change_column", -1))
        row_indices = [int(row_index) for row_index in spec.get("row_indices", [])]
        if len(row_indices) < 2 or not self._valid_rows(table, row_indices) or not self._valid_columns(table, [value_col, change_col]):
            return [], []

        issues: list[ValidationIssueRecord] = []
        checks: list[ValidationCheckRecord] = []
        change_tolerance = float(spec.get("change_tolerance", 1.0))

        for previous_row_index, current_row_index in zip(row_indices, row_indices[1:]):
            previous_row = table.matrix[previous_row_index]
            current_row = table.matrix[current_row_index]
            previous_value = cell_number(previous_row, value_col)
            current_value = cell_number(current_row, value_col)
            current_change = cell_number(current_row, change_col)
            if previous_value is None or current_value is None or current_change is None:
                continue

            expected_change = current_value - previous_value
            passed = abs(current_change - expected_change) <= change_tolerance
            check = self._calculation_check(
                table,
                profile,
                spec,
                check_type="증감률 검수",
                row_index=current_row_index,
                col_index=change_col,
                current=current_change,
                expected=expected_change,
                passed=passed,
                detail="전년도 값과 당해연도 값을 비교해 증감 값을 확인했습니다.",
            )
            checks.append(check)
            if not passed:
                issues.append(self._issue_from_check(check))

        return issues, checks

    def _validate_outliers(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        issues: list[ValidationIssueRecord] = []
        checks: list[ValidationCheckRecord] = []
        threshold = float(spec.get("mad_multiplier", 8.0))
        max_findings = int(spec.get("max_findings", 1))
        candidates: list[tuple[float, int, int, list, float, float]] = []

        for col_index in [int(value) for value in spec.get("columns", [])]:
            values: list[tuple[int, list, float]] = []
            for row_index, row in table.data_rows():
                label = combined_row_label(table, row)
                if is_total_like(label) or normalize_text(label).startswith("소계"):
                    continue
                value = cell_number(row, col_index)
                if value is not None:
                    values.append((row_index, row, value))
            if len(values) < 5:
                continue

            numeric_values = [value for _, _, value in values]
            median_value = median(numeric_values)
            deviations = [abs(value - median_value) for value in numeric_values]
            mad = median(deviations)
            if mad == 0:
                continue

            for row_index, row, value in values:
                score = abs(value - median_value) / mad
                if score <= threshold:
                    continue
                candidates.append((score, row_index, col_index, row, value, median_value))

        for score, row_index, col_index, row, value, median_value in sorted(candidates, reverse=True)[:max_findings]:
            check = self._check_from_pass_fail(
                table,
                profile,
                spec,
                fallback_check_type="이상치 검수",
                location=f"{combined_row_label(table, row) or f'{row_index + 1}행'} {table.column_text(col_index)}",
                current_value=format_number(value),
                expected_value=f"중앙값 {format_number(median_value)} 기준",
                difference=f"MAD {score:.1f}",
                passed=False,
                detail="같은 열의 값 분포에서 중앙값 대비 편차가 큰 값을 확인 대상으로 표시했습니다.",
                row_index=row_index,
                col_index=col_index,
                formula=f"|값-중앙값|/MAD > {threshold:g}",
            )
            checks.append(check)
            issues.append(self._issue_from_check(check))

        if not checks:
            check = self._check_from_pass_fail(
                table,
                profile,
                spec,
                fallback_check_type="이상치 검수",
                location="전체 숫자 열",
                current_value="이상치 후보 0건",
                expected_value="급격한 분포 이탈 없음",
                difference=None,
                passed=True,
                detail="검수 대상 숫자 열에서 통계적으로 큰 분포 이탈 후보가 발견되지 않았습니다.",
            )
            checks.append(check)
        return issues, checks

    def _validate_static_spelling(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        issues: list[ValidationIssueRecord] = []
        checks: list[ValidationCheckRecord] = []
        for row_index, row in enumerate(table.matrix):
            for col_index, cell in enumerate(row):
                if cell is None or not cell.text_value:
                    continue
                text = clean_display_text(cell.text_value)
                if is_repeated_projected_cell(table, row_index, col_index, text):
                    continue
                for term in spec.get("terms", []):
                    current = str(term.get("current", ""))
                    expected = str(term.get("expected", ""))
                    reason = str(term.get("reason") or "철자 오류")
                    if not current or current not in text:
                        continue
                    check = self._check_from_pass_fail(
                        table,
                        profile,
                        spec,
                        fallback_check_type="오탈자 검수",
                        location=f"{row_index + 1}행 {col_index + 1}열",
                        current_value=current,
                        expected_value=expected,
                        difference=reason,
                        passed=False,
                        detail="명백한 철자 오류 또는 문자 깨짐으로 분류된 항목입니다. 실제 서비스에서는 LLM 교정 결과와 담당자 승인값으로 확장됩니다.",
                        row_index=row_index,
                        col_index=col_index,
                    )
                    checks.append(check)
                    issues.append(self._issue_from_check(check))
                    if len(checks) >= 10:
                        return issues, checks

        if not checks:
            checks.append(
                self._check_from_pass_fail(
                    table,
                    profile,
                    spec,
                    fallback_check_type="오탈자 검수",
                    location="전체 텍스트 셀",
                    current_value="오탈자 후보 0건",
                    expected_value="정적 사전 기준 통과",
                    difference=None,
                    passed=True,
                    detail="정적 오탈자 사전에 등록된 표기 후보가 발견되지 않았습니다.",
                )
            )
        return issues, checks

    def _validate_static_terminology(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        issues: list[ValidationIssueRecord] = []
        checks: list[ValidationCheckRecord] = []
        for row_index, row in enumerate(table.matrix):
            for col_index, cell in enumerate(row):
                if cell is None or not cell.text_value:
                    continue
                text = clean_display_text(cell.text_value)
                for term in spec.get("terms", []):
                    current = str(term.get("current", ""))
                    expected = str(term.get("expected", ""))
                    reason = str(term.get("reason") or "표준 용어 확인")
                    if not current or current not in text:
                        continue
                    check = self._check_from_pass_fail(
                        table,
                        profile,
                        spec,
                        fallback_check_type="용어 제안",
                        location=f"{row_index + 1}행 {col_index + 1}열",
                        current_value=current,
                        expected_value=expected,
                        difference=reason,
                        passed=False,
                        detail="발간 표준 용어 또는 기관 용어집 기준으로 더 적절한 표현 후보를 표시했습니다. 최종 반영 여부는 담당자가 확인해야 합니다.",
                        row_index=row_index,
                        col_index=col_index,
                    )
                    checks.append(check)
                    issues.append(self._issue_from_check(check))
                    if len(checks) >= 10:
                        return issues, checks

        if not checks:
            checks.append(
                self._check_from_pass_fail(
                    table,
                    profile,
                    spec,
                    fallback_check_type="용어 제안",
                    location="전체 텍스트 셀",
                    current_value="용어 제안 후보 0건",
                    expected_value="정적 용어집 기준 통과",
                    difference=None,
                    passed=True,
                    detail="정적 용어집 기준의 용어 제안 후보가 발견되지 않았습니다.",
                )
            )
        return issues, checks

    def _validate_title_translation(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        expected = str(spec.get("expected_title_en") or "")
        source_title = str(spec.get("source_title") or table.title)
        if not expected:
            return [], []

        check = self._check_from_pass_fail(
            table,
            profile,
            spec,
            fallback_check_type="번역 검수",
            location="표 제목",
            current_value=source_title,
            expected_value=expected,
            difference="영문 제목 자동 보정",
            passed=False,
            detail="2026 초안에서 '(영문)'으로 표시된 표 제목에 영문 번역 후보를 삽입했습니다. 최종 표기는 담당자 확인이 필요합니다.",
        )
        return [self._issue_from_check(check)], [check]

    def _validate_static_translation(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        issues: list[ValidationIssueRecord] = []
        checks: list[ValidationCheckRecord] = []
        label_columns = set(leading_label_columns(table))
        for row_index, row in enumerate(table.matrix):
            for col_index, cell in enumerate(row):
                if cell is None or not cell.text_value:
                    continue
                if row_index >= table.header_count and col_index not in label_columns:
                    continue
                text = clean_display_text(cell.text_value)
                if contains_known_spelling_typo(text):
                    continue
                for term in spec.get("terms", []):
                    source = str(term.get("source", ""))
                    expected = str(term.get("expected", ""))
                    if not source or not expected or not contains_korean_term(text, source):
                        continue
                    if source == "계" and re.search(r"[A-Za-z]", text) and "total" not in text.lower():
                        continue
                    if expected.lower() in text.lower():
                        continue
                    check = self._check_from_pass_fail(
                        table,
                        profile,
                        spec,
                        fallback_check_type="번역 검수",
                        location=f"{row_index + 1}행 {col_index + 1}열",
                        current_value=text,
                        expected_value=expected,
                        difference="영문 병기 확인",
                        passed=False,
                        detail="기본 국문/영문 병기 용어집 기준으로 영문 표기 누락 또는 불일치 후보를 표시했습니다. 실제 서비스에서는 LLM 번역 검수 결과로 확장됩니다.",
                        row_index=row_index,
                        col_index=col_index,
                    )
                    checks.append(check)
                    issues.append(self._issue_from_check(check))
                    if len(checks) >= 10:
                        return issues, checks

        if not checks:
            checks.append(
                self._check_from_pass_fail(
                    table,
                    profile,
                    spec,
                    fallback_check_type="번역 검수",
                    location="헤더 및 항목명",
                    current_value="번역 확인 후보 0건",
                    expected_value="기본 용어집 기준 통과",
                    difference=None,
                    passed=True,
                    detail="기본 국문/영문 병기 용어집 기준의 번역 확인 후보가 발견되지 않았습니다.",
                )
            )
        return issues, checks

    def _validate_weighted_average(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        target_row = int(spec.get("target_row", -1))
        target_column = int(spec.get("target_column", -1))
        value_column = int(spec.get("value_column", -1))
        weight_column = int(spec.get("weight_column", -1))
        operand_rows = [int(row_index) for row_index in spec.get("operand_rows", [])]
        if (
            target_row < 0
            or target_row >= len(table.matrix)
            or not self._valid_columns(table, [target_column, value_column, weight_column])
            or not operand_rows
        ):
            return [], []

        current = additive_target_cell_number(table.matrix[target_row], target_column)
        weighted_sum = 0.0
        weight_sum = 0.0
        checked_rows = 0
        for row_index in operand_rows:
            if row_index >= len(table.matrix):
                continue
            row = table.matrix[row_index]
            value = additive_operand_cell_number(row, value_column)
            weight = additive_operand_cell_number(row, weight_column)
            if value is None or weight is None:
                continue
            weighted_sum += value * weight
            weight_sum += weight
            checked_rows += 1

        if current is None or checked_rows < 2 or weight_sum == 0:
            return [], []

        expected = weighted_sum / weight_sum
        passed = abs(current - expected) <= float(spec.get("tolerance", 0.05))
        check = self._calculation_check(
            table,
            profile,
            spec,
            check_type="평균 검수",
            row_index=target_row,
            col_index=target_column,
            current=current,
            expected=expected,
            passed=passed,
            detail="통계표별 검수 기준에 정의된 가중 평균 산식을 확인했습니다.",
        )
        return ([self._issue_from_check(check)] if not passed else []), [check]

    def _validate_column_sum(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        target_row = int(spec.get("target_row", -1))
        operand_rows = [int(row_index) for row_index in spec.get("operand_rows", [])]
        columns = [int(col_index) for col_index in spec.get("columns", [])]
        if target_row < 0 or target_row >= len(table.matrix) or not columns or not operand_rows:
            return [], []

        issues: list[ValidationIssueRecord] = []
        checks: list[ValidationCheckRecord] = []
        target_matrix_row = table.matrix[target_row]
        minimum_operands = 1 if spec.get("allow_single_operand") else 2
        for col_index in columns:
            if not self._valid_columns(table, [col_index]):
                continue
            current = additive_target_cell_number(target_matrix_row, col_index)
            values = []
            for row_index in operand_rows:
                if row_index >= len(table.matrix):
                    continue
                value = additive_operand_cell_number(table.matrix[row_index], col_index)
                if value is not None:
                    values.append(value)
            if current is None or len(values) < minimum_operands:
                continue

            expected = sum(values)
            passed = abs(current - expected) <= sum_ratio_tolerance(spec, 1.0)
            check = self._calculation_check(
                table,
                profile,
                spec,
                check_type="합계 검수",
                row_index=target_row,
                col_index=col_index,
                current=current,
                expected=expected,
                passed=passed,
                detail="통계표별 검수 기준에 정의된 열 방향 합계를 확인했습니다.",
            )
            checks.append(check)
            if not passed:
                issues.append(self._issue_from_check(check))
        return issues, checks

    def _calculation_check(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
        *,
        check_type: str,
        row_index: int,
        col_index: int,
        current: float,
        expected: float,
        passed: bool,
        detail: str,
    ) -> ValidationCheckRecord:
        row = table.matrix[row_index] if row_index < len(table.matrix) else []
        row_label = combined_row_label(table, row) or table.row_label(row) or f"{row_index + 1}행"
        column_label = table.column_text(col_index)
        difference = current - expected
        evidence = self._calculation_evidence(table, spec, row_index=row_index, col_index=col_index)
        evidence_detail = f" 연산에 사용한 셀: {evidence}." if evidence else ""
        return self._check_record(
            table,
            profile,
            spec,
            check_type=self._check_type(spec, check_type),
            location=f"{row_label} {column_label}",
            current_value=format_number(current),
            expected_value=format_number(expected),
            difference=None if passed else format_number(difference),
            status=self._status(spec, passed),
            severity=self._severity(spec, passed),
            detail=(
                f"{detail} 적용 기준: {spec.get('label', spec.get('id', '검수 기준'))}."
                f"{evidence_detail}"
            ),
            row_index=row_index,
            col_index=col_index,
            formula=str(spec.get("label") or spec.get("id") or "profile rule"),
        )

    def _calculation_evidence(
        self,
        table: ValidationTable,
        spec: dict[str, Any],
        *,
        row_index: int,
        col_index: int,
    ) -> str:
        rule_type = str(spec.get("type") or "")
        evidence: list[str] = []

        def add_cell(target_row: int, target_col: int) -> None:
            if not self._valid_rows(table, [target_row]) or not self._valid_columns(table, [target_col]):
                return
            row = table.matrix[target_row]
            value = clean_display_text(cell_text(row, target_col)) or "빈값(0 처리)"
            row_label = combined_row_label(table, row) or f"{target_row + 1}행"
            column_label = table.column_text(target_col)
            evidence.append(f"{row_label} / {column_label} = {value}")

        if rule_type == "row_sum":
            for operand_col in [int(value) for value in spec.get("operand_columns", [])]:
                add_cell(row_index, operand_col)
        elif rule_type == "row_arithmetic":
            for term in spec.get("terms", []):
                if isinstance(term, dict) and term.get("column") is not None:
                    add_cell(row_index, int(term["column"]))
        elif rule_type == "row_ratio":
            add_cell(row_index, int(spec.get("numerator_column", -1)))
            denominator_columns = [int(value) for value in spec.get("denominator_columns", [])]
            if not denominator_columns and spec.get("denominator_column") is not None:
                denominator_columns = [int(spec.get("denominator_column", -1))]
            for denominator_col in denominator_columns:
                add_cell(row_index, denominator_col)
        elif rule_type in {"column_sum", "region_total"}:
            for operand_row in [int(value) for value in spec.get("operand_rows", [])]:
                add_cell(operand_row, col_index)
        elif rule_type == "cell_sum":
            for operand in spec.get("operand_cells", []):
                if isinstance(operand, dict):
                    add_cell(int(operand.get("row", -1)), int(operand.get("column", -1)))
        elif rule_type == "column_share_ratio":
            add_cell(row_index, int(spec.get("numerator_column", -1)))
            add_cell(
                int(spec.get("denominator_row", -1)),
                int(spec.get("denominator_column", spec.get("numerator_column", -1))),
            )
        elif rule_type == "row_ratio_by_rows":
            add_cell(int(spec.get("numerator_row", -1)), col_index)
            denominator_rows = [int(value) for value in spec.get("denominator_rows", [])]
            if not denominator_rows:
                denominator_rows = [int(spec.get("denominator_row", -1))]
            for denominator_row in denominator_rows:
                add_cell(denominator_row, col_index)
        elif rule_type in {"row_year_over_year_rate", "row_year_over_year_change_amount"}:
            source_row = int(spec.get("source_row", -1))
            columns = sorted(int(value) for value in spec.get("columns", []))
            previous_columns = [value for value in columns if value < col_index]
            add_cell(source_row, previous_columns[-1] if previous_columns else -1)
            add_cell(source_row, col_index)

        if len(evidence) > 8:
            return "; ".join(evidence[:8]) + f" 외 {len(evidence) - 8}개"
        return "; ".join(evidence)

    def _check_record(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
        *,
        check_type: str,
        location: str,
        current_value: str,
        expected_value: str | None,
        difference: str | None,
        status: str,
        severity: str,
        detail: str,
        row_index: int | None = None,
        col_index: int | None = None,
        formula: str | None = None,
    ) -> ValidationCheckRecord:
        return ValidationCheckRecord(
            table_id=table.id,
            profile_id=profile.id,
            rule_id=str(spec.get("id") or f"profile.{profile.id}"),
            check_type=check_type,
            check_label=str(spec.get("label") or check_type),
            location=location,
            row_index=row_index,
            col_index=col_index,
            current_value=current_value,
            expected_value=expected_value,
            difference=difference,
            status=status,
            severity=severity,
            detail=detail,
            formula=formula,
            confidence=float(spec.get("confidence")) if spec.get("confidence") is not None else None,
        )

    def _issue_from_check(self, check: ValidationCheckRecord) -> ValidationIssueRecord:
        return ValidationIssueRecord(
            table_id=check.table_id,
            rule_id=check.rule_id,
            issue_type=check.check_type,
            location=check.location,
            current_value=check.current_value,
            expected_value=check.expected_value,
            difference=check.difference,
            severity="critical" if check.status == "오류 의심" else check.severity,
            detail=check.detail,
            row_index=check.row_index,
            col_index=check.col_index,
            status=check.status,
            formula=check.formula,
        )

    def _profile_issue(
        self,
        table: ValidationTable,
        profile: ValidationProfile,
        spec: dict[str, Any],
        *,
        issue_type: str,
        row_index: int,
        col_index: int,
        current: float,
        expected: float,
        detail: str,
    ) -> ValidationIssueRecord:
        row = table.matrix[row_index] if row_index < len(table.matrix) else []
        row_label = combined_row_label(table, row) or f"{row_index + 1}행"
        column_label = table.column_text(col_index)
        difference = current - expected
        formula = str(spec.get("label") or spec.get("id") or "profile rule")

        return ValidationIssueRecord(
            table_id=table.id,
            rule_id=str(spec.get("id") or f"profile.{profile.id}"),
            issue_type=issue_type,
            location=f"{row_label} {column_label}",
            current_value=format_number(current),
            expected_value=format_number(expected),
            difference=format_number(difference),
            severity=str(spec.get("severity") or "critical"),
            detail=f"{detail} 적용 프로파일: {profile.table_code} / {profile.structure_signature}.",
            row_index=row_index,
            col_index=col_index,
            formula=formula,
        )

    def _valid_columns(self, table: ValidationTable, columns: list[int]) -> bool:
        max_cols = column_count(table)
        return all(0 <= col_index < max_cols for col_index in columns)

    def _valid_rows(self, table: ValidationTable, rows: list[int]) -> bool:
        return all(0 <= row_index < len(table.matrix) for row_index in rows)
