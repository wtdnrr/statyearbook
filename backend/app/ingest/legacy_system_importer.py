"""Import partial data exported from the existing statistics system.

The source system delivers one logical dataset in three XLS files: table
information, item definitions, and cell values.  This module joins those
files, then overlays the available values onto a selected base yearbook.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable

import xlrd

from app.db.schema import DB_PATH, connect, init_db
from app.ingest.repository import ImportedTable, ReportImportRepository


class LegacySystemImportError(ValueError):
    """Raised when the three-file source-system export is invalid."""


@dataclass(frozen=True)
class LegacyColumn:
    data_number: str
    label: str
    full_label: str
    data_type: str
    display_order: float
    source_order: int


@dataclass(frozen=True)
class LegacyRecord:
    row_number: int
    values: dict[str, str]


@dataclass(frozen=True)
class LegacySystemTable:
    system_table_id: str
    title: str
    department: str
    officer: str
    reference_period: str
    columns: tuple[LegacyColumn, ...]
    records: tuple[LegacyRecord, ...]


@dataclass(frozen=True)
class LegacyOverlayResult:
    report_id: int
    table_count: int
    cell_count: int
    base_overlay_count: int
    direct_import_count: int
    applied_cell_count: int

    def as_dict(self) -> dict[str, int]:
        return {
            "report_id": self.report_id,
            "tables": self.table_count,
            "cells": self.cell_count,
            "base_overlay_count": self.base_overlay_count,
            "direct_import_count": self.direct_import_count,
            "applied_cell_count": self.applied_cell_count,
        }


TABLE_INFO_REQUIRED = {"표id", "표이름", "자료시점"}
ITEM_REQUIRED = {"STATBL_ID", "자료번호", "항목명", "데이터유형"}
DATA_REQUIRED = {"STATBL_ID", "자료번호", "데이터행번호", "데이터값"}


def import_legacy_system_overlay(
    manifest_path: Path,
    *,
    db_path: Path = DB_PATH,
    year: int,
    title: str,
    run_validation: bool = True,
) -> dict[str, int]:
    """Create a partial test report without modifying its 2025 base report.

    ``run_validation`` is accepted to keep every importer on the same workflow
    interface. Validation is intentionally performed by the processing
    workflow after the new report is persisted.
    """

    del run_validation

    manifest = load_manifest(manifest_path)
    base_report_id = required_int(manifest.get("base_report_id"), "기준 연보")
    source_tables = parse_legacy_system_export(manifest_path)
    if not source_tables:
        raise LegacySystemImportError("세 파일에서 함께 확인되는 통계표가 없습니다.")

    base_tables = load_base_tables(base_report_id, db_path=db_path)
    if not base_tables:
        raise LegacySystemImportError("기준 연보에 저장된 통계표가 없습니다.")

    imported_tables: list[ImportedTable] = []
    used_codes: set[str] = set()
    overlay_count = 0
    direct_count = 0
    applied_cells = 0

    for table_order, source_table in enumerate(source_tables):
        base_match, confidence = find_base_table(source_table, base_tables)
        if base_match is not None and confidence >= 0.86:
            overlaid, changes, mapped_values = overlay_base_table(base_match, source_table)
            if mapped_values:
                overlaid = ensure_unique_code(
                    replace(
                        overlaid,
                        table_order=table_order,
                        raw_context=append_legacy_context(overlaid.raw_context, source_table),
                    ),
                    used_codes,
                )
                imported_tables.append(
                    overlaid
                )
                used_codes.add(overlaid.code)
                overlay_count += 1
                applied_cells += changes
                continue

        direct = ensure_unique_code(
            legacy_table_to_imported(source_table, table_order=table_order),
            used_codes,
        )
        imported_tables.append(direct)
        used_codes.add(direct.code)
        direct_count += 1

    result = ReportImportRepository(db_path).replace_report(
        source_path=manifest_path,
        source_file_name="2026_테스트_통계_현행시스템_추가데이터",
        year=year,
        title=title,
        tables=imported_tables,
        archive_previous_same_title=True,
    )
    return LegacyOverlayResult(
        report_id=result.report_id,
        table_count=result.table_count,
        cell_count=result.cell_count,
        base_overlay_count=overlay_count,
        direct_import_count=direct_count,
        applied_cell_count=applied_cells,
    ).as_dict()


def parse_legacy_system_export(manifest_path: Path) -> list[LegacySystemTable]:
    manifest = load_manifest(manifest_path)
    file_map = manifest.get("files")
    if not isinstance(file_map, dict):
        raise LegacySystemImportError("현행 시스템 테스트 업로드 정보가 올바르지 않습니다.")

    records_by_file = {
        file_name: read_xls_records(manifest_path.parent / file_name)
        for file_name in file_map.values()
        if isinstance(file_name, str)
    }
    roles = classify_legacy_records(records_by_file)
    info_by_id = {
        row["표id"]: row
        for row in roles["table_info"]
        if row.get("표id")
    }
    items_by_table = group_rows(roles["items"], "STATBL_ID")
    data_by_table = group_rows(roles["data"], "STATBL_ID")

    parsed: list[LegacySystemTable] = []
    for system_table_id, info in info_by_id.items():
        item_rows = items_by_table.get(system_table_id, [])
        data_rows = data_by_table.get(system_table_id, [])
        if not item_rows or not data_rows:
            continue

        reference_period = info.get("wrttimeIdtfrId", "")
        scoped_data = [
            row for row in data_rows
            if not reference_period or row.get("wrttimeIdtfrId") == reference_period
        ]
        if not scoped_data:
            scoped_data = data_rows

        columns = tuple(
            sorted(
                (
                    LegacyColumn(
                        data_number=row.get("자료번호", ""),
                        label=row.get("항목명", ""),
                        full_label=row.get("전체항목명", "") or row.get("항목명", ""),
                        data_type=row.get("데이터유형", ""),
                        display_order=parse_order(row.get("표시순번", "")),
                        source_order=index,
                    )
                    for index, row in enumerate(item_rows)
                    if row.get("자료번호") and row.get("항목명")
                ),
                key=lambda column: (column.display_order, column.source_order),
            )
        )
        if not columns:
            continue

        records: list[LegacyRecord] = []
        for row_number, rows in sorted(
            group_rows(scoped_data, "데이터행번호").items(),
            key=lambda item: parse_order(item[0]),
        ):
            records.append(
                LegacyRecord(
                    row_number=int(parse_order(row_number)),
                    values={row.get("자료번호", ""): row.get("데이터값", "") for row in rows},
                )
            )
        if not records:
            continue

        parsed.append(
            LegacySystemTable(
                system_table_id=system_table_id,
                title=info.get("표이름", "").strip() or system_table_id,
                department=info.get("담당부서", "").strip(),
                officer=info.get("담당자명", "").strip(),
                reference_period=info.get("자료시점", "").strip(),
                columns=columns,
                records=tuple(records),
            )
        )
    return parsed


def load_base_tables(report_id: int, *, db_path: Path) -> list[ImportedTable]:
    with connect(db_path) as connection:
        init_db(connection)
        report = connection.execute("SELECT id FROM annual_reports WHERE id = ?", (report_id,)).fetchone()
        if report is None:
            raise LegacySystemImportError("선택한 기준 연보를 찾을 수 없습니다.")
        table_rows = connection.execute(
            "SELECT * FROM stat_tables WHERE report_id = ? ORDER BY table_order, id",
            (report_id,),
        ).fetchall()
        result: list[ImportedTable] = []
        for table_row in table_rows:
            cell_rows = connection.execute(
                """
                SELECT row_index, col_index, text_value, is_header, footnote_marker
                FROM stat_table_cells
                WHERE table_id = ?
                ORDER BY row_index, col_index
                """,
                (table_row["id"],),
            ).fetchall()
            matrix, footnotes, header_count = matrix_from_rows(cell_rows)
            if not matrix:
                continue
            result.append(
                ImportedTable(
                    code=str(table_row["code"]),
                    title=str(table_row["title"]),
                    title_en=str(table_row["title_en"] or ""),
                    section_title=str(table_row["section_title"] or ""),
                    section_title_en=str(table_row["section_title_en"] or ""),
                    domain=str(table_row["domain"] or "통계"),
                    unit=str(table_row["unit"] or ""),
                    base_date=str(table_row["base_date"] or ""),
                    section_file=str(table_row["section_file"] or ""),
                    table_order=int(table_row["table_order"] or 0),
                    note=str(table_row["note"] or ""),
                    source=str(table_row["source"] or ""),
                    raw_context=str(table_row["raw_context"] or ""),
                    matrix=matrix,
                    header_count=header_count,
                    footnote_matrix=footnotes,
                )
            )
        return result


def find_base_table(
    source_table: LegacySystemTable,
    base_tables: Iterable[ImportedTable],
) -> tuple[ImportedTable | None, float]:
    source_title = normalize_title(source_table.title)
    candidates: list[tuple[float, ImportedTable]] = []
    for table in base_tables:
        candidate_title = normalize_title(table.title)
        if not candidate_title:
            continue
        score = 1.0 if candidate_title == source_title else SequenceMatcher(
            None, source_title, candidate_title
        ).ratio()
        candidates.append((score, table))
    if not candidates:
        return None, 0.0
    candidates.sort(key=lambda item: item[0], reverse=True)
    score, table = candidates[0]
    if len(candidates) > 1 and score < 0.99 and score - candidates[1][0] < 0.05:
        return None, score
    return table, score


def overlay_base_table(base: ImportedTable, source: LegacySystemTable) -> tuple[ImportedTable, int, int]:
    """Overlay compatible source values and report both changes and matches.

    A partial 2026 export can legitimately repeat a confirmed 2025 value.
    Those cells still prove that the source table has the same layout and must
    retain the richer 2025 table structure in the test report.  ``changes``
    alone cannot distinguish this case from an incompatible table, so the
    result also includes ``mapped_values``.
    """

    matrix = [row[:] for row in base.matrix]
    header_count = min(base.header_count, len(matrix))
    column_map = source_to_base_columns(source.columns, matrix, header_count)
    label_columns = [column for column in source.columns if is_text_column(column)]
    changes = 0
    mapped_values = 0

    for record in source.records:
        target_row = find_target_row(
            matrix,
            header_count,
            label_columns,
            record,
            reference_period=source.reference_period,
        )
        if target_row is None and not label_columns:
            target_row = append_reference_period_row(matrix, header_count, source.reference_period)
        if target_row is None:
            continue
        for source_column, target_col in column_map.items():
            value = record.values.get(source_column, "")
            # A blank source value means the partial export has no new value;
            # preserve the confirmed value from the 2025 base table.
            if value == "" or target_col >= len(matrix[target_row]):
                continue
            mapped_values += 1
            if matrix[target_row][target_col] != value:
                matrix[target_row][target_col] = value
                changes += 1
    return replace(base, matrix=matrix), changes, mapped_values


def source_to_base_columns(
    source_columns: Iterable[LegacyColumn],
    matrix: list[list[str]],
    header_count: int,
) -> dict[str, int]:
    width = max((len(row) for row in matrix), default=0)
    headers = {
        column: semantic_label_candidates(header_text(matrix, column, header_count))
        for column in range(width)
    }
    mapping: dict[str, int] = {}
    used_columns: set[int] = set()
    for source_column in source_columns:
        if is_text_column(source_column):
            continue
        best_score = 0.0
        best_column: int | None = None
        source_labels = semantic_label_candidates(*column_label_candidates(source_column))
        for column, header_labels in headers.items():
            if column in used_columns or not header_labels:
                continue
            score = max(
                (
                    text_similarity(source_label, header_label)
                    for source_label in source_labels
                    for header_label in header_labels
                ),
                default=0.0,
            )
            if score > best_score:
                best_score, best_column = score, column
        if best_column is not None and best_score >= 0.86:
            mapping[source_column.data_number] = best_column
            used_columns.add(best_column)
    return mapping


def find_target_row(
    matrix: list[list[str]],
    header_count: int,
    label_columns: Iterable[LegacyColumn],
    record: LegacyRecord,
    *,
    reference_period: str = "",
) -> int | None:
    labels = [record.values.get(column.data_number, "") for column in label_columns]
    labels = [label for label in labels if normalize_text(label)]
    if not labels:
        return find_reference_period_row(matrix, header_count, reference_period)

    best_score = 0.0
    best_row: int | None = None
    for row_index in range(header_count, len(matrix)):
        candidates = matrix[row_index][:4]
        score = max(
            (text_similarity(label, candidate) for label in labels for candidate in candidates),
            default=0.0,
        )
        if score > best_score:
            best_score, best_row = score, row_index
    return best_row if best_score >= 0.86 else None


def find_reference_period_row(
    matrix: list[list[str]],
    header_count: int,
    reference_period: str,
) -> int | None:
    """Locate the year row when the source system exports one latest record."""

    years = set(re.findall(r"(?:19|20)\d{2}", reference_period))
    if not years:
        return None
    for row_index in range(header_count, len(matrix)):
        row_text = " ".join(matrix[row_index][:3])
        if years.intersection(re.findall(r"(?:19|20)\d{2}", row_text)):
            return row_index
    return None


def append_reference_period_row(
    matrix: list[list[str]],
    header_count: int,
    reference_period: str,
) -> int | None:
    """Append a period row only when the yearbook explicitly has a year column."""

    years = re.findall(r"(?:19|20)\d{2}", reference_period)
    if not years:
        return None
    width = max((len(row) for row in matrix), default=0)
    if width == 0:
        return None
    period_column = next(
        (
            column
            for column in range(width)
            if re.search(r"(?:연도|년도|year)", header_text(matrix, column, header_count), flags=re.IGNORECASE)
        ),
        None,
    )
    if period_column is None:
        return None
    matrix.append(["" for _ in range(width)])
    matrix[-1][period_column] = years[0]
    return len(matrix) - 1


def legacy_table_to_imported(source: LegacySystemTable, *, table_order: int) -> ImportedTable:
    columns = list(source.columns)
    matrix = [[display_column_label(column) for column in columns]]
    for record in source.records:
        matrix.append([record.values.get(column.data_number, "") or "-" for column in columns])
    source_text = " ".join(part for part in (source.department, source.officer) if part)
    return ImportedTable(
        code=f"TEST-{source.system_table_id[-8:]}",
        title=source.title,
        section_title=source.title,
        domain="현행 시스템 추가 데이터",
        base_date=source.reference_period,
        section_file="2026 테스트 데이터",
        table_order=table_order,
        source=source_text,
        raw_context=append_legacy_context("", source),
        matrix=matrix,
        header_count=1,
    )


def ensure_unique_code(table: ImportedTable, used_codes: set[str]) -> ImportedTable:
    if table.code not in used_codes:
        return table
    suffix = 2
    while f"{table.code} 표{suffix}" in used_codes:
        suffix += 1
    return replace(table, code=f"{table.code} 표{suffix}")


def append_legacy_context(existing: str, source: LegacySystemTable) -> str:
    detail = (
        f"현행 시스템 테스트 데이터 | 표ID: {source.system_table_id} | "
        f"표명: {source.title} | 자료시점: {source.reference_period}"
    )
    return "\n".join(part for part in (existing.strip(), detail) if part)[:5000]


def display_column_label(column: LegacyColumn) -> str:
    return re.sub(r"\s*>\s*", " / ", column.full_label).strip() or column.label


def column_label_candidates(column: LegacyColumn) -> tuple[str, ...]:
    """Return both the source-system path and the displayed leaf label.

    The source export often gives an item a full path such as ``총계>조례``,
    while the yearbook header only shows ``조례``.  Treating the leaf as a
    first-class candidate prevents otherwise exact columns from being skipped.
    """

    path_parts = re.split(r"\s*>\s*", column.full_label)
    candidates = (column.full_label, column.label, path_parts[-1] if path_parts else "")
    return tuple(dict.fromkeys(value.strip() for value in candidates if value.strip()))


def semantic_label_candidates(*values: str) -> tuple[str, ...]:
    """Extract short display labels from bilingual or unit-qualified headers."""

    candidates: list[str] = []
    for value in values:
        for line in re.split(r"[\r\n]+", value):
            line = re.sub(r"\s+", " ", line).strip()
            if not line:
                continue
            candidates.append(line)
            without_unit = re.sub(r"\([^)]{1,12}\)", "", line).strip()
            if without_unit and without_unit != line:
                candidates.append(without_unit)
    return tuple(dict.fromkeys(candidates))


def matrix_from_rows(rows: Iterable[object]) -> tuple[list[list[str]], list[list[str]], int]:
    records = list(rows)
    if not records:
        return [], [], 0
    height = max(int(row["row_index"]) for row in records) + 1
    width = max(int(row["col_index"]) for row in records) + 1
    matrix = [["" for _ in range(width)] for _ in range(height)]
    footnotes = [["" for _ in range(width)] for _ in range(height)]
    header_rows: set[int] = set()
    for row in records:
        row_index = int(row["row_index"])
        column_index = int(row["col_index"])
        matrix[row_index][column_index] = str(row["text_value"] or "")
        footnotes[row_index][column_index] = str(row["footnote_marker"] or "")
        if row["is_header"]:
            header_rows.add(row_index)
    return matrix, footnotes, max(header_rows, default=-1) + 1


def read_xls_records(path: Path) -> list[dict[str, str]]:
    if path.suffix.lower() != ".xls":
        raise LegacySystemImportError("현행 시스템 테스트 업로드는 .xls 파일 3개가 필요합니다.")
    try:
        sheet = xlrd.open_workbook(path).sheet_by_index(0)
    except (OSError, xlrd.biffh.XLRDError) as error:
        raise LegacySystemImportError(f"엑셀 파일을 읽지 못했습니다: {path.name}") from error
    if sheet.nrows < 2:
        return []
    headers = [cell_text(sheet.cell_value(0, column)) for column in range(sheet.ncols)]
    return [
        {
            headers[column]: cell_text(sheet.cell_value(row, column))
            for column in range(sheet.ncols)
            if headers[column]
        }
        for row in range(1, sheet.nrows)
    ]


def classify_legacy_records(
    records_by_file: dict[str, list[dict[str, str]]],
) -> dict[str, list[dict[str, str]]]:
    classified: dict[str, list[dict[str, str]]] = {}
    for records in records_by_file.values():
        headers = set(records[0]) if records else set()
        if TABLE_INFO_REQUIRED <= headers:
            role = "table_info"
        elif ITEM_REQUIRED <= headers:
            role = "items"
        elif DATA_REQUIRED <= headers:
            role = "data"
        else:
            continue
        if role in classified:
            raise LegacySystemImportError("표정보·표항목·표데이터 파일은 각각 하나씩만 업로드해 주세요.")
        classified[role] = records
    missing = {"table_info", "items", "data"} - set(classified)
    if missing:
        labels = {"table_info": "표정보", "items": "표항목", "data": "표데이터"}
        raise LegacySystemImportError(
            "현행 시스템 데이터 파일이 부족합니다: " + ", ".join(labels[item] for item in sorted(missing))
        )
    return classified


def load_manifest(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LegacySystemImportError("현행 시스템 테스트 업로드 정보를 읽지 못했습니다.") from error
    if not isinstance(payload, dict):
        raise LegacySystemImportError("현행 시스템 테스트 업로드 정보가 올바르지 않습니다.")
    return payload


def build_manifest_payload(*, base_report_id: int, files: dict[str, Path]) -> dict[str, object]:
    return {
        "kind": "legacy_system_overlay_v3",
        "base_report_id": base_report_id,
        "files": {role: path.name for role, path in sorted(files.items())},
        "file_hashes": {role: file_digest(path) for role, path in sorted(files.items())},
    }


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def group_rows(rows: Iterable[dict[str, str]], key: str) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        value = row.get(key, "")
        if value:
            grouped.setdefault(value, []).append(row)
    return grouped


def normalize_title(value: str) -> str:
    value = re.sub(r"\s*표\s*\d+$", "", value)
    value = re.sub(r"\([^)]*(?:년\s*이후|\d{4}).*?\)", "", value)
    return normalize_text(value)


def normalize_text(value: str) -> str:
    value = value.lower().replace("·", "").replace("･", "").replace("ㆍ", "")
    return re.sub(r"[^0-9a-z가-힣]", "", value)


def text_similarity(left: str, right: str) -> float:
    left_normalized = normalize_text(left)
    right_normalized = normalize_text(right)
    if not left_normalized or not right_normalized:
        return 0.0
    if left_normalized == right_normalized:
        return 1.0
    if left_normalized in right_normalized or right_normalized in left_normalized:
        return 0.86 if min(len(left_normalized), len(right_normalized)) / max(
            len(left_normalized), len(right_normalized)
        ) >= 0.62 else 0.0
    return SequenceMatcher(None, left_normalized, right_normalized).ratio()


def header_text(matrix: list[list[str]], column: int, header_count: int) -> str:
    values = [row[column].strip() for row in matrix[:header_count] if column < len(row) and row[column].strip()]
    return " ".join(dict.fromkeys(values))


def is_text_column(column: LegacyColumn) -> bool:
    return "문자" in column.data_type


def parse_order(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def required_int(value: object, label: str) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise LegacySystemImportError(f"{label} 정보가 올바르지 않습니다.") from error


def cell_text(value: object) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()
