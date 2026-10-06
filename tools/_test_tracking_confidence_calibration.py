import unittest

from core.tracking import get_tracking_stats


def _ct(conf, grade):
    return {'status': 'closed', 'kind': 'stock', 'grade': grade,
            'confidence': conf, 'task_class': 'buy'}


class ConfidenceCalibrationTest(unittest.TestCase):
    def test_buckets_split_by_confidence(self):
        tasks = [
            _ct(8, 'A'), _ct(9, 'B'), _ct(7, 'F'),   # high(7-10): closed3 win2 target2
            _ct(5, 'C'), _ct(6, 'D'),                # mid(5-6): closed2 win1 target0
            _ct(3, 'F'), _ct(2, 'A'),                # low(1-4): closed2 win1 target1
        ]
        cal = get_tracking_stats(tasks)['confidence_calibration']
        self.assertEqual(cal['high']['closed'], 3)
        self.assertEqual(cal['high']['win'], 2)
        self.assertEqual(cal['high']['target'], 2)
        self.assertAlmostEqual(cal['high']['win_rate'], 2 / 3)
        self.assertAlmostEqual(cal['high']['target_rate'], 2 / 3)
        self.assertEqual(cal['mid']['closed'], 2)
        self.assertEqual(cal['mid']['win'], 1)
        self.assertEqual(cal['mid']['target'], 0)
        self.assertEqual(cal['low']['closed'], 2)
        self.assertEqual(cal['low']['win'], 1)
        self.assertEqual(cal['low']['target'], 1)

    def test_zero_confidence_skipped(self):
        cal = get_tracking_stats([_ct(0, 'A'), _ct(8, 'A')])['confidence_calibration']
        self.assertEqual(cal['high']['closed'], 1)
        self.assertEqual(cal['low']['closed'], 0)

    def test_ungraded_skipped(self):
        cal = get_tracking_stats([_ct(8, None), _ct(8, 'A')])['confidence_calibration']
        self.assertEqual(cal['high']['closed'], 1)

    def test_empty_bucket_rate_none(self):
        cal = get_tracking_stats([])['confidence_calibration']
        self.assertIsNone(cal['high']['win_rate'])
        self.assertEqual(cal['high']['closed'], 0)


if __name__ == '__main__':
    unittest.main()
