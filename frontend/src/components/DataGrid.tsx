import { useEffect, useRef } from "react";

import type { ColumnDefinition, ValidationHighlightCell, ValidationHighlightRow } from "../types";
import { formatCellValue } from "../utils/formatters";

interface HighlightLocation {
  rowText?: string;
  columnText?: string;
}

interface DataGridProps {
  columns: ColumnDefinition[];
  rows: Array<Record<string, string | number>>;
  theme: "blue" | "red" | "green";
  maxRows?: number;
  highlight?: {
    tone?: "pass" | "review" | "error";
    rowText?: string;
    columnText?: string;
    highlightCells?: ValidationHighlightCell[];
    highlightRows?: ValidationHighlightRow[];
    focusCell?: ValidationHighlightCell | null;
    targetLocations?: HighlightLocation[];
    headerLocations?: HighlightLocation[];
    relatedLocations?: HighlightLocation[];
  };
  scrollSignal?: number;
  stickyHeader?: boolean;
  headerCount?: number;
}

function normalizeText(value: string | number | undefined) {
  return String(value ?? "")
    .replace(/\([^)]*\)/g, "")
    .replace(/[\s·,._-]/g, "")
    .toLowerCase();
}

function textMatches(source: string | number | undefined, target: string | undefined) {
  const normalizedSource = normalizeText(source);
  const normalizedTarget = normalizeText(target);

  return Boolean(
    normalizedSource &&
      normalizedTarget &&
      (normalizedSource.includes(normalizedTarget) || normalizedTarget.includes(normalizedSource)),
  );
}

function columnMatches(column: ColumnDefinition, target: string | undefined) {
  return textMatches([column.label, column.label_en].filter(Boolean).join(" "), target);
}

function cellKey(rowIndex: number, colIndex: number) {
  return `${rowIndex}:${colIndex}`;
}

