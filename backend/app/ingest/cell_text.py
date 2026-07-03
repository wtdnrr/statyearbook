from __future__ import annotations

import re


YEAR_FOOTNOTE_CAPTURE_RE = re.compile(r"^(\d{4})\s*(\d+\))$")


def normalize_cell_text(value: str) -> str:
    text, _ = split_cell_text(value)
    return text


def split_cell_text(value: str) -> tuple[str, str]:
    cleaned = value.strip()
    year_footnote = YEAR_FOOTNOTE_CAPTURE_RE.fullmatch(cleaned)
    if year_footnote:
        return year_footnote.group(1), year_footnote.group(2)
    return cleaned, ""
