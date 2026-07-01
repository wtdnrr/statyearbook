import { AlertTriangle, ClipboardCheck, FileSpreadsheet, Newspaper } from "lucide-react";

import type { PressInsight, ReportSummary } from "../types";
import { issueCountLabel } from "../utils/formatters";

interface OverviewHeaderProps {
  summary: ReportSummary;
  pressInsights: PressInsight[];
  onInsightSelect: (tableId: string) => void;
}

export function OverviewHeader({ summary, pressInsights, onInsightSelect }: OverviewHeaderProps) {
  return (
    <header className="overview-header">
      <div className="overview-title">
        <div className="file-chip">
          <FileSpreadsheet aria-hidden="true" size={18} />
          <span>{summary.file_name}</span>
        </div>
        <h1>통계연보 검수 보조</h1>
        <p>기준연도 {summary.base_year} · 자동 구조화 데이터</p>
      </div>

      <section className="summary-strip" aria-label="전체 검수 요약">
        <div className="summary-tile">
          <span>전체 표</span>
          <strong>{summary.total_tables}</strong>
        </div>
        <div className="summary-tile summary-tile--normal">
          <span>정상</span>
          <strong>{summary.normal_count}</strong>
        </div>
        <div className="summary-tile summary-tile--review">
          <span>확인 필요</span>
          <strong>{summary.needs_review_count}</strong>
        </div>
        <div className="summary-tile summary-tile--error">
          <span>오류 의심</span>
          <strong>{summary.suspected_error_count}</strong>
        </div>
      </section>

      <div className="risk-line">
        <ClipboardCheck aria-hidden="true" size={17} />
        <span>{issueCountLabel(summary.issue_counts)}</span>
      </div>

      <section className="press-strip" aria-label="보도자료 후보">
        <div className="press-strip__title">
          <Newspaper aria-hidden="true" size={17} />
          <span>보도자료 후보</span>
        </div>
        <div className="press-items">
          {pressInsights.map((insight) => (
            <button
              className={`press-item press-item--${insight.tone}`}
              key={insight.id}
              type="button"
              onClick={() => onInsightSelect(insight.table_id)}
            >
              <AlertTriangle aria-hidden="true" size={14} />
              <span>{insight.title}</span>
            </button>
          ))}
        </div>
      </section>
    </header>
  );
}
