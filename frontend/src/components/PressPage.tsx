import { ArrowRight, BarChart3, FileText, Newspaper } from "lucide-react";

import type { PressInsight, StatTable } from "../types";

interface PressPageProps {
  insights: PressInsight[];
  tables: StatTable[];
  onOpenTable: (tableId: string) => void;
}

export function PressPage({ insights, tables, onOpenTable }: PressPageProps) {
  const tableMap = new Map(tables.map((table) => [table.id, table]));
  const leadInsight = insights[0];
  const leadTable = leadInsight ? tableMap.get(leadInsight.table_id) : undefined;

  return (
    <main className="press-page">
      <section className="press-hero">
        <div>
          <span className="page-kicker">
            <Newspaper aria-hidden="true" size={16} />
            보도자료
          </span>
          <h1>연보 데이터에서 기사화할 변화와 검수 포인트를 정리합니다.</h1>
          <p>표별 요약, 주요 수치, 이상치 검수 결과를 바탕으로 보도자료 초안에 넣을 후보 문장을 모아봅니다.</p>
        </div>
      </section>

      <section className="press-layout">
        <div className="press-candidate-list">
          <div className="content-title-row">
            <div>
              <h2>후보 목록</h2>
              <p>자동 요약 기반 보도자료 소재</p>
            </div>
            <FileText aria-hidden="true" size={20} />
          </div>

          {insights.map((insight) => {
            const table = tableMap.get(insight.table_id);

            return (
              <article className={`press-card press-card--${insight.tone}`} key={insight.id}>
                <div>
                  <span>{table?.domain ?? "통계"} · {table?.code ?? "-"}</span>
                  <h3>{insight.title}</h3>
                  <p>{insight.body}</p>
                </div>
                <button type="button" onClick={() => onOpenTable(insight.table_id)}>
                  <span>표 보기</span>
                  <ArrowRight aria-hidden="true" size={15} />
                </button>
              </article>
            );
          })}
        </div>

        <aside className="press-draft-panel">
          <div className="content-title-row">
            <div>
              <h2>초안 미리보기</h2>
              <p>선택 후보를 기사형 문장으로 재구성</p>
            </div>
            <BarChart3 aria-hidden="true" size={20} />
          </div>

          {leadInsight && leadTable ? (
            <div className="draft-paper">
              <span>{leadTable.section_title}</span>
              <h3>{leadInsight.title}</h3>
              <p>{leadInsight.body}</p>
              <p>
                관련 표인 {leadTable.title}에 따르면 {leadTable.summary[0]} 담당자는 검수 결과의 확인 필요 항목을
                재점검한 뒤 보도자료 반영 여부를 결정할 수 있습니다.
              </p>
              <dl>
                {leadTable.key_figures.slice(0, 3).map((metric) => (
                  <div key={metric.label}>
                    <dt>{metric.label}</dt>
                    <dd>{metric.value}</dd>
                  </div>
                ))}
              </dl>
            </div>
          ) : (
            <p className="empty-copy">보도자료 후보가 없습니다.</p>
          )}
        </aside>
      </section>
    </main>
  );
}
