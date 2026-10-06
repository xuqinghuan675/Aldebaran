import re
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.agents import fundflow_agent
from core.agents import macro_agent
from core.agents import news_event_agent
from core.agents import technical_agent
from core.agents import synthesis as synthesis_agent
from core.kline_provider import build_technical_profile
from core.predictor import derive_opportunity_profile


class AnalysisGroundingTest(unittest.TestCase):
    def test_analysis_agents_do_not_use_flash_model(self):
        agent_files = [
            'core/agents/macro_agent.py',
            'core/agents/company_agent.py',
            'core/agents/fundflow_agent.py',
            'core/agents/news_event_agent.py',
            'core/agents/technical_agent.py',
            'core/agents/synthesis.py',
            'core/agents/decision.py',
        ]
        offenders = []
        root = Path(__file__).resolve().parents[1]
        for rel in agent_files:
            text = (root / rel).read_text(encoding='utf-8')
            if 'call_flash' in text:
                offenders.append(rel)
        self.assertEqual([], offenders)

    def test_fundflow_fallback_preserves_single_day_data(self):
        context = {
            'money_flow_summary': '\n'.join([
                '▶ 资金流向（寒武纪 688256，2026-06-12）：',
                '  主力净流入: +0.77 亿（大单+超大单）',
                '  超大单: -3.04 亿',
                '  散户净流入: -0.03 亿',
                '  趋势: 历史数据不足',
            ]),
            'flow_profile': {
                'available': True,
                'days': 1,
                'main_3d': 77477376.0,
                'main_5d': 77477376.0,
                'small_5d': -3275028.0,
                'xlarge_5d': -303906304.0,
                'main_streak': 1,
                'divergence': 'main_in_small_out',
            },
        }

        original_call = fundflow_agent.call_pro
        try:
            fundflow_agent.call_pro = lambda *a, **k: ('not json', '')
            result = fundflow_agent.run('dummy-key', '688256', '寒武纪', context)
        finally:
            fundflow_agent.call_pro = original_call

        combined = result['summary'] + ' ' + ' '.join(
            str(item.get('value', '')) for item in result.get('evidence', [])
        )
        self.assertNotIn('数据缺失', combined)
        self.assertIn('+0.77 亿', combined)
        self.assertIn('仅1日', combined)
        self.assertLessEqual(result['confidence'], 5)

    def test_macro_fallback_does_not_report_missing_breadth_as_zero(self):
        context = {
            'emotion': {
                'zt': 85,
                'dt': 1,
                'zb': 28,
                'real_zt': 0,
                'real_dt': 0,
                'up': 0,
                'down': 0,
                '_fallback': True,
            },
            'kline_summary': 'MA5=53.3 MA20=53.5',
        }

        original_call = macro_agent.call_pro
        try:
            macro_agent.call_pro = lambda *a, **k: ('not json', '')
            result = macro_agent.run('dummy-key', '601318', '中国平安', 52.76, context, [])
        finally:
            macro_agent.call_pro = original_call

        combined = ' '.join(str(item.get('value', '')) for item in result.get('evidence', []))
        self.assertNotIn('上涨0家', combined)
        self.assertNotIn('下跌0家', combined)
        self.assertIn('涨停85家', combined)
        self.assertIn('跌停1家', combined)
        self.assertIn('上涨/下跌家数缺失', combined)

    def test_f_layer_prompt_restricts_unverified_percentage_math(self):
        prompt = synthesis_agent._SYSTEM
        self.assertIn('禁止自行换算占比', prompt)
        self.assertIn('不要写出百分比', prompt)

    def test_news_prompt_treats_empty_local_news_as_source_boundary(self):
        self.assertIn('本地新闻源未返回', news_event_agent._SYSTEM)
        prompt = news_event_agent._build_variable_data('600887', '伊利股份', [], {})
        self.assertIn('本地新闻源未返回', prompt)
        self.assertNotIn('个股新闻：暂无', prompt)

    def test_news_fallback_does_not_claim_no_real_world_announcements(self):
        original_call = news_event_agent.call_pro
        try:
            news_event_agent.call_pro = lambda *a, **k: ('not json', '')
            result = news_event_agent.run('dummy-key', '600887', '伊利股份', [], {})
        finally:
            news_event_agent.call_pro = original_call

        combined = result['summary'] + ' ' + ' '.join(
            str(item.get('value', '')) for item in result.get('evidence', [])
        )
        self.assertIn('本地新闻源未返回', combined)
        self.assertNotIn('无专属新闻或公告', combined)
        self.assertNotIn('无个股新闻', combined)

    def test_news_fallback_summary_consistent_with_evidence(self):
        from core.agents import news_event_agent
        news = [
            {'title': '中际旭创澄清受汇兑影响业绩暴雷传闻不实', 'date': '2026-06-14'},
            {'title': '中际旭创选举刘圣为董事长', 'date': '2026-06-12'},
        ]
        result = news_event_agent._event_fallback(news, {})
        ev_text = ' '.join(str(e.get('value', '')) for e in result.get('evidence', []))
        self.assertIn('中际旭创', ev_text)
        self.assertNotIn('本地新闻源未返回', result['summary'])

        empty = news_event_agent._event_fallback([], {})
        self.assertIn('本地新闻源未返回', empty['summary'])

    def test_company_fallback_carries_annualized_roe(self):
        from core.agents import company_agent
        summary = (
            "财务摘要(报告期: 2026Q1):\n"
            "  主营收入: 2664.78亿（Q1）\n"
            "  净利润: 293.42亿（Q1）\n"
            "  净利率: 11.0%\n"
            "  ROE: 2.1%（Q1）\n"
            "  ROE年化估算: ≈8.4%（简单年化，仅供参考，勿当作实际年报ROE）\n"
            "  资产负债率: 35.0%\n"
        )
        result = company_agent._stock_fallback_from_summary('600941', '中国移动', summary, ['通信'])
        combined = ' '.join(str(e.get('value', '')) for e in result.get('evidence', []))
        self.assertIn('2.1%', combined)
        self.assertIn('年化', combined)
        self.assertIn('8.4', combined)

    def test_intraday_trigger_failure_is_marked_as_risk(self):
        rows = []
        for i in range(60):
            rows.append({
                'open': 100.0,
                'high': 101.0,
                'low': 99.0,
                'close': 100.0,
                'volume': 1000.0,
            })
        rows[-5:] = [
            {'open': 100.0, 'high': 101.0, 'low': 99.0, 'close': 100.0, 'volume': 1000.0},
            {'open': 100.0, 'high': 101.0, 'low': 99.0, 'close': 100.0, 'volume': 1000.0},
            {'open': 100.0, 'high': 101.0, 'low': 99.0, 'close': 100.0, 'volume': 1000.0},
            {'open': 100.0, 'high': 101.0, 'low': 99.0, 'close': 100.0, 'volume': 1000.0},
            {'open': 101.0, 'high': 102.0, 'low': 98.0, 'close': 99.0, 'volume': 1200.0},
        ]
        df = pd.DataFrame(rows)
        df.index = pd.to_datetime(pd.date_range('2026-01-01', periods=len(df), freq='D'))

        profile = build_technical_profile('000001', df=df)

        self.assertEqual('candidate', profile['status'])
        self.assertLess(profile['close'], profile['entry_trigger'])
        self.assertLessEqual(profile['entry_trigger'], 102.0)
        self.assertIn('冲击触发位失败', profile['risk_flags'])

    def test_public_build_has_no_customer_material_dependency(self):
        root = Path(__file__).resolve().parents[1]
        build = (root / 'tools' / 'build.ps1').read_text(encoding='utf-8')
        self.assertNotIn('customer_materials', build.lower())
        self.assertNotIn('sales_materials', build.lower())


