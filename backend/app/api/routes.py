from fastapi import APIRouter, HTTPException

from app.models.report import (
    ReportPayload,
    StatTable,
    ValidationProfileApprovalRequest,
    ValidationProfileSummary,
    ValidationRunResult,
)
from app.services.report_service import get_report_service
from app.validation.profile_repository import SQLiteValidationProfileRepository
from app.validation.profiles import ValidationProfile
from app.validation.run_validations import run_validations

router = APIRouter()


@router.get("/report", response_model=ReportPayload)
def get_report() -> ReportPayload:
    return get_report_service().get_payload()


@router.get("/tables", response_model=list[StatTable])
def list_tables() -> list[StatTable]:
    return get_report_service().list_tables()


@router.get("/tables/{table_id}", response_model=StatTable)
def get_table(table_id: str) -> StatTable:
    table = get_report_service().get_table(table_id)
    if table is None:
        raise HTTPException(status_code=404, detail="Table not found")
    return table


@router.post("/validation/run", response_model=ValidationRunResult)
def run_validation() -> ValidationRunResult:
    result = run_validations()
    return ValidationRunResult(**result)


@router.get("/validation/profiles", response_model=list[ValidationProfileSummary])
def list_validation_profiles() -> list[ValidationProfileSummary]:
    repository = SQLiteValidationProfileRepository()
    return [profile_to_summary(profile) for profile in repository.list_profiles()]


@router.post("/validation/profiles/{profile_id}/approve", response_model=ValidationProfileSummary)
def approve_validation_profile(
    profile_id: int,
    payload: ValidationProfileApprovalRequest,
) -> ValidationProfileSummary:
    repository = SQLiteValidationProfileRepository()
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
