from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, TypeVar

from app.core.env import env_value, load_local_env_file
from app.db.postgres import PostgresConnection
from app.db.schema import DB_PATH, init_db


COPY_TABLES = (
    "annual_reports",
    "validation_profiles",
    "stat_tables",
    "stat_table_cells",
    "validation_runs",
    "validation_checks",
    "validation_issues",
)

SEQUENCE_TABLES = (
    "annual_reports",
    "stat_tables",
    "stat_table_cells",
    "validation_profiles",
    "validation_runs",
    "validation_checks",
    "validation_issues",
)

T = TypeVar("T")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy one annual report and one validation run from local SQLite to PostgreSQL.",
    )
    parser.add_argument("--report-id", type=int, required=True)
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--sqlite-db", type=Path, default=DB_PATH)
    parser.add_argument(
        "--target-url",
        default="",
        help="PostgreSQL URL. Defaults to TARGET_DATABASE_URL, then DATABASE_URL.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the copy plan without writing to PostgreSQL.",
    )
    args = parser.parse_args()

    load_local_env_file()
    target_url = args.target_url or env_value("TARGET_DATABASE_URL") or env_value("DATABASE_URL")
    if not target_url and not args.dry_run:
        raise SystemExit(
            "TARGET_DATABASE_URL 또는 DATABASE_URL 환경변수에 Railway PostgreSQL URL을 넣어주세요."
        )

    with source_connection(args.sqlite_db) as source:
        package = build_copy_package(source, report_id=args.report_id, run_id=args.run_id)
        print_plan(package)

        if args.dry_run:
            return

        target = PostgresConnection(target_url)
        try:
            init_db(target)
            replace_report_package(target, package)
            target.commit()
        except Exception:
            target.rollback()
            raise
        finally:
            target.close()

    print("PostgreSQL 업로드가 완료되었습니다.")


