from __future__ import annotations

from dataclasses import asdict, dataclass


ERROR_STATUS = "오류 의심"
REVIEW_STATUS = "확인 필요"


@dataclass(frozen=True)
class ValidationRuleDefinition:
    key: str
    name: str
    default_status: str
    default_severity: str
    owner_role: str
    description: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


REQUIRED_RULE_DEFINITIONS: tuple[ValidationRuleDefinition, ...] = (
    ValidationRuleDefinition(
        key="sum",
        name="합계 검수",
        default_status=ERROR_STATUS,
        default_severity="critical",
        owner_role="규칙 엔진",
        description="계, 합계, 소계, 명시 산식의 산술 결과가 표 값과 일치하는지 확인합니다.",
    ),
    ValidationRuleDefinition(
        key="ratio",
        name="비율 검수",
        default_status=ERROR_STATUS,
        default_severity="critical",
        owner_role="규칙 엔진",
        description="비율, 비중, 잔액율 등 분자/분모 산식으로 재계산 가능한 값을 확인합니다. 명시 산식, 같은 행의 분자/분모 열, 같은 열의 분자/분모 행을 포함합니다.",
    ),
    ValidationRuleDefinition(
        key="growth_rate",
        name="증감률 검수",
        default_status=ERROR_STATUS,
        default_severity="critical",
        owner_role="규칙 엔진",
        description="전년 대비 증감액과 증감률 계산값을 확인합니다. 연도 열이 나란히 있는 표의 증감률 행과 연도 행이 나란히 있는 표의 증감/증감률 열을 모두 확인합니다.",
    ),
    ValidationRuleDefinition(
        key="outlier",
        name="이상치 검수",
        default_status=REVIEW_STATUS,
        default_severity="warning",
        owner_role="규칙 엔진",
        description="같은 열 또는 같은 시계열 안에서 통계적으로 튀는 값을 확인 대상으로 표시합니다.",
    ),
    ValidationRuleDefinition(
        key="spelling",
        name="오탈자 검수",
        default_status=ERROR_STATUS,
        default_severity="critical",
        owner_role="LLM/담당자",
        description="국문·영문 철자 오류, 문자 깨짐, 숫자 구분기호 오류와 연관 셀 대비 명확한 표기 불일치를 확인합니다.",
    ),
    ValidationRuleDefinition(
        key="translation",
        name="번역 검수",
        default_status=REVIEW_STATUS,
        default_severity="warning",
        owner_role="LLM/담당자",
        description="국문과 영문의 의미 대응을 확인하며 기관명·행사명·행정구역명은 저장된 번역 사전을 우선 적용합니다.",
    ),
    ValidationRuleDefinition(
        key="metadata",
        name="메타정보 검수",
        default_status=REVIEW_STATUS,
        default_severity="warning",
        owner_role="규칙 엔진/담당자",
        description="단위, 기준일, 출처의 누락 여부와 저장된 프로파일의 단위 기준 일치 여부를 함께 확인합니다.",
    ),
)

RULE_DEFINITION_BY_KEY = {definition.key: definition for definition in REQUIRED_RULE_DEFINITIONS}


def rule_definition_payload() -> list[dict[str, str]]:
    return [definition.to_dict() for definition in REQUIRED_RULE_DEFINITIONS]


def rule_spec(rule_key: str, spec: dict) -> dict:
    definition = RULE_DEFINITION_BY_KEY[rule_key]
    return {
        "check_group": definition.key,
        "check_type": definition.name,
        "failure_status": definition.default_status,
        "severity": definition.default_severity,
        **spec,
    }
