import unittest

import pandas as pd

from core.board_rules import (
    count_mainland_emotion_rows,
    include_in_mainland_emotion_count,
    is_ex_right_name,
    same_source_mainland_emotion_count,
)


class BoardRulesEmotionCountTest(unittest.TestCase):
    def test_excludes_bse_rows_from_mainland_emotion_counts(self):
        self.assertFalse(include_in_mainland_emotion_count('920634', 'Xin Wei Ling'))
        self.assertFalse(include_in_mainland_emotion_count('830000', 'BSE Sample'))

    def test_excludes_ex_right_rows_from_mainland_emotion_counts(self):
        self.assertTrue(is_ex_right_name('XD Hao Hua'))
        self.assertTrue(is_ex_right_name('xr sample'))
        self.assertTrue(is_ex_right_name('DR sample'))
        self.assertFalse(include_in_mainland_emotion_count('600378', 'XD Hao Hua'))

    def test_keeps_regular_mainland_rows(self):
        self.assertTrue(include_in_mainland_emotion_count('002421', 'Da Shi'))
        self.assertTrue(include_in_mainland_emotion_count('300161', 'Hua Zhong'))

    def test_counts_pool_rows_with_mainland_filter(self):
        df = pd.DataFrame([
            {'代码': '920634', '名称': '新威凌'},
            {'代码': '600378', '名称': 'XD昊华科'},
            {'代码': '002421', '名称': '达实智能'},
        ])
        self.assertEqual(count_mainland_emotion_rows(df), 1)

    def test_same_source_count_only_when_raw_rows_match_total(self):
        df = pd.DataFrame([
            {'代码': '920634', '名称': '新威凌'},
            {'代码': '002421', '名称': '达实智能'},
        ])
        self.assertEqual(same_source_mainland_emotion_count(2, df), 1)
        self.assertIsNone(same_source_mainland_emotion_count(3, df))


if __name__ == '__main__':
    unittest.main()
