from __future__ import annotations

import unittest

from app.validation.profile_rules import ProfileSpecRule
from app.validation.catalog import (
    LLM_PROFILE_RULE_TYPES,
    PROFILE_SPEC_EXECUTORS,
    SUPPORTED_PROFILE_SPEC_TYPES,
    check_group_for_rule_type,
)


class ValidationArchitectureTests(unittest.TestCase):
    def test_every_profile_executor_points_to_an_existing_method(self) -> None:
        rule = ProfileSpecRule({})

        for rule_type, method_name in PROFILE_SPEC_EXECUTORS.items():
            with self.subTest(rule_type=rule_type):
                self.assertTrue(callable(getattr(rule, method_name, None)))

    def test_llm_profile_types_are_part_of_the_persisted_spec_contract(self) -> None:
        self.assertTrue(LLM_PROFILE_RULE_TYPES <= SUPPORTED_PROFILE_SPEC_TYPES)

    def test_rule_groups_are_derived_from_the_shared_catalog(self) -> None:
        self.assertEqual(check_group_for_rule_type("row_sum"), "sum")
        self.assertEqual(check_group_for_rule_type("row_ratio"), "ratio")
        self.assertEqual(check_group_for_rule_type("row_growth_rate"), "growth_rate")


if __name__ == "__main__":
    unittest.main()
