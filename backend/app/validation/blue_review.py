from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import sqlite3
import unicodedata
import xml.etree.ElementTree as ET
import zipfile

from app.db.schema import connect, init_db


BLUE_REVIEW_COLORS = {"#0000ff", "0000ff"}
BLUE_REVIEW_RULE_ID = "source.blue_text_review"
BLUE_REVIEW_TYPE = "파란색 표기 확인"
TABLE_CODE_RE = re.compile(r"(?<!\d)(?:[1-9]|1[0-9])-\d{1,2}-\d{1,2}(?:-\d{1,2})?(?![-\d])")


@dataclass(frozen=True)
class BlueReviewCandidate:
    text: str
    table_code: str
    cell_text: str = ""
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
            if inserted:
                connection.execute(
                    "UPDATE validation_runs SET issue_count = issue_count + ? WHERE id = ?",
                    (inserted, run_id),
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
            seen: set[tuple[str, str, str, str]] = set()
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
                        if table_depth == 1:
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
                            context_kind="paragraph",
                        )
                        element.clear()
            return candidates
    except (ET.ParseError, OSError, zipfile.BadZipFile):
        return []


def table_code_from_title_container(table: ET.Element, text: str) -> str:
    matches = list(TABLE_CODE_RE.finditer(text))
    if not matches:
        return ""
    rows = [item for item in list(table) if local_name(item.tag) == "tr"]
    cells = [cell for row in rows for cell in list(row) if local_name(cell.tag) == "tc"]
    if len(rows) > 6 and len(cells) > 8:
        return ""
    return matches[-1].group(0)


def append_blue_table_candidates(
    target: list[BlueReviewCandidate],
    seen: set[tuple[str, str, str, str]],
    table: ET.Element,
    blue_char_ids: set[str],
    *,
    table_code: str,
    context_kind: str,
) -> None:
    cells = [element for element in table.iter() if local_name(element.tag) == "tc"]
    for cell in cells:
        cell_text = normalize_review_text("".join(cell.itertext()))
        append_blue_runs(
            target,
            seen,
            cell,
            blue_char_ids,
            table_code=table_code,
            cell_text=cell_text,
            context_kind=context_kind,
        )


def append_blue_runs(
    target: list[BlueReviewCandidate],
    seen: set[tuple[str, str, str, str]],
    container: ET.Element,
    blue_char_ids: set[str],
    *,
    table_code: str,
    cell_text: str,
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
        if not is_blue_review_candidate(text):
            continue
        if text not in run_texts:
            run_texts.append(text)

    if not run_texts:
        return
    combined_text = normalize_review_text(" ".join(run_texts))
    review_texts = [combined_text] if is_blue_review_candidate(combined_text) else run_texts
    for text in review_texts:
        key = (table_code, text, cell_text, context_kind)
        if key in seen:
            continue
        seen.add(key)
        target.append(
            BlueReviewCandidate(
                text=text,
                table_code=table_code,
                cell_text=cell_text,
                context_kind=context_kind,
            )
        )


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
    for cell in cells:
        normalized_cell = normalize_review_text(cell["text_value"])
        if not normalized_cell:
            continue
        for candidate in candidates:
            if not candidate_matches_table_code(candidate, cell["code"]):
                continue
            if candidate.context_kind == "paragraph":
                continue
            if not text_matches_candidate(normalized_cell, candidate.text):
                continue
            if candidate.cell_text and not cell_context_matches(normalized_cell, candidate.cell_text, candidate.text):
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
    for table in tables:
        for field_name, location in field_labels.items():
            field_value = normalize_review_text(table[field_name])
            if not field_value:
                continue
            for candidate in candidates:
                if not candidate_matches_table_code(candidate, table_code_for_row(table)):
                    continue
                if not text_matches_candidate(field_value, candidate.text):
                    continue
                key = (table["id"], location, candidate.text)
                if key in seen_matches:
                    continue
                if has_existing_specific_review(
                    existing,
                    table_id=table["id"],
                    row_index=None,
                    col_index=None,
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
    payload = (
        run_id,
        table_id,
        BLUE_REVIEW_RULE_ID,
        BLUE_REVIEW_TYPE,
        location,
        row_index,
        col_index,
        current_value,
        "담당자 확인",
        "HWPX 원문 파란색 표시",
        "확인 필요",
        "warning",
        "HWPX 원문에서 파란색으로 표시된 텍스트입니다. 영문 번역, 맞춤법, 신규/수정값 여부를 확인하세요.",
        None,
    )
    connection.execute(
        """
        INSERT INTO validation_issues (
            run_id, table_id, rule_id, issue_type, location, row_index,
            col_index, current_value, expected_value, difference, status,
            severity, detail, formula
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )
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
            table_id,
            BLUE_REVIEW_RULE_ID,
            BLUE_REVIEW_TYPE,
            "HWPX 파란색 표기 후보",
            location,
            row_index,
            col_index,
            current_value,
            "담당자 확인",
            "HWPX 원문 파란색 표시",
            "확인 필요",
            "warning",
            "HWPX 원문에서 파란색으로 표시된 텍스트입니다. 영문 번역, 맞춤법, 신규/수정값 여부를 확인하세요.",
            0.7,
        ),
    )


def existing_review_values(connection: sqlite3.Connection, run_id: int) -> set[tuple[int, int | None, int | None, str]]:
    rows = connection.execute(
        """
        SELECT table_id, row_index, col_index, current_value
        FROM validation_issues
        WHERE run_id = ?
        """,
        (run_id,),
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


def text_matches_candidate(text: str, candidate: str) -> bool:
    normalized_candidate = normalize_review_text(candidate)
    if len(normalized_candidate) < 2:
        return False
    return normalized_candidate in text


def table_code_for_row(row: sqlite3.Row) -> str:
    return str(row["code"]) if "code" in row.keys() else ""


def candidate_matches_table_code(candidate: BlueReviewCandidate, table_code: str) -> bool:
    base_code = re.sub(r"\s+표\d+$", "", table_code).strip()
    return bool(candidate.table_code) and candidate.table_code == base_code


def cell_context_matches(cell_text: str, source_cell_text: str, candidate: str) -> bool:
    normalized_cell = compact_review_match_text(cell_text)
    normalized_source = compact_review_match_text(source_cell_text)
    normalized_candidate = compact_review_match_text(candidate)
    if not normalized_candidate or normalized_candidate not in normalized_cell:
        return False
    if normalized_source in normalized_cell or normalized_cell in normalized_source:
        return True
    return normalized_candidate == normalized_cell


def compact_review_match_text(value: str) -> str:
    normalized = normalize_review_text(value)
    normalized = re.sub(r"(?<=[A-Za-z])\s+-\s*(?=[A-Za-z])", "", normalized)
    return re.sub(r"\s+", "", normalized).lower()


def is_blue_review_candidate(text: str) -> bool:
    if len(text) < 2 or len(text) > 180:
        return False
    if text in {"-", "–", "—", "(영문)"}:
        return False
    if re.fullmatch(r"[\d\s.,:~()/\\-]+", text):
        return False
    meaningful_chars = re.sub(r"[^가-힣A-Za-z]", "", text)
    if len(meaningful_chars) < 4:
        return False
    return bool(re.search(r"[가-힣A-Za-z]", text))


def normalize_review_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value.replace("\xa0", " "))
    return re.sub(r"\s+", " ", normalized).strip()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
