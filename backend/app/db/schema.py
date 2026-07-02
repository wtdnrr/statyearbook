from pathlib import Path
import sqlite3


DB_PATH = Path(__file__).with_name("annual_statistics.sqlite")


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

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
    FOREIGN KEY (table_id) REFERENCES stat_tables(id) ON DELETE CASCADE,
    UNIQUE (table_id, row_index, col_index)
);

CREATE INDEX IF NOT EXISTS idx_stat_tables_report_order
ON stat_tables(report_id, table_order);

CREATE INDEX IF NOT EXISTS idx_stat_table_cells_table_position
ON stat_table_cells(table_id, row_index, col_index);

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
    connection.commit()
