from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from functools import lru_cache
from pathlib import Path
from typing import Any


CURATED_PROFILE_PATH = Path(__file__).with_name("curated_profiles.json")


@dataclass(frozen=True)
class CuratedProfileApplication:
    checks: list[dict[str, Any]]
    table_type: str
    status: str
    notes: str


@lru_cache(maxsize=1)
def curated_profiles() -> dict[str, dict[str, Any]]:
    if not CURATED_PROFILE_PATH.exists():
        return {}

    payload = json.loads(CURATED_PROFILE_PATH.read_text(encoding="utf-8"))
    profiles: dict[str, dict[str, Any]] = {
        code: deepcopy(profile)
        for code, profile in payload.get("profiles", {}).items()
    }
    for group in payload.get("groups", []):
        profile = group.get("profile", {})
        for code in group.get("codes", []):
            profiles[code] = deepcopy(profile)
    return profiles


def apply_curated_profile(
    table_code: str,
    *,
    checks: list[dict[str, Any]],
    table_type: str,
    status: str,
    notes: str,
) -> CuratedProfileApplication:
    profile = curated_profiles().get(table_code)
    if profile is None:
        return CuratedProfileApplication(checks=checks, table_type=table_type, status=status, notes=notes)

    curated_checks = [deepcopy(check) for check in checks]
    apply_confidence_overrides(curated_checks, profile.get("confidence_overrides", []))

    decision = profile.get("decision")
    if decision:
        curated_checks.append(
            {
                "id": f"curated.{table_code}.profile_decision",
                "type": "profile_decision",
                "category": "table",
                "check_group": "profile",
                "check_type": "프로파일 큐레이션",
                "label": str(profile.get("label") or decision),
                "decision": decision,
                "execute": False,
                "confidence": float(profile.get("confidence", 1.0)),
            }
        )

    curated_checks.extend(deepcopy(profile.get("checks", [])))
    return CuratedProfileApplication(
        checks=curated_checks,
        table_type=str(profile.get("table_type") or table_type),
        status=str(profile.get("status") or status),
        notes=str(profile.get("notes") or notes),
    )


def apply_confidence_overrides(
    checks: list[dict[str, Any]],
    overrides: list[dict[str, Any]],
) -> None:
    for override in overrides:
        match = override.get("match", {})
        confidence = override.get("confidence")
        if confidence is None:
            continue
        for check in checks:
            if check_matches(check, match):
                check["confidence"] = float(confidence)
                check["curated"] = True


def check_matches(check: dict[str, Any], match: dict[str, Any]) -> bool:
    return all(check.get(key) == value for key, value in match.items())
