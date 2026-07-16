from __future__ import annotations

from dataclasses import dataclass
from functools import cache
import json
from pathlib import Path
import re
import sqlite3

from app.validation.linguistic_policy import BASE_TRANSLATIONS


HANGUL_RE = re.compile(r"[가-힣]")
LATIN_RE = re.compile(r"[A-Za-z]")
LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")


@dataclass(frozen=True)
class GlossaryEntry:
    id: int
    source_text: str
    target_text: str
    category: str
    subcategory: str
    source_kind: str
    status: str
    priority: int
    source_report_id: int | None
    source_year: int | None
    occurrence_count: int
    evidence: str
    source_url: str
    source_title: str
    aliases: tuple[str, ...]
    valid_from: str
    valid_to: str
    verified_at: str

    def to_prompt_dict(self) -> dict[str, object]:
        return {
            "source": self.source_text,
            "target": self.target_text,
            "category": self.category,
            "subcategory": self.subcategory,
            "status": self.status,
            "source_kind": self.source_kind,
            "source_year": self.source_year,
            "occurrences": self.occurrence_count,
            "evidence": self.evidence,
            "source_url": self.source_url,
            "source_title": self.source_title,
            "aliases": list(self.aliases),
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "verified_at": self.verified_at,
        }


OFFICIAL_GLOSSARY_PATH = Path(__file__).resolve().parents[1] / "data" / "official_translation_glossary.json"
OFFICIAL_ENTITY_NAMES_PATH = Path(__file__).resolve().parents[1] / "data" / "official_entity_names.json"


STATISTICAL_STANDARD_TERMS = {
    "합계",
    "계",
    "총계",
    "소계",
    "누계",
    "누적",
    "비율",
    "구성비",
    "점유율",
    "증감",
    "증감액",
    "증감률",
    "전년대비",
    "전월대비",
    "평균",
    "중앙값",
    "최댓값",
    "최솟값",
}


REGION_TRANSLATIONS: tuple[tuple[str, str], ...] = (
    ("서울", "Seoul"),
    ("서울특별시", "Seoul Special City"),
    ("부산", "Busan"),
    ("부산광역시", "Busan Metropolitan City"),
    ("대구", "Daegu"),
    ("대구광역시", "Daegu Metropolitan City"),
    ("인천", "Incheon"),
    ("인천광역시", "Incheon Metropolitan City"),
    ("광주", "Gwangju"),
    ("광주광역시", "Gwangju Metropolitan City"),
    ("대전", "Daejeon"),
    ("대전광역시", "Daejeon Metropolitan City"),
    ("울산", "Ulsan"),
    ("울산광역시", "Ulsan Metropolitan City"),
    ("세종", "Sejong"),
    ("세종특별자치시", "Sejong Special Self-Governing City"),
    ("경기", "Gyeonggi"),
    ("경기도", "Gyeonggi-do"),
    ("강원", "Gangwon"),
    ("강원특별자치도", "Gangwon Special Self-Governing Province"),
    ("충북", "Chungbuk"),
    ("충청북도", "Chungcheongbuk-do"),
    ("충남", "Chungnam"),
    ("충청남도", "Chungcheongnam-do"),
    ("전북", "Jeonbuk"),
    ("전북특별자치도", "Jeonbuk Special Self-Governing Province"),
    ("전남", "Jeonnam"),
    ("전라남도", "Jeollanam-do"),
    ("경북", "Gyeongbuk"),
    ("경상북도", "Gyeongsangbuk-do"),
    ("경남", "Gyeongnam"),
    ("경상남도", "Gyeongsangnam-do"),
    ("제주", "Jeju"),
    ("제주특별자치도", "Jeju Special Self-Governing Province"),
)


def normalize_source(value: str) -> str:
    return re.sub(r"[\s·･,._\-–—()\[\]{}'\"/]+", "", value).lower()


def normalize_target(value: str) -> str:
    value = re.sub(r"(?<=[A-Za-z])(?:\s+-\s*|-\s+)(?=[A-Za-z])", "", value)
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def clean_glossary_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\\~", "~")).strip(" :;|/\n\t")


