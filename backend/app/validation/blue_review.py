from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import sqlite3
import unicodedata
import xml.etree.ElementTree as ET
import zipfile

from app.db.schema import connect, init_db
from app.validation.linguistic_policy import (
    SPELLING_CHECK_TYPE,
    TERMINOLOGY_CHECK_TYPE,
    TRANSLATION_CHECK_TYPE,
)


BLUE_REVIEW_COLORS = {"#0000ff", "0000ff"}
BLUE_REVIEW_RULE_ID = "source.blue_text_review"
BLUE_REVIEW_TYPE = "파란색 표기 확인"
BLUE_REVIEW_PREVIEW_RULE_ID = "source.blue_text_review.preview"
TABLE_CODE_RE = re.compile(r"(?<![\d-])(?:[1-9]|1[0-9])-\d{1,2}-\d{1,2}(?:-\d{1,2})?(?![-\d])")
APPENDIX_CODE_RE = re.compile(r"부록\s*(\d+)(?:\s*-\s*(\d+))?")
BLUE_ENGLISH_MARKER_RE = re.compile(r"^[\s(\[]*영문[\s)\]]*$")
BLUE_ENGLISH_MARKER_ANY_RE = re.compile(r"[\s(\[]*영문[\s)\]]*")


@dataclass(frozen=True)
class BlueReviewCandidate:
    text: str
    table_code: str
    cell_text: str = ""
    row_text: str = ""
    source_row_index: int | None = None
    source_col_index: int | None = None
    context_kind: str = "cell"


def append_blue_text_review_checks(
    db_path: Path,
    *,
    report_id: int,
    run_id: int,
    source_path: str | Path,
) -> int:
    hwpx_path = resolve_hwpx_source(Path(source_path))
    if hwpx_path is None:
        return 0

    candidates = extract_blue_review_candidates(hwpx_path)
    if not candidates:
        return 0

    with connect(db_path) as connection:
        init_db(connection)
        with connection:
            existing = existing_review_values(connection, run_id)
            inserted = append_cell_reviews(connection, report_id, run_id, candidates, existing)
            inserted += append_table_field_reviews(connection, report_id, run_id, candidates, existing)
            inserted += append_unmatched_reviews(connection, report_id, run_id, candidates, existing)

    return inserted


def materialize_blue_review_preview_placeholders(db_path: Path, *, run_id: int) -> int:
    """Create one-time UI preview issues without changing the normal review pipeline."""

    with connect(db_path) as connection:
        init_db(connection)
        candidates = connection.execute(
            """
            SELECT table_id, location, row_index, col_index, current_value
            FROM linguistic_review_candidates
            WHERE run_id = ? AND candidate_kind LIKE 'blue_text%'
            GROUP BY table_id, location, row_index, col_index, current_value
            ORDER BY table_id, row_index, col_index, location, current_value
            """,
            (run_id,),
        ).fetchall()
        inserted = 0
        with connection:
            for candidate in candidates:
                exists = connection.execute(
                    """
                    SELECT 1
                    FROM validation_issues
                    WHERE run_id = ? AND table_id = ? AND rule_id = ?
                      AND location = ? AND current_value = ?
                    LIMIT 1
                    """,
                    (
                        run_id,
                        candidate["table_id"],
                        BLUE_REVIEW_PREVIEW_RULE_ID,
                        candidate["location"],
                        candidate["current_value"],
                    ),
                ).fetchone()
                if exists:
                    continue

                values = (
                    run_id,
                    candidate["table_id"],
                    BLUE_REVIEW_PREVIEW_RULE_ID,
                    BLUE_REVIEW_TYPE,
                    candidate["location"],
                    candidate["row_index"],
                    candidate["col_index"],
                    candidate["current_value"],
                    "담당자 확인",
                    "HWPX 원문 파란색 표시",
                    "확인 필요",
                    "warning",
                    "화면 확인을 위해 HWPX 원문의 파란색 표시 위치를 임시 검수 결과로 표시했습니다.",
                )
                connection.execute(
                    """
                    INSERT INTO validation_issues (
                        run_id, table_id, rule_id, issue_type, location,
                        row_index, col_index, current_value, expected_value,
                        difference, status, severity, detail
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                connection.execute(
                    """
                    INSERT INTO validation_checks (
                        run_id, table_id, rule_id, check_type, check_label, location,
                        row_index, col_index, current_value, expected_value,
                        difference, status, severity, detail
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        candidate["table_id"],
                        BLUE_REVIEW_PREVIEW_RULE_ID,
                        BLUE_REVIEW_TYPE,
                        BLUE_REVIEW_TYPE,
                        candidate["location"],
                        candidate["row_index"],
                        candidate["col_index"],
                        candidate["current_value"],
                        "담당자 확인",
                        "HWPX 원문 파란색 표시",
                        "확인 필요",
                        "warning",
                        "화면 확인을 위해 HWPX 원문의 파란색 표시 위치를 임시 검수 결과로 표시했습니다.",
                    ),
                )
                inserted += 1

            issue_count = connection.execute(
                "SELECT COUNT(*) AS count FROM validation_issues WHERE run_id = ?",
                (run_id,),
            ).fetchone()["count"]
            connection.execute(
                "UPDATE validation_runs SET issue_count = ? WHERE id = ?",
                (issue_count, run_id),
            )
        return inserted


