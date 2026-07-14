from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
import argparse
import json
import os
import re
import sqlite3
import time
from typing import Any
from urllib import error, request

from app.db.schema import DB_PATH, connect, init_db
from app.validation.blue_review import BLUE_REVIEW_RULE_ID, BLUE_REVIEW_TYPE, normalize_review_text
from app.validation.source_review import SOURCE_FORMAT_RULE_ID, SOURCE_FORMAT_TYPE


OPENAI_RESPONSES_PATH = "/responses"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TRANSLATION_MODEL = "gpt-4o-mini"
LLM_TRANSLATION_RULE_ID = "llm.translation_review"
LLM_SPELLING_RULE_ID = "llm.spelling_review"
LLM_TERMINOLOGY_RULE_ID = "llm.terminology_review"
SOURCE_REVIEW_RULE_IDS = {BLUE_REVIEW_RULE_ID, SOURCE_FORMAT_RULE_ID}


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
        }


def append_llm_translation_reviews(
    db_path: Path = DB_PATH,
    *,
    run_id: int | None = None,
    report_id: int | None = None,
    limit: int | None = None,
) -> LLMReviewResult:
    settings = llm_review_settings()
    if not settings["enabled"]:
        return LLMReviewResult(0, 0, 0, current_issue_count(db_path, run_id), "disabled")
    if not settings["api_key"]:
        return LLMReviewResult(0, 0, 0, current_issue_count(db_path, run_id), "missing OPENAI_API_KEY")

    with connect(db_path) as connection:
        init_db(connection)
        resolved_run_id = run_id or latest_run_id(connection, report_id=report_id)
        if resolved_run_id is None:
            return LLMReviewResult(0, 0, 0, 0, "no validation run")
        items = load_source_review_items(connection, resolved_run_id, limit=limit)

    if not items:
        return LLMReviewResult(0, 0, 0, current_issue_count(db_path, resolved_run_id), "no source review items")

    client = OpenAIResponsesClient(
        api_key=settings["api_key"],
        model=settings["model"],
        base_url=settings["base_url"],
        timeout=settings["timeout"],
    )

    inserted_issues = 0
    inserted_checks = 0
    processed = 0
    for batch in chunked(items, settings["batch_size"]):
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
                batch_counts = save_llm_review_decisions(connection, resolved_run_id, batch, decisions)
                inserted_issues += batch_counts[0]
                inserted_checks += batch_counts[1]
                processed += batch_counts[2]
                refresh_issue_count(connection, resolved_run_id)
        time.sleep(settings["sleep_seconds"])

    return LLMReviewResult(
        processed=processed,
        inserted_issues=inserted_issues,
        inserted_checks=inserted_checks,
        final_issue_count=current_issue_count(db_path, resolved_run_id),
    )


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
            "items": [item.to_prompt_dict() for item in items],
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


