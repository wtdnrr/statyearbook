from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3

from app.core.env import load_local_env_file
from app.db.schema import DB_PATH, connect, init_db
from app.validation.models import ValidationTable
from app.validation.profiles import (
    COMMON_RULE_IDS,
    GPTProfileDraftProvider,
    PROFILE_VERSION,
    HeuristicProfileDraftProvider,
    ProfileDraft,
    ProfileDraftProvider,
    ValidationProfile,
    common_check_specs,
    outlier_check_specs,
    structure_signature,
)
from app.validation.catalog import rule_definition_payload


class SQLiteValidationProfileRepository:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db_path = db_path

    def ensure_profiles(
        self,
        *,
        report_id: int,
        tables: list[ValidationTable],
        provider: ProfileDraftProvider | None = None,
        refresh: bool = False,
    ) -> dict[str, ValidationProfile]:
        draft_provider = provider or default_profile_draft_provider()
        profiles: dict[str, ValidationProfile] = {}

        with connect(self._db_path) as connection:
            init_db(connection)
            with connection:
                for table in tables:
                    signature = structure_signature(table)
                    profile = self._profile_by_code_signature(connection, table.code, signature)
                    inherited_profile = None
                    if profile is None:
                        inherited_profile = self._profile_by_title_signature(
                            connection,
                            table.title,
                            signature,
                        )
                    previous_profile = self._latest_profile_by_code(connection, table.code)
                    if previous_profile is None:
                        previous_profile = self._latest_profile_by_title(connection, table.title)
                    if profile is None or refresh:
                        try:
                            draft = (
                                inherited_profile_draft(table, inherited_profile)
                                if inherited_profile is not None and not refresh
                                else draft_provider.draft(table, previous_profile=previous_profile)
                            )
                        except Exception as error:
                            draft = fallback_profile_draft(table, error)
                        if profile is None:
                            profile_id = self._insert_profile(
                                connection,
                                report_id=report_id,
                                draft=draft,
                            )
                            profile = self._profile_by_id(connection, profile_id)
                        else:
                            self._update_profile(
                                connection,
                                profile_id=profile.id,
                                report_id=report_id,
                                draft=draft,
                            )
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

    def _profile_by_title_signature(
        self,
        connection: sqlite3.Connection,
        table_title: str,
        signature: str,
    ) -> ValidationProfile | None:
        row = connection.execute(
            """
            SELECT *
            FROM validation_profiles
            WHERE table_title = ? AND structure_signature = ?
            ORDER BY
                CASE status WHEN 'approved' THEN 0 WHEN 'ready' THEN 1 ELSE 2 END,
                updated_at DESC,
                id DESC
            LIMIT 1
            """,
            (table_title, signature),
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
                approved_at = NULL,
                approved_by = NULL,
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


def inherited_profile_draft(
    table: ValidationTable,
    profile: ValidationProfile,
) -> ProfileDraft:
    """Reuse a structurally identical profile when only the table code moved."""

    rules = json.loads(json.dumps(profile.rules, ensure_ascii=False))
    rebind_inherited_rule_ids(
        rules,
        previous_table_code=profile.table_code,
        table_code=table.code,
    )
    return ProfileDraft(
        table_code=table.code,
        table_title=table.title,
        structure_signature=structure_signature(table),
        table_type=profile.table_type,
        status="ready" if profile.status == "approved" else profile.status,
        source="inherited",
        rules=rules,
        notes=f"동일한 제목과 구조를 가진 {profile.table_code} 프로파일을 자동 승계했습니다.",
        llm_model=profile.llm_model,
    )


def fallback_profile_draft(table: ValidationTable, error: Exception) -> ProfileDraft:
    """Keep one malformed table from aborting the complete annual report run."""

    checks = [*common_check_specs(table), *outlier_check_specs(table)]
    return ProfileDraft(
        table_code=table.code,
        table_title=table.title,
        structure_signature=structure_signature(table),
        table_type="general",
        status="needs_review",
        source="fallback",
        rules={
            "version": PROFILE_VERSION,
            "rule_definitions": rule_definition_payload(),
            "common_rules": COMMON_RULE_IDS,
            "templates": [],
            "analysis": {},
            "checks": checks,
            "table_rules": [],
            "requires_llm_review": True,
        },
        notes=f"표별 프로파일 생성 실패로 공통 검수만 적용했습니다: {str(error)[:300]}",
    )


def default_profile_draft_provider() -> ProfileDraftProvider:
    load_local_env_file()
    enabled = os.getenv("PROFILE_LLM_ENABLED", "0").strip().lower()
    if enabled in {"1", "true", "yes", "on"}:
        return GPTProfileDraftProvider(
            model=os.getenv("PROFILE_LLM_MODEL", "gpt-5-mini").strip() or "gpt-5-mini"
        )
    return HeuristicProfileDraftProvider()


def rebind_inherited_rule_ids(
    rules: dict[str, object],
    *,
    previous_table_code: str,
    table_code: str,
) -> None:
    """Give an inherited profile IDs that identify its new table number."""

    if previous_table_code == table_code:
        return
    old_prefix = f"profile.{previous_table_code}."
    new_prefix = f"profile.{table_code}."
    for collection_name in ("checks", "table_rules"):
        collection = rules.get(collection_name)
        if not isinstance(collection, list):
            continue
        for spec in collection:
            if not isinstance(spec, dict):
                continue
            rule_id = spec.get("id")
            if isinstance(rule_id, str) and rule_id.startswith(old_prefix):
                spec["id"] = f"{new_prefix}{rule_id.removeprefix(old_prefix)}"
