import unittest

from app.export.metadata_owner_audit import (
    LogicalTable,
    OnnaraUser,
    ReferenceOwner,
    compare_phone,
    current_department,
    department_matches,
    index_users_by_name,
    match_tables_to_references,
    parse_officer_names,
    resolve_expected_users,
)


def user(name: str, department: str, phone: str, row: int = 2) -> OnnaraUser:
    return OnnaraUser(
        row_number=row,
        display_department=f"본부 {department}",
        department=department,
        name=name,
        rank="행정주사",
        position="",
        phone=phone,
    )


class MetadataOwnerAuditTest(unittest.TestCase):
    def test_reference_department_and_officer_cleanup(self) -> None:
        self.assertEqual(current_department("정보공개과->정보공개제도과"), "정보공개제도과")
        self.assertEqual(current_department("디지털인프라혁신과?"), "디지털인프라혁신과")
        self.assertEqual(parse_officer_names("김형범주무관"), ("김형범",))
        self.assertEqual(
            parse_officer_names("김혁수 주무관/김원규 주무관"),
            ("김혁수", "김원규"),
        )

    def test_department_equivalence_handles_hierarchy_and_office_suffix(self) -> None:
        self.assertTrue(department_matches("상훈담당관실", "상훈담당관"))
        self.assertTrue(department_matches("국가기록원 행정지원과", "행정지원과"))
        self.assertFalse(department_matches("정보공개과", "정보공개제도과"))

    def test_multiple_officers_are_resolved_against_corresponding_departments(self) -> None:
        users = [
            user("김혁수", "민방위과", "044-205-4371"),
            user("김원규", "위기관리지원과", "044-205-4428", row=3),
        ]
        resolutions = resolve_expected_users(
            ("김혁수", "김원규"),
            "민방위과/위기관리지원과",
            index_users_by_name(users),
        )
        self.assertEqual(
            [resolution.status for resolution in resolutions],
            ["성명·부서 일치", "성명·부서 일치"],
        )
        self.assertEqual(
            compare_phone(
                expected_officers=("김혁수", "김원규"),
                db_officers=("김혁수", "김원규"),
                db_phones=("044-205-4371", "044-205-4429"),
                resolutions=resolutions,
            ),
            "불일치",
        )

    def test_group_title_override_maps_subtables_to_owner_row(self) -> None:
        references = [
            ReferenceOwner(
                year=2026,
                row_number=49,
                sequence_index=0,
                title="보조금24",
                department_raw="국민맞춤서비스과",
                department="국민맞춤서비스과",
                officer_raw="김양휘 주무관",
                officers=("김양휘",),
                note="",
            )
        ]
        table = LogicalTable(
            report_id=36,
            code="2-1-4-2",
            title="맞춤 안내 수준 현황",
            table_order=33,
            department="국민맞춤서비스과",
            officers=("김양휘",),
            phones=("044-205-2811",),
            sources=(),
            part_ids=(1,),
        )
        match = match_tables_to_references([table], references, 2026)[table.code]
        self.assertEqual(match.reference, references[0])
        self.assertEqual(match.method, "번호별 명칭/묶음 매칭")


if __name__ == "__main__":
    unittest.main()
