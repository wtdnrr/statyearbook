from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import sqlite3
from typing import Any

from app.db.schema import DB_PATH, connect, init_db
from app.models.report import (
    ChangeItem,
    ColumnDefinition,
    Metric,
    PressInsight,
    ReportPayload,
    ReportSummary,
    StatTable,
    TableMetadata,
    ValidationIssue,
    Visualization,
    VisualizationSeries,
)


class SQLiteReportService:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db_path = db_path

    def is_available(self) -> bool:
        if not self._db_path.exists():
            return False

        try:
            with connect(self._db_path) as connection:
                init_db(connection)
                row = connection.execute("SELECT COUNT(*) AS count FROM stat_tables").fetchone()
        except sqlite3.DatabaseError:
            return False

        return bool(row and row["count"] > 0)

    def get_payload(self) -> ReportPayload:
        return ReportPayload(
            summary=self.get_summary(),
            tables=self.list_tables(),
            press_insights=self.get_press_insights(),
        )

    def get_summary(self) -> ReportSummary:
        report = self._latest_report()
        if report is None:
            return ReportSummary(
                file_name="",
                base_year="",
                total_tables=0,
                normal_count=0,
                needs_review_count=0,
                suspected_error_count=0,
                issue_counts={},
            )

        with connect(self._db_path) as connection:
            init_db(connection)
            total = connection.execute(
                "SELECT COUNT(*) AS count FROM stat_tables WHERE report_id = ?",
                (report["id"],),
            ).fetchone()["count"]
            run_id = self._latest_run_id(connection, report["id"])
            issue_rows = []
            if run_id is not None:
                issue_rows = connection.execute(
                    """
                    SELECT table_id, issue_type, severity, status
                    FROM validation_issues
                    WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchall()

        issue_counts: dict[str, int] = {}
        critical_tables: set[int] = set()
        warning_tables: set[int] = set()
        for issue in issue_rows:
            if issue["status"] == "정상":
                continue
            issue_counts[issue["issue_type"]] = issue_counts.get(issue["issue_type"], 0) + 1
            if issue["severity"] == "critical":
                critical_tables.add(issue["table_id"])
            else:
                warning_tables.add(issue["table_id"])

        warning_tables -= critical_tables

        return ReportSummary(
            file_name=report["source_file_name"],
            base_year=str(report["year"]),
            total_tables=total,
            normal_count=max(total - len(critical_tables) - len(warning_tables), 0),
            needs_review_count=len(warning_tables),
            suspected_error_count=len(critical_tables),
            issue_counts=issue_counts,
        )

    def list_tables(self) -> list[StatTable]:
        report = self._latest_report()
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

            return [self._row_to_table(connection, report, row, run_id) for row in rows]

    def get_table(self, table_id: str) -> StatTable | None:
        return next((table for table in self.list_tables() if table.id == table_id), None)

    def get_press_insights(self) -> list[PressInsight]:
        tables = self.list_tables()
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

    def _latest_report(self) -> sqlite3.Row | None:
        with connect(self._db_path) as connection:
            return connection.execute(
                """
                SELECT *
                FROM annual_reports
                ORDER BY imported_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()

    def _row_to_table(
        self,
        connection: sqlite3.Connection,
        report: sqlite3.Row,
        table_row: sqlite3.Row,
        run_id: int | None,
    ) -> StatTable:
        cells = connection.execute(
            """
            SELECT row_index, col_index, text_value, numeric_value, is_header
            FROM stat_table_cells
            WHERE table_id = ?
            ORDER BY row_index, col_index
            """,
            (table_row["id"],),
        ).fetchall()

        matrix = matrix_from_cells(cells)
        header_count = header_count_from_cells(cells, matrix)
        columns = build_columns(matrix, header_count)
        rows = build_rows(matrix, columns, header_count)
        table_id = f"db-{table_row['id']}"
        checks = build_validation_issues(connection, run_id, table_row["id"])
        status, status_label = status_from_checks(checks)

        return StatTable(
            id=table_id,
            code=table_row["code"],
            title=table_row["title"],
            title_en=table_row["title_en"],
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
            columns=columns,
            rows=rows,
            summary=build_summary(table_row, rows, columns),
            key_figures=build_key_figures(table_row, rows, columns),
            checks=checks,
            changes=[
                ChangeItem(
                    id=f"{table_id}-import",
                    category="구조화",
                    item="HWPX 원문 표",
                    previous="더미 데이터",
                    current="SQLite DB 적재",
                    status="정상",
                )
            ],
            visualizations=build_visualizations(table_id, table_row, rows, columns),
            metadata=TableMetadata(
                original_file=report["source_file_name"],
                sheet_name=table_row["section_file"],
                cell_range=table_row["cell_range"],
                note=table_row["note"],
                source=table_row["source"],
                base_date=table_row["base_date"] or f"{report['year']} 기준",
                extracted_at=table_row["extracted_at"],
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


def build_validation_issues(
    connection: sqlite3.Connection,
    run_id: int | None,
    table_id: int,
) -> list[ValidationIssue]:
    if run_id is None:
        return []

    rows = connection.execute(
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
        ValidationIssue(
            id=f"issue-{row['id']}",
            type=row["issue_type"],
            location=row["location"],
            current_value=row["current_value"],
            expected_value=row["expected_value"],
            difference=row["difference"],
            status=row["status"],
            severity=row["severity"],
            detail=row["detail"],
            formula=row["formula"],
        )
        for row in rows
    ]


def status_from_checks(checks: list[ValidationIssue]) -> tuple[str, str]:
    active_checks = [check for check in checks if check.status != "정상"]
    if any(check.severity == "critical" for check in active_checks):
        return "suspected_error", "오류 의심"
    if active_checks:
        return "needs_review", "확인 필요"
    return "normal", "정상"


def matrix_from_cells(cells: list[sqlite3.Row]) -> list[list[str]]:
    if not cells:
        return []

    max_row = max(cell["row_index"] for cell in cells)
    max_col = max(cell["col_index"] for cell in cells)
    matrix = [["" for _ in range(max_col + 1)] for _ in range(max_row + 1)]

    for cell in cells:
        matrix[cell["row_index"]][cell["col_index"]] = cell["text_value"]

    return matrix


def header_count_from_cells(cells: list[sqlite3.Row], matrix: list[list[str]]) -> int:
    header_rows = {cell["row_index"] for cell in cells if cell["is_header"]}
    if header_rows:
        return max(header_rows) + 1
    return 1 if matrix else 0


def clean_label(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return ""

    cleaned = re.sub(r"\([^가-힣)]*[A-Za-z][^)]*\)", "", cleaned)
    koreanish = re.sub(r"[A-Za-z][A-Za-z0-9 /&().,%·･+\-']*", "", cleaned)
    koreanish = re.sub(r"\s+", " ", koreanish).strip(" /")
    koreanish = re.sub(r"^구분(?=\S)", "구분 / ", koreanish)
    koreanish = re.sub(r"(\d{4})(?=[가-힣])", r"\1 ", koreanish)
    koreanish = koreanish.replace("()", "").strip(" /")
    return koreanish or cleaned


def english_label(text: str) -> str | None:
    matches = re.findall(r"[A-Za-z][A-Za-z0-9 /&().,%·･+\-']*", text)
    value = " ".join(item.strip() for item in matches if item.strip())
    return value or None


def build_columns(matrix: list[list[str]], header_count: int) -> list[ColumnDefinition]:
    if not matrix:
        return []

    max_cols = max((len(row) for row in matrix), default=0)
    header_rows = matrix[:header_count] if header_count else []
    columns: list[ColumnDefinition] = []

    for col_index in range(max_cols):
        label_parts: list[str] = []
        english_parts: list[str] = []
        for header_row in header_rows:
            value = header_row[col_index] if col_index < len(header_row) else ""
            label = clean_label(value)
            if label and label not in label_parts:
                label_parts.append(label)
            en = english_label(value)
            if en and en not in english_parts:
                english_parts.append(en)

        label = " / ".join(label_parts) or f"열 {col_index + 1}"
        columns.append(
            ColumnDefinition(
                key=f"c{col_index}",
                label=label,
                label_en=" / ".join(english_parts) if english_parts else None,
                align="left" if col_index == 0 else "right",
                width="24%" if col_index == 0 else None,
            )
        )

    return columns


def build_rows(
    matrix: list[list[str]],
    columns: list[ColumnDefinition],
    header_count: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source_row in matrix[header_count:]:
        if not any(cell for cell in source_row):
            continue

        row: dict[str, Any] = {}
        for col_index, column in enumerate(columns):
            value = source_row[col_index] if col_index < len(source_row) else ""
            row[column.key] = coerce_display_value(value)
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
