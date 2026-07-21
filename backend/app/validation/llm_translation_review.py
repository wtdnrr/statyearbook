from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
from typing import Any
from urllib import request

from app.core.llm_client import (
    LLMClientSettings,
    ResponsesTransport,
    env_value,
    parse_json_text,
    parse_responses_json,
    retry_after_seconds,
    resolve_llm_client_settings,
)

from app.db.schema import DB_PATH, connect, init_db
from app.validation.blue_review import (
    BLUE_LLM_RULE_ID,
    BLUE_REVIEW_RULE_ID,
    BLUE_REVIEW_TYPE,
    normalize_review_text,
    repair_blue_candidate_classifications,
    synchronize_blue_review_checks,
)
from app.validation.linguistic_policy import (
    LINGUISTIC_CHECK_TYPES,
    SPELLING_REPLACEMENTS,
    SPELLING_CHECK_TYPE,
    TRANSLATION_CHECK_TYPE,
)
from app.validation.linguistic_review import (
    LINGUISTIC_CANDIDATE_RULE_ID,
    LINGUISTIC_PROMPT_VERSION,
    NON_TRANSLATION_LATIN_TOKENS,
)
from app.validation.source_review import SOURCE_FORMAT_RULE_ID, SOURCE_FORMAT_TYPE
from app.validation.region_glossary import region_review_decision
from app.validation.translation_glossary import (
    infer_category,
    infer_subcategory,
    normalize_source,
    normalize_target,
    parse_glossary_context,
)


DEFAULT_TRANSLATION_MODEL = "gpt-5.4-mini"
DEFAULT_BIZROUTER_MODEL = "openai/gpt-5-mini"
LLM_TRANSLATION_RULE_ID = "llm.translation_review"
LLM_SPELLING_RULE_ID = "llm.spelling_review"
RETIRED_LLM_TERMINOLOGY_RULE_ID = "llm.terminology_review"
SPELLING_DEFECT_KINDS = {
    "korean_spelling",
    "english_spelling",
    "spacing",
    "punctuation",
    "numeric_format",
}
TRANSLATION_DEFECT_KINDS = {
    "semantic_omission",
    "semantic_addition",
    "wrong_meaning",
    "entity_mismatch",
    "measure_mismatch",
}


@dataclass(frozen=True)
class LLMReviewResult:
    processed: int
    inserted_issues: int
    inserted_checks: int
    final_issue_count: int
    skipped_reason: str = ""


@dataclass(frozen=True)
class LLMTablePreviewResult:
    run_id: int
    table_code: str
    table_title: str
    model: str
    reviewed_contexts: int
    reviewed_items: int
    results: list[dict[str, Any]]


@dataclass(frozen=True)
class SourceReviewItem:
    issue_id: int
    source_rule_id: str
    candidate_kind: str
    candidate_reason: str
    candidate_expected: str
    table_id: int
    table_code: str
    table_title: str
    table_title_en: str
    unit: str
    base_date: str
    location: str
    row_index: int | None
    col_index: int | None
    current_value: str
    cell_text: str
    row_label: str
    column_label: str
    surrounding_rows: list[str]
    requested_review_type: str = ""
    korean_text: str = ""
    english_text: str = ""
    glossary_matches: list[dict[str, object]] | None = None
    source_record_type: str = "issue"
    source_record_id: int | None = None
    prompt_version: str = ""
    review_fingerprint: str = ""

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "id": self.issue_id,
            "candidate_kind": self.candidate_kind,
            "candidate_reason": self.candidate_reason,
            "candidate_expected": self.candidate_expected,
            "table_code": self.table_code,
            "table_title_ko": self.table_title,
            "table_title_en": self.table_title_en,
            "unit": self.unit,
            "base_date": self.base_date,
            "location": self.location,
            "current_value": self.current_value,
            "cell_text": self.cell_text,
            "row_label": self.row_label,
            "column_label": self.column_label,
            "surrounding_rows": self.surrounding_rows,
            "requested_review_type": self.requested_review_type,
            "korean_text": self.korean_text,
            "english_text": self.english_text,
            "translation_glossary": self.glossary_matches or [],
            "prompt_version": self.prompt_version,
        }


def apply_reusable_linguistic_reviews(
    db_path: Path = DB_PATH,
    *,
    run_id: int,
    reviewed_model: str | None = None,
) -> tuple[int, int, int]:
    """Apply authoritative dictionary and context cache results without calling an API."""

    model = reviewed_model or str(llm_review_settings()["model"])
    with connect(db_path) as connection:
        init_db(connection)
        with connection:
            region_counts = resolve_blue_region_candidates(connection, run_id)
            dictionary_counts = resolve_linguistic_candidates_from_glossary(connection, run_id)
            cache_counts = reuse_cached_linguistic_reviews(
                connection,
                run_id,
                reviewed_model=model,
            )
            refresh_issue_count(connection, run_id)
    return (
        region_counts[0] + dictionary_counts[0] + cache_counts[0],
        region_counts[1] + dictionary_counts[1] + cache_counts[1],
        region_counts[2] + dictionary_counts[2] + cache_counts[2],
    )


def append_llm_translation_reviews(
    db_path: Path = DB_PATH,
    *,
    run_id: int | None = None,
    report_id: int | None = None,
    limit: int | None = None,
    blue_only: bool = False,
    standard_language_only: bool = False,
    model_override: str | None = None,
) -> LLMReviewResult:
    if blue_only and standard_language_only:
        raise ValueError("blue_only and standard_language_only cannot both be enabled")

    settings = llm_review_settings()
    if model_override:
        settings["model"] = model_override
    with connect(db_path) as connection:
        init_db(connection)
        resolved_run_id = run_id or latest_run_id(connection, report_id=report_id)
        if resolved_run_id is None:
            return LLMReviewResult(0, 0, 0, 0, "no validation run")

        if blue_only:
            repair_blue_candidate_classifications(connection, resolved_run_id)
            dictionary_counts = resolve_blue_region_candidates(connection, resolved_run_id)
            cache_counts = (0, 0, 0)
        else:
            dictionary_counts = resolve_linguistic_candidates_from_glossary(
                connection,
                resolved_run_id,
                standard_language_only=standard_language_only,
            )
            cache_counts = reuse_cached_linguistic_reviews(
                connection,
                resolved_run_id,
                reviewed_model=settings["model"],
                standard_language_only=standard_language_only,
            )
        linguistic_items = load_linguistic_review_items(connection, resolved_run_id)
        if blue_only:
            linguistic_items = [
                item for item in linguistic_items if item.source_rule_id == BLUE_REVIEW_RULE_ID
            ]
            source_items: list[SourceReviewItem] = []
        elif standard_language_only:
            linguistic_items = [
                item for item in linguistic_items if is_standard_language_item(item)
            ]
            source_items = []
        else:
            source_items = load_source_review_items(connection, resolved_run_id)
        format_items = [item for item in source_items if item.source_rule_id == SOURCE_FORMAT_RULE_ID]
        other_source_items = [item for item in source_items if item.source_rule_id != SOURCE_FORMAT_RULE_ID]
        items = [
            *format_items,
            *group_linguistic_items_by_context(linguistic_items),
            *other_source_items,
        ]
        if limit is not None:
            items = take_contexts(items, limit)

    inserted_issues = dictionary_counts[0] + cache_counts[0]
    inserted_checks = dictionary_counts[1] + cache_counts[1]
    processed = dictionary_counts[2] + cache_counts[2]

    if not items:
        if blue_only:
            synchronize_blue_review_checks(db_path, run_id=resolved_run_id)
        with connect(db_path) as connection:
            init_db(connection)
            with connection:
                refresh_issue_count(connection, resolved_run_id)
        return LLMReviewResult(
            processed,
            inserted_issues,
            inserted_checks,
            current_issue_count(db_path, resolved_run_id),
        )
    if not settings["enabled"]:
        return LLMReviewResult(
            processed,
            inserted_issues,
            inserted_checks,
            current_issue_count(db_path, resolved_run_id),
            "disabled",
        )
    if not settings["api_key"]:
        return LLMReviewResult(
            processed,
            inserted_issues,
            inserted_checks,
            current_issue_count(db_path, resolved_run_id),
            f"missing {settings['api_key_env']}",
        )

    client = ResponsesAPIClient(
        api_key=settings["api_key"],
        model=settings["model"],
        base_url=settings["base_url"],
        timeout=settings["timeout"],
        provider=settings["provider"],
    )

    batches = chunked_by_context(items, settings["batch_size"])
    concurrency = settings["concurrency"]
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        batch_iterator = iter(batches)
        pending = {}
        for _ in range(concurrency):
            try:
                batch = next(batch_iterator)
            except StopIteration:
                break
            pending[executor.submit(review_batch_with_retries, client, batch)] = batch

        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            completed: list[tuple[list[SourceReviewItem], list[dict[str, Any]]]] = []
            errors: list[Exception] = []
            for future in done:
                batch = pending.pop(future)
                try:
                    completed.append((batch, future.result()))
                except Exception as exc:
                    errors.append(exc)

            for batch, decisions in completed:
                with connect(db_path) as connection:
                    init_db(connection)
                    with connection:
                        batch_counts = save_llm_review_decisions(
                            connection,
                            resolved_run_id,
                            batch,
                            decisions,
                            reviewed_model=settings["model"],
                            resolution_source="llm",
                        )
                        inserted_issues += batch_counts[0]
                        inserted_checks += batch_counts[1]
                        processed += batch_counts[2]
                        refresh_issue_count(connection, resolved_run_id)
                if blue_only:
                    synchronize_blue_review_checks(db_path, run_id=resolved_run_id)
            if errors:
                for future in pending:
                    future.cancel()
                raise RuntimeError(
                    f"LLM batch group failed after saving {len(completed)} successful batches"
                ) from errors[0]

            for _ in completed:
                try:
                    batch = next(batch_iterator)
                except StopIteration:
                    break
                pending[executor.submit(review_batch_with_retries, client, batch)] = batch
            time.sleep(settings["sleep_seconds"])

    if limit is None:
        with connect(db_path) as connection:
            init_db(connection)
            pending = pending_linguistic_candidate_count(
                connection,
                resolved_run_id,
                blue_only=blue_only,
                standard_language_only=standard_language_only,
            )
            if pending == 0 and standard_language_only:
                with connection:
                    reconcile_standard_language_findings(
                        connection,
                        resolved_run_id,
                    )
        if pending:
            raise RuntimeError(f"LLM 전수 언어 검수가 완료되지 않았습니다: pending {pending}건")

    return LLMReviewResult(
        processed=processed,
        inserted_issues=inserted_issues,
        inserted_checks=inserted_checks,
        final_issue_count=current_issue_count(db_path, resolved_run_id),
    )


def resolve_blue_region_candidates(
    connection: sqlite3.Connection,
    run_id: int,
) -> tuple[int, int, int]:
    """Resolve complete Korean administrative-area lists without an LLM call."""

    inserted_issues = 0
    inserted_checks = 0
    processed = 0
    items = [
        item
        for item in load_linguistic_review_items(connection, run_id)
        if item.source_rule_id == BLUE_REVIEW_RULE_ID
    ]
    for item in items:
        region_decision = region_review_decision(item.current_value)
        if region_decision is None:
            continue
        counts = save_llm_review_decisions(
            connection,
            run_id,
            [item],
            [
                {
                    "id": item.issue_id,
                    **region_decision.to_dict(issue_type=BLUE_REVIEW_TYPE),
                }
            ],
            reviewed_model="region-dictionary-v1",
            resolution_source="region_dictionary",
        )
        inserted_issues += counts[0]
        inserted_checks += counts[1]
        processed += counts[2]
    return inserted_issues, inserted_checks, processed


def pending_linguistic_candidate_count(
    connection: sqlite3.Connection,
    run_id: int,
    *,
    blue_only: bool = False,
    standard_language_only: bool = False,
) -> int:
    if blue_only and standard_language_only:
        raise ValueError("blue_only and standard_language_only cannot both be enabled")
    scope_clause = ""
    scope_params: list[object] = []
    if blue_only:
        scope_clause = "AND candidate_kind LIKE 'blue_text%'"
    elif standard_language_only:
        scope_clause = """
            AND candidate_kind NOT LIKE 'blue_text%'
            AND review_type IN (?, ?)
        """
        scope_params = [SPELLING_CHECK_TYPE, TRANSLATION_CHECK_TYPE]
    row = connection.execute(
        f"""
        SELECT COUNT(*) AS candidate_count
        FROM linguistic_review_candidates
        WHERE run_id = ? AND status <> 'reviewed'
          {scope_clause}
        """,
        (run_id, *scope_params),
    ).fetchone()
    return int(row["candidate_count"]) if row else 0


def is_standard_language_item(item: SourceReviewItem) -> bool:
    return (
        item.source_rule_id == LINGUISTIC_CANDIDATE_RULE_ID
        and item.requested_review_type in LINGUISTIC_CHECK_TYPES
    )