class OpportunityTextGroundingTest(unittest.TestCase):
    def _ctx(self, tech=None, flow=None):
        tp = {
            'available': True, 'close': 10.0, 'status': 'candidate',
            'setup_name': 'pullback_buy', 'trend_stage': 'consolidation',
            'volume_ratio': 0.8,
            'ma': {'ma5': 10.3, 'ma20': 10.3, 'ma20_slope_pct': -0.2},
            'entry_trigger': 10.5, 'fail_level': 9.8, 'target_level': 11.5,
        }
        tp.update(tech or {})
        fp = {'available': True, 'days': 5, 'main_5d': 0.0, 'small_5d': 0.0}
        fp.update(flow or {})
        ctx = {'_kline_last_close': tp['close'], 'technical_profile': tp, 'flow_profile': fp}
        setup = {'setup_name': tp['setup_name'], 'status': tp['status']}
        return ctx, setup

    def test_c_grade_dipbuy_no_attack_buy_language(self):
        ctx, setup = self._ctx()
        prof = derive_opportunity_profile(ctx, setup)
        self.assertEqual(prof['opportunity_grade'], 'C')
        for word in ('低吸', '持有', '买点'):
            self.assertNotIn(word, prof['attack_level'])

    def test_d_grade_no_attack_buy_language(self):
        ctx, setup = self._ctx(tech={'available': False})
        prof = derive_opportunity_profile(ctx, setup)
        self.assertEqual(prof['opportunity_grade'], 'D')
        for word in ('低吸', '持有', '买点'):
            self.assertNotIn(word, prof['attack_level'])

    def test_single_day_inflow_not_called_structural_positive(self):
        ctx, setup = self._ctx(flow={'days': 1, 'main_5d': 1e8, 'small_5d': -1e8})
        prof = derive_opportunity_profile(ctx, setup)
        self.assertNotIn('资金结构偏正面', '|'.join(prof['evidence']))

    def test_liquidity_gate_C_drops_structure_in_place_evidence(self):
        ctx, setup = self._ctx(
            tech={'close': 10.5, 'setup_name': 'trend_breakout', 'status': 'triggered',
                  'trend_stage': 'uptrend', 'volume_ratio': 1.6,
                  'ma': {'ma5': 10.0, 'ma20': 10.0, 'ma20_slope_pct': 0.5},
                  'entry_trigger': 10.0, 'fail_level': 9.8, 'target_level': 12.5},
            flow={'days': 5, 'main_5d': -1e8, 'small_5d': 1e8})
        prof = derive_opportunity_profile(ctx, setup)
        self.assertEqual(prof['opportunity_grade'], 'C')
        self.assertNotIn('短线结构在位', '|'.join(prof['evidence']))


