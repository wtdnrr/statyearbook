import { useState } from "react";

import { AppHeader, type AppSection } from "./components/AppHeader";
import { DetailView } from "./components/DetailView";
import { PressPage } from "./components/PressPage";
import { ReportWorkspace } from "./components/ReportWorkspace";
import { useReport } from "./hooks/useReport";
import type { TableStatus } from "./types";
import "./styles/global.css";

type FilterValue = TableStatus | "all" | "has_issues";

export default function App() {
  const report = useReport();
  const [activeSection, setActiveSection] = useState<AppSection>("annual");
  const [selectedTableId, setSelectedTableId] = useState<string>("");
  const [detailTableId, setDetailTableId] = useState<string | null>(null);
  const [selectedYear, setSelectedYear] = useState("2025");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<FilterValue>("all");

  const tables = report.data?.tables ?? [];
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

  if (report.status === "loading") {
    return <div className="state-page">데이터를 불러오는 중입니다.</div>;
  }

  if (report.status === "error") {
    return <div className="state-page state-page--error">{report.error}</div>;
  }

  if (detailTable) {
    return <DetailView table={detailTable} onBack={() => setDetailTableId(null)} />;
  }

  const reportWorkspace = (
    <ReportWorkspace
      datasetLabel={activeSection === "keyStats" ? "주요통계집" : "통계 연보"}
      summary={report.data.summary}
      tables={tables}
      selectedTableId={selectedTableId}
      selectedYear={selectedYear}
      query={query}
      filter={filter}
      onYearChange={setSelectedYear}
      onQueryChange={setQuery}
      onFilterChange={setFilter}
      onSelect={handleSelect}
      onOpen={handleOpen}
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
