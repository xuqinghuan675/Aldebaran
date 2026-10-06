import unittest

from core.tracking import _infer_sample_type


class TrackingObservationOnlyTest(unittest.TestCase):
    def test_trade_like_prediction_is_still_observation_sample(self):
        pred = {
            'direction': 'bullish',
            'final_action': 'buy',
            'entry_price': 10,
            'confidence': 9,
            'target_pct': 8,
            'stop_pct': -3,
        }

        self.assertEqual(_infer_sample_type(pred, []), 'watch')


if __name__ == '__main__':
    unittest.main()