def extract_bilingual_pair(value: str) -> tuple[str, str] | None:
    """Extract only an unambiguous Korean-then-English pair from one field."""

    cleaned = clean_glossary_text(value)
    if not HANGUL_RE.search(cleaned) or not LATIN_RE.search(cleaned):
        return None

    latin_match = LATIN_WORD_RE.search(cleaned)
    if latin_match is None or latin_match.start() == 0:
        return None
    source = cleaned[: latin_match.start()].strip(" :;|/\n\t")
    target = cleaned[latin_match.start() :].strip(" :;|/\n\t")
    if not source or not target or HANGUL_RE.search(target):
        return None
    if len(source) > 180 or len(target) > 300:
        return None
    return source, target


def glossary_lookup_source(source_text: str) -> str:
    without_formula = re.sub(r"\([^)]*[A-Za-z][A-Za-z0-9+\-*/= ]*\)", "", source_text)
    return clean_glossary_text(without_formula) or clean_glossary_text(source_text)


def infer_category(source: str, target: str) -> str:
    compact_source = re.sub(r"\s+", "", source)
    target_lower = target.lower()
    if compact_source in STATISTICAL_STANDARD_TERMS:
        return "통계 표준어"
    if compact_source in {item[0] for item in REGION_TRANSLATIONS}:
        return "행정구역명"
    if not compact_source.endswith(("지구", "기구", "연구")) and re.search(
        r"(?:특별시|광역시|특별자치시|특별자치도|도|시|군|구|읍|면|동|리)$",
        compact_source,
    ) and any(
        token in target_lower for token in ("city", "county", "district", "province", "-do")
    ):
        return "행정구역명"
    if re.search(r"(?:부|처|청|위원회|공사|공단|연구원|진흥원|협회|재단)$", compact_source):
        return "기관명"
    if any(token in target_lower for token in ("ministry", "commission", "agency", "institute", "corporation")):
        return "기관명"
    if re.search(r"(?:대회|축제|박람회|행사|기념일)$", compact_source):
        return "행사·사업명"
    return "일반 용어"


def infer_subcategory(source: str, target: str, category: str | None = None) -> str:
    resolved_category = category or infer_category(source, target)
    compact_source = re.sub(r"[\s▪▫]+", "", source)
    target_lower = target.lower()

    if resolved_category == "행정구역명":
        if compact_source.endswith(("특별시", "광역시", "특별자치시", "특별자치도", "도")):
            return "시도"
        if compact_source.endswith(("시", "군", "구")):
            return "시군구"
        if compact_source.endswith(("읍", "면", "동", "리")) or any(
            token in target_lower for token in ("-eup", "-myeon", "-dong", "-ri")
        ):
            return "읍면동"
        return "행정구역 일반"

    if resolved_category == "기관명":
        if normalize_source(source) in official_public_institution_names():
            return "공공기관"
        if compact_source.endswith("위원회"):
            return "위원회"
        if compact_source.endswith(("부", "처")):
            return "중앙부처"
        if compact_source.endswith(("공사", "공단", "재단", "연구원", "진흥원", "협회", "병원")):
            return "공공기관"
        if compact_source.endswith(("청", "원", "본부", "실")):
            return "산하기관"
        return "기관 일반"

    if resolved_category == "행사·사업명":
        for suffix, label in (
            ("박람회", "박람회"),
            ("기념일", "기념일"),
            ("추모행사", "기념행사"),
            ("행사", "행사"),
            ("사업", "정책사업"),
            ("제도", "제도명"),
        ):
            if compact_source.endswith(suffix):
                return label
        return "행사·사업 일반"

    if resolved_category == "통계 표준어":
        for keyword, label in (
            ("합계", "합계"),
            ("총계", "합계"),
            ("소계", "합계"),
            ("계", "합계"),
            ("누", "누계"),
            ("증감률", "증감률"),
            ("대비", "증감률"),
            ("증감", "증감"),
            ("비율", "비율"),
            ("구성비", "비율"),
            ("점유율", "비율"),
            ("평균", "평균"),
            ("중앙값", "대표값"),
        ):
            if keyword in compact_source:
                return label
        return "통계 일반"

    return "표 항목" if compact_source in {"구분", "연도", "단위", "지역"} else "일반"


