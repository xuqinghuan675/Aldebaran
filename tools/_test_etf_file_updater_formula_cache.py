import unittest
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.etf_file_updater import (
    _force_workbook_recalc,
    _refresh_hist_formula_caches,
)


class EtfFileUpdaterFormulaCacheTest(unittest.TestCase):
    def test_refresh_hist_formula_caches_preserves_formula_and_updates_cached_values(self):
        xml = (
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row r="2">'
            '<c r="H2" t="e"><f>(D2-E2)/O2*100</f><v>#DIV/0!</v></c>'
            '<c r="K2"><f>F2/P2*100</f><v>0</v></c>'
            '<c r="P2"><v>200</v></c>'
            '</row></sheetData></worksheet>'
        )

        out = _refresh_hist_formula_caches(
            xml,
            2,
            {'high': 11, 'low': 10, 'pre_close': 10, 'volume': 50},
        )

        self.assertIn('<f>(D2-E2)/O2*100</f><v>10</v>', out)
        self.assertIn('<f>F2/P2*100</f><v>25</v>', out)
        self.assertNotIn('t="e"', out)
        self.assertNotIn('#DIV/0!', out)

    def test_refresh_hist_formula_caches_handles_shared_formula_followers(self):
        xml = (
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row r="3">'
            '<c r="H3" t="e"><f t="shared" si="1"/><v>#DIV/0!</v></c>'
            '<c r="K3"><f t="shared" si="2"/><v>0</v></c>'
            '<c r="P3"><v>100</v></c>'
            '</row></sheetData></worksheet>'
        )

        out = _refresh_hist_formula_caches(
            xml,
            3,
            {'high': 12, 'low': 9, 'pre_close': 10, 'volume': 20},
        )

        self.assertIn('<f t="shared" si="1"/><v>30</v>', out)
        self.assertIn('<f t="shared" si="2"/><v>20</v>', out)
        self.assertNotIn('t="e"', out)
        self.assertNotIn('#DIV/0!', out)

    def test_force_workbook_recalc_sets_excel_recalculation_flags(self):
        xml = (
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<calcPr calcId="191029" iterate="1"/>'
            '</workbook>'
        )

        out = _force_workbook_recalc(xml)

        ET.fromstring(out)
        self.assertIn('calcMode="auto"', out)
        self.assertIn('fullCalcOnLoad="1"', out)
        self.assertIn('forceFullCalc="1"', out)
        self.assertIn('iterate="1"', out)


if __name__ == '__main__':
    unittest.main()
