from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Callable

from app.db.connection import DB_PATH, connect
from app.db.schema import init_db
from app.ingest.excel_importer import import_excel
from app.ingest.hwpx_importer import import_hwpx
from app.ingest.markdown_importer import import_markdown
from app.validation.calculation_workflow import CalculationValidationWorkflow
from app.validation.language_workflow import LanguageValidationWorkflow
from app.validation.repository import ValidationRepository
from app.validation.workflow import validation_issue_count


SUPPORTED_SOURCE_TYPES = {
    ".xlsx": "xlsx",
    ".hwpx": "hwpx",
    ".md": "markdown",
}
SOURCE_IMPORTERS = {
    "xlsx": import_excel,
    "hwpx": import_hwpx,
    "markdown": import_markdown,
}


@dataclass(frozen=True)
class ReportProcessingOptions:
    include_llm: bool = False
    refresh_profiles: bool = False


class ReportProcessingError(RuntimeError):
    def __init__(self, job_id: int, stage: str, message: str) -> None:
        super().__init__(message)
        self.job_id = job_id
        self.stage = stage


class ReportProcessingWorkflow:
    """Import and validate one uploaded report with persistent stage state."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db_path = db_path

    def create_job(
        self,
        *,
        source_path: Path,
        year: int,
        title: str,
        options: ReportProcessingOptions | None = None,
        source_type: str | None = None,
    ) -> int:
        selected_source_type = source_type or SUPPORTED_SOURCE_TYPES.get(source_path.suffix.lower())
        if selected_source_type is None or selected_source_type not in SUPPORTED_SOURCE_TYPES.values():
            supported = ", ".join(sorted(SUPPORTED_SOURCE_TYPES))
            raise ValueError(f"지원하지 않는 파일 형식입니다. 지원 형식: {supported}")
        payload = options or ReportProcessingOptions()
        with connect(self._db_path) as connection:
            init_db(connection)
            cursor = connection.execute(
                """
                INSERT INTO report_processing_jobs (
                    source_file_name, source_file_path, source_type,
                    report_year, report_title, options_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    source_path.name,
                    str(source_path),
                    selected_source_type,
                    year,
                    title,
                    json.dumps(payload.__dict__, ensure_ascii=False),
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def run(self, job_id: int) -> dict[str, object]:
        job = self.get_job(job_id)
        if job is None:
            raise ReportProcessingError(job_id, "queued", "가져오기 작업을 찾을 수 없습니다.")
        if job["status"] == "completed":
            return job

        # ``queue_retry`` changes only the status. Keeping the persisted stage
        # lets a retry resume after the last successful stage instead of
        # importing the same source a second time.
        resume_stage = str(job["current_stage"] or "import")
        self._update_job(job_id, status="running", current_stage=resume_stage, started=True, error="")
        try:
            options = processing_options_from_json(str(job["options_json"] or "{}"))
            report_id = int(job["report_id"]) if job["report_id"] is not None else None
            run_id = int(job["run_id"]) if job["run_id"] is not None else None

            import_result = self._completed_stage_result(job_id, "import")
            if report_id is None or resume_stage == "import":
                import_result = self._run_stage(job_id, "import", lambda: self._import_source(job))
                report_id = int(import_result["report_id"])
                run_id = None
                self._update_job(
                    job_id,
                    report_id=report_id,
                    current_stage="calculation_validation",
                )

            calculation_result = self._completed_stage_result(job_id, "calculation_validation")
            if run_id is None or resume_stage in {"import", "calculation_validation"}:
                calculation_result = self._run_stage(
                    job_id,
                    "calculation_validation",
                    lambda: asdict(
                        CalculationValidationWorkflow(self._db_path).run(
                            report_id=report_id,
                            refresh_profiles=options.refresh_profiles,
                        )
                    ),
                )
                run_id = int(calculation_result["run_id"])
                self._update_job(
                    job_id,
                    report_id=report_id,
                    run_id=run_id,
                    current_stage="language_validation",
                )

            report = ValidationRepository(self._db_path).report(report_id)
            if report is None:
                raise RuntimeError(f"검수할 연보를 찾을 수 없습니다: {report_id}")
            language_result = self._run_stage(
                job_id,
                "language_validation",
                lambda: self._run_language_validation(
                    report_id=report_id,
                    run_id=run_id,
                    source_path=str(report["source_file_path"]),
                    include_llm=options.include_llm,
                ),
            )
            result = {
                "import": import_result,
                "calculation_validation": calculation_result,
                "language_validation": language_result,
                "issues": validation_issue_count(self._db_path, run_id),
            }
            self._update_job(
                job_id,
                report_id=report_id,
                run_id=run_id,
                status="completed",
                current_stage="completed",
                result=result,
                completed=True,
            )
            return self.get_job(job_id) or result
        except ReportProcessingError:
            raise
        except Exception as error:
            stage = str((self.get_job(job_id) or {}).get("current_stage") or "unknown")
            self._update_job(job_id, status="failed", current_stage=stage, error=str(error), completed=True)
            raise ReportProcessingError(job_id, stage, str(error)) from error

    def retry(self, job_id: int) -> dict[str, object]:
        job = self.get_job(job_id)
        if job is None:
            raise ReportProcessingError(job_id, "queued", "가져오기 작업을 찾을 수 없습니다.")
        if job["status"] not in {"failed", "queued"}:
            return job
        return self.run(job_id)

    def queue_retry(self, job_id: int) -> dict[str, object]:
        """Make a failed job observable as queued before a worker resumes it."""

        job = self.get_job(job_id)
        if job is None:
            raise ReportProcessingError(job_id, "queued", "가져오기 작업을 찾을 수 없습니다.")
        if job["status"] == "failed":
            self._update_job(job_id, status="queued", error="")
        return self.get_job(job_id) or job

    def get_job(self, job_id: int) -> dict[str, object] | None:
        with connect(self._db_path) as connection:
            init_db(connection)
            row = connection.execute(
                "SELECT * FROM report_processing_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            stages = connection.execute(
                """
                SELECT stage_name, attempt, status, result_json, error_message,
                       started_at, completed_at
                FROM report_processing_stages
                WHERE job_id = ?
                ORDER BY id
                """,
                (job_id,),
            ).fetchall()
            result = dict(row)
            result["stages"] = [dict(stage) for stage in stages]
            return result

    def _import_source(self, job: dict[str, object]) -> dict[str, int | str]:
        source_path = Path(str(job["source_file_path"]))
        common = {
            "db_path": self._db_path,
            "year": int(job["report_year"]),
            "title": str(job["report_title"]),
            "run_validation": False,
        }
        source_type = str(job["source_type"])
        importer = SOURCE_IMPORTERS.get(source_type)
        if importer is None:
            raise ValueError(f"등록되지 않은 가져오기 형식입니다: {source_type}")
        return importer(source_path, **common)

    def _run_language_validation(
        self,
        *,
        report_id: int,
        run_id: int,
        source_path: str,
        include_llm: bool,
    ) -> dict[str, object]:
        result = LanguageValidationWorkflow(self._db_path).run(
            report_id=report_id,
            run_id=run_id,
            source_path=source_path,
            include_llm=include_llm,
        )
        return {**asdict(result), "status": result.status}

    def _run_stage(
        self,
        job_id: int,
        stage_name: str,
        operation: Callable[[], dict[str, object] | dict[str, int | str]],
    ) -> dict[str, object]:
        attempt = self._start_stage(job_id, stage_name)
        self._update_job(job_id, current_stage=stage_name)
        try:
            result = dict(operation())
        except Exception as error:
            self._finish_stage(job_id, stage_name, attempt, status="failed", error=str(error))
            self._update_job(job_id, status="failed", current_stage=stage_name, error=str(error), completed=True)
            raise ReportProcessingError(job_id, stage_name, str(error)) from error
        self._finish_stage(job_id, stage_name, attempt, status="completed", result=result)
        return result

    def _start_stage(self, job_id: int, stage_name: str) -> int:
        with connect(self._db_path) as connection:
            init_db(connection)
            row = connection.execute(
                """
                SELECT COALESCE(MAX(attempt), 0) + 1 AS next_attempt
                FROM report_processing_stages
                WHERE job_id = ? AND stage_name = ?
                """,
                (job_id, stage_name),
            ).fetchone()
            attempt = int(row["next_attempt"])
            connection.execute(
                """
                INSERT INTO report_processing_stages (job_id, stage_name, attempt)
                VALUES (?, ?, ?)
                """,
                (job_id, stage_name, attempt),
            )
            connection.commit()
            return attempt

    def _completed_stage_result(self, job_id: int, stage_name: str) -> dict[str, object]:
        with connect(self._db_path) as connection:
            init_db(connection)
            row = connection.execute(
                """
                SELECT result_json
                FROM report_processing_stages
                WHERE job_id = ? AND stage_name = ? AND status = 'completed'
                ORDER BY attempt DESC
                LIMIT 1
                """,
                (job_id, stage_name),
            ).fetchone()
            if row is None:
                return {}
            try:
                result = json.loads(str(row["result_json"] or "{}"))
            except json.JSONDecodeError:
                return {}
            return result if isinstance(result, dict) else {}

    def _finish_stage(
        self,
        job_id: int,
        stage_name: str,
        attempt: int,
        *,
        status: str,
        result: dict[str, object] | None = None,
        error: str = "",
    ) -> None:
        with connect(self._db_path) as connection:
            init_db(connection)
            connection.execute(
                """
                UPDATE report_processing_stages
                SET status = ?, result_json = ?, error_message = ?, completed_at = ?
                WHERE job_id = ? AND stage_name = ? AND attempt = ?
                """,
                (
                    status,
                    json.dumps(result or {}, ensure_ascii=False),
                    error,
                    timestamp(),
                    job_id,
                    stage_name,
                    attempt,
                ),
            )
            connection.commit()

    def _update_job(
        self,
        job_id: int,
        *,
        report_id: int | None = None,
        run_id: int | None = None,
        status: str | None = None,
        current_stage: str | None = None,
        error: str | None = None,
        result: dict[str, object] | None = None,
        started: bool = False,
        completed: bool = False,
    ) -> None:
        assignments = ["updated_at = ?"]
        values: list[object] = [timestamp()]
        for column, value in (
            ("report_id", report_id),
            ("run_id", run_id),
            ("status", status),
            ("current_stage", current_stage),
            ("error_message", error),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                values.append(value)
        if result is not None:
            assignments.append("result_json = ?")
            values.append(json.dumps(result, ensure_ascii=False))
        if started:
            assignments.append("started_at = COALESCE(started_at, ?)")
            values.append(timestamp())
        if completed:
            assignments.append("completed_at = ?")
            values.append(timestamp())
        values.append(job_id)
        with connect(self._db_path) as connection:
            init_db(connection)
            connection.execute(
                f"UPDATE report_processing_jobs SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
            connection.commit()


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def processing_options_from_json(value: str) -> ReportProcessingOptions:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return ReportProcessingOptions(
        include_llm=bool(payload.get("include_llm", False)),
        refresh_profiles=bool(payload.get("refresh_profiles", False)),
    )
