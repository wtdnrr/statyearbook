from __future__ import annotations

from dataclasses import dataclass
import re
import sqlite3

from app.validation.blue_review import normalize_review_text
from app.validation.linguistic_policy import (
    SPELLING_CHECK_TYPE,
    TRANSLATION_CHECK_TYPE,
)
from app.validation.translation_glossary import (
    GlossaryEntry,
    extract_bilingual_pair,
    glossary_context_json,
    glossary_entries_for_text,
    refresh_translation_glossary,
)
from app.validation.models import restore_hyphenated_line_breaks


LINGUISTIC_CANDIDATE_RULE_ID = "source.linguistic_review"
LINGUISTIC_PROMPT_VERSION = "language-review-v6-without-terminology"
HANGUL_RE = re.compile(r"[가-힣]")
LATIN_RE = re.compile(r"[A-Za-z]")
MAX_REVIEW_CHARS = 1400
EXCLUDED_METADATA_REVIEW_LOCATIONS = frozenset({"분야", "단위", "기준일", "주석", "출처"})


@dataclass(frozen=True)
class LinguisticReviewSummary:
    candidates: int
    glossary_checks: int
    glossary_issues: int


def clear_linguistic_review_run(
    connection: sqlite3.Connection,
    *,
    report_id: int,
    run_id: int,
) -> None:
    """Remove generated language artifacts while preserving calculation validation."""

    language_rule_ids = (
        "llm.spelling_review",
        "llm.terminology_review",
        "llm.translation_review",
        "source.blue_text_review",
        "source.blue_text_review.preview",
    )
    placeholders = ", ".join("?" for _ in language_rule_ids)
    connection.execute(
        f"DELETE FROM validation_issues WHERE run_id = ? AND rule_id IN ({placeholders})",
        (run_id, *language_rule_ids),
    )
    connection.execute(
        f"DELETE FROM validation_checks WHERE run_id = ? AND rule_id IN ({placeholders})",
        (run_id, *language_rule_ids),
    )
    connection.execute("DELETE FROM linguistic_review_candidates WHERE run_id = ?", (run_id,))
    connection.execute(
        """
        DELETE FROM translation_glossary
        WHERE source_kind = 'llm' AND status = 'llm_reviewed'
          AND source_table_id IN (SELECT id FROM stat_tables WHERE report_id = ?)
        """,
        (report_id,),
    )
    connection.execute(
        "DELETE FROM linguistic_review_cache WHERE prompt_version <> ?",
        (LINGUISTIC_PROMPT_VERSION,),
    )
    restore_report_hyphenated_line_breaks(connection, report_id)
    connection.execute(
        """
        UPDATE validation_runs
        SET issue_count = (
            SELECT COUNT(*) FROM validation_issues WHERE validation_issues.run_id = validation_runs.id
        )
        WHERE id = ?
        """,
        (run_id,),
    )


def restore_report_hyphenated_line_breaks(
    connection: sqlite3.Connection,
    report_id: int,
) -> None:
    for row in connection.execute(
        """
        SELECT c.id, c.text_value
        FROM stat_table_cells c
        JOIN stat_tables st ON st.id = c.table_id
        WHERE st.report_id = ? AND c.text_value LIKE '%-%'
        """,
        (report_id,),
    ).fetchall():
        current = str(row["text_value"] or "")
        restored = restore_hyphenated_line_breaks(current)
        if restored != current:
            connection.execute(
                "UPDATE stat_table_cells SET text_value = ? WHERE id = ?",
                (restored, int(row["id"])),
            )

    title_fields = ("title", "title_en", "section_title", "section_title_en")
    for row in connection.execute(
        f"SELECT id, {', '.join(title_fields)} FROM stat_tables WHERE report_id = ?",
        (report_id,),
    ).fetchall():
        updates = {
            field: restore_hyphenated_line_breaks(str(row[field] or ""))
            for field in title_fields
            if restore_hyphenated_line_breaks(str(row[field] or "")) != str(row[field] or "")
        }
        if not updates:
            continue
        assignments = ", ".join(f"{field} = ?" for field in updates)
        connection.execute(
            f"UPDATE stat_tables SET {assignments} WHERE id = ?",
            (*updates.values(), int(row["id"])),
        )


