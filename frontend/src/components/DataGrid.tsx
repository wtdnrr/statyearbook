import { useEffect, useRef } from "react";

import type { ColumnDefinition } from "../types";
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
    rowText?: string;
    columnText?: string;
    targetLocations?: HighlightLocation[];
    headerLocations?: HighlightLocation[];
    relatedLocations?: HighlightLocation[];
  };
  scrollSignal?: number;
  stickyHeader?: boolean;
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

export function DataGrid({
  columns,
  rows,
  theme,
  maxRows,
  highlight,
  scrollSignal = 0,
  stickyHeader = false,
}: DataGridProps) {
  const visibleRows = maxRows ? rows.slice(0, maxRows) : rows;
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

  return (
    <div className={`data-grid data-grid--${theme} ${stickyHeader ? "data-grid--sticky-header" : ""}`}>
      <table>
        <colgroup>
          {columns.map((column) => (
            <col key={column.key} style={column.width ? { width: column.width } : undefined} />
          ))}
        </colgroup>
        <thead>
          <tr>
            {columns.map((column) => {
              const isHeaderHighlight = headerTargets.some((target) => columnMatches(column, target.columnText));
              const shouldAttachHeaderRef = isHeaderHighlight && !didAttachHighlightedHeaderRef;
              if (shouldAttachHeaderRef) {
                didAttachHighlightedHeaderRef = true;
              }

              return (
                <th
                  className={[
                    `align-${column.align}`,
                    columnTargets.some((target) => columnMatches(column, target.columnText))
                      ? "data-grid__column-highlight"
                      : "",
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
            const rowMatches = primaryTargets.some(
              (target) => !target.columnText && rowMatchesTarget(row, target.rowText),
            );
            const shouldAttachRowRef = rowMatches && !didAttachHighlightedRowRef;
            if (shouldAttachRowRef) {
              didAttachHighlightedRowRef = true;
            }

            return (
              <tr
                className={rowMatches ? "data-grid__row-highlight" : ""}
                key={`${rowIndex}-${row[columns[0].key]}`}
                ref={shouldAttachRowRef ? highlightedRowRef : undefined}
              >
                {columns.map((column, columnIndex) => {
                  const isCellHighlight = primaryTargets.some((target) => {
                    const targetRowMatches = rowMatchesTarget(row, target.rowText);
                    const targetColumnMatches = columnMatches(column, target.columnText);

                    return (
                      targetRowMatches &&
                      (targetColumnMatches || (!hasColumnHighlight && !target.columnText && columnIndex === 0))
                    );
                  });
                  const isRelatedCellHighlight = relatedTargets.some((target) => {
                    const targetRowMatches = rowMatchesTarget(row, target.rowText);
                    const targetColumnMatches = columnMatches(column, target.columnText);

                    return targetRowMatches && targetColumnMatches;
                  });
                  const footnote = row[`${column.key}_footnote`];
                  const shouldAttachCellRef = isCellHighlight && !didAttachHighlightedCellRef;
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