export function DataGrid({
  columns,
  rows,
  theme,
  maxRows,
  highlight,
  scrollSignal = 0,
  stickyHeader = false,
  headerCount = 0,
}: DataGridProps) {
  const visibleRows = maxRows ? rows.slice(0, maxRows) : rows;
  const exactHighlightCells = highlight?.highlightCells ?? [];
  const exactHighlightRows = highlight?.highlightRows ?? [];
  const hasExactHighlights = exactHighlightCells.length > 0 || exactHighlightRows.length > 0;
  const exactHighlightMap = new Map(
    exactHighlightCells.map((cell) => [cellKey(cell.row_index, cell.col_index), cell.role]),
  );
  const exactRowHighlightMap = new Map(exactHighlightRows.map((row) => [row.row_index, row.role]));
  const focusCell = highlight?.focusCell ?? exactHighlightCells.find((cell) => cell.role === "target") ?? exactHighlightCells[0];
  const focusKey = focusCell ? cellKey(focusCell.row_index, focusCell.col_index) : "";
  const primaryTargets = highlight
    ? (highlight.targetLocations ?? [{ rowText: highlight.rowText, columnText: highlight.columnText }])
    : [];
  const headerTargets = highlight?.headerLocations ?? [];
  const relatedTargets = highlight?.relatedLocations ?? [];
  const highlightTargets = [...primaryTargets, ...relatedTargets];
  const columnTargets = [...highlightTargets, ...headerTargets];
  const firstColumnKey = columns[0]?.key;
  const hasColumnHighlight = columns.some((column) =>
    columnTargets.some((target) => columnMatches(column, target.columnText)),
  );
  const highlightedHeaderRef = useRef<HTMLTableCellElement | null>(null);
  const highlightedCellRef = useRef<HTMLTableCellElement | null>(null);
  const highlightedRowRef = useRef<HTMLTableRowElement | null>(null);
  let didAttachHighlightedHeaderRef = false;
  let didAttachHighlightedCellRef = false;
  let didAttachHighlightedRowRef = false;

  function rowMatchesTarget(row: Record<string, string | number>, rowText: string | undefined) {
    if (!rowText) {
      return false;
    }
    const firstColumnValue = firstColumnKey ? row[firstColumnKey] : undefined;
    if (textMatches(firstColumnValue, rowText)) {
      return true;
    }
    return Object.values(row).some((value) => textMatches(value, rowText));
  }

  useEffect(() => {
    if (scrollSignal <= 0) {
      return;
    }

    const target = highlightedCellRef.current ?? highlightedHeaderRef.current ?? highlightedRowRef.current;

    target?.scrollIntoView({
      block: "center",
      inline: "center",
      behavior: "smooth",
    });
  }, [scrollSignal]);

  const highlightTone = highlight?.tone ?? "review";

  return (
    <div
      className={[
        "data-grid",
        `data-grid--${theme}`,
        `data-grid--highlight-${highlightTone}`,
        stickyHeader ? "data-grid--sticky-header" : "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <table>
        <colgroup>
          {columns.map((column) => (
            <col key={column.key} style={column.width ? { width: column.width } : undefined} />
          ))}
        </colgroup>
        <thead>
          <tr>
            {columns.map((column, columnIndex) => {
              const exactHeaderRole = exactHighlightCells
                .filter((cell) => cell.row_index < headerCount && cell.col_index === columnIndex)
                .some((cell) => cell.role === "target")
                ? "target"
                : exactHighlightCells.some((cell) => cell.row_index < headerCount && cell.col_index === columnIndex)
                  ? "related"
                  : undefined;
              const isHeaderHighlight =
                exactHeaderRole === "target" ||
                (!hasExactHighlights && headerTargets.some((target) => columnMatches(column, target.columnText)));
              const shouldAttachHeaderRef = isHeaderHighlight && !didAttachHighlightedHeaderRef;
              if (shouldAttachHeaderRef) {
                didAttachHighlightedHeaderRef = true;
              }

              return (
                <th
                  className={[
                    `align-${column.align}`,
                    !hasExactHighlights && columnTargets.some((target) => columnMatches(column, target.columnText))
                      ? "data-grid__column-highlight"
                      : "",
                    exactHeaderRole === "related" ? "data-grid__header-related-highlight" : "",
                    isHeaderHighlight ? "data-grid__header-cell-highlight" : "",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                  key={column.key}
                  ref={shouldAttachHeaderRef ? highlightedHeaderRef : undefined}
                >
                  <span>{column.label}</span>
                  {column.label_en ? <small>{column.label_en}</small> : null}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {visibleRows.map((row, rowIndex) => {
            const sourceRowIndex =
              typeof row._row_index === "number" ? Number(row._row_index) : headerCount + rowIndex;
            const exactRowRole = exactRowHighlightMap.get(sourceRowIndex);
            const rowMatches = primaryTargets.some(
              (target) => !hasExactHighlights && !target.columnText && rowMatchesTarget(row, target.rowText),
            );
            const shouldAttachRowRef = rowMatches && !didAttachHighlightedRowRef;
            if (shouldAttachRowRef) {
              didAttachHighlightedRowRef = true;
            }

            return (
              <tr
                className={[
                  rowMatches ? "data-grid__row-highlight" : "",
                  exactRowRole === "related" ? "data-grid__row-related-highlight" : "",
                  exactRowRole === "target" ? "data-grid__row-target-highlight" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                key={`${rowIndex}-${row[columns[0].key]}`}
                ref={shouldAttachRowRef ? highlightedRowRef : undefined}
              >
                {columns.map((column, columnIndex) => {
                  const exactRole = exactHighlightMap.get(cellKey(sourceRowIndex, columnIndex));
                  const isCellHighlight = exactRole === "target" || (!hasExactHighlights && primaryTargets.some((target) => {
                    const targetRowMatches = rowMatchesTarget(row, target.rowText);
                    const targetColumnMatches = columnMatches(column, target.columnText);

                    return (
                      targetRowMatches &&
                      (targetColumnMatches || (!hasColumnHighlight && !target.columnText && columnIndex === 0))
                    );
                  }));
                  const isRelatedCellHighlight = exactRole === "related" || (!hasExactHighlights && relatedTargets.some((target) => {
                    const targetRowMatches = rowMatchesTarget(row, target.rowText);
                    const targetColumnMatches = columnMatches(column, target.columnText);

                    return targetRowMatches && targetColumnMatches;
                  }));
                  const footnote = row[`${column.key}_footnote`];
                  const shouldAttachCellRef =
                    (focusKey ? focusKey === cellKey(sourceRowIndex, columnIndex) : isCellHighlight) &&
                    !didAttachHighlightedCellRef;
                  if (shouldAttachCellRef) {
                    didAttachHighlightedCellRef = true;
                  }

                  return (
                    <td
                      className={[
                        `align-${column.align}`,
                        isRelatedCellHighlight ? "data-grid__cell-related-highlight" : "",
                        isCellHighlight ? "data-grid__cell-highlight" : "",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                      key={column.key}
                      ref={shouldAttachCellRef ? highlightedCellRef : undefined}
                    >
                      <span>
                        {formatCellValue(row[column.key] ?? "", column.label)}
                        {footnote ? <sup className="data-grid__footnote">{footnote}</sup> : null}
                      </span>
                      {row[`${column.key}_en`] ? <small>{row[`${column.key}_en`]}</small> : null}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