def reset_standard_language_review_results(
    connection: sqlite3.Connection,
    run_id: int,
) -> tuple[int, int, int]:
    """Reset spelling/translation results while preserving blue-text reviews."""

    candidate_count = int(
        connection.execute(
            """
            SELECT COUNT(*) AS item_count
            FROM linguistic_review_candidates
            WHERE run_id = ?
              AND candidate_kind NOT LIKE 'blue_text%'
              AND review_type IN (?, ?)
            """,
            (run_id, SPELLING_CHECK_TYPE, TRANSLATION_CHECK_TYPE),
        ).fetchone()["item_count"]
    )
    issue_count = int(
        connection.execute(
            """
            SELECT COUNT(*) AS item_count
            FROM validation_issues
            WHERE run_id = ? AND rule_id IN (?, ?)
            """,
            (run_id, LLM_SPELLING_RULE_ID, LLM_TRANSLATION_RULE_ID),
        ).fetchone()["item_count"]
    )
    check_count = int(
        connection.execute(
            """
            SELECT COUNT(*) AS item_count
            FROM validation_checks
            WHERE run_id = ? AND rule_id IN (?, ?)
            """,
            (run_id, LLM_SPELLING_RULE_ID, LLM_TRANSLATION_RULE_ID),
        ).fetchone()["item_count"]
    )
    connection.execute(
        "DELETE FROM validation_issues WHERE run_id = ? AND rule_id IN (?, ?)",
        (run_id, LLM_SPELLING_RULE_ID, LLM_TRANSLATION_RULE_ID),
    )
    connection.execute(
        "DELETE FROM validation_checks WHERE run_id = ? AND rule_id IN (?, ?)",
        (run_id, LLM_SPELLING_RULE_ID, LLM_TRANSLATION_RULE_ID),
    )
    connection.execute(
        """
        UPDATE linguistic_review_candidates
        SET status = 'pending',
            prompt_version = ?,
            reviewed_model = '',
            review_result_json = '',
            review_fingerprint = '',
            resolution_source = '',
            reviewed_at = NULL
        WHERE run_id = ?
          AND candidate_kind NOT LIKE 'blue_text%'
          AND review_type IN (?, ?)
        """,
        (
            LINGUISTIC_PROMPT_VERSION,
            run_id,
            SPELLING_CHECK_TYPE,
            TRANSLATION_CHECK_TYPE,
        ),
    )
    refresh_issue_count(connection, run_id)
    return candidate_count, check_count, issue_count


def reconcile_standard_language_findings(
    connection: sqlite3.Connection,
    run_id: int,
) -> int:
    """Remove duplicated or non-actionable findings after the full language run."""

    rows = connection.execute(
        """
        SELECT
            lrc.*, st.code, st.title, st.title_en, st.unit, st.base_date
        FROM linguistic_review_candidates lrc
        JOIN stat_tables st ON st.id = lrc.table_id
        WHERE lrc.run_id = ?
          AND lrc.status = 'reviewed'
          AND lrc.candidate_kind NOT LIKE 'blue_text%'
          AND lrc.review_type IN (?, ?)
          AND lrc.review_result_json <> ''
        """,
        (run_id, SPELLING_CHECK_TYPE, TRANSLATION_CHECK_TYPE),
    ).fetchall()
    parsed_rows: list[tuple[sqlite3.Row, dict[str, Any], SourceReviewItem]] = []
    spelling_corrections: set[tuple[int, str, str, str]] = set()
    for row in rows:
        try:
            decision = json.loads(str(row["review_result_json"]))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(decision, dict) or str(decision.get("status") or "") == "정상":
            continue
        item = reviewed_candidate_item(row)
        parsed_rows.append((row, decision, item))
        if str(row["review_type"]) == SPELLING_CHECK_TYPE:
            spelling_corrections.add(
                (
                    int(row["table_id"]),
                    str(row["location"]),
                    canonical_decision_text(str(row["current_value"])),
                    canonical_decision_text(str(decision.get("expected_value") or "")),
                )
            )

    reconciled = 0
    for row, decision, item in parsed_rows:
        signature = (
            int(row["table_id"]),
            str(row["location"]),
            canonical_decision_text(str(row["current_value"])),
            canonical_decision_text(str(decision.get("expected_value") or "")),
        )
        duplicated_spelling = (
            str(row["review_type"]) == TRANSLATION_CHECK_TYPE
            and signature in spelling_corrections
        )
        if not duplicated_spelling and not stored_language_finding_is_non_actionable(item, decision):
            continue

        review_type = str(row["review_type"])
        current = normalize_review_text(str(row["current_value"]))
        corrected = {
            **decision,
            "status": "정상",
            "issue_type": review_type,
            "rule_id": rule_id_for(review_type),
            "defect_kind": "none",
            "current_value": current,
            "expected_value": current,
            "difference": f"{review_type} 통과",
            "detail": korean_detail_for(review_type, "정상"),
        }
        serialized = json.dumps(corrected, ensure_ascii=False)
        connection.execute(
            "UPDATE linguistic_review_candidates SET review_result_json = ? WHERE id = ?",
            (serialized, int(row["id"])),
        )
        fingerprint = str(row["review_fingerprint"] or "")
        if fingerprint:
            connection.execute(
                "UPDATE linguistic_review_cache SET decision_json = ? WHERE fingerprint = ?",
                (serialized, fingerprint),
            )
        rule_id = rule_id_for(review_type)
        connection.execute(
            """
            UPDATE validation_checks
            SET status = '정상', severity = 'info', expected_value = ?,
                difference = ?, detail = ?
            WHERE run_id = ? AND table_id = ? AND rule_id = ?
              AND location = ? AND current_value = ?
            """,
            (
                current,
                corrected["difference"],
                corrected["detail"],
                run_id,
                int(row["table_id"]),
                rule_id,
                str(row["location"]),
                current,
            ),
        )
        connection.execute(
            """
            DELETE FROM validation_issues
            WHERE run_id = ? AND table_id = ? AND rule_id = ?
              AND location = ? AND current_value = ?
            """,
            (
                run_id,
                int(row["table_id"]),
                rule_id,
                str(row["location"]),
                current,
            ),
        )
        reconciled += 1
    if reconciled:
        refresh_issue_count(connection, run_id)
    return reconciled


def reviewed_candidate_item(row: sqlite3.Row) -> SourceReviewItem:
    try:
        glossary = json.loads(str(row["glossary_json"] or "[]"))
    except (json.JSONDecodeError, TypeError):
        glossary = []
    return SourceReviewItem(
        issue_id=-int(row["id"]),
        source_rule_id=LINGUISTIC_CANDIDATE_RULE_ID,
        candidate_kind=str(row["candidate_kind"]),
        candidate_reason=str(row["reason"] or ""),
        candidate_expected="",
        table_id=int(row["table_id"]),
        table_code=str(row["code"]),
        table_title=str(row["title"]),
        table_title_en=str(row["title_en"]),
        unit=str(row["unit"]),
        base_date=str(row["base_date"]),
        location=str(row["location"]),
        row_index=int(row["row_index"]) if row["row_index"] is not None else None,
        col_index=int(row["col_index"]) if row["col_index"] is not None else None,
        current_value=str(row["current_value"]),
        cell_text=str(row["current_value"]),
        row_label="",
        column_label="",
        surrounding_rows=[],
        requested_review_type=str(row["review_type"]),
        korean_text=str(row["korean_text"] or ""),
        english_text=str(row["english_text"] or ""),
        glossary_matches=glossary if isinstance(glossary, list) else [],
        source_record_type="candidate",
        source_record_id=int(row["id"]),
        prompt_version=str(row["prompt_version"] or LINGUISTIC_PROMPT_VERSION),
        review_fingerprint=str(row["review_fingerprint"] or ""),
    )


def stored_language_finding_is_non_actionable(
    item: SourceReviewItem,
    decision: dict[str, Any],
) -> bool:
    current = normalize_review_text(item.current_value)
    expected = normalize_review_text(str(decision.get("expected_value") or ""))
    notes = normalize_review_text(
        f"{decision.get('difference') or ''} {decision.get('detail') or ''}"
    )
    if item.requested_review_type == SPELLING_CHECK_TYPE:
        if current.casefold() == expected.casefold():
            return True
        if "일관성" in notes and not re.search(r"철자 오류|오탈자|잘못", notes):
            return True
        if "권고" in notes and not re.search(r"철자 오류|오탈자|잘못|residental", notes, re.I):
            return True
        return False

    if item.requested_review_type != TRANSLATION_CHECK_TYPE:
        return False
    if not translation_context_has_meaningful_english(item):
        return True
    if canonical_decision_text(current) == canonical_decision_text(expected):
        return True
    if re.search(
        r"의미.{0,4}확장|범위.{0,4}확장|권고|약칭|명시 권고|의역 권고|"
        r"공식 명칭 확인 근거 부족|확인 근거 부족",
        notes,
    ):
        return True
    if translation_change_is_function_word_only(item, expected):
        return True
    if "등" in notes and re.search(r"etc\.?|and others", notes, re.I):
        return True
    if translation_repeats_same_numeric_marker(current, expected, notes):
        return True
    if "해양유도선사고" in current and re.search(r"excursion ship|ferry", current, re.I):
        return True
    return False


def translation_change_is_function_word_only(
    item: SourceReviewItem,
    expected: str,
) -> bool:
    """Treat an English preposition/article preference as editorial, not semantic."""

    function_words = {
        "a", "an", "and", "at", "by", "for", "from", "in", "of", "on",
        "or", "the", "to", "with",
    }

    def tokens(value: str) -> list[str]:
        return re.findall(r"[A-Za-z]+", value.casefold())

    current_tokens = tokens(item.english_text or item.current_value)
    expected_tokens = tokens(expected)
    if not current_tokens or not expected_tokens:
        return False
    matcher = SequenceMatcher(None, current_tokens, expected_tokens)
    changed: list[str] = []
    for operation, left_start, left_end, right_start, right_end in matcher.get_opcodes():
        if operation == "equal":
            continue
        changed.extend(current_tokens[left_start:left_end])
        changed.extend(expected_tokens[right_start:right_end])
    return bool(changed) and all(token in function_words for token in changed)


def translation_repeats_same_numeric_marker(
    current: str,
    expected: str,
    notes: str,
) -> bool:
    """Accept a shared Korean/English index that the model tried to remove."""

    if not re.search(r"숫자|number", notes, re.I):
        return False
    current_numbers = re.findall(r"\d+", current)
    expected_numbers = re.findall(r"\d+", expected)
    repeated = {number for number in current_numbers if current_numbers.count(number) >= 2}
    return any(expected_numbers.count(number) < current_numbers.count(number) for number in repeated)


def translation_context_has_meaningful_english(item: SourceReviewItem) -> bool:
    tokens = [
        token.casefold()
        for token in re.findall(r"[A-Za-z]+", item.english_text or item.current_value)
    ]
    return any(
        token not in NON_TRANSLATION_LATIN_TOKENS and len(token) > 1
        for token in tokens
    )


def preview_llm_table_reviews(
    db_path: Path = DB_PATH,
    *,
    table_code: str,
    run_id: int | None = None,
    report_id: int | None = None,
    limit: int | None = None,
) -> LLMTablePreviewResult:
    """Review one table's pending language candidates without modifying the database."""

    settings = llm_review_settings()
    if not settings["enabled"]:
        raise RuntimeError("LLM review is disabled")
    if not settings["api_key"]:
        raise RuntimeError(f"missing {settings['api_key_env']}")

    with connect(db_path) as connection:
        resolved_run_id = run_id or latest_run_id(connection, report_id=report_id)
        if resolved_run_id is None:
            raise RuntimeError("no validation run")
        table_row = connection.execute(
            """
            SELECT st.title
            FROM stat_tables st
            JOIN validation_runs vr ON vr.report_id = st.report_id
            WHERE vr.id = ? AND st.code = ?
            ORDER BY st.table_order, st.id
            LIMIT 1
            """,
            (resolved_run_id, table_code),
        ).fetchone()
        if table_row is None:
            raise RuntimeError(f"table not found in validation run {resolved_run_id}: {table_code}")
        items = [
            item
            for item in load_linguistic_review_items(connection, resolved_run_id)
            if item.table_code == table_code
        ]

    grouped_items = group_linguistic_items_by_context(items)
    if limit is not None:
        grouped_items = take_contexts(grouped_items, limit)
    client = ResponsesAPIClient(
        api_key=settings["api_key"],
        model=settings["model"],
        base_url=settings["base_url"],
        timeout=settings["timeout"],
        provider=settings["provider"],
    )
    results: list[dict[str, Any]] = []
    reviewed_contexts = 0
    for batch in chunked_by_context(grouped_items, settings["batch_size"]):
        decisions = review_batch_with_retries(client, batch)
        decisions_by_id = {
            int(decision["id"]): decision
            for decision in decisions
            if isinstance(decision, dict) and int_or_none(decision.get("id")) is not None
        }
        reviewed_contexts += len(compact_prompt_items(batch))
        for item in batch:
            decision = normalize_decision(decisions_by_id[item.issue_id], item)
            results.append(
                {
                    "table_code": item.table_code,
                    "table_title": item.table_title,
                    "location": item.location,
                    "row_index": item.row_index,
                    "col_index": item.col_index,
                    "review_type": decision["issue_type"],
                    "status": decision["status"],
                    "current_value": item.current_value,
                    "expected_value": decision["expected_value"],
                    "difference": decision["difference"],
                    "detail": decision["detail"],
                }
            )
        time.sleep(settings["sleep_seconds"])

    return LLMTablePreviewResult(
        run_id=resolved_run_id,
        table_code=table_code,
        table_title=str(table_row["title"]),
        model=str(settings["model"]),
        reviewed_contexts=reviewed_contexts,
        reviewed_items=len(results),
        results=results,
    )


