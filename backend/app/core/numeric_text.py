from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


PLACEHOLDER_TOKENS = {"", "-", "－", "―", "–", "—", "_", "＿"}
INTEGER_OR_DECIMAL_RE = re.compile(
    r"^[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?%?$"
)
PARENTHESIZED_NUMBER_RE = re.compile(
    r"^\([+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?%?\)$"
)
MALFORMED_DIGIT_SEPARATOR_RE = re.compile(r"(?<=\d)[ㅡㆍ·'’`´](?=\d)")
REPEATED_SEPARATOR_RE = re.compile(r"(?<=\d)[,.]{2,}(?=\d)")
DOT_GROUPED_INTEGER_RE = re.compile(r"^[+-]?\d{1,3}(?:\.\d{3})+$")
COMMA_GROUPED_INTEGER_RE = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})+$")


@dataclass(frozen=True)
class NumericTextAnomaly:
    reason: str
    suggested_value: str = ""


def normalized_numeric_token(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).replace("−", "-")
    return re.sub(r"\s+", "", normalized).strip()


def is_placeholder(value: str) -> bool:
    return normalized_numeric_token(value) in PLACEHOLDER_TOKENS


def parse_numeric_value(value: str) -> float | None:
    """Parse only unambiguous yearbook numeric notation.

    Thousands commas must use three-digit groups. Suspicious text such as
    ``3ㅡ456`` or ``12,34`` is deliberately left unparsed so it cannot silently
    enter sum and ratio calculations.
    """

    cleaned = normalized_numeric_token(value)
    if cleaned in PLACEHOLDER_TOKENS:
        return None
    if PARENTHESIZED_NUMBER_RE.fullmatch(cleaned):
        cleaned = cleaned[1:-1]
    if not INTEGER_OR_DECIMAL_RE.fullmatch(cleaned):
        return None
    return float(cleaned.removesuffix("%").replace(",", ""))


def numeric_text_anomaly(
    value: str,
    *,
    unit: str = "",
    peer_values: list[str] | None = None,
) -> NumericTextAnomaly | None:
    cleaned = normalized_numeric_token(value)
    if cleaned in PLACEHOLDER_TOKENS:
        return None

    peers = [normalized_numeric_token(peer) for peer in (peer_values or [])]
    if (
        _is_date_token(cleaned)
        or re.fullmatch(r"[’']\d{2}", cleaned)
        or _is_compound_numeric_token(cleaned)
        or not _has_numeric_cell_shape(cleaned)
    ):
        return None

    if MALFORMED_DIGIT_SEPARATOR_RE.search(cleaned):
        suggested = MALFORMED_DIGIT_SEPARATOR_RE.sub(",", cleaned)
        return NumericTextAnomaly("숫자 사이에 천 단위 구분자로 보기 어려운 문자가 있습니다.", suggested)

    if REPEATED_SEPARATOR_RE.search(cleaned):
        return NumericTextAnomaly("숫자 사이의 구분기호가 중복되었습니다.")

    unmatched_numeric_parenthesis = re.fullmatch(
        r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?%?\)",
        cleaned,
    )
    if unmatched_numeric_parenthesis:
        if _has_numeric_peer_context(peers):
            return NumericTextAnomaly("숫자 뒤에 대응하는 여는 괄호가 없는 닫는 괄호가 있습니다.", cleaned[:-1])
        return None

    if "," in cleaned and not _valid_comma_number(cleaned):
        return NumericTextAnomaly("쉼표가 세 자리 천 단위 묶음과 맞지 않습니다.")

    if _dot_grouping_is_suspicious(cleaned, unit=unit, peers=peers):
        return NumericTextAnomaly(
            "정수형 통계 열에서 마침표가 소수점이 아니라 천 단위 구분자로 잘못 입력되었을 가능성이 있습니다.",
            cleaned.replace(".", ","),
        )

    if _looks_numeric_but_is_invalid(cleaned):
        return NumericTextAnomaly("숫자로 보이지만 계산 가능한 숫자 표기 형식이 아닙니다.")
    return None


def _valid_comma_number(value: str) -> bool:
    candidate = value
    if candidate.startswith("(") and candidate.endswith(")"):
        candidate = candidate[1:-1]
    return bool(INTEGER_OR_DECIMAL_RE.fullmatch(candidate))


def _dot_grouping_is_suspicious(value: str, *, unit: str, peers: list[str]) -> bool:
    candidate = value.removesuffix("%")
    if not DOT_GROUPED_INTEGER_RE.fullmatch(candidate):
        return False
    if "%" in value or "%" in unit:
        return False

    compact_unit = re.sub(r"[\s(),]", "", unit).lower()
    count_or_amount_unit = any(
        token in compact_unit
        for token in (
            "명",
            "개",
            "건",
            "회",
            "원",
            "가구",
            "세대",
            "대",
            "곳",
            "개소",
            "krw",
        )
    )
    comma_peer_count = sum(1 for peer in peers if COMMA_GROUPED_INTEGER_RE.fullmatch(peer.removesuffix("%")))
    integer_peer_count = sum(
        1
        for peer in peers
        if re.fullmatch(r"[+-]?\d+", peer.removesuffix("%"))
        or COMMA_GROUPED_INTEGER_RE.fullmatch(peer.removesuffix("%"))
    )
    return count_or_amount_unit or comma_peer_count >= 1 or integer_peer_count >= 3


def _has_numeric_peer_context(peers: list[str]) -> bool:
    return sum(parse_numeric_value(peer) is not None for peer in peers) >= 2


def _looks_numeric_but_is_invalid(value: str) -> bool:
    if parse_numeric_value(value) is not None:
        return False
    if not re.search(r"\d", value):
        return False
    if re.search(r"[가-힣A-Za-z]", value):
        return False
    if _is_date_token(value):
        return False
    return bool(re.fullmatch(r"[+\-()\d\s,.%'’`´ㅡㆍ·]+", value))


def _has_numeric_cell_shape(value: str) -> bool:
    return bool(re.fullmatch(r"[+\-()\d\s,.%'’`´ㅡㆍ·]+", value))


def _is_date_token(value: str) -> bool:
    candidate = value.strip()
    return bool(
        re.fullmatch(
            r"[’']?\d{1,4}[./~-]\d{1,2}(?:[./~-]\d{1,2})?[.]?",
            candidate,
        )
    )


def _is_compound_numeric_token(value: str) -> bool:
    number = r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?"
    return bool(re.fullmatch(rf"{number}\({number}%?\)%?", value))
