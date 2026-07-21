from typing import Any, Literal

from pydantic import BaseModel, Field


TableStatus = Literal["normal", "needs_review", "suspected_error"]
Severity = Literal["info", "warning", "critical"]
HighlightRole = Literal["target", "related"]
HighlightScope = Literal["none", "metadata", "cell", "header", "row", "column"]


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
    source_col_index: int | None = None
    source_col_indexes: list[int] = Field(default_factory=list)


class ValidationHighlightCell(BaseModel):
    row_index: int
    col_index: int
    role: HighlightRole = "related"


class ValidationHighlightRow(BaseModel):
    row_index: int
    role: HighlightRole = "related"


class ValidationIssue(BaseModel):
    id: str
    rule_id: str | None = None
    type: str
    location: str
    row_index: int | None = None
    col_index: int | None = None
    current_value: str
    expected_value: str | None = None
    difference: str | None = None
    status: str = "확인 필요"
    severity: Severity = "warning"
    detail: str
    formula: str | None = None
    highlight_scope: HighlightScope = "none"
    highlight_cells: list[ValidationHighlightCell] = Field(default_factory=list)
    highlight_rows: list[ValidationHighlightRow] = Field(default_factory=list)
    focus_cell: ValidationHighlightCell | None = None


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
    note: str
    source: str
    source_department: str = ""
    source_officer: str = ""
    source_extension: str = ""
    source_reference: str = ""
    base_date: str
    base_date_display: str = ""
    unit_display: str = ""
    extracted_at: str
    header_count: int = 0


class TableHierarchyItem(BaseModel):
    code: str
    title: str
    title_en: str | None = None


class StatTablePart(BaseModel):
    id: str
    code: str
    title: str
    title_en: str
    part_label: str
    unit: str
    status: TableStatus
    status_label: str
    updated_at: str
    columns: list[ColumnDefinition]
    rows: list[dict[str, Any]]
    checks: list[ValidationIssue]
    changes: list[ChangeItem]
    visualizations: list[Visualization]
    metadata: TableMetadata


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
    part_label: str | None = None
    hierarchy: list[TableHierarchyItem] = Field(default_factory=list)
    parts: list[StatTablePart] = Field(default_factory=list)
    columns: list[ColumnDefinition]
    rows: list[dict[str, Any]]
    summary: list[str]
    key_figures: list[Metric]
    checks: list[ValidationIssue]
    changes: list[ChangeItem]
    visualizations: list[Visualization]
    metadata: TableMetadata


class ReportSummary(BaseModel):
    report_id: int | None = None
    file_name: str
    base_year: str
    total_tables: int
    normal_count: int
    needs_review_count: int
    suspected_error_count: int
    issue_counts: dict[str, int] = Field(default_factory=dict)


class ReportOption(BaseModel):
    id: int
    year: int
    title: str
    file_name: str
    imported_at: str
    table_count: int


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
    available_reports: list[ReportOption] = Field(default_factory=list)


class ValidationProfileSummary(BaseModel):
    id: int
    table_code: str
    table_title: str
    structure_signature: str
    table_type: str
    status: str
    source: str
    rules_count: int
    requires_llm_review: bool = False
    notes: str
    created_at: str
    updated_at: str
    approved_at: str | None = None
    approved_by: str | None = None


class ValidationProfileApprovalRequest(BaseModel):
    approved_by: str = "담당자"


class ValidationRunResult(BaseModel):
    run_id: int
    report_id: int
    tables: int
    issues: int
    language_candidates: int = 0
    language_pending: int = 0
    language_reused: int = 0
    language_status: str = "대기"


class ReportProcessingJob(BaseModel):
    id: int
    report_id: int | None = None
    run_id: int | None = None
    source_file_name: str
    source_file_path: str
    source_type: str
    report_year: int
    report_title: str
    options_json: str
    status: str
    current_stage: str
    error_message: str = ""
    result_json: str = "{}"
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    updated_at: str
    stages: list[dict] = Field(default_factory=list)
