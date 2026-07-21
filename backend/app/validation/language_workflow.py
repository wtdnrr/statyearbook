from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.db.schema import DB_PATH, connect, init_db
from app.validation.blue_review import append_blue_text_review_checks, synchronize_blue_review_checks
from app.validation.llm_translation_review import (
    append_llm_translation_reviews,
    apply_reusable_linguistic_reviews,
)
from app.validation.linguistic_review import prepare_linguistic_reviews
from app.validation.source_review import append_source_format_review_checks


@dataclass(frozen=True)
class LanguageValidationResult:
    candidate_count: int
    pending_count: int
    reused_count: int
    blue_candidate_count: int
    source_issue_count: int

    @property
    def status(self) -> str:
        return "완료" if self.pending_count == 0 else "대기"


class LanguageValidationWorkflow:
    """Prepare, reuse and optionally execute linguistic review candidates."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db_path = db_path

    def run(
        self,
        *,
        report_id: int,
        run_id: int,
        source_path: str,
        include_llm: bool,
    ) -> LanguageValidationResult:
        blue_candidates = append_blue_text_review_checks(
            self._db_path,
            report_id=report_id,
            run_id=run_id,
            source_path=source_path,
        )
        source_issues = append_source_format_review_checks(
            self._db_path,
            report_id=report_id,
            run_id=run_id,
        )

        with connect(self._db_path) as connection:
            init_db(connection)
            prepare_linguistic_reviews(
                connection,
                report_id=report_id,
                run_id=run_id,
            )
            refresh_stored_issue_count(connection, run_id)

        _, _, reused_count = apply_reusable_linguistic_reviews(
            self._db_path,
            run_id=run_id,
        )
        if include_llm:
            llm_result = append_llm_translation_reviews(
                self._db_path,
                report_id=report_id,
                run_id=run_id,
                limit=None,
            )
            if llm_result.skipped_reason:
                raise RuntimeError(
                    f"LLM 전수 언어 검수가 실행되지 않았습니다: {llm_result.skipped_reason}"
                )

        synchronize_blue_review_checks(self._db_path, run_id=run_id)
        candidate_count, pending_count = linguistic_review_counts(self._db_path, run_id)
        if include_llm and pending_count:
            raise RuntimeError(
                "LLM 전수 언어 검수가 완료되지 않았습니다: "
                f"{pending_count}/{candidate_count}건 대기"
            )

        return LanguageValidationResult(
            candidate_count=candidate_count,
            pending_count=pending_count,
            reused_count=reused_count,
            blue_candidate_count=blue_candidates,
            source_issue_count=source_issues,
        )


def refresh_stored_issue_count(connection, run_id: int) -> int:
    row = connection.execute(
        "SELECT COUNT(*) AS issue_count FROM validation_issues WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    issue_count = int(row["issue_count"] if row else 0)
    connection.execute(
        "UPDATE validation_runs SET issue_count = ? WHERE id = ?",
        (issue_count, run_id),
    )
    connection.commit()
    return issue_count


def linguistic_review_counts(db_path: Path, run_id: int) -> tuple[int, int]:
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
        return int(row["total"] or 0), int(row["pending"] or 0)