class TechnicalTextConsistencyTest(unittest.TestCase):
    def test_drops_ma5_contradiction_from_llm_summary(self):
        # 当前价 10.5 已在 MA5(10.0) 上方，LLM 却写"未站上MA5" → 删除 LLM 追加句，保留本地画像
        context = {'technical_profile': {
            'available': True, 'close': 10.5, 'status': 'candidate',
            'ma': {'ma5': 10.0, 'ma20': 10.0}}}
        parsed = {'confidence': 6,
                  'summary': '短线偏多，但当前未站上MA5，需要等待放量确认。',
                  'evidence': [], 'counter_evidence': []}
        merged = technical_agent._merge_local_profile(parsed, 10.5, context)
        self.assertNotIn('未站上MA5', merged['summary'])

    def test_keeps_consistent_llm_summary(self):
        context = {'technical_profile': {
            'available': True, 'close': 10.5, 'status': 'candidate',
            'ma': {'ma5': 10.0, 'ma20': 10.0}}}
        parsed = {'confidence': 6,
                  'summary': '短线已站上MA5，量能温和，关注能否放量延续。',
                  'evidence': [], 'counter_evidence': []}
        merged = technical_agent._merge_local_profile(parsed, 10.5, context)
        self.assertIn('站上MA5', merged['summary'])


