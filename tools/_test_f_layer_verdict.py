import unittest

from core.predictor import parse_f_layer_verdict


class FLayerVerdictTest(unittest.TestCase):
    def test_parses_bearish_with_confidence(self):
        text = "……综合判断。\n裁决:bearish 置信:6"
        d, c = parse_f_layer_verdict(text)
        self.assertEqual(d, 'bearish')
        self.assertEqual(c, 6)

    def test_parses_bullish_full_width_colon(self):
        text = "辩论纪要。\n裁决：bullish 置信：8"
        d, c = parse_f_layer_verdict(text)
        self.assertEqual(d, 'bullish')
        self.assertEqual(c, 8)

    def test_missing_verdict_returns_none(self):
        d, c = parse_f_layer_verdict("没有裁决行的自由文本")
        self.assertIsNone(d)
        self.assertIsNone(c)

    def test_garbage_confidence_returns_none_conf(self):
        d, c = parse_f_layer_verdict("裁决:bearish 置信:高")
        self.assertEqual(d, 'bearish')
        self.assertIsNone(c)


if __name__ == '__main__':
    unittest.main()