def prepare_linguistic_reviews(
    connection: sqlite3.Connection,
    *,
    report_id: int,
    run_id: int,
) -> LinguisticReviewSummary:
    """Queue only the language checks required by each source value."""

    refresh_translation_glossary(connection)
    candidate_count = 0

    tables = connection.execute(
        """
        SELECT id, code, title, title_en, section_title, section_title_en, raw_context
        FROM stat_tables
        WHERE report_id = ?
        ORDER BY table_order, id
        """,
        (report_id,),
    ).fetchall()

    with connection:
        blue_keys = blue_review_candidate_keys(connection, run_id)
        for table in tables:
            table_id = int(table["id"])
            document_values = (
                ("표 제목", joined_bilingual_value(str(table["title"]), str(table["title_en"]))),
                (
                    "대제목",
                    joined_bilingual_value(str(table["section_title"]), str(table["section_title_en"])),
                ),
                ("하위표 제목", extract_subtable_caption(str(table["raw_context"] or ""))),
            )
            for field_name, field_value in document_values:
                value = normalize_review_text(str(field_value or ""))
                if not has_language_text(value):
                    continue
                if review_location_key(
                    table_id,
                    field_name,
                    None,
                    None,
                    value,
                ) in blue_keys:
                    continue
                candidate_count += review_text_value(
                    connection,
                    run_id=run_id,
                    report_id=report_id,
                    table_id=table_id,
                    location=field_name,
                    row_index=None,
                    col_index=None,
                    current_value=value,
                )

            cells = connection.execute(
                """
                SELECT row_index, col_index, text_value, numeric_value, is_header
                FROM stat_table_cells
                WHERE table_id = ?
                ORDER BY row_index, col_index
                """,
                (table_id,),
            ).fetchall()
            for cell in cells:
                value = normalize_review_text(str(cell["text_value"] or ""))
                if not has_language_text(value):
                    continue
                row_index = int(cell["row_index"])
                col_index = int(cell["col_index"])
                location = f"{row_index + 1}행 {col_index + 1}열"
                if review_location_key(
                    table_id,
                    location,
                    row_index,
                    col_index,
                    value,
                ) in blue_keys:
                    continue
                candidate_count += review_text_value(
                    connection,
                    run_id=run_id,
                    report_id=report_id,
                    table_id=table_id,
                    location=location,
                    row_index=row_index,
                    col_index=col_index,
                    current_value=value,
                )

        hydrate_pending_glossary_context(
            connection,
            run_id=run_id,
            report_id=report_id,
        )

    return LinguisticReviewSummary(
        candidates=candidate_count,
        glossary_checks=0,
        glossary_issues=0,
    )


def hydrate_pending_glossary_context(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    report_id: int,
) -> None:
    """Attach current glossary evidence to every pending language/blue candidate."""

    rows = connection.execute(
        """
        SELECT id, current_value
        FROM linguistic_review_candidates
        WHERE run_id = ? AND status = 'pending' AND prompt_version = ''
        """,
        (run_id,),
    ).fetchall()
    for row in rows:
        current_value = str(row["current_value"])
        entries = glossary_entries_for_text(
            connection,
            current_value,
            current_report_id=report_id,
        )
        pair = extract_bilingual_pair(current_value)
        korean_text = pair[0] if pair else korean_only_text(current_value)
        english_text = pair[1] if pair else english_only_text(current_value)
        connection.execute(
            """
            UPDATE linguistic_review_candidates
            SET glossary_json = ?, prompt_version = ?,
                korean_text = CASE WHEN korean_text = '' THEN ? ELSE korean_text END,
                english_text = CASE WHEN english_text = '' THEN ? ELSE english_text END
            WHERE id = ?
            """,
            (
                glossary_context_json(entries),
                LINGUISTIC_PROMPT_VERSION,
                korean_text,
                english_text,
                int(row["id"]),
            ),
        )


def review_text_value(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    report_id: int,
    table_id: int,
    location: str,
    row_index: int | None,
    col_index: int | None,
    current_value: str,
) -> int:
    chunks = split_review_chunks(current_value)
    if len(chunks) > 1:
        return sum(
            review_text_value(
                connection,
                run_id=run_id,
                report_id=report_id,
                table_id=table_id,
                location=f"{location} ({index}/{len(chunks)})",
                row_index=row_index,
                col_index=col_index,
                current_value=chunk,
            )
            for index, chunk in enumerate(chunks, start=1)
        )

    pair = extract_bilingual_pair(current_value)
    korean_text = pair[0] if pair else korean_only_text(current_value)
    english_text = pair[1] if pair else english_only_text(current_value)
    entries = glossary_entries_for_text(
        connection,
        current_value,
        current_report_id=report_id,
    )
    glossary_entries = entries
    candidates = 0
    review_specs: list[tuple[str, str, str]] = [
        (
            SPELLING_CHECK_TYPE,
            "language_spelling",
            "위치별 원문 전체를 LLM으로 검토하여 국문·영문 철자, 문자 깨짐, 숫자 혼입 및 문맥상 표기 오류를 확인",
        ),
    ]
    if korean_text or HANGUL_RE.search(current_value):
        review_specs.append((
            TRANSLATION_CHECK_TYPE,
            translation_candidate_kind(current_value, pair),
            "위치별 원문 전체를 LLM으로 검토하여 한국어와 영어의 의미 대응, 번역 누락 및 공식 고유명사를 확인",
        ))
    for review_type, candidate_kind, reason in review_specs:
        candidates += insert_candidate_once(
            connection,
            run_id=run_id,
            table_id=table_id,
            review_type=review_type,
            candidate_kind=candidate_kind,
            location=location,
            row_index=row_index,
            col_index=col_index,
            current_value=current_value,
            korean_text=korean_text,
            english_text=english_text,
            glossary_entries=glossary_entries,
            reason=reason,
        )
    return candidates


