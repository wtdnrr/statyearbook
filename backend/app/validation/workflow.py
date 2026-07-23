from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from app.db.connection import DB_PATH, connect
from app.db.schema import init_db
from app.validation.calculation_workflow import CalculationValidationWorkflow
from app.validation.language_workflow import LanguageValidationWorkflow
from app.validation.repository import ValidationRepository


@dataclass(frozen=True)
class ValidationWorkflowOptions:
    include_llm: bool = False
    refresh_profiles: bool = False


@dataclass(frozen=True)
class ValidationWorkflowResult:
    run_id: int
    report_id: int
    tables: int
    issues: int
    language_candidates: int
    language_pending: int
    language_reused: int
    language_status: str

    def to_dict(self) -> dict[str, int | str]:
        return asdict(self)


class ValidationWorkflow:
    """Application service for one complete validation run.

    Every stage is pinned to one report ID. This is the central entry point for
    CLI commands, API requests and automated imports.
    """

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db_path = db_path
        self._repository = ValidationRepository(db_path)
        self._calculation = CalculationValidationWorkflow(db_path)
        self._language = LanguageValidationWorkflow(db_path)

    def run(
        self,
        *,
        report_id: int | None = None,
        options: ValidationWorkflowOptions | None = None,
    ) -> ValidationWorkflowResult:
        selected_options = options or ValidationWorkflowOptions()
        report = self._repository.report(report_id) if report_id is not None else self._repository.latest_report()
        if report is None:
            raise RuntimeError("검수할 연보 데이터가 없습니다.")

        selected_report_id = int(report["id"])
        calculation = self._calculation.run(
            report_id=selected_report_id,
            refresh_profiles=selected_options.refresh_profiles,
        )
        language = self._language.run(
            report_id=selected_report_id,
            run_id=calculation.run_id,
            source_path=str(report["source_file_path"]),
            include_llm=selected_options.include_llm,
        )
        return ValidationWorkflowResult(
            run_id=calculation.run_id,
            report_id=selected_report_id,
            tables=calculation.table_count,
            issues=validation_issue_count(self._db_path, calculation.run_id),
            language_candidates=language.candidate_count,
            language_pending=language.pending_count,
            language_reused=language.reused_count,
            language_status=language.status,
        )


def validation_issue_count(db_path: Path, run_id: int) -> int:
    with connect(db_path) as connection:
        init_db(connection)
        row = connection.execute(
            "SELECT COUNT(*) AS issue_count FROM validation_issues WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row["issue_count"] if row else 0)
