from __future__ import annotations

from dataclasses import dataclass
import re


SPELLING_CHECK_TYPE = "오탈자 검수"
TERMINOLOGY_CHECK_TYPE = "용어 제안"
TRANSLATION_CHECK_TYPE = "번역 검수"
LINGUISTIC_CHECK_TYPES = {
    SPELLING_CHECK_TYPE,
    TERMINOLOGY_CHECK_TYPE,
    TRANSLATION_CHECK_TYPE,
}


@dataclass(frozen=True)
class LinguisticReviewPolicy:
    check_type: str
    failure_status: str
    severity: str
    purpose: str


LINGUISTIC_REVIEW_POLICIES = {
    SPELLING_CHECK_TYPE: LinguisticReviewPolicy(
        check_type=SPELLING_CHECK_TYPE,
        failure_status="오류 의심",
        severity="critical",
        purpose=(
            "국문·영문 철자 오류, 문자 깨짐, 잘못된 숫자 구분기호, "
            "같은 문맥의 연관 셀과 명확히 다른 표기를 찾습니다."
        ),
    ),
    TERMINOLOGY_CHECK_TYPE: LinguisticReviewPolicy(
        check_type=TERMINOLOGY_CHECK_TYPE,
        failure_status="확인 필요",
        severity="warning",
        purpose=(
            "뜻은 통하지만 사전적 의미, 공공 통계 문체 또는 공식 명칭에 비추어 "
            "더 적절한 국문·영문 표현을 제안합니다."
        ),
    ),
    TRANSLATION_CHECK_TYPE: LinguisticReviewPolicy(
        check_type=TRANSLATION_CHECK_TYPE,
        failure_status="확인 필요",
        severity="warning",
        purpose=(
            "한국어와 영어의 의미 대응을 확인합니다. 기관명·행사명·행정구역명은 "
            "공식 출처가 기록된 번역 사전과 동일 문맥 캐시를 우선 적용하고 나머지를 LLM으로 검토합니다."
        ),
    ),
}


SPELLING_REPLACEMENTS: tuple[dict[str, str], ...] = (
    {"current": "Claasification", "expected": "Classification", "language": "en", "reason": "영문 철자 오류"},
    {"current": "Claasifi-cation", "expected": "Classification", "language": "en", "reason": "영문 철자 오류"},
    {"current": "Ele7ction", "expected": "Election", "language": "en", "reason": "숫자 혼입"},
    {"current": "Nuber", "expected": "Number", "language": "en", "reason": "영문 철자 누락"},
    {"current": "기횎예산처", "expected": "기획예산처", "language": "ko", "reason": "국문 철자 오류"},
    {"current": "eryeong", "expected": "Uiryeong", "language": "en", "reason": "영문 지명 철자 누락"},
)


TERMINOLOGY_REPLACEMENTS: tuple[dict[str, str], ...] = (
    {"current": "잔액율", "expected": "잔액률", "language": "ko", "reason": "표준 국문 용어"},
    {
        "current": "Ministry of Gender Equality & family",
        "expected": "Ministry of Gender Equality and Family",
        "language": "en",
        "reason": "공식 기관명 표기",
    },
    {
        "current": "Ministry of Trade, Industry & Energy",
        "expected": "Ministry of Trade, Industry and Energy",
        "language": "en",
        "reason": "공식 기관명 표기",
    },
    {
        "current": "Nuclear safety and Security Commission",
        "expected": "Nuclear Safety and Security Commission",
        "language": "en",
        "reason": "영문 고유명사 표기",
    },
    {
        "current": "Personal Information Protection commission",
        "expected": "Personal Information Protection Commission",
        "language": "en",
        "reason": "영문 고유명사 표기",
    },
    {
        "current": "Military Manpower administration",
        "expected": "Military Manpower Administration",
        "language": "en",
        "reason": "영문 고유명사 표기",
    },
    {
        "current": "Anti-corruption and Civil Rights Commission",
        "expected": "Anti-corruption & Civil Rights Commission",
        "language": "en",
        "reason": "공식 기관명 표기",
    },
    {
        "current": "National Institute For Unification Education",
        "expected": "National Institute for Unification Education",
        "language": "en",
        "reason": "영문 제목식 표기",
    },
    {
        "current": "National Relief Fund",
        "expected": "Information on Livelihood Recovery Consumer Coupons",
        "language": "en",
        "reason": "통계 문맥에 맞는 명칭",
    },
    {"current": "Use Of Archives", "expected": "Use of Archives", "language": "en", "reason": "영문 제목식 표기"},
    {"current": "No. of Program", "expected": "No. of Programs", "language": "en", "reason": "영문 수 일치"},
    {"current": "No. of Completion", "expected": "No. of Completions", "language": "en", "reason": "영문 수 일치"},
    {"current": "Number of Completion", "expected": "Number of Completions", "language": "en", "reason": "영문 수 일치"},
    {
        "current": "Operational of Safety e-Report",
        "expected": "Operation of Safety e-Report",
        "language": "en",
        "reason": "통계 항목에 맞는 품사",
    },
    {"current": "Small business", "expected": "Small Business", "language": "en", "reason": "영문 제목식 표기"},
)


def needs_terminology_review(value: str) -> bool:
    """Only queue terminology review when a concrete suspect expression is present."""

    normalized = re.sub(r"\s+", " ", value).strip().casefold()
    return any(
        str(replacement["current"]).casefold() in normalized
        for replacement in TERMINOLOGY_REPLACEMENTS
    )


BASE_TRANSLATIONS: tuple[dict[str, str], ...] = (
    {"source": "구분", "expected": "Classification"},
    {"source": "합계", "expected": "Total"},
    {"source": "계", "expected": "Total"},
    {"source": "남성", "expected": "Male"},
    {"source": "여성", "expected": "Female"},
    {"source": "단위", "expected": "Unit"},
    {"source": "잔액률", "expected": "Balance Ratio"},
    {"source": "증감률", "expected": "Rate of Change"},
    {"source": "기후에너지환경부", "expected": "Ministry of Climate, Energy and Environment"},
    {"source": "국가데이터처", "expected": "Ministry of Data and Statistics"},
    {"source": "국가데이터연구원", "expected": "National Data Research Institute"},
    {"source": "방송미디어통신위원회", "expected": "Korea Media and Communications Commission"},
    {"source": "지식재산처", "expected": "Ministry of Intellectual Property"},
    {"source": "민생회복 소비쿠폰 안내", "expected": "Information on Livelihood Recovery Consumer Coupons"},
)
