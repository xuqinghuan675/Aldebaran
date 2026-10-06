import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class IntelligenceEvidenceAdapterTest(unittest.TestCase):
    def test_normalized_items_become_real_evidence_nodes_and_edges(self):
        from core.evidence_seed_builder import build_evidence_pack_from_seed
        from core.intelligence.models import NormalizedIntelItem

        demand = NormalizedIntelItem(
            id='collector:demand:1',
            source_id='existing:stock_news',
            title='Demand order recovered',
            summary='Public text says downstream demand and order visibility improved.',
            layer='demand',
            direction='bullish',
            related_codes=['600584'],
            related_names=['Stock A'],
            related_sectors=['Advanced Packaging'],
            evidence_type='news',
            published_at='2026-07-03',
            fetched_at='2026-07-03T10:00:00',
            url='https://example.com/demand',
            trust_level='media',
            confidence=6,
            time_windows=['short', 'swing'],
            freshness='fresh',
            raw_ref='raw-demand',
        )
        order = NormalizedIntelItem(
            id='collector:order:1',
            source_id='existing:stock_news',
            title='Major customer contract',
            summary='Public announcement says a major customer order contract was signed.',
            layer='order_contract',
            direction='bullish',
            related_codes=['600584'],
            related_names=['Stock A'],
            related_sectors=[],
            evidence_type='order_contract',
            published_at='2026-07-03',
            fetched_at='2026-07-03T10:00:00',
            url='https://example.com/order',
            trust_level='primary',
            confidence=7,
            current_value=1.2,
            unit='yi',
            time_windows=['short'],
            freshness='fresh',
        )

        pack = build_evidence_pack_from_seed({
            'subject': {'code': '600584', 'name': 'Stock A', 'type': 'stock'},
            'normalized_intel_items': [demand.to_dict(), order.to_dict()],
            'intel_events': [],
            'stock_news': [],
            'context': {},
        })

        node_by_id = {node.id: node for node in pack.nodes}
        demand_nodes = [node for node in pack.nodes if node.layer == 'demand' and node.type != 'missing_evidence']
        order_nodes = [node for node in pack.nodes if node.type == 'order_contract']

        self.assertTrue(demand_nodes)
        self.assertTrue(order_nodes)
        self.assertEqual(demand_nodes[0].source, 'existing:stock_news')
        self.assertEqual(demand_nodes[0].freshness, 'fresh')
        self.assertGreater(demand_nodes[0].analysis_weight, 0)
        self.assertEqual(order_nodes[0].metrics['current_value'], 1.2)
        self.assertEqual(order_nodes[0].metrics['unit'], 'yi')
        self.assertNotIn('missing:600584:demand', node_by_id)
        self.assertNotIn('missing:600584:order_contract', node_by_id)
        self.assertTrue(any(edge.source == demand_nodes[0].id for edge in pack.edges))
        self.assertTrue(any(edge.source == order_nodes[0].id for edge in pack.edges))

    def test_adapter_filters_forbidden_technical_items(self):
        from core.evidence_seed_builder import FORBIDDEN_INTELLIGENCE_TERMS, build_evidence_pack_from_seed
        from core.intelligence.models import NormalizedIntelItem

        item = NormalizedIntelItem(
            id='collector:technical:1',
            source_id='existing:stock_news',
            title='MACD buy signal',
            summary='Technical-analysis text must not enter the intelligence graph.',
            layer='news_event',
            direction='bullish',
            related_codes=['600584'],
            related_names=['Stock A'],
            related_sectors=[],
            evidence_type='news',
            published_at='2026-07-03',
            fetched_at='2026-07-03T10:00:00',
            url='https://example.com/technical',
            trust_level='media',
            confidence=4,
            time_windows=['short'],
            freshness='fresh',
        )

        pack = build_evidence_pack_from_seed({
            'subject': {'code': '600584', 'name': 'Stock A', 'type': 'stock'},
            'normalized_intel_items': [item.to_dict()],
            'intel_events': [],
            'stock_news': [],
            'context': {},
        })

        rendered = json.dumps(pack.graph_json, ensure_ascii=False)
        for forbidden in FORBIDDEN_INTELLIGENCE_TERMS:
            self.assertNotIn(forbidden, rendered)

    def test_stock_graph_seed_uses_collector_batch_for_normalized_items(self):
        from core.intelligence.models import NormalizedIntelItem
        from core.intelligence_feed_service import build_stock_graph_seed

        item = NormalizedIntelItem(
            id='collector:financial:1',
            source_id='existing:fundamentals',
            title='Financial summary',
            summary='Collector supplied financial summary.',
            layer='financial',
            direction='neutral',
            related_codes=['600584'],
            related_names=['Stock A'],
            related_sectors=[],
            evidence_type='financial_metric',
            published_at=None,
            fetched_at='2026-07-03T10:00:00',
            url='',
            trust_level='secondary',
            confidence=5,
            time_windows=['swing', 'mid'],
            freshness='unknown',
        )

        with patch('core.intelligence_feed_service._load_stock_context', return_value={}), \
                patch('core.intelligence_feed_service._infer_sectors', return_value=[]), \
                patch('core.intelligence_feed_service._fetch_stock_news', return_value=[]), \
                patch('core.intelligence_feed_service._load_holdings', return_value=[]), \
                patch('core.intelligence_feed_service._load_watchlist', return_value=[]), \
                patch('core.intelligence_feed_service.get_relevant_events_for_stock', return_value=[]), \
                patch('core.intelligence_feed_service.collect_builtin_sources') as collect:
            collect.return_value = {
                'normalized_items': [item],
                'source_status': {'existing:fundamentals': {'status': 'ok', 'normalized_count': 1}},
                'coverage_summary': {
                    'real_node_count': 1,
                    'by_layer': {'financial': 1},
                    'missing_layers': ['cost'],
                    'freshness': {'unknown': 1},
                    'sources': {'ok': 1},
                },
            }
            seed = build_stock_graph_seed('600584', 'Stock A')

        self.assertEqual(seed['normalized_intel_items'][0]['id'], 'collector:financial:1')
        self.assertEqual(
            seed['source_status']['collector_sources']['existing:fundamentals']['status'],
            'ok',
        )
        self.assertEqual(seed['source_status']['coverage_summary']['real_node_count'], 1)
        self.assertEqual(seed['source_status']['coverage_summary']['missing_layers'], ['cost'])
        collect.assert_called_once()

    def test_normalized_items_prevent_duplicate_legacy_variable_nodes(self):
        from core.evidence_seed_builder import build_evidence_pack_from_seed
        from core.intelligence.models import NormalizedIntelItem

        item = NormalizedIntelItem(
            id='collector:cost:1',
            source_id='existing:thin_layer_context',
            title='Cost pressure eased',
            summary='Raw material cost pressure eased versus last month.',
            layer='cost',
            direction='bullish',
            related_codes=['600584'],
            related_names=['Stock A'],
            related_sectors=[],
            evidence_type='cost_factor',
            published_at='2026-07-03',
            fetched_at='2026-07-03T10:00:00',
            url='',
            trust_level='secondary',
            confidence=6,
            metric_name='raw_material_cost_index',
            current_value=92.5,
            unit='index',
            time_windows=['swing', 'mid'],
            freshness='fresh',
        )

        pack = build_evidence_pack_from_seed({
            'subject': {'code': '600584', 'name': 'Stock A', 'type': 'stock'},
            'normalized_intel_items': [item.to_dict()],
            'intel_events': [],
            'stock_news': [],
            'variable_snapshots': {
                'cost': {
                    'summary': 'Raw material cost pressure eased versus last month.',
                    'direction': 'bullish',
                },
            },
            'context': {},
        })

        cost_nodes = [
            node for node in pack.nodes
            if node.layer == 'cost' and node.type != 'missing_evidence'
        ]
        self.assertEqual(len(cost_nodes), 1)
        self.assertTrue(cost_nodes[0].id.startswith('intel:existing:thin_layer_context:'))

    def test_periodic_field_summary_has_summary_node_and_raw_support_edges(self):
        from core.evidence_seed_builder import build_evidence_pack_from_seed
        from core.intelligence.models import NormalizedIntelItem

        raw_item = NormalizedIntelItem(
            id='periodic:business:raw1',
            source_id='requests:company_announcements',
            title='Stock A annual report segment row',
            summary='Segment revenue increased year over year.',
            layer='financial',
            direction='neutral',
            related_codes=['600584'],
            related_names=['Stock A'],
            related_sectors=[],
            evidence_type='periodic_report',
            published_at='2026-04-20',
            fetched_at='2026-07-05T10:00:00',
            url='https://example.com/report.pdf',
            trust_level='primary',
            confidence=8,
            category='periodic_report_body',
            raw_ref='periodic_report:annual:business_segments:raw1',
            metadata={'field': 'business_segments', 'table_quality_score': 0.9},
        )
        summary_item = NormalizedIntelItem(
            id='periodic_field_summary:600584:business_segments:2026-04-20',
            source_id='requests:company_announcements',
            title='business_segments field summary',
            summary='business_segments: 1 supporting evidence rows; direction positive.',
            layer='financial',
            direction='positive',
            related_codes=['600584'],
            related_names=['Stock A'],
            related_sectors=[],
            evidence_type='periodic_field_summary',
            published_at='2026-04-20',
            fetched_at='2026-07-05T10:00:00',
            url='https://example.com/report.pdf',
            trust_level='primary',
            confidence=8,
            category='periodic_field_summary',
            raw_ref='field_summary:business_segments:2026-04-20',
            metadata={
                'field': 'business_segments',
                'evidence_count': 1,
                'top_evidence_ids': ['periodic:business:raw1'],
                'top_raw_refs': ['periodic_report:annual:business_segments:raw1'],
                'average_quality_score': 0.9,
                'strongest_source_title': 'Stock A annual report',
                'source_period': '2026-04-20',
                'direction': 'positive',
                'confidence': 8,
                'missing_reason': '',
                'conflict_reason': '',
                'summary_text': 'business_segments summary',
            },
        )

        pack = build_evidence_pack_from_seed({
            'subject': {'code': '600584', 'name': 'Stock A', 'type': 'stock'},
            'normalized_intel_items': [raw_item.to_dict(), summary_item.to_dict()],
            'intel_events': [],
            'stock_news': [],
            'context': {},
        })

        summary_nodes = [node for node in pack.nodes if node.type == 'periodic_field_summary']
        self.assertEqual(len(summary_nodes), 1)
        summary_node = summary_nodes[0]
        self.assertEqual(summary_node.direction, 'positive')
        self.assertEqual(summary_node.metrics['field_summary']['field'], 'business_segments')
        self.assertTrue(any(
            edge.source == summary_node.id and edge.target == 'stock:600584' and edge.relation == 'impacts'
            for edge in pack.edges
        ))
        self.assertTrue(any(
            edge.target == summary_node.id and edge.relation in {'supports', 'derived_from', 'derives_from'}
            for edge in pack.edges
        ))


if __name__ == '__main__':
    unittest.main()
