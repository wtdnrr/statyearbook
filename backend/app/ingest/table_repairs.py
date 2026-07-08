from __future__ import annotations

import re


DASH_VALUES = {"-", "－", "―"}
NUMBER_LIKE_RE = re.compile(r"^[-+]?(?:0,)?\d{1,3}(?:,\d{3})*(?:\.\d+)?$")


def repair_region_split_rows(matrix: list[list[str]], footnote_matrix: list[list[str]]) -> None:
    if not looks_like_upper_lower_region_split_table(matrix):
        return

    for row_index, row in enumerate(matrix):
        if len(row) < 8 or not is_special_upper_region_label(row[0]):
            continue
        if not (
            is_number_like(row[2])
            and is_number_like(row[3])
            and is_dash(row[4])
            and is_dash(row[5])
            and is_dash(row[6])
            and is_dash(row[7])
        ):
            continue

        row[4] = row[2]
        row[5] = row[3]
        if row_index < len(footnote_matrix) and len(footnote_matrix[row_index]) >= 8:
            footnote_matrix[row_index][4] = footnote_matrix[row_index][2]
            footnote_matrix[row_index][5] = footnote_matrix[row_index][3]


def looks_like_upper_lower_region_split_table(matrix: list[list[str]]) -> bool:
    header_text = normalize_text(" ".join(" ".join(row) for row in matrix[:2]))
    return (
        "총계" in header_text
        and "조례" in header_text
        and "규칙" in header_text
        and ("시도" in header_text or "metropolitancityprovince" in header_text)
        and ("시군자치구" in header_text or "citycountydistrict" in header_text)
    )


def is_special_upper_region_label(value: str) -> bool:
    normalized = normalize_text(value)
    return "세종" in normalized or "sejong" in normalized or "제주" in normalized or "jeju" in normalized


def is_dash(value: str) -> bool:
    return value.strip() in DASH_VALUES


def is_number_like(value: str) -> bool:
    return NUMBER_LIKE_RE.fullmatch(value.strip()) is not None


def normalize_text(value: str) -> str:
    return re.sub(r"[\s･·.,/&]+", "", value).lower()
