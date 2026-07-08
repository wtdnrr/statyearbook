from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
from pathlib import Path
import re
import sqlite3
from typing import Iterable
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from app.db.schema import DB_PATH, connect, init_db
from app.ingest.anomaly import annotate_adjacent_duplicate_tables
from app.ingest.cell_text import split_cell_text


TABLE_CODE_RE = re.compile(r"(?<!\d)(?:[1-9]|1[0-9])-\d{1,2}-\d{1,2}(?:-\d{1,2})?(?![-\d])")
KOREAN_HEADER_LABELS = {
    "구분",
    "분류",
    "지역",
    "연도",
    "기관",
    "기관명",
    "항목",
    "유형",
    "성별",
    "직급",
    "계급",
}
ENGLISH_HEADER_LABELS = {
    "classification",
    "region",
    "year",
    "organization",
    "institution",
    "item",
    "type",
    "category",
    "sex",
    "grade",
}
HEADER_LABEL_PAIRS = (
    ("구분", "classification"),
    ("분류", "classification"),
    ("지역", "region"),
    ("연도", "year"),
    ("기관", "organization"),
    ("기관명", "institution"),
    ("항목", "item"),
    ("유형", "type"),
    ("성별", "sex"),
    ("직급", "grade"),
    ("계급", "grade"),
)

DOMAIN_BY_CHAPTER = {
    "1": "조직",
    "2": "행정관리",
    "3": "전자정부",
    "4": "지방행정",
    "5": "지방재정",
    "6": "안전",
    "7": "재난관리",
    "8": "민방위",
    "9": "비상대비",
}


@dataclass
class TablePart:
    section_file: str
    matrix: list[list[str]]
    raw_text: str


@dataclass
class LogicalTable:
    code: str
    title: str
    title_en: str
    section_title: str
    section_title_en: str
    section_file: str
    table_order: int
    parts: list[TablePart] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def element_text(element: ET.Element) -> str:
    return " ".join("".join(element.itertext()).split())


def split_bilingual(text: str) -> tuple[str, str]:
    cleaned = re.sub(r"\s+", " ", text).strip(" :;?")
    if not cleaned:
        return "", ""

    match = re.search(r"([A-Za-z][A-Za-z0-9 ,&/().%·･+\-']{2,})$", cleaned)
    if match and match.start() > 0:
        return cleaned[: match.start()].strip(), match.group(1).strip()

    if re.fullmatch(r"[A-Za-z0-9 ,&/().%·･+\-']+", cleaned):
        return "", cleaned

    return cleaned, ""


def parse_title_block(text: str) -> tuple[str, str, str, str, str] | None:
    matches = list(TABLE_CODE_RE.finditer(text))
    if not matches:
        return None

    table_match = matches[-1]
    code = table_match.group(0)
    title_ko, title_en = split_bilingual(text[table_match.end() :])

    section_ko = ""
    section_en = ""
    if len(matches) >= 2:
        section_text = text[matches[-2].end() : table_match.start()]
        section_ko, section_en = split_bilingual(section_text)

    return code, title_ko or code, title_en, section_ko, section_en


def cell_position(cell: ET.Element) -> tuple[int, int, int, int]:
    addr = next((item for item in cell.iter() if local_name(item.tag) == "cellAddr"), None)
    span = next((item for item in cell.iter() if local_name(item.tag) == "cellSpan"), None)

    row = int((addr.attrib if addr is not None else {}).get("rowAddr", "0"))
    col = int((addr.attrib if addr is not None else {}).get("colAddr", "0"))
    row_span = int((span.attrib if span is not None else {}).get("rowSpan", "1"))
    col_span = int((span.attrib if span is not None else {}).get("colSpan", "1"))
    return row, col, row_span, col_span


