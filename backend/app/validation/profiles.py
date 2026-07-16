from __future__ import annotations

from dataclasses import dataclass
import hashlib
from itertools import combinations
import json
import re
from typing import Any, Protocol

from app.validation.catalog import rule_definition_payload, rule_spec
from app.validation.curated_profiles import apply_curated_profile
from app.validation.models import ValidationTable, clean_display_text, normalize_text
from app.validation.rules import (
    REGION_NAMES,
    cell_number,
    column_count,
    combined_row_label,
    header_parent_key,
    is_additive_column_label,
    is_schedule_descriptor_column,
    is_total_like,
    leading_label_columns,
    leaf_header,
)


PROFILE_VERSION = "validation-profile-v3"

COMMON_RULE_IDS = [
    "sum",
    "ratio",
    "growth_rate",
    "outlier",
    "spelling",
    "terminology",
    "translation",
    "metadata",
]

@dataclass(frozen=True)
class ValidationProfile:
    id: int
    table_code: str
    table_title: str
    source_report_id: int | None
    structure_signature: str
    table_type: str
    status: str
    source: str
    rules: dict[str, Any]
    notes: str
    created_at: str
    updated_at: str
    approved_at: str | None = None
    approved_by: str | None = None
    llm_model: str | None = None

    @property
    def table_rules(self) -> list[dict[str, Any]]:
        rules = self.rules.get("table_rules", [])
        return rules if isinstance(rules, list) else []

    @property
    def check_specs(self) -> list[dict[str, Any]]:
        checks = self.rules.get("checks", [])
        if isinstance(checks, list):
            return checks
        return self.table_rules

    @property
    def requires_review(self) -> bool:
        return self.status != "approved"


@dataclass(frozen=True)
class ProfileDraft:
    table_code: str
    table_title: str
    structure_signature: str
    table_type: str
    status: str
    source: str
    rules: dict[str, Any]
    notes: str
    llm_model: str | None = None


class ProfileDraftProvider(Protocol):
    source: str

    def draft(
        self,
        table: ValidationTable,
        *,
        previous_profile: ValidationProfile | None = None,
    ) -> ProfileDraft:
        raise NotImplementedError


class HeuristicProfileDraftProvider:
    """Local stand-in for the future GPT-based profile drafter."""

    source = "heuristic"

    def draft(
        self,
        table: ValidationTable,
        *,
        previous_profile: ValidationProfile | None = None,
    ) -> ProfileDraft:
        signature = structure_signature(table)
        analysis = analyze_table(table)
        templates = detect_templates(table)
        checks = infer_profile_checks(table, analysis=analysis, templates=templates)
        needs_llm_review = previous_profile is not None and previous_profile.structure_signature != signature
        status = profile_status(checks, needs_llm_review=needs_llm_review)
        table_type = ", ".join(templates) if templates else "general"

        notes = "휴리스틱 기반 표별 검수 기준입니다. 담당자 승인 후 다음 연도 검수에 재사용하세요."
        if previous_profile is None:
            notes = "신규 통계표로 판단되어 표별 검수 기준을 생성했습니다. 실제 서비스에서는 GPT API 해석 후 담당자 승인을 거치는 흐름입니다."
        elif needs_llm_review:
            notes = "기존 통계표와 구조 서명이 달라 새 표별 검수 기준을 생성했습니다. 실제 서비스에서는 GPT API가 변경된 표 구조를 다시 해석해야 합니다."
        if status == "needs_review":
            notes = f"{notes} 일부 검수 후보의 확신도가 낮아 프로파일 재해석 대상으로 분류했습니다."

        curated = apply_curated_profile(
            table.code,
            checks=checks,
            table_type=table_type,
            status=status,
            notes=notes,
        )
        checks = curated.checks
        table_type = curated.table_type
        status = curated.status
        notes = curated.notes
        needs_llm_review = needs_llm_review and status != "ready"
        table_rules = [check for check in checks if check.get("category") in {"template", "table"}]

        return ProfileDraft(
            table_code=table.code,
            table_title=table.title,
            structure_signature=signature,
            table_type=table_type,
            status=status,
            source=self.source,
            rules={
                "version": PROFILE_VERSION,
                "roles": {
                    "llm": "검수 명세 초안 작성자",
                    "rule_engine": "DB 프로파일에 정의된 검수 명세 실행자",
                    "owner": "검수 프로파일 및 결과 최종 승인자",
                },
                "rule_definitions": rule_definition_payload(),
                "common_rules": COMMON_RULE_IDS,
                "templates": templates,
                "analysis": analysis,
                "checks": checks,
                "table_rules": table_rules,
                "requires_llm_review": needs_llm_review,
            },
            notes=notes,
        )


class GPTProfileDraftProvider:
    """Future adapter point for GPT API based profile drafting.

    The validation engine consumes the same ProfileDraft shape regardless of
    whether this provider is backed by GPT, a rules UI, or local heuristics.
    """

    source = "llm"

    def __init__(self, *, model: str = "gpt-5") -> None:
        self.model = model

    def draft(
        self,
        table: ValidationTable,
        *,
        previous_profile: ValidationProfile | None = None,
    ) -> ProfileDraft:
        raise NotImplementedError("GPT API 연결 시 이 provider에서 표 구조를 해석해 ProfileDraft를 반환합니다.")


def structure_signature(table: ValidationTable) -> str:
    headers: list[list[str]] = []
    for row in table.matrix[: table.header_count]:
        headers.append([normalize_text(cell.text_value if cell else "") for cell in row])

    payload = {
        "header_count": table.header_count,
        "column_count": column_count(table),
        "label_columns": leading_label_columns(table),
        "headers": headers,
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:20]


def detect_templates(table: ValidationTable) -> list[str]:
    templates: list[str] = []
    all_headers = " ".join(table.column_text(col_index) for col_index in range(column_count(table)))
    normalized_headers = normalize_text(all_headers)
    title = normalize_text(f"{table.title} {table.code}")

    region_labels = {region_name_from_label(table.row_label(row)) for _, row in table.data_rows()}
    if "지역" in normalized_headers or len(region_labels & {"서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주"}) >= 8:
        templates.append("regional_table")

    year_like_rows = sum(1 for _, row in table.data_rows() if re.fullmatch(r"\d{4}", normalize_text(table.row_label(row))))
    if "연도" in normalized_headers or "year" in all_headers.lower() or year_like_rows >= 4:
        templates.append("year_trend_table")

    if any(keyword in normalized_headers for keyword in ("남성", "여성", "male", "female")) and any(
        keyword in normalized_headers + title for keyword in ("직급", "계급", "grade")
    ):
        templates.append("sex_grade_table")

    if any(keyword in normalized_headers for keyword in ("연말잔액", "수입액", "지출액", "잔액율", "잔액률", "balance", "income", "expenditure")):
        templates.append("budget_balance_ratio_table")

    return templates


def analyze_table(table: ValidationTable) -> dict[str, Any]:
    label_columns = leading_label_columns(table)
    columns = []
    for col_index in range(column_count(table)):
        role = "label" if col_index in label_columns else infer_column_role(table, col_index)
        columns.append(
            {
                "index": col_index,
                "label": table.column_text(col_index),
                "leaf_label": leaf_header(table, col_index),
                "parent_key": list(header_parent_key(table, col_index)),
                "role": role,
            }
        )

    rows = []
    for row_index, row in table.data_rows():
        label = combined_row_label(table, row) or table.row_label(row)
        rows.append(
            {
                "index": row_index,
                "label": clean_display_text(label),
                "role": infer_row_role(label),
                "numeric_cells": sum(1 for col_index in range(len(row)) if cell_number(row, col_index) is not None),
            }
        )

    return {
        "header_count": table.header_count,
        "column_count": column_count(table),
        "row_count": len(table.data_rows()),
        "label_columns": label_columns,
        "columns": columns,
        "rows": rows,
        "unit": table.unit,
        "base_date": table.base_date,
        "source_present": bool(table.source.strip()),
        "note_present": bool(table.note.strip()),
    }


def infer_column_role(table: ValidationTable, col_index: int) -> str:
    label = table.column_text(col_index)
    normalized = normalize_text(label)
    if is_schedule_descriptor_column(label):
        return "date"
    if any(keyword in normalized for keyword in ("계", "합계", "총계", "소계", "total", "subtotal")):
        return "total"
    if any(keyword in normalized for keyword in ("비율", "비중", "증감률", "잔액율", "잔액률", "율", "rate", "ratio", "percent")):
        return "ratio"
    if re.search(r"\([a-z]\s*=", label, re.IGNORECASE):
        return "formula"
    if re.search(r"\([a-z]\s*/\s*[a-z]", label, re.IGNORECASE):
        return "ratio_formula"
    return "value"


def infer_row_role(label: str) -> str:
    normalized = normalize_text(label)
    if is_total_like(label):
        return "total"
    if normalized.startswith("소계") or "subtotal" in normalized:
        return "subtotal"
    if clean_display_text(label)[:2] in REGION_NAMES:
        return "region"
    if re.fullmatch(r"\d{4}", normalized):
        return "year"
    if any(keyword in normalized for keyword in ("비율", "비중", "증감률", "잔액율", "잔액률", "율")):
        return "ratio"
    return "item"


def total_label_kind(value: str) -> str | None:
    cleaned = clean_display_text(value)
    normalized = normalize_text(cleaned)
    if not cleaned:
        return None
    if re.match(r"^소\s*계(?:\s|$|[(:\[])", cleaned, re.IGNORECASE) or re.search(
        r"\bsub[ -]?total\b", cleaned, re.IGNORECASE
    ):
        return "subtotal"
    if re.match(r"^(?:합\s*계|총\s*계)(?:\s|$|[(:\[])", cleaned, re.IGNORECASE):
        return "total"
    if re.match(r"^계(?:\s+(?:total|sum)\b|\s*$|\s*[(:\[])", cleaned, re.IGNORECASE):
        return "total"
    if normalized in {"total", "grandtotal"} or re.match(r"^(?:grand\s+)?total\b", cleaned, re.IGNORECASE):
        return "total"
    return None


def region_name_from_label(value: str) -> str:
    normalized = normalize_text(value)
    return next((region for region in REGION_NAMES if normalized.startswith(region)), "")


def row_total_kind(table: ValidationTable, row: list) -> str | None:
    kinds: list[str] = []
    for col_index in leading_label_columns(table):
        value = cell_text_at(row, col_index)
        kind = total_label_kind(value)
        if kind:
            kinds.append(kind)
    if "subtotal" in kinds:
        return "subtotal"
    if "total" in kinds:
        return "total"
    return None


def cell_text_at(row: list, col_index: int) -> str:
    cell = row[col_index] if col_index < len(row) else None
    return clean_display_text(cell.text_value if cell else "")


def additive_cell_number(row: list, col_index: int) -> float | None:
    value = cell_number(row, col_index)
    if value is not None:
        return value

    text = cell_text_at(row, col_index)
    if text in {"-", "－", "―", "_", "＿"}:
        return 0.0
    return None


def additive_operand_cell_number(row: list, col_index: int) -> float | None:
    value = additive_cell_number(row, col_index)
    if value is not None:
        return value
    if col_index < len(row) and row[col_index] is not None and not cell_text_at(row, col_index):
        return 0.0
    return None


def additive_target_cell_number(row: list, col_index: int) -> float | None:
    return additive_cell_number(row, col_index)


def is_male_label(leaf: str, full_label: str) -> bool:
    normalized_leaf = normalize_text(leaf)
    return normalized_leaf in {"남", "남성", "male", "man"} or bool(re.search(r"\b(male|man)\b", full_label, re.IGNORECASE))


def is_female_label(leaf: str, full_label: str) -> bool:
    normalized_leaf = normalize_text(leaf)
    return normalized_leaf in {"여", "여성", "female", "woman"} or bool(re.search(r"\b(female|woman)\b", full_label, re.IGNORECASE))


def effective_header_value(table: ValidationTable, header_row_index: int, col_index: int) -> str:
    if header_row_index >= len(table.matrix):
        return ""
    row = table.matrix[header_row_index]
    value = cell_text_at(row, col_index)
    if value:
        return value
    for left_col_index in range(col_index - 1, -1, -1):
        left_value = cell_text_at(row, left_col_index)
        if left_value:
            return left_value
    return ""


def effective_header_parent_key(table: ValidationTable, col_index: int) -> tuple[str, ...]:
    values = [
        effective_header_value(table, row_index, col_index)
        for row_index in range(max(table.header_count - 1, 0))
    ]
    return tuple(value for value in values if value)


