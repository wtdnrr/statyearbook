import type { ValidationIssue } from "../types";

const REPEATABLE_CALCULATION_TYPES = new Set([
  "합계 검수",
  "비율 검수",
]);

export const LINGUISTIC_CHECK_TYPES = new Set([
  "오탈자 검수",
  "번역 검수",
  "용어 제안",
]);

function exactCellGroupKey(check: ValidationIssue) {
  return [
    check.location,
    check.row_index ?? "",
    check.col_index ?? "",
    check.current_value,
  ].join("::");
}

export function repeatedCalculationGroupKey(check: ValidationIssue) {
  if (!REPEATABLE_CALCULATION_TYPES.has(check.type)) {
    return undefined;
  }

  const formula = check.formula?.trim();
  const basis = formula || check.rule_id;
  if (!basis) {
    return undefined;
  }

  return [check.type, basis].join("::");
}

export function validationDisplayGroupKey(check: ValidationIssue) {
  const repeatedKey = repeatedCalculationGroupKey(check);

  if (repeatedKey) {
    return `repeated::${repeatedKey}::${check.status}`;
  }

  if (check.type === "파란색 표기 확인") {
    return `blue-source::${check.status}::${exactCellGroupKey(check)}`;
  }

  if (LINGUISTIC_CHECK_TYPES.has(check.type)) {
    if (check.status === "정상") {
      // A successful language review is presented once per review type. The
      // display group still retains every source check so the grid can mark
      // every header and body cell that was actually reviewed.
      return `linguistic-pass::${check.type}`;
    }
    return [
      "linguistic-problem",
      check.type,
      check.status,
      exactCellGroupKey(check),
      check.expected_value ?? "",
    ].join("::");
  }

  return [
    "single-or-exact",
    check.rule_id ?? check.id,
    check.type,
    check.formula ?? "",
    check.detail,
    check.status,
  ].join("::");
}

export function groupedValidationIssueCount(checks: ValidationIssue[], status: string) {
  return new Set(
    checks
      .filter((check) => check.status === status)
      .map((check) => validationDisplayGroupKey(check)),
  ).size;
}
