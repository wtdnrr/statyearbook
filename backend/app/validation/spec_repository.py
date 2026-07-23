from __future__ import annotations

import json
from typing import Any

from app.db.connection import DatabaseConnection, DatabaseRow
from app.validation.highlights import int_or_none


def load_rule_specs_by_id(
    connection: DatabaseConnection,
    check_rows: list[DatabaseRow],
) -> dict[str, dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {}
    profile_ids = sorted(
        {
            int(row["profile_id"])
            for row in check_rows
            if row["profile_id"] is not None
        }
    )

    if profile_ids:
        placeholders = ", ".join("?" for _ in profile_ids)
        profile_rows = connection.execute(
            f"""
            SELECT id, rules_json
            FROM validation_profiles
            WHERE id IN ({placeholders})
            """,
            profile_ids,
        ).fetchall()

        for profile_row in profile_rows:
            try:
                rules = json.loads(profile_row["rules_json"] or "{}")
            except json.JSONDecodeError:
                continue
            checks = rules.get("checks", [])
            if not isinstance(checks, list):
                continue
            for spec in checks:
                if not isinstance(spec, dict) or not spec.get("id"):
                    continue
                specs[str(spec["id"])] = spec

    for row in check_rows:
        rule_id = str(row["rule_id"])
        spec = synthetic_cross_table_spec(rule_id)
        if spec is not None:
            specs[rule_id] = spec
    return specs


def synthetic_cross_table_spec(rule_id: str) -> dict[str, Any] | None:
    return cross_split_part_row_total_spec(rule_id) or cross_profile_row_sum_operand_spec(rule_id)


def cross_split_part_row_total_spec(rule_id: str) -> dict[str, Any] | None:
    if not rule_id.startswith("cross.split_part_row_total:"):
        return None

    values: dict[str, str] = {}
    for chunk in rule_id.split(":")[1:]:
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        values[key] = value

    target = int_or_none(values.get("target"))
    related = [
        int(value)
        for value in values.get("related", "").split(",")
        if value.strip().isdigit()
    ]
    role = values.get("role", "target")
    if target is None:
        return None

    if role == "operand":
        return {
            "id": rule_id,
            "type": "cross_split_operand_row",
            "operand_columns": related,
        }

    return {
        "id": rule_id,
        "type": "row_sum",
        "target_column": target,
        "operand_columns": related,
    }


def cross_profile_row_sum_operand_spec(rule_id: str) -> dict[str, Any] | None:
    if not rule_id.startswith("cross.profile_row_sum_operand:"):
        return None

    related_chunk = next(
        (chunk for chunk in rule_id.split(":") if chunk.startswith("related=")),
        "",
    )
    related = [
        int(value)
        for value in related_chunk.removeprefix("related=").split(",")
        if value.strip().isdigit()
    ]
    if not related:
        return None
    return {
        "id": rule_id,
        "type": "cross_split_operand_row",
        "operand_columns": related,
    }