def blue_review_candidate_keys(
    connection: sqlite3.Connection,
    run_id: int,
) -> set[tuple[int, str, int | None, int | None, str]]:
    rows = connection.execute(
        """
        SELECT table_id, location, row_index, col_index, current_value
        FROM linguistic_review_candidates
        WHERE run_id = ? AND candidate_kind LIKE 'blue_text%'
        """,
        (run_id,),
    ).fetchall()
    return {
        review_location_key(
            int(row["table_id"]),
            str(row["location"]),
            int(row["row_index"]) if row["row_index"] is not None else None,
            int(row["col_index"]) if row["col_index"] is not None else None,
            str(row["current_value"]),
        )
        for row in rows
    }


def review_location_key(
    table_id: int,
    location: str,
    row_index: int | None,
    col_index: int | None,
    current_value: str,
) -> tuple[int, str, int | None, int | None, str]:
    return (
        table_id,
        location,
        row_index,
        col_index,
        normalize_review_text(current_value).casefold(),
    )


def insert_candidate_once(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    table_id: int,
    review_type: str,
    candidate_kind: str,
    location: str,
    row_index: int | None,
    col_index: int | None,
    current_value: str,
    reason: str,
    korean_text: str = "",
    english_text: str = "",
    glossary_entries: list[GlossaryEntry] | None = None,
) -> int:
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO linguistic_review_candidates (
            run_id, table_id, review_type, candidate_kind, location,
            row_index, col_index, current_value, korean_text, english_text,
            glossary_json, reason, prompt_version
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            table_id,
            review_type,
            candidate_kind,
            location,
            row_index,
            col_index,
            current_value,
            korean_text,
            english_text,
            glossary_context_json(glossary_entries or []),
            reason,
            LINGUISTIC_PROMPT_VERSION,
        ),
    )
    return 1 if cursor.rowcount else 0


def joined_bilingual_value(korean: str, english: str) -> str:
    return "\n".join(value for value in (normalize_review_text(korean), normalize_review_text(english)) if value)


def has_language_text(value: str) -> bool:
    return bool(value) and bool(HANGUL_RE.search(value) or LATIN_RE.search(value))


def korean_only_text(value: str) -> str:
    if LATIN_RE.search(value):
        return ""
    return value if HANGUL_RE.search(value) else ""


def english_only_text(value: str) -> str:
    if HANGUL_RE.search(value):
        return ""
    return value if LATIN_RE.search(value) else ""


def translation_candidate_kind(value: str, pair: tuple[str, str] | None) -> str:
    if pair is not None:
        return "translation_pair"
    has_korean = bool(HANGUL_RE.search(value))
    has_english = bool(LATIN_RE.search(value))
    if has_korean and has_english:
        return "translation_mixed_context"
    if has_korean:
        return "translation_missing"
    return "translation_english_context"


def split_review_chunks(value: str, *, max_chars: int = MAX_REVIEW_CHARS) -> list[str]:
    """Split long content without dropping any Korean or English source text."""

    normalized = normalize_review_text(value)
    if len(normalized) <= max_chars:
        return [normalized]

    chunks: list[str] = []
    remaining = normalized
    while len(remaining) > max_chars:
        boundary = max(
            remaining.rfind("\n", 0, max_chars + 1),
            remaining.rfind(". ", 0, max_chars + 1),
            remaining.rfind("; ", 0, max_chars + 1),
            remaining.rfind(", ", 0, max_chars + 1),
            remaining.rfind(" ", 0, max_chars + 1),
        )
        if boundary < max_chars // 2:
            boundary = max_chars
        else:
            boundary += 1
        chunk = remaining[:boundary].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[boundary:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def extract_subtable_caption(raw_context: str) -> str:
    """Extract a visible `▫` subtable caption that is not stored in title columns."""

    if not raw_context or "▫" not in raw_context:
        return ""
    match = re.search(
        r"▫\s*(.*?)(?=\s*\((?:\d{4}|단위\s*:)|\s+(?:구분|연도)\s*(?:\n|$))",
        raw_context[:1200],
        flags=re.DOTALL,
    )
    if match is None:
        return ""
    return normalize_review_text(match.group(1))
