from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import cache
from pathlib import Path
import re


REGION_DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "korean_admin_regions.csv"
REGION_DATA_SOURCE = (
    "KOSIS 행정구역 코드 참조표를 정리한 tidycensuskr "
    "inst/extdata/lookup_district_code.csv (CRAN 0.2.8)"
)

PROVINCE_SHORT_NAMES = {
    "서울": "Seoul",
    "부산": "Busan",
    "대구": "Daegu",
    "인천": "Incheon",
    "광주": "Gwangju",
    "대전": "Daejeon",
    "울산": "Ulsan",
    "세종": "Sejong",
    "경기": "Gyeonggi",
    "강원": "Gangwon",
    "충북": "Chungbuk",
    "충남": "Chungnam",
    "전북": "Jeonbuk",
    "전남": "Jeonnam",
    "경북": "Gyeongbuk",
    "경남": "Gyeongnam",
    "제주": "Jeju",
}

OFFICIAL_PROVINCE_NAMES = {
    "서울특별시": "Seoul",
    "부산광역시": "Busan Metropolitan City",
    "대구광역시": "Daegu Metropolitan City",
    "인천광역시": "Incheon Metropolitan City",
    "광주광역시": "Gwangju Metropolitan City",
    "대전광역시": "Daejeon Metropolitan City",
    "울산광역시": "Ulsan Metropolitan City",
    "세종특별자치시": "Sejong Special Self-Governing City",
    "경기도": "Gyeonggi-do",
    "강원특별자치도": "Gangwon State",
    "충청북도": "Chungcheongbuk-do",
    "충청남도": "Chungcheongnam-do",
    "전북특별자치도": "Jeonbuk State",
    "전라남도": "Jeollanam-do",
    "경상북도": "Gyeongsangbuk-do",
    "경상남도": "Gyeongsangnam-do",
    "제주특별자치도": "Jeju Special Self-Governing Province",
}