SYSTEM_PROMPT = """You are a meticulous Korean government statistical yearbook source-data reviewer.

Review HWPX blue-marked text and parser-detected numeric or punctuation candidates. Use the table title, row, column, unit, and surrounding rows as evidence.

For each item:
- If Korean and English are both present, judge whether the English faithfully translates the Korean in public-sector statistical style.
- If only Korean is present, provide a concise English translation and mark status as "확인 필요" with issue_type "번역 검수".
- Blue Korean-only text includes headers, notes, place lists, and feature names. It is not "정상" until expected_value contains the proposed English translation; never copy the Korean source unchanged.
- If only English is present, check spelling, capitalization, grammar, and official/public-sector terminology.
- For candidate_kind "number_format", determine whether comma, decimal point, apostrophe, Korean vowel, or dash characters are malformed numeric separators. Compare candidate_expected with the unit and peer values. A valid decimal must remain a decimal; do not change a value merely because it is unusual.
- For candidate_kind "punctuation_format", distinguish a genuine punctuation typo from a line-break marker, URL, date, official notation, or valid compound word.
- Treat normal compound-word hyphens such as "e-learning" as correct. Remove a hyphen only when the source clearly used it solely for a line break inside one word.
- Preserve statistical values. Recommend only an unambiguous formatting correction; otherwise use "확인 필요".
- Use "오탈자 검수" with status "오류 의심" only for clear spelling, casing, mojibake, or typo errors.
- Use "용어 제안" with status "확인 필요" for better terminology when the original is understandable but should be reviewed.
- Use "번역 검수" with status "확인 필요" for missing, mismatched, awkward, or unverifiable translations.
- Use status "정상" only when no action is needed.
- Be conservative with official organization names. If likely official English differs, mark it for review.
- Government organization names are proper names. Never translate Korean rank suffixes such as 부, 처, 청, 원, 위원회 mechanically into Ministry, Office, or Agency. If the existing English is a plausible established official name, retain it unless there is clear evidence of an error.
- Do not invent an alternative official organization name. When uncertain, retain the current English in expected_value and explain what the owner should verify.
- Do not perform or alter sum and ratio calculations in this review.
- expected_value must never be empty. For status "정상", copy the reviewed source or its existing English. For a missing/mismatched translation, put the actual replacement English text in expected_value.
- expected_value is replacement text only. Never put a Korean explanation such as "공식 번역입니다" or "확인이 필요합니다" there; put reasoning in detail.

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
For every Korean source in this retry batch, expected_value must be the actual standalone English replacement text. Do not copy Korean, leave the value blank, or write an explanation. If an official name is uncertain, provide a conservative literal public-sector English translation and state the uncertainty only in detail.
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
    )


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
    current = normalize_review_text(item.current_value)
    if (
        item.candidate_kind == "blue_text"
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


def save_llm_review_decisions(
    connection: sqlite3.Connection,
    run_id: int,
    batch: list[SourceReviewItem],
    decisions: list[dict[str, Any]],
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
        remove_blue_review_placeholder(connection, run_id, item)
        insert_llm_check(connection, run_id, item, decision)
        inserted_checks += 1
        processed += 1
        if decision["status"] != "정상":
            insert_llm_issue(connection, run_id, item, decision)
            inserted_issues += 1

    return inserted_issues, inserted_checks, processed


def normalize_decision(raw: dict[str, Any], item: SourceReviewItem) -> dict[str, str]:
    status = str(raw.get("status") or "확인 필요").strip()
    if status not in {"정상", "확인 필요", "오류 의심"}:
        status = "확인 필요"

    issue_type = str(raw.get("issue_type") or "번역 검수").strip()
    if issue_type not in {"번역 검수", "오탈자 검수", "용어 제안"}:
        issue_type = "번역 검수"

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
    elif status == "오류 의심" and issue_type in {"번역 검수", "용어 제안"}:
        status = "확인 필요"
    elif status == "오류 의심":
        if item.candidate_kind == "blue_text" and looks_like_translation_mismatch(item.current_value, expected):
            status = "확인 필요"
            issue_type = "번역 검수"
        else:
            issue_type = "오탈자 검수"

    if status == "정상" and not expected:
        expected = item.current_value

    return {
        "status": status,
        "issue_type": issue_type,
        "rule_id": rule_id_for(issue_type),
        "current_value": normalize_review_text(str(raw.get("current_value") or item.current_value)),
        "expected_value": expected,
        "difference": normalize_review_text(str(raw.get("difference") or "")),
        "detail": normalize_review_text(str(raw.get("detail") or "LLM이 원문 후보의 번역, 철자 및 숫자 표기를 검토했습니다.")),
    }


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
    if issue_type == "오탈자 검수":
        return LLM_SPELLING_RULE_ID
    if issue_type == "용어 제안":
        return LLM_TERMINOLOGY_RULE_ID
    return LLM_TRANSLATION_RULE_ID


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
            "LLM 원문 번역·오탈자 검수",
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


def chunked(items: list[SourceReviewItem], size: int) -> list[list[SourceReviewItem]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


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