def table_matrix(table: ET.Element) -> list[list[str]]:
    cells: list[tuple[str, int, int, int, int]] = []
    max_row = 0
    max_col = 0

    for row in [item for item in list(table) if local_name(item.tag) == "tr"]:
        for cell in [item for item in list(row) if local_name(item.tag) == "tc"]:
            row_index, col_index, row_span, col_span = cell_position(cell)
            text = element_text(cell)
            cells.append((text, row_index, col_index, row_span, col_span))
            max_row = max(max_row, row_index + row_span)
            max_col = max(max_col, col_index + col_span)

    matrix = [["" for _ in range(max_col)] for _ in range(max_row)]
    for text, row_index, col_index, row_span, col_span in cells:
        for target_row in range(row_index, row_index + row_span):
            for target_col in range(col_index, col_index + col_span):
                if target_row < max_row and target_col < max_col and not matrix[target_row][target_col]:
                    matrix[target_row][target_col] = text if target_col == col_index else ""

    return matrix


def is_data_table(matrix: list[list[str]]) -> bool:
    row_count = len(matrix)
    col_count = max((len(row) for row in matrix), default=0)
    non_empty_count = sum(1 for row in matrix for cell in row if cell)
    return row_count >= 3 and col_count >= 2 and non_empty_count >= 6


def is_title_table(matrix: list[list[str]], text: str) -> bool:
    row_count = len(matrix)
    col_count = max((len(row) for row in matrix), default=0)
    return bool(parse_title_block(text)) and (col_count <= 2 or row_count <= 6)


def append_unique(target: list[str], value: str) -> None:
    cleaned = value.strip()
    if cleaned and cleaned not in target:
        target.append(cleaned)


def parse_hwpx(source_path: Path) -> list[LogicalTable]:
    tables: list[LogicalTable] = []
    current: LogicalTable | None = None

    with ZipFile(source_path) as archive:
        section_names = [
            name
            for name in archive.namelist()
            if re.fullmatch(r"Contents/section[1-9]\.xml", name)
        ]

        for section_name in sorted(section_names):
            table_depth = 0
            for event, element in ET.iterparse(archive.open(section_name), events=("start", "end")):
                tag_name = local_name(element.tag)

                if event == "start" and tag_name == "tbl":
                    table_depth += 1
                    continue

                if event == "end" and tag_name == "tbl":
                    if table_depth == 1:
                        matrix = table_matrix(element)
                        text = element_text(element)
                        title = parse_title_block(text)

                        if title and is_title_table(matrix, text):
                            current = LogicalTable(
                                code=title[0],
                                title=title[1],
                                title_en=title[2],
                                section_title=title[3],
                                section_title_en=title[4],
                                section_file=section_name,
                                table_order=len(tables) + 1,
                            )
                            tables.append(current)
                        elif current and is_data_table(matrix):
                            current.parts.append(
                                TablePart(
                                    section_file=section_name,
                                    matrix=matrix,
                                    raw_text=text,
                                )
                            )

                    table_depth -= 1
                    element.clear()
                    continue

                if event == "end" and tag_name == "p" and table_depth == 0:
                    text = element_text(element)
                    if current and text:
                        if text.startswith("*") or "주무관" in text or "사무관" in text:
                            append_unique(current.sources, text)
                        elif text.startswith("#") or "출처" in text:
                            append_unique(current.notes, text)
                    element.clear()

    return [table for table in tables if table.parts]


def extract_unit_and_base_date(raw_text: str) -> tuple[str, str]:
    unit = ""
    base_date = ""

    unit_match = re.search(r"단위\s*:\s*([^)（]+)", raw_text)
    if unit_match:
        unit = unit_match.group(1).strip()

    date_match = re.search(r"(\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2}\s*\.?)\s*기준", raw_text)
    if date_match:
        base_date = normalize_base_date(date_match.group(1))
    else:
        period_match = reference_period_match(raw_text)
        if period_match:
            base_date = normalize_reference_period(period_match.group(0))

    return unit, base_date


def reference_period_match(raw_text: str) -> re.Match[str] | None:
    return re.search(
        r"\[\s*제\s*\d+\s*기\s*[:：]\s*[’']?\d{2,4}\s*\.\s*\d{1,2}\s*\.?\s*[~∼-]\s*[’']?\d{2,4}\s*\.\s*\d{1,2}\s*\.?\s*\]",
        raw_text,
    )


