from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Protocol

from app.validation.catalog import rule_definition_payload, rule_spec
from app.validation.models import ValidationTable, clean_display_text, normalize_text
from app.validation.rules import (
    REGION_NAMES,
    cell_number,
    column_count,
    combined_row_label,
    header_parent_key,
    is_additive_column_label,
    is_total_like,
    leading_label_columns,
    leaf_header,
)


PROFILE_VERSION = "validation-profile-v1"

COMMON_RULE_IDS = [
    "sum",
    "ratio",
    "growth_rate",
    "outlier",
    "spelling",
    "translation",
    "unit",
    "empty",
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
        table_rules = [
            check
            for check in checks
            if check.get("category") in {"template", "table"} and check.get("type") not in {"year_sequence"}
        ]
        needs_llm_review = previous_profile is not None and previous_profile.structure_signature != signature
        status = profile_status(checks, needs_llm_review=needs_llm_review)

        notes = "휴리스틱 기반 표별 검수 기준입니다. 담당자 승인 후 다음 연도 검수에 재사용하세요."
        if previous_profile is None:
            notes = "신규 통계표로 판단되어 표별 검수 기준을 생성했습니다. 실제 서비스에서는 GPT API 해석 후 담당자 승인을 거치는 흐름입니다."
        elif needs_llm_review:
            notes = "기존 통계표와 구조 서명이 달라 새 표별 검수 기준을 생성했습니다. 실제 서비스에서는 GPT API가 변경된 표 구조를 다시 해석해야 합니다."
        if status == "needs_review":
            notes = f"{notes} 일부 검수 후보의 확신도가 낮아 담당자 확인이 필요합니다."

        return ProfileDraft(
            table_code=table.code,
            table_title=table.title,
            structure_signature=signature,
            table_type=", ".join(templates) if templates else "general",
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

    region_labels = {table.row_label(row)[:2] for _, row in table.data_rows()}
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
    normalized = normalize_text(value)
    if not normalized:
        return None
    if normalized.startswith("소계") or "subtotal" in normalized:
        return "subtotal"
    if normalized in {"계", "total", "합계", "총계"}:
        return "total"
    if normalized.startswith(("계", "합계", "총계")):
        return "total"
    return None


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
    if text in {"-", "－", "―"}:
        return 0.0
    return None


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

    label = effective_column_text(table, col_index)
    normalized = normalize_text(label)
    if normalized.startswith(("총", "총계", "합계")):
        return True
    if re.search(r"\b(total|grand\s+total|subtotal)\b", label, re.IGNORECASE):
        return True
    return False


def is_calculation_row_label(table: ValidationTable, row: list) -> bool:
    label = combined_row_label(table, row) or table.row_label(row)
    return is_ratio_row_label(label) or is_growth_rate_label(label)


def row_sum_match_ratio(
    table: ValidationTable,
    target_col: int,
    operand_columns: list[int],
    *,
    tolerance: float,
) -> tuple[float, int]:
    checked = 0
    passed = 0
    for _, row in table.data_rows():
        current = cell_number(row, target_col)
        operands = [additive_cell_number(row, col_index) for col_index in operand_columns]
        if current is None or any(value is None for value in operands):
            continue
        checked += 1
        expected = sum(value for value in operands if value is not None)
        if abs(current - expected) <= tolerance:
            passed += 1
    return (passed / checked if checked else 0.0, checked)


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
        current = cell_number(target_matrix_row, col_index)
        values = [
            additive_cell_number(table.matrix[row_index], col_index)
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


def numeric_cell_count(table: ValidationTable, col_index: int) -> int:
    return sum(1 for _, row in table.data_rows() if cell_number(row, col_index) is not None)


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
    if "year_trend_table" in templates:
        checks.extend(year_sequence_check_specs(table))

    checks.extend(infer_gender_total_rules(table))
    checks.extend(infer_header_formula_rules(table))
    checks.extend(infer_same_row_ratio_rules(table))
    checks.extend(infer_column_share_ratio_rules(table))
    checks.extend(infer_growth_rate_rules(table))
    checks.extend(infer_year_rows_change_rate_rules(table))
    checks.extend(infer_named_row_ratio_rules(table))
    checks.extend(infer_row_based_ratio_rules(table))
    checks.extend(infer_row_based_growth_rate_rules(table))
    checks.extend(infer_total_column_rules(table))
    checks.extend(infer_total_row_rules(table))
    checks.extend(outlier_check_specs(table))
    checks.extend(static_spelling_check_specs(table))
    checks.extend(static_terminology_check_specs(table))
    checks.extend(static_translation_check_specs(table))

    return dedupe_check_specs(checks)


def common_check_specs(table: ValidationTable) -> list[dict[str, Any]]:
    return [
        rule_spec(
            "unit",
            {
                "id": f"profile.{table.code}.unit_required",
                "type": "unit_required",
                "category": "common",
                "label": "단위 필수 및 프로파일 단위 일치",
                "expected_unit": table.unit,
                "confidence": 1.0,
            },
        ),
        rule_spec(
            "empty",
            {
                "id": f"profile.{table.code}.metadata_required",
                "type": "metadata_required",
                "category": "common",
                "label": "기준일/출처 필수",
                "fields": ["base_date", "source"],
                "confidence": 1.0,
            },
        ),
        rule_spec(
            "empty",
            {
                "id": f"profile.{table.code}.row_label_required",
                "type": "row_label_required",
                "category": "common",
                "label": "데이터 행 항목명 필수",
                "confidence": 0.95,
            },
        ),
        {
            "id": f"profile.{table.code}.numeric_format",
            "type": "numeric_format",
            "category": "common",
            "check_type": "계산용 숫자 형식 검수",
            "label": "계산용 숫자 형식 검수",
            "fields": [],
            "severity": "warning",
            "failure_status": "확인 필요",
            "confidence": 0.9,
        },
    ]


def region_total_check_specs(table: ValidationTable) -> list[dict[str, Any]]:
    rows = table.data_rows()
    target_item = next(
        ((row_index, row) for row_index, row in rows[:5] if row_total_kind(table, row) == "total"),
        None,
    )
    if target_item is None:
        return []

    target_row_index, _ = target_item
    subtotal_rows = [
        row_index
        for row_index, row in rows
        if row_index != target_row_index and row_total_kind(table, row) == "subtotal"
    ]
    region_rows = [
        row_index
        for row_index, row in rows
        if row_index != target_row_index
        and row_total_kind(table, row) is None
        and clean_display_text(combined_row_label(table, row))[:2] in REGION_NAMES
    ]
    operand_rows = subtotal_rows if len(subtotal_rows) >= 2 else region_rows
    unique_regions = {
        clean_display_text(combined_row_label(table, table.matrix[row_index]))[:2]
        for row_index in operand_rows
    }
    if len(operand_rows) < 8:
        return []

    columns = [
        col_index
        for col_index in range(column_count(table))
        if col_index not in leading_label_columns(table) and is_additive_column_label(table.column_text(col_index))
    ]
    if not columns:
        return []

    return [
        rule_spec(
            "sum",
            {
            "id": f"profile.{table.code}.region_total",
            "type": "region_total",
            "category": "template",
            "label": "지역 합계 = 시도별 세부 값 합계",
            "target_row": target_row_index,
            "operand_rows": operand_rows,
            "columns": columns,
            "tolerance": 1.0,
            "confidence": 0.95 if len(unique_regions) >= 15 else 0.82,
            },
        )
    ]


def year_sequence_check_specs(table: ValidationTable) -> list[dict[str, Any]]:
    year_rows = [
        row_index
        for row_index, row in table.data_rows()
        if re.fullmatch(r"\d{4}", normalize_text(combined_row_label(table, row) or table.row_label(row)))
    ]
    year_columns = [
        col_index
        for col_index in range(column_count(table))
        if re.fullmatch(r"\d{4}", normalize_text(leaf_header(table, col_index)))
    ]
    if len(year_rows) < 2 and len(year_columns) < 2:
        return []
    return [
        rule_spec(
            "empty",
            {
            "id": f"profile.{table.code}.year_axis",
            "type": "year_sequence",
            "category": "template",
            "label": "연도 축 인식 및 연속성 확인",
            "row_indices": year_rows,
            "columns": year_columns,
            "confidence": 0.75,
            },
        )
    ]


def infer_total_column_rules(table: ValidationTable) -> list[dict[str, Any]]:
    label_columns = set(leading_label_columns(table))
    numeric_columns = [
        col_index
        for col_index in range(column_count(table))
        if col_index not in label_columns and is_additive_column_label(table.column_text(col_index))
    ]
    specs: list[dict[str, Any]] = []
    for target_col in numeric_columns:
        if not is_total_column_candidate(table, target_col):
            continue

        operand_columns = best_row_sum_operand_columns(table, target_col, numeric_columns)
        if not operand_columns:
            continue
        passed_ratio, checked_count = row_sum_match_ratio(table, target_col, operand_columns, tolerance=1.0)
        if checked_count < 3 or passed_ratio < 0.8:
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
                "tolerance": 1.0,
                "confidence": 0.95 if passed_ratio >= 0.95 else 0.9,
                },
            )
        )
    return specs


