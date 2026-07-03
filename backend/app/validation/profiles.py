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
                "numeric_cells": sum(1 for cell in row if cell and cell.numeric_value is not None),
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
    checks.extend(infer_growth_rate_rules(table))
    checks.extend(infer_total_column_rules(table))
    checks.extend(infer_total_row_rules(table))
    checks.extend(outlier_check_specs(table))
    checks.extend(static_spelling_check_specs(table))
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
            "check_type": "숫자 형식 검수",
            "label": "숫자 형식 검수",
            "fields": [],
            "severity": "warning",
            "failure_status": "확인 필요",
            "confidence": 0.9,
        },
        rule_spec(
            "growth_rate",
            {
                "id": f"profile.{table.code}.growth_rate_scan",
                "type": "growth_rate_scan",
                "category": "common",
                "label": "증감률 항목 탐색",
                "confidence": 0.75,
            },
        ),
    ]


def region_total_check_specs(table: ValidationTable) -> list[dict[str, Any]]:
    rows = table.data_rows()
    target_item = next(
        ((row_index, row) for row_index, row in rows[:5] if is_total_like(combined_row_label(table, row))),
        None,
    )
    if target_item is None:
        return []

    target_row_index, _ = target_item
    operand_rows = [
        row_index
        for row_index, row in rows
        if row_index != target_row_index and clean_display_text(combined_row_label(table, row))[:2] in REGION_NAMES
    ]
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
        if infer_column_role(table, target_col) != "total":
            continue
        parent_key = header_parent_key(table, target_col)
        operand_columns = [
            col_index
            for col_index in numeric_columns
            if col_index != target_col
            and infer_column_role(table, col_index) != "total"
            and header_parent_key(table, col_index) == parent_key
        ]
        if len(operand_columns) < 2:
            continue
        specs.append(
            rule_spec(
                "sum",
                {
                "id": f"profile.{table.code}.row_total_c{target_col}",
                "type": "row_sum",
                "category": "template",
                "label": f"{leaf_header(table, target_col)} = 같은 행 세부 열 합계",
                "target_column": target_col,
                "operand_columns": operand_columns,
                "tolerance": 1.0,
                "confidence": 0.82 if parent_key else 0.68,
                },
            )
        )
    return specs


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
    for position, (target_row_index, row) in enumerate(rows[:8]):
        row_label = combined_row_label(table, row)
        if not is_total_like(row_label):
            continue
        operand_rows = [
            row_index
            for row_index, operand_row in rows[position + 1 :]
            if not is_total_like(combined_row_label(table, operand_row))
        ]
        if len(operand_rows) < 2 or not columns:
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
                "confidence": 0.88 if len(operand_rows) >= 8 else 0.62,
                },
            )
        )
        break
    return specs


def dedupe_check_specs(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for check in checks:
        key = (
            check.get("type"),
            check.get("target_column"),
            tuple(check.get("operand_columns", [])),
            check.get("target_row"),
            tuple(check.get("columns", [])),
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
    total_col = find_column(table, lambda normalized, raw: ("계total" in normalized or normalized.endswith("계")) and "구분" not in normalized)
    male_col = find_column(table, lambda normalized, raw: "남성" in normalized or "male" in raw.lower())
    female_col = find_column(table, lambda normalized, raw: "여성" in normalized or "female" in raw.lower())

    if total_col is None or male_col is None or female_col is None:
        return []
    if len({total_col, male_col, female_col}) != 3:
        return []

    return [
        rule_spec(
            "sum",
            {
            "id": f"profile.{table.code}.gender_total",
            "type": "row_sum",
            "category": "table",
            "label": "계 = 남성 + 여성",
            "target_column": total_col,
            "operand_columns": [male_col, female_col],
            "tolerance": 1.0,
            "confidence": 0.96,
            },
        )
    ]


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

    return rules


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
                "label": "국문/영문 정적 오탈자 사전",
                "terms": [
                    {"current": "잔액율", "expected": "잔액률", "language": "ko"},
                    {"current": "Claasifi-cation", "expected": "Classification", "language": "en"},
                    {"current": "Ele7ction", "expected": "Election", "language": "en"},
                    {"current": "Nuber", "expected": "Number", "language": "en"},
                ],
                "confidence": 0.8,
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
                    {"source": "지역", "expected": "Region"},
                    {"source": "합계", "expected": "Total"},
                    {"source": "계", "expected": "Total"},
                    {"source": "남성", "expected": "Male"},
                    {"source": "여성", "expected": "Female"},
                    {"source": "단위", "expected": "Unit"},
                    {"source": "잔액률", "expected": "Balance Ratio"},
                    {"source": "증감률", "expected": "Rate of Change"},
                ],
                "scope": "header_and_label_cells",
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
