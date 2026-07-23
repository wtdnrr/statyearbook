from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.db.connection import DB_PATH, configured_database_backend, connect


class DatabaseConnectionTest(unittest.TestCase):
    def test_auto_uses_sqlite_without_database_url(self) -> None:
        with patch("app.db.connection.load_local_env_file"), patch.dict(
            os.environ,
            {"DATABASE_BACKEND": "auto"},
            clear=True,
        ):
            self.assertEqual(configured_database_backend(), "sqlite")

    def test_auto_uses_postgres_when_database_url_exists(self) -> None:
        with patch("app.db.connection.load_local_env_file"), patch.dict(
            os.environ,
            {
                "DATABASE_BACKEND": "auto",
                "DATABASE_URL": "postgresql://example.invalid/report",
            },
            clear=True,
        ):
            self.assertEqual(configured_database_backend(), "postgres")

    def test_explicit_sqlite_backend_overrides_database_url(self) -> None:
        with patch("app.db.connection.load_local_env_file"), patch.dict(
            os.environ,
            {
                "DATABASE_BACKEND": "sqlite",
                "DATABASE_URL": "postgresql://example.invalid/report",
            },
            clear=True,
        ):
            self.assertEqual(configured_database_backend(DB_PATH), "sqlite")

    def test_explicit_temporary_path_is_always_sqlite(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "app.db.connection.load_local_env_file"
        ), patch.dict(
            os.environ,
            {
                "DATABASE_BACKEND": "postgres",
                "DATABASE_URL": "postgresql://example.invalid/report",
            },
            clear=True,
        ):
            db_path = Path(directory) / "test.sqlite"
            connection = connect(db_path)
            try:
                self.assertEqual(configured_database_backend(db_path), "sqlite")
                self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
