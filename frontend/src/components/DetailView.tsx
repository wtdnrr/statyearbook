import { useEffect, useMemo, useState } from "react";
import {
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  Download,
  FileCode2,
} from "lucide-react";

import type { StatTable, StatTablePart } from "../types";
import { DataGrid } from "./DataGrid";
import { StatusBadge } from "./StatusBadge";
import { VisualPanel } from "./VisualPanel";

type DetailTab = "checks" | "changes" | "visuals" | "metadata";
type CheckFilter = "failed" | "passed" | "all";

interface DetailViewProps {
  table: StatTable;
  onBack: () => void;
}

const checkFilters: Array<{ id: CheckFilter; label: string }> = [
  { id: "all", label: "전체" },
  { id: "passed", label: "통과" },
  { id: "failed", label: "미통과" },
];

const tabs: Array<{ id: DetailTab; label: string }> = [
  { id: "checks", label: "검수 결과" },
  { id: "changes", label: "전년도 비교" },
  { id: "visuals", label: "시각화" },
  { id: "metadata", label: "출처·메타정보" },
];

function normalizeMatchText(value: string | number | undefined) {
  return String(value ?? "")
    .replace(/\([^)]*\)/g, "")
    .replace(/[\s·,._-]/g, "")
    .toLowerCase();
}

function inferIssueHighlight(table: StatTable, location: string | undefined) {
  if (!location) {
    return undefined;
  }

  const normalizedLocation = normalizeMatchText(location);
  const firstColumnKey = table.columns[0]?.key;
  const rowText = firstColumnKey
    ? table.rows
        .map((row) => String(row[firstColumnKey] ?? ""))
        .filter(Boolean)
        .sort((a, b) => b.length - a.length)
        .find((value) => normalizedLocation.includes(normalizeMatchText(value)))
    : undefined;
  const columnText = table.columns
    .filter((column) => column.key !== firstColumnKey)
    .find((column) => normalizedLocation.includes(normalizeMatchText(column.label)))?.label;

  return {
    rowText,
    columnText,
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

function inferPartIssueHighlight(table: StatTablePart, location: string | undefined) {
  return inferIssueHighlight(table as StatTable, location);
}

export function DetailView({ table, onBack }: DetailViewProps) {
  const [activeTab, setActiveTab] = useState<DetailTab>("checks");
  const tableParts = useMemo(() => (table.parts.length > 0 ? table.parts : [rootTableAsPart(table)]), [table]);
  const [activePartId, setActivePartId] = useState<string>(tableParts[0]?.id ?? table.id);
  const activePart = tableParts.find((part) => part.id === activePartId) ?? tableParts[0];
  const [checkFilter, setCheckFilter] = useState<CheckFilter>("failed");
  const [selectedCheckId, setSelectedCheckId] = useState<string | undefined>(activePart?.checks[0]?.id);
  const [tableScrollSignal, setTableScrollSignal] = useState(0);
  const [isTableHeaderSticky, setIsTableHeaderSticky] = useState(true);
  const failedChecks = useMemo(() => activePart.checks.filter((check) => check.status !== "정상"), [activePart.checks]);
  const passedChecks = useMemo(() => activePart.checks.filter((check) => check.status === "정상"), [activePart.checks]);
  const filteredChecks =
    checkFilter === "failed" ? failedChecks : checkFilter === "passed" ? passedChecks : activePart.checks;
  const selectedIssue =
    filteredChecks.find((check) => check.id === selectedCheckId) ??
    filteredChecks[0] ??
    activePart.checks.find((check) => check.id === selectedCheckId) ??
    activePart.checks[0];
  const activeIssueIndex = Math.max(filteredChecks.findIndex((check) => check.id === selectedIssue?.id), 0);
  const selectedIssueHighlight = inferPartIssueHighlight(activePart, selectedIssue?.location);
  const parentHierarchy = table.hierarchy.slice(0, -1);

  function selectIssue(issueId: string) {
    setSelectedCheckId(issueId);
    setTableScrollSignal((value) => value + 1);
  }

  function selectFilter(filter: CheckFilter) {
    const nextChecks = filter === "failed" ? failedChecks : filter === "passed" ? passedChecks : activePart.checks;
    setCheckFilter(filter);
    setSelectedCheckId(nextChecks[0]?.id);
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
    const initialChecks = failedChecks.length > 0 ? failedChecks : activePart.checks;
    setCheckFilter(failedChecks.length > 0 ? "failed" : "all");
    setSelectedCheckId(initialChecks[0]?.id);
  }, [activePart.id, activePart.checks, failedChecks]);

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
          <span>{table.code}</span>
          <div>
            <h1>{table.title}</h1>
            <p>{table.title_en}</p>
            {parentHierarchy.length > 0 ? (
              <div className="hierarchy-trail hierarchy-trail--detail">
                {parentHierarchy.map((item) => (
                  <span key={`${item.code}-${item.title}`}>
                    {item.code ? <em>{item.code}</em> : null}
                    <strong>{item.title}</strong>
                  </span>
                ))}
              </div>
            ) : null}
          </div>
        </div>
        <StatusBadge status={table.status} label={table.status_label} />
        <button className="secondary-button detail-download" type="button">
          <Download aria-hidden="true" size={16} />
          <span>다운로드</span>
        </button>
      </header>

      <div className="detail-meta-strip">
        <span>
          <em>단위</em>
          <strong>{activePart.unit}</strong>
        </span>
        <span>
          <em>기준일</em>
          <strong>{activePart.metadata.base_date}</strong>
        </span>
        <span>
          <em>수정일</em>
          <strong>{activePart.updated_at}</strong>
        </span>
        {table.parts.length > 0 ? (
          <span>
            <em>하위 표</em>
            <strong>{table.parts.length}개</strong>
          </span>
        ) : null}
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
                  const count =
                    filter.id === "failed"
                      ? failedChecks.length
                      : filter.id === "passed"
                        ? passedChecks.length
                        : activePart.checks.length;

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
                  <div className="check-card__top">
                    <span>{selectedIssue.type}</span>
                    <strong>{selectedIssue.status}</strong>
                  </div>
                  <h3>{selectedIssue.location}</h3>
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
                    <dt>표 위치</dt>
                    <dd>{activePart.metadata.cell_range}</dd>
                  </div>
                  <div>
                    <dt>단위</dt>
                    <dd>{activePart.unit}</dd>
                  </div>
                  <div>
                    <dt>기준일</dt>
                    <dd>{activePart.metadata.base_date}</dd>
                  </div>
                  <div>
                    <dt>최종 수정 일자</dt>
                    <dd>{activePart.metadata.extracted_at}</dd>
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