@cache
def official_public_institution_names() -> frozenset[str]:
    if not OFFICIAL_ENTITY_NAMES_PATH.exists():
        return frozenset()
    payload = json.loads(OFFICIAL_ENTITY_NAMES_PATH.read_text(encoding="utf-8"))
    names: set[str] = set()
    for item in payload.get("entities", []):
        if not isinstance(item, dict):
            continue
        for value in (item.get("name", ""), *item.get("aliases", [])):
            normalized = normalize_source(str(value))
            if normalized:
                names.add(normalized)
    return frozenset(names)


def refresh_translation_glossary(connection: sqlite3.Connection) -> int:
    """Rebuild reusable glossary evidence from curated terms and all imported yearbooks."""

    connection.execute(
        "DELETE FROM translation_glossary WHERE source_kind IN ('yearbook', 'curated', 'seed', 'official')"
    )
    changed = seed_base_glossary(connection)
    changed += seed_official_glossary(connection)
    changed += seed_official_entity_names(connection)
    report_rows = connection.execute("SELECT id, year FROM annual_reports ORDER BY year, id").fetchall()
    for report in report_rows:
        report_id = int(report["id"])
        pairs: dict[tuple[str, str, str], dict[str, object]] = {}
        table_rows = connection.execute(
            """
            SELECT id, code, title, title_en
            FROM stat_tables
            WHERE report_id = ?
            ORDER BY table_order, id
            """,
            (report_id,),
        ).fetchall()
        for table in table_rows:
            if table["title"] and table["title_en"]:
                collect_pair(
                    pairs,
                    str(table["title"]),
                    str(table["title_en"]),
                    table_id=int(table["id"]),
                    evidence=f"{report['year']} 연보 {table['code']} 표 제목",
                )

        cell_rows = connection.execute(
            """
            SELECT c.table_id, st.code, c.text_value
            FROM stat_table_cells c
            JOIN stat_tables st ON st.id = c.table_id
            WHERE st.report_id = ?
              AND trim(c.text_value) <> ''
            """,
            (report_id,),
        ).fetchall()
        for cell in cell_rows:
            pair = extract_bilingual_pair(str(cell["text_value"]))
            if pair is None:
                continue
            collect_pair(
                pairs,
                pair[0],
                pair[1],
                table_id=int(cell["table_id"]),
                evidence=f"{report['year']} 연보 {cell['code']} 표 내부 병기",
            )

        for (source, target, category), payload in pairs.items():
            source_normalized = normalize_source(source)
            target_normalized = normalize_target(target)
            if not source_normalized or not target_normalized:
                continue
            origin_key = f"report:{report_id}:{source_normalized}:{target_normalized}"
            subcategory = infer_subcategory(source, target, category)
            reference_source = reference_source_for(category, subcategory)
            connection.execute(
                """
                INSERT INTO translation_glossary (
                    origin_key, source_text, source_normalized, target_text,
                    target_normalized, category, subcategory, source_kind, source_report_id,
                    source_table_id, status, priority, occurrence_count, evidence,
                    source_url, source_title, verified_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'yearbook', ?, ?, 'reference', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(origin_key) DO UPDATE SET
                    source_text = excluded.source_text,
                    target_text = excluded.target_text,
                    category = excluded.category,
                    subcategory = excluded.subcategory,
                    source_table_id = excluded.source_table_id,
                    priority = excluded.priority,
                    occurrence_count = excluded.occurrence_count,
                    evidence = excluded.evidence,
                    source_url = excluded.source_url,
                    source_title = excluded.source_title,
                    verified_at = excluded.verified_at,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    origin_key,
                    source,
                    source_normalized,
                    target,
                    target_normalized,
                    category,
                    subcategory,
                    report_id,
                    payload["table_id"],
                    int(report["year"]),
                    payload["count"],
                    payload["evidence"],
                    str(reference_source.get("url", "")),
                    str(reference_source.get("title", "")),
                    str(reference_source.get("verified_at", "")),
                ),
            )
            changed += 1
    return changed


def collect_pair(
    pairs: dict[tuple[str, str, str], dict[str, object]],
    source: str,
    target: str,
    *,
    table_id: int,
    evidence: str,
) -> None:
    source_clean = clean_glossary_text(source)
    target_clean = clean_glossary_text(target)
    if not source_clean or not target_clean or not HANGUL_RE.search(source_clean) or not LATIN_RE.search(target_clean):
        return
    category = infer_category(source_clean, target_clean)
    key = (source_clean, target_clean, category)
    current = pairs.setdefault(key, {"table_id": table_id, "count": 0, "evidence": evidence})
    current["count"] = int(current["count"]) + 1


def seed_base_glossary(connection: sqlite3.Connection) -> int:
    """Seed low-trust project hints; these never bypass an LLM review."""

    curated = [
        (item["source"], item["expected"], infer_category(item["source"], item["expected"]), "프로젝트 기본 참고 용어")
        for item in BASE_TRANSLATIONS
    ]
    curated.extend((source, target, "행정구역명", "연보 작성용 지역명 참고값") for source, target in REGION_TRANSLATIONS)
    changed = 0
    for source, target, category, evidence in curated:
        source_normalized = normalize_source(source)
        target_normalized = normalize_target(target)
        origin_key = f"seed:{source_normalized}:{target_normalized}"
        connection.execute(
            """
            INSERT INTO translation_glossary (
                origin_key, source_text, source_normalized, target_text,
                target_normalized, category, subcategory, source_kind, status, priority,
                occurrence_count, evidence
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'seed', 'seed', 100, 1, ?)
            ON CONFLICT(origin_key) DO UPDATE SET
                source_text = excluded.source_text,
                target_text = excluded.target_text,
                category = excluded.category,
                subcategory = excluded.subcategory,
                status = 'seed',
                priority = 100,
                evidence = excluded.evidence,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                origin_key,
                source,
                source_normalized,
                target,
                target_normalized,
                category,
                infer_subcategory(source, target, category),
                evidence,
            ),
        )
        changed += 1
    return changed