class FundflowDiagnosticsTest(unittest.TestCase):
    def _run_fallback(self):
        context = {
            'money_flow_summary': '主力净流入: +0.77 亿（仅1日）',
            'flow_profile': {'available': True, 'days': 1,
                             'main_5d': 77000000.0, 'small_5d': -3000000.0,
                             'xlarge_5d': -30000000.0},
        }
        original = fundflow_agent.call_pro
        try:
            fundflow_agent.call_pro = lambda *a, **k: ('not json', '')
            return fundflow_agent.run('k', '688256', '寒武纪', context)
        finally:
            fundflow_agent.call_pro = original

    def test_parse_failure_not_in_market_evidence(self):
        result = self._run_fallback()
        blob = ' '.join(
            str(item.get('name', '')) + str(item.get('value', ''))
            for item in (result.get('evidence') or []) + (result.get('counter_evidence') or [])
        )
        self.assertNotIn('模型解析失败', blob)
        self.assertNotIn('LLM 输出不可用', blob)

    def test_parse_failure_kept_in_diagnostics(self):
        result = self._run_fallback()
        diag = ' '.join(str(x) for x in (result.get('_diagnostics') or []))
        self.assertIn('LLM', diag)


class QuarterTagTest(unittest.TestCase):
    def test_june_is_q1_not_half_year(self):
        # 6月半年报未披露，最新已披露的仍是Q1（旧逻辑误判半年报→年化×2，应×4）
        from core.fundamentals_provider import _quarter_tag_for_month
        self.assertEqual(_quarter_tag_for_month(6), 'Q1')

    def test_disclosure_calendar_mapping(self):
        from core.fundamentals_provider import _quarter_tag_for_month
        self.assertEqual(_quarter_tag_for_month(3), '年报')
        self.assertEqual(_quarter_tag_for_month(4), 'Q1')
        self.assertEqual(_quarter_tag_for_month(7), '半年报')
        self.assertEqual(_quarter_tag_for_month(9), '半年报')
        self.assertEqual(_quarter_tag_for_month(10), '前三季')
        self.assertEqual(_quarter_tag_for_month(12), '前三季')

    def test_period_label_maps_each_tag(self):
        from core.fundamentals_provider import _period_label
        self.assertIn('一季报', _period_label('Q1'))
        self.assertIn('半年报', _period_label('半年报'))
        self.assertIn('前三季', _period_label('前三季'))
        self.assertIn('年报', _period_label('年报'))

    def test_q1_label_has_no_foreign_period_word(self):
        from core.fundamentals_provider import _period_label
        label = _period_label('Q1')
        for foreign in ('半年', '三季'):
            self.assertNotIn(foreign, label)
        self.assertNotIn('年报', label.replace('一季报', ''))


class ShortTermBandTest(unittest.TestCase):
    def test_band_ratio_and_horizon_cap(self):
        from core.kline_provider import short_term_band
        tgt, stop = short_term_band(0.08, 5)
        self.assertLessEqual(tgt, 0.13 + 1e-9)
        self.assertGreaterEqual(tgt / stop, 2.0 - 1e-6)
        tgt3, stop3 = short_term_band(0.08, 3)
        self.assertLessEqual(tgt3, 0.09 + 1e-9)
        self.assertGreaterEqual(tgt3 / stop3, 2.0 - 1e-6)
        tgt_low, stop_low = short_term_band(0.008, 5)
        self.assertLess(tgt_low, 0.06)
        self.assertGreaterEqual(tgt_low / stop_low, 2.0 - 1e-6)

    def test_target_cap_pct_table(self):
        from core.kline_provider import short_term_target_cap_pct
        self.assertAlmostEqual(short_term_target_cap_pct(2), 7.0)
        self.assertAlmostEqual(short_term_target_cap_pct(5), 13.0)

    def test_entry_trigger_and_target_not_runaway(self):
        # 价格在均线上方、上方唯一阻力是远处的20日高点(150) → 老逻辑触发价直接取150(+29%)
        rows = []
        for _ in range(40):
            rows.append({'open': 100.0, 'high': 101.0, 'low': 99.0, 'close': 100.0, 'volume': 1000.0})
        rows.append({'open': 100.0, 'high': 150.0, 'low': 100.0, 'close': 101.0, 'volume': 1000.0})
        for px in (102.0, 104.0, 106.0, 108.0, 110.0, 112.0, 113.0, 114.0, 115.0, 116.0, 116.0, 116.0, 116.0, 116.0, 116.0):
            rows.append({'open': px - 1, 'high': px + 1, 'low': px - 2, 'close': px, 'volume': 1000.0})
        df = pd.DataFrame(rows)
        df.index = pd.to_datetime(pd.date_range('2026-01-01', periods=len(df), freq='D'))
        profile = build_technical_profile('000001', df=df)
        close = profile['close']
        et = profile['entry_trigger']
        fl = profile['fail_level']
        tl = profile['target_level']
        self.assertIsNotNone(et)
        self.assertLessEqual(et, close * 1.08)
        self.assertLessEqual(tl / et - 1, 0.095)
        self.assertGreaterEqual((tl - et) / (et - fl), 1.9)