def normalize_reference_period(value: str) -> str:
    match = re.search(
        r"제\s*(\d+)\s*기\s*[:：]\s*[’']?(\d{2,4})\s*\.\s*(\d{1,2})\s*\.?\s*[~∼-]\s*[’']?(\d{2,4})\s*\.\s*(\d{1,2})",
        value,
    )
    if not match:
        return re.sub(r"\s+", " ", value.strip("[] ")).strip()

    term, start_year, start_month, end_year, end_month = match.groups()
    return (
        f"제{int(term)}기: {normalize_year(start_year)}. {int(start_month)}. "
        f"~ {normalize_year(end_year)}. {int(end_month)}."
    )


def normalize_year(value: str) -> str:
    if len(value) == 2:
        return f"20{value}"
    return value


def normalize_base_date(value: str) -> str:
    match = re.search(r"(\d{4})\s*\.\s*(\d{1,2})\s*\.\s*(\d{1,2})", value)
    if not match:
        return re.sub(r"\s+", " ", value).strip()

    year, month, day = match.groups()
    return f"{year}. {int(month)}. {int(day)}."


def is_metadata_row(row: list[str]) -> bool:
    non_empty_cells = [cell.strip() for cell in row if cell.strip()]
    joined = " ".join(non_empty_cells)
    has_reference_period = reference_period_match(joined) is not None
    if "단위" in joined and ("기준" in joined or "As of" in joined):
        return True
    if "단위" in joined and has_reference_period and len(set(non_empty_cells)) <= 2:
        return True
    if ("기준" in joined or "As of" in joined) and len(set(non_empty_cells)) <= 2:
        return True
    if has_reference_period and len(set(non_empty_cells)) <= 2:
        return True
    return False


def normalize_matrix_with_footnotes(parts: Iterable[TablePart]) -> tuple[list[list[str]], list[list[str]]]:
    rows: list[list[str]] = []
    footnote_rows: list[list[str]] = []
    max_cols = 0
    seen_data_signatures: set[tuple[str, ...]] = set()
    data_started = False

    for part in parts:
        for row in part.matrix:
            if not any(cell.strip() for cell in row):
                continue
            if is_metadata_row(row):
                continue
            parsed_cells = [split_cell_text(cell) for cell in row]
            cleaned_row = [text for text, _ in parsed_cells]
            footnote_row = [marker for _, marker in parsed_cells]
            row_signature = tuple(cleaned_row)
            if data_started and row_signature in seen_data_signatures:
                continue
            if any(numeric_value(cell) is not None for cell in cleaned_row[1:]):
                data_started = True
            if data_started:
                seen_data_signatures.add(row_signature)
            max_cols = max(max_cols, len(cleaned_row))
            rows.append(cleaned_row)
            footnote_rows.append(footnote_row)

    return (
        [row + [""] * (max_cols - len(row)) for row in rows],
        [row + [""] * (max_cols - len(row)) for row in footnote_rows],
    )


def normalize_matrix(parts: Iterable[TablePart]) -> list[list[str]]:
    matrix, _ = normalize_matrix_with_footnotes(parts)
    return matrix


def numeric_value(text: str) -> float | None:
    cleaned = text.strip().replace(",", "").replace("%", "")
    if re.fullmatch(r"\([-+]?\d+(?:\.\d+)?\)", cleaned):
        cleaned = cleaned[1:-1]
    if not cleaned or cleaned in {"-", "－", "―"}:
        return None
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", cleaned):
        return float(cleaned)
    return None


def looks_like_data_row(row: list[str]) -> bool:
    first_cell = row[0] if row else ""
    if looks_like_header_label(first_cell):
        return False
    if re.fullmatch(r"\d{4}", re.sub(r"\s+", "", first_cell)):
        return True
    if any(numeric_value(cell) is not None for cell in row[1:]):
        return True
    return looks_like_text_data_row(row)


