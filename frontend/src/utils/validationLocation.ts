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

function sourceColumnIndexes(column: ColumnDefinition, fallbackIndex: number) {
  if (column.source_col_indexes?.length) {
    return column.source_col_indexes;
  }
  if (typeof column.source_col_index === "number") {
    return [column.source_col_index];
  }
  return [fallbackIndex];
}

function columnForSourceIndex(columns: ColumnDefinition[], sourceColIndex: number | undefined) {
  if (typeof sourceColIndex !== "number" || sourceColIndex < 0) {
    return undefined;
  }

  return columns.find((column, columnIndex) => sourceColumnIndexes(column, columnIndex).includes(sourceColIndex));
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
  const indexedColumn = columnForSourceIndex(table.columns, issue.col_index);
  const matchedColumn =
    indexedColumn ??
    table.columns
      .slice(1)
      .sort((a, b) => displayColumnLabel(b).length - displayColumnLabel(a).length)
      .find((column) => normalizeMatchText(issue.location).includes(normalizeMatchText(displayColumnLabel(column))));
  const rowLabelCandidates = table.rows
    .flatMap((row) => [row._row_label, firstColumnKey ? row[firstColumnKey] : undefined])
    .map((value) => String(value ?? ""))
    .filter(Boolean)
    .sort((a, b) => b.length - a.length);
  const rowLabelFromText = rowLabelCandidates.find((value) =>
    normalizeMatchText(issue.location).includes(normalizeMatchText(value)),
  );
  const rowLabelFromIndex =
    typeof issue.row_index === "number"
      ? String(
          table.rows[headerCount > 0 ? issue.row_index - headerCount : issue.row_index]?._row_label ??
            (firstColumnKey
              ? table.rows[headerCount > 0 ? issue.row_index - headerCount : issue.row_index]?.[firstColumnKey]
              : "") ??
            "",
        )
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