def resolve_hwpx_source(source_path: Path) -> Path | None:
    source_path = source_path.expanduser()
    if source_path.suffix.lower() == ".hwpx" and source_path.exists():
        return source_path

    direct_match = source_path.with_suffix(".hwpx")
    if direct_match.exists():
        return direct_match

    parent = source_path.parent
    if not parent.exists():
        return None

    source_stem = normalize_review_text(source_path.stem)
    for candidate in parent.iterdir():
        if candidate.suffix.lower() != ".hwpx":
            continue
        if normalize_review_text(candidate.stem) == source_stem:
            return candidate
    return None


def extract_blue_review_terms(hwpx_path: Path) -> list[str]:
    return list(dict.fromkeys(candidate.text for candidate in extract_blue_review_candidates(hwpx_path)))


def extract_blue_review_candidates(hwpx_path: Path) -> list[BlueReviewCandidate]:
    try:
        with zipfile.ZipFile(hwpx_path) as archive:
            blue_char_ids = blue_char_property_ids(archive)
            if not blue_char_ids:
                return []

            candidates: list[BlueReviewCandidate] = []
            seen: set[tuple[str, str, str, str, str, int | None, int | None]] = set()
            for name in sorted(archive.namelist()):
                if not name.startswith("Contents/section") or not name.endswith(".xml"):
                    continue
                table_depth = 0
                current_code = ""
                for event, element in ET.iterparse(archive.open(name), events=("start", "end")):
                    tag_name = local_name(element.tag)
                    if event == "start" and tag_name == "tbl":
                        table_depth += 1
                        continue

                    if event == "end" and tag_name == "tbl":
                        has_nested_table = any(
                            local_name(descendant.tag) == "tbl"
                            for descendant in element.iter()
                            if descendant is not element
                        )
                        if not has_nested_table:
                            table_text = normalize_review_text("".join(element.itertext()))
                            code = table_code_from_title_container(element, table_text)
                            if code:
                                current_code = code
                            append_blue_table_candidates(
                                candidates,
                                seen,
                                element,
                                blue_char_ids,
                                table_code=current_code,
                                context_kind="title" if code else "cell",
                            )
                        table_depth -= 1
                        element.clear()
                        continue

                    if event == "end" and tag_name == "p" and table_depth == 0:
                        append_blue_runs(
                            candidates,
                            seen,
                            element,
                            blue_char_ids,
                            table_code=current_code,
                            cell_text=normalize_review_text("".join(element.itertext())),
                            row_text="",
                            source_row_index=None,
                            source_col_index=None,
                            context_kind="paragraph",
                        )
                        element.clear()
            return candidates
    except (ET.ParseError, OSError, zipfile.BadZipFile):
        return []


