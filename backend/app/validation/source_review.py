from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re
import sqlite3

from app.core.numeric_text import numeric_text_anomaly
from app.db.connection import connect
from app.db.schema import init_db
from app.validation.blue_review import normalize_review_text


SOURCE_FORMAT_RULE_ID = "source.format_review"
SOURCE_FORMAT_TYPE = "오탈자 검수"


def append_source_format_review_checks(
    db_path: Path,
    *,
    report_id: int,
    run_id: int,
) -> int:
    """Store deterministic LLM candidates without changing source values."""

    with connect(db_path) as connection:
        init_db(connection)
        tables = connection.execute(
            "SELECT id, unit FROM stat_tables WHERE report_id = ? ORDER BY table_order, id",
            (report_id,),
        ).fetchall()
        cells = connection.execute(
            """
            SELECT c.table_id, c.row_index, c.col_index, c.text_value,
                   c.is_header, c.footnote_marker
            FROM stat_table_cells c
            JOIN stat_tables st ON st.id = c.table_id
            WHERE st.report_id = ?
            ORDER BY c.table_id, c.row_index, c.col_index
            """,
            (report_id,),
        ).fetchall()

        table_units = {int(row["id"]): row["unit"] for row in tables}
        peers_by_column: dict[tuple[int, int], list[str]] = defaultdict(list)
        for cell in cells:
            if not cell["is_header"] and cell["text_value"].strip():
                peers_by_column[(int(cell["table_id"]), int(cell["col_index"]))].append(cell["text_value"])

        existing = existing_source_candidates(connection, run_id)
        inserted = 0
        with connection:
            for cell in cells:
                text = normalize_review_text(cell["text_value"])
                if not text:
                    continue
                table_id = int(cell["table_id"])
                row_index = int(cell["row_index"])
                col_index = int(cell["col_index"])
                key = (table_id, row_index, col_index, text)
                if key in existing:
                    continue

                anomaly = numeric_text_anomaly(
                    text,
                    unit=table_units.get(table_id, ""),
                    peer_values=peers_by_column.get((table_id, col_index), []),
                )
                if anomaly is not None and anomaly.reason.startswith("숫자 뒤에") and (
                    cell["is_header"] or (cell["footnote_marker"] or "").strip()
                ):
                    anomaly = None
                if anomaly is not None:
                    insert_source_candidate(
                        connection,
                        run_id=run_id,
                        table_id=table_id,
                        row_index=row_index,
                        col_index=col_index,
                        current_value=text,
                        expected_value=anomaly.suggested_value or "LLM 문맥 확인",
                        reason=anomaly.reason,
                        label="LLM 숫자 표기 검수 후보",
                    )
                    existing.add(key)
                    inserted += 1
                    continue

                punctuation_reason = suspicious_punctuation_reason(text)
                if punctuation_reason:
                    insert_source_candidate(
                        connection,
                        run_id=run_id,
                        table_id=table_id,
                        row_index=row_index,
                        col_index=col_index,
                        current_value=text,
                        expected_value="LLM 문맥 확인",
                        reason=punctuation_reason,
                        label="LLM 특수문자·오탈자 검수 후보",
                    )
                    existing.add(key)
                    inserted += 1

            if inserted:
                connection.execute(
                    "UPDATE validation_runs SET issue_count = issue_count + ? WHERE id = ?",
                    (inserted, run_id),
                )
        return inserted


def suspicious_punctuation_reason(value: str) -> str:
    if "�" in value or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", value):
        return "문자 깨짐 또는 제어문자가 포함되어 있습니다."
    if re.search(r"(?<=\d)[ㅡㆍ'’`´](?=\d)", value):
        return "숫자 사이에 잘못 입력된 특수문자가 있을 가능성이 있습니다."
    if re.search(r"(?<=[가-힣A-Za-z0-9])[,.;:]{2,}(?=[가-힣A-Za-z0-9])", value):
        return "단어 또는 숫자 사이의 문장부호가 중복되었습니다."
    return ""


def existing_source_candidates(
    connection: sqlite3.Connection,
    run_id: int,
) -> set[tuple[int, int, int, str]]:
    rows = connection.execute(
        """
        SELECT table_id, row_index, col_index, current_value
        FROM validation_issues
        WHERE run_id = ?
          AND row_index IS NOT NULL
          AND col_index IS NOT NULL
        """,
        (run_id,),
    ).fetchall()
    return {
        (
            int(row["table_id"]),
            int(row["row_index"]),
            int(row["col_index"]),
            normalize_review_text(row["current_value"]),
        )
        for row in rows
    }


def insert_source_candidate(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    table_id: int,
    row_index: int,
    col_index: int,
    current_value: str,
    expected_value: str,
    reason: str,
    label: str,
) -> None:
    location = f"{row_index + 1}행 {col_index + 1}열"
    detail = f"규칙 엔진이 원문 표기 이상 후보를 발견했습니다. {reason} 담당자 또는 LLM이 같은 행·열 문맥을 확인해야 합니다."
    connection.execute(
        """
        INSERT INTO validation_issues (
            run_id, table_id, rule_id, issue_type, location, row_index,
            col_index, current_value, expected_value, difference, status,
            severity, detail, formula
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '확인 필요', 'warning', ?, NULL)
        """,
        (
            run_id,
            table_id,
            SOURCE_FORMAT_RULE_ID,
            SOURCE_FORMAT_TYPE,
            location,
            row_index,
            col_index,
            current_value,
            expected_value,
            reason,
            detail,
        ),
    )
    connection.execute(
        """
        INSERT INTO validation_checks (
            run_id, table_id, profile_id, rule_id, check_type, check_label,
            location, row_index, col_index, current_value, expected_value,
            difference, status, severity, detail, formula, confidence
        )
        VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, '확인 필요', 'warning', ?, NULL, 0.75)
        """,
        (
            run_id,
            table_id,
            SOURCE_FORMAT_RULE_ID,
            SOURCE_FORMAT_TYPE,
            label,
            location,
            row_index,
            col_index,
            current_value,
            expected_value,
            reason,
            detail,
        ),
    )
