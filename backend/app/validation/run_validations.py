from __future__ import annotations

import argparse
from pathlib import Path

from app.db.connection import DB_PATH, connect
from app.db.schema import init_db
from app.validation.blue_review import append_blue_text_review_checks, synchronize_blue_review_checks
from app.validation.llm_translation_review import (
    apply_reusable_linguistic_reviews,
)
from app.validation.linguistic_review import clear_linguistic_review_run, prepare_linguistic_reviews
from app.validation.language_workflow import (
    linguistic_review_counts as language_review_counts,
    refresh_stored_issue_count,
)
from app.validation.workflow import (
    ValidationWorkflow,
    ValidationWorkflowOptions,
    validation_issue_count,
)


def run_validations(
    db_path: Path = DB_PATH,
    *,
    report_id: int | None = None,
    include_llm: bool = False,
    refresh_profiles: bool = False,
) -> dict[str, int | str]:
    return ValidationWorkflow(db_path).run(
        report_id=report_id,
        options=ValidationWorkflowOptions(
            include_llm=include_llm,
            refresh_profiles=refresh_profiles,
        ),
    ).to_dict()


def rebuild_linguistic_review_scope(
    db_path: Path = DB_PATH,
    *,
    report_id: int,
    run_id: int,
) -> dict[str, int]:
    """Rebuild standard language candidates without touching completed blue reviews."""

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
            blue_row = connection.execute(
                """
                SELECT COUNT(*) AS candidate_count
                FROM linguistic_review_candidates
                WHERE run_id = ? AND candidate_kind LIKE 'blue_text%'
                """,
                (run_id,),
            ).fetchone()
            blue_candidates = int(blue_row["candidate_count"] if blue_row else 0)

    # A legacy run may not have blue candidates yet. Populate them once, but do
    # not recreate existing reviewed candidates when only language scope changes.
    if blue_candidates == 0:
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
    candidate_count, pending_count = language_review_counts(db_path, run_id)
    return {
        "blue_candidates": blue_candidates,
        "language_candidates": candidate_count or summary.candidates,
        "language_pending": pending_count,
        "language_reused": reusable_counts[2],
        "issues": refresh_count,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run rule-based validations for the latest report.")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="SQLite database path")
    parser.add_argument("--report-id", type=int, default=None, help="검수할 연보 ID (기본값: 최신 연보)")
    parser.add_argument(
        "--with-llm",
        action="store_true",
        help="규칙·사전 검수 후 LLM 오탈자·번역 검수를 명시적으로 추가합니다.",
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
        report_id=args.report_id,
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