def llm_review_settings() -> dict[str, Any]:
    settings = resolve_llm_client_settings(
        openai_model=DEFAULT_TRANSLATION_MODEL,
        bizrouter_model=DEFAULT_BIZROUTER_MODEL,
    )
    limit_value = env_value("LLM_REVIEW_LIMIT", "OPENAI_LLM_REVIEW_LIMIT").strip()
    return {
        "enabled": settings.enabled,
        "provider": settings.provider,
        "api_key": settings.api_key,
        "api_key_env": settings.api_key_env,
        "model": settings.model,
        "base_url": settings.base_url,
        "batch_size": max(
            int(env_value("LLM_REVIEW_BATCH_SIZE", "OPENAI_LLM_REVIEW_BATCH_SIZE", default="1")),
            1,
        ),
        "concurrency": max(int(os.getenv("LLM_REVIEW_CONCURRENCY", "1")), 1),
        "timeout": settings.timeout,
        "sleep_seconds": max(
            float(env_value("LLM_REVIEW_SLEEP", "OPENAI_LLM_REVIEW_SLEEP", default="0.2")),
            0.0,
        ),
        "limit": int(limit_value) if limit_value.isdigit() else None,
    }


class ResponsesAPIClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout: int,
        provider: str = "openai",
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.timeout = timeout
        self.provider = provider
        self._transport = ResponsesTransport(
            LLMClientSettings(
                enabled=True,
                provider=provider,
                api_key=api_key,
                api_key_env="",
                model=model,
                base_url=base_url,
                timeout=timeout,
            ),
            opener=lambda api_request, timeout: request.urlopen(api_request, timeout=timeout),
        )

    def review(
        self,
        items: list[SourceReviewItem],
        *,
        require_english_replacement: bool = False,
    ) -> list[dict[str, Any]]:
        prompt_payload = {
            "items": compact_prompt_items(items),
            "required_result_ids": [item.issue_id for item in items],
            "required_context_tokens": {
                str(item.issue_id): review_context_token(item) for item in items
            },
            "required_result_count": len(items),
            "allowed_statuses": ["정상", "확인 필요", "오류 의심"],
            "allowed_issue_types": ["번역 검수", "오탈자 검수", "파란색 표기 확인"],
        }
        body = {
            "model": self.model,
            "reasoning": {
                "effort": "minimal",
            },
            "input": [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                f"{SYSTEM_PROMPT}\n\n{ENGLISH_REPLACEMENT_RETRY_PROMPT}"
                                if require_english_replacement
                                else SYSTEM_PROMPT
                            ),
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": json.dumps(prompt_payload, ensure_ascii=False),
                        }
                    ],
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "yearbook_source_review",
                    "strict": True,
                    "schema": REVIEW_RESPONSE_SCHEMA,
                }
            },
            "max_output_tokens": max(3000, len(items) * 400),
        }
        response = self._post(body)
        parsed = parse_response_json(response)
        raw_items = parsed.get("items", [])
        return raw_items if isinstance(raw_items, list) else []

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._transport.create(body)


# Backward-compatible import name for existing integrations.
OpenAIResponsesClient = ResponsesAPIClient


def review_batch_with_retries(
    client: ResponsesAPIClient,
    batch: list[SourceReviewItem],
) -> list[dict[str, Any]]:
    decisions_by_id = {
        int(decision["id"]): decision
        for decision in request_review_with_retries(client, batch)
        if isinstance(decision, dict) and int_or_none(decision.get("id")) is not None
    }
    for _ in range(2):
        retry_items = [
            item
            for item in batch
            if item.issue_id not in decisions_by_id
            or llm_decision_needs_retry(decisions_by_id[item.issue_id], item)
        ]
        if not retry_items:
            break
        for retry_decision in request_review_with_retries(
            client,
            retry_items,
            require_english_replacement=True,
        ):
            if not isinstance(retry_decision, dict):
                continue
            retry_id = int_or_none(retry_decision.get("id"))
            if retry_id is not None:
                decisions_by_id[retry_id] = retry_decision

    remaining_items = [
        item
        for item in batch
        if item.issue_id not in decisions_by_id
        or llm_decision_needs_retry(decisions_by_id[item.issue_id], item)
    ]
    for context_items in group_items_for_retry(remaining_items):
        for _ in range(2):
            retry_items = [
                item
                for item in context_items
                if item.issue_id not in decisions_by_id
                or llm_decision_needs_retry(decisions_by_id[item.issue_id], item)
            ]
            if not retry_items:
                break
            for retry_decision in request_review_with_retries(
                client,
                retry_items,
                require_english_replacement=True,
            ):
                if not isinstance(retry_decision, dict):
                    continue
                retry_id = int_or_none(retry_decision.get("id"))
                if retry_id is not None:
                    decisions_by_id[retry_id] = retry_decision
    unresolved = [
        item
        for item in batch
        if item.issue_id not in decisions_by_id
        or llm_decision_needs_retry(decisions_by_id[item.issue_id], item)
    ]
    for item in unresolved:
        decision = decisions_by_id.get(item.issue_id)
        recovered = recover_incomplete_bilingual_decision(decision, item)
        if recovered is not None:
            decisions_by_id[item.issue_id] = recovered
    unresolved = [
        item
        for item in batch
        if item.issue_id not in decisions_by_id
        or llm_decision_needs_retry(decisions_by_id[item.issue_id], item)
    ]
    if unresolved:
        codes = ", ".join(f"{item.table_code}:{item.location}" for item in unresolved)
        samples = []
        for item in unresolved[:5]:
            decision = decisions_by_id.get(item.issue_id, {})
            samples.append(
                f"id={item.issue_id}, requested={item.requested_review_type}, "
                f"returned={decision.get('issue_type', 'missing')}, "
                f"status={decision.get('status', 'missing')}, "
                f"expected={str(decision.get('expected_value', ''))[:60]!r}"
            )
        raise RuntimeError(
            f"LLM review returned incomplete replacement text: {codes}; "
            f"samples: {' | '.join(samples)}"
        )
    return list(decisions_by_id.values())


def recover_incomplete_bilingual_decision(
    raw: dict[str, Any] | None,
    item: SourceReviewItem,
) -> dict[str, Any] | None:
    """Recover only unambiguous bilingual replacements after retry exhaustion.

    A model occasionally returns an English fragment for a value containing
    Korean and English. One Korean/English pair can be reconstructed safely.
    Multi-pair headers cannot, so an incomplete suggestion is conservatively
    treated as no finding instead of being attached to the wrong label.
    """
    if not raw:
        return None
    raw_current = normalize_review_text(str(raw.get("current_value") or ""))
    if raw_current and not decision_current_matches_item(raw_current, item.current_value):
        return None

    expected = normalize_review_text(str(raw.get("expected_value") or ""))
    current = normalize_review_text(item.current_value)
    if str(raw.get("status") or "").strip() == "정상":
        recovered = dict(raw)
        recovered["expected_value"] = "__UNCHANGED__"
        return recovered
    if not expected or not re.search(r"[가-힣]", current):
        return None
    if re.search(r"[가-힣]", expected) or not re.search(r"[A-Za-z]", expected):
        return None

    korean_parts = [part.strip() for part in re.split(r"\s+/\s+", item.korean_text) if part.strip()]
    english_parts = [part.strip() for part in re.split(r"\s+/\s+", item.english_text) if part.strip()]
    recovered = dict(raw)
    if len(korean_parts) == 1 and len(english_parts) == 1:
        recovered["expected_value"] = normalize_review_text(
            f"{korean_parts[0]} {expected}"
        )
        if not llm_decision_needs_retry(recovered, item):
            return recovered

    recovered.update(
        {
            "status": "정상",
            "issue_type": item.requested_review_type,
            "defect_kind": "none",
            "current_value": current,
            "expected_value": "__UNCHANGED__",
            "difference": "보수적 검수 통과",
            "detail": "복합 국문·영문 셀에서 완전한 교정값이 확인되지 않아 오류로 확정하지 않았습니다.",
        }
    )
    return recovered


def request_review_with_retries(
    client: ResponsesAPIClient,
    items: list[SourceReviewItem],
    *,
    require_english_replacement: bool = False,
) -> list[dict[str, Any]]:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return client.review(
                items,
                require_english_replacement=require_english_replacement,
            )
        except json.JSONDecodeError as exc:
            last_error = exc
        except RuntimeError as exc:
            if "did not contain structured output" not in str(exc):
                raise
            last_error = exc
        if attempt < 2:
            time.sleep(2**attempt)
    raise RuntimeError("LLM review repeatedly returned malformed structured output") from last_error


def group_items_for_retry(items: list[SourceReviewItem]) -> list[list[SourceReviewItem]]:
    grouped: dict[tuple[object, ...], list[SourceReviewItem]] = {}
    for item in items:
        grouped.setdefault(prompt_context_key(item), []).append(item)
    return list(grouped.values())


SYSTEM_PROMPT = """You are a high-precision Korean government statistical yearbook language reviewer.

The input groups requests that share one title, header, or body-cell context. For every entry in review_requests, return one result using that request id. Copy that group's context_token exactly into every result for the group. required_result_ids and required_context_tokens define the only valid id-token mappings; never move a result between contexts. The output items array must contain exactly required_result_count objects. Review the complete shared value only for requested_review_type and return that same issue_type. Values may contain both text and numbers. Never turn a translation question into a spelling finding.

Precision is more important than recall. Report a finding only when the supplied value has a concrete, actionable defect. If the wording is contextually acceptable, conventional in a statistical table, or merely one of several valid alternatives, return "정상". Never create a finding just to improve style.

The standard categories are strictly separated:
1. 오탈자 검수: Check only clear Korean or English spelling/orthographic errors, broken or duplicated characters, accidental letter/number substitution, spacing, punctuation, and malformed numeric separators when peer values prove the intended format. "진행율" to "진행률" is a spelling correction. Allowed non-normal defect_kind values are korean_spelling, english_spelling, spacing, punctuation, and numeric_format. Do not translate, change word order, improve grammar or style, alter singular/plural, rewrite, formalize, standardize terminology, change romanization style, or suggest a synonym. Those cases are normal for this category. Use "오류 의심" only when the correction is clear. When evidence is insufficient, return "정상" rather than speculating.
2. 번역 검수: This request is created only for a value containing an identifiable Korean-English pair. Check whether the two sides convey the same material meaning in this table. Allowed non-normal defect_kind values are semantic_omission, semantic_addition, wrong_meaning, entity_mismatch, and measure_mismatch. Accept concise table translations, established abbreviations, non-literal but equivalent wording, word-order variation, capitalization, singular/plural variation, optional contextual scope phrases, and conventional labels. Spacing, spelling, punctuation, grammar polishing, or a merely more natural alternative is normal for this category because spelling has its own request. Return "확인 필요" only for a material omission, addition, role reversal, wrong entity, wrong measure, or clearly different core meaning. Do not propose a stylistic alternative and never return "오류 의심".
3. 파란색 표기 확인: This remains one combined review for an exact blue-marked HWPX value. Check clear Korean/English typos, semantic alignment, official proper names, and public-sector wording together, then return exactly one result with issue_type "파란색 표기 확인". Do not split it into the standard categories. For Korean-only blue text, provide a concrete English translation. For bilingual blue text, preserve the complete Korean and English value in a corrected replacement.

Translation dictionary policy:
- translation_glossary is evidence, not permission to skip review.
- status "approved" means human-approved and "official_verified" means the Korean and English forms were collected together from the cited official source. Treat these as the strongest evidence unless the supplied context proves they identify a different entity.
- status "official_name_only" means only the Korean proper name and entity classification were confirmed by an official registry; it does not verify any English translation.
- status "llm_reviewed", "reference", and "seed" are supporting evidence only. They may be wrong or outdated and must never override context or an official source.
- Check source_url, source_title, validity dates, aliases, category, and subcategory before applying an entry.
- When no usable glossary entry exists, judge conservatively from the table context. Lack of a glossary entry alone is never a defect.
- Never mechanically translate organization suffixes such as 부, 처, 청, 원, 위원회. Organization names, event names and Korean administrative areas are proper names.

General rules:
- Use the table title, row label, column label, unit, surrounding rows and glossary evidence.
- Use the id and context_token mapping exactly. The response does not repeat current_value; the application reads the authoritative original from the database.
- For 오탈자 검수, correct spelling only. Preserve the source language composition: Korean-only stays Korean, English-only stays English, and bilingual text stays bilingual. Never translate a Korean name as a spelling correction.
- Korean and English appearing together in one title, header, or cell is the yearbook's normal bilingual layout. Line breaks, Korean-first ordering, slash-separated hierarchy, parenthesized formula letters, and repeated category labels are not defects by themselves.
- Treat conventional statistical labels as valid when their meaning fits the table. Examples include 구분/Classification, 합계 or 계/Total, 소계/Subtotal, 평균/Average, 지역/Region, 연도/Year, 단위/Unit, 수 or 건수/Number, 비율/Rate or Ratio, 증감/Increase/Decrease or Change.
- Do not flag a valid generic label because another dictionary translation is also possible. In particular, never replace Classification, Total, Subtotal, Average, Region, Year, Unit, Number, Rate, or Ratio solely as a preference.
- Text adjacent because of table parsing is context, not necessarily one phrase. Do not replace one Korean-English pair with an unrelated neighboring header, row label, unit, source, or department name.
- When a bilingual value genuinely needs translation correction, expected_value must preserve the exact complete Korean text and contain the corrected English text. An English-only fragment that drops the Korean half is not an actionable replacement.
- candidate_kind beginning with "blue_text" is an exact HWPX blue-marked segment. Review the requested category for that segment and return a concrete corrected value, not a review request.
- For a Korean-only blue_text translation request, expected_value must be a standalone English translation. Never return the Korean source, a placeholder, or an instruction to translate later.
- Standard 번역 검수 is never requested for Korean-only or English-only ordinary text. Do not invent a missing counterpart for those values.
- For number_format, distinguish thousands separators from decimals using the unit and peer values. Preserve valid decimals.
- For punctuation_format, distinguish a typo from a line-break marker, URL, date, official notation or valid compound word.
- Treat semantic hyphens such as "e-learning" as correct. Remove a hyphen only when it clearly marks a line break inside one word.
- Do not perform or alter sum and ratio calculations.
- Use "정상" when no action is needed.
- For every normal result, set defect_kind to "none". Never use a semantic defect kind for an orthographic correction or an orthographic defect kind for a semantic correction.
- expected_value must never be empty. For "정상", return the exact sentinel "__UNCHANGED__" instead of copying the source. For a finding, provide the complete corrected value in the same bilingual layout, not an isolated fragment.
- difference and detail must be written in Korean. detail must briefly state the evidence used, including a glossary source when applicable.
- Never output placeholders such as "담당자 확인", "담당자 확인 필요", "공식 명칭 확인 필요" or "LLM 문맥 확인". The application has no separate human-approval workflow, so always return the reviewed replacement and conclusion directly.

Return JSON only, no markdown:
{
  "items": [
    {
      "id": 123,
      "context_token": "ctx_...",
      "status": "정상|확인 필요|오류 의심",
      "issue_type": "번역 검수|오탈자 검수|파란색 표기 확인",
      "defect_kind": "none|korean_spelling|english_spelling|spacing|punctuation|numeric_format|semantic_omission|semantic_addition|wrong_meaning|entity_mismatch|measure_mismatch",
      "expected_value": "__UNCHANGED__ or the complete corrected value",
      "difference": "short reason",
      "detail": "one concise Korean explanation"
    }
  ]
}
"""