# The source draft contains several long administrative-area lists whose omitted
# delimiters and one invalid suffix cannot be reconstructed safely by tokenizing.
# Keep their normalized, reviewed forms in the same dictionary path so they never
# fall through to an LLM.
REGION_VALUE_OVERRIDES = {
    "ㅇ인천 서구 : 654,358": (
        "ㅇ인천 서구: 654,358 / Incheon Seo-gu: 654,358",
        "지역명 사전 영문 표기 적용",
    ),
    "ㅇ서초 반포본동 : 216": (
        "ㅇ서초 반포본동: 216 / Seocho-gu Banpobon-dong: 216",
        "지역명 사전 영문 표기 적용",
    ),
    "경기 가평군, 충남 서산시·예산시, 전남 담양군, 경남 산청군·합천군": (
        "경기 가평군, 충남 서산시·예산군, 전남 담양군, 경남 산청군·합천군 / "
        "Gyeonggi Gapyeong-gun; Chungnam Seosan-si and Yesan-gun; "
        "Jeonnam Damyang-gun; Gyeongnam Sancheong-gun and Hapcheon-gun",
        "예산시를 예산군으로 교정하고 지역명 사전 영문 표기 적용",
    ),
    (
        "광주 북구, 경기 포천시, 충남 천안시·공주시·아산시·당진시·부여군·청양군·홍성군, "
        "전남 나주시·함평군, 경북 청도군, 경남 진주시·의령군·하동군·함양군 광주 광산구 "
        "어룡동·삼도동, 세종 전동면, 충북 청주시 옥산면·오창읍, 충남 서천군 판교면·비인면, "
        "전남 광양시 다압면, 구례군 간전면·토지면, 화순군 이서면, 영광군 군남면·염산면, "
        "신안군 지도읍·임자면·자은면·흑산면, 경남 밀양시 무안면, 거창군 남상면·신원면"
    ): (
        "광주 북구, 경기 포천시, 충남 천안시·공주시·아산시·당진시·부여군·청양군·홍성군, "
        "전남 나주시·함평군, 경북 청도군, 경남 진주시·의령군·하동군·함양군, 광주 광산구 "
        "어룡동·삼도동, 세종 전동면, 충북 청주시 옥산면·오창읍, 충남 서천군 판교면·비인면, "
        "전남 광양시 다압면, 구례군 간전면·토지면, 화순군 이서면, 영광군 군남면·염산면, "
        "신안군 지도읍·임자면·자은면·흑산면, 경남 밀양시 무안면, 거창군 남상면·신원면 / "
        "Gwangju Buk-gu; Gyeonggi Pocheon-si; Chungnam Cheonan-si, Gongju-si, Asan-si, "
        "Dangjin-si, Buyeo-gun, Cheongyang-gun and Hongseong-gun; Jeonnam Naju-si and "
        "Hampyeong-gun; Gyeongbuk Cheongdo-gun; Gyeongnam Jinju-si, Uiryeong-gun, "
        "Hadong-gun and Hamyang-gun; Gwangju Gwangsan-gu Eoryong-dong and Samdo-dong; "
        "Sejong Jeondong-myeon; Chungbuk Cheongju-si Oksan-myeon and Ochang-eup; "
        "Chungnam Seocheon-gun Pangyo-myeon and Biin-myeon; Jeonnam Gwangyang-si "
        "Daap-myeon, Gurye-gun Ganjeon-myeon and Toji-myeon, Hwasun-gun Iseo-myeon, "
        "Yeonggwang-gun Gunnam-myeon and Yeomsan-myeon, Sinan-gun Jido-eup, Imja-myeon, "
        "Jaeun-myeon and Heuksan-myeon; Gyeongnam Miryang-si Muan-myeon and "
        "Geochang-gun Namsang-myeon and Sinwon-myeon",
        "누락된 지역 구분기호를 보완하고 지역명 사전 영문 표기 적용",
    ),
    "전남 무안군 무안읍·일로읍·현경면함평군 함평읍·대동면·나산면 경남 창원시 웅동1동, 김해시 칠산서부동": (
        "전남 무안군 무안읍·일로읍·현경면, 함평군 함평읍·대동면·나산면, "
        "경남 창원시 웅동1동, 김해시 칠산서부동 / "
        "Jeonnam Muan-gun Muan-eup, Ilro-eup and Hyeongyeong-myeon; "
        "Hampyeong-gun Hampyeong-eup, Daedong-myeon and Nasan-myeon; "
        "Gyeongnam Changwon-si Ungdong 1-dong and Gimhae-si Chilsan-seobu-dong",
        "누락된 지역 구분기호를 보완하고 지역명 사전 영문 표기 적용",
    ),
    "경기 포천시 이동면": (
        "경기 포천시 이동면 / Gyeonggi Pocheon-si Idong-myeon",
        "지역명 사전 영문 표기 적용",
    ),
    "경남 산청 울산울주,경북의성,경남하동 경북안동,청송,영양,영덕": (
        "경남 산청군, 울산 울주군, 경북 의성군, 경남 하동군, "
        "경북 안동시·청송군·영양군·영덕군 / "
        "Gyeongnam Sancheong-gun; Ulsan Ulju-gun; Gyeongbuk Uiseong-gun; "
        "Gyeongnam Hadong-gun; Gyeongbuk Andong-si, Cheongsong-gun, "
        "Yeongyang-gun and Yeongdeok-gun",
        "띄어쓰기와 행정구역 접미사를 보완하고 지역명 사전 영문 표기 적용",
    ),
}

COUNT_SUFFIX_RE = re.compile(r"\s*(\(\d+\))\s*$")


@dataclass(frozen=True)
class RegionReviewDecision:
    status: str
    current_value: str
    expected_value: str
    difference: str
    detail: str

    def to_dict(self, *, issue_type: str) -> dict[str, str]:
        return {
            "status": self.status,
            "issue_type": issue_type,
            "current_value": self.current_value,
            "expected_value": self.expected_value,
            "difference": self.difference,
            "detail": self.detail,
        }


@cache
def region_english_names() -> dict[str, str]:
    names = {
        normalize_region_name(source): target
        for source, target in {**PROVINCE_SHORT_NAMES, **OFFICIAL_PROVINCE_NAMES}.items()
    }
    if not REGION_DATA_PATH.exists():
        return names

    with REGION_DATA_PATH.open(encoding="utf-8", newline="") as stream:
        rows = sorted(
            csv.DictReader(stream),
            key=lambda row: int(row.get("base_year") or 0),
        )
        for row in rows:
            add_region_name(names, row.get("sido_kr", ""), row.get("sido_en", ""))

            standard_ko = clean_csv_value(row.get("sigungu_kr", ""))
            standard_en = clean_csv_value(row.get("sigun_en", ""))
            if " " in standard_ko:
                standard_en = clean_csv_value(row.get("sigungu_2_en", "")) or standard_en
            add_region_name(names, standard_ko, standard_en)

            parent_ko = clean_csv_value(row.get("sigungu_1_kr", ""))
            parent_en = (
                clean_csv_value(row.get("sigun_en", ""))
                if parent_ko.endswith(("시", "군"))
                else clean_csv_value(row.get("sigungu_1_en", ""))
            )
            add_region_name(names, parent_ko, parent_en)

            child_ko = clean_csv_value(row.get("sigungu_2_kr", ""))
            child_en = clean_csv_value(row.get("sigungu_1_en", ""))
            add_region_name(names, child_ko, child_en)

    # The bundled KOSIS lookup predates the current English brands for the two
    # special self-governing provinces. The official current names win.
    names.update(
        {
            normalize_region_name(source): target
            for source, target in OFFICIAL_PROVINCE_NAMES.items()
        }
    )

    return names