def best_row_sum_operand_columns(
    table: ValidationTable,
    target_col: int,
    numeric_columns: list[int],
) -> list[int]:
    candidates: list[list[int]] = []

    exact_parent_operands = [
        col_index
        for col_index in numeric_columns
        if col_index != target_col
        and not is_total_column_candidate(table, col_index)
        and effective_header_parent_key(table, col_index) == effective_header_parent_key(table, target_col)
    ]
    if len(exact_parent_operands) >= 2:
        candidates.append(exact_parent_operands)

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

    child_total_operands = [
        col_index
        for col_index in numeric_columns
        if col_index != target_col and is_total_column_candidate(table, col_index)
    ]
    if len(child_total_operands) >= 2:
        candidates.append(child_total_operands)

    all_leaf_operands = [
        col_index
        for col_index in numeric_columns
        if col_index != target_col and not is_total_column_candidate(table, col_index)
    ]
    if len(all_leaf_operands) >= 2:
        candidates.append(all_leaf_operands)

    scored_candidates: list[tuple[float, int, int, list[int]]] = []
    seen: set[tuple[int, ...]] = set()
    for operand_columns in candidates:
        key = tuple(operand_columns)
        if key in seen:
            continue
        seen.add(key)
        passed_ratio, checked_count = row_sum_match_ratio(table, target_col, operand_columns, tolerance=1.0)
        if checked_count < 3 or passed_ratio < 0.8:
            continue
        scored_candidates.append((passed_ratio, checked_count, -len(operand_columns), operand_columns))

    if not scored_candidates:
        return []

    return max(scored_candidates)[3]


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

    label_columns = set(leading_label_columns(table))
    columns = [
        col_index
        for col_index in range(column_count(table))
        if col_index not in label_columns and is_additive_column_label(table.column_text(col_index))
    ]
    specs: list[dict[str, Any]] = []
    for position, (target_row_index, row) in enumerate(rows):
        row_label = combined_row_label(table, row)
        kind = row_total_kind(table, row)
        if kind is None:
            continue
        window: list[tuple[int, list]] = []
        for row_index, operand_row in rows[position + 1 :]:
            operand_kind = row_total_kind(table, operand_row)
            if kind == "subtotal" and operand_kind is not None:
                break
            if kind == "total" and operand_kind == "total":
                break
            window.append((row_index, operand_row))

        if kind == "total":
            subtotal_rows = [row_index for row_index, operand_row in window if row_total_kind(table, operand_row) == "subtotal"]
            operand_rows = (
                subtotal_rows
                if len(subtotal_rows) >= 2
                else [
                    row_index
                    for row_index, operand_row in window
                    if row_total_kind(table, operand_row) is None and not is_calculation_row_label(table, operand_row)
                ]
            )
        else:
            operand_rows = [
                row_index
                for row_index, operand_row in window
                if row_total_kind(table, operand_row) is None and not is_calculation_row_label(table, operand_row)
            ]

        if len(operand_rows) < 2 or not columns:
            continue
        passed_ratio, checked_count = column_sum_match_ratio(table, target_row_index, operand_rows, columns, tolerance=1.0)
        if checked_count < 2 or passed_ratio < 0.8:
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
                "tolerance": 1.0,
                "confidence": 0.96 if passed_ratio >= 0.95 else 0.9,
                },
            )
        )
    return specs


