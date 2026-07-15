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
from app.ingest.anomaly import annotate_adjacent_duplicate_tables
from app.ingest.cell_text import footnote_markers_from_texts, split_cell_text
from app.ingest.table_repairs import repair_region_split_rows
from app.ingest.hwpx_importer import (
    ENGLISH_ONLY_RE,
    ENGLISH_TITLE_RE,
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
    split_bilingual,
)


@dataclass
class TablePart:
    matrix: list[list[str]]
    raw_text: str
    source_kind: str
    title: str = ""
    title_en: str = ""


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
    metadata_context: str = ""


APPENDIX_TITLE_RE = re.compile(r"^부록\s*(\d+)(?:\s*-\s*(\d+))?\s+(.+)$")
HEADER_HINTS = (
    "구분",
    "분류",
    "지역",
    "연도",
    "기관",
    "항목",
    "유형",
    "성별",
    "직급",
    "계급",
    "classification",
    "region",
    "year",
    "organization",
    "institution",
    "item",
    "type",
    "category",
)
HANGUL_RE = re.compile(r"[가-힣]")
LATIN_RE = re.compile(r"[A-Za-z]")


@dataclass(frozen=True)
class AppendixSection:
    code: str
    title: str
    title_en: str


@dataclass(frozen=True)
class OutlineTitle:
    title: str
    title_en: str


DRAFT_TITLE_TRANSLATION_OVERRIDES: dict[str, tuple[str, str]] = {
    "2-1-4-1": ("가입자 수", "Number of Subscribers"),
    "2-1-4-2": ("맞춤 안내 수준 현황", "Status of Personalized Guidance Levels"),
    "2-1-4-3": (
        "수혜적 공공서비스(혜택) 등록 현황",
        "Registration Status of Beneficial Public Services",
    ),
}


def apply_draft_title_translation(code: str, title: str, title_en: str) -> tuple[str, str]:
    override = DRAFT_TITLE_TRANSLATION_OVERRIDES.get(code)
    if not override or title_en:
        return title, title_en

    title_ko, translated_title = override
    if "(영문)" not in title:
        return title, title_en
    return title_ko, translated_title


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
                text = cell.text if offset == 0 else ""
                row.append(text)
                if cell.row_span > 1:
                    rowspans[col_index + offset] = (text, cell.row_span - 1)
            col_index += cell.col_span
        apply_pending()
        matrix.append(row)

    return drop_empty_columns(matrix)


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
    return drop_empty_columns(rows)


def extract_embedded_metadata_rows(
    matrix: list[list[str]],
) -> tuple[list[list[str]], list[str], list[str]]:
    table_rows: list[list[str]] = []
    notes: list[str] = []
    sources: list[str] = []

    for row in matrix:
        metadata_text = single_cell_metadata_text(row)
        if metadata_text and line_is_source(metadata_text):
            append_unique(sources, metadata_text)
            continue
        if metadata_text and line_is_embedded_note(metadata_text):
            append_unique(notes, metadata_text)
            continue
        table_rows.append(row)

    return table_rows, notes, sources


def single_cell_metadata_text(row: list[str]) -> str:
    non_empty_cells = [cell.strip() for cell in row if cell.strip()]
    return non_empty_cells[0] if len(non_empty_cells) == 1 else ""


def line_is_embedded_note(line: str) -> bool:
    return line_is_hash_note(line) or line.startswith("* 주") or line.startswith("*주")


def append_table_part(
    table: LogicalTable,
    matrix: list[list[str]],
    *,
    source_kind: str,
) -> None:
    raw_text = table_raw_text(matrix)
    matrix, part_title, part_title_en = extract_part_caption(matrix)
    matrix, notes, sources = extract_embedded_metadata_rows(matrix)
    for note in notes:
        append_unique(table.notes, note)
    for source in sources:
        append_unique(table.sources, source)
    if matrix:
        table.parts.append(
            TablePart(
                matrix=matrix,
                raw_text=raw_text,
                source_kind=source_kind,
                title=part_title,
                title_en=part_title_en,
            )
        )


