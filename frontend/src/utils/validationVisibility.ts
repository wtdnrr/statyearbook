import type {
  ReportSummary,
  StatTable,
  StatTablePart,
  TableStatus,
  ValidationIssue,
} from "../types";

export const VALIDATION_CHECK_TYPES = [
  "합계 검수",
  "비율 검수",
  "오탈자 검수",
  "번역 검수",
  "용어 제안",
  "파란색 표기 확인",
  "메타정보 검수",
  "이상치 검수",
] as const;

export const DEFAULT_HIDDEN_VALIDATION_TYPES = new Set<string>(["이상치 검수"]);

export function validationTypesForTables(tables: StatTable[]) {
  const discovered = new Set(VALIDATION_CHECK_TYPES);
  for (const table of tables) {
    for (const check of table.checks) {
      discovered.add(check.type as (typeof VALIDATION_CHECK_TYPES)[number]);
    }
  }
  return Array.from(discovered);
}

function visibleChecks(checks: ValidationIssue[], hiddenTypes: ReadonlySet<string>) {
  if (hiddenTypes.size === 0) {
    return checks;
  }

  return checks.filter((check) => !hiddenTypes.has(check.type));
}

function statusFromChecks(checks: ValidationIssue[]): { status: TableStatus; status_label: string } {
  const activeChecks = checks.filter((check) => check.status !== "정상");

  if (activeChecks.some((check) => check.status === "오류 의심" || check.severity === "critical")) {
    return { status: "suspected_error", status_label: "오류 의심" };
  }

  if (activeChecks.length > 0) {
    return { status: "needs_review", status_label: "확인 필요" };
  }

  return { status: "normal", status_label: "정상" };
}

function partWithVisibleChecks(part: StatTablePart, hiddenTypes: ReadonlySet<string>): StatTablePart {
  const checks = visibleChecks(part.checks, hiddenTypes);
  const status = statusFromChecks(checks);

  return {
    ...part,
    checks,
    ...status,
  };
}

export function tablesWithValidationVisibility(
  tables: StatTable[],
  hiddenTypes: ReadonlySet<string>,
): StatTable[] {
  if (hiddenTypes.size === 0) {
    return tables;
  }

  return tables.map((table) => {
    const checks = visibleChecks(table.checks, hiddenTypes);
    const parts = table.parts.map((part) => partWithVisibleChecks(part, hiddenTypes));
    const status = statusFromChecks(checks);

    return {
      ...table,
      checks,
      parts,
      ...status,
    };
  });
}

export function summaryWithValidationVisibility(
  summary: ReportSummary,
  tables: StatTable[],
): ReportSummary {
  const issueCounts: Record<string, number> = {};
  let normalCount = 0;
  let needsReviewCount = 0;
  let suspectedErrorCount = 0;

  for (const table of tables) {
    if (table.status === "normal") {
      normalCount += 1;
    } else if (table.status === "needs_review") {
      needsReviewCount += 1;
    } else if (table.status === "suspected_error") {
      suspectedErrorCount += 1;
    }

    for (const check of table.checks) {
      if (check.status === "정상") {
        continue;
      }
      issueCounts[check.type] = (issueCounts[check.type] ?? 0) + 1;
    }
  }

  return {
    ...summary,
    total_tables: tables.length,
    normal_count: normalCount,
    needs_review_count: needsReviewCount,
    suspected_error_count: suspectedErrorCount,
    issue_counts: issueCounts,
  };
}
