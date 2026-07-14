import type {
  ReportSummary,
  StatTable,
  StatTablePart,
  TableStatus,
  ValidationIssue,
} from "../types";

const OUTLIER_CHECK_TYPE = "이상치 검수";

export function isOutlierCheck(check: ValidationIssue) {
  return check.type === OUTLIER_CHECK_TYPE;
}

function visibleChecks(checks: ValidationIssue[], showOutlierChecks: boolean) {
  if (showOutlierChecks) {
    return checks;
  }

  return checks.filter((check) => !isOutlierCheck(check));
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

function partWithVisibleChecks(part: StatTablePart, showOutlierChecks: boolean): StatTablePart {
  const checks = visibleChecks(part.checks, showOutlierChecks);
  const status = statusFromChecks(checks);

  return {
    ...part,
    checks,
    ...status,
  };
}

export function tablesWithValidationVisibility(
  tables: StatTable[],
  showOutlierChecks: boolean,
): StatTable[] {
  if (showOutlierChecks) {
    return tables;
  }

  return tables.map((table) => {
    const checks = visibleChecks(table.checks, showOutlierChecks);
    const parts = table.parts.map((part) => partWithVisibleChecks(part, showOutlierChecks));
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
