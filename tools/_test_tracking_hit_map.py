import unittest

from core.tracking import _agent_hit_map


def _task(stance):
    return {'pred_snapshot': {'_agent_technical': {'stance': stance, 'confidence': 7}}}


class AgentHitMapTest(unittest.TestCase):
    def _status(self, stance, raw):
        return _agent_hit_map(_task(stance), raw)['technical']['status']

    def test_bearish_hits_when_price_falls(self):
        self.assertEqual(self._status('bearish', -4.57), '命中')

    def test_bearish_misleads_when_price_rises(self):
        self.assertEqual(self._status('bearish', 4.57), '误导')

    def test_bullish_hits_when_price_rises(self):
        self.assertEqual(self._status('bullish', 4.57), '命中')

    def test_bullish_misleads_when_price_falls(self):
        self.assertEqual(self._status('bullish', -4.57), '误导')

    def test_bearish_flat_is_unrealized(self):
        self.assertEqual(self._status('bearish', -0.5), '未兑现')

    def test_bullish_flat_is_unrealized(self):
        self.assertEqual(self._status('bullish', 0.5), '未兑现')

    def test_neutral_hits_when_flat(self):
        self.assertEqual(self._status('neutral', 0.8), '命中')

    def test_neutral_misleads_when_moves(self):
        self.assertEqual(self._status('neutral', 1.8), '误导')


if __name__ == '__main__':
    unittest.main()
