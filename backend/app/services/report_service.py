from app.models.report import PressInsight, ReportOption, ReportPayload, ReportSummary, StatTable


FALLBACK_ORIGINAL_FILE = "2025_통계연보_추출.xlsx"


class ReportService:
    def __init__(self, tables: list[StatTable] | None = None) -> None:
        if tables is None:
            from app.data.dummy_report import TABLES

            tables = TABLES
        self._tables = tables

    def get_payload(self) -> ReportPayload:
        return ReportPayload(
            summary=self.get_summary(),
            tables=self.list_tables(),
            press_insights=self.get_press_insights(),
            available_reports=[
                ReportOption(
                    id=0,
                    year=2025,
                    title="2025 통계연보 더미",
                    file_name=FALLBACK_ORIGINAL_FILE,
                    imported_at="",
                    table_count=len(self._tables),
                )
            ],
        )

    def get_summary(self) -> ReportSummary:
        issue_counts: dict[str, int] = {}
        for table in self._tables:
            for check in table.checks:
                if check.status != "정상":
                    issue_counts[check.type] = issue_counts.get(check.type, 0) + 1

        return ReportSummary(
            report_id=0,
            file_name=FALLBACK_ORIGINAL_FILE,
            base_year="2025",
            total_tables=len(self._tables),
            normal_count=sum(1 for table in self._tables if table.status == "normal"),
            needs_review_count=sum(1 for table in self._tables if table.status == "needs_review"),
            suspected_error_count=sum(1 for table in self._tables if table.status == "suspected_error"),
            issue_counts=issue_counts,
        )

    def list_tables(self, report_id: int | None = None) -> list[StatTable]:
        return self._tables

    def get_table(self, table_id: str, report_id: int | None = None) -> StatTable | None:
        return next((table for table in self._tables if table.id == table_id), None)

    def get_press_insights(self) -> list[PressInsight]:
        return [
            PressInsight(
                id="press-committee-jeju",
                table_id="local-government-committees",
                title="제주, 위원회 평균회의 개최횟수 1위",
                body="제주는 위원회 수는 338개로 적지만 평균회의 개최횟수는 4.9회로 전국에서 가장 높습니다.",
                tone="notable",
            ),
            PressInsight(
                id="press-pedestrian-seoul",
                table_id="pedestrian-districts",
                title="보행환경개선지구 서울 집중",
                body="전국 226개소 중 서울이 76개소로 33.6%를 차지해 지정 규모가 가장 큽니다.",
                tone="notable",
            ),
            PressInsight(
                id="press-fund-balance",
                table_id="disaster-relief-fund",
                title="재해구호기금 연말잔액 증가",
                body="2024년 전국 재해구호기금 연말잔액은 751,852백만원으로 2023년 말 대비 122,712백만원 증가했습니다.",
                tone="increase",
            ),
        ]


def get_report_service() -> ReportService:
    from app.services.sqlite_report_service import SQLiteReportService

    sqlite_service = SQLiteReportService()
    if sqlite_service.is_available():
        return sqlite_service  # type: ignore[return-value]
    return ReportService()
