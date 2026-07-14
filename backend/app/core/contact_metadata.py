from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


PHONE_RE = re.compile(r"(?<!\d)(?:0\d{1,2})[-)]?\s*\d{3,4}-\d{4}(?!\d)")
ROLE_WORDS = (
    "주무관",
    "사무관",
    "서기관",
    "연구사",
    "전문관",
    "과장",
    "팀장",
    "센터장",
    "담당자",
    "경위",
    "소방경",
    "소방위",
    "행정실무원",
)
CONTACT_RE = re.compile(
    rf"(?P<department>[가-힣A-Za-z0-9()·･\s]{{1,80}}?)\s+"
    rf"(?P<role>{'|'.join(ROLE_WORDS)})\s+"
    r"(?P<name>[가-힣]{2,6})"
)


@dataclass(frozen=True)
class ContactMetadata:
    department: str
    officer: str
    extension: str
    source_reference: str


def parse_contact_metadata(raw_source: str) -> ContactMetadata:
    source = clean_source_text(raw_source)
    departments: list[str] = []
    officers: list[str] = []
    matched_spans: list[tuple[int, int]] = []

    for match in CONTACT_RE.finditer(source):
        department = clean_department(match.group("department"))
        if department and department not in departments:
            departments.append(department)
        officer = f"{match.group('role').strip()} {match.group('name').strip()}"
        if officer and officer not in officers:
            officers.append(officer)
        matched_spans.append(match.span())

    phones = unique(PHONE_RE.findall(source))
    source_reference = source_reference_from(source, matched_spans)
    return ContactMetadata(
        department=" / ".join(departments),
        officer=" / ".join(officers),
        extension=" / ".join(normalize_phone(phone) for phone in phones),
        source_reference=source_reference,
    )


def clean_source_text(value: str) -> str:
    cleaned = value.replace("\\*", "*").replace("\\_", "_")
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)\)?", r"\1", cleaned)
    cleaned = cleaned.replace("\\", "")
    cleaned = cleaned.replace("＊", "*")
    cleaned = re.sub(r"^\s*[*※ㆍ·-]\s*", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(" *")


def clean_department(value: str) -> str:
    cleaned = re.split(r"\s+[|/]\s+|[,;]", value)[-1]
    cleaned = re.sub(r"^\s*[*※ㆍ·-]\s*", "", cleaned)
    cleaned = cleaned.strip(" *·･-/")
    return re.sub(r"\s+", " ", cleaned)


def normalize_phone(value: str) -> str:
    return re.sub(r"\s+", "", value).replace(")", "-")


def source_reference_from(source: str, contact_spans: list[tuple[int, int]]) -> str:
    parts = re.split(r"\s+/\s+", source, maxsplit=1)
    if len(parts) == 2 and parts[1].strip():
        return clean_source_text(parts[1])

    without_contact = remove_spans(source, contact_spans)
    without_contact = PHONE_RE.sub("", without_contact)
    return without_contact.strip(" *()/,-") or ""


def remove_spans(value: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return value
    result: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        result.append(value[cursor:start])
        cursor = end
    result.append(value[cursor:])
    return "".join(result)


def unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))
