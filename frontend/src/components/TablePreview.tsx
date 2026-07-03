import {
  ArrowRight,
  ClipboardCheck,
  Download,
  FileText,
  Info,
  Sparkles,
} from "lucide-react";

import type { StatTable } from "../types";
import { resolveIssueLocation } from "../utils/validationLocation";
import { DataGrid } from "./DataGrid";
import { StatusBadge } from "./StatusBadge";

interface TablePreviewProps {
  table: StatTable;
  onOpen: () => void;
}

export function TablePreview({ table, onOpen }: TablePreviewProps) {
  const activeIssues = table.checks.filter((check) => check.status !== "정상");
  const parentHierarchy = table.hierarchy.slice(0, -1);

  return (
    <section className="preview-panel" aria-label="선택 표 요약">
      <div className={`publication-head publication-head--${table.theme}`}>
        <div className="publication-head__body">
          <div className="publication-head__meta">
            <span className="publication-head__code">{table.code}</span>
            {parentHierarchy.map((item) => (
              <span className="publication-head__crumb" key={`${item.code}-${item.title}`}>
                {item.code ? <em>{item.code}</em> : null}
                <strong>{item.title}</strong>
              </span>
            ))}
          </div>
          <h2>{table.title}</h2>
          <p>{table.title_en}</p>
        </div>
        <StatusBadge status={table.status} label={table.status_label} />
      </div>

      <div className="meta-row">
        <span className="meta-chip">
          <em>분야</em>
          <strong>{table.domain}</strong>
        </span>
        <span className="meta-chip">
          <em>기준일</em>
          <strong>{table.metadata.base_date}</strong>
        </span>
        <span className="meta-chip">
          <em>단위</em>
          <strong>{table.unit}</strong>
        </span>
        {table.parts.length > 0 ? (
          <span className="meta-chip">
            <em>하위 표</em>
            <strong>{table.parts.length}개</strong>
          </span>
        ) : null}
      </div>

      <section className="summary-block">
        <div className="section-label">
          <Sparkles aria-hidden="true" size={16} />
          <span>자동 요약</span>
        </div>
        <ul>
          {table.summary.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      </section>

      <section className="quick-check">
        <div className="section-label">
          <ClipboardCheck aria-hidden="true" size={16} />
          <span>검수 결과</span>
        </div>
        {activeIssues.length > 0 ? (
          <div className="review-list">
            {activeIssues.map((issue) => {
              const issueLocation = resolveIssueLocation(table, issue);

              return (
                <article className="review-item" key={issue.id}>
                  <div className="review-heading">
                    <span className="review-type">{issue.type}</span>
                    <div className="review-location">
                      <span>
                        <em>행</em>
                        {issueLocation.row}
                      </span>
                      <span>
                        <em>열</em>
                        {issueLocation.column}
                      </span>
                    </div>
                  </div>
                  <dl className="review-values">
                    <div>
                      <dt>현재값</dt>
                      <dd>{issue.current_value}</dd>
                    </div>
                    {issue.expected_value ? (
                      <div>
                        <dt>검수값</dt>
                        <dd>{issue.expected_value}</dd>
                      </div>
                    ) : null}
                    {issue.difference ? (
                      <div>
                        <dt>차이</dt>
                        <dd>{issue.difference}</dd>
                      </div>
                    ) : null}
                  </dl>
                </article>
              );
            })}
          </div>
        ) : (
          <p className="empty-copy">오류 의심 항목이 없습니다.</p>
        )}
      </section>

      <section className="preview-grid-block">
        <div className="section-label">
          <FileText aria-hidden="true" size={16} />
          <span>원본 표 미리보기</span>
        </div>
        <DataGrid columns={table.columns} rows={table.rows} theme={table.theme} maxRows={6} />
      </section>

      <div className="preview-actions">
        <button className="secondary-button" type="button">
          <Download aria-hidden="true" size={16} />
          <span>다운로드</span>
        </button>
        <button className="secondary-button" type="button">
          <Info aria-hidden="true" size={16} />
          <span>메타정보</span>
        </button>
        <button className="primary-button" type="button" onClick={onOpen}>
          <span>상세 보기</span>
          <ArrowRight aria-hidden="true" size={16} />
        </button>
      </div>
    </section>
  );
}
