import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class IntelligenceCacheStoreTest(unittest.TestCase):
    def test_save_and_load_normalized_items_and_source_status(self):
        from core.intelligence.cache_store import (
            load_normalized,
            load_source_status,
            save_normalized,
            update_source_status,
        )
        from core.intelligence.models import NormalizedIntelItem

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {'ALDEBARAN_INTELLIGENCE_CACHE_DIR': tmp}):
                item = NormalizedIntelItem(
                    id='news:1',
                    source_id='existing:stock_news',
                    title='Demand text',
                    summary='Text-only demand evidence.',
                    layer='demand',
                    direction='neutral',
                    related_codes=['600584'],
                    related_names=['Stock A'],
                    related_sectors=[],
                    evidence_type='news',
                    published_at='2026-07-03T09:00:00',
                    fetched_at='2026-07-03T10:00:00',
                    url='https://example.com/news',
                    trust_level='media',
                    confidence=4,
                    time_windows=['short'],
                    freshness='fresh',
                )

                save_normalized('existing:stock_news', [item])
                loaded = load_normalized('existing:stock_news')
                self.assertEqual(len(loaded), 1)
                self.assertEqual(loaded[0].id, item.id)
                self.assertEqual(loaded[0].freshness, 'fresh')

                update_source_status('existing:stock_news', True, 1)
                update_source_status('requests:bad', False, 0, error='timeout')
                status = load_source_status()

                self.assertEqual(status['existing:stock_news']['status'], 'ok')
                self.assertEqual(status['existing:stock_news']['last_item_count'], 1)
                self.assertEqual(status['requests:bad']['status'], 'error')
                self.assertEqual(status['requests:bad']['last_error'], 'timeout')
                self.assertEqual(status['requests:bad']['error_count'], 1)

    def test_freshness_marks_fresh_stale_and_unknown(self):
        from core.intelligence.freshness import freshness_for_item, is_stale
        from core.intelligence.models import NormalizedIntelItem, SourceSpec

        spec = SourceSpec(
            source_id='existing:stock_news',
            name='Stock News',
            layer='news_event',
            method='api',
            parser='test',
            ttl_hours=3,
        )
        now = datetime(2026, 7, 3, 12, 0, 0)

        fresh = _item('2026-07-03T10:00:00')
        stale = _item('2026-07-02T10:00:00')
        unknown = _item(None)

        self.assertFalse(is_stale(fresh.published_at, spec.ttl_hours, now=now))
        self.assertTrue(is_stale(stale.published_at, spec.ttl_hours, now=now))
        self.assertIsNone(is_stale(unknown.published_at, spec.ttl_hours, now=now))
        self.assertEqual(freshness_for_item(fresh, spec, now=now), 'fresh')
        self.assertEqual(freshness_for_item(stale, spec, now=now), 'stale')
        self.assertEqual(freshness_for_item(unknown, spec, now=now), 'unknown')


def _item(published_at):
    from core.intelligence.models import NormalizedIntelItem

    return NormalizedIntelItem(
        id='item',
        source_id='existing:stock_news',
        title='Title',
        summary='Summary',
        layer='news_event',
        direction='neutral',
        related_codes=['600584'],
        related_names=['Stock A'],
        related_sectors=[],
        evidence_type='news',
        published_at=published_at,
        fetched_at='2026-07-03T12:00:00',
        url='',
        trust_level='media',
        confidence=4,
    )


if __name__ == '__main__':
    unittest.main()
