from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
import argparse
import json
from pathlib import Path
import sqlite3
import time
from typing import Any

from app.db.connection import DB_PATH, connect
from app.db.schema import init_db
from app.validation.blue_review import normalize_review_text
from app.validation.linguistic_policy import (
    SPELLING_CHECK_TYPE,
    SPELLING_REPLACEMENTS,
    TRANSLATION_CHECK_TYPE,
)
from app.validation.linguistic_review import LINGUISTIC_PROMPT_VERSION
from app.validation.llm_translation_review import (
    DEFAULT_BIZROUTER_MODEL,
    ResponsesAPIClient,
    SourceReviewItem,
    append_llm_translation_reviews,
    chunked_by_context,
    llm_review_settings,
    normalize_decision,
    refresh_issue_count,
    request_review_with_retries,
    rule_id_for,
)
from app.validation.translation_glossary import normalize_source, normalize_target


GLOSSARY_AUDIT_PROMPT_VERSION = f"{LINGUISTIC_PROMPT_VERSION}:glossary-audit-v1"
AUDITED_GLOSSARY_STATUSES = {"llm_verified", "rejected", "quarantined"}


@dataclass(frozen=True)
class GlossaryAuditPair:
    primary_id: int
    source_text: str
    target_text: str
    source_normalized: str
    target_normalized: str
    source_table_id: int | None
    table_code: str
    table_title: str
    evidence: str
    occurrence_count: int


@dataclass(frozen=True)
class GlossaryAuditResult:
    total_pairs: int
    deterministic_rejected: int
    authoritative_verified: int
    llm_reviewed: int
    llm_rejected: int
    reset_candidates: int
    remaining_reference_pairs: int


def audit_translation_glossary(
    db_path: Path = DB_PATH,
    *,
    model_override: str = DEFAULT_BIZROUTER_MODEL,
    limit: int | None = None,
    rerun_affected_candidates: bool = True,
) -> GlossaryAuditResult:
    settings = llm_review_settings()
    settings["model"] = model_override or settings["model"]
    if not settings["enabled"]:
        raise RuntimeError("LLM review is disabled")
    if not settings["api_key"]:
        raise RuntimeError(f"missing {settings['api_key_env']}")

    with connect(db_path) as connection:
        init_db(connection)
        total_pairs = count_distinct_reference_pairs(connection)
        with connection:
            deterministic_rejected = quarantine_known_spelling_contamination(connection)
            authoritative_verified = verify_authoritative_reference_pairs(connection)
        pairs = load_reference_pairs(connection, limit=limit)

    client = ResponsesAPIClient(
        api_key=str(settings["api_key"]),
        model=str(settings["model"]),
        base_url=str(settings["base_url"]),
        timeout=int(settings["timeout"]),
        provider=str(settings["provider"]),
    )
    items = [item for pair in pairs for item in audit_items_for_pair(pair)]
    pair_by_primary_id = {pair.primary_id: pair for pair in pairs}
    batches = chunked_by_context(items, int(settings["batch_size"]))
    llm_reviewed = 0
    llm_rejected = 0

    with ThreadPoolExecutor(max_workers=int(settings["concurrency"])) as executor:
        batch_iterator = iter(batches)
        pending: dict[Any, list[SourceReviewItem]] = {}
        for _ in range(int(settings["concurrency"])):
            try:
                batch = next(batch_iterator)
            except StopIteration:
                break
            pending[executor.submit(review_glossary_batch, client, batch)] = batch

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
                        reviewed, rejected = save_glossary_audit_batch(
                            connection,
                            batch,
                            decisions,
                            pair_by_primary_id,
                            model=str(settings["model"]),
                        )
                        llm_reviewed += reviewed
                        llm_rejected += rejected
            if errors:
                for future in pending:
                    future.cancel()
                raise RuntimeError(
                    f"Glossary audit failed after saving {llm_reviewed} pairs"
                ) from errors[0]

            for _ in completed:
                try:
                    batch = next(batch_iterator)
                except StopIteration:
                    break
                pending[executor.submit(review_glossary_batch, client, batch)] = batch
            time.sleep(float(settings["sleep_seconds"]))

    with connect(db_path) as connection:
        init_db(connection)
        with connection:
            reset_candidates, affected_runs = reset_rejected_dictionary_candidates(connection)
    if rerun_affected_candidates:
        for run_id in affected_runs:
            result = append_llm_translation_reviews(
                db_path,
                run_id=run_id,
                limit=None,
                standard_language_only=True,
                model_override=str(settings["model"]),
            )
            if result.skipped_reason:
                raise RuntimeError(
                    f"Affected language candidates were not rerun: {result.skipped_reason}"
                )

    with connect(db_path) as connection:
        remaining = count_distinct_reference_pairs(connection)
    return GlossaryAuditResult(
        total_pairs=total_pairs,
        deterministic_rejected=deterministic_rejected,
        authoritative_verified=authoritative_verified,
        llm_reviewed=llm_reviewed,
        llm_rejected=llm_rejected,
        reset_candidates=reset_candidates,
        remaining_reference_pairs=remaining,
    )


