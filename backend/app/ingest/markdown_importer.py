from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
import re
import sqlite3
from typing import Iterable

from app.db.schema import DB_PATH, connect, init_db
from app.ingest.hwpx_importer import (
    TABLE_CODE_RE,
    append_unique,
    cell_range,
    domain_from_code,
    extract_unit_and_base_date,
    file_hash,
    guess_header_count,
    is_data_table,
    is_metadata_row,
    numeric_value,
    parse_title_block,
)


@dataclass
class TablePart:
    matrix: list[list[str]]
    raw_text: str
    source_kind: str


@dataclass
class LogicalTable:
    code: str
    title: str
    title_en: str
    section_title: str
    section_title_en: str
    table_order: int
    parts: list[TablePart] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


@dataclass
class ParsedHtmlCell:
    text: str
    row_span: int = 1
    col_span: int = 1


class HtmlTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[ParsedHtmlCell]] = []
        self._current_row: list[ParsedHtmlCell] | None = None
        self._current_cell_attrs: dict[str, str] | None = None
        self._current_text: list[str] = []
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "tr":
            self._current_row = []
        elif tag in {"td", "th"} and self._current_row is not None:
            self._in_cell = True
            self._current_cell_attrs = {key.lower(): value or "" for key, value in attrs}
            self._current_text = []
        elif tag == "br" and self._in_cell:
            self._current_text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._in_cell and self._current_row is not None:
            attrs = self._current_cell_attrs or {}
            self._current_row.append(
                ParsedHtmlCell(
                    text=clean_cell_text("".join(self._current_text)),
                    row_span=parse_span(attrs.get("rowspan")),
                    col_span=parse_span(attrs.get("colspan")),
                )
            )
            self._in_cell = False
            self._current_cell_attrs = None
            self._current_text = []
        elif tag == "tr" and self._current_row is not None:
            self.rows.append(self._current_row)
            self._current_row = None

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._current_text.append(data)


def parse_span(value: str | None) -> int:
    if not value:
        return 1
    try:
        return max(int(value), 1)
    except ValueError:
        return 1


def clean_cell_text(value: str) -> str:
    lines = [" ".join(line.split()) for line in value.replace("\xa0", " ").splitlines()]
    return "\n".join(line for line in lines if line).strip()


def html_table_matrix(html: str) -> list[list[str]]:
    parser = HtmlTableParser()
    parser.feed(html)

    matrix: list[list[str]] = []
    rowspans: dict[int, tuple[str, int]] = {}

    for source_row in parser.rows:
        row: list[str] = []
        col_index = 0

        def apply_pending() -> None:
            nonlocal col_index
            while col_index in rowspans:
                text, remaining_rows = rowspans[col_index]
                row.append(text)
                if remaining_rows <= 1:
                    del rowspans[col_index]
                else:
                    rowspans[col_index] = (text, remaining_rows - 1)
                col_index += 1

        apply_pending()
        for cell in source_row:
            apply_pending()
            for offset in range(cell.col_span):
                row.append(cell.text)
                if cell.row_span > 1:
                    rowspans[col_index + offset] = (cell.text, cell.row_span - 1)
            col_index += cell.col_span
        apply_pending()
        matrix.append(row)

    max_cols = max((len(row) for row in matrix), default=0)
    return [row + [""] * (max_cols - len(row)) for row in matrix if any(cell for cell in row)]


def split_markdown_row(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]

    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for char in body:
        if char == "\\" and not escaped:
            escaped = True
            current.append(char)
            continue
        if char == "|" and not escaped:
            cells.append(clean_cell_text("".join(current).replace("<br>", "\n")))
            current = []
            continue
        current.append(char)
        escaped = False
    cells.append(clean_cell_text("".join(current).replace("<br>", "\n")))
    return cells


def is_markdown_separator(row: list[str]) -> bool:
    return bool(row) and all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in row)


def markdown_table_matrix(lines: list[str]) -> list[list[str]]:
    rows = [split_markdown_row(line) for line in lines]
    rows = [row for row in rows if not is_markdown_separator(row)]
    max_cols = max((len(row) for row in rows), default=0)
    return [row + [""] * (max_cols - len(row)) for row in rows if any(cell for cell in row)]


def normalize_text_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip()).strip()


def line_is_title_candidate(line: str) -> bool:
    if not TABLE_CODE_RE.search(line):
        return False
    if line.startswith("|") or line.startswith("<"):
        return False
    return bool(re.search(r"[가-힣A-Za-z]", line))


def line_is_source(line: str) -> bool:
    return line.startswith("*") or "주무관" in line or "사무관" in line or "전문관" in line


def line_is_note(line: str) -> bool:
    return line.startswith("#") or line.startswith("※") or line.startswith("- ") or "출처" in line


def parse_markdown(source_path: Path) -> list[LogicalTable]:
    content = source_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    tables: list[LogicalTable] = []
    current: LogicalTable | None = None
    index = 0

    while index < len(lines):
        raw_line = lines[index]
        line = normalize_text_line(raw_line)

        if raw_line.lstrip().startswith("<table"):
            block: list[str] = [raw_line]
            index += 1
            while index < len(lines):
                block.append(lines[index])
                if "</table>" in lines[index].lower():
                    break
                index += 1
            html = "\n".join(block)
            matrix = html_table_matrix(html)
            if current and is_data_table(matrix):
                current.parts.append(TablePart(matrix=matrix, raw_text=table_raw_text(matrix), source_kind="html"))
            index += 1
            continue

        if raw_line.strip().startswith("|"):
            block = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                block.append(lines[index])
                index += 1
            matrix = markdown_table_matrix(block)
            if current and is_data_table(matrix):
                current.parts.append(
                    TablePart(matrix=matrix, raw_text=table_raw_text(matrix), source_kind="markdown")
                )
            continue

        if line_is_title_candidate(line):
            parsed_title = parse_title_block(line)
            if parsed_title:
                current = LogicalTable(
                    code=parsed_title[0],
                    title=parsed_title[1],
                    title_en=parsed_title[2],
                    section_title=parsed_title[3],
                    section_title_en=parsed_title[4],
                    table_order=len(tables) + 1,
                )
                tables.append(current)
        elif current and line:
            if line_is_source(line):
                append_unique(current.sources, line)
            elif line_is_note(line):
                append_unique(current.notes, line)

        index += 1

    return [table for table in tables if table.parts]


