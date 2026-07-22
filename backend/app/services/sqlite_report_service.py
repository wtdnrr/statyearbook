from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
from urllib.parse import unquote

from app.core.contact_metadata import parse_contact_metadata
from app.core.env import env_value, load_local_env_file
from app.db.schema import DB_PATH, connect, init_db
from app.models.report import (
    ChangeItem,
    ColumnDefinition,
    Metric,
    PressInsight,
    ReportPayload,
    ReportSummary,
    StatTable,
    StatTablePart,
    TableMetadata,
    TableHierarchyItem,
    ValidationHighlightCell,
    ValidationHighlightRow,
    ValidationIssue,
    Visualization,
    VisualizationSeries,
)
from app.validation.models import restore_hyphenated_line_breaks


DISPLAY_CHECK_TYPE_MAP = {
    "증감액 검수": "합계 검수",
    "증감률 검수": "비율 검수",
    "평균 검수": "비율 검수",
}


def display_check_type(check_type: str) -> str:
    return DISPLAY_CHECK_TYPE_MAP.get(check_type, check_type)


class SQLiteReportService:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db_path = db_path

    def is_available(self) -> bool:
        load_local_env_file()
        has_database_url = bool(env_value("DATABASE_URL"))
        if not has_database_url and not self._db_path.exists():
            return False

        try:
            with connect(self._db_path) as connection:
                init_db(connection)
                row = connection.execute("SELECT COUNT(*) AS count FROM stat_tables").fetchone()
        except Exception:
            return False

        return bool(row and row["count"] > 0)

    def get_payload(self) -> ReportPayload:
        return self.get_payload_for_report()

    def get_payload_for_report(self, report_id: int | None = None) -> ReportPayload:
        report = self._resolve_report(report_id)
        tables = self.list_tables(
            report_id,
            include_highlights=False,
            include_passing_checks=False,
        )
        return ReportPayload(
            summary=self._summary_from_tables(report, tables),
            tables=tables,
            press_insights=self._press_insights_from_tables(tables),
            available_reports=self.list_reports(),
        )

    def get_summary(self, report_id: int | None = None) -> ReportSummary:
        report = self._resolve_report(report_id)
        return self._summary_from_tables(
            report,
            self.list_tables(
                report_id,
                include_highlights=False,
                include_passing_checks=False,
            ),
        )

    def _summary_from_tables(
        self,
        report: sqlite3.Row | None,
        tables: list[StatTable],
    ) -> ReportSummary:
        if report is None:
            return ReportSummary(
                report_id=None,
                file_name="",
                base_year="",
                total_tables=0,
                normal_count=0,
                needs_review_count=0,
                suspected_error_count=0,
                issue_counts={},
            )

        issue_counts: dict[str, int] = {}
        for table in tables:
            for check in table.checks:
                if check.status == "정상":
                    continue
                issue_counts[check.type] = issue_counts.get(check.type, 0) + 1

        return ReportSummary(
            report_id=int(report["id"]),
            file_name=decode_display_text(report["source_file_name"]),
            base_year=str(report["year"]),
            total_tables=len(tables),
            normal_count=sum(1 for table in tables if table.status == "normal"),
            needs_review_count=sum(1 for table in tables if table.status == "needs_review"),
            suspected_error_count=sum(1 for table in tables if table.status == "suspected_error"),
            issue_counts=issue_counts,
        )

    def list_tables(
        self,
        report_id: int | None = None,
        *,
        include_highlights: bool = True,
        include_passing_checks: bool = True,
    ) -> list[StatTable]:
        report = self._resolve_report(report_id)
        if report is None:
            return []

        with connect(self._db_path) as connection:
            init_db(connection)
            rows = connection.execute(
                """
                SELECT *
                FROM stat_tables
                WHERE report_id = ?
                ORDER BY table_order, id
                """,
                (report["id"],),
            ).fetchall()
            run_id = self._latest_run_id(connection, report["id"])
            cells_by_table = load_cells_by_table(connection, [int(row["id"]) for row in rows])
            checks_by_table: dict[int, list[sqlite3.Row]] = {}
            issue_rows_by_table: dict[int, list[sqlite3.Row]] = {}
            specs_by_rule_id: dict[str, dict[str, Any]] = {}
            if run_id is not None:
                check_rows = load_checks_for_tables(
                    connection,
                    run_id,
                    [int(row["id"]) for row in rows],
                    include_passing=include_passing_checks,
                )
                checks_by_table = group_rows_by_int(check_rows, "table_id")
                specs_by_rule_id = load_rule_specs_by_id(connection, check_rows)
                issue_rows_by_table = group_rows_by_int(
                    load_issue_rows_for_tables(
                        connection,
                        run_id,
                        [
                            int(row["id"])
                            for row in rows
                            if not checks_by_table.get(int(row["id"]))
                        ],
                    ),
                    "table_id",
                )

            physical_tables = [
                self._row_to_table(
                    connection,
                    report,
                    row,
                    run_id,
                    cells=cells_by_table.get(int(row["id"]), []),
                    check_rows=checks_by_table.get(int(row["id"]), []),
                    issue_rows=issue_rows_by_table.get(int(row["id"]), []),
                    specs_by_rule_id=specs_by_rule_id,
                    include_highlights=include_highlights,
                )
                for row in rows
            ]
            return group_table_parts(physical_tables)

    def get_table(self, table_id: str, report_id: int | None = None) -> StatTable | None:
        report = self._resolve_report(report_id)
        if report is None:
            return None

        with connect(self._db_path) as connection:
            init_db(connection)
            table_rows = self._table_rows_for_detail(connection, report, table_id)
            if not table_rows:
                return None

            run_id = self._latest_run_id(connection, report["id"])
            table_ids = [int(row["id"]) for row in table_rows]
            cells_by_table = load_cells_by_table(connection, table_ids)
            check_rows = load_checks_for_tables(connection, run_id, table_ids) if run_id is not None else []
            checks_by_table = group_rows_by_int(check_rows, "table_id")
            specs_by_rule_id = load_rule_specs_by_id(connection, check_rows)
            issue_rows_by_table = (
                group_rows_by_int(
                    load_issue_rows_for_tables(
                        connection,
                        run_id,
                        [
                            int(row["id"])
                            for row in table_rows
                            if not checks_by_table.get(int(row["id"]))
                        ],
                    ),
                    "table_id",
                )
                if run_id is not None
                else {}
            )

            physical_tables = [
                self._row_to_table(
                    connection,
                    report,
                    row,
                    run_id,
                    cells=cells_by_table.get(int(row["id"]), []),
                    check_rows=checks_by_table.get(int(row["id"]), []),
                    issue_rows=issue_rows_by_table.get(int(row["id"]), []),
                    specs_by_rule_id=specs_by_rule_id,
                    include_highlights=True,
                )
                for row in table_rows
            ]
            grouped = group_table_parts(physical_tables)
            return grouped[0] if grouped else None

    def _table_rows_for_detail(
        self,
        connection: sqlite3.Connection,
        report: sqlite3.Row,
        table_id: str,
    ) -> list[sqlite3.Row]:
        if table_id.startswith("db-"):
            raw_id = table_id.removeprefix("db-")
            if not raw_id.isdigit():
                return []
            return connection.execute(
                """
                SELECT *
                FROM stat_tables
                WHERE report_id = ? AND id = ?
                ORDER BY table_order, id
                """,
                (report["id"], int(raw_id)),
            ).fetchall()

        base_code = table_id.removeprefix("group-")
        return connection.execute(
            """
            SELECT *
            FROM stat_tables
            WHERE report_id = ?
              AND (code = ? OR code LIKE ?)
            ORDER BY table_order, id
            """,
            (report["id"], base_code, f"{base_code} 표%"),
        ).fetchall()

    def get_press_insights(self, report_id: int | None = None) -> list[PressInsight]:
        tables = self.list_tables(
            report_id,
            include_highlights=False,
            include_passing_checks=False,
        )
        return self._press_insights_from_tables(tables)

    def _press_insights_from_tables(self, tables: list[StatTable]) -> list[PressInsight]:
        insights: list[PressInsight] = []

        for table in tables:
            if len(insights) >= 3:
                break
            metric = table.key_figures[0] if table.key_figures else None
            if not metric:
                continue
            insights.append(
                PressInsight(
                    id=f"press-{table.id}",
                    table_id=table.id,
                    title=f"{table.title} 주요 수치",
                    body=f"{table.title}에서 {metric.label}은 {metric.value}입니다.",
                    tone="notable",
                )
            )

        return insights

    def list_reports(self) -> list:
        with connect(self._db_path) as connection:
            init_db(connection)
            rows = connection.execute(
                """
                SELECT r.id, r.year, r.title, r.source_file_name, r.imported_at
                FROM annual_reports r
                WHERE COALESCE(r.is_archived, 0) = 0
                GROUP BY r.id
                ORDER BY r.year DESC, r.imported_at DESC, r.id DESC
                """
            ).fetchall()
            return [
                {
                    "id": int(row["id"]),
                    "year": int(row["year"]),
                    "title": row["title"],
                    "file_name": decode_display_text(row["source_file_name"]),
                    "imported_at": row["imported_at"],
                    "table_count": self._logical_table_count(connection, int(row["id"])),
                }
                for row in rows
            ]

    def _latest_report(self) -> sqlite3.Row | None:
        with connect(self._db_path) as connection:
            return connection.execute(
                """
                SELECT *
                FROM annual_reports
                WHERE COALESCE(is_archived, 0) = 0
                ORDER BY imported_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()

    def _report_by_id(self, report_id: int) -> sqlite3.Row | None:
        with connect(self._db_path) as connection:
            return connection.execute(
                "SELECT * FROM annual_reports WHERE id = ?",
                (report_id,),
            ).fetchone()

    def _resolve_report(self, report_id: int | None = None) -> sqlite3.Row | None:
        if report_id is not None:
            report = self._report_by_id(report_id)
            if report is not None:
                return report
        return self._latest_report()

    def _logical_table_count(self, connection: sqlite3.Connection, report_id: int) -> int:
        rows = connection.execute(
            "SELECT code FROM stat_tables WHERE report_id = ?",
            (report_id,),
        ).fetchall()
        return len({re.sub(r"\s+표\d+$", "", row["code"]) for row in rows})

    def _row_to_table(
        self,
        connection: sqlite3.Connection,
        report: sqlite3.Row,
        table_row: sqlite3.Row,
        run_id: int | None,
        *,
        cells: list[sqlite3.Row] | None = None,
        check_rows: list[sqlite3.Row] | None = None,
        issue_rows: list[sqlite3.Row] | None = None,
        specs_by_rule_id: dict[str, dict[str, Any]] | None = None,
        include_highlights: bool = True,
    ) -> StatTable:
        if cells is None:
            cells = connection.execute(
                """
                SELECT row_index, col_index, text_value, numeric_value, is_header, footnote_marker
                FROM stat_table_cells
                WHERE table_id = ?
                ORDER BY row_index, col_index
                """,
                (table_row["id"],),
            ).fetchall()

        matrix = matrix_from_cells(cells)
        footnote_matrix = footnote_matrix_from_cells(cells)
        header_count = header_count_from_cells(cells, matrix)
        label_column_indexes = leading_label_column_indexes(matrix, header_count)
        columns = build_columns(
            matrix,
            header_count,
            label_column_indexes,
            table_code=table_row["code"],
        )
        rows = build_rows(matrix, columns, header_count, footnote_matrix, label_column_indexes)
        table_id = f"db-{table_row['id']}"
        checks = build_validation_issues(
            connection,
            run_id,
            table_row["id"],
            header_count,
            matrix,
            check_rows=check_rows,
            issue_rows=issue_rows,
            specs_by_rule_id=specs_by_rule_id,
            include_highlights=include_highlights,
        )
        status, status_label = status_from_checks(checks)
        contact = parse_contact_metadata(table_row["source"] or "")

        return StatTable(
            id=table_id,
            code=table_row["code"],
            title=table_row["title"],
            title_en=part_title_en_from_table_row(table_row),
            section_title=table_row["section_title"],
            section_title_en=table_row["section_title_en"],
            domain=table_row["domain"],
            unit=table_row["unit"] or "-",
            sheet_name=table_row["section_file"],
            status=status,
            status_label=status_label,
            year_range=str(report["year"]),
            updated_at=date_only(table_row["extracted_at"]),
            theme=theme_from_code(table_row["code"]),
            part_label=part_label_from_table_row(table_row),
            hierarchy=build_hierarchy(table_row),
            columns=columns,
            rows=rows,
            summary=build_summary(table_row, rows, columns),
            key_figures=build_key_figures(table_row, rows, columns),
            checks=checks,
            changes=[
                ChangeItem(
                    id=f"{table_id}-import",
                    category="구조화",
                    item="원천 데이터 표",
                    previous="더미 데이터",
                    current="SQLite DB 적재",
                    status="정상",
                )
            ],
            visualizations=build_visualizations(table_id, table_row, rows, columns),
            metadata=TableMetadata(
                original_file=decode_display_text(report["source_file_name"]),
                sheet_name=table_row["section_file"],
                note=table_row["note"],
                source=table_row["source"],
                source_department=contact.department,
                source_officer=contact.officer,
                source_extension=contact.extension,
                source_reference=contact.source_reference,
                base_date=table_row["base_date"] or f"{report['year']} 기준",
                base_date_display=metadata_base_date_display(table_row, report["year"]),
                unit_display=metadata_unit_display(table_row),
                extracted_at=table_row["extracted_at"],
                header_count=header_count,
            ),
        )

    def _latest_run_id(self, connection: sqlite3.Connection, report_id: int) -> int | None:
        row = connection.execute(
            """
            SELECT id
            FROM validation_runs
            WHERE report_id = ?
            ORDER BY completed_at DESC, id DESC
            LIMIT 1
            """,
            (report_id,),
        ).fetchone()
        return int(row["id"]) if row else None


def chunks(values: list[int], size: int = 800) -> list[list[int]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def group_rows_by_int(rows: list[sqlite3.Row], key: str) -> dict[int, list[sqlite3.Row]]:
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(int(row[key]), []).append(row)
    return grouped


def load_cells_by_table(
    connection: sqlite3.Connection,
    table_ids: list[int],
) -> dict[int, list[sqlite3.Row]]:
    rows: list[sqlite3.Row] = []
    for chunk in chunks(table_ids):
        placeholders = ", ".join("?" for _ in chunk)
        rows.extend(
            connection.execute(
                f"""
                SELECT table_id, row_index, col_index, text_value, numeric_value, is_header, footnote_marker
                FROM stat_table_cells
                WHERE table_id IN ({placeholders})
                ORDER BY table_id, row_index, col_index
                """,
                chunk,
            ).fetchall()
        )
    return group_rows_by_int(rows, "table_id")


def load_checks_for_tables(
    connection: sqlite3.Connection,
    run_id: int,
    table_ids: list[int],
    *,
    include_passing: bool = True,
) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    status_filter = "" if include_passing else "AND status <> '정상'"
    for chunk in chunks(table_ids):
        placeholders = ", ".join("?" for _ in chunk)
        rows.extend(
            connection.execute(
                f"""
                SELECT *
                FROM validation_checks
                WHERE run_id = ? AND table_id IN ({placeholders})
                {status_filter}
                ORDER BY
                    table_id,
                    CASE status
                        WHEN '오류 의심' THEN 0
                        WHEN '확인 필요' THEN 1
                        WHEN '정상' THEN 2
                        ELSE 3
                    END,
                    CASE severity
                        WHEN 'critical' THEN 0
                        WHEN 'warning' THEN 1
                        ELSE 2
                    END,
                    row_index IS NULL,
                    row_index,
                    col_index,
                    id
                """,
                [run_id, *chunk],
            ).fetchall()
        )
    return rows


def load_issue_rows_for_tables(
    connection: sqlite3.Connection,
    run_id: int,
    table_ids: list[int],
) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    for chunk in chunks(table_ids):
        placeholders = ", ".join("?" for _ in chunk)
        rows.extend(
            connection.execute(
                f"""
                SELECT *
                FROM validation_issues
                WHERE run_id = ? AND table_id IN ({placeholders})
                ORDER BY
                    table_id,
                    CASE severity
                        WHEN 'critical' THEN 0
                        WHEN 'warning' THEN 1
                        ELSE 2
                    END,
                    row_index IS NULL,
                    row_index,
                    col_index,
                    id
                """,
                [run_id, *chunk],
            ).fetchall()
        )
    return rows


def build_validation_issues(
    connection: sqlite3.Connection,
    run_id: int | None,
    table_id: int,
    header_count: int,
    matrix: list[list[Any]] | None = None,
    *,
    check_rows: list[sqlite3.Row] | None = None,
    issue_rows: list[sqlite3.Row] | None = None,
    specs_by_rule_id: dict[str, dict[str, Any]] | None = None,
    include_highlights: bool = True,
) -> list[ValidationIssue]:
    if run_id is None:
        return []

    if check_rows is None:
        check_rows = connection.execute(
            """
            SELECT *
            FROM validation_checks
            WHERE run_id = ? AND table_id = ?
            ORDER BY
                CASE status
                    WHEN '오류 의심' THEN 0
                    WHEN '확인 필요' THEN 1
                    WHEN '정상' THEN 2
                    ELSE 3
                END,
                CASE severity
                    WHEN 'critical' THEN 0
                    WHEN 'warning' THEN 1
                    ELSE 2
                END,
                row_index IS NULL,
                row_index,
                col_index,
                id
            """,
            (run_id, table_id),
        ).fetchall()

    if check_rows:
        specs_by_rule_id = specs_by_rule_id or load_rule_specs_by_id(connection, check_rows)
        return [
            validation_issue_from_row(
                row,
                issue_id=f"check-{row['id']}",
                check_type=row["check_type"],
                header_count=header_count,
                matrix=matrix,
                spec=specs_by_rule_id.get(row["rule_id"]),
                include_highlights=include_highlights,
            )
            for row in check_rows
        ]

    if issue_rows is None:
        issue_rows = connection.execute(
            """
            SELECT *
            FROM validation_issues
            WHERE run_id = ? AND table_id = ?
            ORDER BY
                CASE severity
                    WHEN 'critical' THEN 0
                    WHEN 'warning' THEN 1
                    ELSE 2
                END,
                row_index IS NULL,
                row_index,
                col_index,
                id
            """,
            (run_id, table_id),
        ).fetchall()

    return [
        validation_issue_from_row(
            row,
            issue_id=f"issue-{row['id']}",
            check_type=row["issue_type"],
            header_count=header_count,
            matrix=matrix,
            spec=None,
            include_highlights=include_highlights,
        )
        for row in issue_rows
    ]


@dataclass(frozen=True)
class ResolvedHighlights:
    scope: str
    cells: list[ValidationHighlightCell]
    rows: list[ValidationHighlightRow]
    focus_cell: ValidationHighlightCell | None


def validation_issue_from_row(
    row: sqlite3.Row,
    *,
    issue_id: str,
    check_type: str,
    header_count: int,
    matrix: list[list[Any]] | None,
    spec: dict[str, Any] | None,
    include_highlights: bool = True,
) -> ValidationIssue:
    highlights = (
        resolve_highlights(row, spec=spec, header_count=header_count, matrix=matrix)
        if include_highlights
        else ResolvedHighlights(scope="none", cells=[], rows=[], focus_cell=None)
    )
    return ValidationIssue(
        id=issue_id,
        rule_id=row["rule_id"],
        type=display_check_type(check_type),
        location=row["location"],
        row_index=row["row_index"],
        col_index=row["col_index"],
        current_value=row["current_value"],
        expected_value=row["expected_value"],
        difference=row["difference"],
        status=row["status"],
        severity=row["severity"],
        detail=row["detail"],
        formula=row["formula"],
        highlight_scope=highlights.scope,
        highlight_cells=highlights.cells,
        highlight_rows=highlights.rows,
        focus_cell=highlights.focus_cell,
    )


def resolve_highlights(
    row: sqlite3.Row,
    *,
    spec: dict[str, Any] | None,
    header_count: int,
    matrix: list[list[Any]] | None,
) -> ResolvedHighlights:
    cells = highlight_cells_for(row, spec, matrix)
    focus = next((cell for cell in cells if cell.role == "target"), None)
    return ResolvedHighlights(
        scope=highlight_scope_for(row, spec, header_count),
        cells=cells,
        rows=highlight_rows_for(row, spec),
        focus_cell=focus or next(iter(cells), None),
    )


def load_rule_specs_by_id(
    connection: sqlite3.Connection,
    check_rows: list[sqlite3.Row],
) -> dict[str, dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {}
    profile_ids = sorted(
        {
            int(row["profile_id"])
            for row in check_rows
            if row["profile_id"] is not None
        }
    )

    if profile_ids:
        placeholders = ", ".join("?" for _ in profile_ids)
        profile_rows = connection.execute(
            f"""
            SELECT id, rules_json
            FROM validation_profiles
            WHERE id IN ({placeholders})
            """,
            profile_ids,
        ).fetchall()

        for profile_row in profile_rows:
            try:
                rules = json.loads(profile_row["rules_json"] or "{}")
            except json.JSONDecodeError:
                continue
            checks = rules.get("checks", [])
            if not isinstance(checks, list):
                continue
            for spec in checks:
                if not isinstance(spec, dict) or not spec.get("id"):
                    continue
                specs[str(spec["id"])] = spec

    for row in check_rows:
        rule_id = str(row["rule_id"])
        spec = cross_split_part_row_total_spec(rule_id)
        if spec is None:
            spec = cross_profile_row_sum_operand_spec(rule_id)
        if spec is not None:
            specs[rule_id] = spec
    return specs


def cross_split_part_row_total_spec(rule_id: str) -> dict[str, Any] | None:
    if not rule_id.startswith("cross.split_part_row_total:"):
        return None

    values: dict[str, str] = {}
    for chunk in rule_id.split(":")[1:]:
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        values[key] = value

    target = int_or_none(values.get("target"))
    related = [
        int(value)
        for value in values.get("related", "").split(",")
        if value.strip().isdigit()
    ]
    role = values.get("role", "target")
    if target is None:
        return None

    if role == "operand":
        return {
            "id": rule_id,
            "type": "cross_split_operand_row",
            "operand_columns": related,
        }

    return {
        "id": rule_id,
        "type": "row_sum",
        "target_column": target,
        "operand_columns": related,
    }


def cross_profile_row_sum_operand_spec(rule_id: str) -> dict[str, Any] | None:
    if not rule_id.startswith("cross.profile_row_sum_operand:"):
        return None

    related_chunk = next(
        (chunk for chunk in rule_id.split(":") if chunk.startswith("related=")),
        "",
    )
    related = [
        int(value)
        for value in related_chunk.removeprefix("related=").split(",")
        if value.strip().isdigit()
    ]
    if not related:
        return None
    return {
        "id": rule_id,
        "type": "cross_split_operand_row",
        "operand_columns": related,
    }


def int_or_none(value: Any) -> int | None:
    return int(value) if value is not None else None


def highlight_cell(row_index: int | None, col_index: int | None, role: str) -> ValidationHighlightCell | None:
    if row_index is None or col_index is None or row_index < 0 or col_index < 0:
        return None
    return ValidationHighlightCell(row_index=row_index, col_index=col_index, role=role)


def highlight_row(row_index: int | None, role: str) -> ValidationHighlightRow | None:
    if row_index is None or row_index < 0:
        return None
    return ValidationHighlightRow(row_index=row_index, role=role)


def unique_highlight_cells(cells: list[ValidationHighlightCell | None]) -> list[ValidationHighlightCell]:
    merged: dict[tuple[int, int], ValidationHighlightCell] = {}
    for cell in cells:
        if cell is None:
            continue
        key = (cell.row_index, cell.col_index)
        current = merged.get(key)
        if current is None or cell.role == "target":
            merged[key] = cell
    return list(merged.values())


def spec_columns(spec: dict[str, Any] | None, key: str) -> list[int]:
    if not spec:
        return []
    return [int(value) for value in spec.get(key, []) if value is not None]


def spec_rows(spec: dict[str, Any] | None, key: str) -> list[int]:
    if not spec:
        return []
    return [int(value) for value in spec.get(key, []) if value is not None]


def relation_cell_for_view(
    descriptor: Any,
    matrix: list[list[Any]] | None,
) -> tuple[int, int] | None:
    """Resolve a profile relation cell for exact table-cell highlighting."""

    if not isinstance(descriptor, dict):
        return None
    column = int(descriptor.get("column", -1))
    if column < 0:
        return None

    if descriptor.get("row") is not None:
        row = int(descriptor.get("row", -1))
        return (row, column) if row >= 0 else None
    if not matrix:
        return None

    selector = str(descriptor.get("row_selector") or "")
    if selector == "latest_year":
        candidates: list[tuple[int, int]] = []
        for row_index, cells in enumerate(matrix):
            label = matrix_row_label(cells)
            match = re.fullmatch(r"(?:19|20)(\d{2})", label)
            if match:
                candidates.append((int(match.group(0)), row_index))
        if candidates:
            return max(candidates)[1], column

    if selector == "cumulative_total":
        for row_index, cells in enumerate(matrix):
            label = normalize_lookup_text(matrix_row_label(cells))
            if "누적계" in label or "cumulativetotal" in label:
                return row_index, column

    return None


def matrix_row_label(cells: list[Any]) -> str:
    if not cells or cells[0] is None:
        return ""
    return clean_table_cell_text(cells[0])


def clean_table_cell_text(cell: Any) -> str:
    if isinstance(cell, str):
        value = cell
    elif isinstance(cell, sqlite3.Row):
        value = cell["text_value"]
    elif isinstance(cell, dict):
        value = cell.get("text_value", "")
    else:
        value = str(cell or "")
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_lookup_text(value: str) -> str:
    return re.sub(r"[\s·,._\-()/%]+", "", value).lower()


def formula_previous_column(spec: dict[str, Any] | None, current_col: int | None) -> int | None:
    if spec is None or current_col is None:
        return None
    ordered_columns = sorted(spec_columns(spec, "columns"))
    previous_columns = [col_index for col_index in ordered_columns if col_index < current_col]
    return previous_columns[-1] if previous_columns else None


def highlight_cells_for(
    row: sqlite3.Row,
    spec: dict[str, Any] | None,
    matrix: list[list[Any]] | None = None,
) -> list[ValidationHighlightCell]:
    row_index = int_or_none(row["row_index"])
    col_index = int_or_none(row["col_index"])
    if spec is None:
        return fallback_highlight_cells(row)

    rule_type = str(spec.get("type") or "")
    cells: list[ValidationHighlightCell | None] = []

    if rule_type == "row_sum":
        target_col = int(spec.get("target_column", col_index if col_index is not None else -1))
        cells.append(highlight_cell(row_index, target_col, "target"))
        cells.extend(highlight_cell(row_index, col, "related") for col in spec_columns(spec, "operand_columns"))
    elif rule_type == "cross_table_row_sum":
        target_col = int(spec.get("target_column", col_index if col_index is not None else -1))
        cells.append(highlight_cell(row_index, target_col, "target"))
        # The paired-table operand is rendered on its own tab. Highlight the
        # same-table subtotal here, which is the directly visible input.
        cells.extend(highlight_cell(row_index, col, "related") for col in spec_columns(spec, "operand_columns"))
    elif rule_type == "cross_table_weighted_average":
        target_row = int(spec.get("target_row", row_index if row_index is not None else -1))
        target_col = int(spec.get("target_column", col_index if col_index is not None else -1))
        value_col = int(spec.get("value_column", target_col))
        cells.append(highlight_cell(target_row, target_col, "target"))
        for pair in spec.get("row_pairs", []):
            if isinstance(pair, dict):
                cells.append(highlight_cell(int(pair.get("value_row", -1)), value_col, "related"))
    elif rule_type == "cross_table_cell_match":
        # The source cell belongs to a different table. Only the visible target
        # value is highlighted in this table's original-table panel.
        cells.append(highlight_cell(row_index, col_index, "target"))
    elif rule_type == "cell_sum":
        target_row = int(spec.get("target_row", row_index if row_index is not None else -1))
        target_col = int(spec.get("target_column", col_index if col_index is not None else -1))
        cells.append(highlight_cell(target_row, target_col, "target"))
        for operand in spec.get("operand_cells", []):
            if isinstance(operand, dict):
                cells.append(
                    highlight_cell(
                        int(operand.get("row", -1)),
                        int(operand.get("column", -1)),
                        "related",
                    )
                )
    elif rule_type == "cell_relation_sum":
        for comparison in spec.get("comparisons", []):
            if not isinstance(comparison, dict):
                continue
            target = relation_cell_for_view(comparison.get("target"), matrix)
            if target is None:
                continue
            target_row, target_col = target
            if target_row != row_index or target_col != col_index:
                continue
            cells.append(highlight_cell(target_row, target_col, "target"))
            for operand in comparison.get("operand_cells", []):
                resolved_operand = relation_cell_for_view(operand, matrix)
                if resolved_operand is not None:
                    cells.append(highlight_cell(*resolved_operand, "related"))
            break
    elif rule_type == "row_arithmetic":
        target_col = int(spec.get("target_column", col_index if col_index is not None else -1))
        cells.append(highlight_cell(row_index, target_col, "target"))
        term_columns = [
            int(term.get("column", -1))
            for term in spec.get("terms", [])
            if isinstance(term, dict) and term.get("column") is not None
        ]
        cells.extend(highlight_cell(row_index, col, "related") for col in term_columns)
    elif rule_type == "row_ratio":
        target_col = int(spec.get("target_column", col_index if col_index is not None else -1))
        numerator_col = int(spec.get("numerator_column", -1))
        denominator_columns = spec_columns(spec, "denominator_columns")
        denominator_col = int(spec.get("denominator_column", -1))
        if not denominator_columns and denominator_col >= 0:
            denominator_columns = [denominator_col]
        cells.extend(
            [
                highlight_cell(row_index, target_col, "target"),
                highlight_cell(row_index, numerator_col, "related"),
            ]
        )
        cells.extend(highlight_cell(row_index, denominator_col, "related") for denominator_col in denominator_columns)
    elif rule_type == "column_share_ratio":
        target_col = int(spec.get("target_column", col_index if col_index is not None else -1))
        numerator_col = int(spec.get("numerator_column", -1))
        denominator_row = int(spec.get("denominator_row", -1))
        denominator_col = int(spec.get("denominator_column", numerator_col))
        cells.extend(
            [
                highlight_cell(row_index, target_col, "target"),
                highlight_cell(row_index, numerator_col, "related"),
                highlight_cell(denominator_row, denominator_col, "related"),
            ]
        )
    elif rule_type == "row_growth_rate":
        target_col = int(spec.get("target_column", col_index if col_index is not None else -1))
        current_col = int(spec.get("current_column", -1))
        previous_col = int(spec.get("previous_column", -1))
        cells.extend(
            [
                highlight_cell(row_index, target_col, "target"),
                highlight_cell(row_index, current_col, "related"),
                highlight_cell(row_index, previous_col, "related"),
            ]
        )
    elif rule_type in {"column_sum", "region_total"}:
        target_row = int(spec.get("target_row", row_index if row_index is not None else -1))
        rule_columns = spec_columns(spec, "columns")
        current_col = col_index if col_index is not None else (rule_columns[0] if rule_columns else -1)
        cells.append(highlight_cell(target_row, current_col, "target"))
        operand_rows = spec_rows(spec, "operand_rows")
        for operand_row in operand_rows:
            cells.append(highlight_cell(operand_row, current_col, "related"))
    elif rule_type == "row_ratio_by_rows":
        target_row = int(spec.get("target_row", row_index if row_index is not None else -1))
        numerator_row = int(spec.get("numerator_row", -1))
        cells.extend([highlight_cell(target_row, col_index, "target"), highlight_cell(numerator_row, col_index, "related")])
        denominator_rows = spec_rows(spec, "denominator_rows")
        if not denominator_rows:
            denominator_rows = [int(spec.get("denominator_row", -1))]
        cells.extend(highlight_cell(denominator_row, col_index, "related") for denominator_row in denominator_rows)
    elif rule_type == "weighted_average":
        target_row = int(spec.get("target_row", row_index if row_index is not None else -1))
        target_col = int(spec.get("target_column", col_index if col_index is not None else -1))
        value_col = int(spec.get("value_column", -1))
        weight_col = int(spec.get("weight_column", -1))
        cells.append(highlight_cell(target_row, target_col, "target"))
        for operand_row in spec_rows(spec, "operand_rows"):
            cells.append(highlight_cell(operand_row, value_col, "related"))
            cells.append(highlight_cell(operand_row, weight_col, "related"))
    elif rule_type == "row_year_over_year_rate":
        target_row = row_index if row_index is not None else int(spec.get("target_row", -1))
        source_row = next(
            (
                int(pair.get("source_row", -1))
                for pair in spec.get("row_pairs", [])
                if isinstance(pair, dict) and int(pair.get("target_row", -1)) == target_row
            ),
            int(spec.get("source_row", -1)),
        )
        previous_col = formula_previous_column(spec, col_index)
        cells.extend(
            [
                highlight_cell(target_row, col_index, "target"),
                highlight_cell(source_row, col_index, "related"),
                highlight_cell(source_row, previous_col, "related"),
            ]
        )
    elif rule_type == "row_year_over_year_change_amount":
        target_row = int(spec.get("target_row", row_index if row_index is not None else -1))
        source_row = int(spec.get("source_row", -1))
        previous_col = formula_previous_column(spec, col_index)
        cells.extend(
            [
                highlight_cell(target_row, col_index, "target"),
                highlight_cell(source_row, col_index, "related"),
                highlight_cell(source_row, previous_col, "related"),
            ]
        )
    elif rule_type == "year_rows_change_rate":
        value_col = int(spec.get("value_column", -1))
        change_col = spec.get("change_column")
        change_col_value = int(change_col) if change_col is not None else None
        rate_col = int(spec.get("rate_column", -1))
        row_indices = sorted(spec_rows(spec, "row_indices"))
        previous_rows = [candidate for candidate in row_indices if row_index is not None and candidate < row_index]
        previous_row = previous_rows[-1] if previous_rows else None
        if change_col_value is not None and col_index == change_col_value:
            cells.append(highlight_cell(row_index, change_col_value, "target"))
        elif col_index == rate_col:
            cells.append(highlight_cell(row_index, rate_col, "target"))
        else:
            cells.append(highlight_cell(row_index, col_index, "target"))
        cells.extend(
            [
                highlight_cell(row_index, value_col, "related"),
                highlight_cell(previous_row, value_col, "related"),
            ]
        )
    elif rule_type == "year_rows_change_amount":
        value_col = int(spec.get("value_column", -1))
        change_col = int(spec.get("change_column", col_index if col_index is not None else -1))
        row_indices = sorted(spec_rows(spec, "row_indices"))
        previous_rows = [candidate for candidate in row_indices if row_index is not None and candidate < row_index]
        previous_row = previous_rows[-1] if previous_rows else None
        cells.extend(
            [
                highlight_cell(row_index, change_col, "target"),
                highlight_cell(row_index, value_col, "related"),
                highlight_cell(previous_row, value_col, "related"),
            ]
        )
    elif rule_type == "cross_split_operand_row":
        cells.extend(highlight_cell(row_index, col, "related") for col in spec_columns(spec, "operand_columns"))
    elif rule_type in {
        "spelling_static",
        "translation_static",
        "growth_rate_scan",
    }:
        cells.append(highlight_cell(row_index, col_index, "target"))
    else:
        cells.append(highlight_cell(row_index, col_index, "target"))

    return unique_highlight_cells(cells)


def highlight_rows_for(row: sqlite3.Row, spec: dict[str, Any] | None) -> list[ValidationHighlightRow]:
    if spec is None:
        return fallback_highlight_rows(row)

    rule_type = str(spec.get("type") or "")
    if rule_type in {
        "row_sum",
        "cell_sum",
        "cell_relation_sum",
        "row_arithmetic",
        "row_ratio",
        "column_share_ratio",
        "row_growth_rate",
        "column_sum",
        "region_total",
        "row_ratio_by_rows",
        "weighted_average",
        "row_year_over_year_rate",
        "row_year_over_year_change_amount",
        "year_rows_change_rate",
        "year_rows_change_amount",
        "cross_split_operand_row",
        "cross_table_row_sum",
        "cross_table_weighted_average",
        "cross_table_cell_match",
    }:
        return []

    return []


def highlight_scope_for(row: sqlite3.Row, spec: dict[str, Any] | None, header_count: int) -> str:
    row_index = int_or_none(row["row_index"])
    if spec is None:
        return fallback_highlight_scope(row, header_count)

    rule_type = str(spec.get("type") or "")
    if rule_type in {"unit_required", "metadata_required"}:
        return "metadata"
    if row_index is not None and row_index < header_count:
        return "header"
    if rule_type in {
        "row_sum",
        "cell_sum",
        "cell_relation_sum",
        "row_arithmetic",
        "row_ratio",
        "column_share_ratio",
        "row_growth_rate",
        "row_year_over_year_change_amount",
        "year_rows_change_amount",
        "cross_split_operand_row",
        "cross_table_row_sum",
    }:
        return "row"
    if rule_type in {"column_sum", "region_total", "row_ratio_by_rows", "cross_table_weighted_average"}:
        return "column"
    if rule_type == "weighted_average":
        return "column"
    if rule_type in {"spelling_static", "translation_static"}:
        return "header" if row_index is not None and row_index < header_count else "cell"
    return "cell" if row_index is not None and row["col_index"] is not None else "none"


def fallback_highlight_cells(row: sqlite3.Row) -> list[ValidationHighlightCell]:
    cell = highlight_cell(int_or_none(row["row_index"]), int_or_none(row["col_index"]), "target")
    return [cell] if cell else []


def fallback_highlight_rows(row: sqlite3.Row) -> list[ValidationHighlightRow]:
    if int_or_none(row["col_index"]) is not None:
        return []
    row_highlight = highlight_row(int_or_none(row["row_index"]), "target")
    return [row_highlight] if row_highlight else []


def fallback_highlight_scope(row: sqlite3.Row, header_count: int) -> str:
    row_index = int_or_none(row["row_index"])
    col_index = int_or_none(row["col_index"])
    if row_index is None or col_index is None:
        return "none"
    if row_index < header_count:
        return "header"
    return "cell"


def decode_display_text(value: str) -> str:
    decoded = unquote(value)
    return decoded or value


def status_from_checks(checks: list[ValidationIssue]) -> tuple[str, str]:
    active_checks = [check for check in checks if check.status != "정상"]
    if any(check.severity == "critical" for check in active_checks):
        return "suspected_error", "오류 의심"
    if active_checks:
        return "needs_review", "확인 필요"
    return "normal", "정상"


PART_SUFFIX_RE = re.compile(r"\s+표\s*(\d+)$")


def base_table_code(code: str) -> str:
    return PART_SUFFIX_RE.sub("", code)


def part_label_from_code(code: str) -> str | None:
    match = PART_SUFFIX_RE.search(code)
    return f"표{match.group(1)}" if match else None


def part_caption_from_raw_context(raw_context: str) -> tuple[str, str | None]:
    match = re.search(r"▫\s*([^()]+?)(?:\s*\(|$)", raw_context)
    if not match:
        return "", None
    caption = re.sub(r"\s+", " ", match.group(1)).strip()
    caption = restore_hyphenated_line_breaks(caption)
    title = clean_label(caption)
    title_en = english_label(caption)
    return title or caption, title_en


def part_label_from_table_row(table_row: sqlite3.Row) -> str | None:
    base_label = part_label_from_code(table_row["code"])
    if not base_label:
        return None
    caption, _ = part_caption_from_raw_context(table_row["raw_context"] or "")
    return f"{base_label} · {caption}" if caption else base_label


def part_title_en_from_table_row(table_row: sqlite3.Row) -> str:
    _, title_en = part_caption_from_raw_context(table_row["raw_context"] or "")
    return title_en or table_row["title_en"]


def part_title_from_label(part_label: str | None, fallback: str) -> str:
    if part_label and "·" in part_label:
        return part_label.split("·", 1)[1].strip()
    return fallback


def strip_part_suffix(value: str) -> str:
    return PART_SUFFIX_RE.sub("", value).strip()


def parent_code_for(code: str) -> str:
    base_code = base_table_code(code)
    if base_code.startswith("부록 "):
        match = re.match(r"^(부록\s+\d+)-\d+$", base_code)
        return match.group(1) if match else ""

    parts = base_code.split("-")
    return "-".join(parts[:-1]) if len(parts) > 1 else ""


def build_hierarchy(table_row: sqlite3.Row) -> list[TableHierarchyItem]:
    base_code = base_table_code(table_row["code"])
    base_title = strip_part_suffix(table_row["title"])
    base_title_en = table_row["title_en"]
    hierarchy: list[TableHierarchyItem] = []

    domain = table_row["domain"]
    if domain and domain != "통계":
        hierarchy.append(TableHierarchyItem(code=base_code.split("-", 1)[0], title=domain))

    section_title = strip_part_suffix(table_row["section_title"] or "")
    if section_title and section_title != base_title:
        hierarchy.append(
            TableHierarchyItem(
                code=parent_code_for(base_code),
                title=section_title,
                title_en=table_row["section_title_en"] or None,
            )
        )

    hierarchy.append(TableHierarchyItem(code=base_code, title=base_title, title_en=base_title_en or None))
    return hierarchy


def table_to_part(table: StatTable) -> StatTablePart:
    return StatTablePart(
        id=table.id,
        code=table.code,
        title=part_title_from_label(table.part_label, strip_part_suffix(table.title)),
        title_en=table.title_en,
        part_label=table.part_label or "표",
        unit=table.unit,
        status=table.status,
        status_label=table.status_label,
        updated_at=table.updated_at,
        columns=table.columns,
        rows=table.rows,
        checks=table.checks,
        changes=table.changes,
        visualizations=table.visualizations,
        metadata=table.metadata,
    )


def group_title_en(table: StatTable) -> str:
    if table.hierarchy:
        title_en = table.hierarchy[-1].title_en
        if title_en:
            return title_en
    return table.title_en


def combined_status(tables: list[StatTable]) -> tuple[str, str]:
    if any(table.status == "suspected_error" for table in tables):
        return "suspected_error", "오류 의심"
    if any(table.status == "needs_review" for table in tables):
        return "needs_review", "확인 필요"
    return "normal", "정상"


def group_table_parts(tables: list[StatTable]) -> list[StatTable]:
    grouped_tables: list[StatTable] = []
    groups: dict[str, list[StatTable]] = {}
    order: list[str] = []

    for table in tables:
        base_code = base_table_code(table.code)
        if base_code not in groups:
            groups[base_code] = []
            order.append(base_code)
        groups[base_code].append(table)

    for base_code in order:
        parts = groups[base_code]
        if len(parts) == 1:
            grouped_tables.append(parts[0])
            continue

        first = parts[0]
        status, status_label = combined_status(parts)
        combined_checks = [check for part in parts for check in part.checks]
        combined_changes = [change for part in parts for change in part.changes]
        combined_visualizations = [visualization for part in parts for visualization in part.visualizations]
        grouped_tables.append(
            first.model_copy(
                deep=True,
                update={
                    "id": f"group-{base_code}",
                    "code": base_code,
                    "title": strip_part_suffix(first.title),
                    "title_en": group_title_en(first),
                    "status": status,
                    "status_label": status_label,
                    "checks": combined_checks,
                    "changes": combined_changes,
                    "visualizations": combined_visualizations,
                    "parts": [table_to_part(part) for part in parts],
                },
            )
        )

    return grouped_tables


def matrix_from_cells(cells: list[sqlite3.Row]) -> list[list[str]]:
    if not cells:
        return []

    max_row = max(cell["row_index"] for cell in cells)
    max_col = max(cell["col_index"] for cell in cells)
    matrix = [["" for _ in range(max_col + 1)] for _ in range(max_row + 1)]

    for cell in cells:
        matrix[cell["row_index"]][cell["col_index"]] = cell["text_value"]

    return matrix


def footnote_matrix_from_cells(cells: list[sqlite3.Row]) -> list[list[str]]:
    if not cells:
        return []

    max_row = max(cell["row_index"] for cell in cells)
    max_col = max(cell["col_index"] for cell in cells)
    matrix = [["" for _ in range(max_col + 1)] for _ in range(max_row + 1)]

    for cell in cells:
        matrix[cell["row_index"]][cell["col_index"]] = cell["footnote_marker"] or ""

    return matrix


def header_count_from_cells(cells: list[sqlite3.Row], matrix: list[list[str]]) -> int:
    header_rows = {cell["row_index"] for cell in cells if cell["is_header"]}
    if header_rows:
        return display_header_count_from_matrix(matrix, max(header_rows) + 1)
    return display_header_count_from_matrix(matrix, 1 if matrix else 0)


def display_header_count_from_matrix(matrix: list[list[str]], stored_header_count: int) -> int:
    header_count = stored_header_count
    max_header_count = min(len(matrix), 5)
    while header_count < max_header_count and row_looks_like_header_continuation(matrix[header_count]):
        header_count += 1
    return header_count


def row_looks_like_header_continuation(row: list[str]) -> bool:
    non_empty = [cell.strip() for cell in row if cell.strip()]
    if len(non_empty) < 2:
        return False
    numeric_count = sum(1 for value in non_empty if parse_numeric(value) is not None)
    if numeric_count:
        return False
    joined = " ".join(non_empty).lower()
    return bool(
        re.search(
            r"구분|분류|순위|국가명|지\s*수|지역|연도|항목|계|classification|ranking|country|index|year|total",
            joined,
        )
    )


def row_is_caption_metadata(row: list[str]) -> bool:
    non_empty = [cell.strip() for cell in row if cell.strip()]
    if not non_empty:
        return False
    first = non_empty[0]
    return first.startswith("▫") or first.startswith("※")


ENGLISH_LABEL_BODY = r"A-Za-z0-9 /&().,%·･+=:\-'’‘㎡㎢㎞㎏㎘㎖㎥"
ENGLISH_LABEL_SPAN_RE = re.compile(
    rf"\[\s*[A-Za-z][{ENGLISH_LABEL_BODY}]*\s*\]"
    rf"|\((?=[^)]*[A-Za-z]{{2}})(?=[^)]*\s)[{ENGLISH_LABEL_BODY}]+\)"
    rf"|(?<![A-Za-z0-9가-힣(/=+\-])(?:-\s*)?"
    rf"(?:\d+s|(?=[A-Za-z][{ENGLISH_LABEL_BODY}]*[A-Za-z])[A-Za-z])"
    rf"[{ENGLISH_LABEL_BODY}]*"
)


def clean_label(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", restore_hyphenated_line_breaks(text)).strip()
    if not cleaned:
        return ""

    koreanish = ENGLISH_LABEL_SPAN_RE.sub("", cleaned)
    koreanish = re.sub(r"\s+", " ", koreanish).strip(" /")
    if diagonal_region_header_label(koreanish, cleaned):
        return "지역"
    koreanish = re.sub(r"^구분(?=\S)", "구분 / ", koreanish)
    koreanish = koreanish.replace("()", "").strip(" /")
    koreanish = remove_unmatched_closing_parentheses(koreanish, preserve_numbered_markers=True)
    return koreanish or cleaned


def english_label(text: str) -> str | None:
    text = re.sub(r"\s+", " ", restore_hyphenated_line_breaks(text)).strip()
    if diagonal_region_header_label(clean_label_korean_only(text), text):
        return "Region"
    matches = ENGLISH_LABEL_SPAN_RE.findall(text)
    value = " ".join(item.strip() for item in matches if item.strip())
    value = remove_unmatched_closing_parentheses(value)
    return value or None


def remove_unmatched_closing_parentheses(value: str, *, preserve_numbered_markers: bool = False) -> str:
    """Drop parser artifacts such as `Person)` while retaining balanced units and optional `1)` footnotes."""

    result: list[str] = []
    depth = 0
    for index, character in enumerate(value):
        if character == "(":
            depth += 1
            result.append(character)
            continue
        if character != ")":
            result.append(character)
            continue
        if depth:
            depth -= 1
            result.append(character)
            continue
        if preserve_numbered_markers and index > 0 and value[index - 1].isdigit():
            result.append(character)
    return re.sub(r"\s+", " ", "".join(result)).strip()


def clean_label_korean_only(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", restore_hyphenated_line_breaks(text)).strip()
    koreanish = ENGLISH_LABEL_SPAN_RE.sub("", cleaned)
    return re.sub(r"\s+", " ", koreanish).strip(" /")


def diagonal_region_header_label(korean_text: str, raw_text: str) -> bool:
    normalized_korean = re.sub(r"\s+", "", korean_text)
    normalized_raw = raw_text.lower()
    return (
        "구분" in normalized_korean
        and "지역" in normalized_korean
        and "classification" in normalized_raw
        and "region" in normalized_raw
    )


def is_schedule_descriptor_column_label(value: str) -> bool:
    normalized = re.sub(r"[\s·,._\-()/%]+", "", value).lower()
    if any(keyword in normalized for keyword in ("운영일자", "일자", "일시", "날짜", "기간")):
        return True
    return bool(re.search(r"\b(dates?|period|schedule)\b", value, re.IGNORECASE))


def inherited_header_value(header_row: list[str], col_index: int) -> str:
    if col_index < len(header_row) and header_row[col_index].strip():
        return header_row[col_index]
    for left_col_index in range(col_index - 1, -1, -1):
        if left_col_index < len(header_row) and header_row[left_col_index].strip():
            return header_row[left_col_index]
    return ""


def header_text_for_column(matrix: list[list[str]], header_count: int, col_index: int) -> str:
    values = [
        row[col_index]
        for row in matrix[:header_count]
        if col_index < len(row) and row[col_index].strip()
    ]
    return " ".join(values)


def looks_like_year_label_column(matrix: list[list[str]], header_count: int, col_index: int, values: list[str]) -> bool:
    header = header_text_for_column(matrix, header_count, col_index).lower()
    if "연도" not in header and "year" not in header:
        return False
    if not values:
        return False
    year_like_count = sum(1 for value in values if re.fullmatch(r"[’']?\d{2,4}", value.strip()))
    return year_like_count / len(values) >= 0.7


def leading_label_column_indexes(matrix: list[list[str]], header_count: int) -> list[int]:
    data_rows = [row for row in matrix[header_count:] if any(cell.strip() for cell in row)]
    max_cols = max((len(row) for row in matrix), default=0)
    label_indexes: list[int] = []

    for col_index in range(max_cols):
        values = [
            row[col_index].strip()
            for row in data_rows
            if col_index < len(row) and row[col_index].strip()
        ]
        if not values:
            if label_indexes:
                label_indexes.append(col_index)
                continue
            break

        numeric_count = sum(1 for value in values if parse_numeric(value) is not None)
        numeric_ratio = numeric_count / len(values)
        header_text = header_text_for_column(matrix, header_count, col_index)
        if col_index == 0 and looks_like_year_label_column(matrix, header_count, col_index, values):
            label_indexes.append(col_index)
            continue
        if label_indexes and is_year_measure_column_label(header_text):
            break
        if label_indexes and is_schedule_descriptor_column_label(header_text):
            continue
        # A total measure can contain mostly '-' values (for example, a
        # grand total shown only on the subtotal row). It is still a numeric
        # column, not a second row-label dimension.
        if label_indexes and is_total_measure_column_label(header_text, values):
            break
        if numeric_ratio <= 0.25:
            label_indexes.append(col_index)
            continue
        break

    if independent_descriptor_label_columns(matrix, header_count, label_indexes):
        return [0]

    if len(label_indexes) > 3:
        return [0]

    if len(label_indexes) > 1:
        has_measure_column = any(
            any(
                col_index < len(row) and parse_numeric(row[col_index].strip()) is not None
                for row in data_rows
            )
            for col_index in range(label_indexes[-1] + 1, max_cols)
        )
        if not has_measure_column:
            return [0]

    return label_indexes or [0]


def is_year_measure_column_label(header_text: str) -> bool:
    normalized = re.sub(r"\s+", "", clean_label(header_text))
    return bool(re.fullmatch(r"(?:19|20)\d{2}(?:년(?:도)?)?", normalized))


def is_total_measure_column_label(header_text: str, values: list[str]) -> bool:
    if not any(parse_numeric(value) is not None for value in values):
        return False

    normalized = re.sub(r"\s+", "", header_text).lower()
    return (
        "전체" in header_text
        or "합계" in header_text
        or "총계" in header_text
        or "total" in normalized
        or bool(re.fullmatch(r"계(?:\([^)]*\))?", re.sub(r"\s+", "", header_text)))
    )


def independent_descriptor_label_columns(
    matrix: list[list[str]],
    header_count: int,
    label_indexes: list[int],
) -> bool:
    """Keep registry-style text dimensions as columns instead of a hierarchical row path."""

    if len(label_indexes) < 2:
        return False
    descriptor_hints = (
        "위원회명",
        "설치근거",
        "설립근거",
        "위원장",
        "기관명",
        "기관장",
        "대표자",
        "소재지",
        "주소",
        "주요기능",
        "주요업무",
    )
    headers = [
        re.sub(r"\s+", "", header_text_for_column(matrix, header_count, col_index))
        for col_index in label_indexes
    ]
    normalized_headers = [header.lower() for header in headers]

    def contains(index: int, *hints: str) -> bool:
        return index < len(normalized_headers) and any(
            hint.lower() in normalized_headers[index] for hint in hints
        )

    # These are parallel source dimensions, not levels of one row label.
    # Keep the rule semantic so the same layout works for newly added tables.
    if len(headers) >= 2:
        if (
            contains(0, "연도", "year")
            and contains(1, "구분", "분류", "classification")
        ) or (
            contains(1, "연도", "year")
            and contains(0, "구분", "분류", "classification")
        ):
            return True
        if contains(0, "구분", "분류", "classification") and contains(1, "단위", "unit"):
            return True
        if contains(0, "지역", "region") and contains(
            1,
            "시･군･구",
            "시·군·구",
            "시군구",
            "cities",
            "counties",
            "districts",
        ):
            return True
        if contains(0, "생산(이관)기관", "생산기관", "instruments of production") and contains(
            1,
            "기록물분야",
            "record subjects",
        ):
            return True
    # A service name and its description are parallel text dimensions. They
    # must remain separate columns even though both happen to be non-numeric.
    if any("서비스내용" in header or "servicedescription" in header.lower() for header in headers[1:]):
        return True
    return sum(any(hint in header for hint in descriptor_hints) for header in headers) >= 2


def label_path_for_row(row: list[str], label_column_indexes: list[int], target_col_index: int) -> str:
    parts: list[str] = []
    for col_index in label_column_indexes:
        if col_index > target_col_index or col_index >= len(row):
            continue
        label = clean_label(row[col_index])
        if label and label not in parts:
            parts.append(label)
    return " / ".join(parts)


def english_label_path_for_row(row: list[str], label_column_indexes: list[int], target_col_index: int) -> str | None:
    parts: list[str] = []
    for col_index in label_column_indexes:
        if col_index > target_col_index or col_index >= len(row):
            continue
        label = english_label(row[col_index])
        if label and label not in parts:
            parts.append(label)
    return " / ".join(parts) if parts else None


def inferred_label_column_text(
    matrix: list[list[str]],
    header_count: int,
    label_column_indexes: list[int],
    col_index: int,
) -> tuple[str, str | None]:
    if col_index not in label_column_indexes:
        return "", None

    for row in matrix[header_count:]:
        if col_index >= len(row) or not row[col_index].strip():
            continue
        label = label_path_for_row(row, label_column_indexes, col_index)
        if label:
            return label, english_label_path_for_row(row, label_column_indexes, col_index)
    return "", None


def should_collapse_label_columns(label_column_indexes: list[int]) -> bool:
    return len(label_column_indexes) > 1 and label_column_indexes == list(range(len(label_column_indexes)))


def split_bilingual_descriptor_column_pairs(
    matrix: list[list[str]],
    header_count: int,
) -> set[tuple[int, int]]:
    """Find Korean/English columns produced from one merged HWPX header."""

    max_cols = max((len(row) for row in matrix), default=0)
    data_rows = [row for row in matrix[header_count:] if any(cell.strip() for cell in row)]
    pairs: set[tuple[int, int]] = set()
    for left_col in range(max_cols - 1):
        right_col = left_col + 1
        if not header_text_for_column(matrix, header_count, left_col).strip():
            continue
        if header_text_for_column(matrix, header_count, right_col).strip():
            continue

        paired_values = 0
        bilingual_values = 0
        for row in data_rows:
            left = row[left_col].strip() if left_col < len(row) else ""
            right = row[right_col].strip() if right_col < len(row) else ""
            if not left or not right:
                continue
            paired_values += 1
            if (
                re.search(r"[가-힣]", left)
                and re.search(r"[A-Za-z]", right)
                and not re.search(r"[가-힣]", right)
                and parse_numeric(right) is None
            ):
                bilingual_values += 1

        if paired_values >= 3 and bilingual_values / paired_values >= 0.8:
            pairs.add((left_col, right_col))
    return pairs


def display_source_columns(
    matrix: list[list[str]],
    header_count: int,
    label_column_indexes: list[int],
) -> list[list[int]]:
    max_cols = max((len(row) for row in matrix), default=0)
    if should_collapse_label_columns(label_column_indexes):
        hidden_label_columns = set(label_column_indexes[1:])
        groups = [
            label_column_indexes if col_index == label_column_indexes[0] else [col_index]
            for col_index in range(max_cols)
            if col_index not in hidden_label_columns
        ]
    else:
        groups = [[col_index] for col_index in range(max_cols)]

    bilingual_pairs = split_bilingual_descriptor_column_pairs(matrix, header_count)
    merged_groups: list[list[int]] = []
    index = 0
    while index < len(groups):
        if (
            index + 1 < len(groups)
            and len(groups[index]) == 1
            and len(groups[index + 1]) == 1
            and (groups[index][0], groups[index + 1][0]) in bilingual_pairs
        ):
            merged_groups.append([groups[index][0], groups[index + 1][0]])
            index += 2
            continue
        merged_groups.append(groups[index])
        index += 1
    return merged_groups


def header_rows_for_labels(matrix: list[list[str]], header_count: int) -> list[list[str]]:
    return [row for row in matrix[:header_count] if not row_is_caption_metadata(row)]


def has_display_data(matrix: list[list[str]], header_count: int, col_index: int) -> bool:
    return any(
        col_index < len(row) and row[col_index].strip()
        for row in matrix[header_count:]
    )


def has_display_header(header_rows: list[list[str]], col_index: int) -> bool:
    return any(col_index < len(row) and row[col_index].strip() for row in header_rows)


def build_columns(
    matrix: list[list[str]],
    header_count: int,
    label_column_indexes: list[int] | None = None,
    table_code: str | None = None,
) -> list[ColumnDefinition]:
    if not matrix:
        return []

    header_rows = header_rows_for_labels(matrix, header_count) if header_count else []
    label_column_indexes = label_column_indexes or leading_label_column_indexes(matrix, header_count)
    columns: list[ColumnDefinition] = []

    for source_col_indexes in display_source_columns(matrix, header_count, label_column_indexes):
        col_index = source_col_indexes[0]
        if (
            not any(source_col in label_column_indexes for source_col in source_col_indexes)
            and not has_display_data(matrix, header_count, col_index)
            and not has_display_header(header_rows, col_index)
        ):
            continue
        label_parts: list[str] = []
        english_parts: list[str] = []
        inferred_label, inferred_label_en = ("", None) if len(source_col_indexes) > 1 else inferred_label_column_text(
            matrix,
            header_count,
            label_column_indexes,
            col_index,
        )
        for header_row in header_rows:
            for header_col_index in source_col_indexes:
                value = header_row[header_col_index] if header_col_index < len(header_row) else ""
                if not value.strip() and not inferred_label:
                    value = inherited_header_value(header_row, header_col_index)
                label = clean_label(value)
                if label and label not in label_parts:
                    label_parts.append(label)
                en = english_label(value)
                if en and en not in english_parts:
                    english_parts.append(en)

        label = " / ".join(label_parts) or inferred_label or f"열 {col_index + 1}"
        label_en = " / ".join(english_parts) if english_parts else inferred_label_en
        columns.append(
            ColumnDefinition(
                key=f"c{col_index}",
                label=label,
                label_en=label_en,
                align=column_alignment(col_index, label, label_en),
                width=column_width(col_index, label, label_en),
                source_col_index=col_index,
                source_col_indexes=source_col_indexes,
            )
        )

    apply_column_label_overrides(table_code, columns)
    return columns


def apply_column_label_overrides(
    table_code: str | None,
    columns: list[ColumnDefinition],
) -> None:
    """Correct known HWPX header shifts without changing the source values."""

    if table_code != "5-1-9-1":
        return

    labels = {
        7: ("직영기업 / 지역개발기금", "Business Directly Managed by Local Governments / Local Development Fund"),
        8: ("공사·공단 등 / 소계", "Local Public Corporation and Facilities Management Corporation / Sub-total"),
        9: ("공사·공단 등 / 도시철도", "Local Public Corporation and Facilities Management Corporation / Urban Railway"),
        10: ("공사·공단 등 / 도시개발", "Local Public Corporation and Facilities Management Corporation / Urban Development"),
        11: ("공사·공단 등 / 시설·환경·경륜 등", "Local Public Corporation and Facilities Management Corporation / Facility, Environment, Bicycle Racing, etc."),
    }
    for column in columns:
        source_col = column.source_col_index
        if source_col not in labels:
            continue
        column.label, column.label_en = labels[source_col]


def column_alignment(col_index: int, label: str, label_en: str | None) -> str:
    combined = f"{label} {label_en or ''}"
    if col_index == 0:
        return "left"
    if is_schedule_descriptor_column_label(combined):
        return "center"
    return "right"


def column_width(col_index: int, label: str, label_en: str | None) -> str | None:
    combined = f"{label} {label_en or ''}"
    if col_index == 0:
        return "24%"
    if is_schedule_descriptor_column_label(combined):
        return "18%"
    return None


def build_rows(
    matrix: list[list[str]],
    columns: list[ColumnDefinition],
    header_count: int,
    footnote_matrix: list[list[str]] | None = None,
    label_column_indexes: list[int] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    label_column_indexes = label_column_indexes or leading_label_column_indexes(matrix, header_count)
    for matrix_row_index, source_row in enumerate(matrix[header_count:], start=header_count):
        if not any(cell for cell in source_row):
            continue

        row: dict[str, Any] = {}
        row["_row_index"] = matrix_row_index
        row["_row_label"] = label_path_for_row(
            source_row,
            label_column_indexes,
            label_column_indexes[-1] if label_column_indexes else 0,
        )
        row["_row_label_en"] = english_label_path_for_row(
            source_row,
            label_column_indexes,
            label_column_indexes[-1] if label_column_indexes else 0,
        ) or ""
        for column in columns:
            col_index = column.source_col_index if column.source_col_index is not None else int(column.key[1:])
            is_collapsed_label_column = (
                len(column.source_col_indexes) > 1
                and column.source_col_indexes == label_column_indexes
            )
            is_bilingual_descriptor_column = (
                len(column.source_col_indexes) > 1 and not is_collapsed_label_column
            )
            combined_value = ""
            if is_collapsed_label_column:
                value = row["_row_label"]
            elif is_bilingual_descriptor_column:
                source_values = [
                    source_row[source_col]
                    for source_col in column.source_col_indexes
                    if source_col < len(source_row) and source_row[source_col].strip()
                ]
                combined_value = "\n".join(source_values)
                value = clean_label(combined_value) or (source_values[0] if source_values else "")
            else:
                value = source_row[col_index] if col_index < len(source_row) else ""
            row[column.key] = coerce_display_value(value)
            if is_collapsed_label_column and row["_row_label_en"]:
                row[f"{column.key}_en"] = row["_row_label_en"]
            elif is_bilingual_descriptor_column:
                label_en = english_label(combined_value)
                if label_en:
                    row[f"{column.key}_en"] = label_en
            footnote = (
                footnote_matrix[matrix_row_index][col_index]
                if footnote_matrix and matrix_row_index < len(footnote_matrix) and col_index < len(footnote_matrix[matrix_row_index])
                else ""
            )
            if footnote:
                row[f"{column.key}_footnote"] = footnote
        rows.append(row)

    return rows


def coerce_display_value(value: str) -> str | int | float:
    cleaned = value.strip()
    numeric = parse_numeric(cleaned)
    if numeric is None:
        return cleaned
    if numeric.is_integer() and "." not in cleaned:
        return int(numeric)
    return numeric


def parse_numeric(value: str) -> float | None:
    cleaned = value.replace(",", "").replace("%", "").strip()
    if not cleaned or cleaned in {"-", "－", "―"}:
        return None
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", cleaned):
        return float(cleaned)
    return None


def formatted_value(value: Any, unit: str) -> str:
    if isinstance(value, int):
        body = f"{value:,}"
    elif isinstance(value, float):
        body = f"{value:,.1f}" if not value.is_integer() else f"{int(value):,}"
    else:
        body = str(value)

    return f"{body}{unit}" if unit and unit not in {"-", "%"} and isinstance(value, (int, float)) else body


def metadata_base_date_display(table_row: sqlite3.Row, report_year: int) -> str:
    fallback = table_row["base_date"] or f"{report_year} 기준"
    raw_context = re.sub(r"\s+", " ", table_row["raw_context"] or "")
    match = re.search(r"\(\s*([^()]*?기준)\s*\)\s*\(\s*(As of [^)]+)\s*\)", raw_context)
    if not match:
        return fallback
    korean, english = match.groups()
    return f"{korean.strip()}({english.strip()})"


def metadata_unit_display(table_row: sqlite3.Row) -> str:
    fallback = table_row["unit"] or "-"
    raw_context = re.sub(r"\s+", " ", table_row["raw_context"] or "")
    match = re.search(r"\(\s*단위\s*:\s*([^)]+?)\s*\)\s*\(\s*(Unit\s*:\s*[^)]+)\s*\)", raw_context)
    if not match:
        return fallback
    unit, english = match.groups()
    return f"{unit.strip()}({english.strip()})"


def build_summary(
    table_row: sqlite3.Row,
    rows: list[dict[str, Any]],
    columns: list[ColumnDefinition],
) -> list[str]:
    unit = table_row["unit"] or ""
    summary = [
        f"{table_row['title']} 표는 {len(rows)}개 행과 {len(columns)}개 항목으로 구조화되었습니다.",
    ]

    if table_row["base_date"]:
        summary.append(f"기준일은 {table_row['base_date']}이며, 단위는 {unit or '원문 표기 기준'}입니다.")
    elif unit:
        summary.append(f"단위는 {unit}입니다.")

    first_row = rows[0] if rows else None
    numeric_items = numeric_values(first_row, columns) if first_row else []
    if first_row and numeric_items:
        row_label = str(first_row.get(columns[0].key, "첫 행"))
        metric_label, metric_value = numeric_items[0]
        summary.append(f"{row_label}의 {metric_label} 값은 {formatted_value(metric_value, unit)}입니다.")

    return summary


def build_key_figures(
    table_row: sqlite3.Row,
    rows: list[dict[str, Any]],
    columns: list[ColumnDefinition],
) -> list[Metric]:
    unit = table_row["unit"] or ""
    first_row = rows[0] if rows else None
    items = numeric_values(first_row, columns) if first_row else []

    if not items:
        return [
            Metric(label="행 수", value=f"{len(rows):,}", caption="구조화 결과", tone="blue"),
            Metric(label="열 수", value=f"{len(columns):,}", caption="구조화 결과", tone="neutral"),
        ]

    metrics: list[Metric] = []
    for index, (label, value) in enumerate(items[:3]):
        metrics.append(
            Metric(
                label=label,
                value=formatted_value(value, unit),
                caption="첫 데이터 행 기준",
                tone=["blue", "green", "neutral"][index],
            )
        )
    return metrics


def numeric_values(
    row: dict[str, Any] | None,
    columns: list[ColumnDefinition],
) -> list[tuple[str, int | float]]:
    if not row:
        return []

    values: list[tuple[str, int | float]] = []
    for column in columns[1:]:
        value = row.get(column.key)
        if isinstance(value, (int, float)):
            values.append((column.label, value))
    return values


def build_visualizations(
    table_id: str,
    table_row: sqlite3.Row,
    rows: list[dict[str, Any]],
    columns: list[ColumnDefinition],
) -> list[Visualization]:
    if len(columns) < 2:
        return []

    label_key = columns[0].key
    numeric_column = next(
        (
            column
            for column in columns[1:]
            if any(isinstance(row.get(column.key), (int, float)) for row in rows[:12])
        ),
        None,
    )
    if numeric_column is None:
        return []

    series = [
        VisualizationSeries(label=str(row.get(label_key, "")), value=float(row[numeric_column.key]))
        for row in rows
        if isinstance(row.get(numeric_column.key), (int, float)) and str(row.get(label_key, "")).strip()
    ][:8]

    if len(series) < 2:
        return []

    return [
        Visualization(
            id=f"{table_id}-rank",
            title=f"{numeric_column.label} 주요 항목",
            subtitle=table_row["title"],
            kind="rank",
            unit=table_row["unit"] or "",
            data=series,
        )
    ]


def theme_from_code(code: str) -> str:
    chapter = code.split("-", 1)[0]
    if chapter == "6":
        return "red"
    if chapter == "7":
        return "green"
    return "blue"


def date_only(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d")
    except ValueError:
        return value
