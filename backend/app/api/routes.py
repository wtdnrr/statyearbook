from pathlib import Path
from datetime import datetime
import re
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, status

from app.models.report import (
    ReportPayload,
    ReportProcessingJob,
    StatTable,
    ValidationProfileApprovalRequest,
    ValidationProfileSummary,
    ValidationRunResult,
)
from app.services.report_service import get_report_service
from app.core.config import get_settings
from app.validation.profile_repository import ValidationProfileRepository
from app.validation.profiles import ValidationProfile
from app.validation.run_validations import run_validations
from app.workflows.report_processing import (
    ReportProcessingError,
    ReportProcessingOptions,
    ReportProcessingWorkflow,
)

router = APIRouter()


@router.get("/report", response_model=ReportPayload)
def get_report(report_id: int | None = Query(default=None)) -> ReportPayload:
    return get_report_service().get_payload_for_report(report_id)


@router.get("/tables", response_model=list[StatTable])
def list_tables(report_id: int | None = Query(default=None)) -> list[StatTable]:
    return get_report_service().list_tables(report_id)


@router.get("/tables/{table_id}", response_model=StatTable)
def get_table(table_id: str, report_id: int | None = Query(default=None)) -> StatTable:
    table = get_report_service().get_table(table_id, report_id)
    if table is None:
        raise HTTPException(status_code=404, detail="Table not found")
    return table


@router.post("/validation/run", response_model=ValidationRunResult)
def run_validation(report_id: int | None = Query(default=None)) -> ValidationRunResult:
    result = run_validations(report_id=report_id)
    return ValidationRunResult(**result)


@router.post(
    "/imports",
    response_model=ReportProcessingJob,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_report_import(
    request: Request,
    background_tasks: BackgroundTasks,
    filename: str = Query(min_length=1),
    year: int | None = Query(default=None, ge=1900, le=2200),
    title: str | None = Query(default=None),
    include_llm: bool | None = Query(default=None),
    refresh_profiles: bool = Query(default=False),
) -> ReportProcessingJob:
    """Accept a raw workbook/document body and queue import plus validation.

    A raw request body avoids a multipart runtime dependency. Browser clients
    can send the selected File object directly with the original filename in
    the query string.
    """

    settings = get_settings()
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="업로드 파일이 비어 있습니다.")
    if len(body) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="업로드 가능한 파일 크기를 초과했습니다.")

    safe_name = Path(filename).name
    suffix = Path(safe_name).suffix.lower()
    if suffix not in {".xlsx", ".hwpx", ".md"}:
        raise HTTPException(status_code=415, detail="xlsx, hwpx, md 파일만 업로드할 수 있습니다.")
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    source_path = settings.upload_dir / f"{uuid4().hex}_{safe_name}"
    source_path.write_bytes(body)

    workflow = ReportProcessingWorkflow()
    inferred_year_match = re.search(r"(?:19|20)\d{2}", safe_name)
    report_year = year or (
        int(inferred_year_match.group(0)) if inferred_year_match else datetime.now().year
    )
    try:
        job_id = workflow.create_job(
            source_path=source_path,
            year=report_year,
            title=title or f"{report_year} 행정안전통계연보",
            options=ReportProcessingOptions(
                include_llm=(
                    settings.auto_import_include_llm
                    if include_llm is None
                    else include_llm
                ),
                refresh_profiles=refresh_profiles,
            ),
        )
    except ValueError as error:
        source_path.unlink(missing_ok=True)
        raise HTTPException(status_code=415, detail=str(error)) from error
    background_tasks.add_task(run_processing_job, job_id)
    return ReportProcessingJob(**(workflow.get_job(job_id) or {}))


@router.get("/imports/{job_id}", response_model=ReportProcessingJob)
def get_report_import(job_id: int) -> ReportProcessingJob:
    job = ReportProcessingWorkflow().get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="가져오기 작업을 찾을 수 없습니다.")
    return ReportProcessingJob(**job)


@router.post(
    "/imports/{job_id}/retry",
    response_model=ReportProcessingJob,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_report_import(job_id: int, background_tasks: BackgroundTasks) -> ReportProcessingJob:
    workflow = ReportProcessingWorkflow()
    job = workflow.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="가져오기 작업을 찾을 수 없습니다.")
    job = workflow.queue_retry(job_id)
    background_tasks.add_task(run_processing_job, job_id, True)
    return ReportProcessingJob(**job)


def run_processing_job(job_id: int, retry: bool = False) -> None:
    workflow = ReportProcessingWorkflow()
    try:
        if retry:
            workflow.retry(job_id)
        else:
            workflow.run(job_id)
    except ReportProcessingError:
        # The workflow already persisted the stage and error. The status API is
        # the source of truth, so a background exception must not lose context.
        return


@router.get("/validation/profiles", response_model=list[ValidationProfileSummary])
def list_validation_profiles() -> list[ValidationProfileSummary]:
    repository = ValidationProfileRepository()
    return [profile_to_summary(profile) for profile in repository.list_profiles()]


@router.post("/validation/profiles/{profile_id}/approve", response_model=ValidationProfileSummary)
def approve_validation_profile(
    profile_id: int,
    payload: ValidationProfileApprovalRequest,
) -> ValidationProfileSummary:
    repository = ValidationProfileRepository()
    profile = repository.approve_profile(profile_id, approved_by=payload.approved_by)
    if profile is None:
        raise HTTPException(status_code=404, detail="Validation profile not found")
    return profile_to_summary(profile)


def profile_to_summary(profile: ValidationProfile) -> ValidationProfileSummary:
    return ValidationProfileSummary(
        id=profile.id,
        table_code=profile.table_code,
        table_title=profile.table_title,
        structure_signature=profile.structure_signature,
        table_type=profile.table_type,
        status=profile.status,
        source=profile.source,
        rules_count=len(profile.check_specs),
        requires_llm_review=bool(profile.rules.get("requires_llm_review")),
        notes=profile.notes,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
        approved_at=profile.approved_at,
        approved_by=profile.approved_by,
    )