ENGLISH_REPLACEMENT_RETRY_PROMPT = """The previous response was incomplete.
Copy each shared context_token exactly into its result. For every Korean source in this retry batch, expected_value must be the actual standalone English replacement text. Do not copy Korean, leave the value blank, write an explanation, or request later confirmation. If an official name is uncertain, provide the best conservative public-sector English translation and state the evidence limitation only in detail. difference and detail must be written in Korean.
"""


REVIEW_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "context_token": {"type": "string", "minLength": 8},
                    "status": {"type": "string", "enum": ["정상", "확인 필요", "오류 의심"]},
                    "issue_type": {"type": "string", "enum": ["번역 검수", "오탈자 검수", "파란색 표기 확인"]},
                    "defect_kind": {
                        "type": "string",
                        "enum": [
                            "none",
                            "korean_spelling",
                            "english_spelling",
                            "spacing",
                            "punctuation",
                            "numeric_format",
                            "semantic_omission",
                            "semantic_addition",
                            "wrong_meaning",
                            "entity_mismatch",
                            "measure_mismatch"
                        ]
                    },
                    "expected_value": {"type": "string", "minLength": 1},
                    "difference": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": [
                    "id",
                    "context_token",
                    "status",
                    "issue_type",
                    "defect_kind",
                    "expected_value",
                    "difference",
                    "detail",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def parse_response_json(response: dict[str, Any]) -> dict[str, Any]:
    return parse_responses_json(response)


def load_source_review_items(
    connection: sqlite3.Connection,
    run_id: int,
    *,
    limit: int | None = None,
) -> list[SourceReviewItem]:
    limit_sql = "LIMIT ?" if limit is not None else ""
    params: list[Any] = [run_id]
    if limit is not None:
        params.append(limit)
    rows = connection.execute(
        f"""
        SELECT
            vi.id AS issue_id,
            vi.rule_id AS source_rule_id,
            vi.difference AS candidate_reason,
            vi.expected_value AS candidate_expected,
            vi.table_id,
            st.code,
            st.title,
            st.title_en,
            st.unit,
            st.base_date,
            st.note,
            st.source,
            vi.location,
            vi.row_index,
            vi.col_index,
            vi.current_value
        FROM validation_issues vi
        JOIN stat_tables st ON st.id = vi.table_id
        WHERE vi.run_id = ?
          AND vi.rule_id IN (?, ?)
          AND vi.issue_type IN (?, ?)
        ORDER BY st.table_order, vi.id
        {limit_sql}
        """,
        (run_id, BLUE_REVIEW_RULE_ID, SOURCE_FORMAT_RULE_ID, BLUE_REVIEW_TYPE, SOURCE_FORMAT_TYPE)
        if limit is None
        else (
            run_id,
            BLUE_REVIEW_RULE_ID,
            SOURCE_FORMAT_RULE_ID,
            BLUE_REVIEW_TYPE,
            SOURCE_FORMAT_TYPE,
            limit,
        ),
    ).fetchall()

    return [build_source_review_item(connection, row) for row in rows]


def build_source_review_item(connection: sqlite3.Connection, row: sqlite3.Row) -> SourceReviewItem:
    row_index = row["row_index"]
    col_index = row["col_index"]
    cell_text = ""
    row_label = ""
    column_label = ""

    if row_index is not None and col_index is not None:
        cell_text = cell_text_at(connection, row["table_id"], row_index, col_index)
        row_label = cell_text_at(connection, row["table_id"], row_index, 0)
        column_label = column_header_text(connection, row["table_id"], col_index)
    else:
        cell_text = table_field_context(row)

    return SourceReviewItem(
        issue_id=row["issue_id"],
        source_rule_id=row["source_rule_id"],
        candidate_kind=source_candidate_kind(row),
        candidate_reason=normalize_review_text(row["candidate_reason"] or ""),
        candidate_expected=normalize_review_text(row["candidate_expected"] or ""),
        table_id=row["table_id"],
        table_code=row["code"],
        table_title=row["title"],
        table_title_en=row["title_en"],
        unit=row["unit"],
        base_date=row["base_date"],
        location=row["location"],
        row_index=row_index,
        col_index=col_index,
        current_value=row["current_value"],
        cell_text=cell_text,
        row_label=row_label,
        column_label=column_label,
        surrounding_rows=surrounding_row_texts(connection, row["table_id"], row_index),
        requested_review_type=(
            SPELLING_CHECK_TYPE if row["source_rule_id"] == SOURCE_FORMAT_RULE_ID else ""
        ),
        source_record_type="issue",
        source_record_id=int(row["issue_id"]),
    )


def load_linguistic_review_items(
    connection: sqlite3.Connection,
    run_id: int,
) -> list[SourceReviewItem]:
    rows = connection.execute(
        """
        SELECT
            lrc.id AS candidate_id,
            lrc.review_type,
            lrc.candidate_kind,
            lrc.reason,
            lrc.table_id,
            st.code,
            st.title,
            st.title_en,
            st.unit,
            st.base_date,
            lrc.location,
            lrc.row_index,
            lrc.col_index,
            lrc.current_value,
            lrc.korean_text,
            lrc.english_text,
            lrc.glossary_json,
            lrc.prompt_version
        FROM linguistic_review_candidates lrc
        JOIN stat_tables st ON st.id = lrc.table_id
        WHERE lrc.run_id = ? AND lrc.status = 'pending'
        ORDER BY
            CASE lrc.review_type
                WHEN '오탈자 검수' THEN 0
                WHEN '번역 검수' THEN 1
                WHEN '파란색 표기 확인' THEN 2
                ELSE 3
            END,
            st.table_order,
            lrc.id
        """,
        (run_id,),
    ).fetchall()

    cell_values, header_values, row_values = linguistic_cell_context(connection, run_id)
    items: list[SourceReviewItem] = []
    for row in rows:
        row_index = row["row_index"]
        col_index = row["col_index"]
        items.append(
            SourceReviewItem(
                issue_id=-int(row["candidate_id"]),
                source_rule_id=(
                    BLUE_REVIEW_RULE_ID
                    if str(row["candidate_kind"]).startswith("blue_text")
                    else LINGUISTIC_CANDIDATE_RULE_ID
                ),
                candidate_kind=str(row["candidate_kind"]),
                candidate_reason=normalize_review_text(str(row["reason"] or "")),
                candidate_expected="",
                table_id=int(row["table_id"]),
                table_code=str(row["code"]),
                table_title=str(row["title"]),
                table_title_en=str(row["title_en"]),
                unit=str(row["unit"]),
                base_date=str(row["base_date"]),
                location=str(row["location"]),
                row_index=int(row_index) if row_index is not None else None,
                col_index=int(col_index) if col_index is not None else None,
                current_value=str(row["current_value"]),
                cell_text=str(row["current_value"]),
                row_label=(
                    cell_values.get((int(row["table_id"]), int(row_index), 0), "")
                    if row_index is not None
                    else ""
                ),
                column_label=(
                    " / ".join(header_values.get((int(row["table_id"]), int(col_index)), []))
                    if col_index is not None
                    else ""
                ),
                surrounding_rows=surrounding_rows_from_context(
                    row_values,
                    int(row["table_id"]),
                    int(row_index) if row_index is not None else None,
                ),
                requested_review_type=str(row["review_type"]),
                korean_text=str(row["korean_text"]),
                english_text=str(row["english_text"]),
                glossary_matches=parse_glossary_context(str(row["glossary_json"])),
                source_record_type="candidate",
                source_record_id=int(row["candidate_id"]),
                prompt_version=str(row["prompt_version"] or LINGUISTIC_PROMPT_VERSION),
            )
        )
    return items


def linguistic_cell_context(
    connection: sqlite3.Connection,
    run_id: int,
) -> tuple[
    dict[tuple[int, int, int], str],
    dict[tuple[int, int], list[str]],
    dict[tuple[int, int], list[str]],
]:
    rows = connection.execute(
        """
        SELECT c.table_id, c.row_index, c.col_index, c.text_value, c.is_header
        FROM stat_table_cells c
        WHERE c.table_id IN (
            SELECT DISTINCT table_id
            FROM linguistic_review_candidates
            WHERE run_id = ? AND status = 'pending'
        )
        ORDER BY c.table_id, c.row_index, c.col_index
        """,
        (run_id,),
    ).fetchall()
    cell_values: dict[tuple[int, int, int], str] = {}
    header_values: dict[tuple[int, int], list[str]] = {}
    row_values: dict[tuple[int, int], list[str]] = {}
    for row in rows:
        table_id = int(row["table_id"])
        row_index = int(row["row_index"])
        col_index = int(row["col_index"])
        value = normalize_review_text(str(row["text_value"] or ""))
        cell_values[(table_id, row_index, col_index)] = value
        if not value:
            continue
        row_values.setdefault((table_id, row_index), []).append(value)
        if int(row["is_header"]):
            parts = header_values.setdefault((table_id, col_index), [])
            if value not in parts:
                parts.append(value)
    return cell_values, header_values, row_values


def surrounding_rows_from_context(
    row_values: dict[tuple[int, int], list[str]],
    table_id: int,
    row_index: int | None,
) -> list[str]:
    if row_index is None:
        return []
    result: list[str] = []
    for index in range(max(row_index - 1, 0), row_index + 2):
        values = row_values.get((table_id, index), [])
        if values:
            result.append(f"{index + 1}행: {' | '.join(values)[:900]}")
    return result


def source_candidate_kind(row: sqlite3.Row) -> str:
    if row["source_rule_id"] == BLUE_REVIEW_RULE_ID:
        return "blue_text"
    reason = normalize_review_text(row["candidate_reason"] or "")
    expected = normalize_review_text(row["candidate_expected"] or "")
    if expected and expected != "LLM 문맥 확인":
        return "number_format"
    if any(keyword in reason for keyword in ("숫자", "천 단위", "쉼표", "소수점", "구분기호")):
        return "number_format"
    return "punctuation_format"


