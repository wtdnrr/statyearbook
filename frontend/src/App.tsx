import { useMemo, useState } from "react";

import { AppHeader, type AppSection } from "./components/AppHeader";
import { DetailView } from "./components/DetailView";
import { PressPage } from "./components/PressPage";
import { ReportWorkspace } from "./components/ReportWorkspace";
import { useReport } from "./hooks/useReport";
import type { TableStatus } from "./types";
import {
  DEFAULT_HIDDEN_VALIDATION_TYPES,
  summaryWithValidationVisibility,
  tablesWithValidationVisibility,
  validationTypesForTables,
} from "./utils/validationVisibility";
import "./styles/global.css";

type FilterValue = TableStatus | "all" | "has_issues";

export default function App() {
  const [activeSection, setActiveSection] = useState<AppSection>("annual");
  const [selectedTableId, setSelectedTableId] = useState<string>("");
  const [detailTableId, setDetailTableId] = useState<string | null>(null);
  const [selectedReportId, setSelectedReportId] = useState("");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<FilterValue>("all");
  const [tableListScrollTop, setTableListScrollTop] = useState(0);
  const [hiddenValidationTypes, setHiddenValidationTypes] = useState<Set<string>>(
    () => new Set(DEFAULT_HIDDEN_VALIDATION_TYPES),
  );
  const report = useReport(selectedReportId || undefined);

  const rawTables = report.data?.tables ?? [];
  const validationTypes = useMemo(() => validationTypesForTables(rawTables), [rawTables]);
  const tables = useMemo(
    () => tablesWithValidationVisibility(rawTables, hiddenValidationTypes),
    [hiddenValidationTypes, rawTables],
  );
  const visibleSummary = useMemo(
    () =>
      report.data
        ? summaryWithValidationVisibility(report.data.summary, tables)
        : undefined,
    [report.data, tables],
  );
  const detailTable = tables.find((table) => table.id === detailTableId);

  function handleSelect(tableId: string) {
    setSelectedTableId(tableId);
  }

  function handleOpen(tableId: string) {
    setSelectedTableId(tableId);
    setDetailTableId(tableId);
  }

  function handlePressTableOpen(tableId: string) {
    setSelectedTableId(tableId);
    setActiveSection("annual");
    setDetailTableId(null);
  }

  function handleReportChange(reportId: string) {
    setSelectedReportId(reportId);
    setSelectedTableId("");
    setDetailTableId(null);
    setFilter("all");
    setQuery("");
    setTableListScrollTop(0);
  }

  function handleValidationTypeVisibility(type: string, visible: boolean) {
    setHiddenValidationTypes((current) => {
      const next = new Set(current);
      if (visible) {
        next.delete(type);
      } else {
        next.add(type);
      }
      return next;
    });
  }

  if (report.status === "loading") {
    return <div className="state-page">데이터를 불러오는 중입니다.</div>;
  }

  if (report.status === "error") {
    return <div className="state-page state-page--error">{report.error}</div>;
  }

  if (detailTable) {
    return (
      <DetailView
        table={detailTable}
        validationTypes={validationTypes}
        hiddenValidationTypes={hiddenValidationTypes}
        onValidationTypeVisibilityChange={handleValidationTypeVisibility}
        onShowAllValidationTypes={() => setHiddenValidationTypes(new Set())}
        onHideAllValidationTypes={() => setHiddenValidationTypes(new Set(validationTypes))}
        onBack={() => setDetailTableId(null)}
      />
    );
  }

  const reportWorkspace = (
    <ReportWorkspace
      datasetLabel={activeSection === "keyStats" ? "주요통계집" : "통계 연보"}
      summary={visibleSummary ?? report.data.summary}
      availableReports={report.data.available_reports}
      tables={tables}
      selectedTableId={selectedTableId}
      selectedReportId={selectedReportId}
      query={query}
      filter={filter}
      validationTypes={validationTypes}
      hiddenValidationTypes={hiddenValidationTypes}
      tableListScrollTop={tableListScrollTop}
      onReportChange={handleReportChange}
      onQueryChange={setQuery}
      onFilterChange={setFilter}
      onValidationTypeVisibilityChange={handleValidationTypeVisibility}
      onShowAllValidationTypes={() => setHiddenValidationTypes(new Set())}
      onHideAllValidationTypes={() => setHiddenValidationTypes(new Set(validationTypes))}
      onSelect={handleSelect}
      onOpen={handleOpen}
      onTableListScrollTopChange={setTableListScrollTop}
    />
  );

  return (
    <main className="app-shell">
      <AppHeader activeSection={activeSection} onSectionChange={setActiveSection} />

      {activeSection === "annual" || activeSection === "keyStats" ? reportWorkspace : null}
      {activeSection === "press" ? (
        <PressPage
          insights={report.data.press_insights}
          tables={tables}
          onOpenTable={handlePressTableOpen}
        />
      ) : null}
    </main>
  );
}