def seed_official_glossary(connection: sqlite3.Connection) -> int:
    """Load source-traceable terms collected from official public websites."""

    payload = official_glossary_payload()
    sources = payload.get("sources", {})
    entries = payload.get("entries", [])
    changed = 0
    for item in entries:
        if not isinstance(item, dict):
            continue
        source = clean_glossary_text(str(item.get("source", "")))
        target = clean_glossary_text(str(item.get("target", "")))
        if not source or not target:
            continue
        source_meta = sources.get(str(item.get("source_id", "")), {})
        aliases = tuple(
            clean_glossary_text(str(alias))
            for alias in item.get("aliases", [])
            if clean_glossary_text(str(alias))
        )
        source_normalized = normalize_source(source)
        target_normalized = normalize_target(target)
        origin_key = f"official:{source_normalized}:{target_normalized}"
        cursor = connection.execute(
            """
            INSERT INTO translation_glossary (
                origin_key, source_text, source_normalized, target_text,
                target_normalized, category, subcategory, source_kind, status,
                priority, occurrence_count, evidence, source_url, source_title,
                aliases_json, valid_from, valid_to, verified_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'official', ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(origin_key) DO UPDATE SET
                source_text = excluded.source_text,
                target_text = excluded.target_text,
                category = excluded.category,
                subcategory = excluded.subcategory,
                status = excluded.status,
                priority = excluded.priority,
                evidence = excluded.evidence,
                source_url = excluded.source_url,
                source_title = excluded.source_title,
                aliases_json = excluded.aliases_json,
                valid_from = excluded.valid_from,
                valid_to = excluded.valid_to,
                verified_at = excluded.verified_at,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                origin_key,
                source,
                source_normalized,
                target,
                target_normalized,
                str(item.get("category", "일반 용어")),
                str(item.get("subcategory", "")),
                str(item.get("status", "reference")),
                {
                    "approved": 30000,
                    "official_verified": 20000,
                    "official_name_only": 10000,
                }.get(str(item.get("status", "reference")), 5000),
                f"공식 웹 자료 검색 수집: {source_meta.get('title', '')}",
                str(source_meta.get("url", "")),
                str(source_meta.get("title", "")),
                json.dumps(aliases, ensure_ascii=False),
                str(item.get("valid_from", "")),
                str(item.get("valid_to", "")),
                str(source_meta.get("verified_at", "")),
            ),
        )
        row = connection.execute(
            "SELECT id FROM translation_glossary WHERE origin_key = ?",
            (origin_key,),
        ).fetchone()
        if row is not None:
            glossary_id = int(row["id"])
            for alias in aliases:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO translation_glossary_aliases (
                        glossary_id, alias_text, alias_normalized, alias_kind
                    ) VALUES (?, ?, ?, 'official_alias')
                    """,
                    (glossary_id, alias, normalize_source(alias)),
                )
        changed += 1 if cursor.rowcount else 0
    return changed


