from __future__ import annotations

import re
from typing import Any

from app.validation.models import (
    ValidationCheckRecord,
    ValidationIssueRecord,
    ValidationTable,
    clean_display_text,
    format_number,
    parse_numeric_text,
)
from app.validation.profiles import ValidationProfile
from app.validation.rules import ValidationRule, cell_number, cell_text, combined_row_label, column_count


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
            if rule_type == "metadata_required":
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
            elif rule_type == "row_sum":
                spec_issues, spec_checks = self._validate_row_sum(table, profile, spec)
            elif rule_type == "row_arithmetic":
                spec_issues, spec_checks = self._validate_row_arithmetic(table, profile, spec)
            elif rule_type == "row_ratio":
                spec_issues, spec_checks = self._validate_row_ratio(table, profile, spec)
            else:
                spec_issues, spec_checks = [], []

            issues.extend(spec_issues)
            checks.extend(spec_checks)

        return issues, checks

    def _should_execute(self, spec: dict[str, Any]) -> bool:
        if spec.get("execute") is False:
            return False
        confidence = float(spec.get("confidence", 1.0))
        if spec.get("category") == "template" and confidence < 0.9:
            return False
        return True

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
                check_type="메타정보 확인",
                location=f"메타정보 {label}",
                current_value=str(value).strip() or "없음",
                expected_value=label,
                difference=None if passed else "누락",
                status="정상" if passed else "확인 필요",
                severity="info" if passed else str(spec.get("severity", "warning")),
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
            check_type="빈값 확인",
            location="데이터 행 항목명",
            current_value=f"누락 {len(missing_rows)}건",
            expected_value="누락 0건",
            difference=None if passed else f"{len(missing_rows)}건",
            status="정상" if passed else "확인 필요",
            severity="info" if passed else str(spec.get("severity", "warning")),
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
            check_type="숫자 형식",
            location=f"{first[0] + 1}행 {first[1] + 1}열" if first else "전체 숫자 셀",
            current_value=first[2] if first else "정상",
            expected_value="숫자로 해석 가능한 형식",
            difference=None if passed else f"{len(suspicious)}건",
            status="정상" if passed else "확인 필요",
            severity="info" if passed else str(spec.get("severity", "warning")),
            detail="숫자처럼 보이는 셀이 숫자값으로 해석되는지 확인했습니다.",
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
            check_type="연도 축 확인",
            location="연도 행/열",
            current_value=", ".join(str(year) for year in unique_years[:8]) + ("..." if len(unique_years) > 8 else ""),
            expected_value=expected,
            difference=None,
            status="정상" if passed else "확인 필요",
            severity="info" if passed else str(spec.get("severity", "warning")),
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
        for row_index, row in table.data_rows():
            current = cell_number(row, target_col)
            operands = [cell_number(row, col_index) for col_index in operand_columns]
            if current is None or any(value is None for value in operands):
                continue

            expected = sum(value for value in operands if value is not None)
            passed = abs(current - expected) <= float(spec.get("tolerance", 1.0))
            check = self._calculation_check(
                table,
                profile,
                spec,
                check_type="합계 불일치",
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
            current = cell_number(row, target_col)
            if current is None:
                continue

            expected = 0.0
            for term in terms:
                value = cell_number(row, int(term["column"]))
                if value is None:
                    expected = None
                    break
                expected += -value if term.get("op") == "-" else value
            if expected is None:
                continue

            passed = abs(current - expected) <= float(spec.get("tolerance", 0.5))
            check = self._calculation_check(
                table,
                profile,
                spec,
                check_type="계산식 확인",
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
        denominator_col = int(spec.get("denominator_column", -1))
        if not self._valid_columns(table, [target_col, numerator_col, denominator_col]):
            return [], []

        issues: list[ValidationIssueRecord] = []
        checks: list[ValidationCheckRecord] = []
        multiplier = float(spec.get("multiplier", 1))
        for row_index, row in table.data_rows():
            current = cell_number(row, target_col)
            numerator = cell_number(row, numerator_col)
            denominator = cell_number(row, denominator_col)
            if current is None or numerator is None or denominator in {None, 0}:
                continue

            expected = numerator / denominator * multiplier
            passed = abs(current - expected) <= float(spec.get("tolerance", 0.15))
            check = self._calculation_check(
                table,
                profile,
                spec,
                check_type="비율 확인",
                row_index=row_index,
                col_index=target_col,
                current=current,
                expected=expected,
                passed=passed,
                detail="통계표별 검수 기준에 정의된 비율 산식을 확인했습니다.",
            )
            checks.append(check)
            if not passed:
                issues.append(self._issue_from_check(check))
        return issues, checks

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
        for col_index in columns:
            if not self._valid_columns(table, [col_index]):
                continue
            current = cell_number(target_matrix_row, col_index)
            values = []
            for row_index in operand_rows:
                if row_index >= len(table.matrix):
                    continue
                value = cell_number(table.matrix[row_index], col_index)
                if value is not None:
                    values.append(value)
            if current is None or len(values) < 2:
                continue

            expected = sum(values)
            passed = abs(current - expected) <= float(spec.get("tolerance", 1.0))
            check = self._calculation_check(
                table,
                profile,
                spec,
                check_type="합계 불일치",
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
        severity = "info" if passed else str(spec.get("severity") or "critical")
        status = "정상" if passed else ("오류 의심" if severity == "critical" else "확인 필요")
        return self._check_record(
            table,
            profile,
            spec,
            check_type=check_type,
            location=f"{row_label} {column_label}",
            current_value=format_number(current),
            expected_value=format_number(expected),
            difference=format_number(difference),
            status=status,
            severity=severity,
            detail=f"{detail} 적용 기준: {spec.get('label', spec.get('id', '검수 기준'))}.",
            row_index=row_index,
            col_index=col_index,
            formula=str(spec.get("label") or spec.get("id") or "profile rule"),
        )

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
