import { useEffect, useMemo } from "react";

import type { ReportOption, ReportSummary, StatTable, TableStatus } from "../types";
import { ReportSummaryPanel } from "./ReportSummaryPanel";
import { TableList } from "./TableList";
import { TablePreview } from "./TablePreview";

type FilterValue = TableStatus | "all" | "has_issues";

function matchesWorkspaceFilter(table: StatTable, query: string, filter: FilterValue) {
  const normalizedQuery = query.trim().toLowerCase();
  const hasQueryMatch =
    !normalizedQuery ||
    [table.title, table.title_en, table.code]
      .join(" ")
      .toLowerCase()
      .includes(normalizedQuery);
  const hasFilterMatch =
    filter === "all" ||
    table.status === filter ||
    (filter === "has_issues" && table.checks.some((check) => check.status !== "정상"));

  return hasQueryMatch && hasFilterMatch;
}

interface ReportWorkspaceProps {
  datasetLabel: string;
  summary: ReportSummary;
  availableReports: ReportOption[];
  tables: StatTable[];
  selectedTableId: string;
  selectedReportId: string;
  selectedTableDetail?: StatTable | null;
  query: string;
  filter: FilterValue;
  validationTypes: string[];
  hiddenValidationTypes: ReadonlySet<string>;
  tableListScrollTop: number;
  onReportChange: (reportId: string) => void;
  onQueryChange: (query: string) => void;
  onFilterChange: (filter: FilterValue) => void;
  onValidationTypeVisibilityChange: (type: string, visible: boolean) => void;
  onShowAllValidationTypes: () => void;
  onHideAllValidationTypes: () => void;
  onSelect: (tableId: string) => void;
  onOpen: (tableId: string) => void;
  onTableListScrollTopChange: (scrollTop: number) => void;
}

export function ReportWorkspace({
  datasetLabel,
  summary,
  availableReports,
  tables,
  selectedTableId,
  selectedReportId,
  selectedTableDetail,
  query,
  filter,
  validationTypes,
  hiddenValidationTypes,
  tableListScrollTop,
  onReportChange,
  onQueryChange,
  onFilterChange,
  onValidationTypeVisibilityChange,
  onShowAllValidationTypes,
  onHideAllValidationTypes,
  onSelect,
  onOpen,
  onTableListScrollTopChange,
}: ReportWorkspaceProps) {
  const filteredTables = useMemo(() => {
    return tables.filter((table) => matchesWorkspaceFilter(table, query, filter));
  }, [filter, query, tables]);

  const activeId = filteredTables.some((table) => table.id === selectedTableId)
    ? selectedTableId
    : filteredTables[0]?.id ?? "";
  const selectedTable =
    selectedTableDetail?.id === activeId
      ? selectedTableDetail
      : tables.find((table) => table.id === activeId);

  useEffect(() => {
    if (activeId && selectedTableId !== activeId) {
      onSelect(activeId);
    }
  }, [activeId, onSelect, selectedTableId]);

  function handleSummaryFilterChange(nextFilter: FilterValue) {
    onFilterChange(nextFilter);

    const nextTables = tables.filter((table) => matchesWorkspaceFilter(table, query, nextFilter));
    if (nextTables.length > 0 && !nextTables.some((table) => table.id === selectedTableId)) {
      onSelect(nextTables[0].id);
    }
  }

  return (
    <div className="workspace">
      <div className="workspace-sidebar">
        <ReportSummaryPanel
          summary={summary}
          datasetLabel={datasetLabel}
          availableReports={availableReports}
          selectedReportId={selectedReportId || String(summary.report_id ?? "")}
          activeFilter={filter}
          validationTypes={validationTypes}
          hiddenValidationTypes={hiddenValidationTypes}
          onReportChange={onReportChange}
          onFilterChange={handleSummaryFilterChange}
          onValidationTypeVisibilityChange={onValidationTypeVisibilityChange}
          onShowAllValidationTypes={onShowAllValidationTypes}
          onHideAllValidationTypes={onHideAllValidationTypes}
        />
        <TableList
          tables={filteredTables}
          activeTableId={activeId}
          query={query}
          filter={filter}
          scrollTop={tableListScrollTop}
          onQueryChange={onQueryChange}
          onFilterChange={onFilterChange}
          onSelect={onSelect}
          onOpen={onOpen}
          onScrollTopChange={onTableListScrollTopChange}
        />
      </div>
      {selectedTable ? <TablePreview table={selectedTable} onOpen={() => onOpen(selectedTable.id)} /> : null}
    </div>
  );
}
