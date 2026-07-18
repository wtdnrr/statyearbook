from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json

from app.validation.cross_table_rules import (
    DEFAULT_CROSS_TABLE_RULES,
    AdjacentDuplicateTableRule,
    ConfiguredCrossTableCellMatchRule,
    ConfiguredCrossTableRowSumRule,
    ConfiguredCrossTableWeightedAverageRule,
)
from app.validation.models import ValidationCheckRecord, ValidationIssueRecord, ValidationTable
from app.validation.profile_rules import ProfileSpecRule, ProfileStateRule
from app.validation.profiles import ValidationProfile
from app.validation.rules import DEFAULT_RULES, ValidationRule


@dataclass
class ValidationRunOutcome:
    issues: list[ValidationIssueRecord] = field(default_factory=list)
    checks: list[ValidationCheckRecord] = field(default_factory=list)


class ValidationEngine:
    def __init__(
        self,
        rules: list[ValidationRule] | None = None,
        *,
        profiles: dict[str, ValidationProfile] | None = None,
    ) -> None:
        self._rules = rules if rules is not None else ([] if profiles is not None else DEFAULT_RULES)
        self._profiles = profiles or {}
        self._profile_rules: list[ValidationRule] = []
        if profiles is not None:
            self._profile_rules = [
                ProfileStateRule(self._profiles),
                ProfileSpecRule(self._profiles),
            ]
        self._cross_table_rules = [*DEFAULT_CROSS_TABLE_RULES]
        if profiles is not None:
            self._cross_table_rules.append(ConfiguredCrossTableRowSumRule(self._profiles))
            self._cross_table_rules.append(ConfiguredCrossTableWeightedAverageRule(self._profiles))
            self._cross_table_rules.append(ConfiguredCrossTableCellMatchRule(self._profiles))

    @property
    def rules_version(self) -> str:
        rule_ids = ",".join(
            rule.rule_id
            for rule in [*self._profile_rules, *self._rules, *self._cross_table_rules]
        )
        profile_payload = {
            code: {
                "id": profile.id,
                "signature": profile.structure_signature,
                "status": profile.status,
                "rules": len(profile.table_rules),
            }
            for code, profile in sorted(self._profiles.items())
        }
        profile_part = json.dumps(profile_payload, ensure_ascii=False, sort_keys=True)
        profile_hash = hashlib.sha256(profile_part.encode("utf-8")).hexdigest()[:16]
        return f"rules-v2:{rule_ids}:profiles={profile_hash}"

    def validate(self, tables: list[ValidationTable]) -> list[ValidationIssueRecord]:
        return self.evaluate(tables).issues

    def evaluate(self, tables: list[ValidationTable]) -> ValidationRunOutcome:
        issues: list[ValidationIssueRecord] = []
        checks: list[ValidationCheckRecord] = []
        for table in tables:
            for rule in [*self._profile_rules, *self._rules]:
                if isinstance(rule, ProfileSpecRule):
                    profile_issues, profile_checks = rule.evaluate(table)
                    issues.extend(profile_issues)
                    checks.extend(profile_checks)
                else:
                    issues.extend(rule.validate(table))

        for rule in self._cross_table_rules:
            result = rule.evaluate(tables)
            issues.extend(result.issues)
            checks.extend(result.checks)

        return ValidationRunOutcome(issues=dedupe_issues(issues), checks=checks)


def dedupe_issues(issues: list[ValidationIssueRecord]) -> list[ValidationIssueRecord]:
    deduped: list[ValidationIssueRecord] = []
    seen: set[tuple[object, ...]] = set()

    for issue in issues:
        key = (
            issue.table_id,
            issue.issue_type,
            issue.row_index,
            issue.col_index,
            issue.current_value,
            issue.expected_value,
            issue.difference,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(issue)

    return deduped