def dedupe_check_specs(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for check in checks:
        key = (
            check.get("type"),
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
                    "tolerance": 1.0,
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
        label = table.column_text(col_index)
        normalized = normalize_text(label)
        if col_index in {current_col, previous_col}:
            continue
        if not any(keyword in normalized for keyword in ("증감률", "증감율", "증가율", "감소율", "growthrate", "changerate")):
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
                    "tolerance": 0.15,
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
        confidence = row_ratio_confidence(label, numerator_label, denominator_label)
        if confidence < 0.9:
            continue

        columns = shared_numeric_columns(table, [row, numerator_row, denominator_row])
        if len(columns) < 2:
            continue

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


def static_spelling_check_specs(table: ValidationTable) -> list[dict[str, Any]]:
    return [
        rule_spec(
            "spelling",
            {
                "id": f"profile.{table.code}.spelling_static",
                "type": "spelling_static",
                "category": "common",
                "label": "국문/영문 명백 오탈자 사전",
                "terms": [
                    {"current": "Claasification", "expected": "Classification", "language": "en", "reason": "영문 철자 오류"},
                    {"current": "Claasifi-cation", "expected": "Classification", "language": "en", "reason": "영문 철자 오류"},
                    {"current": "Ele7ction", "expected": "Election", "language": "en", "reason": "숫자 혼입"},
                    {"current": "Nuber", "expected": "Number", "language": "en", "reason": "영문 철자 누락"},
                ],
                "failure_status": "오류 의심",
                "severity": "critical",
                "confidence": 0.9,
            },
        )
    ]


def static_terminology_check_specs(table: ValidationTable) -> list[dict[str, Any]]:
    return [
        rule_spec(
            "translation",
            {
                "id": f"profile.{table.code}.terminology_static",
                "type": "terminology_static",
                "category": "common",
                "check_type": "용어 제안",
                "label": "국문 표준 용어 제안",
                "terms": [
                    {"current": "잔액율", "expected": "잔액률", "language": "ko", "reason": "발간 표준 용어 확인"},
                ],
                "failure_status": "확인 필요",
                "severity": "warning",
                "confidence": 0.75,
            },
        )
    ]


def static_translation_check_specs(table: ValidationTable) -> list[dict[str, Any]]:
    return [
        rule_spec(
            "translation",
            {
                "id": f"profile.{table.code}.translation_static",
                "type": "translation_static",
                "category": "common",
                "label": "기본 국문/영문 병기 용어집",
                "terms": [
                    {"source": "구분", "expected": "Classification"},
                    {"source": "합계", "expected": "Total"},
                    {"source": "계", "expected": "Total"},
                    {"source": "남성", "expected": "Male"},
                    {"source": "여성", "expected": "Female"},
                    {"source": "단위", "expected": "Unit"},
                    {"source": "잔액률", "expected": "Balance Ratio"},
                    {"source": "증감률", "expected": "Rate of Change"},
                ],
                "scope": "header_and_label_cells",
                "failure_status": "확인 필요",
                "severity": "warning",
                "confidence": 0.65,
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


def find_column(table: ValidationTable, predicate: Any) -> int | None:
    for col_index in range(1, column_count(table)):
        raw = table.column_text(col_index)
        normalized = normalize_text(raw)
        if predicate(normalized, raw):
            return col_index
    return None
