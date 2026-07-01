from typing import Any, Literal

from pydantic import BaseModel, Field


TableStatus = Literal["normal", "needs_review", "suspected_error"]
Severity = Literal["info", "warning", "critical"]


class Metric(BaseModel):
    label: str
    value: str
    caption: str | None = None
    tone: Literal["neutral", "blue", "green", "red", "amber"] = "neutral"


class ColumnDefinition(BaseModel):
    key: str
    label: str
    label_en: str | None = None
    align: Literal["left", "right", "center"] = "right"
    width: str | None = None


class ValidationIssue(BaseModel):
    id: str
    type: str
    location: str
    current_value: str
    expected_value: str | None = None
    difference: str | None = None
    status: str = "확인 필요"
    severity: Severity = "warning"
    detail: str
    formula: str | None = None


class ChangeItem(BaseModel):
    id: str
    category: str
    item: str
    previous: str
    current: str
    status: str


class VisualizationSeries(BaseModel):
    label: str
    value: float
    previous: float | None = None


class Visualization(BaseModel):
    id: str
    title: str
    subtitle: str | None = None
    kind: Literal["bar", "line", "rank"]
    unit: str
    data: list[VisualizationSeries]


class TableMetadata(BaseModel):
    original_file: str
    sheet_name: str
    cell_range: str
    note: str
    source: str
    base_date: str
    extracted_at: str


class StatTable(BaseModel):
    id: str
    code: str
    title: str
    title_en: str
    section_title: str
    section_title_en: str
    domain: str
    unit: str
    sheet_name: str
    status: TableStatus
    status_label: str
    year_range: str
    updated_at: str
    theme: Literal["blue", "red", "green"]
    columns: list[ColumnDefinition]
    rows: list[dict[str, Any]]
    summary: list[str]
    key_figures: list[Metric]
    checks: list[ValidationIssue]
    changes: list[ChangeItem]
    visualizations: list[Visualization]
    metadata: TableMetadata


class ReportSummary(BaseModel):
    file_name: str
    base_year: str
    total_tables: int
    normal_count: int
    needs_review_count: int
    suspected_error_count: int
    issue_counts: dict[str, int] = Field(default_factory=dict)


class PressInsight(BaseModel):
    id: str
    title: str
    body: str
    table_id: str
    tone: Literal["neutral", "increase", "risk", "notable"] = "neutral"


class ReportPayload(BaseModel):
    summary: ReportSummary
    tables: list[StatTable]
    press_insights: list[PressInsight]
