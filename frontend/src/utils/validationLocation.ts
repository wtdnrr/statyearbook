import type { ColumnDefinition, ValidationIssue } from "../types";

interface IssueLocationTable {
  columns: ColumnDefinition[];
  rows: Array<Record<string, string | number>>;
  metadata?: {
    header_count?: number;
  };
}

export function normalizeMatchText(value: string | number | undefined) {
  return String(value ?? "")
    .replace(/\([^)]*\)/g, "")
    .replace(/[\s·,._-]/g, "")
    .toLowerCase();
}

function displayColumnLabel(column: ColumnDefinition | undefined) {
  if (!column) {
    return "";
  }

  return [column.label, column.label_en].filter(Boolean).join(" ");
}

function cleanLocationPart(value: string) {
  return value.replace(/\s+/g, " ").replace(/^[\s/·,-]+|[\s/·,-]+$/g, "").trim();
}

function stripColumnFromLocation(location: string, column: ColumnDefinition | undefined) {
  if (!column) {
    return location;
  }

  const partsToRemove = [column.label, column.label_en].filter(Boolean) as string[];
  return cleanLocationPart(partsToRemove.reduce((value, part) => value.replace(part, ""), location));
}

export function resolveIssueLocation(table: IssueLocationTable, issue: ValidationIssue) {
  const firstColumnKey = table.columns[0]?.key;
  const headerCount = table.metadata?.header_count ?? 0;
  const isHeader = typeof issue.row_index === "number" && headerCount > 0 && issue.row_index < headerCount;
  const indexedColumn =
    typeof issue.col_index === "number" && issue.col_index >= 0 ? table.columns[issue.col_index] : undefined;
  const matchedColumn =
    indexedColumn ??
    table.columns
      .slice(1)
      .sort((a, b) => displayColumnLabel(b).length - displayColumnLabel(a).length)
      .find((column) => normalizeMatchText(issue.location).includes(normalizeMatchText(displayColumnLabel(column))));
  const rowLabelFromText = firstColumnKey
    ? table.rows
        .map((row) => String(row[firstColumnKey] ?? ""))
        .filter(Boolean)
        .sort((a, b) => b.length - a.length)
        .find((value) => normalizeMatchText(issue.location).includes(normalizeMatchText(value)))
    : undefined;
  const rowLabelFromIndex =
    typeof issue.row_index === "number" && firstColumnKey
      ? String(table.rows[headerCount > 0 ? issue.row_index - headerCount : issue.row_index]?.[firstColumnKey] ?? "")
      : "";

  if (isHeader) {
    return {
      row: "헤더",
      column: displayColumnLabel(matchedColumn) || "검수 대상",
      isHeader: true,
    };
  }

  return {
    row: cleanLocationPart(rowLabelFromText ?? rowLabelFromIndex) || stripColumnFromLocation(issue.location, matchedColumn),
    column: displayColumnLabel(matchedColumn) || "검수 대상",
    isHeader: false,
  };
}

export function issueTargetsTable(table: IssueLocationTable, issue: ValidationIssue) {
  const firstColumnKey = table.columns[0]?.key;
  const location = resolveIssueLocation(table, issue);
  const rowMatchesTable =
    Boolean(firstColumnKey && location.row) &&
    table.rows.some((row) => normalizeMatchText(String(row[firstColumnKey] ?? "")).includes(normalizeMatchText(location.row)));

  return location.isHeader || location.column !== "검수 대상" || rowMatchesTable;
}

export function firstFocusableCheck(table: IssueLocationTable, checks: ValidationIssue[]) {
  return checks.find((check) => issueTargetsTable(table, check)) ?? checks[0];
}
