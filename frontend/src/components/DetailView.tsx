import { useEffect, useMemo, useState } from "react";
import {
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  Download,
  FileCode2,
} from "lucide-react";

import type {
  StatTable,
  StatTablePart,
  ValidationHighlightCell,
  ValidationHighlightRow,
  ValidationIssue,
} from "../types";
import {
  LINGUISTIC_CHECK_TYPES,
  highlightCellKey,
  relatedCellsForCheck,
  repeatedCalculationGroupKey,
  sortChecksByCalculationHierarchy,
  targetCellForCheck,
  validationDisplayGroupKey,
} from "../utils/validationGrouping";
import { firstFocusableCheck, resolveIssueLocation } from "../utils/validationLocation";
import { DataGrid } from "./DataGrid";
import { StatusBadge } from "./StatusBadge";
import { VisualPanel } from "./VisualPanel";
import { ValidationTypeFilter } from "./ValidationTypeFilter";

type DetailTab = "checks" | "changes" | "visuals" | "metadata";
type CheckFilter = "all" | "passed" | "review" | "error";

interface CheckTargetLocation {
  row: string;
  column: string;
  isHeader: boolean;
}

interface DisplayCheck extends ValidationIssue {
  checks: ValidationIssue[];
  targetLocations: CheckTargetLocation[];
  targetCount: number;
  rowSummary: string;
  columnSummary: string;
  highlight_cells: ValidationHighlightCell[];
  highlight_rows: ValidationHighlightRow[];
  focus_cell?: ValidationHighlightCell | null;
}

interface GridHighlightLocation {
  rowText?: string;
  columnText?: string;
}

interface DetailViewProps {
  table: StatTable;
  validationTypes: string[];
  hiddenValidationTypes: ReadonlySet<string>;
  onValidationTypeVisibilityChange: (type: string, visible: boolean) => void;
  onShowAllValidationTypes: () => void;
  onHideAllValidationTypes: () => void;
  onBack: () => void;
}

const checkFilters: Array<{ id: CheckFilter; label: string }> = [
  { id: "all", label: "전체" },
  { id: "passed", label: "통과" },
  { id: "review", label: "확인 필요" },
  { id: "error", label: "오류 의심" },
];

const tabs: Array<{ id: DetailTab; label: string }> = [
  { id: "checks", label: "검수 결과" },
  { id: "changes", label: "전년도 비교" },
  { id: "visuals", label: "시각화" },
  { id: "metadata", label: "출처·메타정보" },
];

function checksForFilter<T extends { status: string }>(
  filter: CheckFilter,
  allChecks: T[],
  passedChecks: T[],
  reviewChecks: T[],
  errorChecks: T[],
) {
  if (filter === "passed") {
    return passedChecks;
  }
  if (filter === "review") {
    return reviewChecks;
  }
  if (filter === "error") {
    return errorChecks;
  }
  return allChecks;
}

function shouldExpandCalculationFamily(check: ValidationIssue) {
  return Boolean(repeatedCalculationGroupKey(check)) && check.status !== "오류 의심";
}

function aggregateStatus(checks: ValidationIssue[]) {
  if (checks.some((check) => check.status === "오류 의심")) {
    return { status: "오류 의심", severity: "critical" as const };
  }
  if (checks.some((check) => check.status === "확인 필요")) {
    return { status: "확인 필요", severity: "warning" as const };
  }
  return { status: "정상", severity: "info" as const };
}

function uniqueValues(values: Array<string | undefined>) {
  return Array.from(new Set(values.map((value) => value?.trim() ?? "").filter(Boolean)));
}

function summarizeValues(values: string[], unitLabel: string) {
  if (values.length === 0) {
    return "검수 대상";
  }
  if (values.length === 1) {
    return values[0];
  }
  if (values.length <= 3) {
    return values.join(", ");
  }
  return `${values.slice(0, 3).join(", ")} 외 ${values.length - 3}개 ${unitLabel}`;
}

function summarizeRepeatedValues(values: Array<string | undefined>, fallback: string) {
  const unique = uniqueValues(values);
  if (unique.length === 0) {
    return fallback;
  }
  return unique.length === 1 ? unique[0] : fallback;
}