def table_code_from_title_container(table: ET.Element, text: str) -> str:
    rows = [item for item in list(table) if local_name(item.tag) == "tr"]
    cells = [cell for row in rows for cell in list(row) if local_name(cell.tag) == "tc"]
    cell_matches: list[tuple[int, int, str]] = []
    for cell_index, cell in enumerate(cells):
        cell_text = normalize_review_text("".join(cell.itertext()))
        for pattern in (TABLE_CODE_RE, APPENDIX_CODE_RE):
            cell_matches.extend(
                (cell_index, match.start(), match.group(0))
                for match in pattern.finditer(cell_text)
            )
    if cell_matches:
        if len(rows) > 6 and len(cells) > 8:
            cell_matches = [item for item in cell_matches if item[0] <= 4 and item[1] <= 40]
        if cell_matches:
            return normalize_table_code(max(cell_matches, key=lambda item: (item[0], item[1]))[2])

    matches = [
        (match.start(), match.group(0))
        for pattern in (TABLE_CODE_RE, APPENDIX_CODE_RE)
        for match in pattern.finditer(text)
    ]
    if not matches:
        return ""
    if len(rows) > 6 and len(cells) > 8:
        matches = [item for item in matches if item[0] <= 160]
        if not matches:
            return ""
    _, code = max(matches, key=lambda item: item[0])
    return normalize_table_code(code)


def append_blue_table_candidates(
    target: list[BlueReviewCandidate],
    seen: set[tuple[str, str, str, str, str, int | None, int | None]],
    table: ET.Element,
    blue_char_ids: set[str],
    *,
    table_code: str,
    context_kind: str,
) -> None:
    rows = [element for element in list(table) if local_name(element.tag) == "tr"]
    for fallback_row_index, row in enumerate(rows):
        cells = [element for element in list(row) if local_name(element.tag) == "tc"]
        row_text = normalize_review_text(" | ".join("".join(cell.itertext()) for cell in cells))
        for fallback_col_index, cell in enumerate(cells):
            if any(
                local_name(descendant.tag) == "tbl"
                for descendant in cell.iter()
                if descendant is not cell
            ):
                continue
            cell_text = normalize_review_text("".join(cell.itertext()))
            source_row_index, source_col_index = source_cell_coordinates(
                cell,
                fallback_row_index=fallback_row_index,
                fallback_col_index=fallback_col_index,
            )
            append_blue_runs(
                target,
                seen,
                cell,
                blue_char_ids,
                table_code=table_code,
                cell_text=cell_text,
                row_text=row_text,
                source_row_index=source_row_index,
                source_col_index=source_col_index,
                context_kind=context_kind,
            )


def source_cell_coordinates(
    cell: ET.Element,
    *,
    fallback_row_index: int,
    fallback_col_index: int,
) -> tuple[int, int]:
    address = next(
        (element for element in cell.iter() if local_name(element.tag) == "cellAddr"),
        None,
    )
    if address is None:
        return fallback_row_index, fallback_col_index
    try:
        return int(address.attrib.get("rowAddr", fallback_row_index)), int(
            address.attrib.get("colAddr", fallback_col_index)
        )
    except ValueError:
        return fallback_row_index, fallback_col_index


def append_blue_runs(
    target: list[BlueReviewCandidate],
    seen: set[tuple[str, str, str, str, str, int | None, int | None]],
    container: ET.Element,
    blue_char_ids: set[str],
    *,
    table_code: str,
    cell_text: str,
    row_text: str,
    source_row_index: int | None,
    source_col_index: int | None,
    context_kind: str,
) -> None:
    if not table_code:
        return
    run_texts: list[str] = []
    for element in container.iter():
        if local_name(element.tag) != "run":
            continue
        if element.attrib.get("charPrIDRef") not in blue_char_ids:
            continue
        if any(local_name(descendant.tag) == "tbl" for descendant in element.iter() if descendant is not element):
            continue
        text = normalize_review_text("".join(element.itertext()))
        if not text:
            continue
        if text not in run_texts:
            run_texts.append(text)

    if not run_texts:
        return
    review_texts = blue_review_texts(run_texts, cell_text, table_code=table_code)
    for text in review_texts:
        key = (
            table_code,
            text,
            cell_text,
            row_text,
            context_kind,
            source_row_index,
            source_col_index,
        )
        if key in seen:
            continue
        seen.add(key)
        target.append(
            BlueReviewCandidate(
                text=text,
                table_code=table_code,
                cell_text=cell_text,
                row_text=row_text,
                source_row_index=source_row_index,
                source_col_index=source_col_index,
                context_kind=context_kind,
            )
        )


