from __future__ import annotations

import argparse
from pathlib import Path

from app.db.schema import DB_PATH
from app.validation.engine import ValidationEngine
from app.validation.profile_repository import SQLiteValidationProfileRepository
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

    return {
        "run_id": run_id,
        "report_id": report["id"],
        "tables": len(tables),
        "issues": len(outcome.issues),
    }


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
