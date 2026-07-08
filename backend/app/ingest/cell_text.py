from __future__ import annotations

import re


YEAR_FOOTNOTE_CAPTURE_RE = re.compile(r"^(\d{4})\s*(\d+\))$")
LEADING_ZERO_THOUSANDS_RE = re.compile(r"^([+-]?)0,(\d{3})(\.\d+)?$")


def normalize_cell_text(value: str) -> str:
    text, _ = split_cell_text(value)
    return text


def split_cell_text(value: str) -> tuple[str, str]:
    cleaned = value.strip()
    year_footnote = YEAR_FOOTNOTE_CAPTURE_RE.fullmatch(cleaned)
    if year_footnote:
        return year_footnote.group(1), year_footnote.group(2)
    return normalize_numeric_display_text(cleaned), ""


def normalize_numeric_display_text(value: str) -> str:
    match = LEADING_ZERO_THOUSANDS_RE.fullmatch(value)
    if not match:
        return value
    sign, digits, decimal_part = match.groups()
    normalized_digits = str(int(digits))
    return f"{sign}{normalized_digits}{decimal_part or ''}"
