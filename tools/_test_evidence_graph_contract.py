import json
import sys
import unittest
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.evidence_ai import analyze_evidence_pack
from core.evidence_graph import EvidenceEdge, EvidenceNode, EvidencePack
from core.evidence_seed_builder import (
    FORBIDDEN_INTELLIGENCE_TERMS,
    build_evidence_pack_from_seed,
)
from core.intel_models import IntelEvent


def _event(event_id, title, *, summary='', direction='bullish', level='important'):
    return IntelEvent(
        id=event_id,
        title=title,
        category='policy',
        level=level,
        direction=direction,
        summary=summary or title,
        interpretation='',
        related_sectors=['先进封装'],
        timestamp='2026-07-03',
        source='test',
    )


class EvidenceGraphContractTest(unittest.TestCase):
    def test_evidence_pack_serializes_graph_json_with_separate_weights(self):
        nodes = [
            EvidenceNode(
                id='stock:600584',
                label='长电科技',
                type='stock',
                layer='company',
                display_weight=24,
                analysis_weight=1.0,
                summary='当前分析对象',
            ),
            EvidenceNode(
                id='missing:600584:cost',
                label='成本证据缺失',
                type='missing_evidence',
                layer='cost',
                forecast_role='missing',
                display_weight=8,
                analysis_weight=0.0,
                summary='未找到足够成本结构证据。',
            ),
        ]
        edges = [
            EvidenceEdge(
                source='missing:600584:cost',
                target='stock:600584',
                relation='missing',
                layer='cost',
                display_weight=2,
                analysis_weight=0.0,
            )
        ]
        pack = EvidencePack.from_nodes(
            subject_code='600584',
            subject_name='长电科技',
            subject_type='stock',
            nodes=nodes,
            edges=edges,
        )

        json.dumps(pack.graph_json, ensure_ascii=False)
        self.assertEqual(pack.graph_json['meta']['subject_code'], '600584')
        self.assertEqual(pack.graph_json['meta']['node_count'], 2)
        self.assertIn('display_weight', pack.graph_json['nodes'][0])
        self.assertIn('analysis_weight', pack.graph_json['nodes'][0])
        self.assertIn('cost', pack.missing_layers)

    def test_seed_builder_generates_stock_graph_without_technical_analysis_nodes(self):
        seed = {
            'subject': {'code': '600584', 'name': '长电科技', 'type': 'stock', 'price': 38.2},
            'sectors': ['先进封装', '半导体'],
            'intel_events': [
                _event('policy-1', '先进封装产业政策落地', summary='政策支持封测产业链。'),
                _event('tech-1', 'MACD 金叉提示买点', summary='技术分析内容应被排除。'),
            ],
            'stock_news': [
                {'title': '长电科技公告先进封装项目进展', 'date': '2026-07-03', 'source': '公告', 'content': '项目按计划推进。'},
                {'title': 'K线突破压力位', 'date': '2026-07-03', 'source': '技术分析', 'content': '应被排除。'},
            ],
            'context': {
                'fundamental_summary': '财务摘要：营收结构等待进一步拆分。',
                'money_flow_summary': '近 5 日主力资金净流入。',
                'flow_profile': {
                    'available': True,
                    'days': 5,
                    'main_5d': 23000000,
                    'main_streak': 3,
                    'divergence': 'main_in_small_out',
                    'last': {'date': '2026-07-03', 'main': 8000000},
                },
                'emotion': {'zt': 62, 'dt': 9, 'up': 3200, 'down': 1800},
                'global': {'summary': '外围流动性中性。'},
                'market_phase': 'risk_on',
                'public_fund_evidence': {'available': True, 'lhb': {'summary': '龙虎榜席位活跃'}, 'hsgt': {}},
                'restricted_release': {'summary': '未来 30 日无大额解禁'},
                'hot_rank': {'rank': 88, 'change': '+12'},
            },
            'holdings': [{'code': '600584', 'name': '长电科技'}],
            'watchlist': [{'code': '600584', 'name': '长电科技', 'theme': '先进封装'}],
        }

        pack = build_evidence_pack_from_seed(seed)
        node_types = {node.type for node in pack.nodes}
        node_ids = {node.id for node in pack.nodes}

        self.assertIn('stock', node_types)
        self.assertIn('sector', node_types)
        self.assertIn('fund_flow', node_types)
        self.assertIn('hot_money_seat', node_types)
        self.assertIn('financial_metric', node_types)
        self.assertIn('missing_evidence', node_types)
        self.assertIn('missing:600584:cost', node_ids)
        self.assertIn('missing:600584:demand', node_ids)
        self.assertTrue(all(node.display_weight >= 0 for node in pack.nodes))
        self.assertTrue(all(node.analysis_weight >= 0 for node in pack.nodes))

        rendered = json.dumps(pack.graph_json, ensure_ascii=False)
        for forbidden in FORBIDDEN_INTELLIGENCE_TERMS:
            self.assertNotIn(forbidden, rendered)

    def test_seed_builder_adds_existing_trade_behavior_and_missing_thin_layers(self):
        seed = {
            'subject': {'code': '600584', 'name': 'Stock A', 'type': 'stock'},
            'sectors': [],
            'intel_events': [],
            'stock_news': [
                {
                    'title': 'Stock A signed a major customer contract',
                    'date': '2026-07-03',
                    'source': 'announcement',
                    'content': 'The public announcement says the company signed a new order contract.',
                },
            ],
            'context': {
                'public_fund_evidence': {
                    'available': True,
                    'lhb': {'hit': False},
                    'hsgt': {'hit': False},
                    'dzjy': {
                        'hit': True,
                        'rows': 2,
                        'premium_pct_avg': -3.2,
                        'amount_yi': 1.6,
                    },
                },
            },
        }

        pack = build_evidence_pack_from_seed(seed)
        node_types = {node.type for node in pack.nodes}
        node_ids = {node.id for node in pack.nodes}
        missing_ids = {node.id for node in pack.nodes if node.type == 'missing_evidence'}

        self.assertIn('block_trade', node_types)
        self.assertIn('order_contract', node_types)
        self.assertIn('missing:600584:inventory', missing_ids)
        self.assertIn('missing:600584:capacity', missing_ids)
        self.assertIn('missing:600584:competition', missing_ids)
        self.assertNotIn('missing:600584:margin_financing', missing_ids)
        self.assertNotIn('trading_behavior', pack.missing_layers)
        self.assertNotIn('missing:600584:order_contract', node_ids)

    def test_evidence_ai_stays_inside_pack_and_exposes_required_fields(self):
        pack = build_evidence_pack_from_seed({
            'subject': {'code': '600584', 'name': '长电科技', 'type': 'stock'},
            'sectors': ['先进封装'],
            'intel_events': [_event('policy-1', '先进封装产业政策落地')],
            'stock_news': [],
            'context': {'flow_profile': {'available': False}},
            'holdings': [],
            'watchlist': [],
        })

        analysis = analyze_evidence_pack('', pack)
        data = asdict(analysis)

        self.assertIn(data['intel_stage'], {
            'none', 'weak_watch', 'candidate', 'strong',
            'fund_confirmed', 'risk', 'conflict',
        })
        self.assertIn('dominant_variables', data)
        self.assertIn('bullish_chain', data)
        self.assertIn('bearish_chain', data)
        self.assertIn('missing_evidence', data)
        self.assertIn('next_watch', data)

        rendered = json.dumps(data, ensure_ascii=False)
        for forbidden in FORBIDDEN_INTELLIGENCE_TERMS:
            self.assertNotIn(forbidden, rendered)


if __name__ == '__main__':
    unittest.main()
