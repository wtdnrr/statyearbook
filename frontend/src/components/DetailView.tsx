import { useEffect, useMemo, useState } from "react";
import {
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  Download,
  FileCode2,
} from "lucide-react";

import type { ColumnDefinition, StatTable, StatTablePart, ValidationIssue } from "../types";
import { firstFocusableCheck, resolveIssueLocation } from "../utils/validationLocation";
import { DataGrid } from "./DataGrid";
import { StatusBadge } from "./StatusBadge";
import { VisualPanel } from "./VisualPanel";

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
}

interface GridHighlightLocation {
  rowText?: string;
  columnText?: string;
}

interface DetailViewProps {
  table: StatTable;
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

function groupKeyForCheck(check: ValidationIssue) {
  return [check.type, check.status, check.severity, check.formula ?? "", check.detail].join("::");
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

function displayColumnLabel(column: ColumnDefinition | undefined) {
  if (!column) {
    return "";
  }
  return [column.label, column.label_en].filter(Boolean).join(" ");
}

function rowLabelFor(part: StatTablePart, row: Record<string, string | number>) {
  const firstColumnKey = part.columns[0]?.key;
  return firstColumnKey ? String(row[firstColumnKey] ?? "") : "";
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

function formulaSymbols(formula: string | undefined) {
  if (!formula) {
    return [];
  }
  return Array.from(new Set(formula.toLowerCase().match(/[a-z]/g) ?? []));
}

function directFormulaSymbolsForColumn(column: ColumnDefinition) {
  const label = displayColumnLabel(column).toLowerCase();
  const symbols = new Set<string>();

  for (const match of label.matchAll(/(?:^|[\s(])([a-z])\s*\)/g)) {
    symbols.add(match[1]);
  }

  for (const match of label.matchAll(/(?:^|[\s(])([a-z])\s*=/g)) {
    symbols.add(match[1]);
  }

  for (const match of label.matchAll(/(?:^|[\s(])([a-z])\s+[a-z]\s*[+\-=]/g)) {
    symbols.add(match[1]);
  }

  return symbols;
}

function formulaRelatedTargets(part: StatTablePart, check: DisplayCheck) {
  if (!["합계", "비율", "증감률"].some((keyword) => check.type.includes(keyword))) {
    return [];
  }

  const symbols = formulaSymbols(check.formula);
  if (symbols.length === 0) {
    return [];
  }

  const targetColumnIndexes = new Set(
    check.checks
      .map((item) => item.col_index)
      .filter((value): value is number => typeof value === "number" && value >= 0),
  );
  const relatedColumns = part.columns.filter((column, index) => {
    if (index === 0) {
      return false;
    }
    const directSymbols = directFormulaSymbolsForColumn(column);
    return targetColumnIndexes.has(index) || symbols.some((symbol) => directSymbols.has(symbol));
  });
  const rowLabels = part.rows.map((row) => rowLabelFor(part, row)).filter(Boolean);

  return relatedColumns.flatMap((column) =>
    rowLabels.map((rowText) => ({
      rowText,
      columnText: displayColumnLabel(column),
    })),
  );
}

function highlightForCheck(part: StatTablePart, check: DisplayCheck | undefined) {
  if (!check) {
    return undefined;
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
  const relatedLocations = uniqueHighlightLocations(formulaRelatedTargets(part, check));

  return {
    targetLocations,
    headerLocations,
    relatedLocations,
  };
}

function groupChecksForDisplay(part: StatTablePart): DisplayCheck[] {
  const groups = new Map<string, ValidationIssue[]>();
  for (const check of part.checks) {
    const key = groupKeyForCheck(check);
    groups.set(key, [...(groups.get(key) ?? []), check]);
  }

  return Array.from(groups.values()).map((checks) => {
    const first = checks[0];
    const targetLocations = checks.map((check) => resolveIssueLocation(part, check));
    const rows = uniqueValues(targetLocations.map((location) => location.row));
    const columns = uniqueValues(targetLocations.map((location) => location.column));
    const targetCount = checks.length;

    if (targetCount === 1) {
      return {
        ...first,
        checks,
        targetLocations,
        targetCount,
        rowSummary: rows[0] ?? "검수 대상",
        columnSummary: columns[0] ?? "검수 대상",
      };
    }

    return {
      ...first,
      id: `group-${groupKeyForCheck(first)}`,
      location: `${summarizeValues(rows, "행")} / ${summarizeValues(columns, "열")}`,
      current_value: `${targetCount}개 셀`,
      expected_value: summarizeRepeatedValues(
        checks.map((check) => check.expected_value),
        first.formula ? "동일 산식 기준" : "동일 검수 기준",
      ),
      difference: first.status === "정상" ? undefined : `${targetCount}건`,
      detail: `${first.detail} 같은 방식으로 ${targetCount}개 셀에 적용된 검수입니다.`,
      checks,
      targetLocations,
      targetCount,
      rowSummary: summarizeValues(rows, "행"),
      columnSummary: summarizeValues(columns, "열"),
    };
  });
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

export function DetailView({ table, onBack }: DetailViewProps) {
  const [activeTab, setActiveTab] = useState<DetailTab>("checks");
  const tableParts = useMemo(() => (table.parts.length > 0 ? table.parts : [rootTableAsPart(table)]), [table]);
  const [activePartId, setActivePartId] = useState<string>(tableParts[0]?.id ?? table.id);
  const activePart = tableParts.find((part) => part.id === activePartId) ?? tableParts[0];
  const [checkFilter, setCheckFilter] = useState<CheckFilter>("error");
  const [selectedCheckId, setSelectedCheckId] = useState<string | undefined>(activePart?.checks[0]?.id);
  const [tableScrollSignal, setTableScrollSignal] = useState(0);
  const [isTableHeaderSticky, setIsTableHeaderSticky] = useState(true);
  const displayChecks = useMemo(() => groupChecksForDisplay(activePart), [activePart]);
  const passedChecks = useMemo(() => displayChecks.filter((check) => check.status === "정상"), [displayChecks]);
  const reviewChecks = useMemo(
    () => displayChecks.filter((check) => check.status === "확인 필요"),
    [displayChecks],
  );
  const errorChecks = useMemo(
    () => displayChecks.filter((check) => check.status === "오류 의심"),
    [displayChecks],
  );
  const filteredChecks = checksForFilter(checkFilter, displayChecks, passedChecks, reviewChecks, errorChecks);
  const selectedIssue =
    filteredChecks.find((check) => check.id === selectedCheckId) ??
    filteredChecks[0] ??
    displayChecks.find((check) => check.id === selectedCheckId) ??
    displayChecks[0];
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

              {selectedIssue ? (
                <article className={`check-card check-card--${selectedIssue.severity}`}>
                  <div className="check-card__headline">
                    <div>
                      <span>검수 항목</span>
                      <h3>{selectedIssue.type}</h3>
                    </div>
                    <strong>{selectedIssue.status}</strong>
                  </div>
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
                  <p>{selectedIssue.detail}</p>
                  {selectedIssue.formula ? (
                    <div className="formula-box">
                      <FileCode2 aria-hidden="true" size={16} />
                      <span>{selectedIssue.formula}</span>
                    </div>
                  ) : null}
                </article>
              ) : (
                <p className="empty-copy">이 분류에 해당하는 검수 결과가 없습니다.</p>
              )}

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
                <span>
                  {filteredChecks.length > 0 ? `${activeIssueIndex + 1} / ${filteredChecks.length}` : "0 / 0"}
                </span>
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
                <dl className="metadata-grid">
                  <div>
                    <dt>기준일</dt>
                    <dd>{activePart.metadata.base_date}</dd>
                  </div>
                  <div>
                    <dt>단위</dt>
                    <dd>{activePart.unit}</dd>
                  </div>
                  <div>
                    <dt>표 위치</dt>
                    <dd>{activePart.metadata.cell_range}</dd>
                  </div>
                  <div>
                    <dt>최종 수정 일자</dt>
                    <dd>{activePart.updated_at}</dd>
                  </div>
                  <div className="metadata-grid__wide">
                    <dt>출처</dt>
                    <dd>{activePart.metadata.source}</dd>
                  </div>
                  <div className="metadata-grid__wide">
                    <dt>주석</dt>
                    <dd>{activePart.metadata.note}</dd>
                  </div>
                </dl>
              </div>
            ) : null}
          </section>
        </section>
      </div>
    </main>
  );
}
