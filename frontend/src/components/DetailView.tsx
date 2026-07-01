import { useState } from "react";
import {
  ArrowLeft,
  BarChart3,
  ClipboardCheck,
  Download,
  FileCode2,
  FileSpreadsheet,
  GitCompareArrows,
  Info,
} from "lucide-react";

import type { StatTable } from "../types";
import { DataGrid } from "./DataGrid";
import { StatusBadge } from "./StatusBadge";
import { VisualPanel } from "./VisualPanel";

type DetailTab = "checks" | "changes" | "visuals" | "metadata";

interface DetailViewProps {
  table: StatTable;
  onBack: () => void;
}

const tabs: Array<{ id: DetailTab; label: string; icon: typeof ClipboardCheck }> = [
  { id: "checks", label: "검수 결과", icon: ClipboardCheck },
  { id: "changes", label: "전년도 비교", icon: GitCompareArrows },
  { id: "visuals", label: "시각화", icon: BarChart3 },
  { id: "metadata", label: "출처·메타정보", icon: Info },
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

export function DetailView({ table, onBack }: DetailViewProps) {
  const [activeTab, setActiveTab] = useState<DetailTab>("checks");
  const [selectedIssueIndex, setSelectedIssueIndex] = useState(0);
  const [tableScrollSignal, setTableScrollSignal] = useState(0);
  const [isTableHeaderSticky, setIsTableHeaderSticky] = useState(true);
  const activeIssueIndex = Math.min(selectedIssueIndex, Math.max(table.checks.length - 1, 0));
  const selectedIssue = table.checks[activeIssueIndex];
  const selectedIssueHighlight = inferIssueHighlight(table, selectedIssue?.location);

  function selectIssue(index: number) {
    setSelectedIssueIndex(index);
    setTableScrollSignal((value) => value + 1);
  }

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
          </div>
        </div>
        <StatusBadge status={table.status} label={table.status_label} />
        <button className="secondary-button detail-download" type="button">
          <Download aria-hidden="true" size={16} />
          <span>다운로드</span>
        </button>
      </header>

      <div className="detail-meta-strip">
        <span>단위 {table.unit}</span>
        <span>기준일 {table.metadata.base_date}</span>
        <span>시트 {table.sheet_name}</span>
        <span>수정일 {table.updated_at}</span>
      </div>

      <div className="detail-workspace">
        <aside className="original-table-panel">
          <div className="detail-section detail-section--original">
            <div className="content-title-row">
              <div>
                <h2>원본 표</h2>
                <p>{table.metadata.original_file} · {table.metadata.cell_range}</p>
              </div>
              <label className="table-option-toggle">
                <input
                  checked={isTableHeaderSticky}
                  onChange={(event) => setIsTableHeaderSticky(event.target.checked)}
                  type="checkbox"
                />
                <span>헤더 고정</span>
              </label>
              <FileSpreadsheet aria-hidden="true" size={20} />
            </div>
            <DataGrid
              columns={table.columns}
              rows={table.rows}
              theme={table.theme}
              highlight={selectedIssueHighlight}
              scrollSignal={tableScrollSignal}
              stickyHeader={isTableHeaderSticky}
            />
          </div>
        </aside>

        <section className="detail-analysis-panel" aria-label="상세 분석">
          <nav className="detail-tabs" aria-label="상세 탭">
            {tabs.map((tab) => {
              const Icon = tab.icon;

              return (
                <button
                  className={activeTab === tab.id ? "is-active" : ""}
                  key={tab.id}
                  type="button"
                  onClick={() => setActiveTab(tab.id)}
                >
                  <Icon aria-hidden="true" size={16} />
                  <span>{tab.label}</span>
                </button>
              );
            })}
          </nav>

          <section className="detail-content">

            {activeTab === "checks" ? (
              <div className="detail-section split-section">
                <div>
                  <div className="content-title-row">
                    <div>
                      <h2>검수 결과</h2>
                      <p>합계, 비율, 이상치, 표기 검수</p>
                    </div>
                    <ClipboardCheck aria-hidden="true" size={20} />
                  </div>
                  <div className="issue-table">
                    {table.checks.length > 0 ? (
                      table.checks.map((issue, index) => (
                        <button
                          className={[
                            "issue-table__row",
                            `issue-table__row--${issue.severity}`,
                            activeIssueIndex === index ? "is-active" : "",
                          ]
                            .filter(Boolean)
                            .join(" ")}
                          key={issue.id}
                          type="button"
                          onClick={() => selectIssue(index)}
                        >
                          <span className="issue-row-type">{issue.type}</span>
                          <span className="issue-row-location">{issue.location}</span>
                          <span className="issue-row-value">
                            <em>현재값</em>
                            {issue.current_value}
                          </span>
                          <span className="issue-row-value">
                            <em>검수값</em>
                            {issue.expected_value ?? "-"}
                          </span>
                          <span className="issue-row-value">
                            <em>차이</em>
                            {issue.difference ?? "-"}
                          </span>
                          <strong className="issue-row-status">{issue.status}</strong>
                        </button>
                      ))
                    ) : (
                      <p className="empty-copy">현재 등록된 검수 결과가 없습니다.</p>
                    )}
                  </div>
                </div>
                <aside className="explain-panel">
                  <span className="explain-panel__kicker">
                    {table.checks.length > 0 ? `검수 항목 ${activeIssueIndex + 1}` : "검수 결과"}
                  </span>
                  <h3>{selectedIssue?.type ?? "등록된 항목 없음"}</h3>
                  <p>{selectedIssue?.detail ?? "HWPX 원문 표를 DB로 구조화한 상태입니다. 자동 검수 룰을 적용하면 이 영역에 상세 설명이 표시됩니다."}</p>
                  {selectedIssue?.formula ? (
                    <div className="formula-box">
                      <FileCode2 aria-hidden="true" size={16} />
                      <span>{selectedIssue.formula}</span>
                    </div>
                  ) : null}
                </aside>
              </div>
            ) : null}

            {activeTab === "changes" ? (
              <div className="detail-section">
                <div className="content-title-row">
                  <div>
                    <h2>전년도 비교</h2>
                    <p>신규, 삭제, 변경, 단위·주석 변동</p>
                  </div>
                  <GitCompareArrows aria-hidden="true" size={20} />
                </div>
                <div className="change-table">
                  <div className="change-table__header">
                    <span>구분</span>
                    <span>항목</span>
                    <span>전년도 파일</span>
                    <span>올해 파일</span>
                    <span>상태</span>
                  </div>
                  {table.changes.map((change) => (
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
                    <p>통계 특성에 맞춘 추천 차트</p>
                  </div>
                  <BarChart3 aria-hidden="true" size={20} />
                </div>
                <div className="visual-grid">
                  {table.visualizations.map((visualization) => (
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
                    <p>원본 파일, 시트, 셀 위치, 주석</p>
                  </div>
                  <Info aria-hidden="true" size={20} />
                </div>
                <dl className="metadata-grid">
                  <div>
                    <dt>원본 파일</dt>
                    <dd>{table.metadata.original_file}</dd>
                  </div>
                  <div>
                    <dt>시트명</dt>
                    <dd>{table.metadata.sheet_name}</dd>
                  </div>
                  <div>
                    <dt>표 위치</dt>
                    <dd>{table.metadata.cell_range}</dd>
                  </div>
                  <div>
                    <dt>단위</dt>
                    <dd>{table.unit}</dd>
                  </div>
                  <div>
                    <dt>기준일</dt>
                    <dd>{table.metadata.base_date}</dd>
                  </div>
                  <div>
                    <dt>추출 일자</dt>
                    <dd>{table.metadata.extracted_at}</dd>
                  </div>
                  <div className="metadata-grid__wide">
                    <dt>출처</dt>
                    <dd>{table.metadata.source}</dd>
                  </div>
                  <div className="metadata-grid__wide">
                    <dt>주석</dt>
                    <dd>{table.metadata.note}</dd>
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
