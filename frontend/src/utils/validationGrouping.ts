import type { ValidationIssue } from "../types";

const REPEATABLE_CALCULATION_TYPES = new Set([
  "합계 검수",
  "비율 검수",
  "증감액 검수",
  "증감률 검수",
  "평균 검수",
]);

export function repeatedCalculationGroupKey(check: ValidationIssue) {
  if (!REPEATABLE_CALCULATION_TYPES.has(check.type)) {
    return undefined;
  }

  const basis = [check.rule_id, check.formula].filter(Boolean).join("::");
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
