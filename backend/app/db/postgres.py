from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
import re
from typing import Any


LASTROWID_TABLES = {
    "annual_reports",
    "stat_tables",
    "validation_runs",
    "report_processing_jobs",
    "validation_profiles",
}


class EmptyCursor:
    lastrowid: int | None = None
    rowcount = 0

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list[Any]:
        return []

    def __iter__(self) -> Iterator[Any]:
        return iter(())


class PostgresCursor:
    def __init__(self, cursor: Any, *, lastrowid: int | None = None) -> None:
        self._cursor = cursor
        self.lastrowid = lastrowid

    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount)

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchall(self) -> list[Any]:
        return list(self._cursor.fetchall())

    def __iter__(self) -> Iterator[Any]:
        return iter(self._cursor)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class PostgresConnection:
    """Small DB-API compatibility wrapper for the app's SQLite-shaped queries."""

    is_postgres = True

    def __init__(self, database_url: str) -> None:
        import psycopg
        from psycopg.rows import dict_row

        self._connection = psycopg.connect(database_url, row_factory=dict_row)
        self._context_depth = 0

    def execute(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
    ) -> PostgresCursor | EmptyCursor:
        translated = translate_sql(sql)
        if translated is None:
            return EmptyCursor()
        sql_text, should_return_id = translated
        cursor = self._connection.execute(sql_text, params or ())
        lastrowid = None
        if should_return_id:
            row = cursor.fetchone()
            if row is not None:
                lastrowid = int(row["id"])
        return PostgresCursor(cursor, lastrowid=lastrowid)

    def executemany(
        self,
        sql: str,
        params_seq: Iterable[Sequence[Any]],
    ) -> PostgresCursor | EmptyCursor:
        translated = translate_sql(sql, returning_id=False)
        if translated is None:
            return EmptyCursor()
        sql_text, _ = translated
        cursor = self._connection.cursor()
        cursor.executemany(sql_text, params_seq)
        return PostgresCursor(cursor)

    def executescript(self, script: str) -> None:
        for statement in split_sql_script(script):
            if statement.strip():
                self.execute(statement)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> PostgresConnection:
        self._context_depth += 1
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._context_depth -= 1
        if self._context_depth > 0:
            return
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()


def translate_sql(sql: str, *, returning_id: bool | None = None) -> tuple[str, bool] | None:
    text = sql.strip().rstrip(";")
    if not text:
        return None
    if text.upper().startswith("PRAGMA"):
        return None

    text = re.sub(
        r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
        "SERIAL PRIMARY KEY",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", text, flags=re.IGNORECASE)
    text = re.sub(r"\bchar\s*\(", "chr(", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b([A-Za-z_][A-Za-z0-9_.]*)\s+IS\s+\?",
        r"\1 IS NOT DISTINCT FROM ?",
        text,
        flags=re.IGNORECASE,
    )

    is_insert_ignore = re.search(r"\bINSERT\s+INTO\b", text, flags=re.IGNORECASE) and (
        "ON CONFLICT" not in text.upper()
        and re.search(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", sql, flags=re.IGNORECASE)
    )
    if is_insert_ignore:
        text = f"{text} ON CONFLICT DO NOTHING"

    should_return_id = wants_returning_id(text) if returning_id is None else returning_id
    if should_return_id and " RETURNING " not in text.upper():
        text = f"{text} RETURNING id"
    return replace_qmark_placeholders(text), should_return_id


def wants_returning_id(sql: str) -> bool:
    match = re.match(r"\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\b", sql, flags=re.IGNORECASE)
    return bool(match and match.group(1).lower() in LASTROWID_TABLES)


def replace_qmark_placeholders(sql: str) -> str:
    output: list[str] = []
    in_single = False
    in_double = False
    index = 0
    while index < len(sql):
        char = sql[index]
        if char == "'" and not in_double:
            output.append(char)
            if index + 1 < len(sql) and sql[index + 1] == "'":
                output.append(sql[index + 1])
                index += 2
                continue
            in_single = not in_single
        elif char == '"' and not in_single:
            output.append(char)
            in_double = not in_double
        elif char == "?" and not in_single and not in_double:
            output.append("%s")
        else:
            output.append(char)
        index += 1
    return "".join(output)


def split_sql_script(script: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    for char in script:
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        if char == ";" and not in_single and not in_double:
            statements.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        statements.append("".join(current).strip())
    return statements