def source_connection(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def build_copy_package(
    source: sqlite3.Connection,
    *,
    report_id: int,
    run_id: int | None,
) -> dict[str, Any]:
    report = fetch_one(source, "SELECT * FROM annual_reports WHERE id = ?", (report_id,))
    if report is None:
        raise SystemExit(f"annual_reports.id={report_id} 데이터를 찾지 못했습니다.")

    resolved_run_id = run_id or latest_run_id(source, report_id)
    if resolved_run_id is None:
        raise SystemExit(f"report_id={report_id}의 validation run을 찾지 못했습니다.")

    run = fetch_one(source, "SELECT * FROM validation_runs WHERE id = ?", (resolved_run_id,))
    if run is None or int(run["report_id"]) != report_id:
        raise SystemExit(f"validation_runs.id={resolved_run_id}가 report_id={report_id}에 속하지 않습니다.")

    table_ids = [
        int(row["id"])
        for row in source.execute(
            "SELECT id FROM stat_tables WHERE report_id = ? ORDER BY table_order, id",
            (report_id,),
        ).fetchall()
    ]
    profile_ids = [
        int(row["profile_id"])
        for row in source.execute(
            """
            SELECT DISTINCT profile_id
            FROM validation_checks
            WHERE run_id = ? AND profile_id IS NOT NULL
            ORDER BY profile_id
            """,
            (resolved_run_id,),
        ).fetchall()
    ]

    rows_by_table: dict[str, list[dict[str, Any]]] = {
        "annual_reports": [dict(report)],
        "validation_runs": [dict(run)],
        "stat_tables": select_in(source, "stat_tables", "id", table_ids),
        "stat_table_cells": select_in(source, "stat_table_cells", "table_id", table_ids),
        "validation_checks": select_where(source, "validation_checks", "run_id = ?", (resolved_run_id,)),
        "validation_issues": select_where(source, "validation_issues", "run_id = ?", (resolved_run_id,)),
        "validation_profiles": select_in(source, "validation_profiles", "id", profile_ids),
    }
    columns_by_table = {table: source_columns(source, table) for table in COPY_TABLES}

    return {
        "report": dict(report),
        "run": dict(run),
        "table_ids": table_ids,
        "profile_ids": profile_ids,
        "rows_by_table": rows_by_table,
        "columns_by_table": columns_by_table,
    }


def latest_run_id(source: sqlite3.Connection, report_id: int) -> int | None:
    row = fetch_one(
        source,
        """
        SELECT id
        FROM validation_runs
        WHERE report_id = ?
        ORDER BY completed_at DESC, id DESC
        LIMIT 1
        """,
        (report_id,),
    )
    return int(row["id"]) if row else None


def fetch_one(
    connection: sqlite3.Connection,
    sql: str,
    params: Sequence[Any] = (),
) -> sqlite3.Row | None:
    return connection.execute(sql, params).fetchone()


def select_where(
    source: sqlite3.Connection,
    table: str,
    where_sql: str,
    params: Sequence[Any],
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in source.execute(f"SELECT * FROM {table} WHERE {where_sql}", params).fetchall()
    ]


def select_in(
    source: sqlite3.Connection,
    table: str,
    column: str,
    values: Sequence[int],
) -> list[dict[str, Any]]:
    if not values:
        return []
    output: list[dict[str, Any]] = []
    for chunk_values in chunks(values, 800):
        placeholders = ", ".join("?" for _ in chunk_values)
        output.extend(
            dict(row)
            for row in source.execute(
                f"SELECT * FROM {table} WHERE {column} IN ({placeholders})",
                chunk_values,
            ).fetchall()
        )
    return output


def source_columns(source: sqlite3.Connection, table: str) -> list[str]:
    return [str(row["name"]) for row in source.execute(f"PRAGMA table_info({table})").fetchall()]


def replace_report_package(target: PostgresConnection, package: dict[str, Any]) -> None:
    report = package["report"]
    run = package["run"]
    profile_rows = package["rows_by_table"]["validation_profiles"]

    delete_existing_target_rows(target, report, run, profile_rows)

    insert_rows(target, package, "annual_reports")
    insert_rows(target, package, "validation_profiles")
    insert_rows(target, package, "stat_tables")
    insert_rows(target, package, "stat_table_cells")
    insert_rows(target, package, "validation_runs")
    insert_rows(target, package, "validation_checks")
    insert_rows(target, package, "validation_issues")
    reset_postgres_sequences(target)


def delete_existing_target_rows(
    target: PostgresConnection,
    report: dict[str, Any],
    run: dict[str, Any],
    profile_rows: list[dict[str, Any]],
) -> None:
    report_id = int(report["id"])
    run_id = int(run["id"])

    target.execute("DELETE FROM validation_issues WHERE run_id = ?", (run_id,))
    target.execute("DELETE FROM validation_checks WHERE run_id = ?", (run_id,))
    target.execute("DELETE FROM linguistic_review_candidates WHERE run_id = ?", (run_id,))
    target.execute("DELETE FROM validation_runs WHERE id = ?", (run_id,))
    target.execute(
        "DELETE FROM stat_table_cells WHERE table_id IN (SELECT id FROM stat_tables WHERE report_id = ?)",
        (report_id,),
    )
    target.execute("DELETE FROM stat_tables WHERE report_id = ?", (report_id,))

    for profile in profile_rows:
        target.execute(
            """
            DELETE FROM validation_profiles
            WHERE id = ?
               OR (
                   table_code = ?
                   AND structure_signature = ?
               )
            """,
            (
                int(profile["id"]),
                profile["table_code"],
                profile["structure_signature"],
            ),
        )

    target.execute(
        """
        DELETE FROM annual_reports
        WHERE id = ?
           OR file_hash = ?
           OR (
               year = ?
               AND title = ?
               AND source_file_name = ?
           )
        """,
        (
            report_id,
            report["file_hash"],
            int(report["year"]),
            report["title"],
            report["source_file_name"],
        ),
    )


def insert_rows(target: PostgresConnection, package: dict[str, Any], table: str) -> None:
    rows = package["rows_by_table"][table]
    if not rows:
        return

    columns = package["columns_by_table"][table]
    placeholders = ", ".join("?" for _ in columns)
    column_list = ", ".join(columns)
    sql = f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})"
    values = [tuple(row.get(column) for column in columns) for row in rows]

    for batch in chunks(values, 1000):
        target.executemany(sql, batch)


def reset_postgres_sequences(target: PostgresConnection) -> None:
    for table in SEQUENCE_TABLES:
        target.execute(
            f"""
            SELECT setval(
                pg_get_serial_sequence('{table}', 'id'),
                COALESCE((SELECT MAX(id) FROM {table}), 1),
                (SELECT MAX(id) IS NOT NULL FROM {table})
            )
            """
        )


def print_plan(package: dict[str, Any]) -> None:
    report = package["report"]
    run = package["run"]
    rows_by_table = package["rows_by_table"]
    print(
        f"업로드 대상: report_id={report['id']} "
        f"{report['year']} {report['title']} / run_id={run['id']}"
    )
    for table in COPY_TABLES:
        print(f"- {table}: {len(rows_by_table[table]):,} rows")


def chunks(values: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


if __name__ == "__main__":
    main()