def blue_review_texts(run_texts: list[str], cell_text: str, *, table_code: str) -> list[str]:
    """Keep every textual blue mark, including an `(영문)` translation marker."""

    has_english_marker = any(BLUE_ENGLISH_MARKER_RE.fullmatch(text) for text in run_texts)
    substantive_runs = [
        text
        for text in run_texts
        if not BLUE_ENGLISH_MARKER_RE.fullmatch(text) and is_blue_review_candidate(text)
    ]
    review_texts: list[str] = []
    if substantive_runs:
        combined_text = normalize_review_text(" ".join(substantive_runs))
        if is_blue_review_candidate(combined_text):
            review_texts.append(combined_text)

    if has_english_marker:
        marker_source = BLUE_ENGLISH_MARKER_ANY_RE.sub("", normalize_review_text(cell_text)).strip()
        marker_source = TABLE_CODE_RE.sub("", marker_source, count=1)
        marker_source = APPENDIX_CODE_RE.sub("", marker_source, count=1).strip()
        if is_blue_review_candidate(marker_source) and marker_source not in review_texts:
            review_texts.append(marker_source)

    return review_texts


def blue_char_property_ids(archive: zipfile.ZipFile) -> set[str]:
    try:
        root = ET.fromstring(archive.read("Contents/header.xml"))
    except (KeyError, ET.ParseError):
        return set()

    blue_ids: set[str] = set()
    for element in root.iter():
        if local_name(element.tag) != "charPr":
            continue
        text_color = element.attrib.get("textColor", "").lower()
        if text_color in BLUE_REVIEW_COLORS and "id" in element.attrib:
            blue_ids.add(element.attrib["id"])
    return blue_ids


def append_cell_reviews(
    connection: sqlite3.Connection,
    report_id: int,
    run_id: int,
    candidates: list[BlueReviewCandidate],
    existing: set[tuple[int, int | None, int | None, str]],
) -> int:
    cells = connection.execute(
        """
        SELECT c.table_id, c.row_index, c.col_index, c.text_value, st.code
        FROM stat_table_cells c
        JOIN stat_tables st ON st.id = c.table_id
        WHERE c.table_id IN (
            SELECT id FROM stat_tables WHERE report_id = ?
        )
        ORDER BY c.table_id, c.row_index, c.col_index
        """,
        (report_id,),
    ).fetchall()

    inserted = 0
    seen_matches: set[tuple[int, int, int, str]] = set()
    matched_candidates: set[BlueReviewCandidate] = set()
    row_values: dict[tuple[int, int], list[str]] = {}
    for source_cell in cells:
        row_values.setdefault(
            (int(source_cell["table_id"]), int(source_cell["row_index"])),
            [],
        ).append(str(source_cell["text_value"] or ""))
    row_texts = {
        key: normalize_review_text(" | ".join(values))
        for key, values in row_values.items()
    }
    cell_value_counts: dict[tuple[str, str], int] = {}
    for source_cell in cells:
        key = (
            normalize_table_code(str(source_cell["code"])),
            compact_review_match_text(str(source_cell["text_value"] or "")),
        )
        cell_value_counts[key] = cell_value_counts.get(key, 0) + 1
    for cell in cells:
        normalized_cell = normalize_review_text(cell["text_value"])
        if not normalized_cell:
            continue
        for candidate in candidates:
            if candidate in matched_candidates:
                continue
            if not candidate_matches_table_code(candidate, cell["code"]):
                continue
            if candidate.context_kind == "paragraph":
                continue
            if (
                candidate.context_kind == "cell"
                and candidate.source_col_index is not None
                and int(cell["col_index"]) != candidate.source_col_index
            ):
                continue
            if not text_matches_candidate(normalized_cell, candidate.text):
                continue
            if candidate.cell_text and not cell_context_matches(normalized_cell, candidate.cell_text, candidate.text):
                continue
            if candidate.row_text:
                row_matches = row_context_matches(
                    row_texts.get((int(cell["table_id"]), int(cell["row_index"])), ""),
                    candidate.row_text,
                    candidate.cell_text,
                )
                unique_exact_cell = (
                    compact_review_match_text(normalized_cell)
                    == compact_review_match_text(candidate.cell_text)
                    and cell_value_counts.get(
                        (
                            normalize_table_code(str(cell["code"])),
                            compact_review_match_text(normalized_cell),
                        ),
                        0,
                    )
                    == 1
                )
                if not row_matches and not unique_exact_cell:
                    continue
            key = (cell["table_id"], cell["row_index"], cell["col_index"], candidate.text)
            if key in seen_matches:
                continue
            if has_existing_specific_review(
                existing,
                table_id=cell["table_id"],
                row_index=cell["row_index"],
                col_index=cell["col_index"],
                candidate=candidate.text,
            ):
                continue
            insert_blue_review(
                connection,
                run_id=run_id,
                table_id=cell["table_id"],
                location=f"{cell['row_index'] + 1}행 {cell['col_index'] + 1}열",
                row_index=cell["row_index"],
                col_index=cell["col_index"],
                current_value=candidate.text,
            )
            existing.add((cell["table_id"], cell["row_index"], cell["col_index"], candidate.text))
            seen_matches.add(key)
            matched_candidates.add(candidate)
            inserted += 1
    return inserted