@cache
def official_glossary_payload() -> dict[str, object]:
    return json.loads(OFFICIAL_GLOSSARY_PATH.read_text(encoding="utf-8"))


def reference_source_for(category: str, subcategory: str) -> dict[str, object]:
    sources = official_glossary_payload().get("sources", {})
    if not isinstance(sources, dict):
        return {}
    if category == "행정구역명" and subcategory in {"시도", "시군구"}:
        source = sources.get("ngii_admin_regions", {})
        return source if isinstance(source, dict) else {}
    if category == "행정구역명" and subcategory == "읍면동":
        source = sources.get("public_term_database", {})
        return source if isinstance(source, dict) else {}
    return {}


def seed_official_entity_names(connection: sqlite3.Connection) -> int:
    """Load official Korean entity names even when no official English name is published."""

    if not OFFICIAL_ENTITY_NAMES_PATH.exists():
        return 0
    payload = json.loads(OFFICIAL_ENTITY_NAMES_PATH.read_text(encoding="utf-8"))
    source_meta = payload.get("source", {})
    changed = 0
    for item in payload.get("entities", []):
        if not isinstance(item, dict):
            continue
        source = clean_glossary_text(str(item.get("name", "")))
        if not source:
            continue
        aliases = tuple(
            clean_glossary_text(str(alias))
            for alias in item.get("aliases", [])
            if clean_glossary_text(str(alias))
        )
        source_normalized = normalize_source(source)
        origin_key = f"official-name:{source_normalized}"
        evidence_parts = [
            "ALIO 2026년 공공기관 지정현황에서 국문 기관명 확인",
            f"주무부처: {item.get('supervising_ministry', '')}",
            f"기관유형: {item.get('institution_type', '')}",
            f"기관 홈페이지: {item.get('homepage', '')}",
        ]
        cursor = connection.execute(
            """
            INSERT INTO translation_glossary (
                origin_key, source_text, source_normalized, target_text,
                target_normalized, category, subcategory, source_kind, status,
                priority, occurrence_count, evidence, source_url, source_title,
                aliases_json, verified_at
            )
            VALUES (?, ?, ?, '', '', '기관명', '공공기관', 'official',
                    'official_name_only', 10000, 1, ?, ?, ?, ?, ?)
            ON CONFLICT(origin_key) DO UPDATE SET
                source_text = excluded.source_text,
                category = '기관명',
                subcategory = '공공기관',
                status = 'official_name_only',
                priority = 10000,
                evidence = excluded.evidence,
                source_url = excluded.source_url,
                source_title = excluded.source_title,
                aliases_json = excluded.aliases_json,
                verified_at = excluded.verified_at,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                origin_key,
                source,
                source_normalized,
                "; ".join(evidence_parts),
                str(source_meta.get("url", "")),
                str(source_meta.get("title", "")),
                json.dumps(aliases, ensure_ascii=False),
                str(source_meta.get("verified_at", "")),
            ),
        )
        row = connection.execute(
            "SELECT id FROM translation_glossary WHERE origin_key = ?",
            (origin_key,),
        ).fetchone()
        if row is not None:
            glossary_id = int(row["id"])
            for alias in aliases:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO translation_glossary_aliases (
                        glossary_id, alias_text, alias_normalized, alias_kind
                    ) VALUES (?, ?, ?, 'official_alias')
                    """,
                    (glossary_id, alias, normalize_source(alias)),
                )
        changed += 1 if cursor.rowcount else 0
    return changed


def glossary_entries_for_source(
    connection: sqlite3.Connection,
    source_text: str,
    *,
    current_report_id: int | None = None,
    limit: int = 8,
) -> list[GlossaryEntry]:
    normalized = normalize_source(source_text)
    if not normalized:
        return []
    rows = connection.execute(
        """
        SELECT DISTINCT tg.*, ar.year AS source_year
        FROM translation_glossary tg
        LEFT JOIN annual_reports ar ON ar.id = tg.source_report_id
        LEFT JOIN translation_glossary_aliases tga ON tga.glossary_id = tg.id
        WHERE (tg.source_normalized = ? OR tga.alias_normalized = ?)
          AND (tg.source_report_id IS NULL OR tg.source_report_id <> ?)
        ORDER BY
            CASE tg.status
                WHEN 'approved' THEN 0
                WHEN 'official_verified' THEN 1
                WHEN 'official_name_only' THEN 2
                WHEN 'llm_reviewed' THEN 3
                WHEN 'reference' THEN 4
                ELSE 5
            END,
            tg.priority DESC,
            tg.occurrence_count DESC,
            tg.id DESC
        LIMIT ?
        """,
        (normalized, normalized, current_report_id or -1, limit),
    ).fetchall()
    return [row_to_glossary_entry(row) for row in rows]


