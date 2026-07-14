import unittest

from app.export.metadata_owner_audit import (
    LogicalTable,
    OnnaraUser,
    ReferenceMatch,
    ReferenceOwner,
    index_users_by_name,
)
from app.export.owner_reflection_mismatches import build_owner_mismatch_rows


def reference(
    title: str,
    department: str,
    officer: str,
    row: int = 4,
) -> ReferenceOwner:
    return ReferenceOwner(
        year=2026,
        row_number=row,
        sequence_index=0,
        title=title,
        department_raw=department,
        department=department,
        officer_raw=officer,
        officers=(officer,),
        note="",
    )


def table(
    code: str,
    title: str,
    department: str,
    officer: str,
) -> LogicalTable:
    return LogicalTable(
        report_id=36,
        code=code,
        title=title,
        table_order=1,
        department=department,
        officers=(officer,),
        phones=("044-205-0000",),
        sources=(),
        part_ids=(1,),
    )


class OwnerReflectionMismatchTest(unittest.TestCase):
    def test_only_department_or_officer_mismatches_are_exported(self) -> None:
        ok = table("1-1-1", "정상 표", "조직기획과", "홍길동")
        changed = table("1-1-2", "수정 표", "예전과", "김이전")
        ref_ok = reference("정상 표", "조직기획과", "홍길동")
        ref_changed = reference("수정 표", "새로운과", "김신규")

        rows, skipped, low_confidence = build_owner_mismatch_rows(
            tables=(ok, changed),
            matches={
                ok.code: ReferenceMatch(ref_ok, "제목 일치", 1.0),
                changed.code: ReferenceMatch(ref_changed, "제목 일치", 1.0),
            },
        )

        self.assertEqual(skipped, 0)
        self.assertEqual(low_confidence, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].code, "1-1-2")
        self.assertEqual(rows[0].mismatch_fields, "부서, 담당자")

    def test_expected_officer_phone_is_compared_with_draft_phone(self) -> None:
        draft = table("1-1-1", "번호 수정 표", "조직기획과", "홍길동")
        ref = reference("번호 수정 표", "조직기획과", "홍길동")
        users = [
            OnnaraUser(
                row_number=2,
                display_department="조직기획과",
                department="조직기획과",
                name="홍길동",
                rank="행정주사",
                position="",
                phone="044-205-9999",
            )
        ]

        rows, _, _ = build_owner_mismatch_rows(
            tables=(draft,),
            matches={draft.code: ReferenceMatch(ref, "제목 일치", 1.0)},
            users_by_name=index_users_by_name(users),
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].mismatch_fields, "내선번호")
        self.assertEqual(rows[0].expected_phones, "044-205-9999")


if __name__ == "__main__":
    unittest.main()
