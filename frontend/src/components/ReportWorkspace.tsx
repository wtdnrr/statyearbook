import { useMemo } from "react";

import type { ReportSummary, StatTable, TableStatus } from "../types";
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
  tables: StatTable[];
  selectedTableId: string;
  selectedYear: string;
  query: string;
  filter: FilterValue;
  onYearChange: (year: string) => void;
  onQueryChange: (query: string) => void;
  onFilterChange: (filter: FilterValue) => void;
  onSelect: (tableId: string) => void;
  onOpen: (tableId: string) => void;
}

export function ReportWorkspace({
  datasetLabel,
  summary,
  tables,
  selectedTableId,
  selectedYear,
  query,
  filter,
  onYearChange,
  onQueryChange,
  onFilterChange,
  onSelect,
  onOpen,
}: ReportWorkspaceProps) {
  const filteredTables = useMemo(() => {
    return tables.filter((table) => matchesWorkspaceFilter(table, query, filter));
  }, [filter, query, tables]);

  const activeId = selectedTableId || tables[0]?.id || "";
  const selectedTable = tables.find((table) => table.id === activeId) ?? tables[0];

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
          selectedYear={selectedYear}
          activeFilter={filter}
          onYearChange={onYearChange}
          onFilterChange={handleSummaryFilterChange}
        />
        <TableList
          tables={filteredTables}
          activeTableId={activeId}
          query={query}
          filter={filter}
          onQueryChange={onQueryChange}
          onFilterChange={onFilterChange}
          onSelect={onSelect}
          onOpen={onOpen}
        />
      </div>
      {selectedTable ? <TablePreview table={selectedTable} onOpen={() => onOpen(selectedTable.id)} /> : null}
    </div>
  );
}
