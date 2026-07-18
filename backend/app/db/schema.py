from pathlib import Path
import json
import re
import sqlite3
from typing import Callable

from app.validation.catalog import REQUIRED_RULE_DEFINITIONS


DB_PATH = Path(__file__).with_name("annual_statistics.sqlite")


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    key TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS annual_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year INTEGER NOT NULL,
    title TEXT NOT NULL,
    source_file_name TEXT NOT NULL,
    source_file_path TEXT NOT NULL,
    file_hash TEXT NOT NULL UNIQUE,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stat_tables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    title TEXT NOT NULL,
    title_en TEXT NOT NULL DEFAULT '',
    section_title TEXT NOT NULL DEFAULT '',
    section_title_en TEXT NOT NULL DEFAULT '',
    domain TEXT NOT NULL DEFAULT '',
    unit TEXT NOT NULL DEFAULT '',
    base_date TEXT NOT NULL DEFAULT '',
    section_file TEXT NOT NULL DEFAULT '',
    table_order INTEGER NOT NULL DEFAULT 0,
    cell_range TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    extracted_at TEXT NOT NULL DEFAULT '',
    raw_context TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (report_id) REFERENCES annual_reports(id) ON DELETE CASCADE,
    UNIQUE (report_id, code)
);

CREATE TABLE IF NOT EXISTS stat_table_cells (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_id INTEGER NOT NULL,
    row_index INTEGER NOT NULL,
    col_index INTEGER NOT NULL,
    text_value TEXT NOT NULL DEFAULT '',
    numeric_value REAL,
    is_header INTEGER NOT NULL DEFAULT 0,
    footnote_marker TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (table_id) REFERENCES stat_tables(id) ON DELETE CASCADE,
    UNIQUE (table_id, row_index, col_index)
);

CREATE INDEX IF NOT EXISTS idx_stat_tables_report_order
ON stat_tables(report_id, table_order);

CREATE INDEX IF NOT EXISTS idx_stat_table_cells_table_position
ON stat_table_cells(table_id, row_index, col_index);

CREATE TABLE IF NOT EXISTS validation_rule_definitions (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    default_status TEXT NOT NULL,
    default_severity TEXT NOT NULL,
    owner_role TEXT NOT NULL,
    description TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS validation_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_code TEXT NOT NULL,
    table_title TEXT NOT NULL DEFAULT '',
    source_report_id INTEGER,
    structure_signature TEXT NOT NULL,
    table_type TEXT NOT NULL DEFAULT 'general',
    status TEXT NOT NULL DEFAULT 'draft',
    source TEXT NOT NULL DEFAULT 'heuristic',
    llm_model TEXT,
    rules_json TEXT NOT NULL DEFAULT '{}',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    approved_at TEXT,
    approved_by TEXT,
    FOREIGN KEY (source_report_id) REFERENCES annual_reports(id) ON DELETE SET NULL,
    UNIQUE (table_code, structure_signature)
);