def extract_part_caption(matrix: list[list[str]]) -> tuple[list[list[str]], str, str]:
    if not matrix:
        return matrix, "", ""

    first_row = matrix[0]
    non_empty = [cell.strip() for cell in first_row if cell.strip()]
    if not non_empty or not non_empty[0].startswith("▫"):
        return matrix, "", ""

    caption_text = re.sub(r"^▫\s*", "", non_empty[0]).strip()
    caption_text = re.sub(r"\(\s*단위\s*:.*$", "", caption_text).strip()
    title, title_en = split_bilingual_loose(caption_text)
    remaining = drop_empty_columns(matrix[1:])
    return remaining, title or caption_text, title_en


def split_bilingual_loose(text: str) -> tuple[str, str]:
    cleaned = re.sub(r"\s+", " ", text).replace("\\~", "~").replace("᭼", "･").strip(" :;?")
    if not cleaned:
        return "", ""

    match = ENGLISH_TITLE_RE.search(cleaned)
    if match and match.start() > 0:
        return cleaned[: match.start()].strip(), match.group(1).strip()
    if ENGLISH_ONLY_RE.fullmatch(cleaned):
        return "", cleaned
    return cleaned, ""


def drop_empty_columns(rows: list[list[str]]) -> list[list[str]]:
    non_empty_rows = [row for row in rows if any(cell for cell in row)]
    max_cols = max((len(row) for row in non_empty_rows), default=0)
    if not max_cols:
        return []

    padded_rows = [row + [""] * (max_cols - len(row)) for row in non_empty_rows]
    kept_col_indexes = [
        col_index
        for col_index in range(max_cols)
        if any(row[col_index].strip() for row in padded_rows)
    ]
    return [[row[col_index] for col_index in kept_col_indexes] for row in padded_rows]


def drop_empty_columns_with_footnotes(
    rows: list[list[str]],
    footnote_rows: list[list[str]],
) -> tuple[list[list[str]], list[list[str]]]:
    max_cols = max((len(row) for row in rows), default=0)
    if not rows or not max_cols:
        return [], []

    padded_rows = [row + [""] * (max_cols - len(row)) for row in rows]
    padded_footnotes = [row + [""] * (max_cols - len(row)) for row in footnote_rows]
    kept_col_indexes = [
        col_index
        for col_index in range(max_cols)
        if any(row[col_index].strip() for row in padded_rows)
    ]
    return (
        [[row[col_index] for col_index in kept_col_indexes] for row in padded_rows],
        [[row[col_index] for col_index in kept_col_indexes] for row in padded_footnotes],
    )


def has_hangul(value: str) -> bool:
    return bool(HANGUL_RE.search(value))


def has_latin(value: str) -> bool:
    return bool(LATIN_RE.search(value))


def is_english_label_cell(value: str) -> bool:
    stripped = value.strip()
    return bool(stripped) and has_latin(stripped) and not has_hangul(stripped) and numeric_value(stripped) is None


def row_has_numeric_values(row: list[str], *, start_col: int = 2) -> bool:
    return any(numeric_value(cell) is not None for cell in row[start_col:])


def should_merge_split_bilingual_label_column(rows: list[list[str]]) -> bool:
    if len(rows) < 4:
        return False

    max_cols = max((len(row) for row in rows), default=0)
    if max_cols < 4:
        return False

    first_row = rows[0] + [""] * (max_cols - len(rows[0]))
    if not first_row[0].strip() or first_row[1].strip():
        return False

    non_empty_second_col = 0
    english_second_col = 0
    bilingual_pairs = 0
    numeric_rows = 0

    for source_row in rows[1:]:
        row = source_row + [""] * (max_cols - len(source_row))
        first_value = row[0].strip()
        second_value = row[1].strip()

        if row_has_numeric_values(row, start_col=2):
            numeric_rows += 1
        if not second_value:
            continue

        non_empty_second_col += 1
        if is_english_label_cell(second_value):
            english_second_col += 1
        if first_value and is_english_label_cell(second_value):
            bilingual_pairs += 1

    if non_empty_second_col == 0:
        return False

    english_ratio = english_second_col / non_empty_second_col
    return bilingual_pairs >= 3 and numeric_rows >= 3 and english_ratio >= 0.7


def merge_cell_text(first_value: str, second_value: str) -> str:
    values = [value.strip() for value in (first_value, second_value) if value.strip()]
    return "\n".join(values)


