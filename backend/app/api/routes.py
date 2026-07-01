from fastapi import APIRouter, HTTPException

from app.models.report import ReportPayload, StatTable
from app.services.report_service import get_report_service

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