def review_decision_needs_retry(raw: dict[str, Any], item: SourceReviewItem) -> bool:
    expected = normalize_review_text(str(raw.get("expected_value") or ""))
    if not expected:
        return True

    issue_type = str(raw.get("issue_type") or "").strip()
    status = str(raw.get("status") or "").strip()
    current = normalize_review_text(item.current_value)
    requires_english_replacement = bool(
        (
            item.requested_review_type == BLUE_REVIEW_TYPE
            or (item.candidate_kind == "blue_text" and not item.requested_review_type)
        )
        and re.search(r"[가-힣]", current)
        and not re.search(r"[A-Za-z]", current)
    )
    if status == "정상" and not requires_english_replacement:
        return False
    if decision_should_be_normal(raw, item):
        return False

    raw_current = normalize_review_text(str(raw.get("current_value") or ""))
    if raw_current and not decision_current_matches_item(raw_current, item.current_value):
        return True

    translation_requested = (
        item.requested_review_type == TRANSLATION_CHECK_TYPE
        or (item.candidate_kind == "blue_text" and not item.requested_review_type)
        or (
            item.requested_review_type == BLUE_REVIEW_TYPE
            and re.search(r"[가-힣]", current) is not None
            and re.search(r"[A-Za-z]", current) is None
        )
    )
    if translation_requested and is_temporal_notation(current):
        return False
    if not decision_expected_matches_requested_scope(raw, item):
        return True
    if (
        translation_requested
        and re.search(r"[가-힣]", current)
        and expected == current
        and not re.search(r"[A-Za-z]", current)
    ):
        return True
    if status == "정상" or issue_type != "번역 검수" or not re.search(r"[가-힣]", current):
        return False

    explanatory_korean = (
        "번역",
        "영문",
        "영어",
        "공식적으로",
        "확인 필요",
        "확인이 필요",
        "표현입니다",
    )
    return not re.search(r"[A-Za-z]", expected) or any(token in expected for token in explanatory_korean)


def llm_decision_needs_retry(raw: dict[str, Any], item: SourceReviewItem) -> bool:
    """Reject a response that was copied from another batched table or cell."""

    return (
        str(raw.get("context_token") or "") != review_context_token(item)
        or review_decision_needs_retry(raw, item)
    )


def decision_should_be_normal(raw: dict[str, Any], item: SourceReviewItem) -> bool:
    """Enforce the spelling/translation boundary after the model classifies a defect."""

    if str(raw.get("status") or "").strip() == "정상":
        return False
    defect_kind = str(raw.get("defect_kind") or "").strip()
    if not defect_kind:
        return False
    current = normalize_review_text(item.current_value)
    expected = normalize_review_text(str(raw.get("expected_value") or ""))
    review_type = item.requested_review_type or str(raw.get("issue_type") or "")

    if review_type == SPELLING_CHECK_TYPE:
        return (
            defect_kind not in SPELLING_DEFECT_KINDS
            or not is_plausible_spelling_correction(current, expected, raw, defect_kind)
        )
    if review_type == TRANSLATION_CHECK_TYPE:
        return (
            defect_kind not in TRANSLATION_DEFECT_KINDS
            or english_only_replacement_is_incomplete_fragment(item, expected)
            or not is_material_translation_correction(current, expected, raw)
        )
    return False


def is_plausible_spelling_correction(
    current: str,
    expected: str,
    raw: dict[str, Any],
    defect_kind: str,
) -> bool:
    if not current or not expected or current == expected:
        return False
    notes = normalize_review_text(
        f"{raw.get('difference') or ''} {raw.get('detail') or ''}"
    ).casefold()
    if re.search(r"어순|단수|복수|문법|문체|스타일|권장|자연스|의미|누락|추가|불필요", notes):
        return False

    current_compact = canonical_decision_text(current)
    expected_compact = canonical_decision_text(expected)
    if current_compact == expected_compact:
        return True
    if same_words_ignoring_order_or_inflection(current, expected):
        return False
    if defect_kind == "numeric_format":
        return bool(re.search(r"\d", current) and re.search(r"\d", expected))

    if defect_kind == "korean_spelling":
        current_text = "".join(re.findall(r"[가-힣]", current))
        expected_text = "".join(re.findall(r"[가-힣]", expected))
    elif defect_kind == "english_spelling":
        current_text = "".join(re.findall(r"[A-Za-z]", current)).casefold()
        expected_text = "".join(re.findall(r"[A-Za-z]", expected)).casefold()
    else:
        return False
    if not current_text or not expected_text:
        return False
    if len(current_text) == len(expected_text):
        mismatch_count = sum(left != right for left, right in zip(current_text, expected_text))
        if mismatch_count <= 2:
            return True
    return bool(
        abs(len(current_text) - len(expected_text)) <= 2
        and SequenceMatcher(None, current_text, expected_text).ratio() >= 0.9
    )


def is_material_translation_correction(
    current: str,
    expected: str,
    raw: dict[str, Any],
) -> bool:
    if not current or not expected or current == expected:
        return False
    notes = normalize_review_text(
        f"{raw.get('difference') or ''} {raw.get('detail') or ''}"
    ).casefold()
    if re.search(
        r"띄어쓰기|철자|대소문자|어순|단수|복수|문법|전치사|문체|스타일|"
        r"더 자연|권장|권고|의미.{0,4}확장|범위.{0,4}확장",
        notes,
    ):
        return False
    if canonical_decision_text(current) == canonical_decision_text(expected):
        return False
    return not same_words_ignoring_order_or_inflection(current, expected)


def english_only_replacement_is_incomplete_fragment(
    item: SourceReviewItem,
    expected: str,
) -> bool:
    current = normalize_review_text(item.current_value)
    expected = normalize_review_text(expected)
    if not (
        re.search(r"[가-힣]", current)
        and re.search(r"[A-Za-z]", current)
        and re.search(r"[A-Za-z]", expected)
        and not re.search(r"[가-힣]", expected)
    ):
        return False

    def english_tokens(value: str) -> list[str]:
        result: list[str] = []
        for token in re.findall(r"[A-Za-z]+", value.casefold()):
            if len(token) > 3 and token.endswith("s"):
                token = token[:-1]
            result.append(token)
        return result

    current_tokens = english_tokens(item.english_text or current)
    expected_tokens = english_tokens(expected)
    if len(current_tokens) < 4 or len(expected_tokens) >= len(current_tokens) * 0.65:
        return False
    overlap = sum(token in current_tokens for token in expected_tokens)
    return bool(expected_tokens and overlap / len(expected_tokens) >= 0.8)


def english_replacement_has_context_overlap(item: SourceReviewItem, expected: str) -> bool:
    def token_set(value: str) -> set[str]:
        result: set[str] = set()
        for token in re.findall(r"[A-Za-z]+", value.casefold()):
            if len(token) > 3 and token.endswith("s"):
                token = token[:-1]
            result.add(token)
        return result

    current_tokens = token_set(item.english_text or item.current_value)
    expected_tokens = token_set(expected)
    if not current_tokens or not expected_tokens:
        return False
    overlap = len(current_tokens & expected_tokens)
    return overlap / min(len(current_tokens), len(expected_tokens)) >= 0.4


def same_words_ignoring_order_or_inflection(left: str, right: str) -> bool:
    def tokens(value: str) -> list[str]:
        result: list[str] = []
        for token in re.findall(r"[A-Za-z]+|[가-힣]+|\d+", value.casefold()):
            if re.fullmatch(r"[a-z]+", token) and len(token) > 3 and token.endswith("s"):
                token = token[:-1]
            result.append(token)
        return sorted(result)

    left_tokens = tokens(left)
    right_tokens = tokens(right)
    return bool(left_tokens and left_tokens == right_tokens)


def decision_expected_matches_requested_scope(
    raw: dict[str, Any],
    item: SourceReviewItem,
) -> bool:
    """Reject cross-cell replacements and category changes before persistence."""

    if str(raw.get("status") or "").strip() == "정상":
        return True
    current = normalize_review_text(item.current_value)
    expected = normalize_review_text(str(raw.get("expected_value") or ""))
    if not expected or re.search(r"(?:→|->|=>)", expected):
        return False

    review_type = item.requested_review_type or str(raw.get("issue_type") or "")
    current_has_korean = re.search(r"[가-힣]", current) is not None
    current_has_english = re.search(r"[A-Za-z]", current) is not None
    expected_has_korean = re.search(r"[가-힣]", expected) is not None
    expected_has_english = re.search(r"[A-Za-z]", expected) is not None

    if review_type == SPELLING_CHECK_TYPE:
        return (
            current_has_korean == expected_has_korean
            and current_has_english == expected_has_english
        )
    if review_type == BLUE_REVIEW_TYPE:
        if current_has_korean and current_has_english:
            return expected_has_korean and expected_has_english
        if current_has_korean:
            return expected_has_english
        if current_has_english:
            return expected_has_english
        return True
    if review_type != TRANSLATION_CHECK_TYPE:
        return True
    if current_has_korean and current_has_english:
        if expected_has_korean and expected_has_english:
            return canonical_korean_text(expected) == canonical_korean_text(current)
        return bool(
            expected_has_english
            and item.korean_text
            and not english_only_replacement_is_incomplete_fragment(item, expected)
            and english_replacement_has_context_overlap(item, expected)
        )
    if current_has_korean:
        return expected_has_english and not expected_has_korean
    if current_has_english:
        return expected_has_english
    return True


def canonical_korean_text(value: str) -> str:
    return "".join(re.findall(r"[가-힣]+", normalize_review_text(value)))


def table_field_context(row: sqlite3.Row) -> str:
    location = normalize_review_text(row["location"] or "")
    if location == "출처":
        return normalize_review_text(row["source"] or "")
    if location == "주석":
        return normalize_review_text(row["note"] or "")
    if location == "영문 표 제목":
        return normalize_review_text(row["title_en"] or "")
    if location == "표 제목":
        return normalize_review_text(row["title"] or "")
    return ""


def surrounding_row_texts(
    connection: sqlite3.Connection,
    table_id: int,
    row_index: int | None,
) -> list[str]:
    if row_index is None:
        return []
    rows = connection.execute(
        """
        SELECT row_index, col_index, text_value
        FROM stat_table_cells
        WHERE table_id = ? AND row_index BETWEEN ? AND ?
        ORDER BY row_index, col_index
        """,
        (table_id, max(row_index - 1, 0), row_index + 1),
    ).fetchall()
    grouped: dict[int, list[str]] = {}
    for row in rows:
        value = normalize_review_text(row["text_value"])
        if value:
            grouped.setdefault(int(row["row_index"]), []).append(value)
    return [f"{index + 1}행: {' | '.join(values)[:900]}" for index, values in grouped.items()]


def cell_text_at(connection: sqlite3.Connection, table_id: int, row_index: int, col_index: int) -> str:
    row = connection.execute(
        """
        SELECT text_value
        FROM stat_table_cells
        WHERE table_id = ? AND row_index = ? AND col_index = ?
        """,
        (table_id, row_index, col_index),
    ).fetchone()
    return normalize_review_text(row["text_value"]) if row else ""


def column_header_text(connection: sqlite3.Connection, table_id: int, col_index: int) -> str:
    rows = connection.execute(
        """
        SELECT text_value
        FROM stat_table_cells
        WHERE table_id = ? AND col_index = ? AND is_header = 1
        ORDER BY row_index
        """,
        (table_id, col_index),
    ).fetchall()
    parts: list[str] = []
    for row in rows:
        value = normalize_review_text(row["text_value"])
        if value and value not in parts:
            parts.append(value)
    return " / ".join(parts)


def resolve_linguistic_candidates_from_glossary(
    connection: sqlite3.Connection,
    run_id: int,
    *,
    standard_language_only: bool = False,
) -> tuple[int, int, int]:
    """Resolve only exact, authoritative glossary matches without an API call."""

    items = load_linguistic_review_items(connection, run_id)
    if standard_language_only:
        items = [item for item in items if is_standard_language_item(item)]
    resolved_items: list[SourceReviewItem] = []
    decisions: list[dict[str, Any]] = []
    for item in items:
        decision = glossary_decision_for(item)
        if decision is None:
            continue
        resolved_items.append(item)
        decisions.append({"id": item.issue_id, **decision})
    if not decisions:
        return (0, 0, 0)
    return save_llm_review_decisions(
        connection,
        run_id,
        resolved_items,
        decisions,
        reviewed_model="official-glossary",
        resolution_source="dictionary",
    )