def table_raw_text(matrix: list[list[str]]) -> str:
    return " ".join(cell for row in matrix for cell in row if cell)


def normalize_matrix(parts: Iterable[TablePart]) -> list[list[str]]:
    rows: list[list[str]] = []
    max_cols = 0
    header_signature: tuple[str, ...] | None = None
    data_started = False

    for part in parts:
        for source_row in part.matrix:
            cleaned_row = [cell.strip() for cell in source_row]
            if not any(cleaned_row) or is_metadata_row(cleaned_row):
                continue

            row_signature = tuple(cleaned_row)
            if data_started and header_signature and row_signature == header_signature:
                continue

            if header_signature is None:
                header_signature = row_signature

            if any(numeric_value(cell) is not None for cell in cleaned_row[1:]):
                data_started = True

            max_cols = max(max_cols, len(cleaned_row))
            rows.append(cleaned_row)

    return [row + [""] * (max_cols - len(row)) for row in rows]


def insert_report(
    connection: sqlite3.Connection,
    *,
    source_path: Path,
    source_hash: str,
    year: int,
    title: str,
    imported_at: str,
    parsed_tables: list[LogicalTable],
) -> tuple[int, int]:
    connection.execute("DELETE FROM annual_reports WHERE file_hash = ?", (source_hash,))
    cursor = connection.execute(
        """
        INSERT INTO annual_reports (
            year, title, source_file_name, source_file_path, file_hash, imported_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (year, title, source_path.name, str(source_path), source_hash, imported_at),
    )
    report_id = cursor.lastrowid

    inserted_tables = 0
    inserted_cells = 0
    for table in parsed_tables:
        matrix = normalize_matrix(table.parts)
        if not matrix or max((len(row) for row in matrix), default=0) < 2:
            continue

        raw_text = " ".join(part.raw_text for part in table.parts)
        unit, base_date = extract_unit_and_base_date(raw_text)
        header_count = guess_header_count(matrix)
        source = "\n".join(table.sources)
        note = "\n".join(table.notes)

        table_cursor = connection.execute(
            """
            INSERT INTO stat_tables (
                report_id, code, title, title_en, section_title, section_title_en,
                domain, unit, base_date, section_file, table_order, cell_range,
                note, source, extracted_at, raw_context
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report_id,
                table.code,
                table.title,
                table.title_en,
                table.section_title or table.title,
                table.section_title_en or table.title_en,
                domain_from_code(table.code),
                unit,
                base_date,
                "markdown",
                table.table_order,
                cell_range(matrix),
                note,
                source,
                imported_at,
                raw_text[:5000],
            ),
        )
        table_id = table_cursor.lastrowid
        inserted_tables += 1

        for row_index, row in enumerate(matrix):
            for col_index, value in enumerate(row):
                connection.execute(
                    """
                    INSERT INTO stat_table_cells (
                        table_id, row_index, col_index, text_value, numeric_value, is_header
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        table_id,
                        row_index,
                        col_index,
                        value,
                        numeric_value(value),
                        1 if row_index < header_count else 0,
                    ),
                )
                inserted_cells += 1

    return inserted_tables, inserted_cells


def import_markdown(
    source_path: Path,
    *,
    db_path: Path = DB_PATH,
    year: int = 2025,
    title: str | None = None,
    run_validation: bool = True,
) -> dict[str, int | str]:
    source_path = source_path.expanduser().resolve()
    imported_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    parsed_tables = parse_markdown(source_path)
    source_hash = file_hash(source_path)
    report_title = title or f"{year} 행정안전통계연보"

    connection = connect(db_path)
    init_db(connection)
    with connection:
        inserted_tables, inserted_cells = insert_report(
            connection,
            source_path=source_path,
            source_hash=source_hash,
            year=year,
            title=report_title,
            imported_at=imported_at,
            parsed_tables=parsed_tables,
        )
    connection.close()

    validation_issues = 0
    if run_validation:
        from app.validation.run_validations import run_validations

        validation_result = run_validations(db_path)
        validation_issues = int(validation_result["issues"])

    return {
        "db_path": str(db_path),
        "source_file": str(source_path),
        "tables": inserted_tables,
        "cells": inserted_cells,
        "validation_issues": validation_issues,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import a parsed Markdown annual statistics report into SQLite.")
    parser.add_argument("source", type=Path, help="Path to the Markdown file")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="SQLite database path")
    parser.add_argument("--year", type=int, default=2025, help="Report year")
    parser.add_argument("--title", default=None, help="Report title")
    parser.add_argument("--skip-validation", action="store_true", help="Skip rule-based validation")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    result = import_markdown(
        args.source,
        db_path=args.db,
        year=args.year,
        title=args.title,
        run_validation=not args.skip_validation,
    )
    print(
        "Imported {tables} tables and {cells} cells into {db_path}; validation issues: {validation_issues}".format(
            **result
        )
    )


if __name__ == "__main__":
    main()
