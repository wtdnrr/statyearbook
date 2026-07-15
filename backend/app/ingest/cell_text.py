from __future__ import annotations

from collections.abc import Iterable
import re


YEAR_FOOTNOTE_CAPTURE_RE = re.compile(r"^(\d{4})\s*(\d+\))$")
LEADING_ZERO_THOUSANDS_RE = re.compile(r"^([+-]?)0,(\d{3})(\.\d+)?$")
NUMERIC_BODY_RE = re.compile(r"^[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?%?$")
FOOTNOTE_DEFINITION_RE = re.compile(r"(?:^|\s)#?\s*주\s*(\d+)\)")
NON_SEMANTIC_QUALIFIER_LINE_RE = re.compile(r"^\[\s*Type\s+[A-Za-z]+\s*\]$")


def normalize_cell_text(value: str) -> str:
    text, _ = split_cell_text(value)
    return text


def footnote_markers_from_texts(values: Iterable[str]) -> set[str]:
    return {
        f"{match.group(1)})"
        for value in values
        for match in FOOTNOTE_DEFINITION_RE.finditer(value)
    }


def split_cell_text(
    value: str,
    footnote_markers: Iterable[str] = (),
) -> tuple[str, str]:
    cleaned = remove_nonsemantic_qualifier_lines(value.strip())
    for marker in sorted(set(footnote_markers), key=len, reverse=True):
        if not marker or not cleaned.endswith(marker):
            continue
        numeric_text = cleaned[: -len(marker)].strip()
        if NUMERIC_BODY_RE.fullmatch(numeric_text):
            return normalize_numeric_display_text(numeric_text), marker

    year_footnote = YEAR_FOOTNOTE_CAPTURE_RE.fullmatch(cleaned)
    if year_footnote:
        return year_footnote.group(1), year_footnote.group(2)
    return normalize_numeric_display_text(cleaned), ""


def remove_nonsemantic_qualifier_lines(value: str) -> str:
    lines = [
        line
        for line in value.splitlines()
        if not NON_SEMANTIC_QUALIFIER_LINE_RE.fullmatch(line.strip())
    ]
    return "\n".join(lines).strip()


def normalize_numeric_display_text(value: str) -> str:
    match = LEADING_ZERO_THOUSANDS_RE.fullmatch(value)
    if not match:
        return value
    sign, digits, decimal_part = match.groups()
    normalized_digits = str(int(digits))
    return f"{sign}{normalized_digits}{decimal_part or ''}"
