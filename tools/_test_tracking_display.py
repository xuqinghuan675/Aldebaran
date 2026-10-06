import unittest

from core.tracking_display import (
    format_agent_hit_lines,
    format_open_review_rule_html,
    format_open_review_rule_text,
    format_prediction_advice_lines,
    format_prediction_copy_field_lines,
    format_prediction_plan_line,
    format_retrospective_metric_lines,
    format_directional_metric_lines,
    format_skill_kpi_html,
    sample_type_label,
    target_pct_label,
    tracking_price_label,
)


class TrackingDisplayTest(unittest.TestCase):
    def test_all_samples_are_labeled_virtual_watch(self):
        self.assertEqual(sample_type_label('trade'), '虚拟观察单')
        self.assertEqual(sample_type_label('watch'), '虚拟观察单')

    def test_tracking_price_label_uses_analysis_price_for_observation_tasks(self):
        self.assertEqual(tracking_price_label({'task_class': 'watch'}), '分析时价')
        self.assertEqual(tracking_price_label({'task_class': 'avoid'}), '分析时价')

    def test_tracking_price_label_keeps_entry_price_for_triggered_buy_tasks(self):
        self.assertEqual(tracking_price_label({'task_class': 'buy'}), '入场价')
        self.assertEqual(
            tracking_price_label({'task_class': 'watch', 'converted_from_watch': True}),
            '入场价',
        )

    def test_watch_copy_uses_analysis_price_with_stock_precision(self):
        fields = format_prediction_copy_field_lines({
            'code': '002421',
            'direction': 'neutral',
            'task_class': 'watch',
            'entry_ref': None,
            'entry_price': 4.4,
            'expected_range_pct': [-4, 4],
            'horizon_days': 3,
        })
        joined = '\n'.join(fields)
        self.assertIn('观察基准：4.400', joined)
        self.assertIn('预期区间：-4.0% ~ +4.0%', joined)
        self.assertNotIn('观察基准：—', joined)
        self.assertNotIn('[', joined)

    def test_bullish_watch_prices_are_formatted_to_stock_precision(self):
        task = {
            'code': '000725',
            'task_class': 'watch',
            'direction': 'bullish',
            'horizon_days': 5,
            'pred_snapshot': {'task_blueprint': {
                'category': 'bullish_watch',
                'entry_trigger': 5.57,
                'fail_level': 4.78,
            }},
        }
        line = format_prediction_plan_line(task)
        html = format_open_review_rule_html(task)
        self.assertIn('5.570', line)
        self.assertIn('4.780', line)
        self.assertIn('5.570', html)
        self.assertIn('4.780', html)

    def test_public_review_rules_do_not_show_internal_enums(self):
        for task in (
            {'task_class': 'avoid', 'direction': 'bearish'},
            {'task_class': 'watch', 'direction': 'neutral'},
            {'task_class': 'buy', 'direction': 'bullish'},
            {'task_class': 'watch', 'direction': 'bullish',
             'pred_snapshot': {'task_blueprint': {'category': 'bullish_watch'}}},
        ):
            html = format_open_review_rule_html(task)
            for token in ('avoid', 'watch', 'buy', 'bullish_watch', 'expected_range'):
                self.assertNotIn(token, html)

    def test_agent_hit_lines_translate_direction_enums(self):
        lines = format_agent_hit_lines({
            'agent_hit_map': {
                'technical': {'label': '技术 Agent', 'status': '命中', 'stance': 'bearish'},
                'fundflow': {'label': '资金 Agent', 'status': '误导', 'stance': 'bullish'},
            }
        })
        joined = '\n'.join(lines)
        self.assertIn('立场: 看跌回避', joined)
        self.assertIn('立场: 看多', joined)
        self.assertNotIn('bearish', joined)
        self.assertNotIn('bullish', joined)

    def test_watch_advice_formats_expected_range(self):
        lines = format_prediction_advice_lines(
            {'direction': 'neutral', 'task_class': 'watch', 'expected_range_pct': [-4, 4]},
            period_label='短线（3 日）',
            rating_text='观望',
            entry_text='4.400',
            suit_text='中→',
            action_text='仅观察',
        )
        joined = '\n'.join(lines)
        self.assertIn('预期区间: -4.0% ~ +4.0%', joined)
        self.assertNotIn('[', joined)

    def test_avoid_fall_is_correct_avoidance(self):
        lines = format_directional_metric_lines(
            {'task_class': 'avoid', 'final_pct': -4.57, 'peak_pct': -0.37, 'trough_pct': -5.14},
            {'direction': 'bearish'},
        )
        self.assertIn('回避正确', lines[0])
        self.assertNotIn('空头', lines[0])

    def test_avoid_rise_is_missed(self):
        lines = format_directional_metric_lines(
            {'task_class': 'avoid', 'final_pct': 11.89}, {})
        self.assertIn('踏空', lines[0])

    def test_avoid_drop_target_wording_does_not_become_missed_on_positive_close(self):
        lines = format_directional_metric_lines(
            {'task_class': 'avoid', 'outcome_tier': 'drop_target', 'final_pct': 5.23},
            {},
        )
        self.assertIn('\u76d8\u4e2d\u8dcc\u5e45\u8fbe\u6807', lines[0])
        self.assertNotIn('\u8e0f\u7a7a', lines[0])

    def test_buy_shows_buy_return_and_trigger(self):
        lines = format_directional_metric_lines(
            {'task_class': 'buy', 'final_pct': 5.0, 'trigger': 'target'}, {})
        self.assertIn('看多兑现', lines[0])

    def test_watch_rise_is_missed_chance(self):
        lines = format_directional_metric_lines(
            {'task_class': 'watch', 'final_pct': 6.0}, {})
        self.assertIn('错过机会', lines[0])

    def test_legacy_bearish_without_class_uses_avoid_semantics(self):
        lines = format_directional_metric_lines(
            {'final_pct': -3.0}, {'direction': 'bearish'})
        self.assertNotIn('空头', lines[0])
        self.assertIn('回避', lines[0])

    def test_no_short_selling_wording(self):
        for tc in ('buy', 'avoid', 'watch'):
            lines = format_directional_metric_lines(
                {'task_class': tc, 'final_pct': -3.0}, {})
            for line in lines:
                self.assertNotIn('空头', line)
                self.assertNotIn('做空', line)

    def test_bearish_plan_summary_uses_avoid_semantics(self):
        line = format_prediction_plan_line({
            'direction': 'bearish',
            'task_class': 'avoid',
            'target_pct': 0,
            'stop_pct': 0,
            'horizon_days': 5,
        })
        self.assertIn('回避', line)
        self.assertNotIn('目标跌幅', line)
        self.assertNotIn('0.0%', line)

    def test_neutral_plan_summary_uses_watch_semantics(self):
        line = format_prediction_plan_line({
            'direction': 'neutral',
            'task_class': 'watch',
            'target_pct': 0,
            'stop_pct': 0,
            'horizon_days': 5,
        })
        self.assertIn('观望', line)
        self.assertNotIn('目标跌幅', line)
        self.assertNotIn('0.0%', line)

    def test_bearish_target_label_does_not_say_target_drop(self):
        self.assertNotIn('目标跌幅', target_pct_label('bearish'))

    def test_open_avoid_rule_uses_three_class_wording(self):
        html = format_open_review_rule_html({'task_class': 'avoid', 'direction': 'bearish'})
        self.assertIn('回避', html)
        self.assertIn('踏空', html)
        self.assertNotIn('E1', html)
        self.assertNotIn('最终涨跌幅 vs 目标/止损', html)

    def test_open_bullish_watch_rule_shows_trigger_and_fail(self):
        html = format_open_review_rule_html({
            'task_class': 'watch',
            'direction': 'bullish',
            'pred_snapshot': {'task_blueprint': {
                'category': 'bullish_watch',
                'entry_trigger': 235.0,
                'fail_level': 210.0,
            }},
        })
        self.assertIn('看多观察候选', html)
        self.assertIn('235.0', html)
        self.assertIn('210.0', html)
        self.assertNotIn('expected_range', html)

    def test_open_bullish_watch_rule_text_includes_trigger_fail_target_and_standard(self):
        text = format_open_review_rule_text({
            'code': '002421',
            'task_class': 'watch',
            'direction': 'bullish',
            'pred_snapshot': {'task_blueprint': {
                'category': 'bullish_watch',
                'entry_trigger': 4.659,
                'fail_level': 4.12,
                'triggered_target_pct': 6,
            }},
        })
        self.assertIn('触发价：4.659', text)
        self.assertIn('失效价：4.120', text)
        self.assertIn('触发后预计涨幅：+6.0%', text)
        self.assertIn('触发后按买入验证评分', text)
        self.assertIn('审判日评级标准', text)
        self.assertNotIn('bullish_watch', text)

    def test_open_converted_watch_falls_back_to_buy_rule(self):
        html = format_open_review_rule_html({
            'task_class': 'buy',
            'direction': 'bullish',
            'converted_from_watch': True,
            'pred_snapshot': {'task_blueprint': {'category': 'bullish_watch'}},
        })
        self.assertIn('看多单', html)
        self.assertNotIn('看多观察单', html)

    def test_reconcile_bullish_watch_restores_downgraded_neutral(self):
        from core.tracking_display import reconcile_view_to_category
        # 环境降级把看多观察打成中性震荡 → 按 category 还原
        self.assertEqual(
            reconcile_view_to_category('bullish_watch', '中性震荡', 'weak', 'neutral_watch', 'B'),
            ('看多观察', 'candidate', 'direction_watch'))
        self.assertEqual(
            reconcile_view_to_category('bullish_watch', '中性震荡', 'weak', 'neutral_watch', 'A'),
            ('临界看多', 'near_trigger', 'direction_watch'))

    def test_reconcile_watch_forces_neutral_label(self):
        from core.tracking_display import reconcile_view_to_category
        # grade B + neutral → 主映射给了看多观察，但 category=watch 必须显示中性震荡
        self.assertEqual(
            reconcile_view_to_category('watch', '看多观察', 'candidate', 'direction_watch', 'B'),
            ('中性震荡', 'weak', 'neutral_watch'))

    def test_reconcile_avoid_uses_avoidance_wording(self):
        from core.tracking_display import reconcile_view_to_category
        self.assertEqual(
            reconcile_view_to_category('avoid', '看空', 'weak', 'neutral_watch', 'D'),
            ('看跌回避', 'avoid', 'avoid_watch'))

    def test_reconcile_keeps_triggered_bullish_families(self):
        from core.tracking_display import reconcile_view_to_category
        # 已触发待变身的 bullish_watch 保留风险看多/trade，不算矛盾
        self.assertEqual(
            reconcile_view_to_category('bullish_watch', '风险看多', 'risk_triggered', 'trade', 'S'),
            ('风险看多', 'risk_triggered', 'trade'))
        # 已变身买入单残留旧标签 → 归一为看多
        self.assertEqual(
            reconcile_view_to_category('buy', '看多观察', 'candidate', 'direction_watch', 'B'),
            ('看多', 'triggered', 'trade'))

    def test_bullish_watch_plan_line_shows_trigger_not_range(self):
        line = format_prediction_plan_line({
            'task_class': 'watch',
            'direction': '看多观察',
            'horizon_days': 5,
            'pred_snapshot': {'task_blueprint': {
                'category': 'bullish_watch',
                'entry_trigger': 235.0,
                'fail_level': 210.0,
            }},
        })
        self.assertIn('看多观察', line)
        self.assertIn('235.0', line)
        self.assertIn('210.0', line)
        self.assertNotIn('预期区间', line)

    def test_bullish_watch_metric_line_uses_bullish_verdict(self):
        lines = format_directional_metric_lines(
            {'task_class': 'watch', 'grade': 'B', 'final_pct': 3.0},
            {'pred_snapshot': {'task_blueprint': {'category': 'bullish_watch'}}},
        )
        self.assertIn('看多观察', lines[0])
        self.assertIn('等对了', lines[0])
        self.assertNotIn('躲过下跌', lines[0])

    def test_bearish_advice_line_uses_avoid_fields(self):
        lines = format_prediction_advice_lines(
            {'direction': 'bearish', 'target_pct': 6, 'stop_pct': 3},
            period_label='短线（5 日）',
            rating_text='观察',
            entry_text='10.00',
            suit_text='中→',
            action_text='仅观望',
        )
        joined = '\n'.join(lines)
        self.assertIn('看跌回避', joined)
        self.assertIn('回避基准', joined)
        self.assertIn('下跌风险', joined)
        self.assertNotIn('入场参考', joined)
        self.assertNotIn('止损:', joined)

    def test_copy_fields_keep_full_copy_but_use_direction_labels(self):
        fields = format_prediction_copy_field_lines({
            'direction': 'bearish',
            'entry_ref': 10,
            'target_pct': 6,
            'stop_pct': 3,
            'horizon_days': 5,
        })
        joined = '\n'.join(fields)
        self.assertIn('回避基准', joined)
        self.assertIn('下跌风险幅度', joined)
        self.assertNotIn('目标涨跌幅', joined)
        self.assertNotIn('止损幅度', joined)

    def test_agent_hit_lines_surface_agent_responsibility(self):
        lines = format_agent_hit_lines({
            'agent_hit_map': {
                'technical': {'label': '技术 Agent', 'status': '命中', 'stance': 'bearish'},
                'fundflow': {'label': '资金 Agent', 'status': '误导', 'stance': 'bullish'},
            }
        })
        joined = '\n'.join(lines)
        self.assertIn('技术 Agent: 命中', joined)
        self.assertIn('资金 Agent: 误导', joined)

    def test_no_position_retrospective_metrics_do_not_use_trade_return_terms(self):
        text = format_retrospective_metric_lines(
            {'task_class': 'avoid', 'final_pct': -4.2, 'peak_pct': 1.1,
             'trough_pct': -5.0, 'execution_quality': '回避正确'},
            {'task_class': 'avoid', 'direction': 'bearish'},
        )
        self.assertIn('真实涨跌', text)
        self.assertIn('回避正确', text)
        self.assertNotIn('收益捕获率', text)
        self.assertNotIn('R 倍数', text)

    def test_skill_kpi_shows_spread_and_neutral_excess(self):
        html = format_skill_kpi_html({
            'benchmark': -3.22, 'unit_count': 67, 'spread': 2.15,
            'buckets': {
                'buy': {'n': 9, 'mean': -3.16, 'excess': 0.07},
                'avoid': {'n': 18, 'mean': -5.31, 'excess': -2.08},
                'watch': {'n': 40, 'mean': -2.30, 'excess': 0.92},
            },
        })
        self.assertIn('系统判断力', html)
        self.assertIn('+2.15%', html)
        self.assertIn('#2ecc71', html)
        self.assertIn('基准组合均值 -3.2%', html)
        self.assertIn('#8899bb', html)

    def test_skill_kpi_market_benchmark_labels_index(self):
        html = format_skill_kpi_html({
            'benchmark': -2.30, 'benchmark_kind': 'market', 'unit_count': 67, 'spread': 1.2,
            'buckets': {
                'buy': {'n': 9, 'mean': 1.5, 'excess': 1.5},
                'avoid': {'n': 18, 'mean': 0.3, 'excess': 0.3},
                'watch': {'n': 40, 'mean': 0.8, 'excess': 0.8},
            },
        })
        self.assertIn('基准中证全指 -2.3%', html)
        self.assertIn('超额', html)

    def test_skill_kpi_negative_spread_is_red(self):
        html = format_skill_kpi_html({
            'benchmark': -1.0, 'unit_count': 10, 'spread': -1.5,
            'buckets': {
                'buy': {'n': 5, 'mean': -2.0, 'excess': -1.0},
                'avoid': {'n': 5, 'mean': -0.5, 'excess': 0.5},
                'watch': {'n': 0, 'mean': None, 'excess': None},
            },
        })
        self.assertIn('#e74c3c', html)
        self.assertIn('-1.50%', html)
        self.assertIn('—', html)

    def test_skill_kpi_accumulating_when_insufficient(self):
        html = format_skill_kpi_html({
            'benchmark': -2.0, 'unit_count': 1, 'spread': None,
            'buckets': {
                'buy': {'n': 1, 'mean': -2.0, 'excess': None},
                'avoid': {'n': 0, 'mean': None, 'excess': None},
                'watch': {'n': 0, 'mean': None, 'excess': None},
            },
        })
        self.assertIn('系统判断力', html)
        self.assertIn('积累中', html)


if __name__ == '__main__':
    unittest.main()