def merge_footnote_markers(first_value: str, second_value: str) -> str:
    markers: list[str] = []
    for value in (first_value, second_value):
        stripped = value.strip()
        if stripped and stripped not in markers:
            markers.append(stripped)
    return " ".join(markers)


def merge_split_bilingual_label_column(
    rows: list[list[str]],
    footnote_rows: list[list[str]] | None = None,
) -> tuple[list[list[str]], list[list[str]]]:
    if not rows:
        return [], []

    max_cols = max((len(row) for row in rows), default=0)
    normalized_rows = [row + [""] * (max_cols - len(row)) for row in rows]
    normalized_footnotes = (
        [row + [""] * (max_cols - len(row)) for row in footnote_rows]
        if footnote_rows is not None
        else [[""] * max_cols for _ in normalized_rows]
    )

    if not should_merge_split_bilingual_label_column(normalized_rows):
        return normalized_rows, normalized_footnotes

    merged_rows: list[list[str]] = []
    merged_footnotes: list[list[str]] = []
    for row, footnotes in zip(normalized_rows, normalized_footnotes):
        merged_rows.append([merge_cell_text(row[0], row[1]), *row[2:]])
        merged_footnotes.append([merge_footnote_markers(footnotes[0], footnotes[1]), *footnotes[2:]])

    return merged_rows, merged_footnotes


def normalize_text_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip()).strip()


def line_is_title_candidate(line: str) -> bool:
    if line.startswith("|") or line.startswith("<"):
        return False
    if TABLE_CODE_RE.search(line) or APPENDIX_TITLE_RE.match(line):
        return bool(re.search(r"[가-힣A-Za-z]", line))
    return False


def line_is_source(line: str) -> bool:
    return line.startswith("*") or "주무관" in line or "사무관" in line or "전문관" in line


def line_is_note(line: str) -> bool:
    return line_is_hash_note(line) or line.startswith("※") or line.startswith("- ") or "출처" in line


def line_is_hash_note(line: str) -> bool:
    if re.match(r"^#{2,}\s*", line):
        return False
    return bool(re.match(r"^#\s*(?:주|\d+\)|[A-Za-z])", line))


def parse_appendix_title_block(
    text: str,
    appendix_section: AppendixSection | None,
) -> tuple[str, str, str, str, str, AppendixSection | None] | None:
    match = APPENDIX_TITLE_RE.match(text)
    if not match:
        return None

    appendix_number, appendix_sub_number, title_text = match.groups()
    title_ko, title_en = split_bilingual(title_text)
    code = f"부록 {appendix_number}-{appendix_sub_number}" if appendix_sub_number else f"부록 {appendix_number}"

    section_title = ""
    section_title_en = ""
    next_section = appendix_section
    if appendix_sub_number:
        if appendix_section and appendix_section.code == f"부록 {appendix_number}":
            section_title = appendix_section.title
            section_title_en = appendix_section.title_en
    else:
        next_section = AppendixSection(code=code, title=title_ko or code, title_en=title_en)

    return code, title_ko or code, title_en, section_title, section_title_en, next_section


def parse_markdown_title_block(
    line: str,
    appendix_section: AppendixSection | None,
) -> tuple[str, str, str, str, str, AppendixSection | None] | None:
    appendix_title = parse_appendix_title_block(line, appendix_section)
    if appendix_title:
        return appendix_title

    parsed_title = parse_title_block(line)
    if parsed_title:
        return (*parsed_title, None)
    return None


def parent_code_candidates(code: str) -> list[str]:
    if code.startswith("부록 "):
        appendix_match = re.match(r"^(부록\s+\d+)-\d+$", code)
        return [appendix_match.group(1)] if appendix_match else []

    parts = code.split("-")
    return ["-".join(parts[:length]) for length in range(len(parts) - 1, 0, -1)]


def nearest_parent_title(
    code: str,
    outline: dict[str, OutlineTitle],
) -> tuple[str, str, str] | None:
    for parent_code in parent_code_candidates(code):
        parent = outline.get(parent_code)
        if parent:
            return parent_code, parent.title, parent.title_en
    return None