CREATE INDEX IF NOT EXISTS idx_validation_profiles_code_latest
ON validation_profiles(table_code, updated_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS validation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL,
    rules_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    issue_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (report_id) REFERENCES annual_reports(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS validation_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    table_id INTEGER NOT NULL,
    rule_id TEXT NOT NULL,
    issue_type TEXT NOT NULL,
    location TEXT NOT NULL,
    row_index INTEGER,
    col_index INTEGER,
    current_value TEXT NOT NULL DEFAULT '',
    expected_value TEXT,
    difference TEXT,
    status TEXT NOT NULL DEFAULT '확인 필요',
    severity TEXT NOT NULL DEFAULT 'warning',
    detail TEXT NOT NULL,
    formula TEXT,
    FOREIGN KEY (run_id) REFERENCES validation_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (table_id) REFERENCES stat_tables(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_validation_runs_report_latest
ON validation_runs(report_id, completed_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_validation_issues_run_table
ON validation_issues(run_id, table_id);

CREATE TABLE IF NOT EXISTS validation_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    table_id INTEGER NOT NULL,
    profile_id INTEGER,
    rule_id TEXT NOT NULL,
    check_type TEXT NOT NULL,
    check_label TEXT NOT NULL,
    location TEXT NOT NULL,
    row_index INTEGER,
    col_index INTEGER,
    current_value TEXT NOT NULL DEFAULT '',
    expected_value TEXT,
    difference TEXT,
    status TEXT NOT NULL DEFAULT '정상',
    severity TEXT NOT NULL DEFAULT 'info',
    detail TEXT NOT NULL,
    formula TEXT,
    confidence REAL,
    FOREIGN KEY (run_id) REFERENCES validation_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (table_id) REFERENCES stat_tables(id) ON DELETE CASCADE,
    FOREIGN KEY (profile_id) REFERENCES validation_profiles(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_validation_checks_run_table
ON validation_checks(run_id, table_id);

CREATE TABLE IF NOT EXISTS translation_glossary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origin_key TEXT NOT NULL UNIQUE,
    source_text TEXT NOT NULL,
    source_normalized TEXT NOT NULL,
    target_text TEXT NOT NULL,
    target_normalized TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    subcategory TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT 'yearbook',
    source_report_id INTEGER,
    source_table_id INTEGER,
    status TEXT NOT NULL DEFAULT 'reference',
    priority INTEGER NOT NULL DEFAULT 0,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    evidence TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    source_title TEXT NOT NULL DEFAULT '',
    aliases_json TEXT NOT NULL DEFAULT '[]',
    valid_from TEXT NOT NULL DEFAULT '',
    valid_to TEXT NOT NULL DEFAULT '',
    verified_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (source_report_id) REFERENCES annual_reports(id) ON DELETE SET NULL,
    FOREIGN KEY (source_table_id) REFERENCES stat_tables(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_translation_glossary_source
ON translation_glossary(source_normalized, status, priority DESC);

CREATE TABLE IF NOT EXISTS translation_glossary_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    glossary_id INTEGER NOT NULL,
    alias_text TEXT NOT NULL,
    alias_normalized TEXT NOT NULL,
    alias_kind TEXT NOT NULL DEFAULT 'alias',
    FOREIGN KEY (glossary_id) REFERENCES translation_glossary(id) ON DELETE CASCADE,
    UNIQUE (glossary_id, alias_normalized)
);

CREATE INDEX IF NOT EXISTS idx_translation_glossary_alias
ON translation_glossary_aliases(alias_normalized, glossary_id);

CREATE TABLE IF NOT EXISTS linguistic_review_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    table_id INTEGER NOT NULL,
    review_type TEXT NOT NULL,
    candidate_kind TEXT NOT NULL,
    location TEXT NOT NULL,
    row_index INTEGER,
    col_index INTEGER,
    current_value TEXT NOT NULL,
    korean_text TEXT NOT NULL DEFAULT '',
    english_text TEXT NOT NULL DEFAULT '',
    glossary_json TEXT NOT NULL DEFAULT '[]',
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    prompt_version TEXT NOT NULL DEFAULT '',
    reviewed_model TEXT NOT NULL DEFAULT '',
    review_result_json TEXT NOT NULL DEFAULT '',
    review_fingerprint TEXT NOT NULL DEFAULT '',
    resolution_source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TEXT,
    FOREIGN KEY (run_id) REFERENCES validation_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (table_id) REFERENCES stat_tables(id) ON DELETE CASCADE,
    UNIQUE (run_id, table_id, review_type, location, current_value)
);

CREATE INDEX IF NOT EXISTS idx_linguistic_candidates_pending
ON linguistic_review_candidates(run_id, status, review_type, table_id);

CREATE TABLE IF NOT EXISTS linguistic_review_cache (
    fingerprint TEXT PRIMARY KEY,
    review_type TEXT NOT NULL,
    current_value TEXT NOT NULL,
    context_signature TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    reviewed_model TEXT NOT NULL,
    glossary_fingerprint TEXT NOT NULL DEFAULT '',
    decision_json TEXT NOT NULL,
    use_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_linguistic_review_cache_lookup
ON linguistic_review_cache(review_type, prompt_version, reviewed_model);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    resolved_path = db_path or DB_PATH
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(resolved_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)
    ensure_column(connection, "stat_table_cells", "footnote_marker", "TEXT NOT NULL DEFAULT ''")
    ensure_column(connection, "translation_glossary", "subcategory", "TEXT NOT NULL DEFAULT ''")
    ensure_column(connection, "translation_glossary", "source_url", "TEXT NOT NULL DEFAULT ''")
    ensure_column(connection, "translation_glossary", "source_title", "TEXT NOT NULL DEFAULT ''")
    ensure_column(connection, "translation_glossary", "aliases_json", "TEXT NOT NULL DEFAULT '[]'")
    ensure_column(connection, "translation_glossary", "valid_from", "TEXT NOT NULL DEFAULT ''")
    ensure_column(connection, "translation_glossary", "valid_to", "TEXT NOT NULL DEFAULT ''")
    ensure_column(connection, "translation_glossary", "verified_at", "TEXT NOT NULL DEFAULT ''")
    ensure_column(connection, "linguistic_review_candidates", "prompt_version", "TEXT NOT NULL DEFAULT ''")
    ensure_column(connection, "linguistic_review_candidates", "reviewed_model", "TEXT NOT NULL DEFAULT ''")
    ensure_column(connection, "linguistic_review_candidates", "review_result_json", "TEXT NOT NULL DEFAULT ''")
    ensure_column(connection, "linguistic_review_candidates", "review_fingerprint", "TEXT NOT NULL DEFAULT ''")
    ensure_column(connection, "linguistic_review_candidates", "resolution_source", "TEXT NOT NULL DEFAULT ''")
    apply_data_migration(
        connection,
        "retire_metadata_validation_v2",
        retire_metadata_validation,
    )
    apply_data_migration(
        connection,
        "restore_metadata_presence_validation_v1",
        restore_metadata_validation,
    )
    apply_data_migration(
        connection,
        "repair_misaligned_linguistic_reviews_v1",
        repair_misaligned_linguistic_reviews,
    )
    apply_data_migration(
        connection,
        "restore_semantic_type_qualifiers_v1",
        restore_semantic_type_qualifiers,
    )
    seed_validation_rule_definitions(connection)
    connection.commit()


def ensure_column(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_definition: str,
) -> None:
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table_name})")}
    if column_name in columns:
        return
    connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")


def apply_data_migration(
    connection: sqlite3.Connection,
    key: str,
    migration: Callable[[sqlite3.Connection], None],
) -> None:
    if connection.execute("SELECT 1 FROM schema_migrations WHERE key = ?", (key,)).fetchone():
        return
    migration(connection)
    connection.execute("INSERT INTO schema_migrations (key) VALUES (?)", (key,))


def restore_semantic_type_qualifiers(connection: sqlite3.Connection) -> None:
    """Restore Type A/B header qualifiers removed by an older cell normalizer."""

    qualifier_by_column = {2: "[Type A]", 3: "[Type B]"}
    for col_index, qualifier in qualifier_by_column.items():
        header_text = (
            "출자기관\nGovernment-funded Organizations"
            if col_index == 2
            else "출연기관\nGovernment-funded Organizations"
        )
        connection.execute(
            """
            UPDATE stat_table_cells
            SET text_value = text_value || char(10) || ?
            WHERE row_index = 0
              AND col_index = ?
              AND text_value = ?
              AND table_id IN (
                  SELECT id
                  FROM stat_tables
                  WHERE code = '5-1-10'
              )
            """,
            (qualifier, col_index, header_text),
        )


def retire_metadata_validation(connection: sqlite3.Connection) -> None:
    """Historical cleanup retained so older databases migrate deterministically."""

    retired_rule_filter = "rule_id = 'metadata.required' OR rule_id LIKE '%.metadata_required'"
    connection.execute(
        f"DELETE FROM validation_issues WHERE issue_type = '메타정보 검수' OR {retired_rule_filter}"
    )
    connection.execute(
        f"DELETE FROM validation_checks WHERE check_type = '메타정보 검수' OR {retired_rule_filter}"
    )
    retire_metadata_linguistic_reviews(connection)
    connection.execute(
        """
        UPDATE validation_runs
        SET issue_count = (
            SELECT COUNT(*) FROM validation_issues WHERE validation_issues.run_id = validation_runs.id
        )
        """
    )

    profile_rows = connection.execute(
        """
        SELECT id, rules_json
        FROM validation_profiles
        WHERE rules_json LIKE '%metadata_required%'
           OR rules_json LIKE '%unit_required%'
           OR rules_json LIKE '%\"metadata\"%'
        """
    ).fetchall()
    for row in profile_rows:
        try:
            rules = json.loads(str(row["rules_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        if not isinstance(rules, dict):
            continue
        rules["common_rules"] = [
            key for key in rules.get("common_rules", []) if key != "metadata"
        ]
        rules["rule_definitions"] = [
            definition
            for definition in rules.get("rule_definitions", [])
            if not isinstance(definition, dict) or definition.get("key") != "metadata"
        ]
        for field in ("checks", "table_rules"):
            rules[field] = [
                spec
                for spec in rules.get(field, [])
                if not isinstance(spec, dict)
                or spec.get("type") not in {"unit_required", "metadata_required"}
            ]
        connection.execute(
            "UPDATE validation_profiles SET rules_json = ? WHERE id = ?",
            (json.dumps(rules, ensure_ascii=False), int(row["id"])),
        )


def restore_metadata_validation(connection: sqlite3.Connection) -> None:
    """Restore presence checks while keeping metadata out of language review."""

    retire_metadata_linguistic_reviews(connection)
    retire_stale_linguistic_review_records(connection)
    restore_metadata_profile_specs(connection)
    restore_latest_run_metadata_checks(connection)


def restore_metadata_profile_specs(connection: sqlite3.Connection) -> None:
    metadata_definition = next(
        definition.to_dict()
        for definition in REQUIRED_RULE_DEFINITIONS
        if definition.key == "metadata"
    )
    profile_rows = connection.execute(
        """
        SELECT vp.id, vp.table_code, vp.source_report_id, vp.rules_json,
               COALESCE((
                   SELECT st.unit
                   FROM stat_tables st
                   WHERE st.report_id = vp.source_report_id AND st.code = vp.table_code
                   ORDER BY st.id
                   LIMIT 1
               ), '') AS current_unit
        FROM validation_profiles vp
        """
    ).fetchall()
    for row in profile_rows:
        try:
            rules = json.loads(str(row["rules_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        if not isinstance(rules, dict):
            continue

        common_rules = [
            key for key in rules.get("common_rules", []) if key != "metadata"
        ]
        rules["common_rules"] = [*common_rules, "metadata"]
        rule_definitions = [
            definition
            for definition in rules.get("rule_definitions", [])
            if not isinstance(definition, dict) or definition.get("key") != "metadata"
        ]
        rules["rule_definitions"] = [*rule_definitions, metadata_definition]

        metadata_spec = metadata_profile_spec(
            str(row["table_code"]),
            str(row["current_unit"] or ""),
        )
        checks = [
            spec
            for spec in rules.get("checks", [])
            if not isinstance(spec, dict)
            or spec.get("type") not in {"unit_required", "metadata_required"}
        ]
        rules["checks"] = [metadata_spec, *checks]
        rules["table_rules"] = [
            spec
            for spec in rules.get("table_rules", [])
            if not isinstance(spec, dict)
            or spec.get("type") not in {"unit_required", "metadata_required"}
        ]
        connection.execute(
            "UPDATE validation_profiles SET rules_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (json.dumps(rules, ensure_ascii=False), int(row["id"])),
        )


def metadata_profile_spec(table_code: str, expected_unit: str) -> dict[str, object]:
    return {
        "id": f"profile.{table_code}.metadata_required",
        "type": "metadata_required",
        "category": "common",
        "check_group": "metadata",
        "check_type": "메타정보 검수",
        "failure_status": "확인 필요",
        "severity": "warning",
        "label": "단위·기준일·출처 메타정보 확인",
        "fields": ["unit", "base_date", "source"],
        "expected_unit": expected_unit,
        "confidence": 1.0,
    }


def restore_latest_run_metadata_checks(connection: sqlite3.Connection) -> None:
    latest_runs = connection.execute(
        """
        SELECT vr.id, vr.report_id
        FROM validation_runs vr
        JOIN (
            SELECT report_id, MAX(id) AS run_id
            FROM validation_runs
            GROUP BY report_id
        ) latest ON latest.run_id = vr.id
        """
    ).fetchall()
    for run in latest_runs:
        run_id = int(run["id"])
        report_id = int(run["report_id"])
        connection.execute(
            "DELETE FROM validation_issues WHERE run_id = ? AND issue_type = '메타정보 검수'",
            (run_id,),
        )
        connection.execute(
            "DELETE FROM validation_checks WHERE run_id = ? AND check_type = '메타정보 검수'",
            (run_id,),
        )
        table_rows = connection.execute(
            """
            SELECT st.id, st.code, st.unit, st.base_date, st.source, vp.id AS profile_id
            FROM stat_tables st
            LEFT JOIN validation_profiles vp
              ON vp.source_report_id = st.report_id AND vp.table_code = st.code
            WHERE st.report_id = ?
            ORDER BY st.table_order, st.id
            """,
            (report_id,),
        ).fetchall()
        for table in table_rows:
            metadata = (
                ("단위", str(table["unit"] or "").strip()),
                ("기준일", str(table["base_date"] or "").strip()),
                ("출처", str(table["source"] or "").strip()),
            )
            missing = [label for label, value in metadata if not value]
            current_value = " | ".join(
                f"{label}: {value or '없음'}" for label, value in metadata
            )
            difference = f"누락: {', '.join(missing)}" if missing else None
            status = "확인 필요" if missing else "정상"
            severity = "warning" if missing else "info"
            detail = (
                f"출처·메타정보 탭에서 {', '.join(missing)} 항목이 누락되어 있습니다."
                if missing
                else "출처·메타정보 탭의 단위, 기준일, 출처가 모두 입력되어 있습니다."
            )
            rule_id = f"profile.{table['code']}.metadata_required"
            connection.execute(
                """
                INSERT INTO validation_checks (
                    run_id, table_id, profile_id, rule_id, check_type, check_label,
                    location, current_value, expected_value, difference, status,
                    severity, detail, confidence
                ) VALUES (?, ?, ?, ?, '메타정보 검수', '단위·기준일·출처 메타정보 확인',
                          '출처·메타정보', ?, '단위·기준일·출처 입력', ?, ?, ?, ?, 1.0)
                """,
                (
                    run_id,
                    int(table["id"]),
                    int(table["profile_id"]) if table["profile_id"] is not None else None,
                    rule_id,
                    current_value,
                    difference,
                    status,
                    severity,
                    detail,
                ),
            )
            if not missing:
                continue
            connection.execute(
                """
                INSERT INTO validation_issues (
                    run_id, table_id, rule_id, issue_type, location, current_value,
                    expected_value, difference, status, severity, detail
                ) VALUES (?, ?, ?, '메타정보 검수', '출처·메타정보', ?,
                          '단위·기준일·출처 입력', ?, '확인 필요', 'warning', ?)
                """,
                (
                    run_id,
                    int(table["id"]),
                    rule_id,
                    current_value,
                    difference,
                    detail,
                ),
            )

    connection.execute(
        """
        UPDATE validation_runs
        SET issue_count = (
            SELECT COUNT(*) FROM validation_issues WHERE validation_issues.run_id = validation_runs.id
        )
        """
    )


def repair_misaligned_linguistic_reviews(connection: sqlite3.Connection) -> None:
    """Reset LLM results whose returned source text belongs to another candidate."""

    rows = connection.execute(
        """
        SELECT id, run_id, table_id, review_type, candidate_kind, location,
               row_index, col_index, current_value, review_result_json,
               review_fingerprint
        FROM linguistic_review_candidates
        WHERE status = 'reviewed'
          AND resolution_source = 'llm'
          AND review_result_json <> ''
        """
    ).fetchall()
    materialized_values: dict[tuple[object, ...], list[str]] = {}
    for record in connection.execute(
        """
        SELECT run_id, table_id, rule_id, location, row_index, col_index, current_value
        FROM validation_checks
        WHERE rule_id IN (
            'llm.spelling_review', 'llm.terminology_review',
            'llm.translation_review', 'source.blue_text_review'
        )
        UNION ALL
        SELECT run_id, table_id, rule_id, location, row_index, col_index, current_value
        FROM validation_issues
        WHERE rule_id IN (
            'llm.spelling_review', 'llm.terminology_review',
            'llm.translation_review', 'source.blue_text_review'
        )
        """
    ):
        key = (
            int(record["run_id"]),
            int(record["table_id"]),
            str(record["rule_id"]),
            str(record["location"]),
            record["row_index"],
            record["col_index"],
        )
        materialized_values.setdefault(key, []).append(str(record["current_value"] or ""))

    invalid_rows_by_id: dict[int, sqlite3.Row] = {}
    for row in rows:
        try:
            decision = json.loads(str(row["review_result_json"] or "{}"))
        except json.JSONDecodeError:
            invalid_rows_by_id[int(row["id"])] = row
            continue
        returned_value = decision.get("current_value", "") if isinstance(decision, dict) else ""
        if canonical_review_text(str(returned_value)) != canonical_review_text(str(row["current_value"])):
            invalid_rows_by_id[int(row["id"])] = row
            continue

        rule_id = linguistic_materialized_rule_id(
            str(row["candidate_kind"] or ""),
            str(row["review_type"] or ""),
        )
        record_key = (
            int(row["run_id"]),
            int(row["table_id"]),
            rule_id,
            str(row["location"]),
            row["row_index"],
            row["col_index"],
        )
        if any(
            canonical_review_text(value)
            != canonical_review_text(str(row["current_value"]))
            for value in materialized_values.get(record_key, [])
        ):
            invalid_rows_by_id[int(row["id"])] = row

    invalid_rows = list(invalid_rows_by_id.values())

    stale_fingerprints = {
        str(row["review_fingerprint"])
        for row in invalid_rows
        if str(row["review_fingerprint"] or "")
    }
    if stale_fingerprints:
        connection.executemany(
            "DELETE FROM linguistic_review_cache WHERE fingerprint = ?",
            [(fingerprint,) for fingerprint in stale_fingerprints],
        )

    record_keys: set[tuple[object, ...]] = set()
    invalid_translation_sources: set[tuple[int, str]] = set()
    for row in invalid_rows:
        candidate_kind = str(row["candidate_kind"] or "")
        review_type = str(row["review_type"] or "")
        rule_id = linguistic_materialized_rule_id(candidate_kind, review_type)
        if rule_id:
            record_keys.add(
                (
                    int(row["run_id"]),
                    int(row["table_id"]),
                    rule_id,
                    str(row["location"]),
                    row["row_index"],
                    row["col_index"],
                )
            )
        if review_type == "번역 검수" and not candidate_kind.startswith("blue_text"):
            invalid_translation_sources.add(
                (int(row["table_id"]), canonical_review_text(str(row["current_value"])))
            )

    if record_keys:
        delete_sql = """
            DELETE FROM {table_name}
            WHERE run_id = ? AND table_id = ? AND rule_id = ? AND location = ?
              AND row_index IS ? AND col_index IS ?
        """
        for table_name in ("validation_issues", "validation_checks"):
            connection.executemany(
                delete_sql.format(table_name=table_name),
                list(record_keys),
            )

    if invalid_rows:
        connection.executemany(
            """
            UPDATE linguistic_review_candidates
            SET status = 'pending', reviewed_at = NULL, reviewed_model = '',
                review_result_json = '', review_fingerprint = '', resolution_source = ''
            WHERE id = ?
            """,
            [(int(row["id"]),) for row in invalid_rows],
        )

    glossary_rows = connection.execute(
        """
        SELECT id, source_table_id, source_text, evidence
        FROM translation_glossary
        WHERE status = 'llm_reviewed' AND source_kind = 'llm'
        """
    ).fetchall()
    metadata_location_pattern = re.compile(r"\s(분야|단위|기준일|주석|출처)(?:[.(]|$)")
    stale_glossary_ids = {
        int(row["id"])
        for row in glossary_rows
        if (
            row["source_table_id"] is not None
            and (
                int(row["source_table_id"]),
                canonical_review_text(str(row["source_text"])),
            )
            in invalid_translation_sources
        )
        or metadata_location_pattern.search(str(row["evidence"] or "")) is not None
        or looks_like_note_payload(str(row["source_text"] or ""))
    }
    if stale_glossary_ids:
        connection.executemany(
            "DELETE FROM translation_glossary WHERE id = ?",
            [(glossary_id,) for glossary_id in stale_glossary_ids],
        )

    connection.execute(
        """
        UPDATE validation_runs
        SET issue_count = (
            SELECT COUNT(*) FROM validation_issues WHERE validation_issues.run_id = validation_runs.id
        )
        """
    )


def linguistic_materialized_rule_id(candidate_kind: str, review_type: str) -> str:
    if candidate_kind.startswith("blue_text"):
        return "source.blue_text_review"
    return {
        "오탈자 검수": "llm.spelling_review",
        "용어 제안": "llm.terminology_review",
        "번역 검수": "llm.translation_review",
    }.get(review_type, "")


def retire_metadata_linguistic_reviews(connection: sqlite3.Connection) -> None:
    locations = ("분야", "단위", "기준일", "주석", "출처")
    location_filter = " OR ".join("location = ? OR location LIKE ?" for _ in locations)
    location_params = tuple(
        value
        for location in locations
        for value in (location, f"{location} (%")
    )
    language_rule_ids = (
        "llm.spelling_review",
        "llm.terminology_review",
        "llm.translation_review",
    )
    rule_placeholders = ", ".join("?" for _ in language_rule_ids)

    connection.execute(
        f"""
        DELETE FROM linguistic_review_cache
        WHERE fingerprint IN (
            SELECT review_fingerprint
            FROM linguistic_review_candidates
            WHERE review_fingerprint <> '' AND ({location_filter})
        )
        """,
        location_params,
    )
    connection.execute(
        f"DELETE FROM linguistic_review_candidates WHERE {location_filter}",
        location_params,
    )
    connection.execute(
        f"""
        DELETE FROM validation_issues
        WHERE rule_id IN ({rule_placeholders}) AND ({location_filter})
        """,
        (*language_rule_ids, *location_params),
    )
    connection.execute(
        f"""
        DELETE FROM validation_checks
        WHERE rule_id IN ({rule_placeholders}) AND ({location_filter})
        """,
        (*language_rule_ids, *location_params),
    )
    retire_mislinked_metadata_payload_reviews(connection, language_rule_ids)


def retire_mislinked_metadata_payload_reviews(
    connection: sqlite3.Connection,
    language_rule_ids: tuple[str, ...],
) -> None:
    candidate_rows = connection.execute(
        """
        SELECT lrc.id, lrc.review_fingerprint, lrc.review_result_json, st.note, st.source
        FROM linguistic_review_candidates lrc
        JOIN stat_tables st ON st.id = lrc.table_id
        WHERE lrc.status = 'reviewed' AND lrc.review_result_json <> ''
        """
    ).fetchall()
    reset_candidate_ids: list[tuple[int]] = []
    stale_fingerprints: list[tuple[str]] = []
    for row in candidate_rows:
        try:
            decision = json.loads(str(row["review_result_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        if not isinstance(decision, dict):
            continue
        if not any(
            matches_long_metadata_payload(value, str(row["note"] or ""), str(row["source"] or ""))
            for value in (
                decision.get("current_value", ""),
                decision.get("expected_value", ""),
            )
        ):
            continue
        reset_candidate_ids.append((int(row["id"]),))
        fingerprint = str(row["review_fingerprint"] or "")
        if fingerprint:
            stale_fingerprints.append((fingerprint,))

    if stale_fingerprints:
        connection.executemany(
            "DELETE FROM linguistic_review_cache WHERE fingerprint = ?",
            stale_fingerprints,
        )
    if reset_candidate_ids:
        connection.executemany(
            """
            UPDATE linguistic_review_candidates
            SET status = 'pending', reviewed_at = NULL, reviewed_model = '',
                review_result_json = '', review_fingerprint = '', resolution_source = ''
            WHERE id = ?
            """,
            reset_candidate_ids,
        )

    rule_placeholders = ", ".join("?" for _ in language_rule_ids)
    for table_name in ("validation_issues", "validation_checks"):
        rows = connection.execute(
            f"""
            SELECT record.id, record.current_value, record.expected_value, st.note, st.source
            FROM {table_name} record
            JOIN stat_tables st ON st.id = record.table_id
            WHERE record.rule_id IN ({rule_placeholders})
            """,
            language_rule_ids,
        ).fetchall()
        record_ids = [
            (int(row["id"]),)
            for row in rows
            if matches_long_metadata_payload(
                str(row["current_value"] or ""),
                str(row["note"] or ""),
                str(row["source"] or ""),
            )
            or matches_long_metadata_payload(
                str(row["expected_value"] or ""),
                str(row["note"] or ""),
                str(row["source"] or ""),
            )
        ]
        if record_ids:
            connection.executemany(f"DELETE FROM {table_name} WHERE id = ?", record_ids)


def retire_stale_linguistic_review_records(connection: sqlite3.Connection) -> None:
    """Remove language results that contain note/source payloads in cell locations."""

    language_rule_ids = (
        "llm.spelling_review",
        "llm.terminology_review",
        "llm.translation_review",
    )
    placeholders = ", ".join("?" for _ in language_rule_ids)
    for table_name in ("validation_issues", "validation_checks"):
        rows = connection.execute(
            f"""
            SELECT record.id, record.current_value, record.expected_value,
                   st.note, st.source
            FROM {table_name} record
            JOIN stat_tables st ON st.id = record.table_id
            WHERE record.rule_id IN ({placeholders})
            """,
            language_rule_ids,
        ).fetchall()
        stale_ids: list[tuple[int]] = []
        for row in rows:
            current_value = str(row["current_value"] or "")
            expected_value = str(row["expected_value"] or "")
            if matches_long_metadata_payload(
                current_value,
                str(row["note"] or ""),
                str(row["source"] or ""),
            ) or matches_long_metadata_payload(
                expected_value,
                str(row["note"] or ""),
                str(row["source"] or ""),
            ) or looks_like_note_payload(current_value):
                stale_ids.append((int(row["id"]),))

        if stale_ids:
            connection.executemany(f"DELETE FROM {table_name} WHERE id = ?", stale_ids)


def looks_like_note_payload(value: str) -> bool:
    stripped = str(value or "").strip()
    if stripped.startswith(("*", "\\*", "#주", "[출처:")):
        return True
    return bool(re.match(r"^주\s*\d+\)", stripped))


def canonical_review_text(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", str(value or "")).lower()


def matches_long_metadata_payload(value: str, note: str, source: str) -> bool:
    normalized_value = canonical_review_text(value)
    if len(normalized_value) < 8:
        return False
    for metadata_value in (note, source):
        normalized_metadata = canonical_review_text(metadata_value)
        if len(normalized_metadata) < 8:
            continue
        if normalized_value in normalized_metadata or normalized_metadata in normalized_value:
            return True
    return False


def seed_validation_rule_definitions(connection: sqlite3.Connection) -> None:
    connection.executemany(
        "DELETE FROM validation_rule_definitions WHERE key = ?",
        [("unit",), ("empty",)],
    )
    connection.executemany(
        """
        INSERT INTO validation_rule_definitions (
            key, name, default_status, default_severity, owner_role, description
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            name = excluded.name,
            default_status = excluded.default_status,
            default_severity = excluded.default_severity,
            owner_role = excluded.owner_role,
            description = excluded.description,
            updated_at = CURRENT_TIMESTAMP
        """,
        [
            (
                definition.key,
                definition.name,
                definition.default_status,
                definition.default_severity,
                definition.owner_role,
                definition.description,
            )
            for definition in REQUIRED_RULE_DEFINITIONS
        ],
    )