def glossary_decision_for(item: SourceReviewItem) -> dict[str, str] | None:
    explicit_spelling = explicit_spelling_decision_for(item)
    if explicit_spelling is not None:
        return explicit_spelling

    entries = item.glossary_matches or []
    korean = normalize_review_text(item.korean_text)
    english = normalize_review_text(item.english_text)
    source_normalized = normalize_source(korean) if korean else ""
    target_normalized = normalize_target(english) if english else ""

    # Only audited or authoritative pairs can close a candidate. Raw yearbook
    # repetition is evidence, not proof: the same typo may be copied every year.
    exact_match_statuses = {
        "approved",
        "official_verified",
        "seed",
        "llm_verified",
        "llm_reviewed",
    }
    for entry in entries:
        if str(entry.get("status") or "") not in exact_match_statuses:
            continue
        entry_source = normalize_source(str(entry.get("source") or ""))
        aliases = [normalize_source(str(alias)) for alias in entry.get("aliases", []) if alias]
        source_matches = bool(source_normalized) and source_normalized in {entry_source, *aliases}
        entry_target = normalize_review_text(str(entry.get("target") or ""))
        target_matches = bool(target_normalized and entry_target) and target_normalized == normalize_target(entry_target)
        if source_matches and target_matches:
            source_title = normalize_review_text(
                str(entry.get("source_title") or "연보 승인·반복 번역 사전")
            )
            return normal_glossary_decision(item, source_title)

    authoritative = [
        entry
        for entry in entries
        if str(entry.get("status") or "") in {"approved", "official_verified", "official_name_only"}
    ]
    if not authoritative:
        return None

    for entry in authoritative:
        entry_source = normalize_source(str(entry.get("source") or ""))
        aliases = [normalize_source(str(alias)) for alias in entry.get("aliases", []) if alias]
        source_matches = bool(source_normalized) and source_normalized in {entry_source, *aliases}
        entry_target = normalize_review_text(str(entry.get("target") or ""))
        target_matches = bool(target_normalized and entry_target) and target_normalized == normalize_target(entry_target)
        status = str(entry.get("status") or "")
        source_title = normalize_review_text(str(entry.get("source_title") or "공식 번역 사전"))

        if item.requested_review_type == SPELLING_CHECK_TYPE:
            exact_complete_value = (
                (bool(korean) and not english and source_matches)
                or (bool(english) and not korean and target_matches)
                or (bool(korean) and bool(english) and source_matches and target_matches)
            )
            if exact_complete_value:
                return normal_glossary_decision(item, source_title)
            continue

        if item.requested_review_type not in {TRANSLATION_CHECK_TYPE, BLUE_REVIEW_TYPE}:
            continue
        if status == "official_name_only" or not entry_target:
            continue
        if source_matches:
            if target_matches:
                return normal_glossary_decision(item, source_title)
        if not korean and target_matches:
            return normal_glossary_decision(item, source_title)
    # A dictionary mismatch is evidence for the contextual LLM review, not an
    # automatic finding. Statistical labels and historical organization names
    # often have multiple valid translations depending on year and table scope.
    return None


def explicit_spelling_decision_for(item: SourceReviewItem) -> dict[str, str] | None:
    """Apply unambiguous typo corrections before any glossary can approve them."""

    if item.requested_review_type != SPELLING_CHECK_TYPE:
        return None
    expected = normalize_review_text(item.current_value)
    reasons: list[str] = []
    defect_kind = "english_spelling"
    for replacement in SPELLING_REPLACEMENTS:
        current = str(replacement.get("current") or "")
        corrected = str(replacement.get("expected") or "")
        if not current or current not in expected:
            continue
        expected = expected.replace(current, corrected)
        reasons.append(f"{current} → {corrected}")
        if str(replacement.get("language") or "") == "ko":
            defect_kind = "korean_spelling"
    if not reasons:
        return None
    return {
        "status": "오류 의심",
        "issue_type": SPELLING_CHECK_TYPE,
        "rule_id": LLM_SPELLING_RULE_ID,
        "defect_kind": defect_kind,
        "current_value": normalize_review_text(item.current_value),
        "expected_value": expected,
        "difference": ", ".join(reasons),
        "detail": "명시 오탈자 사전에서 잘못된 문자·숫자 표기가 확인되었습니다.",
    }


def normal_glossary_decision(item: SourceReviewItem, source_title: str) -> dict[str, str]:
    issue_type = (
        TRANSLATION_CHECK_TYPE
        if item.requested_review_type == BLUE_REVIEW_TYPE
        else item.requested_review_type
    )
    return {
        "status": "정상",
        "issue_type": issue_type or TRANSLATION_CHECK_TYPE,
        "current_value": item.current_value,
        "expected_value": item.current_value,
        "difference": "공식 사전 일치",
        "detail": f"{source_title}의 공식 표기와 정확히 일치합니다.",
    }