def parse_markdown(source_path: Path) -> list[LogicalTable]:
    content = source_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    tables: list[LogicalTable] = []
    current: LogicalTable | None = None
    appendix_section: AppendixSection | None = None
    outline: dict[str, OutlineTitle] = {}
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
            if current and is_data_table(matrix) and not is_publication_info_table(matrix):
                for part_matrix in split_embedded_caption_sections(matrix):
                    append_table_part(current, part_matrix, source_kind="html")
            index += 1
            continue

        if raw_line.strip().startswith("|"):
            block = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                block.append(lines[index])
                index += 1
            matrix = markdown_table_matrix(block)
            if current and is_data_table(matrix) and not is_publication_info_table(matrix):
                for part_matrix in split_embedded_caption_sections(matrix):
                    append_table_part(current, part_matrix, source_kind="markdown")
            continue

        if line_is_title_candidate(line):
            parsed_title = parse_markdown_title_block(line, appendix_section)
            if parsed_title:
                code, title, title_en, section_title, section_title_en, next_appendix_section = parsed_title
                title, title_en = apply_draft_title_translation(code, title, title_en)
                if next_appendix_section is not None:
                    appendix_section = next_appendix_section
                parent_title = nearest_parent_title(code, outline)
                if parent_title and not section_title:
                    _, section_title, section_title_en = parent_title
                outline[code] = OutlineTitle(title=title, title_en=title_en)
                current = LogicalTable(
                    code=code,
                    title=title,
                    title_en=title_en,
                    section_title=section_title,
                    section_title_en=section_title_en,
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


def split_embedded_caption_sections(matrix: list[list[str]]) -> list[list[list[str]]]:
    sections: list[list[list[str]]] = []
    current: list[list[str]] = []

    for row in matrix:
        if row_is_part_caption(row) and any(row_has_content(source_row) for source_row in current):
            sections.append(trim_empty_edge_rows(current))
            current = [row]
            continue
        current.append(row)

    if any(row_has_content(row) for row in current):
        sections.append(trim_empty_edge_rows(current))

    return [section for section in sections if section]


def row_has_content(row: list[str]) -> bool:
    return any(cell.strip() for cell in row)


def row_is_part_caption(row: list[str]) -> bool:
    non_empty_cells = [cell.strip() for cell in row if cell.strip()]
    return len(non_empty_cells) == 1 and non_empty_cells[0].startswith("▫")


def trim_empty_edge_rows(rows: list[list[str]]) -> list[list[str]]:
    start = 0
    end = len(rows)
    while start < end and not row_has_content(rows[start]):
        start += 1
    while end > start and not row_has_content(rows[end - 1]):
        end -= 1
    return rows[start:end]


def is_publication_info_table(matrix: list[list[str]]) -> bool:
    raw_text = table_raw_text(matrix)
    normalized = compact_signature_cell(raw_text)
    return (
        "통계연보" in raw_text
        and ("발행처" in raw_text or "publishedby" in normalized)
        and ("편집" in raw_text or "editedby" in normalized)
    )


def normalize_matrix_with_footnotes(
    parts: Iterable[TablePart],
    *,
    footnote_markers: Iterable[str] = (),
) -> tuple[list[list[str]], list[list[str]]]:
    rows: list[list[str]] = []
    footnote_rows: list[list[str]] = []
    max_cols = 0
    header_signatures: set[tuple[str, ...]] = set()
    seen_data_signatures: set[tuple[str, ...]] = set()
    data_started = False
    known_markers = set(footnote_markers)

    for part in parts:
        for source_row in part.matrix:
            parsed_cells = [split_cell_text(cell, known_markers) for cell in source_row]
            cleaned_row = [text for text, _ in parsed_cells]
            footnote_row = [marker for _, marker in parsed_cells]
            if not any(cleaned_row) or is_metadata_row(cleaned_row):
                continue

            row_signature = tuple(cleaned_row)
            if data_started and row_signature in header_signatures:
                continue
            if data_started and row_signature in seen_data_signatures:
                continue

            has_numeric_data = any(numeric_value(cell) is not None for cell in cleaned_row[1:])
            if not data_started:
                header_signatures.add(row_signature)
            if has_numeric_data:
                data_started = True
            if data_started:
                seen_data_signatures.add(row_signature)

            max_cols = max(max_cols, len(cleaned_row))
            rows.append(cleaned_row)
            footnote_rows.append(footnote_row)

    padded_rows = [row + [""] * (max_cols - len(row)) for row in rows]
    padded_footnote_rows = [row + [""] * (max_cols - len(row)) for row in footnote_rows]
    normalized_rows, normalized_footnotes = drop_empty_columns_with_footnotes(
        padded_rows,
        padded_footnote_rows,
    )
    repaired_rows, repaired_footnotes = merge_split_bilingual_label_column(normalized_rows, normalized_footnotes)
    repair_region_split_rows(repaired_rows, repaired_footnotes)
    return repaired_rows, repaired_footnotes


def normalize_matrix(parts: Iterable[TablePart]) -> list[list[str]]:
    matrix, _ = normalize_matrix_with_footnotes(parts)
    return matrix


def normalized_part_rows(part: TablePart) -> list[list[str]]:
    rows: list[list[str]] = []
    max_cols = 0

    for source_row in part.matrix:
        cleaned_row = [split_cell_text(cell)[0] for cell in source_row]
        if not any(cleaned_row) or is_metadata_row(cleaned_row):
            continue
        max_cols = max(max_cols, len(cleaned_row))
        rows.append(cleaned_row)

    normalized_rows = drop_empty_columns([row + [""] * (max_cols - len(row)) for row in rows])
    merged_rows, _ = merge_split_bilingual_label_column(normalized_rows)
    return merged_rows


def compact_signature_cell(value: str) -> str:
    without_count = re.sub(r"\(\s*\d+(?:[.,]\d+)?\s*[가-힣A-Za-z%]*\s*\)", "", value)
    return re.sub(r"\s+", "", without_count).lower()


def row_looks_like_header(row: list[str]) -> bool:
    joined = " ".join(row).lower()
    normalized = compact_signature_cell(joined)
    if any(hint in normalized for hint in HEADER_HINTS):
        return True

    non_empty = [cell for cell in row if cell.strip()]
    if not non_empty:
        return False

    numeric_cells = sum(1 for cell in non_empty if numeric_value(cell) is not None)
    return numeric_cells == 0


def part_structure_signature(part: TablePart) -> tuple[str, bool]:
    rows = normalized_part_rows(part)
    return structure_signature_from_rows(rows)


def part_continuation_signature(part: TablePart) -> tuple[str, bool]:
    rows = normalized_part_rows(part)
    if len(rows) > 1 and row_is_continuation_caption(rows[0]):
        return structure_signature_from_rows(rows[1:])
    return structure_signature_from_rows(rows)


def structure_signature_from_rows(rows: list[list[str]]) -> tuple[str, bool]:
    if not rows:
        return "", False

    max_cols = max((len(row) for row in rows), default=0)
    has_header = row_looks_like_header(rows[0])
    if not has_header:
        return f"cols={max_cols};data_continuation", False

    header_count = max(guess_header_count(rows), 1)
    header_rows = rows[: min(header_count, 5)]
    signature_rows = [
        "|".join(compact_signature_cell(cell) for cell in row)
        for row in header_rows
    ]
    return f"cols={max_cols};" + "||".join(signature_rows), True


def row_is_continuation_caption(row: list[str]) -> bool:
    non_empty_cells = [cell.strip() for cell in row if cell.strip()]
    if len(non_empty_cells) != 1:
        return False
    return non_empty_cells[0].startswith("※")


def signature_column_count(signature: str) -> int | None:
    match = re.match(r"cols=(\d+);", signature)
    return int(match.group(1)) if match else None


def split_table_by_part_structure(table: LogicalTable) -> list[LogicalTable]:
    shared_metadata_context = table.metadata_context or table_parts_raw_text(table.parts)
    if not table.metadata_context:
        table.metadata_context = shared_metadata_context

    if len(table.parts) <= 1:
        return [table]

    groups: list[tuple[str, str, list[TablePart]]] = []
    for part in table.parts:
        signature, has_header = part_structure_signature(part)
        continuation_signature, _ = part_continuation_signature(part)
        if not groups:
            groups.append((signature, continuation_signature, [part]))
            continue

        last_signature, last_continuation_signature, last_parts = groups[-1]
        if not has_header and signature_column_count(signature) == signature_column_count(last_signature):
            last_parts.append(part)
            continue

        if signature == last_signature or signature == last_continuation_signature:
            last_parts.append(part)
            continue

        groups.append((signature, continuation_signature, [part]))

    if len(groups) <= 1:
        return [table]

    split_tables: list[LogicalTable] = []
    for group_index, (_, _, parts) in enumerate(groups, start=1):
        split_tables.append(
            LogicalTable(
                code=f"{table.code} 표{group_index}",
                title=table.title,
                title_en=table.title_en,
                section_title=table.section_title,
                section_title_en=table.section_title_en,
                table_order=table.table_order,
                parts=parts,
                notes=list(table.notes),
                sources=list(table.sources),
                metadata_context=shared_metadata_context,
            )
        )
    return split_tables


def table_parts_raw_text(parts: Iterable[TablePart]) -> str:
    return " ".join(part.raw_text for part in parts)


def code_without_part_suffix(code: str) -> str:
    return re.sub(r"\s+표\s*\d+$", "", code).strip()


def immediate_parent_code(code: str) -> str:
    base_code = code_without_part_suffix(code)
    if base_code.startswith("부록 "):
        match = re.match(r"^(부록\s+\d+)-\d+$", base_code)
        return match.group(1) if match else ""

    parts = base_code.split("-")
    return "-".join(parts[:-1]) if len(parts) > 1 else ""


def metadata_unit_key(unit: str) -> str:
    return re.sub(r"\s+", "", unit).strip()


def split_tables_by_part_structure(tables: Iterable[LogicalTable]) -> list[LogicalTable]:
    split_tables: list[LogicalTable] = []
    for table in tables:
        split_tables.extend(split_table_by_part_structure(table))

    for table_order, table in enumerate(split_tables, start=1):
        table.table_order = table_order

    return split_tables


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
    prepared_tables = annotate_adjacent_duplicate_tables(split_tables_by_part_structure(parsed_tables))
    inherited_units: dict[str, set[str]] = {}
    inherited_base_dates: dict[tuple[str, str], str] = {}
    for table in prepared_tables:
        matrix, footnote_matrix = normalize_matrix_with_footnotes(
            table.parts,
            footnote_markers=footnote_markers_from_texts(table.notes),
        )
        if not matrix or max((len(row) for row in matrix), default=0) < 2:
            continue

        raw_text = table_parts_raw_text(table.parts)
        metadata_text = table.metadata_context or raw_text
        unit, base_date = extract_unit_and_base_date(raw_text)
        shared_unit, shared_base_date = extract_unit_and_base_date(metadata_text)
        unit = unit or shared_unit
        parent_code = immediate_parent_code(table.code)
        if not unit and parent_code:
            parent_units = inherited_units.get(parent_code, set())
            if len(parent_units) == 1:
                unit = next(iter(parent_units))
        base_date = base_date or shared_base_date
        unit_key = metadata_unit_key(unit)
        if not base_date and parent_code and unit_key:
            base_date = inherited_base_dates.get((parent_code, unit_key), "")
        if unit and parent_code:
            inherited_units.setdefault(parent_code, set()).add(unit)
        if base_date and parent_code and unit_key:
            inherited_base_dates[(parent_code, unit_key)] = base_date
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

    return inserted_tables, inserted_cells


def import_markdown(
    source_path: Path,
    *,
    db_path: Path = DB_PATH,
    year: int = 2025,
    title: str | None = None,
    run_validation: bool = True,
    limit: int | None = None,
) -> dict[str, int | str]:
    source_path = source_path.expanduser().resolve()
    imported_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    parsed_tables = parse_markdown(source_path)
    if limit is not None:
        parsed_tables = parsed_tables[: max(limit, 0)]
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
    parser.add_argument("--limit", type=int, default=None, help="Import only the first N logical statistics")
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
        limit=args.limit,
    )
    print(
        "Imported {tables} tables and {cells} cells into {db_path}; validation issues: {validation_issues}".format(
            **result
        )
    )


if __name__ == "__main__":
    main()
