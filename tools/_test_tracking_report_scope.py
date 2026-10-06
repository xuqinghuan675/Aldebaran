import unittest
from unittest.mock import patch

from core import tracking


def _task(
    task_id,
    *,
    scope,
    grade,
    final_pct,
    r_multiple=None,
    flags=None,
    path_quality='neutral',
    agent_status='命中',
):
    detail = {
        'final_pct': final_pct,
        'path_quality': path_quality,
        'agent_hit_map': {
            'technical': {'label': '技术 Agent', 'status': agent_status},
        },
    }
    if r_multiple is not None:
        detail['r_multiple'] = r_multiple
        detail['mae_r'] = -abs(r_multiple)
        detail['mfe_r'] = max(r_multiple, 0)
    return {
        'id': task_id,
        'status': 'closed',
        'grade': grade,
        'kind': 'stock',
        'sample_type': 'watch',
        'created_at': '2026-06-01T09:30:00',
        'closed_at': '2026-06-06T15:05:00',
        'code': task_id[-6:],
        'name': task_id,
        'direction': 'neutral' if scope == 'neutral_watch' else 'bullish',
        'final_action': 'buy' if scope == 'trade' else 'watch_only',
        'risk_flags': flags or [],
        'decision_view': {
            'evaluation_scope': scope,
            'bias_label': scope,
            'action_label': scope,
        },
        'pred_snapshot': {
            'setup_type': 'trend_continuation',
            'opportunity_level': 'S' if scope == 'trade' else 'C',
        },
        'grade_detail': detail,
    }


class TrackingReportScopeTest(unittest.TestCase):
    def _build_report(self):
        tasks = [
            _task('trade_win', scope='trade', grade='A', final_pct=12, r_multiple=3.0,
                  flags=['技术过热'], path_quality='P_tested'),
            _task('trade_loss', scope='trade', grade='F', final_pct=-10, r_multiple=-2.0,
                  flags=['技术过热'], path_quality='P_stressed', agent_status='误导'),
            _task('neutral_miss', scope='neutral_watch', grade='F', final_pct=9,
                  flags=['中性观望', '置信度不足'], agent_status='误导'),
            _task('neutral_dodge', scope='neutral_watch', grade='A', final_pct=-4,
                  flags=['中性观望']),
            _task('direction_valid', scope='direction_watch', grade='B', final_pct=7,
                  flags=['未触发观察'], agent_status='误导'),
            _task('direction_failed', scope='direction_watch', grade='D', final_pct=-6,
                  flags=['未触发观察']),
            _task('avoid_ok', scope='avoid_watch', grade='A', final_pct=-5,
                  flags=['观望信号']),
            _task('avoid_fail', scope='avoid_watch', grade='F', final_pct=6,
                  flags=['观望信号']),
        ]
        with patch.object(tracking, 'load_tasks', return_value=tasks), \
                patch.object(tracking, 'load_tracking_events', return_value=[]), \
                patch.object(tracking, '_load_alert_logs', return_value=[]):
            return tracking.build_tracking_report_data({'include_records': True})

    def test_report_splits_trade_watch_and_avoid_scopes(self):
        report = self._build_report()
        scopes = report['performance_by_scope']

        self.assertEqual(scopes['trade']['sample_count'], 2)
        self.assertEqual(scopes['trade']['r_count'], 2)
        self.assertEqual(scopes['trade']['avg_r'], 0.5)
        self.assertEqual(scopes['trade']['median_r'], 0.5)
        self.assertEqual(scopes['neutral_watch']['big_miss_count'], 1)
        self.assertEqual(scopes['direction_watch']['upside_validated_count'], 1)
        self.assertEqual(scopes['direction_watch']['direction_failed_count'], 1)
        self.assertEqual(scopes['avoid_watch']['avoid_failed_count'], 1)
        self.assertIn('P_stressed', scopes['trade']['problem_counts'])

    def test_candidates_keep_scope_and_agent_layers_visible(self):
        report = self._build_report()
        candidate_ids = {item['id'] for item in report['improvement_candidates']}

        self.assertIn('scope_trade_path_stress', candidate_ids)
        self.assertIn('scope_neutral_watch_big_miss', candidate_ids)
        self.assertIn('prompt_rule_agent_技术 Agent', candidate_ids)
        self.assertGreater(len(report['layered_diagnostics']), 3)


    def test_candidates_are_not_artificially_capped(self):
        report = {
            'layered_diagnostics': [
                {
                    'id': f'scope_issue_{idx}',
                    'title': f'Issue {idx}',
                    'count': 1,
                    'sample_ids': [f'sample_{idx}'],
                    'severity': 'medium',
                    'suggestion': 'Review this layer.',
                }
                for idx in range(13)
            ],
            'performance_by_agent': {},
            'risk_diagnostics': {},
            'error_analysis': {'by_root_cause': {}},
        }

        candidate_ids = {item['id'] for item in tracking._improvement_candidates(report)}

        self.assertEqual(len(candidate_ids), 13)
        self.assertIn('scope_issue_12', candidate_ids)

    def test_layered_doc_section_lists_all_diagnostics(self):
        class FakeDoc:
            def __init__(self):
                self.paragraphs = []

            def add_paragraph(self, text=''):
                self.paragraphs.append(text)

        diagnostics = [
            {
                'layer': 'Layer',
                'title': f'Issue {idx}',
                'count': 1,
                'sample_ids': [f'sample_{idx}'],
                'suggestion': 'Review this layer.',
            }
            for idx in range(13)
        ]
        doc = FakeDoc()

        tracking._doc_add_layered_diagnostics(doc, diagnostics)

        self.assertEqual(len(doc.paragraphs), 13)
        self.assertIn('Issue 12', doc.paragraphs[-1])


if __name__ == '__main__':
    unittest.main()