def append_unmatched_reviews(
    connection: sqlite3.Connection,
    report_id: int,
    run_id: int,
    candidates: list[BlueReviewCandidate],
    existing: set[tuple[int, int | None, int | None, str]],
) -> int:
    """Retain a blue HWPX mark even when Markdown normalization changed its cell text."""

    tables = connection.execute(
        """
        SELECT id, code, title, title_en
        FROM stat_tables
        WHERE report_id = ?
        ORDER BY table_order, id
        """,
        (report_id,),
    ).fetchall()
    inserted = 0
    for candidate in candidates:
        matching_tables = [
            table
            for table in tables
            if candidate_matches_table_code(candidate, str(table["code"]))
        ]
        if not matching_tables:
            continue
        matching_table_ids = {int(table["id"]) for table in matching_tables}
        if any(
            table_id in matching_table_ids and value == normalize_review_text(candidate.text)
            for table_id, _, _, value in existing
        ):
            continue

        table = next(
            (
                item
                for item in matching_tables
                if text_matches_candidate(
                    normalize_review_text(f"{item['title']} {item['title_en']}"),
                    candidate.text,
                )
            ),
            matching_tables[0],
        )
        location = {
            "title": "표 제목",
            "paragraph": "HWPX 파란색 문단",
        }.get(candidate.context_kind, "HWPX 파란색 원문")
        insert_blue_review(
            connection,
            run_id=run_id,
            table_id=int(table["id"]),
            location=location,
            row_index=None,
            col_index=None,
            current_value=candidate.text,
        )
        existing.add((int(table["id"]), None, None, normalize_review_text(candidate.text)))
        inserted += 1
    return inserted


def append_table_field_reviews(
    connection: sqlite3.Connection,
    report_id: int,
    run_id: int,
    candidates: list[BlueReviewCandidate],
    existing: set[tuple[int, int | None, int | None, str]],
) -> int:
    tables = connection.execute(
        """
        SELECT id, code, title, title_en, section_title, section_title_en, note, source
        FROM stat_tables
        WHERE report_id = ?
        ORDER BY table_order, id
        """,
        (report_id,),
    ).fetchall()

    field_labels = {
        "title": "표 제목",
        "title_en": "영문 표 제목",
        "section_title": "상위 제목",
        "section_title_en": "영문 상위 제목",
        "note": "주석",
        "source": "출처",
    }
    inserted = 0
    seen_matches: set[tuple[int, str, str]] = set()
    existing_values = {value for _, _, _, value in existing}
    for table in tables:
        for field_name, location in field_labels.items():
            field_value = normalize_review_text(table[field_name])
            if not field_value:
                continue
            for candidate in candidates:
                normalized_candidate = normalize_review_text(candidate.text)
                if normalized_candidate in existing_values:
                    continue
                if not candidate_matches_table_code(candidate, table_code_for_row(table)):
                    continue
                if not text_matches_candidate(field_value, candidate.text):
                    continue
                key = (table["id"], location, candidate.text)
                if key in seen_matches:
                    continue
                if has_existing_table_review(
                    existing,
                    table_id=table["id"],
                    candidate=candidate.text,
                ):
                    continue
                insert_blue_review(
                    connection,
                    run_id=run_id,
                    table_id=table["id"],
                    location=location,
                    row_index=None,
                    col_index=None,
                    current_value=candidate.text,
                )
                existing.add((table["id"], None, None, candidate.text))
                existing_values.add(normalized_candidate)
                seen_matches.add(key)
                inserted += 1
    return inserted


