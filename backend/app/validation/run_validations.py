from __future__ import annotations

import argparse
from pathlib import Path

from app.db.schema import DB_PATH, connect, init_db
from app.validation.blue_review import append_blue_text_review_checks, synchronize_blue_review_checks
from app.validation.engine import ValidationEngine
from app.validation.llm_translation_review import (
    append_llm_translation_reviews,
    apply_reusable_linguistic_reviews,
)
from app.validation.linguistic_review import clear_linguistic_review_run, prepare_linguistic_reviews
from app.validation.profile_repository import SQLiteValidationProfileRepository
from app.validation.source_review import append_source_format_review_checks
from app.validation.sqlite_repository import SQLiteValidationRepository


def run_validations(
    db_path: Path = DB_PATH,
    *,
    include_llm: bool = False,
    refresh_profiles: bool = False,
) -> dict[str, int | str]:
    repository = SQLiteValidationRepository(db_path)
    report = repository.latest_report()
    if report is None:
        raise RuntimeError("검수할 연보 데이터가 없습니다.")

    tables = repository.load_tables(report["id"])
    profile_repository = SQLiteValidationProfileRepository(db_path)
    profiles = profile_repository.ensure_profiles(report_id=report["id"], tables=tables)
    if refresh_profiles:
        refreshed_profiles = profile_repository.refresh_curated_profiles(
            report_id=report["id"],
            tables=tables,
        )
        profiles.update(refreshed_profiles)
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
    with connect(db_path) as connection:
        init_db(connection)
        linguistic_summary = prepare_linguistic_reviews(
            connection,
            report_id=report["id"],
            run_id=run_id,
        )
        refresh_stored_issue_count(connection, run_id)
    reusable_counts = apply_reusable_linguistic_reviews(
        db_path,
        run_id=run_id,
    )
    if include_llm:
        llm_result = append_llm_translation_reviews(
            db_path,
            report_id=report["id"],
            run_id=run_id,
            limit=None,
        )
        if llm_result.skipped_reason:
            raise RuntimeError(f"LLM 전수 언어 검수가 실행되지 않았습니다: {llm_result.skipped_reason}")

    synchronize_blue_review_checks(db_path, run_id=run_id)

    language_counts = linguistic_review_counts(db_path, run_id)
    if include_llm and language_counts["pending"]:
        raise RuntimeError(
            "LLM 전수 언어 검수가 완료되지 않았습니다: "
            f"{language_counts['pending']}/{language_counts['total']}건 대기"
        )

    return {
        "run_id": run_id,
        "report_id": report["id"],
        "tables": len(tables),
        "issues": validation_issue_count(db_path, run_id)
        or len(outcome.issues) + blue_review_issues + source_format_issues,
        "language_candidates": language_counts["total"] or linguistic_summary.candidates,
        "language_pending": language_counts["pending"],
        "language_reused": reusable_counts[2],
        "language_status": "완료" if language_counts["pending"] == 0 else "대기",
    }


def rebuild_linguistic_review_scope(
    db_path: Path = DB_PATH,
    *,
    report_id: int,
    run_id: int,
) -> dict[str, int]:
    """Rebuild language candidates without making any paid LLM request."""

    with connect(db_path) as connection:
        init_db(connection)
        report = connection.execute(
            "SELECT source_file_path FROM annual_reports WHERE id = ?",
            (report_id,),
        ).fetchone()
        if report is None:
            raise RuntimeError(f"연보를 찾을 수 없습니다: {report_id}")
        with connection:
            clear_linguistic_review_run(
                connection,
                report_id=report_id,
                run_id=run_id,
            )

    blue_candidates = append_blue_text_review_checks(
        db_path,
        report_id=report_id,
        run_id=run_id,
        source_path=str(report["source_file_path"]),
    )
    with connect(db_path) as connection:
        init_db(connection)
        with connection:
            summary = prepare_linguistic_reviews(
                connection,
                report_id=report_id,
                run_id=run_id,
            )
    reusable_counts = apply_reusable_linguistic_reviews(db_path, run_id=run_id)
    synchronize_blue_review_checks(db_path, run_id=run_id)
    refresh_count = validation_issue_count(db_path, run_id)
    with connect(db_path) as connection:
        connection.execute(
            "UPDATE validation_runs SET issue_count = ? WHERE id = ?",
            (refresh_count, run_id),
        )
        connection.commit()
    counts = linguistic_review_counts(db_path, run_id)
    return {
        "blue_candidates": blue_candidates,
        "language_candidates": counts["total"] or summary.candidates,
        "language_pending": counts["pending"],
        "language_reused": reusable_counts[2],
        "issues": refresh_count,
    }


def validation_issue_count(db_path: Path, run_id: int) -> int:
    with connect(db_path) as connection:
        init_db(connection)
        row = connection.execute(
            "SELECT COUNT(*) AS issue_count FROM validation_issues WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row["issue_count"]) if row else 0


def refresh_stored_issue_count(connection, run_id: int) -> int:
    row = connection.execute(
        "SELECT COUNT(*) AS issue_count FROM validation_issues WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    issue_count = int(row["issue_count"]) if row else 0
    connection.execute(
        "UPDATE validation_runs SET issue_count = ? WHERE id = ?",
        (issue_count, run_id),
    )
    connection.commit()
    return issue_count


def linguistic_review_counts(db_path: Path, run_id: int) -> dict[str, int]:
    with connect(db_path) as connection:
        init_db(connection)
        row = connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status <> 'reviewed' THEN 1 ELSE 0 END) AS pending
            FROM linguistic_review_candidates
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "pending": int(row["pending"] or 0),
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run rule-based validations for the latest report.")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="SQLite database path")
    parser.add_argument(
        "--with-llm",
        action="store_true",
        help="규칙·사전 검수 후 LLM 오탈자·용어·번역 검수를 명시적으로 추가합니다.",
    )
    parser.add_argument(
        "--refresh-profiles",
        action="store_true",
        help="소스에 관리하는 표별 큐레이션 검수 프로파일을 SQLite에 반영합니다.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    result = run_validations(
        args.db,
        include_llm=args.with_llm,
        refresh_profiles=args.refresh_profiles,
    )
    print(
        "Validation run {run_id}: checked {tables} tables and found {issues} issues; "
        "language review {language_status} ({language_pending}/{language_candidates} pending)".format(
            **result
        )
    )


if __name__ == "__main__":
    main()
