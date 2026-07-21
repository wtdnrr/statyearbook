from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
from pathlib import Path

from app.core.numeric_text import parse_numeric_value
from app.db.schema import DB_PATH, connect, init_db


@dataclass(frozen=True)
class ImportedTable:
    code: str
    title: str
    matrix: list[list[str]]
    title_en: str = ""
    section_title: str = ""
    section_title_en: str = ""
    domain: str = "통계"
    unit: str = ""
    base_date: str = ""
    section_file: str = ""
    table_order: int = 0
    note: str = ""
    source: str = ""
    raw_context: str = ""
    header_count: int = 1
    footnote_matrix: list[list[str]] = field(default_factory=list)


@dataclass(frozen=True)
class ImportResult:
    report_id: int
    table_count: int
    cell_count: int


class ReportImportRepository:
    """Persist parser-neutral annual report tables in one transaction."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db_path = db_path

    def replace_report(
        self,
        *,
        source_path: Path,
        year: int,
        title: str,
        tables: list[ImportedTable],
    ) -> ImportResult:
        valid_tables = [
            table
            for table in tables
            if table.matrix and max((len(row) for row in table.matrix), default=0) >= 2
        ]
        if not valid_tables:
            raise ValueError("원본 파일에서 저장 가능한 통계표를 찾지 못했습니다.")

        imported_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        source_hash = file_digest(source_path)
        table_count = 0
        cell_count = 0

        with connect(self._db_path) as connection:
            init_db(connection)
            with connection:
                connection.execute(
                    "DELETE FROM annual_reports WHERE file_hash = ?",
                    (source_hash,),
                )
                cursor = connection.execute(
                    """
                    INSERT INTO annual_reports (
                        year, title, source_file_name, source_file_path, file_hash, imported_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (year, title, source_path.name, str(source_path), source_hash, imported_at),
                )
                report_id = int(cursor.lastrowid)

                for table in valid_tables:
                    width = max((len(row) for row in table.matrix), default=0)
                    table_cursor = connection.execute(
                        """
                        INSERT INTO stat_tables (
                            report_id, code, title, title_en, section_title, section_title_en,
                            domain, unit, base_date, section_file, table_order, cell_range,
                            note, source, extracted_at, raw_context
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            report_id,
                            table.code,
                            table.title,
                            table.title_en,
                            table.section_title or table.title,
                            table.section_title_en or table.title_en,
                            table.domain,
                            table.unit,
                            table.base_date,
                            table.section_file,
                            table.table_order,
                            excel_range(width, len(table.matrix)),
                            table.note,
                            table.source,
                            imported_at,
                            table.raw_context[:5000],
                        ),
                    )
                    table_id = int(table_cursor.lastrowid)
                    table_count += 1

                    for row_index, source_row in enumerate(table.matrix):
                        row = [*source_row, *([""] * (width - len(source_row)))]
                        footnotes = (
                            table.footnote_matrix[row_index]
                            if row_index < len(table.footnote_matrix)
                            else []
                        )
                        for col_index, value in enumerate(row):
                            footnote = footnotes[col_index] if col_index < len(footnotes) else ""
                            connection.execute(
                                """
                                INSERT INTO stat_table_cells (
                                    table_id, row_index, col_index, text_value, numeric_value,
                                    is_header, footnote_marker
                                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    table_id,
                                    row_index,
                                    col_index,
                                    value,
                                    parse_numeric_value(value),
                                    1 if row_index < table.header_count else 0,
                                    footnote,
                                ),
                            )
                            cell_count += 1

        return ImportResult(report_id=report_id, table_count=table_count, cell_count=cell_count)


def file_digest(source_path: Path) -> str:
    digest = hashlib.sha256()
    with source_path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def excel_range(width: int, height: int) -> str:
    current = width
    column = ""
    while current:
        current, remainder = divmod(current - 1, 26)
        column = chr(65 + remainder) + column
    return f"A1:{column}{height}"
