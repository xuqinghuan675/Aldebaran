import unittest

from core.tracking import _execution_quality


class ExecutionQualityTest(unittest.TestCase):
    def test_buy_target_not_treated_as_watch(self):
        task = {'task_class': 'buy', 'sample_type': 'watch', 'target_pct': 5.0}
        self.assertEqual(_execution_quality(task, 'A', 5.0, 5.0, 'P_clean'), '止盈达标')

    def test_buy_stop_is_stopped_out(self):
        task = {'task_class': 'buy', 'sample_type': 'watch', 'target_pct': 5.0, 'stop_pct': -3.0}
        self.assertEqual(_execution_quality(task, 'F', -3.0, 0.5, 'P_stressed'), '止损出局')

    def test_buy_expire_gain_is_effective(self):
        task = {'task_class': 'buy', 'sample_type': 'watch', 'target_pct': 5.0}
        self.assertEqual(_execution_quality(task, 'C', 1.0, 2.0, 'P_clean'), '计划有效')

    def test_avoid_fall_is_correct(self):
        task = {'task_class': 'avoid', 'sample_type': 'watch'}
        self.assertEqual(_execution_quality(task, 'A', -5.0, 0.0, 'neutral'), '回避正确')

    def test_avoid_rise_is_missed(self):
        task = {'task_class': 'avoid', 'sample_type': 'watch'}
        self.assertEqual(_execution_quality(task, 'F', 6.0, 6.0, 'neutral'), '回避失败（踏空）')

    def test_watch_rise_is_missed(self):
        task = {'task_class': 'watch', 'sample_type': 'watch'}
        self.assertEqual(_execution_quality(task, 'F', 6.0, 6.0, 'neutral'), '观望错过机会')

    def test_watch_fall_is_dodged(self):
        task = {'task_class': 'watch', 'sample_type': 'watch'}
        self.assertEqual(_execution_quality(task, 'A', -6.0, 0.0, 'neutral'), '观望躲过下跌')


if __name__ == '__main__':
    unittest.main()
