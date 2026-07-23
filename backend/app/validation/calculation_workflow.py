from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.db.connection import DB_PATH
from app.validation.engine import ValidationEngine
from app.validation.profile_repository import ValidationProfileRepository
from app.validation.repository import ValidationRepository


@dataclass(frozen=True)
class CalculationValidationResult:
    report_id: int
    run_id: int
    table_count: int
    issue_count: int


class CalculationValidationWorkflow:
    """Prepare profiles and execute deterministic validation rules.

    This workflow owns no language or LLM behavior. That boundary keeps
    arithmetic validation reproducible and makes it safe to retry without an
    external API.
    """

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._validation_repository = ValidationRepository(db_path)
        self._profile_repository = ValidationProfileRepository(db_path)

    def run(
        self,
        *,
        report_id: int,
        refresh_profiles: bool = False,
    ) -> CalculationValidationResult:
        tables = self._validation_repository.load_tables(report_id)
        profiles = self._profile_repository.ensure_profiles(
            report_id=report_id,
            tables=tables,
            refresh=refresh_profiles,
        )

        engine = ValidationEngine(profiles=profiles)
        outcome = engine.evaluate(tables)
        run_id = self._validation_repository.save_run(
            report_id=report_id,
            rules_version=engine.rules_version,
            issues=outcome.issues,
            checks=outcome.checks,
        )
        return CalculationValidationResult(
            report_id=report_id,
            run_id=run_id,
            table_count=len(tables),
            issue_count=len(outcome.issues),
        )