def quarantine_known_spelling_contamination(connection: sqlite3.Connection) -> int:
    contaminated: set[tuple[str, str]] = set()
    for row in connection.execute(
        """
        SELECT source_normalized, target_normalized, source_text, target_text
        FROM translation_glossary
        WHERE status = 'reference'
        """
    ).fetchall():
        combined = f"{row['source_text']} {row['target_text']}"
        if any(str(item["current"]) in combined for item in SPELLING_REPLACEMENTS):
            contaminated.add((str(row["source_normalized"]), str(row["target_normalized"])))
    for source_normalized, target_normalized in contaminated:
        rows = connection.execute(
            """
            SELECT source_text, target_text
            FROM translation_glossary
            WHERE source_normalized = ? AND target_normalized = ?
            ORDER BY id LIMIT 1
            """,
            (source_normalized, target_normalized),
        ).fetchone()
        current = f"{rows['source_text']} {rows['target_text']}" if rows else ""
        corrected = current
        reasons: list[str] = []
        for item in SPELLING_REPLACEMENTS:
            source = str(item["current"])
            target = str(item["expected"])
            if source in corrected:
                corrected = corrected.replace(source, target)
                reasons.append(f"{source} → {target}")
        record_audit_result(
            connection,
            source_normalized=source_normalized,
            target_normalized=target_normalized,
            source_text=str(rows["source_text"]) if rows else "",
            target_text=str(rows["target_text"]) if rows else "",
            audit_status="rejected",
            spelling_status="오류 의심",
            translation_status="정상",
            replacement_text=corrected,
            difference=", ".join(reasons),
            detail="명시 오탈자 사전에서 문자 누락 또는 숫자 혼입을 확인했습니다.",
            model="deterministic",
        )
        update_reference_pair_status(
            connection,
            source_normalized,
            target_normalized,
            "rejected",
        )
    return len(contaminated)


def verify_authoritative_reference_pairs(connection: sqlite3.Connection) -> int:
    pairs = connection.execute(
        """
        SELECT DISTINCT reference.source_normalized, reference.target_normalized,
               reference.source_text, reference.target_text
        FROM translation_glossary reference
        WHERE reference.status = 'reference'
          AND EXISTS (
              SELECT 1
              FROM translation_glossary trusted
              WHERE trusted.source_normalized = reference.source_normalized
                AND trusted.target_normalized = reference.target_normalized
                AND trusted.status IN ('approved', 'official_verified', 'seed')
          )
        """
    ).fetchall()
    for row in pairs:
        source_normalized = str(row["source_normalized"])
        target_normalized = str(row["target_normalized"])
        record_audit_result(
            connection,
            source_normalized=source_normalized,
            target_normalized=target_normalized,
            source_text=str(row["source_text"]),
            target_text=str(row["target_text"]),
            audit_status="verified",
            spelling_status="정상",
            translation_status="정상",
            replacement_text=f"{row['source_text']} {row['target_text']}",
            difference="공식·승인 사전과 정확히 일치",
            detail="공식 또는 승인된 사전의 국문·영문 쌍과 정확히 일치합니다.",
            model="authoritative-dictionary",
        )
        update_reference_pair_status(
            connection,
            source_normalized,
            target_normalized,
            "llm_verified",
        )
    return len(pairs)


