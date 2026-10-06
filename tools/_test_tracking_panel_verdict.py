import unittest

from ui.tracking_panel import _conversion_wait_text, _verdict_chip


class TrackingPanelVerdictTest(unittest.TestCase):
    def test_unconverted_bullish_watch_small_move_is_observation_reasonable(self):
        task = {
            'status': 'closed',
            'task_class': 'watch',
            'direction': 'bullish',
            'grade': 'B',
            'grade_detail': {
                'task_class': 'watch',
                'direction': 'bullish',
                'final_pct': 0.89,
                'outcome_tier': 'waited_right',
            },
            'pred_snapshot': {'task_blueprint': {'category': 'bullish_watch'}},
        }
        self.assertEqual(_verdict_chip(task), '\u2705 \u89c2\u671b\u5408\u7406')

    def test_unconverted_bullish_watch_drop_is_observation_reasonable(self):
        task = {
            'status': 'closed',
            'task_class': 'watch',
            'direction': 'bullish',
            'grade': 'C',
            'grade_detail': {
                'task_class': 'watch',
                'direction': 'bullish',
                'final_pct': -0.8,
                'outcome_tier': 'waited_flat',
            },
            'pred_snapshot': {'task_blueprint': {'category': 'bullish_watch'}},
        }
        self.assertEqual(_verdict_chip(task), '\u2705 \u89c2\u671b\u5408\u7406')

    def test_unconverted_bullish_watch_big_drop_is_wrong(self):
        task = {
            'status': 'closed',
            'task_class': 'watch',
            'direction': 'bullish',
            'grade': 'D',
            'grade_detail': {
                'task_class': 'watch',
                'direction': 'bullish',
                'final_pct': -6.2,
                'outcome_tier': 'setup_broken',
            },
            'pred_snapshot': {'task_blueprint': {'category': 'bullish_watch'}},
        }
        self.assertEqual(_verdict_chip(task), '\u274c \u843d\u7a7a')

    def test_conversion_wait_reason_has_short_text(self):
        task = {
            'status': 'open',
            'conversion_wait_reason': {'type': 'volume_unconfirmed'},
        }
        self.assertIn('\u89e6\u4ef7\u7f29\u91cf', _conversion_wait_text(task))


if __name__ == '__main__':
    unittest.main()
