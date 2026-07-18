from __future__ import annotations

from collections.abc import Iterable
import re


YEAR_FOOTNOTE_CAPTURE_RE = re.compile(r"^(\d{4})\s*(\d+\))$")
LEADING_ZERO_THOUSANDS_RE = re.compile(r"^([+-]?)0,(\d{3})(\.\d+)?$")
FOOTNOTE_DEFINITION_RE = re.compile(r"(?:^|\s)#?\s*주\s*(\d+)\)")


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
    cleaned = value.strip()
    markers = sorted(set(footnote_markers), key=len, reverse=True)
    lines = cleaned.splitlines()
    found_markers: list[str] = []
    for line_index, line in enumerate(lines):
        stripped_line = line.rstrip()
        if stripped_line.count("(") > 0 and stripped_line.count("(") == stripped_line.count(")"):
            continue
        for marker in markers:
            if not marker or not stripped_line.endswith(marker):
                continue
            body_text = stripped_line[: -len(marker)].rstrip()
            if not body_text:
                continue
            lines[line_index] = body_text
            if marker not in found_markers:
                found_markers.append(marker)
            break
    if found_markers:
        return normalize_numeric_display_text("\n".join(lines).strip()), " ".join(found_markers)

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
