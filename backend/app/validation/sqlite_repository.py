from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sqlite3

from app.db.schema import DB_PATH, connect, init_db
from app.validation.models import (
    ValidationCell,
    ValidationCheckRecord,
    ValidationIssueRecord,
    ValidationTable,
)


class SQLiteValidationRepository:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db_path = db_path

    def latest_report(self) -> sqlite3.Row | None:
        with connect(self._db_path) as connection:
            init_db(connection)
            return connection.execute(
                """
                SELECT *
                FROM annual_reports
                ORDER BY imported_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()

    def load_tables(self, report_id: int) -> list[ValidationTable]:
        with connect(self._db_path) as connection:
            init_db(connection)
            table_rows = connection.execute(
                """
                SELECT id, report_id, code, title, unit, base_date, source, note
                FROM stat_tables
                WHERE report_id = ?
                ORDER BY table_order, id
                """,
                (report_id,),
            ).fetchall()

            return [self._load_table(connection, table_row) for table_row in table_rows]

    def save_run(
        self,
        *,
        report_id: int,
        rules_version: str,
        issues: list[ValidationIssueRecord],
        checks: list[ValidationCheckRecord] | None = None,
    ) -> int:
        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        completed_at = started_at
        check_records = checks or []

        with connect(self._db_path) as connection:
            init_db(connection)
            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO validation_runs (
                        report_id, rules_version, started_at, completed_at, issue_count
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (report_id, rules_version, started_at, completed_at, len(issues)),
                )
                run_id = cursor.lastrowid

                connection.executemany(
                    """
                    INSERT INTO validation_issues (
                        run_id, table_id, rule_id, issue_type, location, row_index,
                        col_index, current_value, expected_value, difference, status,
                        severity, detail, formula
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            run_id,
                            issue.table_id,
                            issue.rule_id,
                            issue.issue_type,
                            issue.location,
                            issue.row_index,
                            issue.col_index,
                            issue.current_value,
                            issue.expected_value,
                            issue.difference,
                            issue.status,
                            issue.severity,
                            issue.detail,
                            issue.formula,
                        )
                        for issue in issues
                    ],
                )
                connection.executemany(
                    """
                    INSERT INTO validation_checks (
                        run_id, table_id, profile_id, rule_id, check_type, check_label,
                        location, row_index, col_index, current_value, expected_value,
                        difference, status, severity, detail, formula, confidence
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            run_id,
                            check.table_id,
                            check.profile_id,
                            check.rule_id,
                            check.check_type,
                            check.check_label,
                            check.location,
                            check.row_index,
                            check.col_index,
                            check.current_value,
                            check.expected_value,
                            check.difference,
                            check.status,
                            check.severity,
                            check.detail,
                            check.formula,
                            check.confidence,
                        )
                        for check in check_records
                    ],
                )

        return int(run_id)

    def _load_table(
        self,
        connection: sqlite3.Connection,
        table_row: sqlite3.Row,
    ) -> ValidationTable:
        cells = connection.execute(
            """
            SELECT row_index, col_index, text_value, numeric_value, is_header
            FROM stat_table_cells
            WHERE table_id = ?
            ORDER BY row_index, col_index
            """,
            (table_row["id"],),
        ).fetchall()

        return ValidationTable(
            id=table_row["id"],
            report_id=table_row["report_id"],
            code=table_row["code"],
            title=table_row["title"],
            unit=table_row["unit"],
            base_date=table_row["base_date"],
            source=table_row["source"],
            note=table_row["note"],
            cells=[
                ValidationCell(
                    row_index=cell["row_index"],
                    col_index=cell["col_index"],
                    text_value=cell["text_value"],
                    numeric_value=cell["numeric_value"],
                    is_header=bool(cell["is_header"]),
                )
                for cell in cells
            ],
        )
