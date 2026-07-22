from __future__ import annotations

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.ingest.legacy_system_importer import (
    LegacyColumn,
    LegacyRecord,
    LegacySystemTable,
    column_label_candidates,
    find_base_table,
    find_target_row,
    overlay_base_table,
    semantic_label_candidates,
)
from app.ingest.repository import ImportedTable, ReportImportRepository


class LegacySystemImporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = ImportedTable(
            code="4-1-6-1",
            title="유형별 조례·규칙 보유",
            matrix=[
                ["구분", "총계", "조례", "규칙"],
                ["계", "10", "7", "3"],
                ["서울", "4", "3", "1"],
            ],
            header_count=1,
        )
        self.source = LegacySystemTable(
            system_table_id="M12345678",
            title="유형별 조례･규칙 보유",
            department="선거의회자치법규과",
            officer="담당자",
            reference_period="2025",
            columns=(
                LegacyColumn("10001", "구분", "구분", "문자", 1, 0),
                LegacyColumn("10002", "총계", "총계", "숫자", 2, 1),
                LegacyColumn("10003", "조례", "총계>조례", "숫자", 3, 2),
                LegacyColumn("10004", "규칙", "총계>규칙", "숫자", 4, 3),
            ),
            records=(
                LegacyRecord(1, {"10001": "계", "10002": "12", "10003": "8", "10004": "4"}),
                LegacyRecord(2, {"10001": "서울", "10002": "5", "10003": "4", "10004": "1"}),
            ),
        )

    def test_finds_base_table_by_normalized_title(self) -> None:
        table, confidence = find_base_table(self.source, [self.base])
        self.assertIs(table, self.base)
        self.assertEqual(confidence, 1.0)

    def test_overlays_only_matching_cells(self) -> None:
        table, changes, mapped_values = overlay_base_table(self.base, self.source)
        self.assertEqual(changes, 5)
        self.assertEqual(mapped_values, 6)
        self.assertEqual(table.matrix[1], ["계", "12", "8", "4"])
        self.assertEqual(table.matrix[2], ["서울", "5", "4", "1"])

    def test_unchanged_source_values_still_count_as_a_compatible_overlay(self) -> None:
        source = LegacySystemTable(
            **{**self.source.__dict__, "records": (LegacyRecord(1, {"10001": "계", "10002": "10", "10003": "7", "10004": "3"}),)}
        )
        _, changes, mapped_values = overlay_base_table(self.base, source)
        self.assertEqual(changes, 0)
        self.assertEqual(mapped_values, 3)

    def test_column_path_keeps_leaf_label_as_a_mapping_candidate(self) -> None:
        column = self.source.columns[2]
        self.assertEqual(column_label_candidates(column), ("총계>조례", "조례"))

    def test_single_period_source_targets_the_matching_year_row(self) -> None:
        row_index = find_target_row(
            [["구분", "값"], ["2024", "12"], ["2025", "14"]],
            1,
            [],
            LegacyRecord(1, {"10001": "15"}),
            reference_period="2025",
        )
        self.assertEqual(row_index, 2)

    def test_bilingual_header_candidates_include_the_korean_label_without_unit(self) -> None:
        candidates = semantic_label_candidates(
            "원스톱민원창구 설치·운영\n(기관수)\nEstablishment and Operation"
        )
        self.assertIn("원스톱민원창구 설치·운영", candidates)

    def test_overlay_appends_a_missing_reference_year_without_filling_unknown_cells(self) -> None:
        base = ImportedTable(
            code="test",
            title="연도별 값",
            matrix=[["연도", "값"], ["2024", "12"]],
            header_count=1,
        )
        source = LegacySystemTable(
            system_table_id="M12345679",
            title="연도별 값",
            department="",
            officer="",
            reference_period="2025",
            columns=(LegacyColumn("10001", "값", "값", "숫자", 1, 0),),
            records=(LegacyRecord(1, {"10001": "15"}),),
        )
        table, changes, mapped_values = overlay_base_table(base, source)
        self.assertEqual(changes, 1)
        self.assertEqual(mapped_values, 1)
        self.assertEqual(table.matrix[-1], ["2025", "15"])

    def test_reuploading_identical_source_reuses_the_existing_report(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "overlay.json"
            source.write_text("same source", encoding="utf-8")
            repository = ReportImportRepository(root / "test.sqlite")

            first = repository.replace_report(
                source_path=source,
                year=2026,
                title="2026 테스트 통계",
                tables=[self.base],
                archive_previous_same_title=True,
            )
            second = repository.replace_report(
                source_path=source,
                year=2026,
                title="2026 테스트 통계",
                tables=[self.base],
                archive_previous_same_title=True,
            )

            self.assertEqual(second.report_id, first.report_id)
            self.assertEqual(second.table_count, 1)
            connection = sqlite3.connect(root / "test.sqlite")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM annual_reports").fetchone()[0], 1)

    def test_new_test_source_archives_the_previous_test_report(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first_source = root / "first.json"
            second_source = root / "second.json"
            first_source.write_text("first source", encoding="utf-8")
            second_source.write_text("second source", encoding="utf-8")
            repository = ReportImportRepository(root / "test.sqlite")

            first = repository.replace_report(
                source_path=first_source,
                year=2026,
                title="2026 테스트 통계",
                tables=[self.base],
                archive_previous_same_title=True,
            )
            second = repository.replace_report(
                source_path=second_source,
                year=2026,
                title="2026 테스트 통계",
                tables=[self.base],
                archive_previous_same_title=True,
            )

            connection = sqlite3.connect(root / "test.sqlite")
            archived = connection.execute(
                "SELECT is_archived FROM annual_reports WHERE id = ?",
                (first.report_id,),
            ).fetchone()[0]
            visible = connection.execute(
                "SELECT is_archived FROM annual_reports WHERE id = ?",
                (second.report_id,),
            ).fetchone()[0]
            self.assertEqual(archived, 1)
            self.assertEqual(visible, 0)