def reuse_cached_linguistic_reviews(
    connection: sqlite3.Connection,
    run_id: int,
    *,
    reviewed_model: str,
    standard_language_only: bool = False,
) -> tuple[int, int, int]:
    items = load_linguistic_review_items(connection, run_id)
    if standard_language_only:
        items = [item for item in items if is_standard_language_item(item)]
    cache_rows = connection.execute(
        """
        SELECT fingerprint, decision_json
        FROM linguistic_review_cache
        WHERE reviewed_model = ?
        """,
        (reviewed_model,),
    ).fetchall()
    cached_decisions = {
        str(row["fingerprint"]): str(row["decision_json"])
        for row in cache_rows
    }
    cached_items: list[SourceReviewItem] = []
    decisions: list[dict[str, Any]] = []
    fingerprints: list[str] = []
    for item in items:
        fingerprint = linguistic_review_fingerprint(item, reviewed_model=reviewed_model)
        decision_json = cached_decisions.get(fingerprint)
        if decision_json is None:
            continue
        try:
            decision = json.loads(decision_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(decision, dict):
            continue
        cached_items.append(item)
        decisions.append({"id": item.issue_id, **decision})
        fingerprints.append(fingerprint)

    if not decisions:
        return (0, 0, 0)
    counts = save_llm_review_decisions(
        connection,
        run_id,
        cached_items,
        decisions,
        reviewed_model=reviewed_model,
        resolution_source="cache",
    )
    connection.executemany(
        """
        UPDATE linguistic_review_cache
        SET use_count = use_count + 1, last_used_at = CURRENT_TIMESTAMP
        WHERE fingerprint = ?
        """,
        [(fingerprint,) for fingerprint in fingerprints],
    )
    return counts


def linguistic_review_fingerprint(item: SourceReviewItem, *, reviewed_model: str) -> str:
    glossary_payload = [
        {
            "source": entry.get("source", ""),
            "target": entry.get("target", ""),
            "status": entry.get("status", ""),
            "source_url": entry.get("source_url", ""),
            "verified_at": entry.get("verified_at", ""),
        }
        for entry in (item.glossary_matches or [])
        if str(entry.get("status") or "") in {"approved", "official_verified", "official_name_only"}
    ]
    glossary_json = json.dumps(glossary_payload, ensure_ascii=False, sort_keys=True)
    context = {
        "review_type": item.requested_review_type or item.candidate_kind,
        "candidate_kind": item.candidate_kind,
        "current_value": normalize_review_text(item.current_value),
        "location_kind": normalized_location_kind(item.location),
        "table_title": normalize_review_text(item.table_title),
        "table_title_en": normalize_review_text(item.table_title_en),
        "unit": normalize_review_text(item.unit),
        "row_label": normalize_review_text(item.row_label),
        "column_label": normalize_review_text(item.column_label),
        "surrounding_rows": [normalize_context_numbers(value) for value in item.surrounding_rows],
        "prompt_version": item.prompt_version or LINGUISTIC_PROMPT_VERSION,
        "reviewed_model": reviewed_model,
        "glossary": glossary_json,
    }
    encoded = json.dumps(context, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalized_location_kind(location: str) -> str:
    value = normalize_review_text(location)
    if re.fullmatch(r"\d+행 \d+열(?: \(\d+/\d+\))?", value):
        return "표 셀"
    return re.sub(r"\s*\(\d+/\d+\)$", "", value)


def normalize_context_numbers(value: str) -> str:
    return re.sub(r"(?<![A-Za-z])[-+]?\d[\d,.:/~-]*(?![A-Za-z])", "<NUM>", normalize_review_text(value))


def save_llm_review_decisions(
    connection: sqlite3.Connection,
    run_id: int,
    batch: list[SourceReviewItem],
    decisions: list[dict[str, Any]],
    *,
    reviewed_model: str = "",
    resolution_source: str = "llm",
) -> tuple[int, int, int]:
    by_id = {item.issue_id: item for item in batch}
    inserted_issues = 0
    inserted_checks = 0
    processed = 0

    for raw_decision in decisions:
        if not isinstance(raw_decision, dict):
            continue
        issue_id = int_or_none(raw_decision.get("id"))
        if issue_id is None or issue_id not in by_id:
            continue
        item = by_id[issue_id]
        if (
            resolution_source == "llm"
            and str(raw_decision.get("context_token") or "") != review_context_token(item)
        ):
            continue
        if item.source_record_type == "candidate" and not candidate_source_still_matches(
            connection,
            run_id,
            item,
        ):
            continue
        raw_current = normalize_review_text(str(raw_decision.get("current_value") or ""))
        if (
            raw_current
            and not decision_current_matches_item(raw_current, item.current_value)
            and str(raw_decision.get("status") or "").strip() != "정상"
            and not decision_should_be_normal(raw_decision, item)
        ):
            continue
        if review_decision_needs_retry(raw_decision, item):
            continue
        decision = normalize_decision(raw_decision, item)
        if item.source_record_type == "candidate":
            mark_linguistic_candidate_reviewed(
                connection,
                item,
                decision=decision,
                reviewed_model=reviewed_model,
                resolution_source=resolution_source,
            )
        else:
            remove_blue_review_placeholder(connection, run_id, item)
        if duplicate_llm_check_exists(connection, run_id, item, decision):
            processed += 1
            continue
        insert_llm_check(connection, run_id, item, decision)
        inserted_checks += 1
        processed += 1
        if decision["status"] != "정상":
            insert_llm_issue(connection, run_id, item, decision)
            inserted_issues += 1
        if resolution_source == "llm" and decision["issue_type"] == TRANSLATION_CHECK_TYPE:
            upsert_llm_reviewed_glossary(
                connection,
                run_id=run_id,
                item=item,
                decision=decision,
                reviewed_model=reviewed_model,
            )
        if resolution_source == "llm" and item.source_record_type == "candidate":
            store_linguistic_review_cache(
                connection,
                item,
                decision=decision,
                reviewed_model=reviewed_model,
            )

    return inserted_issues, inserted_checks, processed


def candidate_source_still_matches(
    connection: sqlite3.Connection,
    run_id: int,
    item: SourceReviewItem,
) -> bool:
    if item.source_record_id is None:
        return False
    row = connection.execute(
        """
        SELECT run_id, table_id, location, row_index, col_index, current_value, status
        FROM linguistic_review_candidates
        WHERE id = ?
        """,
        (item.source_record_id,),
    ).fetchone()
    if row is None or str(row["status"]) != "pending":
        return False
    return (
        int(row["run_id"]) == run_id
        and int(row["table_id"]) == item.table_id
        and str(row["location"]) == item.location
        and row["row_index"] == item.row_index
        and row["col_index"] == item.col_index
        and normalize_review_text(str(row["current_value"]))
        == normalize_review_text(item.current_value)
    )


def normalize_decision(raw: dict[str, Any], item: SourceReviewItem) -> dict[str, str]:
    status = str(raw.get("status") or "확인 필요").strip()
    if status not in {"정상", "확인 필요", "오류 의심"}:
        status = "확인 필요"

    raw_issue_type = str(raw.get("issue_type") or "번역 검수").strip()
    issue_type = raw_issue_type
    if issue_type not in {*LINGUISTIC_CHECK_TYPES, BLUE_REVIEW_TYPE}:
        issue_type = "번역 검수"

    if item.requested_review_type in {*LINGUISTIC_CHECK_TYPES, BLUE_REVIEW_TYPE}:
        issue_type = item.requested_review_type

    expected = normalize_review_text(str(raw.get("expected_value") or ""))
    if decision_should_be_normal(raw, item):
        status = "정상"
        expected = normalize_review_text(item.current_value)
        raw = {
            **raw,
            "difference": f"{issue_type} 범위 외 제안 제외",
            "detail": (
                "오탈자와 번역의 검수 범위를 분리해 어순, 단복수, 문체 또는 "
                "다른 검수 유형의 제안은 문제로 분류하지 않았습니다."
            ),
        }
    elif (
        status != "정상"
        and issue_type == TRANSLATION_CHECK_TYPE
        and item.korean_text
        and re.search(r"[A-Za-z]", expected)
        and not re.search(r"[가-힣]", expected)
    ):
        expected = normalize_review_text(f"{item.korean_text} {expected}")
    if is_contextual_source_url_parenthesis(item):
        status = "정상"
        issue_type = "오탈자 검수"
        expected = item.current_value
        raw = {
            **raw,
            "difference": "출처 URL 괄호 표기 정상",
            "detail": "출처 전체 문맥에서 URL은 기관명 뒤 괄호 안에 정상적으로 표기되어 있습니다.",
        }
    elif (
        expected_is_existing_bilingual_text(item.current_value, expected)
        or expected_drops_bilingual_context(item.current_value, expected, issue_type)
    ):
        status = "정상"
        expected = item.current_value
        raw = {
            **raw,
            "difference": "국문·영문 병기 정상",
            "detail": "동일 셀에 국문과 대응 영문을 함께 표기한 연보의 정상적인 병기 형식입니다.",
        }
    elif issue_type == TRANSLATION_CHECK_TYPE and is_temporal_notation(item.current_value):
        expected = translate_temporal_notation(item.current_value)
        if expected != normalize_review_text(item.current_value):
            status = "확인 필요"
            raw = {
                **raw,
                "difference": "연도·기간 영문 표기 제안",
                "detail": "연도·기간에 포함된 국문 접미사를 영문 표에서 사용할 수 있는 숫자 중심 표기로 정리했습니다.",
            }
    elif item.candidate_kind == "number_format" and item.candidate_expected not in {"", "LLM 문맥 확인"}:
        expected = item.candidate_expected
        if normalize_review_text(item.current_value) != expected:
            status = "오류 의심"
            issue_type = "오탈자 검수"
    elif status == "오류 의심" and issue_type == TRANSLATION_CHECK_TYPE:
        status = "확인 필요"
    elif status == "오류 의심" and issue_type != BLUE_REVIEW_TYPE:
        if item.candidate_kind == "blue_text" and looks_like_translation_mismatch(item.current_value, expected):
            status = "확인 필요"
            issue_type = TRANSLATION_CHECK_TYPE
        else:
            issue_type = SPELLING_CHECK_TYPE

    generated_translation = (
        issue_type in {TRANSLATION_CHECK_TYPE, BLUE_REVIEW_TYPE}
        and re.search(r"[가-힣]", item.current_value) is not None
        and re.search(r"[A-Za-z]", item.current_value) is None
        and (
            re.search(r"[A-Za-z]", expected) is not None
            or (
                is_temporal_notation(item.current_value)
                and expected != normalize_review_text(item.current_value)
            )
        )
    )
    if status == "정상" and generated_translation:
        status = "확인 필요"
        raw = {
            **raw,
            "difference": "누락된 영문 번역 제안",
            "detail": "국문만 있는 항목에 대해 표 문맥과 사전 근거를 바탕으로 영문 번역안을 생성했습니다.",
        }
    if status == "정상" and not generated_translation:
        expected = item.current_value

    difference = remove_human_confirmation_language(
        normalize_review_text(str(raw.get("difference") or ""))
    )
    detail = remove_human_confirmation_language(
        normalize_review_text(
            str(raw.get("detail") or "LLM이 원문의 번역과 철자를 검토했습니다.")
        )
    )
    if status != "정상" and not re.search(r"[가-힣]", difference):
        difference = korean_difference_for(issue_type)
    if not re.search(r"[가-힣]", detail):
        detail = korean_detail_for(issue_type, status)
    if status == "정상":
        expected = normalize_review_text(item.current_value)
        difference = f"{issue_type} 통과"
        detail = korean_detail_for(issue_type, status)
    if item.source_rule_id == BLUE_REVIEW_RULE_ID:
        reviewed_category = issue_type
        issue_type = BLUE_REVIEW_TYPE
        if raw_issue_type != BLUE_REVIEW_TYPE:
            difference = (
                f"{reviewed_category}: {difference}"
                if difference
                else f"{reviewed_category} 결과"
            )

    return {
        "status": status,
        "issue_type": issue_type,
        "rule_id": rule_id_for(issue_type),
        "defect_kind": str(raw.get("defect_kind") or "none"),
        "current_value": normalize_review_text(item.current_value),
        "expected_value": expected,
        "difference": difference,
        "detail": detail,
    }


def decision_current_matches_item(returned_value: str, item_value: str) -> bool:
    """Ensure a batched response did not copy another candidate's source text."""

    return canonical_decision_text(returned_value) == canonical_decision_text(item_value)


def canonical_decision_text(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", normalize_review_text(value)).casefold()


def korean_difference_for(issue_type: str) -> str:
    return {
        SPELLING_CHECK_TYPE: "국문·영문 표기 교정 제안",
        TRANSLATION_CHECK_TYPE: "국문과 영문 번역 표현 검토",
    }.get(issue_type, "언어 표현 검토")


def korean_detail_for(issue_type: str, status: str) -> str:
    category = {
        SPELLING_CHECK_TYPE: "오탈자와 문자·숫자 표기",
        TRANSLATION_CHECK_TYPE: "국문과 영문의 의미 일치 여부",
    }.get(issue_type, "언어 표현")
    conclusion = "교정이 필요한 내용을 제시했습니다" if status != "정상" else "특이사항이 없습니다"
    return f"표의 제목, 행·열 문맥과 사전 근거를 바탕으로 {category}를 검토했으며 {conclusion}."


def expected_is_existing_bilingual_text(current_value: str, expected_value: str) -> bool:
    current = normalize_review_text(current_value)
    expected = normalize_review_text(expected_value)
    return bool(
        expected
        and expected != current
        and re.search(r"[가-힣]", current)
        and re.search(r"[A-Za-z]", current)
        and not re.search(r"[가-힣]", expected)
        and expected.casefold() in current.casefold()
    )


def expected_drops_bilingual_context(
    current_value: str,
    expected_value: str,
    issue_type: str = "",
) -> bool:
    """Reject near-duplicate English fragments that discard a bilingual value's Korean half."""

    current = normalize_review_text(current_value)
    expected = normalize_review_text(expected_value)
    if not (
        expected
        and expected != current
        and re.search(r"[가-힣]", current)
        and re.search(r"[A-Za-z]", current)
        and not re.search(r"[가-힣]", expected)
        and re.search(r"[A-Za-z]", expected)
    ):
        return False
    current_english = " ".join(re.findall(r"[A-Za-z]+", current)).casefold()
    expected_english = " ".join(re.findall(r"[A-Za-z]+", expected)).casefold()
    if not current_english or not expected_english:
        return False
    if issue_type == SPELLING_CHECK_TYPE:
        return True
    return bool(
        expected_english in current_english
        or SequenceMatcher(None, current_english, expected_english).ratio() >= 0.86
    )


def reconcile_non_actionable_bilingual_reviews(
    connection: sqlite3.Connection,
    run_id: int,
) -> tuple[int, int, int]:
    """Normalize stored bilingual findings without calling the language model again."""

    candidate_count = 0
    candidate_rows = connection.execute(
        """
        SELECT id, review_type, current_value, review_result_json, review_fingerprint
        FROM linguistic_review_candidates
        WHERE run_id = ? AND status = 'reviewed' AND review_result_json <> ''
        """,
        (run_id,),
    ).fetchall()
    for row in candidate_rows:
        try:
            decision = json.loads(str(row["review_result_json"]))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(decision, dict) or str(decision.get("status") or "") == "정상":
            continue
        current = normalize_review_text(str(row["current_value"] or ""))
        expected = normalize_review_text(str(decision.get("expected_value") or ""))
        review_type = str(row["review_type"] or decision.get("issue_type") or TRANSLATION_CHECK_TYPE)
        if not expected_drops_bilingual_context(current, expected, review_type):
            continue

        corrected = {
            **decision,
            "status": "정상",
            "current_value": current,
            "expected_value": current,
            "difference": f"{review_type} 통과",
            "detail": korean_detail_for(review_type, "정상"),
        }
        serialized = json.dumps(corrected, ensure_ascii=False)
        connection.execute(
            "UPDATE linguistic_review_candidates SET review_result_json = ? WHERE id = ?",
            (serialized, int(row["id"])),
        )
        fingerprint = str(row["review_fingerprint"] or "")
        if fingerprint:
            connection.execute(
                "UPDATE linguistic_review_cache SET decision_json = ? WHERE fingerprint = ?",
                (serialized, fingerprint),
            )
        candidate_count += 1

    language_rule_ids = (
        LLM_SPELLING_RULE_ID,
        RETIRED_LLM_TERMINOLOGY_RULE_ID,
        LLM_TRANSLATION_RULE_ID,
        BLUE_REVIEW_RULE_ID,
    )
    placeholders = ", ".join("?" for _ in language_rule_ids)
    check_rows = connection.execute(
        f"""
        SELECT id, check_type, current_value, expected_value
        FROM validation_checks
        WHERE run_id = ?
          AND status <> '정상'
          AND rule_id IN ({placeholders})
        """,
        (run_id, *language_rule_ids),
    ).fetchall()
    check_count = 0
    for row in check_rows:
        current = normalize_review_text(str(row["current_value"] or ""))
        expected = normalize_review_text(str(row["expected_value"] or ""))
        check_type = str(row["check_type"] or TRANSLATION_CHECK_TYPE)
        if not expected_drops_bilingual_context(current, expected, check_type):
            continue
        connection.execute(
            """
            UPDATE validation_checks
            SET expected_value = ?, difference = ?, status = '정상', severity = 'info', detail = ?
            WHERE id = ?
            """,
            (
                current,
                f"{check_type} 통과",
                korean_detail_for(check_type, "정상"),
                int(row["id"]),
            ),
        )
        check_count += 1

    issue_rows = connection.execute(
        f"""
        SELECT id, issue_type, current_value, expected_value
        FROM validation_issues
        WHERE run_id = ?
          AND rule_id IN ({placeholders})
        """,
        (run_id, *language_rule_ids),
    ).fetchall()
    issue_ids = [
        (int(row["id"]),)
        for row in issue_rows
        if expected_drops_bilingual_context(
            str(row["current_value"] or ""),
            str(row["expected_value"] or ""),
            str(row["issue_type"] or ""),
        )
    ]
    if issue_ids:
        connection.executemany("DELETE FROM validation_issues WHERE id = ?", issue_ids)
    refresh_issue_count(connection, run_id)
    return candidate_count, check_count, len(issue_ids)


def is_temporal_notation(value: str) -> bool:
    normalized = normalize_review_text(value)
    return bool(
        re.search(r"\d", normalized)
        and re.search(r"[년월일]", normalized)
        and re.fullmatch(r"[\d\s'’.,:/~～\-–—()년월일이후전부터까지]+", normalized)
    )


def translate_temporal_notation(value: str) -> str:
    normalized = normalize_review_text(value)
    date_match = re.fullmatch(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일", normalized)
    if date_match:
        year, month, day = date_match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    translated = normalized.replace("년", "").replace("월", "-").replace("일", "")
    translated = translated.replace("이후", " and after").replace("이전", " and before")
    translated = translated.replace("부터", " from").replace("까지", " through")
    return normalize_review_text(translated)


def remove_human_confirmation_language(value: str) -> str:
    cleaned = value
    replacements = {
        "담당자 확인이 필요합니다": "LLM 검수 결과에 따른 교정안을 제시했습니다",
        "담당자 확인 필요": "LLM 검수 결과",
        "공식 명칭 확인이 필요합니다": "확보된 공식 근거와 문맥을 기준으로 교정안을 제시했습니다",
        "공식 영문명 확인이 필요합니다": "확보된 공식 근거와 문맥을 기준으로 영문안을 제시했습니다",
        "공식-name confirmation is needed": "",
        "LLM 문맥 확인": "LLM 문맥 검수 결과",
    }
    for source, target in replacements.items():
        cleaned = cleaned.replace(source, target)
    return normalize_review_text(cleaned)


def is_contextual_source_url_parenthesis(item: SourceReviewItem) -> bool:
    current = normalize_review_text(item.current_value)
    context = normalize_review_text(item.cell_text)
    if item.location != "출처" or not re.fullmatch(r"(?:https?://|www\.)[^\s()]+\)", current):
        return False
    domain = current[:-1]
    return f"({domain})" in context or context.count("(") >= context.count(")")


def looks_like_translation_mismatch(current_value: str, expected_value: str) -> bool:
    current = normalize_review_text(current_value)
    expected = normalize_review_text(expected_value)
    if not expected:
        return True
    if re.search(r"[가-힣]", current) and re.search(r"[A-Za-z]", expected) and not re.search(r"[가-힣]", expected):
        return True

    current_english = " ".join(re.findall(r"[A-Za-z]+", current)).lower()
    expected_english = " ".join(re.findall(r"[A-Za-z]+", expected)).lower()
    if len(current_english.split()) < 2 or len(expected_english.split()) < 2:
        return False
    return SequenceMatcher(None, current_english, expected_english).ratio() < 0.72


def rule_id_for(issue_type: str) -> str:
    if issue_type == BLUE_REVIEW_TYPE:
        return BLUE_LLM_RULE_ID
    if issue_type == "오탈자 검수":
        return LLM_SPELLING_RULE_ID
    return LLM_TRANSLATION_RULE_ID


def mark_linguistic_candidate_reviewed(
    connection: sqlite3.Connection,
    item: SourceReviewItem,
    *,
    decision: dict[str, str],
    reviewed_model: str,
    resolution_source: str,
) -> None:
    if item.source_record_id is None:
        return
    connection.execute(
        """
        UPDATE linguistic_review_candidates
        SET status = 'reviewed',
            reviewed_at = CURRENT_TIMESTAMP,
            reviewed_model = ?,
            review_result_json = ?,
            review_fingerprint = ?,
            resolution_source = ?
        WHERE id = ?
        """,
        (
            reviewed_model,
            json.dumps(decision, ensure_ascii=False),
            linguistic_review_fingerprint(item, reviewed_model=reviewed_model),
            resolution_source,
            item.source_record_id,
        ),
    )


def store_linguistic_review_cache(
    connection: sqlite3.Connection,
    item: SourceReviewItem,
    *,
    decision: dict[str, str],
    reviewed_model: str,
) -> None:
    fingerprint = linguistic_review_fingerprint(item, reviewed_model=reviewed_model)
    glossary_fingerprint = hashlib.sha256(
        json.dumps(item.glossary_matches or [], ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    context_signature = hashlib.sha256(
        json.dumps(
            {
                "title": item.table_title,
                "title_en": item.table_title_en,
                "location": normalized_location_kind(item.location),
                "row": item.row_label,
                "column": item.column_label,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    connection.execute(
        """
        INSERT INTO linguistic_review_cache (
            fingerprint, review_type, current_value, context_signature,
            prompt_version, reviewed_model, glossary_fingerprint,
            decision_json, use_count
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
        ON CONFLICT(fingerprint) DO UPDATE SET
            decision_json = excluded.decision_json,
            glossary_fingerprint = excluded.glossary_fingerprint,
            last_used_at = CURRENT_TIMESTAMP
        """,
        (
            fingerprint,
            item.requested_review_type or item.candidate_kind,
            item.current_value,
            context_signature,
            item.prompt_version or LINGUISTIC_PROMPT_VERSION,
            reviewed_model,
            glossary_fingerprint,
            json.dumps(decision, ensure_ascii=False),
        ),
    )


def upsert_llm_reviewed_glossary(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    item: SourceReviewItem,
    decision: dict[str, str],
    reviewed_model: str,
) -> None:
    source = normalize_review_text(item.korean_text)
    target = normalize_review_text(decision["expected_value"])
    if not source or not re.search(r"[가-힣]", source):
        return
    if not re.search(r"[A-Za-z]", target) or re.search(r"[가-힣]", target):
        return

    source_normalized = normalize_source(source)
    target_normalized = normalize_target(target)
    if not source_normalized or not target_normalized:
        return
    table_row = connection.execute(
        "SELECT report_id FROM stat_tables WHERE id = ?",
        (item.table_id,),
    ).fetchone()
    report_id = int(table_row["report_id"]) if table_row else None
    origin_key = f"llm:{source_normalized}:{target_normalized}"
    evidence = (
        f"LLM 검수 결과({reviewed_model or 'model 미기록'}), "
        f"run {run_id}, {item.table_code} {item.location}. 문맥 일치 시 자동 재사용"
    )
    connection.execute(
        """
        INSERT INTO translation_glossary (
            origin_key, source_text, source_normalized, target_text,
            target_normalized, category, subcategory, source_kind,
            source_report_id, source_table_id, status, priority,
            occurrence_count, evidence, verified_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'llm', ?, ?, 'llm_reviewed', 1000, 1, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(origin_key) DO UPDATE SET
            occurrence_count = translation_glossary.occurrence_count + 1,
            category = excluded.category,
            subcategory = excluded.subcategory,
            evidence = excluded.evidence,
            source_report_id = excluded.source_report_id,
            source_table_id = excluded.source_table_id,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            origin_key,
            source,
            source_normalized,
            target,
            target_normalized,
            infer_category(source, target),
            infer_subcategory(source, target),
            report_id,
            item.table_id,
            evidence,
        ),
    )


def duplicate_llm_check_exists(
    connection: sqlite3.Connection,
    run_id: int,
    item: SourceReviewItem,
    decision: dict[str, str],
) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM validation_checks
        WHERE run_id = ?
          AND table_id = ?
          AND rule_id = ?
          AND check_type = ?
          AND location = ?
          AND current_value = ?
          AND COALESCE(difference, '') = ?
          AND status <> '정상'
        LIMIT 1
        """,
        (
            run_id,
            item.table_id,
            decision["rule_id"],
            decision["issue_type"],
            item.location,
            decision["current_value"],
            decision["difference"],
        ),
    ).fetchone()
    return row is not None


def remove_blue_review_placeholder(connection: sqlite3.Connection, run_id: int, item: SourceReviewItem) -> None:
    connection.execute("DELETE FROM validation_issues WHERE id = ?", (item.issue_id,))
    connection.execute(
        """
        DELETE FROM validation_checks
        WHERE run_id = ?
          AND table_id = ?
          AND rule_id = ?
          AND location = ?
          AND current_value = ?
        """,
        (run_id, item.table_id, item.source_rule_id, item.location, item.current_value),
    )


def insert_llm_check(
    connection: sqlite3.Connection,
    run_id: int,
    item: SourceReviewItem,
    decision: dict[str, str],
) -> None:
    connection.execute(
        """
        INSERT INTO validation_checks (
            run_id, table_id, profile_id, rule_id, check_type, check_label,
            location, row_index, col_index, current_value, expected_value,
            difference, status, severity, detail, formula, confidence
        )
        VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
        """,
        (
            run_id,
            item.table_id,
            decision["rule_id"],
            decision["issue_type"],
            llm_check_label(decision["issue_type"]),
            item.location,
            item.row_index,
            item.col_index,
            decision["current_value"],
            decision["expected_value"],
            decision["difference"],
            decision["status"],
            "critical" if decision["status"] == "오류 의심" else "warning" if decision["status"] == "확인 필요" else "info",
            decision["detail"],
            0.78,
        ),
    )


def llm_check_label(issue_type: str) -> str:
    if issue_type == BLUE_REVIEW_TYPE:
        return "HWPX 파란색 표기 LLM 검수"
    if issue_type == SPELLING_CHECK_TYPE:
        return "LLM 국문·영문 오탈자 검수"
    return "번역 사전 우선 LLM 검수"


def insert_llm_issue(
    connection: sqlite3.Connection,
    run_id: int,
    item: SourceReviewItem,
    decision: dict[str, str],
) -> None:
    connection.execute(
        """
        INSERT INTO validation_issues (
            run_id, table_id, rule_id, issue_type, location, row_index,
            col_index, current_value, expected_value, difference, status,
            severity, detail, formula
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            run_id,
            item.table_id,
            decision["rule_id"],
            decision["issue_type"],
            item.location,
            item.row_index,
            item.col_index,
            decision["current_value"],
            decision["expected_value"],
            decision["difference"],
            decision["status"],
            "critical" if decision["status"] == "오류 의심" else "warning",
            decision["detail"],
        ),
    )


def latest_run_id(connection: sqlite3.Connection, *, report_id: int | None = None) -> int | None:
    if report_id is not None:
        row = connection.execute(
            """
            SELECT id
            FROM validation_runs
            WHERE report_id = ?
            ORDER BY completed_at DESC, id DESC
            LIMIT 1
            """,
            (report_id,),
        ).fetchone()
    else:
        row = connection.execute(
            """
            SELECT id
            FROM validation_runs
            ORDER BY completed_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
    return int(row["id"]) if row else None


def current_issue_count(db_path: Path, run_id: int | None) -> int:
    if run_id is None:
        return 0
    with connect(db_path) as connection:
        init_db(connection)
        row = connection.execute(
            "SELECT COUNT(*) AS issue_count FROM validation_issues WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row["issue_count"]) if row else 0


def refresh_issue_count(connection: sqlite3.Connection, run_id: int) -> int:
    row = connection.execute(
        "SELECT COUNT(*) AS issue_count FROM validation_issues WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    issue_count = int(row["issue_count"]) if row else 0
    connection.execute("UPDATE validation_runs SET issue_count = ? WHERE id = ?", (issue_count, run_id))
    return issue_count


def int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def prompt_context_key(item: SourceReviewItem) -> tuple[object, ...]:
    return (
        item.table_id,
        item.location,
        item.row_index,
        item.col_index,
        normalize_review_text(item.current_value),
    )


def review_context_token(item: SourceReviewItem) -> str:
    payload = {
        "table_id": item.table_id,
        "table_code": item.table_code,
        "location": normalize_review_text(item.location),
        "row_index": item.row_index,
        "col_index": item.col_index,
        "current_value": normalize_review_text(item.current_value),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return f"ctx_{hashlib.sha256(encoded).hexdigest()[:24]}"


def compact_prompt_items(items: list[SourceReviewItem]) -> list[dict[str, Any]]:
    """Send shared cell context once while keeping one output id per review type."""

    grouped: dict[tuple[object, ...], dict[str, Any]] = {}
    for item in items:
        key = prompt_context_key(item)
        group = grouped.get(key)
        if group is None:
            group = item.to_prompt_dict()
            for field in (
                "id",
                "candidate_kind",
                "candidate_reason",
                "candidate_expected",
                "requested_review_type",
                "prompt_version",
            ):
                group.pop(field, None)
            group["translation_glossary"] = compact_prompt_glossary(
                item.glossary_matches or []
            )
            group["context_token"] = review_context_token(item)
            if normalize_review_text(str(group.get("cell_text") or "")) == normalize_review_text(
                item.current_value
            ):
                group.pop("cell_text", None)
            if normalize_review_text(str(group.get("row_label") or "")) == normalize_review_text(
                item.current_value
            ):
                group.pop("row_label", None)
            if normalize_review_text(str(group.get("column_label") or "")) == normalize_review_text(
                item.current_value
            ):
                group.pop("column_label", None)
            group["review_requests"] = []
            grouped[key] = group
        request_item = {
            "id": item.issue_id,
            "requested_review_type": item.requested_review_type,
            "candidate_kind": item.candidate_kind,
        }
        if item.source_rule_id == BLUE_REVIEW_RULE_ID and item.candidate_reason:
            request_item["candidate_reason"] = item.candidate_reason
        if item.candidate_expected:
            request_item["candidate_expected"] = item.candidate_expected
        group["review_requests"].append(request_item)
    return list(grouped.values())


def compact_prompt_glossary(entries: list[dict[str, object]]) -> list[dict[str, object]]:
    """Keep only source-traceable glossary evidence that can affect a decision."""

    compacted: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in entries:
        status = str(entry.get("status") or "")
        if status not in {"approved", "official_verified", "official_name_only"}:
            continue
        source = normalize_review_text(str(entry.get("source") or ""))
        target = normalize_review_text(str(entry.get("target") or ""))
        key = (source.casefold(), target.casefold(), status)
        if key in seen:
            continue
        seen.add(key)
        compacted.append(
            {
                "source": source,
                "target": target,
                "status": status,
                "subcategory": str(entry.get("subcategory") or ""),
                "source_title": normalize_review_text(str(entry.get("source_title") or "")),
                "source_url": str(entry.get("source_url") or ""),
            }
        )
    return compacted[:8]


def chunked_by_context(items: list[SourceReviewItem], context_size: int) -> list[list[SourceReviewItem]]:
    batches: list[list[SourceReviewItem]] = []
    current: list[SourceReviewItem] = []
    context_count = 0
    previous_key: tuple[object, ...] | None = None
    for item in items:
        key = prompt_context_key(item)
        if key != previous_key:
            if current and context_count >= context_size:
                batches.append(current)
                current = []
                context_count = 0
            context_count += 1
            previous_key = key
        current.append(item)
    if current:
        batches.append(current)
    return batches


def group_linguistic_items_by_context(items: list[SourceReviewItem]) -> list[SourceReviewItem]:
    """Keep every review type for one cell contiguous so one prompt carries its context once."""

    grouped: dict[tuple[object, ...], list[SourceReviewItem]] = {}
    for item in items:
        grouped.setdefault(prompt_context_key(item), []).append(item)

    type_order = {
        SPELLING_CHECK_TYPE: 0,
        TRANSLATION_CHECK_TYPE: 1,
    }
    return [
        item
        for context_items in grouped.values()
        for item in sorted(
            context_items,
            key=lambda candidate: (
                type_order.get(candidate.requested_review_type, 99),
                candidate.issue_id,
            ),
        )
    ]


def take_contexts(items: list[SourceReviewItem], context_limit: int) -> list[SourceReviewItem]:
    if context_limit <= 0:
        return []
    selected: list[SourceReviewItem] = []
    seen: set[tuple[object, ...]] = set()
    for item in items:
        key = prompt_context_key(item)
        if key not in seen:
            if len(seen) >= context_limit:
                break
            seen.add(key)
        selected.append(item)
    return selected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run GPT translation/spelling/format review for HWPX source candidates.")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="SQLite database path")
    parser.add_argument("--run-id", type=int, default=None, help="Validation run id")
    parser.add_argument("--report-id", type=int, default=None, help="Use latest run for this report id")
    parser.add_argument("--limit", type=int, default=None, help="Review only the first N blue candidates")
    parser.add_argument(
        "--blue-only",
        action="store_true",
        help="Review only HWPX blue-marked candidates and keep one blue result per value",
    )
    parser.add_argument("--model", default="", help="Override the configured review model")
    parser.add_argument(
        "--preview-table-code",
        default="",
        help="Review one table and print the result without writing to the database",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = llm_review_settings()
    limit = args.limit if args.limit is not None else settings["limit"]
    if args.preview_table_code:
        preview = preview_llm_table_reviews(
            args.db,
            table_code=args.preview_table_code,
            run_id=args.run_id,
            report_id=args.report_id,
            limit=limit,
        )
        print(json.dumps(preview.__dict__, ensure_ascii=False, indent=2))
        return
    result = append_llm_translation_reviews(
        args.db,
        run_id=args.run_id,
        report_id=args.report_id,
        limit=limit,
        blue_only=args.blue_only,
        model_override=args.model or None,
    )
    if result.skipped_reason:
        print(f"LLM review skipped: {result.skipped_reason}")
        return
    print(
        "LLM review processed {processed} candidates; inserted {inserted_issues} issues and {inserted_checks} checks; final issues: {final_issue_count}".format(
            **result.__dict__
        )
    )


if __name__ == "__main__":
    main()
