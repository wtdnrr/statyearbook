from app.services.database_report_service import DatabaseReportService


def get_report_service() -> DatabaseReportService:
    """Return the database-backed report reader for both local and production.

    Database selection is delegated to ``app.db.connection``: local runs use
    SQLite unless configured otherwise, while Railway uses PostgreSQL through
    ``DATABASE_URL``. An empty database naturally returns an empty payload, so
    no dummy-data fallback is required.
    """

    return DatabaseReportService()