function summaryDetailForGroupedCheck(check: ValidationIssue, targetCount: number) {
  const baseDetail = check.detail.split("연산에 사용한 셀:")[0].trim();
  if (!baseDetail) {
    return `같은 방식으로 ${targetCount}개 셀에 적용된 검수입니다.`;
  }
  const normalizedDetail = baseDetail.endsWith(".") ? baseDetail : `${baseDetail}.`;
  return `${normalizedDetail} 같은 방식으로 ${targetCount}개 셀에 적용된 검수입니다.`;
}

function checkDisplayTitle(check: Pick<ValidationIssue, "formula" | "type">) {
  return check.formula?.trim() || check.type;
}

function checkDisplayEyebrow(check: Pick<ValidationIssue, "formula" | "type">) {
  return check.formula?.trim() ? check.type : "검수 항목";
}

function uniqueSourceChecks(sourceChecks: ValidationIssue[]) {
  const seen = new Set<string>();

  return sourceChecks.filter((check) => {
    const key = [
      check.rule_id ?? "",
      check.type,
      check.status,
      check.location,
      check.row_index ?? "",
      check.col_index ?? "",
      check.current_value,
      check.expected_value ?? "",
      check.difference ?? "",
      check.formula ?? "",
    ].join("::");
    if (seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
}

function columnLabelAt(part: StatTablePart, colIndex: number | undefined) {
  if (colIndex === undefined || colIndex < 0) {
    return "열 정보 없음";
  }
  const column = columnForSourceIndex(part, colIndex);
  if (!column) {
    return `${colIndex + 1}열`;
  }
  return [column.label, column.label_en].filter(Boolean).join(" ");
}

function sourceColumnIndexes(column: StatTablePart["columns"][number], fallbackIndex: number) {
  if (column.source_col_indexes?.length) {
    return column.source_col_indexes;
  }
  if (typeof column.source_col_index === "number") {
    return [column.source_col_index];
  }
  return [fallbackIndex];
}

function columnForSourceIndex(part: StatTablePart, sourceColIndex: number | undefined) {
  if (typeof sourceColIndex !== "number" || sourceColIndex < 0) {
    return undefined;
  }
  return part.columns.find((column, columnIndex) =>
    sourceColumnIndexes(column, columnIndex).includes(sourceColIndex),
  );
}

function rowForMatrixIndex(part: StatTablePart, rowIndex: number | undefined) {
  if (rowIndex === undefined) {
    return undefined;
  }
  return part.rows.find((row) => Number(row._row_index) === rowIndex);
}

function rowLabelAt(part: StatTablePart, rowIndex: number | undefined) {
  if (rowIndex === undefined || rowIndex < 0) {
    return "행 정보 없음";
  }
  const headerCount = part.metadata.header_count ?? 0;
  if (rowIndex < headerCount) {
    return "헤더";
  }
  const row = rowForMatrixIndex(part, rowIndex);
  const firstColumnKey = part.columns[0]?.key;
  const label = String(row?._row_label ?? "").trim() || (firstColumnKey ? String(row?.[firstColumnKey] ?? "").trim() : "");
  return label || `${rowIndex + 1}행`;
}

function cellValueAt(part: StatTablePart, rowIndex: number | undefined, colIndex: number | undefined) {
  if (rowIndex === undefined || colIndex === undefined || colIndex < 0) {
    return "";
  }
  const headerCount = part.metadata.header_count ?? 0;
  if (rowIndex < headerCount) {
    return columnLabelAt(part, colIndex);
  }
  const row = rowForMatrixIndex(part, rowIndex);
  const column = columnForSourceIndex(part, colIndex);
  if (!row || !column) {
    return "";
  }
  return String(row[column.key] ?? "").trim();
}

function cellDescription(part: StatTablePart, cell: ValidationHighlightCell) {
  const value = cellValueAt(part, cell.row_index, cell.col_index);
  return {
    key: `${cell.row_index}:${cell.col_index}`,
    row: rowLabelAt(part, cell.row_index),
    column: columnLabelAt(part, cell.col_index),
    value,
  };
}

function uniqueHighlightLocations(locations: GridHighlightLocation[]) {
  const seen = new Set<string>();

  return locations.filter((location) => {
    const key = `${location.rowText ?? ""}::${location.columnText ?? ""}`;
    if (seen.has(key)) {
      return false;
    }
    seen.add(key);
    return Boolean(location.rowText || location.columnText);
  });
}

function highlightRowKey(row: ValidationHighlightRow) {
  return String(row.row_index);
}

function normalizeHighlightRole<T extends ValidationHighlightCell | ValidationHighlightRow>(
  item: T,
  _status: string,
): T {
  return {
    ...item,
    role: item.role,
  };
}

function mergeHighlightCells(checks: ValidationIssue[]) {
  const hasFailure = checks.some((check) => check.status !== "정상");
  const cellsByKey = new Map<string, ValidationHighlightCell>();

  for (const check of checks) {
    for (const cell of check.highlight_cells ?? []) {
      const normalizedCell = normalizeHighlightRole(
        cell,
        hasFailure && check.status === "정상" ? "정상" : check.status,
      );
      const key = highlightCellKey(normalizedCell);
      const current = cellsByKey.get(key);
      const shouldReplace =
        !current ||
        (current.role !== "target" && normalizedCell.role === "target") ||
        (hasFailure && check.status !== "정상" && normalizedCell.role === "target");

      if (shouldReplace) {
        cellsByKey.set(key, normalizedCell);
      }
    }
  }

  return Array.from(cellsByKey.values());
}

function mergeHighlightRows(checks: ValidationIssue[]) {
  const hasFailure = checks.some((check) => check.status !== "정상");
  const rowsByKey = new Map<string, ValidationHighlightRow>();

  for (const check of checks) {
    for (const row of check.highlight_rows ?? []) {
      const normalizedRow = normalizeHighlightRole(
        row,
        hasFailure && check.status === "정상" ? "정상" : check.status,
      );
      const key = highlightRowKey(normalizedRow);
      const current = rowsByKey.get(key);
      if (!current || normalizedRow.role === "target") {
        rowsByKey.set(key, normalizedRow);
      }
    }
  }

  return Array.from(rowsByKey.values());
}

function focusCellForChecks(checks: ValidationIssue[], cells: ValidationHighlightCell[]) {
  const failedTarget = checks
    .filter((check) => check.status !== "정상")
    .flatMap((check) => check.highlight_cells ?? [])
    .find((cell) => cell.role === "target");

  return failedTarget ?? cells.find((cell) => cell.role === "target") ?? cells[0] ?? null;
}

function highlightToneForStatus(status: string): "pass" | "review" | "error" {
  if (status === "오류 의심") {
    return "error";
  }
  if (status === "확인 필요") {
    return "review";
  }
  return "pass";
}

function highlightForCheck(_part: StatTablePart, check: DisplayCheck | undefined) {
  if (!check) {
    return undefined;
  }

  if (check.highlight_cells.length > 0 || check.highlight_rows.length > 0) {
    return {
      tone: highlightToneForStatus(check.status),
      highlightCells: check.highlight_cells,
      highlightRows: check.highlight_rows,
      focusCell: check.focus_cell,
    };
  }

  const targetLocations = uniqueHighlightLocations(
    check.targetLocations
      .filter((location) => !location.isHeader)
      .map((location) => ({
        rowText: location.row,
        columnText: location.column === "검수 대상" ? undefined : location.column,
      })),
  );
  const headerLocations = uniqueHighlightLocations(
    check.targetLocations
      .filter((location) => location.isHeader)
      .map((location) => ({
        columnText: location.column === "검수 대상" ? undefined : location.column,
      })),
  );

  return {
    tone: highlightToneForStatus(check.status),
    targetLocations,
    headerLocations,
    relatedLocations: [],
  };
}

function groupChecksForDisplay(part: StatTablePart, sourceChecks: ValidationIssue[]): DisplayCheck[] {
  const groups = new Map<string, ValidationIssue[]>();
  for (const check of uniqueSourceChecks(sourceChecks)) {
    const key = validationDisplayGroupKey(check);
    groups.set(key, [...(groups.get(key) ?? []), check]);
  }

  const displayChecks = Array.from(groups.values()).map((checks) => {
    const first = checks[0];
    const failedChecks = checks.filter((check) => check.status !== "정상");
    const representative = failedChecks[0] ?? first;
    const aggregated = aggregateStatus(checks);
    const highlightCells = mergeHighlightCells(checks);
    const highlightRows = mergeHighlightRows(checks);
    const focusCell = focusCellForChecks(checks, highlightCells);
    const targetLocations = checks.map((check) => resolveIssueLocation(part, check));
    const rows = uniqueValues(targetLocations.map((location) => location.row));
    const columns = uniqueValues(targetLocations.map((location) => location.column));
    const targetCount = checks.length;
    const isGroupedLinguisticPass =
      checks.every((check) => check.status === "정상" && LINGUISTIC_CHECK_TYPES.has(check.type));

    if (isGroupedLinguisticPass) {
      const reviewedTypes = uniqueValues(checks.map((check) => check.type));
      const reviewType = reviewedTypes[0] ?? representative.type;
      const reviewedCellCount = new Set(
        highlightCells.map((cell) => highlightCellKey(cell)),
      ).size;
      const displayTargetCount = reviewedCellCount || targetCount;
      return {
        ...representative,
        id: `group-${validationDisplayGroupKey(first)}`,
        type: reviewType,
        current_value: `${displayTargetCount}개 셀 검수`,
        expected_value: "교정 사항 없음",
        difference: `${reviewType} 통과`,
        detail: `${displayTargetCount}개 헤더·셀을 확인했으며 교정이 필요한 내용이 발견되지 않았습니다.`,
        status: aggregated.status,
        severity: aggregated.severity,
        checks,
        targetLocations,
        targetCount: displayTargetCount,
        rowSummary: summarizeValues(rows, "행"),
        columnSummary: summarizeValues(columns, "열"),
        highlight_cells: highlightCells,
        highlight_rows: highlightRows,
        focus_cell: focusCell,
      };
    }

    if (targetCount === 1) {
      return {
        ...representative,
        status: aggregated.status,
        severity: aggregated.severity,
        checks,
        targetLocations,
        targetCount,
        rowSummary: rows[0] ?? "검수 대상",
        columnSummary: columns[0] ?? "검수 대상",
        highlight_cells: highlightCells,
        highlight_rows: highlightRows,
        focus_cell: focusCell,
      };
    }

    return {
      ...representative,
      id: `group-${validationDisplayGroupKey(first)}`,
      location: `${summarizeValues(rows, "행")} / ${summarizeValues(columns, "열")}`,
      current_value: failedChecks.length > 0 ? representative.current_value : `${targetCount}개 셀`,
      expected_value: summarizeRepeatedValues(
        checks.map((check) => check.expected_value),
        first.formula ? "동일 산식 기준" : "동일 검수 기준",
      ),
      difference: failedChecks.length > 0 ? `${failedChecks.length}건` : undefined,
      status: aggregated.status,
      severity: aggregated.severity,
      detail: summaryDetailForGroupedCheck(first, targetCount),
      checks,
      targetLocations,
      targetCount,
      rowSummary: summarizeValues(rows, "행"),
      columnSummary: summarizeValues(columns, "열"),
      highlight_cells: highlightCells,
      highlight_rows: highlightRows,
      focus_cell: focusCell,
    };
  });

  return sortChecksByCalculationHierarchy(displayChecks);
}

function shouldShowCalculationUsage(check: DisplayCheck) {
  return check.checks.some(
    (item) =>
      Boolean(item.formula?.trim()) ||
      (item.highlight_cells ?? []).some((cell) => cell.role === "related"),
  );
}

function expandCalculationFamilyForHighlight(
  part: StatTablePart,
  check: DisplayCheck | undefined,
  sourceChecks: ValidationIssue[],
) {
  if (!check || !shouldExpandCalculationFamily(check)) {
    return check;
  }

  const familyKey = repeatedCalculationGroupKey(check);
  const existingIds = new Set(check.checks.map((item) => item.id));
  const siblingChecks = sourceChecks.filter(
    (item) =>
      repeatedCalculationGroupKey(item) === familyKey &&
      item.status === check.status &&
      !existingIds.has(item.id),
  );
  if (siblingChecks.length === 0) {
    return check;
  }

  const checks = [...check.checks, ...siblingChecks];
  if (checks.length > 3) {
    return check;
  }

  const highlightCells = mergeHighlightCells(checks);
  const highlightRows = mergeHighlightRows(checks);
  const focusCell = focusCellForChecks(checks, highlightCells);
  const targetLocations = checks.map((item) => resolveIssueLocation(part, item));
  const rows = uniqueValues(targetLocations.map((location) => location.row));
  const columns = uniqueValues(targetLocations.map((location) => location.column));

  return {
    ...check,
    checks,
    targetLocations,
    targetCount: checks.length,
    rowSummary: summarizeValues(rows, "행"),
    columnSummary: summarizeValues(columns, "열"),
    highlight_cells: highlightCells,
    highlight_rows: highlightRows,
    focus_cell: focusCell,
  };
}

function rootTableAsPart(table: StatTable): StatTablePart {
  return {
    id: table.id,
    code: table.code,
    title: table.title,
    title_en: table.title_en,
    part_label: "원본 표",
    unit: table.unit,
    status: table.status,
    status_label: table.status_label,
    updated_at: table.updated_at,
    columns: table.columns,
    rows: table.rows,
    checks: table.checks,
    changes: table.changes,
    visualizations: table.visualizations,
    metadata: table.metadata,
  };
}

function CalculationUsageList({ part, check }: { part: StatTablePart; check: DisplayCheck }) {
  return (
    <section className="calculation-usage" aria-label="검수 적용 내역">
      <div className="calculation-usage__header">
        <span>검수 적용 내역</span>
        <strong>{check.checks.length}건</strong>
      </div>
      <div className="calculation-usage__list">
        {check.checks.map((rawCheck) => {
          const targetCell = targetCellForCheck(rawCheck);
          const target = targetCell ? cellDescription(part, targetCell) : undefined;
          const relatedCells = relatedCellsForCheck(rawCheck).map((cell) => cellDescription(part, cell));

          return (
            <article className="calculation-usage__item" key={rawCheck.id}>
              <div className="calculation-usage__item-head">
                <span>{rawCheck.status}</span>
                <strong>{checkDisplayTitle(rawCheck)}</strong>
              </div>

              <dl className="calculation-usage__target">
                <div>
                  <dt>대상 행</dt>
                  <dd>{target?.row ?? "행 정보 없음"}</dd>
                </div>
                <div>
                  <dt>대상 열</dt>
                  <dd>{target?.column ?? "열 정보 없음"}</dd>
                </div>
                <div>
                  <dt>현재값</dt>
                  <dd>{rawCheck.current_value}</dd>
                </div>
                <div>
                  <dt>검수값</dt>
                  <dd>{rawCheck.expected_value ?? "-"}</dd>
                </div>
                <div>
                  <dt>차이</dt>
                  <dd>{rawCheck.difference ?? "-"}</dd>
                </div>
              </dl>

              {rawCheck.formula ? (
                <div className="calculation-usage__formula">
                  <FileCode2 aria-hidden="true" size={14} />
                  <span>{rawCheck.formula}</span>
                </div>
              ) : null}

              <div className="calculation-usage__cells">
                <span>연산에 사용한 셀</span>
                {relatedCells.length > 0 ? (
                  <ul>
                    {relatedCells.map((cell) => (
                      <li key={cell.key}>
                        <strong>{cell.row}</strong>
                        <em>{cell.column}</em>
                        {cell.value ? <small>{cell.value}</small> : null}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p>별도 참조 셀 없이 대상 셀 자체를 확인했습니다.</p>
                )}
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}

function MetadataGrid({
  part,
  difference,
}: {
  part: StatTablePart;
  difference?: string;
}) {
  const hasProblem = (label: string) => difference?.includes(label) ?? false;

  return (
    <dl className="metadata-grid">
      <div className={hasProblem("기준일") ? "is-problem" : ""}>
        <dt>기준일</dt>
        <dd>{part.metadata.base_date_display || "-"}</dd>
      </div>
      <div className={hasProblem("단위") ? "is-problem" : ""}>
        <dt>단위</dt>
        <dd>{part.metadata.unit_display || "-"}</dd>
      </div>
      <div>
        <dt>최종 수정 일자</dt>
        <dd>{part.updated_at}</dd>
      </div>
      <div className={hasProblem("출처") ? "is-problem" : ""}>
        <dt>소속</dt>
        <dd>{part.metadata.source_department || "-"}</dd>
      </div>
      <div className={hasProblem("출처") ? "is-problem" : ""}>
        <dt>이름</dt>
        <dd>{part.metadata.source_officer || "-"}</dd>
      </div>
      <div className={hasProblem("출처") ? "is-problem" : ""}>
        <dt>내선번호</dt>
        <dd>{part.metadata.source_extension || "-"}</dd>
      </div>
      <div className={`metadata-grid__wide ${hasProblem("출처") ? "is-problem" : ""}`}>
        <dt>출처</dt>
        <dd>{part.metadata.source_reference || part.metadata.source || "-"}</dd>
      </div>
      <div className="metadata-grid__wide">
        <dt>주석</dt>
        <dd>{part.metadata.note || "-"}</dd>
      </div>
    </dl>
  );
}

export function DetailView({
  table,
  validationTypes,
  hiddenValidationTypes,
  onValidationTypeVisibilityChange,
  onShowAllValidationTypes,
  onHideAllValidationTypes,
  onBack,
}: DetailViewProps) {
  const [activeTab, setActiveTab] = useState<DetailTab>("checks");
  const tableParts = useMemo(() => (table.parts.length > 0 ? table.parts : [rootTableAsPart(table)]), [table]);
  const [activePartId, setActivePartId] = useState<string>(tableParts[0]?.id ?? table.id);
  const activePart = tableParts.find((part) => part.id === activePartId) ?? tableParts[0];
  const [checkFilter, setCheckFilter] = useState<CheckFilter>("error");
  const [selectedCheckId, setSelectedCheckId] = useState<string | undefined>(activePart?.checks[0]?.id);
  const [tableScrollSignal, setTableScrollSignal] = useState(0);
  const [isTableHeaderSticky, setIsTableHeaderSticky] = useState(true);
  const allRawChecks = useMemo(
    () => activePart.checks.filter((check) => !hiddenValidationTypes.has(check.type)),
    [activePart.checks, hiddenValidationTypes],
  );
  const passedRawChecks = useMemo(() => allRawChecks.filter((check) => check.status === "정상"), [allRawChecks]);
  const reviewRawChecks = useMemo(() => allRawChecks.filter((check) => check.status === "확인 필요"), [allRawChecks]);
  const errorRawChecks = useMemo(() => allRawChecks.filter((check) => check.status === "오류 의심"), [allRawChecks]);
  const displayChecks = useMemo(() => groupChecksForDisplay(activePart, allRawChecks), [activePart, allRawChecks]);
  const passedChecks = useMemo(() => groupChecksForDisplay(activePart, passedRawChecks), [activePart, passedRawChecks]);
  const reviewChecks = useMemo(
    () => groupChecksForDisplay(activePart, reviewRawChecks),
    [activePart, reviewRawChecks],
  );
  const errorChecks = useMemo(
    () => groupChecksForDisplay(activePart, errorRawChecks),
    [activePart, errorRawChecks],
  );
  const filteredChecks = checksForFilter(checkFilter, displayChecks, passedChecks, reviewChecks, errorChecks);
  const selectedIssueBase =
    filteredChecks.find((check) => check.id === selectedCheckId) ??
    filteredChecks[0];
  const selectedIssue = expandCalculationFamilyForHighlight(activePart, selectedIssueBase, allRawChecks);
  const isMetadataIssue = selectedIssue?.highlight_scope === "metadata";
  const activeIssueIndex = Math.max(filteredChecks.findIndex((check) => check.id === selectedIssue?.id), 0);
  const selectedIssueHighlight = highlightForCheck(activePart, selectedIssue);
  const parentHierarchy = table.hierarchy.slice(0, -1);

  function selectIssue(issueId: string) {
    setSelectedCheckId(issueId);
    setTableScrollSignal((value) => value + 1);
  }

  function selectFilter(filter: CheckFilter) {
    const nextChecks = checksForFilter(filter, displayChecks, passedChecks, reviewChecks, errorChecks);
    const nextIssue = firstFocusableCheck(activePart, nextChecks);

    setCheckFilter(filter);
    setSelectedCheckId(nextIssue?.id);
    if (nextIssue) {
      setTableScrollSignal((value) => value + 1);
    }
  }

  function selectPart(partId: string) {
    setActivePartId(partId);
    setTableScrollSignal(0);
  }

  function moveIssue(direction: -1 | 1) {
    if (filteredChecks.length === 0) {
      return;
    }

    const nextIndex = (activeIssueIndex + direction + filteredChecks.length) % filteredChecks.length;
    selectIssue(filteredChecks[nextIndex].id);
  }

  useEffect(() => {
    const initialFilter: CheckFilter = errorChecks.length > 0 ? "error" : reviewChecks.length > 0 ? "review" : "all";
    const initialChecks = checksForFilter(initialFilter, displayChecks, passedChecks, reviewChecks, errorChecks);
    setCheckFilter(initialFilter);
    setSelectedCheckId(initialChecks[0]?.id);
  }, [activePart.id, displayChecks, errorChecks, passedChecks, reviewChecks]);

  useEffect(() => {
    setActivePartId(tableParts[0]?.id ?? table.id);
  }, [table.id, tableParts]);

  return (
    <main className="detail-view">
      <header className="detail-header">
        <button className="icon-button" type="button" onClick={onBack} aria-label="뒤로">
          <ArrowLeft aria-hidden="true" size={19} />
        </button>
        <div className={`detail-title detail-title--${table.theme}`}>
          <div className="detail-title__body">
            <div className="detail-title__meta">
              <span className="detail-title__code">{table.code}</span>
              {parentHierarchy.map((item) => (
                <span className="detail-title__crumb" key={`${item.code}-${item.title}`}>
                  {item.code ? <em>{item.code}</em> : null}
                  <strong>{item.title}</strong>
                </span>
              ))}
            </div>
            <h1>{table.title}</h1>
            <p>{table.title_en}</p>
          </div>
        </div>
        <div className="detail-header__actions">
          <ValidationTypeFilter
            types={validationTypes}
            hiddenTypes={hiddenValidationTypes}
            align="right"
            onVisibilityChange={onValidationTypeVisibilityChange}
            onShowAll={onShowAllValidationTypes}
            onHideAll={onHideAllValidationTypes}
          />
          <StatusBadge status={table.status} label={table.status_label} />
          <button className="secondary-button detail-download" type="button">
            <Download aria-hidden="true" size={16} />
            <span>다운로드</span>
          </button>
        </div>
      </header>

      <div className="detail-meta-strip">
        <span>
          <em>기준일</em>
          <strong>{activePart.metadata.base_date}</strong>
        </span>
        <span>
          <em>단위</em>
          <strong>{activePart.unit}</strong>
        </span>
        {table.parts.length > 0 ? (
          <span>
            <em>하위 표</em>
            <strong>{table.parts.length}개</strong>
          </span>
        ) : null}
        <span>
          <em>최종 수정일</em>
          <strong>{activePart.updated_at}</strong>
        </span>
      </div>

      <div className="detail-workspace">
        <aside className="original-table-panel">
          <div className="detail-section detail-section--original">
            <div className="content-title-row">
              <div className="original-title-group">
                <h2>원본 표</h2>
                {tableParts.length > 1 ? (
                  <div className="part-tabs" aria-label="하위 표 선택">
                    {tableParts.map((part) => (
                      <button
                        className={activePart.id === part.id ? "is-active" : ""}
                        key={part.id}
                        type="button"
                        onClick={() => selectPart(part.id)}
                      >
                        <span>{part.part_label}</span>
                      </button>
                    ))}
                  </div>
                ) : null}
              </div>
              <label className="table-option-toggle">
                <input
                  checked={isTableHeaderSticky}
                  onChange={(event) => setIsTableHeaderSticky(event.target.checked)}
                  type="checkbox"
                />
                <span>헤더 고정</span>
              </label>
            </div>
            <DataGrid
              columns={activePart.columns}
              rows={activePart.rows}
              theme={table.theme}
              highlight={selectedIssueHighlight}
              scrollSignal={tableScrollSignal}
              stickyHeader={isTableHeaderSticky}
              headerCount={activePart.metadata.header_count ?? 0}
            />
          </div>
        </aside>

        <section className="detail-analysis-panel" aria-label="상세 분석">
          <nav className="detail-tabs" aria-label="상세 탭">
            {tabs.map((tab) => (
              <button
                className={activeTab === tab.id ? "is-active" : ""}
                key={tab.id}
                type="button"
                onClick={() => setActiveTab(tab.id)}
              >
                <span>{tab.label}</span>
              </button>
            ))}
          </nav>

          <section className="detail-content">
            {activeTab === "checks" ? (
            <div className="detail-section check-review-panel">
              <div className="content-title-row">
                <div>
                  <h2>검수 결과</h2>
                </div>
              </div>

              <div className="check-filter-tabs" aria-label="검수 결과 필터">
                {checkFilters.map((filter) => {
                  const count = checksForFilter(
                    filter.id,
                    displayChecks,
                    passedChecks,
                    reviewChecks,
                    errorChecks,
                  ).length;

                  return (
                    <button
                      className={checkFilter === filter.id ? "is-active" : ""}
                      key={filter.id}
                      type="button"
                      onClick={() => selectFilter(filter.id)}
                    >
                      <span>{filter.label}</span>
                      <strong>{count}</strong>
                    </button>
                  );
                })}
              </div>

              {filteredChecks.length > 0 ? (
                <div className="check-result-list" aria-label="검수 목록">
                  {filteredChecks.map((check, index) => (
                    <button
                      className={selectedIssue?.id === check.id ? "is-active" : ""}
                      key={check.id}
                      type="button"
                      onClick={() => selectIssue(check.id)}
                    >
                      <span>{index + 1}</span>
                      <strong>{checkDisplayTitle(check)}</strong>
                      <em>{check.status}</em>
                    </button>
                  ))}
                </div>
              ) : null}

              {selectedIssue ? (
                <article className={`check-card check-card--${selectedIssue.severity}`}>
                  <div className="check-card__headline">
                    <div>
                      <span>{checkDisplayEyebrow(selectedIssue)}</span>
                      <h3>{checkDisplayTitle(selectedIssue)}</h3>
                    </div>
                    <strong>{selectedIssue.status}</strong>
                  </div>
                  {isMetadataIssue ? (
                    <MetadataGrid part={activePart} difference={selectedIssue.difference} />
                  ) : (
                    <>
                      <dl className="check-card__location">
                        <div>
                          <dt>행</dt>
                          <dd>{selectedIssue.rowSummary}</dd>
                        </div>
                        <div>
                          <dt>열</dt>
                          <dd>{selectedIssue.columnSummary}</dd>
                        </div>
                      </dl>
                      <dl className="check-card__values">
                        <div>
                          <dt>현재값</dt>
                          <dd>{selectedIssue.current_value}</dd>
                        </div>
                        <div>
                          <dt>검수값</dt>
                          <dd>{selectedIssue.expected_value ?? "-"}</dd>
                        </div>
                        <div>
                          <dt>차이</dt>
                          <dd>{selectedIssue.difference ?? "-"}</dd>
                        </div>
                      </dl>
                    </>
                  )}
                  <p>{selectedIssue.detail}</p>
                  {selectedIssue.formula && selectedIssue.formula !== checkDisplayTitle(selectedIssue) ? (
                    <div className="formula-box">
                      <FileCode2 aria-hidden="true" size={16} />
                      <span>{selectedIssue.formula}</span>
                    </div>
                  ) : null}
                  {!isMetadataIssue && shouldShowCalculationUsage(selectedIssue) ? (
                    <CalculationUsageList part={activePart} check={selectedIssue} />
                  ) : null}
                </article>
              ) : (
                <p className="empty-copy">해당 내용이 없습니다.</p>
              )}

              {filteredChecks.length > 0 ? (
                <div className="check-card-nav">
                  <button
                    className="icon-button"
                    type="button"
                    onClick={() => moveIssue(-1)}
                    disabled={filteredChecks.length <= 1}
                    aria-label="이전 검수"
                  >
                    <ChevronLeft aria-hidden="true" size={18} />
                  </button>
                  <span>{`${activeIssueIndex + 1} / ${filteredChecks.length}`}</span>
                  <button
                    className="icon-button"
                    type="button"
                    onClick={() => moveIssue(1)}
                    disabled={filteredChecks.length <= 1}
                    aria-label="다음 검수"
                  >
                    <ChevronRight aria-hidden="true" size={18} />
                  </button>
                </div>
              ) : null}
            </div>
            ) : null}

            {activeTab === "changes" ? (
              <div className="detail-section">
                <div className="content-title-row">
                  <div>
                    <h2>전년도 비교</h2>
                  </div>
                </div>
                <div className="change-table">
                  <div className="change-table__header">
                    <span>구분</span>
                    <span>항목</span>
                    <span>전년도 파일</span>
                    <span>올해 파일</span>
                    <span>상태</span>
                  </div>
                  {activePart.changes.map((change) => (
                    <div className="change-table__row" key={change.id}>
                      <span>{change.category}</span>
                      <strong>{change.item}</strong>
                      <span>{change.previous}</span>
                      <span>{change.current}</span>
                      <span>{change.status}</span>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}

            {activeTab === "visuals" ? (
              <div className="detail-section">
                <div className="content-title-row">
                  <div>
                    <h2>시각화</h2>
                  </div>
                </div>
                <div className="visual-grid">
                  {activePart.visualizations.map((visualization) => (
                    <VisualPanel visualization={visualization} key={visualization.id} />
                  ))}
                </div>
              </div>
            ) : null}

            {activeTab === "metadata" ? (
              <div className="detail-section">
                <div className="content-title-row">
                  <div>
                    <h2>출처·메타정보</h2>
                  </div>
                </div>
                <MetadataGrid part={activePart} />
              </div>
            ) : null}
          </section>
        </section>
      </div>
    </main>
  );
}