def looks_like_text_data_row(row: list[str]) -> bool:
    non_empty_cells = [cell.strip() for cell in row if cell.strip()]
    if len(non_empty_cells) < 2:
        return False

    if any(re.search(r"(?:'\d{2}|’\d{2}|\d{4})[.\-/년]", re.sub(r"\s+", "", cell)) for cell in non_empty_cells):
        return True
    first_cell = non_empty_cells[0]
    if re.search(r"[A-Z][a-z]{2,}\.?\s+\d{1,2}", first_cell):
        return True

    body_like_cells = [
        cell
        for cell in non_empty_cells[1:]
        if len(re.sub(r"\s+", "", cell)) >= 35
        and re.search(r"[,，;:]", cell)
    ]
    return bool(body_like_cells)


def looks_like_header_label(value: str) -> bool:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    for line in lines:
        compact_line = re.sub(r"\s+", "", line).lower()
        if compact_line in KOREAN_HEADER_LABELS or compact_line in ENGLISH_HEADER_LABELS:
            return True

    compact = re.sub(r"\s+", "", value).lower()
    return any(f"{ko}{english}" in compact for ko, english in HEADER_LABEL_PAIRS)


def guess_header_count(matrix: list[list[str]]) -> int:
    for index, row in enumerate(matrix[:8]):
        if looks_like_data_row(row):
            return max(index, 1)
    return 1 if matrix else 0


def excel_column_name(index: int) -> str:
    result = ""
    current = index + 1
    while current:
        current, remainder = divmod(current - 1, 26)
        result = chr(65 + remainder) + result
    return result


def cell_range(matrix: list[list[str]]) -> str:
    if not matrix:
        return ""
    max_cols = max((len(row) for row in matrix), default=0)
    return f"A1:{excel_column_name(max_cols - 1)}{len(matrix)}"


def file_hash(source_path: Path) -> str:
    digest = hashlib.sha256()
    with source_path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def domain_from_code(code: str) -> str:
    chapter = code.split("-", 1)[0]
    return DOMAIN_BY_CHAPTER.get(chapter, "통계")


def import_hwpx(
    source_path: Path,
    *,
    db_path: Path = DB_PATH,
    year: int = 2025,
    title: str | None = None,
    run_validation: bool = True,
) -> dict[str, int | str]:
    source_path = source_path.expanduser().resolve()
    imported_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    parsed_tables = parse_hwpx(source_path)

    connection = connect(db_path)
    init_db(connection)

    report_title = title or f"{year} 행정안전통계연보"
    source_hash = file_hash(source_path)

    with connection:
        connection.execute("DELETE FROM annual_reports WHERE file_hash = ?", (source_hash,))
        cursor = connection.execute(
            """
            INSERT INTO annual_reports (
                year, title, source_file_name, source_file_path, file_hash, imported_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (year, report_title, source_path.name, str(source_path), source_hash, imported_at),
        )
        report_id = cursor.lastrowid

        inserted_tables = 0
        inserted_cells = 0
        for table in annotate_adjacent_duplicate_tables(parsed_tables):
            matrix, footnote_matrix = normalize_matrix_with_footnotes(table.parts)
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
                    table.section_file,
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
                            table_id, row_index, col_index, text_value, numeric_value,
                            is_header, footnote_marker
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            table_id,
                            row_index,
                            col_index,
                            value,
                            numeric_value(value),
                            1 if row_index < header_count else 0,
                            footnote_matrix[row_index][col_index],
                        ),
                    )
                    inserted_cells += 1

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
    parser = argparse.ArgumentParser(description="Import an HWPX annual statistics report into SQLite.")
    parser.add_argument("source", type=Path, help="Path to the HWPX file")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="SQLite database path")
    parser.add_argument("--year", type=int, default=2025, help="Report year")
    parser.add_argument("--title", default=None, help="Report title")
    parser.add_argument("--skip-validation", action="store_true", help="Skip rule-based validation")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    result = import_hwpx(
        args.source,
        db_path=args.db,
        year=args.year,
        title=args.title,
        run_validation=not args.skip_validation,
    )
    print(
        "Imported {tables} tables and {cells} cells into {db_path}; validation issues: {validation_issues}".format(**result)
    )


if __name__ == "__main__":
    main()
