from __future__ import annotations

import argparse
import os
from pathlib import Path

from app.db.schema import DB_PATH, connect, init_db
from app.validation.blue_review import append_blue_text_review_checks
from app.validation.engine import ValidationEngine
from app.validation.llm_translation_review import append_llm_translation_reviews, llm_review_settings
from app.validation.profile_repository import SQLiteValidationProfileRepository
from app.validation.source_review import append_source_format_review_checks
from app.validation.sqlite_repository import SQLiteValidationRepository


def run_validations(db_path: Path = DB_PATH) -> dict[str, int | str]:
    repository = SQLiteValidationRepository(db_path)
    report = repository.latest_report()
    if report is None:
        raise RuntimeError("검수할 연보 데이터가 없습니다.")

    tables = repository.load_tables(report["id"])
    profile_repository = SQLiteValidationProfileRepository(db_path)
    profiles = profile_repository.ensure_profiles(report_id=report["id"], tables=tables)
    engine = ValidationEngine(profiles=profiles)
    outcome = engine.evaluate(tables)
    run_id = repository.save_run(
        report_id=report["id"],
        rules_version=engine.rules_version,
        issues=outcome.issues,
        checks=outcome.checks,
    )
    blue_review_issues = append_blue_text_review_checks(
        db_path,
        report_id=report["id"],
        run_id=run_id,
        source_path=report["source_file_path"],
    )
    source_format_issues = append_source_format_review_checks(
        db_path,
        report_id=report["id"],
        run_id=run_id,
    )
    settings = llm_review_settings()
    if settings["api_key"] and settings["enabled"]:
        try:
            append_llm_translation_reviews(
                db_path,
                report_id=report["id"],
                run_id=run_id,
                limit=settings["limit"],
            )
        except RuntimeError:
            if os.getenv("OPENAI_LLM_REVIEW_STRICT", "").strip().lower() in {"1", "true", "yes", "on"}:
                raise

    return {
        "run_id": run_id,
        "report_id": report["id"],
        "tables": len(tables),
        "issues": validation_issue_count(db_path, run_id)
        or len(outcome.issues) + blue_review_issues + source_format_issues,
    }


def validation_issue_count(db_path: Path, run_id: int) -> int:
    with connect(db_path) as connection:
        init_db(connection)
        row = connection.execute(
            "SELECT COUNT(*) AS issue_count FROM validation_issues WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row["issue_count"]) if row else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run rule-based validations for the latest report.")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="SQLite database path")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    result = run_validations(args.db)
    print(
        "Validation run {run_id}: checked {tables} tables and found {issues} issues".format(
            **result
        )
    )


if __name__ == "__main__":
    main()
