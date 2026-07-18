from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from app.validation.profiles import ValidationProfile


PART_SUFFIX_RE = re.compile(r"\s+표\s*\d+$")
PART_NUMBER_RE = re.compile(r"\s+표\s*(\d+)$")
SINGLE_MEASURE_SPLIT_KEY = "__single_measure_split__"
LEADING_NUMBER_RE = re.compile(r"^([+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?)")


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


class ConfiguredCrossTableRowSumRule:
    """Execute profile-defined totals that span separately stored table parts."""

    rule_id = "cross.profile_row_sum"
    check_type = "합계 검수"

    def __init__(self, profiles: dict[str, "ValidationProfile"]) -> None:
        self._profiles = profiles

    def evaluate(self, tables: list[ValidationTable]) -> CrossTableValidationResult:
        table_by_code = {table.code: table for table in tables}
        issues: list[ValidationIssueRecord] = []
        checks: list[ValidationCheckRecord] = []

        for target_table in tables:
            profile = self._profiles.get(target_table.code)
            if profile is None:
                continue

            for spec in profile.check_specs:
                if spec.get("type") != "cross_table_row_sum" or spec.get("execute") is False:
                    continue
                source_code = str(spec.get("source_table_code") or "")
                source_table = table_by_code.get(source_code)
                if source_table is None:
                    continue

                spec_issues, spec_checks = self._evaluate_spec(
                    target_table,
                    source_table,
                    profile,
                    spec,
                )
                issues.extend(spec_issues)
                checks.extend(spec_checks)

        return CrossTableValidationResult(issues=issues, checks=checks)

    def _evaluate_spec(
        self,
        target_table: ValidationTable,
        source_table: ValidationTable,
        profile: "ValidationProfile",
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        target_column = int(spec.get("target_column", -1))
        target_operand_columns = [int(value) for value in spec.get("operand_columns", [])]
        source_columns = [int(value) for value in spec.get("source_columns", [])]
        if not source_columns:
            source_columns = [int(spec.get("source_column", -1))]
        row_indices = [int(value) for value in spec.get("row_indices", [])]
        if target_column < 0 or any(column < 0 for column in source_columns) or not target_operand_columns:
            return [], []
        if target_column >= column_count(target_table) or any(
            column >= column_count(source_table) for column in source_columns
        ):
            return [], []

        source_rows = rows_by_label(source_table)
        if not row_indices:
            row_indices = [row_index for row_index, _ in target_table.data_rows()]

        issues: list[ValidationIssueRecord] = []
        checks: list[ValidationCheckRecord] = []
        tolerance = max(float(spec.get("tolerance", 1.0)), 1.0)
        failure_status = str(spec.get("failure_status") or "오류 의심")
        failure_severity = str(spec.get("severity") or "critical")
        label = str(spec.get("label") or "분할 표 간 합계")

        for target_row_index in row_indices:
            if target_row_index >= len(target_table.matrix):
                continue
            target_row = target_table.matrix[target_row_index]
            row_key = row_label_key(target_table, target_row)
            source_row_index = source_rows.get(row_key)
            if source_row_index is None or source_row_index >= len(source_table.matrix):
                continue

            current = additive_value(target_row, target_column, blank_as_zero=False)
            if current is None:
                continue

            operand_values = [
                additive_value(target_row, column, blank_as_zero=True)
                for column in target_operand_columns
            ]
            source_values = [
                additive_value(
                    source_table.matrix[source_row_index],
                    column,
                    blank_as_zero=True,
                )
                for column in source_columns
            ]
            if any(value is None for value in [*source_values, *operand_values]):
                continue

            expected = sum(value for value in [*operand_values, *source_values] if value is not None)
            passed = abs(current - expected) <= tolerance
            row_label = combined_row_label(target_table, target_row) or f"{target_row_index + 1}행"
            target_label = target_table.column_text(target_column)
            evidence = [
                f"{row_label} / {target_table.column_text(column)} = "
                f"{clean_display_text(cell_text(target_row, column)) or '공란(0 처리)'}"
                for column in target_operand_columns
            ]
            source_row = source_table.matrix[source_row_index]
            evidence.extend(
                f"{row_label} / {source_table.code} {source_table.column_text(column)} = "
                f"{clean_display_text(cell_text(source_row, column)) or '공란(0 처리)'}"
                for column in source_columns
            )
            detail = (
                "서로 분리된 하위표의 같은 연도 값을 포함해 총계를 확인했습니다. "
                f"적용 기준: {label}. 연산에 사용한 셀: {'; '.join(evidence)}."
            )
            check = ValidationCheckRecord(
                table_id=target_table.id,
                rule_id=str(spec.get("id") or self.rule_id),
                check_type=str(spec.get("check_type") or self.check_type),
                check_label=label,
                location=f"{clean_display_text(row_label)} {target_label}",
                current_value=format_number(current),
                expected_value=format_number(expected),
                difference=None if passed else format_number(current - expected),
                status="정상" if passed else failure_status,
                severity="info" if passed else failure_severity,
                detail=detail,
                row_index=target_row_index,
                col_index=target_column,
                formula=label,
                profile_id=profile.id,
                confidence=float(spec.get("confidence", 1.0)),
            )
            checks.append(check)
            if spec.get("emit_source_checks") is True:
                source_rule_id = (
                    f"cross.profile_row_sum_operand:{spec.get('id') or self.rule_id}:"
                    f"related={','.join(str(column) for column in source_columns)}"
                )
                checks.append(
                    ValidationCheckRecord(
                        table_id=source_table.id,
                        rule_id=source_rule_id,
                        check_type=str(spec.get("check_type") or self.check_type),
                        check_label=label,
                        location=f"{clean_display_text(row_label)} {source_table.code} 계산 근거",
                        current_value=format_number(current),
                        expected_value=format_number(expected),
                        difference=None if passed else format_number(current - expected),
                        status="정상" if passed else failure_status,
                        severity="info" if passed else failure_severity,
                        detail=detail,
                        row_index=source_row_index,
                        col_index=None,
                        formula=label,
                        profile_id=profile.id,
                        confidence=float(spec.get("confidence", 1.0)),
                    )
                )
            if not passed:
                issues.append(issue_from_check(check))

        return issues, checks


class ConfiguredCrossTableWeightedAverageRule:
    """Execute profile-defined averages using weights stored in another table."""

    rule_id = "cross.profile_weighted_average"
    check_type = "평균 검수"

    def __init__(self, profiles: dict[str, "ValidationProfile"]) -> None:
        self._profiles = profiles

    def evaluate(self, tables: list[ValidationTable]) -> CrossTableValidationResult:
        table_by_code = {table.code: table for table in tables}
        issues: list[ValidationIssueRecord] = []
        checks: list[ValidationCheckRecord] = []

        for target_table in tables:
            profile = self._profiles.get(target_table.code)
            if profile is None:
                continue

            for spec in profile.check_specs:
                if spec.get("type") != "cross_table_weighted_average" or spec.get("execute") is False:
                    continue
                source_table = table_by_code.get(str(spec.get("source_table_code") or ""))
                if source_table is None:
                    continue
                spec_issues, spec_checks = self._evaluate_spec(target_table, source_table, profile, spec)
                issues.extend(spec_issues)
                checks.extend(spec_checks)

        return CrossTableValidationResult(issues=issues, checks=checks)

    def _evaluate_spec(
        self,
        target_table: ValidationTable,
        source_table: ValidationTable,
        profile: "ValidationProfile",
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        target_row = int(spec.get("target_row", -1))
        target_column = int(spec.get("target_column", -1))
        value_column = int(spec.get("value_column", target_column))
        weight_column = int(spec.get("source_weight_column", -1))
        row_pairs = [item for item in spec.get("row_pairs", []) if isinstance(item, dict)]
        if (
            target_row < 0
            or target_column < 0
            or value_column < 0
            or weight_column < 0
            or target_row >= len(target_table.matrix)
            or target_column >= column_count(target_table)
            or value_column >= column_count(target_table)
            or weight_column >= column_count(source_table)
            or len(row_pairs) < 2
        ):
            return [], []

        weighted_sum = 0.0
        weight_sum = 0.0
        included_rows = 0
        skipped_rows = 0
        for pair in row_pairs:
            value_row_index = int(pair.get("value_row", -1))
            weight_row_index = int(pair.get("weight_row", -1))
            if value_row_index < 0 or weight_row_index < 0:
                skipped_rows += 1
                continue
            if value_row_index >= len(target_table.matrix) or weight_row_index >= len(source_table.matrix):
                skipped_rows += 1
                continue

            value = leading_numeric_value(target_table.matrix[value_row_index], value_column)
            weight = additive_value(source_table.matrix[weight_row_index], weight_column, blank_as_zero=False)
            if value is None or weight is None:
                skipped_rows += 1
                continue
            weighted_sum += value * weight
            weight_sum += weight
            included_rows += 1

        target_row_values = target_table.matrix[target_row]
        target_label = combined_row_label(target_table, target_row_values) or f"{target_row + 1}행"
        column_label = target_table.column_text(target_column)
        label = str(spec.get("label") or "교차표 가중 평균")
        current = leading_numeric_value(target_row_values, target_column)
        if current is None or included_rows < 2 or weight_sum == 0:
            current_text = clean_display_text(cell_text(target_row_values, target_column)) or "공란"
            unavailable_reason = (
                "검수 대상 평균값이 숫자로 입력되지 않았습니다."
                if current is None
                else f"가중 평균에 사용할 지역별 값 또는 예산 분모가 부족합니다. 사용 가능 {included_rows}개"
            )
            check = ValidationCheckRecord(
                table_id=target_table.id,
                rule_id=str(spec.get("id") or self.rule_id),
                check_type=str(spec.get("check_type") or self.check_type),
                check_label=label,
                location=f"{clean_display_text(target_label)} {column_label}",
                current_value=current_text,
                expected_value="계산 제외 (연산값 부족)",
                difference=None,
                status="정상",
                severity="info",
                detail=(
                    f"{unavailable_reason} 설정된 {len(row_pairs)}개 지역 중 {skipped_rows}개는 값 또는 예산 분모가 없어 "
                    "계산에는 반영되지 않았습니다. 다음 연도 값 입력을 확인할 수 있도록 산식의 대상·구성 셀은 모두 하이라이트합니다."
                ),
                row_index=target_row,
                col_index=target_column,
                formula=label,
                profile_id=profile.id,
                confidence=float(spec.get("confidence", 1.0)),
            )
            return [], [check]

        expected = weighted_sum / weight_sum
        tolerance = float(spec.get("tolerance", 1.0))
        passed = abs(current - expected) <= tolerance
        detail = (
            f"{source_table.code}의 예산 규모를 가중치로 사용해 {included_rows}개 지역 비율의 가중 평균을 확인했습니다. "
            f"값 또는 예산 분모가 없어 계산에서 제외된 지역은 {skipped_rows}개입니다. "
            "평균과 지역별 수치의 예산 기준 차이 및 표시 반올림은 프로파일 허용오차에 반영했습니다."
        )
        check = ValidationCheckRecord(
            table_id=target_table.id,
            rule_id=str(spec.get("id") or self.rule_id),
            check_type=str(spec.get("check_type") or self.check_type),
            check_label=label,
            location=f"{clean_display_text(target_label)} {column_label}",
            current_value=format_number(current),
            expected_value=format_number(expected),
            difference=None if passed else format_number(current - expected),
            status="정상" if passed else str(spec.get("failure_status") or "오류 의심"),
            severity="info" if passed else str(spec.get("severity") or "critical"),
            detail=detail,
            row_index=target_row,
            col_index=target_column,
            formula=label,
            profile_id=profile.id,
            confidence=float(spec.get("confidence", 1.0)),
        )
        return ([issue_from_check(check)] if not passed else []), [check]


class ConfiguredCrossTableCellMatchRule:
    """Compare a configured cell with the corresponding value in another table."""

    rule_id = "cross.profile_cell_match"
    check_type = "평균 검수"

    def __init__(self, profiles: dict[str, "ValidationProfile"]) -> None:
        self._profiles = profiles

    def evaluate(self, tables: list[ValidationTable]) -> CrossTableValidationResult:
        table_by_code = {table.code: table for table in tables}
        issues: list[ValidationIssueRecord] = []
        checks: list[ValidationCheckRecord] = []

        for target_table in tables:
            profile = self._profiles.get(target_table.code)
            if profile is None:
                continue

            for spec in profile.check_specs:
                if spec.get("type") != "cross_table_cell_match" or spec.get("execute") is False:
                    continue
                source_table = table_by_code.get(str(spec.get("source_table_code") or ""))
                if source_table is None:
                    continue

                spec_issues, spec_checks = self._evaluate_spec(target_table, source_table, profile, spec)
                issues.extend(spec_issues)
                checks.extend(spec_checks)

        return CrossTableValidationResult(issues=issues, checks=checks)

    def _evaluate_spec(
        self,
        target_table: ValidationTable,
        source_table: ValidationTable,
        profile: "ValidationProfile",
        spec: dict[str, Any],
    ) -> tuple[list[ValidationIssueRecord], list[ValidationCheckRecord]]:
        target_row = configured_row_index(target_table, spec.get("target_row"))
        source_row = configured_row_index(source_table, spec.get("source_row"))
        target_column = configured_column_index(spec.get("target_column"))
        source_column = configured_column_index(spec.get("source_column"))
        if target_row is None or source_row is None or target_column is None or source_column is None:
            return [], []
        if (
            target_row >= len(target_table.matrix)
            or source_row >= len(source_table.matrix)
            or target_column >= column_count(target_table)
            or source_column >= column_count(source_table)
        ):
            return [], []

        current = additive_value(target_table.matrix[target_row], target_column, blank_as_zero=False)
        expected = additive_value(source_table.matrix[source_row], source_column, blank_as_zero=False)
        if current is None or expected is None:
            return [], []

        tolerance = float(spec.get("tolerance", 1.0))
        passed = abs(current - expected) <= tolerance
        target_label = combined_row_label(target_table, target_table.matrix[target_row]) or f"{target_row + 1}행"
        source_label = combined_row_label(source_table, source_table.matrix[source_row]) or f"{source_row + 1}행"
        target_column_label = target_table.column_text(target_column)
        source_column_label = source_table.column_text(source_column)
        label = str(spec.get("label") or "교차표 값 일치")
        detail = (
            f"{source_table.code}의 {clean_display_text(source_label)} {source_column_label} 값을 기준으로 "
            f"{target_table.code}의 {clean_display_text(target_label)} {target_column_label} 값을 비교했습니다."
        )
        check = ValidationCheckRecord(
            table_id=target_table.id,
            rule_id=str(spec.get("id") or self.rule_id),
            check_type=str(spec.get("check_type") or self.check_type),
            check_label=label,
            location=f"{clean_display_text(target_label)} {target_column_label}",
            current_value=format_number(current),
            expected_value=format_number(expected),
            difference=None if passed else format_number(current - expected),
            status="정상" if passed else str(spec.get("failure_status") or "오류 의심"),
            severity="info" if passed else str(spec.get("severity") or "critical"),
            detail=detail,
            row_index=target_row,
            col_index=target_column,
            formula=label,
            profile_id=profile.id,
            confidence=float(spec.get("confidence", 1.0)),
        )
        return ([issue_from_check(check)] if not passed else []), [check]


def configured_row_index(table: ValidationTable, value: Any) -> int | None:
    if value == "latest_data_row":
        data_rows = table.data_rows()
        return data_rows[-1][0] if data_rows else None
    return configured_column_index(value)


def configured_column_index(value: Any) -> int | None:
    try:
        column_index = int(value)
    except (TypeError, ValueError):
        return None
    return column_index if column_index >= 0 else None


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


def leading_numeric_value(row: list, col_index: int) -> float | None:
    value = cell_number(row, col_index)
    if value is not None:
        return value

    text = clean_display_text(cell_text(row, col_index))
    match = LEADING_NUMBER_RE.match(text)
    if match is None:
        return None
    return float(match.group(1).replace(",", ""))


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
