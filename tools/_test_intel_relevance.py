import unittest
from datetime import date

from core.intel_matcher import filter_relevant_intel, score_intel_event
from core.intel_models import IntelEvent


def _event(
    event_id,
    title,
    *,
    level='info',
    timestamp='2026-06-18',
    related_sectors=None,
    summary='',
    interpretation='',
):
    return IntelEvent(
        id=event_id,
        title=title,
        category='ai',
        level=level,
        direction='bullish',
        summary=summary,
        interpretation=interpretation,
        related_sectors=related_sectors or [],
        timestamp=timestamp,
        source='test',
    )


class IntelRelevanceTest(unittest.TestCase):
    def test_direct_recent_event_ranks_above_old_sector_event(self):
        old_sector = _event(
            'old',
            '锂电产业链旧闻继续发酵',
            level='critical',
            timestamp='2026-06-10',
            related_sectors=['锂电'],
        )
        recent_direct = _event(
            'recent',
            '宁德时代发布新电池订单',
            level='important',
            timestamp='2026-06-18',
            related_sectors=['新能源'],
        )

        events = [old_sector, recent_direct]
        ranked = filter_relevant_intel(
            events, '300750', '宁德时代', sector_hints=['锂电'], max_n=2,
            today=date(2026, 6, 18),
        )

        self.assertEqual([ev.id for ev in ranked], ['recent', 'old'])
        self.assertGreater(
            score_intel_event(recent_direct, '300750', '宁德时代', ['锂电'], today=date(2026, 6, 18)),
            score_intel_event(old_sector, '300750', '宁德时代', ['锂电'], today=date(2026, 6, 18)),
        )

    def test_unrelated_event_is_not_returned(self):
        unrelated = _event(
            'u',
            '银行板块分红方案落地',
            level='critical',
            timestamp='2026-06-18',
            related_sectors=['银行'],
        )

        ranked = filter_relevant_intel(
            [unrelated], '300750', '宁德时代', sector_hints=['锂电'], max_n=3,
            today=date(2026, 6, 18),
        )

        self.assertEqual(ranked, [])


if __name__ == '__main__':
    unittest.main()
