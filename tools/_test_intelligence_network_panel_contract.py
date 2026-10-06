import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui.intelligence_network_panel import IntelligenceNetworkPanel
from ui.main_window import MainWindow
from ui.evidence_graph_view import EvidenceGraphView


class IntelligenceNetworkPanelContractTest(unittest.TestCase):
    def test_panel_preserves_intel_panel_public_api(self):
        for attr in ('status_changed', 'point_count_changed', 'bottom_status_changed'):
            self.assertTrue(hasattr(IntelligenceNetworkPanel, attr), attr)
        for method in ('refresh', 'auto_refresh', 'reapply_font', 'current_point_count'):
            self.assertTrue(hasattr(IntelligenceNetworkPanel, method), method)

    def test_main_window_uses_intelligence_network_panel_for_intel_tab(self):
        source = inspect.getsource(MainWindow._build_deferred_panel)
        self.assertIn('IntelligenceNetworkPanel', source)
        self.assertIn('panel = IntelligenceNetworkPanel(self)', source)
        panel_source = inspect.getsource(IntelligenceNetworkPanel._setup_ui)
        self.assertIn("addItems(['效率', '性能'])", panel_source)
        self.assertNotIn('\u7701\u7535', panel_source)
        self.assertNotIn('\u5747\u8861', panel_source)
        self.assertNotIn('\u9707\u64bc', panel_source)

    def test_graph_view_prepares_dense_visual_layer_without_polluting_evidence(self):
        graph = {
            'nodes': [
                {'id': 'stock:600584', 'name': '长电科技', 'type': 'stock', 'layer': 'company', 'val': 26},
                {'id': 'sector:chip', 'name': '先进封装', 'type': 'sector', 'layer': 'sector', 'val': 14},
                {'id': 'missing:600584:cost', 'name': '成本证据缺失', 'type': 'missing_evidence', 'layer': 'cost', 'forecast_role': 'missing', 'val': 8},
            ],
            'links': [
                {'source': 'sector:chip', 'target': 'stock:600584', 'layer': 'sector', 'value': 2.4},
                {'source': 'missing:600584:cost', 'target': 'stock:600584', 'layer': 'cost', 'relation': 'missing', 'value': 1.2},
            ],
            'meta': {'subject_code': '600584', 'subject_name': '长电科技', 'node_count': 3, 'link_count': 2},
        }

        prepared = EvidenceGraphView.prepare_visual_graph(graph, '效率')
        real_nodes = [node for node in prepared['nodes'] if not node.get('visual_only')]
        visual_nodes = [node for node in prepared['nodes'] if node.get('visual_only')]
        visual_links = [link for link in prepared['links'] if link.get('visual_only')]

        self.assertEqual(graph['meta']['node_count'], 3)
        self.assertEqual(graph['meta']['link_count'], 2)
        self.assertEqual(len(real_nodes), 3)
        self.assertGreaterEqual(len(visual_nodes), 55)
        self.assertLessEqual(len(visual_nodes), 90)
        self.assertGreaterEqual(len(visual_links), 75)
        self.assertLessEqual(len(visual_links), 120)
        self.assertTrue(all(str(node['id']).startswith('visual:') for node in visual_nodes))
        self.assertTrue(all(node['type'] == 'visual_particle' for node in visual_nodes))
        self.assertTrue(all(link['type'] == 'visual_link' for link in visual_links))
        missing_node = next(node for node in real_nodes if node['type'] == 'missing_evidence')
        self.assertEqual(missing_node['layer'], 'missing')
        self.assertLessEqual(max(float(node.get('val') or 0) for node in real_nodes), 14)
        self.assertLessEqual(max(float(node.get('val') or 0) for node in visual_nodes), 1.5)
        self.assertEqual(prepared['meta']['node_count'], 3)
        self.assertEqual(prepared['meta']['link_count'], 2)
        self.assertEqual(prepared['meta']['visual_node_count'], len(visual_nodes))

    def test_performance_mode_builds_deep_spherical_shell_and_cluster_map(self):
        layers = [
            ('sector:chip', 'sector', 'sector'),
            ('macro:market', 'macro', 'macro'),
            ('financial:summary', 'financial_metric', 'financial'),
            ('flow:main', 'fund_flow', 'trading_behavior'),
            ('news:event', 'news', 'news_event'),
            ('risk:item', 'risk', 'risk'),
            ('missing:item', 'missing_evidence', 'cost'),
            ('forecast:item', 'event', 'forecast'),
        ]
        graph = {
            'nodes': [{'id': 'stock:600584', 'name': '长电科技', 'type': 'stock', 'layer': 'company'}] + [
                {'id': node_id, 'name': node_id, 'type': node_type, 'layer': layer, 'forecast_role': 'forecast' if layer == 'forecast' else ''}
                for node_id, node_type, layer in layers
            ],
            'links': [
                {'source': node_id, 'target': 'stock:600584', 'layer': layer, 'value': 2.0}
                for node_id, _node_type, layer in layers
            ],
            'meta': {'subject_code': '600584', 'subject_name': '长电科技', 'node_count': 9, 'link_count': 8},
        }

        prepared = EvidenceGraphView.prepare_visual_graph(graph, '性能')
        visual_nodes = [node for node in prepared['nodes'] if node.get('visual_only')]
        real_nodes = [node for node in prepared['nodes'] if not node.get('visual_only')]
        xs = [float(node['x']) for node in visual_nodes]
        ys = [float(node['y']) for node in visual_nodes]
        zs = [float(node['z']) for node in visual_nodes]
        bands = {node.get('shell_band') for node in visual_nodes}
        clusters = {node.get('cluster_key') for node in real_nodes if node.get('type') != 'stock'}

        self.assertGreaterEqual(prepared['meta']['visual_node_count'], 320)
        self.assertGreater(max(xs) - min(xs), 360)
        self.assertGreater(max(ys) - min(ys), 360)
        self.assertGreater(max(zs) - min(zs), 360)
        self.assertGreaterEqual(len(bands), 6)
        self.assertLessEqual(prepared['meta']['camera_distance'], 520)
        self.assertGreaterEqual(prepared['meta']['globe_screen_ratio'], 0.70)
        self.assertTrue(all('depth_alpha' in node for node in visual_nodes))
        self.assertTrue({'external', 'industry', 'company', 'trading', 'news', 'risk', 'missing', 'forecast'} <= clusters)
        self.assertEqual(prepared['meta']['performance_mode'], '性能')


if __name__ == '__main__':
    unittest.main()
