from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from app.db.schema import connect, init_db
from app.validation.glossary_audit import (
    quarantine_known_spelling_contamination,
    reset_rejected_dictionary_candidates,
)
from app.validation.linguistic_policy import SPELLING_CHECK_TYPE, TRANSLATION_CHECK_TYPE
from app.validation.llm_translation_review import (
    SourceReviewItem,
    glossary_decision_for,
    normalize_decision,
    recover_incomplete_bilingual_decision,
)
from app.validation.translation_glossary import refresh_translation_glossary


class GlossaryAuditTest(unittest.TestCase):
    def test_known_typo_is_quarantined_and_survives_glossary_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "audit.sqlite"
            with connect(db_path) as connection:
                init_db(connection)
                report_id, table_id = self.insert_report_and_table(connection)
                connection.execute(
                    """
                    INSERT INTO translation_glossary (
                        origin_key, source_text, source_normalized, target_text,
                        target_normalized, source_kind, source_report_id,
                        source_table_id, status
                    ) VALUES (
                        'report:1:election', '선거관리 위원회', '선거관리위원회',
                        'National Ele7ction Commission', 'national ele7ction commission',
                        'yearbook', ?, ?, 'reference'
                    )
                    """,
                    (report_id, table_id),
                )

                self.assertEqual(quarantine_known_spelling_contamination(connection), 1)
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM translation_glossary WHERE origin_key='report:1:election'"
                    ).fetchone()["status"],
                    "rejected",
                )
                refresh_translation_glossary(connection)
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM translation_glossary WHERE origin_key='report:1:election'"
                    ).fetchone()["status"],
                    "rejected",
                )

    def test_explicit_spelling_rule_runs_before_exact_glossary_match(self) -> None:
        item = SourceReviewItem(
            issue_id=1,
            source_rule_id="source.linguistic_review",
            candidate_kind="language_spelling",
            candidate_reason="전수 검수",
            candidate_expected="",
            table_id=1,
            table_code="1-2-1-4",
            table_title="계급별 공무원 정원",
            table_title_en="",
            unit="명",
            base_date="",
            location="1행 9열",
            row_index=0,
            col_index=8,
            current_value="선거관리 위원회 National Ele7ction Commission",
            cell_text="선거관리 위원회 National Ele7ction Commission",
            row_label="헤더",
            column_label="선거관리 위원회",
            surrounding_rows=[],
            requested_review_type=SPELLING_CHECK_TYPE,
            korean_text="선거관리 위원회",
            english_text="National Ele7ction Commission",
            glossary_matches=[
                {
                    "source": "선거관리 위원회",
                    "target": "National Ele7ction Commission",
                    "status": "llm_verified",
                }
            ],
        )

        decision = glossary_decision_for(item)

        self.assertIsNotNone(decision)
        self.assertEqual(decision["status"], "오류 의심")  # type: ignore[index]
        self.assertEqual(
            decision["expected_value"],  # type: ignore[index]
            "선거관리 위원회 National Election Commission",
        )
        normalized = normalize_decision({"id": item.issue_id, **decision}, item)  # type: ignore[arg-type]
        self.assertEqual(normalized["status"], "오류 의심")
        self.assertEqual(
            normalized["expected_value"],
            "선거관리 위원회 National Election Commission",
        )

    def test_rejected_pair_resets_dictionary_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "reset.sqlite"
            with connect(db_path) as connection:
                init_db(connection)
                report_id, table_id = self.insert_report_and_table(connection)
                run_id = int(
                    connection.execute(
                        """
                        INSERT INTO validation_runs (
                            report_id, rules_version, started_at, completed_at, issue_count
                        ) VALUES (?, 'test', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)
                        """,
                        (report_id,),
                    ).lastrowid
                )
                connection.execute(
                    """
                    INSERT INTO translation_glossary (
                        origin_key, source_text, source_normalized, target_text,
                        target_normalized, source_kind, status
                    ) VALUES (
                        'rejected:election', '선거관리 위원회', '선거관리위원회',
                        'National Ele7ction Commission', 'national ele7ction commission',
                        'yearbook', 'rejected'
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO linguistic_review_candidates (
                        run_id, table_id, review_type, candidate_kind, location,
                        row_index, col_index, current_value, korean_text, english_text,
                        status, reviewed_model, review_result_json, resolution_source
                    ) VALUES (?, ?, ?, 'language_spelling', '1행 9열', 0, 8, ?, ?, ?,
                              'reviewed', 'official-glossary', '{}', 'dictionary')
                    """,
                    (
                        run_id,
                        table_id,
                        SPELLING_CHECK_TYPE,
                        "선거관리 위원회 National Ele7ction Commission",
                        "선거관리 위원회",
                        "National Ele7ction Commission",
                    ),
                )

                reset, affected_runs = reset_rejected_dictionary_candidates(connection)

                self.assertEqual(reset, 1)
                self.assertEqual(affected_runs, [run_id])
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM linguistic_review_candidates WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()["status"],
                    "pending",
                )

    def test_rejected_pair_does_not_mutate_historical_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "historical.sqlite"
            with connect(db_path) as connection:
                init_db(connection)
                report_id, table_id = self.insert_report_and_table(connection)
                old_run_id = self.insert_run(connection, report_id)
                self.insert_run(connection, report_id)
                connection.execute(
                    """
                    INSERT INTO translation_glossary (
                        origin_key, source_text, source_normalized, target_text,
                        target_normalized, source_kind, status
                    ) VALUES (
                        'rejected:election', '선거관리 위원회', '선거관리위원회',
                        'National Ele7ction Commission', 'national ele7ction commission',
                        'yearbook', 'rejected'
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO linguistic_review_candidates (
                        run_id, table_id, review_type, candidate_kind, location,
                        current_value, korean_text, english_text, status,
                        reviewed_model, review_result_json, resolution_source
                    ) VALUES (?, ?, ?, 'language_spelling', '1행 9열', ?, ?, ?,
                              'reviewed', 'official-glossary', '{}', 'dictionary')
                    """,
                    (
                        old_run_id,
                        table_id,
                        SPELLING_CHECK_TYPE,
                        "선거관리 위원회 National Ele7ction Commission",
                        "선거관리 위원회",
                        "National Ele7ction Commission",
                    ),
                )

                reset, affected_runs = reset_rejected_dictionary_candidates(connection)

                self.assertEqual((reset, affected_runs), (0, []))
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM linguistic_review_candidates WHERE run_id = ?",
                        (old_run_id,),
                    ).fetchone()["status"],
                    "reviewed",
                )

    def test_incomplete_single_pair_replacement_preserves_korean(self) -> None:
        item = self.translation_item(
            current="주택 House",
            korean="주택",
            english="House",
        )
        raw = {
            "id": item.issue_id,
            "context_token": "",
            "issue_type": TRANSLATION_CHECK_TYPE,
            "status": "확인 필요",
            "defect_kind": "translation_mismatch",
            "current_value": "주택 House",
            "expected_value": "Housing",
            "difference": "번역 불일치",
            "detail": "주택의 영문을 확인했습니다.",
        }
        raw["context_token"] = __import__(
            "app.validation.llm_translation_review", fromlist=["review_context_token"]
        ).review_context_token(item)

        recovered = recover_incomplete_bilingual_decision(raw, item)

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["expected_value"], "주택 Housing")  # type: ignore[index]

    def test_incomplete_multi_pair_replacement_is_not_an_issue(self) -> None:
        item = self.translation_item(
            current="구분 Classification 민원서류 Classification",
            korean="구분 / 민원서류",
            english="Classification / Classification",
        )
        raw = {
            "id": item.issue_id,
            "context_token": "",
            "issue_type": TRANSLATION_CHECK_TYPE,
            "status": "확인 필요",
            "defect_kind": "translation_mismatch",
            "current_value": item.current_value,
            "expected_value": "Classification",
            "difference": "번역 불일치",
            "detail": "복합 헤더입니다.",
        }
        raw["context_token"] = __import__(
            "app.validation.llm_translation_review", fromlist=["review_context_token"]
        ).review_context_token(item)

        recovered = recover_incomplete_bilingual_decision(raw, item)

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["status"], "정상")  # type: ignore[index]
        self.assertEqual(recovered["expected_value"], "__UNCHANGED__")  # type: ignore[index]

    @staticmethod
    def insert_report_and_table(connection) -> tuple[int, int]:
        report_id = int(
            connection.execute(
                """
                INSERT INTO annual_reports (
                    year, title, source_file_name, source_file_path, file_hash, imported_at
                ) VALUES (2026, '연보', 'test.md', 'test.md', 'audit', CURRENT_TIMESTAMP)
                """
            ).lastrowid
        )
        table_id = int(
            connection.execute(
                """
                INSERT INTO stat_tables (report_id, code, title, table_order)
                VALUES (?, '1-2-1-4', '계급별 공무원 정원', 1)
                """,
                (report_id,),
            ).lastrowid
        )
        return report_id, table_id

    @staticmethod
    def insert_run(connection, report_id: int) -> int:
        return int(
            connection.execute(
                """
                INSERT INTO validation_runs (
                    report_id, rules_version, started_at, completed_at, issue_count
                ) VALUES (?, 'test', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)
                """,
                (report_id,),
            ).lastrowid
        )

    @staticmethod
    def translation_item(*, current: str, korean: str, english: str) -> SourceReviewItem:
        return SourceReviewItem(
            issue_id=1,
            source_rule_id="source.linguistic_review",
            candidate_kind="translation_pair",
            candidate_reason="전수 검수",
            candidate_expected="",
            table_id=1,
            table_code="test",
            table_title="검수",
            table_title_en="Review",
            unit="",
            base_date="",
            location="1행 1열",
            row_index=0,
            col_index=0,
            current_value=current,
            cell_text=current,
            row_label="",
            column_label="",
            surrounding_rows=[],
            requested_review_type=TRANSLATION_CHECK_TYPE,
            korean_text=korean,
            english_text=english,
            glossary_matches=[],
        )


if __name__ == "__main__":
    unittest.main()
