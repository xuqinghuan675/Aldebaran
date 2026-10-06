import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.mainline import classify_mainline, rs_leads_market


class MainlineTest(unittest.TestCase):
    def test_ai_leaders_classified_major_by_code(self):
        self.assertEqual(classify_mainline('601138', '工业富联', []), 'major')
        self.assertEqual(classify_mainline('300308', '中际旭创', ['中际旭创']), 'major')
        self.assertEqual(classify_mainline('688256', '寒武纪', []), 'major')

    def test_keyword_major_and_minor(self):
        self.assertEqual(classify_mainline('000000', '某某', ['光模块']), 'major')
        self.assertEqual(classify_mainline('000000', '某某机器人', []), 'minor')

    def test_unrelated_is_none(self):
        self.assertEqual(classify_mainline('600519', '贵州茅台', ['白酒']), 'none')

    def test_data_driven_strong_sector_is_minor(self):
        self.assertEqual(
            classify_mainline('000000', '某股', ['某题材'], strong_sectors=['某题材']),
            'minor',
        )

    def test_etf_is_none_even_with_sector_keyword(self):
        self.assertEqual(classify_mainline('159807', '科技ETF', []), 'none')
        self.assertEqual(classify_mainline('510310', '沪深300ETF', []), 'none')
        self.assertEqual(classify_mainline('512480', '半导体ETF', ['半导体']), 'none')
        self.assertEqual(classify_mainline('159995', '芯片ETF', ['芯片']), 'none')

    def test_rs_leads_market(self):
        self.assertTrue(rs_leads_market(8.0, 2.0))
        self.assertFalse(rs_leads_market(1.0, 2.0))
        self.assertFalse(rs_leads_market(None, 2.0))


if __name__ == '__main__':
    unittest.main()