class BankDebtRatioTest(unittest.TestCase):
    def test_bank_debt_ratio_uses_equity_identity_not_zero(self):
        from core import fundamentals_provider as fp
        bank = {
            'updated_date': 20260331,
            'zongzichan': 120000.0,
            'jingzichan': 10000.0,
            'jinglirun': 300.0,
            'liudongfuzhai': None,
            'changqifuzhai': None,
        }
        orig_fetch, orig_fresh = fp.fetch_finance, fp._check_freshness
        try:
            fp.fetch_finance = lambda code: bank
            fp._check_freshness = lambda data: True
            text = fp.summarize('600036')
        finally:
            fp.fetch_finance, fp._check_freshness = orig_fetch, orig_fresh
        self.assertIn('资产负债率', text)
        m = re.search(r'资产负债率: ([\d.]+)%', text)
        self.assertIsNotNone(m)
        self.assertGreater(float(m.group(1)), 80.0)


class BearishTradeSetupViewTest(unittest.TestCase):
    def _ts(self):
        return {'setup_name': 'oversold_rebound', 'status': 'avoid',
                'entry_trigger': 100.0, 'fail_level': 95.0, 'target_level': 110.0,
                'suggested_horizon_days': 3, 'position_hint': 'x', 'secondary_setups': []}

    def test_bearish_drops_long_target_nulls_rr_aligns_horizon(self):
        from core.predictor import _apply_trade_setup_constraints
        pred = {'direction': 'bearish', 'horizon_days': 5}
        op = {'risk_reward': 4.0, 'opportunity_grade': 'D'}
        _apply_trade_setup_constraints(pred, self._ts(), None, op)
        ts = pred['_trade_setup']
        self.assertIsNone(ts['target_level'])
        self.assertEqual(ts['suggested_horizon_days'], 5)
        self.assertIsNone(pred['risk_reward'])
        self.assertIsNone(pred['_opportunity_profile']['risk_reward'])

    def test_bullish_keeps_target_aligns_horizon(self):
        from core.predictor import _apply_trade_setup_constraints
        pred = {'direction': 'bullish', 'horizon_days': 4}
        op = {'risk_reward': 2.5, 'opportunity_grade': 'B'}
        _apply_trade_setup_constraints(pred, self._ts(), None, op)
        ts = pred['_trade_setup']
        self.assertEqual(ts['target_level'], 110.0)
        self.assertEqual(ts['suggested_horizon_days'], 4)
        self.assertEqual(pred['risk_reward'], 2.5)


class AvoidBlueprintFallbackTest(unittest.TestCase):
    def test_below_price_fail_level_synthesized_above(self):
        from core.predictor import _assemble_task_blueprint
        pred = {
            'direction': 'bearish', 'category': 'avoid', 'opportunity_grade': 'D',
            'final_action': 'watch_only', 'entry_price': 391.55,
            'target_pct': 6.0, 'stop_pct': 3.0, 'fail_level': 380.03, 'horizon_days': 3,
        }
        bp = _assemble_task_blueprint(pred, {})
        self.assertIsNotNone(bp)
        self.assertEqual(bp['category'], 'avoid')
        self.assertGreater(bp['fail_level'], pred['entry_price'])


if __name__ == '__main__':
    unittest.main()
