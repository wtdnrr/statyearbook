import { useEffect, useRef } from "react";

import type { ColumnDefinition } from "../types";
import { formatCellValue } from "../utils/formatters";

interface DataGridProps {
  columns: ColumnDefinition[];
  rows: Array<Record<string, string | number>>;
  theme: "blue" | "red" | "green";
  maxRows?: number;
  highlight?: {
    rowText?: string;
    columnText?: string;
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
  const hasColumnHighlight = columns.some((column) => textMatches(column.label, highlight?.columnText));
  const highlightedCellRef = useRef<HTMLTableCellElement | null>(null);
  const highlightedRowRef = useRef<HTMLTableRowElement | null>(null);
  const highlightKey = `${highlight?.rowText ?? ""}:${highlight?.columnText ?? ""}`;

  useEffect(() => {
    const target = highlightedCellRef.current ?? highlightedRowRef.current;

    target?.scrollIntoView({
      block: "center",
      inline: "center",
      behavior: "smooth",
    });
  }, [highlightKey, scrollSignal]);

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
            {columns.map((column) => (
              <th
                className={[
                  `align-${column.align}`,
                  textMatches(column.label, highlight?.columnText) ? "data-grid__column-highlight" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                key={column.key}
              >
                <span>{column.label}</span>
                {column.label_en ? <small>{column.label_en}</small> : null}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {visibleRows.map((row, rowIndex) => {
            const rowMatches = Object.values(row).some((value) => textMatches(value, highlight?.rowText));

            return (
              <tr
                className={rowMatches ? "data-grid__row-highlight" : ""}
                key={`${rowIndex}-${row[columns[0].key]}`}
                ref={rowMatches ? highlightedRowRef : undefined}
              >
                {columns.map((column, columnIndex) => {
                  const columnMatches = textMatches(column.label, highlight?.columnText);
                  const isCellHighlight =
                    rowMatches && (columnMatches || (!hasColumnHighlight && columnIndex === 0));

                  return (
                    <td
                      className={[
                        `align-${column.align}`,
                        isCellHighlight ? "data-grid__cell-highlight" : "",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                      key={column.key}
                      ref={isCellHighlight ? highlightedCellRef : undefined}
                    >
                      <span>{formatCellValue(row[column.key] ?? "")}</span>
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