def insert_blue_review(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    table_id: int,
    location: str,
    row_index: int | None,
    col_index: int | None,
    current_value: str,
) -> None:
    has_korean = bool(re.search(r"[가-힣]", current_value))
    has_english = bool(re.search(r"[A-Za-z]", current_value))
    if has_korean and has_english:
        review_types = (
            SPELLING_CHECK_TYPE,
            TERMINOLOGY_CHECK_TYPE,
            TRANSLATION_CHECK_TYPE,
        )
        candidate_kind = "blue_text_bilingual"
    elif has_korean:
        review_types = (TRANSLATION_CHECK_TYPE,)
        candidate_kind = "blue_text_korean_translation"
    elif has_english:
        review_types = (SPELLING_CHECK_TYPE, TERMINOLOGY_CHECK_TYPE)
        candidate_kind = "blue_text_english"
    else:
        review_types = (SPELLING_CHECK_TYPE,)
        candidate_kind = "blue_text_numeric_or_symbol"

    for review_type in review_types:
        connection.execute(
            """
            INSERT INTO linguistic_review_candidates (
                run_id, table_id, review_type, candidate_kind, location,
                row_index, col_index, current_value, reason, prompt_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '')
            ON CONFLICT(run_id, table_id, review_type, location, current_value)
            DO UPDATE SET
                candidate_kind = excluded.candidate_kind,
                reason = excluded.reason
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
                blue_review_reason(review_type, has_korean=has_korean, has_english=has_english),
            ),
        )


def blue_review_reason(review_type: str, *, has_korean: bool, has_english: bool) -> str:
    if review_type == TRANSLATION_CHECK_TYPE and has_korean and not has_english:
        return "HWPX 파란색 한글 원문을 문맥에 맞는 영어로 직접 번역하여 검수값으로 출력"
    if review_type == TRANSLATION_CHECK_TYPE:
        return "HWPX 파란색 한영 병기의 의미 대응과 공식 고유명사 번역을 검수"
    if review_type == SPELLING_CHECK_TYPE:
        return "HWPX 파란색 표기의 국문·영문 오탈자와 문자·기호 오류를 검수"
    return "HWPX 파란색 표기의 국문·영문 용어와 공식 표현을 검수"


def existing_review_values(connection: sqlite3.Connection, run_id: int) -> set[tuple[int, int | None, int | None, str]]:
    rows = connection.execute(
        """
        SELECT table_id, row_index, col_index, current_value
        FROM validation_issues
        WHERE run_id = ? AND rule_id = ?
        UNION ALL
        SELECT table_id, row_index, col_index, current_value
        FROM linguistic_review_candidates
        WHERE run_id = ? AND candidate_kind LIKE 'blue_text%'
        """,
        (run_id, BLUE_REVIEW_RULE_ID, run_id),
    ).fetchall()
    return {
        (
            row["table_id"],
            row["row_index"],
            row["col_index"],
            normalize_review_text(row["current_value"]),
        )
        for row in rows
    }


def has_existing_specific_review(
    existing: set[tuple[int, int | None, int | None, str]],
    *,
    table_id: int,
    row_index: int | None,
    col_index: int | None,
    candidate: str,
) -> bool:
    normalized_candidate = normalize_review_text(candidate)
    for existing_table_id, existing_row_index, existing_col_index, value in existing:
        if existing_table_id != table_id:
            continue
        if existing_row_index != row_index or existing_col_index != col_index:
            continue
        if normalized_candidate in value or value in normalized_candidate:
            return True
    return False


def has_existing_table_review(
    existing: set[tuple[int, int | None, int | None, str]],
    *,
    table_id: int,
    candidate: str,
) -> bool:
    normalized_candidate = normalize_review_text(candidate)
    return any(
        existing_table_id == table_id
        and (normalized_candidate in value or value in normalized_candidate)
        for existing_table_id, _, _, value in existing
    )


def text_matches_candidate(text: str, candidate: str) -> bool:
    normalized_candidate = normalize_review_text(candidate)
    if not normalized_candidate:
        return False
    if normalized_candidate in text:
        return True
    compact_candidate = compact_review_match_text(normalized_candidate)
    return bool(compact_candidate) and compact_candidate in compact_review_match_text(text)


def table_code_for_row(row: sqlite3.Row) -> str:
    return str(row["code"]) if "code" in row.keys() else ""


def candidate_matches_table_code(candidate: BlueReviewCandidate, table_code: str) -> bool:
    candidate_code = normalize_table_code(candidate.table_code)
    base_code = normalize_table_code(table_code)
    if not candidate_code:
        return False
    if candidate_code == base_code:
        return True
    return base_code.startswith(f"{candidate_code}-")


def cell_context_matches(cell_text: str, source_cell_text: str, candidate: str) -> bool:
    normalized_cell = compact_review_match_text(cell_text)
    normalized_source = compact_review_match_text(source_cell_text)
    normalized_candidate = compact_review_match_text(candidate)
    if not normalized_candidate or normalized_candidate not in normalized_cell:
        return False
    if normalized_source in normalized_cell or normalized_cell in normalized_source:
        return True
    return normalized_candidate == normalized_cell


def row_context_matches(row_text: str, source_row_text: str, source_cell_text: str) -> bool:
    normalized_row = compact_review_match_text(row_text)
    normalized_source = compact_review_match_text(source_row_text)
    if normalized_source in normalized_row or normalized_row in normalized_source:
        return True

    cell_tokens = set(review_context_tokens(source_cell_text))
    anchors = [
        token
        for token in review_context_tokens(source_row_text)
        if token not in cell_tokens and len(token) >= 2
    ]
    strong_anchors = [token for token in anchors if is_strong_row_context_anchor(token)]
    if strong_anchors:
        return any(
            compact_review_match_text(anchor) in normalized_row
            for anchor in strong_anchors
        )

    weak_anchors = [
        token
        for token in anchors
        if re.fullmatch(r"[가-힣]{2,}", token)
    ]
    numeric_anchors = [token for token in anchors if token.isdigit()]
    weak_matches = [
        compact_review_match_text(anchor) in normalized_row
        for anchor in weak_anchors
    ]
    if numeric_anchors:
        row_tokens = set(review_context_tokens(row_text))
        matched = sum(token in row_tokens for token in numeric_anchors)
        numeric_match = matched / len(numeric_anchors) >= 0.7
        return numeric_match and (not weak_matches or any(weak_matches))
    if weak_matches:
        return any(weak_matches)
    return True


def review_context_tokens(value: str) -> list[str]:
    return re.findall(r"[가-힣]{2,}|[A-Za-z]{3,}|\d{2,}", normalize_review_text(value))


def is_strong_row_context_anchor(token: str) -> bool:
    if re.fullmatch(r"[가-힣]{4,}", token):
        return True
    if not re.fullmatch(r"[A-Za-z]{5,}", token):
        return False
    return token.lower() not in {
        "article",
        "committee",
        "consultation",
        "administration",
        "government",
        "management",
        "establishment",
        "organization",
    }


def compact_review_match_text(value: str) -> str:
    normalized = normalize_review_text(value)
    normalized = re.sub(r"(?<=[A-Za-z])\s+-\s*(?=[A-Za-z])", "", normalized)
    normalized = normalized.replace("&", "and")
    return re.sub(r"[^0-9A-Za-z가-힣]", "", normalized).lower()


def is_blue_review_candidate(text: str) -> bool:
    if not text or len(text) > 1400:
        return False
    if text in {"-", "–", "—"} or BLUE_ENGLISH_MARKER_RE.fullmatch(text):
        return False
    meaningful_chars = re.sub(r"[^0-9가-힣A-Za-z]", "", text)
    if not meaningful_chars:
        return False
    return bool(meaningful_chars)


def normalize_table_code(value: str) -> str:
    normalized = normalize_review_text(value)
    normalized = re.sub(r"\s+표\s*\d+$", "", normalized).strip()
    appendix_match = APPENDIX_CODE_RE.fullmatch(normalized)
    if appendix_match:
        number, sub_number = appendix_match.groups()
        return f"부록 {number}-{sub_number}" if sub_number else f"부록 {number}"
    return normalized


def normalize_review_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value.replace("\xa0", " "))
    return re.sub(r"\s+", " ", normalized).strip()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
