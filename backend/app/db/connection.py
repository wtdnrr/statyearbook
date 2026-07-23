from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any

from app.core.env import env_value, load_local_env_file
from app.db.postgres import PostgresConnection


DB_PATH = Path(__file__).with_name("annual_statistics.sqlite")
SUPPORTED_DATABASE_BACKENDS = {"auto", "sqlite", "postgres"}
DatabaseConnection = sqlite3.Connection | PostgresConnection
DatabaseRow = sqlite3.Row | dict[str, Any]


def configured_database_backend(db_path: Path | None = None) -> str:
    """Resolve the database backend without making a network connection.

    Explicit temporary paths always use SQLite so tests and one-off imports do
    not accidentally write to the production database. The default ``auto``
    mode preserves deployment compatibility by using PostgreSQL when
    ``DATABASE_URL`` exists and SQLite otherwise.
    """

    load_local_env_file()
    resolved_path = Path(db_path) if db_path is not None else DB_PATH
    if resolved_path != DB_PATH:
        return "sqlite"

    backend = env_value("DATABASE_BACKEND", default="auto").lower()
    if backend not in SUPPORTED_DATABASE_BACKENDS:
        supported = ", ".join(sorted(SUPPORTED_DATABASE_BACKENDS))
        raise RuntimeError(
            f"DATABASE_BACKEND 값이 올바르지 않습니다: {backend!r}. "
            f"사용 가능 값: {supported}"
        )
    if backend == "auto":
        return "postgres" if env_value("DATABASE_URL") else "sqlite"
    return backend


def connect(db_path: Path | None = None) -> DatabaseConnection:
    backend = configured_database_backend(db_path)
    if backend == "postgres":
        database_url = env_value("DATABASE_URL")
        if not database_url:
            raise RuntimeError(
                "DATABASE_BACKEND=postgres 이지만 DATABASE_URL이 설정되지 않았습니다."
            )
        return PostgresConnection(database_url)

    resolved_path = Path(db_path) if db_path is not None else DB_PATH
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(resolved_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection
