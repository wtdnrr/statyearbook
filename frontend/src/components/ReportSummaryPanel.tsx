import { Database } from "lucide-react";

import type { ReportOption, ReportSummary, TableStatus } from "../types";

type SummaryFilter = TableStatus | "all";

interface ReportSummaryPanelProps {
  summary: ReportSummary;
  datasetLabel: string;
  availableReports: ReportOption[];
  selectedReportId: string;
  activeFilter: TableStatus | "all" | "has_issues";
  onReportChange: (reportId: string) => void;
  onFilterChange: (filter: SummaryFilter) => void;
}

const summaryCards: Array<{
  filter: SummaryFilter;
  label: string;
  countKey: keyof Pick<
    ReportSummary,
    "total_tables" | "normal_count" | "needs_review_count" | "suspected_error_count"
  >;
  tone?: "normal" | "review" | "error";
}> = [
  { filter: "all", label: "전체 표", countKey: "total_tables" },
  { filter: "normal", label: "정상", countKey: "normal_count", tone: "normal" },
  { filter: "needs_review", label: "확인 필요", countKey: "needs_review_count", tone: "review" },
  { filter: "suspected_error", label: "오류 의심", countKey: "suspected_error_count", tone: "error" },
];

export function ReportSummaryPanel({
  summary,
  datasetLabel,
  availableReports,
  selectedReportId,
  activeFilter,
  onReportChange,
  onFilterChange,
}: ReportSummaryPanelProps) {
  const reportOptions =
    availableReports.length > 0
      ? availableReports
      : [
          {
            id: summary.report_id ?? 0,
            year: Number(summary.base_year) || 0,
            title: `${summary.base_year} ${datasetLabel}`,
            file_name: summary.file_name,
            imported_at: "",
            table_count: summary.total_tables,
          },
        ];

  return (
    <section className="report-summary-panel" aria-label="자료 및 검수 요약">
      <div className="dataset-selector">
        <label>
          <Database aria-hidden="true" size={16} />
          <select value={selectedReportId} onChange={(event) => onReportChange(event.target.value)}>
            {reportOptions.map((report) => (
              <option key={report.id} value={report.id}>
                {report.year} {datasetLabel} · {report.table_count}개 표
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="mini-summary-grid">
        {summaryCards.map((card) => (
          <button
            className={[
              "mini-summary-card",
              card.tone ? `mini-summary-card--${card.tone}` : "",
              activeFilter === card.filter ? "is-active" : "",
            ]
              .filter(Boolean)
              .join(" ")}
            key={card.filter}
            type="button"
            onClick={() => onFilterChange(card.filter)}
          >
            <span>{card.label}</span>
            <strong>{summary[card.countKey]}</strong>
          </button>
        ))}
      </div>

    </section>
  );
}
