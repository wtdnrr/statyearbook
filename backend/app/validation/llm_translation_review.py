from __future__ import annotations

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
from urllib import error, request

from app.db.schema import DB_PATH, connect, init_db
from app.validation.blue_review import BLUE_REVIEW_RULE_ID, BLUE_REVIEW_TYPE, normalize_review_text
from app.validation.linguistic_policy import (
    LINGUISTIC_CHECK_TYPES,
    SPELLING_CHECK_TYPE,
    TERMINOLOGY_CHECK_TYPE,
    TRANSLATION_CHECK_TYPE,
)
from app.validation.linguistic_review import (
    LINGUISTIC_CANDIDATE_RULE_ID,
    LINGUISTIC_PROMPT_VERSION,
)
from app.validation.source_review import SOURCE_FORMAT_RULE_ID, SOURCE_FORMAT_TYPE
from app.validation.translation_glossary import (
    infer_category,
    infer_subcategory,
    normalize_source,
    normalize_target,
    parse_glossary_context,
)


OPENAI_RESPONSES_PATH = "/responses"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TRANSLATION_MODEL = "gpt-5.4-mini"
LLM_TRANSLATION_RULE_ID = "llm.translation_review"
LLM_SPELLING_RULE_ID = "llm.spelling_review"
LLM_TERMINOLOGY_RULE_ID = "llm.terminology_review"


@dataclass(frozen=True)
class LLMReviewResult:
    processed: int
    inserted_issues: int
    inserted_checks: int
    final_issue_count: int
    skipped_reason: str = ""


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
            dictionary_counts = resolve_linguistic_candidates_from_glossary(connection, run_id)
            cache_counts = reuse_cached_linguistic_reviews(
                connection,
                run_id,
                reviewed_model=model,
            )
            refresh_issue_count(connection, run_id)
    return (
        dictionary_counts[0] + cache_counts[0],
        dictionary_counts[1] + cache_counts[1],
        dictionary_counts[2] + cache_counts[2],
    )


def append_llm_translation_reviews(
    db_path: Path = DB_PATH,
    *,
    run_id: int | None = None,
    report_id: int | None = None,
    limit: int | None = None,
) -> LLMReviewResult:
    settings = llm_review_settings()
    with connect(db_path) as connection:
        init_db(connection)
        resolved_run_id = run_id or latest_run_id(connection, report_id=report_id)
        if resolved_run_id is None:
            return LLMReviewResult(0, 0, 0, 0, "no validation run")

        dictionary_counts = resolve_linguistic_candidates_from_glossary(
            connection,
            resolved_run_id,
        )
        cache_counts = reuse_cached_linguistic_reviews(
            connection,
            resolved_run_id,
            reviewed_model=settings["model"],
        )
        linguistic_items = load_linguistic_review_items(connection, resolved_run_id)
        source_items = load_source_review_items(connection, resolved_run_id)
        format_items = [item for item in source_items if item.source_rule_id == SOURCE_FORMAT_RULE_ID]
        other_source_items = [item for item in source_items if item.source_rule_id != SOURCE_FORMAT_RULE_ID]
        items = [*format_items, *interleave_linguistic_items(linguistic_items), *other_source_items]
        if limit is not None:
            items = items[:limit]

    inserted_issues = dictionary_counts[0] + cache_counts[0]
    inserted_checks = dictionary_counts[1] + cache_counts[1]
    processed = dictionary_counts[2] + cache_counts[2]

    if not items:
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
            "missing OPENAI_API_KEY",
        )

    client = OpenAIResponsesClient(
        api_key=settings["api_key"],
        model=settings["model"],
        base_url=settings["base_url"],
        timeout=settings["timeout"],
    )

    for batch in chunked_by_context(items, settings["batch_size"]):
        decisions = client.review(batch)
        decisions_by_id = {
            int(decision["id"]): decision
            for decision in decisions
            if isinstance(decision, dict) and int_or_none(decision.get("id")) is not None
        }
        for _ in range(2):
            retry_items = [
                item
                for item in batch
                if item.issue_id not in decisions_by_id
                or review_decision_needs_retry(decisions_by_id[item.issue_id], item)
            ]
            if not retry_items:
                break
            for retry_decision in client.review(retry_items, require_english_replacement=True):
                if not isinstance(retry_decision, dict):
                    continue
                retry_id = int_or_none(retry_decision.get("id"))
                if retry_id is not None:
                    decisions_by_id[retry_id] = retry_decision
        unresolved = [
            item
            for item in batch
            if item.issue_id not in decisions_by_id
            or review_decision_needs_retry(decisions_by_id[item.issue_id], item)
        ]
        if unresolved:
            codes = ", ".join(f"{item.table_code}:{item.location}" for item in unresolved)
            raise RuntimeError(f"LLM review returned incomplete replacement text: {codes}")
        decisions = list(decisions_by_id.values())
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
        time.sleep(settings["sleep_seconds"])

    if limit is None:
        with connect(db_path) as connection:
            init_db(connection)
            pending = pending_linguistic_candidate_count(connection, resolved_run_id)
        if pending:
            raise RuntimeError(f"LLM 전수 언어 검수가 완료되지 않았습니다: pending {pending}건")

    return LLMReviewResult(
        processed=processed,
        inserted_issues=inserted_issues,
        inserted_checks=inserted_checks,
        final_issue_count=current_issue_count(db_path, resolved_run_id),
    )