def add_region_name(names: dict[str, str], korean: str, english: str) -> None:
    korean = clean_csv_value(korean)
    english = clean_csv_value(english)
    if not korean or not english:
        return
    names[normalize_region_name(korean)] = english

    short_korean = re.sub(r"(?:특별자치시|특별자치도|특별시|광역시|시|군|구)$", "", korean)
    short_english = re.sub(
        r"(?: Special Self-Governing (?:City|Province)| Special City| Metropolitan City|-si|-gun|-gu|-do)$",
        "",
        english,
        flags=re.IGNORECASE,
    )
    if short_korean and short_english:
        names.setdefault(normalize_region_name(short_korean), short_english)


def clean_csv_value(value: str | None) -> str:
    cleaned = str(value or "").strip()
    return "" if cleaned in {"NA", "N/A", "nan"} else cleaned


def normalize_region_name(value: str) -> str:
    return re.sub(r"[\s·･.,]+", "", value).casefold()


def region_review_decision(current_value: str) -> RegionReviewDecision | None:
    current = normalize_display_text(current_value)
    override = REGION_VALUE_OVERRIDES.get(current)
    if override is not None:
        expected, difference = override
        return RegionReviewDecision(
            status="확인 필요",
            current_value=current,
            expected_value=expected,
            difference=difference,
            detail=(
                f"{REGION_DATA_SOURCE}와 행정표준코드의 행정구역명을 기준으로 "
                "국문 구분과 영문 표기를 정리했습니다."
            ),
        )

    pair = extract_region_bilingual_pair(current)
    korean = pair[0] if pair else current
    current_english = pair[1] if pair else ""
    translated = translate_region_list(korean)
    if translated is None:
        return None

    if current_english:
        expected = f"{korean} {translated}"
        if canonical_english(current_english) == canonical_english(translated):
            return RegionReviewDecision(
                status="정상",
                current_value=current,
                expected_value=current,
                difference="지역명 사전 일치",
                detail=f"{REGION_DATA_SOURCE}의 시도·시군구 영문 표기와 일치합니다.",
            )
        return RegionReviewDecision(
            status="확인 필요",
            current_value=current,
            expected_value=expected,
            difference="지역명 사전 영문 표기 적용",
            detail=f"{REGION_DATA_SOURCE}를 기준으로 지역명의 로마자 표기와 구분기호를 교정했습니다.",
        )

    return RegionReviewDecision(
        status="확인 필요",
        current_value=current,
        expected_value=translated,
        difference="지역명 영문 표기 제안",
        detail=f"{REGION_DATA_SOURCE}를 기준으로 국문 지역명의 영문 표기를 생성했습니다.",
    )


def extract_region_bilingual_pair(value: str) -> tuple[str, str] | None:
    latin = re.search(r"[A-Za-z]", value)
    if latin is None or latin.start() == 0:
        return None
    korean = value[: latin.start()].strip(" ,;/")
    english = value[latin.start() :].strip(" ,;/")
    if not korean or not english or not re.search(r"[가-힣]", korean):
        return None
    return korean, english


def translate_region_list(korean_value: str) -> str | None:
    items = [item.strip() for item in re.split(r"\s*[,;]\s*", korean_value) if item.strip()]
    if not items:
        return None
    translated: list[str] = []
    for item in items:
        region = translate_region_item(item)
        if region is None:
            return None
        translated.append(region)
    return ", ".join(translated)


def translate_region_item(value: str) -> str | None:
    suffix_match = COUNT_SUFFIX_RE.search(value)
    count_suffix = suffix_match.group(1) if suffix_match else ""
    source = COUNT_SUFFIX_RE.sub("", value).strip()
    names = region_english_names()

    exact = names.get(normalize_region_name(source))
    if exact:
        return f"{exact}{count_suffix}"

    tokens = source.split()
    if len(tokens) < 2:
        return None
    translated_tokens: list[str] = []
    for token in tokens:
        translated = names.get(normalize_region_name(token))
        if not translated:
            return None
        translated_tokens.append(translated)
    return f"{' '.join(translated_tokens)}{count_suffix}"


def normalize_display_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def canonical_english(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
