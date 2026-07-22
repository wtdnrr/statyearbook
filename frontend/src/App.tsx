import { useMemo, useState } from "react";

import { AppHeader, type AppSection } from "./components/AppHeader";
import { DetailView } from "./components/DetailView";
import { PressPage } from "./components/PressPage";
import { ReportWorkspace } from "./components/ReportWorkspace";
import { useReport } from "./hooks/useReport";
import { fetchTable, uploadLegacyOverlay, uploadReport, waitForImport } from "./api/reportApi";
import type { StatTable, TableStatus } from "./types";
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
  const [detailTable, setDetailTable] = useState<StatTable | null>(null);
  const [detailStatus, setDetailStatus] = useState<"idle" | "loading" | "error">("idle");
  const [selectedReportId, setSelectedReportId] = useState("");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<FilterValue>("all");
  const [tableListScrollTop, setTableListScrollTop] = useState(0);
  const [hiddenValidationTypes, setHiddenValidationTypes] = useState<Set<string>>(
    () => new Set(DEFAULT_HIDDEN_VALIDATION_TYPES),
  );
  const [reportRefreshKey, setReportRefreshKey] = useState(0);
  const [uploadState, setUploadState] = useState<"idle" | "uploading" | "error">("idle");
  const [legacyUploadState, setLegacyUploadState] = useState<"idle" | "uploading" | "error">("idle");
  const report = useReport(selectedReportId || undefined, reportRefreshKey);

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
  function handleSelect(tableId: string) {
    setSelectedTableId(tableId);
  }

  async function handleOpen(tableId: string) {
    setSelectedTableId(tableId);
    setDetailTableId(tableId);
    setDetailStatus("loading");
    setDetailTable(null);
    try {
      const table = await fetchTable(tableId, selectedReportId || undefined);
      setDetailTable(table);
      setDetailStatus("idle");
    } catch (error) {
      setDetailStatus("error");
      setDetailTableId(null);
      window.alert(error instanceof Error ? error.message : "통계표 상세 데이터를 불러오지 못했습니다.");
    }
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
    setDetailTable(null);
    setDetailStatus("idle");
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

  async function handleUpload(file: File) {
    setUploadState("uploading");
    try {
      const queued = await uploadReport(file);
      const completed = await waitForImport(queued.id);
      setSelectedReportId(completed.report_id ? String(completed.report_id) : "");
      setSelectedTableId("");
      setDetailTableId(null);
      setDetailTable(null);
      setDetailStatus("idle");
      setReportRefreshKey((current) => current + 1);
      setUploadState("idle");
    } catch (error) {
      setUploadState("error");
      window.alert(error instanceof Error ? error.message : "연보 처리 중 오류가 발생했습니다.");
    }
  }

  async function handleLegacyOverlayUpload(files: File[]) {
    if (files.length !== 3) {
      window.alert("표정보, 표항목, 표데이터 .xls 파일 3개를 함께 선택해 주세요.");
      return;
    }
    const baseReport = report.data?.available_reports.find((item) => item.year === 2025);
    if (!baseReport) {
      window.alert("2026 테스트 데이터의 기준이 될 2025 연보를 먼저 업로드해 주세요.");
      return;
    }

    setLegacyUploadState("uploading");
    try {
      const queued = await uploadLegacyOverlay(files, baseReport.id);
      const completed = await waitForImport(queued.id);
      setSelectedReportId(completed.report_id ? String(completed.report_id) : "");
      setSelectedTableId("");
      setDetailTableId(null);
      setDetailTable(null);
      setDetailStatus("idle");
      setReportRefreshKey((current) => current + 1);
      setLegacyUploadState("idle");
    } catch (error) {
      setLegacyUploadState("error");
      window.alert(error instanceof Error ? error.message : "2026 테스트 데이터 처리 중 오류가 발생했습니다.");
    }
  }

  if (report.status === "loading") {
    return <div className="state-page">데이터를 불러오는 중입니다.</div>;
  }

  if (report.status === "error") {
    return <div className="state-page state-page--error">{report.error}</div>;
  }

  if (detailTableId && detailStatus === "loading") {
    return <div className="state-page">상세 데이터를 불러오는 중입니다.</div>;
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
        onBack={() => {
          setDetailTableId(null);
          setDetailTable(null);
          setDetailStatus("idle");
        }}
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
      <AppHeader
        activeSection={activeSection}
        onSectionChange={setActiveSection}
        onUpload={handleUpload}
        onLegacyOverlayUpload={handleLegacyOverlayUpload}
        uploadState={uploadState}
        legacyUploadState={legacyUploadState}
      />

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