def pending_linguistic_candidate_count(connection: sqlite3.Connection, run_id: int) -> int:
    row = connection.execute(
        """
        SELECT COUNT(*) AS candidate_count
        FROM linguistic_review_candidates
        WHERE run_id = ? AND status <> 'reviewed'
        """,
        (run_id,),
    ).fetchone()
    return int(row["candidate_count"]) if row else 0


def llm_review_settings() -> dict[str, Any]:
    load_local_env_file()
    enabled_value = os.getenv("OPENAI_LLM_REVIEW_ENABLED", "1").strip().lower()
    limit_value = os.getenv("OPENAI_LLM_REVIEW_LIMIT", "").strip()
    return {
        "enabled": enabled_value not in {"0", "false", "no", "off"},
        "api_key": os.getenv("OPENAI_API_KEY", "").strip(),
        "model": os.getenv("OPENAI_TRANSLATION_MODEL", DEFAULT_TRANSLATION_MODEL).strip()
        or DEFAULT_TRANSLATION_MODEL,
        "base_url": os.getenv("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL).rstrip("/"),
        "batch_size": max(int(os.getenv("OPENAI_LLM_REVIEW_BATCH_SIZE", "12")), 1),
        "timeout": max(int(os.getenv("OPENAI_LLM_REVIEW_TIMEOUT", "90")), 10),
        "sleep_seconds": max(float(os.getenv("OPENAI_LLM_REVIEW_SLEEP", "0.2")), 0.0),
        "limit": int(limit_value) if limit_value.isdigit() else None,
    }


def load_local_env_file() -> None:
    env_path = Path(__file__).resolve().parents[3] / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


class OpenAIResponsesClient:
    def __init__(self, *, api_key: str, model: str, base_url: str, timeout: int) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.timeout = timeout

    def review(
        self,
        items: list[SourceReviewItem],
        *,
        require_english_replacement: bool = False,
    ) -> list[dict[str, Any]]:
        prompt_payload = {
            "items": compact_prompt_items(items),
            "allowed_statuses": ["정상", "확인 필요", "오류 의심"],
            "allowed_issue_types": ["번역 검수", "오탈자 검수", "용어 제안"],
        }
        body = {
            "model": self.model,
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
            "max_output_tokens": max(1800, len(items) * 260),
        }
        response = self._post(body)
        parsed = parse_response_json(response)
        raw_items = parsed.get("items", [])
        return raw_items if isinstance(raw_items, list) else []

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = request.Request(
            f"{self.base_url}{OPENAI_RESPONSES_PATH}",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        for attempt in range(3):
            try:
                with request.urlopen(req, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except error.HTTPError as exc:
                body_text = exc.read().decode("utf-8", errors="replace")
                if exc.code not in {408, 409, 429, 500, 502, 503, 504} or attempt == 2:
                    raise RuntimeError(f"OpenAI API error {exc.code}: {body_text[:800]}") from exc
            except error.URLError as exc:
                if attempt == 2:
                    raise RuntimeError(f"OpenAI API request failed: {exc.reason}") from exc
            time.sleep(2**attempt)
        raise RuntimeError("OpenAI API request failed after retries")


SYSTEM_PROMPT = """You are a meticulous Korean government statistical yearbook language reviewer.

The input groups requests that share one title, metadata, header, or cell context. For every entry in review_requests, return one result using that request id. Review the complete shared value for only its requested_review_type and return exactly the same issue_type. Values may contain numbers and text together. Never turn a terminology suggestion into a spelling error or a translation mismatch into a spelling error.

The three categories are strictly separated:
1. 오탈자 검수: Find clear Korean or English misspellings, broken characters, accidental casing errors, malformed numeric separators such as 3.445 instead of 3,445 in an integer context, and text that is clearly inconsistent with related cells. Do not use this category for a merely preferable expression. A clear error is "오류 의심"; an uncertain contextual notation is "확인 필요".
2. 용어 제안: The source is understandable, but a dictionary meaning, official name, public-sector wording, grammar, capitalization convention, or statistical term can be improved. Return "확인 필요", never "오류 의심".
3. 번역 검수: Check whether Korean and English have the same meaning. If English is missing, provide the English replacement. Return "확인 필요" for a missing, mismatched, awkward, or unverifiable translation, never "오류 의심".

Translation dictionary policy:
- translation_glossary is evidence, not permission to skip review.
- status "approved" means human-approved and "official_verified" means the Korean and English forms were collected together from the cited official source. Treat these as the strongest evidence unless the supplied context proves they identify a different entity.
- status "official_name_only" means only the Korean proper name and entity classification were confirmed by an official registry; it does not verify any English translation.
- status "llm_reviewed", "reference", and "seed" are supporting evidence only. They may be wrong or outdated and must never override context or an official source.
- Check source_url, source_title, validity dates, aliases, category, and subcategory before applying an entry.
- When no usable glossary entry exists, infer conservatively from the table context and provide the best concrete replacement directly.
- Never mechanically translate organization suffixes such as 부, 처, 청, 원, 위원회. Organization names, event names and Korean administrative areas are proper names.

General rules:
- Use the table title, row label, column label, unit, surrounding rows and glossary evidence.
- candidate_kind beginning with "blue_text" is an exact HWPX blue-marked segment. Review the requested category for that segment and return a concrete corrected value, not a review request.
- For a Korean-only blue_text translation request, expected_value must be a standalone English translation. Never return the Korean source, a placeholder, or an instruction to translate later.
- For an English-only value under 번역 검수, use nearby Korean labels when available to check semantic correspondence. If there is no Korean counterpart and the English is not problematic, return 정상 instead of inventing a Korean source.
- For Korean-only text under 번역 검수, provide a standalone English translation even if the same value is not normally printed bilingually.
- For number_format, distinguish thousands separators from decimals using the unit and peer values. Preserve valid decimals.
- For punctuation_format, distinguish a typo from a line-break marker, URL, date, official notation or valid compound word.
- Treat semantic hyphens such as "e-learning" as correct. Remove a hyphen only when it clearly marks a line break inside one word.
- Do not perform or alter sum and ratio calculations.
- Use "정상" when no action is needed.
- expected_value must never be empty. For "정상", copy the reviewed source or current English. For a finding, provide only the corrected/recommended replacement.
- difference and detail must be written in Korean. detail must briefly state the evidence used, including a glossary source when applicable.
- Never output placeholders such as "담당자 확인", "담당자 확인 필요", "공식 명칭 확인 필요" or "LLM 문맥 확인". The application has no separate human-approval workflow, so always return the reviewed replacement and conclusion directly.

Return JSON only, no markdown:
{
  "items": [
    {
      "id": 123,
      "status": "정상|확인 필요|오류 의심",
      "issue_type": "번역 검수|오탈자 검수|용어 제안",
      "current_value": "original text reviewed",
      "expected_value": "recommended English/correction or empty string",
      "difference": "short reason",
      "detail": "one concise Korean explanation"
    }
  ]
}
"""


ENGLISH_REPLACEMENT_RETRY_PROMPT = """The previous response was incomplete.
For every Korean source in this retry batch, expected_value must be the actual standalone English replacement text. Do not copy Korean, leave the value blank, write an explanation, or request later confirmation. If an official name is uncertain, provide the best conservative public-sector English translation and state the evidence limitation only in detail. difference and detail must be written in Korean.
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
                    "status": {"type": "string", "enum": ["정상", "확인 필요", "오류 의심"]},
                    "issue_type": {"type": "string", "enum": ["번역 검수", "오탈자 검수", "용어 제안"]},
                    "current_value": {"type": "string"},
                    "expected_value": {"type": "string", "minLength": 1},
                    "difference": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": [
                    "id",
                    "status",
                    "issue_type",
                    "current_value",
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
    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return parse_json_text(output_text)

    parts: list[str] = []
    for output in response.get("output", []):
        if not isinstance(output, dict):
            continue
        for content in output.get("content", []):
            if not isinstance(content, dict):
                continue
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return parse_json_text("\n".join(parts))


def parse_json_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        parsed = json.loads(stripped[start : end + 1])
    return parsed if isinstance(parsed, dict) else {"items": []}


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
                WHEN '용어 제안' THEN 1
                WHEN '번역 검수' THEN 2
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

    status = str(raw.get("status") or "").strip()
    issue_type = str(raw.get("issue_type") or "").strip()
    difference = normalize_review_text(str(raw.get("difference") or ""))
    detail = normalize_review_text(str(raw.get("detail") or ""))
    if not re.search(r"[가-힣]", detail):
        return True
    if status != "정상" and not re.search(r"[가-힣]", difference):
        return True
    current = normalize_review_text(item.current_value)
    if (
        (item.candidate_kind == "blue_text" or item.requested_review_type == TRANSLATION_CHECK_TYPE)
        and re.search(r"[가-힣]", current)
        and expected == current
        and not re.search(r"[A-Za-z]{3,}", current)
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
) -> tuple[int, int, int]:
    """Resolve only exact, authoritative glossary matches without an API call."""

    items = load_linguistic_review_items(connection, run_id)
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
    entries = item.glossary_matches or []
    authoritative = [
        entry
        for entry in entries
        if str(entry.get("status") or "") in {"approved", "official_verified", "official_name_only"}
    ]
    if not authoritative:
        return None

    korean = normalize_review_text(item.korean_text)
    english = normalize_review_text(item.english_text)
    current = normalize_review_text(item.current_value)
    source_normalized = normalize_source(korean) if korean else ""
    target_normalized = normalize_target(english) if english else ""

    for entry in authoritative:
        entry_source = normalize_source(str(entry.get("source") or ""))
        aliases = [normalize_source(str(alias)) for alias in entry.get("aliases", []) if alias]
        source_matches = bool(source_normalized) and source_normalized in {entry_source, *aliases}
        entry_target = normalize_review_text(str(entry.get("target") or ""))
        target_matches = bool(target_normalized and entry_target) and target_normalized == normalize_target(entry_target)
        status = str(entry.get("status") or "")
        source_title = normalize_review_text(str(entry.get("source_title") or "공식 번역 사전"))

        if item.requested_review_type in {SPELLING_CHECK_TYPE, TERMINOLOGY_CHECK_TYPE}:
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
            return {
                "status": "확인 필요",
                "issue_type": TRANSLATION_CHECK_TYPE,
                "current_value": current,
                "expected_value": entry_target,
                "difference": "공식 사전 영문명 적용",
                "detail": f"{source_title}에 수록된 국문·영문 대응을 적용해 영문안을 제시했습니다.",
            }
        if not korean and target_matches:
            return normal_glossary_decision(item, source_title)
    return None


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
) -> tuple[int, int, int]:
    items = load_linguistic_review_items(connection, run_id)
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


def normalize_decision(raw: dict[str, Any], item: SourceReviewItem) -> dict[str, str]:
    status = str(raw.get("status") or "확인 필요").strip()
    if status not in {"정상", "확인 필요", "오류 의심"}:
        status = "확인 필요"

    raw_issue_type = str(raw.get("issue_type") or "번역 검수").strip()
    issue_type = raw_issue_type
    if issue_type not in LINGUISTIC_CHECK_TYPES:
        issue_type = "번역 검수"

    if item.requested_review_type in LINGUISTIC_CHECK_TYPES:
        issue_type = item.requested_review_type

    expected = normalize_review_text(str(raw.get("expected_value") or ""))
    if is_contextual_source_url_parenthesis(item):
        status = "정상"
        issue_type = "오탈자 검수"
        expected = item.current_value
        raw = {
            **raw,
            "difference": "출처 URL 괄호 표기 정상",
            "detail": "출처 전체 문맥에서 URL은 기관명 뒤 괄호 안에 정상적으로 표기되어 있습니다.",
        }
    elif item.candidate_kind == "number_format" and item.candidate_expected not in {"", "LLM 문맥 확인"}:
        expected = item.candidate_expected
        if normalize_review_text(item.current_value) != expected:
            status = "오류 의심"
            issue_type = "오탈자 검수"
    elif status == "오류 의심" and issue_type in {TRANSLATION_CHECK_TYPE, TERMINOLOGY_CHECK_TYPE}:
        status = "확인 필요"
    elif status == "오류 의심":
        if item.candidate_kind == "blue_text" and looks_like_translation_mismatch(item.current_value, expected):
            status = "확인 필요"
            issue_type = TRANSLATION_CHECK_TYPE
        else:
            issue_type = SPELLING_CHECK_TYPE

    if status == "정상" and not expected:
        expected = item.current_value

    difference = remove_human_confirmation_language(
        normalize_review_text(str(raw.get("difference") or ""))
    )
    detail = remove_human_confirmation_language(
        normalize_review_text(
            str(raw.get("detail") or "LLM이 원문의 번역, 철자와 용어를 검토했습니다.")
        )
    )
    if item.source_rule_id == BLUE_REVIEW_RULE_ID:
        reviewed_category = issue_type
        issue_type = BLUE_REVIEW_TYPE
        if raw_issue_type == BLUE_REVIEW_TYPE:
            pass
        elif difference:
            difference = f"{reviewed_category}: {difference}"
        else:
            difference = f"{reviewed_category} 결과"

    return {
        "status": status,
        "issue_type": issue_type,
        "rule_id": rule_id_for(issue_type),
        "current_value": normalize_review_text(str(raw.get("current_value") or item.current_value)),
        "expected_value": expected,
        "difference": difference,
        "detail": detail,
    }


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
        return BLUE_REVIEW_RULE_ID
    if issue_type == "오탈자 검수":
        return LLM_SPELLING_RULE_ID
    if issue_type == "용어 제안":
        return LLM_TERMINOLOGY_RULE_ID
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
    if issue_type == TERMINOLOGY_CHECK_TYPE:
        return "LLM 국문·영문 용어 제안"
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
            ):
                group.pop(field, None)
            group["review_requests"] = []
            grouped[key] = group
        group["review_requests"].append(
            {
                "id": item.issue_id,
                "requested_review_type": item.requested_review_type,
                "candidate_kind": item.candidate_kind,
                "candidate_reason": item.candidate_reason,
                "candidate_expected": item.candidate_expected,
            }
        )
    return list(grouped.values())


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


def interleave_linguistic_items(items: list[SourceReviewItem]) -> list[SourceReviewItem]:
    groups = {
        check_type: [item for item in items if item.requested_review_type == check_type]
        for check_type in (SPELLING_CHECK_TYPE, TERMINOLOGY_CHECK_TYPE, TRANSLATION_CHECK_TYPE)
    }
    interleaved: list[SourceReviewItem] = []
    max_length = max((len(group) for group in groups.values()), default=0)
    for index in range(max_length):
        for check_type in (SPELLING_CHECK_TYPE, TERMINOLOGY_CHECK_TYPE, TRANSLATION_CHECK_TYPE):
            group = groups[check_type]
            if index < len(group):
                interleaved.append(group[index])
    interleaved.extend(
        item
        for item in items
        if item.requested_review_type not in {
            SPELLING_CHECK_TYPE,
            TERMINOLOGY_CHECK_TYPE,
            TRANSLATION_CHECK_TYPE,
        }
    )
    return interleaved


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run GPT translation/spelling/format review for HWPX source candidates.")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="SQLite database path")
    parser.add_argument("--run-id", type=int, default=None, help="Validation run id")
    parser.add_argument("--report-id", type=int, default=None, help="Use latest run for this report id")
    parser.add_argument("--limit", type=int, default=None, help="Review only the first N blue candidates")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = llm_review_settings()
    limit = args.limit if args.limit is not None else settings["limit"]
    result = append_llm_translation_reviews(
        args.db,
        run_id=args.run_id,
        report_id=args.report_id,
        limit=limit,
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