def effective_column_text(table: ValidationTable, col_index: int) -> str:
    parts: list[str] = []
    for row_index in range(table.header_count):
        value = effective_header_value(table, row_index, col_index)
        if value and value not in parts:
            parts.append(value)
    return " / ".join(parts) or table.column_text(col_index)


def effective_header_path(table: ValidationTable, col_index: int) -> list[str]:
    path: list[str] = []
    for row_index in range(table.header_count):
        value = effective_header_value(table, row_index, col_index)
        if value and (not path or normalize_text(path[-1]) != normalize_text(value)):
            path.append(value)
    return path


def total_like_header_position(path: list[str]) -> int | None:
    for index, value in enumerate(path):
        if total_label_kind(value) is not None:
            return index
    return None


def aggregate_group_prefix(table: ValidationTable, col_index: int) -> tuple[str, ...]:
    path = effective_header_path(table, col_index)
    position = total_like_header_position(path)
    if position is None:
        return tuple(path[:-1])
    return tuple(path[:position])


def normalized_path_startswith(path: list[str] | tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    if not prefix or len(path) < len(prefix):
        return False
    return all(normalize_text(path[index]) == normalize_text(value) for index, value in enumerate(prefix))


def is_total_column_candidate(table: ValidationTable, col_index: int) -> bool:
    leaf = leaf_header(table, col_index)
    if total_label_kind(leaf) is not None:
        return True
    if any(total_label_kind(value) is not None for value in effective_header_path(table, col_index)):
        return True

    label = effective_column_text(table, col_index)
    normalized = normalize_text(label)
    if normalized.startswith(("총", "총계", "합계")):
        return True
    if re.search(r"\b(total|grand\s+total|subtotal)\b", label, re.IGNORECASE):
        return True
    return False


def is_calculation_row_label(table: ValidationTable, row: list) -> bool:
    label = combined_row_label(table, row) or table.row_label(row)
    return (
        is_ratio_row_label(label)
        or is_growth_rate_label(label)
        or is_per_unit_measure_label(label)
        or is_average_row_label(label)
    )


def is_per_unit_measure_label(label: str) -> bool:
    cleaned = clean_display_text(label)
    normalized = normalize_text(cleaned)
    if re.search(r"(?:1인|인|건|개|명|회|가구|세대|대|기관)당", normalized):
        return True
    # In bilingual labels the English translation can contain phrases such as
    # "per Item" even when the row is an additive category (for example,
    # "세목별 ... / ... per Item"). Prefer the Korean wording when it is
    # available so those rows are not removed from total operands.
    if re.search(r"[가-힣]", cleaned):
        return False
    return bool(
        re.search(
            r"\b(?:per\s+(?:person|capita|case|task|item|household|organization|unit)|"
            r"(?:cost|amount|cases?|tasks?|items?)\s+per\s+\w+)",
            cleaned,
            re.IGNORECASE,
        )
    )


def is_average_row_label(label: str) -> bool:
    normalized = normalize_text(label)
    if any(keyword in normalized for keyword in ("평균", "평균액", "average", "mean")):
        return True
    return False


def row_sum_match_ratio(
    table: ValidationTable,
    target_col: int,
    operand_columns: list[int],
    *,
    tolerance: float,
) -> tuple[float, int]:
    passed_ratio, checked_count, _ = row_sum_match_summary(
        table,
        target_col,
        operand_columns,
        tolerance=tolerance,
    )
    return passed_ratio, checked_count


def row_sum_match_summary(
    table: ValidationTable,
    target_col: int,
    operand_columns: list[int],
    *,
    tolerance: float,
) -> tuple[float, int, float]:
    checked = 0
    passed = 0
    total_difference = 0.0
    for _, row in table.data_rows():
        if is_calculation_row_label(table, row):
            continue
        current = additive_target_cell_number(row, target_col)
        operands = [additive_operand_cell_number(row, col_index) for col_index in operand_columns]
        if current is None or any(value is None for value in operands):
            continue
        checked += 1
        expected = sum(value for value in operands if value is not None)
        difference = abs(current - expected)
        total_difference += difference
        if difference <= tolerance:
            passed += 1
    return (passed / checked if checked else 0.0, checked, total_difference)


def column_sum_match_ratio(
    table: ValidationTable,
    target_row: int,
    operand_rows: list[int],
    columns: list[int],
    *,
    tolerance: float,
) -> tuple[float, int]:
    if target_row >= len(table.matrix):
        return 0.0, 0
    checked = 0
    passed = 0
    target_matrix_row = table.matrix[target_row]
    for col_index in columns:
        current = additive_target_cell_number(target_matrix_row, col_index)
        values = [
            additive_operand_cell_number(table.matrix[row_index], col_index)
            for row_index in operand_rows
            if row_index < len(table.matrix)
        ]
        numeric_values = [value for value in values if value is not None]
        if current is None or len(numeric_values) < 2:
            continue
        checked += 1
        if abs(current - sum(numeric_values)) <= tolerance:
            passed += 1
    return (passed / checked if checked else 0.0, checked)


def same_row_ratio_match_ratio(
    table: ValidationTable,
    target_col: int,
    numerator_col: int,
    denominator_col: int,
    *,
    multiplier: float,
    tolerance: float,
) -> tuple[float, int]:
    checked = 0
    passed = 0
    for _, row in table.data_rows():
        current = cell_number(row, target_col)
        numerator = cell_number(row, numerator_col)
        denominator = cell_number(row, denominator_col)
        if current is None or numerator is None or denominator in {None, 0}:
            continue
        checked += 1
        expected = numerator / denominator * multiplier
        if abs(current - expected) <= tolerance:
            passed += 1
    return (passed / checked if checked else 0.0, checked)


def column_share_ratio_match_ratio(
    table: ValidationTable,
    target_col: int,
    numerator_col: int,
    denominator_row: int,
    denominator_col: int,
    *,
    multiplier: float,
    tolerance: float,
) -> tuple[float, int]:
    if denominator_row >= len(table.matrix):
        return 0.0, 0
    denominator = cell_number(table.matrix[denominator_row], denominator_col)
    if denominator in {None, 0}:
        return 0.0, 0

    checked = 0
    passed = 0
    for row_index, row in table.data_rows():
        if row_index == denominator_row:
            continue
        current = cell_number(row, target_col)
        numerator = cell_number(row, numerator_col)
        if current is None or numerator is None:
            continue
        checked += 1
        expected = numerator / denominator * multiplier
        if abs(current - expected) <= tolerance:
            passed += 1
    return (passed / checked if checked else 0.0, checked)


def row_ratio_by_rows_match_ratio(
    table: ValidationTable,
    target_row: int,
    numerator_row: int,
    denominator_row: int,
    columns: list[int],
    *,
    multiplier: float,
    tolerance: float,
) -> tuple[float, int]:
    if not all(row_index < len(table.matrix) for row_index in (target_row, numerator_row, denominator_row)):
        return 0.0, 0

    checked = 0
    passed = 0
    for col_index in columns:
        target_value = cell_number(table.matrix[target_row], col_index)
        numerator = cell_number(table.matrix[numerator_row], col_index)
        denominator = cell_number(table.matrix[denominator_row], col_index)
        if target_value is None or numerator is None or denominator in {None, 0}:
            continue
        checked += 1
        expected = numerator / denominator * multiplier
        if abs(target_value - expected) <= tolerance:
            passed += 1
    return (passed / checked if checked else 0.0, checked)


def row_ratio_by_sum_rows_match_ratio(
    table: ValidationTable,
    *,
    target_row: int,
    numerator_row: int,
    denominator_rows: list[int],
    columns: list[int],
    multiplier: float,
    tolerance: float,
) -> tuple[float, int]:
    if not all(
        row_index < len(table.matrix)
        for row_index in [target_row, numerator_row, *denominator_rows]
    ):
        return 0.0, 0

    checked = 0
    passed = 0
    for col_index in columns:
        target_value = cell_number(table.matrix[target_row], col_index)
        numerator = cell_number(table.matrix[numerator_row], col_index)
        denominator_values = [cell_number(table.matrix[row_index], col_index) for row_index in denominator_rows]
        if target_value is None or numerator is None or any(value is None for value in denominator_values):
            continue
        denominator = sum(value for value in denominator_values if value is not None)
        if denominator == 0:
            continue
        checked += 1
        expected = numerator / denominator * multiplier
        if abs(target_value - expected) <= tolerance:
            passed += 1
    return (passed / checked if checked else 0.0, checked)


def numeric_cell_count(table: ValidationTable, col_index: int) -> int:
    return sum(1 for _, row in table.data_rows() if cell_number(row, col_index) is not None)


def additive_cell_count(table: ValidationTable, col_index: int) -> int:
    return sum(1 for _, row in table.data_rows() if additive_cell_number(row, col_index) is not None)


def target_ratio_tolerance(table: ValidationTable, target_col: int, multiplier: float) -> float:
    if multiplier != 100:
        return 0.001

    decimal_places: list[int] = []
    for _, row in table.data_rows():
        cell = row[target_col] if target_col < len(row) else None
        if cell is None or cell_number(row, target_col) is None:
            continue
        text = clean_display_text(cell.text_value).replace(",", "").replace("%", "")
        match = re.fullmatch(r"[-+]?\d+(?:\.(\d+))?", text)
        if match:
            decimal_places.append(len(match.group(1) or ""))

    if decimal_places and max(decimal_places) == 0:
        return 0.5
    return 0.15


def infer_profile_checks(
    table: ValidationTable,
    *,
    analysis: dict[str, Any],
    templates: list[str],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    checks.extend(common_check_specs(table))
    if "regional_table" in templates:
        checks.extend(region_total_check_specs(table))

    checks.extend(infer_gender_total_rules(table))
    checks.extend(infer_header_formula_rules(table))
    checks.extend(infer_same_row_ratio_rules(table))
    checks.extend(infer_following_component_ratio_rules(table))
    checks.extend(infer_column_share_ratio_rules(table))
    checks.extend(infer_growth_rate_rules(table))
    checks.extend(infer_year_rows_change_rate_rules(table))
    checks.extend(infer_named_row_ratio_rules(table))
    checks.extend(infer_per_unit_ratio_rules(table))
    checks.extend(infer_row_based_ratio_rules(table))
    checks.extend(infer_row_based_growth_rate_rules(table))
    checks.extend(infer_wrapped_total_cell_rules(table))
    checks.extend(infer_total_column_rules(table))
    checks.extend(infer_total_row_rules(table))
    checks.extend(outlier_check_specs(table))

    for check in checks:
        if check.get("check_group") == "ratio":
            check.setdefault("aggregate_strategy", "recalculate_from_numerator_and_denominator")

    return dedupe_check_specs(checks)


def common_check_specs(table: ValidationTable) -> list[dict[str, Any]]:
    return [
        rule_spec(
            "metadata",
            {
                "id": f"profile.{table.code}.metadata_required",
                "type": "metadata_required",
                "category": "common",
                "label": "단위·기준일·출처 메타정보 확인",
                "fields": ["unit", "base_date", "source"],
                "expected_unit": table.unit,
                "confidence": 1.0,
            },
        ),
    ]


def region_total_check_specs(table: ValidationTable) -> list[dict[str, Any]]:
    if has_schedule_descriptor_column(table):
        return []

    rows = table.data_rows()
    target_items = [
        (row_index, row)
        for row_index, row in rows[:6]
        if row_total_kind(table, row) == "total"
    ]
    if not target_items:
        return []

    numeric_columns = additive_measure_columns(table)
    target_row_indices = {row_index for row_index, _ in target_items}
    populated_target_columns = {
        col_index
        for target_row_index, _ in target_items
        for col_index in numeric_columns
        if additive_target_cell_number(table.matrix[target_row_index], col_index) is not None
    }
    populated_detail_columns = {
        col_index
        for row_index, row in rows
        if row_index not in target_row_indices
        for col_index in numeric_columns
        if additive_operand_cell_number(row, col_index) is not None
    }
    if len(populated_target_columns) == 1 and len(populated_detail_columns) >= 2:
        return []

    specs: list[dict[str, Any]] = []
    for target_row_index, target_row in target_items:
        target_leaf = clean_display_text(row_leaf_label(table, target_row))
        target_leaf_key = normalize_text(target_leaf)
        has_series_leaf = bool(target_leaf_key) and total_label_kind(target_leaf) is None

        def same_series(row: list) -> bool:
            if not has_series_leaf:
                return True
            return normalize_text(row_leaf_label(table, row)) == target_leaf_key

        subtotal_rows = [
            row_index
            for row_index, row in rows
            if row_index not in target_row_indices
            and row_total_kind(table, row) == "subtotal"
            and same_series(row)
        ]
        detail_rows = [
            row_index
            for row_index, row in rows
            if row_index not in target_row_indices
            and row_total_kind(table, row) is None
            and same_series(row)
            and additive_operand_row(table, row)
            and not is_calculation_row_label(table, row)
        ]
        region_rows = [
            row_index
            for row_index in detail_rows
            if region_name_from_label(combined_row_label(table, table.matrix[row_index]))
        ]
        if len(region_rows) < 8:
            continue
        operand_rows = subtotal_rows if len(subtotal_rows) >= 2 else detail_rows
        if len(operand_rows) < 8:
            continue

        columns = [
            col_index
            for col_index in numeric_columns
            if additive_target_cell_number(target_row, col_index) is not None
            and sum(
                additive_operand_cell_number(table.matrix[row_index], col_index) is not None
                for row_index in operand_rows
            )
            >= 2
        ]
        if not columns:
            continue

        unique_regions = {
            region_name_from_label(combined_row_label(table, table.matrix[row_index]))
            for row_index in operand_rows
        }
        unique_regions.discard("")
        series_suffix = f" ({target_leaf})" if has_series_leaf else ""
        specs.append(
            rule_spec(
                "sum",
                {
                    "id": f"profile.{table.code}.region_total_r{target_row_index}",
                    "type": "region_total",
                    "category": "template",
                    "label": f"지역 합계{series_suffix} = 시도별 세부 값 합계",
                    "target_row": target_row_index,
                    "operand_rows": operand_rows,
                    "columns": columns,
                    "tolerance": sum_execution_tolerance(table),
                    "confidence": 0.95 if len(unique_regions) >= 15 else 0.82,
                },
            )
        )
    return specs


def has_schedule_descriptor_column(table: ValidationTable) -> bool:
    return any(
        is_schedule_descriptor_column(table.column_text(col_index))
        for col_index in range(column_count(table))
    )


def infer_wrapped_total_cell_rules(table: ValidationTable) -> list[dict[str, Any]]:
    """Detect side-by-side label/value blocks sharing one displayed grand total."""

    rows = table.data_rows()
    numeric_columns = additive_measure_columns(table)
    if len(numeric_columns) < 2:
        return []

    for target_row, row in rows[:5]:
        if row_total_kind(table, row) != "total":
            continue
        populated_targets = [
            col_index
            for col_index in numeric_columns
            if additive_target_cell_number(row, col_index) is not None
        ]
        if len(populated_targets) != 1:
            continue

        target_col = populated_targets[0]
        operand_cells: list[dict[str, int]] = []
        populated_operand_columns: set[int] = set()
        numeric_operand_count = 0
        for operand_row, operand_matrix_row in rows:
            if operand_row == target_row or row_total_kind(table, operand_matrix_row) is not None:
                continue
            for operand_col in numeric_columns:
                if operand_col >= len(operand_matrix_row) or operand_matrix_row[operand_col] is None:
                    continue
                operand_cells.append({"row": operand_row, "column": operand_col})
                value = additive_operand_cell_number(operand_matrix_row, operand_col)
                if value is not None:
                    populated_operand_columns.add(operand_col)
                    numeric_operand_count += 1

        if len(populated_operand_columns) < 2 or numeric_operand_count < 4:
            continue

        return [
            rule_spec(
                "sum",
                {
                    "id": f"profile.{table.code}.wrapped_total_r{target_row}_c{target_col}",
                    "type": "cell_sum",
                    "category": "table",
                    "label": "좌우 반복 구역 전체 합계 = 모든 구역 세부 셀 합계",
                    "target_row": target_row,
                    "target_column": target_col,
                    "operand_cells": operand_cells,
                    "tolerance": sum_execution_tolerance(table),
                    "confidence": 0.95,
                },
            )
        ]
    return []


def infer_total_column_rules(table: ValidationTable) -> list[dict[str, Any]]:
    numeric_columns = additive_measure_columns(table)
    specs: list[dict[str, Any]] = []
    for target_col in numeric_columns:
        if not is_total_column_candidate(table, target_col):
            continue

        direct_child_columns = direct_child_sum_operand_columns(table, target_col, numeric_columns)
        if direct_child_columns:
            passed_ratio, checked_count = row_sum_match_ratio(table, target_col, direct_child_columns, tolerance=1.0)
            if checked_count >= 1 and passed_ratio >= 0.8:
                specs.append(
                    rule_spec(
                        "sum",
                        {
                            "id": f"profile.{table.code}.row_total_direct_c{target_col}",
                            "type": "row_sum",
                            "category": "template",
                            "label": row_sum_rule_label(table, target_col, direct_child_columns),
                            "target_column": target_col,
                            "operand_columns": direct_child_columns,
                            "tolerance": sum_execution_tolerance(table),
                            "confidence": semantic_sum_confidence(passed_ratio, checked_count),
                        },
                    )
                )
                # The direct header children are the clearest hierarchy level.
                # A flattened alternative would duplicate the same aggregate.
                continue

        operand_columns = best_row_sum_operand_columns(table, target_col, numeric_columns)
        semantic_total = False
        if not operand_columns:
            operand_columns = semantic_flat_total_operand_columns(table, target_col, numeric_columns)
            if not operand_columns:
                continue
            semantic_total = True
        passed_ratio, checked_count = row_sum_match_ratio(table, target_col, operand_columns, tolerance=1.0)
        if semantic_total:
            matching_rows = matching_row_sum_indices(table, target_col, operand_columns, tolerance=1.0)
            if not matching_rows:
                continue
            restricted_rows = matching_rows if passed_ratio < 0.8 else []
        else:
            restricted_rows = []
            if (
                checked_count < row_sum_minimum_checked_count(operand_columns)
                or passed_ratio < sum_profile_minimum_ratio(checked_count)
            ):
                continue
        specs.append(
            rule_spec(
                "sum",
                {
                "id": f"profile.{table.code}.row_total_c{target_col}",
                "type": "row_sum",
                "category": "template",
                "label": row_sum_rule_label(table, target_col, operand_columns),
                "target_column": target_col,
                "operand_columns": operand_columns,
                "row_indices": restricted_rows,
                "tolerance": sum_execution_tolerance(table),
                "confidence": (
                    semantic_sum_confidence(passed_ratio, checked_count)
                    if semantic_total
                    else sum_profile_confidence(passed_ratio)
                ),
                },
            )
        )
    return specs


def direct_child_sum_operand_columns(
    table: ValidationTable,
    target_col: int,
    numeric_columns: list[int],
) -> list[int]:
    target_path = effective_header_path(table, target_col)
    total_position = total_like_header_position(target_path)
    if not target_path or total_position is None:
        return []

    if total_position == 0:
        top_level_children = top_level_summary_operand_columns(table, target_col, numeric_columns)
        best, _ = best_matching_row_sum_candidate(table, target_col, [top_level_children])
        return best

    candidate_groups: list[list[int]] = []
    nested_children: list[int] = []
    replacement_children: list[int] = []
    for col_index in numeric_columns:
        if col_index == target_col:
            continue
        path = effective_header_path(table, col_index)
        if total_label_kind(leaf_header(table, col_index)) is not None:
            continue
        if (
            len(path) == len(target_path) + 1
            and normalized_path_startswith(path, tuple(target_path))
        ):
            nested_children.append(col_index)
            continue
        if len(path) == len(target_path) and all(
            index == total_position or normalize_text(path[index]) == normalize_text(target_path[index])
            for index in range(len(target_path))
        ):
            replacement_children.append(col_index)

    if len(nested_children) >= 2:
        candidate_groups.append(nested_children)
    if len(replacement_children) >= 2:
        candidate_groups.append(replacement_children)
    best, _ = best_matching_row_sum_candidate(table, target_col, candidate_groups)
    return best


def semantic_flat_total_operand_columns(
    table: ValidationTable,
    target_col: int,
    numeric_columns: list[int],
) -> list[int]:
    """Return leaf columns when a standalone total column is structurally clear.

    This fallback intentionally does not depend on the current values matching;
    otherwise the very typo that validation should find can suppress the rule.
    """

    if re.search(r"\s표\d+$", table.code):
        return []
    target_path = effective_header_path(table, target_col)
    if len(target_path) != 1 or total_label_kind(target_path[0]) != "total":
        return []
    operands = [
        col_index
        for col_index in numeric_columns
        if col_index != target_col and not is_total_column_candidate(table, col_index)
    ]
    return operands if len(operands) >= 2 else []


def matching_row_sum_indices(
    table: ValidationTable,
    target_col: int,
    operand_columns: list[int],
    *,
    tolerance: float,
) -> list[int]:
    matching: list[int] = []
    for row_index, row in table.data_rows():
        if is_calculation_row_label(table, row):
            continue
        current = additive_target_cell_number(row, target_col)
        operands = [additive_operand_cell_number(row, col_index) for col_index in operand_columns]
        if current is None or any(value is None for value in operands):
            continue
        expected = sum(value for value in operands if value is not None)
        if abs(current - expected) <= tolerance:
            matching.append(row_index)
    return matching


def semantic_sum_confidence(passed_ratio: float, checked_count: int) -> float:
    if passed_ratio >= 0.95:
        return 0.98
    if checked_count >= 3 and passed_ratio >= 0.6:
        return 0.9
    return 0.82


def best_row_sum_operand_columns(
    table: ValidationTable,
    target_col: int,
    numeric_columns: list[int],
) -> list[int]:
    candidates: list[list[int]] = []

    sibling_total_operands = sibling_total_operand_columns(table, target_col, numeric_columns)
    if len(sibling_total_operands) >= 2:
        candidates.append(sibling_total_operands)
        passed_ratio, checked_count = row_sum_match_ratio(table, target_col, sibling_total_operands, tolerance=1.0)
        if checked_count >= row_sum_minimum_checked_count(sibling_total_operands) and passed_ratio >= sum_profile_minimum_ratio(checked_count):
            return sibling_total_operands

    exact_parent_operands = [
        col_index
        for col_index in numeric_columns
        if col_index != target_col
        and not is_total_column_candidate(table, col_index)
        and effective_header_parent_key(table, col_index) == effective_header_parent_key(table, target_col)
    ]
    if len(exact_parent_operands) >= 2:
        candidates.append(exact_parent_operands)
        passed_ratio, checked_count = row_sum_match_ratio(table, target_col, exact_parent_operands, tolerance=1.0)
        if checked_count >= row_sum_minimum_checked_count(exact_parent_operands) and passed_ratio >= sum_profile_minimum_ratio(checked_count):
            return exact_parent_operands

    group_prefix = aggregate_group_prefix(table, target_col)
    if group_prefix:
        group_operands = [
            col_index
            for col_index in numeric_columns
            if col_index != target_col
            and not is_total_column_candidate(table, col_index)
            and normalized_path_startswith(effective_header_path(table, col_index), group_prefix)
        ]
        if len(group_operands) >= 2:
            candidates.append(group_operands)
            passed_ratio, checked_count = row_sum_match_ratio(table, target_col, group_operands, tolerance=1.0)
            if checked_count >= row_sum_minimum_checked_count(group_operands) and passed_ratio >= sum_profile_minimum_ratio(checked_count):
                return group_operands

    same_leaf_operands = [
        col_index
        for col_index in numeric_columns
        if col_index != target_col
        and not is_total_column_candidate(table, col_index)
        and normalize_text(leaf_header(table, col_index)) == normalize_text(leaf_header(table, target_col))
    ]
    if len(same_leaf_operands) >= 2:
        candidates.append(same_leaf_operands)

    child_total_operands = [
        col_index
        for col_index in numeric_columns
        if col_index != target_col and is_total_column_candidate(table, col_index)
    ]
    if len(child_total_operands) >= 2:
        candidates.append(child_total_operands)

    top_level_summary_operands = top_level_summary_operand_columns(table, target_col, numeric_columns)
    if len(top_level_summary_operands) >= 2:
        candidates.append(top_level_summary_operands)

    all_leaf_operands = [
        col_index
        for col_index in numeric_columns
        if col_index != target_col and not is_total_column_candidate(table, col_index)
    ]
    if len(all_leaf_operands) >= 2:
        candidates.append(all_leaf_operands)

    best_candidate, best_ratio = best_matching_row_sum_candidate(table, target_col, candidates)
    if best_candidate and best_ratio >= 0.95:
        return best_candidate

    contiguous_candidates = row_sum_contiguous_candidates(target_col, all_leaf_operands)
    best_contiguous, contiguous_ratio = best_matching_row_sum_candidate(table, target_col, contiguous_candidates)
    if best_contiguous and contiguous_ratio >= 0.95:
        return best_contiguous

    combination_candidates = row_sum_combination_candidates(all_leaf_operands)
    fallback_candidates = [
        candidate
        for candidate in [best_candidate, best_contiguous]
        if candidate
    ]
    best_candidate, _ = best_matching_row_sum_candidate(
        table,
        target_col,
        [*fallback_candidates, *combination_candidates],
    )
    return best_candidate or []


def sibling_total_operand_columns(
    table: ValidationTable,
    target_col: int,
    numeric_columns: list[int],
) -> list[int]:
    if total_label_kind(leaf_header(table, target_col)) is None:
        return []

    target_leaf_key = normalize_text(leaf_header(table, target_col))
    target_parent_key = effective_header_parent_key(table, target_col)
    operands = [
        col_index
        for col_index in numeric_columns
        if col_index != target_col
        and total_label_kind(leaf_header(table, col_index)) is not None
        and normalize_text(leaf_header(table, col_index)) == target_leaf_key
        and effective_header_parent_key(table, col_index) != target_parent_key
    ]
    return operands


def top_level_summary_operand_columns(
    table: ValidationTable,
    target_col: int,
    numeric_columns: list[int],
) -> list[int]:
    groups: dict[str, list[int]] = {}
    group_order: list[str] = []
    for col_index in numeric_columns:
        if col_index == target_col:
            continue
        path = effective_header_path(table, col_index)
        if not path:
            continue
        root = path[0]
        root_key = normalize_text(root)
        if not root_key or total_label_kind(root) is not None:
            continue
        if root_key not in groups:
            groups[root_key] = []
            group_order.append(root_key)
        groups[root_key].append(col_index)

    operands: list[int] = []
    for group_key in group_order:
        columns = groups[group_key]
        aggregate_columns = [
            col_index
            for col_index in columns
            if is_total_column_candidate(table, col_index)
        ]
        if aggregate_columns:
            operands.extend(aggregate_columns)
        else:
            operands.extend(columns)
    return operands


def best_matching_row_sum_candidate(
    table: ValidationTable,
    target_col: int,
    candidates: list[list[int]],
) -> tuple[list[int], float]:
    scored_candidates: list[tuple[float, float, int, int, list[int]]] = []
    seen: set[tuple[int, ...]] = set()
    for operand_columns in candidates:
        key = tuple(operand_columns)
        if key in seen:
            continue
        seen.add(key)
        passed_ratio, checked_count, total_difference = row_sum_match_summary(
            table,
            target_col,
            operand_columns,
            tolerance=1.0,
        )
        if checked_count < row_sum_minimum_checked_count(operand_columns) or passed_ratio < sum_profile_minimum_ratio(checked_count):
            continue
        scored_candidates.append((passed_ratio, -total_difference, checked_count, -len(operand_columns), operand_columns))

    if not scored_candidates:
        return [], 0.0

    best = max(scored_candidates)
    return best[4], best[0]


def row_sum_contiguous_candidates(target_col: int, operand_columns: list[int]) -> list[list[int]]:
    candidates: list[list[int]] = []
    ordered = sorted(operand_columns)
    for start in range(len(ordered)):
        for end in range(start + 2, len(ordered) + 1):
            candidate = ordered[start:end]
            if target_col < candidate[0] or target_col > candidate[-1] or abs(target_col - candidate[-1]) <= 3:
                candidates.append(candidate)
    return candidates


def row_sum_combination_candidates(operand_columns: list[int]) -> list[list[int]]:
    if len(operand_columns) > 10:
        return []
    candidates: list[list[int]] = []
    max_size = min(len(operand_columns), 7)
    for size in range(2, max_size + 1):
        candidates.extend([list(candidate) for candidate in combinations(operand_columns, size)])
    return candidates


def row_sum_minimum_checked_count(operand_columns: list[int]) -> int:
    return 1 if len(operand_columns) >= 3 else 2


def row_sum_rule_label(table: ValidationTable, target_col: int, operand_columns: list[int]) -> str:
    target_label = leaf_header(table, target_col)
    if total_label_kind(target_label) is None:
        target_label = effective_column_text(table, target_col)
    leaf_labels = [leaf_header(table, col_index) for col_index in operand_columns]
    operand_labels = (
        [effective_column_text(table, col_index) for col_index in operand_columns]
        if len(set(normalize_text(label) for label in leaf_labels)) < len(leaf_labels)
        else leaf_labels
    )
    if len(operand_labels) <= 3:
        return f"{target_label} = {' + '.join(operand_labels)}"
    return f"{target_label} = 같은 행 세부 열 {len(operand_labels)}개 합계"


def infer_total_row_rules(table: ValidationTable) -> list[dict[str, Any]]:
    rows = table.data_rows()
    if len(rows) < 3:
        return []

    columns = additive_measure_columns(table)
    specs: list[dict[str, Any]] = []
    implicit_child_map = implicit_subtotal_child_map(table, rows)
    for target_row_index, operand_rows in implicit_child_map.items():
        if not operand_rows or not columns:
            continue
        if target_row_index >= len(table.matrix):
            continue
        passed_ratio, checked_count = column_sum_match_ratio_in_matrix(
            table.matrix,
            target_row_index,
            operand_rows,
            columns,
            tolerance=1.0,
            minimum_operands=1 if len(operand_rows) == 1 else 2,
        )
        if checked_count < 2 or passed_ratio < sum_profile_minimum_ratio(checked_count):
            continue
        target_label = combined_row_label(table, table.matrix[target_row_index]) or table.row_label(table.matrix[target_row_index])
        specs.append(
            rule_spec(
                "sum",
                {
                    "id": f"profile.{table.code}.implicit_subtotal_r{target_row_index}",
                    "type": "column_sum",
                    "category": "template",
                    "label": f"{clean_display_text(target_label)} = 내부 하위 행 합계",
                    "target_row": target_row_index,
                    "operand_rows": operand_rows,
                    "columns": columns,
                    "allow_single_operand": len(operand_rows) == 1,
                    "tolerance": 1.0,
                    "confidence": semantic_sum_confidence(passed_ratio, checked_count),
                },
            )
        )
    for position, (target_row_index, row) in enumerate(rows):
        row_label = combined_row_label(table, row)
        kind = row_total_kind(table, row)
        section_total = is_section_total_row_candidate(table, row)
        if kind is None and not section_total:
            continue
        candidates = candidate_total_operand_rows(table, rows, position, kind, section_total, implicit_child_map)
        selected = select_total_operand_rows(
            table,
            target_row_index=target_row_index,
            target_row=row,
            candidates=candidates,
            columns=columns,
            kind=kind,
        )
        for operand_rows in ([selected] if selected else []):
            if len(operand_rows) < 2 or not columns:
                continue
            passed_ratio, checked_count = column_sum_match_ratio(table, target_row_index, operand_rows, columns, tolerance=1.0)
            force_year_total = clear_year_total_candidate(table, kind, operand_rows, checked_count)
            minimum_evidence = minimum_sum_evidence_count(columns, kind)
            if checked_count < minimum_evidence or (
                passed_ratio < sum_profile_minimum_ratio(checked_count) and not force_year_total
            ):
                continue
            specs.append(
                rule_spec(
                    "sum",
                    {
                        "id": f"profile.{table.code}.column_total_r{target_row_index}",
                        "type": "column_sum",
                        "category": "template",
                        "label": f"{clean_display_text(row_label)} = 하위 행 합계",
                        "target_row": target_row_index,
                        "operand_rows": operand_rows,
                        "columns": columns,
                        "tolerance": sum_execution_tolerance(table),
                        "confidence": 0.65 if force_year_total and passed_ratio < sum_profile_minimum_ratio(checked_count) else sum_profile_confidence(passed_ratio),
                    },
                )
            )
    return specs


def select_total_operand_rows(
    table: ValidationTable,
    *,
    target_row_index: int,
    target_row: list,
    candidates: list[list[int]],
    columns: list[int],
    kind: str | None,
) -> list[int]:
    """Choose one immediate hierarchy level for each aggregate row.

    Grand totals prefer subtotal/section-total rows. This keeps a grand total
    expressed as ``subtotal A + subtotal B`` instead of re-adding every leaf,
    while each subtotal still receives its own independent validation rule.
    """

    target_leaf = normalize_text(row_leaf_label(table, target_row))
    scored: list[tuple[int, float, int, int, list[int]]] = []
    for operand_rows in candidates:
        passed_ratio, checked_count = column_sum_match_ratio(
            table,
            target_row_index,
            operand_rows,
            columns,
            tolerance=1.0,
        )
        force_year_total = clear_year_total_candidate(table, kind, operand_rows, checked_count)
        minimum_evidence = minimum_sum_evidence_count(columns, kind)
        if checked_count < minimum_evidence or (
            passed_ratio < sum_profile_minimum_ratio(checked_count) and not force_year_total
        ):
            continue

        operand_matrix_rows = [table.matrix[index] for index in operand_rows if index < len(table.matrix)]
        aggregate_children = all(
            row_total_kind(table, operand_row) == "subtotal"
            or is_section_total_row_candidate(table, operand_row)
            for operand_row in operand_matrix_rows
        )
        same_leaf_children = bool(target_leaf) and all(
            normalize_text(row_leaf_label(table, operand_row)) == target_leaf
            for operand_row in operand_matrix_rows
        )
        hierarchy_priority = 5 if aggregate_children else 4 if same_leaf_children else 3
        if kind == "subtotal" and aggregate_children:
            hierarchy_priority = 2
        scored.append(
            (
                hierarchy_priority,
                passed_ratio,
                checked_count,
                -len(operand_rows),
                operand_rows,
            )
        )

    return max(scored)[4] if scored else []


def minimum_sum_evidence_count(columns: list[int], kind: str | None) -> int:
    """Require less cross-column evidence only for an explicit aggregate row."""

    if len(columns) == 1 and kind in {"total", "subtotal"}:
        return 1
    return 2


def sum_profile_minimum_ratio(checked_count: int) -> float:
    return 0.6 if checked_count >= 3 else 0.8


def sum_profile_confidence(passed_ratio: float) -> float:
    if passed_ratio >= 0.95:
        return 0.96
    if passed_ratio >= 0.8:
        return 0.9
    return 0.76


def sum_execution_tolerance(table: ValidationTable) -> float:
    if count_like_sum_unit(table.unit):
        return 0.0
    return 1.0


def count_like_sum_unit(unit: str) -> bool:
    if any(separator in unit for separator in (",", "%", "/", "·", "･")):
        return False
    normalized = normalize_text(unit)
    if not normalized:
        return False
    if any(keyword in normalized for keyword in ("원", "krw", "금액", "면적", "㎡", "km", "㎞", "㎢", "ha", "천", "백만")):
        return False
    return normalized in {"명", "개", "개소", "건", "회", "종", "곳", "대", "가구", "세대", "필지", "동"}


def clear_year_total_candidate(
    table: ValidationTable,
    kind: str | None,
    operand_rows: list[int],
    checked_count: int,
) -> bool:
    if kind != "total" or checked_count < 2 or len(operand_rows) < 4:
        return False
    labels = [
        normalize_text(combined_row_label(table, table.matrix[row_index]) or table.row_label(table.matrix[row_index]))
        for row_index in operand_rows
        if row_index < len(table.matrix)
    ]
    year_labels = [label for label in labels if re.fullmatch(r"\d{4}", label)]
    return len(year_labels) >= 4 and len(year_labels) / len(labels) >= 0.8


def candidate_total_operand_rows(
    table: ValidationTable,
    rows: list[tuple[int, list]],
    position: int,
    kind: str | None,
    section_total: bool,
    implicit_child_map: dict[int, list[int]],
) -> list[list[int]]:
    target_row_index, _ = rows[position]
    following = following_rows_for_total_candidate(table, rows, position, kind, section_total)
    candidates: list[list[int]] = []

    if kind == "total" and position >= len(rows) // 2:
        preceding_rows = preceding_detail_rows_for_total_candidate(table, rows, position)
        if len(preceding_rows) >= 2:
            candidates.append(preceding_rows)

    immediate_rows: list[int] = []
    for row_index, row in following:
        if row_total_kind(table, row) is not None or is_section_total_row_candidate(table, row):
            break
        if additive_operand_row(table, row):
            immediate_rows.append(row_index)
    if len(immediate_rows) >= 2:
        candidates.append(immediate_rows)

    aggregate_rows = [
        row_index
        for row_index, row in following
        if row_index != target_row_index
        and (row_total_kind(table, row) == "subtotal" or is_section_total_row_candidate(table, row))
        and additive_operand_row(table, row)
    ]
    if len(aggregate_rows) >= 2:
        candidates.append(aggregate_rows)

    collapsed_rows = collapsed_detail_operand_rows(table, following, implicit_child_map)
    if len(collapsed_rows) >= 2:
        candidates.append(collapsed_rows)

    target_leaf_key = normalize_text(row_leaf_label(table, rows[position][1]))
    if target_leaf_key and total_label_kind(target_leaf_key) is None:
        matching_leaf_rows = [
            row_index
            for row_index, row in following
            if row_total_kind(table, row) is None
            and not is_section_total_row_candidate(table, row)
            and additive_operand_row(table, row)
            and normalize_text(row_leaf_label(table, row)) == target_leaf_key
        ]
        if len(matching_leaf_rows) >= 2:
            candidates.append(matching_leaf_rows)

    group_rows: dict[str, list[int]] = {}
    for row_index, row in following:
        if row_total_kind(table, row) is not None or is_section_total_row_candidate(table, row):
            continue
        if not additive_operand_row(table, row):
            continue
        group_key = row_group_key(table, row)
        if not group_key:
            continue
        group_rows.setdefault(group_key, []).append(row_index)
    for operand_rows in group_rows.values():
        if len(operand_rows) >= 2:
            candidates.append(operand_rows)

    all_detail_rows = [
        row_index
        for row_index, row in following
        if row_total_kind(table, row) is None
        and not is_section_total_row_candidate(table, row)
        and additive_operand_row(table, row)
    ]
    if len(all_detail_rows) >= 2:
        candidates.append(all_detail_rows)

    return unique_row_candidates(candidates)


def preceding_detail_rows_for_total_candidate(
    table: ValidationTable,
    rows: list[tuple[int, list]],
    position: int,
) -> list[int]:
    operands: list[int] = []
    for row_index, row in reversed(rows[:position]):
        if row_total_kind(table, row) is not None or is_section_total_row_candidate(table, row):
            break
        if additive_operand_row(table, row):
            operands.append(row_index)
    operands.reverse()
    return operands


def collapsed_detail_operand_rows(
    table: ValidationTable,
    following: list[tuple[int, list]],
    child_map: dict[int, list[int]],
) -> list[int]:
    collapsed: list[int] = []
    skipped_children: set[int] = set()
    for row_index, row in following:
        if row_index in skipped_children:
            continue
        if row_total_kind(table, row) is not None or is_section_total_row_candidate(table, row):
            continue
        if not additive_operand_row(table, row):
            continue
        collapsed.append(row_index)
        skipped_children.update(child_map.get(row_index, []))
    return collapsed


def implicit_subtotal_child_map(
    table: ValidationTable,
    rows: list[tuple[int, list]],
) -> dict[int, list[int]]:
    columns = additive_measure_columns(table)
    if len(columns) < 2:
        return {}

    matrix = table.matrix
    child_map: dict[int, list[int]] = {}
    for position, (target_row_index, target_row) in enumerate(rows):
        if row_total_kind(table, target_row) is not None or is_section_total_row_candidate(table, target_row):
            continue
        if not additive_operand_row(table, target_row):
            continue

        structural_children = structural_nested_child_rows(table, rows, position)
        if structural_children:
            passed_ratio, checked_count = column_sum_match_ratio_in_matrix(
                matrix,
                target_row_index,
                structural_children,
                columns,
                tolerance=1.0,
                minimum_operands=1,
            )
            if checked_count >= 2 and passed_ratio >= sum_profile_minimum_ratio(checked_count):
                child_map[target_row_index] = structural_children
                continue

        if not implicit_subtotal_value_candidate(table, target_row):
            continue

        detail_rows: list[int] = []
        for row_index, row in rows[position + 1 :]:
            if row_total_kind(table, row) is not None or is_section_total_row_candidate(table, row):
                break
            if additive_operand_row(table, row):
                detail_rows.append(row_index)

        max_prefix = min(len(detail_rows), 30)
        for size in range(2, max_prefix + 1):
            candidate_rows = detail_rows[:size]
            if flat_region_rows_are_not_implicit_children(table, target_row_index, candidate_rows):
                continue
            passed_ratio, checked_count = column_sum_match_ratio_in_matrix(
                matrix,
                target_row_index,
                candidate_rows,
                columns,
                tolerance=implicit_subtotal_inference_tolerance(table, target_row),
            )
            if checked_count >= 2 and passed_ratio >= 0.95:
                child_map[target_row_index] = candidate_rows
                break

    return child_map


def implicit_subtotal_value_candidate(table: ValidationTable, row: list) -> bool:
    """Reject ordered data rows that only happen to resemble a subtotal.

    Value-only subtotal inference is intentionally stricter than execution.
    Ranks, years, dates, and numbered event rows describe an ordered series;
    they are not aggregate labels even when nearby values or dashes add up.
    """

    parts = [clean_display_text(value) for value in row_label_parts(table, row) if clean_display_text(value)]
    if not parts:
        return False
    first_display = parts[0]
    first = normalize_text(first_display)
    if re.fullmatch(r"\d{1,4}", first):
        return False
    if re.match(r"^[’']?\d{2,4}[./-]\d", first_display):
        return False
    return True


def implicit_subtotal_inference_tolerance(table: ValidationTable, row: list) -> float:
    label_display = clean_display_text(combined_row_label(table, row) or table.row_label(row))
    label = normalize_text(label_display)
    has_total_word = bool(
        re.search(r"(?:^|[\s/])(?:계|합계|총계|소계)(?=$|[\s/(:])", label_display, re.IGNORECASE)
        or re.search(r"\b(?:grand\s+total|sub[ -]?total|total)\b", label_display, re.IGNORECASE)
    )
    aggregate_keywords = (
        "전체",
        "전국",
        "규모",
        "수입",
        "자산",
        "예산",
        "운행대수",
        "지방자치단체",
        "일반직",
        "특별회계",
        "일반회계",
        "조세",
        "소속기관",
        "total",
        "aggregate",
        "scale",
        "revenue",
        "assets",
        "budget",
    )
    return 1.0 if has_total_word or any(keyword in label for keyword in aggregate_keywords) else 0.01


def structural_nested_child_rows(
    table: ValidationTable,
    rows: list[tuple[int, list]],
    position: int,
) -> list[int]:
    label_columns = leading_label_columns(table)
    if len(label_columns) < 2:
        return []
    _, target_row = rows[position]
    target_depth = row_label_depth(target_row, label_columns)
    if target_depth is None:
        return []

    children: list[int] = []
    for row_index, row in rows[position + 1 :]:
        depth = row_label_depth(row, label_columns)
        if depth is None:
            continue
        if depth <= target_depth:
            break
        if additive_operand_row(table, row):
            children.append(row_index)
    return children


def row_label_depth(row: list, label_columns: list[int]) -> int | None:
    populated = [
        depth
        for depth, col_index in enumerate(label_columns)
        if clean_display_text(cell_text_at(row, col_index))
    ]
    return min(populated) if populated else None


def flat_region_rows_are_not_implicit_children(
    table: ValidationTable,
    target_row_index: int,
    candidate_rows: list[int],
) -> bool:
    if target_row_index >= len(table.matrix):
        return False
    if not plain_region_row(table, table.matrix[target_row_index]):
        return False
    return all(
        row_index < len(table.matrix) and plain_region_row(table, table.matrix[row_index])
        for row_index in candidate_rows
    )


def plain_region_row(table: ValidationTable, row: list) -> bool:
    parts = [clean_display_text(part) for part in row_label_parts(table, row) if clean_display_text(part)]
    if len(parts) != 1:
        return False
    normalized = normalize_text(parts[0])
    return any(normalized.startswith(region) for region in REGION_NAMES)


def column_sum_match_ratio_in_matrix(
    matrix: list[list],
    target_row: int,
    operand_rows: list[int],
    columns: list[int],
    *,
    tolerance: float,
    minimum_operands: int = 2,
) -> tuple[float, int]:
    if target_row >= len(matrix):
        return 0.0, 0
    checked = 0
    passed = 0
    target_matrix_row = matrix[target_row]
    for col_index in columns:
        current = additive_target_cell_number(target_matrix_row, col_index)
        values = [
            additive_operand_cell_number(matrix[row_index], col_index)
            for row_index in operand_rows
            if row_index < len(matrix)
        ]
        numeric_values = [value for value in values if value is not None]
        if current is None or len(numeric_values) < minimum_operands:
            continue
        checked += 1
        if abs(current - sum(numeric_values)) <= tolerance:
            passed += 1
    return (passed / checked if checked else 0.0, checked)


def following_rows_for_total_candidate(
    table: ValidationTable,
    rows: list[tuple[int, list]],
    position: int,
    kind: str | None,
    section_total: bool,
) -> list[tuple[int, list]]:
    target_row = rows[position][1]
    following: list[tuple[int, list]] = []
    for row_index, row in rows[position + 1 :]:
        operand_kind = row_total_kind(table, row)
        operand_section_total = is_section_total_row_candidate(table, row)
        if kind == "subtotal" and (operand_kind is not None or operand_section_total):
            break
        if section_total and (operand_kind is not None or operand_section_total):
            break
        if kind == "total" and operand_kind == "total" and not sibling_total_row(table, target_row, row):
            break
        following.append((row_index, row))
    return following


def additive_measure_columns(table: ValidationTable) -> list[int]:
    label_columns = set(leading_label_columns(table))
    return [
        col_index
        for col_index in range(column_count(table))
        if col_index not in label_columns
        and additive_measure_column_candidate(table, col_index)
        and additive_cell_count(table, col_index) >= 1
    ]


def additive_measure_column_candidate(table: ValidationTable, col_index: int) -> bool:
    label = effective_column_text(table, col_index)
    normalized = normalize_text(label)
    if is_same_row_ratio_column_label(label) or is_growth_rate_label(label):
        return False
    if any(keyword in normalized for keyword in ("평균", "평균액", "1인당", "피해내용", "내용", "비고", "remarks")):
        return False
    if re.search(
        r"\b(rate|average|ratio|percent|percentage|per\s+person|per\s+capita|each\s+worker|assigned\s+to\s+each|details?|remarks?)\b",
        label,
        re.IGNORECASE,
    ):
        return False
    return is_additive_column_label(label)


def sibling_total_row(table: ValidationTable, target_row: list, row: list) -> bool:
    target_parts = row_label_parts(table, target_row)
    row_parts = row_label_parts(table, row)
    if len(target_parts) < 2 or len(row_parts) < 2:
        return False
    return (
        total_label_kind(target_parts[0]) == "total"
        and total_label_kind(row_parts[0]) == "total"
        and normalize_text(row_leaf_label(table, target_row)) != normalize_text(row_leaf_label(table, row))
    )


def unique_row_candidates(candidates: list[list[int]]) -> list[list[int]]:
    unique: list[list[int]] = []
    seen: set[tuple[int, ...]] = set()
    for candidate in candidates:
        key = tuple(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def additive_operand_row(table: ValidationTable, row: list) -> bool:
    label = combined_row_label(table, row) or table.row_label(row)
    normalized = normalize_text(label)
    if is_calculation_row_label(table, row):
        return False
    if any(keyword in normalized for keyword in ("평균", "평균액", "average", "mean")):
        return False
    return row_additive_cell_count(table, row) >= 1


def row_numeric_cell_count(table: ValidationTable, row: list) -> int:
    label_columns = set(leading_label_columns(table))
    return sum(
        1
        for col_index in range(column_count(table))
        if col_index not in label_columns and cell_number(row, col_index) is not None
    )


def row_additive_cell_count(table: ValidationTable, row: list) -> int:
    label_columns = set(leading_label_columns(table))
    return sum(
        1
        for col_index in range(column_count(table))
        if col_index not in label_columns and additive_operand_cell_number(row, col_index) is not None
    )


def row_group_key(table: ValidationTable, row: list) -> str:
    label_columns = leading_label_columns(table)
    if not label_columns:
        return ""
    return normalize_text(cell_text_at(row, label_columns[0]))


def is_section_total_row_candidate(table: ValidationTable, row: list) -> bool:
    if row_total_kind(table, row) is not None:
        return False
    if row_numeric_cell_count(table, row) < 1:
        return False
    label_columns = leading_label_columns(table)
    first_label = cell_text_at(row, label_columns[0] if label_columns else 0)
    cleaned = clean_display_text(first_label)
    return bool(re.match(r"^\d+\s*[.)]", cleaned))


def dedupe_check_specs(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for check in checks:
        check_type = check.get("type")
        if check_type in {"region_total", "column_sum"}:
            check_type = "column_sum"
        key = (
            check_type,
            check.get("target_column"),
            check.get("value_column"),
            check.get("rate_column"),
            check.get("change_column"),
            tuple(check.get("operand_columns", [])),
            check.get("denominator_column"),
            tuple(check.get("denominator_columns", [])),
            check.get("target_row"),
            check.get("source_row"),
            check.get("numerator_row"),
            check.get("denominator_row"),
            tuple(check.get("denominator_rows", [])),
            tuple(check.get("columns", [])),
            tuple(check.get("row_indices", [])),
            tuple(check.get("operand_rows", [])),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(check)
    return deduped


def profile_status(checks: list[dict[str, Any]], *, needs_llm_review: bool) -> str:
    if needs_llm_review:
        return "needs_review"

    specific_checks = [check for check in checks if check.get("category") in {"template", "table"}]
    if not specific_checks:
        return "needs_review"

    low_confidence = [float(check.get("confidence", 0)) for check in specific_checks]
    if low_confidence and min(low_confidence) < 0.7:
        return "needs_review"

    return "ready"


def infer_table_rules(table: ValidationTable) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    rules.extend(infer_gender_total_rules(table))
    rules.extend(infer_header_formula_rules(table))
    return rules


def infer_gender_total_rules(table: ValidationTable) -> list[dict[str, Any]]:
    label_columns = set(leading_label_columns(table))
    specs: list[dict[str, Any]] = []
    for target_col in range(column_count(table)):
        if target_col in label_columns or total_label_kind(leaf_header(table, target_col)) is None:
            continue
        parent_key = effective_header_parent_key(table, target_col)
        male_col = next(
            (
                col_index
                for col_index in range(column_count(table))
                if col_index not in label_columns
                and col_index != target_col
                and effective_header_parent_key(table, col_index) == parent_key
                and is_male_label(leaf_header(table, col_index), table.column_text(col_index))
            ),
            None,
        )
        female_col = next(
            (
                col_index
                for col_index in range(column_count(table))
                if col_index not in label_columns
                and col_index != target_col
                and effective_header_parent_key(table, col_index) == parent_key
                and is_female_label(leaf_header(table, col_index), table.column_text(col_index))
            ),
            None,
        )
        if male_col is None or female_col is None or len({target_col, male_col, female_col}) != 3:
            continue
        passed_ratio, checked_count = row_sum_match_ratio(table, target_col, [male_col, female_col], tolerance=1.0)
        if checked_count < 3 or passed_ratio < 0.8:
            continue
        specs.append(
            rule_spec(
                "sum",
                {
                    "id": f"profile.{table.code}.gender_total_c{target_col}",
                    "type": "row_sum",
                    "category": "table",
                    "label": f"{leaf_header(table, target_col)} = {leaf_header(table, male_col)} + {leaf_header(table, female_col)}",
                    "target_column": target_col,
                    "operand_columns": [male_col, female_col],
                    "tolerance": sum_execution_tolerance(table),
                    "confidence": 0.97 if passed_ratio >= 0.95 else 0.9,
                },
            )
        )
    return specs


def infer_header_formula_rules(table: ValidationTable) -> list[dict[str, Any]]:
    symbol_to_column = symbol_columns(table)
    rules: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    for col_index in range(column_count(table)):
        label = table.column_text(col_index)
        for match in re.finditer(r"\(([a-z])\s*=\s*([a-z](?:\s*[+-]\s*[a-z])+)\)", label, re.IGNORECASE):
            target_symbol = match.group(1).lower()
            target_col = symbol_to_column.get(target_symbol)
            if target_col is None:
                continue
            terms = expression_terms(match.group(2), symbol_to_column)
            if not terms:
                continue
            key = ("arithmetic", target_col)
            if key in seen:
                continue
            seen.add(key)
            rules.append(
                rule_spec(
                    "sum",
                    {
                        "id": f"profile.{table.code}.{target_symbol}_arithmetic",
                        "type": "row_arithmetic",
                        "category": "table",
                        "label": f"{target_symbol}={match.group(2).replace(' ', '')}",
                        "target_column": target_col,
                        "terms": terms,
                        "tolerance": 0.5,
                        "confidence": 0.98,
                    },
                )
            )

        for match in re.finditer(r"\(([a-z](?:\s*[+-]\s*[a-z])+)\)", label, re.IGNORECASE):
            if is_ratio_denominator_expression(label, match.start()):
                continue
            expression = match.group(1)
            terms = expression_terms(expression, symbol_to_column)
            if not terms:
                continue
            key = ("arithmetic", col_index)
            if key in seen:
                continue
            seen.add(key)
            clean_expression = expression.replace(" ", "").lower()
            check_type = "증감액 검수" if is_change_amount_label(label) else "합계 검수"
            rules.append(
                rule_spec(
                    "sum",
                    {
                        "id": f"profile.{table.code}.c{col_index}_arithmetic",
                        "type": "row_arithmetic",
                        "category": "table",
                        "check_type": check_type,
                        "label": f"{leaf_header(table, col_index)} = {clean_expression}",
                        "target_column": col_index,
                        "terms": terms,
                        "tolerance": 0.5,
                        "confidence": 0.98,
                    },
                )
            )

        for match in re.finditer(r"\(([a-z])\s*/\s*([a-z])(?:\s*\*\s*100)?\)", label, re.IGNORECASE):
            numerator_symbol = match.group(1).lower()
            denominator_symbol = match.group(2).lower()
            numerator_col = symbol_to_column.get(numerator_symbol)
            denominator_col = symbol_to_column.get(denominator_symbol)
            if numerator_col is None or denominator_col is None:
                continue
            key = ("ratio", col_index)
            if key in seen:
                continue
            seen.add(key)
            dependency_columns = arithmetic_dependency_columns(table, numerator_col, symbol_to_column)
            dependency_columns.extend(arithmetic_dependency_columns(table, denominator_col, symbol_to_column))
            dependency_columns = unique_ints(dependency_columns)
            rules.append(
                rule_spec(
                    "ratio",
                    {
                        "id": f"profile.{table.code}.{numerator_symbol}_{denominator_symbol}_ratio",
                        "type": "row_ratio",
                        "category": "table",
                        "label": f"{numerator_symbol}/{denominator_symbol}*100",
                        "target_column": col_index,
                        "numerator_column": numerator_col,
                        "denominator_column": denominator_col,
                        "multiplier": 100,
                        "tolerance": 0.15,
                        "confidence": 0.98,
                        "dependency_columns": dependency_columns,
                    },
                )
            )

        for match in re.finditer(r"([a-z])\s*/\s*\(([a-z](?:\s*\+\s*[a-z])+)\)", label, re.IGNORECASE):
            numerator_symbol = match.group(1).lower()
            denominator_expression = match.group(2).lower().replace(" ", "")
            numerator_col = symbol_to_column.get(numerator_symbol)
            denominator_columns = [
                symbol_to_column[symbol]
                for symbol in re.findall(r"[a-z]", denominator_expression)
                if symbol in symbol_to_column
            ]
            if numerator_col is None or len(denominator_columns) < 2:
                continue
            key = ("ratio", col_index)
            if key in seen:
                continue
            seen.add(key)
            dependency_columns = arithmetic_dependency_columns(table, numerator_col, symbol_to_column)
            for denominator_col in denominator_columns:
                dependency_columns.extend(arithmetic_dependency_columns(table, denominator_col, symbol_to_column))
            dependency_columns = unique_ints(dependency_columns)
            rules.append(
                rule_spec(
                    "ratio",
                    {
                        "id": f"profile.{table.code}.{numerator_symbol}_sum_ratio_c{col_index}",
                        "type": "row_ratio",
                        "category": "table",
                        "label": f"{numerator_symbol}/({denominator_expression})*100",
                        "target_column": col_index,
                        "numerator_column": numerator_col,
                        "denominator_columns": denominator_columns,
                        "multiplier": 100,
                        "tolerance": target_ratio_tolerance(table, col_index, 100),
                        "confidence": 0.98,
                        "dependency_columns": dependency_columns,
                    },
                )
            )

    return rules


def infer_same_row_ratio_rules(table: ValidationTable) -> list[dict[str, Any]]:
    label_columns = set(leading_label_columns(table))
    numeric_counts = {col_index: numeric_cell_count(table, col_index) for col_index in range(column_count(table))}
    source_columns = [
        col_index
        for col_index in range(column_count(table))
        if numeric_counts[col_index] >= 1
    ]
    target_columns = [
        col_index
        for col_index in source_columns
        if col_index not in label_columns
    ]
    specs: list[dict[str, Any]] = []

    for target_col in target_columns:
        target_label = effective_column_text(table, target_col)
        if not is_same_row_ratio_column_label(target_label):
            continue

        candidates: list[tuple[float, int, int, int, int, int, float]] = []
        for numerator_col in source_columns:
            if numerator_col == target_col:
                continue
            for denominator_col in source_columns:
                if denominator_col in {target_col, numerator_col}:
                    continue
                for multiplier in (100.0, 1.0):
                    tolerance = target_ratio_tolerance(table, target_col, multiplier)
                    passed_ratio, checked_count = same_row_ratio_match_ratio(
                        table,
                        target_col,
                        numerator_col,
                        denominator_col,
                        multiplier=multiplier,
                        tolerance=tolerance,
                    )
                    semantic_score = same_row_ratio_candidate_score(
                        table,
                        target_col=target_col,
                        numerator_col=numerator_col,
                        denominator_col=denominator_col,
                    )
                    minimum_checked = 1 if numeric_counts[target_col] == 1 and semantic_score >= 6 else 3
                    if checked_count < minimum_checked or passed_ratio < 0.8:
                        continue
                    distance_score = abs(target_col - numerator_col) + abs(target_col - denominator_col)
                    candidates.append(
                        (
                            passed_ratio,
                            checked_count,
                            semantic_score,
                            -distance_score,
                            numerator_col,
                            denominator_col,
                            multiplier,
                        )
                    )

        if not candidates:
            continue

        passed_ratio, checked_count, _, _, numerator_col, denominator_col, multiplier = max(candidates)
        dependency_columns = arithmetic_dependency_columns(table, numerator_col)
        dependency_columns.extend(arithmetic_dependency_columns(table, denominator_col))
        dependency_columns = unique_ints(dependency_columns)
        specs.append(
            rule_spec(
                "ratio",
                {
                    "id": f"profile.{table.code}.same_row_ratio_c{target_col}",
                    "type": "row_ratio",
                    "category": "table",
                    "label": f"{leaf_header(table, target_col)} = {leaf_header(table, numerator_col)} / {leaf_header(table, denominator_col)} * {multiplier:g}",
                    "target_column": target_col,
                    "numerator_column": numerator_col,
                    "denominator_column": denominator_col,
                    "multiplier": multiplier,
                    "tolerance": target_ratio_tolerance(table, target_col, multiplier),
                    "confidence": 0.97 if passed_ratio >= 0.95 else 0.9,
                    "matched_rows": checked_count,
                    "dependency_columns": dependency_columns,
                },
            )
        )

    return specs


def infer_following_component_ratio_rules(table: ValidationTable) -> list[dict[str, Any]]:
    """Infer ``part / (part + complement)`` rows such as disclosure rate."""

    rows = table.data_rows()
    specs: list[dict[str, Any]] = []
    for position, (target_row_index, target_row) in enumerate(rows):
        target_label = combined_row_label(table, target_row) or table.row_label(target_row)
        if not is_ratio_row_label(target_label) or is_growth_rate_label(target_label):
            continue
        following_values = [
            (row_index, row)
            for row_index, row in rows[position + 1 : position + 4]
            if row_total_kind(table, row) is None
            and not is_ratio_row_label(combined_row_label(table, row) or table.row_label(row))
        ]
        if len(following_values) < 2:
            continue

        numerator_row_index, numerator_row = following_values[0]
        complement_row_index, complement_row = following_values[1]
        columns = shared_numeric_columns(table, [target_row, numerator_row, complement_row])
        if len(columns) < 2:
            continue
        passed_ratio, checked_count = row_ratio_by_sum_rows_match_ratio(
            table,
            target_row=target_row_index,
            numerator_row=numerator_row_index,
            denominator_rows=[numerator_row_index, complement_row_index],
            columns=columns,
            multiplier=100,
            tolerance=0.15,
        )
        if checked_count < 2 or passed_ratio < 0.8:
            continue

        numerator_label = combined_row_label(table, numerator_row) or table.row_label(numerator_row)
        complement_label = combined_row_label(table, complement_row) or table.row_label(complement_row)
        specs.append(
            rule_spec(
                "ratio",
                {
                    "id": f"profile.{table.code}.component_ratio_r{target_row_index}",
                    "type": "row_ratio_by_rows",
                    "category": "table",
                    "label": (
                        f"{clean_display_text(target_label)} = {clean_display_text(numerator_label)} / "
                        f"({clean_display_text(numerator_label)} + {clean_display_text(complement_label)}) * 100"
                    ),
                    "target_row": target_row_index,
                    "numerator_row": numerator_row_index,
                    "denominator_rows": [numerator_row_index, complement_row_index],
                    "columns": columns,
                    "multiplier": 100,
                    "tolerance": 0.15,
                    "confidence": 0.97 if passed_ratio >= 0.95 else 0.9,
                },
            )
        )
    return specs


def infer_column_share_ratio_rules(table: ValidationTable) -> list[dict[str, Any]]:
    label_columns = set(leading_label_columns(table))
    numeric_counts = {col_index: numeric_cell_count(table, col_index) for col_index in range(column_count(table))}
    source_columns = [col_index for col_index in range(column_count(table)) if numeric_counts[col_index] >= 3]
    target_columns = [col_index for col_index in source_columns if col_index not in label_columns]
    total_rows = [row_index for row_index, row in table.data_rows() if row_total_kind(table, row) == "total"]
    if not total_rows:
        return []

    specs: list[dict[str, Any]] = []
    for target_col in target_columns:
        if not is_same_row_ratio_column_label(effective_column_text(table, target_col)):
            continue
        candidates: list[tuple[float, int, int, int, int, int]] = []
        for numerator_col in source_columns:
            if numerator_col == target_col:
                continue
            for denominator_row in total_rows:
                tolerance = target_ratio_tolerance(table, target_col, 100)
                passed_ratio, checked_count = column_share_ratio_match_ratio(
                    table,
                    target_col,
                    numerator_col,
                    denominator_row,
                    numerator_col,
                    multiplier=100,
                    tolerance=tolerance,
                )
                if checked_count < 3 or passed_ratio < 0.8:
                    continue
                semantic_score = same_row_ratio_candidate_score(
                    table,
                    target_col=target_col,
                    numerator_col=numerator_col,
                    denominator_col=numerator_col,
                )
                distance_score = abs(target_col - numerator_col)
                candidates.append((passed_ratio, checked_count, semantic_score, -distance_score, numerator_col, denominator_row))

        if not candidates:
            continue

        passed_ratio, checked_count, _, _, numerator_col, denominator_row = max(candidates)
        specs.append(
            rule_spec(
                "ratio",
                {
                    "id": f"profile.{table.code}.share_ratio_c{target_col}",
                    "type": "column_share_ratio",
                    "category": "table",
                    "label": f"{leaf_header(table, target_col)} = {leaf_header(table, numerator_col)} / 총계 {leaf_header(table, numerator_col)} * 100",
                    "target_column": target_col,
                    "numerator_column": numerator_col,
                    "denominator_row": denominator_row,
                    "denominator_column": numerator_col,
                    "multiplier": 100,
                    "tolerance": target_ratio_tolerance(table, target_col, 100),
                    "confidence": 0.95 if passed_ratio >= 0.95 else 0.9,
                    "matched_rows": checked_count,
                },
            )
        )

    return specs


def infer_named_row_ratio_rules(table: ValidationTable) -> list[dict[str, Any]]:
    rows = table.data_rows()
    specs: list[dict[str, Any]] = []
    denominator_candidates = [
        (row_index, row)
        for row_index, row in rows
        if row_denominator_score(table, row) >= 2
    ]
    if not denominator_candidates:
        return []

    for target_position, (target_row_index, target_row) in enumerate(rows):
        target_label = combined_row_label(table, target_row) or table.row_label(target_row)
        if not is_ratio_row_label(target_label) or is_growth_rate_label(target_label):
            continue

        target_leaf = normalize_text(row_leaf_label(table, target_row))
        if not target_leaf:
            continue

        numerator_item = next(
            (
                (row_index, row)
                for row_index, row in rows[:target_position]
                if normalize_text(row_leaf_label(table, row)) == target_leaf
                and not is_ratio_row_label(combined_row_label(table, row) or table.row_label(row))
            ),
            None,
        )
        if numerator_item is None:
            continue

        numerator_row_index, numerator_row = numerator_item
        denominator_item = max(
            (
                (row_denominator_score(table, row), row_index, row)
                for row_index, row in denominator_candidates
                if row_index < target_row_index and row_index != numerator_row_index
            ),
            default=None,
        )
        if denominator_item is None:
            continue

        _, denominator_row_index, denominator_row = denominator_item
        columns = shared_numeric_columns(table, [target_row, numerator_row, denominator_row])
        if len(columns) < 2:
            continue

        tolerance = 0.15
        passed_ratio, checked_count = row_ratio_by_rows_match_ratio(
            table,
            target_row_index,
            numerator_row_index,
            denominator_row_index,
            columns,
            multiplier=100,
            tolerance=tolerance,
        )
        if checked_count < 2 or passed_ratio < 0.8:
            continue

        specs.append(
            rule_spec(
                "ratio",
                {
                    "id": f"profile.{table.code}.named_row_ratio_r{target_row_index}",
                    "type": "row_ratio_by_rows",
                    "category": "table",
                    "label": f"{clean_display_text(target_label)} = {clean_display_text(combined_row_label(table, numerator_row))} / {clean_display_text(combined_row_label(table, denominator_row))} * 100",
                    "target_row": target_row_index,
                    "numerator_row": numerator_row_index,
                    "denominator_row": denominator_row_index,
                    "columns": columns,
                    "multiplier": 100,
                    "tolerance": tolerance,
                    "confidence": 0.96 if passed_ratio >= 0.95 else 0.9,
                    "matched_columns": checked_count,
                },
            )
        )

    return specs


def infer_growth_rate_rules(table: ValidationTable) -> list[dict[str, Any]]:
    symbol_to_column = symbol_columns(table)
    current_col = symbol_to_column.get("a")
    previous_col = symbol_to_column.get("b")
    if current_col is None or previous_col is None:
        return []

    rules: list[dict[str, Any]] = []
    for col_index in range(column_count(table)):
        label = effective_column_text(table, col_index)
        normalized = normalize_text(label)
        if col_index in {current_col, previous_col}:
            continue
        explicit_rate = any(
            keyword in normalized
            for keyword in ("증감률", "증감율", "증가율", "감소율", "growthrate", "changerate")
        )
        change_percent = (
            any(keyword in normalized for keyword in ("증감", "change"))
            and any(keyword in normalized for keyword in ("비율", "percent", "rate", "%"))
        )
        if not explicit_rate and not change_percent:
            continue
        rules.append(
            rule_spec(
                "growth_rate",
                {
                    "id": f"profile.{table.code}.growth_rate_c{col_index}",
                    "type": "row_growth_rate",
                    "category": "table",
                    "label": "증감률 = (현재값-전년값)/전년값*100",
                    "target_column": col_index,
                    "current_column": current_col,
                    "previous_column": previous_col,
                    "denominator": "previous",
                    "multiplier": 100,
                    "tolerance": target_ratio_tolerance(table, col_index, 100),
                    "confidence": 0.88,
                },
            )
        )
    return rules


def infer_year_rows_change_rate_rules(table: ValidationTable) -> list[dict[str, Any]]:
    rows = table.data_rows()
    year_rows: list[tuple[int, list]] = []
    for row_index, row in rows:
        label = combined_row_label(table, row) or table.row_label(row)
        if re.fullmatch(r"\d{4}", normalize_text(label)):
            year_rows.append((row_index, row))

    if len(year_rows) < 3:
        return []

    label_columns = set(leading_label_columns(table))
    rate_col = next(
        (
            col_index
            for col_index in range(column_count(table))
            if col_index not in label_columns and is_year_row_change_rate_label(table.column_text(col_index))
        ),
        None,
    )
    if rate_col is None:
        return []

    change_col = next(
        (
            col_index
            for col_index in range(column_count(table))
            if col_index not in label_columns
            and col_index != rate_col
            and is_change_amount_label(table.column_text(col_index))
        ),
        None,
    )
    value_columns = [
        col_index
        for col_index in range(column_count(table))
        if col_index not in label_columns
        and col_index not in {rate_col, change_col}
        and sum(1 for _, row in year_rows if cell_number(row, col_index) is not None) >= 3
    ]
    if not value_columns:
        return []

    value_col = value_columns[0]
    row_indices = [
        row_index
        for row_index, row in year_rows
        if cell_number(row, value_col) is not None and cell_number(row, rate_col) is not None
    ]
    if len(row_indices) < 3:
        return []

    if change_col is not None and rate_col_looks_like_same_row_share(table, rate_col, change_col, value_col):
        return [
            rule_spec(
                "growth_rate",
                {
                    "id": f"profile.{table.code}.year_rows_change_amount_c{change_col}",
                    "type": "year_rows_change_amount",
                    "category": "table",
                    "label": f"{leaf_header(table, change_col)} = 당해연도 - 전년도",
                    "value_column": value_col,
                    "change_column": change_col,
                    "row_indices": row_indices,
                    "change_tolerance": 1.0,
                    "confidence": 0.94,
                },
            )
        ]

    label = f"{leaf_header(table, rate_col)} = ({leaf_header(table, value_col)} 당해연도 - 전년도) / 전년도 * 100"
    if change_col is not None:
        label = f"{leaf_header(table, change_col)} = 당해연도 - 전년도, {label}"

    return [
        rule_spec(
            "growth_rate",
            {
                "id": f"profile.{table.code}.year_rows_change_rate_c{rate_col}",
                "type": "year_rows_change_rate",
                "category": "table",
                "label": label,
                "value_column": value_col,
                "change_column": change_col,
                "rate_column": rate_col,
                "row_indices": row_indices,
                "multiplier": 100,
                "change_tolerance": 1.0,
                "rate_tolerance": 0.15,
                "confidence": 0.94 if change_col is not None else 0.9,
            },
        )
    ]


def rate_col_looks_like_same_row_share(
    table: ValidationTable,
    rate_col: int,
    change_col: int,
    value_col: int,
) -> bool:
    tolerance = target_ratio_tolerance(table, rate_col, 100)
    passed_ratio, checked_count = same_row_ratio_match_ratio(
        table,
        rate_col,
        change_col,
        value_col,
        multiplier=100,
        tolerance=tolerance,
    )
    return checked_count >= 3 and passed_ratio >= 0.8


def infer_row_based_ratio_rules(table: ValidationTable) -> list[dict[str, Any]]:
    rows = table.data_rows()
    specs: list[dict[str, Any]] = []
    for position, (target_row_index, row) in enumerate(rows):
        label = combined_row_label(table, row) or table.row_label(row)
        if not is_ratio_row_label(label) or is_growth_rate_label(label):
            continue

        numerator_item = previous_value_row(table, rows, position)
        if numerator_item is None:
            continue
        denominator_item = previous_value_row(table, rows, numerator_item[0])
        if denominator_item is None:
            continue

        numerator_position, numerator_row_index, numerator_row = numerator_item
        _, denominator_row_index, denominator_row = denominator_item
        numerator_label = combined_row_label(table, numerator_row)
        denominator_label = combined_row_label(table, denominator_row)
        columns = shared_numeric_columns(table, [row, numerator_row, denominator_row])
        if len(columns) < 2:
            continue
        passed_ratio, checked_count = row_ratio_by_rows_match_ratio(
            table,
            target_row_index,
            numerator_row_index,
            denominator_row_index,
            columns,
            multiplier=100,
            tolerance=0.15,
        )
        semantic_confidence = row_ratio_confidence(label, numerator_label, denominator_label)
        if semantic_confidence < 0.9 and (checked_count < 2 or passed_ratio < 0.8):
            continue
        confidence = max(
            semantic_confidence,
            0.96 if passed_ratio >= 0.95 else 0.88,
        )

        specs.append(
            rule_spec(
                "ratio",
                {
                    "id": f"profile.{table.code}.row_ratio_r{target_row_index}",
                    "type": "row_ratio_by_rows",
                    "category": "table",
                    "label": f"{clean_display_text(label)} = {clean_display_text(combined_row_label(table, numerator_row))} / {clean_display_text(combined_row_label(table, denominator_row))} * 100",
                    "target_row": target_row_index,
                    "numerator_row": numerator_row_index,
                    "denominator_row": denominator_row_index,
                    "columns": columns,
                    "multiplier": 100,
                    "tolerance": 0.15,
                    "confidence": confidence if position - numerator_position <= 2 else min(confidence, 0.82),
                },
            )
        )
    return specs


def infer_per_unit_ratio_rules(table: ValidationTable) -> list[dict[str, Any]]:
    """Infer per-unit rows such as cost per task from the two source rows above."""

    rows = table.data_rows()
    specs: list[dict[str, Any]] = []
    for position, (target_row_index, target_row) in enumerate(rows):
        target_label = combined_row_label(table, target_row) or table.row_label(target_row)
        if not is_per_unit_measure_label(target_label):
            continue

        numerator_item = previous_value_row(table, rows, position)
        if numerator_item is None:
            continue
        numerator_position, numerator_row_index, numerator_row = numerator_item
        denominator_item = previous_value_row(table, rows, numerator_position)
        if denominator_item is None:
            continue
        _, denominator_row_index, denominator_row = denominator_item

        columns = shared_numeric_columns(table, [target_row, numerator_row, denominator_row])
        if len(columns) < 2:
            continue

        passed_ratio, checked_count = row_ratio_by_rows_match_ratio(
            table,
            target_row_index,
            numerator_row_index,
            denominator_row_index,
            columns,
            multiplier=1,
            tolerance=1.0,
        )
        if checked_count < 2 or passed_ratio < 0.8:
            continue

        numerator_label = combined_row_label(table, numerator_row) or table.row_label(numerator_row)
        denominator_label = combined_row_label(table, denominator_row) or table.row_label(denominator_row)
        specs.append(
            rule_spec(
                "ratio",
                {
                    "id": f"profile.{table.code}.per_unit_ratio_r{target_row_index}",
                    "type": "row_ratio_by_rows",
                    "category": "table",
                    "label": (
                        f"{clean_display_text(target_label)} = "
                        f"{clean_display_text(numerator_label)} / {clean_display_text(denominator_label)}"
                    ),
                    "target_row": target_row_index,
                    "numerator_row": numerator_row_index,
                    "denominator_row": denominator_row_index,
                    "columns": columns,
                    "multiplier": 1,
                    "tolerance": 0.05,
                    "confidence": 0.96 if passed_ratio >= 0.95 else 0.9,
                    "aggregate_strategy": "recalculate_from_numerator_and_denominator",
                },
            )
        )
    return specs


def infer_row_based_growth_rate_rules(table: ValidationTable) -> list[dict[str, Any]]:
    rows = table.data_rows()
    specs: list[dict[str, Any]] = []
    for position, (target_row_index, row) in enumerate(rows):
        label = combined_row_label(table, row) or table.row_label(row)
        if not is_growth_rate_label(label):
            continue

        source_item = previous_value_row(table, rows, position)
        if source_item is None:
            continue
        source_position, source_row_index, source_row = source_item
        columns = shared_numeric_columns(table, [row, source_row])
        if len(columns) < 2:
            continue

        specs.append(
            rule_spec(
                "growth_rate",
                {
                    "id": f"profile.{table.code}.row_yoy_growth_r{target_row_index}",
                    "type": "row_year_over_year_rate",
                    "category": "table",
                    "label": f"{clean_display_text(label)} = ({clean_display_text(combined_row_label(table, source_row))} 당해연도 - 전년도) / 전년도 * 100",
                    "target_row": target_row_index,
                    "source_row": source_row_index,
                    "columns": columns,
                    "multiplier": 100,
                    "tolerance": 0.15,
                    "confidence": 0.92 if position - source_position <= 2 else 0.82,
                },
            )
        )
    return specs


def is_growth_rate_label(label: str) -> bool:
    normalized = normalize_text(label)
    return any(keyword in normalized for keyword in ("증감률", "증감율", "증가율", "감소율", "rateofgrowth", "rateofincrease", "changerate", "growthrate"))


def is_same_row_ratio_column_label(label: str) -> bool:
    normalized = normalize_text(label)
    if is_growth_rate_label(label):
        return False
    if any(keyword in normalized for keyword in ("법률", "법령", "law", "laws")) and not any(
        keyword in normalized for keyword in ("비율", "비중", "ratio", "rate", "percent", "percentage")
    ):
        return False
    if any(keyword in normalized for keyword in ("비율", "비중", "이용률", "사용률", "활용률", "참여율", "잔액율", "잔액률", "률", "율")):
        return True
    return bool(re.search(r"\b(rate|ratio|percent|percentage)\b", clean_display_text(label), re.IGNORECASE))


def same_row_ratio_candidate_score(
    table: ValidationTable,
    *,
    target_col: int,
    numerator_col: int,
    denominator_col: int,
) -> int:
    target_label = normalize_text(effective_column_text(table, target_col))
    numerator_label = normalize_text(effective_column_text(table, numerator_col))
    denominator_label = normalize_text(effective_column_text(table, denominator_col))
    score = 0

    if numerator_col == target_col - 1:
        score += 4
    if denominator_col < numerator_col:
        score += 1
    if total_label_kind(leaf_header(table, denominator_col)) == "total" or any(
        keyword in denominator_label for keyword in ("계", "합계", "총계", "total")
    ):
        score += 5
    if any(keyword in denominator_label for keyword in ("대상", "기준", "전체", "인구", "신고", "접수", "base", "target", "population", "total")):
        score += 2
    if any(keyword in denominator_label for keyword in ("gdp", "총", "전체", "계")):
        score += 2
    if any(keyword in target_label for keyword in ("이용", "사용", "활용")) and any(
        keyword in numerator_label for keyword in ("이용", "사용", "활용", "usage", "use")
    ):
        score += 3
    if any(keyword in target_label for keyword in ("참여", "가입", "채택", "확보", "비축", "수용")) and any(
        keyword in numerator_label for keyword in ("참여", "가입", "채택", "확보", "비축", "수용", "adoption", "insured", "secured", "reserve")
    ):
        score += 3
    shared_topic_keywords = (
        "조세",
        "국세",
        "지방세",
        "재정",
        "복구",
        "정비",
        "내진",
        "대피",
        "방독면",
        "시설",
        "보험",
        "보조금",
    )
    if any(keyword in target_label and keyword in numerator_label for keyword in shared_topic_keywords):
        score += 3
    if numerator_label == denominator_label:
        score -= 4

    return score


def is_year_row_change_rate_label(label: str) -> bool:
    normalized = normalize_text(label)
    if any(keyword in normalized for keyword in ("증감률", "증감율", "증가율", "감소율", "percentagechange", "changerate", "rateofincrease", "rateofdecrease")):
        return True
    return "rateofgrowth" in normalized and any(keyword in normalized for keyword in ("증감", "증가", "감소"))


def is_change_amount_label(label: str) -> bool:
    normalized = normalize_text(label)
    return any(keyword in normalized for keyword in ("증감", "증가", "감소", "increase", "decrease", "change")) and not is_growth_rate_label(label)


def is_ratio_row_label(label: str) -> bool:
    normalized = normalize_text(label)
    if any(keyword in normalized for keyword in ("비율", "비중", "참여율", "잔액율", "잔액률", "율")):
        return True
    return bool(re.search(r"\b(rate|ratio|percent|percentage)\b", clean_display_text(label), re.IGNORECASE))


def row_ratio_confidence(target_label: str, numerator_label: str, denominator_label: str) -> float:
    target = normalize_text(target_label)
    numerator = normalize_text(numerator_label)
    denominator = normalize_text(denominator_label)
    if "참여율" in target and any(keyword in numerator for keyword in ("참여", "volunteer")) and any(
        keyword in denominator for keyword in ("인구", "population")
    ):
        return 0.94
    return 0.0


def row_label_parts(table: ValidationTable, row: list) -> list[str]:
    parts: list[str] = []
    for col_index in leading_label_columns(table):
        value = cell_text_at(row, col_index)
        if value:
            parts.append(value)
    return parts


def row_leaf_label(table: ValidationTable, row: list) -> str:
    parts = row_label_parts(table, row)
    return parts[-1] if parts else table.row_label(row)


def row_denominator_score(table: ValidationTable, row: list) -> int:
    label = normalize_text(combined_row_label(table, row) or table.row_label(row))
    score = 0
    if "gdp" in label:
        score += 4
    if any(keyword in label for keyword in ("계", "합계", "총계", "전체", "대상", "기준", "인구", "total", "base", "target", "population")):
        score += 2
    if is_ratio_row_label(label) or is_growth_rate_label(label):
        score -= 4
    return score


def previous_value_row(
    table: ValidationTable,
    rows: list[tuple[int, list]],
    before_position: int,
) -> tuple[int, int, list] | None:
    for position in range(before_position - 1, -1, -1):
        row_index, row = rows[position]
        label = combined_row_label(table, row) or table.row_label(row)
        if is_ratio_row_label(label) or is_growth_rate_label(label):
            continue
        if sum(1 for col_index in range(len(row)) if cell_number(row, col_index) is not None) < 2:
            continue
        return position, row_index, row
    return None


def shared_numeric_columns(table: ValidationTable, rows: list[list]) -> list[int]:
    label_columns = set(leading_label_columns(table))
    columns: list[int] = []
    for col_index in range(column_count(table)):
        if col_index in label_columns:
            continue
        if all(cell_number(row, col_index) is not None for row in rows):
            columns.append(col_index)
    return columns


def outlier_check_specs(table: ValidationTable) -> list[dict[str, Any]]:
    label_columns = set(leading_label_columns(table))
    columns: list[int] = []
    for col_index in range(column_count(table)):
        if col_index in label_columns:
            continue
        values = []
        for _, row in table.data_rows():
            label = combined_row_label(table, row)
            if is_total_like(label) or infer_row_role(label) == "subtotal":
                continue
            value = cell_number(row, col_index)
            if value is not None:
                values.append(value)
        if len(values) >= 5:
            columns.append(col_index)

    if not columns:
        return []

    return [
        rule_spec(
            "outlier",
            {
                "id": f"profile.{table.code}.outlier_columns",
                "type": "outlier_columns",
                "category": "common",
                "label": "열별 이상치 후보",
                "columns": columns,
                "mad_multiplier": 20.0,
                "max_findings": 1,
                "confidence": 0.7,
            },
        )
    ]


def symbol_columns(table: ValidationTable) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for col_index in range(column_count(table)):
        label = table.column_text(col_index)
        for match in re.finditer(r"\(([a-z])\s*=", label, re.IGNORECASE):
            mapping.setdefault(match.group(1).lower(), col_index)
        for match in re.finditer(r"\(([a-z])\)", label, re.IGNORECASE):
            mapping.setdefault(match.group(1).lower(), col_index)
    return mapping


def expression_terms(expression: str, symbol_to_column: dict[str, int]) -> list[dict[str, Any]]:
    terms: list[dict[str, Any]] = []
    for match in re.finditer(r"([+-]?)([a-z])", expression.lower().replace(" ", "")):
        symbol = match.group(2)
        col_index = symbol_to_column.get(symbol)
        if col_index is None:
            return []
        terms.append({"op": "-" if match.group(1) == "-" else "+", "column": col_index})
    return terms


def is_ratio_denominator_expression(label: str, start_index: int) -> bool:
    return label[:start_index].rstrip().endswith("/")


def unique_ints(values: list[int]) -> list[int]:
    seen: set[int] = set()
    unique_values: list[int] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique_values.append(value)
    return unique_values


def arithmetic_dependency_columns(
    table: ValidationTable,
    target_col: int,
    symbol_to_column: dict[str, int] | None = None,
) -> list[int]:
    symbols = symbol_to_column or symbol_columns(table)
    label = table.column_text(target_col)
    dependencies: list[int] = []

    for match in re.finditer(r"\(([a-z])\s*=\s*([a-z](?:\s*[+-]\s*[a-z])+)\)", label, re.IGNORECASE):
        target_symbol = match.group(1).lower()
        if symbols.get(target_symbol) != target_col:
            continue
        dependencies.extend(int(term["column"]) for term in expression_terms(match.group(2), symbols))

    for match in re.finditer(r"\(([a-z](?:\s*[+-]\s*[a-z])+)\)", label, re.IGNORECASE):
        if is_ratio_denominator_expression(label, match.start()):
            continue
        dependencies.extend(int(term["column"]) for term in expression_terms(match.group(1), symbols))

    return unique_ints(dependencies)


def find_column(table: ValidationTable, predicate: Any) -> int | None:
    for col_index in range(1, column_count(table)):
        raw = table.column_text(col_index)
        normalized = normalize_text(raw)
        if predicate(normalized, raw):
            return col_index
    return None