def glossary_entries_for_text(
    connection: sqlite3.Connection,
    value: str,
    *,
    current_report_id: int | None = None,
    limit: int = 16,
) -> list[GlossaryEntry]:
    """Return exact and contained official/reference terms as LLM evidence."""

    pair = extract_bilingual_pair(value)
    lookup_value = pair[0] if pair else value
    exact = glossary_entries_for_source(
        connection,
        glossary_lookup_source(lookup_value),
        current_report_id=current_report_id,
        limit=limit,
    )
    if len(exact) >= limit:
        return exact

    normalized_source = normalize_source(value)
    normalized_target = normalize_target(value)
    if not normalized_source and not normalized_target:
        return exact
    rows = connection.execute(
        """
        SELECT DISTINCT tg.*, ar.year AS source_year
        FROM translation_glossary tg
        LEFT JOIN annual_reports ar ON ar.id = tg.source_report_id
        LEFT JOIN translation_glossary_aliases tga ON tga.glossary_id = tg.id
        WHERE (tg.source_report_id IS NULL OR tg.source_report_id <> ?)
          AND (
              (length(tg.source_normalized) >= 2 AND instr(?, tg.source_normalized) > 0)
              OR (length(tga.alias_normalized) >= 2 AND instr(?, tga.alias_normalized) > 0)
              OR (length(tg.target_normalized) >= 4 AND instr(?, tg.target_normalized) > 0)
          )
        ORDER BY
            CASE tg.status
                WHEN 'approved' THEN 0
                WHEN 'official_verified' THEN 1
                WHEN 'official_name_only' THEN 2
                WHEN 'llm_reviewed' THEN 3
                WHEN 'reference' THEN 4
                ELSE 5
            END,
            tg.priority DESC,
            length(tg.source_normalized) DESC,
            tg.occurrence_count DESC,
            tg.id DESC
        LIMIT ?
        """,
        (
            current_report_id or -1,
            normalized_source,
            normalized_source,
            normalized_target,
            limit,
        ),
    ).fetchall()
    by_id = {entry.id: entry for entry in exact}
    for row in rows:
        entry = row_to_glossary_entry(row)
        by_id.setdefault(entry.id, entry)
        if len(by_id) >= limit:
            break
    return list(by_id.values())


def preferred_glossary_entry(entries: list[GlossaryEntry]) -> GlossaryEntry | None:
    """Return only a glossary entry strong enough for deterministic replacement."""

    return next(
        (
            entry
            for entry in entries
            if entry.status in {"approved", "official_verified"} and entry.target_text
        ),
        None,
    )


def glossary_context_json(entries: list[GlossaryEntry]) -> str:
    return json.dumps([entry.to_prompt_dict() for entry in entries], ensure_ascii=False)


def parse_glossary_context(value: str) -> list[dict[str, object]]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


def row_to_glossary_entry(row: sqlite3.Row) -> GlossaryEntry:
    return GlossaryEntry(
        id=int(row["id"]),
        source_text=str(row["source_text"]),
        target_text=str(row["target_text"]),
        category=str(row["category"]),
        subcategory=str(row["subcategory"]),
        source_kind=str(row["source_kind"]),
        status=str(row["status"]),
        priority=int(row["priority"]),
        source_report_id=int(row["source_report_id"]) if row["source_report_id"] is not None else None,
        source_year=int(row["source_year"]) if row["source_year"] is not None else None,
        occurrence_count=int(row["occurrence_count"]),
        evidence=str(row["evidence"]),
        source_url=str(row["source_url"]),
        source_title=str(row["source_title"]),
        aliases=tuple(json.loads(str(row["aliases_json"] or "[]"))),
        valid_from=str(row["valid_from"]),
        valid_to=str(row["valid_to"]),
        verified_at=str(row["verified_at"]),
    )
