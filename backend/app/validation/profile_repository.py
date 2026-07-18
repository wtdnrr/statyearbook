from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import sqlite3

from app.db.schema import DB_PATH, connect, init_db
from app.validation.models import ValidationTable
from app.validation.profiles import (
    HeuristicProfileDraftProvider,
    ProfileDraft,
    ProfileDraftProvider,
    ValidationProfile,
    structure_signature,
)
from app.validation.curated_profiles import curated_profiles


class SQLiteValidationProfileRepository:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db_path = db_path

    def ensure_profiles(
        self,
        *,
        report_id: int,
        tables: list[ValidationTable],
        provider: ProfileDraftProvider | None = None,
    ) -> dict[str, ValidationProfile]:
        draft_provider = provider or HeuristicProfileDraftProvider()
        profiles: dict[str, ValidationProfile] = {}

        with connect(self._db_path) as connection:
            init_db(connection)
            with connection:
                for table in tables:
                    signature = structure_signature(table)
                    profile = self._profile_by_code_signature(connection, table.code, signature)
                    previous_profile = self._latest_profile_by_code(connection, table.code)
                    if previous_profile is None:
                        previous_profile = self._latest_profile_by_title(connection, table.title)
                    if profile is None:
                        draft = draft_provider.draft(table, previous_profile=previous_profile)
                        profile_id = self._insert_profile(connection, report_id=report_id, draft=draft)
                        profile = self._profile_by_id(connection, profile_id)
                    elif profile.status != "approved":
                        draft = draft_provider.draft(table, previous_profile=previous_profile)
                        self._update_profile(connection, profile_id=profile.id, report_id=report_id, draft=draft)
                        profile = self._profile_by_id(connection, profile.id)
                    if profile is not None:
                        profiles[table.code] = profile

        return profiles

    def list_profiles(self) -> list[ValidationProfile]:
        with connect(self._db_path) as connection:
            init_db(connection)
            rows = connection.execute(
                """
                SELECT *
                FROM validation_profiles
                ORDER BY table_code, updated_at DESC, id DESC
                """
            ).fetchall()
            return [row_to_profile(row) for row in rows]

    def refresh_curated_profiles(
        self,
        *,
        report_id: int,
        tables: list[ValidationTable],
        provider: ProfileDraftProvider | None = None,
    ) -> dict[str, ValidationProfile]:
        """Persist the currently maintained curated rules for this report.

        Curated profiles are authored in source control, but the validation
        engine always reads profiles from SQLite. This explicit refresh keeps
        those two layers in sync without replacing unrelated, manually
        approved profiles.
        """

        curated_codes = set(curated_profiles())
        if not curated_codes:
            return {}

        draft_provider = provider or HeuristicProfileDraftProvider()
        profiles: dict[str, ValidationProfile] = {}
        with connect(self._db_path) as connection:
            init_db(connection)
            with connection:
                for table in tables:
                    if table.code not in curated_codes:
                        continue

                    signature = structure_signature(table)
                    current = self._profile_by_code_signature(connection, table.code, signature)
                    previous = current or self._latest_profile_by_code(connection, table.code)
                    draft = draft_provider.draft(table, previous_profile=previous)
                    draft = replace(draft, source="curated")

                    if current is None:
                        profile_id = self._insert_profile(connection, report_id=report_id, draft=draft)
                        current = self._profile_by_id(connection, profile_id)
                    else:
                        self._update_profile(
                            connection,
                            profile_id=current.id,
                            report_id=report_id,
                            draft=draft,
                        )
                        current = self._profile_by_id(connection, current.id)

                    if current is not None:
                        profiles[table.code] = current

        return profiles

    def approve_profile(self, profile_id: int, *, approved_by: str = "담당자") -> ValidationProfile | None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with connect(self._db_path) as connection:
            init_db(connection)
            with connection:
                connection.execute(
                    """
                    UPDATE validation_profiles
                    SET status = 'approved',
                        approved_at = ?,
                        approved_by = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, approved_by, now, profile_id),
                )
            return self._profile_by_id(connection, profile_id)

    def _profile_by_code_signature(
        self,
        connection: sqlite3.Connection,
        table_code: str,
        signature: str,
    ) -> ValidationProfile | None:
        row = connection.execute(
            """
            SELECT *
            FROM validation_profiles
            WHERE table_code = ? AND structure_signature = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (table_code, signature),
        ).fetchone()
        return row_to_profile(row) if row else None

    def _latest_profile_by_code(
        self,
        connection: sqlite3.Connection,
        table_code: str,
    ) -> ValidationProfile | None:
        row = connection.execute(
            """
            SELECT *
            FROM validation_profiles
            WHERE table_code = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (table_code,),
        ).fetchone()
        return row_to_profile(row) if row else None

    def _latest_profile_by_title(
        self,
        connection: sqlite3.Connection,
        table_title: str,
    ) -> ValidationProfile | None:
        row = connection.execute(
            """
            SELECT *
            FROM validation_profiles
            WHERE table_title = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (table_title,),
        ).fetchone()
        return row_to_profile(row) if row else None

    def _profile_by_id(
        self,
        connection: sqlite3.Connection,
        profile_id: int,
    ) -> ValidationProfile | None:
        row = connection.execute(
            "SELECT * FROM validation_profiles WHERE id = ?",
            (profile_id,),
        ).fetchone()
        return row_to_profile(row) if row else None

    def _insert_profile(
        self,
        connection: sqlite3.Connection,
        *,
        report_id: int,
        draft: ProfileDraft,
    ) -> int:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = connection.execute(
            """
            INSERT INTO validation_profiles (
                table_code, table_title, source_report_id, structure_signature,
                table_type, status, source, llm_model, rules_json, notes,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                draft.table_code,
                draft.table_title,
                report_id,
                draft.structure_signature,
                draft.table_type,
                draft.status,
                draft.source,
                draft.llm_model,
                json.dumps(draft.rules, ensure_ascii=False, sort_keys=True),
                draft.notes,
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)

    def _update_profile(
        self,
        connection: sqlite3.Connection,
        *,
        profile_id: int,
        report_id: int,
        draft: ProfileDraft,
    ) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        connection.execute(
            """
            UPDATE validation_profiles
            SET table_title = ?,
                source_report_id = ?,
                structure_signature = ?,
                table_type = ?,
                status = ?,
                source = ?,
                llm_model = ?,
                rules_json = ?,
                notes = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                draft.table_title,
                report_id,
                draft.structure_signature,
                draft.table_type,
                draft.status,
                draft.source,
                draft.llm_model,
                json.dumps(draft.rules, ensure_ascii=False, sort_keys=True),
                draft.notes,
                now,
                profile_id,
            ),
        )


def row_to_profile(row: sqlite3.Row) -> ValidationProfile:
    rules_json = row["rules_json"] or "{}"
    try:
        rules = json.loads(rules_json)
    except json.JSONDecodeError:
        rules = {}

    return ValidationProfile(
        id=int(row["id"]),
        table_code=row["table_code"],
        table_title=row["table_title"],
        source_report_id=row["source_report_id"],
        structure_signature=row["structure_signature"],
        table_type=row["table_type"],
        status=row["status"],
        source=row["source"],
        llm_model=row["llm_model"],
        rules=rules,
        notes=row["notes"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        approved_at=row["approved_at"],
        approved_by=row["approved_by"],
    )
