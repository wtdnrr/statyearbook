from __future__ import annotations

from app.validation.models import ValidationIssueRecord, ValidationTable
from app.validation.rules import DEFAULT_RULES, ValidationRule


class ValidationEngine:
    def __init__(self, rules: list[ValidationRule] | None = None) -> None:
        self._rules = rules or DEFAULT_RULES

    @property
    def rules_version(self) -> str:
        rule_ids = ",".join(rule.rule_id for rule in self._rules)
        return f"rules-v1:{rule_ids}"

    def validate(self, tables: list[ValidationTable]) -> list[ValidationIssueRecord]:
        issues: list[ValidationIssueRecord] = []
        for table in tables:
            for rule in self._rules:
                issues.extend(rule.validate(table))
        return issues

