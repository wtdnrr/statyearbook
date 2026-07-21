import type { ValidationHighlightCell, ValidationIssue } from "../types";

export const VALIDATION_CHECK_TYPE_ORDER = new Map(
  [
    "합계 검수",
    "비율 검수",
    "오탈자 검수",
    "번역 검수",
    "메타정보 검수",
    "이상치 검수",
    "파란색 표기 확인",
  ].map((type, index) => [type, index]),
);

const REPEATABLE_CALCULATION_TYPES = new Set([
  "합계 검수",
  "비율 검수",
]);

export const LINGUISTIC_CHECK_TYPES = new Set([
  "오탈자 검수",
  "번역 검수",
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

export function highlightCellKey(cell: ValidationHighlightCell) {
  return `${cell.row_index}:${cell.col_index}`;
}

export function targetCellForCheck(check: ValidationIssue): ValidationHighlightCell | undefined {
  return (
    check.highlight_cells?.find((cell) => cell.role === "target") ??
    (check.row_index !== undefined && check.col_index !== undefined
      ? { row_index: check.row_index, col_index: check.col_index, role: "target" }
      : undefined)
  );
}

export function relatedCellsForCheck(check: ValidationIssue) {
  const target = targetCellForCheck(check);
  const targetKey = target ? highlightCellKey(target) : "";
  const seen = new Set<string>();

  return (check.highlight_cells ?? []).filter((cell) => {
    const key = highlightCellKey(cell);
    if (cell.role !== "related" || key === targetKey || seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
}

type CalculationDirection = "row" | "column" | "mixed" | "other";

interface GroupedValidationCheck extends ValidationIssue {
  checks: ValidationIssue[];
}

function sourceCalculationDirection(check: ValidationIssue): CalculationDirection {
  const target = targetCellForCheck(check);
  const related = relatedCellsForCheck(check);
  if (target && related.length > 0) {
    const sameRow = related.every((cell) => cell.row_index === target.row_index);
    const sameColumn = related.every((cell) => cell.col_index === target.col_index);
    if (sameRow && !sameColumn) return "row";
    if (sameColumn && !sameRow) return "column";
    if (!sameRow && !sameColumn) return "mixed";
  }
  const context = [check.rule_id, check.formula, check.detail].filter(Boolean).join(" ").toLowerCase();
  if (/column[_ -]?(sum|total)|region[_ -]?total|열\s*(방향|기준)?\s*합계/.test(context)) {
    return "column";
  }
  if (/row[_ -]?(sum|total)|행\s*(방향|기준)?\s*합계/.test(context)) {
    return "row";
  }
  return "other";
}

function calculationDirection(check: GroupedValidationCheck): CalculationDirection {
  if (check.type !== "합계 검수") return "other";
  const directions = new Set(
    check.checks.map(sourceCalculationDirection).filter((direction) => direction !== "other"),
  );
  if (directions.size === 1) return directions.values().next().value ?? "other";
  return directions.size > 1 ? "mixed" : "other";
}

export function sortChecksByCalculationHierarchy<T extends GroupedValidationCheck>(checks: T[]): T[] {
  if (checks.length < 2) return checks;

  const targets = checks.map(
    (check) =>
      new Set(
        (check.highlight_cells ?? [])
          .filter((cell) => cell.role === "target")
          .map(highlightCellKey),
      ),
  );
  const related = checks.map(
    (check) =>
      new Set(
        (check.highlight_cells ?? [])
          .filter((cell) => cell.role === "related")
          .map(highlightCellKey),
      ),
  );
  const children = checks.map(() => new Set<number>());
  const indegree = checks.map(() => 0);

  for (let parent = 0; parent < checks.length; parent += 1) {
    for (let child = 0; child < checks.length; child += 1) {
      if (parent === child || checks[parent].type !== checks[child].type) continue;
      if (![...targets[child]].some((key) => related[parent].has(key)) || children[parent].has(child)) continue;
      children[parent].add(child);
      indegree[child] += 1;
    }
  }

  const compare = (left: number, right: number) =>
    (VALIDATION_CHECK_TYPE_ORDER.get(checks[left].type) ?? 999) -
      (VALIDATION_CHECK_TYPE_ORDER.get(checks[right].type) ?? 999) ||
    left - right;
  const ready = checks.map((_, index) => index).filter((index) => indegree[index] === 0);
  const ordered: T[] = [];
  while (ready.length > 0) {
    ready.sort(compare);
    const index = ready.shift();
    if (index === undefined) break;
    ordered.push(checks[index]);
    for (const child of children[index]) {
      indegree[child] -= 1;
      if (indegree[child] === 0) ready.push(child);
    }
  }
  const orderedIds = new Set(ordered.map((check) => check.id));
  ordered.push(...checks.filter((check) => !orderedIds.has(check.id)));

  const originalPosition = new Map(ordered.map((check, index) => [check.id, index]));
  const firstDirection = new Map<CalculationDirection, number>();
  ordered.forEach((check, index) => {
    if (check.type === "합계 검수" && !firstDirection.has(calculationDirection(check))) {
      firstDirection.set(calculationDirection(check), index);
    }
  });
  return [...ordered].sort((left, right) => {
    const typeOrder =
      (VALIDATION_CHECK_TYPE_ORDER.get(left.type) ?? 999) -
      (VALIDATION_CHECK_TYPE_ORDER.get(right.type) ?? 999);
    if (typeOrder !== 0) return typeOrder;
    if (left.type === "합계 검수") {
      const directionOrder =
        (firstDirection.get(calculationDirection(left)) ?? 999) -
        (firstDirection.get(calculationDirection(right)) ?? 999);
      if (directionOrder !== 0) return directionOrder;
    }
    return (originalPosition.get(left.id) ?? 999) - (originalPosition.get(right.id) ?? 999);
  });
}
