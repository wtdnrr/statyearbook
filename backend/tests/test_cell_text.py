from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET

from app.ingest.cell_text import footnote_markers_from_texts, split_cell_text
from app.ingest.hwpx_importer import cell_text_with_footnote
from app.ingest.markdown_importer import line_is_note


class CellTextTest(unittest.TestCase):
    def test_numeric_footnote_is_split_only_when_definition_exists(self) -> None:
        markers = footnote_markers_from_texts(
            ["#주1) 중앙시도 통제장비", "#주2) 지진해일 통제장비"]
        )

        self.assertEqual(markers, {"1)", "2)"})
        self.assertEqual(split_cell_text("621)", markers), ("62", "1)"))
        self.assertEqual(split_cell_text("292)", markers), ("29", "2)"))
        self.assertEqual(split_cell_text("2292)", markers), ("229", "2)"))
        self.assertEqual(split_cell_text("621)"), ("621)", ""))

    def test_hwpx_superscript_run_is_stored_as_footnote_marker(self) -> None:
        cell = ET.fromstring(
            """
            <tc>
              <run charPrIDRef="53"><t>62</t></run>
              <run charPrIDRef="86"><t>1)</t></run>
            </tc>
            """
        )

        self.assertEqual(cell_text_with_footnote(cell, {"86"}), ("62", "1)"))

    def test_markdown_headings_are_not_table_notes(self) -> None:
        self.assertFalse(line_is_note("### 제5장"))
        self.assertTrue(line_is_note("#주1) 기준 변경"))
        self.assertTrue(line_is_note("# Onnara BPS : Onnara business process system"))

    def test_semantic_type_qualifier_line_is_preserved(self) -> None:
        self.assertEqual(
            split_cell_text("출자기관\nGovernment-funded Organizations\n[Type A]"),
            ("출자기관\nGovernment-funded Organizations\n[Type A]", ""),
        )


if __name__ == "__main__":
    unittest.main()