def load_reference_pairs(
    connection: sqlite3.Connection,
    *,
    limit: int | None = None,
) -> list[GlossaryAuditPair]:
    limit_clause = "LIMIT ?" if limit is not None else ""
    params: tuple[object, ...] = (max(limit or 0, 0),) if limit is not None else ()
    rows = connection.execute(
        f"""
        SELECT MIN(tg.id) AS primary_id,
               MIN(tg.source_text) AS source_text,
               MIN(tg.target_text) AS target_text,
               tg.source_normalized, tg.target_normalized,
               MIN(tg.source_table_id) AS source_table_id,
               COALESCE(MIN(st.code), '사전') AS table_code,
               COALESCE(MIN(st.title), '번역 사전') AS table_title,
               GROUP_CONCAT(DISTINCT tg.evidence) AS evidence,
               SUM(tg.occurrence_count) AS occurrence_count
        FROM translation_glossary tg
        LEFT JOIN stat_tables st ON st.id = tg.source_table_id
        WHERE tg.status = 'reference'
        GROUP BY tg.source_normalized, tg.target_normalized
        ORDER BY SUM(tg.occurrence_count) DESC, MIN(tg.id)
        {limit_clause}
        """,
        params,
    ).fetchall()
    return [
        GlossaryAuditPair(
            primary_id=int(row["primary_id"]),
            source_text=str(row["source_text"]),
            target_text=str(row["target_text"]),
            source_normalized=str(row["source_normalized"]),
            target_normalized=str(row["target_normalized"]),
            source_table_id=int(row["source_table_id"]) if row["source_table_id"] else None,
            table_code=str(row["table_code"]),
            table_title=str(row["table_title"]),
            evidence=str(row["evidence"] or ""),
            occurrence_count=int(row["occurrence_count"] or 0),
        )
        for row in rows
    ]


def audit_items_for_pair(pair: GlossaryAuditPair) -> list[SourceReviewItem]:
    current = normalize_review_text(f"{pair.source_text} {pair.target_text}")
    common = {
        "source_rule_id": "source.glossary_audit",
        "candidate_reason": (
            f"연보 reference 사전 전수 감사; {pair.occurrence_count}회 관측; {pair.evidence}"
        ),
        "candidate_expected": "",
        "table_id": pair.source_table_id or 0,
        "table_code": pair.table_code,
        "table_title": pair.table_title,
        "table_title_en": "",
        "unit": "",
        "base_date": "",
        "location": "번역 사전",
        "row_index": None,
        "col_index": None,
        "current_value": current,
        "cell_text": current,
        "row_label": pair.source_text,
        "column_label": pair.target_text,
        "surrounding_rows": [],
        "korean_text": pair.source_text,
        "english_text": pair.target_text,
        "glossary_matches": [],
        "source_record_type": "glossary_audit",
        "source_record_id": pair.primary_id,
        "prompt_version": GLOSSARY_AUDIT_PROMPT_VERSION,
        "review_fingerprint": "",
    }
    return [
        SourceReviewItem(
            issue_id=pair.primary_id * 10 + 1,
            candidate_kind="glossary_spelling",
            requested_review_type=SPELLING_CHECK_TYPE,
            **common,
        ),
        SourceReviewItem(
            issue_id=pair.primary_id * 10 + 2,
            candidate_kind="translation_pair",
            requested_review_type=TRANSLATION_CHECK_TYPE,
            **common,
        ),
    ]


def review_glossary_batch(
    client: ResponsesAPIClient,
    batch: list[SourceReviewItem],
) -> list[dict[str, Any]]:
    """Audit pair quality without requiring a complete cell replacement.

    A glossary pair can contain a long comma-separated list. At this stage the
    model only decides whether the pair is trustworthy; affected cells are
    reviewed again later with the full table context and strict replacement
    validation.
    """
    decisions_by_id: dict[int, dict[str, Any]] = {}
    pending = list(batch)
    for _ in range(3):
        for raw in request_review_with_retries(client, pending):
            if not isinstance(raw, dict):
                continue
            try:
                issue_id = int(raw.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if issue_id:
                decisions_by_id[issue_id] = raw
        pending = [item for item in batch if item.issue_id not in decisions_by_id]
        if not pending:
            return list(decisions_by_id.values())

    missing = ", ".join(
        f"{item.table_code}:{item.location}:{item.requested_review_type}"
        for item in pending
    )
    raise RuntimeError(f"Glossary audit returned missing decisions: {missing}")


def save_glossary_audit_batch(
    connection: sqlite3.Connection,
    batch: list[SourceReviewItem],
    decisions: list[dict[str, Any]],
    pair_by_primary_id: dict[int, GlossaryAuditPair],
    *,
    model: str,
) -> tuple[int, int]:
    item_by_id = {item.issue_id: item for item in batch}
    normalized_by_pair: dict[int, dict[str, dict[str, str]]] = {}
    for raw in decisions:
        issue_id = int(raw.get("id") or 0)
        item = item_by_id.get(issue_id)
        if item is None:
            continue
        decision = normalize_decision(raw, item)
        primary_id = issue_id // 10
        normalized_by_pair.setdefault(primary_id, {})[item.requested_review_type] = decision

    reviewed = 0
    rejected = 0
    for primary_id, by_type in normalized_by_pair.items():
        pair = pair_by_primary_id[primary_id]
        spelling = by_type.get(SPELLING_CHECK_TYPE)
        translation = by_type.get(TRANSLATION_CHECK_TYPE)
        if spelling is None or translation is None:
            raise RuntimeError(f"Incomplete glossary audit result: {primary_id}")
        is_rejected = spelling["status"] != "정상" or translation["status"] != "정상"
        audit_status = "rejected" if is_rejected else "verified"
        replacement = (
            translation["expected_value"]
            if translation["status"] != "정상"
            else spelling["expected_value"]
        )
        difference = " / ".join(
            value["difference"]
            for value in (spelling, translation)
            if value["status"] != "정상"
        ) or "오탈자·번역 검수 통과"
        detail = " / ".join(
            value["detail"]
            for value in (spelling, translation)
            if value["status"] != "정상"
        ) or "고유 사전 쌍의 오탈자와 국문·영문 의미 대응을 확인했습니다."
        record_audit_result(
            connection,
            source_normalized=pair.source_normalized,
            target_normalized=pair.target_normalized,
            source_text=pair.source_text,
            target_text=pair.target_text,
            audit_status=audit_status,
            spelling_status=spelling["status"],
            translation_status=translation["status"],
            replacement_text=replacement,
            difference=difference,
            detail=detail,
            model=model,
        )
        update_reference_pair_status(
            connection,
            pair.source_normalized,
            pair.target_normalized,
            "rejected" if is_rejected else "llm_verified",
        )
        reviewed += 1
        rejected += int(is_rejected)
    return reviewed, rejected


def record_audit_result(
    connection: sqlite3.Connection,
    *,
    source_normalized: str,
    target_normalized: str,
    source_text: str,
    target_text: str,
    audit_status: str,
    spelling_status: str,
    translation_status: str,
    replacement_text: str,
    difference: str,
    detail: str,
    model: str,
) -> None:
    connection.execute(
        """
        INSERT INTO glossary_audit_results (
            source_normalized, target_normalized, source_text, target_text,
            audit_status, spelling_status, translation_status, replacement_text,
            difference, detail, model, prompt_version, audited_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(source_normalized, target_normalized, model, prompt_version)
        DO UPDATE SET
            source_text = excluded.source_text,
            target_text = excluded.target_text,
            audit_status = excluded.audit_status,
            spelling_status = excluded.spelling_status,
            translation_status = excluded.translation_status,
            replacement_text = excluded.replacement_text,
            difference = excluded.difference,
            detail = excluded.detail,
            audited_at = CURRENT_TIMESTAMP
        """,
        (
            source_normalized,
            target_normalized,
            source_text,
            target_text,
            audit_status,
            spelling_status,
            translation_status,
            replacement_text,
            difference,
            detail,
            model,
            GLOSSARY_AUDIT_PROMPT_VERSION,
        ),
    )


def update_reference_pair_status(
    connection: sqlite3.Connection,
    source_normalized: str,
    target_normalized: str,
    status: str,
) -> None:
    connection.execute(
        """
        UPDATE translation_glossary
        SET status = ?, updated_at = CURRENT_TIMESTAMP
        WHERE source_normalized = ? AND target_normalized = ?
          AND status = 'reference'
        """,
        (status, source_normalized, target_normalized),
    )


def reset_rejected_dictionary_candidates(
    connection: sqlite3.Connection,
) -> tuple[int, list[int]]:
    rejected_pairs = {
        (str(row["source_normalized"]), str(row["target_normalized"]))
        for row in connection.execute(
            """
            SELECT DISTINCT source_normalized, target_normalized
            FROM translation_glossary
            WHERE status = 'rejected'
            """
        ).fetchall()
    }
    if not rejected_pairs:
        return 0, []
    rows = connection.execute(
        """
        SELECT *
        FROM linguistic_review_candidates
        WHERE status = 'reviewed' AND resolution_source = 'dictionary'
          AND review_type IN (?, ?)
          AND run_id IN (
              SELECT MAX(id)
              FROM validation_runs
              GROUP BY report_id
          )
        """,
        (SPELLING_CHECK_TYPE, TRANSLATION_CHECK_TYPE),
    ).fetchall()
    reset = 0
    affected_runs: set[int] = set()
    for row in rows:
        pair = (
            normalize_source(str(row["korean_text"])),
            normalize_target(str(row["english_text"])),
        )
        if pair not in rejected_pairs:
            continue
        run_id = int(row["run_id"])
        table_id = int(row["table_id"])
        review_type = str(row["review_type"])
        location = str(row["location"])
        current = str(row["current_value"])
        connection.execute(
            """
            UPDATE linguistic_review_candidates
            SET status = 'pending', reviewed_model = '', review_result_json = '',
                review_fingerprint = '', resolution_source = '', reviewed_at = NULL
            WHERE id = ?
            """,
            (int(row["id"]),),
        )
        rule_id = rule_id_for(review_type)
        connection.execute(
            """
            DELETE FROM validation_checks
            WHERE run_id = ? AND table_id = ? AND rule_id = ?
              AND location = ? AND current_value = ?
            """,
            (run_id, table_id, rule_id, location, current),
        )
        connection.execute(
            """
            DELETE FROM validation_issues
            WHERE run_id = ? AND table_id = ? AND rule_id = ?
              AND location = ? AND current_value = ?
            """,
            (run_id, table_id, rule_id, location, current),
        )
        reset += 1
        affected_runs.add(run_id)
    for run_id in affected_runs:
        refresh_issue_count(connection, run_id)
    return reset, sorted(affected_runs)


def count_distinct_reference_pairs(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        """
        SELECT COUNT(*) AS pair_count
        FROM (
            SELECT 1
            FROM translation_glossary
            WHERE status = 'reference'
            GROUP BY source_normalized, target_normalized
        )
        """
    ).fetchone()
    return int(row["pair_count"] if row else 0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit yearbook reference glossary pairs")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--model", default=DEFAULT_BIZROUTER_MODEL)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-rerun", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = audit_translation_glossary(
        args.db,
        model_override=args.model,
        limit=args.limit,
        rerun_affected_candidates=not args.no_rerun,
    )
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
